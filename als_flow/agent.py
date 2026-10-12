"""als-agent: persistent named agents (Claude Code `Agent(name:)` + `SendMessage`).

Claude Code keeps a named teammate alive and routes `SendMessage` to it. Codex
has no live agents, but a non-ephemeral `codex exec` session can be continued
with `codex exec resume SESSION_ID`, which restores the full context. So:

    als-agent start NAME --type AGENT_TYPE [--cwd DIR] [--replace] "first instruction"
    als-agent send NAME [--from OTHER] "message"     (`-` reads stdin)
    als-agent list
    als-agent show NAME [--json]

State: `_workspace/agents/NAME.json` ({name, agent_type, session_id, cwd,
started, usage, pins}) and `NAME.log.jsonl` (one record per turn). Agents do
not talk to each other directly; the lead relays with `send --from OTHER`.
Exit codes: 0 ok, 1 command or model failure, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from . import common

HEADER = ('You are the persistent team agent named "{name}" (role {agent_type}). The team lead sends you '
          'instructions in this session over several turns. Messages relayed from other agents start with '
          '"[from NAME]". Reply to the lead concisely: what you did, results of checks you actually ran, '
          'and open problems.')


def agents_dir():
    return common.workspace_dir() / 'agents'


def read_message(text):
    if text == '-':
        text = sys.stdin.read()
    if not text or not text.strip():
        raise common.UsageError('empty message')
    return text


def load_agent(name):
    if not common.valid_name(name):
        raise common.UsageError('invalid agent name %r' % name)
    data = common.read_json(agents_dir() / (name + '.json'))
    if not isinstance(data, dict):
        raise common.UsageError('unknown agent %s (start it with: als-agent start %s --type ...)' % (name, name))
    return data


def cmd_start(args):
    name = args.name
    if not common.valid_name(name):
        raise common.UsageError('invalid agent name %r (letters, digits, _ . -)' % name)
    folder = agents_dir()
    record_path = folder / (name + '.json')
    message = read_message(args.instruction)
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not cwd.is_dir():
        raise common.UsageError('cwd does not exist: %s' % cwd)
    role = common.find_role(args.type, cwd)
    pins = common.role_pins(role)
    codex = common.codex_bin()
    base_url = common.native_base_url(codex)
    with common.file_lock(folder / (name + '.lock')):
        if record_path.exists() and not args.replace:
            raise common.UsageError('agent %s already exists; use --replace to start a new session' % name)
        prompt = '\n\n'.join(p for p in (
            HEADER.format(name=name, agent_type=role.get('name') or args.type),
            (role.get('developer_instructions') or '').strip(),
            '# First instruction\n\n' + message.strip()) if p)
        with tempfile.TemporaryDirectory(prefix='als-agent-') as tmp:
            argv = common.exec_argv(codex, base_url, pins['model'], pins['effort'], pins['sandbox'], cwd,
                                    Path(tmp) / 'last.txt', ephemeral=False)
            res = common.run_codex(argv, prompt, cwd, args.timeout)
        turn = {'ts': time.time(), 'kind': 'start', 'from': 'lead', 'message': message,
                'reply': res['reply'], 'usage': res['usage'], 'error': res['error'],
                'session_id': res['thread_id']}
        if res['error'] or not res['thread_id']:
            common.append_jsonl(folder / (name + '.log.jsonl'), turn)
            raise common.CommandError('start failed: %s' % (res['error'] or 'codex reported no thread id'))
        if record_path.exists():
            old = folder / (name + '.log.jsonl')
            if old.exists():
                old.replace(folder / ('%s.log.%d.jsonl' % (name, int(time.time()))))
        record = {'name': name, 'agent_type': role.get('name') or args.type, 'session_id': res['thread_id'],
                  'cwd': str(cwd), 'started': time.time(), 'usage': res['usage'], 'turns': 1,
                  'pins': pins, 'base_url': base_url, 'role_file': role.get('_path'),
                  'last_reply': res['reply'], 'updated': time.time()}
        common.write_json(record_path, record)
        common.append_jsonl(folder / (name + '.log.jsonl'), turn)
    print(res['reply'].rstrip('\n'))
    return 0


def cmd_send(args):
    record = load_agent(args.name)
    message = read_message(args.message)
    if args.sender:
        if not common.valid_name(args.sender):
            raise common.UsageError('invalid --from name %r' % args.sender)
        prompt = '[from %s] %s' % (args.sender, message)
    else:
        prompt = message
    folder = agents_dir()
    codex = common.codex_bin()
    pins = record.get('pins') or {}
    base_url = common.native_base_url(codex)
    with common.file_lock(folder / (args.name + '.lock')):  # one turn at a time per session
        record = load_agent(args.name)
        with tempfile.TemporaryDirectory(prefix='als-agent-') as tmp:
            argv = common.resume_argv(codex, base_url, pins.get('model'), pins.get('effort'),
                                      pins.get('sandbox'), record['session_id'], Path(tmp) / 'last.txt')
            res = common.run_codex(argv, prompt, record['cwd'], args.timeout)
        turn = {'ts': time.time(), 'kind': 'send', 'from': args.sender or 'lead', 'message': message,
                'reply': res['reply'], 'usage': res['usage'], 'error': res['error'],
                'session_id': record['session_id']}
        common.append_jsonl(folder / (args.name + '.log.jsonl'), turn)
        if res['error']:
            raise common.CommandError('send failed: %s' % res['error'])
        record['usage'] = common.add_usage(dict(record.get('usage') or {}), res['usage'])
        record['turns'] = record.get('turns', 0) + 1
        record['last_reply'] = res['reply']
        record['updated'] = time.time()
        common.write_json(folder / (args.name + '.json'), record)
    print(res['reply'].rstrip('\n'))
    return 0


def cmd_list(_args):
    folder = agents_dir()
    rows = []
    for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
        data = common.read_json(path)
        if isinstance(data, dict) and data.get('session_id'):
            rows.append('%-20s %-22s turns %-3d billable %-8d %s' % (
                data['name'], data.get('agent_type'), data.get('turns', 0), common.billable(data.get('usage')),
                data['session_id']))
    print('\n'.join(rows) if rows else 'no agents in %s' % folder)
    return 0


def cmd_show(args):
    record = load_agent(args.name)
    if args.json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0
    usage = record.get('usage') or {}
    print('agent %s  type %s  session %s' % (record['name'], record.get('agent_type'), record['session_id']))
    print('cwd %s  turns %d' % (record.get('cwd'), record.get('turns', 0)))
    print('usage: input %d (cached %d), output %d, billable %d' % (
        usage.get('input_tokens', 0), usage.get('cached_input_tokens', 0), usage.get('output_tokens', 0),
        common.billable(usage)))
    print('--- last reply ---')
    print((record.get('last_reply') or '').rstrip('\n'))
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, '%s: error: %s\n' % (self.prog, message))


def parser():
    p = Parser(prog='als-agent', description='Persistent named Codex agents (start / send / list / show).')
    sub = p.add_subparsers(dest='cmd', parser_class=Parser)
    sub.required = True
    start = sub.add_parser('start', help='start a named agent session')
    start.add_argument('name')
    start.add_argument('--type', required=True, help='agent type (role name)')
    start.add_argument('--cwd')
    start.add_argument('--replace', action='store_true', help='replace an existing agent of this name')
    start.add_argument('--timeout', type=int, default=1800)
    start.add_argument('instruction', help='first instruction, or - for stdin')
    send = sub.add_parser('send', help='send a message to an agent (resumes its session)')
    send.add_argument('name')
    send.add_argument('message', help='message, or - for stdin')
    send.add_argument('--from', dest='sender', metavar='OTHER')
    send.add_argument('--timeout', type=int, default=1800)
    sub.add_parser('list', help='list agents')
    show = sub.add_parser('show', help='last reply and usage of an agent')
    show.add_argument('name')
    show.add_argument('--json', action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    handlers = {'start': cmd_start, 'send': cmd_send, 'list': cmd_list, 'show': cmd_show}
    try:
        return handlers[args.cmd](args)
    except common.UsageError as error:
        print('als-agent: %s' % error, file=sys.stderr)
        return 2
    except common.CommandError as error:
        print('als-agent: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
