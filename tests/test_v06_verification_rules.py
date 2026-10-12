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

    def test_generic_harness_skill_keeps_verify_once(self):
        """The generic harness (astra-luna-sol) is a Harness v2 orchestrator with the verify-once rule."""
        skill = (ROOT / 'profiles/skills/astra-luna-sol/SKILL.md').read_text()
        for section in ('## 실행 모드: 혼합', '## 에이전트 구성', '### 0단계: 기존 작업 확인',
                        '## 검증: 한 번, 위험도에 비례', '## 오류 처리', '## 테스트 시나리오'):
            self.assertIn(section, skill)
        for phrase in ('가장 좁은 관련 테스트만', '마지막에 완료 조건을 한 번 확인한다',
                       '바뀌지 않은 코드를 다시 검수하지 않는다', '감시·검증 작업을 따로 만들지 않는다',
                       '"미확인"', '계획 검토는 반복하지 않는다'):
            self.assertIn(phrase, skill)
        self.assertLess(len(skill.splitlines()), 500)

    def test_every_role_has_a_stop_rule(self):
        roles = sorted((ROOT / 'profiles/agents').glob('als_*.toml'))
        self.assertEqual(len(roles), 8)
        for role in roles:
            text = instructions(role)
            self.assertTrue(re.search(r'\bonce\b|[Ss]top (as soon|once)', text), role.name)
            self.assertNotIn('re-verify everything', text)


if __name__ == '__main__':
    unittest.main()
