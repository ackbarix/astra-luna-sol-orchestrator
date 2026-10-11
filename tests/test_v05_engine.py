"""Regression tests for the 0.5.0 state-engine audit fixes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from als_harness.state import Store, locate, evidence_root
from als_harness import verify
from als_harness.engine import Engine
from als_harness.common import HarnessError, redact, relative


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        (self.project / 'source.py').write_text('x=1\n')
        self.s = Store(self.project)
        self.e = Engine(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def plan(self, tasks=None, argv=None):
        argv = argv or [sys.executable, '-c', 'print("ok")']
        return {'goal': 'synthetic', 'tasks': tasks or [],
                'checks': [{'id': 'unit', 'argv': argv, 'timeout': 5}], 'criteria': []}

    def task(self, key='one', path='source.py', role='als_worker'):
        return {'id': key, 'role': role, 'write_paths': [path] if path else [], 'checks': []}

    def start(self, tasks=None, session=None, **kwargs):
        return self.e.start(self.plan(tasks, **kwargs), session)


class RedactAndPathTests(unittest.TestCase):
    def test_redact_extended_secret_shapes(self):
        samples = {
            'secret=s3cr3tvalue': 's3cr3tvalue',
            'passwd: hunter22': 'hunter22',
            'private_key=abcd1234': 'abcd1234',
            'credential=c-r-e-d': 'c-r-e-d',
            'run --token tok123456 next': 'tok123456',
            'jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl end': 'eyJzdWIiOiIxIn0',
            'key AKIAABCDEFGHIJKLMNOP here': 'AKIAABCDEFGHIJKLMNOP',
            'github_pat_11ABCDEFG_abcdefghijk': 'github_pat_11ABCDEFG',
            '{"password": "pw-in-json"}': 'pw-in-json',
        }
        for text, secret in samples.items():
            with self.subTest(text=text):
                self.assertNotIn(secret, redact(text))
                self.assertIn('[REDACTED]', redact(text))

    def test_redact_keeps_ordinary_text(self):
        self.assertEqual(redact('ran 12 tests --tokens-file report'), 'ran 12 tests --tokens-file report')

    def test_codex_hook_configuration_is_reserved(self):
        for value in ('.codex', '.codex/hooks.json', '.git/config', '.astra-luna-sol/x'):
            with self.subTest(value=value):
                with self.assertRaises(HarnessError):
                    relative(value)
        self.assertEqual(relative('codex/x.py'), 'codex/x.py')


class BudgetTests(EngineCase):
    def add_usage(self, rid, thread, response, input_tokens, cached, output):
        usage = {'input_tokens': input_tokens, 'cached_input_tokens': cached, 'output_tokens': output}
        with self.s.tx():
            self.s.db.execute('INSERT INTO usage VALUES(?,?,?,?)',
                              (rid, thread, response, json.dumps({'usage': usage})))

    def test_budget_counts_uncached_input_plus_output(self):
        task = self.task()
        task['estimate_tokens'] = 1000
        rid = self.start([task])
        self.e.reserve(rid, 'one', 'call')
        self.e.bind(rid, 'call', 'thread-1')
        self.add_usage(rid, 'thread-1', 'r1', 900, 800, 50)
        budget = self.e.budget(self.s.run(rid))
        self.assertEqual(budget['confirmed_tokens'], 150)
        self.assertEqual(budget['reserved_estimate'], 850)


class StateScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        (self.project / 'source.py').write_text('x=1\n')
        self.env = patch.dict(os.environ, {'ALS_STATE_HOME': str(self.root / 'state-home')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_project_scope_layout_unchanged(self):
        self.assertEqual(locate(self.project), self.project.resolve() / '.astra-luna-sol/state/runs.sqlite')
        self.assertEqual(evidence_root(self.project), self.project.resolve() / '.astra-luna-sol/evidence')
        store = Store(self.project)
        try:
            self.assertEqual(store.scope, 'project')
            self.assertEqual(store.path, locate(self.project))
            self.assertEqual(store.evidence_root, evidence_root(self.project))
        finally:
            store.close()

    def test_global_scope_paths_and_marker(self):
        db = locate(self.project, 'global')
        self.assertEqual(db.parent.parent, self.root / 'state-home')
        self.assertEqual(len(db.parent.name), 16)
        self.assertFalse(db.exists())
        store = Store(self.project, scope='global')
        try:
            self.assertEqual(store.path, db)
            self.assertTrue(db.is_file())
            self.assertEqual(store.evidence_root, db.parent / 'evidence')
            self.assertEqual((db.parent / 'project.txt').read_text().strip(), str(self.project.resolve()))
            self.assertFalse((self.project / '.astra-luna-sol').exists())
        finally:
            store.close()

    def test_global_scope_falls_back_to_xdg(self):
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'xdg')}):
            os.environ.pop('ALS_STATE_HOME')
            self.assertEqual(locate(self.project, 'global').parent.parent, self.root / 'xdg' / 'als')

    def test_unknown_scope_rejected(self):
        with self.assertRaises(HarnessError):
            locate(self.project, 'elsewhere')

    def test_global_scope_verification_evidence(self):
        store = Store(self.project, scope='global')
        try:
            engine = Engine(store)
            rid = engine.start({'goal': 'g', 'checks': [{'id': 'unit', 'argv': [sys.executable, '-c', 'print(1)']}]})
            self.assertTrue(verify.verify(engine, rid, execute_commands=True)['passed'])
            log_path = store.run(rid)['evidence']['unit']['log_path']
            self.assertTrue(os.path.isabs(log_path))
            self.assertTrue(Path(log_path).is_relative_to(store.evidence_root))
            self.assertTrue(engine.complete(rid)['completed'])
        finally:
            store.close()


class EvidencePathTests(EngineCase):
    def test_evidence_outside_root_or_symlinked_is_invalid(self):
        rid = self.start()
        self.assertTrue(verify.verify(self.e, rid, execute_commands=True)['passed'])
        record = self.s.run(rid)['evidence']['unit']
        snap = record['snapshot']
        self.assertTrue(self.e.evidence_valid(record, snap))
        forged = dict(record, log_path='source.py', log_sha256=hashlib.sha256(b'x=1\n').hexdigest())
        self.assertFalse(self.e.evidence_valid(forged, snap))
        absolute = dict(record, log_path=str(self.project / record['log_path']))
        self.assertFalse(self.e.evidence_valid(absolute, snap))
        log = self.project / record['log_path']
        moved = log.with_name('real.log')
        log.rename(moved)
        log.symlink_to(moved)
        self.assertFalse(self.e.evidence_valid(record, snap))


class LifecycleTests(EngineCase):
    def stopped_worker(self, rid, tid, call, thread):
        self.assertTrue(self.e.reserve(rid, tid, call)['allowed'])
        self.e.bind(rid, call, thread)
        self.e.worker_stopped('sid', thread, 'als_worker')
        self.assertEqual(self.s.task(rid, tid)['status'], 'awaiting_validation')

    def test_cancel_releases_claims_for_later_runs(self):
        first = self.start([self.task('a'), self.task('b', 'other.py')], session='s1')
        self.e.reserve(first, 'a', 'ca')
        self.stopped_worker(first, 'b', 'cb', 'thread-b')
        self.assertTrue(self.s.task(first, 'b')['claims_held'])
        self.e.pause(first, 'stop it', cancel=True)
        for key in ('a', 'b'):
            task = self.s.task(first, key)
            self.assertEqual(task['status'], 'cancelled')
            self.assertFalse(task['claims_held'])
            self.assertEqual(task['attempts'][-1]['status'], 'cancelled')
        second = self.start([self.task('a'), self.task('b', 'other.py')], session='s2')
        self.assertTrue(self.e.reserve(second, 'a', 'c2a')['allowed'])
        self.assertTrue(self.e.reserve(second, 'b', 'c2b')['allowed'])

    def test_stale_claims_of_terminal_runs_are_ignored(self):
        first = self.start([self.task('a')], session='s1')
        self.e.reserve(first, 'a', 'ca')
        with self.s.tx():
            run = self.s.run(first)
            run['status'] = 'cancelled'
            self.s.save_run(run)
        second = self.start([self.task('a')], session='s2')
        self.assertTrue(verify.verify(self.e, second, execute_commands=True)['passed'])
        self.assertTrue(self.e.reserve(second, 'a', 'c2')['allowed'])

    def test_resume_keeps_finished_work_awaiting_validation(self):
        rid = self.start([self.task('done'), self.task('busy', 'other.py')])
        self.stopped_worker(rid, 'done', 'c1', 'thread-1')
        self.e.reserve(rid, 'busy', 'c2')
        self.e.pause(rid, 'interrupted')
        with self.assertRaises(HarnessError):
            self.e.resume(rid)
        self.e.resume(rid, workers_stopped=True)
        self.assertEqual(self.s.task(rid, 'done')['status'], 'awaiting_validation')
        self.assertEqual(self.s.task(rid, 'busy')['status'], 'failed')
        self.assertEqual(self.s.task(rid, 'busy')['attempts'][-1]['status'], 'interrupted')
        self.e.finish(rid, 'done', 'worker result')
        self.e.accept(rid, 'done')
        self.assertEqual(self.s.task(rid, 'done')['status'], 'completed')

    def test_resume_without_flag_when_only_stopped_workers_hold_claims(self):
        rid = self.start([self.task('done')])
        self.stopped_worker(rid, 'done', 'c1', 'thread-1')
        self.e.pause(rid, 'break')
        self.assertTrue(self.e.resume(rid)['resumed'])
        self.assertEqual(self.s.task(rid, 'done')['status'], 'awaiting_validation')

    def test_snapshot_observed_by_finish_accept_assess_complete(self):
        plan = self.plan([self.task('one')])
        plan['criteria'] = [{'id': 'crit', 'description': 'reviewed'}]
        rid = self.e.start(plan)
        steps = [
            lambda: (self.e.reserve(rid, 'one', 'c1'), self.e.finish(rid, 'one', 'note', stopped=True)),
            lambda: self.e.accept(rid, 'one'),
            lambda: self.e.assess(rid, 'crit', 'looks fine', 'head'),
            lambda: self.e.complete(rid),
        ]
        for number, step in enumerate(steps):
            with self.subTest(step=number):
                (self.project / 'source.py').write_text('x=' + str(number + 10) + '\n')
                step()
                self.e.pause(rid, 'checkpoint')
                self.e.resume(rid)
        self.assertNotIn('checkpoint', self.s.run(rid))

    def test_retry_clears_result_and_accept_requires_new_finish(self):
        rid = self.start([self.task('one')])
        self.e.reserve(rid, 'one', 'c1')
        self.e.finish(rid, 'one', 'broken', failed=True, stopped=True)
        self.e.retry(rid, 'one', 'different approach')
        self.assertIsNone(self.s.task(rid, 'one')['result'])
        self.stopped_worker(rid, 'one', 'c2', 'thread-2')
        with self.assertRaises(HarnessError):
            self.e.accept(rid, 'one')
        with self.s.tx():
            task = self.s.task(rid, 'one')
            task['result'] = {'note': 'stale', 'snapshot': None, 'basis': 'caller_reported', 'stamp': 1.0}
            self.s.save_task(task)
        with self.assertRaises(HarnessError):
            self.e.accept(rid, 'one')
        self.e.finish(rid, 'one', 'fixed')
        self.e.accept(rid, 'one')
        self.assertEqual(self.s.task(rid, 'one')['status'], 'completed')

    def test_completed_run_is_never_downgraded(self):
        rid = self.start(session='sid')
        self.assertTrue(verify.verify(self.e, rid, execute_commands=True)['passed'])
        self.assertTrue(self.e.complete(rid)['completed'])
        log = self.s.evidence_path(self.s.run(rid)['evidence']['unit']['log_path'])
        log.unlink()
        again = self.e.complete(rid)
        self.assertTrue(again['completed'])
        self.assertTrue(again['already_completed'])
        self.assertEqual(self.s.run(rid)['status'], 'completed')
        self.assertIsNone(self.s.for_session('sid'))
        other = self.start(session='sid')
        self.assertEqual(self.s.for_session('sid')['id'], other)
        (self.project / 'source.py').write_text('changed\n')
        with self.assertRaises(HarnessError):
            self.e.complete(rid)
        self.assertEqual(self.s.run(rid)['status'], 'completed')

    def test_bind_after_pause_keeps_task_uncertain(self):
        rid = self.start([self.task('one')])
        self.e.reserve(rid, 'one', 'c1')
        self.e.pause(rid, 'interrupted')
        result = self.e.bind(rid, 'c1', 'thread-1')
        task = self.s.task(rid, 'one')
        self.assertEqual(task['status'], 'uncertain')
        self.assertEqual(task['attempts'][-1]['thread_id'], 'thread-1')
        self.assertTrue(result['bound'])

    def test_claim_release_survives_snapshot_failure(self):
        rid = self.start([self.task('one'), self.task('two', 'other.py')])
        self.e.reserve(rid, 'one', 'c1')
        self.e.reserve(rid, 'two', 'c2')
        with patch('als_harness.engine.capture', side_effect=HarnessError('Git snapshot failed')):
            self.e.finish(rid, 'one', 'crashed', failed=True, stopped=True)
            task = self.s.task(rid, 'one')
            self.assertEqual(task['status'], 'failed')
            self.assertFalse(task['claims_held'])
            self.assertIsNone(task['result']['snapshot'])
            self.assertTrue(any('snapshot unavailable' in w for w in self.s.run(rid)['warnings']))
            self.e.pause(rid, 'pause anyway')
            self.assertEqual(self.s.run(rid)['status'], 'paused')
            self.e.pause(rid, 'cancel anyway', cancel=True)
        self.assertEqual(self.s.run(rid)['status'], 'cancelled')
        self.assertFalse(self.s.task(rid, 'two')['claims_held'])


if __name__ == '__main__':
    unittest.main()
