"""Flow: the Codex equivalent of a Claude Code `Workflow` script.

Claude Code runs a workflow script with `agent()`, `pipeline()`, `parallel()`,
phases, a token budget and `resumeFromRunId`. Here the script is plain Python:

    from als_flow import Flow
    flow = Flow(name='review', phases=['collect', 'verify'])
    with flow.phase('collect'):
        found = flow.pipeline(items, lambda item: flow.agent('... ' + item, agent_type='als_explorer'))
    flow.done({'found': found})

Each `agent()` call is one ephemeral `codex exec` using the role file's pins.
A run lives in `_workspace/.flow/<run_id>/` (meta.json, log.jsonl,
calls/<sha>.json, result.json). `Flow(resume_from=RUN_ID)` (or env
`ALS_FLOW_RESUME`) reuses the cached results of identical successful calls, so
a re-run only pays for changed or failed calls.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import uuid

from . import common

MAX_CONCURRENCY = 6
DEFAULT_CONCURRENCY = 4


def flow_root(cwd=None):
    return common.workspace_dir(cwd) / '.flow'


def call_key(prompt, agent_type, schema, model, effort):
    blob = json.dumps({'prompt': prompt, 'agent_type': agent_type, 'schema': schema,
                       'model': model, 'effort': effort}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def check_schema(value, schema):
    """Top-level check only: object type and `required` keys."""
    if not isinstance(schema, dict):
        return True
    if schema.get('type') == 'object' or schema.get('required'):
        if not isinstance(value, dict):
            return False
        return all(key in value for key in schema.get('required') or [])
    return True


class Flow:
    """One workflow run. Thread-safe: `agent()` may be called from any thread."""

    MAX_CALLS = 1000

    def __init__(self, name, phases=None, workdir='.', budget_tokens=None, concurrency=DEFAULT_CONCURRENCY,
                 resume_from=None):
        if not isinstance(name, str) or not name.strip():
            raise ValueError('flow name must be a non-empty string')
        for title in phases or []:
            if not isinstance(title, str) or not title:
                raise ValueError('phase titles must be non-empty strings')
        self.name = name
        self.phases = list(phases or [])
        self.workdir = Path(workdir).resolve()
        self.budget_tokens = budget_tokens
        self.concurrency = max(1, min(MAX_CONCURRENCY, int(concurrency or DEFAULT_CONCURRENCY)))
        self.max_calls = self.MAX_CALLS
        self.root = flow_root(self.workdir)
        resume_from = resume_from or os.environ.get('ALS_FLOW_RESUME') or None
        self.resume_from = resume_from
        if resume_from and not (self.root / resume_from / 'meta.json').is_file():
            raise ValueError('cannot resume: unknown run %r in %s' % (resume_from, self.root))
        slug = re.sub(r'[^A-Za-z0-9_.-]+', '-', name).strip('-')[:40] or 'flow'
        self.run_id = '%s-%s-%s' % (time.strftime('%Y%m%d-%H%M%S'), slug, uuid.uuid4().hex[:4])
        self.dir = self.root / self.run_id
        (self.dir / 'calls').mkdir(parents=True, exist_ok=False)
        self._lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(self.concurrency)
        self._phase = threading.local()
        self._usage = {'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0}
        self._counts = {'total': 0, 'ok': 0, 'failed': 0, 'cached': 0, 'refused': 0}
        self._codex = None
        self._base_url = None
        self._roles = {}
        self.meta = {
            'run_id': self.run_id, 'name': name, 'workdir': str(self.workdir),
            'phases': [{'title': t, 'status': 'pending'} for t in self.phases],
            'budget_tokens': budget_tokens, 'concurrency': self.concurrency, 'resume_from': resume_from,
            'script': os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else None,
            'started': time.time(), 'status': 'running', 'calls': self._counts, 'usage': self._usage,
            'result_path': None,
        }
        self._save_meta()
        self.log('run %s started%s' % (self.run_id, ' (resume from %s)' % resume_from if resume_from else ''),
                 event='start')

    # ------------------------------------------------------------ bookkeeping

    def _save_meta(self):
        with self._lock:
            common.write_json(self.dir / 'meta.json', self.meta)

    def log(self, msg, event='log', **fields):
        record = {'ts': time.time(), 'event': event, 'msg': str(msg)}
        phase = getattr(self._phase, 'title', None) or self.__dict__.get('_current_phase')
        if phase:
            record['phase'] = phase
        record.update(fields)
        with self._lock:
            common.append_jsonl(self.dir / 'log.jsonl', record)
        if event in ('log', 'phase_start', 'phase_end', 'refused', 'call_failed'):
            print('[flow %s] %s' % (self.name, msg), file=sys.stderr, flush=True)

    def usage(self):
        """Tokens used by this run's real calls plus call counts."""
        with self._lock:
            out = dict(self._usage)
            out['billable_tokens'] = common.billable(self._usage)
            out['calls'] = dict(self._counts)
            return out

    def budget_remaining(self):
        if self.budget_tokens is None:
            return None
        with self._lock:
            return self.budget_tokens - common.billable(self._usage)

    @contextlib.contextmanager
    def phase(self, title):
        if title not in self.phases:
            raise ValueError('unknown phase %r; declared phases: %s' % (title, self.phases))
        entry = self.meta['phases'][self.phases.index(title)]
        started = time.time()
        with self._lock:
            entry.update(status='running', started=started)
            self._save_meta()
        previous = self.__dict__.get('_current_phase')
        self._current_phase = title
        self.log('phase %s started' % title, event='phase_start')
        status = 'done'
        try:
            yield self
        except BaseException:
            status = 'failed'
            raise
        finally:
            self._current_phase = previous
            with self._lock:
                entry.update(status=status, ended=time.time())
                self._save_meta()
            self.log('phase %s %s (%.1fs)' % (title, status, time.time() - started), event='phase_end',
                     phase=title)

    def done(self, result=None):
        path = self.dir / 'result.json'
        common.write_json(path, result)
        with self._lock:
            self.meta.update(status='done', ended=time.time(), result_path=str(path))
            self._save_meta()
        self.log('run done', event='done')
        print('flow run %s done: %s' % (self.run_id, path))
        return path

    # ------------------------------------------------------------ concurrency

    def pipeline(self, items, fn):
        """`fn(item)` for each item, at most `concurrency` at once; results in item order."""
        items = list(items)
        results = [None] * len(items)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self._guard, fn, item): i for i, item in enumerate(items)}
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def parallel(self, fns):
        """Run thunks concurrently; results in order (model calls still share the slot limit)."""
        fns = list(fns)
        if not fns:
            return []
        results = [None] * len(fns)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(fns)) as pool:
            futures = {pool.submit(self._guard, fn): i for i, fn in enumerate(fns)}
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def _guard(self, fn, *args):
        phase = self.__dict__.get('_current_phase')
        self._phase.title = phase
        try:
            return fn(*args)
        except Exception as error:  # noqa: BLE001 - a failed item becomes None
            self.log('task raised %s: %s' % (type(error).__name__, error), event='call_failed')
            return None
        finally:
            self._phase.title = None

    # ------------------------------------------------------------ agent

    def _role(self, agent_type):
        with self._lock:
            if agent_type not in self._roles:
                self._roles[agent_type] = common.find_role(agent_type, self.workdir)
            return self._roles[agent_type]

    def _endpoint(self):
        with self._lock:
            if self._codex is None:
                self._codex = common.codex_bin()
                self._base_url = common.native_base_url(self._codex)
            return self._codex, self._base_url

    def agent(self, prompt, agent_type=None, schema=None, label=None, model=None, effort=None, sandbox=None,
              cwd=None, timeout=1800):
        """One `codex exec`. Returns parsed JSON (with `schema`) or the final text; None on failure."""
        if agent_type is not None and (model or effort or sandbox):
            raise ValueError('model/effort/sandbox cannot be overridden for agent_type=%r; '
                             'the role file is the source of truth' % agent_type)
        if sandbox is not None and sandbox not in common.SANDBOXES:
            raise ValueError('sandbox must be one of %s' % (common.SANDBOXES,))
        if schema is not None and not isinstance(schema, dict):
            raise ValueError('schema must be a dict')
        instructions = ''
        if agent_type is not None:
            try:
                role = self._role(agent_type)
            except common.UsageError as error:
                raise ValueError(str(error)) from None
            pins = common.role_pins(role)
            model, effort, sandbox = pins['model'], pins['effort'], pins['sandbox']
            instructions = (role.get('developer_instructions') or '').strip()
        sandbox = sandbox or 'read-only'
        label = label or (prompt.strip().splitlines() or [''])[0][:60]
        full_prompt = (instructions + '\n\n# Task\n\n' + prompt) if instructions else prompt
        key = call_key(prompt, agent_type, schema, model, effort)
        record = {'key': key, 'label': label, 'agent_type': agent_type, 'model': model, 'effort': effort,
                  'sandbox': sandbox, 'phase': getattr(self._phase, 'title', None)
                  or self.__dict__.get('_current_phase')}

        with self._lock:
            if self._counts['total'] >= self.max_calls:
                self._counts['refused'] += 1
                refuse = 'call cap %d reached' % self.max_calls
            else:
                self._counts['total'] += 1
                refuse = None
        if refuse:
            self.log('%s: refused (%s)' % (label, refuse), event='refused', label=label)
            return None

        cached = self._cached(key)
        if cached is not None:
            record.update(status='cached', result=cached['result'], usage=cached.get('usage') or {},
                          cached_from=self.resume_from, started=time.time(), ended=time.time())
            common.write_json(self.dir / 'calls' / (key + '.json'), record)
            with self._lock:
                self._counts['cached'] += 1
                self._save_meta()
            self.log('%s: cached' % label, event='call', label=label, status='cached', key=key)
            return cached['result']

        with self._slots:
            remaining = self.budget_remaining()
            if remaining is not None and remaining <= 0:
                with self._lock:
                    self._counts['refused'] += 1
                    self._save_meta()
                self.log('%s: refused (budget exhausted, remaining %d)' % (label, remaining), event='refused',
                         label=label)
                return None
            result, outcome = self._run(full_prompt, schema, model, effort, sandbox, cwd, timeout)

        record.update(outcome)
        record['result'] = result
        ok = outcome['status'] == 'ok'
        common.write_json(self.dir / 'calls' / (key + '.json'), record)
        with self._lock:
            common.add_usage(self._usage, outcome.get('usage'))
            self._counts['ok' if ok else 'failed'] += 1
            self._save_meta()
        if ok:
            self.log('%s: ok' % label, event='call', label=label, status='ok', key=key,
                     usage=outcome.get('usage'))
        else:
            self.log('%s: failed (%s)' % (label, outcome.get('reason')), event='call_failed', label=label,
                     status='failed', key=key, reason=outcome.get('reason'))
        return result if ok else None

    def _cached(self, key):
        if not self.resume_from:
            return None
        data = common.read_json(self.root / self.resume_from / 'calls' / (key + '.json'))
        if isinstance(data, dict) and data.get('status') in ('ok', 'cached') and data.get('result') is not None:
            return data
        return None

    def _run(self, prompt, schema, model, effort, sandbox, cwd, timeout):
        started = time.time()
        workdir = Path(cwd).resolve() if cwd else self.workdir
        try:
            codex, base_url = self._endpoint()
        except common.UsageError as error:
            return None, {'status': 'failed', 'reason': str(error), 'started': started, 'ended': time.time()}
        with tempfile.TemporaryDirectory(prefix='als-flow-') as tmp:
            last_file = Path(tmp) / 'last.txt'
            schema_file = None
            if schema is not None:
                schema_file = Path(tmp) / 'schema.json'
                schema_file.write_text(json.dumps(schema), encoding='utf-8')
            argv = common.exec_argv(codex, base_url, model, effort, sandbox, workdir, last_file, schema_file)
            res = common.run_codex(argv, prompt, workdir, timeout)
        outcome = {'started': started, 'ended': time.time(), 'usage': res['usage'], 'exit_code': res['exit_code'],
                   'cwd': str(workdir)}
        if res['error']:
            outcome.update(status='failed', reason=res['error'])
            return None, outcome
        reply = res['reply']
        if schema is None:
            outcome['status'] = 'ok'
            return reply, outcome
        try:
            value = json.loads(reply)
        except ValueError:
            outcome.update(status='failed', reason='reply is not valid JSON', reply=reply[:2000])
            return None, outcome
        if not check_schema(value, schema):
            outcome.update(status='failed', reason='reply misses required keys %s' % schema.get('required'),
                           reply=reply[:2000])
            return None, outcome
        outcome['status'] = 'ok'
        return value, outcome
