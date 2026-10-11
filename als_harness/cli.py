"""User/HEAD CLI; no model dispatch, implicit retries or background processes."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from .common import HarnessError, checked_path, now, redact
from .engine import Engine, validate_plan
from .state import Store
from . import state, hooks, report, usage, verify, efficiency, telemetry

try:
    from . import deps  # COMPAT(0.5 installer branch): provided by als_harness/deps.py
except ImportError:
    deps = None

# Directory holding this harness (als_harness/ and harness.py when installed).
HOME = Path(__file__).resolve().parents[1]
DOCTOR_EVENTS = ('SessionStart', 'PreToolUse', 'PostToolUse', 'SubagentStart', 'SubagentStop', 'Stop', 'SessionEnd')
RECENT_SESSION_SECONDS = 600


def detect_scope(home=HOME):
    """'project' when running from <project>/.astra-luna-sol/harness.py, else 'global'."""
    if home.name == '.astra-luna-sol' and (home / 'harness.py').is_file():
        return 'project'
    return 'global'


def git_project(cwd):
    """Main project for a directory: git common dir's parent, so forked worktrees map home.

    Portable to old git: no --path-format (git >= 2.31); relative output is resolved here.
    """
    cwd = Path(cwd).resolve()

    def rev_parse(flag):
        try:
            done = subprocess.run(['git', '-C', str(cwd), 'rev-parse', flag],
                                  capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = done.stdout.strip()
        return value if done.returncode == 0 and value else None
    top = rev_parse('--show-toplevel')
    common = rev_parse('--git-common-dir')
    if common is not None:
        # Relative output is relative to cwd; very old git printed it relative to the top level.
        candidates = [cwd / common] + ([Path(top) / common] if top else [])
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.name == '.git' and candidate.is_dir():
                return candidate.parent
    return Path(top).resolve() if top and Path(top).is_dir() else None


NOT_A_PROJECT = ('Not inside a git project. Next: cd into the project (any subdirectory or worktree)'
                 ' or pass --project <path>.')


def resolve_project(args, scope, payload=None):
    """--project wins (legacy hooks); project scope owns its parent; global uses cwd/git."""
    if args.project is not None:
        return args.project.resolve(strict=True)
    if scope == 'project':
        return HOME.parent.resolve(strict=True)
    if args.cmd == 'hook':
        cwd = payload.get('cwd') if isinstance(payload, dict) else None
        if not isinstance(cwd, str) or not cwd or not Path(cwd).is_dir():
            return None
        return git_project(Path(cwd))
    found = git_project(Path.cwd())
    if found is None:
        raise HarnessError(NOT_A_PROJECT)
    return found


def state_path(project, scope):
    return state.locate(project, scope=scope)


def open_store(project, scope, hook=False):
    return Store(project, scope=scope, busy_ms=state.HOOK_BUSY_MS if hook else state.CLI_BUSY_MS)


def default_session(store, mode):
    """--session default -> (session, source, warning).

    $CODEX_THREAD_ID wins when set. Otherwise, in observe mode only, pick
    the session only when exactly one session receipt saw ANY hook event in
    the last 10 minutes (ended sessions included in that count) and its last
    event was not SessionEnd, and always warn naming it. Guarded mode never
    auto-picks: it requires --session (or $CODEX_THREAD_ID).
    """
    env = os.environ.get('CODEX_THREAD_ID')
    if env:
        return env, 'CODEX_THREAD_ID', None
    if mode == 'guarded':
        raise HarnessError('Guarded mode requires --session <session id>; it never picks a session automatically.')
    cutoff = now() - RECENT_SESSION_SECONDS
    recent, ended = [], []
    for row in store.db.execute('SELECT id,doc FROM sessions'):
        doc = json.loads(row['doc'])
        last = doc.get('last_event', doc.get('session_start_at'))
        if isinstance(last, (int, float)) and last >= cutoff:
            recent.append(row['id'])
            if isinstance(doc.get('ended_at'), (int, float)):
                ended.append(row['id'])
    if len(recent) == 1 and ended:
        return None, None, ('No --session given and the only session with hook activity in the last 10 minutes ('
                            + recent[0] + ') has ended; cannot pick the session safely. Observe run started'
                            ' without a session; usage/hook linkage disabled.')
    if len(recent) == 1:
        return recent[0], 'recent_session_start', (
            'No --session given; picked session ' + recent[0] + ', the only session with hook activity in the'
            ' last 10 minutes. If that is not this chat, cancel this run and pass --session <session id>.')
    problem = ('No --session given and ' + str(len(recent))
               + ' sessions with hook activity in the last 10 minutes; cannot pick the session safely.')
    return None, None, problem + ' Observe run started without a session; usage/hook linkage disabled.'


def hooks_files(project):
    """Hook files doctor inspects, in order: the project's .codex/hooks.json
    (project installs and `--hooks-only` installs) then $CODEX_HOME/hooks.json
    (global installs). Both scopes check both; Codex loads both layers."""
    files = []
    try:
        files.append(checked_path(project, '.codex/hooks.json'))
    except HarnessError:
        pass  # symlinked .codex: never followed
    files.append(Path(os.environ.get('CODEX_HOME') or '~/.codex').expanduser() / 'hooks.json')
    return files


# A handler running a harness.py entrypoint in hook mode: the global form
# `python3 "${HOME:-/abs}/.local/share/als/harness.py" hook`, the project form
# `c=$(git rev-parse --git-common-dir ...) ... exec python3 "$s" hook`, and 0.4
# absolute-path forms. Same pattern as integration.ALS_COMMAND.
ALS_HOOK_COMMAND = re.compile(r'''harness\.py\b.*\shook\s*$''', re.S)


def als_command(handler):
    command = handler.get('command') if isinstance(handler, dict) else None
    return isinstance(command, str) and ALS_HOOK_COMMAND.search(command) is not None


def configured_events(doc):
    configured = doc.get('hooks') if isinstance(doc, dict) else None
    if not isinstance(configured, dict):
        return set()
    found = set()
    for event in DOCTOR_EVENTS:
        groups = configured.get(event)
        groups = groups if isinstance(groups, list) else []
        if any(isinstance(g, dict) and isinstance(g.get('hooks'), list) and any(als_command(h) for h in g['hooks'])
               for g in groups):
            found.add(event)
    return found


def missing_events(doc):
    found = configured_events(doc)
    return [event for event in DOCTOR_EVENTS if event not in found]


def dependency_report(project, probe=True):
    if deps is None:
        return {'ok': None, 'items': [], 'note': 'dependency checker unavailable in this install'}
    try:
        return deps.check(project, probe=probe)
    except Exception as ex:  # a broken checker must not hide the rest of doctor
        return {'ok': None, 'items': [], 'note': 'dependency check failed: ' + type(ex).__name__}


def doctor_failed(result):
    """True when a REQUIRED dependency failed (doctor exits 1). Hook and
    optional-item problems are warnings: the CLI works without hooks."""
    return any(not item.get('ok') and item.get('required', True) is not False
               for item in result.get('deps', {}).get('items', []))


def doctor_text(result):
    where = result.get('hooks_found_in') or {}
    lines = ['ALS doctor (' + result['scope'] + ' scope)',
             'state:  ' + result['state_path'],
             'hooks:  ' + ('configured' if result['hooks_configured'] else 'NOT configured')
             + (' (found in ' + ', '.join(where) + ')' if where
                else ' (checked ' + ', '.join(result.get('hooks_checked') or []) + ')')]
    problems = ['warn ' + issue for issue in result['issues']]
    for item in result.get('deps', {}).get('items', []):
        if item.get('ok'):
            continue
        required = item.get('required', True)
        mark = 'warn' if required is False else 'FAIL'
        line = mark + ' ' + str(item.get('name')) + ': found ' + str(item.get('found') or 'nothing')
        if not isinstance(required, bool) and required is not None:
            line += ', need ' + str(required)
        if item.get('hint'):
            line += ' -> ' + str(item['hint'])
        problems.append(line)
    if 'deps' in result and result['deps'].get('ok') is None:
        lines.append('deps:   not checked (' + str(result.get('deps', {}).get('note')) + ')')
    failed = sum(p.startswith('FAIL') for p in problems)
    warned = len(problems) - failed
    if not problems:
        summary = 'all checks passed'
    else:
        summary = str(failed) + ' required problem(s), ' + str(warned) + ' warning(s)'
    lines.append('summary: ' + summary)
    return '\n'.join(lines + problems)


def doctor(project, store, probe=False, scope='project', deps_probe=True, with_deps=True):
    """Hook/state report plus the embedded dependency check.

    with_deps=False (`--no-deps`) omits the 'deps' section entirely, for
    callers such as bin/als that already ran deps.check() themselves; the
    exit code then reflects only usage/state errors."""
    issues = []
    checked, found_in, configured = [], {}, set()
    for path in hooks_files(project):
        checked.append(str(path))
        try:
            doc = json.loads(path.read_bytes())
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as ex:
            issues.append('Hooks file unreadable: ' + str(path) + ' (' + type(ex).__name__ + ')')
            continue
        events = configured_events(doc)
        if events:
            found_in[str(path)] = [e for e in DOCTOR_EVENTS if e in events]
            configured |= events
    missing = [e for e in DOCTOR_EVENTS if e not in configured]
    installed = not missing
    if not found_in:
        issues.append('ALS hooks not installed (checked ' + ', '.join(checked)
                      + ') -> install ALS hooks, or use the CLI without hooks')
    elif missing:
        issues.append('ALS hook handler missing for ' + ', '.join(missing)
                      + ' -> reinstall ALS hooks, then review /hooks in Codex')
    observed = {}
    rows = store.db.execute('SELECT id,doc FROM sessions ORDER BY rowid DESC LIMIT 8') if store else []
    for row in rows:
        observed[row['id']] = json.loads(row['doc']).get('seen', [])
    result = {'hooks_configured': installed, 'hook_events_missing': missing, 'scope': scope,
              'hooks_file': next(iter(found_in), None), 'hooks_found_in': found_in, 'hooks_checked': checked,
              'state_path': str(state_path(project, scope)),
              'observed_events_by_session': observed, 'issues': issues,
              'paid_model_calls': False, 'hook_trust_verified': False, 'blocking_behavior_verified': False,
              'note': 'Review/trust with /hooks. Observed callbacks and fixtures are not an end-to-end proof'
                      ' of installed Codex enforcement.'}
    if with_deps:
        result['deps'] = dependency_report(project, deps_probe)
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
    result['exit_code'] = 1 if doctor_failed(result) else 0
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', type=Path, default=None,
                   help='Project root. Default: the parent of an installed .astra-luna-sol/ (project scope);'
                        ' otherwise the main worktree of the current directory (hooks: of the payload cwd).')
    sub = p.add_subparsers(dest='cmd', required=True)
    start = sub.add_parser('start')
    start.add_argument('--plan', type=Path, required=True, help="Plan JSON file, or '-' for stdin.")
    start.add_argument('--session', default=None,
                       help='Codex session id. Default: $CODEX_THREAD_ID; else (observe only) the single session'
                            ' with any hook event in the last 10 minutes, with a warning naming it. With zero or'
                            ' several, observe starts without a session (warning). Guarded mode never picks:'
                            ' pass --session.')
    start.add_argument('--mode', choices=['observe', 'guarded'], default='observe')
    start.add_argument('--ack-hook-limits', action='store_true')
    d = sub.add_parser('doctor', description='Check hooks, state and dependencies. Exit 0 when every REQUIRED'
                                             ' dependency is present (warnings allowed), 1 when a required one'
                                             ' fails, 2 on a usage or state error.')
    d.add_argument('--probe', action='store_true', help='Also run `codex --version` / `codex features list`.')
    d.add_argument('--no-probe', action='store_true',
                   help='Dependency check without `codex features list` (the codex-feature items are omitted);'
                        ' `codex --version`, `git --version` and `ocx --version` still run once.')
    d.add_argument('--no-deps', action='store_true',
                   help='Skip the embedded dependency check entirely (no "deps" section, no external programs'
                        ' unless --probe); for callers that already ran it.')
    d.add_argument('--format', choices=['json', 'text'], default='json')
    sub.add_parser('hook')
    status = sub.add_parser('status', help='Unfinished runs (all with --all), or one run with --run.')
    status.add_argument('--run')
    status.add_argument('--all', action='store_true')
    status.add_argument('--compact', action='store_true')
    status.add_argument('--format', choices=['json', 'text'], default='json')
    prep = sub.add_parser('prepare')
    prep.add_argument('--plan', type=Path, required=True, help="Plan JSON file, or '-' for stdin.")
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


PLAN_LIMIT = 262144
UNFINISHED = {'completed', 'cancelled'}


def read_plan(path):
    if str(path) == '-':
        raw = sys.stdin.buffer.read(PLAN_LIMIT + 1)
    else:
        raw = path.read_bytes()
    if len(raw) > PLAN_LIMIT:
        raise HarnessError('Plan file too large')
    try:
        return json.loads(raw)
    except ValueError as ex:
        raise HarnessError('Plan is not valid JSON (' + str(ex)[:120] + ').'
                           ' Next: fix the file, then harness.py prepare --plan <file> to validate it.') from ex


def status_rows(store, everything=False):
    keys = ('id', 'status', 'session', 'mode', 'updated')
    return [{k: r.get(k) for k in keys} for r in store.runs() if everything or r['status'] not in UNFINISHED]


def status_text(out):
    if 'runs' not in out:
        return None
    if not out['runs']:
        return (out.get('note') or 'No unfinished runs') + '. Next: harness.py start --plan <plan.json>'
    lines = ['%-16s  %-9s  %-8s  %-20s  %s' % ('RUN', 'STATUS', 'MODE', 'SESSION', 'UPDATED')]
    for r in out['runs']:
        updated = r.get('updated')
        stamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(updated)) if isinstance(updated, (int, float)) else '-'
        lines.append('%-16s  %-9s  %-8s  %-20s  %s' % (r['id'], r['status'], r.get('mode') or '-',
                                                       (r.get('session') or '-')[:20], stamp))
    return '\n'.join(lines)


def next_step(args, message):
    """One concrete follow-up command for common mistakes; None when there is no obvious one."""
    run = getattr(args, 'run', None) or '<run>'
    hints = (
        ('Unknown run', 'harness.py status --all  (lists run ids)'),
        ('Unknown task', 'harness.py status --run ' + run + '  (lists task ids)'),
        ('No managed runs', 'harness.py start --plan <plan.json>'),
        ('Worktree/HEAD differs', 'git status; after reviewing: harness.py resume --run ' + run + ' --accept-drift'),
        ('Confirm old workers', 'after confirming no worker is still running: harness.py resume --run ' + run
         + ' --workers-stopped'),
        ('Confirm worker stopped', 'after confirming the worker stopped: harness.py finish --run ' + run
         + ' --task <task> --note <result> --workers-stopped'),
        ('Session already has an unfinished run', 'harness.py status  (then resume or cancel that run)'),
        ('Guarded mode needs', 'harness.py start --plan <plan.json> --mode observe  (guarded needs real hook'
         ' receipts and --ack-hook-limits)'),
        ('Plan file too large', 'shrink the plan below 256 KiB'),
    )
    for needle, hint in hints:
        if needle in message:
            return hint
    return None


def dispatch(args, store):
    """Run one state-backed command; returns (output, exit code) or (None, 0) when already printed."""
    e = Engine(store)
    code = 0
    out = None
    if args.cmd == 'start':
        plan = read_plan(args.plan)
        session, source, problem = args.session, 'explicit', None
        if session is None:
            session, source, problem = default_session(store, args.mode)
        # The run works in the worktree it was started from (main checkout or a
        # linked `git worktree`); state stays keyed by the main project.
        worktree = store.worktree_of(Path.cwd())
        rid = e.start(plan, session, args.mode, args.ack_hook_limits, worktree=worktree)
        out = {'run': rid, 'models_called': False, 'session': session, 'session_source': source,
               'worktree': store.run(rid)['worktree']}
        if problem:
            out['warnings'] = [problem]
            print('ALS WARNING: ' + problem, file=sys.stderr)
    elif args.cmd == 'doctor':
        out = doctor(store.project, store, args.probe, args.scope, not args.no_probe, not args.no_deps)
        code = out['exit_code']
    elif args.cmd == 'status':
        if args.run:
            out = report.report(e, args.run)
        else:
            out = {'runs': status_rows(store, args.all)}
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
        out = e.bind(args.run, args.call_id, args.thread) or {'bound': True, 'reason': None}
        code = 0 if out.get('bound') else 3
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
        paused = e.pause(args.run, args.reason, args.cmd == 'cancel')
        # Only the run's own recorded check group can be killed; Codex
        # workers are never signalled by the harness.
        out = {'status': store.run(args.run)['status'],
               'check_group_killed': paused['check_group_killed'],
               'workers_signalled': False}
    elif args.cmd == 'resume':
        out = e.resume(args.run, args.accept_drift, args.workers_stopped, args.session)
    elif args.cmd == 'sync-usage':
        results = usage.sync(e, args.run)
        run = store.run(args.run)
        out = {'results': results, 'budget': dict(e.budget(run), usage_synced_at=run.get('last_usage_sync'))}
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
    exists = state_path(project, args.scope).is_file()
    try:
        if args.cmd == 'prepare':
            plan = read_plan(args.plan)
            if args.use_history and exists:
                store = open_store(project, args.scope)
            history = telemetry.history(store, plan.get('workload', 'unspecified')) if args.use_history else None
            return validate_plan(efficiency.prepare(plan, args.profile, history))
        if args.report_file:
            raw = args.report_file.read_bytes()
            if len(raw) > 4 * 1024 * 1024:
                raise HarnessError('Report file too large')
            return telemetry.metrics(json.loads(raw))
        if exists:
            store = open_store(project, args.scope)
        if args.run:
            if store is None:
                raise HarnessError('No managed runs registered')
            return telemetry.metrics(report.report(Engine(store), args.run))
        return telemetry.history(store, args.workload)
    finally:
        if store:
            store.close()


def emit(args, out):
    if getattr(args, 'format', 'json') == 'text':
        if args.cmd == 'doctor':
            text = doctor_text(out)
        elif args.cmd == 'status':
            text = status_text(out) if 'runs' in out else report.run_text(out)
        else:
            text = None
        if text is not None:
            print(text)
            return
    print(json.dumps(out, ensure_ascii=False, indent=2))


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
        args.scope = detect_scope()
        project = resolve_project(args, args.scope, payload)
        if project is None:
            # Global hook outside any git project: nothing to record or enforce.
            print(json.dumps({}))
            return 0
        if args.cmd in ('prepare', 'analyze'):
            print(json.dumps(offline(args, project), ensure_ascii=False, indent=2))
            return 0
        if args.cmd in ('status', 'doctor') and not state_path(project, args.scope).exists():
            code = 0
            if args.cmd == 'doctor':
                out = doctor(project, None, args.probe, args.scope, not args.no_probe, not args.no_deps)
                code = out['exit_code']
            else:
                out = {'runs': [], 'note': 'No managed runs registered', 'next': 'harness.py start --plan <plan.json>'}
            emit(args, out)
            return code
        store = open_store(project, args.scope, hook=args.cmd == 'hook')
        if args.cmd == 'hook':
            out = hooks.handle(store, payload)
            code = 0
        else:
            out, code = dispatch(args, store)
            if out is None:
                return code
        emit(args, out)
        return code
    except (HarnessError, OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.TimeoutExpired) as ex:
        if args.cmd == 'hook':
            # Fail visibly; never certify completion or queue recovery on corrupt
            # state. Runtime hook errors may fail open in Codex: not a sandbox.
            print(json.dumps(hooks.warn('ALS unavailable: ' + type(ex).__name__
                                        + '. No completion certification or automatic recovery.'
                                        + ' Inspect harness.py doctor.')))
            return 0
        message = redact(str(ex)) if str(ex) else type(ex).__name__
        if isinstance(ex, FileNotFoundError):
            message = 'File not found: ' + redact(str(ex.filename))
            hint = "check the path; pass --plan - to read the plan from stdin"
        else:
            hint = next_step(args, message)
        print('ALS ERROR: ' + message, file=sys.stderr)
        if hint and 'Next:' not in message:
            print('Next: ' + hint, file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()
