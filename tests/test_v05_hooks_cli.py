"""ALS 0.5.0 CLI scope/project resolution regressions. Temp repos only; no Codex calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from als_harness import cli, hooks
from als_harness.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
FAKE_CODEX = ('#!/bin/sh\nif [ "$1" = features ]; then printf "hooks stable true\\nmulti_agent stable true\\n"; '
              'else echo "codex-cli 9.9.9"; fi\n')


def fake_codex_dir(base):
    """Directory holding a fake `codex` so doctor never depends on the host's Codex CLI."""
    folder = Path(base) / 'fake-bin'
    folder.mkdir(exist_ok=True)
    exe = folder / 'codex'
    exe.write_text(FAKE_CODEX)
    exe.chmod(0o755)
    ocx = folder / 'ocx'  # deps also runs `ocx --version`
    ocx.write_text('#!/bin/sh\necho ocx 0.0.0\n')
    ocx.chmod(0o755)
    return folder


GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


class CliScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.project = base / 'project'
        self.project.mkdir()
        git('init', '-q', cwd=self.project)
        (self.project / 'source.py').write_text('x=1\n')
        git('add', 'source.py', cwd=self.project)
        git('commit', '-q', '-m', 'init', cwd=self.project)
        self.codex = base / 'codex-home'
        self.codex.mkdir()
        # Never touch the real ~/.codex or ~/.local/state.
        self.env = patch.dict(os.environ, {'ALS_STATE_HOME': str(base / 'als-state'), 'CODEX_HOME': str(self.codex),
                                           'CODEX_THREAD_ID': '',
                                           # doctor probes `codex`; never run the host's binary
                                           'PATH': str(fake_codex_dir(base)) + os.pathsep + os.environ.get('PATH', '')})
        self.env.start()
        self.plan = base / 'plan.json'
        self.plan.write_text(json.dumps({'goal': 'cli audit', 'tasks': [{'id': 'one', 'role': 'als_worker',
                                                                          'write_paths': ['source.py']}],
                                         'checks': [], 'criteria': []}))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def main(self, *args):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['--project', str(self.project), *args])
        return code, out.getvalue(), err.getvalue()

    def store(self):
        return cli.open_store(self.project, 'global')

    def receipt(self, sid):
        s = self.store()
        try:
            hooks.handle(s, {'hook_event_name': 'SessionStart', 'session_id': sid, 'cwd': str(self.project)})
        finally:
            s.close()

    # Item 1
    def test_scope_detection(self):
        installed = Path(self.tmp.name) / 'p2' / '.astra-luna-sol'
        installed.mkdir(parents=True)
        (installed / 'harness.py').write_text('')
        self.assertEqual(cli.detect_scope(installed), 'project')
        self.assertEqual(cli.detect_scope(ROOT), 'global')

    def test_forked_worktree_maps_to_main_project(self):
        fork = Path(self.tmp.name) / 'fork'
        git('worktree', 'add', '-q', str(fork), cwd=self.project)
        (self.project / 'sub').mkdir()
        self.assertEqual(cli.git_project(fork), self.project.resolve())
        self.assertEqual(cli.git_project(self.project / 'sub'), self.project.resolve())
        self.assertIsNone(cli.git_project(Path(self.tmp.name)))

    def test_project_scope_hook_without_project_flag(self):
        home = self.project / '.astra-luna-sol'
        home.mkdir()
        shutil.copytree(ROOT / 'als_harness', home / 'als_harness')
        shutil.copy(ROOT / 'scripts/harness.py', home / 'harness.py')
        fork = Path(self.tmp.name) / 'fork'
        git('worktree', 'add', '-q', str(fork), cwd=self.project)
        payload = {'hook_event_name': 'SessionStart', 'session_id': 'sid', 'cwd': str(self.project / '.astra-luna-sol')}
        done = subprocess.run([sys.executable, str(home / 'harness.py'), 'hook'], input=json.dumps(payload),
                              capture_output=True, text=True, cwd=str(fork))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('ALS harness available', done.stdout)
        self.assertTrue((home / 'state/runs.sqlite').is_file())
        status = subprocess.run([sys.executable, str(home / 'harness.py'), 'doctor'], capture_output=True, text=True,
                                cwd=str(fork))
        self.assertEqual(json.loads(status.stdout)['scope'], 'project')
        self.assertEqual(json.loads(status.stdout)['state_path'], str(home / 'state/runs.sqlite'))

    def test_global_hook_resolves_project_from_payload_cwd(self):
        (self.project / 'sub').mkdir()
        payload = {'hook_event_name': 'SessionStart', 'session_id': 'gsid', 'cwd': str(self.project / 'sub')}
        done = subprocess.run([sys.executable, str(ROOT / 'scripts/harness.py'), 'hook'], input=json.dumps(payload),
                              capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('ALS harness available', done.stdout)
        s = self.store()
        try:
            self.assertEqual(s.get('sessions', 'gsid')['seen'], ['SessionStart'])
        finally:
            s.close()

    def test_global_hook_outside_git_is_noop(self):
        outside = Path(self.tmp.name) / 'plain'
        outside.mkdir()
        payload = {'hook_event_name': 'SessionStart', 'session_id': 'x', 'cwd': str(outside)}
        done = subprocess.run([sys.executable, str(ROOT / 'scripts/harness.py'), 'hook'], input=json.dumps(payload),
                              capture_output=True, text=True, cwd=str(outside))
        self.assertEqual(json.loads(done.stdout), {})
        self.assertFalse((outside / '.astra-luna-sol').exists())

    def test_status_uses_state_locate(self):
        self.receipt('sid')
        missing = Path(self.tmp.name) / 'elsewhere/runs.sqlite'
        with patch.object(cli.state, 'locate', lambda project, scope='project': missing):
            code, out, _ = self.main('status')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['note'], 'No managed runs registered')

    # Item 6
    def test_cli_bind_reports_unbound_with_exit_3(self):
        code, out, err = self.main('start', '--plan', str(self.plan), '--session', 'sid')
        self.assertEqual(code, 0, err)
        rid = json.loads(out)['run']
        self.assertEqual(self.main('reserve', '--run', rid, '--task', 'one', '--call-id', 'c1')[0], 0)
        self.assertEqual(self.main('finish', '--run', rid, '--task', 'one', '--note', 'done', '--workers-stopped')[0], 0)
        code, out, _ = self.main('bind', '--run', rid, '--call-id', 'c1', '--thread', 'kid')
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)['bound'], False)
        self.assertIn('attempt already', json.loads(out)['reason'])

    # Item 10
    def test_start_uses_single_recent_session_start(self):
        self.receipt('only')
        code, out, err = self.main('start', '--plan', str(self.plan))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['session'], 'only')
        self.assertEqual(json.loads(out)['session_source'], 'recent_session_start')

    def test_start_ambiguous_sessions(self):
        self.receipt('first')
        self.receipt('second')
        code, out, err = self.main('start', '--plan', str(self.plan))
        self.assertEqual(code, 0, err)
        self.assertIsNone(json.loads(out)['session'])
        self.assertIn('cannot pick the session', err)
        code, _, err = self.main('start', '--plan', str(self.plan), '--mode', 'guarded', '--ack-hook-limits')
        self.assertEqual(code, 2)
        self.assertIn('requires --session', err)

    def test_start_ignores_stale_session_start(self):
        self.receipt('old')
        s = self.store()
        try:
            with s.tx():
                doc = s.get('sessions', 'old')
                doc['session_start_at'] = time.time() - 3600
                doc['last_event'] = time.time() - 3600  # r3: any recent event counts
                s.session('old', doc)
        finally:
            s.close()
        code, out, _ = self.main('start', '--plan', str(self.plan))
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)['session'])

    def test_start_env_thread_id_wins(self):
        self.receipt('only')
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'envsid'}):
            code, out, _ = self.main('start', '--plan', str(self.plan))
        self.assertEqual(json.loads(out)['session'], 'envsid')

    # Item 11
    def hooks_doc(self, events, command):
        return {'hooks': {e: [{'matcher': 'spawn_agent', 'hooks': [{'type': 'command', 'command': command}]}]
                          for e in events}}

    def test_doctor_accepts_new_hook_format(self):
        command = 'python3 "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.astra-luna-sol/harness.py" hook'
        (self.project / '.codex').mkdir()
        (self.project / '.codex/hooks.json').write_text(json.dumps(self.hooks_doc(cli.DOCTOR_EVENTS, command)))
        out = cli.doctor(self.project, None, scope='project')
        self.assertTrue(out['hooks_configured'], out)
        self.assertEqual(out['scope'], 'project')
        self.assertTrue(out['state_path'].endswith('runs.sqlite'))
        (self.project / '.codex/hooks.json').write_text(json.dumps(self.hooks_doc(cli.DOCTOR_EVENTS[:-1], command)))
        out = cli.doctor(self.project, None, scope='project')
        self.assertFalse(out['hooks_configured'])
        self.assertEqual(out['hook_events_missing'], ['SessionEnd'])

    def test_doctor_global_reads_codex_home(self):
        command = 'python3 "$HOME/.local/share/als/harness.py" hook'
        (self.codex / 'hooks.json').write_text(json.dumps(self.hooks_doc(cli.DOCTOR_EVENTS, command)))
        # A fake codex on PATH: the exit code must not depend on the host's Codex install.
        fake = str(fake_codex_dir(self.tmp.name))
        with patch.dict(os.environ, {'PATH': fake + os.pathsep + os.environ.get('PATH', '')}):
            code, out, err = self.main('doctor')
        self.assertEqual(code, 0, out + err)
        data = json.loads(out)
        self.assertTrue(data['hooks_configured'], data)
        self.assertEqual(data['scope'], 'global')
        self.assertEqual(data['hooks_file'], str(self.codex / 'hooks.json'))

    def test_doctor_rejects_non_hook_commands(self):
        command = 'python3 harness.py hooks-report'
        (self.codex / 'hooks.json').write_text(json.dumps(self.hooks_doc(cli.DOCTOR_EVENTS, command)))
        self.assertFalse(cli.doctor(self.project, None, scope='global')['hooks_configured'])


if __name__ == '__main__':
    unittest.main()
