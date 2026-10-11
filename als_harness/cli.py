"""User/HEAD CLI; no model dispatch, implicit retries or background processes."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from .common import HarnessError, checked_path, redact
from .engine import Engine, validate_plan
from .state import Store
from . import hooks, integration, report, usage, verify, efficiency, telemetry


def doctor(project, store, probe=False):
    issues = []
    installed = False
    try:
        raw = checked_path(project, '.codex/hooks.json').read_bytes()
        doc = integration.parse(raw)
        installed = all(g in doc.get('hooks', {}).get(e, []) for e, g in integration.groups(project).items())
        if not installed:
            issues.append('ALS hooks missing or customized; review /hooks')
    except (OSError, HarnessError):
        issues.append('Project hooks not installed/readable')
    observed = {}
    rows = store.db.execute('SELECT id,doc FROM sessions ORDER BY rowid DESC LIMIT 8') if store else []
    for row in rows:
        observed[row['id']] = json.loads(row['doc']).get('seen', [])
    result = {'hooks_configured': installed, 'observed_events_by_session': observed, 'issues': issues,
              'paid_model_calls': False, 'hook_trust_verified': False, 'blocking_behavior_verified': False,
              'note': 'Review/trust with /hooks. Observed callbacks and fixtures are not an end-to-end proof'
                      ' of installed Codex enforcement.'}
    if probe:
        result['probes'] = {}
        for key, cmd in {'version': ['codex', '--version'], 'features': ['codex', 'features', 'list']}.items():
            if not shutil.which(cmd[0]):
                result['issues'].append('codex not found')
                continue
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                result['probes'][key] = {'exit_code': p.returncode, 'output': redact(p.stdout[:2000])}
            except (OSError, subprocess.TimeoutExpired):
                result['issues'].append(key + ' probe failed')
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parents[1]
    default = here.parent if here.name == '.astra-luna-sol' else Path.cwd()
    p.add_argument('--project', type=Path, default=default)
    sub = p.add_subparsers(dest='cmd', required=True)
    start = sub.add_parser('start')
    start.add_argument('--plan', type=Path, required=True)
    start.add_argument('--session', default=os.environ.get('CODEX_THREAD_ID'))
    start.add_argument('--mode', choices=['observe', 'guarded'], default='observe')
    start.add_argument('--ack-hook-limits', action='store_true')
    d = sub.add_parser('doctor')
    d.add_argument('--probe', action='store_true')
    sub.add_parser('hook')
    status = sub.add_parser('status')
    status.add_argument('--run')
    status.add_argument('--compact', action='store_true')
    prep = sub.add_parser('prepare')
    prep.add_argument('--plan', type=Path, required=True)
    prep.add_argument('--profile', choices=sorted(efficiency.PROFILES), default='balanced')
    prep.add_argument('--use-history', action='store_true')
    ana = sub.add_parser('analyze')
    choice = ana.add_mutually_exclusive_group()
    choice.add_argument('--run')
    choice.add_argument('--report-file', type=Path)
    ana.add_argument('--workload', default='unspecified')
    for name in ('task-message', 'begin-head'):
        q = sub.add_parser(name)
        q.add_argument('--run', required=True)
        q.add_argument('--task', required=True)
    measure = sub.add_parser('measure')
    measure.add_argument('--run', required=True)
    measure.add_argument('--workload', required=True)
    measure.add_argument('--quality', choices=['pass', 'fail'], required=True)
    measure.add_argument('--ack-recorded-only', action='store_true')
    measure.add_argument('--elapsed-seconds', type=float)
    for name in ('complete', 'sync-usage', 'report', 'verify', 'pause', 'cancel', 'resume', 'reserve', 'bind',
                 'finish', 'accept', 'retry', 'assess'):
        q = sub.add_parser(name)
        q.add_argument('--run', required=True)
        if name in ('reserve', 'finish', 'accept', 'retry'):
            q.add_argument('--task', required=True)
        if name in ('reserve', 'bind'):
            q.add_argument('--call-id', required=True)
        if name == 'bind':
            q.add_argument('--thread', required=True)
        if name == 'finish':
            q.add_argument('--note', required=True)
            q.add_argument('--failed', action='store_true')
            q.add_argument('--workers-stopped', action='store_true')
            q.add_argument('--error-code', default='work_failure')
        if name == 'retry':
            q.add_argument('--approach', required=True)
            q.add_argument('--role')
        if name == 'verify':
            q.add_argument('--check')
            q.add_argument('--execute', action='store_true')
        if name in ('pause', 'cancel'):
            q.add_argument('--reason', required=True)
        if name == 'resume':
            q.add_argument('--accept-drift', action='store_true')
            q.add_argument('--workers-stopped', action='store_true')
            q.add_argument('--session')
        if name == 'assess':
            q.add_argument('--criterion', required=True)
            q.add_argument('--note', required=True)
            q.add_argument('--actor', required=True)
        if name == 'report':
            q.add_argument('--format', choices=['json', 'md'], default='json')
    return p


def dispatch(args, store):
    """Run one state-backed command; returns (output, exit code) or (None, 0) when already printed."""
    e = Engine(store)
    code = 0
    out = None
    if args.cmd == 'start':
        data = args.plan.read_bytes()
        if len(data) > 262144:
            raise HarnessError('Plan file too large')
        out = {'run': e.start(json.loads(data), args.session, args.mode, args.ack_hook_limits), 'models_called': False}
    elif args.cmd == 'doctor':
        out = doctor(store.project, store, args.probe)
    elif args.cmd == 'status':
        if args.run:
            out = report.report(e, args.run)
        else:
            out = {'runs': [{k: r.get(k) for k in ('id', 'status', 'session', 'mode', 'updated')} for r in store.runs()]}
    elif args.cmd == 'task-message':
        r = store.run(args.run)
        t = store.task(args.run, args.task)
        print(efficiency.build_message(r['plan'], t))
        return None, 0
    elif args.cmd == 'begin-head':
        out = e.begin_head(args.run, args.task)
        code = 0 if out['allowed'] else 3
    elif args.cmd == 'measure':
        out = telemetry.review_measurement(e, args.run, args.workload, args.quality, args.ack_recorded_only,
                                           args.elapsed_seconds)
    elif args.cmd == 'reserve':
        out = e.reserve(args.run, args.task, args.call_id)
        code = 0 if out['allowed'] else 3
    elif args.cmd == 'bind':
        e.bind(args.run, args.call_id, args.thread)
        out = {'bound': True}
    elif args.cmd == 'finish':
        e.finish(args.run, args.task, args.note, args.failed, args.workers_stopped, args.error_code)
        out = {'recorded': True, 'completion_verified': False}
    elif args.cmd == 'accept':
        e.accept(args.run, args.task)
        out = {'accepted': True, 'basis': 'declared_checks_and_HEAD_assessment'}
    elif args.cmd == 'retry':
        e.retry(args.run, args.task, args.approach, args.role)
        out = {'pending': True, 'worker_restarted': False}
    elif args.cmd == 'verify':
        out = verify.verify(e, args.run, args.check, args.execute)
        code = 3 if out.get('executed') and not out.get('passed') else 0
    elif args.cmd == 'assess':
        e.assess(args.run, args.criterion, args.note, args.actor)
        out = {'recorded': True, 'independent_human_identity_verified': False}
    elif args.cmd == 'complete':
        out = e.complete(args.run)
        code = 0 if out['completed'] else 3
    elif args.cmd in ('pause', 'cancel'):
        e.pause(args.run, args.reason, args.cmd == 'cancel')
        out = {'status': store.run(args.run)['status'], 'processes_killed': False}
    elif args.cmd == 'resume':
        out = e.resume(args.run, args.accept_drift, args.workers_stopped, args.session)
    elif args.cmd == 'sync-usage':
        out = {'results': usage.sync(e, args.run), 'budget': e.budget(store.run(args.run))}
    elif args.cmd == 'report':
        out = report.report(e, args.run)
        if args.format == 'md':
            print(report.markdown(out))
            return None, 0
    if args.cmd == 'status' and args.compact and args.run:
        out = report.compact(out)
    return out, code


def offline(args, project):
    """prepare/analyze: no DB is created just to analyze a report or prepare a plan."""
    store = None
    exists = (project / '.astra-luna-sol/state/runs.sqlite').is_file()
    try:
        if args.cmd == 'prepare':
            raw = args.plan.read_bytes()
            if len(raw) > 262144:
                raise HarnessError('Plan file too large')
            plan = json.loads(raw)
            if args.use_history and exists:
                store = Store(project)
            history = telemetry.history(store, plan.get('workload', 'unspecified')) if args.use_history else None
            return validate_plan(efficiency.prepare(plan, args.profile, history))
        if args.report_file:
            raw = args.report_file.read_bytes()
            if len(raw) > 4 * 1024 * 1024:
                raise HarnessError('Report file too large')
            return telemetry.metrics(json.loads(raw))
        if exists:
            store = Store(project)
        if args.run:
            if store is None:
                raise HarnessError('No managed runs registered')
            return telemetry.metrics(report.report(Engine(store), args.run))
        return telemetry.history(store, args.workload)
    finally:
        if store:
            store.close()


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    payload = None
    try:
        # Hook input is untrusted. Never shell-execute fields or echo raw payloads.
        if args.cmd == 'hook':
            data = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise HarnessError('Oversized hook input')
            payload = json.loads(data)
        project = args.project.resolve(strict=True)
        if args.cmd in ('prepare', 'analyze'):
            print(json.dumps(offline(args, project), ensure_ascii=False, indent=2))
            return 0
        if args.cmd in ('status', 'doctor') and not (project / '.astra-luna-sol/state/runs.sqlite').exists():
            if args.cmd == 'doctor':
                out = doctor(project, None, args.probe)
            else:
                out = {'runs': [], 'note': 'No managed runs registered'}
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        store = Store(project)
        if args.cmd == 'hook':
            out = hooks.handle(store, payload)
            code = 0
        else:
            out, code = dispatch(args, store)
            if out is None:
                return code
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return code
    except (HarnessError, OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.TimeoutExpired) as ex:
        if args.cmd == 'hook':
            # Fail visibly; never certify completion or queue recovery on corrupt
            # state. Runtime hook errors may fail open in Codex: not a sandbox.
            print(json.dumps(hooks.warn('ALS unavailable: ' + type(ex).__name__
                                        + '. No completion certification or automatic recovery.'
                                        + ' Inspect harness.py doctor.')))
            return 0
        print('ALS ERROR: ' + redact(str(ex)), file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()
