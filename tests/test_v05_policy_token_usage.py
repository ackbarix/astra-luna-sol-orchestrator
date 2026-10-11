"""0.5.0 token_usage.py regressions: synthetic rollouts in temp dirs only."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/token_usage.py'
spec = importlib.util.spec_from_file_location('usage_v05', SCRIPT)
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)


def tokens(i=100, o=10):
    return dict(input_tokens=i, cached_input_tokens=0, output_tokens=o,
                reasoning_output_tokens=0, total_tokens=i + o)


def context(model='gpt-6-astra', effort='medium', turn=None):
    return {'type': 'turn_context', 'payload': {'model': model, 'effort': effort, 'turn_id': turn}}


def record(i=100, o=10, ts='2026-10-08T12:00:01Z', **fields):
    return {'type': 'token_usage_record', 'timestamp': ts, 'payload': {'usage': tokens(i, o), **fields}}


def cumulative(i, o, ts='2026-10-08T12:00:02Z'):
    return {'type': 'event_msg', 'timestamp': ts,
            'payload': {'type': 'token_count', 'info': {'total_token_usage': tokens(i, o)}}}


class Fixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.dir = self.home / 'sessions'
        self.archive = self.home / 'archived_sessions'
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, ident='root', root='root', role='root', day='08', events=None, started=None,
            base=None, **meta):
        source = 'cli' if role == 'root' else {'subagent': {'thread_spawn': {'agent_role': role}}}
        obj = {'type': 'session_meta', 'payload': {
            'id': ident, 'session_id': root, 'source': source, 'cwd': '/proj',
            'timestamp': started or f'2026-10-{day}T12:00:00Z', **meta}}
        folder = (base or self.dir) / '2026' / '10' / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'rollout-{ident}.jsonl'
        path.write_text('\n'.join(json.dumps(x) for x in [obj, *(events or [])]) + '\n')
        return path

    def raw(self, name, text, day='08'):
        folder = self.dir / '2026' / '10' / day
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--sessions-dir', str(self.dir), *args],
                              capture_output=True, text=True)

    def report(self, *args, code=0):
        r = self.cli(*args, '--format', 'json')
        self.assertEqual(r.returncode, code, r.stderr)
        return json.loads(r.stdout)


class IndexWarningIsolationTests(Fixture, unittest.TestCase):
    """Item 1: warnings of unrelated rollouts must not leak into a clean lineage."""

    def unrelated_noise(self):
        self.raw('rollout-garbage.jsonl', '{"bad":\n')                      # missing_session_meta
        self.raw('rollout-nometa.jsonl', json.dumps({'type': 'other'}) + '\n')
        self.put('orphan', 'ghost', 'als_worker', events=[context(), record()])  # missing_root_metadata
        broken = self.put('other', 'other', events=[context(), record()])
        broken.write_text(broken.read_text() + '{"bad":\n')                  # unreadable_line elsewhere

    def test_clean_root_stays_complete_and_strict_passes(self):
        self.put(events=[context(), record()])
        self.unrelated_noise()
        report = self.report('--root', 'root', '--strict')
        self.assertTrue(report['complete'], report['warnings'])
        self.assertEqual(report['warnings'], [])
        joined = ' '.join(report['index_warnings'])
        self.assertIn('missing_session_meta', joined)
        self.assertIn('missing_root_metadata:ghost', joined)
        # Unrelated threads are not fully parsed, so their bad lines are not reported at all.
        self.assertNotIn('unreadable_line:rollout-other', joined)

    def test_latest_clean_session_unaffected(self):
        self.put(events=[context(), record(ts='2026-10-08T13:00:01Z')], started='2026-10-08T13:00:00Z')
        self.unrelated_noise()
        report = self.report('--latest', '--strict')
        self.assertEqual(report['root'], 'root')
        self.assertTrue(report['complete'])

    def test_selected_lineage_warnings_still_reported(self):
        path = self.put(events=[context(), record()])
        path.write_text(path.read_text() + '{"bad":\n')
        self.unrelated_noise()
        report = self.report('--root', 'root', '--strict', code=3)
        self.assertFalse(report['complete'])
        self.assertTrue(any('unreadable_line' in w for w in report['warnings']))
        self.assertFalse(any('ghost' in w for w in report['warnings']))

    def test_missing_root_of_selected_lineage_is_selected_warning(self):
        self.put('orphan', 'ghost', 'als_worker', events=[context(), record()])
        self.put(events=[context(), record()])
        report = self.report('--root', 'ghost', '--strict', code=3)
        self.assertTrue(any('missing_root_metadata:ghost' in w for w in report['warnings']))

    def test_check_runtime_report_not_failed_by_unrelated_files(self):
        roles = {'root': ('gpt-6-astra', 'medium'), 'als_explorer': ('gpt-6-luna', 'medium'),
                 'als_worker': ('gpt-6-luna', 'max'), 'als_hard_worker': ('gpt-6.1-sol', 'high'),
                 'als_reviewer': ('gpt-6.1-sol', 'medium')}
        for role, (model, effort) in roles.items():
            ident = 'root' if role == 'root' else role
            self.put(ident, 'root', role, events=[context(model, effort), record(response_id=ident)])
        self.unrelated_noise()
        out = self.home / 'report.json'
        out.write_text(self.cli('--root', 'root', '--format', 'json').stdout)
        checker = subprocess.run([sys.executable, str(ROOT / 'scripts/check-runtime.py'),
                                  '--project', str(ROOT / 'profiles'), '--report', str(out)],
                                 capture_output=True, text=True)
        data = json.loads(checker.stdout)
        self.assertFalse([p for p in data['problems'] if 'Usage report' in p or 'Missing' in p], data)


class MalformedContextTests(Fixture, unittest.TestCase):
    """Item 2: non-string identity values must not crash the tool."""

    def test_dict_list_int_identity_values(self):
        events = [
            {'type': 'turn_context', 'payload': {'model': {'x': 1}, 'effort': ['high'], 'turn_id': {'a': 1}}},
            record(response_id='a', turn_id={'a': 1}),
            {'type': 'turn_context', 'payload': {'model': 7, 'effort': 'medium', 'turn_id': ['t']}},
            record(ts='2026-10-08T12:00:03Z', response_id='b', turn_id=['t']),
            {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra', 'effort': 3, 'turn_id': 5}},
            record(ts='2026-10-08T12:00:04Z', response_id='c', model={'bad': 1}),
        ]
        self.put(events=events)
        report = self.report('--root', 'root')
        self.assertEqual(report['confirmed_usage']['total_tokens'], 330)
        self.assertTrue(any('invalid_identity_context' in w for w in report['warnings']))
        for row in report['by_model_effort']:
            self.assertIsInstance(row['model'], str)
            self.assertIsInstance(row['effort'], str)
        self.assertEqual(self.cli('--root', 'root').returncode, 0)

    def test_non_string_agent_role_and_parent(self):
        self.put(events=[context(), record()])
        self.put('kid', None, 'als_worker', events=[context(), record()],
                 source={'subagent': {'thread_spawn': {'agent_role': {'x': 1}, 'parent_thread_id': ['root']}}},
                 parent_thread_id={'bad': 1})
        r = self.cli('--list', '--format', 'json')
        self.assertEqual(r.returncode, 0, r.stderr)


class DayScopeTests(Fixture, unittest.TestCase):
    """Item 3: threads without in-day events do not affect that day's completeness."""

    def test_other_day_legacy_thread_ignored(self):
        self.put(events=[context(), record()])
        self.put('legacy', 'legacy', day='05', events=[context(), cumulative(100, 10, ts='2026-10-05T12:00:02Z')])
        report = self.report('--day', '2026-10-08', '--strict')
        self.assertTrue(report['complete'], report['warnings'])
        self.assertEqual([t['id'] for t in report['threads']], ['root'])
        self.assertEqual(report['confirmed_usage']['total_tokens'], 110)

    def test_other_day_warning_thread_ignored(self):
        self.put(events=[context(), record()])
        path = self.put('old', 'old', day='05', events=[context(), record(ts='2026-10-05T12:00:01Z')])
        path.write_text(path.read_text() + '{"bad":\n')
        self.assertTrue(self.report('--day', '2026-10-08', '--strict')['complete'])

    def test_same_day_legacy_thread_still_incomplete(self):
        self.put(events=[context(), record()])
        self.put('legacy', 'legacy', events=[context(), cumulative(100, 10)])
        report = self.report('--day', '2026-10-08', '--strict', code=3)
        self.assertFalse(report['complete'])
        self.assertIn('legacy', [t['id'] for t in report['threads']])


class CwdTests(Fixture, unittest.TestCase):
    """Item 4: --cwd matches the project and its subdirectories on component boundaries."""

    def test_subdirectory_matches_sibling_prefix_does_not(self):
        self.put(events=[context(), record()])
        self.put('sub', 'sub', events=[context(), record()], cwd='/proj/pkg/inner')
        self.put('sib', 'sib', events=[context(), record()], cwd='/project2')
        self.put('none', 'none', events=[context(), record()], cwd=None)
        roots = {s['root'] for s in self.report('--list', '--cwd', '/proj')['sessions']}
        self.assertEqual(roots, {'root', 'sub'})


class ArchivedTests(Fixture, unittest.TestCase):
    """Item 5: archived sessions are opt-in; missing members are flagged."""

    def setUp(self):
        super().setUp()
        self.put(events=[context(), record()], base=self.archive)
        self.put('kid', 'root', 'als_worker', events=[context('gpt-6-luna', 'max'), record(50, 5)])

    def test_off_by_default_with_hint(self):
        report = self.report('--root', 'root', '--strict', code=3)
        self.assertEqual(report['confirmed_usage']['total_tokens'], 55)
        self.assertTrue(any('archived_sessions_not_scanned' in w for w in report['warnings']))

    def test_include_archived(self):
        report = self.report('--root', 'root', '--include-archived', '--strict')
        self.assertEqual(report['confirmed_usage']['total_tokens'], 165)
        self.assertTrue(report['complete'])


class OversizedLineTests(Fixture, unittest.TestCase):
    """Item 6: oversized lines are skipped with a warning and later lines still parse."""

    def test_oversized_line_skipped(self):
        path = self.dir / 'rollout-x.jsonl'
        path.write_bytes(b'{"a":"' + b'x' * 300 + b'"}\n' + json.dumps({'ok': 1}).encode() + b'\n')
        warnings = []
        with patch.object(usage, 'MAX_LINE', 64):
            values = list(usage.objects(path, warnings))
        self.assertEqual(values, [{'ok': 1}])
        self.assertEqual(warnings, ['oversized_line:rollout-x.jsonl:1'])


if __name__ == '__main__':
    unittest.main()
