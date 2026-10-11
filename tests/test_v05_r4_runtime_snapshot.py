"""ALS 0.5.0 round 4: untracked Python bytecode caches never fail verify. Temp
repos only; no Codex calls."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from als_harness import snapshot, verify
from als_harness.engine import Engine
from als_harness.state import Store

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


def fake_codex(folder, script):
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / 'codex'
    exe.write_text('#!/bin/sh\n' + script)
    exe.chmod(0o755)
    return folder


class TempEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {'ALS_STATE_HOME': str(self.base / 'state'),
                                           'CODEX_HOME': str(self.base / 'codex-home'), 'CODEX_THREAD_ID': '',
                                           'ALS_HOME': str(self.base / 'als-home')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def path_with(self, script):
        folder = fake_codex(self.base / 'bin', script)
        return patch.dict(os.environ, {'PATH': str(folder) + os.pathsep + '/usr/bin:/bin'})

    def items(self, result):
        return {i['name']: i for i in result['items']}


class BytecodeCacheTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.project = self.base / 'project'
        self.project.mkdir()
        git('init', '-q', cwd=self.project)
        (self.project / 'mod.py').write_text('x=1\n')
        git('add', 'mod.py', cwd=self.project)
        git('commit', '-q', '-m', 'init', cwd=self.project)

    def test_untracked_bytecode_ignored_tracked_kept(self):
        before = snapshot.capture(self.project)['digest']
        (self.project / '__pycache__').mkdir()
        (self.project / '__pycache__/mod.cpython-39.pyc').write_bytes(b'\0')
        (self.project / 'stray.pyc').write_bytes(b'\0')
        (self.project / 'old.pyo').write_bytes(b'\0')
        self.assertEqual(snapshot.capture(self.project)['digest'], before)
        git('add', '-f', 'stray.pyc', cwd=self.project)
        self.assertNotEqual(snapshot.capture(self.project)['digest'], before)

    def test_first_python_check_passes(self):
        (self.project / 'test_mod.py').write_text('import mod\n')
        git('add', 'test_mod.py', cwd=self.project)
        git('commit', '-q', '-m', 'test', cwd=self.project)
        s = Store(self.project, scope='global')
        try:
            e = Engine(s)
            rid = e.start({'goal': 'g', 'tasks': [], 'criteria': [], 'checks': [
                {'id': 'unit', 'argv': [sys.executable, '-c', 'import test_mod'], 'timeout': 30}]})
            out = verify.verify(e, rid, execute_commands=True)
        finally:
            s.close()
        self.assertTrue(out['passed'], out)

    def test_checks_run_without_writing_bytecode(self):
        code = 'import os,sys; sys.exit(0 if os.environ.get("PYTHONDONTWRITEBYTECODE") == "1" else 1)'
        out = verify.execute([sys.executable, '-c', code], self.project, 30)
        self.assertEqual(out['exit_code'], 0, out)


if __name__ == '__main__':
    unittest.main()
