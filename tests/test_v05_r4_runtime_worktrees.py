"""ALS 0.5.0 round 4: per-worktree claims/locks and recorded-worktree validation.

Real `git worktree add` in temp repos only; no Codex calls.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from als_harness import report, verify
from als_harness.common import HarnessError
from als_harness.engine import Engine
from als_harness.state import Store

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.project = self.base / 'project'
        self.project.mkdir()
        git('init', '-q', cwd=self.project)
        (self.project / 'source.py').write_text('x=1\n')
        git('add', 'source.py', cwd=self.project)
        git('commit', '-q', '-m', 'init', cwd=self.project)
        self.wt_a = self.base / 'wt-a'
        self.wt_b = self.base / 'wt-b'
        git('worktree', 'add', '-q', '-b', 'a', str(self.wt_a), cwd=self.project)
        git('worktree', 'add', '-q', '-b', 'b', str(self.wt_b), cwd=self.project)
        self.env = patch.dict(os.environ, {'ALS_STATE_HOME': str(self.base / 'state'),
                                           'CODEX_HOME': str(self.base / 'codex'), 'CODEX_THREAD_ID': ''})
        self.env.start()
        self.s = Store(self.project, scope='global')
        self.e = Engine(self.s)

    def tearDown(self):
        self.s.close()
        self.env.stop()
        self.tmp.cleanup()

    def plan(self, write_paths=('source.py',), resources=(), task_id='one'):
        return {'goal': 'g', 'tasks': [{'id': task_id, 'role': 'als_worker', 'write_paths': list(write_paths),
                                        'resources': list(resources)}],
                'checks': [{'id': 'unit', 'argv': [sys.executable, '-c', 'pass'], 'timeout': 30}], 'criteria': []}

    def checks_only(self):
        return {'goal': 'g', 'tasks': [],
                'checks': [{'id': 'unit', 'argv': [sys.executable, '-c', 'pass'], 'timeout': 30}], 'criteria': []}


class PerWorktreeClaimTests(Base):
    def test_same_relative_path_in_two_worktrees_does_not_conflict(self):
        a = self.e.start(self.plan(), worktree=self.wt_a)
        b = self.e.start(self.plan(), worktree=self.wt_b)
        self.assertTrue(self.e.reserve(a, 'one', 'ca')['allowed'])
        out = self.e.reserve(b, 'one', 'cb')
        self.assertTrue(out['allowed'], out)

    def test_same_worktree_still_conflicts(self):
        a = self.e.start(self.plan(), worktree=self.wt_a)
        b = self.e.start(self.plan(), worktree=self.wt_a)
        self.assertTrue(self.e.reserve(a, 'one', 'ca')['allowed'])
        out = self.e.reserve(b, 'one', 'cb')
        self.assertFalse(out['allowed'])
        self.assertTrue(any(r.startswith('resource owned by ' + a) for r in out['reasons']), out)

    def test_shared_resources_conflict_across_worktrees(self):
        a = self.e.start(self.plan(['a.py'], ['port:8080']), worktree=self.wt_a)
        b = self.e.start(self.plan(['b.py'], ['port:8080']), worktree=self.wt_b)
        self.assertTrue(self.e.reserve(a, 'one', 'ca')['allowed'])
        out = self.e.reserve(b, 'one', 'cb')
        self.assertFalse(out['allowed'])
        self.assertIn('resource owned by ' + a + '/one', out['reasons'])

    def test_writer_cap_is_per_worktree(self):
        plan = self.plan()
        plan['tasks'] = [{'id': 'w1', 'role': 'als_worker', 'write_paths': ['one.py']},
                         {'id': 'w2', 'role': 'als_worker', 'write_paths': ['two.py']}]
        a = self.e.start(plan, worktree=self.wt_a)
        self.assertTrue(self.e.reserve(a, 'w1', 'c1')['allowed'])
        self.assertTrue(self.e.reserve(a, 'w2', 'c2')['allowed'])
        b = self.e.start(self.plan(['three.py']), worktree=self.wt_b)
        out = self.e.reserve(b, 'one', 'cb')
        self.assertTrue(out['allowed'], out)
        same = self.e.start(self.plan(['three.py']), worktree=self.wt_a)
        out = self.e.reserve(same, 'one', 'cs')
        self.assertIn('two-writer capacity reached', out['reasons'])

    def test_final_verify_not_blocked_by_other_worktree_worker(self):
        a = self.e.start(self.plan(), worktree=self.wt_a)
        self.assertTrue(self.e.reserve(a, 'one', 'ca')['allowed'])
        b = self.e.start(self.checks_only(), worktree=self.wt_b)
        self.assertTrue(verify.verify(self.e, b, execute_commands=True)['passed'])
        same = self.e.start(self.checks_only(), worktree=self.wt_a)
        with self.assertRaisesRegex(HarnessError, 'active workers'):
            verify.verify(self.e, same, execute_commands=True)

    def test_verification_lock_is_per_worktree(self):
        a = self.e.start(self.checks_only(), worktree=self.wt_a)
        with self.s.tx():
            run = self.s.run(a)
            run['verification'] = self.e.new_verification_lock('held')  # this live process
            self.s.save_run(run)
        b = self.e.start(self.checks_only(), worktree=self.wt_b)
        self.assertTrue(verify.verify(self.e, b, execute_commands=True)['passed'])
        same = self.e.start(self.checks_only(), worktree=self.wt_a)
        with self.assertRaisesRegex(HarnessError, 'Another verification'):
            verify.verify(self.e, same, execute_commands=True)
        other = self.e.start(self.plan(), worktree=self.wt_b)
        self.assertTrue(self.e.reserve(other, 'one', 'co')['allowed'])
        here = self.e.start(self.plan(['x.py']), worktree=self.wt_a)
        self.assertIn('verification owns stable worktree', self.e.reserve(here, 'one', 'ch')['reasons'])


class RecordedWorktreeValidationTests(Base):
    def start_b(self):
        return self.e.start(self.checks_only(), worktree=self.wt_b)

    def assert_refused_but_manageable(self, rid):
        with self.assertRaisesRegex(HarnessError, 'worktree missing or moved'):
            verify.verify(self.e, rid, execute_commands=True)
        with self.assertRaisesRegex(HarnessError, 'worktree missing or moved'):
            self.e.complete(rid)
        self.assertEqual(report.report(self.e, rid)['status'], 'running')
        self.e.pause(rid, 'gone', cancel=True)
        self.assertEqual(self.s.run(rid)['status'], 'cancelled')

    def test_removed_worktree_is_refused(self):
        rid = self.start_b()
        git('worktree', 'remove', '--force', str(self.wt_b), cwd=self.project)
        self.assert_refused_but_manageable(rid)

    def test_plain_directory_at_old_path_is_refused(self):
        rid = self.start_b()
        git('worktree', 'remove', '--force', str(self.wt_b), cwd=self.project)
        self.wt_b.mkdir()
        (self.wt_b / 'marker').write_text('not the worktree\n')
        self.assert_refused_but_manageable(rid)

    def test_foreign_repository_at_old_path_is_refused(self):
        rid = self.start_b()
        git('worktree', 'remove', '--force', str(self.wt_b), cwd=self.project)
        self.wt_b.mkdir()
        git('init', '-q', cwd=self.wt_b)
        self.assert_refused_but_manageable(rid)

    def test_nested_plain_directory_inside_project_is_refused(self):
        nested = self.project / 'nested-wt'
        git('worktree', 'add', '-q', '-b', 'n', str(nested), cwd=self.project)
        (self.project / '.gitignore').write_text('nested-wt/\n')
        rid = self.e.start(self.checks_only(), worktree=nested)
        git('worktree', 'remove', '--force', str(nested), cwd=self.project)
        nested.mkdir()  # same git common dir via the enclosing checkout, but not a worktree top level
        self.assert_refused_but_manageable(rid)

    def test_intact_worktree_and_project_still_verify(self):
        rid = self.start_b()
        self.assertTrue(verify.verify(self.e, rid, execute_commands=True)['passed'])
        main = self.e.start(self.checks_only())
        self.assertTrue(verify.verify(self.e, main, execute_commands=True)['passed'])

class MovedWorktreeTests(Base):
    """A run whose worktree was moved keeps its claims counting everywhere."""

    def move_a(self):
        moved = self.base / 'wt-a-moved'
        git('worktree', 'move', str(self.wt_a), str(moved), cwd=self.project)
        return moved

    def test_moved_run_claims_still_block_the_new_location(self):
        a = self.e.start(self.plan(), worktree=self.wt_a)
        self.assertTrue(self.e.reserve(a, 'one', 'ca')['allowed'])
        moved = self.move_a()
        b = self.e.start(self.plan(), worktree=moved)
        out = self.e.reserve(b, 'one', 'cb')
        self.assertFalse(out['allowed'], out)
        self.assertTrue(any(r.startswith('resource owned by ' + a) for r in out['reasons']), out)

    def test_moved_run_blocks_final_verify_until_cancelled(self):
        a = self.e.start(self.plan(), worktree=self.wt_a)
        self.assertTrue(self.e.reserve(a, 'one', 'ca')['allowed'])
        moved = self.move_a()
        c = self.e.start(self.checks_only(), worktree=moved)
        with self.assertRaises(HarnessError):
            verify.verify(self.e, c, execute_commands=True)
        self.e.pause(a, 'worktree moved', cancel=True)
        self.assertTrue(verify.verify(self.e, c, execute_commands=True)['passed'])
        b = self.e.start(self.plan(), worktree=moved)
        self.assertTrue(self.e.reserve(b, 'one', 'cb')['allowed'])


if __name__ == '__main__':
    unittest.main()
