"""0.4 policy regressions: local Python/Git only, no live model invocations."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from als_harness import efficiency as f, telemetry as a, hooks, report, usage, verify
from als_harness.engine import Engine, validate_plan
from als_harness.common import HarnessError, encoded
from als_harness.state import Store


ROOT=Path(__file__).resolve().parents[1]

def task(tid='scan', **kwargs):
    return {'id':tid,'objective':'Read the relevant module '+tid,'kind':'exploration',
            'complexity':'bounded','risk':'low','context_files':[],
            'write_paths':[],'resources':[],'depends_on':[],'checks':['pwd'],
            'acceptance':['Return evidence from named files'],**kwargs}

def draft(tasks=None, **kw):
    return {'goal':'Policy test','workload':'bounded-fix','purpose':'production',
            'tasks':[task()] if tasks is None else tasks,
            'checks':[{'id':'pwd','argv':['pwd'],'timeout':10}], 'criteria':[],
            'budget':{'tokens':0,'seconds':0}, **kw}

class RoutingTests(unittest.TestCase):
    def test_trivial_direct(self):
        p=f.prepare(draft([task(complexity='trivial')]))
        self.assertEqual(p['tasks'][0]['executor'],'head')
    def test_justified_trivial_delegation(self):
        t=task(complexity='trivial',delegation_reason='Independent review of a critical assumption')
        self.assertEqual(f.prepare(draft([t]))['tasks'][0]['executor'],'worker')
    def test_roles(self):
        cases=[('exploration','bounded','low','als_explorer'),('research','bounded','normal','als_researcher'),
               ('test_execution','bounded','normal','als_tester'),('test_design','complex','normal','als_test_designer'),
               ('implementation','bounded','normal','als_worker'),('implementation','complex','normal','als_hard_worker'),
               ('review','bounded','normal','als_reviewer'),('review','bounded','high','als_reviewer_deep')]
        for k,c,r,w in cases:
            with self.subTest(kind=k,risk=r):self.assertEqual(f.route(task(kind=k,complexity=c,risk=r))['role'],w)
    def test_role_conflict_needs_reason(self):
        with self.assertRaises(HarnessError):f.prepare(draft([task(role='als_worker')]))
    def test_explicit_role_reason(self):
        t=task(kind='implementation',role='als_hard_worker',role_override_reason='Harder than initial classification')
        self.assertEqual(f.prepare(draft([t]))['tasks'][0]['role'],'als_hard_worker')
    def test_high_risk_never_silently_downshift(self):
        with self.assertRaises(HarnessError):f.prepare(draft([task(kind='implementation',risk='high',role='als_explorer',role_override_reason='cheap')]))
    def test_invalid_enum(self):
        for key in ('kind','complexity','risk'):
            with self.assertRaises(HarnessError):f.prepare(draft([task(**{key:'invented'})]))
    def test_duplicate_scope(self):
        t=task();u=copy.deepcopy(t);u['id']='other'
        with self.assertRaises(HarnessError):f.prepare(draft([t,u]))
    def test_review_not_duplicate_implementation(self):
        t=task(kind='implementation');u=copy.deepcopy(t);u.update(id='review',kind='review')
        self.assertEqual(len(f.prepare(draft([t,u]))['tasks']),2)
    def test_refs_no_ingestion(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'sensitive.txt').write_text('DO_NOT_INGEST_THIS')
            p=f.prepare(draft([task(context_files=['sensitive.txt'])]))
            msg=f.build_message(p,p['tasks'][0]);self.assertIn('sensitive.txt',msg);self.assertNotIn('DO_NOT_INGEST_THIS',msg)
    def test_oversize_contract_is_not_truncated(self):
        t=task(known_facts=['x'*350]*8,constraints=['c'*350]*8,acceptance=['a'*350]*8)
        with self.assertRaises(HarnessError):f.prepare(draft([t]))
    def test_invalid_refs(self):
        for path in ('../secret','/etc/passwd','.git/config','.astra-luna-sol/state/x'):
            with self.assertRaises(HarnessError):f.prepare(draft([task(context_files=[path])]))
    def test_many_refs(self):
        with self.assertRaises(HarnessError):f.prepare(draft([task(context_files=[str(x) for x in range(13)])]))
    def test_profile_boundaries(self):
        self.assertEqual(f.prepare(draft())['efficiency']['max_concurrent'],3)
        self.assertEqual(f.prepare(draft(), 'lean')['efficiency']['max_concurrent'],1)
        with self.assertRaises(HarnessError):f.prepare(draft(),'throughput')
        p=f.prepare(draft(efficiency={'parallel_reason':'Six independent modules with disjoint paths and resources'}),'throughput')
        self.assertEqual(p['efficiency']['max_concurrent'],6)
    def test_high_risk_single_writer(self):
        p=f.prepare(draft([task(kind='implementation',risk='high',write_paths=['src/a.py'])]))
        self.assertEqual(p['efficiency']['max_writers'],1)
    def test_legacy_plan_unchanged(self):
        p={'goal':'old','tasks':[{'id':'x','role':'als_worker'}]}
        out=validate_plan(p);self.assertNotIn('efficiency',out)
    def test_prepare_has_no_side_effect_on_input(self):
        p=draft();old=copy.deepcopy(p);f.prepare(p);self.assertEqual(p,old)
    def test_invalid_capacity_values(self):
        for n in (0,7,True,'3'):
            with self.assertRaises(HarnessError):f.prepare(draft(efficiency={'max_concurrent':n}))
    def test_sample_shortage_does_not_raise_budget(self):
        p=f.prepare(draft(budget={'tokens':1000}),history={'eligible':False})
        self.assertEqual(p['budget']['tokens'],1000);self.assertFalse(p['efficiency']['history_applied'])
    def test_history_only_explicit_cohort(self):
        t=task();t['role']='als_explorer'
        hist={'eligible':True,'workload':'bounded-fix','suggested_max_concurrent':2,
              'estimates':{f.bucket(t):{'suggested_tokens':1000,'run_ids':['a','b','c','d','e']}}}
        p=f.prepare(draft([t],budget={'tokens':500}),history=hist)
        self.assertEqual(p['tasks'][0]['estimate_tokens'],1000);self.assertEqual(p['budget']['tokens'],500)
        self.assertEqual(p['efficiency']['max_concurrent'],2)

class EngineFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name)/'project';self.p.mkdir()
        subprocess.run(['git','init','-q',str(self.p)],check=True)
        (self.p/'src').mkdir();(self.p/'src/a.py').write_text('print(1)\n')
        self.s=Store(self.p);self.e=Engine(self.s)
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def start(self,p=None):return self.e.start(f.prepare(p or draft()),session='root')
    def event(self,r,thread='root',n=100,cached=50,model='gpt-6-astra',effort='medium',response='r1'):
        d={'model':model,'effort':effort,'usage':{'input_tokens':n,'cached_input_tokens':cached,'output_tokens':10,'reasoning_output_tokens':0}}
        self.s.db.execute('INSERT INTO usage VALUES(?,?,?,?)',(r,thread,response,encoded(d)))
class EnginePolicyTests(EngineFixture, unittest.TestCase):
    def test_direct_denied_as_worker(self):
        r=self.start(draft([task(complexity='trivial')]))
        result=self.e.reserve(r,'scan','call');self.assertFalse(result['allowed'])
        self.assertIn('HEAD',' '.join(result['reasons']))
    def test_head_claims_without_fake_worker(self):
        r=self.start(draft([task(complexity='trivial',kind='implementation',write_paths=['src/a.py'])]))
        self.assertTrue(self.e.begin_head(r,'scan')['allowed'])
        self.assertIsNone(self.s.task(r,'scan')['attempts'][0]['thread_id'])
        self.e.finish(r,'scan','Real caller result')
        self.assertTrue(verify.verify(self.e,r,execute_commands=True)['passed'])
        self.e.accept(r,'scan');self.assertTrue(self.e.complete(r)['completed'])
    def test_two_heads_conflict_by_path(self):
        ts=[task('a',complexity='trivial',kind='implementation',write_paths=['src/a.py']),task('b',complexity='trivial',kind='implementation',write_paths=['src/a.py'])]
        r=self.start(draft(ts));self.assertTrue(self.e.begin_head(r,'a')['allowed']);self.assertFalse(self.e.begin_head(r,'b')['allowed'])
    def test_begin_head_cannot_execute_worker(self):
        r=self.start();self.assertFalse(self.e.begin_head(r,'scan')['allowed'])
    def test_concurrent_three_not_six_by_default(self):
        r=self.start(draft([task(str(i)) for i in range(4)]))
        for i in range(3):self.assertTrue(self.e.reserve(r,str(i),'c'+str(i))['allowed'])
        self.assertFalse(self.e.reserve(r,'3','c3')['allowed'])
    def test_character_limit(self):
        r=self.start();out=self.e.reserve(r,'scan','c',requested={'message':'ALS_TASK=scan\n'+'x'*8000})
        self.assertFalse(out['allowed']);self.assertEqual(self.s.task(r,'scan')['attempts'],[])
    def test_fork_context_requires_reason(self):
        r=self.start();self.assertFalse(self.e.reserve(r,'scan','c',requested={'message':'ALS_TASK=scan','fork_context':True})['allowed'])
    def test_fork_context_can_be_justified(self):
        r=self.start(draft([task(full_context_reason='Interdependent context cannot be faithfully restated')]))
        self.assertTrue(self.e.reserve(r,'scan','c',requested={'message':'ALS_TASK=scan','fork_context':True})['allowed'])
    def test_no_unsupported_argument_injection(self):
        r=self.start();self.assertTrue(self.e.reserve(r,'scan','c',requested={'message':'ALS_TASK=scan'})['allowed'])
        ctx=self.s.task(r,'scan')['attempts'][0]['context'];self.assertEqual(ctx['fork_context'],'not_exposed_or_unspecified');self.assertNotIn('message',ctx)
    def test_budget_finalization_reserve(self):
        r=self.start(draft([task(estimate_tokens=850)],budget={'tokens':1000}))
        self.assertFalse(self.e.reserve(r,'scan','c')['allowed'])
    def test_budget_explicit_unknown_estimate(self):
        r=self.start(draft(budget={'tokens':1000}))
        self.assertIn('set a worker estimate',' '.join(self.e.reserve(r,'scan','c')['reasons']))
    def test_actual_usage_deducted_from_outstanding_estimate(self):
        r=self.start(draft([task(estimate_tokens=100)]));self.e.reserve(r,'scan','c');self.e.bind(r,'c','kid')
        self.event(r,'kid',n=70,model='gpt-6-luna',effort='medium')
        self.assertEqual(self.e.budget(self.s.run(r))['reserved_estimate'],20)
    def test_terminal_clock_frozen(self):
        r=self.start(draft([]));verify.verify(self.e,r,execute_commands=True);self.e.complete(r)
        before=self.e.budget(self.s.run(r))['elapsed_wall_seconds']
        with patch('als_harness.engine.now',return_value=9e20):self.assertEqual(self.e.budget(self.s.run(r))['elapsed_wall_seconds'],before)
    def test_finish_does_not_rewrite_worker_end(self):
        r=self.start();self.e.reserve(r,'scan','c');self.e.bind(r,'c','kid');self.e.worker_stopped('root','kid','als_explorer')
        end=self.s.task(r,'scan')['attempts'][0]['ended'];self.e.finish(r,'scan','report')
        self.assertEqual(self.s.task(r,'scan')['attempts'][0]['ended'],end)
    def test_guarded_small_task_rejected_through_real_adapter(self):
        r=self.start(draft([task(complexity='trivial')]))
        with self.s.tx():x=self.s.run(r);x['mode']='guarded';self.s.save_run(x)
        payload={'hook_event_name':'PreToolUse','session_id':'root','cwd':str(self.p),'tool_use_id':'c','tool_name':'spawn_agent',
                 'tool_input':{'agent_type':'als_explorer','message':'ALS_TASK=scan'}}
        out=hooks.handle(self.s,payload);self.assertEqual(out['hookSpecificOutput']['permissionDecision'],'deny')
    def test_observe_only_warns(self):
        r=self.start(draft([task(complexity='trivial')]))
        out=hooks.handle(self.s,{'hook_event_name':'PreToolUse','session_id':'root','cwd':str(self.p),'tool_use_id':'c','tool_name':'spawn_agent',
                               'tool_input':{'agent_type':'als_explorer','message':'ALS_TASK=scan'}})
        self.assertIn('systemMessage',out);self.assertNotIn('permissionDecision',out.get('hookSpecificOutput',{}))
    def test_measure_requires_terminal_and_attestation(self):
        r=self.start()
        with self.assertRaises(HarnessError):a.review_measurement(self.e,r,'bounded-fix','pass',True)
        self.e.pause(r,'test end',cancel=True)
        with self.assertRaises(HarnessError):a.review_measurement(self.e,r,'bounded-fix','pass',True)
        with self.assertRaises(HarnessError):a.review_measurement(self.e,r,'bounded-fix','fail',False)
        self.assertTrue(a.review_measurement(self.e,r,'bounded-fix','fail',True)['recorded'])
    def test_metrics_exact_subsets(self):
        r=self.start();self.event(r,n=454001,cached=432768);self.event(r,'kid',n=182619,cached=144896,model='gpt-6-luna')
        m=a.metrics(report.report(self.e,r));self.assertEqual(m['recorded_totals']['total_tokens'],636640)
        self.assertEqual(m['recorded_totals']['uncached_input_tokens'],58956)
        self.assertEqual(m['recorded_totals']['uncached_plus_output_tokens'],58976)
    def test_no_history_no_gain_claim(self):
        r=self.start();self.assertFalse(a.history(self.s,'bounded-fix')['eligible'])
        self.assertFalse(a.metrics(report.report(self.e,r))['billing_verified'])
    def test_context_symlink_rejected(self):
        (self.p/'src/link').symlink_to(Path(self.tmp.name)/'elsewhere')
        r=self.start(draft([task(context_files=['src/link'])]))
        with self.assertRaises(HarnessError):self.e.reserve(r,'scan','c')

class HistoryTests(EngineFixture, unittest.TestCase):
    def sample(self,idx,quality='pass',purpose='production',with_warning=False):
        r=self.e.start(f.prepare(draft(purpose=purpose)),session='root'+str(idx))
        self.e.reserve(r,'scan','c');self.e.bind(r,'c','kid'+str(idx));self.e.worker_stopped('root'+str(idx),'kid'+str(idx),'als_explorer')
        self.e.finish(r,'scan','Actual inspected findings')
        verify.verify(self.e,r,execute_commands=True);self.e.accept(r,'scan');self.e.complete(r)
        self.event(r,'root'+str(idx),response='root');self.event(r,'kid'+str(idx),model='gpt-6-luna',response='kid')
        a.review_measurement(self.e,r,'bounded-fix',quality,True)
        if with_warning:
            x=self.s.run(r);x['warnings']=['missing usage'];self.s.save_run(x)
        return r
    def test_five_distinct_production_runs(self):
        for i in range(4):self.sample(i)
        self.assertFalse(a.history(self.s,'bounded-fix')['eligible'])
        self.sample(4)
        h=a.history(self.s,'bounded-fix');self.assertTrue(h['eligible']);self.assertTrue(h['estimates'])
        self.assertEqual(next(iter(h['estimates'].values()))['suggested_tokens'],75)
    def test_smoke_tests_excluded(self):
        for i in range(5):self.sample(i,purpose='diagnostic')
        self.assertEqual(a.history(self.s,'bounded-fix')['reviewed_runs'],0)
    def test_failures_included_and_reduce_concurrency(self):
        for i in range(5):self.sample(i,quality='fail' if i<3 else 'pass')
        h=a.history(self.s,'bounded-fix');self.assertEqual(h['reviewed_runs'],5);self.assertEqual(h['suggested_max_concurrent'],1)
    def test_uncertain_samples_excluded(self):
        for i in range(5):self.sample(i,with_warning=True)
        self.assertFalse(a.history(self.s,'bounded-fix')['eligible'])
    def test_wrong_workload_not_reused(self):
        for i in range(5):self.sample(i)
        self.assertFalse(a.history(self.s,'different')['eligible'])

class CLITests(unittest.TestCase):
    def test_prepare_analyze_do_not_create_state_or_call_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);source=p/'draft.json';source.write_text(json.dumps(draft()))
            result=subprocess.run([sys.executable,str(ROOT/'scripts/harness.py'),'--project',tmp,'prepare','--plan',str(source)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['efficiency']['profile'],'balanced')
            self.assertFalse((p/'.astra-luna-sol').exists())
            result=subprocess.run([sys.executable,str(ROOT/'scripts/harness.py'),'--project',tmp,'analyze'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);self.assertFalse((p/'.astra-luna-sol').exists())
    def test_invalid_report_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);source=p/'report.json';source.write_text('{"schema_version":2}')
            result=subprocess.run([sys.executable,str(ROOT/'scripts/harness.py'),'--project',tmp,'analyze','--report-file',str(source)],capture_output=True,text=True)
            self.assertEqual(result.returncode,2)

if __name__=='__main__':unittest.main()
