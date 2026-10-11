"""Dependency and installation-conflict checks for ALS (`als doctor`).

Standard library only, Python 3.9+. Never calls a model: the only external
programs run are `git --version`, `codex --version`, `codex features list`
and `ocx --version`, each with a 10 second timeout.

    check(project=None, probe=True) -> {'ok': bool, 'items': [item, ...]}

Each item is {'name', 'found', 'required', 'ok', 'hint'}. 'ok' at the top is
true when every required item is ok; optional items only carry hints.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

TIMEOUT = 10
RUNTIME_FLOOR = (3, 9)
INSTALLER_TOML = (3, 11)
GIT_WORKTREE = (2, 5)
CODEX_FEATURES = {
    'hooks': 'enable with `codex features enable hooks` (or [features] hooks = true in config.toml)',
    'multi_agent': 'enable with `codex features enable multi_agent` (or [features] multi_agent = true)',
}


def _item(name, found, ok, hint='', required=True):
    return {'name': name, 'found': found, 'required': required, 'ok': bool(ok), 'hint': '' if ok else hint}


def _run(argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return None, type(ex).__name__
    return done.returncode, (done.stdout or '') + (done.stderr or '')


def _version(text):
    match = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', text or '')
    return tuple(int(x or 0) for x in match.groups()) if match else None


def als_home():
    data = os.environ.get('XDG_DATA_HOME') or os.path.join(os.path.expanduser('~'), '.local/share')
    return Path(os.environ.get('ALS_HOME') or os.path.join(data, 'als')).expanduser()


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex')).expanduser()


def bin_dir():
    return Path(os.environ.get('ALS_BIN_DIR') or os.path.join(os.path.expanduser('~'), '.local/bin')).expanduser()


def python_items():
    found = '.'.join(map(str, sys.version_info[:3])) + ' (' + sys.executable + ')'
    runtime = _item('python', found, sys.version_info[:2] >= RUNTIME_FLOOR,
                    'ALS needs Python 3.9+ as `python3` on PATH (hooks run python3)')
    toml = sys.version_info[:2] >= INSTALLER_TOML
    if not toml:
        try:
            import tomli  # noqa: F401
            toml = True
        except ImportError:
            pass
    installer = _item('python-installer', 'tomllib' if sys.version_info[:2] >= INSTALLER_TOML
                      else ('tomli' if toml else 'no TOML parser'), toml,
                      'Python 3.11+ (or `pip install tomli`) is needed only to merge a project '
                      'config.toml; `als install --global` works without it', required=False)
    path_python = shutil.which('python3')
    if path_python and os.path.realpath(path_python) != os.path.realpath(sys.executable):
        code, out = _run([path_python, '-c', 'import sys;print("%d.%d.%d" % sys.version_info[:3])'])
        version = _version(out) if code == 0 else None
        found = (out.strip() if version else 'unusable') + ' (' + path_python + ')'
        return [runtime, installer, _item('python3-on-path', found, version and version[:2] >= RUNTIME_FLOOR,
                                          'hooks run `python3` from PATH; put Python 3.9+ first on PATH '
                                          'or reinstall so the hook records an absolute interpreter')]
    return [runtime, installer]


def git_item():
    exe = shutil.which('git')
    if not exe:
        return _item('git', None, False, 'install git; project hooks locate the repository with git')
    code, out = _run([exe, '--version'])
    version = _version(out) if code == 0 else None
    found = (out.strip() if version else 'unusable') + ' (' + exe + ')'
    return _item('git', found, version and version[:2] >= GIT_WORKTREE,
                 'git 2.5+ is needed for `git worktree` support used by forked workers')


def sqlite_item():
    try:
        import sqlite3
    except ImportError:
        return _item('sqlite3', None, False, 'this Python lacks the sqlite3 module; install a full Python build')
    return _item('sqlite3', sqlite3.sqlite_version, True)


def codex_items(probe=True):
    exe = shutil.which('codex')
    if not exe:
        return [_item('codex', None, False, 'install the Codex CLI (npm i -g @openai/codex) and put it on PATH')]
    code, out = _run([exe, '--version'])
    items = [_item('codex', (out.strip().splitlines() or ['?'])[0] if code == 0 else 'unusable (' + exe + ')',
                   code == 0, '`codex --version` failed; reinstall the Codex CLI')]
    if not probe or code != 0:
        return items
    code, out = _run([exe, 'features', 'list'])
    states = {}
    for line in (out or '').splitlines() if code == 0 else []:
        parts = line.split()
        if len(parts) >= 2 and parts[-1] in ('true', 'false'):
            states[parts[0]] = parts[-1] == 'true'
    for feature, hint in CODEX_FEATURES.items():
        state = states.get(feature)
        found = None if code != 0 else ('enabled' if state else 'disabled' if state is False else 'unknown')
        items.append(_item('codex-feature:' + feature, found, state is True,
                           hint if code == 0 else '`codex features list` failed; ' + hint))
    return items


def ocx_item():
    exe = shutil.which('ocx')
    if not exe:
        return _item('ocx', None, False, 'optional: OpenCodex (ocx) is only needed for `run.sh --configure-ocx`',
                     required=False)
    code, out = _run([exe, '--version'])
    return _item('ocx', (out.strip().splitlines() or [exe])[0] if code == 0 else exe, True, required=False)


def bin_item():
    directory = bin_dir()
    entries = [os.path.realpath(p) for p in os.environ.get('PATH', '').split(os.pathsep) if p]
    on_path = os.path.realpath(str(directory)) in entries
    return _item('als-on-path', str(directory), on_path,
                 'add to your shell profile: export PATH="' + str(directory) + ':$PATH"', required=False)


def _manifest_version(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data.get('version') if isinstance(data, dict) else None


def conflict_items(project=None):
    items = []
    global_agents = sorted(p.name for p in (codex_home() / 'agents').glob('als_*.toml'))
    global_version = _manifest_version(als_home() / 'manifest.json')
    if project is None:
        return items
    project = Path(project)
    local_agents = sorted(p.name for p in (project / '.codex/agents').glob('als_*.toml'))
    shadowed = sorted(set(local_agents) & set(global_agents))
    items.append(_item('conflict:agent-roles', ', '.join(shadowed) or 'none', not shadowed,
                       'project .codex/agents/als_*.toml override the global roles in ' + str(codex_home() / 'agents')
                       + '; keep one: `als uninstall` (project) or `als uninstall --global`', required=False))
    project_version = _manifest_version(project / '.astra-luna-sol/manifest.json')
    differ = bool(project_version and global_version and project_version != global_version)
    items.append(_item('conflict:versions', 'project ' + str(project_version) + ', global ' + str(global_version),
                       not differ, 'project and global ALS versions differ; reinstall the older one with the '
                       'current release (`als install` or `als install --global`)', required=False))
    return items


def check(project: Path | None = None, probe: bool = True) -> dict:
    items = python_items() + [git_item(), sqlite_item()] + codex_items(probe) + [ocx_item(), bin_item()]
    items += conflict_items(project)
    return {'ok': all(i['ok'] for i in items if i['required']), 'items': items}


def render(result):
    lines = []
    for item in result['items']:
        mark = 'ok  ' if item['ok'] else ('FAIL' if item['required'] else 'warn')
        lines.append('[' + mark + '] ' + item['name'] + ': ' + str(item['found']))
        if item['hint']:
            lines.append('       -> ' + item['hint'])
    lines.append('dependencies OK' if result['ok'] else 'dependencies MISSING (see FAIL lines)')
    return '\n'.join(lines)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='Check ALS dependencies (never calls a model)')
    ap.add_argument('--project', type=Path)
    ap.add_argument('--no-probe', action='store_true', help='skip `codex features list`')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    result = check(args.project, not args.no_probe)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
