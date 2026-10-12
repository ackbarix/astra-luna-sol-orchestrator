"""ALS hook groups in Codex hooks.json (releases before 1.2).

1.2 removed the managed-run harness and installs no hooks; the installer keeps
this module only to recognise and splice out the groups older releases added,
leaving user hooks untouched.

Hook commands are relocatable (0.5): a project install resolves the main
repository root at run time through git, so moving the project or opening a
forked worktree/subdirectory keeps working; a missing harness exits 0. A
global install points at the shared ALS home, written as
``${HOME:-/abs/home}/...`` when it lives under the home dir.
"""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import re
import shlex

import hashlib


class HarnessError(ValueError):
    """An unsafe or unsupported hooks change."""


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

HOOK_EVENTS = (
    'SessionStart', 'PreToolUse', 'PostToolUse', 'SubagentStart',
    'SubagentStop', 'Stop', 'SessionEnd',
)
# Codex compiles a group's "matcher" as a pattern over the tool name; only the
# spawn tool matters to the harness, so other tool calls skip the process start.
SPAWN_MATCHER = 'spawn_agent'
MATCHED_EVENTS = {'PreToolUse', 'PostToolUse'}
# Portable to old git (no --path-format): --git-common-dir may be relative to
# the cwd, so resolve it with cd/pwd. Works from subdirectories and worktrees.
# Git < 2.13 prints a relative common dir relative to the top level even from a
# subdirectory; a relative path that does not exist from the cwd is therefore
# resolved against ``--show-cdup``.
# The common dir's parent is the main checkout only when the common dir is a
# ``.git`` directory; in a submodule (.git/modules/NAME) use the top level, like
# cli.git_project. CDPATH is cleared so an exported CDPATH cannot redirect or
# pollute the ``cd``. A missing harness (fresh clone, submodule without ALS,
# no repository) exits 0 silently so it never blocks a tool call.
PROJECT_RESOLVE = ('c=$(git rev-parse --git-common-dir 2>/dev/null) || exit 0; '
                   'case $c in /*) ;; *) [ -d "$c" ] || c=$(git rev-parse --show-cdup 2>/dev/null)$c ;; esac; '
                   'case $c in .git|*/.git) r=$(CDPATH= cd -- "$c/.." && pwd) ;; '
                   '*) r=$(git rev-parse --show-toplevel 2>/dev/null) ;; esac || exit 0; '
                   's=$r/.astra-luna-sol/harness.py; [ -f "$s" ] || exit 0; ')
DEFAULT_PYTHON = 'python3'

# Any handler that runs a harness.py entrypoint in hook mode: 0.3/0.4 used an
# absolute path plus --project; 0.5 uses the project/global forms built below
# (the project form names harness.py first and ends with `exec PY "$s" hook`).
ALS_COMMAND = re.compile(r'''harness\.py\b.*\shook\s*$''', re.S)
# Characters that may appear unescaped as the default in "${HOME:-...}".
_SAFE_HOME = re.compile(r'[A-Za-z0-9_./@%+=:,-]+')


def _escape_double_quoted(text):
    return re.sub(r'(["\\$`])', r'\\\1', text)


def interpreter(python=None):
    """'python3' from PATH (default) or a shell-quoted absolute interpreter."""
    if not python or python == DEFAULT_PYTHON:
        return DEFAULT_PYTHON
    if not os.path.isabs(python):
        raise HarnessError('Hook interpreter must be python3 or an absolute path')
    return shlex.quote(python)


def home_command(als_home, home=None, python=None):
    """Shell command for a global install.

    Under HOME it is ``"${HOME:-/abs/home}/rel/harness.py"``: relocatable with
    HOME, and still valid when Codex runs hooks with HOME unset. A home path
    that needs quoting inside ``${...}`` falls back to the absolute path.
    """
    target = Path(als_home).expanduser().resolve() / 'harness.py'
    home = Path(home or os.environ.get('HOME') or Path.home()).resolve()
    try:
        rel = target.relative_to(home)
    except ValueError:
        rel = None
    if rel is None or not _SAFE_HOME.fullmatch(str(home)):
        return interpreter(python) + ' ' + shlex.quote(str(target)) + ' hook'
    return (interpreter(python) + ' "${HOME:-' + str(home) + '}/'
            + _escape_double_quoted(rel.as_posix()) + '" hook')


def command(scope='project', als_home=None, python=None):
    if isinstance(scope, Path):
        scope = 'project'  # pre-0.5 callers passed the project path
    if scope == 'project':
        return PROJECT_RESOLVE + 'exec ' + interpreter(python) + ' "$s" hook'
    if scope == 'global':
        if als_home is None:
            raise HarnessError('Global hook scope requires the ALS home path')
        return home_command(als_home, python=python)
    raise HarnessError('Unknown hook scope: ' + str(scope))


def groups(scope='project', als_home=None, python=None) -> dict:
    """ALS-owned hook groups. No absolute project path is embedded."""
    cmd = command(scope, als_home, python)
    out = {}
    for event in HOOK_EVENTS:
        handler = {
            'type': 'command',
            'command': cmd,
            'timeout': 3 if event == 'SessionEnd' else 10,
            'statusMessage': 'ALS harness',
        }
        if event in {'SessionStart', 'SubagentStart', 'PreToolUse'}:
            handler['additionalContextLimit'] = 500
        group = {'hooks': [handler]}
        if event in MATCHED_EVENTS:
            group = {'matcher': SPAWN_MATCHER, **group}
        out[event] = group
    return out


def legacy_project(state):
    """Absolute project path baked into a 0.3/0.4 manifest's hook groups."""
    for group in (state or {}).get('groups', {}).values():
        for handler in group.get('hooks', []) if isinstance(group, dict) else []:
            cmd = handler.get('command', '') if isinstance(handler, dict) else ''
            if ' --project ' in cmd and cmd.endswith(' hook'):
                try:
                    parts = shlex.split(cmd)
                except ValueError:
                    return None
                return parts[parts.index('--project') + 1]
    return None


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


def serialize(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()


def original_bytes(state):
    encoded = (state or {}).get('original_b64')
    if encoded is None:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as ex:
        raise HarnessError('Corrupt hooks restoration data in manifest') from ex


def untouched(raw, state):
    """True when the file is exactly what ALS wrote (byte-exact restore is safe)."""
    return raw is not None and state.get('installed_sha256') == digest(raw)


def remove(raw, state):
    """Splice ALS groups out; restore the pre-install bytes when nothing else changed."""
    original = original_bytes(state)
    if original is not None and untouched(raw, state):
        return original
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
    return serialize(data)


def unmanaged_changed(raw, state):
    """ALS groups are intact but other hook definitions changed since install."""
    if raw is None or not state or untouched(raw, state) or 'installed_sha256' not in state:
        return False
    try:
        rest = remove(raw, state)
    except HarnessError:
        return False
    before = original_bytes(state)
    if before is None and state.get('created'):
        return rest is not None
    return before is not None and parse(rest) != parse(before)


def handler_commands(data):
    for event_groups in data.get('hooks', {}).values():
        for group in event_groups:
            handlers = group.get('hooks', []) if isinstance(group, dict) else []
            for handler in handlers if isinstance(handlers, list) else []:
                cmd = handler.get('command') if isinstance(handler, dict) else None
                if isinstance(cmd, str):
                    yield cmd


def has_global_handler(data):
    """An ALS handler that runs a shared (global, --hooks-only) harness."""
    return any(ALS_COMMAND.search(cmd) and '.astra-luna-sol/harness.py' not in cmd
               for cmd in handler_commands(data))


def has_als_handler(data):
    return any(ALS_COMMAND.search(cmd) for cmd in handler_commands(data))


def merge(raw, old_state, scope='project', als_home=None, python=None):
    if old_state:
        raw = remove(raw, old_state)
    data = parse(raw)
    before = copy.deepcopy(data)
    owned = groups(scope, als_home, python)
    # Refuse orphan ALS handlers rather than stacking a second installation.
    if has_als_handler(data):
        raise HarnessError('Unowned ALS hook command found in hooks.json; remove it or use --adopt')
    existing = data.setdefault('hooks', {})
    for event, group in owned.items():
        existing.setdefault(event, []).append(group)
    new = serialize(data)
    state = {
        'created': raw is None,
        'prior_hooks_key': 'hooks' in before,
        'prior_events': list(before.get('hooks', {})),
        'groups': owned,
        'installed_sha256': digest(new),
        'original_b64': base64.b64encode(raw).decode('ascii') if raw is not None else None,
    }
    spliced = dict(state, original_b64=None, installed_sha256=None)
    if parse(remove(new, spliced)) != before:
        raise HarnessError('Unmanaged hook definitions changed')
    return new, state


def adopt(raw, scope='project', als_home=None, python=None):
    """Synthesize ownership of exact current ALS groups found without a manifest."""
    if raw is None:
        return None
    data = parse(raw)
    owned = groups(scope, als_home, python)
    hooks = data.get('hooks', {})
    for event, group in owned.items():
        if sum(1 for x in hooks.get(event, []) if x == group) != 1:
            return None
    prior = [e for e, v in hooks.items() if [x for x in v if x != owned.get(e)]]
    only_als = not prior and set(data) == {'hooks'}
    return {
        'created': only_als,
        'prior_hooks_key': not only_als,
        'prior_events': prior,
        'groups': owned,
        'installed_sha256': None,
        'original_b64': None,
    }
