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
    proc = subprocess.Popen(argv, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    data = bytearray()
    truncated = False
    expired = False
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
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
        if not expired:
            try:
                proc.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                expired = True
    finally:
        # Child descendants belong to this new group; never kill arbitrary workers.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        selector.close()
        proc.stdout.close()
    return {'exit_code': proc.returncode, 'timed_out': expired, 'truncated': truncated,
            'log': redact(data.decode('utf-8', errors='replace'))}


def verify(engine, rid, check_id=None, execute_commands=False):
    s = engine.s
    if not execute_commands:
        run = s.run(rid)
        return {'executed': False, 'checks': run['plan']['checks'],
                'message': 'Use --execute only after reviewing the declared argv commands.'}
    before = capture(s.project)
    ticket = uuid.uuid4().hex
    with s.tx():
        run = s.run(rid)
        if run['status'] != 'running':
            raise HarnessError('Verification requires running run')
        if any(t['status'] in ACTIVE or (t['write_paths'] and t.get('claims_held')) for t in s.tasks()):
            raise HarnessError('Stop/finish active workers and release write claims before final checks')
        if any(x.get('verification') for x in s.runs()):
            raise HarnessError('Another verification owns the worktree')
        checks = [c for c in run['plan']['checks'] if not check_id or c['id'] == check_id]
        if not checks:
            raise HarnessError('No matching checks')
        run['verification'] = {'ticket': ticket, 'started': now(), 'pid': os.getpid()}
        s.save_run(run)
    results = []
    try:
        private_dir(s.project, '.astra-luna-sol/evidence/' + rid)
        for check in checks:
            record = {'id': check['id'], 'argv': check['argv'], 'cwd': str(s.project), 'stamp': now(),
                      'snapshot': before['digest']}
            try:
                outcome = execute(check['argv'], s.project, check['timeout'])
            except OSError as ex:
                outcome = {'exit_code': None, 'timed_out': False, 'truncated': False, 'log': type(ex).__name__}
            after = capture(s.project)
            record.update({k: v for k, v in outcome.items() if k != 'log'})
            unchanged = after['digest'] == before['digest']
            record['status'] = 'passed' if outcome['exit_code'] == 0 and not outcome['timed_out'] and unchanged else 'failed'
            record['code_changed_during_check'] = not unchanged
            filename = check['id'] + '-' + ticket + '.log'
            path = checked_path(s.project, '.astra-luna-sol/evidence/' + rid + '/' + filename)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(outcome['log'])
            record['log_path'] = str(path.relative_to(s.project))
            record['log_sha256'] = digest(outcome['log'].encode())
            with s.tx():
                run = s.run(rid)
                if (run.get('verification') or {}).get('ticket') != ticket or run['status'] != 'running':
                    raise HarnessError('Run paused/cancelled/reconciled while verification was running; evidence not accepted')
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
