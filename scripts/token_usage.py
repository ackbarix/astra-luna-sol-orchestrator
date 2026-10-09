#!/usr/bin/env python3
# Adapted from donvito/codex-astra-luna-orchestrator/scripts/token_usage.py.
# Copyright 2026 contributors. Apache-2.0: see licenses/Apache-2.0.txt.
# ALS 0.2: per-response model/effort attribution; cross-day lineage;
# explicit uncertainty for legacy counters; separate session/day selection.
"""Read-only Codex usage report. Not a bill, benchmark or upstream routing proof."""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
UTC = timezone.utc
MAX_LINE = 8 * 1024 * 1024


def empty_usage():
    return dict.fromkeys(KEYS, 0)


def add_usage(target, usage):
    for key in KEYS:
        target[key] += usage[key]


def parse_ts(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, AttributeError, TypeError):
        return None


def iso(value):
    return value.isoformat() if value else None


def thread_role(meta):
    source = meta.get("source")
    if isinstance(source, dict):
        sub = source.get("subagent")
        if isinstance(sub, str):
            return sub, ""
        if isinstance(sub, dict):
            spawn = sub.get("thread_spawn")
            if isinstance(spawn, dict):
                return spawn.get("agent_role") or "subagent", spawn.get("agent_nickname") or ""
            if sub.get("other"):
                return str(sub["other"]), ""
    if meta.get("thread_source"):
        return str(meta["thread_source"]), ""
    is_child = meta.get("parent_thread_id") or (meta.get("session_id") and meta.get("session_id") != meta.get("id"))
    return ("subagent", "") if is_child else ("root", "")


def guardian(role):
    return role in {"guardian", "guardian_review"}


def objects(path, warnings):
    """Bound individual lines; never echo raw prompts, credentials or tool output."""
    try:
        with path.open("rb") as stream:
            lineno = 0
            while True:
                data = stream.readline(MAX_LINE + 1)
                if not data:
                    break
                lineno += 1
                if len(data) > MAX_LINE:
                    while data and not data.endswith(b"\n"):
                        data = stream.readline(MAX_LINE + 1)
                    warnings.append(f"oversized_line:{path.name}:{lineno}")
                    continue
                if not data.strip():
                    continue
                try:
                    value = json.loads(data.decode("utf-8-sig"))
                except (UnicodeError, json.JSONDecodeError):
                    warnings.append(f"unreadable_line:{path.name}:{lineno}")
                    continue
                if isinstance(value, dict):
                    yield value
                else:
                    warnings.append(f"non_object_record:{path.name}:{lineno}")
    except OSError:
        warnings.append(f"unreadable_file:{path.name}")


def index_sessions(directory, warnings):
    """Scan metadata first. Date NEVER prunes lineage by storage folder."""
    nodes = {}
    for path in sorted(directory.rglob("rollout-*.jsonl")):
        if path.is_symlink():
            warnings.append(f"symlink_skipped:{path.name}")
            continue
        iterator = objects(path, warnings)
        first = next(iterator, None)
        iterator.close()
        if not first or first.get("type") != "session_meta" or not isinstance(first.get("payload"), dict):
            warnings.append(f"missing_session_meta:{path.name}")
            continue
        meta = first["payload"]
        ident = meta.get("id")
        if not isinstance(ident, str) or not ident:
            warnings.append(f"missing_thread_id:{path.name}")
            continue
        node = nodes.setdefault(ident, {"meta": meta, "paths": []})
        node["paths"].append(path)
        if node["meta"].get("session_id") != meta.get("session_id"):
            warnings.append(f"conflicting_lineage:{ident}")
    def parent(meta):
        source = meta.get("source")
        sub = source.get("subagent", {}) if isinstance(source, dict) else {}
        spawn = sub.get("thread_spawn", {}) if isinstance(sub, dict) else {}
        return meta.get("parent_thread_id") or (spawn.get("parent_thread_id") if isinstance(spawn, dict) else None)
    def root_for(ident):
        visited = set()
        while ident in nodes:
            if ident in visited:
                warnings.append(f"cyclic_lineage:{ident}")
                return ident
            visited.add(ident)
            meta = nodes[ident]["meta"]
            session = meta.get("session_id")
            next_id = session if isinstance(session, str) and session != ident else parent(meta)
            if not next_id or next_id == ident:
                return ident
            ident = next_id
        return ident
    groups = defaultdict(list)
    for ident, node in nodes.items():
        groups[root_for(ident)].append(node)
    return dict(groups)


def normalized_usage(raw, warnings):
    if not isinstance(raw, dict) or not all(k in raw for k in ("input_tokens", "output_tokens")):
        warnings.append("incomplete_usage_record")
        return None
    data = {}
    for key in KEYS:
        value = raw.get(key)
        if value is None:
            if key == "total_tokens":
                value = data["input_tokens"] + data["output_tokens"]
            else:
                value = 0
                warnings.append("not_reported:" + key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            warnings.append("invalid_usage:" + key)
            return None
        data[key] = value
    if data["cached_input_tokens"] > data["input_tokens"] or data["reasoning_output_tokens"] > data["output_tokens"]:
        warnings.append("invalid_usage_subset")
        return None
    if data["total_tokens"] != data["input_tokens"] + data["output_tokens"]:
        warnings.append("reported_total_differs_from_input_plus_output")
    return data


def analyze_node(node):
    meta, warnings, events = node["meta"], [], []
    context, contexts, last_total, rate_first, rate_last = {}, {}, None, None, None
    observed, seen_ids, replay = set(), {}, {}
    first_ts, last_ts = parse_ts(meta.get("timestamp")), parse_ts(meta.get("timestamp"))
    previous_counter, counter_reset = None, False
    for path in node["paths"]:
        # A resumed file may not repeat context immediately; retain current context.
        for obj in objects(path, warnings):
            stamp = parse_ts(obj.get("timestamp"))
            if stamp:
                first_ts = min(first_ts, stamp) if first_ts else stamp
                last_ts = max(last_ts, stamp) if last_ts else stamp
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = obj.get("type")
            if kind == "turn_context":
                context = {"model": payload.get("model") or "unknown",
                           "effort": payload.get("effort") or payload.get("reasoning_effort") or "unknown"}
                if payload.get("turn_id"):
                    contexts[payload["turn_id"]] = context.copy()
                observed.add((context["model"], context["effort"]))
            is_usage = kind == "token_usage_record" or (kind == "event_msg" and payload.get("type") == "token_usage_record")
            if is_usage:
                started = parse_ts(meta.get("timestamp"))
                if stamp and started and stamp < started:
                    warnings.append("pre_session_response_ignored:possible_inherited_history")
                    continue
                usage = normalized_usage(payload.get("usage"), warnings)
                if usage is None:
                    continue
                active = contexts.get(payload.get("turn_id"), context)
                model = payload.get("model") or active.get("model", "unknown")
                effort = payload.get("effort") or payload.get("reasoning_effort") or (active.get("effort", "unknown") if model == active.get("model") else "unknown")
                event = {"timestamp": iso(stamp), "model": model, "effort": effort, "usage": usage,
                         "source": "per_response", "response_id": payload.get("response_id") or payload.get("id")}
                if not isinstance(model, str) or not isinstance(effort, str):
                    warnings.append("invalid_identity_record")
                    continue
                identity = event["response_id"] if isinstance(event["response_id"], str) else None
                fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
                if identity:
                    if identity in seen_ids:
                        if seen_ids[identity] != fingerprint:
                            warnings.append("conflicting_response_id:" + str(identity))
                        continue
                    seen_ids[identity] = fingerprint
                elif stamp:
                    if fingerprint in replay and replay[fingerprint] != path:
                        warnings.append("cross_file_replay_deduplicated")
                        continue
                    replay[fingerprint] = path
                if model == "unknown" or effort == "unknown":
                    warnings.append("unknown_recorded_identity")
                if stamp is None:
                    warnings.append("missing_response_timestamp")
                events.append(event)
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                raw = info.get("total_token_usage") if isinstance(info, dict) else None
                if raw:
                    current = normalized_usage(raw, warnings)
                    if current:
                        if previous_counter and current["total_tokens"] < previous_counter["total_tokens"]:
                            counter_reset = True
                        previous_counter = current
                        last_total = {"timestamp": iso(stamp), "usage": current}
                if isinstance(payload.get("rate_limits"), dict):
                    rate_first = rate_first or payload["rate_limits"]
                    rate_last = payload["rate_limits"]
    if counter_reset:
        warnings.append("cumulative_counter_reset_or_resume")
    if not events and last_total:
        warnings.append("legacy_cumulative_only:attribution_and_increment_unverified")
    if not events and not last_total:
        warnings.append("no_usage_records")
    if events and last_total:
        total = sum(e["usage"]["total_tokens"] for e in events)
        if total != last_total["usage"]["total_tokens"]:
            warnings.append("cumulative_and_response_totals_differ:no_residual_added")
    role, nickname = thread_role(meta)
    return {"id": meta["id"], "role": role, "nickname": nickname, "cwd": meta.get("cwd"),
            "parent_thread_id": meta.get("parent_thread_id"), "cli_version": meta.get("cli_version"),
            "started": iso(parse_ts(meta.get("timestamp"))), "first_observed": iso(first_ts), "last_observed": iso(last_ts),
            "recorded_identities": [{"model": m, "effort": e} for m, e in sorted(observed)],
            "events": events, "cumulative_snapshot": last_total, "warnings": sorted(set(warnings)),
            "rate_first": rate_first, "rate_last": rate_last}


def summarize_thread(thread, day=None, tz=UTC, include_events=False):
    events = thread["events"]
    if day:
        events = [e for e in events if parse_ts(e["timestamp"]) and parse_ts(e["timestamp"]).astimezone(tz).date().isoformat() == day]
    rows, per_model, responses = {}, defaultdict(empty_usage), defaultdict(int)
    total = empty_usage()
    for event in events:
        key = (event["model"], event["effort"])
        row = rows.setdefault(key, {"model": key[0], "effort": key[1], "responses": 0, "usage": empty_usage()})
        add_usage(row["usage"], event["usage"])
        add_usage(per_model[key[0]], event["usage"])
        add_usage(total, event["usage"])
        row["responses"] += 1
        responses[key[0]] += 1
    result = {k: v for k, v in thread.items() if k != "events"}
    result.update({"per_model_effort": list(rows.values()), "per_model": dict(per_model), "responses": dict(responses),
                   "confirmed_usage": total, "confirmed_responses": len(events)})
    # Counters can include inherited/resumed history; never allocate to last model.
    result["unattributed_snapshot"] = thread["cumulative_snapshot"] if not thread["events"] else None
    if include_events:
        result["events"] = events
    return result


def build_report(root_id, threads, include_guardian=False, day=None, tz=UTC, include_events=False, warnings=None):
    result, totals, aggregated = [], empty_usage(), {}
    notes = list(warnings or [])
    for thread in threads:
        item = summarize_thread(thread, day, tz, include_events)
        item["counted"] = include_guardian or not guardian(item["role"])
        if item["counted"]:
            add_usage(totals, item["confirmed_usage"])
            notes.extend(f'{item["id"][:8]}:{w}' for w in item["warnings"])
            for row in item["per_model_effort"]:
                key = (row["model"], row["effort"])
                dest = aggregated.setdefault(key, {"model": key[0], "effort": key[1], "threads": 0, "responses": 0, "usage": empty_usage()})
                dest["threads"] += 1
                dest["responses"] += row["responses"]
                add_usage(dest["usage"], row["usage"])
        result.append(item)
    result.sort(key=lambda x: (x["role"] != "root", x["started"] or "", x["id"]))
    return {"schema_version": 2, "root": root_id, "scope": "response_day" if day else "full_session",
            "day": day, "timezone": str(tz), "threads": result, "by_model_effort": list(aggregated.values()),
            "confirmed_usage": totals, "warnings": sorted(set(notes)), "upstream_routing_verified": False,
            "billing_verified": False, "legacy_snapshots_excluded_from_totals": True,
            "complete": not notes}


def render(report):
    lines = [f'## Codex usage: {report["root"] or report["day"]}',
             f'Scope: {report["scope"]}; timezone: {report["timezone"]}',
             'Recorded model/effort only. Cache is part of input; reasoning is part of output.',
             '', '| Thread / role | Model | Effort | Responses | Uncached in | Cached in | Output | Reasoning subset | Total |',
             '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for thread in report["threads"]:
        if not thread["counted"]:
            continue
        for row in thread["per_model_effort"]:
            u = row["usage"]
            lines.append(f'| {thread["id"][:8]} / {thread["role"]} | {row["model"]} | {row["effort"]} | {row["responses"]} | {u["input_tokens"]-u["cached_input_tokens"]:,} | {u["cached_input_tokens"]:,} | {u["output_tokens"]:,} | {u["reasoning_output_tokens"]:,} | {u["total_tokens"]:,} |')
        if thread["unattributed_snapshot"]:
            number = thread["unattributed_snapshot"]["usage"]["total_tokens"]
            lines.append(f'| {thread["id"][:8]} / {thread["role"]} | UNATTRIBUTED cumulative snapshot | unknown | - | - | - | - | - | {number:,} (NOT counted) |')
    lines += ['', '| Model aggregate | Effort | Threads | Responses | Input | Cache subset | Output | Reasoning subset | Total |',
              '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for row in report['by_model_effort']:
        u = row['usage']
        lines.append(f'| {row["model"]} | {row["effort"]} | {row["threads"]} | {row["responses"]} | {u["input_tokens"]:,} | {u["cached_input_tokens"]:,} | {u["output_tokens"]:,} | {u["reasoning_output_tokens"]:,} | {u["total_tokens"]:,} |')
    total = report["confirmed_usage"]
    lines += ['', f'Confirmed per-response total: {total["total_tokens"]:,}',
              'Legacy cumulative snapshots are NOT part of this total; this may be a lower bound.']
    if total["input_tokens"]:
        lines.append(f'Cache hit rate on reported input: {100*total["cached_input_tokens"]/total["input_tokens"]:.1f}%')
    if report["warnings"]:
        lines += ['', 'Warnings / incomplete evidence:', *['- ' + w for w in report['warnings']]]
    lines += ['', 'Not actual charges or quota consumption. Observed thread spans do not prove parallel execution.']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-dir", default=str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"))
    parser.add_argument("--cwd", help="Root project directory filter")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--list", action="store_true")
    selector.add_argument("--root", help="Root ID or unique prefix: includes ALL dates")
    selector.add_argument("--latest", action="store_true", help="Latest started session, INCLUDING root-only")
    selector.add_argument("--latest-with-workers", action="store_true", help="Legacy latest delegated session behavior")
    selector.add_argument("--day", help="Response timestamps on this day (YYYY-MM-DD); independent of log folders")
    parser.add_argument("--date", help="Filter by ROOT START date, but retain full session lineage (changed in 0.2)")
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--include-guardian", action="store_true")
    parser.add_argument("--include-events", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Exit 3 when report has warnings/incomplete evidence")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--format", choices=("md", "json"), default="md")
    args = parser.parse_args(argv)
    try:
        tz = ZoneInfo(args.timezone)
        for value in (args.date, args.day):
            if value and datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
                raise ValueError("Expected YYYY-MM-DD")
    except (ValueError, ZoneInfoNotFoundError) as ex:
        parser.error(str(ex))
    if args.day and args.date:
        parser.error("Use --day OR --date; daily usage and full session selection are different")
    if args.limit < 1:
        parser.error("--limit must be positive")
    directory = Path(args.sessions_dir).expanduser()
    if not directory.is_dir():
        print("Sessions directory not found", file=sys.stderr)
        return 2
    warnings = []
    sessions = index_sessions(directory, warnings)
    chosen = {}
    for root, nodes in sessions.items():
        root_node = next((n for n in nodes if n["meta"].get("id") == root), None)
        if root_node is None:
            warnings.append("missing_root_metadata:" + str(root))
            if args.cwd or args.date:
                continue
        meta = root_node["meta"] if root_node else {}
        if args.cwd and Path(meta.get("cwd") or "/").resolve() != Path(args.cwd).expanduser().resolve():
            continue
        started = parse_ts(meta.get("timestamp"))
        if args.date and (not started or started.astimezone(tz).date().isoformat() != args.date):
            continue
        chosen[root] = nodes
    if args.root:
        matches = [root for root in chosen if root.startswith(args.root)]
        if len(matches) != 1:
            print(f'--root matched {len(matches)} sessions; use a unique ID', file=sys.stderr)
            return 2
        chosen = {matches[0]: chosen[matches[0]]}
    if not chosen:
        print("No matching sessions", file=sys.stderr)
        return 1
    def started_key(pair):
        root, nodes = pair
        meta = next((n["meta"] for n in nodes if n["meta"].get("id") == root), {})
        return (parse_ts(meta.get("timestamp")) or datetime.min.replace(tzinfo=UTC), root)
    if args.list:
        listing = []
        for root, nodes in sorted(chosen.items(), key=started_key, reverse=True)[:args.limit]:
            meta = next((n["meta"] for n in nodes if n["meta"].get("id") == root), {})
            listing.append({"root": root, "started": meta.get("timestamp"), "cwd": meta.get("cwd"),
                            "subagents": sum(n["meta"].get("id") != root and not guardian(thread_role(n["meta"])[0]) for n in nodes)})
        if args.format == "json":
            print(json.dumps({"sessions": listing, "warnings": warnings}, indent=2))
        else:
            for row in listing:
                print(f'{row["started"]} {row["root"]} workers={row["subagents"]} {row["cwd"]}')
            for warning in warnings:
                print("WARNING: " + warning, file=sys.stderr)
        return 3 if args.strict and warnings else 0
    if args.latest or args.latest_with_workers:
        candidates = chosen
        if args.latest_with_workers:
            candidates = {r: ns for r, ns in chosen.items() if any(n["meta"].get("id") != r and not guardian(thread_role(n["meta"])[0]) for n in ns)}
        if not candidates:
            print("No session with workers found", file=sys.stderr)
            return 1
        root, nodes = max(candidates.items(), key=started_key)
        chosen = {root: nodes}
    threads = [analyze_node(n) for ns in chosen.values() for n in ns]
    root = next(iter(chosen)) if len(chosen) == 1 and not args.day else None
    report = build_report(root, threads, args.include_guardian, args.day, tz, args.include_events, warnings)
    if args.date:
        report["root_started_on"] = args.date
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.format == "json" else render(report))
    return 3 if args.strict and report["warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
