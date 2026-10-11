"""Regression tests for the 0.5.0 state-engine audit fixes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import tempfile
import time
import unittest
from unittest.mock import patch
from als_harness.state import Store, locate, evidence_root
from als_harness import verify
from als_harness.engine import Engine, validate_plan
from als_harness import efficiency
from als_harness.common import HarnessError, process_alive, process_info, process_start, redact, relative

REPO = Path(__file__).resolve().parents[1]



def recycled(start):
    """A start time that differs from the real one, in its platform form:
    Linux gives integer clock ticks, macOS a `ps -o lstart=` string."""
    if isinstance(start, int):
        return start + 1
    return (start or '') + ' (recycled)'

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


class VerificationLockTests(EngineCase):
    SLOW = [sys.executable, '-c', 'import time; time.sleep(60)']

    def spawn_verify(self, rid):
        code = ('import sys, faulthandler, signal; faulthandler.register(signal.SIGUSR1, all_threads=True);'
                'sys.path.insert(0, sys.argv[1]); from pathlib import Path;'
                'from als_harness.state import Store; from als_harness.engine import Engine;'
                'from als_harness import verify;'
                'verify.verify(Engine(Store(Path(sys.argv[2]))), sys.argv[3], execute_commands=True)')
        # stderr goes to a file so a stuck verify can dump its stack (SIGUSR1).
        self.verify_err = Path(self.tmp.name) / ('verify-%s.err' % rid)
        err = open(self.verify_err, 'wb')
        self.addCleanup(err.close)
        proc = subprocess.Popen([sys.executable, '-c', code, str(REPO), str(self.project), rid],
                                stdout=subprocess.DEVNULL, stderr=err)
        self.addCleanup(self.reap, proc)
        # Generous deadline for loaded machines; polls, never a fixed sleep.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            lock = self.s.run(rid).get('verification') or {}
            if lock.get('pgid'):
                return proc, lock
            if proc.poll() is not None:
                self.fail('verify exited (%s) before recording its check process group' % proc.returncode)
            time.sleep(.05)
        self.fail('verify never recorded its check process group')

    def reap(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    def wait_dead(self, pid, start):
        deadline = time.monotonic() + 30
        uid = os.geteuid()  # verify and its checks run as this test's uid
        while process_alive(pid, start, uid) and time.monotonic() < deadline:
            time.sleep(.05)
        return not process_alive(pid, start, uid)

    def assert_dead(self, pid, start, verify_proc=None):
        """wait_dead with a diagnostic message (platform process-state paths differ)."""
        if self.wait_dead(pid, start):
            return
        raw = subprocess.run(['ps', '-o', 'pid=,ppid=,pgid=,stat=,lstart=,command=', '-p', str(pid)],
                             capture_output=True, text=True)
        stack = ''
        if verify_proc is not None and verify_proc.poll() is None:
            os.kill(verify_proc.pid, signal.SIGUSR1)
            time.sleep(1)
            stack = self.verify_err.read_text(errors='replace')[-12000:]
        events = [dict(r) for r in self.s.db.execute(
            "SELECT key, kind, doc FROM events WHERE kind='verification_lock_cleared'")]
        self.fail('check group leader %s still alive: process_info=%r recorded_start=%r ps=%r verify_rc=%r\n'
                  'lock events: %r\nverify stack:\n%s'
                  % (pid, process_info(pid), start, raw.stdout.strip(),
                     verify_proc.poll() if verify_proc is not None else None, events, stack))

    def test_lock_records_owner_and_check_group(self):
        rid = self.start(argv=self.SLOW)
        proc, lock = self.spawn_verify(rid)
        self.assertEqual(lock['pid'], proc.pid)
        self.assertEqual(lock['pgid_start'], process_start(lock['pgid']))
        self.assertEqual(lock['check'], 'unit')
        self.e.pause(rid, 'stop')
        self.assertTrue(self.wait_dead(lock['pgid'], lock['pgid_start']))

    def test_sigkilled_verify_lock_is_cleared_automatically(self):
        rid = self.start([self.task('one')], argv=self.SLOW)
        proc, lock = self.spawn_verify(rid)
        with self.s.tx():
            self.assertEqual(len(self.e.live_verifications()), 1)
        proc.kill()
        proc.wait()
        self.assertTrue(self.e.reserve(rid, 'one', 'c1')['allowed'])
        self.assertIsNone(self.s.run(rid)['verification'])
        self.assertTrue(self.wait_dead(lock['pgid'], lock['pgid_start']))
        event = self.s.get('events', 'verification-cleared:' + lock['ticket'], col='key')
        self.assertEqual(event['reason'], 'verify process no longer running')
        self.assertTrue(event['check_group_killed'])

    def test_pause_and_cancel_clear_live_lock_and_kill_check(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                rid = self.start(argv=self.SLOW)
                proc, lock = self.spawn_verify(rid)
                self.e.pause(rid, 'operator stop', cancel=cancel)
                self.assertIsNone(self.s.run(rid)['verification'])
                self.assert_dead(lock['pgid'], lock['pgid_start'], proc)
                proc.wait(timeout=60)
                self.assertEqual(self.s.run(rid)['evidence'], {})

    def test_resume_requires_ack_for_live_lock_then_clears_it(self):
        rid = self.start(argv=self.SLOW)
        proc, lock = self.spawn_verify(rid)
        with self.assertRaises(HarnessError):
            self.e.resume(rid)
        self.e.resume(rid, workers_stopped=True)
        self.assertIsNone(self.s.run(rid)['verification'])
        self.assert_dead(lock['pgid'], lock['pgid_start'], proc)
        proc.wait(timeout=60)
        self.assertEqual(self.s.run(rid)['evidence'], {})

    def test_recycled_pid_lock_is_stale_and_foreign_group_is_never_killed(self):
        rid = self.start()
        bystander = subprocess.Popen(self.SLOW, start_new_session=True)
        self.addCleanup(self.reap, bystander)
        me = os.getpid()
        with self.s.tx():
            run = self.s.run(rid)
            run['verification'] = {'ticket': 'forged', 'pid': me, 'pid_start': recycled(process_start(me)),
                                   'boot_id': None, 'started': 0, 'pgid': bystander.pid,
                                   'pgid_start': recycled(process_start(bystander.pid)), 'check': 'unit'}
            self.s.save_run(run)
        self.assertTrue(verify.verify(self.e, rid, execute_commands=True)['passed'])
        self.assertIsNone(bystander.poll())

    def test_legacy_lock_from_dead_process_is_cleared(self):
        rid = self.start()
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        with self.s.tx():
            run = self.s.run(rid)
            run['verification'] = {'ticket': 'old', 'started': 0, 'pid': dead.pid}
            self.s.save_run(run)
        self.assertTrue(verify.verify(self.e, rid, execute_commands=True)['passed'])
        self.assertTrue(self.e.complete(rid)['completed'])


class ProcessIdentityFallbackTests(unittest.TestCase):
    def test_ps_fallback_without_proc(self):
        from als_harness import common
        real_read = Path.read_bytes

        def no_proc(path):
            if str(path).startswith('/proc/'):
                raise FileNotFoundError(path)
            return real_read(path)

        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        with patch.object(common.sys, 'platform', 'darwin'), patch.object(Path, 'read_bytes', no_proc):
            start = common.process_start(child.pid)
            self.assertIsInstance(start, str)
            self.assertTrue(common.process_alive(child.pid, start))
            self.assertFalse(common.process_alive(child.pid, start + ' recycled'))
            child.kill()
            child.wait()
            self.assertFalse(common.process_alive(child.pid, start))


    def test_unlisted_pid_with_successful_kill_is_reported_exited(self):
        """macOS: an exited (zombie) process is missing from `ps -p` while
        kill(pid, 0) still succeeds; it must not count as alive."""
        from als_harness import common

        def no_proc(path):
            raise FileNotFoundError(path)

        empty = subprocess.CompletedProcess(['ps'], 1, b'', b'')
        with patch.object(common.sys, 'platform', 'darwin'), patch.object(Path, 'read_bytes', no_proc), \
                patch.object(common.os, 'kill', return_value=None), \
                patch.object(common.subprocess, 'run', return_value=empty):
            self.assertEqual(common.process_info(4242), ('X', None))
            self.assertFalse(common.process_alive(4242, 'Mon Oct 12 00:00:00 2026'))
            self.assertIsNone(common.process_start(4242))
        with patch.object(common.sys, 'platform', 'darwin'), patch.object(Path, 'read_bytes', no_proc), \
                patch.object(common.os, 'kill', return_value=None), \
                patch.object(common.subprocess, 'run', side_effect=FileNotFoundError('ps')):
            # ps unavailable: unknown, so liveness stays conservative (alive).
            self.assertIsNone(common.process_info(4242))
            self.assertTrue(common.process_alive(4242))

    def test_sandbox_hidden_live_owner_stays_alive(self):
        """Codex Seatbelt can deny both signals and ps for a LIVE same-uid
        process: never report it exited (that would release a live lock)."""
        from als_harness import common

        def no_proc(path):
            raise FileNotFoundError(path)

        empty = subprocess.CompletedProcess(['ps'], 1, b'', b'')
        with patch.object(common.sys, 'platform', 'darwin'), patch.object(Path, 'read_bytes', no_proc), \
                patch.object(common.os, 'kill', side_effect=PermissionError(1, 'EPERM')), \
                patch.object(common.subprocess, 'run', return_value=empty):
            self.assertIsNone(common.process_info(4242))
            self.assertTrue(common.process_alive(4242, 'start', os.geteuid()))

    def test_eperm_with_readable_info_uses_start_time(self):
        """sudo/other uid: signals denied but /proc readable; start decides."""
        from als_harness import common
        with patch.object(common.os, 'kill', side_effect=PermissionError(1, 'EPERM')), \
                patch.object(common, 'process_info', return_value=('S', 5459)):
            self.assertTrue(common.process_alive(4242, 5459, os.geteuid()))
            self.assertFalse(common.process_alive(4242, 9999, os.geteuid()))

    def test_linux_pid_recycled_by_another_user_is_not_alive(self):
        """Linux: EPERM plus unreadable /proc for an owner recorded with our uid
        means another user's process holds the pid. Without a recorded uid
        (legacy lock) or on other platforms it stays conservative."""
        from als_harness import common

        def no_proc(path):
            raise FileNotFoundError(path)  # e.g. hidepid hides other users

        with patch.object(common.sys, 'platform', 'linux'), patch.object(Path, 'read_bytes', no_proc), \
                patch.object(common.os, 'kill', side_effect=PermissionError(1, 'EPERM')):
            self.assertFalse(common.process_alive(4242, 5459, os.geteuid()))
            self.assertTrue(common.process_alive(4242, 5459))  # legacy lock: no uid
            self.assertTrue(common.process_alive(4242, 5459, os.geteuid() + 1))  # recorded as another uid

class TaskCheckConcurrencyTests(EngineCase):
    def test_task_check_runs_beside_read_only_and_other_run_writers(self):
        rid = self.start([self.task('scan', None, 'als_explorer'), self.task('edit')], session='s1')
        other = self.start([self.task('elsewhere', 'other.py')], session='s2')
        self.e.reserve(rid, 'scan', 'c1')
        self.e.reserve(other, 'elsewhere', 'c2')
        self.assertTrue(verify.verify(self.e, rid, 'unit', execute_commands=True)['passed'])
        with self.assertRaises(HarnessError):
            verify.verify(self.e, rid, execute_commands=True)

    def test_task_check_refused_while_this_run_holds_write_claim(self):
        rid = self.start([self.task('edit')])
        self.e.reserve(rid, 'edit', 'c1')
        with self.assertRaises(HarnessError):
            verify.verify(self.e, rid, 'unit', execute_commands=True)


def prepared_task(tid='scan', **kwargs):
    item = {'id': tid, 'objective': 'Inspect module ' + tid, 'kind': 'exploration', 'complexity': 'bounded',
            'risk': 'low', 'context_files': [], 'write_paths': [], 'resources': [], 'depends_on': [],
            'checks': [], 'acceptance': ['Return evidence']}
    item.update(kwargs)
    return item


def prepared_plan(tasks):
    draft = {'goal': 'prepared', 'workload': 'bounded-fix', 'purpose': 'production', 'tasks': tasks,
             'checks': [{'id': 'unit', 'argv': [sys.executable, '-c', 'print(1)']}], 'criteria': [],
             'budget': {'tokens': 0, 'seconds': 0}}
    return efficiency.prepare(draft)


class PlanValidationTests(EngineCase):
    def test_legacy_plan_rejects_runtime_keys(self):
        for key, value in (('executor', 'head'), ('claims_held', True), ('accepted_snapshot', 'x'),
                           ('attempts', []), ('status', 'completed'), ('result', {}), ('run', 'other')):
            with self.subTest(key=key):
                task = self.task()
                task[key] = value
                with self.assertRaises(HarnessError):
                    validate_plan(self.plan([task]))

    def test_head_executor_cannot_bypass_gates_in_legacy_plan(self):
        task = self.task()
        task['executor'] = 'head'
        with self.assertRaises(HarnessError):
            self.e.start(self.plan([task]))

    def test_prepared_plan_keeps_executor_and_policy_keys(self):
        plan = prepared_plan([prepared_task(complexity='trivial')])
        plan['tasks'][0]['recommend_escalation'] = True
        plan['efficiency']['caller_limits_capped'] = ['max_concurrent']
        checked = validate_plan(plan)
        self.assertEqual(checked['tasks'][0]['executor'], 'head')
        self.assertTrue(checked['tasks'][0]['recommend_escalation'])
        self.assertEqual(checked['efficiency']['caller_limits_capped'], ['max_concurrent'])
        plan['tasks'][0]['claims_held'] = True
        with self.assertRaises(HarnessError):
            validate_plan(plan)


class HighRiskRetryTests(EngineCase):
    def failed_run(self, task):
        rid = self.e.start(prepared_plan([task]))
        self.assertTrue(self.e.reserve(rid, task['id'], 'c1')['allowed'])
        self.e.finish(rid, task['id'], 'failed', failed=True, stopped=True)
        return rid

    def test_read_only_kinds_may_retry_on_read_only_roles(self):
        for kind, role in (('exploration', 'als_explorer'), ('research', 'als_researcher')):
            with self.subTest(kind=kind):
                rid = self.failed_run(prepared_task('t' + kind[:3], kind=kind, risk='high'))
                self.e.retry(rid, 't' + kind[:3], 'narrower read-only pass', role)
                self.assertEqual(self.s.task(rid, 't' + kind[:3])['role'], role)
                self.e.pause(rid, 'done', cancel=True)

    def test_high_risk_write_task_cannot_be_downgraded(self):
        rid = self.failed_run(prepared_task('impl', kind='implementation', risk='high', write_paths=['source.py']))
        with self.assertRaises(HarnessError):
            self.e.retry(rid, 'impl', 'cheaper worker', 'als_worker')
        with self.assertRaises(HarnessError):
            self.e.retry(rid, 'impl', 'read only', 'als_explorer')
        self.e.retry(rid, 'impl', 'deep worker again', 'als_hard_worker')


if __name__ == '__main__':
    unittest.main()
