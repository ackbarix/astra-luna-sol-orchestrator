import pathlib, subprocess, sys, tempfile, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
INSTALL=ROOT/"install.py"
class InstallTest(unittest.TestCase):
 def run_install(self,target,*args):
  return subprocess.run([sys.executable,str(INSTALL),"--target",str(target),*args],capture_output=True,text=True)
 def test_dry_run(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d);result=self.run_install(p,"--dry-run")
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertFalse((p/".astra-luna-sol").exists())
 def test_install_check_uninstall(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d)
   for args in [(),("--check",),(),("--uninstall",)]:
    result=self.run_install(p,*args)
    self.assertEqual(result.returncode,0,result.stderr)
   self.assertFalse((p/".astra-luna-sol/manifest.json").exists())
 def test_preserve_user_config_and_agents(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d);(p/".codex").mkdir()
   (p/".codex/config.toml").write_text("model = 'custom'\n")
   (p/"AGENTS.md").write_text("User instructions\n")
   self.assertEqual(self.run_install(p).returncode,0)
   import tomllib
   with (p/".codex/config.toml").open("rb") as f:cfg=tomllib.load(f)
   self.assertEqual(cfg["model"],"gpt-6-astra")
   self.assertEqual(cfg["agents"]["default_subagent_model"],"gpt-6-luna")
   self.assertEqual(self.run_install(p,"--uninstall").returncode,0)
   self.assertEqual((p/".codex/config.toml").read_text(),"model = 'custom'\n")
   self.assertIn("User instructions",(p/"AGENTS.md").read_text())
 def test_refuse_overwrite_modified_role(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d);self.assertEqual(self.run_install(p).returncode,0)
   role=p/".codex/agents/als_worker.toml";role.write_text("user change\n")
   self.assertEqual(self.run_install(p).returncode,2)
   self.assertEqual(self.run_install(p,"--uninstall").returncode,2)
if __name__=="__main__":unittest.main()
