import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
from test_harness import HarnessTests
from als_harness.common import HarnessError
from als_harness.state import Store
from als_harness import usage,verify,report,hooks

# Reuse only fixture helpers, not the parent's complete test class.
class HarnessEdges(unittest.TestCase):
    setUp=HarnessTests.setUp
    tearDown=HarnessTests.tearDown
    plan=HarnessTests.plan
    task=HarnessTests.task
    runid=HarnessTests.runid
    event=HarnessTests.event
    guarded=HarnessTests.guarded
    def test_evidence_deleted_or_edited_is_invalid(self):
        r=self.runid();out=verify.verify(self.e,r,execute_commands=True)
        path=self.project/out['results'][0]['log_path'];path.write_text('tampered')
        self.assertFalse(self.e.complete(r)['completed'])
    def test_verify_refuses_live_writer(self):
        r=self.runid([self.task()]);self.e.reserve(r,'one','c')
        with self.assertRaises(HarnessError):verify.verify(self.e,r,execute_commands=True)
    def test_criteria_stale_after_code_edit(self):
        p=self.plan();p['criteria']=[{'id':'outcome','description':'required behavior'}]
        r=self.e.start(p);self.e.assess(r,'outcome','checked','HEAD')
        (self.project/'source.py').write_text('changed')
        verify.verify(self.e,r,execute_commands=True)
        self.assertFalse(self.e.complete(r)['completed'])
    def test_completed_run_cannot_be_reopened_silently(self):
        r=self.runid();verify.verify(self.e,r,execute_commands=True);self.e.complete(r)
        (self.project/'source.py').write_text('human edit')
        with self.assertRaises(HarnessError):self.e.complete(r)
    def test_spent_call_cannot_redispatch(self):
        r=self.runid([self.task()]);self.e.reserve(r,'one','c');self.e.finish(r,'one','stopped',stopped=True)
        self.assertFalse(self.e.reserve(r,'one','c')['allowed'])
    def test_cross_run_shared_file_reservation(self):
        a=self.runid([self.task()],session='first');b=self.runid([self.task()],session='second')
        self.e.reserve(a,'one','c1');self.assertFalse(self.e.reserve(b,'one','c2')['allowed'])
    def test_write_path_link_refused(self):
        outside=Path(self.tmp.name)/'outside';outside.write_text('user')
        (self.project/'alias').symlink_to(outside)
        r=self.runid([self.task(path='alias')])
        with self.assertRaises(HarnessError):self.e.reserve(r,'one','c')
        self.assertEqual(outside.read_text(),'user')
    def test_no_completion_contract_is_not_complete(self):
        r=self.e.start({'goal':'no contract'})
        self.assertFalse(self.e.complete(r)['completed'])
    def test_session_collision(self):
        self.runid()
        with self.assertRaises(HarnessError):self.runid()
    def test_outside_cwd_hook_not_applied(self):
        self.runid();o=self.event('SessionEnd',cwd='/')
        self.assertIn('systemMessage',o)
        self.assertEqual(self.s.for_session('sid')['status'],'running')
    def test_missing_stop_schema_not_recorded(self):
        o=hooks.handle(self.s,{'hook_event_name':'Stop','session_id':'sid','cwd':str(self.project)})
        self.assertIn('unknown Stop schema',o['systemMessage'])
        self.assertIsNone(self.s.get('sessions','sid'))
    def test_interrupt_never_restarts(self):
        r=self.guarded([self.task()]);self.e.reserve(r,'one','c');self.e.complete(r)
        self.assertEqual(self.event('Interrupt'),{})
        self.assertEqual(self.s.run(r)['status'],'paused')
        self.assertEqual(self.s.task(r,'one')['status'],'uncertain')
        self.assertEqual(self.event('Stop'),{})
    def test_100_percent_time_budget_no_stop_continuation(self):
        r=self.guarded();self.e.complete(r)
        with self.s.tx():
            run=self.s.run(r);run['created']-=100;run['plan']['budget']['seconds']=1;self.s.save_run(run)
        self.assertNotEqual(self.event('Stop').get('decision'),'block')
    def test_partial_usage_line_not_consumed(self):
        r=self.runid();home=Path(self.tmp.name)/'home';folder=home/'sessions';folder.mkdir(parents=True)
        p=folder/'rollout-a.jsonl';meta={'type':'session_meta','payload':{'id':'sid'}}
        stamp=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
        event={'type':'token_usage_record','timestamp':stamp,'payload':{'model':'gpt-6-astra','effort':'medium','response_id':'r1','usage':{'input_tokens':5,'output_tokens':2}}}
        text=json.dumps(event)
        p.write_text(json.dumps(meta)+'\n'+text[:15])
        with patch.dict(os.environ,{'CODEX_HOME':str(home)}):
            usage.sync_path(self.s,self.s.run(r),p)
            self.assertEqual(self.e.budget(self.s.run(r))['confirmed_tokens'],0)
            with p.open('a') as f:f.write(text[15:]+'\n')
            usage.sync_path(self.s,self.s.run(r),p)
            self.assertEqual(self.e.budget(self.s.run(r))['confirmed_tokens'],7)
    def test_usage_cumulative_only_not_added(self):
        r=self.runid();home=Path(self.tmp.name)/'home';folder=home/'sessions';folder.mkdir(parents=True)
        p=folder/'rollout-a.jsonl'
        p.write_text(json.dumps({'type':'session_meta','payload':{'id':'sid'}})+'\n'+json.dumps({'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':900,'output_tokens':100}}}})+'\n')
        with patch.dict(os.environ,{'CODEX_HOME':str(home)}):usage.sync_path(self.s,self.s.run(r),p)
        self.assertEqual(self.e.budget(self.s.run(r))['confirmed_tokens'],0)
        self.assertTrue(self.s.run(r)['warnings'])

if __name__=='__main__':unittest.main()

del HarnessTests
