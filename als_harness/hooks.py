"""Codex hook adapter. Observe by default; no LLM or test commands in hooks.
Wire contract: https://developers.openai.com/codex/hooks (checked 2026-10-11).
"""
from __future__ import annotations
import json
from pathlib import Path
import re
from .common import HarnessError, digest, ident, now
from .engine import Engine

EVENTS = {'SessionStart', 'PreToolUse', 'PostToolUse', 'SubagentStart', 'SubagentStop', 'Stop', 'SessionEnd', 'Interrupt'}


def spawn_name(name):
    return isinstance(name, str) and name.rsplit('.', 1)[-1] in {'spawn_agent', 'Agent'}


def context(event, message):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': message[:1600]}}


def warn(message):
    return {'systemMessage': message[:1000]}


def deny(message):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                   'permissionDecision': 'deny',
                                   'permissionDecisionReason': message[:1000]}}


def response_thread(value):
    """Known exact result keys only; never infer ID from free-form narrative."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    candidate = value.get('agent_id')
    if isinstance(candidate, str):
        return candidate
    nested = value.get('result')
    if isinstance(nested, dict) and isinstance(nested.get('agent_id'), str):
        return nested.get('agent_id')
    return None


def handle(store, payload):
    if not isinstance(payload, dict):
        raise HarnessError('Hook payload must be an object')
    event = payload.get('hook_event_name')
    sid = payload.get('session_id')
    if event not in EVENTS:
        return warn('ALS: unsupported hook event; no enforcement claimed')
    ident(sid)
    cwd = Path(payload.get('cwd') or '').resolve()
    if cwd != store.project and store.project not in cwd.parents:
        return warn('ALS: hook cwd outside the installed project; ignored')
    if event in {'PreToolUse', 'PostToolUse'} and (
        not isinstance(payload.get('tool_use_id'), str) or not isinstance(payload.get('tool_input'), dict)
    ):
        return warn('ALS: unknown tool hook schema; no capability receipt recorded')
    if event == 'Stop' and (
        not isinstance(payload.get('stop_hook_active'), bool) or not isinstance(payload.get('turn_id'), str)
    ):
        return warn('ALS: unknown Stop schema; no automatic continuation')
    # Store receipts, not prompts, tool commands or raw results. Repeat SessionStart
    # is harmless. A session receipt is NOT proof of a tool actually being blocked.
    with store.tx():
        receipt = store.get('sessions', sid) or {'seen': [], 'transcripts': []}
        receipt['seen'] = sorted(set(receipt['seen']) | {event})
        receipt['last_event'] = now()
        if event == 'SessionStart' and isinstance(payload.get('transcript_path'), str):
            receipt['transcripts'] = sorted(set(receipt['transcripts']) | {payload['transcript_path']})[-8:]
        store.session(sid, receipt)
    engine = Engine(store)
    run = store.for_session(sid)
    if not run:
        if event == 'SessionStart':
            pending = [r['id'] for r in store.runs() if r['status'] not in {'completed', 'cancelled'}][:4]
            return context(event, 'ALS harness available; session ' + sid + '. Do not create a run for trivial work. '
                           + 'Register a plan with harness.py start --session ' + sid
                           + ' for managed work. Pending runs: ' + (','.join(pending) or 'none'))
        return {}
    rid = run['id']
    mode = run['mode']
    tool = payload.get('tool_name')
    # Cancellations never create continuation prompts. Children may still be
    # running: mark them uncertain and retain claims until explicit reconciliation.
    if event in {'SessionEnd', 'Interrupt'}:
        engine.pause(rid, 'Codex ' + event)
        return {}
    if event == 'SessionStart':
        return context(event, 'ALS run ' + rid + ' is ' + run['status']
                       + '. Inspect harness.py status before restarting any worker.'
                       + ' A resumed chat is not an automatic task replay.')
    if event == 'SubagentStart':
        wid = ident(payload.get('agent_id'))
        with store.tx():
            w = store.get('workers', wid) or {}
            w.update(started=w.get('started') or now(), role=payload.get('agent_type'))
            store.worker(wid, sid, w)
        return context(event, 'ALS child: perform only the assigned task. Do not spawn more agents.'
                       + ' Worker stopping is not completion proof.')
    if event == 'SubagentStop':
        wid = ident(payload.get('agent_id'))
        engine.worker_stopped(sid, wid, payload.get('agent_type'))
        if isinstance(payload.get('agent_transcript_path'), str):
            with store.tx():
                w = store.get('workers', wid) or {}
                w['transcript'] = payload['agent_transcript_path']
                store.worker(wid, sid, w)
        return {}
    if event == 'PreToolUse' and spawn_name(tool):
        inp = payload.get('tool_input')
        if not isinstance(inp, dict):
            if mode == 'guarded':
                return deny('ALS cannot validate spawn arguments')
            return warn('ALS unknown spawn arguments; observe only')
        message = inp.get('message') or inp.get('prompt') or ''
        match = None
        if isinstance(message, str):
            match = re.search(r'^ALS_TASK=([A-Za-z0-9][A-Za-z0-9_.-]{0,99})\s*$', message, re.M)
        if not match:
            msg = 'ALS managed run: start spawn message with ALS_TASK=<plan task id>; use the configured agent_type.'
            return deny(msg) if mode == 'guarded' else warn(msg)
        call_id = payload.get('tool_use_id')
        if not isinstance(inp.get('agent_type'), str):
            if mode == 'guarded':
                return deny('ALS guarded spawn requires an explicit configured agent_type')
            return warn('ALS observe: missing role; routing not verified')
        try:
            # Incremental reads only from previously registered rollout paths.
            from .usage import sync
            sync(engine, rid)
            result = engine.reserve(rid, match.group(1), call_id, role=inp.get('agent_type'), requested=inp)
        except (HarnessError, OSError) as ex:
            return deny(str(ex)) if mode == 'guarded' else warn(str(ex))
        if not result['allowed']:
            if mode == 'guarded':
                return deny('; '.join(result['reasons']))
            return warn('ALS observe: ' + '; '.join(result['reasons']))
        b = engine.budget(store.run(rid))
        if b['stage'] != 'normal':
            with store.tx():
                first = store.event('budget:' + rid + ':' + b['stage'], rid, 'budget_stage', {'stage': b['stage']})
            if first:
                return context('PreToolUse', 'ALS soft budget ' + b['stage']
                               + '. Confirmed tokens are a lower bound; avoid unnecessary new work.')
        return {}  # never implicitly grant any tool permission
    if event == 'PostToolUse' and spawn_name(tool):
        call_id = payload.get('tool_use_id')
        thread = response_thread(payload.get('tool_response'))
        if thread:
            try:
                engine.bind(rid, call_id, thread)
            except HarnessError as ex:
                return warn(str(ex))
        else:
            # A network failure may conceal successful dispatch. Keep reservations
            # until a caller confirms the old worker stopped; never retry blindly.
            return warn('ALS spawn result not correlated. Inspect task status; no automatic retry.')
        return {}
    if event == 'Stop':
        from .usage import sync
        sync(engine, rid)
        # Only an explicit completion request can solicit one continuation. A
        # normal answer, blocked task, cancel, or stop_hook_active never loops.
        run = store.run(rid)
        if not run.get('completion_requested') or run['status'] != 'running':
            return {}
        try:
            result = engine.complete(rid)
        except (HarnessError, OSError):
            return warn('ALS completion evidence unavailable; not certified')
        if result['completed']:
            return {}
        with store.tx():
            r = store.run(rid)
            key = 'stop:' + rid + ':' + str(payload.get('turn_id')) + ':' + str(bool(payload.get('stop_hook_active')))
            first = store.event(key, rid, 'completion_gate', {'problems': result['problems'][:10]})
            if not first:
                return {}
            budget = engine.budget(r)
            if (mode == 'guarded' and not payload.get('stop_hook_active') and r['stop_blocks'] < 1
                    and budget['stage'] != 'stop_new_work'):
                r['stop_blocks'] += 1
                store.save_run(r)
                return {'decision': 'block',
                        'reason': 'ALS completion not verified: ' + '; '.join(result['problems'])[:900]
                        + '. Perform only authorized missing checks, or pause/report a blocker. Do not keep retrying.'}
            r['status'] = 'blocked'
            r['completion_requested'] = False
            store.save_run(r)
        return warn('ALS run not certified; paused at completion gate to avoid an automatic loop.')
    return {}
