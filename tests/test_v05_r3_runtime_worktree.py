"""ALS 0.5.0 round 3: HEAD sessions inside a linked `git worktree`. Temp repos only."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from als_harness import cli
from als_harness.engine import Engine
from als_harness.state import Store

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


class LinkedWorktreeRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.project = base / 'project'
        self.project.mkdir()
        git('init', '-q', cwd=self.project)
        (self.project / 'source.py').write_text('x=1\n')
        git('add', 'source.py', cwd=self.project)
        git('commit', '-q', '-m', 'init', cwd=self.project)
        self.wt = base / 'wt'
        git('worktree', 'add', '-q', '-b', 'feature', str(self.wt), cwd=self.project)
        (self.wt / 'sub').mkdir()
        # Exists only in the linked worktree: a check passes only when run there.
        (self.wt / 'wt_marker.txt').write_text('here\n')
        self.env = patch.dict(os.environ, {'ALS_STATE_HOME': str(base / 'state'), 'CODEX_HOME': str(base / 'codex'),
                                           'CODEX_THREAD_ID': ''})
        self.env.start()
        self.old_cwd = os.getcwd()
        os.chdir(self.wt)
        check = [sys.executable, '-c', 'import os,sys; sys.exit(0 if os.path.exists("wt_marker.txt") else 1)']
        self.plan = base / 'plan.json'
        self.plan.write_text(json.dumps({'goal': 'worktree', 'tasks': [{'id': 'one', 'role': 'als_worker',
                                                                         'write_paths': ['source.py']}],
                                         'checks': [{'id': 'unit', 'argv': check, 'timeout': 30}], 'criteria': []}))
        self.checks_only = base / 'checks.json'
        self.checks_only.write_text(json.dumps({'goal': 'worktree checks', 'tasks': [],
                                                'checks': [{'id': 'unit', 'argv': check, 'timeout': 30}],
                                                'criteria': []}))

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.env.stop()
        self.tmp.cleanup()

    def main(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        fake_stdin = type('S', (), {'buffer': io.BytesIO((stdin or '').encode())})()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(sys, 'stdin', fake_stdin):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def hook(self, event, cwd=None, **extra):
        payload = {'hook_event_name': event, 'session_id': 'sid', 'cwd': str(cwd or self.wt), 'turn_id': 't1',
                   'stop_hook_active': False, 'tool_use_id': 'call-1', 'tool_input': {}}
        payload.update(extra)
        code, out, _ = self.main('hook', stdin=json.dumps(payload))
        self.assertEqual(code, 0)
        return json.loads(out)

    def store(self):
        return Store(self.project, scope='global')

    def test_start_records_worktree_and_state_stays_with_main_project(self):
        code, out, err = self.main('start', '--plan', str(self.plan), '--session', 'sid')
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data['worktree'], str(self.wt))
        s = self.store()
        try:
            self.assertEqual(s.run(data['run'])['worktree'], str(self.wt))
        finally:
            s.close()

    def test_head_hooks_from_worktree_are_handled(self):
        rid = json.loads(self.main('start', '--plan', str(self.plan), '--session', 'sid')[1])['run']
        started = self.hook('SessionStart', source='startup')
        self.assertIn(rid, started['hookSpecificOutput']['additionalContext'])
        spawn = self.hook('PreToolUse', cwd=self.wt / 'sub', tool_name='spawn_agent',
                          tool_input={'message': 'ALS_TASK=one\nwork', 'agent_type': 'als_worker'})
        self.assertNotIn('systemMessage', spawn)
        stop = self.hook('Stop')
        self.assertEqual(stop, {})
        s = self.store()
        try:
            self.assertEqual(s.task(rid, 'one')['status'], 'reserved')
            seen = s.get('sessions', 'sid')['seen']
            self.assertTrue({'SessionStart', 'PreToolUse', 'Stop'} <= set(seen), seen)
        finally:
            s.close()

    def test_verify_runs_in_worktree_and_main_changes_do_not_stale_evidence(self):
        rid = json.loads(self.main('start', '--plan', str(self.checks_only), '--session', 'sid2')[1])['run']
        code, out, err = self.main('verify', '--run', rid, '--execute')
        self.assertEqual(code, 0, out + err)
        result = json.loads(out)
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['results'][0]['cwd'], str(self.wt))
        # A change in the MAIN checkout is not this run's code.
        (self.project / 'source.py').write_text('x=2\n')
        code, out, err = self.main('complete', '--run', rid)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(json.loads(out)['completed'])

    def test_worktree_change_still_stales_evidence(self):
        rid = json.loads(self.main('start', '--plan', str(self.checks_only), '--session', 'sid3')[1])['run']
        self.assertEqual(self.main('verify', '--run', rid, '--execute')[0], 0)
        (self.wt / 'source.py').write_text('x=3\n')
        code, out, _ = self.main('complete', '--run', rid)
        self.assertEqual(code, 3)
        self.assertIn('missing/failed/stale check unit', json.loads(out)['problems'])

    def test_foreign_repository_cwd_is_still_ignored(self):
        self.main('start', '--plan', str(self.plan), '--session', 'sid')
        other = Path(self.tmp.name).resolve() / 'other'
        other.mkdir()
        git('init', '-q', cwd=other)
        s = self.store()
        try:
            self.assertFalse(s.belongs(other))
            self.assertTrue(s.belongs(self.wt / 'sub'))
            from als_harness import hooks
            out = hooks.handle(s, {'hook_event_name': 'Stop', 'session_id': 'sid', 'cwd': str(other),
                                   'turn_id': 't', 'stop_hook_active': False})
            self.assertIn('outside the installed project', out['systemMessage'])
        finally:
            s.close()

    def test_legacy_run_without_worktree_uses_project(self):
        s = self.store()
        try:
            e = Engine(s)
            rid = e.start({'goal': 'legacy'})
            with s.tx():
                run = s.run(rid)
                run.pop('worktree')
                s.save_run(run)
            self.assertEqual(s.run_root(s.run(rid)), self.project)
            self.assertEqual(e.snapshot(rid)['digest'], s.run(rid)['baseline']['digest'])
        finally:
            s.close()


if __name__ == '__main__':
    unittest.main()
