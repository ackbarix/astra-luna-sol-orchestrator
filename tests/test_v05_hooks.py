"""ALS 0.5.0 hook audit regressions: synthetic payloads only, no Codex or model calls."""
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from als_harness.state import Store
from als_harness.engine import Engine
from als_harness.common import HarnessError
from als_harness import cli, hooks


class HookAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        (self.project / 'source.py').write_text('x=1\n')
        self.s = Store(self.project)
        self.e = Engine(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def plan(self, tasks=None):
        return {'goal': 'hook audit', 'tasks': tasks or [], 'checks': [], 'criteria': []}

    def task(self, key='one', path='source.py'):
        return {'id': key, 'role': 'als_worker', 'write_paths': [path] if path else [], 'checks': []}

    def event(self, event, sid='sid', **kwargs):
        payload = {'hook_event_name': event, 'session_id': sid, 'cwd': str(self.project), 'turn_id': 'turn',
                   'stop_hook_active': False, 'tool_use_id': 'call', 'tool_input': {}}
        payload.update(kwargs)
        return hooks.handle(self.s, payload)

    def guarded(self, tasks=None):
        for name in ('SessionStart', 'PreToolUse', 'PostToolUse', 'Stop'):
            self.event(name)
        return self.e.start(self.plan(tasks), 'sid', 'guarded', True)

    def spawn(self, **kwargs):
        tool_input = {'message': 'ALS_TASK=one\nimplement', 'agent_type': 'als_worker'}
        return self.event('PreToolUse', tool_name='spawn_agent', tool_input=tool_input, **kwargs)

    # Item 2: child-origin spawns
    def test_guarded_child_spawn_denied_without_reservation(self):
        r = self.guarded([self.task()])
        out = self.spawn(agent_id='kid', agent_type='als_worker', tool_use_id='child-call')
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('child agents may not spawn', out['hookSpecificOutput']['permissionDecisionReason'])
        self.assertEqual(self.s.task(r, 'one')['status'], 'pending')
        self.assertEqual(self.s.task(r, 'one')['attempts'], [])
        attempt = self.s.get('events', 'child-spawn:' + r + ':child-call', 'key')
        self.assertEqual(attempt['parent_agent_id'], 'kid')

    def test_observe_child_spawn_warns_without_reservation(self):
        r = self.e.start(self.plan([self.task()]), 'sid')
        out = self.spawn(agent_id='kid')
        self.assertIn('child agents may not spawn', out['systemMessage'])
        self.assertNotIn('hookSpecificOutput', out)
        self.assertEqual(self.s.task(r, 'one')['attempts'], [])

    def test_parent_spawn_still_reserves(self):
        r = self.guarded([self.task()])
        self.assertEqual(self.spawn(), {})
        self.assertEqual(self.s.task(r, 'one')['status'], 'reserved')

    # Item 3: forked worktree children
    def test_child_events_outside_project_kept_for_linked_session(self):
        r = self.e.start(self.plan([self.task()]), 'sid')
        self.e.reserve(r, 'one', 'call')
        self.e.bind(r, 'call', 'kid')
        fork = Path(self.tmp.name) / 'fork'
        fork.mkdir()
        out = self.event('SubagentStart', cwd=str(fork), agent_id='kid', agent_type='als_worker')
        self.assertNotIn('systemMessage', out)
        self.assertEqual(self.s.get('workers', 'kid')['cwd'], str(fork.resolve()))
        self.event('SubagentStop', cwd=str(fork), agent_id='kid', agent_type='als_worker')
        self.assertEqual(self.s.task(r, 'one')['status'], 'awaiting_validation')

    def test_child_spawn_outside_project_still_denied(self):
        r = self.guarded([self.task()])
        fork = Path(self.tmp.name) / 'fork'
        fork.mkdir()
        out = self.spawn(cwd=str(fork), agent_id='kid')
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(self.s.task(r, 'one')['attempts'], [])

    def test_unlinked_outside_events_still_ignored(self):
        self.e.start(self.plan(), 'sid')
        out = self.event('SubagentStart', sid='other', cwd='/', agent_id='kid', agent_type='als_worker')
        self.assertIn('outside the installed project', out['systemMessage'])
        self.assertIsNone(self.s.get('workers', 'kid'))
        out = self.event('SessionEnd', cwd='/')
        self.assertIn('systemMessage', out)
        self.assertEqual(self.s.for_session('sid')['status'], 'running')

    # Item 4: deny reason
    def test_deny_never_empty(self):
        for message in ('', '   ', None):
            reason = hooks.deny(message)['hookSpecificOutput']['permissionDecisionReason']
            self.assertTrue(reason.strip())

    def test_engine_error_without_text_denies_with_reason(self):
        self.guarded([self.task()])
        with patch.object(Engine, 'reserve', side_effect=HarnessError('')):
            out = self.spawn()
        self.assertTrue(out['hookSpecificOutput']['permissionDecisionReason'].strip())
        with patch.object(Engine, 'reserve', return_value={'allowed': False, 'reasons': []}):
            out = self.spawn()
        self.assertTrue(out['hookSpecificOutput']['permissionDecisionReason'].strip())

    # Item 5: no write transaction for repeat receipts
    def test_repeat_receipt_skips_write_transaction(self):
        self.event('PreToolUse', tool_name='shell')
        calls = []
        real = Store.tx

        def counting(store):
            calls.append(1)
            return real(store)
        with patch.object(Store, 'tx', counting):
            self.event('PreToolUse', tool_name='shell')
        self.assertEqual(calls, [])
        with patch.object(Store, 'tx', counting):
            self.event('PostToolUse', tool_name='shell')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.s.get('sessions', 'sid')['seen'], ['PostToolUse', 'PreToolUse'])

    # Item 6: bind results are surfaced
    def test_post_tool_use_warns_when_not_bound(self):
        r = self.e.start(self.plan([self.task()]), 'sid')
        self.e.reserve(r, 'one', 'call')
        self.e.finish(r, 'one', 'done', stopped=True)
        out = self.event('PostToolUse', tool_name='spawn_agent', tool_response={'agent_id': 'kid'})
        self.assertIn('not bound', out['systemMessage'])
        self.assertIn('attempt already', out['systemMessage'])

    def test_post_tool_use_bound_is_silent(self):
        r = self.e.start(self.plan([self.task()]), 'sid')
        self.e.reserve(r, 'one', 'call')
        self.assertEqual(self.event('PostToolUse', tool_name='spawn_agent', tool_response={'agent_id': 'kid'}), {})
        self.assertEqual(self.s.task(r, 'one')['status'], 'running')



class SessionResumeTests(unittest.TestCase):
    """Item 8: SessionEnd keeps pausing; the next SessionStart prints the exact resume command."""
    setUp = HookAuditTests.setUp
    tearDown = HookAuditTests.tearDown
    plan = HookAuditTests.plan
    task = HookAuditTests.task
    event = HookAuditTests.event

    def command(self, out):
        text = out['hookSpecificOutput']['additionalContext']
        line = next(x for x in text.split('\n') if ' resume --run ' in x)
        return shlex.split(line.strip())

    def parsed(self, argv):
        start = next(i for i, x in enumerate(argv) if x.endswith('harness.py'))
        return cli.parser().parse_args(argv[start + 1:])

    def test_new_session_gets_resume_command_with_workers_stopped(self):
        r = self.e.start(self.plan([self.task()]), 'old')
        self.e.reserve(r, 'one', 'c')
        self.event('SessionEnd', sid='old')
        self.assertEqual(self.s.run(r)['status'], 'paused')
        args = self.parsed(self.command(self.event('SessionStart', sid='new', source='startup')))
        self.assertEqual((args.cmd, args.run, args.session, args.workers_stopped), ('resume', r, 'new', True))
        self.assertEqual(args.project.resolve(), self.project.resolve())
        self.assertEqual(self.s.run(r)['status'], 'paused')
        self.e.resume(args.run, args.accept_drift, args.workers_stopped, args.session)
        self.assertEqual(self.s.for_session('new')['id'], r)

    def test_resume_command_without_active_tasks(self):
        r = self.e.start(self.plan([self.task()]), 'old')
        self.event('SessionEnd', sid='old')
        args = self.parsed(self.command(self.event('SessionStart', sid='new')))
        self.assertEqual((args.run, args.session, args.workers_stopped), (r, 'new', False))

    def test_same_session_restart_is_not_auto_reattached(self):
        r = self.e.start(self.plan([self.task()]), 'sid')
        self.event('SessionEnd')
        for source in ('resume', 'compact', 'clear'):
            out = self.event('SessionStart', source=source)
            args = self.parsed(self.command(out))
            self.assertEqual((args.run, args.session), (r, 'sid'))
            self.assertEqual(self.s.run(r)['status'], 'paused')

    def test_user_pause_does_not_advertise_session_resume(self):
        r = self.e.start(self.plan([self.task()]), 'old')
        self.e.pause(r, 'human review')
        text = self.event('SessionStart', sid='new')['hookSpecificOutput']['additionalContext']
        self.assertNotIn('resume --run', text)
        self.assertIn(r, text)

class ScopeHintTests(unittest.TestCase):
    """SessionStart guidance must name the command that works in this scope."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        self.env = patch.dict('os.environ', {'ALS_STATE_HOME': str(Path(self.tmp.name) / 'state')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def start_text(self, scope):
        store = Store(self.project, scope=scope)
        try:
            out = hooks.handle(store, {'hook_event_name': 'SessionStart', 'session_id': 'sid',
                                       'cwd': str(self.project), 'source': 'startup'})
        finally:
            store.close()
        return out['hookSpecificOutput']['additionalContext']

    def test_global_scope_names_als(self):
        text = self.start_text('global')
        self.assertIn('`als start --plan FILE --session sid`', text)
        self.assertNotIn('harness.py', text)

    def test_project_scope_names_installed_harness(self):
        # Without `als` on PATH: the absolute harness path and the running interpreter.
        with patch.object(hooks.shutil, 'which', return_value=None):
            text = self.start_text('project')
        self.assertIn(hooks.harness_invocation() + ' start', text)
        self.assertNotIn('python3 .astra-luna-sol/harness.py', text)

    def test_global_resume_command_uses_als(self):
        store = Store(self.project, scope='global')
        try:
            rid = Engine(store).start({'goal': 'g', 'tasks': [], 'checks': [], 'criteria': []}, session='old')
            self.assertTrue(hooks.resume_command(store, store.run(rid), 'new').startswith('als resume --run '))
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
