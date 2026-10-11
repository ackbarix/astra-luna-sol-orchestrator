#!/usr/bin/env python3
"""ALS installer: preflight, reversible config merge, rollback.

Modes (no model calls, no network access):
  --target PROJECT               project-local install (default mode)
  --global                       shared install: harness in $ALS_HOME, roles/skill
                                 and the `als` profile in $CODEX_HOME; never edits
                                 $CODEX_HOME/config.toml and installs no hooks
  --target PROJECT --hooks-only  per-project hooks that call the global harness

Modified owned files and symlinks fail closed. An interrupted/failed write is
rolled back on ordinary exceptions; power-loss atomicity is not claimed.

Exit codes: 0 ok; 1 --check: not installed; 2 refused, or --check found drift
(modified/missing files, file modes, unmanaged hook changes, moved project);
4 --check: intact but outdated (reinstall with this release).
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 9):
    sys.stderr.write("REFUSED: Python 3.9+ is required (3.11+ or tomli to merge config.toml); "
                     "python3 --version\n")
    raise SystemExit(2)

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time

import config_overlay
from als_harness import integration as hook_config

ROOT = Path(__file__).resolve().parent
START, END = "<!-- ALS:BEGIN -->", "<!-- ALS:END -->"
CONFIG, AGENTS, MANIFEST = ".codex/config.toml", "AGENTS.md", ".astra-luna-sol/manifest.json"
HOOKS = ".codex/hooks.json"
IGNORE = ".astra-luna-sol/.gitignore"
PROFILE = "als.config.toml"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
# Directories that only ALS creates; used to tidy pre-0.5 installs whose
# manifests did not record created directories.
LEGACY_DIRS = [".astra-luna-sol/als_harness", ".astra-luna-sol/licenses", ".astra-luna-sol",
               ".agents/skills/astra-luna-sol"]
EXIT_OK, EXIT_ABSENT, EXIT_DRIFT, EXIT_OUTDATED = 0, 1, 2, 4


class Refused(ValueError):
    pass


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


def file_mode(path: Path) -> int | None:
    return stat.S_IMODE(path.stat().st_mode) if path.exists() else None


def release_mode(rel: str) -> int:
    return 0o755 if rel.endswith((".sh", ".py")) or rel.rsplit("/", 1)[-1] == "als" else 0o644


RUNTIME_FLOOR = (3, 9)


def hook_python() -> str:
    """'python3' when PATH has a 3.9+ python3; else this interpreter's path."""
    found = shutil.which("python3")
    if found:
        try:
            probe = subprocess.run([found, "-c", "import sys;sys.exit(sys.version_info[:2] < (3, 9))"],
                                   capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
            if probe.returncode == 0:
                return hook_config.DEFAULT_PYTHON
        except (OSError, subprocess.TimeoutExpired):
            pass
    me = os.path.abspath(sys.executable or "")
    if sys.version_info[:2] >= RUNTIME_FLOOR and sys.executable and os.path.isfile(me):
        print("NOTICE: `python3` on PATH is " + ("older than 3.9 or unusable" if found else "missing")
              + "; hooks will run " + me, file=sys.stderr)
        return me
    raise Refused("Hooks need Python 3.9+ as python3 on PATH; install it or rerun the installer with a "
                  "suitable interpreter")


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


class Layout:
    """Maps managed keys to files. Project mode: one root, keys are relative
    paths. Global/hooks-only: keys are '<ROOTNAME>/<relative path>'."""

    def __init__(self, roots: dict[str, Path]):
        self.roots = roots
        self.single = "" in roots

    def split(self, key: str) -> tuple[Path, str]:
        if self.single:
            return self.roots[""], key
        prefix, _, rel = key.partition("/")
        if prefix not in self.roots or not rel:
            raise ValueError("Invalid managed path: " + key)
        return self.roots[prefix], rel

    def path(self, key: str) -> Path:
        root, rel = self.split(key)
        return safe_path(root, rel)

    def show(self, key: str) -> str:
        root, rel = self.split(key)
        return rel if self.single else str(root / rel)

    def dir_key(self, path: Path) -> str:
        return path.relative_to(self.roots[""]).as_posix() if self.single else str(path)

    def dir_path(self, key: str) -> Path | None:
        """A recorded created directory, only if it is inside a managed root."""
        if self.single:
            parts = PurePosixPath(key)
            if not key or parts.is_absolute() or ".." in parts.parts:
                return None
            return self.roots[""] / key
        path = Path(key)
        if not path.is_absolute() or ".." in path.parts:
            return None
        return path if self.within_roots(path) else None

    def within_roots(self, path: Path) -> bool:
        """Inside or equal to a root; in global mode also a root's ancestor
        (e.g. ~/.local/share created for ALS_HOME)."""
        if any(path == r or r in path.parents for r in self.roots.values()):
            return True
        return not self.single and any(path in r.parents for r in self.roots.values())


def planned_dirs(layout: Layout, keys) -> list[Path]:
    """Missing parent directories (inside managed roots) that writing keys creates."""
    out = set()
    for key in keys:
        parent = layout.path(key).parent
        while not parent.exists():
            if layout.within_roots(parent):
                out.add(parent)
            parent = parent.parent
    return sorted(out)


def make_parents(layout: Layout, key: str) -> list[Path]:
    """Create missing parent directories one level at a time; return those
    created inside (or equal to) a managed root."""
    path = layout.path(key)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    created = []
    for directory in reversed(missing):
        directory.mkdir()
        if layout.within_roots(directory):
            created.append(directory)
    return created


def remove_empty_dirs(paths) -> list[Path]:
    removed = []
    for path in sorted(set(paths), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                path.rmdir()
                removed.append(path)
        except OSError:
            pass
    return removed


def remove_pycache(directory: Path) -> None:
    """Remove als_harness/__pycache__ only if it holds nothing but .pyc files."""
    cache = directory / "__pycache__"
    if not cache.is_dir() or cache.is_symlink():
        return
    entries = list(cache.iterdir())
    if all(e.is_file() and not e.is_symlink() and e.suffix == ".pyc" for e in entries):
        for entry in entries:
            entry.unlink()
        cache.rmdir()


def commit(root: Path, plan: dict, snapshots: dict, modes: dict | None = None,
           layout: Layout | None = None) -> list[Path]:
    """Check for drift before writing; roll back our writes on normal errors.

    Returns the directories created inside managed roots."""
    layout = layout or Layout({"": Path(root)})
    modes = modes or {}
    for key, (before, _) in snapshots.items():
        if read(layout.path(key)) != before:
            raise ValueError("Concurrent modification detected: " + key)
    changed, created = [], []
    try:
        for key, data in plan.items():
            path = layout.path(key)
            before, mode = snapshots[key]
            want = modes.get(key, mode)
            if data == before and (data is None or file_mode(path) == want):
                continue
            if data is not None:
                created += make_parents(layout, key)
            atomic_write(path, data, want)
            changed.append(key)
    except Exception:
        for key in reversed(changed):
            path = layout.path(key)
            before, mode = snapshots[key]
            if read(path) == plan[key]:
                atomic_write(path, before, mode)
        remove_empty_dirs(created)
        raise
    return created


class Session:
    def __init__(self, layout: Layout):
        self.layout = layout
        self.snapshots: dict[str, tuple[bytes | None, int]] = {}
        self.modes: dict[str, int] = {}

    def snapshot(self, key: str) -> bytes | None:
        if key not in self.snapshots:
            path = self.layout.path(key)
            data = read(path)
            mode = file_mode(path) if data is not None else release_mode(key)
            self.snapshots[key] = (data, mode)
        return self.snapshots[key][0]

    def mode(self, key: str) -> int | None:
        self.snapshot(key)
        return self.snapshots[key][1] if self.snapshots[key][0] is not None else None


# ---------------------------------------------------------------- sources

def project_sources() -> dict[str, Path]:
    out = {".codex/agents/" + p.name: p for p in sorted((ROOT / "profiles/agents").glob("als_*.toml"))}
    if len(out) < 6:
        raise ValueError("Incomplete release: agent role files missing")
    out[".agents/skills/astra-luna-sol/SKILL.md"] = ROOT / "profiles/skills/astra-luna-sol/SKILL.md"
    for rel, source in runtime_sources().items():
        out[".astra-luna-sol/" + rel] = source
    out[IGNORE] = ROOT / "profiles/runtime.gitignore"
    return out


def runtime_sources() -> dict[str, Path]:
    """Harness/runtime files, relative to the runtime directory."""
    out = {
        "run.sh": ROOT / "scripts/run.sh",
        "token_usage.py": ROOT / "scripts/token_usage.py",
        "usage.sh": ROOT / "scripts/usage.sh",
        "check-runtime.py": ROOT / "scripts/check-runtime.py",
        "smoke-test.txt": ROOT / "docs/smoke-test.txt",
        "VERSION": ROOT / "VERSION",
        "licenses/Apache-2.0.txt": ROOT / "licenses/Apache-2.0.txt",
        "harness.py": ROOT / "scripts/harness.py",
        "harness-guide.md": ROOT / "docs/harness.md",
        "efficiency-guide.md": ROOT / "docs/efficiency.md",
    }
    for module in sorted((ROOT / "als_harness").glob("*.py")):
        out["als_harness/" + module.name] = module
    return out


RELEASE_TOP = ("install.py", "install.sh", "setup.sh", "config_overlay.py", "VERSION", "bin/als",
               "docs/harness.md", "docs/efficiency.md", "docs/smoke-test.txt")


def release_files() -> dict[str, Path]:
    """Repository-relative files needed to rerun this installer (`als install`)."""
    out = {rel: ROOT / rel for rel in RELEASE_TOP}
    for pattern in ("als_harness/*.py", "scripts/*", "licenses/*", "profiles/*", "profiles/agents/*.toml",
                    "profiles/skills/astra-luna-sol/*"):
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file():
                out[path.relative_to(ROOT).as_posix()] = path
    return out


def global_sources() -> dict[str, Path]:
    out = {"ALS_HOME/" + rel: src for rel, src in runtime_sources().items()}
    out.update({"ALS_HOME/release/" + rel: src for rel, src in release_files().items()})
    out["BIN/als"] = ROOT / "bin/als"
    agents = sorted((ROOT / "profiles/agents").glob("als_*.toml"))
    if len(agents) < 6:
        raise ValueError("Incomplete release: agent role files missing")
    for path in agents:
        out["CODEX_HOME/agents/" + path.name] = path
    out["CODEX_HOME/skills/astra-luna-sol/SKILL.md"] = ROOT / "profiles/skills/astra-luna-sol/SKILL.md"
    out["CODEX_HOME/" + PROFILE] = ROOT / "profiles/config.toml"
    return out


def snippet_block() -> str:
    return START + "\n" + (ROOT / "profiles/AGENTS.snippet.md").read_text(encoding="utf-8").strip() + "\n" + END


# ------------------------------------------------------------- manifests

def _hooks_state_ok(value) -> bool:
    if value is None:
        return True
    return (isinstance(value, dict) and isinstance(value.get("groups"), dict)
            and all(isinstance(g, dict) for g in value["groups"].values())
            and isinstance(value.get("prior_events"), list)
            and isinstance(value.get("created"), bool)
            and isinstance(value.get("prior_hooks_key"), bool)
            and isinstance(value.get("installed_sha256"), (str, type(None)))
            and isinstance(value.get("original_b64"), (str, type(None))))


def validate_manifest(state, where: str, remedy: str) -> dict:
    def need(ok, field):
        if not ok:
            raise Refused("Invalid installation manifest (" + field + ") in " + where
                          + "; no changes made. " + remedy)

    need(isinstance(state, dict), "not a JSON object")
    files = state.get("files")
    need(isinstance(files, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()),
         "files")
    need(isinstance(state.get("version"), (str, type(None))), "version")
    config = state.get("config")
    need(config is None or (isinstance(config, dict) and config.get("mode") in ("created", "merged")), "config")
    if isinstance(config, dict) and config.get("mode") == "created":
        need(isinstance(config.get("sha256"), str), "config.sha256")
    if isinstance(config, dict) and config.get("mode") == "merged":
        need(all(isinstance(config.get(k), str) for k in ("root_hash", "agent_hash", "backups_hash")), "config hashes")
    need(_hooks_state_ok(state.get("hooks")), "hooks")
    need(isinstance(state.get("agents"), (str, type(None))), "agents")
    need(isinstance(state.get("agents_join", ""), str), "agents_join")
    need(isinstance(state.get("created", False), bool), "created")
    dirs = state.get("created_dirs", [])
    need(isinstance(dirs, list) and all(isinstance(x, str) for x in dirs), "created_dirs")
    need(isinstance(state.get("policy_file", AGENTS), str), "policy_file")
    projects = state.get("hooks_projects", {})
    need(isinstance(projects, dict) and all(isinstance(k, str) and _hooks_state_ok(v) and v is not None
                                            for k, v in projects.items()), "hooks_projects")
    need(isinstance(state.get("codex_home", ""), str), "codex_home")
    need(isinstance(state.get("bin_dir", ""), str), "bin_dir")
    need(isinstance(state.get("interpreter", "python3"), str), "interpreter")
    return copy.deepcopy(state)


def load_manifest(raw: bytes | None, where: str, remedy: str, default: dict) -> dict:
    if raw is None:
        return copy.deepcopy(default)
    try:
        state = json.loads(text(raw))
    except (ValueError, UnicodeError):
        raise Refused("Invalid installation manifest (not JSON) in " + where + "; no changes made. " + remedy)
    return validate_manifest(state, where, remedy)


# ------------------------------------------------------------- utilities

def installer_command(*args: str) -> str:
    return " ".join(["sh", shlex.quote(str(ROOT / "install.sh")), *(shlex.quote(a) for a in args)])


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def backup(session: Session, keys: list[str], backup_root: Path, dry_run: bool) -> None:
    """Copy drifted files aside before a forced uninstall removes them."""
    if not keys:
        return
    for key in keys:
        source = session.layout.path(key)
        rel = key if session.layout.single else key.replace("/", "__", 1)
        destination = backup_root / rel
        print("BACKUP " + session.layout.show(key) + " -> " + str(destination))
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def print_plan(session: Session, plan: dict) -> None:
    for key, data in plan.items():
        before = session.snapshot(key)
        want = session.modes.get(key)
        if data != before:
            verb = "REMOVE " if data is None else "UPDATE " if before is not None else "CREATE "
            print(verb + session.layout.show(key))
        elif data is not None and want is not None and session.mode(key) != want:
            print("CHMOD  " + session.layout.show(key) + " " + oct(want))


def git_warning(target: Path) -> None:
    if not (target / ".git").exists():
        print("WARNING: " + str(target) + " is not a git work tree root; ALS hooks locate the repository "
              "with git and will not run here", file=sys.stderr)


def validate_target(raw: str | None) -> Path:
    if raw is None or not raw.strip():
        raise Refused("Empty project path; pass an existing project directory, e.g. "
                      + installer_command("/path/to/project"))
    target = Path(raw).expanduser().resolve()
    if not target.is_dir() or target in (ROOT, Path(target.anchor), Path.home().resolve()) or ROOT in target.parents:
        raise Refused("Select an existing project separate from the installer, not / or your home: " + raw)
    return target


def classify(session: Session, files: dict[str, str]) -> tuple[list[str], list[str]]:
    missing, modified = [], []
    for key, expected in files.items():
        data = session.snapshot(key)
        if data is None:
            missing.append(key)
        elif digest(data) != expected:
            modified.append(key)
    return missing, modified


def mode_drift(session: Session, files: dict[str, str]) -> list[str]:
    out = []
    for key in files:
        current = session.mode(key)
        if current is not None and current != release_mode(key):
            out.append(key)
    return out


def adopt_or_refuse(session: Session, desired: dict[str, bytes], owned: dict[str, str], manifestless: bool,
                    adopt: bool, adopt_cmd: str) -> list[str]:
    """New release paths that already exist: adopt exact copies, else refuse."""
    adopted = []
    for key, data in desired.items():
        if key in owned:
            continue
        previous = session.snapshot(key)
        if previous is None:
            continue
        retained_ignore = key == IGNORE and manifestless
        if previous == data and (adopt or retained_ignore):
            # 0.3+ deliberately retains the runtime ignore file after uninstall
            # (state/evidence may be gone later). Adopt an exact copy only.
            adopted.append(key)
            continue
        if retained_ignore:
            raise Refused("User-owned path already exists: " + key + " (a retained runtime ignore file that "
                          "differs from this release). Move your extra rules elsewhere, then delete it: rm "
                          + shlex.quote(str(session.layout.path(key))))
        hint = (" It is identical to this release; take ownership with: " + adopt_cmd) if previous == data else \
            " Move it aside (it is not managed by ALS) and rerun the installer."
        raise Refused("User-owned path already exists: " + session.layout.show(key) + "." + hint)
    return adopted


# ------------------------------------------------------------ project mode

def policy_block(original: str, state: dict, name: str) -> str | None:
    if START not in original and END not in original:
        if state.get("agents"):
            raise ValueError("Managed " + name + " section missing")
        return None
    if original.count(START) != 1 or original.count(END) != 1 or original.index(START) > original.index(END):
        raise ValueError("Broken " + name + " markers")
    block = original[original.index(START):original.index(END) + len(END)]
    if digest(block.encode()) != state.get("agents"):
        raise ValueError("Unowned or edited " + name + " section")
    return block


def resolve_policy(target: Path, requested: str | None, state: dict, remedy_args: list[str]) -> str:
    recorded = state.get("policy_file") if state.get("agents") else None
    if requested is None:
        rel = recorded or AGENTS
    else:
        path = Path(requested)
        if path.is_absolute():
            try:
                rel = path.relative_to(target).as_posix()
            except ValueError:
                raise Refused("--policy-file must be inside the project: " + requested)
        else:
            rel = PurePosixPath(requested).as_posix()
        if recorded and rel != recorded:
            raise Refused("ALS policy block lives in " + recorded + "; uninstall first or rerun without "
                          "--policy-file: " + installer_command(str(target), "--uninstall"))
    try:
        path = safe_path(target, rel)
    except ValueError as ex:
        if "Symlink" in str(ex):
            raise Refused(rel + " is a symlink (refused). Target the real in-repo file instead: "
                          + installer_command(str(target), *remedy_args, "--policy-file", "PATH/TO/AGENTS.md"))
        raise
    if requested is not None and not path.is_file():
        raise Refused("--policy-file must name an existing regular file in the project: " + rel)
    return rel


def project_mode(args) -> int:
    target = validate_target(args.target)
    if config_overlay.tomllib is None:
        raise Refused(config_overlay.TOML_MISSING)
    layout = Layout({"": target})
    session = Session(layout)
    t = str(target)
    remedy = ("Inspect it, or move it aside and run: " + installer_command(t, "--adopt"))
    raw_state = session.snapshot(MANIFEST)
    state = load_manifest(raw_state, MANIFEST, remedy, {"files": {}, "agents": None, "created": False})
    src = project_sources()
    if set(state["files"]) - (set(src) | {CONFIG}):
        raise Refused("Manifest includes unknown managed paths; manual review required")
    if CONFIG in state["files"]:
        state.setdefault("config", {"mode": "created", "sha256": state["files"].pop(CONFIG)})
    removing = args.uninstall or args.force_uninstall
    if raw_state is None and (args.check or removing):
        print("Not installed")
        return EXIT_ABSENT if args.check else EXIT_OK
    mode_args = [] if not args.skip_hooks else ["--skip-hooks"]
    policy = resolve_policy(target, args.policy_file, state, mode_args)
    desired = {rel: path.read_bytes() for rel, path in src.items()}
    force_cmd = installer_command(t, "--force-uninstall")
    missing, modified = classify(session, state["files"])
    drift: list[str] = []      # human-readable drift reasons
    drifted: list[str] = []    # keys to back up on a forced uninstall
    if modified:
        drift.append("modified managed files: " + ", ".join(modified))
        drifted += modified
    if missing:
        drift.append("missing managed files: " + ", ".join(missing))

    # Codex config (merged overlay or created file)
    before_config = session.snapshot(CONFIG)
    config_state = state.get("config")
    config_error = None
    restored = text(before_config) if before_config is not None else None
    if config_state and before_config is None:
        drift.append("managed Codex config missing: " + CONFIG)
        restored = None
    elif config_state:
        try:
            config_overlay.check(text(before_config), config_state)
            restored = (config_overlay.restore(text(before_config), config_state)
                        if config_state["mode"] == "merged" else None)
        except ValueError as ex:
            config_error = str(ex)
            drift.append(CONFIG + ": " + config_error)
            drifted.append(CONFIG)
    elif args.adopt and before_config is not None:
        if before_config == (ROOT / "profiles/config.toml").read_bytes():
            config_state = {"mode": "created", "sha256": digest(before_config)}
            restored = None
        elif config_overlay.BACKUP in text(before_config) or config_overlay.ROOT_BEGIN in text(before_config):
            config_state = config_overlay.adopt_state(text(before_config))
            restored = config_overlay.restore(text(before_config), config_state)

    # Policy (AGENTS.md) block
    original_agents = session.snapshot(policy)
    old_block, agents_error = None, None
    if args.adopt and not state.get("agents") and START in text(original_agents):
        state["agents"] = digest(snippet_block().encode())
        state["created"] = text(original_agents) == snippet_block() + "\n"
    try:
        if original_agents is None and state.get("agents"):
            drift.append("policy file missing: " + policy)
        else:
            old_block = policy_block(text(original_agents), state, policy)
    except ValueError as ex:
        agents_error = str(ex)
        if "section missing" in agents_error:
            drift.append(agents_error)
            agents_error = None
        else:
            drift.append(agents_error)
            drifted.append(policy)

    # Hooks
    before_hooks = session.snapshot(HOOKS)
    hooks_state = state.get("hooks")
    if args.adopt and not hooks_state:
        hooks_state = hook_config.adopt(before_hooks, "project", None, state.get("interpreter"))
    hooks_error = None
    restored_hooks = before_hooks
    if hooks_state and before_hooks is None:
        drift.append("managed hooks file missing: " + HOOKS)
        hooks_state = None
    elif hooks_state:
        try:
            restored_hooks = hook_config.remove(before_hooks, hooks_state)
        except ValueError as ex:
            hooks_error = str(ex)
            drift.append(HOOKS + ": " + hooks_error)
            drifted.append(HOOKS)

    if args.check:
        return project_check(session, state, desired, drift, before_config, config_state, restored,
                             before_hooks, hooks_state, target, args)

    if removing:
        if drifted and not args.force_uninstall:
            raise Refused("Uninstall would lose changes (" + "; ".join(d for d in drift) + "). Inspect them, "
                          "or back them up and remove ALS-managed paths with: " + force_cmd)
        return project_uninstall(session, state, args, policy, restored, config_state, config_error,
                                 original_agents, old_block, agents_error, hooks_state, hooks_error,
                                 restored_hooks, drifted)

    if drifted and raw_state is None:
        raise Refused("ALS content found without a manifest (" + "; ".join(drift) + "). If it came from this "
                      "release, take ownership with: " + installer_command(t, "--adopt")
                      + "  otherwise remove the ALS markers/files by hand.")
    if drifted:
        raise Refused("Installed ALS files were changed (" + "; ".join(drift) + "). Inspect them; to back "
                      "them up and remove ALS-managed paths run: " + force_cmd + "  then reinstall.")
    adopted = adopt_or_refuse(session, desired, state["files"], raw_state is None, args.adopt,
                              installer_command(t, "--adopt"))

    if restored is None:
        config_data = (ROOT / "profiles/config.toml").read_bytes()
        config_overlay.validate_defaults(config_overlay.parse(text(config_data)))
        next_config = {"mode": "created", "sha256": digest(config_data)}
    else:
        parsed = config_overlay.parse(restored)
        for setting in config_overlay.risky_settings(parsed):
            print("WARNING: " + CONFIG + " sets " + setting + "; ALS leaves it unchanged", file=sys.stderr)
        merged, next_config = config_overlay.merge(restored)
        config_data = merged.encode("utf-8")

    next_hooks = None
    python = state.get("interpreter", hook_config.DEFAULT_PYTHON)
    if args.skip_hooks:
        hooks_data = restored_hooks
    else:
        if config_overlay.parse(text(before_config)).get("hooks"):
            raise Refused("Inline Codex hooks already exist in " + CONFIG + "; reconcile them or install without "
                          "hooks: " + installer_command(t, "--skip-hooks"))
        python = hook_python()
        hooks_data, next_hooks = hook_config.merge(restored_hooks if hooks_state else before_hooks, None,
                                                   "project", None, python)
        git_warning(target)

    plan: dict[str, bytes | None] = dict(desired)
    for rel in desired:
        session.snapshot(rel)
        session.modes[rel] = release_mode(rel)
    if next_hooks or hooks_state:
        plan[HOOKS] = hooks_data
    plan[CONFIG] = config_data
    block = snippet_block()
    original = text(original_agents)
    if old_block:
        updated = original.replace(old_block, block, 1)
        join = state.get("agents_join", "\n\n")
    else:
        join = "\n" if original else ""
        updated = original + join + block + "\n"
    plan[policy] = updated.encode()
    created_flag = state.get("created", False) if original_agents is not None else True
    for key in plan:
        session.snapshot(key)
    session.snapshot(MANIFEST)
    previous_dirs = [d for d in state.get("created_dirs", []) if layout.dir_path(d)]
    new_dirs = [layout.dir_key(d) for d in planned_dirs(layout, [*plan, MANIFEST])]
    manifest = {"version": VERSION, "files": {rel: digest(data) for rel, data in desired.items()},
                "config": next_config, "hooks": next_hooks, "agents": digest(block.encode()),
                "agents_join": join, "created": created_flag, "policy_file": policy, "interpreter": python,
                "created_dirs": sorted(set(previous_dirs) | set(new_dirs))}
    plan[MANIFEST] = (json.dumps(manifest, indent=2) + "\n").encode()
    for key in adopted:
        print("ADOPT  " + key)
    print_plan(session, plan)
    if args.dry_run:
        print("DRY RUN: nothing written, no Codex/OpenCodex calls")
        return EXIT_OK
    commit(target, plan, {k: session.snapshots[k] for k in plan}, session.modes, layout)
    print("INSTALL OK " + VERSION + ": run codex; review /hooks before managing a run (observe default)")
    return EXIT_OK


def project_check(session, state, desired, drift, before_config, config_state, restored,
                  before_hooks, hooks_state, target, args) -> int:
    t = str(target)
    if drift:
        print("DRIFT: " + "; ".join(drift))
        print("Inspect the files. To back up and remove ALS-managed paths: " + installer_command(t, "--force-uninstall"))
        return EXIT_DRIFT
    moved = hook_config.legacy_project(hooks_state)
    if moved and moved != t and not args.skip_hooks:
        print("PROJECT MOVED: installed hooks still point to " + moved + "; relocate them with: "
              + installer_command(t))
        return EXIT_DRIFT
    if hooks_state and hook_config.unmanaged_changed(before_hooks, hooks_state):
        print("UNMANAGED HOOKS CHANGED: ALS groups in " + HOOKS + " are intact, but other hook definitions "
              "changed since install; review /hooks. Uninstall will splice out only ALS groups.")
        return EXIT_DRIFT
    modes = mode_drift(session, state["files"])
    if modes:
        print("MODE DRIFT: " + ", ".join(f"{k} {oct(session.mode(k))} (expected {oct(release_mode(k))})"
                                          for k in modes) + "; repair with: " + installer_command(t))
        return EXIT_DRIFT
    outdated = state.get("version") != VERSION or config_state is None
    outdated |= any(state["files"].get(k) != digest(v) for k, v in desired.items())
    if restored is None:
        outdated |= before_config != (ROOT / "profiles/config.toml").read_bytes()
    else:
        try:
            outdated |= config_overlay.merge(restored)[0].encode() != before_config
        except ValueError:
            outdated = True
    if not args.skip_hooks:
        if not hooks_state:
            outdated = True
        else:
            current = hook_config.remove(before_hooks, hooks_state)
            try:
                outdated |= hook_config.merge(current, None, "project", None,
                                              state.get("interpreter"))[0] != before_hooks
            except ValueError:
                outdated = True
    if args.policy_file is None and state.get("agents") != digest(snippet_block().encode()):
        outdated = True
    if outdated:
        print("ALS OUTDATED: installed " + str(state.get("version") or "pre-0.3") + ", this release " + VERSION
              + "; files are intact. Update with: " + installer_command(t, *(["--skip-hooks"] if args.skip_hooks else [])))
        return EXIT_OUTDATED
    print("INSTALL VERIFIED " + VERSION + ": file/config/mode integrity only; live Codex routing not verified")
    return EXIT_OK


def project_uninstall(session, state, args, policy, restored, config_state, config_error, original_agents,
                      old_block, agents_error, hooks_state, hooks_error, restored_hooks, drifted) -> int:
    layout = session.layout
    target = layout.roots[""]
    plan: dict[str, bytes | None] = {rel: None for rel in state["files"]}
    keep_notes = []
    # Runtime state/evidence are user data, not release files. Keep their ignore
    # policy on uninstall if any runtime data remain; never delete the data.
    if any((target / ".astra-luna-sol" / x).exists() for x in ("state", "evidence")):
        plan.pop(IGNORE, None)
    if hooks_state:
        if hooks_error:
            keep_notes.append(HOOKS + " left in place (ALS groups edited): remove ALS hook groups by hand")
        else:
            plan[HOOKS] = restored_hooks
    if config_state:
        if config_error:
            if config_state["mode"] == "created":
                plan[CONFIG] = None
            else:
                keep_notes.append(CONFIG + " left in place (ALS overlay edited): remove the # ALS: marker lines "
                                           "by hand; the backup holds the edited copy")
        elif session.snapshot(CONFIG) is not None:
            plan[CONFIG] = restored.encode() if restored is not None else None
    if old_block and not agents_error:
        original = text(original_agents)
        if state.get("created") and original == old_block + "\n":
            plan[policy] = None
        else:
            pos = original.index(old_block)
            before, after = original[:pos], original[pos + len(old_block):]
            join = state.get("agents_join", "\n\n")
            if join and before.endswith(join):
                before = before[:-len(join)]
            if after.startswith("\n"):
                after = after[1:]
            plan[policy] = (before + after).encode()
    elif agents_error:
        keep_notes.append(policy + " left in place (ALS block edited): remove the <!-- ALS:BEGIN/END --> block by hand")
    plan[MANIFEST] = None
    for key in plan:
        session.snapshot(key)
    if drifted:
        backup_root = target / ".astra-luna-sol" / ("backup-" + utc_stamp())
        backup(session, [k for k in dict.fromkeys(drifted) if session.snapshot(k) is not None], backup_root,
               args.dry_run)
    print_plan(session, plan)
    for note in keep_notes:
        print("KEEP   " + note)
    if args.dry_run:
        print("DRY RUN: nothing written, no Codex/OpenCodex calls")
        return EXIT_OK
    commit(target, plan, {k: session.snapshots[k] for k in plan}, None, layout)
    remove_pycache(target / ".astra-luna-sol/als_harness")
    recorded = [layout.dir_path(d) for d in state.get("created_dirs", [])] if "created_dirs" in state else \
        [target / d for d in LEGACY_DIRS]
    remove_empty_dirs([d for d in recorded if d is not None])
    print("UNINSTALL OK" + (" (forced; see BACKUP/KEEP lines above)" if drifted else ""))
    return EXIT_OK


# ------------------------------------------------------------- global mode

def global_homes() -> tuple[Path, Path, Path]:
    home = Path(os.environ.get("HOME") or Path.home())
    data_home = os.environ.get("XDG_DATA_HOME") or str(home / ".local/share")
    als_home = Path(os.environ.get("ALS_HOME") or Path(data_home) / "als").expanduser().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser().resolve()
    bin_dir = Path(os.environ.get("ALS_BIN_DIR") or home / ".local/bin").expanduser().resolve()
    for name, path in (("ALS_HOME", als_home), ("CODEX_HOME", codex_home), ("ALS_BIN_DIR", bin_dir)):
        if path in (Path(path.anchor), home.resolve()):
            raise Refused(name + " must be a dedicated directory, not / or your home: " + str(path))
        installed_copy = name == "ALS_HOME" and ROOT == path / "release"
        if not installed_copy and (path == ROOT or ROOT in path.parents or path in ROOT.parents):
            raise Refused(name + " must be outside the installer checkout: " + str(path))
        if path.exists() and not path.is_dir():
            raise Refused(name + " must be a directory: " + str(path))
    for a, b in ((als_home, codex_home), (als_home, bin_dir), (codex_home, bin_dir)):
        if a == b or a in b.parents or b in a.parents:
            raise Refused("ALS_HOME, CODEX_HOME and ALS_BIN_DIR must not contain each other: "
                          + str(a) + ", " + str(b))
    return als_home, codex_home, bin_dir


GLOBAL_MANIFEST = "ALS_HOME/manifest.json"


def global_mode(args) -> int:
    als_home, codex_home, bin_dir = global_homes()
    layout = Layout({"ALS_HOME": als_home, "CODEX_HOME": codex_home, "BIN": bin_dir})
    session = Session(layout)
    where = str(als_home / "manifest.json")
    remedy = "Inspect it, or move it aside and run: " + installer_command("--global", "--adopt")
    raw_state = session.snapshot(GLOBAL_MANIFEST)
    state = load_manifest(raw_state, where, remedy, {"files": {}, "hooks_projects": {}})
    removing = args.uninstall or args.force_uninstall
    if raw_state is None and (args.check or removing):
        print("Not installed (no " + where + ")")
        return EXIT_ABSENT if args.check else EXIT_OK
    for name, key, path in (("CODEX_HOME", "codex_home", codex_home), ("ALS_BIN_DIR", "bin_dir", bin_dir)):
        if raw_state is not None and state.get(key, str(path)) != str(path):
            raise Refused("This global install used " + name + "=" + state[key] + "; rerun with that value, e.g. "
                          + name + "=" + shlex.quote(state[key]) + " "
                          + installer_command("--global", *(["--check"] if args.check else [])))
    src = global_sources()
    if set(state["files"]) - set(src):
        raise Refused("Manifest includes unknown managed paths; manual review required")
    desired = {key: path.read_bytes() for key, path in src.items()}
    missing, modified = classify(session, state["files"])
    projects = state.get("hooks_projects", {})
    if args.check:
        if modified or missing:
            print("DRIFT: " + "; ".join(x for x in ("modified: " + ", ".join(modified) if modified else "",
                                                     "missing: " + ", ".join(missing) if missing else "") if x))
            print("Inspect the files. To back up and remove ALS-managed paths: "
                  + installer_command("--global", "--force-uninstall"))
            return EXIT_DRIFT
        gone = [p for p in projects if not Path(p).is_dir()]
        if gone:
            print("PROJECT MOVED: registered hook projects no longer exist: " + ", ".join(gone)
                  + ". Drop each stale entry with: " + installer_command("PATH", "--hooks-only", "--uninstall")
                  + "  then rerun --hooks-only at the new location.")
            return EXIT_DRIFT
        modes = mode_drift(session, state["files"])
        if modes:
            print("MODE DRIFT: " + ", ".join(layout.show(k) for k in modes) + "; repair with: "
                  + installer_command("--global"))
            return EXIT_DRIFT
        stale_hooks = [p for p, h in projects.items()
                       if h.get("groups") != hook_config.groups("global", als_home, h.get("interpreter"))]
        if state.get("version") != VERSION or any(state["files"].get(k) != digest(v) for k, v in desired.items()) \
                or set(state["files"]) != set(desired) or stale_hooks:
            print("ALS OUTDATED: installed " + str(state.get("version")) + ", this release " + VERSION
                  + "; files are intact. Update with: " + installer_command("--global")
                  + "".join("; and " + installer_command(p, "--hooks-only") for p in stale_hooks))
            return EXIT_OUTDATED
        print("GLOBAL INSTALL VERIFIED " + VERSION + ": file/mode integrity only; opt in with `codex --profile als`;"
              " live Codex routing not verified")
        return EXIT_OK
    if removing:
        if projects:
            raise Refused("Projects still use the global harness through hooks; remove them first:\n  "
                          + "\n  ".join(installer_command(p, "--hooks-only", "--uninstall") for p in projects))
        if modified and not args.force_uninstall:
            raise Refused("Uninstall would lose changes to: " + ", ".join(layout.show(k) for k in modified)
                          + ". Inspect them, or back them up and remove ALS-managed paths with: "
                          + installer_command("--global", "--force-uninstall"))
        plan = {key: None for key in state["files"]}
        plan[GLOBAL_MANIFEST] = None
        for key in plan:
            session.snapshot(key)
        if modified:
            backup(session, modified, als_home / ("backup-" + utc_stamp()), args.dry_run)
        print_plan(session, plan)
        if args.dry_run:
            print("DRY RUN: nothing written, no Codex/OpenCodex calls")
            return EXIT_OK
        commit(als_home, plan, {k: session.snapshots[k] for k in plan}, None, layout)
        for directory in ("als_harness", "release", "release/als_harness", "release/scripts"):
            remove_pycache(als_home / directory)
        remove_empty_dirs([d for d in (layout.dir_path(x) for x in state.get("created_dirs", [])) if d])
        print("GLOBAL UNINSTALL OK" + (" (forced; see BACKUP lines above)" if modified else ""))
        return EXIT_OK
    if modified:
        raise Refused("Installed ALS files were changed: " + ", ".join(layout.show(k) for k in modified)
                      + ". Inspect them; to back them up and remove ALS-managed paths run: "
                      + installer_command("--global", "--force-uninstall") + "  then reinstall.")
    adopted = adopt_or_refuse(session, desired, state["files"], False, args.adopt,
                              installer_command("--global", "--adopt"))
    plan = dict(desired)
    for key in desired:
        session.snapshot(key)
        session.modes[key] = release_mode(key)
    # Files dropped from the release since the last install.
    for key in set(state["files"]) - set(desired):
        plan[key] = None
        session.snapshot(key)
    session.snapshot(GLOBAL_MANIFEST)
    previous = [d for d in state.get("created_dirs", []) if layout.dir_path(d)]
    new_dirs = [layout.dir_key(d) for d in planned_dirs(layout, [*plan, GLOBAL_MANIFEST])]
    manifest = {"version": VERSION, "kind": "global", "als_home": str(als_home), "codex_home": str(codex_home),
                "bin_dir": str(bin_dir),
                "files": {key: digest(data) for key, data in desired.items()},
                "created_dirs": sorted(set(previous) | set(new_dirs)),
                "hooks_projects": projects}
    plan[GLOBAL_MANIFEST] = (json.dumps(manifest, indent=2) + "\n").encode()
    for key in adopted:
        print("ADOPT  " + layout.show(key))
    print_plan(session, plan)
    if args.dry_run:
        print("DRY RUN: nothing written, no Codex/OpenCodex calls")
        return EXIT_OK
    commit(als_home, plan, {k: session.snapshots[k] for k in plan}, session.modes, layout)
    # Point at the installed entrypoint, not this clone, which may be deleted.
    print("GLOBAL INSTALL OK " + VERSION + ": start Codex with `als run` (codex --profile als); "
          + "add harness hooks per project with `cd PROJECT && als install --hooks-only`")
    if projects and any(h.get("groups") != hook_config.groups("global", als_home, h.get("interpreter"))
                        for h in projects.values()):
        print("NOTE: refresh project hooks with --hooks-only for: " + ", ".join(projects))
    if str(bin_dir) not in [os.path.realpath(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]:
        print("NOTE: " + str(bin_dir) + " is not on PATH; add this line to your shell profile:\n"
              "  export PATH=\"" + str(bin_dir) + ":$PATH\"")
    return EXIT_OK


def inline_hooks(content: str) -> bool:
    """[hooks] tables in a project config.toml (text scan without a TOML parser)."""
    if config_overlay.tomllib is not None:
        return bool(config_overlay.parse(content).get("hooks"))
    return any(re.match(r'\s*(\[\[?\s*["\']?hooks\b|["\']?hooks["\']?\s*[.=])', line)
               for line in content.splitlines())


def hooks_only_mode(args) -> int:
    als_home, _, _ = global_homes()
    removing = args.uninstall or args.force_uninstall
    raw_target = args.target
    if raw_target is None or not raw_target.strip():
        validate_target(raw_target)
    target = Path(raw_target).expanduser().resolve()
    gone = removing and not target.exists()
    if not gone:
        target = validate_target(raw_target)
    layout = Layout({"ALS_HOME": als_home, "PROJECT": target})
    session = Session(layout)
    where = str(als_home / "manifest.json")
    raw_global = session.snapshot(GLOBAL_MANIFEST)
    if raw_global is None:
        if args.check or removing:
            print("Not installed (no global ALS install at " + str(als_home) + ")")
            return EXIT_ABSENT if args.check else EXIT_OK
        raise Refused("--hooks-only needs the global harness; install it first: " + installer_command("--global"))
    gstate = load_manifest(raw_global, where, "Inspect it, or move it aside and run: "
                           + installer_command("--global", "--adopt"), {})
    projects = gstate.setdefault("hooks_projects", {})
    key, t = "PROJECT/" + HOOKS, str(target)
    entry = projects.get(t)
    force_cmd = installer_command(t, "--hooks-only", "--force-uninstall")
    if gone:
        if entry is None:
            print("Not installed")
            return EXIT_OK
        projects.pop(t)
        print("FORGET " + t + " (project directory no longer exists; nothing else to remove)")
        if args.dry_run:
            print("DRY RUN: nothing written")
            return EXIT_OK
        commit(als_home, {GLOBAL_MANIFEST: (json.dumps(gstate, indent=2) + "\n").encode()},
               {GLOBAL_MANIFEST: session.snapshots[GLOBAL_MANIFEST]}, None, layout)
        print("HOOKS UNINSTALL OK")
        return EXIT_OK
    before = session.snapshot(key)
    if args.check or removing:
        if entry is None:
            stale = [p for p in projects if not Path(p).is_dir()]
            found = before is not None and hook_config.has_als_handler(hook_config.parse(before))
            if args.check and found:
                print("PROJECT MOVED: " + HOOKS + " has ALS hooks but " + t + " is not registered"
                      + (" (stale registrations: " + ", ".join(stale) + ")" if stale else "")
                      + ". Run: " + installer_command(stale[0] if stale else "OLD_PATH", "--hooks-only", "--uninstall")
                      + "  then edit out the ALS groups or reinstall here with --hooks-only --adopt")
                return EXIT_DRIFT
            print("Not installed")
            return EXIT_ABSENT if args.check else EXIT_OK
    if entry is None and args.adopt:
        entry = hook_config.adopt(before, "global", als_home)
    error, restored = None, before
    if entry and before is None:
        restored = None
        if args.check:
            print("DRIFT: managed hooks file missing: " + HOOKS + "; reinstall with " + installer_command(t, "--hooks-only"))
            return EXIT_DRIFT
    elif entry:
        try:
            restored = hook_config.remove(before, entry)
        except ValueError as ex:
            error = str(ex)
    if args.check:
        if error:
            print("DRIFT: " + HOOKS + ": " + error + ". Back up and remove with: " + force_cmd)
            return EXIT_DRIFT
        if hook_config.unmanaged_changed(before, entry):
            print("UNMANAGED HOOKS CHANGED: ALS groups intact; other hook definitions changed since install")
            return EXIT_DRIFT
        if entry["groups"] != hook_config.groups("global", als_home, entry.get("interpreter")) \
                or gstate.get("version") != VERSION:
            print("ALS OUTDATED: refresh with " + installer_command(t, "--hooks-only"))
            return EXIT_OUTDATED
        print("HOOKS VERIFIED " + VERSION + ": " + HOOKS + " -> " + hook_config.command("global", als_home))
        return EXIT_OK
    plan: dict[str, bytes | None] = {}
    if removing:
        if error and not args.force_uninstall:
            raise Refused(HOOKS + ": " + error + ". Inspect it, or back it up and remove with: " + force_cmd)
        if error:
            backup(session, [key], als_home / ("backup-" + utc_stamp()), args.dry_run)
            print("KEEP   " + HOOKS + " left in place (ALS groups edited): remove ALS hook groups by hand")
        elif before is not None:
            plan[key] = restored
        projects.pop(t, None)
    else:
        if error:
            raise Refused(HOOKS + ": " + error + ". Inspect it; to back it up and remove: " + force_cmd)
        if (target / MANIFEST).exists():
            raise Refused("This project has a project-mode ALS install; its hooks are managed there. "
                          "Uninstall it first: " + installer_command(t, "--uninstall"))
        project_config = target / CONFIG
        if project_config.is_file() and not project_config.is_symlink():
            if inline_hooks(project_config.read_text(encoding="utf-8")):
                raise Refused("Inline Codex hooks already exist in " + CONFIG + "; reconcile them first")
        python = hook_python()
        data, new_state = hook_config.merge(restored, None, "global", als_home, python)
        new_state["interpreter"] = python
        new_state["created_dirs"] = sorted(set((entry or {}).get("created_dirs", []))
                                           | {str(d) for d in planned_dirs(layout, [key])})
        plan[key] = data
        projects[t] = new_state
        git_warning(target)
    gstate_bytes = (json.dumps(gstate, indent=2) + "\n").encode()
    print_plan(session, plan)
    print("UPDATE " + where)
    if args.dry_run:
        print("DRY RUN: nothing written, no Codex/OpenCodex calls")
        return EXIT_OK
    plan[GLOBAL_MANIFEST] = gstate_bytes
    commit(target, plan, {k: session.snapshots[k] for k in plan}, None, layout)
    if removing:
        recorded = [Path(d) for d in (entry or {}).get("created_dirs", [])]
        remove_empty_dirs([d for d in recorded if d.is_absolute() and target in d.parents])
        print("HOOKS UNINSTALL OK")
    else:
        print("HOOKS INSTALL OK: " + HOOKS + " runs " + hook_config.command("global", als_home)
              + "; trust and review them with /hooks")
    return EXIT_OK


# ------------------------------------------------------------------ main

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="install.py (--target PROJECT [--hooks-only] | --global) [options]")
    ap.add_argument("--target", help="existing project directory (project or --hooks-only mode)")
    ap.add_argument("--global", dest="global_mode", action="store_true",
                    help="install into $ALS_HOME and $CODEX_HOME (profile `als`), no hooks")
    ap.add_argument("--hooks-only", action="store_true",
                    help="write only the project .codex/hooks.json that calls the global harness")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    ap.add_argument("--skip-hooks", action="store_true",
                    help="project mode: install CLI only; remove only prior ALS hook groups")
    ap.add_argument("--adopt", action="store_true",
                    help="take ownership of existing files byte-identical to this release (lost manifest)")
    ap.add_argument("--policy-file", metavar="PATH",
                    help="project mode: in-repo regular file for the ALS policy block (default AGENTS.md)")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="verify; rc 0 ok, 1 absent, 2 drift, 4 outdated")
    group.add_argument("--uninstall", action="store_true",
                       help="remove ALS files; missing files are fine, modified ones refuse")
    group.add_argument("--force-uninstall", action="store_true",
                       help="back up drifted files to backup-<UTC>/ then remove only manifest-listed paths")
    return ap


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.global_mode == (args.target is not None):
        raise Refused("Choose exactly one of --target PROJECT or --global")
    if args.global_mode and (args.skip_hooks or args.policy_file or args.hooks_only):
        raise Refused("--global takes no --skip-hooks/--policy-file/--hooks-only (hooks are per project: "
                      "--target PROJECT --hooks-only)")
    if args.hooks_only and (args.skip_hooks or args.policy_file):
        raise Refused("--hooks-only takes no --skip-hooks/--policy-file")
    if args.adopt and (args.check or args.uninstall or args.force_uninstall):
        raise Refused("--adopt is an install option; it cannot be combined with --check/--uninstall")
    if args.global_mode:
        return global_mode(args)
    if args.hooks_only:
        return hooks_only_mode(args)
    return project_mode(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as ex:
        print("REFUSED: " + str(ex), file=sys.stderr)
        raise SystemExit(2)
