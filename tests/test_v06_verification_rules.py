"""ALS 0.6: GPT instructions carry the verify-once rule (Claude-style verification)."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def instructions(role_file):
    text = role_file.read_text()
    return re.search(r'developer_instructions = """(.*?)"""', text, re.S).group(1)


class VerificationRuleTests(unittest.TestCase):
    def test_always_loaded_snippet_says_verify_once(self):
        snippet = (ROOT / 'profiles/AGENTS.snippet.md').read_text()
        self.assertIn('Verify once, then stop', snippet)
        self.assertIn('acceptance check once', snippet)
        self.assertIn('unverified', snippet)

    def test_skill_has_verification_section_before_managed_work(self):
        skill = (ROOT / 'profiles/skills/astra-luna-sol/SKILL.md').read_text()
        self.assertLess(skill.index('## Verification: once'), skill.index('## Managed work'))
        for phrase in ('narrowest relevant test', 'acceptance check once', 'Never a second review',
                       'monitor, watcher or per-step verifier'):
            self.assertIn(phrase, skill)

    def test_every_role_has_a_stop_rule(self):
        roles = sorted((ROOT / 'profiles/agents').glob('als_*.toml'))
        self.assertEqual(len(roles), 8)
        for role in roles:
            text = instructions(role)
            self.assertTrue(re.search(r'\bonce\b|[Ss]top (as soon|once)', text), role.name)
            self.assertNotIn('re-verify everything', text)


if __name__ == '__main__':
    unittest.main()
