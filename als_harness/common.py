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
ACTIVE = {'reserved', 'running', 'uncertain'}
TERMINAL = {'completed', 'cancelled'}

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
    if path.parts[0] in {'.git', '.astra-luna-sol'} or any(c in value for c in '*?[]'):
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

def redact(value):
    # Best effort, not a guarantee: keep raw verification logs local and private.
    s = str(value)
    s = re.sub(r'(?i)(authorization\s*[:=]\s*|bearer\s+|(?:api[_-]?key|token|password)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', s)
    return re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,})\b', '[REDACTED]', s)
