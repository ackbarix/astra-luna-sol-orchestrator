"""Installer/hook coexistence and installed entrypoint tests, no Codex calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import install

ROOT=Path(__file__).resolve().parents[1]

class HarnessInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name)/'project';self.p.mkdir()
        subprocess.run(['git','init','-q',str(self.p)],check=True)
    def tearDown(self):self.tmp.cleanup()
    def call(self,*args):return subprocess.run([sys.executable,str(ROOT/'install.py'),'--target',str(self.p),*args],capture_output=True,text=True)
    def test_hook_and_modules_installed_bare_codex_config(self):
        r=self.call();self.assertEqual(r.returncode,0,r.stderr)
        self.assertTrue((self.p/'.codex/hooks.json').exists())
        for name in ('harness.py','als_harness/state.py','als_harness/engine.py','harness-guide.md'):
            self.assertTrue((self.p/'.astra-luna-sol'/name).is_file())
        r=subprocess.run([sys.executable,str(self.p/'.astra-luna-sol/harness.py'),'doctor'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        d=json.loads(r.stdout);self.assertTrue(d['hooks_configured']);self.assertFalse(d['paid_model_calls'])
        self.assertFalse((self.p/'.astra-luna-sol/state').exists())
    def test_user_hooks_survive_upgrade_uninstall(self):
        self.p.joinpath('.codex').mkdir()
        hooks=self.p/'.codex/hooks.json';original={'description':'user','hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo hello'}]}]}}
        hooks.write_text(json.dumps(original))
        self.assertEqual(self.call().returncode,0);self.assertEqual(self.call('--check').returncode,0)
        self.assertEqual(self.call().returncode,0);self.assertEqual(self.call('--uninstall').returncode,0)
        self.assertEqual(json.loads(hooks.read_text()),original)
    def test_edited_managed_hook_refused_without_writes(self):
        self.assertEqual(self.call().returncode,0)
        p=self.p/'.codex/hooks.json';p.write_text(p.read_text().replace('ALS harness','Changed'))
        before=(self.p/'.codex/config.toml').read_bytes()
        self.assertEqual(self.call().returncode,2)
        self.assertEqual(before,(self.p/'.codex/config.toml').read_bytes())
    def test_inline_hook_conflict_or_cli_only(self):
        cfg=self.p/'.codex/config.toml';cfg.parent.mkdir();cfg.write_text('[[hooks.Stop]]\nmatcher="*"\n')
        self.assertEqual(self.call().returncode,2)
        self.assertFalse((self.p/'.astra-luna-sol').exists())
        self.assertEqual(self.call('--skip-hooks').returncode,0)
        self.assertFalse((self.p/'.codex/hooks.json').exists())
    def test_uninstall_preserves_runtime_data_and_ignore(self):
        self.assertEqual(self.call().returncode,0)
        runtime=self.p/'.astra-luna-sol/state';runtime.mkdir();(runtime/'user-note').write_text('keep')
        self.assertEqual(self.call('--uninstall').returncode,0)
        self.assertEqual((runtime/'user-note').read_text(),'keep')
        self.assertTrue((self.p/'.astra-luna-sol/.gitignore').exists())
    def test_hooks_removed_when_skip_selected(self):
        self.assertEqual(self.call().returncode,0)
        self.assertEqual(self.call('--skip-hooks').returncode,0)
        self.assertFalse((self.p/'.codex/hooks.json').exists())
        self.assertEqual(self.call('--skip-hooks','--check').returncode,0)
    def test_legacy_02_manifest_is_upgradable(self):
        self.assertEqual(self.call('--skip-hooks').returncode,0)
        mf=self.p/'.astra-luna-sol/manifest.json';m=json.loads(mf.read_text())
        m['version']='0.2.0';m.pop('hooks',None)
        new=[k for k in m['files'] if 'als_harness' in k or k.endswith(('harness.py','harness-guide.md','.gitignore'))]
        for k in new:m['files'].pop(k);(self.p/k).unlink()
        mf.write_text(json.dumps(m))
        self.assertEqual(self.call().returncode,0)
        self.assertEqual(self.call('--check').returncode,0)

if __name__=='__main__':unittest.main()
