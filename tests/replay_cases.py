"""Incident replays that drive the watcher, the result reader, and acceptance."""
import os
from pathlib import Path
import subprocess
import sys

import replay_engine as engine


def _reap(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def watch_cursor(_spec, _directory, runs):
    store = engine.store
    store.add_event(runs, 'task', 'DONE', 'old completion', name='task')
    cursor = store.open_cursor(runs, 'watch', create=True)
    hidden = store.events_after(runs, cursor)
    store.add_event(runs, 'task', 'NEEDS_INPUT', 'new question', name='task')
    seen = [row['kind'] + ' ' + row['detail'] for row in store.events_after(runs, cursor)]
    store.save_cursor(runs, 'watch', store.max_seq(runs))
    again = store.events_after(runs, store.open_cursor(runs, 'watch'))
    try:
        store.open_cursor(runs, 'missing', create=False)
        unknown = False
    except store.UnknownCursor:
        unknown = True
    first = store.claim_remedy(runs, 'task', 'task-seg', 'stall_thinking#1', 'start_to_first_tool')
    second = store.claim_remedy(runs, 'task', 'task-seg', 'stall_thinking#1', 'start_to_first_tool')
    engine.prevent(runs, 'task', 'watch_cursor', 'a new watch starts at the current seq')
    return dict(root_cause='watch_cursor', prevented=True, replayed=bool(hidden), seen=seen, resumed_empty=again == [],
                unknown_cursor=unknown, second_remedy=second, first_remedy=first, detected=True)


def orphan_watcher(_spec, _directory, runs):
    oversight = engine.oversight
    handle = oversight.try_heal_lock()
    try:
        second = oversight.try_heal_lock()
        roles = ['heal' if handle else 'observe', 'observe' if second is None else 'heal']
        if second:
            second.close()
        proc = subprocess.Popen([str(engine.ROOT / 'bin' / 'agents-watch'), '--poll-secs', '0.05'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ)
        try:
            import selectors
            sel = selectors.DefaultSelector()
            sel.register(proc.stderr, selectors.EVENT_READ)
            ready = sel.select(3)
            line = proc.stderr.readline().decode() if ready else ''
            sel.close()
        finally:
            _reap(proc)
    finally:
        if handle:
            handle.close()
    stamp = oversight.watch_config_stamp()
    unchanged = oversight.watch_config_changed(stamp)
    path = Path(os.environ['XDG_CONFIG_HOME']) / 'godfather' / 'godfather.toml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[healing]\nenabled = false\nstall_secs = 4\n')
    changed = oversight.watch_config_changed(stamp)
    pipe = subprocess.Popen([str(engine.ROOT / 'bin' / 'agents-watch'), '--no-heal', '--poll-secs', '0.05'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ)
    pipe.stdout.close()
    try:
        code = pipe.wait(timeout=3)
    finally:
        if pipe.poll() is None:
            pipe.kill()
            pipe.wait(timeout=3)
        if pipe.stderr is not None and not pipe.stderr.closed:
            pipe.stderr.close()
    engine.prevent(runs, 'orphan-watcher', 'orphan_watcher', 'one healer, and the watcher stops when its output or parent is gone')
    return dict(root_cause='orphan_watcher', prevented=True, roles=roles,
                observe_message='observe only' in line, parent_exits=oversight.parent_gone(1) and not oversight.parent_gone(os.getpid()),
                stdout_exits=code == 0, config_reread=unchanged is None and changed[0]['enabled'] is False and changed[0]['stall_secs'] == 4,
                detected=True)


def missing_stream(_spec, _directory, runs):
    base = runs / 'missing'
    proc = subprocess.run([sys.executable, str(engine.ROOT / 'bin' / 'agent_oversight.py'), 'grok-result', str(base)],
                          capture_output=True, text=True, env=os.environ)
    engine.prevent(runs, 'missing', 'missing_stream', 'a missing stream is an error, not a traceback')
    return dict(root_cause='missing_stream', prevented=True, exit_code=proc.returncode,
                clear_error='Grok stream file is missing' in proc.stderr, traceback='Traceback' not in proc.stderr,
                cause='supervise returns 2 before opening the stream when the working directory is locked or refused',
                detected=True)


def index_only(_spec, _directory, runs):
    policy = engine.policy
    repo = runs / 'repo'
    repo.mkdir()
    subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
    (repo / '.env').write_text('SECRET=1\n')
    (repo / 'keep.txt').write_text('ok\n')
    subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
    context = dict(cwd=str(repo), base=str(runs / 'task'), scratch=[])
    staged = policy.evaluate(['git', 'restore', '--staged', ':/'], context, cwd=str(repo))
    worktree = policy.evaluate(['git', 'restore', '--staged', '--worktree', ':/'], context, cwd=str(repo))
    engine.prevent(runs, 'task', 'index_only', 'unstaging does not write the worktree')
    return dict(root_cause='index_only', prevented=True, verdict=staged['verdict'], effect=staged['effect'],
                targets=staged['targets'], worktree_verdict=worktree['verdict'],
                worktree_hits_secret=str((repo / '.env').resolve()) in worktree['targets'], detected=True)


def final_report(spec, directory, runs):
    name = 'final-report'
    engine.open_run(runs, name, 'grok')
    book = engine.stream.Book('grok', started_at=engine.STARTED)
    book.replay(directory / 'stream.jsonl', engine.STARTED, float(spec['clock']['step_secs']))
    commit = next(beat['detail'] for beat in book.beats if 'git commit' in (beat.get('detail') or ''))
    commit_at = next(beat['at'] for beat in book.beats if 'git commit' in (beat.get('detail') or ''))
    rules = engine.sup.load_policies('')
    snap = engine.sup.snapshot_from_book(book, commit_at, check_passed=True, commit=commit)
    decision = engine.sup.evaluate(snap, rules, commit_at, {})
    engine.store.transition(runs, name, 'done', decision['root_cause'])
    snap['report_started_at'] = commit_at
    turn = engine.sup.evaluate(snap, rules, commit_at + rules['timeouts']['report_secs'] - 1, {})
    late = engine.sup.evaluate(snap, rules, commit_at + rules['timeouts']['report_secs'], {})
    text = engine.sup.synthesize_report(commit, [dict(kind='CHECK', detail='pass')])
    engine.store.add_event(runs, name, 'REPORT', late['root_cause'], name=name)
    return dict(root_cause=decision['root_cause'], prevented=True, failure_class='' if decision['failure_class'] is None else decision['failure_class'],
                remedy=decision['action'], classified_by_secs=commit_at - engine.STARTED, saw_statuses=engine.saw_statuses(runs, name),
                report_cap_secs=rules['timeouts']['report_secs'], report_turn_root=turn['root_cause'],
                synthesize_root=late['root_cause'], synthesized='git commit' in text and 'CHECK pass' in text,
                not_stall=late['failure_class'] is None, kill=bool(decision['kill']), detected=True)


def _tripwire(directory, name):
    path = directory / name
    path.write_text("#!/bin/sh\nprintf '%s %s\\n' " + name + ' "$*" >> "$GODFATHER_TRIPWIRE"\nexit 0\n')
    path.chmod(0o755)


def _trip_hits(path):
    try:
        return [line.strip() for line in path.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def hermetic_suite(_spec, _directory, runs):
    trip = runs / 'trip'
    trip.mkdir()
    for name in ('agy', 'codex', 'grok', 'cursor-agent'):
        _tripwire(trip, name)
    hits = runs / 'hits'
    home = runs / 'home'
    (home / '.codex').mkdir(parents=True)
    (home / '.codex' / 'auth.json').write_text('{"ok":true}\n')
    (home / '.grok').mkdir()
    (home / '.grok' / 'auth.json').write_text('{"ok":true}\n')
    (home / '.gemini').mkdir()
    (home / '.gemini' / 'oauth_creds.json').write_text('{"ok":true}\n')
    env = dict(os.environ)
    env.update(PATH=str(trip) + os.pathsep + env.get('PATH', ''), HOME=str(home),
               CURSOR_AGENT_BIN=str(trip / 'cursor-agent'),
               GODFATHER_MCP_SDK='0', PREFIX=str(runs / 'prefix'), PYTHONDONTWRITEBYTECODE='1',
               XDG_STATE_HOME=str(runs / 'state'), XDG_CONFIG_HOME=str(runs / 'config'),
               AGENT_RUNS_DIR=str(runs / 'agent-runs'), GODFATHER_TRIPWIRE=str(hits))
    for key in ('CODEX_BIN', 'CODEX_HOME', 'GEMINI_API_KEY', 'XAI_API_KEY',
                'GODFATHER_ORIGINAL_PATH', 'GODFATHER_ORIGINAL_BASH_ENV', 'GODFATHER_ORIGINAL_ZDOTDIR'):
        env.pop(key, None)
    installer = engine.copy_installer(engine.ROOT, runs / 'installer-source')
    install = subprocess.run(['bash', str(installer)], env=env, cwd=str(engine.ROOT),
                             capture_output=True, text=True, timeout=60)
    install_hits = _trip_hits(hits)
    hits.write_text('')
    doctor = subprocess.run([sys.executable, str(engine.ROOT / 'bin/godfather'), 'doctor', '--since', '1h'],
                            env=env, cwd=str(runs), capture_output=True, text=True, timeout=30)
    doctor_hits = _trip_hits(hits)
    hits.write_text('')
    live = subprocess.run([sys.executable, str(engine.ROOT / 'bin/godfather'), 'doctor', '--live', '--since', '1h'],
                          env=env, cwd=str(runs), capture_output=True, text=True, timeout=30)
    probes = _trip_hits(hits)
    engine.prevent(runs, 'hermetic-suite', 'hermetic_suite', 'install and doctor do not launch a model CLI')
    return dict(root_cause='hermetic_suite',
                prevented=install.returncode == 0 and doctor.returncode == 0 and live.returncode == 0 and not install_hits and not doctor_hits,
                install_launched=bool(install_hits), doctor_launched=bool(doctor_hits),
                hint='godfather doctor' in install.stdout, live_probes=probes, detected=True)


CASES = {
    'watch_cursor': watch_cursor,
    'orphan_watcher': orphan_watcher,
    'missing_stream': missing_stream,
    'index_only': index_only,
    'final_report': final_report,
    'hermetic_suite': hermetic_suite,
}


def stream_devices(_spec, directory, runs):
    import json
    results = [engine.policy.evaluate(json.loads(line), dict(cwd=str(runs)), policy={'rules': []}, cwd=str(runs))
               for line in (directory / 'commands.jsonl').read_text().splitlines()]
    return dict(prevented=all(r['verdict'] == 'allow' and r['targets'] == [] for r in results))

CASES['stream_devices'] = stream_devices


def pending_question(_spec, directory, runs):
    import json
    run = json.loads((directory / 'run.json').read_text())
    engine.safety.atomic(runs / (run['name'] + '.control.json'), {'questions': run['questions']})
    from unittest import mock
    with mock.patch('godfather_lifecycle.subprocess.Popen') as spawn:
        import godfather_lifecycle as life
        healed = life.request_heal(run, 'stall', '900 seconds without output')
    decision = engine.sup.evaluate(dict(started_at=engine.STARTED, pending_decision=True), engine.sup.load_policies(''), engine.STARTED + 9000)
    return dict(prevented=not healed and not spawn.called and decision['action'] is None)

CASES['pending_question'] = pending_question


def stale_input(_spec, directory, runs):
    import json
    run = json.loads((directory / 'run.json').read_text())
    (runs / (run['name'] + '.last.md')).write_text('Work completed and committed.\n')
    engine.safety.atomic(runs / (run['name'] + '.run.json'), run)
    result = engine.oversight.reconcile_run(run, runs, {})
    return dict(status=result['status'], prevented=result['status'] == 'done')

CASES['stale_input'] = stale_input


def committed_idle(_spec, directory, runs):
    from unittest import mock
    book = engine.stream.Book('grok', started_at=engine.STARTED)
    book.replay(directory / 'stream.jsonl', engine.STARTED, 1)
    run = dict(name='idle', cli='grok', cwd=str(runs))
    progress = mock.Mock(book=book)
    with mock.patch.object(engine.sup, 'git_head', return_value='new-commit'):
        early = engine.sup.finished_report(run, progress, runs / 'idle', 'old-commit', engine.STARTED + 2)
        done = engine.sup.finished_report(run, progress, runs / 'idle', 'old-commit', engine.STARTED + 60)
    return dict(prevented=early is None and done['report'] == 'Committed and tested.', commit=done['commit'])

CASES['committed_idle'] = committed_idle


def diagnostic_noise(_spec, directory, runs):
    import json
    levels = []
    path = Path(os.environ['XDG_CONFIG_HOME']) / 'godfather' / 'godfather.toml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[watch]\noptional_mcp = ["mcp.example.com"]\n')
    for row in map(json.loads, (directory / 'diagnostics.jsonl').read_text().splitlines()):
        progress = engine.oversight.Progress(dict(cli=row['cli'], started_at=engine.STARTED))
        progress.error(row['message'])
        levels.append(progress.events[0][0])
    return dict(levels=levels, prevented='ERROR' not in levels[:-1] and levels[-1] == 'ERROR')

CASES['diagnostic_noise'] = diagnostic_noise
