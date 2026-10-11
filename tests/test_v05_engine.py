"""Regression tests for the 0.5.0 state-engine audit fixes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from als_harness.state import Store
from als_harness.engine import Engine
from als_harness.common import HarnessError, redact, relative


class EngineCase(unittest.TestCase):
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

    def plan(self, tasks=None, argv=None):
        argv = argv or [sys.executable, '-c', 'print("ok")']
        return {'goal': 'synthetic', 'tasks': tasks or [],
                'checks': [{'id': 'unit', 'argv': argv, 'timeout': 5}], 'criteria': []}

    def task(self, key='one', path='source.py', role='als_worker'):
        return {'id': key, 'role': role, 'write_paths': [path] if path else [], 'checks': []}

    def start(self, tasks=None, session=None, **kwargs):
        return self.e.start(self.plan(tasks, **kwargs), session)


class RedactAndPathTests(unittest.TestCase):
    def test_redact_extended_secret_shapes(self):
        samples = {
            'secret=s3cr3tvalue': 's3cr3tvalue',
            'passwd: hunter22': 'hunter22',
            'private_key=abcd1234': 'abcd1234',
            'credential=c-r-e-d': 'c-r-e-d',
            'run --token tok123456 next': 'tok123456',
            'jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl end': 'eyJzdWIiOiIxIn0',
            'key AKIAABCDEFGHIJKLMNOP here': 'AKIAABCDEFGHIJKLMNOP',
            'github_pat_11ABCDEFG_abcdefghijk': 'github_pat_11ABCDEFG',
            '{"password": "pw-in-json"}': 'pw-in-json',
        }
        for text, secret in samples.items():
            with self.subTest(text=text):
                self.assertNotIn(secret, redact(text))
                self.assertIn('[REDACTED]', redact(text))

    def test_redact_keeps_ordinary_text(self):
        self.assertEqual(redact('ran 12 tests --tokens-file report'), 'ran 12 tests --tokens-file report')

    def test_codex_hook_configuration_is_reserved(self):
        for value in ('.codex', '.codex/hooks.json', '.git/config', '.astra-luna-sol/x'):
            with self.subTest(value=value):
                with self.assertRaises(HarnessError):
                    relative(value)
        self.assertEqual(relative('codex/x.py'), 'codex/x.py')


class BudgetTests(EngineCase):
    def add_usage(self, rid, thread, response, input_tokens, cached, output):
        usage = {'input_tokens': input_tokens, 'cached_input_tokens': cached, 'output_tokens': output}
        with self.s.tx():
            self.s.db.execute('INSERT INTO usage VALUES(?,?,?,?)',
                              (rid, thread, response, json.dumps({'usage': usage})))

    def test_budget_counts_uncached_input_plus_output(self):
        task = self.task()
        task['estimate_tokens'] = 1000
        rid = self.start([task])
        self.e.reserve(rid, 'one', 'call')
        self.e.bind(rid, 'call', 'thread-1')
        self.add_usage(rid, 'thread-1', 'r1', 900, 800, 50)
        budget = self.e.budget(self.s.run(rid))
        self.assertEqual(budget['confirmed_tokens'], 150)
        self.assertEqual(budget['reserved_estimate'], 850)


if __name__ == '__main__':
    unittest.main()
