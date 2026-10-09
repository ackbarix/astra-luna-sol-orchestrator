#!/usr/bin/env python3
"""Read-only ALS config/probe/report check. Never starts a paid model request."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ required")
import tomllib

EXPECTED = {
    "als_explorer": ("gpt-6-luna", "medium"),
    "als_researcher": ("gpt-6-luna", "medium"),
    "als_tester": ("gpt-6-luna", "medium"),
    "als_worker": ("gpt-6-luna", "max"),
    "als_test_designer": ("gpt-6-luna", "max"),
    "als_hard_worker": ("gpt-6.1-sol", "high"),
    "als_reviewer": ("gpt-6.1-sol", "medium"),
    "als_reviewer_deep": ("gpt-6.1-sol", "high"),
}
SMOKE_ROLES = ("als_explorer", "als_worker", "als_hard_worker", "als_reviewer")


def load_toml(path):
    with path.open("rb") as stream:
        return tomllib.load(stream)


def inspect_config(project):
    problems, values = [], {}
    try:
        config = load_toml(project / ".codex/config.toml")
        values["head"] = [config.get("model"), config.get("model_reasoning_effort")]
        if values["head"] != ["gpt-6-astra", "medium"]:
            problems.append("HEAD config does not match Astra Medium")
        agents = config.get("agents", {})
        for key, expected in {"enabled": True, "max_concurrent_threads_per_session": 6,
                              "max_depth": 1, "default_subagent_model": "gpt-6-luna",
                              "default_subagent_reasoning_effort": "max"}.items():
            if agents.get(key) != expected:
                problems.append("Unexpected agents." + key)
        for role, expected in EXPECTED.items():
            data = load_toml(project / ".codex/agents" / (role + ".toml"))
            actual = (data.get("model"), data.get("model_reasoning_effort"))
            values[role] = actual
            if data.get("name") != role or actual != expected:
                problems.append("Role mismatch: " + role)
    except (OSError, ValueError) as ex:
        problems.append("Cannot inspect project config: " + str(ex))
    return values, problems


def verify_report(data):
    """Validate recorded usage identity, NOT model self-description/upstream routing."""
    problems, observed = [], {}
    if data.get("schema_version") != 2 or data.get("scope") != "full_session":
        return {}, ["Use an ALS schema-v2 full-session JSON report (--root, not --day)"]
    for thread in data.get("threads", []):
        for row in thread.get("per_model_effort", []):
            if row.get("responses", 0) > 0:
                observed.setdefault(thread.get("role"), set()).add((row.get("model"), row.get("effort")))
    expected = {role: EXPECTED[role] for role in SMOKE_ROLES}
    expected["root"] = ("gpt-6-astra", "medium")
    for role, pair in expected.items():
        if pair not in observed.get(role, set()):
            problems.append("Missing recorded response identity: " + role + " / " + "/".join(pair))
        if observed.get(role, set()) - {pair}:
            problems.append("Unexpected recorded identity for role: " + role)
    if data.get("warnings"):
        problems.append("Usage report contains incomplete/ambiguous evidence warnings")
    return {k: sorted(v) for k, v in observed.items()}, problems


def probe():
    values, warnings = {}, []
    commands = {"codex_version": ["codex", "--version"],
                "ocx_surface": ["ocx", "v2", "status", "--json"],
                "ocx_effort": ["ocx", "effort", "status", "--json"]}
    for name, cmd in commands.items():
        if not shutil.which(cmd[0]):
            warnings.append(cmd[0] + " missing from PATH")
            continue
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if result.returncode:
                warnings.append(name + " probe failed; inspect locally")
            elif name == "codex_version":
                values[name] = result.stdout.strip()[:160]
            else:
                data = json.loads(result.stdout)
                found = {}
                def pick(item):
                    if isinstance(item, dict):
                        for key, value in item.items():
                            if key in {"effortCap", "subagentEffortCap", "modelPinnedEfforts", "multiAgentMode", "maxConcurrentThreadsPerSession", "enabled"}:
                                found[key] = value
                            else:
                                pick(value)
                pick(data)
                values[name] = found
                if not found:
                    warnings.append(name + " returned an unknown schema")
                if found.get("effortCap") or found.get("modelPinnedEfforts"):
                    warnings.append("OpenCodex caps/pins may override role efforts; inspect matching request logs")
                if found.get("multiAgentMode") not in (None, "v1"):
                    warnings.append("Expected tested v1 surface; start a NEW session after changes")
                if found.get("subagentEffortCap") not in (None, "max", "ultra"):
                    warnings.append("Child effort cap may lower Luna Max")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            warnings.append(name + " unavailable or unsupported")
    return values, warnings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    default = here.parent if here.name == ".astra-luna-sol" else Path.cwd()
    parser.add_argument("--project", type=Path, default=default)
    parser.add_argument("--probe", action="store_true", help="Read CLI status only; no model work")
    parser.add_argument("--report", type=Path, help="Validate a saved smoke-session usage JSON")
    parser.add_argument("--smoke-prompt", action="store_true", help="Print instructions; does not execute them")
    args = parser.parse_args(argv)
    if args.smoke_prompt:
        path = here / "smoke-test.txt" if here.name == ".astra-luna-sol" else here.parent / "docs/smoke-test.txt"
        print(path.read_text(encoding="utf-8"))
        return 0
    values, problems = inspect_config(args.project.resolve())
    output = {"project_settings": values, "problems": problems, "live_workers_executed_by_this_check": False,
              "upstream_routing_verified": False}
    if args.probe:
        output["runtime_probe"], warnings = probe()
        output["problems"].extend(warnings)
    if args.report:
        recorded, warnings = verify_report(json.loads(args.report.read_text(encoding="utf-8")))
        output["recorded_worker_identities"] = recorded
        output["problems"].extend(warnings)
    output["status"] = "CHECK_FAILED_OR_INCOMPLETE" if output["problems"] else "CONFIG_AND_REQUESTED_CHECKS_OK"
    output["note"] = "Config/record checks are not a model quality benchmark, completion proof, or upstream billing proof."
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 2 if output["problems"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError) as ex:
        print("CHECK ERROR: " + str(ex), file=sys.stderr)
        raise SystemExit(2)
