"""ALS 0.5.0 round 3: CLI session pick and doctor. Temp dirs and env overrides only."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from als_harness import cli, hooks

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.project = base / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        (self.project / 'source.py').write_text('x=1\n')
        self.codex = base / 'codex-home'
        self.codex.mkdir()
        self.env = patch.dict(os.environ, {'ALS_STATE_HOME': str(base / 'als-state'), 'CODEX_HOME': str(self.codex),
                                           'CODEX_THREAD_ID': ''})
        self.env.start()
        self.plan = base / 'plan.json'
        self.plan.write_text(json.dumps({'goal': 'g', 'tasks': [{'id': 'one', 'role': 'als_worker'}]}))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['--project', str(self.project), *args])
        return code, out.getvalue(), err.getvalue()

    def store(self):
        return cli.open_store(self.project, 'global')

    def hook(self, sid, event, **extra):
        payload = {'hook_event_name': event, 'session_id': sid, 'cwd': str(self.project), 'turn_id': 't',
                   'stop_hook_active': False, 'tool_use_id': 'c', 'tool_input': {}}
        payload.update(extra)
        s = self.store()
        try:
            hooks.handle(s, payload)
        finally:
            s.close()

    def age(self, sid, field, seconds):
        s = self.store()
        try:
            with s.tx():
                doc = s.get('sessions', sid)
                doc[field] = time.time() - seconds
                s.session(sid, doc)
        finally:
            s.close()


class SessionPickTests(CliCase):
    def test_any_recent_event_counts(self):
        self.hook('long', 'SessionStart')
        self.age('long', 'session_start_at', 3 * 3600)
        self.hook('long', 'Stop')
        code, out, err = self.main('start', '--plan', str(self.plan))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['session'], 'long')
        self.assertIn('picked session long', err)
        self.assertIn('picked session long', json.loads(out)['warnings'][0])

    def test_two_active_sessions_are_ambiguous(self):
        self.hook('busy', 'SessionStart')
        self.age('busy', 'session_start_at', 3 * 3600)
        self.hook('busy', 'Stop')          # long-running chat, still active
        self.hook('fresh', 'SessionStart')  # another chat just opened
        code, out, err = self.main('start', '--plan', str(self.plan))
        self.assertEqual(code, 0, err)
        self.assertIsNone(json.loads(out)['session'])
        self.assertIn('cannot pick the session', err)

    def test_stale_activity_not_picked(self):
        self.hook('old', 'SessionStart')
        self.age('old', 'session_start_at', 3600)
        self.age('old', 'last_event', 3600)
        code, out, _ = self.main('start', '--plan', str(self.plan))
        self.assertIsNone(json.loads(out)['session'])

    def test_repeated_event_refreshes_activity(self):
        self.hook('s', 'Stop')
        self.age('s', 'last_event', 900)
        self.hook('s', 'Stop')
        s = self.store()
        try:
            self.assertLess(time.time() - s.get('sessions', 's')['last_event'], 60)
        finally:
            s.close()

    def test_guarded_never_auto_picks(self):
        for event in ('SessionStart', 'PreToolUse', 'PostToolUse', 'Stop'):
            self.hook('only', event)
        code, _, err = self.main('start', '--plan', str(self.plan), '--mode', 'guarded', '--ack-hook-limits')
        self.assertEqual(code, 2)
        self.assertIn('requires --session', err)
        code, out, err = self.main('start', '--plan', str(self.plan), '--mode', 'guarded', '--ack-hook-limits',
                                   '--session', 'only')
        self.assertEqual(code, 0, err)


if __name__ == '__main__':
    unittest.main()
