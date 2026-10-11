"""Coexist with user Codex hooks. Merge only exact ALS-owned JSON groups."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shlex

from .common import HarnessError, digest

HOOK_EVENTS = (
    'SessionStart', 'PreToolUse', 'PostToolUse', 'SubagentStart',
    'SubagentStop', 'Stop', 'SessionEnd',
)


def groups(project):
    command = (
        'python3 ' + shlex.quote(str(project / '.astra-luna-sol/harness.py'))
        + ' --project ' + shlex.quote(str(project)) + ' hook'
    )
    out = {}
    for event in HOOK_EVENTS:
        handler = {
            'type': 'command',
            'command': command,
            'timeout': 3 if event == 'SessionEnd' else 10,
            'statusMessage': 'ALS harness',
        }
        if event in {'SessionStart', 'SubagentStart', 'PreToolUse'}:
            handler['additionalContextLimit'] = 500
        out[event] = {'hooks': [handler]}
    return out


def parse(raw):
    try:
        data = json.loads(raw) if raw is not None else {}
    except (ValueError, UnicodeError) as ex:
        raise HarnessError('Invalid existing hooks.json') from ex
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        raise HarnessError('Unsupported hooks configuration shape')
    if any(not isinstance(v, list) for v in data.get('hooks', {}).values()):
        raise HarnessError('Hook events must contain matcher group arrays')
    return data


def remove(raw, state):
    data = parse(raw)
    for event, owned in state['groups'].items():
        current = data.get('hooks', {}).get(event, [])
        matches = [i for i, x in enumerate(current) if x == owned]
        if len(matches) != 1:
            raise HarnessError('Edited/missing/duplicated ALS hook ' + event + '; inspect before reinstall')
        del current[matches[0]]
        if not current and event not in state['prior_events']:
            data['hooks'].pop(event, None)
    if not data.get('hooks') and not state['prior_hooks_key']:
        data.pop('hooks', None)
    if state['created'] and not data:
        return None
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()


def merge(raw, old_state, project):
    if old_state:
        raw = remove(raw, old_state)
    data = parse(raw)
    before = copy.deepcopy(data)
    owned = groups(project)
    # Refuse orphan ALS handlers rather than stacking a second installation.
    if '.astra-luna-sol/harness.py' in json.dumps(data):
        raise HarnessError('Unowned ALS hook command found')
    existing = data.setdefault('hooks', {})
    for event, group in owned.items():
        existing.setdefault(event, []).append(group)
    state = {
        'created': raw is None,
        'prior_hooks_key': 'hooks' in before,
        'prior_events': list(before.get('hooks', {})),
        'groups': owned,
    }
    new = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    if parse(remove(new, state)) != before:
        raise HarnessError('Unmanaged hook definitions changed')
    return new, state
