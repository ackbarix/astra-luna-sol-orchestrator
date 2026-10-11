"""ALS 0.5.0 round 3: check-runtime.py from a global install. Temp dirs only; no codex/ocx calls."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('checker_r3', ROOT / 'scripts/check-runtime.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class GlobalInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.codex = self.base / 'codex'
        (self.codex / 'agents').mkdir(parents=True)
        shutil.copy(ROOT / 'profiles/config.toml', self.codex / 'als.config.toml')
        for role in (ROOT / 'profiles/agents').glob('als_*.toml'):
            shutil.copy(role, self.codex / 'agents' / role.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_global_config_used_when_project_has_none(self):
        values, problems = checker.inspect_config(self.project, codex_home=self.codex)
        self.assertEqual(problems, [])
        self.assertEqual(values['config_source'], 'global')
        self.assertEqual(values['config_path'], str(self.codex / 'als.config.toml'))

    def test_project_config_wins(self):
        (self.project / '.codex/agents').mkdir(parents=True)
        (self.project / '.codex/agents/als_worker.toml').write_text('name = "als_worker"\n')
        values, problems = checker.inspect_config(self.project, codex_home=self.codex)
        self.assertEqual(values['config_source'], 'project')
        self.assertTrue(problems)

    def test_smoke_prompt_from_global_als_home(self):
        home = self.base / 'share/als'
        home.mkdir(parents=True)
        shutil.copy(ROOT / 'scripts/check-runtime.py', home / 'check-runtime.py')
        (home / 'smoke-test.txt').write_text('SMOKE-PROMPT-TEXT\n')
        r = subprocess.run([sys.executable, str(home / 'check-runtime.py'), '--smoke-prompt'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('SMOKE-PROMPT-TEXT', r.stdout)

    def test_smoke_prompt_from_source_checkout(self):
        r = subprocess.run([sys.executable, str(ROOT / 'scripts/check-runtime.py'), '--smoke-prompt'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), (ROOT / 'docs/smoke-test.txt').read_text().strip())


if __name__ == '__main__':
    unittest.main()
