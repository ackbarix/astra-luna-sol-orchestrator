"""ALS 0.5.0 rollout-ingest regressions: synthetic local JSONL only, no Codex calls."""
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
from als_harness import hooks, report, usage


def stamp():
    return (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()


def meta(thread, **extra):
    return {'type': 'session_meta', 'payload': {'id': thread, 'instructions': 'SECRET-INSTRUCTIONS',
                                                'cwd': '/private/cwd', 'git': {'branch': 'secret'}, **extra}}


def record(rid, tokens=10):
    return {'type': 'token_usage_record', 'timestamp': stamp(),
            'payload': {'model': 'gpt-6-luna', 'effort': 'max', 'response_id': rid,
                        'usage': {'input_tokens': tokens, 'output_tokens': 1}}}


class UsageAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.project = base / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        self.home = base / 'codex'
        (self.home / 'sessions').mkdir(parents=True)
        self.log = self.home / 'sessions/rollout-a.jsonl'
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.home)})
        self.env.start()
        self.s = Store(self.project)
        self.e = Engine(self.s)

    def tearDown(self):
        self.env.stop()
        self.s.close()
        self.tmp.cleanup()

    def write(self, rows, mode='w'):
        with self.log.open(mode) as f:
            f.write(''.join(json.dumps(x) + '\n' for x in rows))

    def tokens(self, rid):
        return self.e.budget(self.s.run(rid))['confirmed_tokens']

    def cursor(self, rid, path=None):
        return self.s.get('cursors', str(path or self.log) + '#als-run=' + rid, 'path')

    def run_with_child(self):
        r = self.e.start({'goal': 'usage', 'tasks': [{'id': 'one', 'role': 'als_explorer'}]}, 'sid')
        return r

    # 7a
    def test_oversized_line_skipped_and_cursor_advances(self):
        r = self.e.start({'goal': 'usage'}, 'sid')
        with self.log.open('w') as f:
            f.write(json.dumps(meta('sid')) + '\n')
            f.write('{"type":"x","payload":"' + 'a' * (1024 * 1024 + 10) + '"}\n')
            f.write(json.dumps(record('r1')) + '\n')
        for _ in range(3):
            usage.sync_path(self.s, self.s.run(r), self.log)
        self.assertEqual(self.tokens(r), 11)
        warnings = [w for w in self.s.run(r)['warnings'] if 'oversized' in w]
        self.assertEqual(warnings, ['oversized rollout line skipped'])
        self.assertEqual(self.cursor(r)['offset'], self.log.stat().st_size)

    def test_oversized_partial_line_resumes_skipping(self):
        r = self.e.start({'goal': 'usage'}, 'sid')
        with self.log.open('w') as f:
            f.write(json.dumps(meta('sid')) + '\n')
            f.write('{"type":"x","payload":"' + 'a' * (1024 * 1024 + 10))
        usage.sync_path(self.s, self.s.run(r), self.log)
        with self.log.open('a') as f:
            f.write('b' * 100 + '"}\n' + json.dumps(record('r1')) + '\n')
        usage.sync_path(self.s, self.s.run(r), self.log)
        self.assertEqual(self.tokens(r), 11)

    # 7b
    def test_unbound_thread_not_skipped_before_bind(self):
        r = self.run_with_child()
        self.write([meta('kid'), record('k1'), record('k2')])
        usage.sync_path(self.s, self.s.run(r), self.log)
        self.assertEqual(self.tokens(r), 0)
        self.assertEqual(sum('unbound thread' in w for w in self.s.run(r)['warnings']), 1)
        self.e.reserve(r, 'one', 'call')
        self.e.bind(r, 'call', 'kid')
        usage.sync_path(self.s, self.s.run(r), self.log)
        self.assertEqual(self.tokens(r), 22)

    def test_known_threads_reread_after_miss(self):
        r = self.run_with_child()
        self.write([meta('kid'), record('k1')])
        real = usage.known_threads
        calls = []

        def late_bind(store, run):
            calls.append(1)
            if len(calls) == 2:
                self.e.reserve(r, 'one', 'call')
                self.e.bind(r, 'call', 'kid')
            return real(store, run)
        with patch.object(usage, 'known_threads', late_bind):
            usage.sync_path(self.s, self.s.run(r), self.log)
        self.assertEqual(self.tokens(r), 11)

    # 7c
    def test_cursor_keeps_only_thread_lineage(self):
        r = self.e.start({'goal': 'usage'}, 'sid')
        self.write([meta('sid', source={'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}), record('r1')])
        usage.sync_path(self.s, self.s.run(r), self.log)
        cursor = self.cursor(r)
        self.assertEqual(cursor['meta'], {'id': 'sid', 'parent_thread_id': 'root'})
        self.assertNotIn('SECRET', json.dumps(cursor))

    # 7d
    def test_truncate_and_regrow_is_detected(self):
        r = self.e.start({'goal': 'usage'}, 'sid')
        self.write([meta('sid'), record('r1'), record('r2')])
        usage.sync_path(self.s, self.s.run(r), self.log)
        self.assertEqual(self.tokens(r), 22)
        # Same inode, rewritten from scratch and grown past the old offset.
        with self.log.open('r+') as f:
            f.truncate(0)
            f.write(''.join(json.dumps(x) + '\n' for x in [meta('sid', note='v2'), record('n1', 100),
                                                            record('n2', 100), record('n3', 100)]))
        self.assertGreater(self.log.stat().st_size, self.cursor(r)['offset'])
        usage.sync_path(self.s, self.s.run(r), self.log)
        self.assertEqual(self.tokens(r), 22 + 303)

    # 7e
    def test_registered_base_used_instead_of_hook_env(self):
        r = self.e.start({'goal': 'usage'}, 'sid')
        self.write([meta('sid'), record('r1')])
        hooks.handle(self.s, {'hook_event_name': 'SessionStart', 'session_id': 'sid', 'cwd': str(self.project),
                              'transcript_path': str(self.log), 'source': 'startup'})
        receipt = self.s.get('sessions', 'sid')
        self.assertEqual(receipt['transcript_bases'][str(self.log)], str((self.home / 'sessions').resolve()))
        with patch.dict(os.environ, {'CODEX_HOME': str(Path(self.tmp.name) / 'other-home')}):
            results = usage.sync(self.e, r)
        self.assertNotIn('warning', results[0])
        self.assertEqual(self.tokens(r), 11)

    def test_registered_base_still_confines_path(self):
        r = self.e.start({'goal': 'usage'}, 'sid')
        outside = self.project / 'rollout.jsonl'
        outside.write_text(json.dumps(meta('sid')) + '\n')
        with self.s.tx():
            self.s.session('sid', {'seen': [], 'transcripts': [str(outside)],
                                   'transcript_bases': {str(outside): str((self.home / 'sessions').resolve())}})
        results = usage.sync(self.e, r)
        self.assertIn('warning', results[0])

    def test_subagent_stop_registers_base_and_syncs(self):
        r = self.run_with_child()
        self.e.reserve(r, 'one', 'call')
        self.e.bind(r, 'call', 'kid')
        child = self.home / 'sessions/rollout-kid.jsonl'
        child.write_text(json.dumps(meta('kid')) + '\n' + json.dumps(record('k1')) + '\n')
        hooks.handle(self.s, {'hook_event_name': 'SubagentStop', 'session_id': 'sid', 'cwd': str(self.project),
                              'agent_id': 'kid', 'agent_type': 'als_explorer', 'agent_transcript_path': str(child),
                              'stop_hook_active': False, 'turn_id': 't'})
        self.assertEqual(self.s.get('workers', 'kid')['transcript_base'], str((self.home / 'sessions').resolve()))
        self.assertEqual(self.tokens(r), 11)

    # 7f
    def test_pre_tool_use_sync_is_bounded(self):
        r = self.e.start({'goal': 'usage'}, 'sid')
        self.write([meta('sid')] + [record('r' + str(n)) for n in range(4000)])
        with self.s.tx():
            self.s.session('sid', {'seen': [], 'transcripts': [str(self.log)]})
        results = usage.sync(self.e, r, limit=usage.PRE_TOOL_SYNC_BYTES)
        self.assertLessEqual(results[0]['bytes_read'], usage.PRE_TOOL_SYNC_BYTES + 4096)
        self.assertLess(self.cursor(r)['offset'], self.log.stat().st_size)

    def test_budget_reports_usage_synced_at(self):
        r = self.e.start({'goal': 'usage'}, 'sid')
        self.write([meta('sid'), record('r1')])
        usage.sync_path(self.s, self.s.run(r), self.log)
        synced = self.s.run(r)['last_usage_sync']
        self.assertEqual(report.report(self.e, r)['budget']['usage_synced_at'], synced)

    # Item 9
    def test_report_schema_2_keeps_keys(self):
        r = self.e.start({'goal': 'usage'}, 'sid')
        data = report.report(self.e, r)
        self.assertEqual(data['schema_version'], 2)
        for key in ('run', 'goal', 'status', 'mode', 'session', 'workers', 'checks', 'budget',
                    'usage_by_thread_model_effort', 'efficiency', 'measurement', 'purpose', 'last_usage_sync',
                    'closed_at', 'warnings', 'completion_problems', 'billing_verified', 'note'):
            self.assertIn(key, data)


if __name__ == '__main__':
    unittest.main()
