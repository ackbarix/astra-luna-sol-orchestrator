#!/usr/bin/env python3
"""als-gpt: run one ALS GPT worker through the Codex CLI, without OpenCodex.

Claude Code is the head. It calls this script (usually in the background) to
hand a bounded task to a GPT role. The role file (model, reasoning effort,
sandbox, instructions) is the single source of truth; this script never lets
the caller raise a role's effort or loosen its sandbox.

    als-gpt run ROLE TASK.md|-   [--cwd DIR] [--in-place] [--timeout S]
    als-gpt roles               list roles and their pins
    als-gpt ls                  recent runs for this repository
    als-gpt show RUN            full report of one run
    als-gpt clean RUN           remove a finished run's worktree (keeps the branch)

Writer roles work in their own git worktree on branch `als/<run>` so parallel
workers never share files; the head reviews the diff and merges. Read-only
roles run in the current checkout with a read-only sandbox.

Standard library only, Python 3.9+. Exit codes: 0 worker finished, 1 worker
failed, 2 usage/setup error, 124 timeout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
NATIVE_CHATGPT_URL = 'https://chatgpt.com/backend-api/codex'
NATIVE_API_URL = 'https://api.openai.com/v1'
MAX_REPORT_CHARS = 4000
RETURN_CONTRACT = (
    'Return a concise report: what you did, files changed, checks you actually ran '
    'with their results, and open problems. Do not paste whole files or raw logs. '
    'Never claim a check passed unless you ran it.')


class UsageError(Exception):
    """Bad arguments or environment; exit code 2."""


# ------------------------------------------------------------------ roles

def roles_dir():
    env = os.environ.get('ALS_ROLES_DIR')
    candidates = [Path(env)] if env else []
    candidates += [HERE / 'roles', HERE.parent / 'profiles' / 'agents']
    for path in candidates:
        if path.is_dir() and any(path.glob('als_*.toml')):
            return path
    raise UsageError('role files not found; set ALS_ROLES_DIR or reinstall')


def parse_role(text):
    """Minimal TOML reader for role files: `key = "..."` and `key = \"\"\"...\"\"\"`."""
    values, lines, i = {}, text.splitlines(), 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = (part.strip() for part in line.split('=', 1))
        if value.startswith('"""'):
            body = value[3:]
            if body.endswith('"""'):
                values[key] = body[:-3]
                continue
            collected = [body] if body else []
            while i < len(lines) and not lines[i].rstrip().endswith('"""'):
                collected.append(lines[i])
                i += 1
            if i < len(lines):
                collected.append(lines[i].rstrip()[:-3])
                i += 1
            values[key] = '\n'.join(collected).strip('\n')
        elif value.startswith('"'):
            match = re.match(r'"((?:[^"\\]|\\.)*)"', value)
            if match:
                escapes = {'n': '\n', 't': '\t', '"': '"', '\\': '\\'}
                values[key] = re.sub(r'\\(.)', lambda m: escapes.get(m.group(1), m.group(0)), match.group(1))
    return values


def load_roles():
    roles = {}
    for path in sorted(roles_dir().glob('als_*.toml')):
        role = parse_role(path.read_text(encoding='utf-8'))
        missing = [k for k in ('name', 'model', 'model_reasoning_effort', 'sandbox_mode',
                               'developer_instructions') if not role.get(k)]
        if missing:
            raise UsageError('%s is missing %s' % (path.name, ', '.join(missing)))
        roles[role['name']] = role
    return roles


# ------------------------------------------------------------------ git / paths

def git(args, cwd, check=True):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise UsageError('git %s failed: %s' % (' '.join(args), result.stderr.strip()[:300]))
    return result.stdout.strip()


def repo_root(cwd):
    result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd, capture_output=True,
                            text=True, timeout=30)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def state_dir(root):
    base = os.environ.get('ALS_STATE_HOME')
    if base and not os.path.isabs(base):
        raise UsageError('ALS_STATE_HOME must be an absolute path')
    if not base:
        xdg = os.environ.get('XDG_STATE_HOME')
        base = xdg if xdg and os.path.isabs(xdg) else str(Path.home() / '.local/state')
        base = os.path.join(base, 'als')
    key = hashlib.sha256(str(root).encode()).hexdigest()[:16] if root else 'no-repo'
    return Path(base) / 'gpt-runs' / key


# ------------------------------------------------------------------ codex

def codex_bin():
    found = os.environ.get('ALS_CODEX') or shutil.which('codex')
    if not found:
        raise UsageError('codex CLI not found on PATH (install Codex and run `codex login`)')
    return found


def native_base_url(codex):
    """Endpoint that bypasses any proxy (e.g. OpenCodex) set in config.toml."""
    override = os.environ.get('ALS_CODEX_BASE_URL')
    if override is not None:
        return override or None  # empty string: keep config.toml as is
    try:
        status = subprocess.run([codex, 'login', 'status'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return NATIVE_CHATGPT_URL
    text = (status.stdout + status.stderr).lower()
    if 'api key' in text:
        return NATIVE_API_URL
    return NATIVE_CHATGPT_URL


def build_prompt(role, task):
    return (role['developer_instructions'].strip() + '\n\n# Task\n\n' + task.strip()
            + '\n\n# Report\n\n' + RETURN_CONTRACT + '\n')


def build_command(codex, role, workdir, last_file, base_url):
    argv = [codex, 'exec', '-m', role['model'], '-c', 'model_reasoning_effort="%s"' % role['model_reasoning_effort'],
            '-s', role['sandbox_mode'], '--cd', str(workdir), '--skip-git-repo-check', '--ephemeral',
            '--json', '-o', str(last_file)]
    if base_url:
        argv[2:2] = ['-c', 'openai_base_url="%s"' % base_url]
    argv.append('-')  # prompt on stdin
    return argv


def parse_usage(events_text):
    usage = {}
    for line in events_text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict):
            for key, value in event['usage'].items():
                if isinstance(value, int):
                    usage[key] = usage.get(key, 0) + value
    return usage


# ------------------------------------------------------------------ commands

def cmd_roles(_args):
    for name, role in sorted(load_roles().items()):
        print('%-20s %-12s %-7s %s' % (name, role['model'], role['model_reasoning_effort'], role['sandbox_mode']))
    return 0


def read_task(spec):
    if spec == '-':
        text = sys.stdin.read()
    else:
        path = Path(spec)
        if not path.is_file():
            raise UsageError('task file not found: %s' % spec)
        text = path.read_text(encoding='utf-8')
    if not text.strip():
        raise UsageError('empty task')
    return text


def cmd_run(args):
    roles = load_roles()
    role = roles.get(args.role) or roles.get('als_' + args.role)
    if role is None:
        raise UsageError('unknown role %r; see `als-gpt roles`' % args.role)
    task = read_task(args.task)
    cwd = Path(args.cwd or os.getcwd()).resolve()
    root = repo_root(cwd)
    writer = role['sandbox_mode'] != 'read-only'
    run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    folder = state_dir(root) / run_id
    folder.mkdir(parents=True, exist_ok=False)
    meta = {'run': run_id, 'role': role['name'], 'model': role['model'],
            'effort': role['model_reasoning_effort'], 'sandbox': role['sandbox_mode'],
            'repo': str(root) if root else None, 'cwd': str(cwd), 'started': time.time()}
    workdir = cwd
    if writer and root and not args.in_place:
        base = git(['rev-parse', 'HEAD'], root)
        branch = 'als/' + run_id
        worktree = folder / 'worktree'
        git(['worktree', 'add', '-q', '-b', branch, str(worktree), base], root)
        relative = cwd.relative_to(root) if cwd != root else Path('.')
        workdir = worktree / relative
        meta.update(worktree=str(worktree), branch=branch, base=base)
    codex = codex_bin()
    base_url = native_base_url(codex)
    prompt = build_prompt(role, task)
    (folder / 'prompt.md').write_text(prompt, encoding='utf-8')
    last_file = folder / 'last.md'
    argv = build_command(codex, role, workdir, last_file, base_url)
    meta.update(argv=argv[:-1], base_url=base_url)
    if args.dry_run:
        meta['status'] = 'dry-run'
        write_meta(folder, meta)
        print(json.dumps(meta, indent=2))
        return 0
    timed_out = False
    with open(folder / 'events.jsonl', 'wb') as events, open(folder / 'stderr.txt', 'wb') as errors:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=events, stderr=errors,
                                start_new_session=True)
        try:
            proc.communicate(prompt.encode('utf-8'), timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
    meta.update(ended=time.time(), exit_code=proc.returncode, timed_out=timed_out,
                usage=parse_usage((folder / 'events.jsonl').read_text(encoding='utf-8', errors='replace')))
    if meta.get('worktree'):
        git(['add', '-A'], meta['worktree'])
        meta['changed'] = git(['diff', '--cached', '--stat', meta['base']], meta['worktree']).splitlines()[-30:]
        git(['reset', '-q'], meta['worktree'])
    meta['status'] = 'timeout' if timed_out else ('finished' if proc.returncode == 0 else 'failed')
    write_meta(folder, meta)
    print_report(folder, meta, args.json)
    return 124 if timed_out else (0 if proc.returncode == 0 else 1)


def write_meta(folder, meta):
    (folder / 'meta.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')


def print_report(folder, meta, as_json=False):
    last = (folder / 'last.md').read_text(encoding='utf-8', errors='replace') if (folder / 'last.md').exists() else ''
    if as_json:
        print(json.dumps(dict(meta, report=last[:MAX_REPORT_CHARS], folder=str(folder)), indent=2, ensure_ascii=False))
        return
    usage = meta.get('usage') or {}
    print('run %s  role %s (%s/%s)  status %s' % (meta['run'], meta['role'], meta['model'], meta['effort'],
                                                  meta.get('status')))
    if usage:
        print('usage: input %s (cached %s), output %s' % (usage.get('input_tokens', 0),
                                                          usage.get('cached_input_tokens', 0),
                                                          usage.get('output_tokens', 0)))
    if meta.get('worktree'):
        print('worktree %s  branch %s' % (meta['worktree'], meta['branch']))
        for line in meta.get('changed') or ['(no changes)']:
            print('  ' + line)
    if meta.get('status') != 'finished':
        errors = (folder / 'stderr.txt').read_text(encoding='utf-8', errors='replace').strip().splitlines()[-5:]
        for line in errors:
            print('stderr: ' + line[:300])
    print('--- report ---')
    print(last[:MAX_REPORT_CHARS] + ('\n[truncated; full: %s]' % (folder / 'last.md') if len(last) > MAX_REPORT_CHARS else ''))


def find_run(root, run_id):
    folder = state_dir(root) / run_id
    if not (folder / 'meta.json').is_file():
        raise UsageError('unknown run %s' % run_id)
    return folder, json.loads((folder / 'meta.json').read_text(encoding='utf-8'))


def cmd_ls(args):
    folder = state_dir(repo_root(Path(args.cwd or os.getcwd())))
    rows = []
    for meta_file in sorted(folder.glob('*/meta.json'))[-args.limit:]:
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        rows.append('%-24s %-18s %-9s %s' % (meta['run'], meta['role'], meta.get('status'), meta.get('branch') or '-'))
    print('\n'.join(rows) if rows else 'no runs for this repository')
    return 0


def cmd_show(args):
    folder, meta = find_run(repo_root(Path(args.cwd or os.getcwd())), args.run)
    print_report(folder, meta, args.json)
    return 0


def cmd_clean(args):
    root = repo_root(Path(args.cwd or os.getcwd()))
    folder, meta = find_run(root, args.run)
    if not meta.get('worktree'):
        print('run %s has no worktree' % args.run)
        return 0
    if root and Path(meta['worktree']).exists():
        git(['worktree', 'remove', '--force', meta['worktree']], root)
    meta['worktree_removed'] = True
    write_meta(folder, meta)
    print('removed worktree of %s; branch %s kept (delete with: git branch -D %s)'
          % (args.run, meta['branch'], meta['branch']))
    return 0


def parser():
    p = argparse.ArgumentParser(prog='als-gpt', description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    run = sub.add_parser('run', help='run one GPT worker')
    run.add_argument('role')
    run.add_argument('task', help='task file, or - for stdin')
    run.add_argument('--cwd')
    run.add_argument('--in-place', action='store_true', help='writer works in the current checkout (no worktree)')
    run.add_argument('--timeout', type=int, default=3600)
    run.add_argument('--dry-run', action='store_true', help='print the codex command without running it')
    run.add_argument('--json', action='store_true')
    sub.add_parser('roles', help='list roles')
    ls = sub.add_parser('ls', help='recent runs for this repository')
    ls.add_argument('--cwd')
    ls.add_argument('--limit', type=int, default=20)
    for name in ('show', 'clean'):
        q = sub.add_parser(name)
        q.add_argument('run')
        q.add_argument('--cwd')
        if name == 'show':
            q.add_argument('--json', action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    handlers = {'run': cmd_run, 'roles': cmd_roles, 'ls': cmd_ls, 'show': cmd_show, 'clean': cmd_clean}
    try:
        return handlers[args.cmd](args)
    except UsageError as error:
        print('als-gpt: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
