"""ALS 0.5.0 CLI ease-of-use and portability regressions. Temp repos only; no Codex calls."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from als_harness import cli

ROOT = Path(__file__).resolve().parents[1]
OWNED = ('als_harness/hooks.py', 'als_harness/usage.py', 'als_harness/cli.py', 'als_harness/report.py',
         'scripts/harness.py')
GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}
PLAN = {'goal': 'ease', 'tasks': [{'id': 'one', 'role': 'als_worker', 'write_paths': ['source.py']}],
        'checks': [], 'criteria': []}


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


class EaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.project = base / 'project'
        (self.project / 'sub').mkdir(parents=True)
        git('init', '-q', cwd=self.project)
        (self.project / 'source.py').write_text('x=1\n')
        git('add', 'source.py', cwd=self.project)
        git('commit', '-q', '-m', 'init', cwd=self.project)
        self.fork = base / 'fork'
        git('worktree', 'add', '-q', str(self.fork), cwd=self.project)
        self.environ = {**os.environ, 'ALS_STATE_HOME': str(base / 'als-state'), 'CODEX_HOME': str(base / 'codex'),
                        'CODEX_THREAD_ID': ''}
        self.env = patch.dict(os.environ, self.environ)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def harness(self, *args, cwd=None, stdin=None):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/harness.py'), *args], input=stdin,
                              capture_output=True, text=True, cwd=str(cwd or self.project), env=self.environ)

    def main(self, *args):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['--project', str(self.project), *args])
        return code, out.getvalue(), err.getvalue()

    # Python 3.9 floor
    def test_owned_files_parse_as_python_39(self):
        for name in OWNED:
            with self.subTest(name=name):
                ast.parse((ROOT / name).read_text(), feature_version=(3, 9))
        self.assertIn('(3, 9)', (ROOT / 'scripts/harness.py').read_text())

    # Portable git resolution
    def test_git_resolution_avoids_path_format(self):
        seen = []
        real = subprocess.run

        def spy(argv, *a, **k):
            seen.append(argv)
            return real(argv, *a, **k)
        with patch.object(cli.subprocess, 'run', spy):
            self.assertEqual(cli.git_project(self.project / 'sub'), self.project.resolve())
            self.assertEqual(cli.git_project(self.fork), self.project.resolve())
        self.assertTrue(seen)
        self.assertFalse(any('--path-format=absolute' in argv for argv in seen))

    # No --project from subdirectories and forked worktrees; --plan - reads stdin
    def test_start_from_stdin_in_subdir_then_status_from_fork(self):
        done = self.harness('start', '--plan', '-', cwd=self.project / 'sub', stdin=json.dumps(PLAN))
        self.assertEqual(done.returncode, 0, done.stderr)
        rid = json.loads(done.stdout)['run']
        status = self.harness('status', cwd=self.fork)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual([r['id'] for r in json.loads(status.stdout)['runs']], [rid])
        table = self.harness('status', '--format', 'text', cwd=self.fork)
        self.assertIn(rid, table.stdout)
        self.assertIn('running', table.stdout)

    def test_status_lists_only_unfinished_runs(self):
        plan = Path(self.tmp.name) / 'plan.json'
        plan.write_text(json.dumps(PLAN))
        first = json.loads(self.main('start', '--plan', str(plan), '--session', 'a')[1])['run']
        second = json.loads(self.main('start', '--plan', str(plan), '--session', 'b')[1])['run']
        self.main('cancel', '--run', first, '--reason', 'test')
        runs = json.loads(self.main('status')[1])['runs']
        self.assertEqual([r['id'] for r in runs], [second])
        everything = json.loads(self.main('status', '--all')[1])['runs']
        self.assertEqual({r['id'] for r in everything}, {first, second})
        code, text, _ = self.main('status', '--format', 'text')
        self.assertEqual(code, 0)
        self.assertNotIn(first, text)
        self.assertIn(second, text)

    def test_outside_git_without_project_gives_next_command(self):
        plain = Path(self.tmp.name) / 'plain'
        plain.mkdir()
        done = self.harness('start', '--plan', '-', cwd=plain, stdin=json.dumps(PLAN))
        self.assertEqual(done.returncode, 2)
        self.assertIn('Next:', done.stderr)
        self.assertFalse((plain / '.astra-luna-sol').exists())

    # Error messages print the next command
    def test_common_mistakes_print_next_command(self):
        plan = Path(self.tmp.name) / 'plan.json'
        plan.write_text(json.dumps(PLAN))
        rid = json.loads(self.main('start', '--plan', str(plan), '--session', 'a')[1])['run']
        code, _, err = self.main('status', '--run', 'missing')
        self.assertEqual(code, 2)
        # The hint is a runnable command: `als` on PATH, else this harness by absolute path.
        self.assertRegex(err, r'Next: (als|\S+ \S*harness\.py) status --all')
        code, _, err = self.main('start', '--plan', str(Path(self.tmp.name) / 'absent.json'))
        self.assertEqual(code, 2)
        self.assertIn('Next:', err)
        self.main('reserve', '--run', rid, '--task', 'one', '--call-id', 'c1')
        self.main('pause', '--run', rid, '--reason', 'x')
        code, _, err = self.main('resume', '--run', rid)
        self.assertEqual(code, 2)
        self.assertIn('--workers-stopped', err.split('Next:', 1)[1])

    # Doctor: deps and text summary
    def fake_deps(self, ok):
        item = {'name': 'git', 'found': '2.0', 'required': '>=2.5', 'ok': ok, 'hint': 'install a newer git'}
        return types.SimpleNamespace(check=lambda project=None, probe=True: {'ok': ok, 'items': [item]})

    def test_doctor_includes_deps_and_text_summary(self):
        with patch.object(cli, 'deps', self.fake_deps(False)):
            code, out, _ = self.main('doctor')
            self.assertEqual(json.loads(out)['deps']['items'][0]['name'], 'git')
            code, text, _ = self.main('doctor', '--format', 'text')
        self.assertEqual(code, 1)  # r3: a failing REQUIRED dependency exits 1
        last = text.strip().splitlines()[-1]
        self.assertIn('git', last)
        self.assertIn('install a newer git', last)

    def test_doctor_without_deps_module(self):
        with patch.object(cli, 'deps', None):
            data = json.loads(self.main('doctor')[1])
        self.assertIsNone(data['deps']['ok'])


if __name__ == '__main__':
    unittest.main()
