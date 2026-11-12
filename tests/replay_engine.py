"""Replay checked-in incident fixtures through the production mechanisms.

The clock is the fixture's. Nothing here calls a model, sleeps, or reads
~/.agent-runs/.
"""
import contextlib
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import agent_oversight as oversight
import godfather_permissions as perms
import godfather_policy as policy
import godfather_safety as safety
import godfather_store as store
import godfather_stream as stream
import godfather_supervisor as sup
from installer_fixture import copy_installer

STARTED = 1_000_000.0


def replay(directory, runs):
    spec = policy.document((Path(directory) / 'fixture.toml').read_text())
    sup._CACHE.clear()
    result = HANDLERS[spec['incident']](spec, Path(directory), Path(runs))
    if 'events' not in result:
        result['events'] = event_lines(runs)
    return result


def event_lines(runs):
    return [row['kind'] + ((' ' + row['detail']) if row['detail'] else '') for row in store.events_after(runs, 0)]


def saw_statuses(runs, run_id):
    """Run transitions only. A segment step shares run_id and is not a run status."""
    conn = store.connect(runs)
    try:
        rows = conn.execute(
            'SELECT to_status FROM transitions WHERE run_id=? AND segment_id IS NULL ORDER BY id',
            (run_id,)).fetchall()
    finally:
        conn.close()
    return [row['to_status'] for row in rows]


@contextlib.contextmanager
def scratch_home(runs):
    """A home outside /tmp and /var/folders, so scratch writability is the rule under test."""
    base = Path(pwd.getpwuid(os.getuid()).pw_dir) / 'Library' / 'Caches' / ('godfather-replay-' + runs.parent.name)
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    previous = os.environ.get('HOME')
    os.environ['HOME'] = str(base)
    try:
        yield base
    finally:
        if previous is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = previous
        shutil.rmtree(base, ignore_errors=True)


def open_run(runs, name, cli):
    store.insert_run(runs, name, name, 'running', cwd=str(runs), prompt='replay', started_at=STARTED)
    store.insert_segment(runs, name + '-seg', name, 'streaming', cli=cli, model='replay', started_at=STARTED)


def prevent(runs, name, root, detail):
    store.add_event(runs, name, 'PREVENTED', root + ': ' + detail, name=name)


def play_until_failure(book, data, started, step, rules):
    """Feed complete lines. Return the first classified failure and its clock."""
    at = float(started)
    found = why = decision = None
    classified = healthy = None
    for part in data.splitlines(keepends=True):
        if book.feed(part, at) and found is None:
            snap = sup.snapshot_from_book(book, at)
            failure, root = sup.classify(snap, rules, at)
            if failure:
                found, why = failure, root
                classified = at - started
                decision = sup.evaluate(snap, rules, at, {})
            else:
                healthy = at - started
        if part.endswith(b'\n'):
            at += step
    return dict(failure=found, root=why, decision=decision, classified=classified, healthy=healthy, at=at)


def apply_remedy(runs, name, decision, root):
    run = dict(run_id=name, name=name, cwd=str(runs), base=runs / name, segment_id=name + '-seg')
    decision = dict(decision, claim=decision['failure_class'] + '#1')
    sup.commit_action(run, decision, runs)
    store.transition(runs, name, 'retrying', root)
    return decision


def thinking(spec, directory, runs):
    name = 'grok-think-16m'
    open_run(runs, name, 'grok')
    rules = sup.load_policies('')
    book = stream.Book('grok', started_at=STARTED)
    played = play_until_failure(book, (directory / 'stream.jsonl').read_bytes(), STARTED, float(spec['clock']['step_secs']), rules)
    apply_remedy(runs, name, played['decision'], played['root'])
    return dict(failure_class=played['failure'], root_cause=played['root'], remedy=played['decision']['action'],
                classified_by_secs=played['classified'], healthy_through_secs=played['healthy'],
                saw_statuses=saw_statuses(runs, name), kill=played['decision']['kill'], prevented=False, detected=True)


def oversized(spec, directory, runs):
    name = 'argon-oversized-write'
    open_run(runs, name, 'agy')
    limit = int(spec['stream']['max_line_bytes'])
    book = stream.Book('agy', started_at=STARTED, max_line_bytes=limit, spill_path=str(runs / 'spill.bin'))
    played = play_until_failure(book, (directory / 'stream.jsonl').read_bytes(), STARTED, float(spec['clock']['step_secs']), sup.load_policies(''))
    pending = book.reader.pending and not book.saw_end
    book.reader.close()
    store.mark_broken_stream(runs, name + '-seg', 'oversized')
    apply_remedy(runs, name, played['decision'], played['root'])
    return dict(failure_class=played['failure'], root_cause=played['root'], remedy=played['decision']['action'],
                backoff_secs=played['decision']['backoff_secs'], classified_by_secs=played['classified'],
                saw_statuses=saw_statuses(runs, name), segment_status=store.segment_status(runs, name + '-seg'),
                partial_pending=pending, kill=played['decision']['kill'], prevented=False, detected=True)


def auth_blip(_spec, directory, runs):
    name = 'auth-blip'
    open_run(runs, name, 'grok')
    rules = sup.load_policies('')
    book = stream.Book('grok', started_at=STARTED)
    played = play_until_failure(book, (directory / 'stream.jsonl').read_bytes(), STARTED, 0, rules)
    apply_remedy(runs, name, played['decision'], played['root'])
    later = sup.evaluate(sup.snapshot_from_book(book, STARTED + 120), rules, STARTED + 120, sup.stored_attempts(runs, name))
    capped = sup.evaluate(sup.snapshot_from_book(book, STARTED + 1800), rules, STARTED + 1800, {})
    store.add_event(runs, name, 'ESCALATE', later['root_cause'], name=name)
    return dict(failure_class=played['failure'], root_cause=played['root'], remedy=played['decision']['action'],
                backoff_secs=played['decision']['backoff_secs'], classified_by_secs=played['classified'],
                saw_statuses=saw_statuses(runs, name), kill=played['decision']['kill'],
                assess_after_silence=book.assess(STARTED + 120)['failure_class'],
                later_action=later['action'], later_kill=later['kill'],
                segment_timeout_root=capped['root_cause'], prevented=False, detected=True)


def atomic_install(_spec, _directory, runs):
    env = dict(os.environ, GODFATHER_MCP_SDK='0', PREFIX=str(runs / 'prefix'))
    script = str(copy_installer(ROOT, runs / 'installer-source'))
    subprocess.run(['bash', script], env=env, cwd=str(ROOT), check=True, timeout=60)
    target = runs / 'prefix' / 'codex-run'
    fd = os.open(target, os.O_RDONLY)
    try:
        before = os.fstat(fd).st_ino
        subprocess.run(['bash', script], env=env, cwd=str(ROOT), check=True, timeout=60)
        kept = os.fstat(fd).st_ino == before and target.stat().st_ino != before
    finally:
        os.close(fd)
    future = runs / 'future.sqlite'
    conn = sqlite3.connect(future)
    conn.execute('PRAGMA user_version = 99')
    conn.commit()
    conn.close()
    inode = future.stat().st_ino
    os.environ['GODFATHER_DB'] = str(future)
    try:
        try:
            store.connect(runs)
            code = 0
        except store.SchemaVersion as exc:
            code = int(exc.code)
    finally:
        os.environ.pop('GODFATHER_DB', None)
    prevent(runs, 'atomic-install', 'atomic_install', 'running launcher kept its inode')
    return dict(root_cause='atomic_install', prevented=True, old_inode_kept=kept, schema_exit=code,
                schema_inode_kept=future.stat().st_ino == inode, detected=True)


def partial_segment(_spec, directory, runs):
    name = 'partial-segment'
    open_run(runs, name, 'grok')
    book = stream.Book('grok', started_at=STARTED)
    book.feed((directory / 'stream.jsonl').read_bytes(), STARTED)
    (runs / (name + '.last.md')).write_text('done early\n')
    status = store.machine_status(runs, name)['status']
    segment = store.segment_status(runs, name + '-seg')
    prefix_terminal = status in store.TERMINAL or segment == 'exited' or book.saw_end
    prevent(runs, name, 'partial_segment', 'a stream prefix is not terminal')
    store.commit_terminal(runs, name, name + '-seg', 'done', 'partial_segment', 0)
    return dict(root_cause='partial_segment', prevented=True, prefix_terminal=prefix_terminal,
                segment_before=segment, segment_status=store.segment_status(runs, name + '-seg'),
                saw_statuses=saw_statuses(runs, name), detected=True)


def codex_sandbox(_spec, _directory, runs):
    work = runs / 'work'
    work.mkdir()
    subprocess.run(['git', '-C', str(work), 'init', '-q'], check=True)
    git_status = subprocess.run(['git', '-C', str(work), 'status', '--porcelain'], capture_output=True).returncode
    run = dict(cli='codex', cwd=str(work), base=str(runs / 'codex'), task_type='edit')
    os.environ['GODFATHER_NATIVE'] = '1'
    try:
        profile = perms.compile_profile(run)
        perms.write_profile(runs / 'codex', run)
        command = perms.apply_native(['codex', 'exec', '-'], run, profile)
    finally:
        os.environ.pop('GODFATHER_NATIVE', None)
    rules = Path(str(runs / 'codex') + '.permissions.rules').read_text()
    prevent(runs, 'codex', 'codex_sandbox_ps_git', 'full access allows ps and worktree .git')
    return dict(root_cause='codex_sandbox_ps_git', prevented=True, sandbox=profile['sandbox'], git=profile['git'],
                nested_sandbox_exec=any('sandbox-exec' in part for part in command),
                allows_ps='/bin/ps' in profile['allow_bins'] and '/bin/ps' in rules,
                git_writable=profile['sandbox'] == 'danger-full-access',
                git_status=git_status, detected=True)


def help_only(_spec, _directory, runs):
    context = dict(cwd=str(runs), base=str(runs / 'task'), scratch=[])
    policy.config_dir().mkdir(parents=True, exist_ok=True)
    (policy.config_dir() / 'policy.toml').write_text('[[outward]]\nbinary = "acme"\ncommands = ["apps"]\n')
    effect = policy.evaluate(['acme', 'apps', 'deploy', '--help'], context)
    deploy = policy.evaluate(['acme', 'apps', 'deploy'], context)
    prevent(runs, 'deploy-help', 'help_only', 'help probe is not a deploy')
    return dict(root_cause='help_only', prevented=True, verdict=effect['verdict'], effect=effect['effect'],
                deploy_verdict=deploy['verdict'], shim_mode=perms.shim_mode(), detected=True)


def metadata_chmod(_spec, _directory, runs):
    with scratch_home(runs):
        script = Path.home() / '.grok' / 'long-running-background-tasks' / 'watch_index.sh'
        script.parent.mkdir(parents=True)
        script.write_text('#!/bin/sh\n')
        born = getattr(script.stat(), 'st_birthtime', script.stat().st_ctime)
        context = dict(cwd=str(runs), base=str(runs / 'task'), scratch=[], started_at=born - 5)
        effect = policy.evaluate(['chmod', '755', str(script)], context)
    prevent(runs, 'chmod', 'metadata_chmod', 'chmod is metadata on a file this run created')
    return dict(root_cause='metadata_chmod', prevented=True, verdict=effect['verdict'], effect=effect['effect'],
                not_delete=effect['effect'] != 'delete or overwrite files', detected=True)


def learned_rule(_spec, _directory, runs):
    with scratch_home(runs):
        return _learned_rule(runs)


def _learned_rule(runs):
    script = Path.home() / '.grok' / 'long-running-background-tasks' / 'watch_index.sh'
    other = Path.home() / '.grok' / 'other' / 'helper.sh'
    script.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    script.write_text('#!/bin/sh\n')
    other.write_text('#!/bin/sh\n')
    base = runs / 'task'
    context = dict(cwd=str(runs), base=str(base), scratch=[], started_at=time_after_birth())
    safety.atomic(str(base) + '.run.json', dict(name='task', cli='grok', status='running', cwd=str(runs),
                                                 started_at=context['started_at'], launcher_pid=1))
    safety.atomic(str(base) + '.control.json', dict(questions=[dict(
        id='q', answer=None, options='allow|deny|allow+remember', question='chmod?',
        effect=dict(command=['chmod', '+x', str(script)], targets=[str(script.resolve())], cwd=str(runs)))]))
    import io
    import contextlib
    from types import SimpleNamespace
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        safety.answer(SimpleNamespace(name='task', answer='allow+remember (indexer under grok scratch)', remember=False))
    shown = err.getvalue()
    other_effect = policy.evaluate(['chmod', '+x', str(other)], context)
    flags = policy.evaluate(['chmod', '-R', '+x', str(other)], context)
    prevent(runs, 'task', 'learned_rule', 'same binary and flags under the CLI tree')
    return dict(root_cause='learned_rule', prevented=True, rule_args=['+x'], covers_tree='.grok' in shown and '/**' in shown,
                other_verdict=other_effect['verdict'], other_flags_verdict=flags['verdict'],
                summary_visible='chmod +x under' in shown and 'Learned rule' in shown, detected=True)


def time_after_birth():
    return 9_999_999_999.0


def verdict_answer(_spec, _directory, runs):
    base = runs / 'task'
    safety.atomic(str(base) + '.run.json', dict(name='task', cli='grok', status='running', cwd=str(runs), launcher_pid=1))
    safety.atomic(str(base) + '.control.json', dict(questions=[dict(
        id='q', answer=None, options='allow|deny|allow+remember', question='approve?',
        effect=dict(command=['rm', '/external/file'], targets=['/external/file'], cwd=str(runs)))]))
    from types import SimpleNamespace
    args = SimpleNamespace(name='task', answer='allowish', remember=False)
    try:
        safety.answer(args)
        rejected = False
    except ValueError as exc:
        rejected = 'unrecognized' in str(exc)
    unanswered = safety.control(base)['questions'][-1].get('answer') is None
    args.answer = 'allow (the file is ours)'
    safety.answer(args)
    saved = safety.control(base)['questions'][-1]
    import json
    records = [json.loads(line) for line in (policy.state_dir() / 'incidents.jsonl').read_text().splitlines()]
    last = records[-1]
    prevent(runs, 'task', 'verdict_answer', 'a reason after allow is not a deny')
    return dict(root_cause='verdict_answer', prevented=True, rejected=rejected and unanswered,
                stored_answer=saved['answer'], reason=saved.get('reason'), not_deny=last.get('answer') != 'deny',
                incident_reason=last.get('reason'), detected=True)


def inflight(spec, directory, runs):
    name = 'inflight-sleep'
    open_run(runs, name, 'grok')
    book = stream.Book('grok', started_at=STARTED)
    book.replay(directory / 'stream.jsonl', STARTED, 0)
    rules = sup.load_policies('')
    early, _early_root = sup.classify(sup.snapshot_from_book(book, STARTED + 120), rules, STARTED + 120)
    late = sup.classify(sup.snapshot_from_book(book, STARTED + 601), rules, STARTED + 601)
    busy = sup.classify(sup.snapshot_from_book(book, STARTED + 601, children_cpu=True), rules, STARTED + 601)
    cfg = Path(os.environ['XDG_CONFIG_HOME']) / 'godfather' / 'godfather.toml'
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[healing]\nenabled = true\n[[retry]]\nclass = "crash"\nattempts = 1\nbackoff_secs = 0\naction = "restart"\n')
    sup._CACHE.clear()
    rows = [dict(row) for row in spec['ps']]
    progress = mock.Mock(book=book)
    run = dict(name=name, run_id=name, pid=10, cli='grok', cwd=str(runs))
    with mock.patch.object(sup, '_ps_rows', return_value=rows):
        signal = sup.refuse_signal(run, progress)
        with mock.patch('godfather_lifecycle.request_heal') as heal:
            sup.schedule(run, progress, {})
        heal_called = heal.called
    prevent(runs, name, 'inflight_tool', 'live children and a declared wait are not a stall')
    return dict(root_cause='inflight_tool', prevented=True, failure_class='' if early is None else early,
                kill=False, heal_called=heal_called, signal=signal or '',
                declared_wait_secs=sup.declared_wait(book.beats[-1]['detail']),
                healthy_at_secs=120, late_failure_class=late[0], late_root_cause=late[1],
                late_with_cpu='' if busy[0] is None else busy[0], saw_statuses=saw_statuses(runs, name), detected=True)


HANDLERS = {
    'thinking_without_tool': thinking,
    'oversized_write': oversized,
    'auth_blip': auth_blip,
    'atomic_install': atomic_install,
    'partial_segment': partial_segment,
    'codex_sandbox': codex_sandbox,
    'help_only': help_only,
    'metadata_chmod': metadata_chmod,
    'learned_rule': learned_rule,
    'verdict_answer': verdict_answer,
    'inflight_tool': inflight,
}


def _load_cases():
    import replay_cases
    HANDLERS.update(replay_cases.CASES)


_load_cases()
