"""SQLite single source of truth. Reservations and transitions share transactions."""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from .common import HarnessError, checked_path, private_dir, encoded, now

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

class Store:
    def __init__(self, project: Path):
        self.project = project.resolve(strict=True)
        private_dir(self.project, '.astra-luna-sol/state')
        self.path = checked_path(self.project, '.astra-luna-sol/state/runs.sqlite')
        for tail in ('', '-wal', '-shm', '-journal'):
            checked_path(self.project, '.astra-luna-sol/state/runs.sqlite' + tail)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path, timeout=1.5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=1500')
        v = self.db.execute('PRAGMA user_version').fetchone()[0]
        if v not in (0, 1):
            self.db.close()
            raise HarnessError('Unsupported future state schema; database left unchanged')
        if not v:
            self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

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
        if (table,col) not in {('sessions','id'),('workers','id'),('cursors','path'),('events','key')}:
            raise HarnessError('Unsupported state lookup')
        row = self.db.execute(f'SELECT doc FROM {table} WHERE {col}=?', (key,)).fetchone()
        return json.loads(row['doc']) if row else None

    def session(self, sid, data):
        self.db.execute('INSERT INTO sessions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET doc=excluded.doc', (sid, encoded(data)))

    def worker(self, wid, sid, data):
        self.db.execute('INSERT INTO workers VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET doc=excluded.doc', (wid, sid, encoded(data)))

    def for_session(self, sid):
        matches = [r for r in self.runs() if r.get('session') == sid and r['status'] not in {'completed','cancelled'}]
        if len(matches) > 1:
            raise HarnessError('Ambiguous session/run association')
        return matches[0] if matches else None
