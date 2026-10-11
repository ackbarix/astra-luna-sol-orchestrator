"""ALS 0.5.0 round 3: HEAD rollout cursors start at EOF; sync budget rotates. Synthetic JSONL only."""
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from als_harness.state import Store
from als_harness.engine import Engine
from als_harness import hooks, usage


def stamp(seconds=1):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def meta(thread):
    return {'type': 'session_meta', 'payload': {'id': thread}}


def record(rid, tokens=10, when=None):
    return {'type': 'token_usage_record', 'timestamp': when or stamp(),
            'payload': {'model': 'gpt-6-astra', 'effort': 'medium', 'response_id': rid,
                        'usage': {'input_tokens': tokens, 'output_tokens': 0}}}


def filler(n_bytes):
    line = json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'text': 'h' * 1000}}) + '\n'
    return line * (n_bytes // len(line) + 1)


class EofCursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.project = base / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        (self.project / 'source.py').write_text('x=1\n')
        self.home = base / 'codex'
        (self.home / 'sessions').mkdir(parents=True)
        self.head = self.home / 'sessions/rollout-a-head.jsonl'
        self.worker = self.home / 'sessions/rollout-b-worker.jsonl'
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.home)})
        self.env.start()
        self.s = Store(self.project)
        self.e = Engine(self.s)

    def tearDown(self):
        self.env.stop()
        self.s.close()
        self.tmp.cleanup()

    def append(self, path, rows='', raw=''):
        with path.open('a') as f:
            f.write(raw + ''.join(json.dumps(x) + '\n' for x in rows))

    def session_start(self, path=None, sid='sid'):
        return hooks.handle(self.s, {'hook_event_name': 'SessionStart', 'session_id': sid, 'cwd': str(self.project),
                                     'transcript_path': str(path or self.head), 'source': 'resume'})

    def cursor(self, rid, path=None):
        return self.s.get('cursors', str(path or self.head) + '#als-run=' + rid, 'path')

    def tokens(self, rid):
        return self.e.budget(self.s.run(rid))['confirmed_tokens']

    def long_history(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.append(self.head, [meta('sid')])
        self.append(self.head, raw=filler(2 * 1024 * 1024))
        # Codex writes a turn_context every turn; the latest one is near EOF.
        self.append(self.head, [{'type': 'turn_context', 'payload': {'model': 'gpt-6-astra', 'effort': 'medium'}},
                                record('old', 5000, old)])

    def test_resumed_session_spawn_sees_new_usage_over_budget(self):
        self.long_history()
        self.session_start()
        rid = self.e.start({'goal': 'g', 'budget': {'tokens': 100, 'seconds': 0},
                            'tasks': [{'id': 'one', 'role': 'als_explorer', 'estimate_tokens': 10}]}, 'sid')
        size = self.head.stat().st_size
        self.assertEqual(self.cursor(rid)['offset'], size)
        self.append(self.head, [record('new', 1000)])
        out = hooks.handle(self.s, {'hook_event_name': 'PreToolUse', 'session_id': 'sid', 'cwd': str(self.project),
                                    'tool_name': 'spawn_agent', 'tool_use_id': 'c1',
                                    'tool_input': {'message': 'ALS_TASK=one\nscan', 'agent_type': 'als_explorer'}})
        self.assertIn('soft budget reached', out.get('systemMessage', ''))
        self.assertEqual(self.tokens(rid), 1000)
        self.assertEqual(self.s.task(rid, 'one')['status'], 'pending')

    def test_anchor_keeps_lineage_and_context(self):
        self.long_history()
        self.session_start()
        rid = self.e.start({'goal': 'g'}, 'sid')
        cursor = self.cursor(rid)
        self.assertEqual(cursor['meta']['id'], 'sid')
        self.assertEqual(cursor['context'], {'model': 'gpt-6-astra', 'effort': 'medium'})

    def test_anchor_uses_last_complete_line(self):
        self.append(self.head, [meta('sid')], raw='')
        complete = self.head.stat().st_size
        self.append(self.head, raw='{"type":"token_usage_record","partial')
        self.session_start()
        rid = self.e.start({'goal': 'g'}, 'sid')
        self.assertEqual(self.cursor(rid)['offset'], complete)

    def test_existing_cursor_not_moved_by_resume_or_session_start(self):
        self.append(self.head, [meta('sid')])
        self.session_start()
        rid = self.e.start({'goal': 'g'}, 'sid')
        before = self.cursor(rid)['offset']
        self.append(self.head, [record('r1', 7)])
        self.session_start()
        self.e.pause(rid, 'x')
        self.e.resume(rid, session='sid')
        self.assertEqual(self.cursor(rid)['offset'], before)
        usage.sync(self.e, rid)
        self.assertEqual(self.tokens(rid), 7)

    def test_session_start_registers_new_transcript_at_eof(self):
        self.append(self.head, [meta('sid')])
        self.session_start()
        rid = self.e.start({'goal': 'g'}, 'sid')
        second = self.home / 'sessions/rollout-c-resumed.jsonl'
        self.append(second, [meta('sid')])
        self.append(second, raw=filler(64 * 1024))
        self.session_start(second)
        self.assertEqual(self.cursor(rid, second)['offset'], second.stat().st_size)

    def test_resume_session_anchors_new_session(self):
        rid = self.e.start({'goal': 'g'})
        self.append(self.head, [meta('sid2')])
        self.append(self.head, raw=filler(64 * 1024))
        self.session_start(sid='sid2')
        self.e.pause(rid, 'x')
        self.e.resume(rid, session='sid2')
        self.assertEqual(self.cursor(rid)['offset'], self.head.stat().st_size)

    def test_budget_rotates_so_head_cannot_starve_workers(self):
        self.append(self.head, [meta('sid')])
        self.session_start()
        rid = self.e.start({'goal': 'g', 'tasks': [{'id': 'one', 'role': 'als_explorer'}]}, 'sid')
        self.e.reserve(rid, 'one', 'c1')
        self.e.bind(rid, 'c1', 'kid')
        hooks.update_worker(self.s, 'kid', 'sid', transcript=str(self.worker),
                            transcript_base=str(usage.sessions_base()))
        self.append(self.worker, [meta('kid'), record('k1', 33)])
        # Busy HEAD: far more new bytes than one PreToolUse budget.
        self.append(self.head, raw=filler(4 * usage.PRE_TOOL_SYNC_BYTES))
        for _ in range(2):
            usage.sync(self.e, rid, limit=usage.PRE_TOOL_SYNC_BYTES)
        self.assertEqual(self.tokens(rid), 33)


if __name__ == '__main__':
    unittest.main()
