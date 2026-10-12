"""Tests for als_flow.Flow (the Codex equivalent of a Claude Code Workflow), fake codex only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from als_flow import Flow
from als_flow import common
from test_v10_flow_support import FakeCodexMixin


def option(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


class FlowCase(FakeCodexMixin, unittest.TestCase):
    def flow(self, **kw):
        kw.setdefault('name', 'test-flow')
        kw.setdefault('phases', ['collect', 'verify'])
        with mock.patch('sys.stderr', io.StringIO()):
            return Flow(**kw)

    @contextlib.contextmanager
    def quiet(self):
        with mock.patch('sys.stderr', io.StringIO()), mock.patch('sys.stdout', io.StringIO()):
            yield

    # ------------------------------------------------------------ pins

    def test_role_pins_applied_and_instructions_prepended(self):
        flow = self.flow()
        with self.quiet():
            out = flow.agent('inspect module X', agent_type='reader')
        self.assertEqual(out, 'reply: inspect module X')
        (call,) = self.calls()
        argv = call['argv']
        self.assertEqual(option(argv, '-m'), ['gpt-test-luna'])
        self.assertIn('model_reasoning_effort="max"', option(argv, '-c'))
        self.assertEqual(option(argv, '-s'), ['read-only'])
        self.assertIn('--ephemeral', argv)
        self.assertIn('--json', argv)
        self.assertEqual(argv[-1], '-')
        self.assertEqual(option(argv, '--cd'), [str(self.project)])
        self.assertTrue(call['stdin'].startswith('You are the READER role.'))
        self.assertIn('inspect module X', call['stdin'])

    def test_role_by_file_stem_and_global_dir(self):
        (self.codex_home / 'agents' / 'globalrole.toml').write_text(
            'name = "globalrole"\nmodel = "gpt-global"\nsandbox_mode = "read-only"\n')
        flow = self.flow()
        with self.quiet():
            self.assertIsNotNone(flow.agent('a', agent_type='als_writer'))  # file stem, name is "writer"
            self.assertIsNotNone(flow.agent('b', agent_type='globalrole'))
        first, second = self.calls()
        self.assertEqual(option(first['argv'], '-s'), ['workspace-write'])
        self.assertEqual(option(second['argv'], '-m'), ['gpt-global'])

    def test_project_role_wins_over_global(self):
        (self.codex_home / 'agents' / 'reader.toml').write_text('name = "reader"\nmodel = "gpt-global"\n')
        flow = self.flow()
        with self.quiet():
            flow.agent('x', agent_type='reader')
        self.assertEqual(option(self.calls()[0]['argv'], '-m'), ['gpt-test-luna'])

    def test_override_refused_with_agent_type(self):
        flow = self.flow()
        for kw in ({'model': 'gpt-x'}, {'effort': 'low'}, {'sandbox': 'danger-full-access'}):
            with self.assertRaises(ValueError):
                flow.agent('x', agent_type='reader', **kw)
        self.assertEqual(self.calls(), [])

    def test_unknown_role_raises(self):
        with self.assertRaises(ValueError):
            self.flow().agent('x', agent_type='nope')

    def test_free_call_uses_given_pins(self):
        flow = self.flow()
        with self.quiet():
            flow.agent('x', model='gpt-free', effort='low', sandbox='workspace-write')
        argv = self.calls()[0]['argv']
        self.assertEqual(option(argv, '-m'), ['gpt-free'])
        self.assertIn('model_reasoning_effort="low"', option(argv, '-c'))
        self.assertEqual(option(argv, '-s'), ['workspace-write'])
        self.assertEqual(self.calls()[0]['stdin'], 'x')

    def test_minimal_toml_reader_matches_tomllib(self):
        from test_v10_flow_support import ROLE_READER
        role = common._parse_role_minimal(ROLE_READER)
        self.assertEqual(role['model'], 'gpt-test-luna')
        self.assertEqual(role['sandbox_mode'], 'read-only')
        self.assertIn('Line two of the instructions.', role['developer_instructions'])
        self.assertEqual(common.parse_role(ROLE_READER)['developer_instructions'].strip(),
                         role['developer_instructions'].strip())

    # ------------------------------------------------------------ schema / failure

    def test_schema_parsed_and_file_passed(self):
        flow = self.flow()
        schema = {'type': 'object', 'required': ['finding', 'severity'],
                  'properties': {'finding': {'type': 'string'}, 'severity': {'type': 'string'}}}
        with self.quiet():
            out = flow.agent('check', agent_type='reader', schema=schema)
        self.assertEqual(out, {'finding': 'v-finding', 'severity': 'v-severity'})
        self.assertEqual(len(option(self.calls()[0]['argv'], '--output-schema')), 1)

    def test_schema_invalid_json_and_missing_keys_return_none(self):
        flow = self.flow()
        schema = {'type': 'object', 'required': ['a', 'b']}
        os.environ['FAKE_CODEX_INVALID'] = '1'
        with self.quiet():
            self.assertIsNone(flow.agent('one', schema=schema))
        del os.environ['FAKE_CODEX_INVALID']
        os.environ['FAKE_CODEX_JSON'] = json.dumps({'a': 1})
        with self.quiet():
            self.assertIsNone(flow.agent('two', schema=schema))
        os.environ['FAKE_CODEX_JSON'] = json.dumps([1, 2])
        with self.quiet():
            self.assertIsNone(flow.agent('three', schema=schema))
        self.assertEqual(flow.usage()['calls']['failed'], 3)
        reasons = [json.loads(l).get('reason') for l in (flow.dir / 'log.jsonl').read_text().splitlines()]
        self.assertTrue(any('not valid JSON' in (r or '') for r in reasons))
        self.assertTrue(any('required keys' in (r or '') for r in reasons))

    def test_failure_and_timeout_return_none_and_record_reason(self):
        flow = self.flow()
        os.environ['FAKE_CODEX_FAIL'] = '1'
        with self.quiet():
            self.assertIsNone(flow.agent('boom', label='boom'))
        del os.environ['FAKE_CODEX_FAIL']
        os.environ['FAKE_CODEX_SLEEP'] = '3'
        with self.quiet():
            self.assertIsNone(flow.agent('slow', label='slow', timeout=0.5))
        records = [json.loads(p.read_text()) for p in (flow.dir / 'calls').glob('*.json')]
        reasons = sorted(r['reason'] for r in records)
        self.assertIn('fake failure', reasons[0] + reasons[1])
        self.assertTrue(any('timeout' in r for r in reasons))
        self.assertEqual(flow.usage()['calls']['failed'], 2)

    def test_missing_codex_returns_none(self):
        os.environ['ALS_CODEX'] = str(self.tmp / 'no-such-codex')
        os.environ['PATH'] = '/nonexistent'
        flow = self.flow()
        with self.quiet():
            self.assertIsNone(flow.agent('x'))

    # ------------------------------------------------------------ concurrency

    def test_pipeline_order_and_concurrency_limit(self):
        os.environ['FAKE_CODEX_SLEEP'] = '0.4'
        flow = self.flow(concurrency=2)
        items = ['i%d' % n for n in range(6)]
        with self.quiet():
            out = flow.pipeline(items, lambda item: flow.agent('item ' + item, label=item))
        self.assertEqual(out, ['reply: item ' + i for i in items])
        spans = sorted((c['start'], c['end']) for c in self.calls())
        peak = max(sum(1 for s, e in spans if s <= t < e) for t, _ in spans)
        self.assertEqual(peak, 2)

    def test_pipeline_exception_becomes_none(self):
        flow = self.flow()

        def fn(item):
            if item == 2:
                raise RuntimeError('bad item')
            return item * 10
        with self.quiet():
            self.assertEqual(flow.pipeline([1, 2, 3], fn), [10, None, 30])

    def test_concurrency_clamped(self):
        self.assertEqual(self.flow(concurrency=50).concurrency, 6)
        self.assertEqual(self.flow(concurrency=0).concurrency, 4)
        self.assertEqual(self.flow().concurrency, 4)

    def test_parallel_runs_concurrently_in_order(self):
        os.environ['FAKE_CODEX_SLEEP'] = '0.4'
        flow = self.flow()
        with self.quiet():
            out = flow.parallel([lambda: flow.agent('first'), lambda: flow.agent('second'),
                                 lambda: 1 / 0])
        self.assertEqual(out, ['reply: first', 'reply: second', None])
        a, b = self.calls()
        self.assertLess(max(a['start'], b['start']), min(a['end'], b['end']))

    def test_slot_limit_applies_across_parallel(self):
        os.environ['FAKE_CODEX_SLEEP'] = '0.3'
        flow = self.flow(concurrency=1)
        with self.quiet():
            flow.parallel([lambda: flow.agent('a'), lambda: flow.agent('b')])
        a, b = sorted(self.calls(), key=lambda c: c['start'])
        self.assertGreaterEqual(b['start'], a['end'] - 0.05)

    # ------------------------------------------------------------ phases / files

    def test_phase_validation_and_logging(self):
        flow = self.flow()
        with self.assertRaises(ValueError):
            with flow.phase('nope'):
                pass
        with self.quiet():
            with flow.phase('collect'):
                flow.agent('in phase', label='p')
            flow.log('3 items skipped')
            path = flow.done({'summary': 'ok'})
        meta = json.loads((flow.dir / 'meta.json').read_text())
        self.assertEqual(meta['phases'][0]['status'], 'done')
        self.assertEqual(meta['phases'][1]['status'], 'pending')
        self.assertEqual(meta['status'], 'done')
        self.assertEqual(json.loads(path.read_text()), {'summary': 'ok'})
        events = [json.loads(l) for l in (flow.dir / 'log.jsonl').read_text().splitlines()]
        kinds = [e['event'] for e in events]
        self.assertIn('phase_start', kinds)
        self.assertIn('phase_end', kinds)
        call = next(e for e in events if e['event'] == 'call')
        self.assertEqual(call['phase'], 'collect')
        self.assertTrue(any(e['msg'] == '3 items skipped' for e in events))
        self.assertEqual(flow.dir.parent, self.project / '_workspace' / '.flow')

    def test_phase_failure_marked(self):
        flow = self.flow()
        with self.quiet(), self.assertRaises(RuntimeError):
            with flow.phase('verify'):
                raise RuntimeError('x')
        meta = json.loads((flow.dir / 'meta.json').read_text())
        self.assertEqual(meta['phases'][1]['status'], 'failed')

    # ------------------------------------------------------------ resume

    def test_resume_reuses_unchanged_calls_only(self):
        schema = {'type': 'object', 'required': ['k']}
        first = self.flow()
        with self.quiet():
            first.agent('same prompt', agent_type='reader')
            first.agent('schema prompt', agent_type='reader', schema=schema)
            os.environ['FAKE_CODEX_FAIL'] = '1'
            self.assertIsNone(first.agent('failed before', agent_type='reader'))
            del os.environ['FAKE_CODEX_FAIL']
            first.done({})
        self.assertEqual(len(self.calls()), 3)
        second = self.flow(resume_from=first.run_id)
        with self.quiet():
            self.assertEqual(second.agent('same prompt', agent_type='reader'), 'reply: same prompt')
            self.assertEqual(second.agent('schema prompt', agent_type='reader', schema=schema), {'k': 'v-k'})
            second.agent('changed prompt', agent_type='reader')
            second.agent('same prompt', agent_type='als_writer')  # different agent_type
            second.agent('failed before', agent_type='reader')  # failures are retried
        rerun = [c['stdin'].rsplit('\n', 1)[-1] for c in self.calls()[3:]]
        self.assertEqual(rerun, ['changed prompt', 'same prompt', 'failed before'])
        counts = second.usage()['calls']
        self.assertEqual((counts['cached'], counts['ok']), (2, 3))
        self.assertEqual(second.usage()['input_tokens'], 300)  # cached calls cost nothing
        # chaining: a resume of the resumed run still finds the cached results
        third = self.flow(resume_from=second.run_id)
        with self.quiet():
            third.agent('same prompt', agent_type='reader')
        self.assertEqual(len(self.calls()), 6)

    def test_resume_from_env_and_unknown_run(self):
        first = self.flow()
        with self.quiet():
            first.agent('p')
        os.environ['ALS_FLOW_RESUME'] = first.run_id
        second = self.flow()
        self.assertEqual(second.resume_from, first.run_id)
        with self.quiet():
            second.agent('p')
        self.assertEqual(len(self.calls()), 1)
        with self.assertRaises(ValueError):
            self.flow(resume_from='no-such-run')

    # ------------------------------------------------------------ budget / cap

    def test_budget_refusal(self):
        os.environ['FAKE_CODEX_USAGE'] = '100,40,10'  # 70 billable per call
        flow = self.flow(budget_tokens=100)
        self.assertEqual(flow.budget_remaining(), 100)
        with self.quiet():
            self.assertIsNotNone(flow.agent('one'))
            self.assertEqual(flow.budget_remaining(), 30)
            self.assertIsNotNone(flow.agent('two'))
            self.assertEqual(flow.budget_remaining(), -40)
            self.assertIsNone(flow.agent('three'))
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(flow.usage()['calls']['refused'], 1)
        self.assertEqual(flow.usage()['billable_tokens'], 140)
        self.assertIsNone(self.flow().budget_remaining())
        log = (flow.dir / 'log.jsonl').read_text()
        self.assertIn('budget exhausted', log)

    def test_call_cap(self):
        flow = self.flow()
        flow.max_calls = 2
        with self.quiet():
            results = [flow.agent('c%d' % n) for n in range(4)]
        self.assertEqual(results[:2], ['reply: c0', 'reply: c1'])
        self.assertEqual(results[2:], [None, None])
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(flow.usage()['calls']['refused'], 2)
        self.assertIn('call cap 2 reached', (flow.dir / 'log.jsonl').read_text())
        self.assertEqual(Flow.MAX_CALLS, 1000)

    # ------------------------------------------------------------ endpoint

    def test_native_endpoint_chatgpt_api_key_and_override(self):
        def base_urls():
            return [v for v in option(self.calls()[-1]['argv'], '-c') if v.startswith('openai_base_url')]
        with self.quiet():
            self.flow().agent('a')
        self.assertEqual(base_urls(), ['openai_base_url="https://chatgpt.com/backend-api/codex"'])
        os.environ['FAKE_LOGIN_STATUS'] = 'Logged in using an API key - sk-***'
        with self.quiet():
            self.flow().agent('b')
        self.assertEqual(base_urls(), ['openai_base_url="https://api.openai.com/v1"'])
        os.environ['ALS_CODEX_BASE_URL'] = 'http://127.0.0.1:9/v1'
        with self.quiet():
            self.flow().agent('c')
        self.assertEqual(base_urls(), ['openai_base_url="http://127.0.0.1:9/v1"'])
        os.environ['ALS_CODEX_BASE_URL'] = ''
        with self.quiet():
            self.flow().agent('d')
        self.assertEqual(base_urls(), [])

    def test_als_codex_env_selects_binary(self):
        os.environ['ALS_CODEX'] = str(self.fake)
        os.environ['PATH'] = '/usr/bin:/bin'
        with self.quiet():
            self.assertEqual(self.flow().agent('z'), 'reply: z')

    def test_cwd_parameter(self):
        sub = self.project / 'sub'
        sub.mkdir()
        with self.quiet():
            self.flow().agent('x', cwd=str(sub))
        call = self.calls()[0]
        self.assertEqual(option(call['argv'], '--cd'), [str(sub)])
        self.assertEqual(call['cwd'], str(sub))

    def test_thread_safe_counts(self):
        flow = self.flow(concurrency=6)
        with self.quiet():
            flow.pipeline(range(12), lambda n: flow.agent('n%d' % n))
        self.assertEqual(flow.usage()['calls']['ok'], 12)
        self.assertEqual(flow.usage()['input_tokens'], 1200)
        self.assertEqual(len(list((flow.dir / 'calls').glob('*.json'))), 12)


if __name__ == '__main__':
    unittest.main()
