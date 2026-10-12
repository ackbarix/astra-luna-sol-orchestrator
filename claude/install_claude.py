#!/usr/bin/env python3
"""Install ALS for Claude Code: the `als-gpt` worker launcher, the role files and the `als` skill.

    python3 claude/install_claude.py [--dry-run | --check | --uninstall]

Writes only:
  ${ALS_HOME:-~/.local/share/als}/claude/  (als_gpt.py, roles/*.toml, manifest.json)
  ${ALS_BIN_DIR:-~/.local/bin}/als-gpt     (small sh wrapper)
  ${CLAUDE_HOME:-~/.claude}/skills/als/SKILL.md

Never edits Claude Code or Codex settings and never touches OpenCodex. Existing files it did
not install are left alone (refused, exit 2). Exit codes: 0 ok, 1 not installed (--check),
2 refused or drifted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

REPO = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get('HOME') or Path.home())


def targets():
    als_home = Path(os.environ.get('ALS_HOME') or HOME / '.local/share/als').expanduser().resolve()
    bin_dir = Path(os.environ.get('ALS_BIN_DIR') or HOME / '.local/bin').expanduser().resolve()
    claude_home = Path(os.environ.get('CLAUDE_HOME') or HOME / '.claude').expanduser().resolve()
    return als_home / 'claude', bin_dir, claude_home


def desired():
    base, bin_dir, claude_home = targets()
    files = {base / 'als_gpt.py': (REPO / 'claude/als_gpt.py').read_bytes()}
    for role in sorted((REPO / 'profiles/agents').glob('als_*.toml')):
        files[base / 'roles' / role.name] = role.read_bytes()
    files[claude_home / 'skills/als/SKILL.md'] = (REPO / 'claude/skills/als/SKILL.md').read_bytes()
    python = os.environ.get('ALS_PYTHON') or sys.executable
    wrapper = ('#!/bin/sh\n# ALS for Claude Code: GPT worker launcher (installed by install_claude.py)\n'
               'exec %s %s "$@"\n' % (shlex.quote(python), shlex.quote(str(base / 'als_gpt.py'))))
    files[bin_dir / 'als-gpt'] = wrapper.encode()
    return files


def digest(data):
    return hashlib.sha256(data).hexdigest()


def manifest_path():
    return targets()[0] / 'manifest.json'


def load_manifest():
    path = manifest_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError:
        raise SystemExit('REFUSED: unreadable manifest %s; inspect or delete it' % path)
    if not isinstance(data, dict) or not isinstance(data.get('files'), dict):
        raise SystemExit('REFUSED: malformed manifest %s' % path)
    return data


def install(dry_run):
    files = desired()
    manifest = load_manifest() or {'files': {}}
    owned = manifest['files']
    for path, data in files.items():
        if path.is_symlink():
            print('REFUSED: symlink at %s' % path)
            return 2
        if path.exists() and str(path) not in owned and path.read_bytes() != data:
            print('REFUSED: %s exists and was not installed by ALS; move it aside and rerun' % path)
            return 2
        if str(path) in owned and path.exists() and digest(path.read_bytes()) != owned[str(path)]:
            print('REFUSED: %s was edited after install; restore or remove it, then rerun' % path)
            return 2
    for path, data in files.items():
        action = 'UPDATE' if path.exists() else 'CREATE'
        if path.exists() and path.read_bytes() == data:
            action = 'KEEP  '
        print(action, path)
        if dry_run:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if path.name in ('als-gpt', 'als_gpt.py'):
            path.chmod(0o755)
    stale = [p for p in owned if Path(p) not in files]
    for path in stale:
        print('REMOVE', path)
        if not dry_run and Path(path).exists():
            Path(path).unlink()
    if dry_run:
        print('DRY RUN: nothing written')
        return 0
    version = (REPO / 'VERSION').read_text().strip()
    manifest = {'version': version, 'files': {str(p): digest(d) for p, d in files.items()}}
    manifest_path().write_text(json.dumps(manifest, indent=2))
    bin_dir = targets()[1]
    print('INSTALL OK (ALS for Claude Code %s). In Claude Code, ask for the `als` skill; workers run via als-gpt.'
          % version)
    if str(bin_dir) not in os.environ.get('PATH', '').split(os.pathsep):
        print('NOTE: %s is not on PATH; add: export PATH="%s:$PATH"' % (bin_dir, bin_dir))
    return 0


def check():
    manifest = load_manifest()
    if manifest is None:
        print('NOT INSTALLED')
        return 1
    drift = [p for p, h in manifest['files'].items() if not Path(p).is_file() or digest(Path(p).read_bytes()) != h]
    if drift:
        print('DRIFT: ' + ', '.join(drift))
        return 2
    outdated = {str(p): digest(d) for p, d in desired().items()} != manifest['files']
    print('OUTDATED: rerun install_claude.py' if outdated else 'VERIFIED')
    return 2 if outdated else 0


def uninstall(dry_run):
    manifest = load_manifest()
    if manifest is None:
        print('NOT INSTALLED')
        return 0
    edited = [p for p, h in manifest['files'].items() if Path(p).is_file() and digest(Path(p).read_bytes()) != h]
    if edited:
        print('REFUSED: edited after install: ' + ', '.join(edited))
        return 2
    for path in manifest['files']:
        print('REMOVE', path)
        if not dry_run and Path(path).exists():
            Path(path).unlink()
    if dry_run:
        print('DRY RUN: nothing removed')
        return 0
    manifest_path().unlink()
    base, _, claude_home = targets()
    for folder in (base / 'roles', base, claude_home / 'skills/als'):
        try:
            folder.rmdir()
        except OSError:
            pass
    print('UNINSTALL OK')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--uninstall', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args(argv)
    if args.check:
        return check()
    if args.uninstall:
        return uninstall(args.dry_run)
    return install(args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
