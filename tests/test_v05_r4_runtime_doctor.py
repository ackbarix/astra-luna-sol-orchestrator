"""ALS 0.5.0 round 4: doctor/deps semantics and status text. Fake executables
and temp dirs only; never runs the host's Codex CLI or touches ~/.codex."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from als_harness import deps

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


class CodexFeatureProbeTests(TempEnv):
    def test_failed_features_list_is_unknown_not_required(self):
        with self.path_with('[ "$1" = features ] && exit 3\necho "codex-cli 9.9.9"\n'):
            result = deps.check(None)
        items = self.items(result)
        for name in ('codex-feature:multi_agent',):
            self.assertIsNone(items[name]['ok'])
            self.assertFalse(items[name]['required'])
            self.assertIn('could not run `codex features list`; verify manually', items[name]['hint'])
        self.assertTrue(items['codex']['ok'])
        self.assertTrue(result['ok'], result)

    def test_disabled_feature_is_still_reported(self):
        with self.path_with('[ "$1" = features ] && { echo "multi_agent stable false"; '
                            'exit 0; }\necho "codex-cli 9.9.9"\n'):
            items = self.items(deps.check(None))
        self.assertIs(items['codex-feature:multi_agent']['ok'], False)
        self.assertEqual(items['codex-feature:multi_agent']['found'], 'disabled')

    def test_missing_codex_is_required_failure(self):
        with patch.dict(os.environ, {'PATH': str(self.base / 'empty')}):
            result = deps.check(None)
        self.assertIs(self.items(result)['codex']['ok'], False)
        self.assertTrue(self.items(result)['codex']['required'])
        self.assertFalse(result['ok'])

    def test_timeout_is_reported_as_timeout(self):
        with self.path_with('exec sleep 5\n'):
            result = deps.check(None, timeout=0.2)
        item = self.items(result)['codex']
        self.assertIn('did not answer within 0.2s; raise ALS_DEPS_TIMEOUT', item['hint'])
        self.assertNotIn('reinstall', item['hint'])

    def test_features_timeout_is_unknown(self):
        with self.path_with('[ "$1" = features ] && exec sleep 5\necho "codex-cli 9.9.9"\n'):
            items = self.items(deps.check(None, timeout=0.3))
        self.assertIsNone(items['codex-feature:multi_agent']['ok'])
        self.assertIn('did not answer within', items['codex-feature:multi_agent']['hint'])

    def test_no_probe_runs_only_codex_version(self):
        log = self.base / 'calls.log'
        with self.path_with('echo "$@" >> "' + str(log) + '"\necho "codex-cli 9.9.9"\n'):
            deps.check(None, probe=False)
        self.assertEqual(log.read_text().split('\n')[:-1], ['--version'])

    def test_render_marks_unknown(self):
        with self.path_with('[ "$1" = features ] && exit 3\necho "codex-cli 9.9.9"\n'):
            text = deps.render(deps.check(None))
        self.assertIn('[unk ] codex-feature:multi_agent', text)
        self.assertIn('dependencies OK', text)


class AgentRoleConflictTests(TempEnv):
    def test_home_directory_is_not_its_own_conflict(self):
        home = self.base / 'home'
        (home / '.codex/agents').mkdir(parents=True)
        (home / '.codex/agents/als_worker.toml').write_text('x = 1\n')
        with patch.dict(os.environ, {'CODEX_HOME': str(home / '.codex')}):
            names = self.items({'items': deps.conflict_items(home)})
        self.assertNotIn('conflict:agent-roles', names)

    def test_real_project_shadowing_is_still_reported(self):
        home = self.base / 'home'
        (home / '.codex/agents').mkdir(parents=True)
        (home / '.codex/agents/als_worker.toml').write_text('x = 1\n')
        project = self.base / 'project'
        (project / '.codex/agents').mkdir(parents=True)
        (project / '.codex/agents/als_worker.toml').write_text('x = 1\n')
        with patch.dict(os.environ, {'CODEX_HOME': str(home / '.codex')}):
            item = self.items({'items': deps.conflict_items(project)})['conflict:agent-roles']
        self.assertFalse(item['ok'])


if __name__ == '__main__':
    unittest.main()
