"""als harness diff|apply: install a staged harness into the protected Codex paths.

Codex's workspace-write sandbox keeps `.codex/` and `.agents/` read-only for the
agent itself, so the `harness` skill builds the team under
`_workspace/harness-staging/` with the standard layout:

    _workspace/harness-staging/.codex/agents/*.toml
    _workspace/harness-staging/.agents/skills/<name>/...

`als harness apply` (run by the user, or by the agent with the user's approval
because it writes outside the sandbox) validates the staged files and copies
them to the project's `.codex/agents/` and `.agents/skills/`. Files it
overwrites with different content are backed up first. `AGENTS.md` is not
protected, so the skill writes it directly.

Exit codes: 0 ok (or nothing to do), 1 validation failed, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import time

from . import common

STAGING = Path('_workspace/harness-staging')
MANAGED = ('.codex/agents', '.agents/skills')
REQUIRED_AGENT_KEYS = ('name', 'description', 'model', 'model_reasoning_effort', 'sandbox_mode',
                       'developer_instructions')
KNOWN_MODELS = ('gpt-6-astra', 'gpt-6-luna', 'gpt-6.1-sol')


def project_root():
    return common.workspace_dir().parent


def staged_files(staging):
    out = {}
    for managed in MANAGED:
        base = staging / managed
        if base.is_dir():
            for path in sorted(base.rglob('*')):
                if path.is_file() and not path.is_symlink():
                    out[path.relative_to(staging).as_posix()] = path
    return out


def load_toml(text):
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.9/3.10
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return common.parse_role(text)
    return tomllib.loads(text)


def validate(files):
    """(errors, warnings) for staged agent TOMLs and skills."""
    errors, warnings = [], []
    for rel, path in files.items():
        text = path.read_text(encoding='utf-8', errors='replace')
        if rel.startswith('.codex/agents/'):
            if not rel.endswith('.toml'):
                errors.append('%s: agent files must be .toml' % rel)
                continue
            try:
                data = load_toml(text)
            except Exception as error:  # noqa: BLE001 - report any TOML error
                errors.append('%s: invalid TOML (%s)' % (rel, error))
                continue
            if data is None:
                warnings.append('%s: no TOML parser on this Python; not validated' % rel)
                continue
            missing = [k for k in REQUIRED_AGENT_KEYS if not data.get(k)]
            if missing:
                errors.append('%s: missing %s' % (rel, ', '.join(missing)))
                continue
            if data['model'] not in KNOWN_MODELS:
                warnings.append('%s: unexpected model %r' % (rel, data['model']))
            if data['model'] == 'gpt-6-luna' and data['model_reasoning_effort'] != 'max':
                errors.append('%s: Luna roles must use model_reasoning_effort = "max"' % rel)
            if data['sandbox_mode'] not in ('read-only', 'workspace-write', 'danger-full-access'):
                errors.append('%s: invalid sandbox_mode %r' % (rel, data['sandbox_mode']))
        elif rel.endswith('/SKILL.md'):
            match = re.match(r'---\n(.*?)\n---\n', text, re.S)
            front = match.group(1) if match else ''
            for key in ('name', 'description'):
                if not re.search(r'^%s:\s*\S' % key, front, re.M):
                    errors.append('%s: frontmatter lacks %s' % (rel, key))
    if not any(rel.startswith('.codex/agents/') for rel in files):
        warnings.append('no staged agents (.codex/agents/*.toml)')
    return errors, warnings


def plan(files, root):
    """[(rel, action)] with action create|update|same."""
    out = []
    for rel, path in files.items():
        target = root / rel
        if not target.exists():
            out.append((rel, 'create'))
        elif target.read_bytes() == path.read_bytes():
            out.append((rel, 'same'))
        else:
            out.append((rel, 'update'))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog='als harness', description=__doc__.split('\n\n')[0])
    parser.add_argument('cmd', choices=('diff', 'apply'))
    parser.add_argument('--staging', help='staging directory (default: _workspace/harness-staging)')
    args = parser.parse_args(argv)
    root = project_root()
    staging = Path(args.staging).resolve() if args.staging else root / STAGING
    if not staging.is_dir():
        print('als harness: no staged harness at %s' % staging, file=sys.stderr)
        return 2
    files = staged_files(staging)
    if not files:
        print('als harness: nothing staged under %s/{%s}' % (staging, ','.join(MANAGED)), file=sys.stderr)
        return 2
    errors, warnings = validate(files)
    for warning in warnings:
        print('warn  ' + warning)
    for error in errors:
        print('ERROR ' + error)
    steps = plan(files, root)
    for rel, action in steps:
        print('%-6s %s' % (action.upper(), rel))
    if errors:
        print('REFUSED: fix the staged files above, then rerun')
        return 1
    if args.cmd == 'diff':
        return 0
    stamp = time.strftime('%Y%m%dT%H%M%S')
    record_dir = staging / '.applied'
    backup_dir = record_dir / ('backup-' + stamp)
    for rel, action in steps:
        if action == 'same':
            continue
        target = root / rel
        if action == 'update':
            backup = backup_dir / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(files[rel], target)
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / (stamp + '.json')).write_text(json.dumps(
        {'applied': [rel for rel, action in steps if action != 'same'], 'backup': str(backup_dir)
         if backup_dir.exists() else None}, indent=2, ensure_ascii=False), encoding='utf-8')
    changed = sum(1 for _, action in steps if action != 'same')
    print('APPLIED %d file(s) to %s%s' % (changed, root, ('; backups in ' + str(backup_dir))
                                          if backup_dir.exists() else ''))
    print('Start a new Codex session so it loads the new agents and skills.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
