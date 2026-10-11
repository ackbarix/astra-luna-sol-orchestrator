"""ALS 0.5.0 round 3: broad token_usage selections count unattributable rollouts."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v05_policy_token_usage import Fixture, context, record  # noqa: E402


class BroadSelectionTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.put(events=[context(), record()])
        self.raw('rollout-garbage.jsonl', '{"bad":\n')  # cannot be tied to a session

    def test_day_without_cwd_is_incomplete(self):
        report = self.report('--day', '2026-10-08', '--strict', code=3)
        self.assertFalse(report['complete'])
        self.assertTrue(any(w.startswith('missing_session_meta:') for w in report['warnings']), report['warnings'])

    def test_day_with_cwd_stays_complete(self):
        report = self.report('--day', '2026-10-08', '--cwd', '/proj', '--strict')
        self.assertTrue(report['complete'], report['warnings'])
        self.assertIn('missing_session_meta', ' '.join(report['index_warnings']))

    def test_root_selection_stays_complete(self):
        self.assertTrue(self.report('--root', 'root', '--strict')['complete'])

    def test_missing_sessions_dir_names_path(self):
        missing = self.home / 'nowhere'
        r = self.cli('--sessions-dir', str(missing), '--list')
        self.assertEqual(r.returncode, 2)
        self.assertIn(str(missing), r.stderr)


if __name__ == '__main__':
    unittest.main()
