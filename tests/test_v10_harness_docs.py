"""Checks for the Codex + GPT port of the revfactory/harness v2 skills."""
import ast
import re
import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / 'harness'
SKILLS = HARNESS / 'skills'
MAIN = SKILLS / 'harness' / 'SKILL.md'
EVOLVE = SKILLS / 'harness-evolve' / 'SKILL.md'
REFS = SKILLS / 'harness' / 'references'
NOTICE = HARNESS / 'NOTICE.md'
REFERENCE_NAMES = [
    'execution-modes.md', 'model-selection-guide.md', 'orchestrator-template.md',
    'qa-agent-guide.md', 'skill-testing-guide.md', 'skill-writing-guide.md',
    'team-examples.md', 'team-patterns.md', 'workflow-recipes.md',
]
LUNA = 'gpt-6-luna'
FORBIDDEN = ['.claude/', 'CLAUDE.md', 'subagent_type', 'SendMessage', 'TaskCreate',
             'Workflow(', 'TeamCreate']
FORBIDDEN_MODELS = re.compile(r'fable|opus|sonnet', re.IGNORECASE)
DIFF_HEADING = '원본과의 차이'


def doc_files():
    return sorted(SKILLS.rglob('*.md'))


def frontmatter(path):
    text = path.read_text(encoding='utf-8')
    match = re.match(r'---\n(.*?)\n---\n', text, re.S)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(':')
        if sep and not line.startswith(' '):
            fields[key.strip()] = value.strip().strip('"')
    return fields


def code_blocks(text):
    """Yield (language, body) for each fenced block, honouring fence length."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        opening = re.match(r'^(`{3,})([\w-]*)\s*$', lines[i])
        if not opening:
            i += 1
            continue
        fence, lang = opening.group(1), opening.group(2)
        body = []
        i += 1
        while i < len(lines) and lines[i].strip() != fence:
            body.append(lines[i])
            i += 1
        yield lang, '\n'.join(body) + '\n'
        i += 1


def without_diff_note(text):
    """Drop an explicit '원본과의 차이' section (heading to next same-or-higher heading)."""
    out, skipping, level = [], False, 0
    for line in text.splitlines():
        heading = re.match(r'^(#+)\s', line)
        if heading and DIFF_HEADING in line:
            skipping, level = True, len(heading.group(1))
            continue
        if skipping and heading and len(heading.group(1)) <= level:
            skipping = False
        if not skipping:
            out.append(line)
    return '\n'.join(out)


def all_blocks(lang):
    for path in doc_files():
        for block_lang, body in code_blocks(path.read_text(encoding='utf-8')):
            if block_lang == lang:
                yield path, body


class FrontmatterTest(unittest.TestCase):
    def test_both_skills_have_name_and_description(self):
        for path, name in ((MAIN, 'harness'), (EVOLVE, 'harness-evolve')):
            fields = frontmatter(path)
            self.assertEqual(fields.get('name'), name, path)
            self.assertTrue(fields.get('description'), path)

    def test_main_skill_is_under_500_lines(self):
        self.assertLess(len(MAIN.read_text(encoding='utf-8').splitlines()), 500)


class ReferenceTest(unittest.TestCase):
    def test_all_nine_references_exist(self):
        self.assertEqual(sorted(p.name for p in REFS.glob('*.md')), sorted(REFERENCE_NAMES))

    def test_every_reference_mentioned_by_skill_exists(self):
        mentioned = set(re.findall(r'references/([\w-]+\.md)', MAIN.read_text(encoding='utf-8')))
        self.assertEqual(mentioned, set(REFERENCE_NAMES))
        for name in mentioned:
            self.assertTrue((REFS / name).is_file(), name)

    def test_notice_lists_upstream_and_every_ported_file(self):
        text = NOTICE.read_text(encoding='utf-8')
        for needle in ('https://github.com/revfactory/harness', '92d9f1b', 'Apache',
                       'modified for Codex + GPT by ALS'):
            self.assertIn(needle, text)
        for path in doc_files():
            self.assertIn(str(path.relative_to(HARNESS)), text)


class LeftoverTokenTest(unittest.TestCase):
    def test_no_claude_specific_tokens_outside_diff_note(self):
        for path in doc_files() + [NOTICE]:
            text = without_diff_note(path.read_text(encoding='utf-8'))
            for token in FORBIDDEN:
                self.assertNotIn(token, text, f'{token} in {path}')
            self.assertIsNone(FORBIDDEN_MODELS.search(text), path)


class LunaEffortTest(unittest.TestCase):
    def test_prose_never_pairs_luna_with_other_effort(self):
        for path in doc_files():
            for line in path.read_text(encoding='utf-8').splitlines():
                if LUNA in line and 'effort' in line:
                    for level in ('minimal', 'low', 'medium', 'high', 'xhigh'):
                        self.assertNotRegex(line, rf"{LUNA}['\"]?,?\s*(effort=)?['\"`]{level}\b",
                                            f'{path}: {line}')
                    self.assertIn('max', line, f'{path}: {line}')

    def test_toml_luna_examples_use_max(self):
        if tomllib is None:
            self.skipTest('tomllib unavailable')
        for path, body in all_blocks('toml'):
            data = tomllib.loads(body)
            if data.get('model') == LUNA:
                self.assertEqual(data.get('model_reasoning_effort'), 'max', path)

    def test_python_luna_calls_use_max(self):
        for path, body in all_blocks('python'):
            for node in ast.walk(ast.parse(body)):
                if not isinstance(node, ast.Call):
                    continue
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                model = kw.get('model')
                if isinstance(model, ast.Constant) and model.value == LUNA:
                    effort = kw.get('effort')
                    self.assertTrue(isinstance(effort, ast.Constant) and effort.value == 'max',
                                    path)


class ExampleSyntaxTest(unittest.TestCase):
    def test_python_blocks_compile(self):
        blocks = list(all_blocks('python'))
        self.assertGreaterEqual(len(blocks), 10)
        for path, body in blocks:
            ast.parse(body)
            compile(body, str(path), 'exec')  # also rejects 'return' outside a function

    def test_no_javascript_blocks_remain(self):
        self.assertEqual(list(all_blocks('javascript')), [])

    def test_toml_blocks_parse(self):
        if tomllib is None:
            self.skipTest('tomllib unavailable')
        blocks = list(all_blocks('toml'))
        self.assertGreaterEqual(len(blocks), 3)
        for path, body in blocks:
            data = tomllib.loads(body)
            for key in ('name', 'description', 'model', 'model_reasoning_effort',
                        'sandbox_mode', 'developer_instructions'):
                self.assertIn(key, data, path)

    def test_read_only_examples_say_so_in_instructions(self):
        if tomllib is None:
            self.skipTest('tomllib unavailable')
        for path, body in all_blocks('toml'):
            data = tomllib.loads(body)
            if data.get('sandbox_mode') == 'read-only':
                self.assertIn('읽기 전용', data['developer_instructions'], path)

    def test_flow_scripts_filter_none_results(self):
        for path, body in all_blocks('python'):
            if 'flow.parallel(' in body or 'flow.pipeline(' in body:
                self.assertRegex(body, r'is not None', path)


class PortRuleTest(unittest.TestCase):
    def test_verify_once_rule_present(self):
        self.assertIn('검증은 한 번 하고 멈춘다', MAIN.read_text(encoding='utf-8'))
        self.assertIn('검증은 한 번 하고 멈춘다',
                      (REFS / 'team-patterns.md').read_text(encoding='utf-8'))

    def test_codex_paths_and_tools_used(self):
        text = MAIN.read_text(encoding='utf-8')
        for needle in ('.codex/agents/', '.agents/skills/', 'AGENTS.md', 'spawn_agent',
                       'als-flow', 'als-agent', 'als-task', '$harness-evolve',
                       'gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna'):
            self.assertIn(needle, text)


if __name__ == '__main__':
    unittest.main()
