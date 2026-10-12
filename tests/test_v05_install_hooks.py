"""0.5 relocatable hooks: command forms, matcher, byte-exact restore, upgrades."""
import json
from pathlib import Path
import shlex
import subprocess
import unittest

from als_harness import integration
from als_harness.integration import HarnessError


class HookGroupTests(unittest.TestCase):
    def test_project_groups_have_no_absolute_paths(self):
        groups = integration.groups()
        self.assertEqual(groups, integration.groups('project'))
        self.assertEqual(set(groups), set(integration.HOOK_EVENTS))
        for event, group in groups.items():
            command = group['hooks'][0]['command']
            self.assertEqual(command, integration.PROJECT_RESOLVE + 'exec python3 "$s" hook')
            self.assertIn('CDPATH= cd -- ', command)
            self.assertIn('[ -f "$s" ] || exit 0', command)
            self.assertNotIn('--path-format', command)
            self.assertNotIn('--project', command)

    def test_matcher_only_on_tool_events_and_session_end_timeout(self):
        groups = integration.groups()
        for event, group in groups.items():
            if event in ('PreToolUse', 'PostToolUse'):
                self.assertEqual(group['matcher'], 'spawn_agent')
            else:
                self.assertNotIn('matcher', group)
        self.assertLessEqual(groups['SessionEnd']['hooks'][0]['timeout'], 3)

    def test_global_command_home_relative_and_quoted(self):
        self.assertEqual(integration.home_command('/h/u/.local/share/als', home='/h/u'),
                         'python3 "${HOME:-/h/u}/.local/share/als/harness.py" hook')
        odd = integration.home_command('/h/u/a "b" $c`d', home='/h/u')
        self.assertEqual(odd, 'python3 "${HOME:-/h/u}/a \\"b\\" \\$c\\`d/harness.py" hook')
        argv = subprocess.run(['sh', '-c', odd.replace('python3', 'printf %s', 1)[:-len(' hook')]],
                              capture_output=True, text=True, env={'HOME': '/h/u'}).stdout
        self.assertEqual(argv, '/h/u/a "b" $c`d/harness.py')
        outside = integration.home_command('/opt/my als', home='/h/u')
        self.assertEqual(outside, 'python3 ' + shlex.quote('/opt/my als/harness.py') + ' hook')
        with self.assertRaises(HarnessError):
            integration.groups('global')

    def test_interpreter_override_is_quoted(self):
        cmd = integration.command('project', python='/opt/py 3/bin/python3')
        self.assertTrue(cmd.endswith("exec '/opt/py 3/bin/python3' \"$s\" hook"))
        self.assertTrue(integration.command('global', '/h/als', python='python3').startswith('python3 '))
        with self.assertRaises(HarnessError):
            integration.command('project', python='py')

    def test_legacy_path_argument_means_project_scope(self):
        self.assertEqual(integration.groups(Path('/x')), integration.groups('project'))

    def test_byte_exact_restore_and_splice(self):
        raw = b'{"description":"mine",  "hooks": {"Stop": []}}\n'
        new, state = integration.merge(raw, None)
        self.assertEqual(integration.remove(new, state), raw)
        # A user edit elsewhere: splice out ALS groups instead (no byte restore).
        data = json.loads(new)
        data['hooks']['Stop'].append({'hooks': [{'type': 'command', 'command': 'echo user'}]})
        edited = json.dumps(data).encode()
        spliced = json.loads(integration.remove(edited, state))
        self.assertEqual(spliced['hooks']['Stop'], [{'hooks': [{'type': 'command', 'command': 'echo user'}]}])
        self.assertTrue(integration.unmanaged_changed(edited, state))
        self.assertFalse(integration.unmanaged_changed(new, state))

    def test_orphan_als_handlers_refused(self):
        for command in ('python3 /old/p/.astra-luna-sol/harness.py --project /old/p hook',
                        'python3 "$HOME/.local/share/als/harness.py" hook'):
            raw = json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': command}]}]}}).encode()
            with self.subTest(command=command), self.assertRaises(HarnessError):
                integration.merge(raw, None)


if __name__ == '__main__':
    unittest.main()
