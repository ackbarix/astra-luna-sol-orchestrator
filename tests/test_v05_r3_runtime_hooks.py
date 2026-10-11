"""ALS 0.5.0 round 3: spawn tool names, unprepared plans and SessionStart guidance."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from als_harness import hooks
from als_harness.common import HarnessError
from als_harness.engine import Engine
from als_harness.state import Store


class HookAndPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        self.s = Store(self.project)
        self.e = Engine(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_spawn_name_matches_installed_matcher_only(self):
        self.assertTrue(hooks.spawn_name('spawn_agent'))
        self.assertTrue(hooks.spawn_name('multi_agent.spawn_agent'))
        self.assertFalse(hooks.spawn_name('Agent'))
        self.assertFalse(hooks.spawn_name('tools.Agent'))
        self.assertFalse(hooks.spawn_name('spawn_agents'))

    def test_namespaced_spawn_reserves(self):
        rid = self.e.start({'goal': 'g', 'tasks': [{'id': 'one', 'role': 'als_explorer'}]}, 'sid')
        out = hooks.handle(self.s, {'hook_event_name': 'PreToolUse', 'session_id': 'sid', 'cwd': str(self.project),
                                    'tool_name': 'multi_agent.spawn_agent', 'tool_use_id': 'c1',
                                    'tool_input': {'message': 'ALS_TASK=one\nscan', 'agent_type': 'als_explorer'}})
        self.assertEqual(out, {})
        self.assertEqual(self.s.task(rid, 'one')['status'], 'reserved')

    def test_unprepared_draft_explains_prepare(self):
        with self.assertRaises(HarnessError) as ctx:
            self.e.start({'goal': 'g', 'tasks': [{'id': 'one', 'kind': 'exploration'}]})
        message = str(ctx.exception)
        self.assertIn('plan not prepared', message)
        self.assertIn('als prepare --plan FILE > prepared.json', message)
        self.assertIn('als start --plan prepared.json', message)

    def test_explicit_unknown_role_still_rejected(self):
        with self.assertRaises(HarnessError) as ctx:
            self.e.start({'goal': 'g', 'tasks': [{'id': 'one', 'role': 'nope'}]})
        self.assertIn('Unknown role', str(ctx.exception))

    def test_session_start_mentions_prepare_then_start(self):
        out = hooks.handle(self.s, {'hook_event_name': 'SessionStart', 'session_id': 'sid',
                                    'cwd': str(self.project), 'source': 'startup'})
        text = out['hookSpecificOutput']['additionalContext']
        self.assertIn('prepare --plan', text)
        self.assertLess(text.index('prepare --plan'), text.index('start --plan'))


if __name__ == '__main__':
    unittest.main()
