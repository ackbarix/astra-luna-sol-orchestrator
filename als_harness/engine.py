"""Deterministic task policy. Methods never call a model or silently replay work."""
from __future__ import annotations
import copy
import re
import uuid
from .common import ACTIVE, TERMINAL, ROLES, HarnessError, digest, ident, now, overlap, relative, checked_path
from .snapshot import capture
from . import efficiency


def validate_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('goal'), str) or not plan['goal'].strip():
        raise HarnessError('Plan needs a nonempty goal')
    if len(plan['goal']) > 8000:
        raise HarnessError('Goal too large; provide a bounded plan')
    p = copy.deepcopy(plan)
    p.setdefault('tasks', []); p.setdefault('checks', []); p.setdefault('criteria', [])
    if not all(isinstance(p[k], list) for k in ('tasks','checks','criteria')):
        raise HarnessError('Tasks/checks/criteria must be lists')
    if len(p['tasks']) > 100 or len(p['checks']) > 100 or len(p['criteria']) > 100:
        raise HarnessError('Plan too large')
    for collection in ('tasks','checks','criteria'):
        names = [ident(x.get('id')) for x in p[collection] if isinstance(x,dict)]
        if len(names) != len(p[collection]) or len(set(names)) != len(names):
            raise HarnessError('Invalid or duplicated plan IDs')
    for c in p['checks']:
        if not isinstance(c.get('argv'), list) or not c['argv'] or not all(isinstance(x,str) and x and '\x00' not in x for x in c['argv']):
            raise HarnessError('Check argv must be a nonempty string array (no shell parsing)')
        c.setdefault('timeout',120); c.setdefault('required',True)
        if type(c['timeout']) is not int or not 1 <= c['timeout'] <= 3600 or type(c['required']) is not bool:
            raise HarnessError('Invalid check timeout/required')
    for c in p['criteria']:
        c.setdefault('required',True)
        if not isinstance(c.get('description'),str) or not c['description'] or type(c['required']) is not bool:
            raise HarnessError('Criterion requires description and boolean required')
    tasks = {t['id']:t for t in p['tasks']}
    checks = {c['id'] for c in p['checks']}
    for t in tasks.values():
        if t.get('role') not in ROLES:
            raise HarnessError('Unknown role')
        for field in ('write_paths','resources','depends_on','checks'):
            t.setdefault(field, [])
            if not isinstance(t[field],list) or not all(isinstance(s,str) for s in t[field]):
                raise HarnessError('Invalid task ' + field)
        t['write_paths'] = sorted(set(relative(x) for x in t['write_paths']))
        if t['write_paths'] and not ROLES[t['role']][2]:
            raise HarnessError('Read-only role cannot own write paths')
        if any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9:._/-]{0,120}', s) for s in t['resources']):
            raise HarnessError('Invalid shared resource name')
        if set(t['depends_on']) - tasks.keys() or t['id'] in t['depends_on'] or set(t['checks']) - checks:
            raise HarnessError('Missing dependency/check or self-dependency')
        t.setdefault('estimate_tokens',0)
        if type(t['estimate_tokens']) is not int or t['estimate_tokens'] < 0:
            raise HarnessError('Invalid estimated token count')
    seen, visiting = set(), set()
    def walk(key):
        if key in visiting: raise HarnessError('Cyclic task dependencies')
        if key in seen: return
        visiting.add(key)
        for dep in tasks[key]['depends_on']: walk(dep)
        visiting.remove(key); seen.add(key)
    for key in tasks: walk(key)
    b = p.setdefault('budget',{})
    if not isinstance(b,dict): raise HarnessError('Budget must be an object')
    for key in ('tokens','seconds'):
        b.setdefault(key,0)
        if type(b[key]) is not int or b[key] < 0: raise HarnessError('Invalid budget')
    efficiency.validate(p)
    return p


class Engine:
    def __init__(self, store):
        self.s = store

    def start(self, plan, session=None, mode='observe', ack=False):
        p = validate_plan(plan)
        if mode not in ('observe','guarded'): raise HarnessError('Unknown mode')
        if session is not None: ident(session)
        baseline = capture(self.s.project)
        with self.s.tx():
            if session and self.s.for_session(session): raise HarnessError('Session already has an unfinished run')
            receipts = self.s.get('sessions',session) if session else None
            if mode == 'guarded' and (not ack or not session or not receipts or not {'SessionStart','PreToolUse','PostToolUse','Stop'} <= set(receipts.get('seen',[]))):
                raise HarnessError('Guarded mode needs --ack-hook-limits and real hook receipts for this session. Use observe first.')
            rid = uuid.uuid4().hex[:16]
            run = {'id':rid,'goal':p['goal'],'status':'running','session':session,'mode':mode,
                   'created':now(),'baseline':baseline,'plan':p,'plan_hash':digest(p),
                   'completion_requested':False,'stop_blocks':0,'evidence':{},'assessments':{},
                   'warnings':[], 'verification':None}
            self.s.save_run(run)
            for task in p['tasks']:
                item = dict(task,run=rid,status='pending',attempts=[],result=None)
                self.s.save_task(item)
            self.s.event('start:'+rid,rid,'run_started',{'mode':mode,'requested_head':'gpt-6-astra/medium'})
        return rid

    def budget(self, run):
        rows = self.s.db.execute('SELECT doc FROM usage WHERE run=?',(run['id'],)).fetchall()
        import json
        confirmed = sum(json.loads(row['doc'])['usage']['input_tokens'] + json.loads(row['doc'])['usage']['output_tokens'] for row in rows)
        active = [t for t in self.s.tasks(run['id']) if t['status'] in ACTIVE]
        # Consume estimates as confirmed worker usage arrives, avoiding double
        # reservation of the same already-accounted responses.
        by_thread = {}
        for row in self.s.db.execute('SELECT thread,doc FROM usage WHERE run=?',(run['id'],)):
            u=json.loads(row['doc'])['usage']
            by_thread[row['thread']]=by_thread.get(row['thread'],0)+u['input_tokens']+u['output_tokens']
        reserved = sum(max(0,t['estimate_tokens']-by_thread.get(t['attempts'][-1].get('thread_id'),0))
                       for t in active if t.get('attempts') and t.get('executor')!='head')
        seconds = max(0,run.get('closed_at',now())-run['created'])
        b = run['plan']['budget']
        fractions = ([confirmed/b['tokens']] if b['tokens'] else []) + ([seconds/b['seconds']] if b['seconds'] else [])
        fraction = max(fractions, default=0)
        stage = 'stop_new_work' if fraction >= 1 else 'reduce_parallelism' if fraction >= .9 else 'warning' if fraction >= .7 else 'normal'
        return {'confirmed_tokens':confirmed,'reserved_estimate':reserved,'elapsed_wall_seconds':round(seconds,2),
                'stage':stage,'limits':b,'response_records':len(rows),
                'unmeasured_inflight':any(not t['estimate_tokens'] for t in active),
                'usage_is_lower_bound':True, 'hard_billing_limit':False}

    def reserve(self, rid, tid, call_id, role=None, requested=None, as_head=False):
        ident(call_id)
        with self.s.tx():
            run, task = self.s.run(rid), self.s.task(rid,tid)
            prior = next((a for a in task['attempts'] if a['call_id']==call_id),None)
            if prior:
                ok = prior['status'] in ('reserved','running')
                return {'allowed':ok,'duplicate':True,'attempt':prior['number'],
                        'reasons':[] if ok else ['completed call ID cannot be dispatched again']}
            reasons = []
            is_head = task.get('executor') == 'head'
            if as_head and (not is_head or not run['plan'].get('efficiency')):
                reasons.append('begin-head requires a task assigned to HEAD')
            if not as_head:
                reasons.extend(efficiency.admission(run['plan'],task,self.s.tasks(rid),requested))
            if run['plan'].get('efficiency'):
                for path in task.get('context_files',[]): checked_path(self.s.project,path)
            for path in task['write_paths']:
                checked_path(self.s.project, path)
            if run['status'] != 'running': reasons.append('run is not running')
            if task['status'] != 'pending': reasons.append('task is not pending')
            if len(task['attempts']) >= 3: reasons.append('three-attempt limit reached')
            if role is not None and role != task['role']: reasons.append('role differs from task contract')
            if requested:
                if requested.get('model') not in (None, ROLES[task['role']][0]) or requested.get('reasoning_effort') not in (None,ROLES[task['role']][1]):
                    reasons.append('model/effort override conflicts with pinned role')
            if any(self.s.task(rid,d)['status'] != 'completed' for d in task['depends_on']): reasons.append('dependency not completed')
            all_tasks = self.s.tasks()
            held = [t for t in all_tasks if t['status'] in ACTIVE or (t['status']=='awaiting_validation' and t.get('claims_held'))]
            active = [t for t in all_tasks if t['run']==rid and t['status'] in ACTIVE and t.get('executor')!='head']
            policy=run['plan'].get('efficiency',{})
            max_children=policy.get('max_concurrent',6)
            if not as_head and len(active)>=max_children:
                reasons.append('six-child capacity reached' if max_children==6 else 'optimized concurrent worker limit reached')
            if task['write_paths'] and sum(bool(t['write_paths']) for t in held) >= policy.get('max_writers',2):
                reasons.append('two-writer capacity reached' if policy.get('max_writers',2)==2 else 'high-risk single-writer limit reached')
            for other in held:
                if any(overlap(a,b) for a in task['write_paths'] for b in other['write_paths']) or set(task['resources']) & set(other['resources']):
                    reasons.append('resource owned by '+other['run']+'/'+other['id'])
            if any(r.get('verification') for r in self.s.runs()): reasons.append('verification owns stable worktree')
            b = self.budget(run)
            if b['stage']=='stop_new_work': reasons.append('soft budget reached')
            if b['stage']=='reduce_parallelism' and active: reasons.append('90% budget: serialize new work')
            cap = b['limits']['tokens']
            finalization=(cap*policy.get('finalization_reserve_percent',0)+99)//100 if cap else 0
            if cap and policy and not as_head and not task['estimate_tokens']:
                reasons.append('set a worker estimate before using an explicit soft budget')
            if cap and not as_head and task['estimate_tokens'] and b['confirmed_tokens']+b['reserved_estimate']+task['estimate_tokens']+finalization > cap:
                reasons.append('reservation estimate exceeds remaining soft budget')
            if reasons:
                self.s.event('reject:'+rid+':'+call_id,rid,'reservation_rejected',{'task':tid,'reasons':reasons})
                return {'allowed':False,'reasons':reasons}
            a = {'number':len(task['attempts'])+1,'call_id':call_id,'thread_id':None,'started':now(),
                 'role':task['role'],'requested_model':ROLES[task['role']][0],'requested_effort':ROLES[task['role']][1],
                 'status':'reserved','ended':None}
            if as_head:
                a.update(executor='head',requested_model='gpt-6-astra',requested_effort='medium',status='running')
            elif requested is not None:
                message=requested.get('message',requested.get('prompt',''))
                a['context']={'message_chars':len(message) if isinstance(message,str) else None,
                              'message_sha256':digest(message.encode()) if isinstance(message,str) else None,
                              'fork_context':requested.get('fork_context','not_exposed_or_unspecified')}
            task['attempts'].append(a); task['status']=a['status']; task['claims_held']=True
            self.s.save_task(task)
            self.s.event('reserve:'+rid+':'+call_id,rid,'task_reserved',{'task':tid,'attempt':a['number']})
            return {'allowed':True,'attempt':a['number']}

    def begin_head(self,rid,tid):
        # Resource admission without spawning or pretending a child exists.
        return self.reserve(rid,tid,'head-'+uuid.uuid4().hex,as_head=True)

    def bind(self, rid, call_id, thread):
        ident(thread)
        with self.s.tx():
            tasks = [t for t in self.s.tasks(rid) if t['attempts'] and t['attempts'][-1]['call_id']==call_id]
            if len(tasks)!=1: raise HarnessError('Cannot correlate spawn response to one task')
            t=tasks[0]; a=t['attempts'][-1]
            if a['thread_id'] not in (None,thread): raise HarnessError('Conflicting thread identity')
            if a['status'] not in ('reserved','running'):
                return {'bound':False,'reason':'attempt already '+a['status']}
            # Lifecycle Stop can arrive before the tool response; never undo it.
            w=self.s.get('workers',thread) or {}
            a['thread_id']=thread
            ended=w.get('stopped')
            a['status']='awaiting_validation' if ended else 'running'
            t['status']=a['status']; a['ended']=ended
            self.s.save_task(t)
            return {'bound':True,'reason':None}

    def worker_stopped(self, sid, thread, role):
        with self.s.tx():
            w=self.s.get('workers',thread) or {}
            w.update(role=role,stopped=w.get('stopped') or now())
            self.s.worker(thread,sid,w)
            for t in self.s.tasks():
                if t['attempts'] and t['attempts'][-1].get('thread_id')==thread and t['status'] in ACTIVE:
                    t['status']='awaiting_validation'; t['attempts'][-1]['status']=t['status']; t['attempts'][-1]['ended']=now()
                    self.s.save_task(t)

    def finish(self, rid, tid, note, failed=False, stopped=False, error_code='work_failure'):
        if not note or len(note)>4000: raise HarnessError('A short evidence/result note is required')
        snapshot=capture(self.s.project)
        with self.s.tx():
            run,t = self.s.run(rid), self.s.task(rid,tid)
            if run['status'] in TERMINAL: raise HarnessError('Run is terminal')
            if not t['attempts'] or t['status'] not in ACTIVE | {'awaiting_validation'}: raise HarnessError('Task is not in progress')
            if t['status'] in ACTIVE and not stopped and t.get('executor')!='head': raise HarnessError('Confirm worker stopped before releasing its claims')
            t['status']='failed' if failed else 'awaiting_validation'; t['claims_held']=False
            t['result']={'note':note,'snapshot':snapshot['digest'],'basis':'caller_reported','stamp':now()}
            a=t['attempts'][-1]; a['status']=t['status']; a['ended']=a.get('ended') or now(); a['result_recorded_at']=now()
            if failed: a['failure']=digest({'code':error_code,'snapshot':snapshot['digest']})
            self.s.save_task(t)

    def accept(self, rid, tid):
        snap=capture(self.s.project)['digest']
        with self.s.tx():
            r,t=self.s.run(rid),self.s.task(rid,tid)
            if r['status'] in TERMINAL: raise HarnessError('Run is terminal')
            if t['status']!='awaiting_validation' or not t.get('result'): raise HarnessError('Result must be recorded before acceptance')
            for key in t['checks']:
                e=r['evidence'].get(key)
                if not self.evidence_valid(e,snap):
                    raise HarnessError('Missing current passing task check: '+key)
            # Acceptance records HEAD judgment, not objective model quality.
            t['status']='completed';t['claims_held']=False;t['accepted_snapshot']=snap
            if t['attempts']:t['attempts'][-1]['status']='completed'
            self.s.save_task(t)

    def retry(self, rid, tid, approach, role=None):
        if not approach or len(approach)>2000: raise HarnessError('Explain the changed approach')
        with self.s.tx():
            r,t=self.s.run(rid),self.s.task(rid,tid)
            if r['status']!='running' or t['status']!='failed': raise HarnessError('Retry requires a failed, stopped task in a running run')
            if len(t['attempts'])>=3: raise HarnessError('Three-attempt limit; pause and reassess')
            failures=[a.get('failure') for a in t['attempts'] if a.get('failure')]
            if len(failures)>=2 and failures[-1]==failures[-2]: raise HarnessError('Repeated failure without code progress; automatic retry refused')
            if any(a.get('next_approach')==approach for a in t['attempts']): raise HarnessError('Identical retry approach')
            if role and role not in ROLES: raise HarnessError('Unknown retry role')
            if r['plan'].get('efficiency') and role and t.get('risk')=='high' and role not in {'als_hard_worker','als_reviewer_deep'}:
                raise HarnessError('High-risk retry cannot be downgraded to a light role')
            if role and t['write_paths'] and not ROLES[role][2]: raise HarnessError('Read-only retry cannot inherit write ownership')
            t['attempts'][-1]['next_approach']=approach
            if role:
                t['role']=role
                if r['plan'].get('efficiency'):t['role_override_reason']=approach[:500]
            t['status']='pending';self.s.save_task(t)
            # No spawn or external action occurs here.

    def assess(self,rid,key,note,actor):
        snap=capture(self.s.project)['digest']
        if not note or not actor: raise HarnessError('Assessment needs note and actor')
        with self.s.tx():
            r=self.s.run(rid)
            if r['status'] in TERMINAL or key not in {c['id'] for c in r['plan']['criteria']}: raise HarnessError('Unknown criterion or terminal run')
            r['assessments'][key]={'note':note[:2000],'actor':actor[:100],'snapshot':snap,'basis':'self_reported_assessment','stamp':now()}
            self.s.save_run(r)

    def evidence_valid(self, evidence, snap):
        if not evidence or evidence['status']!='passed' or evidence['snapshot']!=snap:
            return False
        try:
            path = checked_path(self.s.project, evidence['log_path'])
            return digest(path.read_bytes()) == evidence['log_sha256']
        except (OSError, HarnessError, KeyError):
            return False

    def problems(self, r, snap):
        out=[]
        for t in self.s.tasks(r['id']):
            if t['status']!='completed':out.append('task '+t['id']+': '+t['status'])
        if not r['plan']['checks'] and not r['plan']['criteria']: out.append('no acceptance checks or criteria defined')
        for c in r['plan']['checks']:
            if not c['required']:continue
            e=r['evidence'].get(c['id'])
            if not self.evidence_valid(e,snap):out.append('missing/failed/stale check '+c['id'])
        for c in r['plan']['criteria']:
            if c['required'] and r['assessments'].get(c['id'],{}).get('snapshot')!=snap:out.append('missing/stale assessment '+c['id'])
        if r.get('verification'):out.append('verification still running or interrupted')
        return out

    def complete(self,rid):
        snap=capture(self.s.project)['digest']
        with self.s.tx():
            r=self.s.run(rid)
            if r['status'] not in ('running','completed'):raise HarnessError('Resume/reconcile the run before completing')
            if r['status']=='completed' and r.get('completed_snapshot')!=snap:
                raise HarnessError('Completed run belongs to different code; start a new run')
            issues=self.problems(r,snap)
            r['completion_requested']=bool(issues);r['completion_problems']=issues
            r['status']='running' if issues else 'completed'
            if not issues:
                r['completed_snapshot']=snap
                r.setdefault('closed_at',now())
            self.s.save_run(r)
            return {'completed':not issues,'problems':issues}

    def pause(self,rid,reason,cancel=False):
        with self.s.tx():
            r=self.s.run(rid)
            if r['status'] in TERMINAL:return
            r['status']='cancelled' if cancel else 'paused';r['pause_reason']=reason[:1000];r['completion_requested']=False
            if cancel:r.setdefault('closed_at',now())
            r['checkpoint']=r.get('last_snapshot',r['baseline']['digest'])
            for t in self.s.tasks(rid):
                if t['status'] in ACTIVE:
                    t['status']='uncertain';self.s.save_task(t)
            self.s.save_run(r)

    def resume(self,rid,accept_drift=False,workers_stopped=False,session=None):
        snapshot=capture(self.s.project)
        with self.s.tx():
            r=self.s.run(rid)
            if r['status']=='cancelled': raise HarnessError('Cancelled runs are not auto-resumable; create a new run')
            if r['status']=='completed': raise HarnessError('Completed run; create a new run')
            if snapshot['digest']!=r.get('last_snapshot',r['baseline']['digest']) and not accept_drift:
                raise HarnessError('Worktree/HEAD differs: inspect changes, then --accept-drift')
            active=[t for t in self.s.tasks(rid) if t['status'] in ACTIVE or t.get('claims_held')]
            if (active or r.get('verification')) and not workers_stopped:
                raise HarnessError('Confirm old workers/verification process stopped; no automatic duplicate dispatch')
            if session:
                ident(session)
                other=self.s.for_session(session)
                if other and other['id']!=rid:raise HarnessError('Session bound to another run')
                r['session']=session
            if workers_stopped:
                for t in active:
                    t['status']='failed';t['claims_held']=False
                    if t['attempts']:
                        t['attempts'][-1]['status']='interrupted';t['attempts'][-1]['ended']=now()
                    self.s.save_task(t)
                r['verification']=None
            r['status']='running';r['last_snapshot']=snapshot['digest'];r['completion_requested']=False
            self.s.save_run(r)
            return {'resumed':True,'code_restored':False,'workers_restarted':False,'snapshot':snapshot['digest']}
