"""Release versions agree; no model calls."""
from pathlib import Path
import unittest
import install
import als_harness


class ReleasePolicyTests(unittest.TestCase):
    def test_release_versions_agree(self):
        version = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()
        self.assertEqual(version, '1.2.0')
        self.assertEqual(install.VERSION, version)
        self.assertEqual(als_harness.__version__, version)


if __name__ == '__main__':
    unittest.main()
