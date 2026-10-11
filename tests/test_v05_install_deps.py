"""als_harness.deps: dependency report with fake tools; never calls a model."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from als_harness import deps


class DepsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        env = {'HOME': str(self.home), 'CODEX_HOME': str(self.home / '.codex'),
               'XDG_DATA_HOME': str(self.home / '.local/share'), 'PATH': str(self.bin)}
        self.patch = mock.patch.dict(os.environ, env, clear=False)
        self.patch.start()
        os.environ.pop('ALS_HOME', None)
        os.environ.pop('ALS_BIN_DIR', None)
        os.environ.pop('ALS_DEPS_TIMEOUT', None)
        git = '/usr/bin/git' if os.path.exists('/usr/bin/git') else None
        if git:
            os.symlink(git, self.bin / 'git')

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def tool(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)

    def items(self, result):
        return {i['name']: i for i in result['items']}

    def test_missing_codex_is_required_failure(self):
        result = deps.check(None, probe=True)
        items = self.items(result)
        self.assertFalse(result['ok'])
        self.assertFalse(items['codex']['ok'])
        self.assertIn('npm', items['codex']['hint'])
        self.assertFalse(items['ocx']['required'])
        for item in result['items']:
            self.assertEqual(set(item), {'name', 'found', 'required', 'ok', 'hint'})

    def test_codex_features_probe(self):
        self.tool('codex', 'case "$1" in --version) echo codex-cli 1.2.3;; '
                           'features) printf "hooks  stable  false\\nmulti_agent  stable  true\\n";; esac\n')
        items = self.items(deps.check(None, probe=True))
        self.assertEqual(items['codex']['found'], 'codex-cli 1.2.3')
        self.assertFalse(items['codex-feature:hooks']['ok'])
        self.assertIn('codex features enable hooks', items['codex-feature:hooks']['hint'])
        self.assertTrue(items['codex-feature:multi_agent']['ok'])
        no_probe = self.items(deps.check(None, probe=False))
        self.assertNotIn('codex-feature:hooks', no_probe)

    def test_bin_dir_on_path_and_conflicts(self):
        os.environ['PATH'] += os.pathsep + str(self.home / '.local/bin')
        project = self.base / 'p'
        (project / '.codex/agents').mkdir(parents=True)
        (project / '.codex/agents/als_worker.toml').write_text('x')
        (project / '.astra-luna-sol').mkdir()
        (project / '.astra-luna-sol/manifest.json').write_text('{"version": "0.4.0"}')
        (self.home / '.codex/agents').mkdir(parents=True)
        (self.home / '.codex/agents/als_worker.toml').write_text('x')
        (self.home / '.local/share/als').mkdir(parents=True)
        (self.home / '.local/share/als/manifest.json').write_text('{"version": "0.5.0"}')
        items = self.items(deps.check(project, probe=False))
        self.assertTrue(items['als-on-path']['ok'])
        self.assertFalse(items['conflict:agent-roles']['ok'])
        self.assertIn('als_worker.toml', items['conflict:agent-roles']['found'])
        self.assertFalse(items['conflict:versions']['ok'])

    def test_render_and_timeout_handling(self):
        self.tool('codex', 'exec /bin/sleep 30\n')
        items = self.items(deps.check(None, probe=True, timeout=1))
        self.assertFalse(items['codex']['ok'])
        os.environ['ALS_DEPS_TIMEOUT'] = '1'
        self.assertIn('[FAIL] codex', deps.render(deps.check(None, probe=False)))


if __name__ == '__main__':
    unittest.main()
