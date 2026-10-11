"""0.5.0 efficiency policy regressions: pure functions, no models or state."""
import copy
import itertools
import unittest

from als_harness import efficiency as f
from als_harness.common import HarnessError, ROLES

READ_ONLY_KINDS = {'exploration', 'research', 'review'}
REASON = 'Six independent modules with disjoint paths and resources'


def task(tid='scan', **kwargs):
    return {'id': tid, 'objective': 'Read the relevant module', 'kind': 'exploration',
            'complexity': 'bounded', 'risk': 'low', 'context_files': [], 'write_paths': [],
            'resources': [], 'depends_on': [], 'checks': ['pwd'],
            'acceptance': ['Return evidence from named files'], **kwargs}


def draft(tasks=None, **kw):
    return {'goal': 'Policy test', 'workload': 'bounded-fix', 'purpose': 'production',
            'tasks': [task()] if tasks is None else tasks,
            'checks': [{'id': 'pwd', 'argv': ['pwd'], 'timeout': 10}, {'id': 'unit', 'argv': ['true'], 'timeout': 10}],
            'criteria': [], 'budget': {'tokens': 0, 'seconds': 0}, **kw}


class ProfileCapTests(unittest.TestCase):
    """Item 7: caller limits may only lower profile caps; validate re-checks them."""

    def test_caller_cannot_raise_lean(self):
        p = f.prepare(draft(efficiency={'max_concurrent': 6, 'max_writers': 2, 'max_total_spawns': 50,
                                        'parallel_reason': REASON}), 'lean')
        e = p['efficiency']
        self.assertEqual((e['max_concurrent'], e['max_writers'], e['max_total_spawns']), (1, 1, 6))
        self.assertEqual(sorted(e['caller_limits_capped']), ['max_concurrent', 'max_total_spawns', 'max_writers'])

    def test_caller_cannot_raise_balanced(self):
        e = f.prepare(draft(efficiency={'max_concurrent': 6, 'max_total_spawns': 50, 'parallel_reason': REASON}))['efficiency']
        self.assertEqual((e['max_concurrent'], e['max_total_spawns']), (3, 12))

    def test_caller_can_lower(self):
        e = f.prepare(draft(efficiency={'max_concurrent': 2, 'max_total_spawns': 3}), 'throughput')['efficiency']
        self.assertEqual((e['max_concurrent'], e['max_total_spawns']), (2, 3))
        self.assertNotIn('caller_limits_capped', e)

    def test_edited_prepared_plan_fails_validation(self):
        for key, value in (('max_concurrent', 3), ('max_writers', 2), ('max_total_spawns', 7)):
            p = f.prepare(draft(), 'lean')
            p['efficiency'][key] = value
            with self.subTest(key=key), self.assertRaises(HarnessError):
                f.validate(p)

    def test_high_risk_writer_rederived_in_validate(self):
        p = f.prepare(draft([task(kind='implementation', risk='high', write_paths=['src/a.py'])]))
        self.assertEqual(p['efficiency']['max_writers'], 1)
        p['efficiency']['max_writers'] = 2
        with self.assertRaises(HarnessError):
            f.validate(p)

    def test_unedited_prepared_plans_validate(self):
        for profile in f.PROFILES:
            extra = {'efficiency': {'parallel_reason': REASON}} if profile == 'throughput' else {}
            f.validate(f.prepare(draft(**extra), profile))


class SignatureTests(unittest.TestCase):
    """Item 8: exact duplicate detection over all semantic fields."""

    def pair(self, **changes):
        a = task('a', kind='implementation', write_paths=['src/a.py'])
        b = copy.deepcopy(a)
        b['id'] = 'b'
        b.update(changes)
        return [a, b]

    def test_distinct_semantic_fields_are_not_duplicates(self):
        cases = {'checks': {'checks': ['unit']}, 'constraints': {'constraints': ['Keep API stable']},
                 'risk': {'risk': 'normal'}, 'complexity': {'complexity': 'complex'},
                 'role': {'role': 'als_hard_worker', 'role_override_reason': 'Needs deeper reasoning'}}
        for name, change in cases.items():
            with self.subTest(field=name):
                self.assertEqual(len(f.prepare(draft(self.pair(**change)))['tasks']), 2)

    def test_equivalent_paths_are_duplicates(self):
        for paths in (['./src/a.py'], ['src//a.py'], ['src/a.py/']):
            with self.subTest(paths=paths), self.assertRaises(HarnessError):
                f.prepare(draft(self.pair(write_paths=paths)))

    def test_exact_duplicate_still_detected(self):
        with self.assertRaises(HarnessError):
            f.prepare(draft(self.pair()))

    def test_whitespace_case_objective_still_exact_duplicate(self):
        with self.assertRaises(HarnessError):
            f.prepare(draft(self.pair(objective='  read THE relevant   module')))


class ReadOnlyRoutingTests(unittest.TestCase):
    """Item 9: read-only kinds never route to a write-capable role."""

    def test_every_combination(self):
        for kind, complexity, risk in itertools.product(sorted(f.KINDS), sorted(f.COMPLEXITIES), sorted(f.RISKS)):
            with self.subTest(kind=kind, complexity=complexity, risk=risk):
                out = f.route(task(kind=kind, complexity=complexity, risk=risk))
                writes = ROLES[out['role']][2]
                if kind in READ_ONLY_KINDS:
                    self.assertFalse(writes, out)
                if kind == 'exploration':
                    self.assertEqual(out['role'], 'als_explorer')
                if kind == 'research':
                    self.assertEqual(out['role'], 'als_researcher')
                if kind == 'review':
                    deep = risk == 'high' or complexity == 'complex'
                    self.assertEqual(out['role'], 'als_reviewer_deep' if deep else 'als_reviewer')
                hard = risk == 'high' or complexity == 'complex'
                if kind in {'exploration', 'research'} and hard:
                    self.assertTrue(out.get('recommend_escalation'))
                else:
                    self.assertNotIn('recommend_escalation', out)
                if kind in {'implementation', 'test_execution'} and hard:
                    self.assertEqual(out['role'], 'als_hard_worker')

    def test_read_only_override_allowed_only_to_read_only_roles(self):
        ok = task(kind='exploration', risk='high', role='als_reviewer_deep',
                  role_override_reason='Adversarial read-only inspection')
        self.assertEqual(f.route(ok)['role'], 'als_reviewer_deep')
        for role in ('als_hard_worker', 'als_worker', 'als_tester'):
            bad = task(kind='research', complexity='complex', role=role, role_override_reason='Wants power')
            with self.subTest(role=role), self.assertRaises(HarnessError):
                f.route(bad)

    def test_high_risk_review_stays_deep(self):
        bad = task(kind='review', risk='high', role='als_reviewer', role_override_reason='Cheaper')
        with self.assertRaises(HarnessError):
            f.route(bad)

    def test_high_risk_write_kind_not_downgraded(self):
        bad = task(kind='implementation', risk='high', role='als_worker', role_override_reason='Cheaper')
        with self.assertRaises(HarnessError):
            f.route(bad)

    def test_prepared_read_only_plan_validates(self):
        p = f.prepare(draft([task(kind='exploration', complexity='complex', risk='high')]))
        self.assertEqual(p['tasks'][0]['role'], 'als_explorer')
        self.assertIn('recommend_escalation', p['tasks'][0])
        f.validate(p)


if __name__ == '__main__':
    unittest.main()
