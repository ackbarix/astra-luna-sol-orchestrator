"""0.5.0 check-runtime.py regressions: no codex/ocx execution, no model calls."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('checker_v05', ROOT / 'scripts/check-runtime.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def valid_report():
    expected = {r: checker.EXPECTED[r] for r in checker.SMOKE_ROLES}
    expected['root'] = ('gpt-6-astra', 'medium')
    return {'schema_version': 2, 'scope': 'full_session', 'warnings': [], 'index_warnings': [],
            'threads': [{'role': r, 'counted': True, 'per_model_effort': [{'model': m, 'effort': e, 'responses': 1}]}
                        for r, (m, e) in expected.items()]}


def thread(role, model, effort, counted=True):
    return {'role': role, 'counted': counted, 'per_model_effort': [{'model': model, 'effort': effort, 'responses': 1}]}


class VerifyReportTests(unittest.TestCase):
    def test_index_warnings_do_not_fail(self):
        data = valid_report()
        data['index_warnings'] = ['missing_session_meta:rollout-x.jsonl', 'missing_root_metadata:ghost']
        notes = []
        observed, problems = checker.verify_report(data, notes)
        self.assertEqual(problems, [])
        self.assertTrue(any('index' in n for n in notes))

    def test_selected_warnings_still_fail(self):
        data = valid_report()
        data['warnings'] = ['root:unreadable_line:rollout-root.jsonl:3']
        self.assertTrue(checker.verify_report(data)[1])

    def test_unexpected_extra_role_is_warning(self):
        data = valid_report()
        data['threads'].append(thread('review', 'gpt-6.1-sol', 'medium'))
        notes = []
        observed, problems = checker.verify_report(data, notes)
        self.assertEqual(problems, [])
        self.assertTrue(any('review' in n for n in notes))

    def test_extra_known_role_with_wrong_identity_is_problem(self):
        data = valid_report()
        data['threads'].append(thread('als_tester', 'gpt-6.1-sol', 'high'))
        self.assertTrue(any('als_tester' in p for p in checker.verify_report(data)[1]))

    def test_extra_known_role_with_expected_identity_ok(self):
        data = valid_report()
        data['threads'].append(thread('als_tester', 'gpt-6-luna', 'medium'))
        notes = []
        self.assertEqual(checker.verify_report(data, notes)[1], [])
        self.assertTrue(any('als_tester' in n for n in notes))

    def test_uncounted_guardian_threads_ignored(self):
        data = valid_report()
        data['threads'].append(thread('guardian', 'other-model', 'low', counted=False))
        notes = []
        self.assertEqual(checker.verify_report(data, notes)[1], [])
        self.assertFalse(any('guardian' in n for n in notes))

    def test_main_reports_warnings_without_failing(self):
        data = valid_report()
        data['threads'].append(thread('review', 'gpt-6.1-sol', 'medium'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'r.json'
            path.write_text(json.dumps(data))
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch.object(checker, 'inspect_config', return_value=({}, [])):
                code = checker.main(['--report', str(path)])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'CONFIG_AND_REQUESTED_CHECKS_OK')
        self.assertTrue(any('review' in w for w in result['warnings']))


class CommandTests(unittest.TestCase):
    def test_smoke_prompt_prints_repository_text(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checker.main(['--smoke-prompt'])
        self.assertEqual(code, 0)
        expected = (ROOT / 'docs/smoke-test.txt').read_text(encoding='utf-8')
        self.assertEqual(out.getvalue(), expected + '\n')

    def test_probe_without_codex_on_path(self):
        with tempfile.TemporaryDirectory() as empty, patch.dict(os.environ, {'PATH': empty}), \
                patch.object(checker.subprocess, 'run', side_effect=AssertionError('must not execute')):
            values, warnings = checker.probe()
        self.assertEqual(values, {})
        self.assertIn('codex missing from PATH', warnings)
        self.assertIn('ocx missing from PATH', warnings)

    def test_probe_with_only_codex_present(self):
        with tempfile.TemporaryDirectory() as bindir:
            fake = Path(bindir) / 'codex'
            fake.write_text('#!/bin/sh\necho "codex-cli 9.9.9"\n')
            fake.chmod(0o700)
            with patch.dict(os.environ, {'PATH': bindir}):
                values, warnings = checker.probe()
        self.assertEqual(values, {'codex_version': 'codex-cli 9.9.9'})
        self.assertEqual(warnings, ['ocx missing from PATH', 'ocx missing from PATH'])


if __name__ == '__main__':
    unittest.main()
