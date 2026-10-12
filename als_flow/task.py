"""als-task: shared team task list (Claude Code `TaskCreate` / `TaskUpdate` / `TaskList`).

    als-task add "title" [--owner NAME] [--deps ID,ID]      prints the new ID
    als-task update ID --status todo|in_progress|blocked|done [--owner NAME] [--note TEXT]
    als-task list [--json]

Stored in `_workspace/tasks.json`; every read-modify-write holds an fcntl lock
on `_workspace/tasks.json.lock`, so the lead and agents can update it at once.
A task whose dependencies are not all `done` is listed as `blocked-by ID,...`.
Exit codes: 0 ok, 1 command failure (e.g. unknown ID), 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import common

STATUSES = ('todo', 'in_progress', 'blocked', 'done')


def paths():
    root = common.workspace_dir()
    return root / 'tasks.json', root / 'tasks.json.lock'


def load(path):
    data = common.read_json(path)
    if data is None and path.exists():
        raise common.CommandError('%s is not valid JSON; fix or remove it' % path)
    if not isinstance(data, dict):
        data = {'next_id': 1, 'tasks': []}
    data.setdefault('next_id', 1)
    data.setdefault('tasks', [])
    return data


def blocked_by(task, tasks):
    status = {t['id']: t['status'] for t in tasks}
    return [dep for dep in task.get('deps') or [] if status.get(dep) != 'done']


def parse_deps(text):
    if not text:
        return []
    deps = []
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise common.UsageError('invalid dependency id %r' % part)
        deps.append(int(part))
    return deps


def cmd_add(args):
    if not args.title.strip():
        raise common.UsageError('empty title')
    deps = parse_deps(args.deps)
    path, lock = paths()
    with common.file_lock(lock):
        data = load(path)
        known = {t['id'] for t in data['tasks']}
        missing = [d for d in deps if d not in known]
        if missing:
            raise common.CommandError('unknown dependency id(s): %s' % ', '.join(map(str, missing)))
        task_id = data['next_id']
        now = time.time()
        data['tasks'].append({'id': task_id, 'title': args.title.strip(), 'status': 'todo', 'owner': args.owner,
                              'deps': deps, 'notes': [], 'created': now, 'updated': now})
        data['next_id'] = task_id + 1
        common.write_json(path, data)
    print(task_id)
    return 0


def cmd_update(args):
    path, lock = paths()
    with common.file_lock(lock):
        data = load(path)
        task = next((t for t in data['tasks'] if t['id'] == args.id), None)
        if task is None:
            raise common.CommandError('unknown task id %d' % args.id)
        if args.status:
            task['status'] = args.status
        if args.owner is not None:
            task['owner'] = args.owner or None
        if args.note:
            task['notes'].append({'ts': time.time(), 'text': args.note})
        task['updated'] = time.time()
        common.write_json(path, data)
        waiting = blocked_by(task, data['tasks'])
    line = 'task %d: %s' % (task['id'], task['status'])
    if waiting and task['status'] in ('in_progress', 'done'):
        line += ' (warning: still blocked by %s)' % ','.join(map(str, waiting))
    print(line)
    return 0


def cmd_list(args):
    path, lock = paths()
    with common.file_lock(lock):
        data = load(path)
    tasks = data['tasks']
    rows = []
    for task in tasks:
        row = dict(task, blocked_by=blocked_by(task, tasks))
        rows.append(row)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print('no tasks')
        return 0
    for row in rows:
        extra = '  blocked-by %s' % ','.join(map(str, row['blocked_by'])) if row['blocked_by'] else ''
        print('%-4d %-12s %-14s %s%s' % (row['id'], row['status'], row.get('owner') or '-', row['title'], extra))
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, '%s: error: %s\n' % (self.prog, message))


def parser():
    p = Parser(prog='als-task', description='Shared team task list in _workspace/tasks.json.')
    sub = p.add_subparsers(dest='cmd', parser_class=Parser)
    sub.required = True
    add = sub.add_parser('add', help='add a task; prints its ID')
    add.add_argument('title')
    add.add_argument('--owner')
    add.add_argument('--deps', help='comma-separated task IDs')
    update = sub.add_parser('update', help='change status, owner or add a note')
    update.add_argument('id', type=int)
    update.add_argument('--status', choices=STATUSES)
    update.add_argument('--owner')
    update.add_argument('--note')
    lst = sub.add_parser('list', help='list tasks')
    lst.add_argument('--json', action='store_true')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.cmd == 'update' and not (args.status or args.owner is not None or args.note):
        p.error('update needs --status, --owner or --note')
    handlers = {'add': cmd_add, 'update': cmd_update, 'list': cmd_list}
    try:
        return handlers[args.cmd](args)
    except common.UsageError as error:
        print('als-task: %s' % error, file=sys.stderr)
        return 2
    except common.CommandError as error:
        print('als-task: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
