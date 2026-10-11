"""Run-scoped cursors and terminal boundaries: synthetic local JSONL only."""
import json
from datetime import datetime,timezone
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from als_harness.common import encoded
from als_harness.state import Store
from als_harness.engine import Engine
from als_harness import usage, telemetry, efficiency

def plan():return {'goal':'cursor regression','tasks':[],'checks':[],'criteria':[{'id':'ok','description':'caller checked'}]}
def stamp(n):return datetime.fromtimestamp(n,timezone.utc).isoformat()
def records(start):
    return [
      {'type':'session_meta','payload':{'id':'root'}},
      {'type':'turn_context','payload':{'model':'gpt-6-astra','effort':'medium'}},
      {'type':'token_usage_record','timestamp':stamp(start+1),'payload':{'response_id':'first','usage':{'input_tokens':90,'cached_input_tokens':50,'output_tokens':10}}},
      {'type':'token_usage_record','timestamp':stamp(start+5),'payload':{'response_id':'second','usage':{'input_tokens':190,'cached_input_tokens':50,'output_tokens':10}}},
    ]

class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();base=Path(self.tmp.name)
        self.p=base/'project';self.p.mkdir();subprocess.run(['git','init','-q',str(self.p)],check=True)
        self.home=base/'codex';(self.home/'sessions').mkdir(parents=True)
        self.log=self.home/'sessions/rollout.jsonl';self.s=Store(self.p);self.e=Engine(self.s)
        self.env=patch.dict(os.environ,{'CODEX_HOME':str(self.home)});self.env.start()
    def tearDown(self):self.env.stop();self.s.close();self.tmp.cleanup()
    def file(self,items):self.log.write_text(''.join(json.dumps(x)+'\n' for x in items))
    def n(self,r):return self.e.budget(self.s.run(r))['confirmed_tokens']
    def test_completed_root_excludes_later_task(self):
        r=self.e.start(plan(),'root');x=self.s.run(r);start=x['created'];x['closed_at']=start+2;x['status']='completed';self.s.save_run(x)
        self.file(records(start));usage.sync_path(self.s,self.s.run(r),self.log)
        self.assertEqual(self.n(r),50)
    def test_each_run_has_separate_cursor(self):
        first=self.e.start(plan(),'root');x=self.s.run(first);start=x['created'];x['closed_at']=start+2;x['status']='completed';self.s.save_run(x)
        self.file(records(start));usage.sync_path(self.s,self.s.run(first),self.log)
        second=self.e.start(plan(),'root');x=self.s.run(second);x['created']=start+3;self.s.save_run(x)
        usage.sync_path(self.s,self.s.run(second),self.log)
        self.assertEqual(self.n(first),50);self.assertEqual(self.n(second),150)
        usage.sync_path(self.s,self.s.run(second),self.log);self.assertEqual(self.n(second),150)
    def test_late_child_not_silently_dropped_after_cancel(self):
        r=self.e.start({'goal':'child','tasks':[{'id':'x','role':'als_explorer'}]},'root')
        self.e.reserve(r,'x','call');self.e.bind(r,'call','kid');x=self.s.run(r);start=x['created'];x['closed_at']=start+2;x['status']='cancelled';self.s.save_run(x)
        items=records(start);items[0]['payload']['id']='kid';self.file(items)
        usage.sync_path(self.s,self.s.run(r),self.log)
        self.assertEqual(self.n(r),200)
        self.assertTrue(any('after run closure' in w for w in self.s.run(r)['warnings']))
    def test_legacy_boundary_warning(self):
        r=self.e.start(plan(),'root');x=self.s.run(r);start=x['created'];x['status']='completed';self.s.save_run(x)
        self.file(records(start));usage.sync_path(self.s,self.s.run(r),self.log)
        self.assertTrue(any('legacy run' in w for w in self.s.run(r)['warnings']))
    def test_incomplete_line_does_not_consume_tail(self):
        r=self.e.start(plan(),'root');start=self.s.run(r)['created'];self.file(records(start))
        with self.log.open('a') as f:f.write('{"type":')
        result=usage.sync_path(self.s,self.s.run(r),self.log);self.assertEqual(self.n(r),200)
        result=usage.sync_path(self.s,self.s.run(r),self.log);self.assertEqual(result['bytes_read'],0)

class AnalysisInputTests(unittest.TestCase):
    def report(self,row):return {'schema_version':1,'workers':[],'usage_by_thread_model_effort':[row]}
    def test_cache_never_double_counted(self):
        m=telemetry.metrics(self.report({'input_tokens':100,'cached_input_tokens':80,'output_tokens':20,'reasoning_output_tokens':5}))
        self.assertEqual(m['recorded_totals']['total_tokens'],120)
        self.assertEqual(m['recorded_totals']['uncached_plus_output_tokens'],40)
    def test_malformed_counter_refused(self):
        for value in (-1,True,1.5,'100'):
            with self.assertRaises(ValueError):telemetry.metrics(self.report({'input_tokens':value}))
    def test_bad_subsets_refused(self):
        with self.assertRaises(ValueError):telemetry.metrics(self.report({'input_tokens':100,'cached_input_tokens':101}))
    def test_empty_usage_is_unknown_not_free(self):
        out=telemetry.metrics({'schema_version':1,'workers':[]})
        self.assertTrue(out['warnings']);self.assertFalse(out['billing_verified'])
    def test_contract_characters_are_not_tokens(self):
        p=efficiency.prepare({'goal':'test','tasks':[{'id':'a','objective':'한국어로 확인','kind':'research','complexity':'bounded','risk':'low','acceptance':['근거 확인']}]})
        msg=efficiency.build_message(p,p['tasks'][0]);self.assertLess(len(msg),8000);self.assertIn('한국어',msg)

if __name__=='__main__':unittest.main()
