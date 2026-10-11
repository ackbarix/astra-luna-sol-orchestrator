"""Conservative code fingerprint for evidence freshness. Never checks out/stashes."""
from __future__ import annotations
import os
from pathlib import Path
import stat
import subprocess
from .common import HarnessError, digest

MAX_FILES, MAX_BYTES = 25000, 256 * 1024 * 1024

def git(project, *args):
    r = subprocess.run(['git', '-C', str(project), *args], capture_output=True, timeout=15)
    if r.returncode:
        raise HarnessError('Git snapshot failed; use an initialized repository')
    return r.stdout

def capture(project: Path):
    root = Path(os.fsdecode(git(project, 'rev-parse', '--show-toplevel').strip())).resolve()
    if root != project.resolve():
        raise HarnessError('Manage the Git worktree root, not a nested project directory')
    # Tracked files plus nonignored untracked files. Runtime evidence excluded.
    paths = set(git(project, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')) - {b''}
    if len(paths) > MAX_FILES:
        raise HarnessError('Snapshot file limit exceeded; no completion certification')
    records, total = [], 0
    for raw in sorted(paths):
        rel = os.fsdecode(raw)
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
            records.append((rel, 'deleted')); continue
        if stat.S_ISLNK(before.st_mode):
            records.append((rel, 'symlink', os.readlink(path))); continue
        if not stat.S_ISREG(before.st_mode):
            raise HarnessError('Unsupported snapshot path (submodule/special file): ' + rel)
        total += before.st_size
        if total > MAX_BYTES:
            raise HarnessError('Snapshot byte limit exceeded')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as f:
            data = f.read(MAX_BYTES + 1)
            after = os.fstat(f.fileno())
        if (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
            raise HarnessError('Code changed while fingerprinting; retry when stable')
        records.append((rel, stat.S_IMODE(before.st_mode), digest(data)))
    head = subprocess.run(['git','-C',str(project),'rev-parse','--verify','HEAD'], capture_output=True, timeout=10)
    branch = subprocess.run(['git','-C',str(project),'symbolic-ref','-q','HEAD'], capture_output=True, timeout=10)
    state = {'head':head.stdout.decode().strip() if not head.returncode else 'unborn',
             'branch':branch.stdout.decode().strip() if not branch.returncode else 'detached',
             'files':records}
    state['digest'] = digest(state)
    return state
