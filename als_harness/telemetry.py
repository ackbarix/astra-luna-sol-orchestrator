"""Read-only cost/context diagnostics and explicitly reviewed calibration.

No price table, LLM grading, benchmark claims, run mutation during analysis,
implicit tuning of live sessions, or exclusion of failed attempts from totals.
"""
from __future__ import annotations
from collections import defaultdict
import json
import math
from .common import HarnessError, ROLES, TERMINAL, now
from .efficiency import bucket, short

FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
MIN_RUNS = 5
HISTORY_LIMIT = 50


def count(value):
    if type(value) is not int or value < 0:
        raise HarnessError('Invalid usage counter; cannot calibrate from this record')
    return value


def metrics(data):
    """Analyze ALS schema-1 run reports. No session text or source code exported."""
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise HarnessError('Expected an ALS harness schema-1 run report')
    rows = data.get('usage_by_thread_model_effort', [])
    if not isinstance(rows, list):
        raise HarnessError('Invalid usage rows')
    total = {k: 0 for k in FIELDS}
    by_identity = {}
    head_raw = head_responses = responses = 0
    for row in rows:
        if not isinstance(row, dict):
            raise HarnessError('Invalid usage row')
        u = {k: count(row.get(k, 0)) for k in FIELDS}
        n = count(row.get('responses', 0))
        if u['cached_input_tokens'] > u['input_tokens'] or u['reasoning_output_tokens'] > u['output_tokens']:
            raise HarnessError('Cache/reasoning counter exceeds containing total')
        key = (row.get('recorded_model', 'unknown'), row.get('recorded_effort', 'unknown'))
        if not all(isinstance(x, str) for x in key):
            raise HarnessError('Invalid recorded model identity')
        dest = by_identity.setdefault(key, dict.fromkeys(FIELDS, 0) | {'responses': 0})
        for k in FIELDS:
            total[k] += u[k]; dest[k] += u[k]
        dest['responses'] += n; responses += n
        if row.get('thread') == data.get('session') and data.get('session') is not None:
            head_raw += u['input_tokens'] + u['output_tokens']; head_responses += n
    raw = total['input_tokens'] + total['output_tokens']
    uncached = total['input_tokens'] - total['cached_input_tokens']
    tasks = data.get('workers', [])
    if not isinstance(tasks, list):
        raise HarnessError('Invalid task rows')
    attempts = sum(len(t.get('attempts', [])) for t in tasks if t.get('executor') != 'head')
    retries = sum(max(0, len(t.get('attempts', []))-1) for t in tasks if t.get('executor') != 'head')
    messages = [a.get('context', {}).get('message_chars') for t in tasks for a in t.get('attempts', [])]
    messages = [x for x in messages if type(x) is int and x >= 0]
    warnings = list(data.get('warnings', []))
    recommendations = []
    if not raw:
        warnings.append('No response usage observed; this does not mean zero consumption')
    if head_responses > 6 and raw and head_raw/raw > .6:
        recommendations.append('Inspect repeated HEAD coordination and long sessions; batch related steps and return short worker evidence. Cached reads are not billed like uncached input.')
    if retries:
        recommendations.append('Review failed/repeated attempts before lowering effort; retries are included, not removed from the comparison.')
    if messages and max(messages) > 8000:
        recommendations.append('Some worker messages exceeded 8000 characters; prefer task-message and targeted file references.')
    if any(a.get('context', {}).get('fork_context') is True for t in tasks for a in t.get('attempts', [])):
        recommendations.append('Parent context was explicitly requested for some workers; inspect the recorded reason before reusing it.')
    if not data.get('efficiency'):
        recommendations.append('Legacy run: use prepare for future work; existing run settings are intentionally unchanged.')
    return {
        'run': data.get('run'), 'status': data.get('status'),
        'recorded_totals': total | {'total_tokens': raw, 'uncached_input_tokens': uncached,
                                     'uncached_plus_output_tokens': uncached+total['output_tokens']},
        'input_cache_fraction': round(total['cached_input_tokens']/total['input_tokens'], 4) if total['input_tokens'] else None,
        'head_fraction_of_recorded_total': round(head_raw/raw, 4) if raw else None,
        'head_responses': head_responses, 'all_responses': responses,
        'worker_dispatch_attempts': attempts, 'retry_attempts': retries,
        'by_model_effort': [{'model': m, 'effort': e, **u} for (m,e),u in sorted(by_identity.items())],
        'message_chars_observed': messages, 'recommendations': recommendations,
        'warnings': warnings, 'recorded_usage_is_lower_bound': True,
        'billing_verified': False, 'causal_performance_comparison': False,
        'note': 'Cache is part of input; reasoning is part of output. Total input+output is not money. Runtime duration includes user waits unless separately measured.',
    }


def review_measurement(engine, rid, workload, quality, acknowledge, elapsed_seconds=None):
    """User/HEAD evidence attestation, NOT authenticated or automatic grading."""
    workload = short(workload, 'workload', 100, True)
    if quality not in {'pass', 'fail'} or not acknowledge:
        raise HarnessError('Measurement needs pass/fail and --ack-recorded-only after reviewing evidence')
    if elapsed_seconds is not None and (not math.isfinite(elapsed_seconds) or elapsed_seconds <= 0):
        raise HarnessError('Measured elapsed seconds must be positive and finite')
    with engine.s.tx():
        r = engine.s.run(rid)
        if r['status'] not in TERMINAL:
            raise HarnessError('Review only terminal runs; finish or cancel without deleting failed work')
        if any(t.get('claims_held') for t in engine.s.tasks(rid)):
            raise HarnessError('Release/reconcile active claims before measurement review')
        if quality == 'pass' and r['status'] != 'completed':
            raise HarnessError('A cancelled run cannot be labelled passed')
        r['measurement'] = {'workload': workload, 'quality': quality, 'reviewed_recorded_coverage': True,
                            'elapsed_seconds': elapsed_seconds, 'stamp': now(),
                            'basis': 'caller_attestation_not_objective_benchmark'}
        engine.s.save_run(r)
    return {'recorded': True, 'automatic_model_grading': False, 'benchmark_proven': False}


def history(store, workload):
    """At least five distinct reviewed production runs; failed runs remain included.

    P80 + 25% is a cautious starting heuristic, not a token guarantee. Uses only
    same-workload, same-role/kind/complexity/risk cohorts. No auto model changes.
    """
    result = {'workload': workload, 'eligible': False, 'reviewed_runs': 0, 'estimates': {},
              'suggested_max_concurrent': 3, 'minimum_runs': MIN_RUNS,
              'automatic_changes': False, 'warnings': []}
    if store is None:
        result['warnings'].append('No local history. Retain provisional profile and explicit user budget.')
        return result
    candidates = [json.loads(x['doc']) for x in store.db.execute(
        'SELECT doc FROM runs ORDER BY rowid DESC LIMIT ?', (HISTORY_LIMIT,))]
    samples = defaultdict(list); eligible_runs = []; qualities = []; retry_runs = 0
    for r in candidates:
        m = r.get('measurement', {})
        if (r.get('status') not in TERMINAL or r.get('plan', {}).get('purpose') != 'production'
            or not m.get('reviewed_recorded_coverage') or m.get('workload') != workload
            or r.get('plan', {}).get('workload') != workload or m.get('quality') not in {'pass','fail'}):
            continue
        tasks = store.tasks(r['id'])
        if r.get('warnings') or any(t.get('claims_held') for t in tasks):
            continue
        records = [json.loads(x['doc']) | {'thread': x['thread']} for x in store.db.execute(
            'SELECT thread,doc FROM usage WHERE run=?', (r['id'],))]
        if not records:
            continue
        by_thread = defaultdict(int); identities = defaultdict(set)
        try:
            for ev in records:
                u = ev['usage']; raw = count(u['input_tokens']) + count(u['output_tokens'])
                by_thread[ev['thread']] += raw
                identities[ev['thread']].add((ev['model'], ev['effort']))
        except (KeyError, TypeError, HarnessError):
            continue
        # Missing head/worker records or identity mismatches cannot guide budgets.
        if not r.get('session') or not by_thread[r['session']]:
            continue
        cohort = []
        valid = True
        for t in tasks:
            if t.get('executor') == 'head' or not t.get('attempts'):
                continue
            threads = [a.get('thread_id') for a in t['attempts']]
            if any(not tid or not by_thread[tid] for tid in threads):
                valid = False; break
            for a in t['attempts']:
                expected = (a['requested_model'], a['requested_effort'])
                if identities[a['thread_id']] != {expected}:
                    valid = False; break
            if not valid:
                break
            # Include all retry costs but do not misassign different-model retries
            # to the final role's cost bucket.
            if len({(a['requested_model'],a['requested_effort']) for a in t['attempts']}) > 1:
                continue
            if all(k in t for k in ('role','kind','complexity','risk')):
                cohort.append((bucket(t), sum(by_thread[tid] for tid in set(threads))))
        if not valid:
            continue
        eligible_runs.append(r['id']); qualities.append(m['quality'])
        retry_runs += any(len(t.get('attempts', [])) > 1 for t in tasks)
        for key, value in cohort:
            samples[key].append((r['id'], value))
    result['reviewed_runs'] = len(eligible_runs)
    if len(eligible_runs) < MIN_RUNS:
        result['warnings'].append('Insufficient reviewed production samples; diagnostic smoke tests are not calibration data.')
        return result
    result['eligible'] = True
    result['run_ids'] = eligible_runs
    failure_fraction = qualities.count('fail')/len(qualities)
    result['failure_fraction'] = round(failure_fraction, 3)
    result['suggested_max_concurrent'] = 1 if failure_fraction >= .4 else 2 if failure_fraction > .1 or retry_runs/len(qualities) > .2 else 3
    for key, group in samples.items():
        ids = sorted({rid for rid,_ in group})
        if len(ids) < MIN_RUNS:
            continue
        # Multiple tasks in one run do not fake a five-run sample size.
        values = sorted(value for _,value in group)
        p80 = values[max(0, math.ceil(.8*len(values))-1)]
        result['estimates'][key] = {'suggested_tokens': math.ceil(p80*1.25),
                                    'p80_observed_tokens': p80, 'run_ids': ids,
                                    'sample_tasks': len(values)}
    result['warnings'].append('Recommendations use caller-reviewed recorded lower bounds, not invoices, randomized quality trials or guaranteed speed improvements. Budgets are never raised automatically.')
    return result
