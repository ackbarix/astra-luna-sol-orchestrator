"""Small shared primitives; standard library only, no model or network calls."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
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


def boot_id():
    """Identity of the current kernel boot, or None where /proc is unavailable."""
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except OSError:
        return None


def process_info(pid):
    """(state letter, start time in clock ticks) from /proc, or None."""
    try:
        data = Path('/proc/' + str(int(pid)) + '/stat').read_bytes()
    except (OSError, ValueError):
        return None
    fields = data[data.rindex(b')') + 2:].split()
    try:
        return fields[0].decode(), int(fields[19])
    except (IndexError, ValueError):
        return None


def process_start(pid):
    info = process_info(pid)
    return info[1] if info else None


def process_alive(pid, start=None):
    """True when pid exists, is not a zombie and (when known) has the recorded
    start time, i.e. the pid was not recycled by an unrelated process."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    info = process_info(pid)
    if info is None:
        return True
    state, current_start = info
    if state in ('Z', 'X'):
        return False
    return start is None or current_start == start
