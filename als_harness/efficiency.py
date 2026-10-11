"""Deterministic delegation/role/context policy. No model calls or file ingestion.

A prepared plan opts into versioned policy; existing 0.3 runs stay unchanged.
Complexity/risk are caller declarations, not an AI classifier or security boundary.
"""
from __future__ import annotations
import copy
import json
from .common import HarnessError, ROLES, digest, relative

POLICY_VERSION = 1
KINDS = {'exploration', 'research', 'test_execution', 'test_design', 'implementation', 'review'}
COMPLEXITIES = {'trivial', 'bounded', 'complex'}
RISKS = {'low', 'normal', 'high'}
PROFILES = {
    'lean': {'max_concurrent': 1, 'max_writers': 1, 'max_total_spawns': 6},
    'balanced': {'max_concurrent': 3, 'max_writers': 2, 'max_total_spawns': 12},
    'throughput': {'max_concurrent': 6, 'max_writers': 2, 'max_total_spawns': 24},
}
DEFAULT_ROLE = {
    'exploration': 'als_explorer', 'research': 'als_researcher',
    'test_execution': 'als_tester', 'test_design': 'als_test_designer',
    'implementation': 'als_worker', 'review': 'als_reviewer',
}
MAX_MESSAGE_CHARS = 8000  # Character bound, deliberately NOT labelled a token cap.
MAX_RESULT_CHARS = 2000  # Instruction to worker; not an API generation limit.


def short(value, name, limit, required=False):
    if not isinstance(value, str) or '\x00' in value or len(value) > limit or (required and not value.strip()):
        raise HarnessError(f'{name}: expected {"nonempty " if required else ""}text of at most {limit} characters')
    return value


def texts(values, name, count, width):
    if not isinstance(values, list) or len(values) > count:
        raise HarnessError(f'{name}: expected at most {count} items')
    return [short(v, name, width, True) for v in values]


def route(task):
    """Select a fixed TOML role; never silently override an explicit role."""
    kind, complexity, risk = (task.get(k) for k in ('kind', 'complexity', 'risk'))
    if kind not in KINDS or complexity not in COMPLEXITIES or risk not in RISKS:
        raise HarnessError('Prepared tasks require valid kind, complexity and risk')
    reason = short(task.get('delegation_reason', ''), 'delegation_reason', 500)
    executor = 'head' if complexity == 'trivial' and risk == 'low' and not reason.strip() else 'worker'
    role = DEFAULT_ROLE[kind]
    if kind == 'review' and (risk == 'high' or complexity == 'complex'):
        role = 'als_reviewer_deep'
    elif kind != 'review' and (risk == 'high' or (complexity == 'complex' and kind not in {'test_design'})):
        role = 'als_hard_worker'
    # Test execution with hard failure analysis should be classified test_design.
    if kind == 'test_design' and risk != 'high':
        role = 'als_test_designer'
    explicit = task.get('role')
    override = short(task.get('role_override_reason', ''), 'role_override_reason', 500)
    if explicit and explicit != role:
        if explicit not in ROLES or not override.strip():
            raise HarnessError(f'Task {task.get("id")}: recommended {role}; explicit role needs role_override_reason')
        if risk == 'high' and explicit not in {'als_hard_worker', 'als_reviewer_deep'}:
            raise HarnessError('High-risk work cannot be silently downgraded to a light role')
        role = explicit
    return {'executor': executor, 'role': role, 'basis': 'declared_kind_complexity_risk',
            'requested_model': 'gpt-6-astra' if executor == 'head' else ROLES[role][0],
            'requested_effort': 'medium' if executor == 'head' else ROLES[role][1]}


def normalize_task(task):
    t = copy.deepcopy(task)
    short(t.get('objective', ''), 'objective', 1200, True)
    short(t.get('id', ''), 'id', 100, True)
    t['context_files'] = sorted(set(relative(p) for p in texts(t.get('context_files', []), 'context_files', 12, 300)))
    t['known_facts'] = texts(t.get('known_facts', []), 'known_facts', 8, 350)
    t['constraints'] = texts(t.get('constraints', []), 'constraints', 8, 350)
    t['acceptance'] = texts(t.get('acceptance', []), 'acceptance', 8, 350)
    if not t['acceptance']:
        raise HarnessError('Each prepared task needs explicit acceptance conditions')
    short(t.get('full_context_reason', ''), 'full_context_reason', 500)
    t.update(route(t))
    return t


def signature(task):
    # Exact semantic-field duplicate detection only; do not invent fuzzy matches.
    return digest({'kind': task['kind'], 'objective': ' '.join(task['objective'].split()).casefold(),
                   'context_files': sorted(task.get('context_files', [])),
                   'write_paths': sorted(task.get('write_paths', [])),
                   'resources': sorted(task.get('resources', [])),
                   'acceptance': task.get('acceptance', [])})


def prepare(plan, profile='balanced', history=None):
    """Prepare a NEW plan. No DB mutation, commands, automatic spend or delegation."""
    if profile not in PROFILES or not isinstance(plan, dict):
        raise HarnessError('Unknown optimization profile or invalid plan')
    p = copy.deepcopy(plan)
    p['tasks'] = [normalize_task(t) for t in p.get('tasks', [])]
    prior = p.get('efficiency', {})
    if not isinstance(prior, dict):
        raise HarnessError('efficiency must be an object')
    e = dict(PROFILES[profile], version=POLICY_VERSION, profile=profile,
             max_message_chars=MAX_MESSAGE_CHARS, result_chars=MAX_RESULT_CHARS,
             finalization_reserve_percent=20)
    # Explicit caller restrictions can be lower, not silently higher than profile.
    for k, maximum in (('max_concurrent', 6), ('max_writers', 2), ('max_total_spawns', 50)):
        if k in prior:
            n = prior[k]
            if type(n) is not int or not 1 <= n <= maximum:
                raise HarnessError('Invalid efficiency.' + k)
            e[k] = n
    rationale = short(prior.get('parallel_reason', ''), 'parallel_reason', 500)
    if e['max_concurrent'] > 3 and len(rationale.strip()) < 20:
        raise HarnessError('More than 3 concurrent workers requires a specific parallel_reason')
    e['parallel_reason'] = rationale
    if any(t['risk'] == 'high' and t.get('write_paths') for t in p['tasks']):
        e['max_writers'] = 1
    p['purpose'] = p.get('purpose', 'production')
    if p['purpose'] not in {'production', 'diagnostic'}:
        raise HarnessError('purpose must be production or diagnostic')
    p['workload'] = short(p.get('workload', 'unspecified'), 'workload', 100, True)
    e['history_applied'] = False
    if history and history.get('eligible') and history.get('workload') == p['workload']:
        # Estimates from reviewed runs are still provisional; never lift a budget.
        by_bucket = history.get('estimates', {})
        for t in p['tasks']:
            sample = by_bucket.get(bucket(t))
            if sample and t['executor'] == 'worker':
                estimate = sample['suggested_tokens']
                if type(estimate) is int and estimate > 0:
                    t['estimate_tokens'] = max(t.get('estimate_tokens', 0), estimate)
                    t['estimate_basis'] = {'type': 'reviewed_observed_p80_with_margin',
                                           'runs': sample['run_ids']}
                    e['history_applied'] = True
        e['max_concurrent'] = min(e['max_concurrent'], history['suggested_max_concurrent'])
        e['history_note'] = 'Reviewed observed data; not complete billing, causal speed evidence or optimality.'
    p['efficiency'] = e
    validate(p)
    return p


def bucket(task):
    return '|'.join(str(task.get(k, 'unknown')) for k in ('role', 'kind', 'complexity', 'risk'))


def validate(plan):
    """Validate optional optimized-plan extension without changing legacy plans."""
    e = plan.get('efficiency')
    if e is None:
        return
    if not isinstance(e, dict) or type(e.get('version')) is not int or e['version'] != POLICY_VERSION:
        raise HarnessError('Unsupported efficiency policy version; use prepare')
    if e.get('profile') not in PROFILES:
        raise HarnessError('Unknown efficiency profile')
    for key, cap in (('max_concurrent', 6), ('max_writers', 2), ('max_total_spawns', 50),
                     ('max_message_chars', MAX_MESSAGE_CHARS), ('result_chars', MAX_RESULT_CHARS)):
        if type(e.get(key)) is not int or not 1 <= e[key] <= cap:
            raise HarnessError('Invalid efficiency.' + key)
    if e.get('finalization_reserve_percent') != 20:
        raise HarnessError('Prepared policy reserves 20% of an explicit soft budget for finalization')
    if e['max_concurrent'] > 3 and len(short(e.get('parallel_reason', ''), 'parallel_reason', 500).strip()) < 20:
        raise HarnessError('High concurrency requires parallel_reason')
    signatures = set()
    for t in plan.get('tasks', []):
        normalized = normalize_task(t)
        if t.get('executor') != normalized['executor'] or t.get('role') != normalized['role']:
            raise HarnessError('Prepared task routing was edited inconsistently')
        sig = signature(normalized)
        if sig in signatures:
            raise HarnessError('Duplicate task scope/objective; bundle work or reuse the existing worker')
        signatures.add(sig)
        if len(build_message(plan, normalized)) > e['max_message_chars']:
            raise HarnessError('Task contract too large; reduce scope, not acceptance requirements')
    if sum(t.get('executor') == 'worker' for t in plan.get('tasks', [])) > e['max_total_spawns']:
        raise HarnessError('Planned workers exceed total spawn limit; split the run or set an explicit limit')


def build_message(plan, task):
    """References only. Does NOT read project files or carry parent conversation."""
    e = plan.get('efficiency') or {}
    if not e:
        raise HarnessError('task-message requires a prepared plan')
    contract = {
        'objective': task['objective'],
        'read_first': task.get('context_files', []),
        'known_facts': task.get('known_facts', []),
        'write_paths': task.get('write_paths', []),
        'resources': task.get('resources', []),
        'constraints': task.get('constraints', []),
        'acceptance': task.get('acceptance', []),
        'declared_check_ids': task.get('checks', []),
    }
    return ('ALS_TASK=' + task['id'] + '\n'
            + json.dumps(contract, ensure_ascii=False, separators=(',', ':')) + '\n'
            + 'Read only relevant ranges; do not repeat existing findings or spawn children. '
            + 'Use read_first as pointers, not permission to broaden scope. Preserve user changes. '
            + f'Return at most {e.get("result_chars", MAX_RESULT_CHARS)} characters of summary: '
            + 'result, file/symbol references, checks actually run, blockers/uncertainties. '
            + 'Do not paste full files or raw logs. Do not omit material failures to fit; '
            + 'point to local evidence and request focused follow-up instead.')


def admission(plan, task, tasks, requested=None):
    """Additional reasons for the existing reservation gate (no implicit allow)."""
    e = plan.get('efficiency')
    if not e:
        return []
    reasons = []
    if task.get('executor') == 'head':
        reasons.append('trivial task assigned to HEAD; no new worker needed')
    if sum(len(t.get('attempts', [])) for t in tasks if t.get('executor') != 'head') >= e['max_total_spawns']:
        reasons.append('optimized total spawn allowance exhausted')
    if requested is not None:
        message = requested.get('message', requested.get('prompt'))
        if not isinstance(message, str):
            reasons.append('optimized spawn requires a text task contract')
        elif len(message) > e['max_message_chars']:
            reasons.append('spawn message exceeds character budget; use task-message and references')
        # Never add unsupported fields to a runtime tool. Inspect only when present.
        if 'fork_context' in requested:
            if type(requested['fork_context']) is not bool:
                reasons.append('invalid fork_context value')
            elif requested['fork_context'] and not task.get('full_context_reason', '').strip():
                reasons.append('full context inheritance requires a recorded full_context_reason')
    return reasons
