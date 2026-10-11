"""Snapshot robustness regressions (0.5.0 audit): nested repos, gitlinks,
non-UTF-8 names and special files must not disable the harness."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from als_harness.snapshot import capture
from als_harness.state import Store
from als_harness.engine import Engine
from als_harness import verify

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')


def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True, env=GIT_ENV).stdout.decode().strip()


class SnapshotRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        git('init', '-q', str(self.project))
        (self.project / 'source.py').write_text('x=1\n')

    def tearDown(self):
        self.tmp.cleanup()

    def nested_repo(self, name='nested', commit=True):
        path = self.project / name
        path.mkdir()
        git('init', '-q', str(path))
        if commit:
            (path / 'inner.txt').write_text('inner\n')
            git('-C', str(path), 'add', 'inner.txt')
            git('-C', str(path), 'commit', '-q', '-m', 'inner')
        return path

    def entry(self, state, name):
        return [list(r) for r in state['files'] if r[0] == name]

    def test_untracked_nested_repo_recorded_by_head(self):
        path = self.nested_repo()
        state = capture(self.project)
        head = git('-C', str(path), 'rev-parse', 'HEAD')
        self.assertEqual(self.entry(state, 'nested'), [['nested', 'gitlink', head]])
        (path / 'inner.txt').write_text('changed\n')
        git('-C', str(path), 'commit', '-q', '-am', 'second')
        self.assertNotEqual(capture(self.project)['digest'], state['digest'])

    def test_nested_repo_without_commits(self):
        self.nested_repo('empty', commit=False)
        (self.project / 'empty' / 'f.txt').write_text('x')
        self.assertEqual(self.entry(capture(self.project), 'empty'), [['empty', 'gitlink', 'untracked-repo']])

    def test_tracked_gitlink(self):
        path = self.nested_repo('sub')
        git('-C', str(self.project), 'add', 'sub')
        head = git('-C', str(path), 'rev-parse', 'HEAD')
        self.assertEqual(self.entry(capture(self.project), 'sub'), [['sub', 'gitlink', head]])

    def test_non_utf8_filename(self):
        raw = os.path.join(os.fsencode(self.project), b'bad\xff.txt')
        with open(raw, 'wb') as handle:
            handle.write(b'payload')
        state = capture(self.project)
        self.assertEqual(len(self.entry(state, 'hex:' + b'bad\xff.txt'.hex())), 1)
        self.assertEqual(len(self.entry(state, 'source.py')), 1)
        with open(raw, 'wb') as handle:
            handle.write(b'changed')
        self.assertNotEqual(capture(self.project)['digest'], state['digest'])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'needs mkfifo')
    def test_special_file_does_not_raise(self):
        (self.project / 'pipe').write_text('tracked file later replaced by a FIFO')
        git('-C', str(self.project), 'add', 'pipe')
        (self.project / 'pipe').unlink()
        # A tracked path replaced by a FIFO (git does not list untracked FIFOs).
        (self.project / 'pipe').write_text('x')
        git('-C', str(self.project), 'add', 'pipe')
        (self.project / 'pipe').unlink()
        os.mkfifo(self.project / 'pipe')
        state = capture(self.project)
        self.assertEqual(self.entry(state, 'pipe')[0][1], 'special')

    def test_lifecycle_works_with_nested_repo_and_odd_names(self):
        self.nested_repo()
        with open(os.path.join(os.fsencode(self.project), b'odd\xfe'), 'wb') as handle:
            handle.write(b'1')
        store = Store(self.project)
        try:
            engine = Engine(store)
            plan = {'goal': 'g', 'tasks': [{'id': 'one', 'role': 'als_worker', 'write_paths': ['source.py']}],
                    'checks': [{'id': 'unit', 'argv': [sys.executable, '-c', 'print(1)']}]}
            rid = engine.start(plan)
            engine.reserve(rid, 'one', 'c1')
            engine.finish(rid, 'one', 'done', stopped=True)
            engine.accept(rid, 'one')
            engine.pause(rid, 'break')
            engine.resume(rid)
            self.assertTrue(verify.verify(engine, rid, execute_commands=True)['passed'])
            self.assertTrue(engine.complete(rid)['completed'])
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
