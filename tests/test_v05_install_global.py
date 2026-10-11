"""0.5 global mode, per-project --hooks-only, the `als` entry point and setup.sh.

All runs use temporary HOME/CODEX_HOME/XDG dirs and a fake `codex` on PATH.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v05_install_base as base  # noqa: E402

ROOT = base.ROOT
FAKE_CODEX = '''#!/bin/sh
case "$1" in
 --version) echo "codex-cli 9.9.9" ;;
 features) printf 'hooks    stable  %s\\nmulti_agent    stable  true\\n' "${FAKE_HOOKS:-true}" ;;
 *) echo "FAKE CODEX $*" ;;
esac
'''


class GlobalTests(base.Sandbox, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.bin = self.home / '.local/bin'
        fake = self.base / 'fakebin'
        fake.mkdir()
        (fake / 'codex').write_text(FAKE_CODEX)
        (fake / 'codex').chmod(0o755)
        self.env['PATH'] = str(fake) + os.pathsep + self.env.get('PATH', '')

    def glob(self, *args):
        return self.py('--global', *args)

    def als(self, *args, cwd=None):
        return subprocess.run([str(self.bin / 'als'), *args], capture_output=True, text=True,
                              env=self.env, cwd=cwd or self.p)

    def test_global_install_layout_check_uninstall(self):
        (self.codex_home).mkdir()
        user_config = b'model = "user-choice"\n'
        (self.codex_home / 'config.toml').write_bytes(user_config)
        self.ok(self.glob('--dry-run'))
        self.assertFalse(self.als_home.exists())
        result = self.ok(self.glob())
        self.assertIn('not on PATH', result.stdout)
        for rel in ('harness.py', 'als_harness/deps.py', 'token_usage.py', 'usage.sh', 'run.sh',
                    'check-runtime.py', 'smoke-test.txt', 'VERSION', 'licenses/Apache-2.0.txt',
                    'harness-guide.md', 'efficiency-guide.md', 'manifest.json', 'release/install.py',
                    'release/bin/als', 'release/profiles/config.toml'):
            self.assertTrue((self.als_home / rel).is_file(), rel)
        for rel in ('agents/als_worker.toml', 'agents/als_reviewer_deep.toml', 'skills/astra-luna-sol/SKILL.md'):
            self.assertTrue((self.codex_home / rel).is_file(), rel)
        self.assertEqual((self.codex_home / 'als.config.toml').read_bytes(),
                         (ROOT / 'profiles/config.toml').read_bytes())
        self.assertEqual((self.codex_home / 'config.toml').read_bytes(), user_config)
        self.assertFalse((self.codex_home / 'hooks.json').exists())
        self.assertTrue(os.access(self.bin / 'als', os.X_OK))
        self.ok(self.glob('--check'))
        self.ok(self.glob())  # idempotent
        self.ok(self.glob('--uninstall'))
        self.assertEqual(sorted(p.name for p in self.codex_home.iterdir()), ['config.toml'])
        self.assertFalse(self.als_home.exists())
        self.assertFalse(self.bin.exists())
        self.assertFalse((self.home / '.local').exists())

    def test_user_owned_profile_refused(self):
        self.codex_home.mkdir()
        (self.codex_home / 'als.config.toml').write_text('model = "mine"\n')
        result = self.glob()
        self.assertEqual(result.returncode, 2)
        self.assertIn('User-owned path already exists', result.stderr)
        self.assertFalse(self.als_home.exists())

    def test_global_drift_force_uninstall_and_adopt(self):
        self.ok(self.glob())
        role = self.codex_home / 'agents/als_worker.toml'
        role.write_text('mine')
        check = self.glob('--check')
        self.assertEqual(check.returncode, 2)
        self.assertIn('DRIFT', check.stdout)
        refused = self.glob('--uninstall')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('--global --force-uninstall', refused.stderr)
        forced = self.ok(self.glob('--force-uninstall'))
        self.assertIn('BACKUP', forced.stdout)
        backups = list(self.als_home.glob('backup-*/*'))
        self.assertEqual(len(backups), 1)
        self.assertFalse(role.exists())
        # Lost manifest: identical files are adopted with --adopt.
        self.ok(self.glob())
        (self.als_home / 'manifest.json').unlink()
        self.assertEqual(self.glob().returncode, 2)
        self.ok(self.glob('--adopt'))
        self.ok(self.glob('--check'))

    def test_codex_home_mismatch_refused(self):
        self.ok(self.glob())
        self.env['CODEX_HOME'] = str(self.base / 'other')
        result = self.glob('--uninstall')
        self.assertEqual(result.returncode, 2)
        self.assertIn('CODEX_HOME=' + str(self.codex_home), result.stderr)

    def test_hooks_only_lifecycle(self):
        refused = self.proj('--hooks-only')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('--global', refused.stderr)
        self.ok(self.glob())
        self.ok(self.proj('--hooks-only'))
        hooks = json.loads((self.p / '.codex/hooks.json').read_text())
        command = hooks['hooks']['Stop'][0]['hooks'][0]['command']
        self.assertEqual(command, 'python3 "${HOME:-' + str(self.home) + '}/.local/share/als/harness.py" hook')
        self.assertEqual(hooks['hooks']['PreToolUse'][0]['matcher'], 'spawn_agent')
        self.assertEqual(sorted(p.name for p in self.p.iterdir()), ['.codex', '.git'])
        self.ok(self.proj('--hooks-only', '--check'))
        self.ok(self.glob('--check'))
        blocked = self.glob('--uninstall')
        self.assertEqual(blocked.returncode, 2)
        self.assertIn('als uninstall --hooks-only', blocked.stderr)
        self.ok(self.proj('--hooks-only', '--uninstall'))
        self.assertEqual(sorted(p.name for p in self.p.iterdir()), ['.git'])
        self.ok(self.glob('--uninstall'))

    def test_hooks_only_preserves_user_hooks_bytes(self):
        self.ok(self.glob())
        (self.p / '.codex').mkdir()
        original = b'{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}'
        (self.p / '.codex/hooks.json').write_bytes(original)
        self.ok(self.proj('--hooks-only'))
        self.ok(self.proj('--hooks-only'))
        self.ok(self.proj('--hooks-only', '--uninstall'))
        self.assertEqual((self.p / '.codex/hooks.json').read_bytes(), original)

    @unittest.skipIf(__import__('config_overlay').tomllib is None, 'project mode needs tomllib or tomli')
    def test_hooks_only_refused_over_project_install(self):
        self.ok(self.glob())
        self.ok(self.proj())
        result = self.proj('--hooks-only')
        self.assertEqual(result.returncode, 2)
        self.assertIn('project-mode', result.stderr)

    def test_moved_hooks_project_detected_and_forgotten(self):
        self.ok(self.glob())
        self.ok(self.proj('--hooks-only'))
        old = self.p
        moved = self.base / 'moved'
        old.rename(moved)
        self.p = moved
        check = self.glob('--check')
        self.assertEqual(check.returncode, 2)
        self.assertIn('PROJECT MOVED', check.stdout)
        here = self.proj('--hooks-only', '--check')
        self.assertEqual(here.returncode, 2)
        self.assertIn('PROJECT MOVED', here.stdout)
        forget = self.py('--target', str(old), '--hooks-only', '--uninstall')
        self.assertEqual(forget.returncode, 0, forget.stderr)
        self.assertIn('FORGET', forget.stdout)
        self.ok(self.glob('--check'))

    # ------------------------------------------------------------ bin/als
    def test_als_entrypoint(self):
        self.ok(self.glob())
        helptext = self.als('help').stdout
        for word in ('install', 'uninstall', 'doctor', 'run', 'plan new FILE', 'usage', 'status', 'version'):
            self.assertIn(word, helptext)
        self.assertEqual(self.als('version').stdout.strip(), (ROOT / 'VERSION').read_text().strip())
        doctor = self.als('doctor')
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn('codex-feature:hooks: enabled', doctor.stdout)
        self.assertIn('Harness', doctor.stdout)
        self.env['FAKE_HOOKS'] = 'false'
        doctor = self.als('doctor')
        self.assertNotEqual(doctor.returncode, 0)
        self.assertIn('codex features enable hooks', doctor.stdout)
        self.env.pop('FAKE_HOOKS')
        run = self.als('run', '--foo')
        self.assertEqual(run.stdout.strip(), 'FAKE CODEX --profile als --foo')
        self.ok(self.als('install', '--hooks-only'))
        self.assertTrue((self.p / '.codex/hooks.json').is_file())
        sub = self.p / 'sub'
        sub.mkdir()
        status = self.als('status', cwd=sub)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn('runs', json.loads(status.stdout))
        plan = self.als('plan', 'new', 'plan.json')
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(self.als('plan', 'new', 'plan.json').returncode, 2)
        prepared = self.als('prepare', '--plan', str(self.p / 'plan.json'))
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.ok(self.als('uninstall', '--hooks-only'))
        self.ok(self.als('uninstall', '--global'))
        self.assertFalse(self.als_home.exists())

    def test_global_install_points_at_entrypoint_not_clone(self):
        result = self.glob()
        self.ok(result)
        self.assertIn('als install --hooks-only', result.stdout)
        self.assertNotIn(str(ROOT / 'install.sh'), result.stdout)

    def test_doctor_is_human_text_by_default_and_json_on_request(self):
        self.ok(self.glob())
        text = self.als('doctor', '--no-probe')
        self.assertEqual(text.returncode, 0, text.stdout + text.stderr)
        self.assertFalse(text.stdout.lstrip().startswith('{'), text.stdout[:200])
        self.assertNotIn('"hooks_configured"', text.stdout)
        as_json = self.als('doctor', '--no-probe', '--json')
        self.assertIn('"items"', as_json.stdout)
        bad = self.als('doctor', '--bogus')
        self.assertEqual(bad.returncode, 2)

    def test_doctor_no_probe_never_runs_codex_features(self):
        self.ok(self.glob())
        marker = self.base / 'features-called'
        fake = self.base / 'fakebin' / 'codex'
        fake.write_text('#!/bin/sh\ncase "$1" in\n --version) echo "codex-cli 9.9.9" ;;\n'
                        ' features) touch "' + str(marker) + '"; printf "hooks stable true\\n" ;;\nesac\n')
        for args in (('doctor', '--no-probe'), ('doctor', '--no-probe', '--json')):
            self.als(*args)
            self.assertFalse(marker.exists(), args)

    def test_als_run_without_profile_uses_plain_codex(self):
        self.ok(self.glob())
        (self.codex_home / 'als.config.toml').rename(self.base / 'saved')
        self.assertEqual(self.als('run').stdout.strip(), 'FAKE CODEX')

    def test_setup_bootstrap(self):
        result = subprocess.run(['sh', str(ROOT / 'setup.sh')], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in ('doctor', 'install --hooks-only', ' run'):
            self.assertIn(line, result.stdout)
        self.assertIn('export PATH="' + str(self.bin) + ':$PATH"', result.stdout)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ['.codex', '.local'])
        doctor = self.als('doctor', '--no-probe')
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)


if __name__ == '__main__':
    unittest.main()
