"""ALS 0.5.0 round 3: state DB journal mode, busy timeouts and state-home resolution."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from als_harness import cli, state
from als_harness.common import HarnessError
from als_harness.state import Store


class StateDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def pragma(self, store, name):
        return store.db.execute('PRAGMA ' + name).fetchone()[0]

    def test_wal_mode(self):
        s = Store(self.project)
        try:
            self.assertEqual(self.pragma(s, 'journal_mode'), 'wal')
        finally:
            s.close()

    def test_reader_not_blocked_by_open_write_transaction(self):
        writer, reader = Store(self.project), Store(self.project, busy_ms=100)
        try:
            writer.db.execute('BEGIN IMMEDIATE')
            writer.db.execute("INSERT INTO sessions VALUES('x','{}')")
            self.assertEqual(reader.db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
            writer.db.execute('COMMIT')
        finally:
            writer.close()
            reader.close()

    def test_busy_timeouts_cli_and_hook(self):
        cli_store = cli.open_store(self.project, 'project')
        hook_store = cli.open_store(self.project, 'project', hook=True)
        try:
            self.assertEqual(self.pragma(cli_store, 'busy_timeout'), 5000)
            self.assertLessEqual(self.pragma(hook_store, 'busy_timeout'), 2000)
        finally:
            cli_store.close()
            hook_store.close()


class StateHomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_als_state_home_refused(self):
        with patch.dict(os.environ, {'ALS_STATE_HOME': 'rel/state'}):
            with self.assertRaises(HarnessError) as ctx:
                state.global_base()
        self.assertIn('ALS_STATE_HOME must be an absolute path', str(ctx.exception))

    def test_relative_als_state_home_cli_error(self):
        project = self.base / 'project'
        project.mkdir()
        subprocess.run(['git', 'init', '-q', str(project)], check=True)
        err = io.StringIO()
        with patch.dict(os.environ, {'ALS_STATE_HOME': 'rel/state'}), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(['--project', str(project), 'status'])
        self.assertEqual(code, 2)
        self.assertIn('ALS_STATE_HOME must be an absolute path', err.getvalue())

    def test_absolute_and_tilde_state_home(self):
        with patch.dict(os.environ, {'ALS_STATE_HOME': str(self.base / 's')}):
            self.assertEqual(state.global_base(), self.base / 's')
        with patch.dict(os.environ, {'ALS_STATE_HOME': '~/als-x', 'HOME': str(self.base)}):
            self.assertEqual(state.global_base(), self.base / 'als-x')

    def test_relative_xdg_state_home_ignored(self):
        env = {'XDG_STATE_HOME': 'relative/xdg', 'HOME': str(self.base)}
        with patch.dict(os.environ, env):
            os.environ.pop('ALS_STATE_HOME', None)
            self.assertEqual(state.global_base(), self.base / '.local/state/als')
        env['XDG_STATE_HOME'] = str(self.base / 'xdg')
        with patch.dict(os.environ, env):
            os.environ.pop('ALS_STATE_HOME', None)
            self.assertEqual(state.global_base(), self.base / 'xdg/als')
            self.assertTrue(state.global_base().is_absolute())


if __name__ == '__main__':
    unittest.main()
