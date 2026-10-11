"""Installer/hook coexistence and installed entrypoint tests, no Codex calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
try:
    import tomllib  # noqa: F401
except ModuleNotFoundError:  # Python 3.9/3.10: installer tests need tomllib or tomli
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        raise unittest.SkipTest('needs tomllib (3.11+) or tomli')
import install

ROOT=Path(__file__).resolve().parents[1]


FAKE_CODEX = ('#!/bin/sh\nif [ "$1" = features ]; then printf "hooks stable true\\nmulti_agent stable true\\n"; '
              'else echo "codex-cli 9.9.9"; fi\n')


def fake_codex_dir(base):
    """Directory holding a fake `codex` so doctor never depends on the host's Codex CLI."""
    folder = Path(base) / 'fake-bin'
    folder.mkdir(exist_ok=True)
    exe = folder / 'codex'
    exe.write_text(FAKE_CODEX)
    exe.chmod(0o755)
    return folder

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
        # A fake codex on PATH: the result must not depend on the host's Codex install.
        env=dict(os.environ,PATH=str(fake_codex_dir(self.tmp.name))+os.pathsep+os.environ.get('PATH',''))
        r=subprocess.run([sys.executable,str(self.p/'.astra-luna-sol/harness.py'),'doctor'],capture_output=True,text=True,env=env)
        self.assertEqual(r.returncode,0,r.stderr)
        d=json.loads(r.stdout);self.assertTrue(d['hooks_configured']);self.assertFalse(d['paid_model_calls'])
        self.assertIn('codex-cli 9.9.9',[i['found'] for i in d['deps']['items'] if i['name']=='codex'])
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
    def test_reinstall_after_uninstall_retains_runtime_ignore(self):
        self.assertEqual(self.call().returncode,0)
        state=self.p/'.astra-luna-sol/state'
        state.mkdir()
        (state/'runs.sqlite').write_bytes(b'user-runtime-state')
        self.assertEqual(self.call('--uninstall').returncode,0)
        ignore=self.p/'.astra-luna-sol/.gitignore'
        self.assertTrue(ignore.is_file())
        self.assertEqual(ignore.read_bytes(),(ROOT/'profiles/runtime.gitignore').read_bytes())
        preview=self.call('--dry-run')
        self.assertEqual(preview.returncode,0,preview.stderr)
        result=self.call()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.call('--check').returncode,0)
        self.assertEqual((state/'runs.sqlite').read_bytes(),b'user-runtime-state')
        self.assertTrue(ignore.exists())
        self.assertEqual(self.call('--uninstall').returncode,0)
        self.assertEqual((state/'runs.sqlite').read_bytes(),b'user-runtime-state')
        self.assertTrue(ignore.exists())

    def test_modified_preserved_ignore_refuses_reinstall_without_writes(self):
        self.assertEqual(self.call().returncode,0)
        runtime=self.p/'.astra-luna-sol/state'
        runtime.mkdir()
        (runtime/'keep').write_text('do-not-remove')
        self.assertEqual(self.call('--uninstall').returncode,0)
        ignore=self.p/'.astra-luna-sol/.gitignore'
        ignore.write_text(ignore.read_text()+'custom-rule/\\n')
        old=ignore.read_bytes()
        for args in (('--dry-run',),()):
            failure=self.call(*args)
            self.assertEqual(failure.returncode,2)
            self.assertIn('User-owned path already exists',failure.stderr)
        self.assertFalse((self.p/'.astra-luna-sol/manifest.json').exists())
        self.assertEqual(ignore.read_bytes(),old)
        self.assertEqual((runtime/'keep').read_text(),'do-not-remove')

    def test_identical_ignore_without_runtime_is_adopted(self):
        # 0.5: a retained release-identical ignore file is adopted even after
        # the user deleted state/evidence (previously a reinstall dead end).
        target=self.p/'.astra-luna-sol'
        target.mkdir()
        (target/'.gitignore').write_bytes((ROOT/'profiles/runtime.gitignore').read_bytes())
        result=self.call('--dry-run')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((target/'manifest.json').exists())

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
