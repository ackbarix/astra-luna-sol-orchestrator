"""Explicit argv-based verification; hooks NEVER execute commands from payloads."""
from __future__ import annotations
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import uuid
from .common import ACTIVE, HarnessError, checked_path, digest, now, private_dir, redact
from .snapshot import capture

MAX_LOG = 65536

def execute(argv, project, timeout):
    """Bounded memory/time; kill ONLY our process group on timeout/leftover pipes."""
    proc = subprocess.Popen(argv,cwd=project,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL,start_new_session=True)
    data, truncated, expired = bytearray(),False,False
    select=selectors.DefaultSelector();select.register(proc.stdout,selectors.EVENT_READ)
    end=time.monotonic()+timeout
    try:
        while select.get_map():
            if time.monotonic()>=end:
                expired=True;break
            for key,_ in select.select(min(.1,max(0,end-time.monotonic()))):
                chunk=os.read(key.fileobj.fileno(),8192)
                if not chunk: select.unregister(key.fileobj);continue
                space=MAX_LOG-len(data);data.extend(chunk[:max(0,space)])
                truncated |= len(chunk)>space
        if not expired:
            try:proc.wait(timeout=max(.01,end-time.monotonic()))
            except subprocess.TimeoutExpired:expired=True
    finally:
        # Child descendants belong to this new group; never kill arbitrary workers.
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.wait();select.close();proc.stdout.close()
    return {'exit_code':proc.returncode,'timed_out':expired,'truncated':truncated,
            'log':redact(data.decode('utf-8',errors='replace'))}


def verify(engine,rid,check_id=None,execute_commands=False):
    s=engine.s
    if not execute_commands:
        r=s.run(rid)
        return {'executed':False,'checks':r['plan']['checks'],'message':'Use --execute only after reviewing the declared argv commands.'}
    before=capture(s.project); ticket=uuid.uuid4().hex
    with s.tx():
        r=s.run(rid)
        if r['status']!='running':raise HarnessError('Verification requires running run')
        if any(t['status'] in ACTIVE or (t['write_paths'] and t.get('claims_held')) for t in s.tasks()):
            raise HarnessError('Stop/finish active workers and release write claims before final checks')
        if any(x.get('verification') for x in s.runs()):raise HarnessError('Another verification owns the worktree')
        checks=[c for c in r['plan']['checks'] if not check_id or c['id']==check_id]
        if not checks:raise HarnessError('No matching checks')
        r['verification']={'ticket':ticket,'started':now(),'pid':os.getpid()};s.save_run(r)
    results=[]
    try:
        folder=private_dir(s.project,'.astra-luna-sol/evidence/'+rid)
        for c in checks:
            record={'id':c['id'],'argv':c['argv'],'cwd':str(s.project),'stamp':now(),'snapshot':before['digest']}
            try:
                outcome=execute(c['argv'],s.project,c['timeout'])
            except OSError as ex:
                outcome={'exit_code':None,'timed_out':False,'truncated':False,'log':type(ex).__name__}
            after=capture(s.project)
            record.update({k:v for k,v in outcome.items() if k!='log'})
            record['status']='passed' if outcome['exit_code']==0 and not outcome['timed_out'] and after['digest']==before['digest'] else 'failed'
            record['code_changed_during_check']=after['digest']!=before['digest']
            filename=c['id']+'-'+ticket+'.log'
            path=checked_path(s.project,'.astra-luna-sol/evidence/'+rid+'/'+filename)
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(outcome['log'])
            record['log_path']=str(path.relative_to(s.project));record['log_sha256']=digest(outcome['log'].encode())
            with s.tx():
                r=s.run(rid)
                if (r.get('verification') or {}).get('ticket')!=ticket or r['status']!='running':
                    raise HarnessError('Run paused/cancelled/reconciled while verification was running; evidence not accepted')
                r['evidence'][c['id']]=record;r['last_snapshot']=after['digest'];s.save_run(r)
                s.event('check:'+ticket+':'+c['id'],rid,'check_finished',{'check':c['id'],'status':record['status']})
            results.append(record)
            if record['code_changed_during_check']:break
    finally:
        with s.tx():
            r=s.run(rid)
            if (r.get('verification') or {}).get('ticket')==ticket:
                r['verification']=None;s.save_run(r)
    return {'executed':True,'passed':bool(results) and all(x['status']=='passed' for x in results),'results':results}
