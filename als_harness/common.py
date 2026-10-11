"""Small shared primitives; standard library only, no model or network calls."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time


class HarnessError(ValueError):
    """An unsafe, unsupported or incomplete operation; never a model failure."""


ROLES = {
    'als_explorer': ('gpt-6-luna', 'medium', False),
    'als_researcher': ('gpt-6-luna', 'medium', False),
    'als_tester': ('gpt-6-luna', 'medium', True),
    'als_worker': ('gpt-6-luna', 'max', True),
    'als_test_designer': ('gpt-6-luna', 'max', True),
    'als_hard_worker': ('gpt-6.1-sol', 'high', True),
    'als_reviewer': ('gpt-6.1-sol', 'medium', False),
    'als_reviewer_deep': ('gpt-6.1-sol', 'high', False),
}
# Worker ownership may never cover VCS metadata, harness state or the Codex
# hook configuration that enforces the harness itself.
RESERVED_TOP_LEVEL = {'.git', '.astra-luna-sol', '.codex'}
ACTIVE = {'reserved', 'running', 'uncertain'}
TERMINAL = {'completed', 'cancelled'}


def counted_tokens(usage):
    """Budget/estimate unit: uncached input plus output.

    Cached input is part of input_tokens but is billed and rate-limited very
    differently; counting it fully exhausts soft budgets on long sessions.
    """
    cached = usage.get('cached_input_tokens') or 0
    return max(0, usage['input_tokens'] - cached) + usage['output_tokens']


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value).encode()).hexdigest()


def now():
    return time.time()


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
        raise HarnessError('Invalid identifier')
    return value


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise HarnessError('Invalid relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise HarnessError('Path must remain inside the project')
    if path.parts[0] in RESERVED_TOP_LEVEL or any(c in value for c in '*?[]'):
        raise HarnessError('Reserved path or glob in file ownership')
    return path.as_posix().rstrip('/')


def checked_path(root: Path, rel: str):
    """Reject links at every component, including dangling links."""
    p = PurePosixPath(rel)
    if p.is_absolute() or '..' in p.parts or '\\' in rel:
        raise HarnessError('Unsafe internal path')
    result = root
    for component in p.parts:
        result = result / component
        if result.is_symlink():
            raise HarnessError('Symlink refused: ' + str(result))
    return result


def private_dir(root: Path, rel: str):
    path = checked_path(root, rel)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise HarnessError('State location is not a directory')
    os.chmod(path, 0o700)
    return path


def overlap(a, b):
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')


_SECRET_NAMES = (r'(?:api[_-]?key|private[_-]?key|access[_-]?key|token|password|passwd|secret|credentials?)')
_SECRET_ASSIGNMENT = re.compile(
    r'(?i)(authorization\s*[:=]\s*|bearer\s+|' + _SECRET_NAMES + r'["\']?\s*[=:]\s*["\']?)[^\s,;"\']+')
_SECRET_FLAG = re.compile(r'(?i)(--' + _SECRET_NAMES.replace('[_-]', '-') + r'\s+)[^\s,;-][^\s,;]*')
_SECRET_LITERAL = re.compile(
    r'\b(?:gh[pousr]_[A-Za-z0-9_]+'
    r'|github_pat_[A-Za-z0-9_]+'
    r'|sk-[A-Za-z0-9_-]{16,}'
    r'|AKIA[0-9A-Z]{16}'
    r'|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)')


def redact(value):
    """Mask common credential shapes. Best effort, not a guarantee: keep raw
    verification logs local and private."""
    text = str(value)
    text = _SECRET_ASSIGNMENT.sub(r'\1[REDACTED]', text)
    text = _SECRET_FLAG.sub(r'\1[REDACTED]', text)
    return _SECRET_LITERAL.sub('[REDACTED]', text)


def _command_output(argv):
    try:
        # C locale keeps ps/sysctl output stable across user locales.
        result = subprocess.run(argv, capture_output=True, timeout=5, env=dict(os.environ, LC_ALL='C'))
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stdout.decode('utf-8', 'replace').strip()
    return text if result.returncode == 0 and text else None


def boot_id():
    """Identity of the current boot: Linux boot_id, else BSD/macOS
    kern.boottime; None when neither is available."""
    try:
        value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if value:
            return value
    except OSError:
        pass
    if sys.platform.startswith('linux'):
        return None
    return _command_output(['sysctl', '-n', 'kern.boottime'])


def process_info(pid):
    """(state, start time) of pid, or None when unavailable.

    Linux reads /proc/<pid>/stat (start in clock ticks since boot). Other
    POSIX systems (macOS) fall back to `ps -o stat=,lstart=`; the start is
    then an opaque string, compared only with values recorded on the same
    host.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        data = Path('/proc/' + str(pid) + '/stat').read_bytes()
    except OSError:
        data = None
    if data is not None:
        fields = data[data.rindex(b')') + 2:].split()
        try:
            return fields[0].decode(), int(fields[19])
        except (IndexError, ValueError):
            return None
    if sys.platform.startswith('linux'):
        return None
    try:
        result = subprocess.run(['ps', '-o', 'stat=,lstart=', '-p', str(pid)], capture_output=True, timeout=5,
                                env=dict(os.environ, LC_ALL='C'))
    except (OSError, subprocess.SubprocessError):
        return None  # ps unavailable: unknown, callers stay conservative
    line = result.stdout.decode('utf-8', 'replace').strip()
    if not line:
        # ps ran but lists no such process. A running process is always
        # listed; macOS omits exited (zombie) processes even though kill(pid, 0)
        # still succeeds for them. Report it as exited, not unknown.
        return ('X', None) if result.returncode in (0, 1) else None
    if len(line.split(None, 1)) != 2:
        return None
    state, started = line.split(None, 1)
    return state[0], ' '.join(started.split())


def process_start(pid):
    info = process_info(pid)
    return info[1] if info else None


def process_alive(pid, start=None):
    """True when pid exists, is not a zombie and (when known) has the recorded
    start time, i.e. the pid was not recycled by an unrelated process.

    Every pid ALS records (verify processes, check process groups) runs as the
    current user, so EPERM from kill(pid, 0) proves the pid now belongs to
    another user's process: ours has exited and the pid was recycled. That is
    reported as not alive rather than "unknown" (on hardened /proc, e.g.
    hidepid, another user's process is also unreadable, which used to be
    treated as alive and kept dead lock owners live)."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    info = process_info(pid)
    if info is None:
        return True
    state, current_start = info
    if state in ('Z', 'X'):
        return False
    return start is None or current_start == start


def _rev_parse(path, flag):
    try:
        done = subprocess.run(['git', '-C', str(path), 'rev-parse', flag], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    value = os.fsdecode(done.stdout.strip())
    return value if done.returncode == 0 and value else None


def git_toplevel(path):
    """Absolute top level of the worktree containing path, or None."""
    top = _rev_parse(path, '--show-toplevel')
    return Path(top).resolve() if top else None


def git_common_dir(path):
    """Absolute git common directory shared by every worktree of path's
    repository, or None. Old git prints it relative to cwd (no --path-format)."""
    path = Path(path).resolve()
    common = _rev_parse(path, '--git-common-dir')
    if common is None:
        return None
    candidate = (path / common).resolve()
    if candidate.is_dir():
        return candidate
    top = git_toplevel(path)
    candidate = (top / common).resolve() if top else None
    return candidate if candidate is not None and candidate.is_dir() else None
