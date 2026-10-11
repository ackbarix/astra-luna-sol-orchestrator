"""Round-3 installer/entrypoint regressions (ALS 0.5.0).

Every subprocess uses a temporary HOME/CODEX_HOME/XDG_* and ALS_BIN_DIR plus a
fake `codex`; no Codex model is ever started.
"""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v05_install_base as base  # noqa: E402

ROOT = base.ROOT
sys.path.insert(0, str(ROOT))
import config_overlay  # noqa: E402
import install  # noqa: E402
from als_harness import deps, integration  # noqa: E402

HAVE_TOML = config_overlay.tomllib is not None
FAKE_CODEX = '''#!/bin/sh
case "$1" in
 --version) echo "codex-cli 9.9.9" ;;
 features) printf 'hooks    stable  true\\nmulti_agent    stable  true\\n' ;;
 *) echo "FAKE CODEX $*" ;;
esac
'''
NO_TOML = ('import sys; sys.modules["tomllib"] = None; sys.modules["tomli"] = None; '
           'sys.argv = ["install.py", *sys.argv[1:]]; sys.path.insert(0, %r); '
           'import runpy; runpy.run_path(%r, run_name="__main__")' % (str(ROOT), str(ROOT / 'install.py')))


def history(commit):
    """`git archive` bytes of an old release, or None when history is unavailable."""
    has = subprocess.run(['git', '-C', str(ROOT), 'cat-file', '-e', commit + '^{commit}'], capture_output=True)
    if has.returncode:
        return None
    return subprocess.run(['git', '-C', str(ROOT), 'archive', commit], capture_output=True, check=True).stdout


class R3Sandbox(base.Sandbox):
    def setUp(self):
        super().setUp()
        self.bin = self.home / '.local/bin'
        self.env['ALS_BIN_DIR'] = str(self.bin)
        self.env.pop('ALS_PYTHON', None)
        self.env.pop('CDPATH', None)
        fake = self.base / 'fakebin'
        fake.mkdir()
        (fake / 'codex').write_text(FAKE_CODEX)
        (fake / 'codex').chmod(0o755)
        self.env['PATH'] = str(fake) + os.pathsep + self.env.get('PATH', '')

    def glob(self, *args):
        return self.py('--global', *args)

    def als(self, *args, cwd=None, env=None):
        return subprocess.run([str(self.bin / 'als'), *args], capture_output=True, text=True,
                              env=env or self.env, cwd=cwd or self.p)

    def extract(self, commit, name):
        data = history(commit)
        if data is None:
            self.skipTest(commit + ' not available in this checkout')
        old = self.base / name
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            if sys.version_info >= (3, 12):
                tar.extractall(old, filter='data')
            else:
                tar.extractall(old)
        return old

    def tree(self, root=None):
        root = root or self.p
        out = {}
        for path in sorted(root.rglob('*')):
            rel = path.relative_to(root)
            if '.git' in rel.parts:
                continue
            out[str(rel)] = path.read_bytes() if path.is_file() else 'DIR'
        return out

    def git_commit(self, repo):
        git = ['git', '-C', str(repo), '-c', 'user.email=t@t', '-c', 'user.name=t']
        subprocess.run(git + ['add', '-A'], check=True, capture_output=True)
        subprocess.run(git + ['commit', '-qm', 'x', '--allow-empty'], check=True, capture_output=True)


# ------------------------------------------------------------------ item 1
@unittest.skipUnless(HAVE_TOML, 'project mode needs tomllib or tomli')
class LegacyUpgradeUninstallTests(R3Sandbox, unittest.TestCase):
    def roundtrip(self, commit, prepare=None):
        old = self.extract(commit, 'als-' + commit)
        if prepare:
            prepare()
        before = self.tree()
        legacy = subprocess.run([sys.executable, str(old / 'install.py'), '--target', str(self.p)],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(legacy.returncode, 0, legacy.stdout + legacy.stderr)
        self.assertNotIn('created_dirs', self.manifest())
        self.ok(self.proj())
        self.assertTrue(self.manifest()['created_dirs'])
        self.ok(self.proj('--check'))
        self.ok(self.proj('--uninstall'))
        after = self.tree()
        if isinstance(before.get('.codex/hooks.json'), bytes):
            # 0.3/0.4 manifests kept no original bytes: JSON-equal, not byte-equal.
            for tree in (before, after):
                tree['.codex/hooks.json'] = json.loads(tree['.codex/hooks.json'])
        self.assertEqual(after, before)

    def test_02_install_upgrade_uninstall_restores_tree(self):
        self.roundtrip('5971495')

    def test_04_install_upgrade_uninstall_restores_tree(self):
        self.roundtrip('15ff2b1')

    def test_04_with_user_codex_dir_keeps_it(self):
        def prepare():
            (self.p / '.codex').mkdir()
            (self.p / '.codex/hooks.json').write_text('{"hooks": {}}\n')
            (self.p / 'AGENTS.md').write_text('# mine\n')
        self.roundtrip('15ff2b1', prepare)

    def test_seeded_dirs_are_als_only(self):
        self.extract('15ff2b1', 'als-old')
        legacy = subprocess.run([sys.executable, str(self.base / 'als-old/install.py'), '--target', str(self.p)],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.ok(self.proj())
        for rel in self.manifest()['created_dirs']:
            self.assertIn(rel, install.LEGACY_DIRS + ['.codex'])


# ------------------------------------------------------------------ item 2
class InlineHooksTests(unittest.TestCase):
    def scan(self, content):
        with mock.patch.object(config_overlay, 'tomllib', None):
            return install.inline_hooks(content)

    def test_feature_switch_is_not_inline_hooks(self):
        self.assertFalse(self.scan('[features]\nhooks = true\nmulti_agent = true\n'))
        self.assertFalse(self.scan('model = "x"\n[profiles.a]\nhooks = 1\n[hooksmith]\nx = 1\n'))
        self.assertFalse(self.scan('x = [\n  "[hooks]",\n]\n'))

    def test_root_hooks_detected(self):
        for content in ('hooks = {}\n', 'hooks.Stop = []\n', '"hooks".Stop = []\n', '[hooks]\nx = 1\n',
                        '[[hooks.Stop]]\nmatcher = "x"\n', 'a = 1\n[ hooks . PreToolUse ]\n'):
            with self.subTest(content=content):
                self.assertTrue(self.scan(content))

    @unittest.skipUnless(HAVE_TOML, 'reference parser needs tomllib or tomli')
    def test_text_scan_agrees_with_parser(self):
        for content in ('[features]\nhooks = true\n', 'hooks.Stop = []\n', '[[hooks.Stop]]\nmatcher = "x"\n',
                        'model = "m"\n'):
            with self.subTest(content=content):
                self.assertEqual(self.scan(content), install.inline_hooks(content))


class HooksOnlyWithoutTomlTests(R3Sandbox, unittest.TestCase):
    def test_hooks_only_accepts_features_hooks_without_toml(self):
        self.ok(subprocess.run([sys.executable, '-c', NO_TOML, '--global'], capture_output=True, text=True,
                               env=self.env))
        (self.p / '.codex').mkdir()
        (self.p / '.codex/config.toml').write_text('[features]\nhooks = true\n')
        result = subprocess.run([sys.executable, '-c', NO_TOML, '--target', str(self.p), '--hooks-only'],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.p / '.codex/hooks.json').is_file())


# ------------------------------------------------------------------ items 3, 5, 11, 14
class EntrypointTests(R3Sandbox, unittest.TestCase):
    def setup(self, **extra):
        env = dict(self.env, **extra)
        result = subprocess.run(['sh', str(ROOT / 'setup.sh')], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_doctor_json_is_one_object(self):
        self.setup()
        self.ok(self.als('install', '--hooks-only'))  # harness doctor may fail without hooks
        for args in (('doctor', '--no-probe', '--json'), ('doctor', '--json')):
            result = self.als(*args)
            # Exit status follows required checks (deps and harness doctor); stdout is one object.
            data = json.loads(result.stdout)
            self.assertEqual(set(data), {'deps', 'harness'})
            self.assertTrue(data['deps']['ok'])
            self.assertIn('hooks_configured', data['harness'])
        outside = self.base / 'plain'
        outside.mkdir()
        data = json.loads(self.als('doctor', '--no-probe', '--json', cwd=outside).stdout)
        self.assertIsNone(data['harness'])

    def test_doctor_json_exit_code_on_required_failure(self):
        self.setup()
        (self.base / 'fakebin/codex').write_text('#!/bin/sh\nexit 3\n')  # broken codex: required FAIL
        result = self.als('doctor', '--no-probe', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['deps']['ok'])

    def test_plan_new_names_prepare_and_start(self):
        self.setup()
        result = self.als('plan', 'new', 'p.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Edit p.json', result.stdout)
        self.assertIn('als prepare --plan p.json > prepared.json', result.stdout)
        self.assertIn('als start --plan prepared.json', result.stdout)

    def test_setup_path_note_once_and_help_text(self):
        result = self.setup()
        self.assertEqual(result.stdout.count('is not on PATH'), 1, result.stdout)
        helptext = self.als('help').stdout
        self.assertNotIn('(usage.sh)', helptext)
        self.assertIn('token_usage.py', helptext)

    def test_py39_ci_recipe_setup_with_alt_python_then_doctor_json(self):
        """Mirror of the CI step: ALS_PYTHON names the interpreter; global mode needs no TOML."""
        shim = self.base / 'pybin'
        block = self.base / 'notoml'
        shim.mkdir()
        block.mkdir()
        for name in ('tomllib', 'tomli'):
            (block / (name + '.py')).write_text('raise ModuleNotFoundError("blocked for this test")\n')
        (shim / 'altpython').write_text('#!/bin/sh\nPYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH} exec %s "$@"\n' % (
            shlex.quote(str(block)), shlex.quote(sys.executable)))
        (shim / 'altpython').chmod(0o755)
        probe = subprocess.run([str(shim / 'altpython'), '-c', 'import config_overlay as c; print(c.tomllib)'],
                               capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(probe.stdout.strip(), 'None', probe.stderr)
        env = dict(self.env, ALS_PYTHON='altpython', PATH=str(shim) + os.pathsep + self.env['PATH'])
        result = subprocess.run(['sh', str(ROOT / 'setup.sh')], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        doctor = self.als('doctor', '--no-probe', '--json', env=env)
        self.assertEqual(set(json.loads(doctor.stdout)), {'deps', 'harness'}, doctor.stderr)


# ------------------------------------------------------------------ item 4
class HooksOnlyThenProjectTests(R3Sandbox, unittest.TestCase):
    @unittest.skipUnless(HAVE_TOML, 'project mode needs tomllib or tomli')
    def test_project_install_over_hooks_only_names_working_remedy(self):
        self.ok(self.glob())
        self.ok(self.proj('--hooks-only'))
        result = self.proj()
        self.assertEqual(result.returncode, 2)
        self.assertIn('als uninstall --hooks-only', result.stderr)
        self.assertNotIn('--adopt', result.stderr)
        # Registry gone but global-form hooks still in the file: same advice.
        manifest = self.manifest(self.als_home / 'manifest.json')
        manifest['hooks_projects'] = {}
        self.write_manifest(manifest, self.als_home / 'manifest.json')
        result = self.proj()
        self.assertEqual(result.returncode, 2)
        self.assertIn('als uninstall --hooks-only', result.stderr)

    def test_global_uninstall_refusal_suggests_als_command(self):
        self.ok(self.glob())
        self.ok(self.proj('--hooks-only'))
        blocked = self.glob('--uninstall')
        self.assertEqual(blocked.returncode, 2)
        self.assertIn('cd ' + shlex.quote(str(self.p)) + ' && als uninstall --hooks-only', blocked.stderr)
        self.assertNotIn('install.sh', blocked.stderr)


# ------------------------------------------------------------------ items 6, 7
class ResolutionTests(R3Sandbox, unittest.TestCase):
    FAKE_HARNESS = 'import os, sys\nprint(os.path.realpath(__file__), *sys.argv[1:])\n'

    def submodule(self):
        inner = self.repo('inner')
        (inner / 'README').write_text('x\n')
        self.git_commit(inner)
        self.git_commit(self.p)
        subprocess.run(['git', '-C', str(self.p), '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q',
                        str(inner), 'mod'], check=True, capture_output=True, env=self.env)
        mod = self.p / 'mod'
        self.assertTrue((mod / '.git').is_file())
        return mod

    def fake_harness(self, root):
        (root / '.astra-luna-sol').mkdir()
        (root / '.astra-luna-sol/harness.py').write_text(self.FAKE_HARNESS)
        return os.path.realpath(root / '.astra-luna-sol/harness.py')

    def run_hook(self, cwd, env=None):
        return subprocess.run(['sh', '-c', integration.command('project')], cwd=cwd, input='{}',
                              capture_output=True, text=True, env=env or self.env)

    def test_project_hook_from_submodule(self):
        mod = self.submodule()
        (mod / 'deep').mkdir()
        silent = self.run_hook(mod / 'deep')
        self.assertEqual((silent.returncode, silent.stdout, silent.stderr), (0, '', ''))
        harness = self.fake_harness(mod)
        result = self.run_hook(mod / 'deep')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [harness, 'hook'])

    def test_project_hook_missing_harness_and_outside_git_exit_zero(self):
        for cwd in (self.p, self.base):
            with self.subTest(cwd=cwd):
                result = self.run_hook(cwd)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_project_hook_ignores_exported_cdpath(self):
        harness = self.fake_harness(self.p)
        decoy = self.base / 'decoy'
        (decoy / '.git').mkdir(parents=True)
        (decoy / '.astra-luna-sol').mkdir()
        (decoy / '.astra-luna-sol/harness.py').write_text('print("DECOY")\n')
        env = dict(self.env, CDPATH=str(decoy))
        result = self.run_hook(self.p, env)
        self.assertEqual(result.stdout.split(), [harness, 'hook'], result.stderr)

    def test_als_and_scripts_resolve_submodule_root(self):
        self.ok(self.glob())
        mod = self.submodule()
        (mod / 'deep').mkdir()
        harness = self.fake_harness(mod)
        status = self.als('status', cwd=mod / 'deep', env=dict(self.env, CDPATH=str(self.p)))
        self.assertEqual(status.stdout.split(), [harness, '--project', os.path.realpath(mod), 'status'],
                         status.stderr)
        # usage.sh / run.sh from a global-style location (not .astra-luna-sol)
        tools = self.base / 'tools'
        tools.mkdir()
        for name in ('usage.sh', 'run.sh'):
            (tools / name).write_bytes((ROOT / 'scripts' / name).read_bytes())
        (tools / 'token_usage.py').write_text('import sys\nprint(*sys.argv[1:])\n')
        usage = subprocess.run(['bash', str(tools / 'usage.sh'), '--list'], cwd=mod / 'deep', capture_output=True,
                               text=True, env=self.env)
        self.assertEqual(usage.stdout.split(), ['--cwd', os.path.realpath(mod), '--list'], usage.stderr)
        (self.base / 'fakebin/codex').write_text('#!/bin/sh\npwd -P\n')
        run = subprocess.run(['bash', str(tools / 'run.sh')], cwd=mod / 'deep', capture_output=True, text=True,
                             env=dict(self.env, CDPATH=str(self.p)))
        self.assertEqual(run.stdout.strip(), os.path.realpath(mod), run.stderr)


# ------------------------------------------------------------------ items 8, 9
class GlobalHookCommandTests(R3Sandbox, unittest.TestCase):
    def test_global_command_works_without_home(self):
        self.ok(self.glob())
        echo = self.base / 'echo-py'
        echo.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        echo.chmod(0o755)
        cmd = integration.home_command(self.als_home, home=self.home, python=str(echo))
        self.assertIn('${HOME:-' + str(self.home) + '}', cmd)
        unset = subprocess.run(['env', '-u', 'HOME', 'sh', '-c', cmd], capture_output=True, text=True)
        self.assertEqual(unset.stdout.split('\n')[:2], [str(self.als_home / 'harness.py'), 'hook'], unset.stderr)
        moved = subprocess.run(['sh', '-c', cmd], capture_output=True, text=True, env={'HOME': '/elsewhere'})
        self.assertEqual(moved.stdout.split('\n')[0], '/elsewhere/.local/share/als/harness.py')

    def test_unsafe_home_falls_back_to_absolute(self):
        cmd = integration.home_command('/h/a b}/als', home='/h/a b}')
        self.assertEqual(cmd, 'python3 ' + shlex.quote('/h/a b}/als/harness.py') + ' hook')

    def test_installed_global_hook_runs_with_home_unset(self):
        self.ok(self.glob())
        self.ok(self.proj('--hooks-only'))
        hooks = json.loads((self.p / '.codex/hooks.json').read_text())
        cmd = hooks['hooks']['Stop'][0]['hooks'][0]['command']
        doctor = cmd[:-len(' hook')] + ' --project ' + shlex.quote(str(self.p)) + ' doctor'
        env = {k: v for k, v in self.env.items() if k != 'HOME'}
        result = subprocess.run(['sh', '-c', doctor], cwd=self.p, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('hooks_configured', result.stdout)

    def test_hooks_only_message_names_resolved_interpreter(self):
        self.ok(self.glob())
        fake = self.base / 'nopython'
        fake.mkdir()
        git = subprocess.run(['sh', '-c', 'command -v git'], capture_output=True, text=True).stdout.strip()
        os.symlink(git, fake / 'git')
        env = dict(self.env, PATH=str(fake))
        result = subprocess.run([sys.executable, str(ROOT / 'install.py'), '--target', str(self.p), '--hooks-only'],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        me = shlex.quote(os.path.abspath(sys.executable))
        self.assertIn('HOOKS INSTALL OK: .codex/hooks.json runs ' + me + ' ', result.stdout)
        check = subprocess.run([sys.executable, str(ROOT / 'install.py'), '--target', str(self.p), '--hooks-only',
                                '--check'], capture_output=True, text=True, env=env)
        self.assertEqual(check.returncode, 0, check.stdout)
        self.assertIn('-> ' + me + ' ', check.stdout)


class Upgrade050Tests(R3Sandbox, unittest.TestCase):
    """Hook commands changed after 6f87966; installs written by it must upgrade."""

    def old(self):
        return self.extract('6f87966', 'als-6f87966')

    def test_hooks_only_from_6f87966_upgrades_and_uninstalls(self):
        old = self.old()
        before = self.tree()
        run = lambda *a: subprocess.run([sys.executable, str(old / 'install.py'), *a],  # noqa: E731
                                        capture_output=True, text=True, env=self.env)
        self.assertEqual(run('--global').returncode, 0)
        self.assertEqual(run('--target', str(self.p), '--hooks-only').returncode, 0)
        self.assertIn('"$HOME/', (self.p / '.codex/hooks.json').read_text())
        self.ok(self.glob())
        self.assertEqual(self.proj('--hooks-only', '--check').returncode, 4)
        self.ok(self.proj('--hooks-only'))
        self.assertIn('${HOME:-', (self.p / '.codex/hooks.json').read_text())
        self.ok(self.proj('--hooks-only', '--check'))
        self.ok(self.glob('--check'))
        self.ok(self.proj('--hooks-only', '--uninstall'))
        self.ok(self.glob('--uninstall'))
        self.assertEqual(self.tree(), before)

    @unittest.skipUnless(HAVE_TOML, 'project mode needs tomllib or tomli')
    def test_project_from_6f87966_upgrades_and_uninstalls(self):
        old = self.old()
        (self.p / '.codex').mkdir()
        user = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo u'}]}]}}
        (self.p / '.codex/hooks.json').write_text(json.dumps(user))
        before = self.tree()
        done = subprocess.run([sys.executable, str(old / 'install.py'), '--target', str(self.p)],
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.proj('--check').returncode, 4)
        self.ok(self.proj())
        stop = json.loads((self.p / '.codex/hooks.json').read_text())['hooks']['Stop'][-1]
        self.assertEqual(stop['hooks'][0]['command'], integration.command('project'))
        self.ok(self.proj('--check'))
        self.ok(self.proj('--uninstall'))
        self.assertEqual(json.loads((self.p / '.codex/hooks.json').read_text()), user)
        self.assertEqual(set(self.tree()), set(before))


# ------------------------------------------------------------------ item 10
class InterpreterTests(R3Sandbox, unittest.TestCase):
    def test_install_sh_floor_message_and_als_python(self):
        env = dict(self.env, ALS_PYTHON='no-such-python-als')
        for script, args in (('install.sh', ['--global']), ('setup.sh', [])):
            with self.subTest(script=script):
                result = subprocess.run(['sh', str(ROOT / script), *args], capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 2)
                self.assertIn('no-such-python-als (Python 3.9+) not found', result.stderr)
                self.assertNotIn('3.11+', result.stderr)
        self.assertFalse(self.als_home.exists())

    def test_install_sh_uses_als_python(self):
        marker = self.base / 'used'
        shim = self.base / 'shim-python'
        shim.write_text('#!/bin/sh\ntouch %s\nexec %s "$@"\n' % (shlex.quote(str(marker)),
                                                                  shlex.quote(sys.executable)))
        shim.chmod(0o755)
        result = self.sh('--global', '--dry-run', env=dict(self.env, ALS_PYTHON=str(shim)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())

    @unittest.skipUnless(HAVE_TOML, 'creating the project install needs tomllib or tomli')
    def test_check_and_uninstall_without_toml_for_created_config(self):
        self.ok(self.proj())
        run = lambda *a: subprocess.run([sys.executable, '-c', NO_TOML, '--target', str(self.p), *a],  # noqa: E731
                                        capture_output=True, text=True, env=self.env)
        check = run('--check')
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        install_again = run()
        self.assertEqual(install_again.returncode, 2)
        self.assertIn('needed only to merge config.toml', install_again.stderr)
        removed = run('--uninstall')
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(self.tree(), {})

    @unittest.skipUnless(HAVE_TOML, 'creating the project install needs tomllib or tomli')
    def test_merged_config_still_needs_toml(self):
        (self.p / '.codex').mkdir()
        (self.p / '.codex/config.toml').write_text('model = "mine"\n')
        self.ok(self.proj())
        result = subprocess.run([sys.executable, '-c', NO_TOML, '--target', str(self.p), '--uninstall'],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 2)
        self.assertIn('merged ALS keys', result.stderr)
        self.assertTrue((self.p / '.astra-luna-sol/manifest.json').exists())


# ------------------------------------------------------------------ item 12
class ProfileShapeTests(R3Sandbox, unittest.TestCase):
    """`codex --profile als` overlays $CODEX_HOME/als.config.toml with top-level keys."""

    def headers(self, text):
        lines = text.splitlines()
        return [l.strip() for l, h in zip(lines, config_overlay.table_headers(lines)) if h]

    def test_profile_file_uses_top_level_keys_and_agents_table_only(self):
        self.ok(self.glob())
        written = (self.codex_home / 'als.config.toml').read_text()
        self.assertEqual(written, (ROOT / 'profiles/config.toml').read_text())
        self.assertEqual(self.headers(written), ['[agents]'])
        code = [line for line in written.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        self.assertFalse([line for line in code if 'profiles' in line or line.lstrip().startswith('profile')])
        if HAVE_TOML:
            data = config_overlay.parse(written)
            self.assertEqual(set(data) - {'agents'},
                             {'model', 'model_reasoning_effort', 'approval_policy', 'sandbox_mode'})
            self.assertNotIn('profiles', data)
            self.assertFalse(any(isinstance(v, dict) for k, v in data.items() if k != 'agents'))
            self.assertFalse(any(isinstance(v, dict) for v in data['agents'].values()))


# ------------------------------------------------------------------ item 13
class DepsTimeoutTests(unittest.TestCase):
    def test_timeout_argument_and_env(self):
        with mock.patch.object(deps.subprocess, 'run', side_effect=deps.subprocess.TimeoutExpired('x', 1)) as run:
            self.assertEqual(deps._run(['x'])[0], None)
            deps.check(None, probe=False, timeout=1.5)
            self.assertTrue(all(c.kwargs['timeout'] == 1.5 for c in run.call_args_list[1:]))
        with mock.patch.dict(os.environ, {'ALS_DEPS_TIMEOUT': '2'}):
            self.assertEqual(deps.default_timeout(), 2.0)
        with mock.patch.dict(os.environ, {'ALS_DEPS_TIMEOUT': 'bogus'}):
            self.assertEqual(deps.default_timeout(), deps.TIMEOUT)

    def test_hung_codex_bounded_by_timeout(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            codex = Path(tmp) / 'codex'
            codex.write_text('#!/bin/sh\nexec sleep 30\n')
            codex.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': tmp + os.pathsep + os.environ.get('PATH', '')}):
                start = time.monotonic()
                items = {i['name']: i for i in deps.check(None, probe=False, timeout=1)['items']}
                self.assertLess(time.monotonic() - start, 6)
        self.assertFalse(items['codex']['ok'])


if __name__ == '__main__':
    unittest.main()
