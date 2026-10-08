import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import config_overlay

class OverlayTests(unittest.TestCase):
    def test_restore_existing_keys_byte_for_byte(self):
        original = '# user settings\nmodel="old" # keep\napproval_policy="never"\n\n[agents]\nmax_concurrent_threads_per_session=2\nnotes="keep"\n'
        content,state=config_overlay.merge(original)
        parsed=tomllib.loads(content)
        self.assertEqual(parsed["model"],"gpt-6-astra")
        self.assertEqual(parsed["agents"]["notes"],"keep")
        self.assertEqual(parsed["agents"]["max_concurrent_threads_per_session"],6)
        self.assertEqual(config_overlay.restore(content,state),original)

    def test_generated_agents_table_restores_original(self):
        for original in ("", "model='old'\n", "[features]\nfoo=true\n"):
            content,state=config_overlay.merge(original)
            self.assertEqual(config_overlay.restore(content,state),original)

    def test_unrelated_user_edits_survive(self):
        content,state=config_overlay.merge('model="old"\n')
        content+='\n[features]\nuser_feature=true\n'
        self.assertIn("user_feature=true",config_overlay.restore(content,state))

    def test_edited_block_refuses_overwrite(self):
        content,state=config_overlay.merge('')
        with self.assertRaises(ValueError):
            config_overlay.restore(content.replace('gpt-6-astra','not-astra'),state)

    def test_complex_config_refuses_unsafe_merge(self):
        for bad in ("agents={enabled=false}\n","[agents.misc]\na=1\n",'message="""a\nb"""\n'):
            with self.assertRaises(ValueError):config_overlay.merge(bad)

    def test_legacy_owned_config_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)
            def invoke(*flags):
                return subprocess.run([sys.executable,str(ROOT/"install.py"),"--target",tmp,*flags],capture_output=True,text=True)
            self.assertEqual(invoke().returncode,0)
            mf=target/".astra-luna-sol/manifest.json"
            manifest=json.loads(mf.read_text())
            original=manifest.pop("config")
            manifest["files"][".codex/config.toml"]=original["sha256"]
            mf.write_text(json.dumps(manifest))
            self.assertEqual(invoke().returncode,0)
            self.assertEqual(invoke("--check").returncode,0)
            self.assertEqual(invoke("--uninstall").returncode,0)
            self.assertFalse((target/".codex/config.toml").exists())
