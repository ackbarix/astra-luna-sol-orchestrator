"""Conservative code fingerprint for evidence freshness. Never checks out/stashes."""
from __future__ import annotations
import os
from pathlib import Path
import stat
import subprocess
from .common import HarnessError, digest

MAX_FILES = 25000
MAX_BYTES = 256 * 1024 * 1024


def git(project, *args):
    r = subprocess.run(['git', '-C', str(project), *args], capture_output=True, timeout=15)
    if r.returncode:
        raise HarnessError('Git snapshot failed; use an initialized repository')
    return r.stdout


def stable_name(raw: bytes) -> str:
    """Digest-safe name: UTF-8 text unchanged (keeps old digests), other
    byte strings as 'hex:<bytes>' so non-UTF-8 filenames never break JSON."""
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return 'hex:' + raw.hex()


def nested_head(path: Path) -> str:
    """HEAD of a nested repository/submodule checkout, or 'untracked-repo'.

    Only a directory with its own .git entry is asked; otherwise git would
    walk up and report the enclosing repository's HEAD. Changes inside the
    nested worktree are not fingerprinted, only its HEAD.
    """
    if not os.path.lexists(os.path.join(path, '.git')):
        return 'untracked-repo'
    result = subprocess.run(['git', '-C', str(path), 'rev-parse', '--verify', '-q', 'HEAD'],
                            capture_output=True, timeout=15)
    value = result.stdout.strip()
    if result.returncode or not value:
        return 'untracked-repo'
    return stable_name(value)


def bytecode_cache(raw: bytes) -> bool:
    """True for Python bytecode caches: anything under a `__pycache__/`
    directory, or a `*.pyc` / `*.pyo` file."""
    parts = raw.rstrip(b'/').split(b'/')
    return b'__pycache__' in parts or parts[-1].endswith((b'.pyc', b'.pyo'))


def capture(project: Path):
    """Fingerprint tracked files plus nonignored untracked files.

    UNTRACKED Python bytecode caches (bytecode_cache) are excluded: running
    a Python check writes them into repositories that do not ignore them,
    which would otherwise fail the first verify with
    code_changed_during_check. Tracked .pyc files stay fingerprinted.
    """
    root = Path(os.fsdecode(git(project, 'rev-parse', '--show-toplevel').strip())).resolve()
    if root != project.resolve():
        raise HarnessError('Manage the Git worktree root, not a nested project directory')
    # Tracked files plus nonignored untracked files. Runtime evidence excluded.
    # An untracked nested repository is listed once as 'dir/'.
    tracked = git(project, 'ls-files', '-z', '--cached').split(b'\0')
    others = git(project, 'ls-files', '-z', '--others', '--exclude-standard').split(b'\0')
    paths = ({raw.rstrip(b'/') for raw in tracked}
             | {raw.rstrip(b'/') for raw in others if not bytecode_cache(raw)}) - {b''}
    if len(paths) > MAX_FILES:
        raise HarnessError('Snapshot file limit exceeded; no completion certification')
    records = []
    total = 0
    for raw in sorted(paths):
        rel = os.fsdecode(raw)
        key = stable_name(raw)
        if rel == '.astra-luna-sol' or rel.startswith('.astra-luna-sol/'):
            continue
        path = project / rel
        if '..' in Path(rel).parts or Path(rel).is_absolute():
            raise HarnessError('Unsafe Git path')
        if any(p.is_symlink() for p in path.parents if p != project and project in p.parents):
            raise HarnessError('Symlinked snapshot directory')
        try:
            before = path.lstat()
        except FileNotFoundError:
            records.append((key, 'deleted'))
            continue
        if stat.S_ISLNK(before.st_mode):
            records.append((key, 'symlink', stable_name(os.readlink(os.fsencode(path)))))
            continue
        if stat.S_ISDIR(before.st_mode):
            # Submodule gitlink or untracked nested repository.
            records.append((key, 'gitlink', nested_head(path)))
            continue
        if not stat.S_ISREG(before.st_mode):
            records.append((key, 'special', stat.S_IFMT(before.st_mode)))
            continue
        total += before.st_size
        if total > MAX_BYTES:
            raise HarnessError('Snapshot byte limit exceeded')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as f:
            data = f.read(MAX_BYTES + 1)
            after = os.fstat(f.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise HarnessError('Code changed while fingerprinting; retry when stable')
        records.append((key, stat.S_IMODE(before.st_mode), digest(data)))
    head = subprocess.run(['git', '-C', str(project), 'rev-parse', '--verify', 'HEAD'], capture_output=True, timeout=10)
    branch = subprocess.run(['git', '-C', str(project), 'symbolic-ref', '-q', 'HEAD'], capture_output=True, timeout=10)
    state = {'head': stable_name(head.stdout.strip()) if not head.returncode else 'unborn',
             'branch': stable_name(branch.stdout.strip()) if not branch.returncode else 'detached',
             'files': records}
    state['digest'] = digest(state)
    return state
