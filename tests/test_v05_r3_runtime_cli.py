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


GLOBAL_FORM = 'python3 "${HOME:-/abs/home}/.local/share/als/harness.py" hook'
PROJECT_FORM = ('c=$(git rev-parse --git-common-dir 2>/dev/null) || exit 0; '
                'case $c in .git|*/.git) r=$(CDPATH= cd -- "$c/.." && pwd) ;; '
                '*) r=$(git rev-parse --show-toplevel 2>/dev/null) ;; esac || exit 0; '
                's=$r/.astra-luna-sol/harness.py; [ -f "$s" ] || exit 0; exec python3 "$s" hook')


def hooks_doc(command, events=cli.DOCTOR_EVENTS):
    return {'hooks': {e: [{'matcher': 'spawn_agent', 'hooks': [{'type': 'command', 'command': command}]}]
                      for e in events}}


def fake_deps(items):
    calls = []

    def check(project=None, probe=True):
        calls.append(probe)
        return {'ok': all(i['ok'] for i in items if i['required']), 'items': items}
    return types.SimpleNamespace(check=check), calls


def item(name, ok, required=True, hint='fix it'):
    return {'name': name, 'found': None if not ok else '1', 'required': required, 'ok': ok, 'hint': '' if ok else hint}


class DoctorTests(CliCase):
    def setUp(self):
        super().setUp()
        # Some tests use the real deps module; never run the host's `codex`.
        fake = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (fake / 'codex').write_text('#!/bin/sh\nif [ "$1" = features ]; then printf "hooks stable true\\nmulti_agent stable true\\n"; else echo "codex-cli 9.9.9"; fi\n')
        (fake / 'codex').chmod(0o755)
        (fake / 'ocx').write_text('#!/bin/sh\necho ocx 0.0.0\n')  # deps runs `ocx --version`
        (fake / 'ocx').chmod(0o755)
        path_patch = patch.dict(os.environ, {'PATH': str(fake) + os.pathsep + os.environ.get('PATH', '')})
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def write_project_hooks(self, command):
        (self.project / '.codex').mkdir(exist_ok=True)
        (self.project / '.codex/hooks.json').write_text(json.dumps(hooks_doc(command)))

    def doctor(self, *extra, items=()):
        module, calls = fake_deps(list(items))
        with patch.object(cli, 'deps', module):
            code, out, err = self.main('doctor', *extra)
        return code, out, err, calls

    def test_hooks_only_install_found_in_project_for_global_scope(self):
        for command in (GLOBAL_FORM, PROJECT_FORM):
            with self.subTest(command=command[:20]):
                self.write_project_hooks(command)
                code, out, err, _ = self.doctor()
                data = json.loads(out)
                self.assertEqual(data['scope'], 'global')
                self.assertTrue(data['hooks_configured'], data)
                self.assertEqual(data['hooks_file'], str(self.project / '.codex/hooks.json'))
                self.assertEqual(code, 0, err)

    def test_both_forms_recognised(self):
        for command in (GLOBAL_FORM, PROJECT_FORM):
            self.assertTrue(cli.als_command({'command': command}), command)
        for command in ('python3 harness.py hooks-report', 'python3 harness.py hook --debug', 'echo hook'):
            self.assertFalse(cli.als_command({'command': command}), command)

    def test_project_scope_also_reads_codex_home(self):
        (self.codex / 'hooks.json').write_text(json.dumps(hooks_doc(GLOBAL_FORM)))
        out = cli.doctor(self.project, None, scope='project')
        self.assertTrue(out['hooks_configured'])
        self.assertEqual(list(out['hooks_found_in']), [str(self.codex / 'hooks.json')])

    def test_text_output_marks_optional_warn_and_required_fail(self):
        items = [item('ocx', False, required=False, hint='optional tool'), item('git', False, hint='install git')]
        code, out, _, _ = self.doctor('--format', 'text', items=items)
        self.assertEqual(code, 1)
        self.assertIn('warn ocx', out)
        self.assertNotIn('FAIL ocx', out)
        self.assertIn('FAIL git', out)
        self.assertNotIn('need False', out)
        self.assertNotIn('need True', out)

    def test_warnings_only_exit_zero(self):
        items = [item('ocx', False, required=False), item('git', True)]
        code, out, _, _ = self.doctor('--format', 'text', items=items)
        self.assertEqual(code, 0)
        self.assertIn('warn ocx', out)
        code, out, _, _ = self.doctor(items=items)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['exit_code'], 0)

    def test_required_failure_exit_one_in_json(self):
        code, out, _, _ = self.doctor(items=[item('python', False)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['exit_code'], 1)

    def test_no_probe_passed_to_deps(self):
        _, _, _, calls = self.doctor('--no-probe')
        self.assertEqual(calls, [False])
        _, _, _, calls = self.doctor()
        self.assertEqual(calls, [True])

    def test_help_documents_exit_codes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(['doctor', '--help'])
        self.assertIn('1 when a required one', ' '.join(out.getvalue().split()))


if __name__ == '__main__':
    unittest.main()
