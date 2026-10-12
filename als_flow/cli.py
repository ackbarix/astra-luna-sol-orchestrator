"""als-flow: run and inspect Flow scripts (Claude Code's `Workflow` tool + run list).

    als-flow run SCRIPT.py [--resume RUN_ID] [-- SCRIPT ARGS]
    als-flow status [RUN_ID]      phases, calls (ok/failed/cached), usage, result path
    als-flow ls                   runs, newest last

`--resume` is Claude Code's `resumeFromRunId`: it sets ALS_FLOW_RESUME so the
script's `Flow` reuses cached results of unchanged calls.
Exit codes: 0 ok, 1 script/model failure, 2 usage error.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from . import common
from .flow import flow_root

PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)


def load_runs(root):
    runs = []
    if root.is_dir():
        for meta_file in root.glob('*/meta.json'):
            meta = common.read_json(meta_file)
            if isinstance(meta, dict) and meta.get('run_id'):
                runs.append(meta)
    runs.sort(key=lambda m: (m.get('started') or 0, m['run_id']))
    return runs


def cmd_run(args):
    script = Path(args.script)
    if not script.is_file():
        raise common.UsageError('script not found: %s' % args.script)
    env = dict(os.environ)
    env['PYTHONPATH'] = PACKAGE_PARENT + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    root = flow_root()
    if args.resume:
        if not (root / args.resume / 'meta.json').is_file():
            raise common.UsageError('unknown run %s in %s' % (args.resume, root))
        env['ALS_FLOW_RESUME'] = args.resume
    else:
        env.pop('ALS_FLOW_RESUME', None)
    started = time.time()
    script_args = args.script_args
    code = subprocess.call([sys.executable, str(script)] + script_args, env=env)
    # A script that died before flow.done() leaves its run 'running'; mark it.
    for meta in load_runs(root):
        if (meta.get('started') or 0) >= started - 1 and meta.get('status') == 'running':
            meta.update(status='exited' if code == 0 else 'failed', exit_code=code, ended=time.time())
            common.write_json(root / meta['run_id'] / 'meta.json', meta)
    return 0 if code == 0 else (2 if code == 2 else 1)


def cmd_status(args):
    root = flow_root()
    runs = load_runs(root)
    if args.run_id:
        meta = common.read_json(root / args.run_id / 'meta.json')
        if not isinstance(meta, dict):
            raise common.UsageError('unknown run %s' % args.run_id)
    elif runs:
        meta = runs[-1]
    else:
        raise common.CommandError('no flow runs in %s' % root)
    calls = meta.get('calls') or {}
    usage = meta.get('usage') or {}
    print('run %s  (%s)  status %s' % (meta['run_id'], meta.get('name'), meta.get('status')))
    if meta.get('resume_from'):
        print('resumed from %s' % meta['resume_from'])
    for phase in meta.get('phases') or []:
        took = ''
        if phase.get('started') and phase.get('ended'):
            took = '  %.1fs' % (phase['ended'] - phase['started'])
        print('  phase %-20s %s%s' % (phase['title'], phase.get('status'), took))
    print('calls: %d total, %d ok, %d failed, %d cached, %d refused' % tuple(
        calls.get(k, 0) for k in ('total', 'ok', 'failed', 'cached', 'refused')))
    line = 'usage: input %d (cached %d), output %d, billable %d' % (
        usage.get('input_tokens', 0), usage.get('cached_input_tokens', 0), usage.get('output_tokens', 0),
        common.billable(usage))
    if meta.get('budget_tokens') is not None:
        line += ', budget %d' % meta['budget_tokens']
    print(line)
    print('result: %s' % (meta.get('result_path') or '(none)'))
    print('dir: %s' % (root / meta['run_id']))
    return 0


def cmd_ls(_args):
    root = flow_root()
    runs = load_runs(root)
    if not runs:
        print('no flow runs in %s' % root)
        return 0
    for meta in runs:
        calls = meta.get('calls') or {}
        print('%-48s %-8s calls %d (failed %d, cached %d)  billable %d' % (
            meta['run_id'], meta.get('status'), calls.get('total', 0), calls.get('failed', 0),
            calls.get('cached', 0), common.billable(meta.get('usage'))))
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, '%s: error: %s\n' % (self.prog, message))


def parser():
    p = Parser(prog='als-flow', description='Run and inspect als_flow workflow scripts.')
    sub = p.add_subparsers(dest='cmd', parser_class=Parser)
    sub.required = True
    run = sub.add_parser('run', help='run a Flow script')
    run.add_argument('script')
    run.add_argument('--resume', metavar='RUN_ID')
    run.add_argument('args', nargs=argparse.REMAINDER)
    status = sub.add_parser('status', help='show a run (latest by default)')
    status.add_argument('run_id', nargs='?')
    sub.add_parser('ls', help='list runs')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.cmd == 'run':
        # `--resume` may follow the script; everything after `--` (or unknown) goes to the script.
        rest, args.script_args = list(args.args), []
        while rest:
            arg = rest.pop(0)
            if arg == '--':
                args.script_args += rest
                break
            if arg == '--resume' and rest:
                args.resume = rest.pop(0)
            elif arg.startswith('--resume='):
                args.resume = arg.split('=', 1)[1]
            elif arg == '--resume':
                p.error('--resume needs a RUN_ID')
            else:
                args.script_args.append(arg)
    handlers = {'run': cmd_run, 'status': cmd_status, 'ls': cmd_ls}
    try:
        return handlers[args.cmd](args)
    except common.UsageError as error:
        print('als-flow: %s' % error, file=sys.stderr)
        return 2
    except common.CommandError as error:
        print('als-flow: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
