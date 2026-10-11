"""Records, not model recollection. Do not claim serving identity or cost savings."""
import json
from .common import redact

def report(engine,rid):
    r=engine.s.run(rid)
    tasks=engine.s.tasks(rid)
    usage={}
    for row in engine.s.db.execute('SELECT thread,doc FROM usage WHERE run=?',(rid,)):
        e=json.loads(row['doc']);key=(row['thread'],e['model'],e['effort'])
        u=usage.setdefault(key,{'thread':key[0],'recorded_model':key[1],'recorded_effort':key[2],'responses':0,'input_tokens':0,'cached_input_tokens':0,'output_tokens':0,'reasoning_output_tokens':0})
        u['responses']+=1
        for k in ('input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens'):u[k]+=e['usage'][k]
    return {'schema_version':1,'run':rid,'goal':redact(r['goal']),'status':r['status'],'mode':r['mode'],'session':r.get('session'),
            'head_requested':['gpt-6-astra','medium'],'workers':[{k:t[k] for k in ('id','role','status','attempts','write_paths','resources')} for t in tasks],
            'checks':[{k:e.get(k) for k in ('id','status','exit_code','timed_out','snapshot','stamp','log_path')} for e in r['evidence'].values()],
            'assessment_ids':list(r['assessments']),'assessments_are_self_reported':True,
            'budget':engine.budget(r),'usage_by_thread_model_effort':list(usage.values()),
            'warnings':r.get('warnings',[]),'completion_problems':r.get('completion_problems',[]),
            'last_usage_sync':r.get('last_usage_sync'),'upstream_identity_verified':False,'billing_verified':False,
            'note':'Completion checks validate declared checks, not all possible bugs. No OS sandbox or hard spend cap. Unobserved worker routing stays unverified.'}

def markdown(data):
    lines=[f"# ALS run {data['run']}",f"Status: {data['status']} | mode: {data['mode']}",
           '', '|Task|Role|State|Attempts|','|---|---|---|---:|']
    for t in data['workers']:lines.append(f"|{t['id']}|{t['role']}|{t['status']}|{len(t['attempts'])}|")
    lines+=['',f"Confirmed tokens: {data['budget']['confirmed_tokens']}; budget stage: {data['budget']['stage']}",data['note']]
    lines+=['- '+redact(w) for w in data['warnings']+data['completion_problems']]
    return '\n'.join(lines)
