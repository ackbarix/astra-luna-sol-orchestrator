import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/token_usage.py'
spec=importlib.util.spec_from_file_location('usage',SCRIPT); usage=importlib.util.module_from_spec(spec);spec.loader.exec_module(usage)

def tokens(i=100,o=10):
    return dict(input_tokens=i,cached_input_tokens=0,output_tokens=o,reasoning_output_tokens=0,total_tokens=i+o)
def context(model='gpt-6-astra',effort='medium',turn=None):
    return {'type':'turn_context','payload':{'model':model,'effort':effort,'turn_id':turn}}
def record(i=100,o=10,ts='2026-10-08T12:00:01Z',**fields):
    return {'type':'token_usage_record','timestamp':ts,'payload':{'usage':tokens(i,o),**fields}}
def cumulative(i,o,ts='2026-10-08T12:00:02Z'):
    return {'type':'event_msg','timestamp':ts,'payload':{'type':'token_count','info':{'total_token_usage':tokens(i,o)}}}

class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def put(self,ident='root',root='root',role='root',day='08',events=None,started=None,**meta):
        source='cli' if role=='root' else {'subagent':{'thread_spawn':{'agent_role':role}}}
        obj={'type':'session_meta','payload':{'id':ident,'session_id':root,'source':source,'cwd':'/proj','timestamp':started or f'2026-10-{day}T12:00:00Z',**meta}}
        folder=self.dir/'2026'/'10'/day; folder.mkdir(parents=True,exist_ok=True)
        path=folder/f'rollout-{ident}.jsonl'
        path.write_text('\n'.join(json.dumps(x) for x in [obj,*(events or [])])+'\n')
        return path
    def cli(self,*args,env=None):
        return subprocess.run([sys.executable,str(SCRIPT),'--sessions-dir',str(self.dir),*args],capture_output=True,text=True,env=env)
    def report(self,*args):
        r=self.cli(*args,'--format','json'); self.assertEqual(r.returncode,0,r.stderr); return json.loads(r.stdout)
    def test_per_response_effort_split(self):
        self.put(events=[context('gpt-6.1-sol','high'),record(),context('gpt-6.1-sol','medium'),record(200,20,ts='2026-10-08T12:02:00Z')])
        report=self.report('--root','root'); rows=report['threads'][0]['per_model_effort']
        self.assertEqual({r['effort']:r['usage']['total_tokens'] for r in rows},{'high':110,'medium':220})
        self.assertEqual(report['confirmed_usage']['total_tokens'],330)
    def test_cumulative_multimodel_not_allocated_to_last(self):
        self.put(events=[context(),cumulative(100,10),context('gpt-6-luna','max'),cumulative(200,20)])
        report=self.report('--root','root');thread=report['threads'][0]
        self.assertEqual(thread['per_model'],{})
        self.assertEqual(thread['unattributed_snapshot']['usage']['total_tokens'],220)
        self.assertEqual(report['confirmed_usage']['total_tokens'],0)
        self.assertFalse(report['complete'])
    def test_cumulative_reset_is_not_summed(self):
        self.put(events=[context(),cumulative(100,10),cumulative(50,5)])
        report=self.report('--root','root')
        self.assertTrue(any('reset' in x for x in report['warnings']))
        self.assertEqual(report['confirmed_usage']['total_tokens'],0)
    def cross_day(self):
        self.put(events=[context(),record()])
        self.put('child','root','als_worker','09',[context('gpt-6-luna','max'),record(50,5,ts='2026-10-09T00:00:01Z')],started='2026-10-09T00:00:00Z')
    def test_root_across_dates_complete(self):
        self.cross_day(); self.assertEqual(self.report('--root','root')['confirmed_usage']['total_tokens'],165)
    def test_date_is_root_start_not_storage_pruning(self):
        self.cross_day(); self.assertEqual(self.report('--root','root','--date','2026-10-08')['confirmed_usage']['total_tokens'],165)
    def test_daily_timestamp_filter(self):
        self.cross_day()
        self.assertEqual(self.report('--day','2026-10-08')['confirmed_usage']['total_tokens'],110)
        self.assertEqual(self.report('--day','2026-10-09')['confirmed_usage']['total_tokens'],55)
    def test_timezone_daily(self):
        self.put(events=[context(),record(ts='2026-10-08T15:01:00Z')])
        self.assertEqual(self.report('--day','2026-10-09','--timezone','Asia/Seoul')['confirmed_usage']['total_tokens'],110)
    def test_latest_includes_root_only(self):
        self.cross_day();self.put('new','new','root','10',[context(),record(ts='2026-10-10T12:00:01Z')])
        self.assertEqual(self.report('--latest')['root'],'new')
        self.assertEqual(self.report('--latest-with-workers')['root'],'root')
    def test_input_cache_and_output_reasoning_are_subsets(self):
        ev=record();ev['payload']['usage']['cached_input_tokens']=70;ev['payload']['usage']['reasoning_output_tokens']=3
        self.put(events=[context(),ev]);r=self.report('--root','root')
        self.assertEqual(r['confirmed_usage']['total_tokens'],110)
        self.assertEqual(r['confirmed_usage']['cached_input_tokens'],70)
    def test_no_double_count_response_and_counter(self):
        self.put(events=[context(),record(),cumulative(100,10)])
        self.assertEqual(self.report('--root','root')['confirmed_usage']['total_tokens'],110)
    def test_partial_record_coverage_flagged_not_filled(self):
        self.put(events=[context(),record(),cumulative(200,20)])
        r=self.report('--root','root');self.assertEqual(r['confirmed_usage']['total_tokens'],110)
        self.assertTrue(any('totals_differ' in x for x in r['warnings']))
    def test_duplicate_response_ids(self):
        ev=record(response_id='response1');self.put(events=[context(),ev,ev])
        self.assertEqual(self.report('--root','root')['confirmed_usage']['total_tokens'],110)
    def test_replayed_file_not_double_counted(self):
        path=self.put(events=[context(),record()]);duplicate=path.parent/'rollout-copy.jsonl';duplicate.write_bytes(path.read_bytes())
        self.assertEqual(self.report('--root','root')['confirmed_usage']['total_tokens'],110)
    def test_delayed_record_uses_turn_id(self):
        self.put(events=[context('gpt-6.1-sol','high','a'),context('gpt-6.1-sol','medium','b'),record(turn_id='a')])
        self.assertEqual(self.report('--root','root')['threads'][0]['per_model_effort'][0]['effort'],'high')
    def test_corrupt_line_is_reported(self):
        path=self.put(events=[context(),record()]);path.write_text(path.read_text()+'{"bad":')
        r=self.report('--root','root');self.assertTrue(any('unreadable_line' in x for x in r['warnings']))
        self.assertEqual(self.cli('--root','root','--strict').returncode,3)
    def test_missing_timestamp_excluded_from_day(self):
        self.put(events=[context(),record(ts=None)])
        self.assertEqual(self.report('--day','2026-10-08')['confirmed_usage']['total_tokens'],0)
        self.assertEqual(self.report('--root','root')['confirmed_usage']['total_tokens'],110)
    def test_parent_lineage_without_session_id(self):
        self.put(events=[context(),record()])
        self.put('child',None,'als_worker',events=[context('gpt-6-luna','max'),record(50,5)],parent_thread_id='root')
        self.assertEqual(self.report('--root','root')['confirmed_usage']['total_tokens'],165)
    def test_guardians_opt_in(self):
        self.put(events=[context(),record()]);self.put('guard','root','guardian',events=[context(),record(50,5)])
        self.assertEqual(self.report('--root','root')['confirmed_usage']['total_tokens'],110)
        self.assertEqual(self.report('--root','root','--include-guardian')['confirmed_usage']['total_tokens'],165)
    def test_cwd_no_leak_of_other_project(self):
        self.put(events=[context(),record()]);self.put('other','other',events=[context(),record()],cwd='/elsewhere')
        r=self.report('--list','--cwd','/proj');self.assertEqual(len(r['sessions']),1)
    def test_cli_invalid_selection(self):
        self.put(events=[context(),record()])
        for args in [('--root','missing'),('--day','bad'),('--day','2026-10-08','--date','2026-10-08'),('--list','--limit','0')]:
            with self.subTest(args=args): self.assertEqual(self.cli(*args).returncode,2)
    def test_string_subagent_roles(self):
        for role in ('review','compact','memory_consolidation'):
            self.assertEqual(usage.thread_role({'source':{'subagent':role}}),(role,''))
    def test_usage_file_read_only(self):
        p=self.put(events=[context(),record()]);before=p.read_bytes();self.report('--root','root');self.assertEqual(p.read_bytes(),before)
    def test_no_raw_prompt_leak(self):
        self.put(events=[context(),{'type':'event_msg','payload':{'type':'user_message','message':'secret-should-not-appear'}},record()])
        self.assertNotIn('secret-should-not-appear',self.cli('--root','root').stdout)
    def test_invalid_usage_rejected(self):
        ev=record();ev['payload']['usage']['cached_input_tokens']=1000;self.put(events=[context(),ev])
        r=self.report('--root','root');self.assertEqual(r['confirmed_usage']['total_tokens'],0);self.assertFalse(r['complete'])

    def test_inherited_pre_spawn_usage_not_counted_again(self):
        self.put(events=[context(),record()])
        self.put('child','root','als_worker','09', [context('gpt-6-luna','max'),record(),record(50,5,ts='2026-10-09T12:01:00Z')])
        r=self.report('--root','root')
        self.assertEqual(r['confirmed_usage']['total_tokens'],165)
        self.assertTrue(any('possible_inherited_history' in x for x in r['warnings']))
