"""Shared plumbing for the Codex port of Claude Code's team runtime.

Claude Code resolves agent definitions (`.claude/agents/*.md`), runs them on its
own models and keeps team state in memory. On Codex the same pieces are:

* role files: `<project>/.codex/agents/*.toml`, then `$CODEX_HOME/agents/*.toml`;
* one model call: a `codex exec` process (`codex exec resume` for a later turn);
* shared state: files under `_workspace/` at the repository top level.

Everything here is standard library only (Python 3.9+) and never talks to a
model by itself; it only builds and runs `codex` command lines.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # pragma: no cover - exercised on Python 3.9/3.10
    _toml = None

NATIVE_CHATGPT_URL = 'https://chatgpt.com/backend-api/codex'
NATIVE_API_URL = 'https://api.openai.com/v1'
SANDBOXES = ('read-only', 'workspace-write', 'danger-full-access')


class UsageError(Exception):
    """Bad arguments or environment (exit code 2)."""


class CommandError(Exception):
    """A command or model call failed (exit code 1)."""


# ------------------------------------------------------------------ paths

def git_toplevel(cwd):
    try:
        out = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=str(cwd),
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return Path(out.stdout.strip()).resolve()


def project_root(cwd=None):
    """Git top level of `cwd`, else `cwd` itself."""
    cwd = Path(cwd or os.getcwd()).resolve()
    return git_toplevel(cwd) or cwd


def workspace_dir(cwd=None):
    """`_workspace` at the git top level of cwd (or cwd); `ALS_WORKSPACE` overrides."""
    env = os.environ.get('ALS_WORKSPACE')
    if env:
        return (Path(cwd or os.getcwd()) / env).resolve()
    return project_root(cwd) / '_workspace'


def write_json(path, data):
    """Atomic JSON write (temp file + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write('\n')
        os.replace(tmp, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def append_jsonl(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')


@contextlib.contextmanager
def file_lock(path):
    """Exclusive advisory lock (fcntl) on `path`, created if missing."""
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def valid_name(name):
    return bool(re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$', name or ''))


# ------------------------------------------------------------------ roles

def parse_role(text):
    """Read a role TOML file. Uses tomllib when available, else a minimal reader
    for `key = "..."`, `key = '...'` and triple-quoted strings."""
    if _toml is not None:
        try:
            data = _toml.loads(text)
            return {k: v for k, v in data.items() if isinstance(v, str)}
        except ValueError:
            pass
    return _parse_role_minimal(text)


def _parse_role_minimal(text):
    values, lines, i = {}, text.splitlines(), 0
    escapes = {'n': '\n', 't': '\t', '"': '"', '\\': '\\'}
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith('#') or line.startswith('[') or '=' not in line:
            continue
        key, value = (part.strip() for part in line.split('=', 1))
        quote = value[:3] if value[:3] in ('"""', "'''") else None
        if quote:
            body = value[3:]
            if body.endswith(quote):
                values[key] = body[:-3]
                continue
            collected = [body] if body else []
            while i < len(lines) and not lines[i].rstrip().endswith(quote):
                collected.append(lines[i])
                i += 1
            if i < len(lines):
                collected.append(lines[i].rstrip()[:-3])
                i += 1
            text_value = '\n'.join(collected)
            if quote == '"""':
                text_value = re.sub(r'\\(.)', lambda m: escapes.get(m.group(1), m.group(0)), text_value)
            values[key] = text_value
        elif value.startswith('"'):
            match = re.match(r'"((?:[^"\\]|\\.)*)"', value)
            if match:
                values[key] = re.sub(r'\\(.)', lambda m: escapes.get(m.group(1), m.group(0)), match.group(1))
        elif value.startswith("'"):
            match = re.match(r"'([^']*)'", value)
            if match:
                values[key] = match.group(1)
    return values


def role_dirs(cwd=None):
    codex_home = os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')
    return [project_root(cwd) / '.codex' / 'agents', Path(codex_home) / 'agents']


def find_role(agent_type, cwd=None):
    """Resolve a role by its `name` (or file stem): project first, then global."""
    for folder in role_dirs(cwd):
        if not folder.is_dir():
            continue
        by_stem = None
        for path in sorted(folder.glob('*.toml')):
            try:
                role = parse_role(path.read_text(encoding='utf-8'))
            except OSError:
                continue
            role['_path'] = str(path)
            if role.get('name') == agent_type:
                return role
            if path.stem == agent_type and by_stem is None:
                by_stem = role
        if by_stem is not None:
            by_stem.setdefault('name', agent_type)
            return by_stem
    raise UsageError('unknown agent type %r (looked in %s)' % (
        agent_type, ', '.join(str(p) for p in role_dirs(cwd))))


def role_pins(role):
    sandbox = role.get('sandbox_mode') or 'read-only'
    if sandbox not in SANDBOXES:
        raise UsageError('role %s has invalid sandbox_mode %r' % (role.get('name'), sandbox))
    return {'model': role.get('model') or None,
            'effort': role.get('model_reasoning_effort') or None,
            'sandbox': sandbox}


# ------------------------------------------------------------------ codex

def codex_bin():
    found = os.environ.get('ALS_CODEX') or shutil.which('codex')
    if not found:
        raise UsageError('codex CLI not found on PATH (or set ALS_CODEX)')
    return found


def native_base_url(codex):
    """Endpoint that bypasses any proxy set in config.toml.

    `ALS_CODEX_BASE_URL` overrides (empty: no override); otherwise the login
    kind decides: API key -> api.openai.com, ChatGPT -> chatgpt.com backend."""
    override = os.environ.get('ALS_CODEX_BASE_URL')
    if override is not None:
        return override or None
    try:
        status = subprocess.run([codex, 'login', 'status'], capture_output=True, text=True, timeout=30)
        text = status.stdout + status.stderr
    except (OSError, subprocess.SubprocessError):
        return NATIVE_CHATGPT_URL
    return NATIVE_API_URL if 'api key' in text.lower() else NATIVE_CHATGPT_URL


def pin_args(base_url, model, effort, sandbox_flag=None, sandbox_config=None):
    args = []
    if base_url:
        args += ['-c', 'openai_base_url="%s"' % base_url]
    if model:
        args += ['-m', model]
    if effort:
        args += ['-c', 'model_reasoning_effort="%s"' % effort]
    if sandbox_flag:
        args += ['-s', sandbox_flag]
    if sandbox_config:
        # `codex exec resume` has no -s flag; the config key sets the same policy.
        args += ['-c', 'sandbox_mode="%s"' % sandbox_config]
    return args


def exec_argv(codex, base_url, model, effort, sandbox, cwd, last_file, schema_file=None, ephemeral=True):
    argv = [codex, 'exec'] + pin_args(base_url, model, effort, sandbox_flag=sandbox)
    argv += ['--cd', str(cwd), '--skip-git-repo-check']
    if ephemeral:
        argv.append('--ephemeral')
    argv += ['--json', '-o', str(last_file)]
    if schema_file:
        argv += ['--output-schema', str(schema_file)]
    argv.append('-')  # prompt on stdin
    return argv


def resume_argv(codex, base_url, model, effort, sandbox, session_id, last_file):
    """`codex exec resume [OPTIONS] SESSION_ID -` (options after `resume`, per --help)."""
    argv = [codex, 'exec', 'resume'] + pin_args(base_url, model, effort, sandbox_config=sandbox)
    argv += ['--skip-git-repo-check', '--json', '-o', str(last_file), session_id, '-']
    return argv


def parse_events(text):
    """Sum `turn.completed` usage, find the thread id and the last error message."""
    usage = {'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0}
    thread_id = error = None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind == 'thread.started' and event.get('thread_id'):
            thread_id = event['thread_id']
        elif kind == 'turn.completed' and isinstance(event.get('usage'), dict):
            for key, value in event['usage'].items():
                if isinstance(value, int):
                    usage[key] = usage.get(key, 0) + value
        elif kind in ('error', 'turn.failed'):
            detail = event.get('message') or event.get('error')
            if isinstance(detail, dict):
                detail = detail.get('message')
            error = str(detail or kind)[:500]
    return usage, thread_id, error


def billable(usage):
    """Uncached input plus output tokens."""
    usage = usage or {}
    return max(0, usage.get('input_tokens', 0) - usage.get('cached_input_tokens', 0)) + usage.get('output_tokens', 0)


def add_usage(total, usage):
    for key, value in (usage or {}).items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value
    return total


def run_codex(argv, prompt, cwd, timeout):
    """Run one codex process with the prompt on stdin.

    Returns a dict: exit_code, timed_out, reply (last message or ''), usage,
    thread_id, error, stderr_tail. Never raises for model failures."""
    last_file = None
    for i, arg in enumerate(argv):
        if arg == '-o' and i + 1 < len(argv):
            last_file = Path(argv[i + 1])
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                    cwd=str(cwd), start_new_session=True)
        except OSError as error:
            return {'exit_code': None, 'timed_out': False, 'reply': '', 'usage': {}, 'thread_id': None,
                    'error': 'cannot start codex: %s' % error, 'stderr_tail': ''}
        timed_out = False
        try:
            proc.communicate(prompt.encode('utf-8'), timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        out.seek(0)
        err.seek(0)
        events = out.read().decode('utf-8', 'replace')
        stderr = err.read().decode('utf-8', 'replace')
    usage, thread_id, error = parse_events(events)
    reply = ''
    if last_file and last_file.is_file():
        reply = last_file.read_text(encoding='utf-8', errors='replace')
    tail = '\n'.join(stderr.strip().splitlines()[-5:])[-1000:]
    if timed_out:
        error = 'timeout after %ss' % timeout
    elif proc.returncode != 0:
        error = error or ('codex exited %s: %s' % (proc.returncode, tail or 'no stderr'))
    return {'exit_code': proc.returncode, 'timed_out': timed_out, 'reply': reply, 'usage': usage,
            'thread_id': thread_id, 'error': error if (timed_out or proc.returncode) else None,
            'stderr_tail': tail}


def now():
    return time.time()
