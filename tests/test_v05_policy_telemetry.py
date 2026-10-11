"""0.5.0 telemetry calibration regressions: local SQLite fixtures, no model calls."""
import unittest

from als_harness import efficiency as f, report, telemetry as a, verify
from als_harness.common import HarnessError
from test_efficiency import EngineFixture, draft

REASON = 'Six independent modules with disjoint paths and resources'


class CalibrationTests(EngineFixture, unittest.TestCase):
    def sample(self, idx, warnings=(), failed_first=None, accepted_usage=True, quality='pass'):
        """failed_first: None | 'unbound' | 'bound_zero' adds a failed attempt before the success."""
        root, kid = 'root' + str(idx), 'kid' + str(idx)
        r = self.e.start(f.prepare(draft()), session=root)
        if failed_first:
            self.e.reserve(r, 'scan', 'fail')
            if failed_first == 'bound_zero':
                self.e.bind(r, 'fail', 'dead' + str(idx))
            self.e.finish(r, 'scan', 'Worker failed before useful output', failed=True, stopped=True)
            self.e.retry(r, 'scan', 'Narrow the scope to one module')
        self.e.reserve(r, 'scan', 'c')
        self.e.bind(r, 'c', kid)
        self.e.worker_stopped(root, kid, 'als_explorer')
        self.e.finish(r, 'scan', 'Actual inspected findings')
        verify.verify(self.e, r, execute_commands=True)
        self.e.accept(r, 'scan')
        self.e.complete(r)
        self.event(r, root, response='root')
        if accepted_usage:
            self.event(r, kid, model='gpt-6-luna', response='kid')
        a.review_measurement(self.e, r, 'bounded-fix', quality, True)
        if warnings:
            x = self.s.run(r)
            x['warnings'] = list(warnings)
            self.s.save_run(x)
        return r

    def history(self):
        return a.history(self.s, 'bounded-fix')

    def test_benign_warnings_do_not_disqualify(self):
        benign = ['no response ID: exact-record deduplication used', 'not_reported:cached_input_tokens',
                  'reported_total_differs_from_input_plus_output']
        for i in range(5):
            self.sample(i, warnings=benign)
        self.assertTrue(self.history()['eligible'])

    def test_integrity_warnings_disqualify(self):
        for i in range(5):
            self.sample(i, warnings=['conflicting response ID; first record retained'])
        self.assertFalse(self.history()['eligible'])

    def test_unbound_failed_attempts_are_zero_cost_failures(self):
        for i in range(5):
            self.sample(i, failed_first='unbound')
        h = self.history()
        self.assertTrue(h['eligible'])
        self.assertEqual(h['failed_attempts'], 5)
        self.assertEqual(h['suggested_max_concurrent'], 2)   # retries reduce parallelism
        self.assertEqual(next(iter(h['estimates'].values()))['p80_observed_tokens'], 60)

    def test_bound_zero_usage_failed_attempt_included(self):
        for i in range(5):
            self.sample(i, failed_first='bound_zero')
        h = self.history()
        self.assertTrue(h['eligible'])
        self.assertEqual(h['failed_attempts'], 5)

    def test_accepted_attempt_without_usage_still_disqualifies(self):
        for i in range(5):
            self.sample(i, accepted_usage=False)
        self.assertFalse(self.history()['eligible'])

    def test_estimates_use_counted_tokens(self):
        for i in range(5):
            self.sample(i)
        est = next(iter(self.history()['estimates'].values()))
        # input 100 (50 cached) + output 10 -> counted 60, p80*1.25 -> 75.
        self.assertEqual((est['p80_observed_tokens'], est['suggested_tokens']), (60, 75))

    def test_clean_history_does_not_clamp_throughput(self):
        for i in range(5):
            self.sample(i)
        h = self.history()
        self.assertGreaterEqual(h['suggested_max_concurrent'], 6)
        p = f.prepare(draft(efficiency={'parallel_reason': REASON}), 'throughput', history=h)
        self.assertEqual(p['efficiency']['max_concurrent'], 6)
        self.assertEqual(f.prepare(draft(), 'balanced', history=h)['efficiency']['max_concurrent'], 3)

    def test_failures_still_reduce_concurrency(self):
        for i in range(5):
            self.sample(i, quality='fail' if i < 3 else 'pass')
        self.assertEqual(self.history()['suggested_max_concurrent'], 1)

    def test_metrics_schema_two_and_counted_tokens(self):
        r = self.sample(0)
        data = report.report(self.e, r)
        for version in (1, 2):
            data['schema_version'] = version
            totals = a.metrics(data)['recorded_totals']
            self.assertEqual(totals['counted_tokens'], 120)          # (100-50)+10 per thread, two threads
            self.assertEqual(totals['total_tokens'], 220)
        data['schema_version'] = 3
        with self.assertRaises(HarnessError):
            a.metrics(data)

    def test_metrics_rejects_non_run_reports(self):
        with self.assertRaises(HarnessError):
            a.metrics({'schema_version': 2})
        with self.assertRaises(HarnessError):
            a.metrics({'schema_version': 2, 'scope': 'full_session', 'threads': []})


if __name__ == '__main__':
    unittest.main()
