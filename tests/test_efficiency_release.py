"""Release version and total-dispatch boundaries; no model calls."""
from pathlib import Path
import unittest
import install
import als_harness
from test_efficiency import EngineFixture, draft, task
from als_harness import verify

class ReleasePolicyTests(EngineFixture, unittest.TestCase):
    def test_release_versions_agree(self):
        version=(Path(__file__).resolve().parents[1]/'VERSION').read_text().strip()
        self.assertEqual(version,'0.4.0')
        self.assertEqual(install.VERSION,version)
        self.assertEqual(als_harness.__version__,version)

    def test_total_spawn_limit_applies_to_retries(self):
        r=self.start(draft(efficiency={'max_total_spawns':1}))
        self.assertTrue(self.e.reserve(r,'scan','first')['allowed'])
        self.e.finish(r,'scan','Confirmed test-only stopped failure',failed=True,stopped=True)
        self.e.retry(r,'scan','Use a different focused approach')
        outcome=self.e.reserve(r,'scan','second')
        self.assertFalse(outcome['allowed'])
        self.assertIn('optimized total spawn allowance exhausted',outcome['reasons'])
        self.assertEqual(len(self.s.task(r,'scan')['attempts']),1)

    def test_head_work_does_not_consume_child_allowance(self):
        tasks=[task('direct',complexity='trivial'),task('worker')]
        r=self.start(draft(tasks,efficiency={'max_total_spawns':1}))
        self.assertTrue(self.e.begin_head(r,'direct')['allowed'])
        self.e.finish(r,'direct','Direct work completed')
        self.assertTrue(verify.verify(self.e,r,execute_commands=True)['passed'])
        self.e.accept(r,'direct')
        self.assertTrue(self.e.reserve(r,'worker','only-child')['allowed'])

if __name__=='__main__':unittest.main()
