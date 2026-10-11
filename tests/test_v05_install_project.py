"""0.5 installer, project mode: hooks in place, upgrades, recovery and small fixes."""
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tarfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v05_install_base as base  # noqa: E402

try:
    import tomllib  # noqa: F401
    from als_harness import integration
    HAVE_TOML = True
except ModuleNotFoundError:
    try:
        import tomli  # noqa: F401
        from als_harness import integration
        HAVE_TOML = True
    except ModuleNotFoundError:
        HAVE_TOML = False

ROOT = base.ROOT


@unittest.skipUnless(HAVE_TOML, 'installer project mode needs tomllib or tomli')
class InstalledHookTests(base.Sandbox, unittest.TestCase):
    def hook_command(self, project):
        hooks = json.loads((project / '.codex/hooks.json').read_text())
        return hooks['hooks']['Stop'][0]['hooks'][0]['command']

    def run_doctor(self, command, cwd):
        doctor = command[:-len(' hook')] + ' doctor'
        return subprocess.run(['sh', '-c', doctor], cwd=cwd, capture_output=True, text=True, env=self.env)

    def test_hook_command_runs_from_subdirectory_and_worktree(self):
        self.ok(self.proj())
        command = self.hook_command(self.p)
        sub = self.p / 'a/b'
        sub.mkdir(parents=True)
        result = self.run_doctor(command, sub)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['hooks_configured'])
        git = ['git', '-C', str(self.p), '-c', 'user.email=t@t', '-c', 'user.name=t']
        subprocess.run(git + ['add', '-A'], check=True, capture_output=True)
        subprocess.run(git + ['commit', '-qm', 'x'], check=True, capture_output=True)
        worktree = self.base / 'fork'
        subprocess.run(git + ['worktree', 'add', '-q', str(worktree)], check=True, capture_output=True)
        result = self.run_doctor(command, worktree)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_project_move_keeps_hooks_valid(self):
        self.ok(self.proj())
        moved = self.base / 'moved'
        self.p.rename(moved)
        self.p = moved
        self.ok(self.proj('--check'))

    def test_preexisting_hooks_restored_byte_exact(self):
        (self.p / '.codex').mkdir()
        original = b'{ "hooks" : { "Stop" : [ { "hooks" : [ { "type":"command", "command":"echo hi" } ] } ] } }'
        (self.p / '.codex/hooks.json').write_bytes(original)
        self.ok(self.proj())
        self.ok(self.proj())  # reinstall keeps the original bytes in the manifest
        self.ok(self.proj('--check'))
        self.ok(self.proj('--uninstall'))
        self.assertEqual((self.p / '.codex/hooks.json').read_bytes(), original)

    def test_unmanaged_hook_edit_spliced_on_uninstall(self):
        (self.p / '.codex').mkdir()
        (self.p / '.codex/hooks.json').write_bytes(b'{"hooks":{}}')
        self.ok(self.proj())
        path = self.p / '.codex/hooks.json'
        data = json.loads(path.read_text())
        data['hooks']['Stop'].append({'hooks': [{'type': 'command', 'command': 'echo user'}]})
        path.write_text(json.dumps(data))
        check = self.proj('--check')
        self.assertEqual(check.returncode, 2)
        self.assertIn('UNMANAGED HOOKS CHANGED', check.stdout)
        self.ok(self.proj('--uninstall'))
        self.assertEqual(json.loads(path.read_text()),
                         {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo user'}]}]}})

    def legacy_groups(self, project):
        command = ('python3 ' + shlex.quote(str(project / '.astra-luna-sol/harness.py'))
                   + ' --project ' + shlex.quote(str(project)) + ' hook')
        out = {}
        for event in integration.HOOK_EVENTS:
            handler = {'type': 'command', 'command': command, 'timeout': 3 if event == 'SessionEnd' else 10,
                       'statusMessage': 'ALS harness'}
            if event in {'SessionStart', 'SubagentStart', 'PreToolUse'}:
                handler['additionalContextLimit'] = 500
            out[event] = {'hooks': [handler]}
        return out

    def make_legacy(self, project_in_hooks):
        """Rewrite a fresh install into the 0.4 hook shape (absolute --project)."""
        self.ok(self.proj())
        user = {'hooks': [{'type': 'command', 'command': 'echo keep'}]}
        groups = self.legacy_groups(project_in_hooks)
        doc = {'hooks': {e: ([user] if e == 'Stop' else []) + [g] for e, g in groups.items()}}
        raw = (json.dumps(doc, indent=2) + '\n').encode()
        (self.p / '.codex/hooks.json').write_bytes(raw)
        m = self.manifest()
        m['version'] = '0.4.0'
        m['hooks'] = {'created': False, 'prior_hooks_key': True, 'prior_events': ['Stop'], 'groups': groups}
        self.write_manifest(m)
        return user

    def test_upgrade_replaces_legacy_absolute_groups(self):
        user = self.make_legacy(self.p)
        check = self.proj('--check')
        self.assertEqual(check.returncode, 4, check.stdout + check.stderr)
        self.assertIn('ALS OUTDATED', check.stdout)
        self.ok(self.proj())
        self.ok(self.proj('--check'))
        hooks = json.loads((self.p / '.codex/hooks.json').read_text())['hooks']
        self.assertNotIn('--project', json.dumps(hooks))
        self.assertEqual(hooks['Stop'][0], user)
        self.ok(self.proj('--uninstall'))
        self.assertEqual(json.loads((self.p / '.codex/hooks.json').read_text()), {'hooks': {'Stop': [user]}})

    def test_moved_legacy_project_reported(self):
        self.make_legacy(self.base / 'old-location')
        check = self.proj('--check')
        self.assertEqual(check.returncode, 2)
        self.assertIn('PROJECT MOVED', check.stdout)
        self.assertIn('install.sh', check.stdout)
        self.ok(self.proj())
        self.ok(self.proj('--check'))

    def test_real_04_install_upgrades(self):
        """Install with the 0.4.0 tree from git history, then upgrade in place."""
        has = subprocess.run(['git', '-C', str(ROOT), 'cat-file', '-e', '15ff2b1^{commit}'],
                             capture_output=True)
        if has.returncode:
            self.skipTest('0.4.0 history (15ff2b1) not available in this checkout')
        archive = subprocess.run(['git', '-C', str(ROOT), 'archive', '15ff2b1'], capture_output=True, check=True)
        old = self.base / 'als-0.4'
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(old, filter='data')
        (self.p / '.codex').mkdir()
        (self.p / '.codex/hooks.json').write_text('{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo u"}]}]}}')
        legacy = subprocess.run(['python3', str(old / 'install.py'), '--target', str(self.p)],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertIn('--project', (self.p / '.codex/hooks.json').read_text())
        self.assertEqual(self.proj('--check').returncode, 4)
        self.ok(self.proj())
        self.ok(self.proj('--check'))
        self.assertNotIn('--project', (self.p / '.codex/hooks.json').read_text())
        self.ok(self.proj('--uninstall'))
        self.assertEqual(json.loads((self.p / '.codex/hooks.json').read_text()),
                         {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo u'}]}]}})



@unittest.skipUnless(HAVE_TOML, 'installer project mode needs tomllib or tomli')
class RecoveryTests(base.Sandbox, unittest.TestCase):
    def test_uninstall_proceeds_when_managed_files_missing(self):
        self.ok(self.proj())
        for rel in ('.codex/agents/als_worker.toml', '.astra-luna-sol/harness.py', '.codex/hooks.json',
                    '.codex/config.toml', 'AGENTS.md'):
            (self.p / rel).unlink()
        self.assertEqual(self.proj('--check').returncode, 2)
        self.ok(self.proj('--uninstall'))
        self.assertEqual(self.files(), {})
        self.assertEqual(self.dirs(), [])

    def test_modified_file_refusal_names_force_command_and_force_backs_up(self):
        self.ok(self.proj())
        role = self.p / '.codex/agents/als_worker.toml'
        role.write_text('edited by user\n')
        refused = self.proj('--uninstall')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('--force-uninstall', refused.stderr)
        self.assertIn('install.sh', refused.stderr)
        self.assertTrue(role.exists())
        forced = self.ok(self.proj('--force-uninstall'))
        self.assertIn('BACKUP .codex/agents/als_worker.toml', forced.stdout)
        backups = list((self.p / '.astra-luna-sol').glob('backup-*/.codex/agents/als_worker.toml'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'edited by user\n')
        self.assertFalse(role.exists())
        self.assertFalse((self.p / '.astra-luna-sol/manifest.json').exists())

    def test_force_uninstall_keeps_edited_merged_config_and_backs_it_up(self):
        cfg = self.p / '.codex/config.toml'
        cfg.parent.mkdir()
        cfg.write_text('model = "mine"\n')
        self.ok(self.proj())
        cfg.write_text(cfg.read_text().replace('gpt-6-astra', 'other'))
        self.assertEqual(self.proj('--uninstall').returncode, 2)
        forced = self.ok(self.proj('--force-uninstall'))
        self.assertIn('KEEP', forced.stdout)
        self.assertTrue(cfg.exists())
        self.assertTrue(list((self.p / '.astra-luna-sol').glob('backup-*/.codex/config.toml')))

    def test_adopt_after_lost_manifest(self):
        self.ok(self.proj())
        (self.p / '.astra-luna-sol/manifest.json').unlink()
        refused = self.proj()
        self.assertEqual(refused.returncode, 2)
        self.assertIn('--adopt', refused.stderr)
        self.ok(self.proj('--adopt'))
        self.ok(self.proj('--check'))
        self.ok(self.proj('--uninstall'))
        self.assertEqual(self.files(), {})

    def test_adopt_recovers_merged_config(self):
        cfg = self.p / '.codex/config.toml'
        cfg.parent.mkdir()
        original = b'model = "mine"\n[agents]\nenabled = false\n'
        cfg.write_bytes(original)
        self.ok(self.proj())
        (self.p / '.astra-luna-sol/manifest.json').unlink()
        self.ok(self.proj('--adopt'))
        self.ok(self.proj('--uninstall'))
        self.assertEqual(cfg.read_bytes(), original)

    def test_adopt_refuses_non_identical_files(self):
        (self.p / '.codex/agents').mkdir(parents=True)
        (self.p / '.codex/agents/als_worker.toml').write_text('mine')
        result = self.proj('--adopt')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Move it aside', result.stderr)


@unittest.skipUnless(HAVE_TOML, 'installer project mode needs tomllib or tomli')
class SmallFixTests(base.Sandbox, unittest.TestCase):
    def test_blank_target_rejected(self):
        for value in ('', '   '):
            with self.subTest(value=value):
                result = self.py('--target', value)
                self.assertEqual(result.returncode, 2)
                self.assertIn('Empty project path', result.stderr)
                result = self.sh(value)
                self.assertEqual(result.returncode, 2)
                self.assertIn('empty project path', result.stderr)

    def test_missing_python3_clear_message(self):
        fake = self.base / 'bin'
        fake.mkdir()
        for tool in ('dirname', 'cat'):
            found = subprocess.run(['sh', '-c', 'command -v ' + tool], capture_output=True, text=True).stdout.strip()
            os.symlink(found, fake / tool)
        env = dict(self.env, PATH=str(fake))
        sh = subprocess.run(['sh', '-c', 'command -v sh'], capture_output=True, text=True).stdout.strip()
        result = subprocess.run([sh, str(ROOT / 'install.sh'), str(self.p)], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn('python3', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_usage_lists_all_flags(self):
        result = self.sh()
        self.assertEqual(result.returncode, 2)
        helptext = self.sh('--help').stdout + self.py('--help').stdout
        for flag in ('--global', '--hooks-only', '--dry-run', '--check', '--uninstall', '--force-uninstall',
                     '--adopt', '--skip-hooks', '--policy-file'):
            self.assertIn(flag, result.stderr)
            self.assertIn(flag, helptext)
        self.assertIn('4', self.py('--help').stdout)

    def test_malformed_manifest_refused_not_traceback(self):
        self.ok(self.proj())
        for field, value in (('hooks', 'x'), ('config', 3), ('files', []), ('created_dirs', 'x'),
                             ('agents', 5), ('hooks', {'groups': 1})):
            with self.subTest(field=field):
                m = self.manifest()
                good = json.dumps(m)
                m[field] = value
                self.write_manifest(m)
                for args in ((), ('--check',), ('--uninstall',)):
                    result = self.proj(*args)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn('REFUSED: Invalid installation manifest', result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
                (self.p / '.astra-luna-sol/manifest.json').write_text(good)
        (self.p / '.astra-luna-sol/manifest.json').write_text('{not json')
        self.assertIn('REFUSED: Invalid installation manifest', self.proj().stderr)

    def test_modified_retained_ignore_specific_message(self):
        self.ok(self.proj())
        (self.p / '.astra-luna-sol/state').mkdir()
        (self.p / '.astra-luna-sol/state/x').write_text('keep')
        self.ok(self.proj('--uninstall'))
        ignore = self.p / '.astra-luna-sol/.gitignore'
        ignore.write_text(ignore.read_text() + 'mine/\n')
        result = self.proj()
        self.assertEqual(result.returncode, 2)
        self.assertIn('retained runtime ignore file', result.stderr)
        self.assertIn('rm ', result.stderr)

    def test_uninstall_removes_created_dirs_and_pycache(self):
        (self.p / '.codex').mkdir()
        (self.p / '.codex/keep.txt').write_text('user')
        self.ok(self.proj())
        cache = self.p / '.astra-luna-sol/als_harness/__pycache__'
        cache.mkdir()
        (cache / 'cli.cpython-311.pyc').write_bytes(b'x')
        self.ok(self.proj('--uninstall'))
        self.assertEqual(self.dirs(), ['.codex'])
        self.assertEqual(self.files(), {'.codex/keep.txt': b'user'})

    def test_check_outdated_rc4_and_mode_drift_rc2(self):
        self.ok(self.proj())
        m = self.manifest()
        m['version'] = '0.0.1'
        self.write_manifest(m)
        result = self.proj('--check')
        self.assertEqual(result.returncode, 4)
        self.assertIn('ALS OUTDATED', result.stdout)
        self.ok(self.proj())
        harness = self.p / '.astra-luna-sol/harness.py'
        harness.chmod(0o600)
        result = self.proj('--check')
        self.assertEqual(result.returncode, 2)
        self.assertIn('MODE DRIFT', result.stdout)
        self.ok(self.proj())
        self.assertEqual(stat.S_IMODE(harness.stat().st_mode), 0o755)
        self.ok(self.proj('--check'))

    def test_risky_existing_settings_warned_not_changed(self):
        cfg = self.p / '.codex/config.toml'
        cfg.parent.mkdir()
        cfg.write_text('approval_policy = "never"\nsandbox_mode = "danger-full-access"\n')
        result = self.ok(self.proj())
        self.assertIn('approval_policy = "never"', result.stderr)
        self.assertIn('sandbox_mode = "danger-full-access"', result.stderr)
        self.assertIn('approval_policy = "never"', cfg.read_text())

    def test_policy_file_for_symlinked_agents(self):
        docs = self.p / 'docs'
        docs.mkdir()
        real = docs / 'AGENTS.md'
        real.write_text('# rules\n')
        (self.p / 'AGENTS.md').symlink_to('docs/AGENTS.md')
        refused = self.proj()
        self.assertEqual(refused.returncode, 2)
        self.assertIn('--policy-file', refused.stderr)
        self.ok(self.proj('--policy-file', 'docs/AGENTS.md'))
        self.assertIn('ALS:BEGIN', real.read_text())
        self.ok(self.proj('--check'))
        self.ok(self.proj())  # recorded policy file reused without the flag
        self.ok(self.proj('--uninstall'))
        self.assertEqual(real.read_text(), '# rules\n')
        outside = self.proj('--policy-file', '../x.md')
        self.assertEqual(outside.returncode, 2)

    def test_hook_interpreter_falls_back_to_installer_python(self):
        fake = self.base / 'bin'
        fake.mkdir()
        (fake / 'python3').write_text('#!/bin/sh\nexit 1\n')
        (fake / 'python3').chmod(0o755)
        git = subprocess.run(['sh', '-c', 'command -v git'], capture_output=True, text=True).stdout.strip()
        os.symlink(git, fake / 'git')
        env = dict(self.env, PATH=str(fake))
        result = subprocess.run([sys.executable, str(ROOT / 'install.py'), '--target', str(self.p)],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('NOTICE', result.stderr)
        command = json.loads((self.p / '.codex/hooks.json').read_text())['hooks']['Stop'][0]['hooks'][0]['command']
        self.assertTrue(command.startswith(shlex.quote(os.path.abspath(sys.executable)) + ' '))
        self.assertEqual(self.manifest()['interpreter'], os.path.abspath(sys.executable))
        check = subprocess.run([sys.executable, str(ROOT / 'install.py'), '--target', str(self.p), '--check'],
                               capture_output=True, text=True, env=env)
        self.assertEqual(check.returncode, 0, check.stdout)


class NoTomlTests(base.Sandbox, unittest.TestCase):
    """Simulate Python 3.9/3.10 without tomli: project mode refuses clearly."""

    def run_without_toml(self, *args):
        code = ('import sys; sys.modules["tomllib"] = None; sys.modules["tomli"] = None; '
                'sys.argv = ["install.py", *sys.argv[1:]]; sys.path.insert(0, %r); '
                'import runpy; runpy.run_path(%r, run_name="__main__")' % (str(ROOT), str(ROOT / 'install.py')))
        return subprocess.run([sys.executable, '-c', code, *args], capture_output=True, text=True, env=self.env)

    def test_project_mode_needs_toml_global_does_not(self):
        result = self.run_without_toml('--target', str(self.p))
        self.assertEqual(result.returncode, 2)
        self.assertIn('Python 3.11+ (or tomli) is needed only to merge config.toml', result.stderr)
        self.assertFalse((self.p / '.codex').exists())
        result = self.run_without_toml('--global')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.codex_home / 'als.config.toml').is_file())
        result = self.run_without_toml('--global', '--uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
