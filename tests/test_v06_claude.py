"""ALS 0.6 Claude Code track: als-gpt launcher and installer. Fake codex only, no model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'claude'))
import als_gpt  # noqa: E402
from als_harness.common import ROLES  # noqa: E402

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
if args[:2] == ['login', 'status']:
    print(os.environ.get('FAKE_AUTH', 'Logged in using ChatGPT')); sys.exit(0)
log = os.environ['FAKE_LOG']
prompt = sys.stdin.read()
with open(log, 'a') as f:
    f.write(json.dumps({'argv': args, 'prompt': prompt, 'cwd': os.getcwd()}) + '\n')
cd = args[args.index('--cd') + 1]
if os.environ.get('FAKE_WRITE'):
    with open(os.path.join(cd, os.environ['FAKE_WRITE']), 'w') as f:
        f.write('changed\n')
time.sleep(float(os.environ.get('FAKE_SLEEP', '0')))
out = args[args.index('-o') + 1]
with open(out, 'w') as f:
    f.write('DONE: did the task')
print(json.dumps({'type': 'thread.started'}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 120, 'cached_input_tokens': 100, 'output_tokens': 7}}))
sys.exit(int(os.environ.get('FAKE_RC', '0')))
'''


def git(*args, cwd):
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / 'repo'
        (self.repo / 'pkg').mkdir(parents=True)
        git('init', '-q', cwd=self.repo)
        (self.repo / 'pkg/a.py').write_text('x = 1\n')
        git('add', '.', cwd=self.repo)
        git('commit', '-q', '-m', 'init', cwd=self.repo)
        fake = self.base / 'codex'
        fake.write_text(FAKE_CODEX)
        fake.chmod(0o755)
        self.log = self.base / 'calls.jsonl'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('ALS_', 'FAKE_'))}
        self.env.update(ALS_CODEX=str(fake), FAKE_LOG=str(self.log), ALS_STATE_HOME=str(self.base / 'state'),
                        HOME=str(self.base / 'home'))
        self.task = self.base / 'task.md'
        self.task.write_text('Add a docstring to pkg/a.py')

    def tearDown(self):
        self.tmp.cleanup()

    def run_gpt(self, *args, cwd=None, **env):
        return subprocess.run([sys.executable, str(ROOT / 'claude/als_gpt.py'), *args], cwd=cwd or self.repo,
                              capture_output=True, text=True, env=dict(self.env, **env), timeout=60)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


class RoleTests(unittest.TestCase):
    def test_role_files_match_harness_pins(self):
        roles = als_gpt.load_roles()
        self.assertEqual(set(roles), set(ROLES))
        for name, (model, effort, writes) in ROLES.items():
            self.assertEqual(roles[name]['model'], model)
            self.assertEqual(roles[name]['model_reasoning_effort'], effort)
            self.assertEqual(roles[name]['sandbox_mode'] != 'read-only', writes, name)
            self.assertIn('ALS', roles[name]['developer_instructions'])

    def test_parse_role_strings(self):
        role = als_gpt.parse_role('name = "a\\"b 한글"\ndeveloper_instructions = """\nline1\nline2\n"""\n')
        self.assertEqual(role['name'], 'a"b 한글')
        self.assertEqual(role['developer_instructions'], 'line1\nline2')


class RunTests(Base):
    def test_read_only_role_runs_in_place_with_pins_and_native_url(self):
        result = self.run_gpt('run', 'reviewer', str(self.task))
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.calls()[0]
        argv = call['argv']
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-6.1-sol')
        self.assertIn('model_reasoning_effort="medium"', argv)
        self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
        self.assertIn('openai_base_url="%s"' % als_gpt.NATIVE_CHATGPT_URL, argv)
        self.assertEqual(Path(argv[argv.index('--cd') + 1]), self.repo)
        self.assertIn('ALS / REVIEWER', call['prompt'])
        self.assertIn('Add a docstring to pkg/a.py', call['prompt'])
        self.assertIn('usage: input 120 (cached 100), output 7', result.stdout)
        self.assertIn('DONE: did the task', result.stdout)
        self.assertNotIn('worktree', result.stdout.split('--- report ---')[0])

    def test_writer_gets_own_worktree_and_branch(self):
        result = self.run_gpt('run', 'worker', str(self.task), FAKE_WRITE='new.txt')
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.calls()[0]['argv']
        workdir = Path(argv[argv.index('--cd') + 1])
        self.assertNotEqual(workdir, self.repo)
        self.assertTrue((workdir / 'new.txt').exists())
        self.assertFalse((self.repo / 'new.txt').exists())
        self.assertIn('new.txt', result.stdout)
        run_id = result.stdout.split()[1]
        self.assertIn('als/' + run_id, git('branch', '--list', 'als/*', cwd=self.repo))
        self.assertEqual(argv[argv.index('-s') + 1], 'workspace-write')
        self.assertIn('model_reasoning_effort="max"', argv)
        clean = self.run_gpt('clean', run_id)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertFalse(workdir.exists())
        self.assertIn('als/' + run_id, git('branch', '--list', 'als/*', cwd=self.repo))

    def test_subdirectory_maps_into_worktree(self):
        result = self.run_gpt('run', 'worker', str(self.task), cwd=self.repo / 'pkg')
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.calls()[0]['argv']
        self.assertEqual(Path(argv[argv.index('--cd') + 1]).name, 'pkg')

    def test_in_place_writer(self):
        result = self.run_gpt('run', 'worker', str(self.task), '--in-place', FAKE_WRITE='inplace.txt')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / 'inplace.txt').exists())

    def test_failure_and_timeout_exit_codes(self):
        failed = self.run_gpt('run', 'explorer', str(self.task), FAKE_RC='3')
        self.assertEqual(failed.returncode, 1)
        self.assertIn('status failed', failed.stdout)
        start = time.monotonic()
        timed = self.run_gpt('run', 'explorer', str(self.task), '--timeout', '1', FAKE_SLEEP='30')
        self.assertEqual(timed.returncode, 124)
        self.assertLess(time.monotonic() - start, 20)
        self.assertIn('status timeout', timed.stdout)

    def test_endpoint_selection(self):
        self.run_gpt('run', 'explorer', str(self.task), ALS_CODEX_BASE_URL='')
        self.assertFalse(any('openai_base_url' in a for a in self.calls()[-1]['argv']))
        self.run_gpt('run', 'explorer', str(self.task), FAKE_AUTH='Logged in using an API key')
        self.assertIn('openai_base_url="%s"' % als_gpt.NATIVE_API_URL, self.calls()[-1]['argv'])

    def test_usage_errors(self):
        self.assertEqual(self.run_gpt('run', 'nobody', str(self.task)).returncode, 2)
        empty = self.base / 'empty.md'
        empty.write_text('  \n')
        self.assertEqual(self.run_gpt('run', 'worker', str(empty)).returncode, 2)
        self.assertEqual(self.run_gpt('run', 'worker', str(self.task), ALS_STATE_HOME='rel').returncode, 2)

    def test_dry_run_does_not_call_codex(self):
        result = self.run_gpt('run', 'tester', str(self.task), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(json.loads(result.stdout)['status'], 'dry-run')

    def test_ls_and_show(self):
        run_id = self.run_gpt('run', 'explorer', str(self.task)).stdout.split()[1]
        self.assertIn(run_id, self.run_gpt('ls').stdout)
        shown = self.run_gpt('show', run_id, '--json')
        self.assertEqual(json.loads(shown.stdout)['run'], run_id)
        self.assertEqual(self.run_gpt('show', 'missing').returncode, 2)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name).resolve()
        self.home = home
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('ALS_', 'CLAUDE_'))}
        self.env['HOME'] = str(home)

    def tearDown(self):
        self.tmp.cleanup()

    def inst(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'claude/install_claude.py'), *args],
                              capture_output=True, text=True, env=self.env, timeout=60)

    def test_install_check_uninstall(self):
        self.assertEqual(self.inst('--check').returncode, 1)
        self.assertEqual(self.inst('--dry-run').returncode, 0)
        self.assertFalse((self.home / '.claude').exists())
        self.assertEqual(self.inst().returncode, 0)
        skill = self.home / '.claude/skills/als/SKILL.md'
        launcher = self.home / '.local/bin/als-gpt'
        self.assertTrue(skill.is_file())
        roles = subprocess.run([str(launcher), 'roles'], capture_output=True, text=True, env=self.env)
        self.assertEqual(roles.returncode, 0, roles.stderr)
        self.assertIn('als_hard_worker', roles.stdout)
        self.assertEqual(self.inst('--check').returncode, 0)
        self.assertEqual(self.inst().returncode, 0)  # idempotent
        self.assertEqual(self.inst('--uninstall').returncode, 0)
        self.assertFalse(skill.exists())
        self.assertFalse(launcher.exists())
        self.assertFalse((self.home / '.local/share/als/claude').exists())

    def test_refuses_user_owned_and_edited_files(self):
        skill = self.home / '.claude/skills/als/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('my own skill\n')
        self.assertEqual(self.inst().returncode, 2)
        self.assertEqual(skill.read_text(), 'my own skill\n')
        skill.unlink()
        self.assertEqual(self.inst().returncode, 0)
        skill.write_text('edited\n')
        self.assertEqual(self.inst('--check').returncode, 2)
        self.assertEqual(self.inst('--uninstall').returncode, 2)
        self.assertEqual(skill.read_text(), 'edited\n')


if __name__ == '__main__':
    unittest.main()
