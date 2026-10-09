import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import install

ROOT = Path(__file__).resolve().parents[1]

class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
    def tearDown(self): self.tmp.cleanup()
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT/'install.py'), '--target', str(self.project), *args], capture_output=True, text=True)
    def ok(self, *args):
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result
    def snapshot(self):
        return {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
    def test_dry_run_zero_writes(self):
        self.ok('--dry-run')
        self.assertEqual(list(self.project.iterdir()), [])
    def test_install_reinstall_check_uninstall(self):
        self.ok()
        first = self.snapshot()
        self.ok('--check'); self.ok()
        self.assertEqual(self.snapshot(), first)
        self.ok('--uninstall')
        self.assertEqual(self.snapshot(), {})
    def test_existing_config_array_and_permission_preservation(self):
        cfg = self.project/'.codex/config.toml'; cfg.parent.mkdir()
        original = b'model="custom"\n[agents]\nenabled=false\n[[skills.config]]\nenabled=false\npath="/x"\n'
        cfg.write_bytes(original); cfg.chmod(0o600)
        self.ok(); self.ok('--check')
        self.assertEqual(tomllib.loads(cfg.read_text())['model'], 'gpt-6-astra')
        self.assertIs(tomllib.loads(cfg.read_text())['skills']['config'][0]['enabled'], False)
        self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
        self.ok('--uninstall')
        self.assertEqual(cfg.read_bytes(), original)
    def test_agents_exact_restoration(self):
        for data in (b'', b'# rule', b'# rule\n', b'# rule\n\n', b'# rule\r\n'):
            with self.subTest(data=data):
                (self.project/'AGENTS.md').write_bytes(data)
                self.ok(); self.ok('--uninstall')
                self.assertEqual((self.project/'AGENTS.md').read_bytes(), data)
    def test_agents_unrelated_edits_preserved(self):
        p = self.project/'AGENTS.md'; p.write_text('Original\n')
        self.ok(); p.write_text(p.read_text() + '\nNew rule\n')
        self.ok('--uninstall')
        self.assertEqual(p.read_text(), 'Original\n\nNew rule\n')
    def test_modified_role_blocked_before_any_write(self):
        self.ok(); p = self.project/'.codex/agents/als_worker.toml'; p.write_text('edited')
        before = self.snapshot()
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertEqual(self.run_cli('--uninstall').returncode, 2)
        self.assertEqual(before, self.snapshot())
    def test_bad_config_zero_writes(self):
        p = self.project/'.codex/config.toml'; p.parent.mkdir(); p.write_text('invalid=')
        before = self.snapshot()
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertEqual(before, self.snapshot())
    def test_symlinks_blocked(self):
        outside = Path(self.tmp.name)/'outside'; outside.mkdir()
        (self.project/'.codex').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertEqual(list(outside.iterdir()), [])
    def test_manifest_path_traversal_blocked(self):
        self.ok()
        mf = self.project/'.astra-luna-sol/manifest.json'; data = json.loads(mf.read_text())
        data['files']['../outside'] = 'bogus'; mf.write_text(json.dumps(data))
        before = self.snapshot()
        self.assertEqual(self.run_cli('--uninstall').returncode, 2)
        self.assertEqual(before, self.snapshot())
    def test_legacy_created_manifest_upgrade(self):
        self.ok(); mf = self.project/'.astra-luna-sol/manifest.json'
        data = json.loads(mf.read_text()); data.pop('version')
        data['files']['.codex/config.toml'] = data.pop('config')['sha256']
        role = self.project/'.codex/agents/als_explorer.toml'
        role.write_text(role.read_text().replace('model_reasoning_effort = "medium"','model_reasoning_effort = "max"'))
        data['files']['.codex/agents/als_explorer.toml'] = hashlib.sha256(role.read_bytes()).hexdigest()
        mf.write_text(json.dumps(data))
        self.ok(); self.ok('--check')
        self.assertEqual(tomllib.loads(role.read_text())['model_reasoning_effort'], 'medium')
    def test_usage_script_and_license_installed(self):
        self.ok()
        for p in ('token_usage.py','usage.sh','check-runtime.py','smoke-test.txt','licenses/Apache-2.0.txt'):
            self.assertTrue((self.project/'.astra-luna-sol'/p).is_file())
    def test_local_checker(self):
        self.ok()
        result = subprocess.run([sys.executable, str(self.project/'.astra-luna-sol/check-runtime.py')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['live_workers_executed_by_this_check'])
    def test_runtime_dry_run_no_model_calls(self):
        self.ok()
        result = subprocess.run(['bash', str(self.project/'.astra-luna-sol/run.sh'), '--dry-run', '--configure-ocx'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('gpt-6-astra', result.stdout)
    def test_rollback_on_exception(self):
        p1, p2 = self.project/'a', self.project/'b'
        p1.write_bytes(b'old'); p2.write_bytes(b'old')
        original = install.atomic_write
        def failing(path, data, mode=0o644):
            if path.name == 'b' and data == b'new': raise OSError('simulated disk failure')
            original(path, data, mode)
        with patch.object(install, 'atomic_write', side_effect=failing):
            with self.assertRaises(OSError): install.commit(self.project, {'a':b'new','b':b'new'}, {'a':(b'old',0o644),'b':(b'old',0o644)})
        self.assertEqual(p1.read_bytes(), b'old'); self.assertEqual(p2.read_bytes(), b'old')
    def test_concurrent_drift_refused(self):
        p=self.project/'a'; p.write_bytes(b'changed')
        with self.assertRaises(ValueError): install.commit(self.project, {'a':b'new'}, {'a':(b'old',0o644)})
        self.assertEqual(p.read_bytes(),b'changed')
    def test_uninstalled_check_fails(self):
        self.assertEqual(self.run_cli('--check').returncode, 1)
