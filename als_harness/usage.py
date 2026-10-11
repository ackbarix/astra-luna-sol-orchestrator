"""Incremental response-ledger input. Reuses ALS 0.2 normalization, not billing."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
from .common import HarnessError, digest, encoded, now

NORMALIZER = None


def normalizer():
    global NORMALIZER
    if NORMALIZER is None:
        root = Path(__file__).resolve().parents[1]
        path = root / 'token_usage.py' if (root / 'token_usage.py').exists() else root / 'scripts/token_usage.py'
        spec = importlib.util.spec_from_file_location('als_usage_normalizer', path)
        NORMALIZER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(NORMALIZER)
    return NORMALIZER


MAX_LINE = 1024 * 1024
TICK_BYTES = 1024 * 1024
PRE_TOOL_SYNC_BYTES = 256 * 1024  # PreToolUse runs on every spawn; keep it cheap
HEAD_BYTES = 4096
SKIP_CHUNK = 64 * 1024
TAIL_CONTEXT_BYTES = 1024 * 1024  # where an EOF anchor looks for the latest turn_context


def sessions_base():
    """Resolved CODEX_HOME/sessions for this process; recorded at registration time."""
    return (Path(os.environ.get('CODEX_HOME') or '~/.codex').expanduser() / 'sessions').resolve()


def known_threads(store, run):
    threads = {run.get('session')} | {a.get('thread_id') for t in store.tasks(run['id']) for a in t['attempts']}
    threads.discard(None)
    return threads


def lineage(data):
    """Only thread identity from session_meta; never instructions, cwd or git payloads."""
    parent = data.get('parent_thread_id')
    source = data.get('source')
    sub = source.get('subagent') if isinstance(source, dict) else None
    spawn = sub.get('thread_spawn') if isinstance(sub, dict) else None
    if not parent and isinstance(spawn, dict):
        parent = spawn.get('parent_thread_id')
    thread = data.get('id')
    return {'id': thread if isinstance(thread, str) else None,
            'parent_thread_id': parent if isinstance(parent, str) else None}


def head_digest(f):
    """Digest of the first line (capped at 4 KiB) so truncate-and-regrow is detected."""
    f.seek(0)
    head = f.read(HEAD_BYTES)
    cut = head.find(b'\n')
    if cut >= 0:
        return digest(head[:cut + 1])
    return digest(head) if len(head) == HEAD_BYTES else None


def skip_line(f):
    """Discard through the next newline; returns (bytes skipped, newline found)."""
    skipped = 0
    while True:
        chunk = f.readline(SKIP_CHUNK)
        if not chunk:
            return skipped, False
        skipped += len(chunk)
        if chunk.endswith(b'\n'):
            return skipped, True


def checked_rollout(path, base=None):
    base = Path(base).resolve() if base else sessions_base()
    p = Path(path).expanduser()
    if not p.is_absolute() or base not in p.resolve().parents or p.is_symlink() or p.suffix != '.jsonl':
        raise HarnessError('Usage path must be a JSONL rollout beneath the registered CODEX_HOME/sessions')
    if any(x.is_symlink() for x in p.parents if x != base and base in x.parents):
        raise HarnessError('Symlinked rollout parent')
    return p


def cursor_key(path, rid):
    return str(Path(path).expanduser()) + '#als-run=' + rid


def last_line_end(f, size):
    """Offset just past the last complete line (0 when there is none)."""
    end = size
    while end > 0:
        start = max(0, end - SKIP_CHUNK)
        f.seek(start)
        chunk = f.read(end - start)
        cut = chunk.rfind(b'\n')
        if cut >= 0:
            return start + cut + 1
        end = start
    return 0


def eof_cursor(path, base=None):
    """Cursor for `path` at its last complete line, so a run never spends its
    sync budget on session history written before it existed.

    Keeps what later records need: the session_meta lineage (first line)
    and the most recent turn_context found in the last TAIL_CONTEXT_BYTES.
    Raises OSError/HarnessError for a missing or unacceptable file.
    """
    p = checked_rollout(path, base)
    with p.open('rb') as f:
        stat = os.fstat(f.fileno())
        cursor = {'file_key': [stat.st_dev, stat.st_ino, head_digest(f)], 'offset': 0, 'context': {},
                  'contexts': {}, 'meta': None, 'anchored_at_eof': True}
        offset = last_line_end(f, stat.st_size)
        cursor['offset'] = offset
        f.seek(0)
        first = f.readline(MAX_LINE + 1)
        try:
            o = json.loads(first) if first.endswith(b'\n') else None
        except (ValueError, UnicodeError):
            o = None
        if isinstance(o, dict) and o.get('type') == 'session_meta' and isinstance(o.get('payload'), dict):
            cursor['meta'] = lineage(o['payload'])
        start = max(len(first), offset - TAIL_CONTEXT_BYTES)
        f.seek(start)
        if start > len(first):
            skip_line(f)
        while f.tell() < offset:
            raw = f.readline(MAX_LINE + 1)
            if not raw or not raw.endswith(b'\n'):
                if raw and len(raw) > MAX_LINE:
                    skip_line(f)
                    continue
                break
            if b'turn_context' not in raw:
                continue
            try:
                o = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            data = o.get('payload') if isinstance(o, dict) else None
            if isinstance(data, dict) and o.get('type') == 'turn_context':
                context = {'model': data.get('model', 'unknown'),
                           'effort': data.get('effort', data.get('reasoning_effort', 'unknown'))}
                cursor['context'] = context
                if data.get('turn_id'):
                    cursor['contexts'][data['turn_id']] = context
                    if len(cursor['contexts']) > 100:
                        cursor['contexts'].pop(next(iter(cursor['contexts'])))
    return cursor


def session_sources(store, session):
    """Transcripts registered for a session with their recorded sessions base."""
    receipt = (store.get('sessions', session) if session else None) or {}
    bases = receipt.get('transcript_bases') or {}
    return {p: bases.get(p) for p in receipt.get('transcripts', [])}


def eof_anchors(store, session):
    """[(path, cursor)] for every readable transcript of a session. Call
    BEFORE the transaction that creates/rebinds the run, so lines written
    after the anchor are still read (records older than the run are
    excluded by timestamp)."""
    anchors = []
    for path, base in sorted(session_sources(store, session).items()):
        try:
            anchors.append((path, eof_cursor(path, base)))
        except (OSError, HarnessError):
            continue  # unreadable now: sync reports it later
    return anchors


def store_anchors(store, rid, anchors):
    """Insert anchors for a run inside the caller's transaction; a cursor that
    already exists for this run is never moved."""
    for path, cursor in anchors:
        store.db.execute('INSERT OR IGNORE INTO cursors VALUES(?,?)', (cursor_key(path, rid), encoded(cursor)))


def sync_path(store, run, path, base=None, limit=TICK_BYTES):
    """Only registered, readable rollouts under CODEX_HOME/sessions; at most `limit` bytes per tick.

    `base` is the sessions directory recorded when the transcript was registered;
    the current process environment is only a fallback for legacy registrations.
    """
    p = checked_rollout(path, base)
    key_name = cursor_key(p, run['id'])
    old = store.get('cursors', key_name, 'path') or {}
    consumed = 0
    events = []
    notes = []
    known = known_threads(store, run)
    if run.get('status') in {'completed', 'cancelled'} and not run.get('closed_at'):
        notes.append('legacy run has no terminal timestamp; post-run usage boundary unverified')
    with p.open('rb') as f:
        stat = os.fstat(f.fileno())
        key = [stat.st_dev, stat.st_ino, head_digest(f)]
        if old.get('file_key') != key or stat.st_size < old.get('offset', 0):
            cursor = {'file_key': key, 'offset': 0, 'context': {}, 'contexts': {}, 'meta': None}
        else:
            cursor = old
        f.seek(cursor['offset'])
        if cursor.get('skipping'):
            skipped, found = skip_line(f)
            consumed += skipped
            cursor['offset'] = f.tell()
            if found:
                cursor.pop('skipping')
        while consumed < limit and not cursor.get('skipping'):
            raw = f.readline(MAX_LINE + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE:
                notes.append('oversized rollout line skipped')
                skipped, found = (0, True) if raw.endswith(b'\n') else skip_line(f)
                consumed += len(raw) + skipped
                cursor['offset'] = f.tell()
                if not found:
                    cursor['skipping'] = True
                continue
            if not raw.endswith(b'\n'):
                break
            try:
                o = json.loads(raw)
            except (ValueError, UnicodeError):
                o = None
                notes.append('malformed rollout line')
            data = o.get('payload') if isinstance(o, dict) else None
            kind = o.get('type') if isinstance(o, dict) else None
            thread = (cursor.get('meta') or {}).get('id')
            if isinstance(data, dict) and kind != 'session_meta' and thread and thread not in known:
                known = known_threads(store, run)
                if thread not in known:
                    # Leave the cursor here: a later bind/resume --session can ingest it.
                    notes.append('unbound thread; usage pending until the thread is bound')
                    break
            cursor['offset'] = f.tell()
            consumed += len(raw)
            if not isinstance(data, dict):
                continue
            if kind == 'session_meta':
                cursor['meta'] = lineage(data)
                continue
            if not thread:
                notes.append('rollout record before session_meta; usage excluded')
                continue
            if kind == 'turn_context':
                context = {'model': data.get('model', 'unknown'),
                           'effort': data.get('effort', data.get('reasoning_effort', 'unknown'))}
                cursor['context'] = context
                if data.get('turn_id'):
                    cursor['contexts'][data['turn_id']] = context
                if len(cursor['contexts']) > 100:
                    cursor['contexts'].pop(next(iter(cursor['contexts'])))
            if kind == 'token_usage_record' or (kind == 'event_msg' and data.get('type') == 'token_usage_record'):
                stamp = normalizer().parse_ts(o.get('timestamp'))
                # Only events after this work run began; never inherited session history.
                if not stamp:
                    notes.append('missing usage timestamp; excluded')
                    continue
                if stamp.timestamp() < run['created']:
                    continue
                if run.get('closed_at') and stamp.timestamp() > run['closed_at']:
                    if thread == run.get('session'):
                        continue
                    notes.append('linked child response after run closure; cancellation did not stop live work')
                warnings = []
                usage = normalizer().normalized_usage(data.get('usage'), warnings)
                notes.extend(warnings)
                if usage is None:
                    continue
                context = cursor['contexts'].get(data.get('turn_id'), cursor['context'])
                model = data.get('model') or context.get('model', 'unknown')
                effort = data.get('effort') or data.get('reasoning_effort')
                if not effort:
                    effort = context.get('effort', 'unknown') if model == context.get('model') else 'unknown'
                if not isinstance(model, str) or not isinstance(effort, str):
                    notes.append('invalid identity')
                    continue
                event = {'timestamp': stamp.isoformat(), 'model': model, 'effort': effort, 'usage': usage}
                response = data.get('response_id') or data.get('id')
                if not isinstance(response, str) or not response:
                    response = digest(event)
                    notes.append('no response ID: exact-record deduplication used')
                events.append((thread, response, event))
            elif kind == 'event_msg' and data.get('type') == 'token_count':
                cursor['has_cumulative'] = True
    with store.tx():
        for thread, response, event in events:
            previous = store.db.execute('SELECT doc FROM usage WHERE run=? AND thread=? AND response=?',
                                        (run['id'], thread, response)).fetchone()
            if previous and previous['doc'] != encoded(event):
                notes.append('conflicting response ID; first record retained')
            store.db.execute('INSERT OR IGNORE INTO usage VALUES(?,?,?,?)', (run['id'], thread, response, encoded(event)))
        cursor['synced_at'] = now()
        store.db.execute('INSERT INTO cursors VALUES(?,?) ON CONFLICT(path) DO UPDATE SET doc=excluded.doc',
                         (key_name, encoded(cursor)))
        r = store.run(run['id'])
        r['warnings'] = sorted(set(r['warnings']) | set(notes))[:100]
        has_usage = store.db.execute('SELECT 1 FROM usage WHERE run=? LIMIT 1', (run['id'],)).fetchone()
        if cursor.get('has_cumulative') and not has_usage:
            r['warnings'] = sorted(set(r['warnings']) | {'cumulative-only source: token budget incomplete'})
        r['last_usage_sync'] = now()
        store.save_run(r)
    return {'bytes_read': consumed, 'response_records_seen': len(events), 'warnings': sorted(set(notes))}


def registered_sources(store, run):
    """(path, sessions base recorded at registration or None for legacy rows)."""
    session = (store.get('sessions', run['session']) if run.get('session') else None) or {}
    bases = session.get('transcript_bases') or {}
    sources = {p: bases.get(p) for p in session.get('transcripts', [])}
    for t in store.tasks(run['id']):
        for a in t['attempts']:
            w = (store.get('workers', a['thread_id']) if a.get('thread_id') else None) or {}
            if w.get('transcript'):
                sources[w['transcript']] = w.get('transcript_base')
    return sources


def last_synced(store, rid, path):
    cursor = store.get('cursors', cursor_key(path, rid), 'path') or {}
    stamp = cursor.get('synced_at')
    return stamp if isinstance(stamp, (int, float)) else 0


def sync(engine, rid, limit=None):
    """Incremental sync of every registered rollout. `limit` caps total bytes for this call.

    Sources are served least-recently-synced first, so a busy HEAD transcript
    cannot starve worker rollouts when the per-call budget runs out.
    """
    r = engine.s.run(rid)
    remaining = limit
    results = []
    sources = registered_sources(engine.s, r)
    order = sorted(sources, key=lambda p: (last_synced(engine.s, rid, p), p))
    for p in order:
        base = sources[p]
        if remaining is not None and remaining <= 0:
            break
        try:
            result = sync_path(engine.s, r, p, base, TICK_BYTES if remaining is None else remaining)
            results.append(result)
            if remaining is not None:
                remaining -= result['bytes_read']
        except (OSError, HarnessError) as ex:
            results.append({'warning': str(ex)})
            with engine.s.tx():
                r = engine.s.run(rid)
                r['warnings'] = sorted(set(r['warnings']) | {'registered usage source unreadable'})
                engine.s.save_run(r)
    return results
