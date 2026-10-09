"""Reversible, fail-closed Codex TOML overlay. Python 3.11+, stdlib only.

Text is preserved outside ALS-owned assignments. tomllib validates BOTH the
input/output and equality of every unmanaged value (including array tables).
Unsupported multiline/inline assignment forms are rejected before any write.
Legacy manifest/block hashes remain readable so the 0.1 installer can upgrade.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import math
import re
import tomllib

ROOT_BEGIN = "# ALS:ROOT:BEGIN\n"
ROOT_END = "# ALS:ROOT:END\n"
AGENTS_BEGIN = "# ALS:AGENTS:BEGIN\n"
AGENTS_END = "# ALS:AGENTS:END\n"
CREATED = "# ALS:GENERATED_AGENTS\n"
BACKUP = "# ALS:RESTORE:"
ROOT_KEYS = {"model": '"gpt-6-astra"', "model_reasoning_effort": '"medium"'}
AGENT_KEYS = {
    "enabled": "true", "max_concurrent_threads_per_session": "6",
    "max_depth": "1", "default_subagent_model": '"gpt-6-luna"',
    "default_subagent_reasoning_effort": '"max"',
}
# Detect EVERY table boundary, not just [table]. Strict parse below validates it.
HEADER = re.compile(r"^[ \t]*\[", re.M)
AGENTS_HEADER = re.compile(r'^\s*\[(?:agents|"agents"|\'agents\')\]\s*(?:#.*)?$')
KEY = re.compile(r'^\s*([A-Za-z_][\w-]*)\s*=')


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as ex:
        raise ValueError("Invalid Codex TOML; no changes made: " + str(ex)) from ex


def _equal(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and math.isnan(a):
        return math.isnan(b)
    return a == b


def unmanaged(data: dict) -> dict:
    data = copy.deepcopy(data)
    for key in ROOT_KEYS:
        data.pop(key, None)
    agents = data.get("agents")
    if isinstance(agents, dict):
        for key in (*AGENT_KEYS, "max_threads"):
            agents.pop(key, None)
        if not agents:
            data.pop("agents")
    return data


def validate_defaults(data: dict) -> None:
    for key, value in ROOT_KEYS.items():
        if data.get(key) != tomllib.loads("v=" + value)["v"]:
            raise ValueError("Root model setting not applied: " + key)
    for key, value in AGENT_KEYS.items():
        if data.get("agents", {}).get(key) != tomllib.loads("v=" + value)["v"]:
            raise ValueError("Agent setting not applied: " + key)
    if "max_threads" in data.get("agents", {}):
        raise ValueError("Conflicting legacy max_threads alias remains")


def block(begin: str, end: str, keys: dict) -> str:
    return begin + "".join(f"{k} = {v}\n" for k, v in keys.items()) + end


def get_block(text: str, begin: str, end: str) -> str:
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ValueError("Missing or duplicated ALS config markers")
    a = text.index(begin)
    if text.index(end) < a:
        raise ValueError("Reversed ALS config markers")
    return text[a:text.index(end, a) + len(end)]


def restore(content: str, state: dict) -> str:
    """Restore assignments for BOTH old and new manifests; preserve user edits."""
    if state.get("mode") != "merged":
        return content
    root = get_block(content, ROOT_BEGIN, ROOT_END)
    agents = get_block(content, AGENTS_BEGIN, AGENTS_END)
    if sha(root) != state["root_hash"] or sha(agents) != state["agent_hash"]:
        raise ValueError("Managed Codex settings edited; refusing overwrite")
    backups = [x for x in content.splitlines(keepends=True) if x.startswith(BACKUP)]
    if sha("".join(backups)) != state["backups_hash"]:
        raise ValueError("Restoration markers changed")
    content = content.replace(root, "", 1).replace(agents, "", 1)
    if state.get("header_added_eol"):
        # This case only arises from a final [agents] header without newline.
        marker = state["header_added_eol"]
        if content.endswith(marker + "\n"):
            content = content[:-1]
    if state.get("generated_agents"):
        generated = "\n" + CREATED + "[agents]\n"
        if content.count(generated) != 1:
            raise ValueError("Generated agents header changed")
        after = content.split(generated, 1)[1]
        boundary = HEADER.search(after)
        body = after[:boundary.start()] if boundary else after
        if any(x.strip() and not x.lstrip().startswith("#") for x in body.splitlines()):
            # Keep user-added agents settings, but remove our ownership comment.
            content = content.replace(CREATED, "", 1)
        else:
            content = content.replace(generated, "", 1)
    result = []
    for line in content.splitlines(keepends=True):
        if line.startswith(BACKUP):
            try:
                result.append(base64.b64decode(line[len(BACKUP):].strip(), validate=True).decode("utf-8"))
            except (ValueError, UnicodeError) as ex:
                raise ValueError("Corrupt config restoration marker") from ex
        else:
            result.append(line)
    restored = "".join(result)
    parse(restored)
    return restored


def merge(original: str) -> tuple[str, dict]:
    before = parse(original)
    if any(x in original for x in (ROOT_BEGIN, AGENTS_BEGIN, CREATED, BACKUP)):
        raise ValueError("Unowned ALS markers present")
    if "'''" in original or '\"\"\"' in original:
        raise ValueError("Multiline TOML strings require manual merge; file unchanged")
    lines = original.splitlines(keepends=True)
    has_agents = any(AGENTS_HEADER.fullmatch(x.rstrip("\r\n")) for x in lines)
    if "agents" in before and (not isinstance(before["agents"], dict) or not has_agents):
        raise ValueError("Inline/dotted agents table requires manual merge")
    scope, replaced = "root", {"root": set(), "agents": set()}
    body, header_added_eol = [], None
    for line in lines:
        if HEADER.match(line):
            scope = "agents" if AGENTS_HEADER.fullmatch(line.rstrip("\r\n")) else "other"
            body.append(line)
            if scope == "agents":
                if not line.endswith("\n"):
                    header_added_eol = line
                    body.append("\n")
                body.append(block(AGENTS_BEGIN, AGENTS_END, AGENT_KEYS))
            continue
        match = KEY.match(line)
        key = match.group(1) if match else None
        owned = ROOT_KEYS if scope == "root" else {**AGENT_KEYS, "max_threads": ""} if scope == "agents" else {}
        if key in owned:
            # Refuse multi-line expressions for a managed scalar.
            statement = parse(line)
            if key not in statement or isinstance(statement[key], (list, dict)):
                raise ValueError("Unsupported managed assignment: " + key)
            replaced[scope].add(key)
            body.append(BACKUP + base64.b64encode(line.encode("utf-8")).decode("ascii") + "\n")
        else:
            body.append(line)
    for scope, keys, data in (("root", ROOT_KEYS, before), ("agents", {**AGENT_KEYS, "max_threads": ""}, before.get("agents", {}))):
        for key in keys:
            if key in data and key not in replaced[scope]:
                raise ValueError("Unsupported quoted/dotted managed key: " + key)
    root_block = block(ROOT_BEGIN, ROOT_END, ROOT_KEYS)
    agent_block = block(AGENTS_BEGIN, AGENTS_END, AGENT_KEYS)
    merged = root_block + "".join(body)
    if not has_agents:
        merged += "\n" + CREATED + "[agents]\n" + agent_block
    after = parse(merged)
    validate_defaults(after)
    if not _equal(unmanaged(before), unmanaged(after)):
        raise ValueError("Unmanaged TOML values would change; refusing all writes")
    backups = [x for x in merged.splitlines(keepends=True) if x.startswith(BACKUP)]
    state = {"mode": "merged", "format": 2, "root_hash": sha(root_block),
             "agent_hash": sha(agent_block), "backups_hash": sha("".join(backups)),
             "generated_agents": not has_agents, "header_added_eol": header_added_eol}
    if restore(merged, state) != original:
        raise ValueError("Byte-exact restoration failed; refusing all writes")
    return merged, state


def check(content: str, state: dict) -> None:
    if state.get("mode") == "created":
        if sha(content) != state.get("sha256"):
            raise ValueError("Installer-created config edited; manual review required")
        parse(content)
    elif state.get("mode") == "merged":
        restored = restore(content, state)
        if state.get("format", 1) >= 2:
            validate_defaults(parse(content))
            if not _equal(unmanaged(parse(restored)), unmanaged(parse(content))):
                raise ValueError("Unmanaged TOML values were altered")
        # Legacy merges must be restored, then re-merged with new safeguards.
    else:
        raise ValueError("Unknown config ownership mode")
