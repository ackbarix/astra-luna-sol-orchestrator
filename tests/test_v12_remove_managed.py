"""1.2 removed the managed-run harness: upgrading a real 1.1.0 install removes its
harness files and ALS hooks, keeps user hooks and runtime data. No Codex calls."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v05_r3_install as r3  # noqa: E402

RELEASE_110 = 'b863a1f'  # merge of ALS 1.1.0 (generic harness)
USER_HOOKS = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo user'}]}]}}


class UpgradeFrom110Tests(r3.R3Sandbox, unittest.TestCase):
    def old(self, *args):
        release = getattr(self, '_old', None) or self.extract(RELEASE_110, 'als-110')
        self._old = release
        result = subprocess.run([sys.executable, str(release / 'install.py'), *args],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_global_upgrade_removes_managed_harness(self):
        self.old('--global')
        self.assertTrue((self.als_home / 'harness.py').is_file())
        self.assertEqual(self.glob('--check').returncode, 4)
        upgraded = self.ok(self.glob())
        self.assertIn('REMOVE ' + str(self.als_home / 'harness.py'), upgraded.stdout)
        for rel in ('harness.py', 'harness-guide.md', 'efficiency-guide.md', 'als_harness/engine.py',
                    'release/scripts/harness.py'):
            self.assertFalse((self.als_home / rel).exists(), rel)
        self.assertTrue((self.als_home / 'als_flow/flow.py').is_file())
        self.ok(self.glob('--check'))
        self.assertEqual(json.loads((self.als_home / 'manifest.json').read_text())['version'],
                         (r3.ROOT / 'VERSION').read_text().strip())
        gone = self.als('status')
        self.assertEqual(gone.returncode, 2)
        self.assertIn('removed in 1.2', gone.stderr)
        self.ok(self.glob('--uninstall'))
        self.assertFalse(self.als_home.exists())

    def test_hooks_only_project_is_cleaned_up(self):
        self.old('--global')
        (self.p / '.codex').mkdir()
        (self.p / '.codex/hooks.json').write_text(json.dumps(USER_HOOKS))
        self.old('--target', str(self.p), '--hooks-only')
        self.assertIn('harness.py', (self.p / '.codex/hooks.json').read_text())
        upgraded = self.ok(self.glob())
        self.assertIn('als uninstall --hooks-only', upgraded.stdout)
        self.assertEqual(self.glob('--check').returncode, 4)
        doctor = self.als('doctor', '--no-probe')
        self.assertIn('still has ALS hooks from a release before 1.2', doctor.stdout)
        refused = self.glob('--uninstall')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('als uninstall --hooks-only', refused.stderr)
        self.ok(self.als('uninstall', '--hooks-only'))
        self.assertEqual(json.loads((self.p / '.codex/hooks.json').read_text()), USER_HOOKS)
        self.assertNotIn('before 1.2', self.als('doctor', '--no-probe').stdout)
        self.ok(self.glob('--check'))

    @unittest.skipUnless(r3.HAVE_TOML, 'project mode needs tomllib or tomli')
    def test_project_upgrade_removes_hooks_and_harness_keeps_data(self):
        (self.p / '.codex').mkdir()
        (self.p / '.codex/hooks.json').write_text(json.dumps(USER_HOOKS))
        before = self.tree()
        self.old('--target', str(self.p))
        runtime = self.p / '.astra-luna-sol'
        self.assertTrue((runtime / 'harness.py').is_file())
        (runtime / 'state').mkdir()
        (runtime / 'state/runs.sqlite').write_bytes(b'user-state')
        self.assertEqual(self.proj('--check').returncode, 4)
        self.ok(self.proj())
        self.assertEqual(json.loads((self.p / '.codex/hooks.json').read_text()), USER_HOOKS)
        for rel in ('harness.py', 'harness-guide.md', 'efficiency-guide.md', 'als_harness/engine.py'):
            self.assertFalse((runtime / rel).exists(), rel)
        self.assertTrue((runtime / 'als_flow/flow.py').is_file())
        self.assertEqual((runtime / 'state/runs.sqlite').read_bytes(), b'user-state')
        self.assertIsNone(self.manifest()['hooks'])
        self.ok(self.proj('--check'))
        self.ok(self.proj('--uninstall'))
        self.assertEqual((runtime / 'state/runs.sqlite').read_bytes(), b'user-state')
        after = {k: v for k, v in self.tree().items() if k.partition('/')[0] != '.astra-luna-sol'}  # kept data
        self.assertEqual(after, before)

    @unittest.skipUnless(r3.HAVE_TOML, 'project mode needs tomllib or tomli')
    def test_edited_retired_file_refuses_upgrade(self):
        self.old('--target', str(self.p))
        harness = self.p / '.astra-luna-sol/harness.py'
        harness.write_text(harness.read_text() + '# local edit\n')
        refused = self.proj()
        self.assertEqual(refused.returncode, 2)
        self.assertIn('.astra-luna-sol/harness.py', refused.stderr)
        self.assertTrue(harness.read_text().endswith('# local edit\n'))


if __name__ == '__main__':
    unittest.main()
