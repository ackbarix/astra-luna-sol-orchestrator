"""SQLite single source of truth. Reservations and transitions share transactions."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
from .common import HarnessError, checked_path, git_common_dir, git_toplevel, private_dir, encoded, now

SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, session TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT, run TEXT, doc TEXT NOT NULL, PRIMARY KEY(run,id));
CREATE TABLE IF NOT EXISTS events(key TEXT PRIMARY KEY, run TEXT, kind TEXT, stamp REAL, doc TEXT);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY, session TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage(run TEXT, thread TEXT, response TEXT, doc TEXT NOT NULL,
 PRIMARY KEY(run,thread,response));
CREATE INDEX IF NOT EXISTS tasks_by_run ON tasks(run);
CREATE INDEX IF NOT EXISTS events_by_run ON events(run,stamp);
PRAGMA user_version=1;
'''


SCOPES = ('project', 'global')
PROJECT_STATE = '.astra-luna-sol/state'
PROJECT_EVIDENCE = '.astra-luna-sol/evidence'
DB_NAME = 'runs.sqlite'
# CLI commands may wait longer for a concurrent writer; hooks must stay fast.
CLI_BUSY_MS = 5000
HOOK_BUSY_MS = 2000


def _scope(scope):
    if scope not in SCOPES:
        raise HarnessError('Unknown state scope: ' + str(scope))
    return scope


def global_base() -> Path:
    """$ALS_STATE_HOME, else ${XDG_STATE_HOME:-~/.local/state}/als; always absolute.

    A relative ALS_STATE_HOME is refused (it would move with the current
    directory, splitting state between commands and hooks). A relative
    XDG_STATE_HOME is ignored, as the XDG Base Directory spec requires.
    """
    explicit = os.environ.get('ALS_STATE_HOME')
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise HarnessError('ALS_STATE_HOME must be an absolute path (got a relative path: '
                               + explicit[:200] + ')')
        return path
    xdg = os.environ.get('XDG_STATE_HOME')
    base = Path(xdg).expanduser() if xdg else None
    if base is None or not base.is_absolute():
        base = Path.home() / '.local/state'
    return base / 'als'


def state_dir(project: Path, scope: str = 'project') -> Path:
    """Directory holding the database (and, in global scope, evidence)."""
    root = project.resolve()
    if _scope(scope) == 'project':
        return root / PROJECT_STATE
    return global_base() / hashlib.sha256(str(root).encode('utf-8', 'surrogateescape')).hexdigest()[:16]


def locate(project: Path, scope: str = 'project') -> Path:
    """Path of the state database for a project, without creating anything.

    Contract (0.5): every caller that needs to know whether state exists or
    where evidence lives must use this (and evidence_root) instead of
    hardcoding paths. 'project' keeps state inside the worktree;
    'global' keeps it under a per-project directory outside the worktree.
    """
    return state_dir(project, scope) / DB_NAME


def evidence_root(project: Path, scope: str = 'project') -> Path:
    """Directory that holds verification logs, without creating anything."""
    if _scope(scope) == 'project':
        return project.resolve() / PROJECT_EVIDENCE
    return state_dir(project, scope) / 'evidence'


class Store:
    def __init__(self, project: Path, scope: str = 'project', busy_ms: int = CLI_BUSY_MS):
        self.project = project.resolve(strict=True)
        self.scope = _scope(scope)
        if self.scope == 'project':
            self.state_dir = private_dir(self.project, PROJECT_STATE)
            self.path = checked_path(self.project, PROJECT_STATE + '/' + DB_NAME)
            for tail in ('', '-wal', '-shm', '-journal'):
                checked_path(self.project, PROJECT_STATE + '/' + DB_NAME + tail)
        else:
            base = global_base()
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder = state_dir(self.project, 'global')
            self.state_dir = private_dir(base, folder.name)
            for tail in ('', '-wal', '-shm', '-journal'):
                checked_path(self.state_dir, DB_NAME + tail)
            self.path = self.state_dir / DB_NAME
            self._write_project_marker()
        self.evidence_root = evidence_root(self.project, self.scope)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path, timeout=busy_ms / 1000, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=%d' % int(busy_ms))
        try:
            # WAL: readers never block the writer (verify subprocesses, hooks
            # and the CLI share this file). Persistent once set; if another
            # process holds a lock right now, keep the current mode.
            self.db.execute('PRAGMA journal_mode=WAL')
        except sqlite3.OperationalError:
            pass
        v = self.db.execute('PRAGMA user_version').fetchone()[0]
        if v not in (0, 1):
            self.db.close()
            raise HarnessError('Unsupported future state schema; database left unchanged')
        if not v:
            self.db.executescript(SCHEMA)

    def _write_project_marker(self):
        """Human-readable pointer from a hashed global directory to its project."""
        marker = checked_path(self.state_dir, 'project.txt')
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            return
        with os.fdopen(fd, 'w', encoding='utf-8', errors='surrogateescape') as handle:
            handle.write(str(self.project) + '\n')

    def evidence_dir(self, rid):
        """Private per-run evidence directory, created on demand."""
        if self.scope == 'project':
            return private_dir(self.project, PROJECT_EVIDENCE + '/' + rid)
        return private_dir(self.state_dir, 'evidence/' + rid)

    def evidence_record_path(self, path: Path) -> str:
        """Stored form of a log path: project-relative (project scope) or absolute (global)."""
        if self.scope == 'project':
            return str(path.relative_to(self.project))
        return str(path)

    def evidence_path(self, recorded) -> Path:
        """Resolve a stored log path; only files inside evidence_root, no symlinks."""
        if not isinstance(recorded, str) or not recorded or '\x00' in recorded:
            raise HarnessError('Invalid evidence path')
        candidate = PurePosixPath(recorded)
        if self.scope == 'project':
            if candidate.is_absolute():
                raise HarnessError('Project-scope evidence paths are project-relative')
            prefix = PurePosixPath(PROJECT_EVIDENCE)
            anchor, inner = self.project, candidate
        else:
            if not candidate.is_absolute():
                raise HarnessError('Global-scope evidence paths are absolute')
            prefix = PurePosixPath('evidence')
            anchor = self.state_dir
            inner = PurePosixPath(os.path.relpath(recorded, str(self.state_dir)))
        if '..' in candidate.parts or '..' in inner.parts:
            raise HarnessError('Evidence path outside evidence root')
        try:
            inner.relative_to(prefix)
        except ValueError:
            raise HarnessError('Evidence path outside evidence root') from None
        if inner == prefix:
            raise HarnessError('Evidence path outside evidence root')
        return checked_path(anchor, inner.as_posix())

    def close(self):
        self.db.close()

    _common = False

    def common_dir(self):
        """Git common dir of the managed project (cached), or None outside git."""
        if self._common is False:
            self._common = git_common_dir(self.project)
        return self._common

    def belongs(self, path):
        """True when path is inside the main checkout or inside any linked
        worktree of the same repository (same git common dir)."""
        path = Path(path).resolve()
        if path == self.project or self.project in path.parents:
            return True
        mine = self.common_dir()
        return mine is not None and path.is_dir() and git_common_dir(path) == mine

    def worktree_of(self, path):
        """Top level of the worktree of this repository that contains path, or None.

        A linked worktree nested under the main checkout maps to itself; any
        other directory under the main checkout (including a nested foreign
        repository) maps to the project.
        """
        path = Path(path).resolve()
        if not path.is_dir():
            return None
        top = git_toplevel(path)
        mine = self.common_dir()
        if top is not None and mine is not None and git_common_dir(top) == mine:
            return top
        if path == self.project or self.project in path.parents:
            return self.project
        return None

    def run_root(self, run):
        """Worktree a run works in: its recorded worktree, else the project (pre-0.5.1 runs).

        Not validated: use it to compare runs (same root = same files) or to
        display. Anything that reads or runs code in the worktree must use
        checked_run_root()."""
        recorded = run.get('worktree') if isinstance(run, dict) else None
        return Path(recorded) if isinstance(recorded, str) and recorded else self.project

    def run_key(self, run):
        """Resolved run_root() as a string: runs with equal keys share files."""
        return os.path.realpath(str(self.run_root(run)))

    def checked_run_root(self, run):
        """run_root() after confirming it is still this repository's worktree.

        The recorded path must exist as a directory and, unless it is the
        project itself, be the top level of a worktree whose git common dir
        equals the project's. A removed worktree, or another directory now
        sitting at that path, raises HarnessError (cancel/status do not need
        the worktree and keep working)."""
        root = self.run_root(run)
        problem = None
        if not root.is_dir():
            problem = 'does not exist'
        elif root.resolve() != self.project.resolve():
            mine = self.common_dir()
            if mine is None or git_toplevel(root) != root.resolve() or git_common_dir(root) != mine:
                problem = 'is no longer a worktree of this repository'
        if problem:
            raise HarnessError('Run worktree missing or moved (' + str(root) + ' ' + problem
                               + '); cancel the run and start a new one from the intended worktree')
        return root

    @contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def run(self, rid):
        row = self.db.execute('SELECT doc FROM runs WHERE id=?', (rid,)).fetchone()
        if row is None:
            raise HarnessError('Unknown run: ' + str(rid))
        return json.loads(row['doc'])

    def runs(self):
        return [json.loads(r['doc']) for r in self.db.execute('SELECT doc FROM runs ORDER BY rowid DESC')]

    def save_run(self, run):
        run['updated'] = now()
        self.db.execute('INSERT INTO runs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET session=excluded.session,doc=excluded.doc',
                        (run['id'], run.get('session'), encoded(run)))

    def tasks(self, rid=None):
        sql, args = ('SELECT doc FROM tasks', ()) if rid is None else ('SELECT doc FROM tasks WHERE run=?', (rid,))
        return [json.loads(r['doc']) for r in self.db.execute(sql, args)]

    def task(self, rid, tid):
        row = self.db.execute('SELECT doc FROM tasks WHERE run=? AND id=?', (rid, tid)).fetchone()
        if row is None:
            raise HarnessError('Unknown task: ' + str(tid))
        return json.loads(row['doc'])

    def save_task(self, task):
        self.db.execute('INSERT INTO tasks VALUES(?,?,?) ON CONFLICT(run,id) DO UPDATE SET doc=excluded.doc',
                        (task['id'], task['run'], encoded(task)))

    def event(self, key, rid, kind, data):
        cur = self.db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)', (key, rid, kind, now(), encoded(data)))
        return bool(cur.rowcount)

    def get(self, table, key, col='id'):
        if (table, col) not in {('sessions', 'id'), ('workers', 'id'), ('cursors', 'path'), ('events', 'key')}:
            raise HarnessError('Unsupported state lookup')
        row = self.db.execute(f'SELECT doc FROM {table} WHERE {col}=?', (key,)).fetchone()
        return json.loads(row['doc']) if row else None

    def session(self, sid, data):
        self.db.execute('INSERT INTO sessions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET doc=excluded.doc', (sid, encoded(data)))

    def worker(self, wid, sid, data):
        self.db.execute('INSERT INTO workers VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET doc=excluded.doc', (wid, sid, encoded(data)))

    def for_session(self, sid):
        matches = [r for r in self.runs() if r.get('session') == sid and r['status'] not in {'completed', 'cancelled'}]
        if len(matches) > 1:
            raise HarnessError('Ambiguous session/run association')
        return matches[0] if matches else None
