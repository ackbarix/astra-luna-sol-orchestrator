"""Conservative, reversible project Codex TOML settings overlay (Python 3.11+).

Only fixed model keys are managed. Existing key assignments are encoded as
comment-only restoration markers; unrelated settings are never copied into a
separate manifest or backup file.
"""
from __future__ import annotations

import base64
import hashlib
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
    "enabled": "true",
    "max_concurrent_threads_per_session": "6",
    "default_subagent_model": '"gpt-6-luna"',
    "default_subagent_reasoning_effort": '"max"',
}
HEADER = re.compile(r'^\s*\[([^\[\]]+)\]\s*(?:#.*)?$', re.M)
KEY = re.compile(r'^\s*([A-Za-z_][\w-]*)\s*=')


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse(s):
    try:
        return tomllib.loads(s)
    except tomllib.TOMLDecodeError as ex:
        raise ValueError("Invalid Codex TOML: " + str(ex)) from ex


def block(begin, end, keys):
    return begin + "".join(f"{k} = {v}\n" for k, v in keys.items()) + end


def get_block(s, begin, end):
    if s.count(begin) != 1 or s.count(end) != 1:
        raise ValueError("Managed Codex settings are missing or duplicated")
    a = s.index(begin)
    b = s.index(end, a) + len(end)
    return s[a:b]


def restore(content, state):
    """Remove ALS-only keys, restoring preexisting assignments byte-for-byte."""
    if state.get("mode") != "merged":
        return content
    root = get_block(content, ROOT_BEGIN, ROOT_END)
    agents = get_block(content, AGENTS_BEGIN, AGENTS_END)
    if sha(root) != state["root_hash"] or sha(agents) != state["agent_hash"]:
        raise ValueError("Managed Codex model settings were edited; cannot overwrite")
    backups = [line for line in content.splitlines(keepends=True) if line.startswith(BACKUP)]
    if sha("".join(backups)) != state["backups_hash"]:
        raise ValueError("Original Codex setting restoration markers were changed")
    content = content.replace(root, "", 1).replace(agents, "", 1)
    if state.get("generated_agents"):
        generated = "\n" + CREATED + "[agents]\n"
        if content.count(generated) != 1:
            raise ValueError("Generated [agents] table was modified")
        after = content.split(generated, 1)[1]
        next_table = HEADER.search(after)
        candidate = after[:next_table.start()] if next_table else after
        if any(x.strip() and not x.lstrip().startswith("#") for x in candidate.splitlines()):
            raise ValueError("New settings inside generated [agents] need manual migration")
        content = content.replace(generated, "", 1)
    result = []
    for line in content.splitlines(keepends=True):
        if line.startswith(BACKUP):
            try:
                result.append(base64.b64decode(
                    line[len(BACKUP):].strip(), validate=True
                ).decode("utf-8"))
            except Exception as ex:
                raise ValueError("Corrupt original config backup line") from ex
        else:
            result.append(line)
    restored = "".join(result)
    parse(restored)
    return restored


def merge(original):
    """Overlay model defaults without dropping other user TOML settings."""
    parsed = parse(original)
    if any(v in original for v in [ROOT_BEGIN, AGENTS_BEGIN, CREATED, BACKUP]):
        raise ValueError("ALS markers already present without ownership metadata")
    # Conservative escape hatch: text-based transforms do not parse TOML
    # multiline strings as distinct language states.
    if "'''" in original or '"""' in original:
        raise ValueError("Multiline TOML string: use launcher or merge manually")
    if isinstance(parsed.get("agents"), dict) and not re.search(
        r'^\s*\[agents\]\s*(?:#.*)?$', original, re.M
    ):
        raise ValueError("Existing agents use dotted, inline or nested table: manual merge required")
    scope = "root"
    replaced = {"root": set(), "agents": set()}
    result = []
    agent_header = False
    for line in original.splitlines(keepends=True):
        header = HEADER.match(line)
        if header:
            scope = "agents" if header.group(1).strip() == "agents" else "other"
            agent_header |= scope == "agents"
            result.append(line)
            continue
        m = KEY.match(line)
        field = m.group(1) if m else None
        owned = ROOT_KEYS if scope == "root" else AGENT_KEYS if scope == "agents" else {}
        if field in owned:
            replaced[scope].add(field)
            result.append(BACKUP + base64.b64encode(line.encode("utf-8")).decode() + "\n")
        else:
            result.append(line)
    for field in ROOT_KEYS:
        if field in parsed and field not in replaced["root"]:
            raise ValueError("Unsupported TOML root assignment: " + field)
    for field in AGENT_KEYS:
        if field in parsed.get("agents", {}) and field not in replaced["agents"]:
            raise ValueError("Unsupported TOML [agents] assignment: " + field)
    root = block(ROOT_BEGIN, ROOT_END, ROOT_KEYS)
    agents = block(AGENTS_BEGIN, AGENTS_END, AGENT_KEYS)
    merged = root + "".join(result)
    if agent_header:
        lines = merged.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if re.match(r'^\s*\[agents\]\s*(?:#.*)?$', line):
                lines.insert(i + 1, agents)
                break
        merged = "".join(lines)
    else:
        merged += "\n" + CREATED + "[agents]\n" + agents
    parse(merged)
    backups = [x for x in merged.splitlines(keepends=True) if x.startswith(BACKUP)]
    return merged, {
        "mode": "merged",
        "root_hash": sha(root),
        "agent_hash": sha(agents),
        "backups_hash": sha("".join(backups)),
        "generated_agents": not agent_header,
    }


def check(content, state):
    if state.get("mode") == "created":
        if sha(content) != state.get("sha256"):
            raise ValueError("Created Codex config was modified; manual review required")
    elif state.get("mode") == "merged":
        restore(content, state)
        parsed = parse(content)
        if parsed.get("model") != "gpt-6-astra" or parsed.get("model_reasoning_effort") != "medium":
            raise ValueError("HEAD model defaults no longer active")
    else:
        raise ValueError("Unknown managed Codex config mode")
