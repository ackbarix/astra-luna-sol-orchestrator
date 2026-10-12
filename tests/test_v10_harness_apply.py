"""ALS 1.0: `als harness diff|apply` installs a staged harness into protected Codex paths."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
AGENT = '''name = "{name}"
description = "d"
model = "{model}"
model_reasoning_effort = "{effort}"
sandbox_mode = "read-only"
developer_instructions = """
x
"""
'''
SKILL = '---\nname: {name}\ndescription: "does {name}"\n---\n# {name}\n'


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / 'proj'
        self.p.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.p)], check=True)
        self.stage = self.p / '_workspace/harness-staging'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('ALS_')}

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rel, text):
        path = self.stage / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def als(self, *args):
        return subprocess.run(['sh', str(ROOT / 'bin/als'), 'harness', *args], cwd=self.p,
                              capture_output=True, text=True, env=self.env, timeout=60)

    def stage_team(self):
        self.put('.codex/agents/dev.toml', AGENT.format(name='dev', model='gpt-6.1-sol', effort='high'))
        self.put('.codex/agents/qa.toml', AGENT.format(name='qa', model='gpt-6-luna', effort='max'))
        self.put('.agents/skills/team/SKILL.md', SKILL.format(name='team'))
        self.put('.agents/skills/team/references/notes.md', 'notes\n')

    def test_diff_then_apply_then_idempotent(self):
        self.stage_team()
        diff = self.als('diff')
        self.assertEqual(diff.returncode, 0, diff.stdout + diff.stderr)
        self.assertIn('CREATE .codex/agents/dev.toml', diff.stdout)
        self.assertFalse((self.p / '.codex').exists())
        applied = self.als('apply')
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertTrue((self.p / '.codex/agents/qa.toml').is_file())
        self.assertTrue((self.p / '.agents/skills/team/references/notes.md').is_file())
        self.assertIn('APPLIED 0', self.als('apply').stdout)

    def test_update_backs_up_previous_content(self):
        self.stage_team()
        self.als('apply')
        (self.p / '.codex/agents/dev.toml').write_text(AGENT.format(name='dev', model='gpt-6.1-sol', effort='medium'))
        result = self.als('apply')
        self.assertIn('UPDATE .codex/agents/dev.toml', result.stdout)
        backups = list((self.stage / '.applied').glob('backup-*/.codex/agents/dev.toml'))
        self.assertEqual(len(backups), 1)
        self.assertIn('"medium"', backups[0].read_text())
        record = sorted((self.stage / '.applied').glob('*.json'))[-1]
        self.assertIn('.codex/agents/dev.toml', json.loads(record.read_text())['applied'])

    def test_validation_refuses_bad_staging(self):
        self.put('.codex/agents/slow.toml', AGENT.format(name='slow', model='gpt-6-luna', effort='medium'))
        self.put('.codex/agents/broken.toml', 'name = "broken"\n')
        self.put('.agents/skills/nofront/SKILL.md', '# no frontmatter\n')
        result = self.als('apply')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Luna roles must use model_reasoning_effort = "max"', result.stdout)
        self.assertIn('broken.toml: missing', result.stdout)
        self.assertIn('frontmatter lacks name', result.stdout)
        self.assertFalse((self.p / '.codex').exists())

    def test_nothing_staged(self):
        self.assertEqual(self.als('diff').returncode, 2)


if __name__ == '__main__':
    unittest.main()
