"""Codex hook adapter. Observe by default; no LLM or test commands in hooks.
Wire contract: https://developers.openai.com/codex/hooks (checked 2026-10-11).
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import shlex
from .common import ACTIVE, HarnessError, digest, ident, now
from .engine import Engine
from . import usage

EVENTS = {'SessionStart', 'PreToolUse', 'PostToolUse', 'SubagentStart', 'SubagentStop', 'Stop', 'SessionEnd', 'Interrupt'}


def spawn_name(name):
    return isinstance(name, str) and name.rsplit('.', 1)[-1] in {'spawn_agent', 'Agent'}


def context(event, message):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': message[:1600]}}


def warn(message):
    return {'systemMessage': message[:1000]}


DENY_FALLBACK = 'ALS guarded mode denied this spawn; inspect the harness status (als status) for the reason.'


def deny(message):
    # Codex rejects a deny decision without a non-empty reason, failing open.
    reason = message.strip() if isinstance(message, str) else ''
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                   'permissionDecision': 'deny',
                                   'permissionDecisionReason': (reason or DENY_FALLBACK)[:1000]}}


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


def child_agent(payload):
    """Codex adds agent_id/agent_type to tool hooks only when a subagent made the call."""
    value = payload.get('agent_id')
    return ident(value) if value is not None else None


def linked_run(store, sid, agent):
    """Run for a session, or for the parent session recorded on a known child worker."""
    run = store.for_session(sid)
    if run or not agent:
        return run
    worker = store.get('workers', agent) or {}
    parent = worker.get('session')
    return store.for_session(parent) if isinstance(parent, str) and parent != sid else None


def record_receipt(store, sid, event, payload):
    """Session receipt; skips the write transaction when nothing would change."""
    receipt = store.get('sessions', sid)
    if receipt is not None and event in receipt.get('seen', []) and event != 'SessionStart':
        return receipt
    with store.tx():
        receipt = store.get('sessions', sid) or {'seen': [], 'transcripts': []}
        receipt['seen'] = sorted(set(receipt['seen']) | {event})
        receipt['last_event'] = now()
        if event == 'SessionStart':
            receipt['session_start_at'] = receipt['last_event']
            if isinstance(payload.get('source'), str):
                receipt['source'] = payload['source'][:40]
            if isinstance(payload.get('transcript_path'), str):
                receipt['transcripts'] = sorted(set(receipt['transcripts']) | {payload['transcript_path']})[-8:]
                # Validate later reads against the base Codex used here, not a later process env.
                bases = dict(receipt.get('transcript_bases') or {})
                bases.setdefault(payload['transcript_path'], str(usage.sessions_base()))
                receipt['transcript_bases'] = {k: v for k, v in bases.items() if k in receipt['transcripts']}
        store.session(sid, receipt)
    return receipt


def update_worker(store, wid, sid, **fields):
    with store.tx():
        w = store.get('workers', wid) or {}
        w.setdefault('session', sid)
        w.update(fields)
        store.worker(wid, sid, w)


def harness_script():
    home = Path(__file__).resolve().parents[1]
    installed = home / 'harness.py'
    return installed if installed.is_file() else home / 'scripts/harness.py'


def cli_hint(store):
    """How the user/HEAD invokes the harness for this store's scope."""
    if getattr(store, 'scope', 'project') == 'global':
        return 'als'
    return 'python3 .astra-luna-sol/harness.py'


def resume_command(store, run, sid):
    """Exact one-line resume for a run paused by a Codex SessionEnd. Never executed here."""
    if getattr(store, 'scope', 'project') == 'global':
        # `als` resolves the project from the current repository.
        argv = ['als', 'resume', '--run', run['id'], '--session', sid]
    else:
        argv = ['python3', str(harness_script()), '--project', str(store.project), 'resume', '--run', run['id'],
                '--session', sid]
    if any(t['status'] in ACTIVE or t.get('claims_held') for t in store.tasks(run['id'])):
        argv.append('--workers-stopped')
    return ' '.join(shlex.quote(x) for x in argv)


def session_end_runs(store):
    return [r for r in store.runs() if r['status'] == 'paused' and r.get('paused_by_session_end')]


def handle(store, payload):
    if not isinstance(payload, dict):
        raise HarnessError('Hook payload must be an object')
    event = payload.get('hook_event_name')
    sid = payload.get('session_id')
    if event not in EVENTS:
        return warn('ALS: unsupported hook event; no enforcement claimed')
    ident(sid)
    agent = child_agent(payload)
    cwd = Path(payload.get('cwd') or '').resolve()
    if not store.belongs(cwd):
        # The main checkout and every linked worktree of the same repository
        # belong to this project. Children forked elsewhere keep their lifecycle
        # and spawn events only when the parent session maps to a run here.
        child_event = event in {'SubagentStart', 'SubagentStop'} or (
            event in {'PreToolUse', 'PostToolUse'} and agent is not None)
        if not child_event or not linked_run(store, sid, agent):
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
    record_receipt(store, sid, event, payload)
    engine = Engine(store)
    run = linked_run(store, sid, agent)
    if not run:
        if event == 'SessionStart':
            pending = [r['id'] for r in store.runs() if r['status'] not in {'completed', 'cancelled'}][:4]
            message = ('ALS harness available; session ' + sid + '. Do not create a run for trivial work. '
                       + 'Register a plan with `' + cli_hint(store) + ' start --plan FILE --session ' + sid + '`'
                       + ' for managed work. Pending runs: ' + (','.join(pending) or 'none'))
            ended = session_end_runs(store)[:2]
            if ended:
                # SessionEnd paused these for safety; reattaching stays an explicit user step.
                message += ('\nPaused by a previous Codex SessionEnd. Only if the user wants to continue, run'
                            ' (after confirming old workers stopped; add --accept-drift only after reviewing'
                            ' reported changes):\n' + '\n'.join(resume_command(store, r, sid) for r in ended))
            return context(event, message)
        return {}
    rid = run['id']
    mode = run['mode']
    tool = payload.get('tool_name')
    # Cancellations never create continuation prompts. Children may still be
    # running: mark them uncertain and retain claims until explicit reconciliation.
    if event in {'SessionEnd', 'Interrupt'}:
        engine.pause(rid, 'Codex ' + event)
        if event == 'SessionEnd':
            with store.tx():
                r = store.run(rid)
                if r['status'] == 'paused':
                    r['paused_by_session_end'] = sid
                    store.save_run(r)
        return {}
    if event == 'SessionStart':
        if run.get('session') == sid:
            # A newly registered transcript of this run's session: skip its history.
            anchors = usage.eof_anchors(store, sid)
            if anchors:
                with store.tx():
                    usage.store_anchors(store, rid, anchors)
        message = ('ALS run ' + rid + ' is ' + run['status'] + '. Inspect `' + cli_hint(store) + ' status --run ' + rid
                   + '` before restarting any worker.'
                   + ' A resumed chat is not an automatic task replay.')
        if run['status'] == 'paused' and run.get('paused_by_session_end'):
            # Same session id after resume/compact/clear: still no automatic reattach.
            message += ('\nPaused by Codex SessionEnd; not reattached automatically. Only if the user wants to'
                        ' continue, run (add --accept-drift only after reviewing reported changes):\n'
                        + resume_command(store, run, sid))
        return context(event, message)
    if event == 'SubagentStart':
        wid = ident(payload.get('agent_id'))
        with store.tx():
            w = store.get('workers', wid) or {}
            w.setdefault('session', sid)
            w.update(started=w.get('started') or now(), role=payload.get('agent_type'), cwd=str(cwd))
            store.worker(wid, sid, w)
        return context(event, 'ALS child: perform only the assigned task. Do not spawn more agents.'
                       + ' Worker stopping is not completion proof.')
    if event == 'SubagentStop':
        wid = ident(payload.get('agent_id'))
        engine.worker_stopped(sid, wid, payload.get('agent_type'))
        fields = {'cwd': str(cwd)}
        if isinstance(payload.get('agent_transcript_path'), str):
            fields['transcript'] = payload['agent_transcript_path']
            fields['transcript_base'] = str(usage.sessions_base())
        update_worker(store, wid, sid, **fields)
        usage.sync(engine, rid)
        return {}
    if event == 'PreToolUse' and spawn_name(tool) and agent:
        # Child-origin spawn: never reserve a plan task for it; record the parent.
        call_id = ident(payload.get('tool_use_id'))
        with store.tx():
            store.event('child-spawn:' + rid + ':' + call_id, rid, 'child_spawn_attempt',
                        {'parent_agent_id': agent, 'parent_agent_type': payload.get('agent_type'),
                         'tool_use_id': call_id, 'cwd': str(cwd)})
        msg = 'ALS: child agents may not spawn; finish the assigned task and report to the parent.'
        return deny(msg) if mode == 'guarded' else warn('ALS observe: ' + msg)
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
            # Incremental, byte-bounded reads only from previously registered rollout paths.
            usage.sync(engine, rid, limit=usage.PRE_TOOL_SYNC_BYTES)
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
    if event == 'PostToolUse' and spawn_name(tool) and agent:
        return {}  # denied or warned at PreToolUse; nothing was reserved
    if event == 'PostToolUse' and spawn_name(tool):
        call_id = payload.get('tool_use_id')
        thread = response_thread(payload.get('tool_response'))
        if thread:
            try:
                bound = engine.bind(rid, call_id, thread) or {'bound': True, 'reason': None}
            except HarnessError as ex:
                return warn(str(ex))
            if not bound.get('bound'):
                return warn('ALS spawn result not bound: ' + str(bound.get('reason') or 'unknown reason')
                            + '. Inspect task status; the child may be running untracked.')
        else:
            # A network failure may conceal successful dispatch. Keep reservations
            # until a caller confirms the old worker stopped; never retry blindly.
            return warn('ALS spawn result not correlated. Inspect task status; no automatic retry.')
        return {}
    if event == 'Stop':
        usage.sync(engine, rid)
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
