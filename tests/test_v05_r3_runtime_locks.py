"""ALS 0.5.0 round 3: verification cleanup order and legacy lock staleness."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from als_harness import verify
from als_harness.common import now
from als_harness.engine import Engine
from als_harness.state import Store


class ReapOrderTests(unittest.TestCase):
    def test_group_killed_before_leader_is_reaped(self):
        seen = []
        real = os.killpg

        def spy(pgid, sig):
            # os.kill(pid, 0) succeeds for an unreaped (zombie) leader and
            # raises ProcessLookupError once it was reaped and its pid freed.
            try:
                os.kill(pgid, 0)
                seen.append('unreaped')
            except ProcessLookupError:
                seen.append('reaped')
            return real(pgid, sig)
        with patch.object(verify.os, 'killpg', spy):
            out = verify.execute([sys.executable, '-c', 'print("ok")'], Path.cwd(), 30)
        self.assertEqual(out['exit_code'], 0)
        self.assertEqual(out['log'].strip(), 'ok')
        self.assertEqual(seen, ['unreaped'])

    def test_timeout_still_kills_group(self):
        out = verify.execute([sys.executable, '-c', 'import time; time.sleep(30)'], Path.cwd(), 1)
        self.assertTrue(out['timed_out'])
        self.assertNotEqual(out['exit_code'], 0)

    def test_leader_exit_with_lingering_descendant(self):
        code = ('import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],'
                ' stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print("done")')
        out = verify.execute([sys.executable, '-c', code], Path.cwd(), 20)
        self.assertEqual(out['exit_code'], 0)
        self.assertFalse(out['timed_out'])


class LegacyLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        self.s = Store(self.project)
        self.e = Engine(self.s)
        self.rid = self.e.start({'goal': 'locks', 'checks': [
            {'id': 'unit', 'argv': [sys.executable, '-c', 'pass'], 'timeout': 30}]})

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def legacy(self, age):
        # A 0.4 lock: no pid_start/boot_id; the pid (this process) is alive.
        with self.s.tx():
            run = self.s.run(self.rid)
            run['verification'] = {'ticket': 'legacy', 'pid': os.getpid(), 'started': now() - age, 'check': 'unit'}
            self.s.save_run(run)

    def test_old_legacy_lock_with_live_pid_is_stale(self):
        self.legacy(30 + 60 + 5)
        self.assertTrue(verify.verify(self.e, self.rid, execute_commands=True)['passed'])
        event = self.s.get('events', 'verification-cleared:legacy', col='key')
        self.assertEqual(event['reason'], 'verify process no longer running')

    def test_recent_legacy_lock_with_live_pid_is_respected(self):
        self.legacy(5)
        with self.s.tx():
            self.assertEqual(len(self.e.live_verifications()), 1)

    def test_unknown_check_uses_one_hour(self):
        run = self.s.run(self.rid)
        lock = {'ticket': 'x', 'pid': os.getpid(), 'started': now() - 3000}
        self.assertTrue(self.e.verification_live(lock, run))
        lock['started'] = now() - 3600 - 61
        self.assertFalse(self.e.verification_live(lock, run))

    def test_modern_lock_is_not_aged_out(self):
        from als_harness.common import boot_id, process_start
        lock = {'ticket': 'x', 'pid': os.getpid(), 'pid_start': process_start(os.getpid()), 'boot_id': boot_id(),
                'started': now() - 100000, 'check': 'unit'}
        self.assertTrue(self.e.verification_live(lock, self.s.run(self.rid)))


if __name__ == '__main__':
    unittest.main()
