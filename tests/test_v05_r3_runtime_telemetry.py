"""ALS 0.5.0 round 3: telemetry treats cancelled/interrupted attempts as failures."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v05_policy_telemetry as base  # noqa: E402
from test_efficiency import EngineFixture  # noqa: E402


class AttemptOutcomeTests(EngineFixture, unittest.TestCase):
    sample = base.CalibrationTests.sample
    history = base.CalibrationTests.history

    def bound_failure(self, idx, status):
        r = self.sample(idx, failed_first='bound_zero')
        self.event(r, 'dead' + str(idx), model='gpt-6-luna', response='dead')
        task = self.s.task(r, 'scan')
        task['attempts'][0]['status'] = status
        self.s.save_task(task)
        return r

    def test_cancelled_and_interrupted_count_as_failed(self):
        for status in ('cancelled', 'interrupted', 'failed'):
            with self.subTest(status=status):
                self.s.db.execute('DELETE FROM runs')
                self.s.db.execute('DELETE FROM tasks')
                self.s.db.execute('DELETE FROM usage')
                for i in range(5):
                    self.bound_failure(i, status)
                h = self.history()
                self.assertTrue(h['eligible'], h)
                self.assertEqual(h['failed_attempts'], 5)

    def test_non_completed_tasks_are_not_cost_samples(self):
        for i in range(5):
            r = self.sample(i)
            task = self.s.task(r, 'scan')
            task['status'] = 'cancelled'
            self.s.save_task(task)
        h = self.history()
        self.assertTrue(h['eligible'])
        self.assertEqual(h['estimates'], {})


if __name__ == '__main__':
    unittest.main()
