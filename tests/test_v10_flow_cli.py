"""Tests for the als-flow, als-agent and als-task CLIs and workspace resolution (fake codex only)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from als_flow import common
from test_v10_flow_support import BIN, FakeCodexMixin

SCRIPT = '''
from als_flow import Flow
flow = Flow(name='cli-flow', phases=['collect', 'verify'])
with flow.phase('collect'):
    found = flow.pipeline(['a', 'b'], lambda i: flow.agent('item ' + i, agent_type='reader', label=i))
flow.log('done collecting')
flow.done({'found': found})
'''


class WrapperCase(unittest.TestCase):
    def test_wrappers_pass_dash_and_are_executable(self):
        for name in ('als-flow', 'als-agent', 'als-task'):
            path = BIN / name
            self.assertTrue(os.access(str(path), os.X_OK), name)
            dash = '/bin/dash' if os.path.exists('/bin/dash') else None
            if dash:
                self.assertEqual(subprocess.run([dash, '-n', str(path)]).returncode, 0, name)
            self.assertIn('${ALS_PYTHON:-python3}', path.read_text())


class WorkspaceCase(FakeCodexMixin, unittest.TestCase):
    def test_workspace_at_git_toplevel_from_subdir(self):
        sub = self.project / 'a' / 'b'
        sub.mkdir(parents=True)
        self.assertEqual(common.workspace_dir(sub), self.project / '_workspace')

    def test_workspace_outside_git_is_cwd(self):
        plain = self.tmp / 'plain'
        plain.mkdir()
        self.assertEqual(common.workspace_dir(plain), plain / '_workspace')

    def test_workspace_env_override(self):
        os.environ['ALS_WORKSPACE'] = str(self.tmp / 'ws')
        self.assertEqual(common.workspace_dir(self.project), self.tmp / 'ws')
        result = self.cli('als-task', 'add', 'x')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / 'ws' / 'tasks.json').exists())
        self.assertFalse((self.project / '_workspace').exists())


class FlowCliCase(FakeCodexMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.script = self.project / 'flow_script.py'
        self.script.write_text(SCRIPT)

    def test_run_status_ls_and_resume(self):
        result = self.cli('als-flow', 'run', str(self.script))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('flow run ', result.stdout)
        self.assertEqual(len(self.calls()), 2)
        status = self.cli('als-flow', 'status')
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn('phase collect', status.stdout)
        self.assertIn('2 ok, 0 failed, 0 cached', status.stdout)
        self.assertIn('result.json', status.stdout)
        run_id = status.stdout.split()[1]
        result = self.cli('als-flow', 'run', str(self.script), '--resume', run_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 2)  # all cached
        status = self.cli('als-flow', 'status')
        self.assertIn('2 cached', status.stdout)
        self.assertIn('resumed from %s' % run_id, status.stdout)
        old = self.cli('als-flow', 'status', run_id)
        self.assertIn('2 ok', old.stdout)
        listing = self.cli('als-flow', 'ls')
        self.assertEqual(listing.returncode, 0)
        self.assertEqual(len(listing.stdout.strip().splitlines()), 2)

    def test_exit_codes(self):
        self.assertEqual(self.cli('als-flow').returncode, 2)
        self.assertEqual(self.cli('als-flow', 'bogus').returncode, 2)
        self.assertEqual(self.cli('als-flow', 'run', 'missing.py').returncode, 2)
        self.assertEqual(self.cli('als-flow', 'run', str(self.script), '--resume', 'nope').returncode, 2)
        self.assertEqual(self.cli('als-flow', 'status').returncode, 1)  # no runs yet
        self.assertEqual(self.cli('als-flow', 'status', 'nope').returncode, 2)
        bad = self.project / 'bad.py'
        bad.write_text(SCRIPT.replace("flow.done", "raise SystemExit(3)\nflow.done"))
        result = self.cli('als-flow', 'run', str(bad))
        self.assertEqual(result.returncode, 1)
        status = self.cli('als-flow', 'status')
        self.assertIn('status failed', status.stdout)

    def test_script_args_and_env_cleared(self):
        echo = self.project / 'echo.py'
        echo.write_text('import os, sys\nprint(sys.argv[1:], os.environ.get("ALS_FLOW_RESUME"))\n')
        result = self.cli('als-flow', 'run', str(echo), '--', 'x', '--y', env={'ALS_FLOW_RESUME': 'stale'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("['x', '--y'] None", result.stdout)


class AgentCliCase(FakeCodexMixin, unittest.TestCase):
    def test_start_send_resume_uses_session(self):
        start = self.cli('als-agent', 'start', 'alice', '--type', 'reader', 'map the repo')
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertIn('reply: map the repo', start.stdout)
        first = self.calls()[0]
        self.assertNotIn('--ephemeral', first['argv'])
        self.assertIn('-m', first['argv'])
        self.assertIn('alice', first['stdin'])
        self.assertIn('You are the READER role.', first['stdin'])
        record = json.loads((self.project / '_workspace' / 'agents' / 'alice.json').read_text())
        session = record['session_id']
        self.assertEqual(session, first['session'])
        self.assertEqual(record['agent_type'], 'reader')
        self.assertEqual(record['cwd'], str(self.project))
        for key in ('name', 'started', 'usage'):
            self.assertIn(key, record)

        send = self.cli('als-agent', 'send', 'alice', '--from', 'bob', 'check module X')
        self.assertEqual(send.returncode, 0, send.stderr)
        self.assertIn('reply: [from bob] check module X', send.stdout)
        second = self.calls()[1]
        argv = second['argv']
        self.assertEqual(argv[:2], ['exec', 'resume'])
        self.assertEqual(argv[-2:], [session, '-'])
        self.assertNotIn('-s', argv)
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertIn('model_reasoning_effort="max"', argv)
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-test-luna')
        self.assertIn('--json', argv)
        self.assertEqual(second['session'], session)
        self.assertEqual(second['cwd'], str(self.project))

        piped = self.cli('als-agent', 'send', 'alice', '-', stdin='from stdin\n')
        self.assertEqual(piped.returncode, 0, piped.stderr)
        self.assertIn('reply: from stdin', piped.stdout)
        log = (self.project / '_workspace' / 'agents' / 'alice.log.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(l)['kind'] for l in log], ['start', 'send', 'send'])
        self.assertEqual(json.loads(log[1])['from'], 'bob')

        show = self.cli('als-agent', 'show', 'alice')
        self.assertEqual(show.returncode, 0)
        self.assertIn('reply: from stdin', show.stdout)
        self.assertIn('input 300', show.stdout)
        listing = self.cli('als-agent', 'list')
        self.assertIn('alice', listing.stdout)
        self.assertIn('turns 3', listing.stdout)

    def test_duplicate_name_refused_unless_replace(self):
        self.assertEqual(self.cli('als-agent', 'start', 'a1', '--type', 'reader', 'hi').returncode, 0)
        dup = self.cli('als-agent', 'start', 'a1', '--type', 'reader', 'again')
        self.assertEqual(dup.returncode, 2)
        self.assertIn('already exists', dup.stderr)
        self.assertEqual(len(self.calls()), 1)
        rep = self.cli('als-agent', 'start', 'a1', '--type', 'als_writer', '--replace', 'new')
        self.assertEqual(rep.returncode, 0, rep.stderr)
        record = json.loads((self.project / '_workspace' / 'agents' / 'a1.json').read_text())
        self.assertEqual(record['agent_type'], 'writer')
        self.assertEqual(record['session_id'], self.calls()[1]['session'])

    def test_agent_errors_and_exit_codes(self):
        self.assertEqual(self.cli('als-agent').returncode, 2)
        self.assertEqual(self.cli('als-agent', 'start', 'x', 'no type').returncode, 2)
        self.assertEqual(self.cli('als-agent', 'start', 'x', '--type', 'nope', 'hi').returncode, 2)
        self.assertEqual(self.cli('als-agent', 'start', '../evil', '--type', 'reader', 'hi').returncode, 2)
        self.assertEqual(self.cli('als-agent', 'send', 'ghost', 'hi').returncode, 2)
        self.assertEqual(self.cli('als-agent', 'show', 'ghost').returncode, 2)
        failed = self.cli('als-agent', 'start', 'f', '--type', 'reader', 'hi', env={'FAKE_CODEX_FAIL': '1'})
        self.assertEqual(failed.returncode, 1)
        self.assertFalse((self.project / '_workspace' / 'agents' / 'f.json').exists())
        nothread = self.cli('als-agent', 'start', 'g', '--type', 'reader', 'hi', env={'FAKE_CODEX_NO_THREAD': '1'})
        self.assertEqual(nothread.returncode, 1)
        self.assertEqual(self.cli('als-agent', 'start', 'ok', '--type', 'reader', 'hi').returncode, 0)
        send = self.cli('als-agent', 'send', 'ok', 'hi', env={'FAKE_CODEX_FAIL': '1'})
        self.assertEqual(send.returncode, 1)
        self.assertEqual(self.cli('als-agent', 'list').returncode, 0)

    def test_agent_cwd_option(self):
        sub = self.project / 'pkg'
        sub.mkdir()
        result = self.cli('als-agent', 'start', 'c', '--type', 'reader', '--cwd', str(sub), 'hi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cli('als-agent', 'send', 'c', 'more').returncode, 0)
        first, second = self.calls()
        self.assertEqual(first['argv'][first['argv'].index('--cd') + 1], str(sub))
        self.assertEqual(second['cwd'], str(sub))


class TaskCliCase(FakeCodexMixin, unittest.TestCase):
    def task(self, *args):
        return self.cli('als-task', *args)

    def test_add_update_deps_blocked(self):
        self.assertEqual(self.task('add', 'design').stdout.strip(), '1')
        self.assertEqual(self.task('add', 'build', '--owner', 'alice', '--deps', '1').stdout.strip(), '2')
        self.assertEqual(self.task('add', 'ship', '--deps', '1,2').stdout.strip(), '3')
        listing = self.task('list').stdout.splitlines()
        self.assertIn('blocked-by 1', listing[1])
        self.assertIn('alice', listing[1])
        self.assertIn('blocked-by 1,2', listing[2])
        self.assertNotIn('blocked-by', listing[0])
        result = self.task('update', '1', '--status', 'done', '--note', 'approved')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(self.task('list', '--json').stdout)
        self.assertEqual(rows[1]['blocked_by'], [])
        self.assertEqual(rows[2]['blocked_by'], [2])
        self.assertEqual(rows[0]['notes'][0]['text'], 'approved')
        self.task('update', '2', '--status', 'in_progress', '--owner', 'bob')
        rows = json.loads(self.task('list', '--json').stdout)
        self.assertEqual((rows[1]['status'], rows[1]['owner']), ('in_progress', 'bob'))

    def test_task_errors(self):
        self.assertEqual(self.task('list').stdout.strip(), 'no tasks')
        self.assertEqual(self.task('add', 'x', '--deps', '9').returncode, 1)
        self.assertEqual(self.task('add', 'x', '--deps', 'a').returncode, 2)
        self.assertEqual(self.task('update', '5', '--status', 'done').returncode, 1)
        self.task('add', 'x')
        self.assertEqual(self.task('update', '1', '--status', 'weird').returncode, 2)
        self.assertEqual(self.task('update', '1').returncode, 2)
        self.assertEqual(self.task().returncode, 2)

    def test_concurrent_adds_are_serialised(self):
        results = []

        def add(n):
            results.append(self.task('add', 'task %d' % n))
        threads = [threading.Thread(target=add, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertEqual(sorted(int(r.stdout) for r in results), list(range(1, 9)))
        rows = json.loads(self.task('list', '--json').stdout)
        self.assertEqual(len(rows), 8)


if __name__ == '__main__':
    unittest.main()
