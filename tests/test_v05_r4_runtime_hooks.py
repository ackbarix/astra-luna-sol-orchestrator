"""ALS 0.5.0 round 4: SessionStart command hints and session auto-pick. Temp
repos only; no Codex calls."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from als_harness import cli, hooks
from als_harness.state import Store

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


def fake_codex(folder, script):
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / 'codex'
    exe.write_text('#!/bin/sh\n' + script)
    exe.chmod(0o755)
    return folder


class TempEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {'ALS_STATE_HOME': str(self.base / 'state'),
                                           'CODEX_HOME': str(self.base / 'codex-home'), 'CODEX_THREAD_ID': '',
                                           'ALS_HOME': str(self.base / 'als-home')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def path_with(self, script):
        folder = fake_codex(self.base / 'bin', script)
        return patch.dict(os.environ, {'PATH': str(folder) + os.pathsep + '/usr/bin:/bin'})

    def items(self, result):
        return {i['name']: i for i in result['items']}


class ProjectHintTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.project = self.base / 'project'
        self.project.mkdir()
        git('init', '-q', cwd=self.project)
        (self.project / 'source.py').write_text('x=1\n')
        git('add', 'source.py', cwd=self.project)
        git('commit', '-q', '-m', 'init', cwd=self.project)

    def start_text(self):
        store = Store(self.project, scope='project')
        try:
            out = hooks.handle(store, {'hook_event_name': 'SessionStart', 'session_id': 'sid',
                                       'cwd': str(self.project), 'source': 'startup'})
        finally:
            store.close()
        return out['hookSpecificOutput']['additionalContext']

    def test_project_hint_uses_als_when_on_path(self):
        with patch.object(hooks.shutil, 'which', return_value='/x/als'):
            self.assertIn('`als start --plan FILE', self.start_text())

    def test_project_hint_is_absolute_without_als(self):
        with patch.object(hooks.shutil, 'which', return_value=None):
            text = self.start_text()
        self.assertNotIn('.astra-luna-sol/harness.py start', text.replace(str(hooks.harness_script()), ''))
        self.assertIn(sys.executable, text)
        self.assertIn(str(hooks.harness_script()), text)


class SessionAutoPickTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.project = self.base / 'project'
        self.project.mkdir()
        git('init', '-q', cwd=self.project)
        self.s = Store(self.project, scope='global')

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def hook(self, event, sid):
        payload = {'hook_event_name': event, 'session_id': sid, 'cwd': str(self.project)}
        if event in ('PreToolUse', 'PostToolUse'):
            payload.update(tool_name='shell', tool_use_id='call-1', tool_input={})
        hooks.handle(self.s, payload)

    def test_ended_session_is_not_picked(self):
        self.hook('SessionStart', 'old')
        self.hook('SessionEnd', 'old')
        session, source, warning = cli.default_session(self.s, 'observe')
        self.assertIsNone(session)
        self.assertIn('has ended', warning)

    def test_ended_session_counts_as_recent_activity(self):
        self.hook('SessionStart', 'old')
        self.hook('SessionEnd', 'old')
        self.hook('SessionStart', 'new')
        session, _, warning = cli.default_session(self.s, 'observe')
        self.assertIsNone(session)
        self.assertIn('2 sessions', warning)

    def test_resumed_session_is_picked_again(self):
        self.hook('SessionStart', 'sid')
        self.hook('SessionEnd', 'sid')
        self.hook('SessionStart', 'sid')
        self.assertEqual(cli.default_session(self.s, 'observe')[0], 'sid')

    def test_session_end_is_recorded_even_inside_refresh_window(self):
        self.hook('SessionStart', 'sid')
        self.hook('PreToolUse', 'sid')
        self.hook('SessionEnd', 'sid')
        self.hook('SessionEnd', 'sid')
        self.assertIsNotNone(self.s.get('sessions', 'sid').get('ended_at'))
        self.hook('PreToolUse', 'sid')
        self.assertIsNone(self.s.get('sessions', 'sid').get('ended_at'))


if __name__ == '__main__':
    unittest.main()
