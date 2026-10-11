#!/usr/bin/env python3
"""Project-local ALS installer: preflight, reversible config merge, rollback.

No model calls, network access, global settings changes or application edits.
Modified owned files and symlinks fail closed. An interrupted/failed write is
rolled back on ordinary exceptions; power-loss atomicity is not claimed.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required (python3 --version)")
import config_overlay
from als_harness import integration as hook_config

ROOT = Path(__file__).resolve().parent
START, END = "<!-- ALS:BEGIN -->", "<!-- ALS:END -->"
CONFIG, AGENTS, MANIFEST = ".codex/config.toml", "AGENTS.md", ".astra-luna-sol/manifest.json"
VERSION = "0.3.0"
HOOKS = ".codex/hooks.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(root: Path, rel: str) -> Path:
    parts = PurePosixPath(rel)
    if not rel or parts.is_absolute() or ".." in parts.parts or "\\" in rel:
        raise ValueError("Invalid managed path: " + rel)
    path = root
    for index, part in enumerate(parts.parts):
        path = path / part
        if path.is_symlink():
            raise ValueError("Symlink refused: " + rel)
        if index < len(parts.parts) - 1 and path.exists() and not path.is_dir():
            raise ValueError("Parent is not a directory: " + rel)
    if path.exists() and not path.is_file():
        raise ValueError("Expected regular file: " + rel)
    return path


def read(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def text(data: bytes | None) -> str:
    return data.decode("utf-8") if data is not None else ""


def atomic_write(path: Path, data: bytes | None, mode: int = 0o644) -> None:
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".als-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def commit(root: Path, plan: dict, snapshots: dict) -> None:
    """Check for drift before writing; roll back our writes on normal errors."""
    for rel, (before, _) in snapshots.items():
        if read(safe_path(root, rel)) != before:
            raise ValueError("Concurrent modification detected: " + rel)
    changed = []
    try:
        for rel, data in plan.items():
            path = safe_path(root, rel)
            before, mode = snapshots[rel]
            if data == before:
                continue
            atomic_write(path, data, mode)
            changed.append(rel)
    except Exception:
        for rel in reversed(changed):
            path = safe_path(root, rel)
            before, mode = snapshots[rel]
            if read(path) == plan[rel]:
                atomic_write(path, before, mode)
        raise


def sources() -> dict[str, Path]:
    out = {".codex/agents/" + p.name: p for p in sorted((ROOT / "profiles/agents").glob("als_*.toml"))}
    if len(out) < 6:
        raise ValueError("Incomplete release: agent role files missing")
    for target, source in {
        ".agents/skills/astra-luna-sol/SKILL.md": "profiles/skills/astra-luna-sol/SKILL.md",
        ".astra-luna-sol/run.sh": "scripts/run.sh",
        ".astra-luna-sol/token_usage.py": "scripts/token_usage.py",
        ".astra-luna-sol/usage.sh": "scripts/usage.sh",
        ".astra-luna-sol/check-runtime.py": "scripts/check-runtime.py",
        ".astra-luna-sol/smoke-test.txt": "docs/smoke-test.txt",
        ".astra-luna-sol/VERSION": "VERSION",
        ".astra-luna-sol/licenses/Apache-2.0.txt": "licenses/Apache-2.0.txt",
    }.items():
        out[target] = ROOT / source
    out[".astra-luna-sol/harness.py"] = ROOT / "scripts/harness.py"
    out[".astra-luna-sol/.gitignore"] = ROOT / "profiles/runtime.gitignore"
    out[".astra-luna-sol/harness-guide.md"] = ROOT / "docs/harness.md"
    for module in sorted((ROOT / "als_harness").glob("*.py")):
        out[".astra-luna-sol/als_harness/" + module.name] = module
    return out


def policy_block(original: str, state: dict) -> str | None:
    if START not in original and END not in original:
        if state.get("agents"):
            raise ValueError("Managed AGENTS.md section missing")
        return None
    if original.count(START) != 1 or original.count(END) != 1 or original.index(START) > original.index(END):
        raise ValueError("Broken AGENTS.md markers")
    block = original[original.index(START):original.index(END) + len(END)]
    if digest(block.encode()) != state.get("agents"):
        raise ValueError("Unowned or edited AGENTS.md section")
    return block


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-hooks", action="store_true", help="Install CLI only; remove only prior ALS hook groups")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true")
    group.add_argument("--uninstall", action="store_true")
    args = ap.parse_args(argv)
    target = Path(args.target).expanduser().resolve()
    if not target.is_dir() or target in (ROOT, Path(target.anchor), Path.home().resolve()) or ROOT in target.parents:
        raise ValueError("Select an existing project separate from the installer, not / or your home")
    snapshots = {}

    def snapshot(rel):
        if rel not in snapshots:
            path = safe_path(target, rel)
            data = read(path)
            mode = stat.S_IMODE(path.stat().st_mode) if data is not None else (0o755 if rel.endswith((".sh", ".py")) else 0o644)
            snapshots[rel] = (data, mode)
        return snapshots[rel][0]

    raw_state = snapshot(MANIFEST)
    state = json.loads(text(raw_state)) if raw_state is not None else {"files": {}, "agents": None, "created": False}
    if not isinstance(state, dict) or not isinstance(state.get("files"), dict):
        raise ValueError("Invalid installation manifest")
    src = sources()
    # Never trust manifest paths as arbitrary read/delete targets.
    if set(state["files"]) - (set(src) | {CONFIG}):
        raise ValueError("Manifest includes unknown managed paths; manual review required")
    state = copy.deepcopy(state)
    if CONFIG in state["files"]:
        state.setdefault("config", {"mode": "created", "sha256": state["files"].pop(CONFIG)})
    if raw_state is None and (args.check or args.uninstall):
        print("Not installed")
        return 1 if args.check else 0
    desired = {rel: path.read_bytes() for rel, path in src.items()}
    for rel, expected in state["files"].items():
        before = snapshot(rel)
        if before is None or digest(before) != expected:
            raise ValueError("Modified or missing managed file: " + rel)
    for rel in desired:
        if rel not in state["files"] and snapshot(rel) is not None:
            raise ValueError("User-owned path already exists: " + rel)
    before_config = snapshot(CONFIG)
    config_state = state.get("config")
    if config_state:
        if before_config is None:
            raise ValueError("Managed Codex config missing")
        config_overlay.check(text(before_config), config_state)
        restored = config_overlay.restore(text(before_config), config_state) if config_state["mode"] == "merged" else None
    else:
        restored = text(before_config) if before_config is not None else None
    if restored is None:
        config_data = (ROOT / "profiles/config.toml").read_bytes()
        config_overlay.validate_defaults(config_overlay.parse(text(config_data)))
        next_config = {"mode": "created", "sha256": digest(config_data)}
    else:
        merged, next_config = config_overlay.merge(restored)
        config_data = merged.encode("utf-8")
    original_agents = snapshot(AGENTS)
    old_block = policy_block(text(original_agents), state)
    before_hooks = snapshot(HOOKS)
    hooks_state = state.get("hooks")
    restored_hooks = hook_config.remove(before_hooks, hooks_state) if hooks_state else before_hooks
    next_hooks = None
    if args.uninstall or args.skip_hooks:
        hooks_data = restored_hooks
    else:
        if config_overlay.parse(text(before_config)).get("hooks"):
            raise ValueError("Inline Codex hooks already exist; use --skip-hooks or reconcile sources explicitly")
        hooks_data, next_hooks = hook_config.merge(before_hooks, hooks_state, target)
    if args.check:
        outdated = state.get("version") != VERSION or config_state is None or config_data != before_config
        outdated |= any(state["files"].get(k) != digest(v) for k, v in desired.items())
        outdated |= not args.skip_hooks and (not hooks_state or hooks_data != before_hooks)
        if outdated:
            print("Installed files are intact, but update/reinstall is required")
            return 2
        print("INSTALL VERIFIED: file/config integrity only; live Codex routing not verified")
        return 0
    plan = {}
    if args.uninstall:
        plan.update({rel: None for rel in state["files"]})
        # Runtime state/evidence are user data, not release files. Keep their ignore
        # policy on uninstall if any runtime data remain; never delete the data.
        if any((target / ".astra-luna-sol" / x).exists() for x in ("state", "evidence")):
            plan.pop(".astra-luna-sol/.gitignore", None)
        if hooks_state:
            plan[HOOKS] = restored_hooks
        if config_state:
            plan[CONFIG] = restored.encode() if restored is not None else None
        if old_block:
            original = text(original_agents)
            if state.get("created") and original == old_block + "\n":
                plan[AGENTS] = None
            else:
                pos = original.index(old_block)
                before, after = original[:pos], original[pos + len(old_block):]
                join = state.get("agents_join", "\n\n")
                if join and before.endswith(join):
                    before = before[:-len(join)]
                if after.startswith("\n"):
                    after = after[1:]
                plan[AGENTS] = (before + after).encode()
        plan[MANIFEST] = None
    else:
        plan.update(desired)
        if next_hooks or hooks_state:
            plan[HOOKS] = hooks_data
        plan[CONFIG] = config_data
        block = START + "\n" + (ROOT / "profiles/AGENTS.snippet.md").read_text(encoding="utf-8").strip() + "\n" + END
        original = text(original_agents)
        if old_block:
            updated = original.replace(old_block, block, 1)
            join = state.get("agents_join", "\n\n")
        else:
            join = "\n" if original else ""
            updated = original + join + block + "\n"
        plan[AGENTS] = updated.encode()
        manifest = {"version": VERSION, "files": {rel: digest(data) for rel, data in desired.items()},
                    "config": next_config, "hooks": next_hooks, "agents": digest(block.encode()), "agents_join": join,
                    "created": state.get("created", False) if original_agents is not None else True}
        plan[MANIFEST] = (json.dumps(manifest, indent=2) + "\n").encode()
    for rel, data in plan.items():
        before = snapshot(rel)
        if data != before:
            print(("REMOVE " if data is None else "UPDATE " if before is not None else "CREATE ") + rel)
    if args.dry_run:
        print("DRY RUN: nothing written, no Codex/OpenCodex calls")
        return 0
    commit(target, plan, snapshots)
    print("UNINSTALL OK" if args.uninstall else "INSTALL OK 0.3.0: run codex; review /hooks before managing a run (observe default)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, UnicodeError) as ex:
        print("REFUSED: " + str(ex), file=sys.stderr)
        raise SystemExit(2)
