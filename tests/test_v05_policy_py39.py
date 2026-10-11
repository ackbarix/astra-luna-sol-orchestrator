"""Python 3.9 floor for policy/telemetry/usage/check files.

No 3.9 interpreter is required: grammar is checked with ast feature_version,
risky constructs by an AST walk, and 3.9-only runtime behaviour is emulated.
"""
import ast
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
FILES = ['als_harness/efficiency.py', 'als_harness/telemetry.py', 'scripts/token_usage.py', 'scripts/check-runtime.py']


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


usage = load('usage_py39', 'scripts/token_usage.py')

PY39_ISO = re.compile(r'^\d{4}-\d\d-\d\d([T ]\d\d(:\d\d(:\d\d(\.(\d{3}|\d{6}))?)?)?([+-]\d\d:\d\d(:\d\d)?)?)?$')


class Py39Datetime(datetime):
    """fromisoformat as accepted by Python 3.9/3.10 (no 'Z', 3 or 6 fraction digits)."""

    @classmethod
    def fromisoformat(cls, text):
        if not isinstance(text, str) or not PY39_ISO.match(text):
            raise ValueError('Invalid isoformat string: %r' % (text,))
        return datetime.fromisoformat(text)


class SyntaxFloorTests(unittest.TestCase):
    def test_grammar_and_constructs(self):
        for rel in FILES:
            with self.subTest(file=rel):
                tree = ast.parse((ROOT / rel).read_text(encoding='utf-8'), feature_version=(3, 9))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        self.assertNotIn('strict', [k.arg for k in node.keywords], 'zip(strict=) is 3.10+')
                        name = getattr(node.func, 'id', None)
                        if name in ('isinstance', 'issubclass') and len(node.args) == 2:
                            self.assertNotIsInstance(node.args[1], ast.BinOp, 'X | Y types are 3.10+')
                    if isinstance(node, ast.Attribute):
                        self.assertNotIn(node.attr, {'bit_count', 'pairwise', 'TypeAlias', 'UTC'})
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name == 'tomllib':
                                self.assertEqual(rel, 'scripts/check-runtime.py')

    def test_tomllib_import_is_guarded(self):
        tree = ast.parse((ROOT / 'scripts/check-runtime.py').read_text(encoding='utf-8'))
        guarded = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
                   and any(isinstance(b, ast.Import) and b.names[0].name == 'tomllib' for b in n.body)]
        self.assertTrue(guarded)


class TimestampTests(unittest.TestCase):
    CASES = {
        '2026-10-08T12:00:01Z': datetime(2026, 10, 8, 12, 0, 1, tzinfo=timezone.utc),
        '2026-10-08T12:00:01.5Z': datetime(2026, 10, 8, 12, 0, 1, 500000, tzinfo=timezone.utc),
        '2026-10-08T12:00:01.123456789Z': datetime(2026, 10, 8, 12, 0, 1, 123456, tzinfo=timezone.utc),
        '2026-10-08T21:00:01.123+09:00': datetime(2026, 10, 8, 12, 0, 1, 123000, tzinfo=timezone.utc),
        '2026-10-08T12:00:01': None, 'garbage': None, '': None, None: None, 5: None,
    }

    def check(self):
        for text, expected in self.CASES.items():
            with self.subTest(text=text):
                self.assertEqual(usage.parse_ts(text), expected)

    def test_current_interpreter(self):
        self.check()

    def test_emulated_python39_fromisoformat(self):
        with patch.object(usage, 'datetime', Py39Datetime):
            self.check()


class MinimalTomlTests(unittest.TestCase):
    @unittest.skipIf(tomllib is None, 'reference parser needs Python 3.11+')
    def test_matches_tomllib_on_shipped_profiles(self):
        checker = load('checker_py39', 'scripts/check-runtime.py')
        for path in sorted((ROOT / 'profiles').rglob('*.toml')):
            with self.subTest(path=path.name):
                full = tomllib.loads(path.read_text(encoding='utf-8'))
                small = checker.load_minimal_toml(path.read_text(encoding='utf-8'))
                for key, value in small.items():
                    self.assertEqual(full[key], value)
                for key in ('name', 'model', 'model_reasoning_effort', 'sandbox_mode'):
                    if key in full:
                        self.assertEqual(small[key], full[key])
                if 'agents' in full:
                    self.assertEqual(small['agents'], full['agents'])

    def test_multiline_string_content_not_read_as_keys(self):
        checker = load('checker_py39b', 'scripts/check-runtime.py')
        text = 'name = "als_worker"\ndeveloper_instructions = """\nmodel = "evil"\n"""\nmodel = "gpt-6-luna" # pinned\n'
        self.assertEqual(checker.load_minimal_toml(text), {'name': 'als_worker', 'model': 'gpt-6-luna'})

    def test_unsupported_values_not_guessed_and_duplicates_refused(self):
        checker = load('checker_py39c', 'scripts/check-runtime.py')
        self.assertEqual(checker.load_minimal_toml('a = [1, 2]\nb = 1.5\nc = {x = 1}\n'), {})
        with self.assertRaises(ValueError):
            checker.load_minimal_toml('a = "x"\na = "y"\n')

    def test_without_tomllib_or_tomli(self):
        with patch.dict(sys.modules, {'tomllib': None, 'tomli': None}):
            checker = load('checker_no_toml', 'scripts/check-runtime.py')
        self.assertEqual(checker.TOML_PARSER, 'minimal')
        self.assertIsNone(checker.tomllib)
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / '.codex').mkdir()
            shutil.copy(ROOT / 'profiles/config.toml', project / '.codex/config.toml')
            shutil.copytree(ROOT / 'profiles/agents', project / '.codex/agents')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = checker.main(['--project', str(project)])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual(result['problems'], [])
        self.assertEqual(result['toml_parser'], 'minimal')
        self.assertTrue(any('minimal' in w for w in result['warnings']))


if __name__ == '__main__':
    unittest.main()
