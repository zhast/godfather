"""SQLite run store. One database, one writer, explicit state machines.

The default registry lives at
${XDG_STATE_HOME:-~/.local/state}/godfather/godfather.sqlite.
A non-default AGENT_RUNS_DIR keeps its own file beside that registry so
tests and extra registries do not share the user database.
"""
import datetime as dt
import os
from pathlib import Path
import sqlite3
import time


class IllegalTransition(ValueError):
    def __init__(self, kind, current, target):
        super().__init__('illegal %s transition: %s -> %s' % (kind, current, target))
        self.kind = kind
        self.current = current
        self.target = target


class UnknownCursor(ValueError):
    def __init__(self, name):
        super().__init__('unknown cursor: ' + name)
        self.name = name


class SchemaVersion(SystemExit):
    def __init__(self, found):
        super().__init__(2)
        self.found = found


TERMINAL = ('done', 'failed', 'check_failed', 'review_failed', 'review_findings', 'dependency_failed', 'cancelled', 'timed_out', 'stopped')
RUN_STATUSES = ('queued', 'provisioning', 'running', 'needs_input', 'checking', 'reviewing', 'retrying',
                'fallback', 'merging', 'waiting_quota') + TERMINAL
NONTERMINAL = tuple(status for status in RUN_STATUSES if status not in TERMINAL)
# Direct edges only. Reopen is the single edge out of every terminal except dependency_failed.
RUN_EDGES = {
    'queued': ('provisioning', 'dependency_failed', 'cancelled'),
    'provisioning': ('running', 'failed', 'cancelled'),
    'running': ('needs_input', 'checking', 'reviewing', 'merging', 'retrying', 'fallback', 'done', 'failed', 'cancelled', 'timed_out'),
    'needs_input': ('running', 'done', 'failed', 'cancelled', 'timed_out'),
    'checking': ('reviewing', 'merging', 'done', 'retrying', 'check_failed', 'cancelled'),
    'reviewing': ('merging', 'done', 'review_failed', 'review_findings', 'retrying', 'cancelled'),
    'retrying': ('running', 'fallback', 'failed', 'cancelled', 'timed_out'),
    'fallback': ('running', 'failed', 'cancelled'),
    'merging': ('done', 'failed', 'cancelled'),
}
RUN_EDGES['waiting_quota'] = ('retrying', 'running', 'failed', 'cancelled')
for _status in ('running', 'retrying', 'fallback'):
    RUN_EDGES[_status] += ('waiting_quota',)
for _status in RUN_EDGES:
    RUN_EDGES[_status] += ('stopped',)
for _terminal in TERMINAL:
    RUN_EDGES[_terminal] = (('stopped',) if _terminal == 'dependency_failed' else
                            ('running',) if _terminal == 'stopped' else ('running', 'stopped'))

SEGMENT_STATUSES = ('starting', 'streaming', 'finishing', 'exited', 'spawn_failed', 'broken_stream')
SEGMENT_EDGES = {
    'starting': ('streaming', 'finishing', 'exited', 'spawn_failed', 'broken_stream'),
    'streaming': ('finishing', 'exited', 'broken_stream'),
    'finishing': ('exited', 'broken_stream'),
    'exited': (),
    'spawn_failed': (),
    'broken_stream': (),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta ( key TEXT PRIMARY KEY, value TEXT NOT NULL );
CREATE TABLE IF NOT EXISTS plans ( id TEXT PRIMARY KEY, name TEXT NOT NULL, repo TEXT NOT NULL, base_sha TEXT NOT NULL, integration_branch TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL );
CREATE TABLE IF NOT EXISTS runs ( id TEXT PRIMARY KEY, plan_id TEXT REFERENCES plans(id), task_id TEXT, name TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, pending_decision INTEGER NOT NULL DEFAULT 0, cwd TEXT NOT NULL, worktree TEXT, branch TEXT, base_sha TEXT, prompt TEXT NOT NULL, check_cmd TEXT, review_by TEXT, isolate INTEGER NOT NULL, container INTEGER NOT NULL DEFAULT 0, task_type TEXT NOT NULL, failure_class TEXT, exit_code INTEGER, error TEXT, artifact_dir TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL, ended_at REAL, payload TEXT NOT NULL DEFAULT '{}', UNIQUE (name, generation) );
CREATE TABLE IF NOT EXISTS segments ( id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), ordinal INTEGER NOT NULL, cli TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL, status TEXT NOT NULL, session_id TEXT, pid INTEGER, pid_identity TEXT, launcher_pid INTEGER, supervisor_pid INTEGER, failure_class TEXT, exit_code INTEGER, started_at REAL NOT NULL, ended_at REAL, last_heartbeat_at REAL, last_heartbeat_kind TEXT, last_tool_at REAL, tokens_in INTEGER, tokens_cached INTEGER, tokens_out INTEGER, handoff_from TEXT, permission_profile TEXT, UNIQUE (run_id, ordinal) );
CREATE TABLE IF NOT EXISTS events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, segment_id TEXT, at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, name TEXT, legacy_id TEXT UNIQUE );
CREATE TABLE IF NOT EXISTS cursors ( subscriber TEXT PRIMARY KEY, seq INTEGER NOT NULL, updated_at REAL NOT NULL );
CREATE TABLE IF NOT EXISTS watch_leases ( subscriber TEXT PRIMARY KEY REFERENCES cursors(subscriber), pid INTEGER NOT NULL, identity TEXT NOT NULL, acquired_at REAL NOT NULL );
CREATE TABLE IF NOT EXISTS incidents ( id TEXT PRIMARY KEY, run_id TEXT, segment_id TEXT, at REAL NOT NULL, command_json TEXT, cwd TEXT, targets_json TEXT, verdict TEXT NOT NULL, answer TEXT, question_id TEXT, effect TEXT, source TEXT NOT NULL );
CREATE TABLE IF NOT EXISTS queue ( run_id TEXT PRIMARY KEY REFERENCES runs(id), deps_json TEXT NOT NULL, any_status INTEGER NOT NULL, env_json TEXT NOT NULL, created_at REAL NOT NULL );
CREATE TABLE IF NOT EXISTS decisions ( id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), segment_id TEXT, question TEXT NOT NULL, options TEXT, effect_json TEXT, blocking INTEGER NOT NULL, answer TEXT, asked_at REAL NOT NULL, answered_at REAL, delivered INTEGER NOT NULL DEFAULT 0, timeout_at REAL );
CREATE TABLE IF NOT EXISTS transitions ( id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, segment_id TEXT, at REAL NOT NULL, from_status TEXT NOT NULL, to_status TEXT NOT NULL, reason TEXT NOT NULL );
CREATE TABLE IF NOT EXISTS remedies ( run_id TEXT NOT NULL, segment_id TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, attempted_at REAL NOT NULL, result TEXT NOT NULL, PRIMARY KEY (run_id, segment_id, action) );
CREATE TABLE IF NOT EXISTS quota ( key TEXT PRIMARY KEY, until_at REAL NOT NULL, reason TEXT, updated_at REAL NOT NULL );
CREATE TABLE IF NOT EXISTS ledger ( id INTEGER PRIMARY KEY, run_id TEXT, segment_id TEXT, cli TEXT, model TEXT, started_at REAL, ended_at REAL, exit_status INTEGER, tokens_in INTEGER, tokens_cached INTEGER, tokens_out INTEGER, quota_fallback INTEGER NOT NULL DEFAULT 0, prices_json TEXT );
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_live_name ON runs(name) WHERE status NOT IN ( 'done', 'failed', 'check_failed', 'review_failed', 'review_findings', 'dependency_failed', 'cancelled', 'timed_out', 'stopped');
"""

_IMPORTING = set()
SCHEMA_VERSION = 4


def runs_root(explicit=None):
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env = os.getenv('AGENT_RUNS_DIR')
    base = Path(env).expanduser() if env else Path.home() / '.agent-runs'
    return base.resolve()


def db_path(root=None, create_parent=True):
    """Default registry uses the XDG path. Any other runs directory is private."""
    root = runs_root(root)
    override = os.getenv('GODFATHER_DB')
    if override:
        path = Path(override).expanduser()
    elif root != (Path.home() / '.agent-runs').resolve():
        path = root / '.godfather.sqlite'
    else:
        path = Path(os.getenv('XDG_STATE_HOME', str(Path.home() / '.local/state'))).expanduser() / 'godfather' / 'godfather.sqlite'
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect(root=None, migrate=True):
    path = db_path(root)
    conn = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA foreign_keys=ON')
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise SchemaVersion(version)
    if version == 0:
        conn.executescript(SCHEMA)
        conn.execute('PRAGMA user_version = %d' % SCHEMA_VERSION)
        _meta(conn, 'schema_version', str(SCHEMA_VERSION))
    if version == 1:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DROP INDEX IF EXISTS runs_one_live_name')
            conn.execute("CREATE UNIQUE INDEX runs_one_live_name ON runs(name) WHERE status NOT IN (" +
                         ','.join("'" + status + "'" for status in TERMINAL) + ')')
            conn.execute('PRAGMA user_version = 2')
            _meta(conn, 'schema_version', '2')
    if version in (1, 2):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('CREATE TABLE IF NOT EXISTS watch_leases ( subscriber TEXT PRIMARY KEY REFERENCES cursors(subscriber), pid INTEGER NOT NULL, identity TEXT NOT NULL, acquired_at REAL NOT NULL );')
            conn.execute('PRAGMA user_version = 3')
            _meta(conn, 'schema_version', '3')
    if version in (1, 2, 3):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DROP INDEX IF EXISTS runs_one_live_name')
            conn.execute("CREATE UNIQUE INDEX runs_one_live_name ON runs(name) WHERE status NOT IN (" +
                         ','.join("'" + status + "'" for status in TERMINAL) + ')')
            conn.execute('PRAGMA user_version = 4')
            _meta(conn, 'schema_version', '4')
    if migrate and _meta_get(conn, 'imported') != '1' and str(path) not in _IMPORTING:
        _IMPORTING.add(str(path))
        try:
            from godfather_registry import import_registries
            import_registries(conn, runs_root(root))
            _meta(conn, 'imported', '1')
        finally:
            _IMPORTING.discard(str(path))
    return conn


def _meta(conn, key, value):
    conn.execute('INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def _meta_get(conn, key):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row else None


def meta_get(root, key):
    conn = connect(root)
    try:
        return _meta_get(conn, key)
    finally:
        conn.close()


def meta_set(root, key, value):
    conn = connect(root)
    try:
        _lock(conn, lambda: _meta(conn, key, value))
    finally:
        conn.close()


def epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def _lock(conn, fn):
    for attempt in range(5):
        try:
            conn.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
            continue
        try:
            result = fn()
            conn.execute('COMMIT')
            return result
        except Exception:
            conn.execute('ROLLBACK')
            raise
    raise sqlite3.OperationalError('database is locked')


def _event(conn, run_id, kind, detail, name=None, segment_id=None, at=None, legacy_id=None):
    if legacy_id:
        found = conn.execute('SELECT seq FROM events WHERE legacy_id=?', (legacy_id,)).fetchone()
        if found:
            return found['seq']
    cur = conn.execute(
        'INSERT INTO events(run_id, segment_id, at, kind, detail, name, legacy_id) VALUES(?,?,?,?,?,?,?)',
        (run_id, segment_id, time.time() if at is None else at, kind, detail, name, legacy_id))
    return cur.lastrowid


def _step(conn, table, kind, row_id, target, reason, edges, segment_id=None):
    row = conn.execute('SELECT status FROM %s WHERE id=?' % table, (row_id,)).fetchone()
    if row is None:
        raise KeyError(row_id)
    current = row['status']
    if target not in edges.get(current, ()):
        raise IllegalTransition(kind, current, target)
    now = time.time()
    updated = conn.execute('UPDATE %s SET status=? WHERE id=? AND status=?' % table, (target, row_id, current))
    if updated.rowcount != 1:
        latest = conn.execute('SELECT status FROM %s WHERE id=?' % table, (row_id,)).fetchone()
        return latest['status'] if latest else current
    run_id = row_id if kind == 'run' else conn.execute('SELECT run_id FROM segments WHERE id=?', (row_id,)).fetchone()['run_id']
    if kind == 'run':
        conn.execute('UPDATE runs SET updated_at=? WHERE id=?', (now, row_id))
    conn.execute(
        'INSERT INTO transitions(run_id, segment_id, at, from_status, to_status, reason) VALUES(?,?,?,?,?,?)',
        (run_id, segment_id if kind == 'segment' else None, now, current, target, reason))
    if kind == 'run':
        name = conn.execute('SELECT name FROM runs WHERE id=?', (row_id,)).fetchone()['name']
        _event(conn, row_id, 'STATUS', '%s -> %s: %s' % (current, target, reason), name=name)
    return target


def transition(root, run_id, target, reason, expect=None):
    """Move a run. The only writer of runs.status. Illegal edges raise."""
    conn = connect(root)
    try:
        def run():
            row = conn.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if expect is not None and row['status'] != expect:
                return row['status']
            return _step(conn, 'runs', 'run', run_id, target, reason, RUN_EDGES)
        return _lock(conn, run)
    finally:
        conn.close()


def transition_segment(root, segment_id, target, reason, expect=None):
    conn = connect(root)
    try:
        def run():
            row = conn.execute('SELECT status FROM segments WHERE id=?', (segment_id,)).fetchone()
            if row is None:
                raise KeyError(segment_id)
            if expect is not None and row['status'] != expect:
                return row['status']
            return _step(conn, 'segments', 'segment', segment_id, target, reason, SEGMENT_EDGES, segment_id)
        return _lock(conn, run)
    finally:
        conn.close()


def commit_terminal(root, run_id, segment_id, status, reason, exit_code=0):
    """Store the result and both terminal transitions in one transaction.

    A stream prefix or a segment that is still streaming is not a result.
    Callers must not treat those as done. This is the only path that publishes
    a terminal status together with the exit code.
    """
    if status not in TERMINAL:
        raise IllegalTransition('run', 'running', status)
    conn = connect(root)
    try:
        def run():
            if conn.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone() is None:
                raise KeyError(run_id)
            conn.execute('UPDATE runs SET exit_code=?, ended_at=?, updated_at=? WHERE id=?',
                         (exit_code, time.time(), time.time(), run_id))
            _step(conn, 'runs', 'run', run_id, status, reason, RUN_EDGES)
            row = conn.execute('SELECT status FROM segments WHERE id=?', (segment_id,)).fetchone()
            if row and row['status'] != 'exited' and 'exited' in SEGMENT_EDGES.get(row['status'], ()):
                _step(conn, 'segments', 'segment', segment_id, 'exited', reason, SEGMENT_EDGES, segment_id)
            return status
        return _lock(conn, run)
    finally:
        conn.close()


def successors(status):
    items = list(RUN_EDGES.get(status, ()))
    items.sort(key=lambda item: (item == 'cancelled', item))
    return items


def adopt(conn, run_id, target, reason):
    """Reach target using only legal edges. Used when a legacy writer skips a step."""
    current = conn.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()['status']
    if current == target:
        return current
    queue = [(current, [])]
    seen = {current}
    found = None
    while queue:
        node, path = queue.pop(0)
        for nxt in successors(node):
            step = path + [nxt]
            if nxt == target:
                found = step
                queue = []
                break
            if nxt not in seen and len(step) < 4:
                seen.add(nxt)
                queue.append((nxt, step))
    if not found:
        raise IllegalTransition('run', current, target)
    for step in found:
        _step(conn, 'runs', 'run', run_id, step, reason, RUN_EDGES)
    return target


def max_seq(root=None):
    conn = connect(root)
    try:
        row = conn.execute('SELECT MAX(seq) FROM events').fetchone()
        return row[0] or 0
    finally:
        conn.close()


def events_after(root, seq):
    conn = connect(root)
    try:
        rows = conn.execute(
            'SELECT seq, run_id, segment_id, at, kind, detail, name, legacy_id FROM events WHERE seq > ? ORDER BY seq',
            (seq,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def open_cursor(root, name, create=False):
    """Resume a subscriber. A new default watch starts at the current max seq."""
    conn = connect(root)
    try:
        def run():
            row = conn.execute('SELECT seq FROM cursors WHERE subscriber=?', (name,)).fetchone()
            if row:
                return row['seq']
            if not create:
                raise UnknownCursor(name)
            seq = conn.execute('SELECT MAX(seq) FROM events').fetchone()[0] or 0
            conn.execute('INSERT INTO cursors(subscriber, seq, updated_at) VALUES(?,?,?)', (name, seq, time.time()))
            return seq
        return _lock(conn, run)
    finally:
        conn.close()


def claim_watch(root, name=None, require_existing=False):
    """Lease a persistent consumer; dead holders leave their cursor intact."""
    import agent_oversight as oversight
    conn = connect(root)
    try:
        def run():
            procs = oversight.processes()
            pid = os.getpid()
            identity = procs[pid]['identity']
            subscriber = name or 'watch'
            slot = 1
            while True:
                holder = conn.execute('SELECT pid, identity FROM watch_leases WHERE subscriber=?', (subscriber,)).fetchone()
                if not holder or not oversight.alive(holder['pid'], holder['identity'], procs):
                    break
                if name is not None:
                    raise ValueError('watch consumer already held by a live watcher: ' + subscriber)
                slot += 1
                subscriber = 'watch-' + str(slot)
            row = conn.execute('SELECT seq FROM cursors WHERE subscriber=?', (subscriber,)).fetchone()
            if row is None:
                if require_existing:
                    raise UnknownCursor(subscriber)
                seq = conn.execute('SELECT MAX(seq) FROM events').fetchone()[0] or 0
                conn.execute('INSERT INTO cursors(subscriber, seq, updated_at) VALUES(?,?,?)', (subscriber, seq, time.time()))
            else:
                seq = row['seq']
            conn.execute('INSERT INTO watch_leases(subscriber, pid, identity, acquired_at) VALUES(?,?,?,?) '
                         'ON CONFLICT(subscriber) DO UPDATE SET pid=excluded.pid, identity=excluded.identity, acquired_at=excluded.acquired_at',
                         (subscriber, pid, identity, time.time()))
            return subscriber, seq
        return _lock(conn, run)
    finally:
        conn.close()


def release_watch(root, subscriber):
    import agent_oversight as oversight
    identity = oversight.processes().get(os.getpid(), {}).get('identity')
    conn = connect(root)
    try:
        _lock(conn, lambda: conn.execute('DELETE FROM watch_leases WHERE subscriber=? AND pid=? AND identity=?',
                                         (subscriber, os.getpid(), identity)))
    finally:
        conn.close()


def claim_delivery(root, subscriber, event_id):
    """Persist delivery of synthesized diagnostics as well as sequenced events."""
    conn = connect(root)
    try:
        def run():
            return conn.execute('INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)',
                                ('delivery:' + subscriber + ':' + event_id, str(time.time()))).rowcount == 1
        return _lock(conn, run)
    finally:
        conn.close()


def save_cursor(root, name, seq):
    """Move a cursor forward only, so a replay cannot rewind a subscriber."""
    conn = connect(root)
    try:
        def run():
            row = conn.execute('SELECT seq FROM cursors WHERE subscriber=?', (name,)).fetchone()
            if row is None:
                conn.execute('INSERT INTO cursors(subscriber, seq, updated_at) VALUES(?,?,?)', (name, seq, time.time()))
            elif seq > row['seq']:
                conn.execute('UPDATE cursors SET seq=?, updated_at=? WHERE subscriber=?', (seq, time.time(), name))
        _lock(conn, run)
    finally:
        conn.close()


def insert_run(root, run_id, name, status, **fields):
    """Create a run already in status. Creation is not a transition."""
    if status not in RUN_STATUSES:
        raise IllegalTransition('run', '(new)', status)
    conn = connect(root)
    try:
        def run():
            if conn.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
                raise ValueError('run already exists: ' + run_id)
            _retire_live(conn, name, run_id)
            generation = fields.pop('generation', None)
            if generation is None:
                generation = (conn.execute('SELECT COALESCE(MAX(generation), 0) FROM runs WHERE name=?', (name,)).fetchone()[0] or 0) + 1
            now = time.time()
            conn.execute(
                '''INSERT INTO runs(id, name, generation, status, pending_decision, cwd, prompt, isolate, container,
                   task_type, artifact_dir, created_at, updated_at, started_at, ended_at, failure_class, exit_code, error, payload,
                   check_cmd, review_by, worktree, branch, base_sha, plan_id, task_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (run_id, name, generation, status, 1 if fields.get('pending_decision') else 0,
                 fields.get('cwd') or '', fields.get('prompt') or '', 1 if fields.get('isolate') else 0,
                 1 if fields.get('container') else 0, fields.get('task_type') or 'edit',
                 fields.get('artifact_dir') or str(runs_root(root)), now, now, fields.get('started_at') or now,
                 fields.get('ended_at'), fields.get('failure_class'), fields.get('exit_code'), fields.get('error'),
                 fields.get('payload') or '{}', fields.get('check_cmd'), fields.get('review_by'),
                 fields.get('worktree'), fields.get('branch'), fields.get('base_sha'),
                 fields.get('plan_id'), fields.get('task_id')))
            conn.execute(
                'INSERT INTO transitions(run_id, at, from_status, to_status, reason) VALUES(?,?,?,?,?)',
                (run_id, now, '(new)', status, fields.get('reason') or 'create'))
        _lock(conn, run)
    finally:
        conn.close()


def insert_segment(root, segment_id, run_id, status='starting', **fields):
    if status not in SEGMENT_STATUSES:
        raise IllegalTransition('segment', '(new)', status)
    conn = connect(root)
    try:
        def run():
            now = time.time()
            conn.execute(
                '''INSERT INTO segments(id, run_id, ordinal, cli, model, effort, status, session_id, pid, pid_identity,
                   launcher_pid, supervisor_pid, failure_class, exit_code, started_at, ended_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (segment_id, run_id, fields.get('ordinal') or 1, fields.get('cli') or 'codex',
                 fields.get('model') or '?', fields.get('effort') or 'high', status, fields.get('session_id'),
                 fields.get('pid'), fields.get('pid_identity'), fields.get('launcher_pid'), fields.get('supervisor_pid'),
                 fields.get('failure_class'), fields.get('exit_code'), fields.get('started_at') or now, fields.get('ended_at')))
        _lock(conn, run)
    finally:
        conn.close()


def _retire_live(conn, name, keep_id):
    rows = conn.execute(
        'SELECT id, status FROM runs WHERE name=? AND id!=? AND status NOT IN (%s)' % ','.join('?' * len(TERMINAL)),
        (name, keep_id) + TERMINAL).fetchall()
    for row in rows:
        adopt(conn, row['id'], 'cancelled', 'replaced by a newer generation')


def machine_status(root, run_id):
    conn = connect(root)
    try:
        row = conn.execute('SELECT status, failure_class, generation FROM runs WHERE id=?', (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def segment_status(root, segment_id):
    conn = connect(root)
    try:
        row = conn.execute('SELECT status FROM segments WHERE id=?', (segment_id,)).fetchone()
        return row['status'] if row else None
    finally:
        conn.close()


def load_runs(root):
    """Run dicts for status and watch. status is the state-machine column."""
    conn = connect(root)
    try:
        import json
        rows = conn.execute('SELECT id, name, status, failure_class, payload, ended_at, started_at, exit_code, plan_id, task_type, cwd FROM runs ORDER BY name, generation').fetchall()
        result = []
        for row in rows:
            try:
                data = json.loads(row['payload'] or '{}')
            except ValueError:
                data = {}
            if not isinstance(data, dict) or data.get('_godfather_queue'):
                continue
            data.setdefault('name', row['name'])
            if row['plan_id'] is not None:
                data.setdefault('cli', 'plan')
                data.setdefault('model', row['task_type'])
                data.setdefault('progress_paths', [])
                data.setdefault('cwd', row['cwd'])
            data['legacy_status'] = data.get('status')
            data['status'] = row['status']
            data['run_id'] = row['id']
            if row['failure_class']:
                data['failure_class'] = row['failure_class']
            if 'ended_at' not in data and row['ended_at']:
                data['ended_at'] = row['ended_at']
            if 'started_at' not in data and row['started_at']:
                data['started_at'] = row['started_at']
            if row['exit_code'] is not None and 'exit_code' not in data:
                data['exit_code'] = row['exit_code']
            result.append(data)
        return result
    finally:
        conn.close()



def plan_task(root, name):
    """Resolve a plan task without creating a database for unknown/queued names."""
    if not db_path(root, create_parent=False).is_file():
        return None
    conn = connect(root)
    try:
        row = conn.execute('SELECT * FROM runs WHERE name=? ORDER BY generation DESC LIMIT 1',
                           (name,)).fetchone()
        return dict(row) if row and row['plan_id'] is not None else None
    finally:
        conn.close()

def load_routes(root):
    conn = connect(root)
    try:
        import json
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'route:%' ORDER BY key").fetchall()
        result = []
        for row in rows:
            try:
                result.append(json.loads(row['value']))
            except ValueError:
                continue
        return result
    finally:
        conn.close()


def load_queue(root):
    conn = connect(root)
    try:
        import json
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'queue:%' ORDER BY key").fetchall()
        result = []
        for row in rows:
            try:
                job = json.loads(row['value'])
            except ValueError:
                continue
            if isinstance(job, dict):
                result.append(job)
        return result
    finally:
        conn.close()


def count_events(root):
    conn = connect(root)
    try:
        return conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    finally:
        conn.close()


def segment_heartbeat(root, segment_id):
    conn = connect(root)
    try:
        row = conn.execute(
            '''SELECT id, status, failure_class, last_heartbeat_at, last_heartbeat_kind, last_tool_at,
               tokens_in, tokens_cached, tokens_out, started_at FROM segments WHERE id=?''', (segment_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def record_heartbeats(root, segment_id, beats):
    """Update last_heartbeat_* and last_tool_at. Heartbeats are not event rows.

    Timestamps only move forward, so a reader that restarts does not restamp
    old frames over a newer clock. Returns False when the segment is missing.
    """
    if not beats:
        return False
    conn = connect(root)
    try:
        def run():
            row = conn.execute(
                'SELECT failure_class, last_heartbeat_at, last_tool_at FROM segments WHERE id=?',
                (segment_id,)).fetchone()
            if row is None:
                return False
            last_at = row['last_heartbeat_at']
            last_tool = row['last_tool_at']
            failure = row['failure_class']
            kind = None
            tokens = None
            accepted = False
            for beat in beats:
                at = float(beat['at'])
                if last_at is None or at >= last_at:
                    last_at = at
                    kind = beat['kind']
                    accepted = True
                if beat.get('progress') and (last_tool is None or at >= last_tool):
                    last_tool = at
                incoming = beat.get('failure_class')
                if incoming and failure not in ('broken_stream', 'crash'):
                    if not failure or failure == 'auth' or incoming == 'broken_stream':
                        failure = incoming
                if beat.get('tokens') is not None:
                    tokens = beat['tokens']
            if accepted:
                conn.execute(
                    'UPDATE segments SET last_heartbeat_at=?, last_heartbeat_kind=? WHERE id=?',
                    (last_at, kind, segment_id))
            if last_tool != row['last_tool_at']:
                conn.execute('UPDATE segments SET last_tool_at=? WHERE id=?', (last_tool, segment_id))
            if failure != row['failure_class']:
                conn.execute('UPDATE segments SET failure_class=? WHERE id=?', (failure, segment_id))
            if tokens is not None:
                conn.execute(
                    'UPDATE segments SET tokens_in=?, tokens_cached=?, tokens_out=? WHERE id=?',
                    (tokens[0], tokens[1], tokens[2], segment_id))
            return True
        return _lock(conn, run)
    finally:
        conn.close()


def mark_broken_stream(root, segment_id, reason='dropped'):
    """The CLI dropped. A partial or oversized frame is not a clean exit."""
    conn = connect(root)
    try:
        def run():
            row = conn.execute('SELECT status FROM segments WHERE id=?', (segment_id,)).fetchone()
            if row is None:
                raise KeyError(segment_id)
            status = row['status']
            if status != 'broken_stream' and 'broken_stream' in SEGMENT_EDGES.get(status, ()):
                status = _step(conn, 'segments', 'segment', segment_id, 'broken_stream', reason, SEGMENT_EDGES, segment_id)
            conn.execute('UPDATE segments SET failure_class=? WHERE id=?', ('broken_stream', segment_id))
            return status
        return _lock(conn, run)
    finally:
        conn.close()


def claim_remedy(root, run_id, segment_id, action, reason, result='pending'):
    """Insert the remedy before it runs. False when this action was already claimed."""
    conn = connect(root)
    try:
        def run():
            row = conn.execute(
                'SELECT 1 FROM remedies WHERE run_id=? AND segment_id=? AND action=?',
                (run_id, segment_id, action)).fetchone()
            if row:
                return False
            conn.execute(
                '''INSERT INTO remedies(run_id, segment_id, action, reason, attempted_at, result)
                   VALUES(?,?,?,?,?,?)''',
                (run_id, segment_id, action, reason, time.time(), result))
            return True
        return _lock(conn, run)
    finally:
        conn.close()


RUN_FIELDS = ('cwd', 'worktree', 'branch', 'base_sha', 'prompt', 'check_cmd', 'error', 'exit_code',
              'failure_class', 'artifact_dir', 'payload', 'started_at', 'ended_at', 'task_type', 'pending_decision')


def update_run(root, run_id, **fields):
    """Update run columns other than status. Status changes go through transition."""
    unknown = set(fields) - set(RUN_FIELDS)
    if unknown:
        raise ValueError('unknown run field: ' + ', '.join(sorted(unknown)))
    if not fields:
        return
    conn = connect(root)
    try:
        def run():
            if conn.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone() is None:
                raise KeyError(run_id)
            keys = list(fields)
            conn.execute(
                'UPDATE runs SET %s, updated_at=? WHERE id=?' % ', '.join(key + '=?' for key in keys),
                [fields[key] for key in keys] + [time.time(), run_id])
        _lock(conn, run)
    finally:
        conn.close()



def update_run_payload(root, run_id, changes, pending_decision=None):
    """Merge payload fields under the write lock so scheduler/worker updates coexist."""
    import json
    conn = connect(root)
    try:
        def update():
            row = conn.execute('SELECT payload FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            payload = json.loads(row['payload'] or '{}')
            payload.update(changes)
            conn.execute('UPDATE runs SET payload=?, updated_at=? WHERE id=?',
                         (json.dumps(payload), time.time(), run_id))
            if pending_decision is not None:
                conn.execute('UPDATE runs SET pending_decision=? WHERE id=?', (int(pending_decision), run_id))
        _lock(conn, update)
    finally:
        conn.close()

def add_event(root, run_id, kind, detail, name=None):
    conn = connect(root)
    try:
        return _lock(conn, lambda: _event(conn, run_id, kind, detail, name=name))
    finally:
        conn.close()


PLAN_STATUSES = ('open', 'merging', 'done', 'failed')
PLAN_EDGES = {
    'open': ('merging', 'done', 'failed'),
    'merging': ('done', 'failed'),
    'done': (),
    'failed': ('open',),
}


def insert_plan(root, plan_id, name, repo, base_sha, integration_branch, status='open'):
    if status not in PLAN_STATUSES:
        raise IllegalTransition('plan', '(new)', status)
    conn = connect(root)
    try:
        def run():
            if conn.execute('SELECT 1 FROM plans WHERE id=?', (plan_id,)).fetchone():
                raise ValueError('plan already exists: ' + plan_id)
            conn.execute(
                '''INSERT INTO plans(id, name, repo, base_sha, integration_branch, status, created_at)
                   VALUES(?,?,?,?,?,?,?)''',
                (plan_id, name, repo, base_sha or '', integration_branch or '', status, time.time()))
        _lock(conn, run)
    finally:
        conn.close()


def cas_plan(root, plan_id, expect, target):
    """Compare-and-set a plan row. A lost race returns the current status."""
    if target not in PLAN_STATUSES:
        raise IllegalTransition('plan', expect, target)
    conn = connect(root)
    try:
        def run():
            row = conn.execute('SELECT status FROM plans WHERE id=?', (plan_id,)).fetchone()
            if row is None:
                raise KeyError(plan_id)
            current = row['status']
            if current != expect:
                return current
            if target not in PLAN_EDGES.get(current, ()):
                raise IllegalTransition('plan', current, target)
            updated = conn.execute('UPDATE plans SET status=? WHERE id=? AND status=?', (target, plan_id, expect))
            if updated.rowcount != 1:
                latest = conn.execute('SELECT status FROM plans WHERE id=?', (plan_id,)).fetchone()
                return latest['status'] if latest else current
            return target
        return _lock(conn, run)
    finally:
        conn.close()


def load_plans(root=None):
    conn = connect(root)
    try:
        rows = conn.execute(
            '''SELECT id, name, repo, base_sha, integration_branch, status, created_at
               FROM plans ORDER BY created_at, id''').fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def plan_runs(root, plan_id):
    conn = connect(root)
    try:
        rows = conn.execute(
            '''SELECT id, name, task_id, generation, status, prompt, check_cmd, task_type, isolate, container,
                      cwd, worktree, branch, base_sha, error, exit_code, failure_class, payload, artifact_dir
               FROM runs WHERE plan_id=? ORDER BY generation, task_id''', (plan_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
