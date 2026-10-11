"""ALS 0.5.0 round 12: user-facing hints name a command that exists."""
from pathlib import Path
import unittest
from unittest.mock import patch
from als_harness import common
from als_harness.common import HarnessError
from als_harness.engine import validate_plan

ROOT = Path(__file__).resolve().parents[1]


class HintCommandTests(unittest.TestCase):
    def test_without_als_hint_is_an_existing_harness_script(self):
        with patch('shutil.which', return_value=None):
            hint = common.hint_cmd()
        script = hint.split(' ', 1)[1].strip("'")
        self.assertTrue(Path(script).is_file(), hint)

    def test_with_als_on_path_hint_is_als(self):
        with patch('shutil.which', return_value='/usr/local/bin/als'):
            self.assertEqual(common.hint_cmd(), 'als')

    def test_unprepared_plan_error_uses_the_hint(self):
        plan = {'goal': 'g', 'tasks': [{'id': 't', 'objective': 'x'}], 'checks': [], 'criteria': []}
        with patch('shutil.which', return_value=None):
            with self.assertRaises(HarnessError) as caught:
                validate_plan(plan)
        message = str(caught.exception)
        self.assertIn('plan not prepared', message)
        self.assertNotIn('`als ', message)
        self.assertIn('harness.py prepare --plan FILE', message)


if __name__ == '__main__':
    unittest.main()
