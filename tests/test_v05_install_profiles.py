"""Role profiles match the ROLES table; snippet and scripts stay portable."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

# name: (model, effort, writes). Luna is pinned to max.
ROLES = {
    'als_explorer': ('gpt-6-luna', 'max', False),
    'als_researcher': ('gpt-6-luna', 'max', False),
    'als_tester': ('gpt-6-luna', 'max', True),
    'als_worker': ('gpt-6-luna', 'max', True),
    'als_test_designer': ('gpt-6-luna', 'max', True),
    'als_hard_worker': ('gpt-6.1-sol', 'high', True),
    'als_reviewer': ('gpt-6.1-sol', 'medium', False),
    'als_reviewer_deep': ('gpt-6.1-sol', 'high', False),
}

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / 'profiles/agents'


def toml_value(text, key):
    """Top-level string assignment without a TOML parser (runs on Python 3.9)."""
    match = re.search(r'^' + key + r'\s*=\s*"([^"]*)"', text, re.M)
    return match.group(1) if match else None


class ProfileTests(unittest.TestCase):
    def test_every_role_file_matches_roles(self):
        files = {p.stem: p.read_text(encoding='utf-8') for p in AGENTS.glob('*.toml')}
        self.assertEqual(set(files), set(ROLES))
        for name, text in files.items():
            model, effort, writes = ROLES[name]
            with self.subTest(role=name):
                self.assertEqual(toml_value(text, 'name'), name)
                self.assertEqual(toml_value(text, 'model'), model)
                self.assertEqual(toml_value(text, 'model_reasoning_effort'), effort)
                sandbox = toml_value(text, 'sandbox_mode')
                self.assertEqual(sandbox, 'workspace-write' if writes else 'read-only')
                if not writes:
                    self.assertIn('inherit', text.lower())
        try:
            import tomllib
        except ModuleNotFoundError:
            return
        for name, text in files.items():
            data = tomllib.loads(text)
            self.assertEqual((data['model'], data['model_reasoning_effort']), ROLES[name][:2])

    def test_role_texts_are_distinct_and_specific(self):
        designer = (AGENTS / 'als_test_designer.toml').read_text()
        self.assertIn('TEST DESIGNER', designer)
        self.assertNotIn('ALS / TESTER', designer)
        self.assertNotIn('Modify tests only if', designer)
        deep = (AGENTS / 'als_reviewer_deep.toml').read_text()
        reviewer = (AGENTS / 'als_reviewer.toml').read_text()
        body = lambda t: t.split('developer_instructions', 1)[1]
        self.assertNotEqual(body(deep), body(reviewer))
        for topic in ('security', 'concurrency', 'data integrity', 'state'):
            self.assertIn(topic, deep.lower())

    def test_snippet_short_and_points_at_skill(self):
        lines = (ROOT / 'profiles/AGENTS.snippet.md').read_text().strip().splitlines()
        self.assertLessEqual(len(lines), 8)
        self.assertIn('$astra-luna-sol', '\n'.join(lines))

    def test_config_header_has_no_stale_version(self):
        header = (ROOT / 'profiles/config.toml').read_text().split('\n', 1)[0]
        self.assertNotRegex(header, r'\d+\.\d+\.\d+')


class ScriptTests(unittest.TestCase):
    def test_posix_scripts_parse_with_sh_and_dash(self):
        shells = [s for s in ('sh', 'dash') if shutil.which(s)]
        for script in ('install.sh', 'setup.sh', 'bin/als'):
            self.assertTrue((ROOT / script).read_text().startswith('#!/usr/bin/env sh'), script)
            for shell in shells:
                with self.subTest(script=script, shell=shell):
                    result = subprocess.run([shell, '-n', str(ROOT / script)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_bash_scripts_parse(self):
        for script in sorted((ROOT / 'scripts').glob('*.sh')):
            result = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_sh_empty_extra_args_and_missing_ocx(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/run.sh'), '--dry-run'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Would execute: codex', result.stdout)
        text = (ROOT / 'scripts/run.sh').read_text()
        self.assertIn('${extra[@]+"${extra[@]}"}', text)
        self.assertLess(text.index('command -v ocx'), text.index('ocx v2 mode'))

    def test_setup_banner_reads_version(self):
        self.assertIn('VERSION', (ROOT / 'setup.sh').read_text())
        self.assertNotIn('0.2.0', (ROOT / 'setup.sh').read_text())


if __name__ == '__main__':
    unittest.main()
