"""Shared sandbox for the 0.5 installer tests (no test cases here).

Every subprocess gets temporary HOME, CODEX_HOME and XDG_* directories so no
test can touch the real ~/.codex or ~/.local. No Codex or model is started.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


class Sandbox:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.codex_home = self.home / '.codex'
        self.als_home = self.home / '.local/share/als'
        self.env = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.codex_home),
                        XDG_DATA_HOME=str(self.home / '.local/share'),
                        XDG_STATE_HOME=str(self.home / '.local/state'),
                        XDG_CONFIG_HOME=str(self.home / '.config'))
        self.env.pop('ALS_HOME', None)
        self.p = self.repo('project')

    def tearDown(self):
        self._tmp.cleanup()

    def repo(self, name):
        path = self.base / name
        path.mkdir()
        subprocess.run(['git', 'init', '-q', str(path)], check=True, env=self.env)
        return path

    def py(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'install.py'), *args],
                              capture_output=True, text=True, env=self.env)

    def sh(self, *args, env=None):
        return subprocess.run(['sh', str(ROOT / 'install.sh'), *args],
                              capture_output=True, text=True, env=env or self.env)

    def proj(self, *args):
        return self.py('--target', str(self.p), *args)

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def files(self, root=None):
        root = root or self.p
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
                if p.is_file() and '.git' not in p.relative_to(root).parts}

    def dirs(self, root=None):
        root = root or self.p
        return sorted(str(p.relative_to(root)) for p in root.rglob('*')
                      if p.is_dir() and '.git' not in p.relative_to(root).parts)

    def manifest(self, path=None):
        return json.loads((path or self.p / '.astra-luna-sol/manifest.json').read_text())

    def write_manifest(self, data, path=None):
        (path or self.p / '.astra-luna-sol/manifest.json').write_text(json.dumps(data))
