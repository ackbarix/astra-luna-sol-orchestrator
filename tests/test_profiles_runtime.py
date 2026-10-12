import importlib.util
from pathlib import Path
import unittest
import sys
if sys.version_info < (3, 11):  # scripts/check-runtime.py itself requires 3.11
    raise unittest.SkipTest('needs Python 3.11+ (tomllib, check-runtime.py)')
import tomllib
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('checker',ROOT/'scripts/check-runtime.py');checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)

class ContractTests(unittest.TestCase):
    def test_named_models_efforts_pinned(self):
        for role,pair in checker.EXPECTED.items():
            data=tomllib.loads((ROOT/'profiles/agents'/f'{role}.toml').read_text())
            self.assertEqual((data['model'],data['model_reasoning_effort']),pair)
            self.assertEqual(data['name'],role)
    def test_all_luna_roles_and_roleless_children_use_max(self):
        from als_harness.common import ROLES
        self.assertTrue(all(effort=='max' for model,effort,_ in ROLES.values() if model=='gpt-6-luna'))
        config=tomllib.loads((ROOT/'profiles/config.toml').read_text())
        self.assertEqual(config['agents']['default_subagent_reasoning_effort'],'max')
    def test_reviewers_read_only(self):
        for role in ('als_reviewer','als_reviewer_deep'):
            data=tomllib.loads((ROOT/'profiles/agents'/f'{role}.toml').read_text());self.assertEqual(data['sandbox_mode'],'read-only')
    def test_profile_matches_overlay(self):
        import config_overlay
        config_overlay.validate_defaults(tomllib.loads((ROOT/'profiles/config.toml').read_text()))
    def valid_report(self):
        expected={r:checker.EXPECTED[r] for r in checker.SMOKE_ROLES};expected['root']=('gpt-6-astra','medium')
        return {'schema_version':2,'scope':'full_session','warnings':[], 'threads':[{'role':r,'per_model_effort':[{'model':m,'effort':e,'responses':1}]} for r,(m,e) in expected.items()]}
    def test_recorded_smoke_report(self):
        observed,errors=checker.verify_report(self.valid_report());self.assertFalse(errors);self.assertIn('root',observed)
    def test_missing_worker_not_reported_success(self):
        data=self.valid_report();data['threads']=data['threads'][:1]
        self.assertTrue(checker.verify_report(data)[1])
    def test_wrong_effort_detected(self):
        data=self.valid_report();data['threads'][0]['per_model_effort'][0]['effort']='medium'
        self.assertTrue(checker.verify_report(data)[1])
    def test_day_report_not_valid_smoke_evidence(self):
        data=self.valid_report();data['scope']='response_day'
        self.assertTrue(checker.verify_report(data)[1])
