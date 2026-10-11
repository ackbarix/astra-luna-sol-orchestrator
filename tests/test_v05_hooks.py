"""ALS 0.5.0 hook audit regressions: synthetic payloads only, no Codex or model calls."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from als_harness.state import Store
from als_harness.engine import Engine
from als_harness.common import HarnessError
from als_harness import hooks


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


if __name__ == '__main__':
    unittest.main()
