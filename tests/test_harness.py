import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
from als_harness.state import Store
from als_harness.engine import Engine,validate_plan
from als_harness.common import HarnessError
from als_harness import hooks, integration, usage, verify, report

class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.project=Path(self.tmp.name)/'project';self.project.mkdir()
        subprocess.run(['git','init','-q',str(self.project)],check=True)
        (self.project/'source.py').write_text('x=1\n')
        self.s=Store(self.project);self.e=Engine(self.s)
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def plan(self,tasks=None):
        return {'goal':'synthetic test run','tasks':tasks or [],'checks':[{'id':'unit','argv':[sys.executable,'-c','print("ok")'],'timeout':3}], 'criteria':[]}
    def task(self,key='one',path='source.py'):
        return {'id':key,'role':'als_worker','write_paths':[path] if path else [],'checks':[]}
    def runid(self,tasks=None,session='sid'):
        return self.e.start(self.plan(tasks),session)
    def event(self,event,sid='sid',**kwargs):
        payload={'hook_event_name':event,'session_id':sid,'cwd':str(self.project),'turn_id':'turn','stop_hook_active':False,'tool_use_id':'call','tool_input':{}}
        payload.update(kwargs);return hooks.handle(self.s,payload)
    def guarded(self,tasks=None):
        for event in ('SessionStart','PreToolUse','PostToolUse','Stop'):self.event(event)
        return self.e.start(self.plan(tasks),'sid','guarded',True)
    def test_completion_needs_real_check_and_stales(self):
        r=self.runid();self.assertFalse(self.e.complete(r)['completed'])
        self.assertTrue(verify.verify(self.e,r,execute_commands=True)['passed'])
        (self.project/'source.py').write_text('x=2\n')
        self.assertFalse(self.e.complete(r)['completed'])
        verify.verify(self.e,r,execute_commands=True)
        self.assertTrue(self.e.complete(r)['completed'])
    def test_failed_command_not_accepted(self):
        p=self.plan();p['checks'][0]['argv']=[sys.executable,'-c','raise SystemExit(4)']
        r=self.e.start(p)
        self.assertFalse(verify.verify(self.e,r,execute_commands=True)['passed'])
        self.assertFalse(self.e.complete(r)['completed'])
    def test_check_preview_does_not_execute(self):
        r=self.runid();o=verify.verify(self.e,r)
        self.assertFalse(o['executed']);self.assertFalse(self.s.run(r)['evidence'])
    def test_check_modifies_source_cannot_certify(self):
        p=self.plan();p['checks'][0]['argv']=[sys.executable,'-c','open("source.py","w").write("x=3")']
        r=self.e.start(p);o=verify.verify(self.e,r,execute_commands=True)
        self.assertFalse(o['passed']);self.assertTrue(o['results'][0]['code_changed_during_check'])
    def test_timed_out_check(self):
        out=verify.execute([sys.executable,'-c','import time;time.sleep(8)'],self.project,1)
        self.assertTrue(out['timed_out'])
    def test_output_bounded_and_redacted(self):
        out=verify.execute([sys.executable,'-c','print("token=SECRET"); print("x"*80000)'],self.project,3)
        self.assertNotIn('SECRET',out['log']);self.assertTrue(out['truncated'])
    def test_duplicate_reserve_not_charged_twice(self):
        r=self.runid([self.task()]);self.e.reserve(r,'one','c1')
        self.assertTrue(self.e.reserve(r,'one','c1')['duplicate'])
        self.assertEqual(len(self.s.task(r,'one')['attempts']),1)
    def test_conflict_and_independent_paths(self):
        r=self.runid([self.task('a','src'),self.task('b','src/a.py'),self.task('c','other')])
        self.assertTrue(self.e.reserve(r,'a','c1')['allowed'])
        self.assertFalse(self.e.reserve(r,'b','c2')['allowed'])
        self.assertTrue(self.e.reserve(r,'c','c3')['allowed'])
    def test_shared_resource_conflict(self):
        a=self.task('a','a');b=self.task('b','b');a['resources']=b['resources']=['db:test']
        r=self.runid([a,b]);self.e.reserve(r,'a','a1')
        self.assertFalse(self.e.reserve(r,'b','b1')['allowed'])
    def test_two_writer_limit(self):
        r=self.runid([self.task('a','a'),self.task('b','b'),self.task('c','c')])
        self.e.reserve(r,'a','a');self.e.reserve(r,'b','b')
        self.assertFalse(self.e.reserve(r,'c','c')['allowed'])
    def test_six_reader_limit(self):
        r=self.runid([self.task(str(n),'') for n in range(7)])
        for n in range(6):self.assertTrue(self.e.reserve(r,str(n),'c'+str(n))['allowed'])
        self.assertFalse(self.e.reserve(r,'6','c6')['allowed'])
    def test_dependency_gate(self):
        a=self.task('a','a');b=self.task('b','b');b['depends_on']=['a']
        r=self.runid([a,b]);self.assertFalse(self.e.reserve(r,'b','cb')['allowed'])
        self.e.reserve(r,'a','ca');self.e.finish(r,'a','done',stopped=True);self.e.accept(r,'a')
        self.assertTrue(self.e.reserve(r,'b','cb2')['allowed'])
    def test_worker_stop_is_not_complete(self):
        r=self.runid([self.task()]);self.e.reserve(r,'one','c');self.e.bind(r,'c','thread')
        self.e.worker_stopped('sid','thread','als_worker')
        self.assertEqual(self.s.task(r,'one')['status'],'awaiting_validation')
        with self.assertRaises(HarnessError):self.e.accept(r,'one')
    def test_stop_before_spawn_result(self):
        r=self.runid([self.task()]);self.e.reserve(r,'one','c');self.e.worker_stopped('sid','thread','als_worker');self.e.bind(r,'c','thread')
        self.assertEqual(self.s.task(r,'one')['status'],'awaiting_validation')
    def test_unknown_spawn_result_retains_reservation(self):
        r=self.runid([self.task()]);self.e.reserve(r,'one','call')
        self.event('PostToolUse',tool_name='spawn_agent',tool_response={'text':'some unverified text'})
        self.assertEqual(self.s.task(r,'one')['status'],'reserved')
    def test_guarded_requires_callbacks(self):
        with self.assertRaises(HarnessError):self.e.start(self.plan(),'sid','guarded',True)
    def test_guarded_rejects_unregistered_spawn(self):
        self.guarded([self.task()])
        o=self.event('PreToolUse',tool_name='spawn_agent',tool_input={'message':'hello','agent_type':'als_worker'})
        self.assertEqual(o['hookSpecificOutput']['permissionDecision'],'deny')
    def test_observe_never_blocks_invalid_spawn(self):
        self.runid([self.task()])
        o=self.event('PreToolUse',tool_name='spawn_agent',tool_input={'message':'hello'})
        self.assertNotIn('hookSpecificOutput',o)
    def test_hook_tag_reserves_exact_role(self):
        r=self.guarded([self.task()])
        self.assertEqual(self.event('PreToolUse',tool_name='spawn_agent',tool_input={'message':'ALS_TASK=one\nimplement','agent_type':'als_worker'}),{})
        self.assertEqual(self.s.task(r,'one')['status'],'reserved')
    def test_guarded_blocks_wrong_effort(self):
        self.guarded([self.task()])
        o=self.event('PreToolUse',tool_name='spawn_agent',tool_input={'message':'ALS_TASK=one','agent_type':'als_worker','reasoning_effort':'low'})
        self.assertEqual(o['hookSpecificOutput']['permissionDecision'],'deny')
    def test_normal_stop_never_continues(self):
        self.guarded();self.assertEqual(self.event('Stop'),{})
    def test_completion_continue_bounded(self):
        r=self.guarded();self.e.complete(r)
        self.assertEqual(self.event('Stop').get('decision'),'block')
        self.assertNotEqual(self.event('Stop',stop_hook_active=True).get('decision'),'block')
        self.assertEqual(self.s.run(r)['status'],'blocked')
    def test_cancel_stops_continuations(self):
        r=self.guarded();self.e.complete(r);self.e.pause(r,'cancelled',cancel=True)
        self.assertEqual(self.event('Stop'),{})
        with self.assertRaises(HarnessError):self.e.resume(r,True,True)
    def test_resume_requires_reconciliation(self):
        r=self.runid([self.task()]);self.e.reserve(r,'one','c');self.e.pause(r,'interrupted')
        self.assertEqual(self.s.task(r,'one')['status'],'uncertain')
        with self.assertRaises(HarnessError):self.e.resume(r)
        o=self.e.resume(r,workers_stopped=True)
        self.assertFalse(o['workers_restarted'])
    def test_resume_code_drift_is_not_reverted(self):
        r=self.runid();self.e.pause(r,'break');(self.project/'source.py').write_text('human edit')
        with self.assertRaises(HarnessError):self.e.resume(r)
        self.e.resume(r,accept_drift=True);self.assertEqual((self.project/'source.py').read_text(),'human edit')
    def test_three_attempt_cap(self):
        r=self.runid([self.task()])
        for n in range(3):
            self.e.reserve(r,'one','c'+str(n));self.e.finish(r,'one','failure',True,True,'different'+str(n))
            if n<2:self.e.retry(r,'one','approach'+str(n))
        with self.assertRaises(HarnessError):self.e.retry(r,'one','fourth')
    def test_identical_failure_no_progress(self):
        r=self.runid([self.task()]);self.e.reserve(r,'one','c1');self.e.finish(r,'one','failed',True,True)
        self.e.retry(r,'one','new approach');self.e.reserve(r,'one','c2');self.e.finish(r,'one','same failure',True,True)
        with self.assertRaises(HarnessError):self.e.retry(r,'one','again')
    def test_validation_of_plan_paths_cycles(self):
        for value in ('../outside','/etc/passwd','.git/config','src/*'):
            with self.assertRaises(HarnessError):validate_plan(self.plan([self.task('a',value)]))
        a=self.task('a');b=self.task('b');a['depends_on']=['b'];b['depends_on']=['a']
        with self.assertRaises(HarnessError):validate_plan(self.plan([a,b]))
    def test_assessment_not_inferred_from_worker_prose(self):
        p=self.plan();p['criteria']=[{'id':'goal','description':'desired behavior'}]
        r=self.e.start(p);verify.verify(self.e,r,execute_commands=True)
        self.assertFalse(self.e.complete(r)['completed']);self.e.assess(r,'goal','reviewed requirements','HEAD')
        self.assertTrue(self.e.complete(r)['completed']);self.assertTrue(report.report(self.e,r)['assessments_are_self_reported'])
    def test_concurrent_claims_only_one_wins(self):
        r=self.runid([self.task('a'),self.task('b')])
        def claim(t):
            s=Store(self.project)
            try:return Engine(s).reserve(r,t,'call-'+t)['allowed']
            finally:s.close()
        with ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(claim,['a','b']))
        self.assertEqual(sum(out),1)
    def test_state_permissions(self):
        self.assertEqual(self.s.path.stat().st_mode&0o777,0o600)
    def test_incremental_usage_model_effort_and_dedup(self):
        r=self.runid();home=Path(self.tmp.name)/'codex';sessions=home/'sessions';sessions.mkdir(parents=True)
        file=sessions/'rollout-test.jsonl';stamp=datetime.now(timezone.utc)+timedelta(seconds=1)
        rows=[{'type':'session_meta','payload':{'id':'sid'}}]
        for n,eff in enumerate(('high','medium')):
            rows+=[{'type':'turn_context','payload':{'model':'gpt-6.1-sol','effort':eff}},
                  {'type':'token_usage_record','timestamp':stamp.isoformat(),'payload':{'response_id':'resp'+str(n),'usage':{'input_tokens':100,'cached_input_tokens':70,'output_tokens':10,'reasoning_output_tokens':3,'total_tokens':110}}}]
        file.write_text('\n'.join(json.dumps(x) for x in rows)+'\n')
        with patch.dict(os.environ,{'CODEX_HOME':str(home)}):
            usage.sync_path(self.s,self.s.run(r),file);usage.sync_path(self.s,self.s.run(r),file)
        result=report.report(self.e,r)
        self.assertEqual(result['budget']['confirmed_tokens'],80)  # counted: uncached input + output
        self.assertEqual({x['recorded_effort'] for x in result['usage_by_thread_model_effort']},{'high','medium'})
    def test_budget_estimate_stops_new_reservation(self):
        t=self.task();t['estimate_tokens']=101;p=self.plan([t]);p['budget']={'tokens':100}
        r=self.e.start(p)
        self.assertFalse(self.e.reserve(r,'one','c')['allowed'])
    def test_usage_outside_sessions_refused(self):
        r=self.runid()
        with self.assertRaises(HarnessError):usage.sync_path(self.s,self.s.run(r),self.project/'source.py')
    def test_hook_merge_and_uninstall_user_groups(self):
        original={'description':'user','hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo user'}]}]}}
        raw=json.dumps(original).encode();new,state=integration.merge(raw,None,self.project)
        self.assertEqual(integration.parse(integration.remove(new,state)),original)
        newer,state2=integration.merge(new,state,self.project);self.assertEqual(new,newer)
    def test_hook_edit_refused(self):
        raw,state=integration.merge(None,None,self.project)
        with self.assertRaises(HarnessError):integration.remove(raw.replace(b'ALS harness',b'changed'),state)
    def test_cli_full_head_only_flow(self):
        plan=self.project/'plan.json';plan.write_text(json.dumps(self.plan()))
        script=Path(__file__).resolve().parents[1]/'scripts/harness.py'
        # Repo script runs in global scope. Never inherit the caller's ALS_* or
        # Codex session variables (e.g. an exported CODEX_THREAD_ID).
        env={k:v for k,v in os.environ.items() if not k.startswith('ALS_') and k!='CODEX_THREAD_ID'}
        env.update(ALS_STATE_HOME=str(Path(self.tmp.name)/'als-state'),CODEX_HOME=str(Path(self.tmp.name)/'codex'))
        def call(*args):return subprocess.run([sys.executable,str(script),'--project',str(self.project),*args],capture_output=True,text=True,env=env)
        s=call('start','--plan',str(plan));self.assertEqual(s.returncode,0,s.stderr);rid=json.loads(s.stdout)['run']
        self.assertEqual(call('complete','--run',rid).returncode,3)
        self.assertEqual(call('verify','--run',rid,'--execute').returncode,0)
        self.assertEqual(call('complete','--run',rid).returncode,0)

if __name__=='__main__':unittest.main()
