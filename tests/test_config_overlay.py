import base64
import copy
import unittest
try:
    import tomllib  # noqa: F401
except ModuleNotFoundError:  # Python 3.9/3.10: installer tests need tomllib or tomli
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        raise unittest.SkipTest('needs tomllib (3.11+) or tomli')
import config_overlay as c

class OverlayTests(unittest.TestCase):
    def roundtrip(self, original):
        merged, state = c.merge(original)
        c.check(merged, state)
        self.assertEqual(c.restore(merged, state), original)
        self.assertTrue(c._equal(c.unmanaged(c.parse(original)), c.unmanaged(c.parse(merged))))
        return merged, state

    def test_array_table_disabled_skill_preserved(self):
        original = 'model="old"\n[agents]\nenabled=true\n[[skills.config]]\npath="/x"\nenabled=false\n'
        merged, _ = self.roundtrip(original)
        self.assertIs(c.parse(merged)['skills']['config'][0]['enabled'], False)

    def test_multiple_array_tables_and_agents_names(self):
        for original in ('[[skills.config]]\nenabled=false\n[[skills.config]]\nenabled=true\n',
                         '[agents]\nenabled=false\n[[nested.agents]]\nenabled=false\nmodel="keep"\n',
                         '[agents]\nmax_threads=2\n[[profiles]]\nmodel="not-root"\n',
                         '["agents"]\nenabled=false\n["skills.config"]\nenabled=false\n'):
            with self.subTest(original=original): self.roundtrip(original)

    def test_empty_and_newline_forms(self):
        for original in ('', '# comment', 'model="old"', '[agents]', '[agents]\n',
                         '[features]\nx=true\n', 'model="old"\r\n[agents]\r\nenabled=false\r\n'):
            with self.subTest(original=original): self.roundtrip(original)

    def test_unmanaged_values_preserved(self):
        self.roundtrip('approval_policy="never"\nsandbox_mode="read-only"\n[agents]\nmax_threads=2\ncustom="keep"\n[other]\nmodel="keep"\nvalues=[1,2,3]\n')

    def test_post_install_unrelated_edit_survives(self):
        merged, state = c.merge('model="old"\n[agents]\ncustom=1\n')
        merged = merged.replace('custom=1', 'custom=42') + '\n[[skills.config]]\nenabled=false\n'
        c.check(merged, state)
        restored = c.restore(merged, state)
        self.assertIn('custom=42', restored)
        self.assertIn('enabled=false', restored)

    def test_generated_agents_user_keys_kept(self):
        merged, state = c.merge('model="old"\n')
        merged += 'user_key="new"\n'
        restored = c.restore(merged, state)
        self.assertEqual(c.parse(restored)['agents']['user_key'], 'new')

    def test_model_and_marker_edits_refused(self):
        merged, state = c.merge('model="old"\n')
        for mutated in (merged.replace('gpt-6-astra', 'bad'), merged.replace(c.ROOT_END, ''),
                        merged + c.ROOT_BEGIN, merged.replace(c.BACKUP, '# LOST:')):
            with self.subTest(mutated=mutated), self.assertRaises(ValueError): c.restore(mutated, state)

    def test_unsupported_inputs_fail_closed(self):
        for value in ('model="x"\nmodel="y"\n', 'agents={enabled=false}\n',
                      '[agents.nested]\nx=1\n', 'text="""a\nb"""\n',
                      '"model"="old"\n', 'agents.enabled=false\n'):
            with self.subTest(value=value), self.assertRaises(ValueError): c.merge(value)

    def test_no_nonowned_change_can_pass_check(self):
        merged, state = c.merge('[agents]\nenabled=true\n[[skills.config]]\nenabled=false\n')
        # Move our valid block into an unrelated table: hashes alone aren't enough.
        block = c.get_block(merged, c.AGENTS_BEGIN, c.AGENTS_END)
        mutated = merged.replace(block, '') + '\n[other]\n' + block
        with self.assertRaises(ValueError): c.check(mutated, state)

    def test_legacy_overlay_restores_then_repairs_array_bug(self):
        old_agents = {k: v for k, v in c.AGENT_KEYS.items() if k != 'max_depth'}
        rb = c.block(c.ROOT_BEGIN, c.ROOT_END, c.ROOT_KEYS)
        ab = c.block(c.AGENTS_BEGIN, c.AGENTS_END, old_agents)
        encoded = c.BACKUP + base64.b64encode(b'enabled=false\n').decode() + '\n'
        old = rb + '[agents]\n' + ab + '[[skills.config]]\npath="/x"\n' + encoded
        state = {'mode': 'merged', 'root_hash': c.sha(rb), 'agent_hash': c.sha(ab),
                 'backups_hash': c.sha(encoded), 'generated_agents': False}
        restored = c.restore(old, state)
        merged, new_state = c.merge(restored)
        self.assertIs(c.parse(merged)['skills']['config'][0]['enabled'], False)
        self.assertEqual(new_state['format'], 2)

    def test_nan_is_not_spurious_change(self):
        self.roundtrip('value=nan\n')

    def test_alias_removed_and_restored(self):
        merged, _ = self.roundtrip('[agents]\nmax_threads=4\n')
        data = c.parse(merged)['agents']
        self.assertNotIn('max_threads', data)
        self.assertEqual(data['max_concurrent_threads_per_session'], 6)
