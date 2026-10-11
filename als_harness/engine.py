"""Deterministic task policy. Methods never call a model or silently replay work."""
from __future__ import annotations
import copy
import json
import os
import re
import signal
import subprocess
import uuid
from .common import (ACTIVE, TERMINAL, ROLES, HarnessError, boot_id, checked_path, counted_tokens, digest, ident,
                     now, overlap, process_alive, process_start, relative)
from .snapshot import capture
from . import efficiency, usage

# Attempt states that are already closed and must not be rewritten.
ATTEMPT_FINAL = {'completed', 'failed', 'interrupted', 'cancelled'}
# Task fields owned by the engine at runtime. A plan may only carry
# 'executor', and only when a prepared efficiency block routes it.
RUNTIME_TASK_KEYS = {'executor', 'claims_held', 'accepted_snapshot', 'attempts', 'status', 'result', 'run'}
# High-risk retries may move to these roles; read-only kinds may also stay read-only.
STRONG_RETRY_ROLES = {'als_hard_worker', 'als_reviewer_deep'}
READ_ONLY_KINDS = {'exploration', 'research'}
# Age limit for legacy verification locks without a recorded process start.
LEGACY_LOCK_TIMEOUT = 3600
LEGACY_LOCK_GRACE = 60


def validate_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('goal'), str) or not plan['goal'].strip():
        raise HarnessError('Plan needs a nonempty goal')
    if len(plan['goal']) > 8000:
        raise HarnessError('Goal too large; provide a bounded plan')
    p = copy.deepcopy(plan)
    p.setdefault('tasks', [])
    p.setdefault('checks', [])
    p.setdefault('criteria', [])
    if not all(isinstance(p[k], list) for k in ('tasks', 'checks', 'criteria')):
        raise HarnessError('Tasks/checks/criteria must be lists')
    if len(p['tasks']) > 100 or len(p['checks']) > 100 or len(p['criteria']) > 100:
        raise HarnessError('Plan too large')
    for collection in ('tasks', 'checks', 'criteria'):
        names = [ident(x.get('id')) for x in p[collection] if isinstance(x, dict)]
        if len(names) != len(p[collection]) or len(set(names)) != len(names):
            raise HarnessError('Invalid or duplicated plan IDs')
    for check in p['checks']:
        argv = check.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x and '\x00' not in x for x in argv):
            raise HarnessError('Check argv must be a nonempty string array (no shell parsing)')
        check.setdefault('timeout', 120)
        check.setdefault('required', True)
        if type(check['timeout']) is not int or not 1 <= check['timeout'] <= 3600 or type(check['required']) is not bool:
            raise HarnessError('Invalid check timeout/required')
    for criterion in p['criteria']:
        criterion.setdefault('required', True)
        if (not isinstance(criterion.get('description'), str) or not criterion['description']
                or type(criterion['required']) is not bool):
            raise HarnessError('Criterion requires description and boolean required')
    prepared = 'efficiency' in p
    for task in p['tasks']:
        forbidden = {k for k in RUNTIME_TASK_KEYS & task.keys() if not (k == 'executor' and prepared)}
        if forbidden:
            raise HarnessError('Runtime-only task fields are not plan input: ' + ', '.join(sorted(forbidden)))
    tasks = {t['id']: t for t in p['tasks']}
    checks = {c['id'] for c in p['checks']}
    for t in tasks.values():
        if t.get('role') not in ROLES:
            raise HarnessError('Unknown role')
        for field in ('write_paths', 'resources', 'depends_on', 'checks'):
            t.setdefault(field, [])
            if not isinstance(t[field], list) or not all(isinstance(s, str) for s in t[field]):
                raise HarnessError('Invalid task ' + field)
        t['write_paths'] = sorted(set(relative(x) for x in t['write_paths']))
        if t['write_paths'] and not ROLES[t['role']][2]:
            raise HarnessError('Read-only role cannot own write paths')
        if any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9:._/-]{0,120}', s) for s in t['resources']):
            raise HarnessError('Invalid shared resource name')
        if set(t['depends_on']) - tasks.keys() or t['id'] in t['depends_on'] or set(t['checks']) - checks:
            raise HarnessError('Missing dependency/check or self-dependency')
        t.setdefault('estimate_tokens', 0)
        if type(t['estimate_tokens']) is not int or t['estimate_tokens'] < 0:
            raise HarnessError('Invalid estimated token count')
    seen, visiting = set(), set()

    def walk(key):
        if key in visiting:
            raise HarnessError('Cyclic task dependencies')
        if key in seen:
            return
        visiting.add(key)
        for dep in tasks[key]['depends_on']:
            walk(dep)
        visiting.remove(key)
        seen.add(key)

    for key in tasks:
        walk(key)
    budget = p.setdefault('budget', {})
    if not isinstance(budget, dict):
        raise HarnessError('Budget must be an object')
    for key in ('tokens', 'seconds'):
        budget.setdefault(key, 0)
        if type(budget[key]) is not int or budget[key] < 0:
            raise HarnessError('Invalid budget')
    efficiency.validate(p)
    return p


def holds_claim(task):
    """Write/resource claims are held while a worker may still act, and after
    it stopped until finish/accept records the result."""
    return task['status'] in ACTIVE or (task['status'] == 'awaiting_validation' and bool(task.get('claims_held')))


class Engine:
    def __init__(self, store):
        self.s = store

    def start(self, plan, session=None, mode='observe', ack=False, worktree=None):
        """Register a run. `worktree` is the checkout the run works in (a
        linked git worktree of this project, or the project itself); code
        snapshots, checks and evidence freshness use it, while state stays
        keyed by the main project."""
        p = validate_plan(plan)
        if mode not in ('observe', 'guarded'):
            raise HarnessError('Unknown mode')
        if session is not None:
            ident(session)
        root = self.s.project
        if worktree is not None:
            root = self.s.worktree_of(worktree)
            if root is None:
                raise HarnessError('Worktree is not part of this project\'s repository: ' + str(worktree))
        baseline = capture(root)
        # HEAD transcripts already hold pre-run history: start their cursors at EOF.
        anchors = usage.eof_anchors(self.s, session) if session else []
        with self.s.tx():
            if session and self.s.for_session(session):
                raise HarnessError('Session already has an unfinished run')
            receipts = self.s.get('sessions', session) if session else None
            required_events = {'SessionStart', 'PreToolUse', 'PostToolUse', 'Stop'}
            if mode == 'guarded' and (not ack or not session or not receipts
                                      or not required_events <= set(receipts.get('seen', []))):
                raise HarnessError('Guarded mode needs --ack-hook-limits and real hook receipts for this session. Use observe first.')
            rid = uuid.uuid4().hex[:16]
            run = {'id': rid, 'goal': p['goal'], 'status': 'running', 'session': session, 'mode': mode,
                   'created': now(), 'baseline': baseline, 'plan': p, 'plan_hash': digest(p),
                   'completion_requested': False, 'stop_blocks': 0, 'evidence': {}, 'assessments': {},
                   'warnings': [], 'verification': None, 'worktree': str(root)}
            self.s.save_run(run)
            for task in p['tasks']:
                item = dict(task, run=rid, status='pending', attempts=[], result=None)
                self.s.save_task(item)
            usage.store_anchors(self.s, rid, anchors)
            self.s.event('start:' + rid, rid, 'run_started', {'mode': mode, 'requested_head': 'gpt-6-astra/medium'})
        return rid

    def live_tasks(self):
        """Tasks of non-terminal runs. Every cross-run scan must use this:
        completed/cancelled runs can never release anything again."""
        closed = {r['id'] for r in self.s.runs() if r['status'] in TERMINAL}
        return [t for t in self.s.tasks() if t['run'] not in closed]

    def root(self, run):
        """Checkout the run works in (see Store.run_root)."""
        return self.s.run_root(run)

    def snapshot(self, rid):
        return capture(self.root(self.s.run(rid)))

    # Verification lock -------------------------------------------------
    # run['verification'] = {'ticket', 'pid', 'pid_start', 'boot_id',
    #   'started', 'pgid', 'pgid_start', 'check'}: pid/pid_start identify the
    # verify process holding the lock; pgid/pgid_start identify the check
    # process group it started (start_new_session=True, so pgid == leader pid).

    def new_verification_lock(self, ticket):
        pid = os.getpid()
        return {'ticket': ticket, 'pid': pid, 'pid_start': process_start(pid), 'boot_id': boot_id(),
                'started': now(), 'pgid': None, 'pgid_start': None, 'check': None}

    def verification_live(self, lock, run=None):
        """A lock is live while its verify process exists with the recorded
        start time in the same boot. Missing pids, recycled pids and locks
        from an earlier boot are stale.

        A legacy (0.4) lock has no pid_start, so a recycled pid would keep it
        live forever: it is also stale once older than its check's timeout
        (3600 s when the check is unknown) plus 60 s.
        """
        current_boot = boot_id()
        if lock.get('boot_id') and current_boot and lock['boot_id'] != current_boot:
            return False
        if lock.get('pid_start') is None:
            timeout = LEGACY_LOCK_TIMEOUT
            checks = (run or {}).get('plan', {}).get('checks', [])
            for check in checks:
                if check.get('id') == lock.get('check') and type(check.get('timeout')) is int:
                    timeout = check['timeout']
            started = lock.get('started')
            if not isinstance(started, (int, float)) or now() - started > timeout + LEGACY_LOCK_GRACE:
                return False
        return process_alive(lock.get('pid'), lock.get('pid_start'))

    def _kill_check_group(self, lock):
        """SIGKILL the recorded check group only if its leader is still the
        exact process verify started; never signal any other group."""
        pgid = lock.get('pgid')
        start = lock.get('pgid_start')
        if type(pgid) is not int or pgid <= 1 or start is None:
            return False
        if lock.get('boot_id') != boot_id() or not process_alive(pgid, start):
            return False
        try:
            if os.getpgid(pgid) != pgid:
                return False
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def release_verification(self, run, reason):
        """Clear run's lock (caller saves the run inside its transaction),
        killing a still-running check group that we recorded."""
        lock = run.get('verification')
        if not lock:
            return False
        killed = self._kill_check_group(lock)
        run['verification'] = None
        self.s.event('verification-cleared:' + str(lock.get('ticket')), run['id'], 'verification_lock_cleared',
                     {'reason': reason, 'pid': lock.get('pid'), 'pgid': lock.get('pgid'),
                      'check': lock.get('check'), 'check_group_killed': killed})
        return killed

    def live_verifications(self):
        """Runs holding a live verification lock. Must run inside a
        transaction: stale locks (dead owner) and locks left on terminal runs
        are cleared and recorded as verification_lock_cleared events."""
        live = []
        for run in self.s.runs():
            lock = run.get('verification')
            if not lock:
                continue
            if run['status'] in TERMINAL:
                self.release_verification(run, 'run is ' + run['status'])
                self.s.save_run(run)
            elif self.verification_live(lock, run):
                live.append(run)
            else:
                self.release_verification(run, 'verify process no longer running')
                self.s.save_run(run)
        return live

    def budget(self, run):
        """Soft budget position in counted tokens (uncached input + output)."""
        rows = self.s.db.execute('SELECT thread,doc FROM usage WHERE run=?', (run['id'],)).fetchall()
        confirmed = 0
        # Consume estimates as confirmed worker usage arrives, avoiding double
        # reservation of the same already-accounted responses.
        by_thread = {}
        for row in rows:
            tokens = counted_tokens(json.loads(row['doc'])['usage'])
            confirmed += tokens
            by_thread[row['thread']] = by_thread.get(row['thread'], 0) + tokens
        active = [t for t in self.s.tasks(run['id']) if t['status'] in ACTIVE]
        reserved = sum(max(0, t['estimate_tokens'] - by_thread.get(t['attempts'][-1].get('thread_id'), 0))
                       for t in active if t.get('attempts') and t.get('executor') != 'head')
        seconds = max(0, run.get('closed_at', now()) - run['created'])
        limits = run['plan']['budget']
        fractions = []
        if limits['tokens']:
            fractions.append(confirmed / limits['tokens'])
        if limits['seconds']:
            fractions.append(seconds / limits['seconds'])
        fraction = max(fractions, default=0)
        if fraction >= 1:
            stage = 'stop_new_work'
        elif fraction >= .9:
            stage = 'reduce_parallelism'
        elif fraction >= .7:
            stage = 'warning'
        else:
            stage = 'normal'
        return {'confirmed_tokens': confirmed, 'reserved_estimate': reserved,
                'elapsed_wall_seconds': round(seconds, 2), 'stage': stage, 'limits': limits,
                'response_records': len(rows),
                'unmeasured_inflight': any(not t['estimate_tokens'] for t in active),
                'usage_is_lower_bound': True, 'hard_billing_limit': False}

    def reserve(self, rid, tid, call_id, role=None, requested=None, as_head=False):
        ident(call_id)
        with self.s.tx():
            run = self.s.run(rid)
            task = self.s.task(rid, tid)
            prior = next((a for a in task['attempts'] if a['call_id'] == call_id), None)
            if prior:
                ok = prior['status'] in ('reserved', 'running')
                return {'allowed': ok, 'duplicate': True, 'attempt': prior['number'],
                        'reasons': [] if ok else ['completed call ID cannot be dispatched again']}
            reasons = []
            is_head = task.get('executor') == 'head'
            if as_head and (not is_head or not run['plan'].get('efficiency')):
                reasons.append('begin-head requires a task assigned to HEAD')
            if not as_head:
                reasons.extend(efficiency.admission(run['plan'], task, self.s.tasks(rid), requested))
            root = self.root(run)
            if run['plan'].get('efficiency'):
                for path in task.get('context_files', []):
                    checked_path(root, path)
            for path in task['write_paths']:
                checked_path(root, path)
            if run['status'] != 'running':
                reasons.append('run is not running')
            if task['status'] != 'pending':
                reasons.append('task is not pending')
            if len(task['attempts']) >= 3:
                reasons.append('three-attempt limit reached')
            if role is not None and role != task['role']:
                reasons.append('role differs from task contract')
            if requested:
                pinned_model, pinned_effort = ROLES[task['role']][0], ROLES[task['role']][1]
                if (requested.get('model') not in (None, pinned_model)
                        or requested.get('reasoning_effort') not in (None, pinned_effort)):
                    reasons.append('model/effort override conflicts with pinned role')
            if any(self.s.task(rid, d)['status'] != 'completed' for d in task['depends_on']):
                reasons.append('dependency not completed')
            all_tasks = self.live_tasks()
            held = [t for t in all_tasks if holds_claim(t)]
            active = [t for t in all_tasks
                      if t['run'] == rid and t['status'] in ACTIVE and t.get('executor') != 'head']
            policy = run['plan'].get('efficiency', {})
            max_children = policy.get('max_concurrent', 6)
            if not as_head and len(active) >= max_children:
                reasons.append('six-child capacity reached' if max_children == 6
                               else 'optimized concurrent worker limit reached')
            max_writers = policy.get('max_writers', 2)
            if task['write_paths'] and sum(bool(t['write_paths']) for t in held) >= max_writers:
                reasons.append('two-writer capacity reached' if max_writers == 2
                               else 'high-risk single-writer limit reached')
            for other in held:
                paths_overlap = any(overlap(a, b) for a in task['write_paths'] for b in other['write_paths'])
                if paths_overlap or set(task['resources']) & set(other['resources']):
                    reasons.append('resource owned by ' + other['run'] + '/' + other['id'])
            if self.live_verifications():
                reasons.append('verification owns stable worktree')
            budget = self.budget(run)
            if budget['stage'] == 'stop_new_work':
                reasons.append('soft budget reached')
            if budget['stage'] == 'reduce_parallelism' and active:
                reasons.append('90% budget: serialize new work')
            cap = budget['limits']['tokens']
            finalization = (cap * policy.get('finalization_reserve_percent', 0) + 99) // 100 if cap else 0
            if cap and policy and not as_head and not task['estimate_tokens']:
                reasons.append('set a worker estimate before using an explicit soft budget')
            if cap and not as_head and task['estimate_tokens']:
                projected = budget['confirmed_tokens'] + budget['reserved_estimate'] + task['estimate_tokens'] + finalization
                if projected > cap:
                    reasons.append('reservation estimate exceeds remaining soft budget')
            if reasons:
                self.s.event('reject:' + rid + ':' + call_id, rid, 'reservation_rejected', {'task': tid, 'reasons': reasons})
                return {'allowed': False, 'reasons': reasons}
            attempt = {'number': len(task['attempts']) + 1, 'call_id': call_id, 'thread_id': None, 'started': now(),
                       'role': task['role'], 'requested_model': ROLES[task['role']][0],
                       'requested_effort': ROLES[task['role']][1], 'status': 'reserved', 'ended': None}
            if as_head:
                attempt.update(executor='head', requested_model='gpt-6-astra', requested_effort='medium', status='running')
            elif requested is not None:
                message = requested.get('message', requested.get('prompt', ''))
                is_text = isinstance(message, str)
                attempt['context'] = {'message_chars': len(message) if is_text else None,
                                      'message_sha256': digest(message.encode()) if is_text else None,
                                      'fork_context': requested.get('fork_context', 'not_exposed_or_unspecified')}
            task['attempts'].append(attempt)
            task['status'] = attempt['status']
            task['claims_held'] = True
            self.s.save_task(task)
            self.s.event('reserve:' + rid + ':' + call_id, rid, 'task_reserved', {'task': tid, 'attempt': attempt['number']})
            return {'allowed': True, 'attempt': attempt['number']}

    def begin_head(self, rid, tid):
        # Resource admission without spawning or pretending a child exists.
        return self.reserve(rid, tid, 'head-' + uuid.uuid4().hex, as_head=True)

    def bind(self, rid, call_id, thread):
        ident(thread)
        with self.s.tx():
            run = self.s.run(rid)
            tasks = [t for t in self.s.tasks(rid) if t['attempts'] and t['attempts'][-1]['call_id'] == call_id]
            if len(tasks) != 1:
                raise HarnessError('Cannot correlate spawn response to one task')
            task = tasks[0]
            attempt = task['attempts'][-1]
            if attempt['thread_id'] not in (None, thread):
                raise HarnessError('Conflicting thread identity')
            if attempt['status'] not in ('reserved', 'running'):
                return {'bound': False, 'reason': 'attempt already ' + attempt['status']}
            # Lifecycle Stop can arrive before the tool response; never undo it.
            worker = self.s.get('workers', thread) or {}
            attempt['thread_id'] = thread
            ended = worker.get('stopped')
            attempt['ended'] = ended
            if run['status'] != 'running':
                # A paused run keeps the task uncertain until resume reconciles it.
                self.s.save_task(task)
                return {'bound': True, 'reason': 'run is ' + run['status'] + '; task left ' + task['status']}
            attempt['status'] = 'awaiting_validation' if ended else 'running'
            task['status'] = attempt['status']
            self.s.save_task(task)
            return {'bound': True, 'reason': None}

    def worker_stopped(self, sid, thread, role):
        with self.s.tx():
            worker = self.s.get('workers', thread) or {}
            worker.update(role=role, stopped=worker.get('stopped') or now())
            self.s.worker(thread, sid, worker)
            for task in self.s.tasks():
                if task['attempts'] and task['attempts'][-1].get('thread_id') == thread and task['status'] in ACTIVE:
                    task['status'] = 'awaiting_validation'
                    task['attempts'][-1]['status'] = task['status']
                    task['attempts'][-1]['ended'] = now()
                    self.s.save_task(task)

    def finish(self, rid, tid, note, failed=False, stopped=False, error_code='work_failure'):
        """Record a worker result and release its claims.

        Claim release never depends on the code fingerprint: when capture()
        fails the result is recorded with snapshot None and a run warning.
        """
        if not note or len(note) > 4000:
            raise HarnessError('A short evidence/result note is required')
        snapshot, warning = self._try_capture(rid)
        with self.s.tx():
            run = self.s.run(rid)
            task = self.s.task(rid, tid)
            if run['status'] in TERMINAL:
                raise HarnessError('Run is terminal')
            if not task['attempts'] or task['status'] not in ACTIVE | {'awaiting_validation'}:
                raise HarnessError('Task is not in progress')
            if task['status'] in ACTIVE and not stopped and task.get('executor') != 'head':
                raise HarnessError('Confirm worker stopped before releasing its claims')
            digest_now = snapshot['digest'] if snapshot else None
            task['status'] = 'failed' if failed else 'awaiting_validation'
            task['claims_held'] = False
            task['result'] = {'note': note, 'snapshot': digest_now, 'basis': 'caller_reported', 'stamp': now()}
            attempt = task['attempts'][-1]
            attempt['status'] = task['status']
            attempt['ended'] = attempt.get('ended') or now()
            attempt['result_recorded_at'] = now()
            if failed:
                attempt['failure'] = digest({'code': error_code, 'snapshot': digest_now})
            self.s.save_task(task)
            self._observe(run, digest_now, warning, 'finish ' + tid)
            self.s.save_run(run)

    def accept(self, rid, tid):
        snap = self.snapshot(rid)['digest']
        with self.s.tx():
            run = self.s.run(rid)
            task = self.s.task(rid, tid)
            if run['status'] in TERMINAL:
                raise HarnessError('Run is terminal')
            if task['status'] != 'awaiting_validation' or not task.get('result'):
                raise HarnessError('Result must be recorded before acceptance')
            attempt = task['attempts'][-1] if task['attempts'] else {}
            started = attempt.get('started') or 0
            if not attempt.get('result_recorded_at') or task['result'].get('stamp', 0) < started:
                raise HarnessError('Record this attempt\'s result with finish before acceptance')
            for key in task['checks']:
                if not self.evidence_valid(run['evidence'].get(key), snap):
                    raise HarnessError('Missing current passing task check: ' + key)
            # Acceptance records HEAD judgment, not objective model quality.
            task['status'] = 'completed'
            task['claims_held'] = False
            task['accepted_snapshot'] = snap
            if task['attempts']:
                task['attempts'][-1]['status'] = 'completed'
            self.s.save_task(task)
            self._observe(run, snap)
            self.s.save_run(run)

    def retry(self, rid, tid, approach, role=None):
        if not approach or len(approach) > 2000:
            raise HarnessError('Explain the changed approach')
        with self.s.tx():
            run = self.s.run(rid)
            task = self.s.task(rid, tid)
            if run['status'] != 'running' or task['status'] != 'failed':
                raise HarnessError('Retry requires a failed, stopped task in a running run')
            if len(task['attempts']) >= 3:
                raise HarnessError('Three-attempt limit; pause and reassess')
            failures = [a.get('failure') for a in task['attempts'] if a.get('failure')]
            if len(failures) >= 2 and failures[-1] == failures[-2]:
                raise HarnessError('Repeated failure without code progress; automatic retry refused')
            if any(a.get('next_approach') == approach for a in task['attempts']):
                raise HarnessError('Identical retry approach')
            if role and role not in ROLES:
                raise HarnessError('Unknown retry role')
            efficient = run['plan'].get('efficiency')
            if efficient and role and task.get('risk') == 'high':
                read_only_kind = task.get('kind') in READ_ONLY_KINDS and not ROLES[role][2]
                if role not in STRONG_RETRY_ROLES and role != task['role'] and not read_only_kind:
                    raise HarnessError('High-risk retry cannot be downgraded to a light role')
            if role and task['write_paths'] and not ROLES[role][2]:
                raise HarnessError('Read-only retry cannot inherit write ownership')
            task['attempts'][-1]['next_approach'] = approach
            if role:
                task['role'] = role
                if efficient:
                    task['role_override_reason'] = approach[:500]
            task['status'] = 'pending'
            # The failed attempt's result must never satisfy a later accept.
            task['result'] = None
            self.s.save_task(task)
            # No spawn or external action occurs here.

    def assess(self, rid, key, note, actor):
        snap = self.snapshot(rid)['digest']
        if not note or not actor:
            raise HarnessError('Assessment needs note and actor')
        with self.s.tx():
            run = self.s.run(rid)
            if run['status'] in TERMINAL or key not in {c['id'] for c in run['plan']['criteria']}:
                raise HarnessError('Unknown criterion or terminal run')
            run['assessments'][key] = {'note': note[:2000], 'actor': actor[:100], 'snapshot': snap,
                                       'basis': 'self_reported_assessment', 'stamp': now()}
            self._observe(run, snap)
            self.s.save_run(run)

    def _try_capture(self, rid):
        """(snapshot, None) or (None, warning) when the fingerprint fails."""
        try:
            return self.snapshot(rid), None
        except (HarnessError, OSError, UnicodeError, subprocess.SubprocessError) as ex:
            return None, 'code snapshot unavailable (' + type(ex).__name__ + '): ' + str(ex)[:200]

    def _observe(self, run, snap, warning=None, context=''):
        """Record a fresh capture taken under the run's authority so resume
        compares against what the harness last saw, not only verify/resume."""
        if snap:
            run['last_snapshot'] = snap
        if warning:
            run.setdefault('warnings', []).append((context + ': ' if context else '') + warning)

    def evidence_valid(self, evidence, snap):
        if not evidence or evidence['status'] != 'passed' or evidence['snapshot'] != snap:
            return False
        try:
            path = self.s.evidence_path(evidence['log_path'])
            return digest(path.read_bytes()) == evidence['log_sha256']
        except (OSError, HarnessError, KeyError):
            return False

    def problems(self, run, snap):
        out = []
        for task in self.s.tasks(run['id']):
            if task['status'] != 'completed':
                out.append('task ' + task['id'] + ': ' + task['status'])
        if not run['plan']['checks'] and not run['plan']['criteria']:
            out.append('no acceptance checks or criteria defined')
        for check in run['plan']['checks']:
            if not check['required']:
                continue
            if not self.evidence_valid(run['evidence'].get(check['id']), snap):
                out.append('missing/failed/stale check ' + check['id'])
        for criterion in run['plan']['criteria']:
            if criterion['required'] and run['assessments'].get(criterion['id'], {}).get('snapshot') != snap:
                out.append('missing/stale assessment ' + criterion['id'])
        if run.get('verification'):
            out.append('verification still running or interrupted')
        return out

    def complete(self, rid):
        """Certify completion. A completed run is never downgraded: completing
        it again returns the recorded result for the same code, or an error
        when the code differs."""
        snap = self.snapshot(rid)['digest']
        with self.s.tx():
            self.live_verifications()
            run = self.s.run(rid)
            if run['status'] == 'completed':
                if run.get('completed_snapshot') != snap:
                    raise HarnessError('Completed run belongs to different code; start a new run')
                return {'completed': True, 'problems': [], 'already_completed': True}
            if run['status'] != 'running':
                raise HarnessError('Resume/reconcile the run before completing')
            issues = self.problems(run, snap)
            run['completion_requested'] = bool(issues)
            run['completion_problems'] = issues
            self._observe(run, snap)
            if not issues:
                run['status'] = 'completed'
                run['completed_snapshot'] = snap
                run.setdefault('closed_at', now())
            self.s.save_run(run)
            return {'completed': not issues, 'problems': issues}

    def pause(self, rid, reason, cancel=False):
        """Pause (active tasks become uncertain) or cancel a run.

        Cancel releases every claim: claim-holding tasks become 'cancelled'
        and their open attempts are marked cancelled, because a terminal run
        can never finish, retry or accept them later. Both clear the run's
        verification lock and SIGKILL its recorded check group if that exact
        process is still alive. Neither path needs a code fingerprint.
        """
        with self.s.tx():
            run = self.s.run(rid)
            if run['status'] in TERMINAL:
                return {'check_group_killed': False}
            run['status'] = 'cancelled' if cancel else 'paused'
            run['pause_reason'] = reason[:1000]
            run['completion_requested'] = False
            if cancel:
                run.setdefault('closed_at', now())
            killed = self.release_verification(run, 'run cancelled' if cancel else 'run paused')
            for task in self.s.tasks(rid):
                if cancel and (task['status'] in ACTIVE or task.get('claims_held')):
                    task['status'] = 'cancelled'
                    task['claims_held'] = False
                    if task['attempts'] and task['attempts'][-1]['status'] not in ATTEMPT_FINAL:
                        task['attempts'][-1]['status'] = 'cancelled'
                        task['attempts'][-1]['ended'] = task['attempts'][-1].get('ended') or now()
                    self.s.save_task(task)
                elif not cancel and task['status'] in ACTIVE:
                    task['status'] = 'uncertain'
                    self.s.save_task(task)
            self.s.save_run(run)
            return {'check_group_killed': bool(killed)}

    def resume(self, rid, accept_drift=False, workers_stopped=False, session=None):
        """Reconcile a paused/blocked run without replaying work.

        Only reserved/running/uncertain tasks need confirmation that their
        worker stopped; with --workers-stopped they become failed/interrupted.
        awaiting_validation tasks (worker known stopped) keep their status and
        stay finish-able. A stale verification lock is cleared automatically;
        a live one needs --workers-stopped, which clears it and kills the
        recorded check group.
        """
        snapshot = self.snapshot(rid)
        anchors = usage.eof_anchors(self.s, session) if session else []
        with self.s.tx():
            run = self.s.run(rid)
            if run['status'] == 'cancelled':
                raise HarnessError('Cancelled runs are not auto-resumable; create a new run')
            if run['status'] == 'completed':
                raise HarnessError('Completed run; create a new run')
            if snapshot['digest'] != run.get('last_snapshot', run['baseline']['digest']) and not accept_drift:
                raise HarnessError('Worktree/HEAD differs: inspect changes, then --accept-drift')
            active = [t for t in self.s.tasks(rid) if t['status'] in ACTIVE]
            lock = run.get('verification')
            if lock and not self.verification_live(lock, run):
                self.release_verification(run, 'verify process no longer running')
            if (active or run.get('verification')) and not workers_stopped:
                raise HarnessError('Confirm old workers/verification process stopped; no automatic duplicate dispatch')
            if session:
                ident(session)
                other = self.s.for_session(session)
                if other and other['id'] != rid:
                    raise HarnessError('Session bound to another run')
                run['session'] = session
                usage.store_anchors(self.s, rid, anchors)
            if workers_stopped:
                for task in active:
                    task['status'] = 'failed'
                    task['claims_held'] = False
                    if task['attempts']:
                        task['attempts'][-1]['status'] = 'interrupted'
                        task['attempts'][-1]['ended'] = now()
                    self.s.save_task(task)
                self.release_verification(run, 'resume --workers-stopped')
            run['status'] = 'running'
            run['last_snapshot'] = snapshot['digest']
            run['completion_requested'] = False
            self.s.save_run(run)
            return {'resumed': True, 'code_restored': False, 'workers_restarted': False,
                    'snapshot': snapshot['digest']}
