"""0.5 config overlay fixes: nested arrays, risky settings, adoption, no-TOML."""
import unittest

import config_overlay as c


@unittest.skipIf(c.tomllib is None, 'needs tomllib or tomli')
class OverlayV05Tests(unittest.TestCase):
    def test_nested_array_lines_are_not_headers(self):
        original = ('model = "old"\nmatrix = [\n  [1, 2],\n  [3, 4]\n]\nnames = [\n  ["a"]\n]\n'
                    '[agents]\nenabled = false\n')
        merged, state = c.merge(original)
        data = c.parse(merged)
        self.assertEqual(data['matrix'], [[1, 2], [3, 4]])
        self.assertEqual(data['model'], 'gpt-6-astra')
        self.assertEqual(c.restore(merged, state), original)

    def test_table_header_flags(self):
        lines = ['x = [\n', '  [1, 2],\n', ']\n', '[t]\n', 's = "[not]"\n', '[[a.b]]\n']
        self.assertEqual(c.table_headers(lines), [False, False, False, True, False, True])

    def test_generated_agents_restore_with_nested_array_user_keys(self):
        merged, state = c.merge('model = "old"\n')
        merged += 'grid = [\n  [1],\n]\n'
        restored = c.restore(merged, state)
        self.assertEqual(c.parse(restored)['agents']['grid'], [[1]])

    def test_risky_settings(self):
        self.assertEqual(c.risky_settings({'approval_policy': 'never', 'sandbox_mode': 'danger-full-access'}),
                         ['approval_policy = "never"', 'sandbox_mode = "danger-full-access"'])
        self.assertEqual(c.risky_settings({'approval_policy': 'on-request'}), [])

    def test_adopt_state_rebuilds_merged_ownership(self):
        original = 'model = "mine"\n[agents]\nenabled = false\n[x]\ny = 1\n'
        merged, _ = c.merge(original)
        state = c.adopt_state(merged)
        self.assertEqual(c.restore(merged, state), original)
        with self.assertRaises(ValueError):
            c.adopt_state(merged.replace('"medium"', '"high"'))


class NoTomlTests(unittest.TestCase):
    def test_missing_parser_message(self):
        saved = c.tomllib
        c.tomllib = None
        try:
            with self.assertRaisesRegex(ValueError, r'Python 3\.11\+ \(or tomli\) is needed only to merge config\.toml'):
                c.parse('a = 1')
        finally:
            c.tomllib = saved


if __name__ == '__main__':
    unittest.main()
