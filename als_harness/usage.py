"""Incremental response-ledger input. Reuses ALS 0.2 normalization, not billing."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
from .common import HarnessError, digest, encoded, now

NORMALIZER=None

def normalizer():
    global NORMALIZER
    if NORMALIZER is None:
        root=Path(__file__).resolve().parents[1]
        path=root/'token_usage.py' if (root/'token_usage.py').exists() else root/'scripts/token_usage.py'
        spec=importlib.util.spec_from_file_location('als_usage_normalizer',path)
        NORMALIZER=importlib.util.module_from_spec(spec);spec.loader.exec_module(NORMALIZER)
    return NORMALIZER

def sync_path(store,run,path):
    """Only registered, readable rollouts under CODEX_HOME/sessions. 1 MiB/tick."""
    base=Path(os.environ.get('CODEX_HOME','~/.codex')).expanduser()/ 'sessions'
    p=Path(path).expanduser()
    base=base.resolve()
    if not p.is_absolute() or base not in p.resolve().parents or p.is_symlink() or p.suffix!='.jsonl':
        raise HarnessError('Usage path must be a JSONL rollout beneath CODEX_HOME/sessions')
    if any(x.is_symlink() for x in p.parents if x!=base and base in x.parents):
        raise HarnessError('Symlinked rollout parent')
    stat=p.stat(); old=store.get('cursors',str(p),'path') or {}
    key=[stat.st_dev,stat.st_ino]
    if old.get('file_key')!=key or stat.st_size<old.get('offset',0):
        cursor={'file_key':key,'offset':0,'context':{},'contexts':{},'meta':None}
    else:cursor=old
    offset=cursor['offset']; consumed=0; events=[]; notes=[]
    with p.open('rb') as f:
        f.seek(offset)
        while consumed<1024*1024:
            pos=f.tell();raw=f.readline(1024*1024+1)
            if not raw:break
            if len(raw)>1024*1024:
                notes.append('oversized rollout line; cursor not advanced');break
            if not raw.endswith(b'\n'):break
            cursor['offset']=f.tell();consumed+=len(raw)
            try:o=json.loads(raw)
            except (ValueError,UnicodeError):notes.append('malformed rollout line');continue
            if not isinstance(o,dict) or not isinstance(o.get('payload'),dict):continue
            data=o['payload'];kind=o.get('type')
            if kind=='session_meta':cursor['meta']=data;continue
            meta=cursor.get('meta') or {};thread=meta.get('id')
            known={run.get('session')}|{a.get('thread_id') for t in store.tasks(run['id']) for a in t['attempts']}
            if thread not in known or not thread:
                notes.append('unbound thread; usage excluded');continue
            if kind=='turn_context':
                context={'model':data.get('model','unknown'),'effort':data.get('effort',data.get('reasoning_effort','unknown'))}
                cursor['context']=context
                if data.get('turn_id'):cursor['contexts'][data['turn_id']]=context
                if len(cursor['contexts'])>100:cursor['contexts'].pop(next(iter(cursor['contexts'])))
            if kind=='token_usage_record' or (kind=='event_msg' and data.get('type')=='token_usage_record'):
                stamp=normalizer().parse_ts(o.get('timestamp'))
                # Only events after this work run began; never inherited session history.
                if not stamp:
                    notes.append('missing usage timestamp; excluded');continue
                if stamp.timestamp()<run['created']:continue
                warnings=[]; usage=normalizer().normalized_usage(data.get('usage'),warnings)
                notes.extend(warnings)
                if usage is None:continue
                context=cursor['contexts'].get(data.get('turn_id'),cursor['context'])
                model=data.get('model') or context.get('model','unknown')
                effort=data.get('effort') or data.get('reasoning_effort') or (context.get('effort','unknown') if model==context.get('model') else 'unknown')
                if not isinstance(model,str) or not isinstance(effort,str):notes.append('invalid identity');continue
                event={'timestamp':stamp.isoformat(),'model':model,'effort':effort,'usage':usage}
                response=data.get('response_id') or data.get('id')
                if not isinstance(response,str) or not response:
                    response=digest(event);notes.append('no response ID: exact-record deduplication used')
                events.append((thread,response,event))
            elif kind=='event_msg' and data.get('type')=='token_count':
                cursor['has_cumulative']=True
    with store.tx():
        for thread,response,event in events:
            previous=store.db.execute('SELECT doc FROM usage WHERE run=? AND thread=? AND response=?',(run['id'],thread,response)).fetchone()
            if previous and previous['doc']!=encoded(event):notes.append('conflicting response ID; first record retained')
            store.db.execute('INSERT OR IGNORE INTO usage VALUES(?,?,?,?)',(run['id'],thread,response,encoded(event)))
        store.db.execute('INSERT INTO cursors VALUES(?,?) ON CONFLICT(path) DO UPDATE SET doc=excluded.doc',(str(p),encoded(cursor)))
        r=store.run(run['id'])
        r['warnings']=sorted(set(r['warnings'])|set(notes))[:100]
        if cursor.get('has_cumulative') and not store.db.execute('SELECT 1 FROM usage WHERE run=? LIMIT 1',(run['id'],)).fetchone():
            r['warnings']=sorted(set(r['warnings'])|{'cumulative-only source: token budget incomplete'})
        r['last_usage_sync']=now();store.save_run(r)
    return {'bytes_read':consumed,'response_records_seen':len(events),'warnings':sorted(set(notes))}

def sync(engine,rid):
    r=engine.s.run(rid)
    session=engine.s.get('sessions',r.get('session')) or {}
    paths=set(session.get('transcripts',[]))
    for t in engine.s.tasks(rid):
        for a in t['attempts']:
            w=engine.s.get('workers',a.get('thread_id')) or {}
            if w.get('transcript'):paths.add(w['transcript'])
    results=[]
    for p in paths:
        try:results.append(sync_path(engine.s,r,p))
        except (OSError,HarnessError) as ex:
            results.append({'warning':str(ex)})
            with engine.s.tx():
                r=engine.s.run(rid);r['warnings']=sorted(set(r['warnings'])|{'registered usage source unreadable'})
                engine.s.save_run(r)
    return results
