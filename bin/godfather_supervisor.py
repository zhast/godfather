"""Declarative timeout, retry, and fallback policy.

The supervisor classifies a segment from heartbeat columns and applies one
action: nudge, resume, switch (handoff), or escalate. Each action records an
incident whose effect is the root_cause tag. [healing] is the migration
fallback and is ignored once [[retry]] is present in godfather.toml.

An in-flight tool is alive until its own budget (tool_secs, or a longer
declared wait such as sleep N). CPU-active child processes are never a stall,
and a nudge or restart will not signal that tree.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

import godfather_errors as errors
import godfather_policy as policy
import godfather_store as store

HEARTBEAT = dict(silence_secs=120, think_without_tool_secs=180, tool_secs=600,
                 start_to_first_tool_secs=180, poll_secs=5)
TIMEOUTS = dict(run_secs=3600, segment_secs=1800, ask_secs=1800, policy_ask_secs=900,
                check_secs=600, provision_secs=60, report_secs=120, final_idle_secs=30, silent_final_idle_secs=600)
RETRY = (
    dict(class_='capacity', attempts=2, backoff_secs=15, action='resume'),
    dict(class_='auth', attempts=1, backoff_secs=30, action='resume'),
    dict(class_='broken_stream', attempts=1, backoff_secs=2, action='resume_chunked'),
    dict(class_='stall_thinking', attempts=1, backoff_secs=0, action='nudge_tool'),
    dict(class_='stall_silent', attempts=1, backoff_secs=0, action='nudge_status'),
    dict(class_='crash', attempts=1, backoff_secs=1, action='restart'),
    dict(class_='check', attempts=2, backoff_secs=0, action='fix'),
    dict(class_='quota', attempts=0, backoff_secs=0, action='fallback'),
    dict(class_='no_output', attempts=0, backoff_secs=0, action='fallback'),
    dict(class_='tool_denied', attempts=0, backoff_secs=0, action='fallback'),
)
FALLBACK = dict(on=('quota', 'auth', 'capacity', 'stall_thinking', 'stall_silent', 'broken_stream', 'crash', 'no_output', 'tool_denied'),
                same_cli='resume', cross_cli='handoff', stop=('policy', 'cancelled', 'other'))
HARD = ('auth', 'quota', 'capacity', 'crash', 'broken_stream', 'policy', 'cancelled', 'no_output', 'tool_denied', 'context_window', 'model_not_found', 'permission_denied', 'bad_request')
PUBLIC = dict(nudge_tool='nudge', nudge_status='nudge', fix='nudge', resume='resume',
              resume_chunked='resume', restart='switch', fallback='switch', handoff='switch', escalate='escalate')
PROBE_TTL = 300
_CACHE = {}
_PS = {}


def segment_outcome(base, rc, run):
    """A successful process must produce a report, not just a headless refusal."""
    def read(path):
        try:
            return Path(path).read_text(errors='replace')
        except OSError:
            return ''
    report = read(str(base) + '.last.md').strip()
    # Native terminal errors outrank earlier tool denials in the transcript.
    # failure_detail excludes assistant prose and command output.
    failure = provider_failure(base, run, final=bool(rc))
    if rc and failure and failure['category'] != 'other':
        return errors.public_class(failure), failure['category'] + ': ' + str(failure['evidence'])[:300]
    def denied(text):
        return re.search(r'(?:auto[- ]denied|(?:tool|command|permission)[^\n]{0,120}\bdenied\b)', text, re.I) and re.search(
            r'headless|non[- ]interactive|cannot prompt|can.t prompt', text, re.I)
    def write_denied(text):
        return bool(denied(text) and re.search(r'\bwrite_file\b|"(?:EditFile|WriteFile|MultiEdit)"', text, re.I))
    diagnostic_report = re.match(
        r'^(?:[\w.-]+:\s*|no output produced|(?:a |the |one or more )?(?:tool|command)\b)', report, re.I)
    diagnostics = read(str(base) + '.log')
    for path in run.get('diagnostic_paths') or []:
        diagnostics += '\n' + read(path)
    combined = report + '\n' + diagnostics + '\n' + '\n'.join(str(e) for e in run.get('errors') or [])
    if diagnostic_report and denied(report):
        if write_denied(combined):
            return 'tool_denied', 'write_file auto-denied in headless mode; no final report'
        return 'tool_denied', 'tool permission denied in headless mode; no final report'
    if not report and denied(diagnostics):
        if write_denied(combined):
            return 'tool_denied', 'write_file auto-denied in headless mode; no final report'
        return 'tool_denied', 'tool permission denied in headless mode; no final report'
    if rc and write_denied(combined):
        return 'tool_denied', 'write_file auto-denied in headless mode; no final report'
    if rc == 0 and not report:
        return 'no_output', 'CLI exited 0 without a final report (.last.md is empty)'
    return None, None


def provider_failure(base, run, final=False):
    """Read native terminal errors, never tool output or assistant prose."""
    from godfather_router import failure_evidence
    from godfather_stream import diagnostic_level
    found = None
    rank = None
    transient = None
    candidates = failure_evidence(base, run)
    saved = run.get('failure')
    if saved and saved.get('evidence') is not None:
        candidates.insert(0, (saved['evidence'], 2 if saved.get('source') == 'structured' else 1))
    for raw, authority in candidates:
        text = raw if isinstance(raw, str) else json.dumps(raw)
        # Missing terminal output is a consequence of the earlier native failure.
        if 'Grok stream ended without terminal end event' in text:
            continue
        failure = errors.classify(run.get('cli', ''), raw)
        if failure['source'] == 'text' and (errors.diagnostic_noise(run.get('cli'), text) or
                                            diagnostic_level(text) != 'error'):
            continue
        if failure['transient']:
            transient = failure
            continue
        # Current failing results outrank saved/history/discovery diagnostics.
        # Within a result, typed and recognized failures survive later noise.
        if authority == 0 and failure['source'] == 'structured' and failure['category'] != 'other':
            authority = 2  # A typed provider error remains evidence even in a discovery file.
        candidate_rank = (authority, failure['category'] != 'other', failure['source'] == 'structured')
        if rank is None or candidate_rank >= rank:
            found, rank = failure, candidate_rank
    if found and found['category'] == 'auth' and run.get('failure_class') == 'tool_denied':
        # A recovered silent-auth warning must not relabel a terminal tool denial.
        return errors.classify(run.get('cli', ''), run.get('failure_reason') or 'tool permission denied')
    if found is None and final and transient:
        return dict(transient, transient=False)
    return found


def outcome_fallback(failure, attempts=None):
    """Completed output failures use the same declarative fallback policy."""
    rules = load_policies()
    decision = evaluate(dict(started_at=0, failure_class=failure, confidence='high'), rules, 0, attempts)
    return failure in rules['fallback']['on'] and decision['action'] in ('handoff', 'fallback')


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(name + ' must be a nonnegative number')
    return value


def _defaults():
    return dict(
        timeouts=dict(TIMEOUTS),
        heartbeat=dict(HEARTBEAT),
        retry=[dict(class_=row['class_'], attempts=row['attempts'], backoff_secs=row['backoff_secs'], action=row['action'])
               for row in RETRY],
        fallback=dict(on=list(FALLBACK['on']), same_cli=FALLBACK['same_cli'], cross_cli=FALLBACK['cross_cli'],
                      stop=list(FALLBACK['stop'])),
        effort=[], active=False)


def load_policies(text=None):
    """Merge godfather.toml over the defaults. active once [[retry]] is present."""
    path = policy.config_dir() / 'godfather.toml'
    if text is None:
        try:
            stamp = path.stat().st_mtime_ns
            body = path.read_text()
        except OSError:
            stamp, body = None, ''
        cached = _CACHE.get('stamp')
        if cached == (str(path), stamp) and 'policy' in _CACHE:
            return _CACHE['policy']
    else:
        stamp, body, path = None, text, None
    raw = policy.document(body) if body.strip() else {}
    merged = _defaults()
    merged['active'] = 'retry' in raw
    for table, spec in (('timeouts', TIMEOUTS), ('heartbeat', HEARTBEAT)):
        found = raw.get(table) or {}
        if not isinstance(found, dict):
            raise ValueError(table + ' must be a table')
        unknown = set(found) - set(spec)
        if unknown:
            raise ValueError('unknown %s setting: %s' % (table, ', '.join(sorted(unknown))))
        for key, value in found.items():
            merged[table][key] = _number(value, key)
    if merged['active']:
        by_class = {row['class_']: row for row in merged['retry']}
        for row in raw.get('retry') or []:
            if not isinstance(row, dict):
                raise ValueError('retry entries must be tables')
            unknown = set(row) - {'class', 'attempts', 'backoff_secs', 'action'}
            if unknown or 'class' not in row:
                raise ValueError('retry needs class, attempts, backoff_secs, action')
            action = row['action']
            if action not in PUBLIC:
                raise ValueError('unknown retry action: ' + str(action))
            by_class[row['class']] = dict(class_=row['class'], attempts=int(_number(row.get('attempts', 0), 'attempts')),
                                           backoff_secs=_number(row.get('backoff_secs', 0), 'backoff_secs'), action=action)
        merged['retry'] = list(by_class.values())
        found = raw.get('fallback') or {}
        if not isinstance(found, dict):
            raise ValueError('fallback must be a table')
        unknown = set(found) - {'on', 'same_cli', 'cross_cli', 'stop'}
        if unknown:
            raise ValueError('unknown fallback setting: ' + ', '.join(sorted(unknown)))
        for key in ('on', 'stop'):
            if key in found:
                if not isinstance(found[key], list) or not all(isinstance(item, str) for item in found[key]):
                    raise ValueError('fallback.' + key + ' must be a list of strings')
                merged['fallback'][key] = list(found[key])
        for key in ('same_cli', 'cross_cli'):
            if key in found:
                merged['fallback'][key] = found[key]
    for row in raw.get('effort') or []:
        merged['effort'].append(_effort_row(row))
    if text is None:
        _CACHE['stamp'] = (str(path), stamp)
        _CACHE['policy'] = merged
    return merged


def policies_active():
    return load_policies()['active']


def auth_retry_budget():
    """(attempts, backoff_secs) for an auth blip. [[retry]] wins when it is present."""
    rules = load_policies()
    if rules['active']:
        row = next((item for item in rules['retry'] if item['class_'] == 'auth'), None)
        if not row:
            return 0, 0.0
        return int(row['attempts']), float(row['backoff_secs'])
    from godfather_lifecycle import settings
    healing, _ = settings()
    if not healing.get('enabled', True):
        return 0, 0.0
    return int(healing['auth_retries']), float(healing['auth_retry_secs'])


def _effort_row(row):
    if not isinstance(row, dict) or set(row) - {'model', 'task_type', 'effort'} or not all(k in row for k in ('model', 'task_type', 'effort')):
        raise ValueError('effort rows need model, task_type, and effort')
    for key in ('model', 'task_type', 'effort'):
        if not isinstance(row[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:/*-]+', row[key]):
            raise ValueError('effort %s must be a token' % key)
    return dict(model=row['model'], task_type=row['task_type'], effort=row['effort'])


def _pick(rows, model, task_type):
    for want_model in (model, '*'):
        for want_task in (task_type, '*'):
            for row in rows:
                if row['model'] == want_model and row['task_type'] == want_task:
                    return row['effort']
    return None


def resolve_effort(model, task_type, model_effort, model_rows=None):
    """Specific model, then wildcards. godfather.toml rows beat models.toml rows."""
    policy_rows = load_policies()['effort']
    if model_rows is None:
        import godfather_router as router
        try:
            model_rows = router.parse_effort_rows(router.config_path().read_text())
        except (OSError, ValueError):
            model_rows = []
    for rows in (policy_rows, model_rows):
        # Exact model before wildcard, but a file's specific row beats the other file's wildcard.
        chosen = _pick([row for row in rows if row['model'] != '*'], model, task_type)
        if chosen:
            return chosen
    for rows in (policy_rows, model_rows):
        chosen = _pick([row for row in rows if row['model'] == '*'], model, task_type)
        if chosen:
            return chosen
    return model_effort or 'high'


def declared_wait(text):
    found = [float(item) for item in re.findall(r'\bsleep\s+(\d+(?:\.\d+)?)\b', text or '')]
    return max(found) if found else None


def _ps_rows():
    now = time.monotonic()
    if _PS.get('at') and now - _PS['at'] < 0.2:
        return _PS['rows']
    rows = []
    try:
        out = subprocess.run(['ps', '-axo', 'pid=,ppid=,pcpu=,state=,command='],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ''
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            rows.append(dict(pid=int(parts[0]), ppid=int(parts[1]), pcpu=float(parts[2]),
                             state=parts[3], command=parts[4]))
        except ValueError:
            continue
    _PS['at'] = now
    _PS['rows'] = rows
    return rows


def descendant_block(pid):
    """Reason a live child tree must not be treated as a stall or signaled."""
    if not pid:
        return None
    try:
        root = int(pid)
    except (TypeError, ValueError):
        return None
    kids = {}
    for row in _ps_rows():
        kids.setdefault(row['ppid'], []).append(row)
    stack, seen = list(kids.get(root, [])), set()
    while stack:
        row = stack.pop()
        if row['pid'] in seen:
            continue
        seen.add(row['pid'])
        stack.extend(kids.get(row['pid'], []))
        if row['state'].startswith('R') or row['pcpu'] >= 1:
            return 'cpu-active children'
        if declared_wait(row['command']):
            return 'declared wait'
    return None


def tool_within_budget(book, now, rules=None):
    if book is None or not book.open_tools:
        return False
    rules = rules or load_policies()
    start = min(book.open_tools.values())
    declared = 0
    for beat in book.beats:
        if beat.get('tool_state') == 'open' and beat.get('tool_id') in book.open_tools:
            declared = max(declared, declared_wait(beat.get('detail') or '') or 0)
    return (now - start) < max(rules['heartbeat']['tool_secs'], declared)


def inflight_protected(progress, run, now=None):
    """True when this run is executing a tool, not stalling."""
    now = time.time() if now is None else now
    if descendant_block(run.get('pid')):
        return True
    return tool_within_budget(getattr(progress, 'book', None), now)


def pending_question(run):
    """Read control afresh: a watcher snapshot may predate a blocking ask."""
    import agent_oversight as a
    questions = run.get('questions') or []
    if run.get('name'):
        base = a.runs_dir() / run['name']
        questions = a.read(str(base) + '.control.json').get('questions', questions)
    return bool(run.get('pending_decision') or any(q.get('answer') is None or not q.get('delivered', True) for q in questions))


def pause_for_decision(progress, run, now=None):
    if not pending_question(run):
        return False
    now = time.time() if now is None else now
    progress.last_at = now
    book = getattr(progress, 'book', None)
    if book is not None:
        book.last_at = now
        book.thought_origin = now
        if book.last_tool_at is not None:
            book.last_tool_at = now
        book.open_tools = {key: now for key in book.open_tools}
    return True


def refuse_signal(run, progress=None):
    """Nudge and restart must not signal a tree that is doing work."""
    if pending_question(run) or run.get('status') == 'needs_input':
        return 'pending question'
    reason = descendant_block(run.get('pid'))
    if reason:
        return reason
    book = getattr(progress, 'book', None) if progress is not None else None
    if tool_within_budget(book, time.time()):
        return 'in-flight tool'
    return None


def interruption_prompt(run, reason_class, reason=None, progress=None):
    """Capture unfinished tools before a retry truncates the previous stream."""
    if progress is None:
        import agent_oversight as oversight
        try:
            progress = oversight.Progress(run).update(run)
        except (OSError, ValueError, KeyError):
            progress = None
    book = getattr(progress, 'book', None)
    tool = next((beat['detail'] for beat in reversed(book.beats)
                 if beat.get('tool_state') == 'open' and beat.get('tool_id') in book.open_tools), None) if book else None
    context = 'The previous segment was interrupted by the supervisor, not the user. '
    context += 'Reason class: {}. '.format(reason_class or 'unknown')
    if reason:
        context += 'Reason: {}. '.format(reason)
    if tool:
        context += ('Last command/tool call in flight: {}. The supervisor interrupted it; '
                    'check its effects before rerunning it; don\'t redo completed steps.').format(tool)
    else:
        context += ('The last command/tool call in flight is unknown; check state first, then '
                    'check its effects before rerunning it; don\'t redo completed steps.')
    return context + '\n\n'


def snapshot_from_book(book, now, **extra):
    declared = 0
    for beat in book.beats:
        if beat.get('tool_state') == 'open' and beat.get('tool_id') in book.open_tools:
            declared = max(declared, declared_wait(beat.get('detail') or '') or 0)
    snap = dict(
        started_at=book.started_at, last_heartbeat_at=book.last_at, last_heartbeat_kind=book.last_kind,
        last_tool_at=book.last_tool_at, open_tools=len(book.open_tools),
        tool_started_at=min(book.open_tools.values()) if book.open_tools else None,
        thought_only_secs=book.thought_only_secs(now), failure_class=book.failure_class,
        declared_wait_secs=declared or None, children_cpu=False, confidence='medium', same_cli=None)
    snap.update(extra)
    return snap


def _decision(failure, root, action, backoff, kill=False, confidence='medium'):
    public = PUBLIC.get(action, action)
    return dict(failure_class=failure, root_cause=root, action=action, public=public,
                backoff_secs=backoff, kill=bool(kill), confidence=confidence)


def classify(snapshot, rules, now):
    """Return (failure_class, root_cause). None when the segment is healthy."""
    if snapshot.get('pending_decision') or snapshot.get('status') == 'needs_input':
        return None, None
    hb = rules['heartbeat']
    fc = snapshot.get('failure_class')
    if fc in HARD or fc in ('check', 'other'):
        return fc, snapshot.get('root_cause') or fc
    if snapshot.get('children_cpu'):
        return None, None
    open_tools = snapshot.get('open_tools') or 0
    tool_at = snapshot.get('tool_started_at')
    if open_tools and tool_at is not None:
        declared = snapshot.get('declared_wait_secs') or 0
        if snapshot.get('wait_process') or (now - tool_at) < max(hb['tool_secs'], declared):
            return None, None
        return 'stall_silent', 'tool_timeout'
    started = snapshot['started_at']
    last_at = snapshot.get('last_heartbeat_at')
    silence = now - (last_at if last_at is not None else started)
    think = snapshot.get('thought_only_secs') or 0
    kind = snapshot.get('last_heartbeat_kind')
    if not snapshot.get('last_tool_at') and not open_tools and kind in ('thought', 'text', 'usage', 'stream'):
        if think >= hb['start_to_first_tool_secs']:
            return 'stall_thinking', 'start_to_first_tool'
    if last_at is None and silence >= hb['silence_secs']:
        return 'stall_silent', 'stall_silent'
    if think >= hb['think_without_tool_secs'] and silence < hb['silence_secs']:
        return 'stall_thinking', 'stall_thinking'
    if silence >= hb['silence_secs']:
        return 'stall_silent', 'stall_silent'
    return None, None


def git_head(cwd):
    from godfather_safety import original_environment
    try:
        result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', 'HEAD'],
                                env=original_environment(), capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def finished_report(run, progress, base, initial_head, now=None):
    """A new commit, a report after the last tool, and idle time prove completion."""
    now = time.time() if now is None else now
    book = getattr(progress, 'book', None)
    if not initial_head or pending_question(run) or run.get('stop_requested'):
        return None
    if book is not None and (book.open_tools or book.open_items or book.failure_class):
        return None
    if descendant_block(run.get('pid')):
        return None
    report, report_at = '', None
    path = Path(str(base) + '.last.md')
    if path.is_file():
        report = path.read_text(errors='replace').strip()
        if report:
            report_at = path.stat().st_mtime
    if book is not None and book.report_text:
        report, report_at = book.report_text, book.report_at
    last_tool = book.last_tool_at if book is not None else None
    if not report or report_at is None or (last_tool is not None and report_at < last_tool):
        return None
    last_event = max(book.last_event_at or 0, book.last_at or 0) if book is not None else progress.last_at
    timeouts = load_policies()['timeouts']
    ended = book is not None and book.end_at is not None and book.end_at >= report_at
    idle_secs = timeouts['final_idle_secs'] if ended else timeouts['silent_final_idle_secs']
    if now - max(report_at, last_tool or report_at, last_event or 0) < idle_secs:
        return None
    head = git_head(run['cwd'])
    if not head or head == initial_head:
        return None
    return dict(commit=head, report=report)


def synthesize_report(commit, events):
    """A short report from the commit and the recorded events. No model call."""
    lines = ['Commit: %s' % (commit or 'unknown')]
    for event in events or []:
        kind = event.get('kind') or 'EVENT'
        detail = event.get('detail') or ''
        lines.append((kind + ' ' + detail).strip())
    return '\n'.join(lines)[:4000]


def settle_acceptance(snapshot, rules, now):
    """The run is done once the check passed and a commit exists.

    The final report is a separate turn. Past report_secs, synthesize it.
    Thought after that point is not a stall and does not hold the run open.
    """
    if not snapshot.get('check_passed') or not snapshot.get('commit'):
        return None
    started = snapshot.get('report_started_at')
    cap = rules['timeouts']['report_secs']
    confidence = snapshot.get('confidence') or 'medium'
    if started is None:
        return _decision(None, 'acceptance_done', 'done', 0, confidence=confidence)
    if now - float(started) >= cap:
        return _decision(None, 'report_timeout', 'synthesize_report', 0, confidence=confidence)
    return _decision(None, 'report_turn', None, 0, confidence=confidence)


def evaluate(snapshot, rules, now, attempts=None):
    """Choose the next public action. kill is set only for a dead process."""
    if snapshot.get('pending_decision') or snapshot.get('status') == 'needs_input':
        return _decision(None, 'pending_question', None, 0)
    settled = settle_acceptance(snapshot, rules, now)
    if settled:
        return settled
    attempts = attempts or {}
    confidence = snapshot.get('confidence') or 'medium'
    run_started = snapshot.get('run_started_at', snapshot['started_at'])
    if now - run_started >= rules['timeouts']['run_secs']:
        return _decision('timed_out', 'run_timeout', 'escalate', 0, confidence=confidence)
    if now - snapshot['started_at'] >= rules['timeouts']['segment_secs']:
        return _decision('timed_out', 'segment_timeout', 'escalate', 0, confidence=confidence)
    failure, root = classify(snapshot, rules, now)
    if not failure:
        return _decision(None, None, None, 0, confidence=confidence)
    rule = next((row for row in rules['retry'] if row['class_'] == failure), None)
    used = attempts.get(failure, 0)
    if rule and used < rule['attempts'] and rule['action'] != 'fallback':
        kill = rule['action'] == 'restart' and confidence == 'high'
        if rule['action'] == 'restart' and confidence != 'high':
            return _decision(failure, root, 'escalate', 0, kill=False, confidence=confidence)
        if PUBLIC.get(rule['action']) in ('nudge', 'resume') and confidence == 'low':
            return _decision(failure, root, 'escalate', 0, kill=False, confidence=confidence)
        return _decision(failure, root, rule['action'], rule['backoff_secs'], kill=kill, confidence=confidence)
    fallback = rules['fallback']
    if failure in fallback['on'] and confidence == 'high':
        if snapshot.get('same_cli') == 'agy' and fallback.get('same_cli') == 'resume':
            return _decision(failure, root, 'resume', 0, kill=False, confidence=confidence)
        return _decision(failure, root, 'handoff', 0, kill=True, confidence=confidence)
    if failure in fallback['stop'] or failure == 'other':
        return _decision(failure, root or 'other', 'escalate', 0, confidence=confidence)
    # Attempts are spent, or the process is still alive: do not kill it.
    return _decision(failure, root, 'escalate', 0, kill=False, confidence=confidence)


def stored_attempts(runs, run_id):
    """How many retry claims this run already recorded, keyed by failure class."""
    conn = store.connect(runs)
    try:
        rows = conn.execute('SELECT action FROM remedies WHERE run_id=?', (run_id,)).fetchall()
    finally:
        conn.close()
    counts = {}
    for row in rows:
        action = row['action']
        if '#' not in action:
            continue
        kind, _, number = action.partition('#')
        try:
            counts[kind] = max(counts.get(kind, 0), int(number))
        except ValueError:
            continue
    return counts


def commit_action(run, decision, runs=None):
    """Claim the remedy, then record the incident. False when already claimed."""
    if not decision.get('public'):
        return False
    run_id = run.get('run_id') or run.get('name') or 'unknown'
    segment = run.get('segment_id') or run_id
    action = decision.get('claim') or decision.get('action') or decision['public']
    reason = decision.get('root_cause') or action
    if not store.claim_remedy(runs, run_id, segment, action, reason):
        return False
    base = run.get('base') or run.get('name') or run_id
    policy.incident(base, ['policy', decision['public'], action], run.get('cwd') or '', [],
                    'audit', effect=reason, source='policy', root_cause=reason, action=decision['public'])
    return True


def schedule(run, progress, procs=None):
    """Apply the declarative policy. No-op until [[retry]] exists and healing is on."""
    if pause_for_decision(progress, run) or run.get('status') == 'needs_input':
        return _decision(None, 'pending_question', None, 0)
    rules = load_policies()
    if not rules['active']:
        return None
    from godfather_lifecycle import settings
    healing, _ = settings()
    if not healing.get('enabled', True):
        return None
    if inflight_protected(progress, run):
        return _decision(None, None, None, 0)
    book = getattr(progress, 'book', None)
    if book is None:
        return None
    now = time.time()
    block = descendant_block(run.get('pid'))
    confidence = 'low' if block else ('high' if not _alive(run, procs) else 'medium')
    import agent_oversight as oversight
    runs = oversight.runs_dir()
    base = runs / (run.get('router_name') or run['name'])
    run_id = run.get('run_id') or run.get('name')
    snap = snapshot_from_book(book, now, confidence=confidence, children_cpu=bool(block and 'cpu' in str(block)),
                              same_cli='agy' if run.get('cli') == 'agy' else run.get('cli'))
    decision = evaluate(snap, rules, now, stored_attempts(runs, run_id))
    if not decision.get('public'):
        return decision
    used = stored_attempts(runs, run_id).get(decision['failure_class'], 0)
    if decision['action'] in ('handoff', 'escalate'):
        decision['claim'] = decision['action'] + ':' + (decision['root_cause'] or '')
    else:
        decision['claim'] = '%s#%d' % (decision['failure_class'], used + 1)
    if not commit_action(dict(run, base=base), decision, runs):
        return decision
    if decision['public'] == 'escalate' or (decision['public'] == 'switch' and not decision['kill']):
        oversight_event(base, 'ESCALATE', decision['root_cause'] or decision['action'])
        return decision
    mapped = 'stream' if decision['action'] in ('resume_chunked', 'nudge_tool', 'resume') else (
        'stall' if decision['action'] in ('nudge_status', 'fix') else 'died')
    if decision['kill']:
        mapped = 'restart' if _alive(run, procs) else 'died'
    from godfather_lifecycle import request_heal
    if decision['confidence'] != 'low':
        request_heal(run, mapped, decision['root_cause'] or decision['action'],
                     reason_class=decision['failure_class'], progress=progress)
    return decision


def oversight_event(base, kind, detail):
    import godfather_safety as safety
    safety.event(base, kind, detail)


def _alive(run, procs):
    import agent_oversight as oversight
    return oversight.run_alive(run, procs)


def parse_tui_quota(cli, text, now=None):
    """Remaining weekly percent, or 'parse' when the screen is not a quota report.

    An empty capture is unavailable, not a parse failure. Grok's figure is the
    used share. Codex's figure is the share left.
    """
    if text is None or not str(text).strip():
        return None
    # tmux preserves the padding and box borders of full-width modal dialogs.
    # Normalize those before applying bounded quota-field patterns.
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(text))
    text = re.sub(r'[ \t│]+', ' ', text)
    flat = re.sub(r'\s+', ' ', text)
    plan, consistent = None, True
    if cli == 'grok':
        match = re.search(r'Weekly limit\s*\(([^)]*)\)\s*[█░▓▒━─ ]*?(\d+(?:\.\d+)?)\s*%(\s*left)?', flat, re.I)
        bar = re.search(r'Weekly limit left:\s*(\d+(?:\.\d+)?)\s*%', flat, re.I)
        if not match and not bar:
            return 'parse'
        if match:
            plan = match[1].strip()
            number = float(match[2])
            remaining = number if match[3] else 100 - number
            if bar:
                consistent = abs(remaining - float(bar[1])) <= 1
        else:
            remaining = float(bar[1])
    elif cli == 'codex':
        match = re.search(r'Weekly limit:\s*(.{0,120}?)(\d+(?:\.\d+)?)\s*%\s*left', text, re.I | re.S)
        if not match:
            return 'parse'
        remaining = float(match.group(2))
    else:
        return None
    if remaining < 0 or remaining > 100:
        return 'parse'
    reset = None
    clock = re.search(r'resets?\s*(?:in|at|:)?\s*([^\n]{1,80})', text, re.I)
    if clock:
        reset = clock.group(1).strip()
    resets_at = None
    # Grok omits the year and displays the reset in the machine's local time.
    local = re.search(r'Resets:\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{1,2}):(\d\d)', flat, re.I)
    if local:
        import datetime as dt
        clock = time.time() if now is None else now
        year = dt.datetime.fromtimestamp(clock).year
        for candidate_year in range(year, year + 5):
            try:
                candidate = dt.datetime.strptime('{} {} {} {}:{}'.format(candidate_year, *local.groups()), '%Y %B %d %H:%M').timestamp()
                if candidate > clock:
                    resets_at = candidate
                    break
            except ValueError:
                pass
    return dict(remaining=remaining, used_percent=100 - remaining, reset=reset,
                resets_at=resets_at, plan=plan, consistent=consistent)


def _probe_cache(now):
    path = policy.state_dir() / 'quota-probe.json'
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    return path, data if isinstance(data, dict) else {}


def structured_quota(cli, home, now):
    """Recent native rate-limit observations, scoped to the selected account."""
    import agent_oversight as a
    candidates = []
    for path in a.runs_dir().glob('*.run.json'):
        run = a.read(path)
        if run.get('cli') != cli or a.backend_home(cli, run) != Path(home):
            continue
        at = run.get('rate_limits_at')
        if not isinstance(at, (int, float)) or not 0 <= now - at < PROBE_TTL:
            continue
        limits = run.get('rate_limits')
        if isinstance(limits, dict):
            candidates.append((at, limits))
    if not candidates:
        return None
    at, limits = max(candidates, key=lambda row: row[0])
    windows = []
    def walk(obj):
        if not isinstance(obj, dict):
            return
        reset = errors.timestamp(obj.get('resetsAt', obj.get('resets_at')))
        if reset and reset <= now:
            return
        used = obj.get('usedPercent', obj.get('used_percent'))
        if isinstance(used, (int, float)) and 0 <= used < float('inf'):
            windows.append(max(0, 100 - used))
        for child in obj.values():
            if isinstance(child, dict):
                walk(child)
    walk(limits)
    if not windows:
        return None
    return dict(remaining=min(windows), resets_at=errors.reset_at(limits, now), at=at, source='structured')


def quota_preflight(cli, binary, minimum=0, key=None, now=None, capture=None, home=None, force=False):
    """Cache unreadable screens briefly; only parsed exhaustion is a cooldown."""
    if cli not in ('codex', 'grok') or os.getenv('GODFATHER_QUOTA_PROBE') == '0':
        return dict(status='unknown', detail='cooldown clear; headless remaining-quota query unavailable', unavailable=True)
    now = time.time() if now is None else now
    path, cache = _probe_cache(now)
    from agent_oversight import backend_home
    selected_home = backend_home(cli, {cli + '_home': home})
    cache_key = cli
    if selected_home != Path.home() / ('.' + cli):
        cache_key += '@' + str(selected_home)
    native = None if force else structured_quota(cli, selected_home, now)
    if native is not None:
        remaining = native['remaining']
        status = 'cooldown' if remaining <= 0 else 'ineligible' if minimum and remaining < minimum else 'ok'
        detail = 'structured quota remaining %.0f%%' % remaining
        if native.get('resets_at'):
            detail += ' resets_at=' + str(native['resets_at'])
        return dict(native, status=status, detail=detail)
    entry = cache.get(cache_key) or {}
    # Discard the old false-exhaustion cache format after upgrading.
    if entry.get('detail') == 'quota probe parse failed':
        entry = {}
    if not force and entry.get('at') and 0 <= now - float(entry['at']) < PROBE_TTL and 'status' in entry:
        result = dict(entry, cached=True)
        # Older caches did not record whether capture started. Only the known
        # unavailable result permits log fallback; parse failures never do.
        result.setdefault('unavailable', entry.get('detail') == 'cooldown clear; headless remaining-quota query unavailable')
        if 'remaining' in result:
            remaining = result['remaining']
            result['status'] = 'cooldown' if remaining <= 0 else 'ineligible' if remaining < minimum else 'ok'
        return result
    if capture is None and (not binary or not shutil.which('tmux')):
        result = dict(status='unknown', detail='cooldown clear; headless remaining-quota query unavailable', at=now, unavailable=True)
    else:
        slash = '/usage' if cli == 'grok' else '/status'
        unreadable = False
        try:
            text = capture(binary, slash) if capture else capture_tui(binary, slash, home=str(selected_home), home_variable=cli.upper() + '_HOME')
        except OSError:
            text = ''
        except subprocess.TimeoutExpired:
            text, unreadable = '', True
        parsed = parse_tui_quota(cli, text, now=now)
        if parsed == 'parse' or unreadable:
            result = dict(status='unknown', detail='quota probe unreadable; remaining quota unknown', at=now, unavailable=False)
        elif parsed is None:
            result = dict(status='unknown', detail='cooldown clear; headless remaining-quota query unavailable', at=now, unavailable=True)
        elif parsed['remaining'] <= 0 or (minimum and parsed['remaining'] < minimum):
            if parsed['remaining'] <= 0:
                result = dict(status='cooldown', detail='weekly limit exhausted', remaining=parsed['remaining'], at=now)
            else:
                result = dict(status='ineligible', detail='weekly remaining %.0f%%' % parsed['remaining'],
                              remaining=parsed['remaining'], at=now)
        else:
            result = dict(status='ok', detail='weekly remaining %.0f%%' % parsed['remaining'],
                          remaining=parsed['remaining'], reset=parsed.get('reset'), at=now)
        if isinstance(parsed, dict):
            result.update(parsed, source='tui', unavailable=False)
            if parsed.get('resets_at'):
                result['detail'] += ' resets_at=' + str(parsed['resets_at'])
            if not parsed.get('consistent', True):
                result['detail'] += '; status bar disagrees; using weekly panel'
    cache[cache_key] = result
    try:
        path.write_text(json.dumps(cache))
    except OSError:
        pass
    return result


def capture_tui(binary, slash, wait=8, home=None, home_variable='CODEX_HOME'):
    """Empty means unavailable; capture failures after startup mean unreadable."""
    if not binary or not shutil.which('tmux'):
        return ''
    session = 'gfprobe%s-%s' % (os.getpid(), uuid.uuid4().hex[:8])
    started_ok = False
    try:
        subprocess.run(['tmux', 'kill-session', '-t', session], capture_output=True, timeout=3)
        # tmux servers retain an old environment; pass the home in the command
        # executed in the pane rather than relying on the tmux client's env.
        worker = [binary] if home is None else ['env', home_variable + '=' + home, binary]
        import shlex
        worker_command = ' '.join(shlex.quote(part) for part in worker)
        started = subprocess.run(['tmux', 'new-session', '-d', '-s', session, '-x', '180', '-y', '50', worker_command],
                                 capture_output=True, text=True, timeout=8)
        if started.returncode:
            return ''
        started_ok = True
        time.sleep(min(wait, 6))
        # Newer Codex TUIs coalesce text + Enter in one burst as a paste.
        # Submit separately so /status executes rather than staying in the editor.
        subprocess.run(['tmux', 'send-keys', '-t', session, '-l', slash], capture_output=True, timeout=3)
        time.sleep(.5)
        subprocess.run(['tmux', 'send-keys', '-t', session, 'Enter'], capture_output=True, timeout=3)
        time.sleep(min(wait, 6))
        pane = subprocess.run(['tmux', 'capture-pane', '-t', session, '-p', '-S', '-200'],
                              capture_output=True, text=True, timeout=3)
        visible = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', pane.stdout or '')
        return pane.stdout if visible.strip() else 'quota probe unreadable'
    except OSError:
        return 'quota probe unreadable' if started_ok else ''
    finally:
        try:
            subprocess.run(['tmux', 'kill-session', '-t', session], capture_output=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
