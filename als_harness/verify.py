"""Explicit argv-based verification; hooks NEVER execute commands from payloads."""
from __future__ import annotations
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import uuid
from .common import ACTIVE, HarnessError, checked_path, digest, now, process_info, process_start, redact
from .engine import holds_claim
from .snapshot import capture

MAX_LOG = 65536


def leader_exited(pid):
    """True once the group leader has exited, WITHOUT reaping it.

    An unreaped leader (zombie) keeps its pid, so the process group id cannot
    be recycled before our final killpg. Uses waitid(WNOWAIT) where
    available, else the process state (zombie/absent)."""
    waitid = getattr(os, 'waitid', None)
    if waitid is not None and hasattr(os, 'WNOWAIT'):
        try:
            return waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
    info = process_info(pid)
    return info is None or info[0] in ('Z', 'X')


def execute(argv, project, timeout, on_spawn=None):
    """Bounded memory/time; kill ONLY our process group on timeout/leftover pipes.

    on_spawn(proc) runs before any waiting so the caller can persist the new
    process group; if it raises, the group is killed and the error propagates.
    """
    proc = subprocess.Popen(argv, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    data = bytearray()
    truncated = False
    expired = False
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        if on_spawn is not None:
            on_spawn(proc)
        while selector.get_map():
            if time.monotonic() >= deadline:
                expired = True
                break
            for key, _ in selector.select(min(.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                space = MAX_LOG - len(data)
                data.extend(chunk[:max(0, space)])
                truncated |= len(chunk) > space
        # Wait for the leader without reaping it (see leader_exited).
        while not expired and not leader_exited(proc.pid):
            if time.monotonic() >= deadline:
                expired = True
                break
            time.sleep(.01)
    finally:
        # Child descendants belong to this new group; never kill arbitrary
        # workers. The leader is still unreaped here, so proc.pid is still
        # our group id: kill first, reap second.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        selector.close()
        proc.stdout.close()
    return {'exit_code': proc.returncode, 'timed_out': expired, 'truncated': truncated,
            'log': redact(data.decode('utf-8', errors='replace'))}


def _write_claim(task):
    return bool(task['write_paths']) and (holds_claim(task) or bool(task.get('claims_held')))


def verify(engine, rid, check_id=None, execute_commands=False):
    """Run declared checks under the run's worktree verification lock.

    The lock and the gates below only consider runs working in the same
    resolved worktree (Engine.same_root_runs): runs in other linked
    worktrees touch different files. The recorded worktree must still be
    this repository's (Store.checked_run_root), else HarnessError.

    Final verification (no check_id) requires that no non-terminal run in
    this worktree has an active worker or a held write claim. A task-level check (check_id)
    may run while other work is active, provided THIS run holds no write
    claim: read-only workers anywhere and writers in other runs do not
    block it, and a concurrent code change is still detected by the
    before/after snapshot and fails the check. The lock records this
    process and, before waiting, the check's process group so a stale lock
    (killed verify process) can be detected and cleared.
    """
    s = engine.s
    if not execute_commands:
        run = s.run(rid)
        return {'executed': False, 'checks': run['plan']['checks'],
                'message': 'Use --execute only after reviewing the declared argv commands.'}
    root = engine.root(s.run(rid))
    before = capture(root)
    ticket = uuid.uuid4().hex
    with s.tx():
        run = s.run(rid)
        if engine.live_verifications(same_as=run):
            raise HarnessError('Another verification owns the worktree')
        local = engine.same_root_runs(run)
        if run['status'] != 'running':
            raise HarnessError('Verification requires running run')
        live = engine.live_tasks()
        if check_id:
            if any(_write_claim(t) for t in live if t['run'] == rid):
                raise HarnessError('Finish or release this run\'s write claims before task-level checks')
        elif any(t['status'] in ACTIVE or _write_claim(t) for t in live if t['run'] in local):
            raise HarnessError('Stop/finish active workers and release write claims before final checks')
        checks = [c for c in run['plan']['checks'] if not check_id or c['id'] == check_id]
        if not checks:
            raise HarnessError('No matching checks')
        run['verification'] = engine.new_verification_lock(ticket)
        s.save_run(run)

    def lost_lock(run):
        return (run.get('verification') or {}).get('ticket') != ticket or run['status'] != 'running'

    def record_group(proc, check_id):
        with s.tx():
            run = s.run(rid)
            if lost_lock(run):
                raise HarnessError('Run paused/cancelled/reconciled while verification was running; evidence not accepted')
            run['verification'].update(pgid=proc.pid, pgid_start=process_start(proc.pid), check=check_id)
            s.save_run(run)

    results = []
    try:
        folder = s.evidence_dir(rid)
        for check in checks:
            record = {'id': check['id'], 'argv': check['argv'], 'cwd': str(root), 'stamp': now(),
                      'snapshot': before['digest']}
            try:
                outcome = execute(check['argv'], root, check['timeout'],
                                  on_spawn=lambda proc, key=check['id']: record_group(proc, key))
            except OSError as ex:
                outcome = {'exit_code': None, 'timed_out': False, 'truncated': False, 'log': type(ex).__name__}
            after = capture(root)
            record.update({k: v for k, v in outcome.items() if k != 'log'})
            unchanged = after['digest'] == before['digest']
            record['status'] = 'passed' if outcome['exit_code'] == 0 and not outcome['timed_out'] and unchanged else 'failed'
            record['code_changed_during_check'] = not unchanged
            filename = check['id'] + '-' + ticket + '.log'
            path = checked_path(folder, filename)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(outcome['log'])
            record['log_path'] = s.evidence_record_path(path)
            record['log_sha256'] = digest(outcome['log'].encode())
            with s.tx():
                run = s.run(rid)
                if lost_lock(run):
                    raise HarnessError('Run paused/cancelled/reconciled while verification was running; evidence not accepted')
                run['verification'].update(pgid=None, pgid_start=None, check=None)
                run['evidence'][check['id']] = record
                run['last_snapshot'] = after['digest']
                s.save_run(run)
                s.event('check:' + ticket + ':' + check['id'], rid, 'check_finished',
                        {'check': check['id'], 'status': record['status']})
            results.append(record)
            if record['code_changed_during_check']:
                break
    finally:
        with s.tx():
            run = s.run(rid)
            if (run.get('verification') or {}).get('ticket') == ticket:
                run['verification'] = None
                s.save_run(run)
    return {'executed': True, 'passed': bool(results) and all(x['status'] == 'passed' for x in results),
            'results': results}
