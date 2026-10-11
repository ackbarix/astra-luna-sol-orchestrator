"""Round-4 installer/entrypoint regressions (ALS 0.5.0).

Every subprocess uses a temporary HOME/CODEX_HOME/XDG_* and ALS_BIN_DIR plus a
fake `codex`; no Codex model is ever started.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v05_r3_install as r3  # noqa: E402

ROOT = r3.ROOT
integration = r3.integration
HAVE_TOML = r3.HAVE_TOML
VERSION = (ROOT / 'VERSION').read_text().strip()

# Fake harness: prints its argv; `doctor --help` lists --no-deps only when
# the NO_DEPS file exists beside it (a 0.5 harness with or without the flag).
FAKE_HARNESS = '''import os, sys
here = os.path.dirname(os.path.realpath(__file__))
if '--help' in sys.argv:
    print('usage: harness.py doctor [--no-probe]' + (' [--no-deps]' if os.path.exists(os.path.join(here, 'NO_DEPS')) else ''))
    raise SystemExit(0)
print('FAKE-HARNESS', *sys.argv[1:])
'''


def old_git_wrapper(directory):
    """`git` that mimics git < 2.13: a relative --git-common-dir is relative to
    the top level (``.git``) even from a subdirectory."""
    real = shutil.which('git')
    directory.mkdir(exist_ok=True)
    script = directory / 'git'
    script.write_text('#!/bin/sh\n'
                      'if [ "$#" -eq 2 ] && [ "$1" = rev-parse ] && [ "$2" = --git-common-dir ]; then\n'
                      '  c=$(%s rev-parse --git-common-dir) || exit $?\n'
                      '  case $c in /*) printf "%%s\\n" "$c" ;; *) printf ".git\\n" ;; esac\n'
                      '  exit 0\n'
                      'fi\n'
                      'exec %s "$@"\n' % (shlex.quote(real), shlex.quote(real)))
    script.chmod(0o755)
    return directory


class R4Sandbox(r3.R3Sandbox):
    def setup(self, env=None):
        result = subprocess.run(['sh', str(ROOT / 'setup.sh')], capture_output=True, text=True,
                                env=env or self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def fake_project_harness(self, version=VERSION, no_deps=False):
        runtime = self.p / '.astra-luna-sol'
        runtime.mkdir(exist_ok=True)
        (runtime / 'harness.py').write_text(FAKE_HARNESS)
        if version is not None:
            (runtime / 'VERSION').write_text(version + '\n')
        if no_deps:
            (runtime / 'NO_DEPS').write_text('')
        return os.path.realpath(runtime / 'harness.py')


# ------------------------------------------------------------------ item 1
class SkipHooksGuardTests(R4Sandbox, unittest.TestCase):
    @unittest.skipUnless(HAVE_TOML, 'project mode needs tomllib or tomli')
    def test_skip_hooks_project_install_refused_over_hooks_only(self):
        self.ok(self.glob())
        self.ok(self.proj('--hooks-only'))
        result = self.proj('--skip-hooks')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('als uninstall --hooks-only', result.stderr)
        self.assertFalse((self.p / '.astra-luna-sol/manifest.json').exists())
        # Registry dropped but global-form hooks still in hooks.json: refused too.
        manifest = self.manifest(self.als_home / 'manifest.json')
        manifest['hooks_projects'] = {}
        self.write_manifest(manifest, self.als_home / 'manifest.json')
        result = self.proj('--skip-hooks')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((self.p / '.astra-luna-sol/manifest.json').exists())

    @unittest.skipUnless(HAVE_TOML, 'project mode needs tomllib or tomli')
    def test_als_install_skip_hooks_after_hooks_only(self):
        self.setup()
        self.ok(self.als('install', '--hooks-only'))
        result = self.als('install', '--skip-hooks')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn('INSTALL OK', result.stdout)
        self.ok(self.als('uninstall', '--hooks-only'))
        self.ok(self.als('install', '--skip-hooks'))


# ------------------------------------------------------------------ item 2
class PlanTemplateTests(R4Sandbox, unittest.TestCase):
    def test_template_check_writes_no_bytecode(self):
        self.setup()
        self.ok(self.als('plan', 'new', 'p.json'))
        plan = json.loads((self.p / 'p.json').read_text())
        argv = plan['checks'][0]['argv']
        self.assertEqual(argv[:2], ['python3', '-B'])
        tests = self.p / 'tests'
        tests.mkdir()
        (tests / 'helper.py').write_text('X = 1\n')
        (tests / 'test_x.py').write_text('import unittest\nimport helper\n\n'
                                         'class T(unittest.TestCase):\n    def test(self):\n'
                                         '        self.assertEqual(helper.X, 1)\n')
        env = {k: v for k, v in self.env.items() if k != 'PYTHONDONTWRITEBYTECODE'}
        argv = [sys.executable if argv[0] == 'python3' else argv[0], *argv[1:]]
        result = subprocess.run(argv, cwd=self.p, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.p.rglob('__pycache__')), [])


# ------------------------------------------------------------------ item 3
class DoctorOutdatedProjectTests(R4Sandbox, unittest.TestCase):
    @unittest.skipUnless(HAVE_TOML, 'project mode needs tomllib or tomli')
    def test_doctor_against_04_project_install(self):
        old = self.extract('15ff2b1', 'als-04')
        legacy = subprocess.run([sys.executable, str(old / 'install.py'), '--target', str(self.p)],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(legacy.returncode, 0, legacy.stdout + legacy.stderr)
        version = (self.p / '.astra-luna-sol/VERSION').read_text().strip()
        self.assertTrue(version.startswith('0.4'), version)
        self.setup()
        text = self.als('doctor', '--no-probe')
        self.assertEqual(text.returncode, 0, text.stdout + text.stderr)
        self.assertIn('project install ' + version + ' is outdated: run `als install` (upgrade) or '
                      '`als uninstall`', text.stdout)
        as_json = self.als('doctor', '--no-probe', '--json')
        self.assertEqual(as_json.returncode, 0, as_json.stdout + as_json.stderr)
        data = json.loads(as_json.stdout)
        self.assertEqual(set(data), {'deps', 'harness'})
        self.assertEqual(data['harness']['outdated_project_install'], version)
        # The README upgrade path then works.
        self.ok(self.als('install'))
        self.assertNotIn('is outdated', self.als('doctor', '--no-probe').stdout)

    def test_missing_version_file_counts_as_outdated(self):
        self.setup()
        self.fake_project_harness(version=None)
        result = self.als('doctor', '--no-probe')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('is outdated', result.stdout)
        self.assertNotIn('FAKE-HARNESS', result.stdout)

    def test_no_deps_passed_only_when_harness_supports_it(self):
        self.setup()
        self.fake_project_harness(no_deps=False)
        out = self.als('doctor', '--no-probe').stdout
        line = [x for x in out.splitlines() if x.startswith('FAKE-HARNESS')][0]
        self.assertIn('doctor', line)
        self.assertNotIn('--no-deps', line)
        (self.p / '.astra-luna-sol/NO_DEPS').write_text('')
        for args in (('doctor', '--no-probe'), ('doctor', '--no-probe', '--json')):
            with self.subTest(args=args):
                out = self.als(*args).stdout
                self.assertIn('--no-deps', out)

    def test_real_harness_doctor_omits_deps_when_supported(self):
        self.setup()
        self.ok(self.als('install', '--hooks-only'))
        helptext = subprocess.run([sys.executable, str(ROOT / 'scripts/harness.py'), 'doctor', '--help'],
                                  capture_output=True, text=True).stdout
        if '--no-deps' not in helptext:
            self.skipTest('harness doctor --no-deps not available yet (runtime worker)')
        out = self.als('doctor', '--no-probe').stdout
        self.assertIn('\nHarness (', out)
        # bin/als already printed the dependency report once (bin dir not on PATH: one warning).
        self.assertEqual(out.count('als-on-path'), 1, out)


# ------------------------------------------------------------------ item 4
class RememberedDefaultsTests(R4Sandbox, unittest.TestCase):
    def test_custom_als_home_and_python_survive_unset(self):
        shim_dir = self.base / 'pyshim'
        shim_dir.mkdir()
        marker = self.base / 'shim-used'
        shim = shim_dir / 'alspy'
        shim.write_text('#!/bin/sh\ntouch %s\nexec %s "$@"\n' % (shlex.quote(str(marker)),
                                                                  shlex.quote(sys.executable)))
        shim.chmod(0o755)
        custom = self.base / 'custom-als'
        env = dict(self.env, ALS_HOME=str(custom), ALS_PYTHON='alspy',
                   PATH=str(shim_dir) + os.pathsep + self.env['PATH'])
        self.setup(env)
        self.assertTrue((custom / 'harness.py').is_file())
        self.assertFalse(self.als_home.exists())
        bare = {k: v for k, v in env.items() if k not in ('ALS_HOME', 'ALS_PYTHON')}
        marker.unlink()
        version = self.als('version', env=bare)
        self.assertEqual((version.returncode, version.stdout.strip()), (0, VERSION), version.stderr)
        status = self.als('status', env=bare)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertTrue(marker.exists(), 'configured interpreter not used')
        self.ok(self.als('install', '--global', '--check', env=bare))
        self.ok(self.als('install', '--hooks-only', env=bare))
        self.assertIn(str(custom), (self.p / '.codex/hooks.json').read_text())
        self.ok(self.als('uninstall', '--hooks-only', env=bare))
        # The environment still overrides the remembered defaults.
        other = dict(bare, ALS_HOME=str(self.base / 'elsewhere'))
        self.assertIn('VERSION not found', self.als('version', env=other).stderr)
        self.ok(self.als('uninstall', '--global', env=bare))
        self.assertFalse(custom.exists())

    def test_missing_interpreter_error_names_it(self):
        shim_dir = self.base / 'pyshim'
        shim_dir.mkdir()
        shim = shim_dir / 'alspy'
        shim.write_text('#!/bin/sh\nexec %s "$@"\n' % shlex.quote(sys.executable))
        shim.chmod(0o755)
        env = dict(self.env, ALS_PYTHON=str(shim))
        self.setup(env)
        shim.unlink()
        result = self.als('status')
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(shim), result.stderr)
        self.assertNotIn('python3 (3.9+)', result.stderr)

    def test_default_install_records_default_home(self):
        self.setup()
        installed = (self.bin / 'als').read_text()
        self.assertIn('ALS_DEFAULT_HOME=' + shlex.quote(str(self.als_home)), installed)
        self.assertIn('ALS_DEFAULT_PYTHON=\n', installed)
        self.ok(self.glob('--check'))


# ------------------------------------------------------------------ item 5
class UsageDefaultTests(R4Sandbox, unittest.TestCase):
    TOKEN_USAGE = 'import sys\nprint("TU", *sys.argv[1:])\n'

    def test_als_usage_defaults_to_latest(self):
        self.setup()
        runtime = self.p / '.astra-luna-sol'
        runtime.mkdir()
        (runtime / 'token_usage.py').write_text(self.TOKEN_USAGE)
        cases = {(): True, ('--limit', '5'): True, ('--list',): False, ('--root', 'abc'): False,
                 ('--root=abc',): False, ('--day', '2026-01-01'): False, ('--latest-with-workers',): False,
                 ('--latest',): None, ('--help',): False}
        for args, adds in cases.items():
            with self.subTest(args=args):
                result = self.als('usage', *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                words = result.stdout.split()
                if adds is True:
                    self.assertEqual(words[-1], '--latest')
                elif adds is False:
                    self.assertNotIn('--latest', words)
                else:
                    self.assertEqual(words.count('--latest'), 1)

    def test_usage_sh_defaults_to_latest(self):
        tools = self.base / 'tools'
        tools.mkdir()
        (tools / 'usage.sh').write_bytes((ROOT / 'scripts/usage.sh').read_bytes())
        (tools / 'token_usage.py').write_text(self.TOKEN_USAGE)
        result = subprocess.run(['bash', str(tools / 'usage.sh')], cwd=self.p, capture_output=True, text=True,
                                env=self.env)
        self.assertEqual(result.stdout.split()[-1], '--latest', result.stderr)
        result = subprocess.run(['bash', str(tools / 'usage.sh'), '--list'], cwd=self.p, capture_output=True,
                                text=True, env=self.env)
        self.assertNotIn('--latest', result.stdout.split())

    def test_real_token_usage_no_args_is_not_an_argparse_error(self):
        self.setup()
        (self.codex_home / 'sessions').mkdir(parents=True, exist_ok=True)
        result = self.als('usage')
        # No sessions in the sandbox: "No matching sessions" (1), never the argparse error (2).
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn('one of the arguments', result.stderr)


# ------------------------------------------------------------------ item 6
class OldGitTests(R4Sandbox, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.oldgit = dict(self.env, PATH=str(old_git_wrapper(self.base / 'oldgit')) + os.pathsep
                           + self.env['PATH'])
        self.deep = self.p / 'a' / 'b'
        self.deep.mkdir(parents=True)

    def test_wrapper_mimics_old_git(self):
        out = subprocess.run(['git', 'rev-parse', '--git-common-dir'], cwd=self.deep, capture_output=True,
                             text=True, env=self.oldgit).stdout.strip()
        self.assertEqual(out, '.git')

    def test_project_hook_from_subdirectory_with_old_git(self):
        harness = self.fake_project_harness()
        for cwd in (self.p, self.deep):
            with self.subTest(cwd=cwd):
                result = subprocess.run(['sh', '-c', integration.command('project')], cwd=cwd, input='{}',
                                        capture_output=True, text=True, env=self.oldgit)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.split(), ['FAKE-HARNESS', 'hook'])
        self.assertTrue(harness)

    def test_project_hook_from_linked_worktree_subdirectory(self):
        harness = self.fake_project_harness()
        self.git_commit(self.p)
        wt = self.base / 'wt'
        subprocess.run(['git', '-C', str(self.p), 'worktree', 'add', '-q', str(wt)], check=True,
                       capture_output=True, env=self.env)
        (wt / 'sub').mkdir(exist_ok=True)
        result = subprocess.run(['sh', '-c', integration.command('project')], cwd=wt / 'sub', input='{}',
                                capture_output=True, text=True, env=self.oldgit)
        self.assertEqual(result.stdout.split(), ['FAKE-HARNESS', 'hook'], result.stderr)
        self.assertTrue(harness)

    def test_als_and_scripts_from_subdirectory_with_old_git(self):
        self.setup()
        self.fake_project_harness()
        status = self.als('status', cwd=self.deep, env=self.oldgit)
        self.assertEqual(status.stdout.split(), ['FAKE-HARNESS', '--project', os.path.realpath(self.p), 'status'],
                         status.stderr)
        tools = self.base / 'tools'
        tools.mkdir()
        for name in ('usage.sh', 'run.sh'):
            (tools / name).write_bytes((ROOT / 'scripts' / name).read_bytes())
        (tools / 'token_usage.py').write_text('import sys\nprint(*sys.argv[1:])\n')
        usage = subprocess.run(['bash', str(tools / 'usage.sh'), '--list'], cwd=self.deep, capture_output=True,
                               text=True, env=self.oldgit)
        self.assertEqual(usage.stdout.split(), ['--cwd', os.path.realpath(self.p), '--list'], usage.stderr)
        (self.base / 'fakebin/codex').write_text('#!/bin/sh\npwd -P\n')
        run = subprocess.run(['bash', str(tools / 'run.sh')], cwd=self.deep, capture_output=True, text=True,
                             env=self.oldgit)
        self.assertEqual(run.stdout.strip(), os.path.realpath(self.p), run.stderr)

    @unittest.skipUnless(HAVE_TOML, 'project mode needs tomllib or tomli')
    def test_upgrade_from_5ad6eb6_hook_command(self):
        old = self.extract('5ad6eb6', 'als-5ad6eb6')
        (self.p / '.codex').mkdir()
        user = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo u'}]}]}}
        (self.p / '.codex/hooks.json').write_text(json.dumps(user))
        done = subprocess.run([sys.executable, str(old / 'install.py'), '--target', str(self.p)],
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        old_cmd = json.loads((self.p / '.codex/hooks.json').read_text())['hooks']['Stop'][-1]['hooks'][0]['command']
        self.assertNotEqual(old_cmd, integration.command('project'))
        self.assertTrue(integration.ALS_COMMAND.search(old_cmd))
        self.assertEqual(self.proj('--check').returncode, 4)
        self.ok(self.proj())
        hooks = json.loads((self.p / '.codex/hooks.json').read_text())['hooks']
        commands = [h['command'] for groups in hooks.values() for g in groups for h in g['hooks']]
        self.assertNotIn(old_cmd, commands)
        self.assertEqual(hooks['Stop'][-1]['hooks'][0]['command'], integration.command('project'))
        self.ok(self.proj('--check'))
        self.ok(self.proj('--uninstall'))
        self.assertEqual(json.loads((self.p / '.codex/hooks.json').read_text()), user)



# ------------------------------------------------------------------ item 7
class DocsTests(unittest.TestCase):
    def test_hook_command_forms_documented(self):
        guide = (ROOT / 'docs/harness.md').read_text()
        readme = (ROOT / 'README.md').read_text()
        self.assertIn(integration.command('project'), guide)
        for text in (guide, readme):
            self.assertIn('python3 "${HOME:-/home/me}/.local/share/als/harness.py" hook', text)
            self.assertNotIn('python3 "$HOME/.local/share/als/harness.py" hook', text)
        for name in ('ALS_DEPS_TIMEOUT', 'ALS_PYTHON', 'ALS_HOME', 'outdated_project_install'):
            self.assertIn(name, readme)


if __name__ == '__main__':
    unittest.main()
