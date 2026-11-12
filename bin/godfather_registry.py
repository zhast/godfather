"""Import JSON registries into the store and mirror writes for one release."""
import json
import fcntl
import os
from pathlib import Path
import time
import uuid

import godfather_store as store

# Legacy run-file words that are not states. The machine never stores them.
STATUS_MAP = {
    'killed': 'cancelled',
    'quota': 'fallback',
    'launching': 'provisioning',
    'replaced': 'cancelled',
    'healing': 'running',
    'died': 'failed',
}
WARNINGS = []


def import_registries(conn, root):
    """Idempotent load of run files, queue, events, incidents, ledger and quota."""
    root = Path(root)
    warnings = []
    if root.is_dir():
        for path in sorted(root.glob('*.run.json')):
            _import_run_file(conn, path, warnings)
        for path in sorted(root.glob('*.route.json')):
            data = _read_object(path, warnings)
            if data is not None:
                _save_meta(conn, 'route:' + (data.get('name') or path.name[:-11]), data)
        for path in sorted(root.glob('*.events.jsonl')):
            _import_events(conn, path, warnings)
    state = _state_dir()
    queue = state / 'queue'
    if queue.is_dir():
        for path in sorted(queue.glob('*.json')):
            data = _read_object(path, warnings)
            if data is not None and data.get('name'):
                _mirror_queue(conn, data)
    _import_jsonl(conn, state / 'incidents.jsonl', _mirror_incident, warnings)
    _import_jsonl(conn, state / 'ledger.jsonl', _mirror_ledger, warnings)
    quota = _read_object(state / 'quota.json', warnings) if (state / 'quota.json').exists() else None
    if isinstance(quota, dict):
        _mirror_quota(conn, quota)
    if warnings:
        prior = []
        row = conn.execute("SELECT value FROM meta WHERE key='import_warnings'").fetchone()
        if row:
            try:
                prior = json.loads(row['value'])
            except ValueError:
                prior = []
        conn.execute(
            'INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('import_warnings', json.dumps(prior + warnings)))
    WARNINGS[:] = warnings
    return warnings


def note_write(path, data):
    path = Path(path)
    if not isinstance(data, dict):
        return
    name = path.name
    if name.endswith('.run.json'):
        _with_run(path.parent, lambda conn: _mirror_run(conn, data, path))
    elif name.endswith('.route.json'):
        _with_run(path.parent, lambda conn: _save_meta(conn, 'route:' + (data.get('name') or path.name[:-11]), data))
    elif name.endswith('.control.json'):
        _with_run(path.parent, lambda conn: _mirror_control(conn, path, data))
    elif name == 'quota.json':
        _with_run(None, lambda conn: _mirror_quota(conn, data))
    elif path.parent.name == 'queue' and name.endswith('.json'):
        _with_run(None, lambda conn: _mirror_queue(conn, data))


def note_event(record, runs=None):
    root = Path(runs) if runs else None
    conn = store.connect(root)
    try:
        def run():
            run_id = _run_id(conn, record.get('name'))
            return store._event(conn, run_id, record.get('kind') or 'EVENT', record.get('detail') or '',
                                name=record.get('name'), at=record.get('at'), legacy_id=record.get('id'))
        return store._lock(conn, run)
    finally:
        conn.close()


def note_incident(record, runs=None):
    _with_run(runs, lambda conn: _mirror_incident(conn, record))


def note_ledger(record, runs=None):
    _with_run(runs, lambda conn: _mirror_ledger(conn, record))


def _with_run(root, fn):
    conn = store.connect(root)
    try:
        return store._lock(conn, lambda: fn(conn))
    finally:
        conn.close()


def _read_object(path, warnings):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        warnings.append('%s: truncated or invalid JSON' % path)
        return None
    if not isinstance(data, dict):
        warnings.append('%s: expected an object' % path)
        return None
    return data


def _import_events(conn, path, warnings):
    for record in _lines(path, warnings):
        if not isinstance(record, dict) or 'kind' not in record:
            warnings.append('%s: truncated or invalid JSON' % path)
            continue
        run_id = _run_id(conn, record.get('name'))
        store._event(conn, run_id, record.get('kind') or 'EVENT', record.get('detail') or '',
                     name=record.get('name'), at=record.get('at'), legacy_id=record.get('id') or None)


def _lines(path, warnings):
    try:
        text = Path(path).read_text(errors='replace')
    except FileNotFoundError:
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            warnings.append('%s: truncated or invalid JSON' % path)


def _import_jsonl(conn, path, writer, warnings):
    for record in _lines(path, warnings):
        if isinstance(record, dict):
            writer(conn, record)
        else:
            warnings.append('%s: truncated or invalid JSON' % path)


def _import_run_file(conn, path, warnings):
    data = _read_object(path, warnings)
    if not data or not data.get('name'):
        return
    if _abandoned(data, path.parent):
        base = str(path)[:-9]
        control = _read_object(base + '.control.json', warnings) or {}
        questions = control.get('questions', data.get('questions', []))
        updated = exited_record(data, base, questions)
        if updated is not None:
            data = updated
            # Import already owns the store transaction; publish JSON without
            # recursively opening another writer through note_write.
            for dest, payload in ((path, data), (Path(base + '.control.json'), dict(control, questions=data['questions']))):
                temp = dest.with_name(dest.name + '.' + uuid.uuid4().hex + '.tmp')
                temp.write_text(json.dumps(payload, indent=2) + '\n')
                os.replace(temp, dest)
    _mirror_run(conn, data, path, reason='import')


def expire_terminal_questions(path, procs=None):
    """Expire decisions only after a terminal run's last verified owner exits.

    Use the same control lock as asks and registry writes, and reread under it
    so a replacement generation or concurrent answer cannot be overwritten.
    """
    from agent_oversight import owners_alive
    path = Path(path)
    base = str(path)[:-9]
    with open(base + '.control.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read_object(path, [])
        if not data or _map_status(data)[0] not in store.TERMINAL:
            return None
        control_path = Path(base + '.control.json')
        control = _read_object(control_path, []) or {}
        questions = control.get('questions', data.get('questions', []))
        pending = [q for q in questions if q.get('waiting') or q.get('answer') is None
                   or not q.get('delivered', True)]
        if not pending or owners_alive(data, procs=procs, root=path.parent):
            return None
        stamp = time.time()
        updated = []
        for q in questions:
            if q in pending:
                was_pending = q.get('answer') is None or not q.get('delivered', True)
                q = dict(q, waiting=False, answer='expired' if q.get('answer') is None else q['answer'],
                         delivered=True, reason='expired: run ended')
                if was_pending:
                    q['expired_at'] = stamp
            updated.append(q)
        questions = updated
        data.update(questions=questions, pending_decision=False)
        control.update(questions=questions, pending_decision=False)
        for dest, payload in ((control_path, control), (path, data)):
            temp = dest.with_name(dest.name + '.' + uuid.uuid4().hex + '.tmp')
            temp.write_text(json.dumps(payload, indent=2) + '\n')
            os.replace(temp, dest)
        note_write(path, data)
        return data


def expire_ended_asks(root):
    """Idempotent repair for registries imported before terminal ask expiry."""
    from agent_oversight import processes
    procs = processes()
    return sum(expire_terminal_questions(path, procs=procs) is not None
               for path in sorted(Path(root).glob('*.run.json')))


def completion_failure(base, questions):
    """An exit code alone does not certify that the requested work finished."""
    if any(q.get('blocking') and (q.get('answer') is None or not q.get('delivered', True) or q.get('expired_at')) for q in questions):
        return 'pending_question', 'agent exited with a blocking question pending'
    try:
        first = Path(str(base) + '.last.md').read_text(errors='replace').splitlines()[0].lstrip()
    except (OSError, IndexError):
        first = ''
    import re
    if re.match(r'^(?:INCOMPLETE|BLOCKED|FAILED)\b', first):
        return 'incomplete', first[:300]
    return None, None


def exited_record(data, base, questions):
    """Resolve dead blocking asks without manufacturing an approval."""
    pending = [q for q in questions if q.get('answer') is None or not q.get('delivered', True)]
    rc = data.get('exit_code') if data.get('exit_code') is not None else data.get('child_exit_code')
    if rc == 0 and any(not q.get('blocking') for q in pending):
        return None  # An intentionally exited nonblocking ask remains answerable.
    failure, reason = completion_failure(base, questions)
    stamp = time.time()
    questions = [dict(q, answer=q.get('answer') or 'expired', delivered=True, waiting=False,
                      reason='expired: run ended', expired_at=stamp) if q in pending else q for q in questions]
    report = Path(str(base) + '.last.md')
    status = 'done' if rc == 0 and not failure and report.is_file() and report.stat().st_size else 'failed'
    return dict(data, status=status, questions=questions, pending_decision=False,
                exit_code=0 if status == 'done' else (rc or 1),
                ended_at=data.get('ended_at') or stamp, failure_class=None if status == 'done' else failure or 'crash',
                failure_reason=reason)


def _abandoned(data, root=None):
    """A non-terminal registry whose child and launcher are both gone."""
    mapped, _ = _map_status(data)
    if mapped in store.TERMINAL or mapped == 'waiting_quota':
        return False
    pid = data.get('pid')
    if not isinstance(pid, int) or pid <= 0:
        return False
    import agent_oversight as oversight
    if oversight.owners_alive(data, root=root):
        return False
    launcher = data.get('launcher_pid')
    if isinstance(launcher, int) and launcher > 0 and _alive(launcher, data.get('launcher_identity')):
        return False
    return True


def _alive(pid, identity):
    try:
        import agent_oversight as oversight
        return oversight.alive(pid, identity)
    except (OSError, TypeError, ValueError):
        return False


def _map_status(data):
    raw = data.get('status') or 'failed'
    if raw in ('running', 'needs_input', 'checking', 'reviewing', 'retrying'):
        if _pending(data):
            return 'needs_input', data.get('failure_class')
        if raw == 'needs_input' and not data.get('ended_at'):
            raw = 'running'
    if raw == 'quota':
        return 'fallback', data.get('failure_class') or 'quota'
    mapped = STATUS_MAP.get(raw, raw)
    if mapped not in store.RUN_STATUSES:
        raise store.IllegalTransition('run', raw, mapped)
    return mapped, data.get('failure_class')


def _mirror_run(conn, data, path, reason='registry'):
    name = data.get('name') or Path(path).stem.replace('.run', '')
    if data.get('status') == 'done':
        failure, detail = completion_failure(str(path)[:-9], data.get('questions', []))
        if failure:
            data = dict(data, status='failed', failure_class=failure, failure_reason=detail, exit_code=1)
    status, failure = _map_status(data)
    run_id = data.get('run_id') or 'legacy:%s:%s' % (name, data.get('started_at') or '')
    payload = json.dumps(data)
    now = time.time()
    existing = conn.execute('SELECT id FROM runs WHERE id=?', (run_id,)).fetchone()
    if existing is None:
        store._retire_live(conn, name, run_id)
        generation = (conn.execute('SELECT COALESCE(MAX(generation), 0) FROM runs WHERE name=?', (name,)).fetchone()[0] or 0) + 1
        conn.execute(
            '''INSERT INTO runs(id, name, generation, status, pending_decision, cwd, prompt, isolate, container,
               task_type, artifact_dir, created_at, updated_at, started_at, ended_at, failure_class, exit_code,
               error, payload, check_cmd, review_by)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (run_id, name, generation, status, _pending(data), data.get('cwd') or '', _prompt(data),
             1 if data.get('isolate') else 0, 0, data.get('task_type') or 'edit', str(Path(path).parent),
             now, now, store.epoch(data.get('started_at')) or now, store.epoch(data.get('ended_at')),
             failure, data.get('exit_code'), _error(data), payload, data.get('check_cmd') or data.get('check'),
             data.get('review_by')))
        conn.execute(
            'INSERT INTO transitions(run_id, at, from_status, to_status, reason) VALUES(?,?,?,?,?)',
            (run_id, now, '(new)', status, reason))
    else:
        store._retire_live(conn, name, run_id)
        store.adopt(conn, run_id, status, reason)
        conn.execute(
            '''UPDATE runs SET payload=?, cwd=?, prompt=?, updated_at=?, started_at=COALESCE(?, started_at),
               ended_at=?, failure_class=?, exit_code=?, error=?, pending_decision=?, check_cmd=?, review_by=?,
               task_type=? WHERE id=?''',
            (payload, data.get('cwd') or '', _prompt(data), now, store.epoch(data.get('started_at')),
             store.epoch(data.get('ended_at')), failure, data.get('exit_code'), _error(data), _pending(data),
             data.get('check_cmd') or data.get('check'), data.get('review_by'), data.get('task_type') or 'edit', run_id))
    _mirror_segment(conn, run_id, data, status, failure)
    _mirror_decisions(conn, run_id, data)
    return run_id


def _mirror_segment(conn, run_id, data, run_status, failure):
    segment_id = data.get('segment_id') or run_id + ':1'
    target = _segment_status(run_status, data, failure)
    row = conn.execute('SELECT id, status FROM segments WHERE id=?', (segment_id,)).fetchone()
    # A finished segment stays finished. Review and reopen start a new segment row.
    if row is not None and row['status'] != target and _segment_path(row['status'], target) is None:
        segment_id = segment_id + ':' + target
        row = conn.execute('SELECT id, status FROM segments WHERE id=?', (segment_id,)).fetchone()
    now = time.time()
    ordinal = _free_ordinal(conn, run_id, _ordinal(data.get('name') or ''))
    pid = data.get('pid') if isinstance(data.get('pid'), int) else None
    if row is None:
        conn.execute(
            '''INSERT INTO segments(id, run_id, ordinal, cli, model, effort, status, session_id, pid, pid_identity,
               launcher_pid, supervisor_pid, failure_class, exit_code, started_at, ended_at, tokens_in, tokens_cached, tokens_out, permission_profile)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (segment_id, run_id, ordinal, data.get('cli') or 'unknown', data.get('model') or '?',
             data.get('effort') or 'high', target, data.get('session_id') or data.get('conversation_id'), pid,
             data.get('pid_identity'), data.get('launcher_pid') if isinstance(data.get('launcher_pid'), int) else None,
             data.get('supervisor_pid') if isinstance(data.get('supervisor_pid'), int) else None, failure,
             data.get('exit_code'), store.epoch(data.get('segment_started_at') or data.get('started_at')) or now,
             store.epoch(data.get('ended_at')), data.get('tokens_in'), data.get('tokens_cached'), data.get('tokens_out'),
             data.get('permission_profile')))
        return
    current = row['status']
    if current != target:
        for step in _segment_path(current, target):
            store._step(conn, 'segments', 'segment', segment_id, step, 'registry', store.SEGMENT_EDGES, segment_id)
    # A registry refresh often has no failure class and no token totals yet.
    # COALESCE keeps the stream ingestor's heartbeat values instead of erasing them.
    conn.execute(
        '''UPDATE segments SET pid=?, pid_identity=?, launcher_pid=?, supervisor_pid=?, exit_code=?, ended_at=?,
           failure_class=COALESCE(?, failure_class), tokens_in=COALESCE(?, tokens_in),
           tokens_cached=COALESCE(?, tokens_cached), tokens_out=COALESCE(?, tokens_out),
           session_id=COALESCE(?, session_id), permission_profile=COALESCE(?, permission_profile) WHERE id=?''',
        (pid, data.get('pid_identity'), data.get('launcher_pid') if isinstance(data.get('launcher_pid'), int) else None,
         data.get('supervisor_pid') if isinstance(data.get('supervisor_pid'), int) else None, data.get('exit_code'),
         store.epoch(data.get('ended_at')), failure, data.get('tokens_in'), data.get('tokens_cached'),
         data.get('tokens_out'), data.get('session_id') or data.get('conversation_id'),
         data.get('permission_profile'), segment_id))


def _segment_status(run_status, data, failure):
    if data.get('exit_code') == 127 and run_status == 'failed':
        return 'spawn_failed'
    if failure == 'broken_stream':
        return 'broken_stream'
    if run_status in store.TERMINAL or run_status == 'fallback':
        return 'exited'
    if run_status in ('checking', 'reviewing', 'needs_input', 'retrying', 'merging'):
        return 'finishing'
    if data.get('pid'):
        return 'streaming'
    return 'starting'


def _segment_path(current, target):
    if current == target:
        return []
    queue = [(current, [])]
    seen = {current}
    while queue:
        node, path = queue.pop(0)
        for nxt in store.SEGMENT_EDGES.get(node, ()):
            step = path + [nxt]
            if nxt == target:
                return step
            if nxt not in seen and len(step) < 4:
                seen.add(nxt)
                queue.append((nxt, step))
    return None


def _mirror_decisions(conn, run_id, data):
    pending = 0
    for question in data.get('questions') or []:
        if not isinstance(question, dict) or not question.get('id'):
            continue
        if question.get('answer') is None or not question.get('delivered', True):
            pending = 1
        effect = question.get('effect')
        conn.execute(
            '''INSERT INTO decisions(id, run_id, question, options, effect_json, blocking, answer, asked_at, answered_at, delivered)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET answer=excluded.answer, answered_at=excluded.answered_at,
                 delivered=excluded.delivered, options=excluded.options, effect_json=excluded.effect_json''',
            (question['id'], run_id, question.get('question') or '', question.get('options'),
             json.dumps(effect) if effect else None, 1 if question.get('blocking') else 0, question.get('answer'),
             question.get('asked_at') or time.time(), question.get('answered_at'), 1 if question.get('delivered') else 0))
    conn.execute('UPDATE runs SET pending_decision=? WHERE id=?', (pending, run_id))


def _mirror_control(conn, path, data):
    run_path = Path(str(path)[:-13] + '.run.json')
    if run_path.exists():
        try:
            run = json.loads(run_path.read_text())
        except (OSError, ValueError):
            run = None
        if isinstance(run, dict):
            run.update(data)
            _mirror_run(conn, run, run_path)


def _mirror_queue(conn, job):
    name = job.get('name')
    if not name:
        return
    _save_meta(conn, 'queue:' + name, job)
    if job.get('kind') == 'quota':
        return  # The existing real run owns the wait; never create a placeholder.
    raw = job.get('status') or 'queued'
    run_id = 'queue:' + name
    if raw == 'running':
        row = conn.execute('SELECT id, status FROM runs WHERE id=?', (run_id,)).fetchone()
        if row and row['status'] not in store.TERMINAL:
            store.adopt(conn, row['id'], 'cancelled', 'queue handed off to a launcher')
        return
    mapped = STATUS_MAP.get(raw, raw if raw in store.RUN_STATUSES else 'failed')
    # A queue placeholder must not cancel the launcher's live row.
    if mapped not in store.TERMINAL and _real_live(conn, name):
        return
    existing = conn.execute('SELECT id FROM runs WHERE id=?', (run_id,)).fetchone()
    payload = json.dumps(dict(job, _godfather_queue=True))
    now = time.time()
    if existing is None:
        if mapped not in store.TERMINAL:
            store._retire_live(conn, name, run_id)
        generation = (conn.execute('SELECT COALESCE(MAX(generation), 0) FROM runs WHERE name=?', (name,)).fetchone()[0] or 0) + 1
        conn.execute(
            '''INSERT INTO runs(id, name, generation, status, pending_decision, cwd, prompt, isolate, container,
               task_type, artifact_dir, created_at, updated_at, payload)
               VALUES(?,?,?,?,0,'','',0,0,'edit',?,?,?,?)''',
            (run_id, name, generation, mapped, str(store.runs_root()), now, now, payload))
        conn.execute('INSERT INTO queue(run_id, deps_json, any_status, env_json, created_at) VALUES(?,?,?,?,?)',
                     (run_id, json.dumps(job.get('dependencies') or []), 1 if job.get('any_status') else 0,
                      json.dumps(job.get('env') or {}), job.get('created') or now))
    else:
        store.adopt(conn, run_id, mapped, 'queue')
        conn.execute('UPDATE runs SET payload=?, updated_at=? WHERE id=?', (payload, now, run_id))
        conn.execute(
            '''INSERT INTO queue(run_id, deps_json, any_status, env_json, created_at) VALUES(?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET deps_json=excluded.deps_json, any_status=excluded.any_status,
                 env_json=excluded.env_json''',
            (run_id, json.dumps(job.get('dependencies') or []), 1 if job.get('any_status') else 0,
             json.dumps(job.get('env') or {}), job.get('created') or now))


def _mirror_incident(conn, record):
    ident = record.get('id') or uuid.uuid4().hex
    if conn.execute('SELECT 1 FROM incidents WHERE id=?', (ident,)).fetchone():
        conn.execute('UPDATE incidents SET answer=?, verdict=? WHERE id=?', (record.get('answer'), record.get('verdict') or 'audit', ident))
        return
    command = record.get('command')
    conn.execute(
        '''INSERT INTO incidents(id, run_id, at, command_json, cwd, targets_json, verdict, answer, question_id, effect, source)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (ident, _run_id(conn, record.get('run')), record.get('time') or time.time(),
         json.dumps(command) if not isinstance(command, str) else command, record.get('cwd'),
         json.dumps(record.get('targets') or []), record.get('verdict') or 'audit', record.get('answer'),
         record.get('question_id'), record.get('effect'), record.get('source') or 'shim-audit'))


def _mirror_ledger(conn, record):
    segment = record.get('segment_id')
    if segment and conn.execute('SELECT 1 FROM ledger WHERE segment_id=?', (segment,)).fetchone():
        return
    conn.execute(
        '''INSERT INTO ledger(run_id, segment_id, cli, model, started_at, ended_at, exit_status, tokens_in,
           tokens_cached, tokens_out, quota_fallback, prices_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
        (_run_id(conn, record.get('run') or record.get('name')), segment, record.get('cli'), record.get('model'),
         store.epoch(record.get('started_at')), store.epoch(record.get('ended_at')), record.get('exit_status'),
         record.get('tokens_in'), record.get('tokens_cached'), record.get('tokens_out'),
         1 if record.get('quota_fallback') else 0, json.dumps(record.get('prices') or {})))


def _mirror_quota(conn, data):
    now = time.time()
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        until = store.epoch(entry.get('until')) or 0
        conn.execute(
            '''INSERT INTO quota(key, until_at, reason, updated_at) VALUES(?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET until_at=excluded.until_at, reason=excluded.reason, updated_at=excluded.updated_at''',
            (key, until, entry.get('reason'), now))


def _save_meta(conn, key, data):
    conn.execute(
        'INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, json.dumps(data)))


def _real_live(conn, name):
    marks = ','.join('?' * len(store.TERMINAL))
    row = conn.execute(
        'SELECT id FROM runs WHERE name=? AND id NOT LIKE ? AND status NOT IN (%s) LIMIT 1' % marks,
        (name, 'queue:%') + store.TERMINAL).fetchone()
    return row is not None


def _run_id(conn, name):
    if not name:
        return None
    marks = ','.join('?' * len(store.TERMINAL))
    row = conn.execute(
        'SELECT id FROM runs WHERE name=? AND status NOT IN (%s) ORDER BY generation DESC LIMIT 1' % marks,
        (name,) + store.TERMINAL).fetchone()
    if row:
        return row['id']
    row = conn.execute('SELECT id FROM runs WHERE name=? ORDER BY generation DESC LIMIT 1', (name,)).fetchone()
    return row['id'] if row else None


def _pending(data):
    return int(any(isinstance(q, dict) and (q.get('answer') is None or not q.get('delivered', True))
                   for q in data.get('questions') or []))


def _error(data):
    errors = data.get('errors') or []
    if isinstance(errors, list):
        return '\n'.join(str(item) for item in errors) or None
    return str(errors)


def _prompt(data):
    path = data.get('prompt_file')
    if path:
        try:
            return Path(path).read_text()[:100000]
        except OSError:
            return ''
    return data.get('prompt') or ''


def _ordinal(name):
    marker = name.rfind('#')
    if marker == -1:
        return 1
    tail = name[marker + 1:]
    return int(tail) if tail.isdigit() else 1


def _free_ordinal(conn, run_id, ordinal):
    taken = {row['ordinal'] for row in conn.execute('SELECT ordinal FROM segments WHERE run_id=?', (run_id,))}
    if ordinal not in taken:
        return ordinal
    return (max(taken) if taken else 0) + 1


def _state_dir():
    return Path(os.getenv('XDG_STATE_HOME', str(Path.home() / '.local/state'))).expanduser() / 'godfather'
