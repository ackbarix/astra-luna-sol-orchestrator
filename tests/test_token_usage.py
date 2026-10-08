import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
USAGE=ROOT/"scripts/token_usage.py"

def rollout(folder,name,meta,entries):
    lines=[{"type":"session_meta","payload":meta},*entries]
    (folder/f"rollout-{name}.jsonl").write_text("\n".join(json.dumps(x) for x in lines)+"\n")

class UsageReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.directory=Path(self.tmp.name)
        rollout(self.directory,"root",{
            "id":"root-aa-bb","session_id":"root-aa-bb","source":"cli",
            "cwd":"/proj","timestamp":"2026-10-08T12:00:00Z"},[
            {"type":"turn_context","payload":{"model":"gpt-6-astra","effort":"medium"}},
            {"type":"token_usage_record","payload":{"usage":{
                "input_tokens":100,"cached_input_tokens":70,
                "output_tokens":10,"reasoning_output_tokens":3,"total_tokens":110}}}])
        rollout(self.directory,"child",{
            "id":"child-a","session_id":"root-aa-bb",
            "source":{"subagent":{"thread_spawn":{"agent_role":"als_worker"}}},
            "cwd":"/proj","timestamp":"2026-10-08T12:01:00Z"},[
            {"type":"turn_context","payload":{"model":"gpt-6-luna","effort":"max"}},
            {"type":"token_usage_record","payload":{"usage":{
                "input_tokens":50,"cached_input_tokens":20,
                "output_tokens":20,"total_tokens":70}}}])

    def tearDown(self):self.tmp.cleanup()

    def call(self,*args):
        return subprocess.run([sys.executable,str(USAGE),"--sessions-dir",str(self.directory),*args],capture_output=True,text=True)

    def test_latest_and_per_model(self):
        result=self.call("--latest","--format","json")
        self.assertEqual(result.returncode,0,result.stderr)
        doc=json.loads(result.stdout)
        self.assertEqual(len(doc["threads"]),2)
        self.assertEqual(doc["threads"][0]["role"],"root")
        by_model={}
        for thread in doc["threads"]:
            by_model.update(thread["per_model"])
        self.assertEqual(by_model["gpt-6-astra"]["cached_input_tokens"],70)
        self.assertEqual(by_model["gpt-6-luna"]["output_tokens"],20)

    def test_cwd_filter(self):
        self.assertEqual(self.call("--list","--cwd","/proj").returncode,0)
        self.assertEqual(self.call("--list","--cwd","/another").returncode,1)

    def test_markdown_separates_cache_and_output(self):
        result=self.call("--latest")
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("Cache hit rate on input",result.stdout)
        self.assertIn("Uncached in",result.stdout)

    def test_root_unique_prefix(self):
        self.assertEqual(self.call("--root","root-aa").returncode,0)
        self.assertEqual(self.call("--root","unknown").returncode,2)
