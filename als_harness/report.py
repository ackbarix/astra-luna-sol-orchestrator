"""Records, not model recollection. Do not claim serving identity or cost savings."""
import json
from .common import redact

USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
WORKER_FIELDS = ('id', 'role', 'status', 'attempts', 'write_paths', 'resources', 'executor', 'kind', 'complexity', 'risk')
CHECK_FIELDS = ('id', 'status', 'exit_code', 'timed_out', 'snapshot', 'stamp', 'log_path')


def report(engine, rid):
    r = engine.s.run(rid)
    tasks = engine.s.tasks(rid)
    budget = engine.budget(r)
    budget['usage_synced_at'] = r.get('last_usage_sync')  # staleness of confirmed_tokens
    usage = {}
    for row in engine.s.db.execute('SELECT thread,doc FROM usage WHERE run=?', (rid,)):
        e = json.loads(row['doc'])
        key = (row['thread'], e['model'], e['effort'])
        u = usage.setdefault(key, {'thread': key[0], 'recorded_model': key[1], 'recorded_effort': key[2],
                                   'responses': 0, **{k: 0 for k in USAGE_FIELDS}})
        u['responses'] += 1
        for k in USAGE_FIELDS:
            u[k] += e['usage'][k]
    return {'schema_version': 1, 'run': rid, 'goal': redact(r['goal']), 'status': r['status'], 'mode': r['mode'],
            'session': r.get('session'),
            'head_requested': ['gpt-6-astra', 'medium'],
            'workers': [{k: t.get(k) for k in WORKER_FIELDS} for t in tasks],
            'checks': [{k: e.get(k) for k in CHECK_FIELDS} for e in r['evidence'].values()],
            'assessment_ids': list(r['assessments']), 'assessments_are_self_reported': True,
            'budget': budget, 'usage_by_thread_model_effort': list(usage.values()),
            'efficiency': r['plan'].get('efficiency'), 'measurement': r.get('measurement'),
            'closed_at': r.get('closed_at'), 'purpose': r['plan'].get('purpose', 'legacy'),
            'usage_scope': 'root run window and linked child records; not billing',
            'warnings': r.get('warnings', []), 'completion_problems': r.get('completion_problems', []),
            'last_usage_sync': r.get('last_usage_sync'), 'upstream_identity_verified': False,
            'billing_verified': False,
            'note': 'Completion checks validate declared checks, not all possible bugs. No OS sandbox or hard spend cap.'
                    ' Unobserved worker routing stays unverified.'}


def markdown(data):
    lines = [f"# ALS run {data['run']}", f"Status: {data['status']} | mode: {data['mode']}",
             '', '|Task|Role|State|Attempts|', '|---|---|---|---:|']
    for t in data['workers']:
        lines.append(f"|{t['id']}|{t['role']}|{t['status']}|{len(t['attempts'])}|")
    lines += ['', f"Confirmed tokens: {data['budget']['confirmed_tokens']}; budget stage: {data['budget']['stage']}",
              data['note']]
    lines += ['- ' + redact(w) for w in data['warnings'] + data['completion_problems']]
    return '\n'.join(lines)


def compact(data):
    """Small progress response; full evidence remains in report/status without this flag."""
    return {'run': data['run'], 'status': data['status'], 'mode': data['mode'],
            'tasks': [{k: t.get(k) for k in ('id', 'role', 'executor', 'status')} for t in data['workers']],
            'checks': [{'id': c['id'], 'status': c['status']} for c in data['checks']],
            'recorded_tokens': data['budget']['confirmed_tokens'],
            'problems': data['completion_problems'], 'warnings': data['warnings']}
