"""Global mode, the `als` entry point and setup.sh (pre-1.2 hook cleanup: test_v12_remove_managed).

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
 features) printf 'multi_agent    stable  %s\\n' "${FAKE_MULTI:-true}" ;;
 login) echo "${FAKE_LOGIN:-Logged in using ChatGPT}" ;;
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
        for rel in ('als_harness/deps.py', 'als_flow/flow.py', 'token_usage.py', 'usage.sh', 'run.sh',
                    'check-runtime.py', 'smoke-test.txt', 'VERSION', 'licenses/Apache-2.0.txt',
                    'manifest.json', 'release/install.py', 'release/bin/als', 'release/profiles/config.toml'):
            self.assertTrue((self.als_home / rel).is_file(), rel)
        for rel in ('harness.py', 'harness-guide.md', 'efficiency-guide.md', 'als_harness/engine.py'):
            self.assertFalse((self.als_home / rel).exists(), rel)
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

    # ------------------------------------------------------------ bin/als
    def test_als_entrypoint(self):
        self.ok(self.glob())
        helptext = self.als('help').stdout
        for word in ('install', 'uninstall', 'doctor', 'run', 'harness init', 'usage', 'version'):
            self.assertIn(word, helptext)
        for word in ('plan new', 'status|start'):
            self.assertNotIn(word, helptext)
        self.assertEqual(self.als('version').stdout.strip(), (ROOT / 'VERSION').read_text().strip())
        doctor = self.als('doctor')
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn('codex-feature:multi_agent: enabled', doctor.stdout)
        self.assertNotIn('hooks', doctor.stdout)
        self.env['FAKE_MULTI'] = 'false'
        doctor = self.als('doctor')
        self.assertNotEqual(doctor.returncode, 0)
        self.assertIn('codex features enable multi_agent', doctor.stdout)
        self.env.pop('FAKE_MULTI')
        run = self.als('run', '--foo')
        self.assertEqual(run.stdout.strip(), 'FAKE CODEX --profile als -c openai_base_url="https://chatgpt.com/backend-api/codex" --foo')
        sub = self.p / 'sub'
        sub.mkdir()
        for old in ('status', 'start', 'plan', 'verify', 'hook'):
            gone = self.als(old, cwd=sub)
            self.assertEqual(gone.returncode, 2)
            self.assertIn('removed in 1.2', gone.stderr)
        self.assertEqual(self.als('no-such-command').returncode, 2)
        init = self.als('harness', 'init', cwd=sub)
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertIn('$astra-luna-sol', (self.p / 'AGENTS.md').read_text())
        self.ok(self.als('uninstall', '--global'))
        self.assertFalse(self.als_home.exists())

    def test_global_install_points_at_entrypoint_not_clone(self):
        result = self.glob()
        self.ok(result)
        self.assertIn('als harness init', result.stdout)
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

    def test_reinstall_without_custom_home_names_the_other_install(self):
        custom = self.base / "custom als"
        self.env['ALS_HOME'] = str(custom)
        self.ok(self.glob())
        del self.env['ALS_HOME']
        result = self.glob()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        text = result.stdout + result.stderr
        self.assertIn('ALS install at ' + str(custom), text)
        self.assertNotIn('not managed by ALS', text)

    def test_same_home_manifestless_als_suggests_adopt_not_a_loop(self):
        # A custom ALS_HOME is baked into the installed `als`; the same home
        # must not be reported as "another install".
        custom = self.base / 'custom home'
        self.env['ALS_HOME'] = str(custom)
        self.ok(self.glob())
        manifest = custom / 'manifest.json'
        data = json.loads(manifest.read_text())
        del data['files']['BIN/als']  # the entrypoint on PATH, not release/bin/als
        manifest.write_text(json.dumps(data))
        result = self.glob()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        text = result.stdout + result.stderr
        self.assertNotIn('ALS install at', text)
        self.assertIn('--adopt', text)
        self.ok(self.glob('--adopt'))

    def test_printed_remedy_carries_custom_als_home(self):
        custom = self.base / "custom 'home'"
        self.env['ALS_HOME'] = str(custom)
        self.ok(self.glob())
        manifest = custom / 'manifest.json'
        data = json.loads(manifest.read_text())
        del data['files']['BIN/als']
        manifest.write_text(json.dumps(data))
        del self.env['ALS_HOME']  # the installed `als` supplies its recorded home
        refused = self.als('install', '--global')
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        text = refused.stdout + refused.stderr
        remedy = text.split('take ownership with: ', 1)[1].strip().splitlines()[0]
        self.assertTrue(remedy.startswith('ALS_HOME='), remedy)
        self.ok(subprocess.run(['sh', '-c', remedy], capture_output=True, text=True, env=self.env))
        self.ok(self.als('install', '--global', '--check'))

    def test_als_run_without_profile_uses_plain_codex(self):
        self.ok(self.glob())
        (self.codex_home / 'als.config.toml').rename(self.base / 'saved')
        self.assertEqual(self.als('run').stdout.strip(), 'FAKE CODEX -c openai_base_url="https://chatgpt.com/backend-api/codex"')

    def test_als_run_endpoint_bypasses_proxy_and_follows_login(self):
        """als run talks to the GPT endpoint directly (no OpenCodex) unless told otherwise."""
        self.ok(self.glob())
        self.env['FAKE_LOGIN'] = 'Logged in using an API key - sk-***'
        self.assertIn('-c openai_base_url="https://api.openai.com/v1"', self.als('run').stdout)
        del self.env['FAKE_LOGIN']
        self.env['ALS_CODEX_BASE_URL'] = ''
        self.assertEqual(self.als('run', '--x').stdout.strip(), 'FAKE CODEX --profile als --x')
        self.env['ALS_CODEX_BASE_URL'] = 'http://127.0.0.1:10100/v1'
        self.assertIn('-c openai_base_url="http://127.0.0.1:10100/v1"', self.als('run').stdout)

    def test_setup_bootstrap(self):
        result = subprocess.run(['sh', str(ROOT / 'setup.sh')], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in ('doctor', 'harness init', ' run'):
            self.assertIn(line, result.stdout)
        self.assertIn('export PATH="' + str(self.bin) + ':$PATH"', result.stdout)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ['.codex', '.local'])
        doctor = self.als('doctor', '--no-probe')
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)


if __name__ == '__main__':
    unittest.main()
