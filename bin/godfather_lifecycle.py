"""Persistent dependencies, watchdog remediation, and incident-driven improvement."""
import argparse
import collections
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

import agent_oversight as a
import godfather_safety as s
from godfather_policy import config_dir, document, incident, state_dir
from godfather_store import NONTERMINAL, TERMINAL

HEALING = dict(enabled=True, attempts=1, stall_secs=120, hard_limit_secs=900,
               auth_retry_secs=30, auth_retries=1)
IGNORE = ['not logged into Antigravity (silent', 'cs path', 'GeminiDir', 'refresh cache in background',
          'refreshing cache in background', 'git info for workspace', 'recordTrajectoryAnalytics',
          'in-loop tool exit', 'tool exited with code', 'tool failed (']

ACTIVE = NONTERMINAL + ('healing',)


def settings():
    path = config_dir() / 'godfather.toml'
    data = document(path.read_text()) if path.exists() else {}
    healing = dict(HEALING, **data.get('healing', {}))
    if set(healing) - set(HEALING):
        raise ValueError('unknown healing setting')
    for key in set(HEALING) - {'enabled'}:
        if not isinstance(healing[key], (int, float)) or healing[key] < 0:
            raise ValueError('healing thresholds must be nonnegative')
    return healing, IGNORE + data.get('watch', {}).get('ignore', [])


def queue_path(name):
    s.resolve_name(name)  # validate before building paths
    return state_dir() / 'queue' / (name + '.json')


def detached_popen(command, **kwargs):
    """No terminal, inherited input pipe, or hangup disposition in durable work.

    Ignore HUP *before* spawning: unlike a handler installed after exec, this
    also covers the exec/startup window and survives subsequent execs.
    TERM/INT keep their normal cancellation semantics for godfather stop.
    """
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        return subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                start_new_session=True, **kwargs)
    finally:
        signal.signal(signal.SIGHUP, previous)


def launcher_process(command, **kwargs):
    # The worker sets its own PID before exec; children cannot accidentally
    # inherit the exemption from the direct launcher's attachment wrapper.
    return detached_popen([sys.executable, str(Path(__file__).resolve()), 'worker'] +
                          list(command), **kwargs)


def attached_run(argv, name):
    return attached_command([str(Path(__file__).with_name('godfather'))] + list(argv),
                            name, dict(s.original_environment(), GODFATHER_DETACHED_RUN='1'))


def attached_command(command, name, env):
    """The durable worker owns a session; this expendable client tails files."""
    root = a.runs_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    s.resolve_name(name)
    prefix = root / ('.' + name + '.attach-' + uuid.uuid4().hex)
    paths = [Path(str(prefix) + suffix) for suffix in ('.out', '.err')]
    status = Path(str(prefix) + '.owner')
    with paths[0].open('wb') as out, paths[1].open('wb') as err:
        # setsid alone leaves the worker in the conductor's descendant tree.
        # The bootstrap forks and exits; its durable owner is reparented to
        # init/launchd, beyond a desktop tool session's descendant cleanup.
        child = detached_popen([sys.executable, str(Path(__file__).resolve()), 'orphan',
                                str(prefix), str(os.getpid())] + list(command),
                               env=env, stdout=out, stderr=err)
    def detach(_sig, _frame):
        raise SystemExit(0)
    handlers = {sig: signal.signal(sig, detach) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    readers = [path.open('rb') for path in paths]
    startup_deadline = time.monotonic() + 10
    try:
        while True:
            boot_rc = child.poll()  # Reap the short-lived bootstrap.
            owner = a.read(status)
            rc = owner.get('exit_code')
            if rc is None and owner and not a.alive(owner['pid'], owner['identity']):
                # Re-read after exit: the first read can predate publication.
                rc = a.read(status).get('exit_code', 1)
            if rc is None and not owner and boot_rc is not None and (boot_rc or time.monotonic() > startup_deadline):
                rc = boot_rc or 1
            for reader, stream in zip(readers, (sys.stdout, sys.stderr)):
                data = reader.read()
                if data:
                    stream.buffer.write(data)
                    stream.flush()
            if rc is not None:
                return rc
            time.sleep(.05)
    except BrokenPipeError:
        # Losing the attached reader must never propagate to the router or CLI.
        for stream in (sys.stdout, sys.stderr):
            try:
                os.dup2(os.open(os.devnull, os.O_WRONLY), stream.fileno())
            except OSError:
                pass
        return 0
    finally:
        for reader in readers:
            reader.close()
        for path in paths + [status]:
            path.unlink(missing_ok=True)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def orphan_owner(prefix, client, command):
    """Own durable work after the bootstrap exits, including its exit status."""
    client_identity = a.processes().get(client, {}).get('identity')
    if os.fork():
        return 0
    os.setsid()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    status = Path(str(prefix) + '.owner')
    owner = dict(pid=os.getpid(), identity=a.processes()[os.getpid()]['identity'], exit_code=None)
    if a.alive(client, client_identity):
        a.write(status, owner)
    rc = 1
    try:
        child = launcher_process(command, env=s.original_environment(),
                                 stdout=sys.stdout, stderr=sys.stderr)
        rc = child.wait()
        rc = 128 - rc if rc < 0 else rc
    finally:
        if a.alive(client, client_identity):
            owner['exit_code'] = rc
            a.write(status, owner)
        else:
            for suffix in ('.out', '.err', '.owner'):
                Path(str(prefix) + suffix).unlink(missing_ok=True)
    # The forked owner must not run the bootstrap's Python teardown twice.
    os._exit(rc)


def logical_status(name, include_healing=True):
    base = a.runs_dir().resolve() / name
    if not s.read(str(base) + '.run.json') and not s.read(str(base) + '.route.json'):
        from godfather_store import plan_task
        task = plan_task(base.parent, name)
        if task:
            return task['status']
    active = s.resolve_name(name)
    run = s.read(str(active) + '.run.json')
    if run:
        run = a.decision_state(a.reconcile_run(run, base.parent), base.parent)
        if run.get('status') == 'needs_input':
            return 'needs_input'
        if run.get('status') == 'failed' and run.get('failure_class') in ('incomplete', 'pending_question'):
            return 'failed'
    queued = s.read(queue_path(name))
    if queued.get('kind') != 'quota' and queued.get('status') in ('queued', 'launching'):
        return 'queued'
    if queued.get('kind') != 'quota' and queued.get('status') in ('dependency_failed', 'stopped'):
        return queued['status']
    nudge_base = s.resolve_name(name)
    nudge = s.read(str(nudge_base) + '.nudge.json')
    if nudge.get('pending') and a.alive(nudge.get('pid'), nudge.get('identity')):
        return 'running'
    healing = s.read(str(base) + '.healing.json')
    if include_healing and healing.get('pending'):
        if a.alive(healing.get('pid'), healing.get('identity')):
            return 'healing'
    route = s.read(str(base) + '.route.json')
    job = s.read(str(base) + '.job.json')
    if job and job.get('run_id') != route.get('run_id') and (not route or job.get('started_at', 0) >= a.epoch(route.get('started_at'))):
        return 'queued' if a.alive(job.get('pid'), job.get('identity')) else 'failed'
    if queued.get('kind') != 'quota' and queued.get('status') == 'running' and (not route or
            (route.get('run_id') != queued.get('run_id') and a.epoch(route.get('started_at')) < queued.get('created', 0))):
        return 'queued' if a.alive(queued.get('pid'), queued.get('identity')) else 'failed'
    if route.get('status') == 'running':
        if a.alive(route.get('router_pid'), route.get('router_identity')):
            return 'running'
    active = a.runs_dir() / (route.get('active_name') or name)
    run = s.read(str(active) + '.run.json')
    if not run:
        return route.get('result', 'failed') if route else 'unknown'
    run = a.reconcile_run(run, base.parent)
    if run.get('status') in TERMINAL:
        return run['status']
    if any(q.get('answer') is None for q in run.get('questions', [])):
        return 'needs_input'
    if route.get('run_id'):
        for path in base.parent.glob(name + '#*.run.json'):
            segment = s.read(path)
            if segment.get('router_id') == route['run_id'] and segment.get('status') in ACTIVE:
                if a.run_alive(segment) or a.alive(segment.get('launcher_pid'), segment.get('launcher_identity')):
                    return 'running'
    if run.get('status') in NONTERMINAL and run.get('status') not in ('queued', 'needs_input', 'waiting_quota') and not a.owners_alive(run, root=base.parent):
        # finish() publishes before the launcher exits. A snapshot taken just
        # before that publication can outlive both owners; don't report a
        # completed detached run as dead from that stale snapshot.
        current = s.read(str(active) + '.run.json')
        if current.get('run_id') == run.get('run_id') and current.get('status') in TERMINAL:
            return current['status']
        return 'died'
    return run['status']


def stop_run(name):
    """Stop this name's verified owners and descendants, including detached workers."""
    base = s.resolve_name(name)
    logical = a.runs_dir().resolve() / name
    run = s.read(str(base) + '.run.json')
    route = s.read(str(logical) + '.route.json')
    job = s.read(queue_path(name))
    nudge = s.read(str(base) + '.nudge.json')
    healing = s.read(str(logical) + '.healing.json')
    if not run and not route and not job:
        raise ValueError('run is not registered: ' + name)
    from godfather_codex_bridge import control
    with contextlib.suppress(OSError, ValueError):
        control(base, 'turn/interrupt')
    procs = a.processes()
    roots = []
    for data, key, identity in ((run, 'pid', 'pid_identity'), (run, 'launcher_pid', 'launcher_identity'),
                                (run, 'supervisor_pid', 'supervisor_identity'),
                                (route, 'router_pid', 'router_identity'), (job, 'pid', 'identity'),
                                (nudge, 'pid', 'identity'), (healing, 'pid', 'identity')):
        pid = data.get(key)
        verified = data.get(identity) or (key == 'supervisor_pid' and
                   procs.get(pid, {}).get('ppid') == run.get('launcher_pid') and
                   a.alive(run.get('launcher_pid'), run.get('launcher_identity'), procs))
        if pid and verified and pid != os.getpid() and a.alive(pid, data.get(identity), procs):
            roots.append(pid)
    sessions = {pid for pid in roots if procs[pid].get('sid') == pid}
    if a.session_members(run, procs):
        sessions.add(run['process_session_id'])
    if os.getsid(0) in sessions:
        raise ValueError('refusing to stop the caller session')
    owned = {}
    def persist(writer, *args):
        # JSON is published before the SQLite mirror. A corrupt/stale mirror
        # must never prevent the emergency process-tree stop.
        import sqlite3
        from godfather_store import IllegalTransition, SchemaVersion
        try:
            writer(*args)
        except (sqlite3.Error, IllegalTransition, SchemaVersion) as exc:
            print('stop: registry mirror unavailable: ' + str(exc), file=sys.stderr)
    def refresh():
        snapshot = a.processes()
        selected = {pid for pid in roots if a.alive(pid, procs[pid]['identity'], snapshot)}
        selected.update(pid for pid, row in snapshot.items() if row.get('sid') in sessions)
        selected.update(pid for pid, identity in owned.items() if a.alive(pid, identity, snapshot))
        while True:
            more = {pid for pid, row in snapshot.items() if row['ppid'] in selected} - selected
            if not more:
                break
            selected.update(more)
        if os.getpid() in selected or any(snapshot[pid].get('pgid') == os.getpgrp() for pid in selected):
            raise ValueError('refusing to stop the caller process group')
        owned.update((pid, snapshot[pid]['identity']) for pid in selected)
        return snapshot
    refresh()
    if run:
        persist(s.control, base, lambda data: data.update(stop_requested=time.time()))
    for record, path in ((nudge, str(base) + '.nudge.json'), (healing, str(logical) + '.healing.json')):
        if record:
            pending_nudge = path == str(base) + '.nudge.json' and record.get('pending')
            record.update(pending=False, cancelled=True)
            if pending_nudge:
                persist(expire_nudge, base, record, dict(run, status='stopped'))
            else:
                persist(s.atomic, path, record)
    if job:
        job.update(status='stopped', ended_at=time.time())
        persist(s.atomic, queue_path(name), job)
    for sig, grace in ((signal.SIGTERM, .5), (signal.SIGKILL, .5)):
        deadline = time.monotonic() + grace
        while True:
            snapshot = refresh()
            living = {pid for pid, identity in owned.items() if a.alive(pid, identity, snapshot)}
            if not living:
                break
            # Every group here belongs to a verified private run session.
            groups = {snapshot[pid]['pgid'] for pid in living if snapshot[pid].get('sid') in sessions}
            for group in groups:
                if group <= 1 or group == os.getpgrp():
                    raise ValueError('refusing to signal an unsafe process group')
                try:
                    os.killpg(group, sig)
                except ProcessLookupError:
                    pass
            for pid in sorted(living, reverse=True):
                if a.alive(pid, owned[pid]):
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            if time.monotonic() >= deadline:
                break
            time.sleep(.05)
    snapshot = refresh()
    if any(a.alive(pid, identity, snapshot) for pid, identity in owned.items()):
        raise ValueError('run processes survived stop; terminal state not recorded')
    if run:
        run = s.read(str(base) + '.run.json')
        run.update(status='stopped', exit_code=143, ended_at=a.now())
        persist(s.registry_write, str(base) + '.run.json', run)
    if route:
        route.update(status='stopped', result='stopped', ended_at=a.now())
        persist(s.atomic, str(logical) + '.route.json', route)
    persist(s.event, logical, 'DONE', 'stopped')
    print(name + ' stopped', flush=True)
    return 0


def wait_run(name, timeout=None):
    if timeout is not None and timeout < 0:
        raise ValueError('--timeout must be nonnegative')
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        ensure_daemon()
        status = logical_status(name)
        if status == 'needs_input' or status not in ACTIVE:
            return status, False
        if deadline is not None and time.monotonic() >= deadline:
            return status, True
        time.sleep(.1)


def enqueue(args):
    after = args.after or args.after_any
    dependencies = [name.strip() for name in after.split(',')]
    dependency_timeout = getattr(args, 'dependency_timeout', 300)
    if dependency_timeout <= 0:
        raise ValueError('--dependency-timeout must be positive')
    if args.name in dependencies:
        raise ValueError('a run cannot depend on itself')
    for name in dependencies:
        s.resolve_name(name)  # pending names are allowed, malformed names are not
    # Explicit isolation must refuse an attached devbox before enqueue.
    # Defaults may adopt it; route() rechecks the attachment at launch too.
    if getattr(args, 'isolate', True):
        from godfather_router import _inside_git, check_devbox_isolation
        cwd = os.getenv('CWD')
        if not cwd or not Path(cwd).is_dir():
            raise ValueError('CWD must name an existing task directory')
        if _inside_git(cwd):
            check_devbox_isolation(cwd, explicit=getattr(args, 'isolate_explicit', False))
    path = queue_path(args.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = s.read(path)
        if prior.get('status') in ('queued', 'launching', 'running') or logical_status(args.name) in ACTIVE:
            raise ValueError('Run name already active: ' + args.name)
        # Persist the prompt itself; a brief in /tmp may disappear before launch.
        argv = ['run', '-p', args.prompt if args.prompt is not None else Path(args.prompt_file).read_text()]
        if not getattr(args, 'isolate', True):
            argv.append('--no-isolate')
        elif getattr(args, 'isolate_explicit', False):
            argv.append('--isolate')
        if getattr(args, 'container', False):
            argv.append('--container')
        for key in ('self', 'review_fix', 'wait_lock'):
            if getattr(args, key):
                argv += ['--' + key.replace('_', '-')]
        for key in ('check', 'review_by', 'max_fix_rounds', 'review_rounds', 'review_threshold'):
            value = getattr(args, key, None)
            if value is not None:
                argv += ['--' + key.replace('_', '-'), str(value)]
        if getattr(args, 'task_type', 'edit') not in (None, '', 'edit'):
            argv += ['--task-type', args.task_type]
        argv += [args.name]
        env = {k: v for k, v in s.original_environment().items() if k in ('HOME', 'PATH', 'TMPDIR', 'CWD', 'AGENT_RUNS_DIR', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'CODEX_BIN', 'CODEX_HOME', 'GROK_HOME', 'ADD_DIRS', 'GODFATHER_MCP_SDK', 'PYTHONDONTWRITEBYTECODE')}
        created = time.time()
        bindings = {}
        for name in dependencies:
            identity, _ = dependency_instance(name)
            bindings[name] = identity if logical_status(name) in ACTIVE else None
        s.atomic(path, dict(name=args.name, run_id=str(uuid.uuid4()), status='queued', dependencies=dependencies,
                            dependency_instances=bindings,
                            any_status=bool(args.after_any), argv=argv, env=env, created=created,
                            dependency_timeout=dependency_timeout))
    root = a.runs_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    s.event(root / args.name, 'QUEUED', 'after ' + ','.join(dependencies))
    print('QUEUED ' + args.name + ' after ' + ','.join(dependencies), flush=True)
    ensure_daemon()
    return 0


def ensure_daemon():
    root = state_dir() / 'queue'
    if os.getenv('GODFATHER_QUEUE_DAEMON') == '1' or not root.exists():
        return
    if not any(s.read(p).get('status') in ('queued', 'launching', 'running') for p in root.glob('*.json')):
        return
    with (root / '.daemon.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
    with (state_dir() / 'daemon.log').open('a') as out:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'daemon'],
                         env=s.original_environment(), stdout=out, stderr=out, start_new_session=True)


def dependency_instance(name):
    """The logical generation owns all of its fallback segments."""
    from godfather_store import epoch
    base = a.runs_dir() / name
    route = s.read(str(base) + '.route.json')
    run = s.read(str(base) + '.run.json')
    queued = s.read(queue_path(name))
    reservation = s.read(str(base) + '.job.json')
    latest = max(epoch(route.get('started_at')) or 0, epoch(run.get('started_at')) or 0)
    if queued and queued.get('run_id') and (queued['created'] >= latest or queued['run_id'] == route.get('run_id')):
        return queued['run_id'], queued['created']
    if reservation and (epoch(reservation.get('started_at')) or 0) > latest:
        return reservation['run_id'], epoch(reservation.get('started_at'))
    # A restarted router publishes before its first segment replaces run.json.
    # Its active status must bind to that new generation, not the old segment.
    if route and (not run or run.get('router_id') == route.get('run_id') or
                  (epoch(route.get('started_at')) or 0) >= (epoch(run.get('started_at')) or 0)):
        record = route
    else:
        record = run
    return record.get('run_id') or record.get('started_at'), epoch(record.get('started_at'))


def dependency_gate(job, now=None):
    now = time.time() if now is None else now
    statuses = {}
    bindings = job.setdefault('dependency_instances', {})
    for name in job['dependencies']:
        identity, started = dependency_instance(name)
        status = logical_status(name)
        # Queues persisted by older launchers may lack an enqueue-time binding.
        # An active generation is eligible even if it predates this child;
        # completed older generations must still wait for a fresh registration.
        if not bindings.get(name) and (not identity or
                ((started is None or started < job['created']) and status not in ACTIVE)):
            statuses[name] = 'unknown'
            continue
        if bindings.get(name) and bindings[name] != identity:
            return 'dependency_failed', 'bound dependency instance was replaced: ' + name
        if not bindings.get(name):
            bindings[name] = identity
            s.atomic(queue_path(job['name']), job)
        statuses[name] = status
    missing = [name for name, status in statuses.items() if status == 'unknown']
    if missing:
        if now - job['created'] >= job.get('dependency_timeout', 300):
            return 'dependency_failed', 'dependency registration timed out: ' + ', '.join(missing)
        return 'waiting', 'waiting for dependency registration: ' + ', '.join(missing)
    if not job['any_status']:
        from godfather_registry import completion_failure
        for name, status in statuses.items():
            base = s.resolve_name(name)
            run = s.read(str(base) + '.run.json')
            if status in ACTIVE and not run.get('ended_at'):
                continue
            failure, reason = completion_failure(base, s.read(str(base) + '.control.json').get('questions', run.get('questions', [])))
            if failure:
                detail = 'after ' + name + ': ' + reason
                if job.get('error') != detail:
                    job['error'] = detail
                    s.atomic(queue_path(job['name']), job)
                return 'waiting', detail
    if any(status in ACTIVE for status in statuses.values()):
        return 'waiting', 'dependencies still active'
    if not job['any_status'] and any(status != 'done' for status in statuses.values()):
        return 'dependency_failed', 'dependency did not succeed'
    return 'ready', 'dependencies finished'


def daemon():
    root = state_dir() / 'queue'
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.daemon.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        os.environ['GODFATHER_QUEUE_DAEMON'] = '1'
        children = {}
        while True:
            pending = False
            for path in sorted(root.glob('*.json')):
                job = s.read(path)
                if job['status'] in ('launching', 'running'):
                    child = children.get(job['name'])
                    if child:
                        child.poll()
                    if a.alive(job.get('pid'), job.get('identity')):
                        pending = True
                        continue
                    if job.get('kind') == 'quota' and quota_ready(job) in ('waiting', 'ready'):
                        # No session was started; a dead daemon/worker may safely reclaim the wait.
                        job['status'] = 'queued'
                        s.atomic(path, job)
                        pending = True
                        continue
                    status = logical_status(job['name']) if job['status'] == 'running' else 'failed'
                    # No relaunch after a crash during process creation: avoid duplicate work.
                    job['status'] = status if status not in ACTIVE else 'failed'
                    s.atomic(path, job)
                if job['status'] != 'queued':
                    continue
                if job.get('kind') == 'quota':
                    gate = quota_ready(job)
                    detail = 'quota reset reached'
                    if gate == 'cancelled':
                        job['status'] = 'stopped'
                        s.atomic(path, job)
                        continue
                else:
                    gate, detail = dependency_gate(job)
                pending = True
                if gate == 'waiting':
                    continue
                if gate == 'dependency_failed':
                    job.update(status=gate, error=detail)
                    s.atomic(path, job)
                    s.event(a.runs_dir() / job['name'], 'DONE', 'dependency_failed: ' + detail)
                    continue
                job['status'] = 'launching'
                s.atomic(path, job)
                with (a.runs_dir() / (job['name'] + '.queue.log')).open('a') as out:
                    command = ([sys.executable, str(Path(__file__).resolve()), 'quota', job['base']]
                               if job.get('kind') == 'quota' else [str(Path(__file__).with_name('godfather'))] + job['argv'])
                    child = subprocess.Popen(command,
                                             env=dict(job.get('env') or s.original_environment(), GODFATHER_QUEUE_DAEMON='1',
                                                      GODFATHER_ROUTE_ID=job.get('run_id', '')), stdout=out, stderr=out, start_new_session=True)
                children[job['name']] = child
                job.update(status='running', pid=child.pid, identity=a.processes().get(child.pid, {}).get('identity'))
                s.atomic(path, job)
                s.event(a.runs_dir() / job['name'], 'START', 'dependencies finished')
            if not pending:
                return 0
            time.sleep(.2)


def schedule_quota(base, run, resets_at=None, now=None, max_wait=None):
    """Persist a delayed exact-session resume in the existing queue registry."""
    import math
    import godfather_errors as errors
    now = time.time() if now is None else now
    from godfather_supervisor import pending_question
    if pending_question(run):
        return False
    maximum = float(os.getenv('GODFATHER_QUOTA_MAX_WAIT', '21600') if max_wait is None else max_wait)
    if not math.isfinite(maximum) or maximum < 0:
        raise ValueError('GODFATHER_QUOTA_MAX_WAIT must be finite nonnegative seconds')
    resets_at = resets_at or errors.reset_at(run.get('rate_limits'), now) or errors.reset_at(run.get('failure'), now)
    if not resets_at and run.get('cli') == 'agy':
        from godfather_usage import state_dir as usage_state
        from godfather_router import agy_limits
        snapshot = s.read(usage_state() / 'agy-usage.json')
        if snapshot.get('text'):
            _, _, blocked = agy_limits(snapshot['text'], run.get('model'), 0)
            resets_at = errors.reset_at(blocked or '', now)
    session = run.get('conversation_id') if run.get('cli') == 'agy' else run.get('session_id')
    if not session or not resets_at or resets_at <= now or resets_at + 60 - now > maximum:
        return False
    logical = run.get('router_name') or base.name
    resume_at = resets_at + 60
    job = dict(name=logical, kind='quota', status='queued', base=str(base),
               run_id=run.get('run_id'), resume_at=resume_at, created=now, max_wait=maximum)
    path = queue_path(logical)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish the durable job first. The daemon uses the same short lock so it
    # cannot cancel that job while the run is still being published.
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        s.atomic(path, job)
        run.update(status='waiting_quota', resume_at=resume_at, ended_at=None, exit_code=None)
        s.registry_write(str(base) + '.run.json', run)
        if run.get('stop_requested') or run.get('status') == 'stopped':
            s.atomic(path, dict(job, status='stopped'))
            return False
    s.event(base, 'QUOTA_WAIT', 'waiting for quota, resumes ' + time.strftime('%H:%M', time.localtime(resume_at)))
    ensure_daemon()
    return True


def quota_ready(job, now=None):
    with open(str(queue_path(job['name'])) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = s.read(job['base'] + '.run.json')
        if current.get('run_id') != job.get('run_id') or current.get('stop_requested') or current.get('status') == 'stopped':
            return 'cancelled'
        if current.get('status') != 'waiting_quota':
            return 'cancelled'
        return 'ready' if (time.time() if now is None else now) >= job['resume_at'] else 'waiting'


def execute_quota(base):
    run = s.read(str(base) + '.run.json')
    logical = run.get('router_name') or base.name
    job = s.read(queue_path(logical))
    if quota_ready(job) != 'ready':
        return 0
    # The scheduling router/launcher must have exited before its session is reused.
    while a.owners_alive(run):
        time.sleep(.1)
        run = s.read(str(base) + '.run.json')
        if quota_ready(job) != 'ready':
            return 0
    path = str(base) + '.nudge.json'
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        request = s.read(path)
        message = 'Quota has reset. Continue the task in this same session; verify existing work first.'
        if request.get('pending') and request.get('run_id') == run.get('run_id'):
            message += '\n' + request['message']
            request.update(delivered_at=time.time())
            s.atomic(path, request)
    run.update(status='retrying', resume_at=None)
    s.registry_write(str(base) + '.run.json', run)
    s.event(base, 'QUOTA_RESUME', 'resuming the saved session')
    try:
        os.environ.pop('GODFATHER_LOCK_HELD', None)
        rc = a.nudge_main([base.name, message], expected_instance=run.get('run_id'))
    except (Exception, SystemExit) as exc:
        s.event(base, 'ESCALATE', 'quota resume failed: ' + str(exc))
        rc = 2
    if request.get('pending') and request.get('run_id') == run.get('run_id'):
        with open(path + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            latest = s.read(path)
            if latest.get('id') == request.get('id') and latest.get('pending'):
                latest.update(pending=False, exit_code=rc, finished=time.time())
                s.atomic(path, latest)
    current = s.read(str(base) + '.run.json')
    if current.get('status') == 'quota' and not current.get('stop_requested'):
        schedule_quota(base, current, max_wait=job.get('max_wait'))
    elif rc and current.get('status') == 'retrying':
        current.update(status='failed', exit_code=rc, ended_at=a.now())
        s.registry_write(str(base) + '.run.json', current)
    route_path = str(a.runs_dir() / logical) + '.route.json'
    route = s.read(route_path)
    current = s.read(str(base) + '.run.json')
    if route and route.get('active_name') == base.name:
        waiting = current.get('status') == 'waiting_quota'
        route.update(status='waiting_quota' if waiting else 'finished', result=current.get('status'),
                     resume_at=current.get('resume_at'), ended_at=a.now())
        s.atomic(route_path, route)
    return rc


def deliver_nudge(name, message, remember=False):
    base = s.resolve_name(name)
    run = s.read(str(base) + '.run.json')
    if not run:
        raise ValueError('run is not registered')
    from godfather_supervisor import pending_question
    if pending_question(run):
        raise ValueError('run has a pending question; answer it instead of nudging')
    run = a.discover(run)
    if not (run.get('conversation_id') if run['cli'] == 'agy' else run.get('session_id')):
        raise ValueError('session is not known yet; run left untouched')
    from godfather_codex_bridge import control
    try:
        control(base, 'turn/steer', message)
    except Exception:
        # A live control attempt is best effort; preserve the nudge on any failure.
        pass
    else:
        s.event(base, 'NUDGE', 'delivered through turn/steer')
        return 0
    path = str(base) + '.nudge.json'
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = s.read(path)
        if prior.get('pending') and (run.get('status') == 'waiting_quota' or a.alive(prior.get('pid'), prior.get('identity'))):
            raise ValueError('a nudge is already pending')
        if remember:
            s.add_lesson(run.get('project_cwd') or run.get('launcher_env', {}).get('GODFATHER_PROJECT_CWD') or run['cwd'], message, base.parent)
        reopen = run.get('status') not in ACTIVE
        request = dict(id=uuid.uuid4().hex, pending=True, message=message, requested=time.time(),
                       run_id=run.get('run_id'), stop_requested=run.get('stop_requested'),
                       reopen=reopen, queued_status=run.get('status'))
        s.atomic(path, request)
        if run.get('status') == 'waiting_quota':
            s.event(base, 'NUDGE', 'queued for delivery at the quota reset')
            return 0
        with open(str(base) + '.nudge.log', 'ab') as out:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'nudge', str(base)],
                                     env=s.original_environment(), stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=out, start_new_session=True)
        request.update(pid=child.pid, identity=a.processes().get(child.pid, {}).get('identity'))
        s.atomic(path, request)
        if run.get('status') not in ACTIVE:
            run.update(status='retrying', nudge_pending=request['id'])
            s.registry_write(str(base) + '.run.json', run)
    if reopen:
        s.event(base, 'NUDGE', 'queued; reopens the ' + str(run.get('status')) + ' run')
        print('NUDGE ' + name + ' queued', flush=True)
    else:
        s.event(base, 'NUDGE', 'queued for delivery when the live segment exits; reopens on completion; '
                'expires only on a user stop or replaced run instance')
        print('NUDGE ' + name + ' queued; reopens on completion; expires only on a user stop '
              'or replaced run instance', flush=True)
    return 0


def expire_nudge(base, request, current, reason=None):
    """Keep an audit trail when a stop or replacement invalidates a correction."""
    status = current.get('status')
    reason = reason or 'run ended ' + str(status) + ' by user stop before delivery; nudge again to reopen it'
    request.update(pending=False, expired=True, reason=reason, finished=time.time(), exit_code=0)
    path = str(base) + '.nudge.json'
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if s.read(path).get('id') == request.get('id'):
            s.atomic(path, request)
    requested = request.get('requested')
    queued_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(requested)) if requested else 'unknown'
    with open(str(base) + '.models.log', 'a') as f:
        f.write('[{}] NUDGE_EXPIRED status={} queued_at={} message={}\n'.format(
            a.now(), status, queued_at, json.dumps(request.get('message'))))
    s.event(base, 'NUDGE', 'expired: ' + reason)
    print('NUDGE expired: ' + reason, flush=True)
    return 0


def execute_nudge(base):
    path = str(base) + '.nudge.json'
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        request = s.read(path)
    try:
        while True:
            current = s.read(str(base) + '.run.json')
            latest = s.read(path)
            if not latest.get('pending') or latest.get('id') != request['id']:
                return 0
            if current.get('run_id') != request.get('run_id'):
                return expire_nudge(base, request, current, 'run instance replaced before delivery; session left untouched')
            if ((current.get('stop_requested') and current.get('stop_requested') != request.get('stop_requested')) or
                    (not request.get('reopen') and (current.get('status') == 'stopped' or current.get('stop_requested')))):
                return expire_nudge(base, request, current)
            if not a.owners_alive(current):
                break
            time.sleep(.1)
        # finish() can publish after the snapshot above; judge the settled record.
        current = s.read(str(base) + '.run.json')
        if current.get('run_id') != request.get('run_id'):
            return expire_nudge(base, request, current, 'run instance replaced before delivery; session left untouched')
        if ((current.get('stop_requested') and current.get('stop_requested') != request.get('stop_requested')) or
                (not request.get('reopen') and (current.get('status') == 'stopped' or current.get('stop_requested')))):
            return expire_nudge(base, request, current)
        rc = a.nudge_main([base.name, request['message']])
    except (Exception, SystemExit) as exc:
        print(str(exc), file=sys.stderr)
        rc = 2
    request.update(pending=False, finished=time.time(), exit_code=rc)
    s.atomic(path, request)
    if rc:
        current = s.read(str(base) + '.run.json')
        if (current.get('run_id') == request.get('run_id') and current.get('status') != 'stopped'
                and not a.owners_alive(current)):
            current.update(status='failed', exit_code=rc, ended_at=a.now())
            s.registry_write(str(base) + '.run.json', current)
        s.event(base, 'ESCALATE', 'nudge failed; see ' + str(base) + '.nudge.log')
    return rc


def request_heal(run, action, reason, reason_class=None, progress=None):
    """Claim persisted remediation before spawning; survives watcher restarts."""
    from godfather_supervisor import pending_question
    if pending_question(run) or run.get('status') == 'needs_input':
        return False
    base = a.runs_dir().resolve() / (run.get('healing_parent') or run.get('router_name') or run['name'])
    route = s.read(str(base) + '.route.json')
    identity = route.get('run_id') or run.get('run_id') or run['started_at']
    path = str(base) + '.healing.json'
    config, _ = settings()
    if not config['enabled']:
        return False
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = s.read(path)
        if data.get('run_id') != identity:
            data = dict(run_id=identity, attempts={}, policy_attempts=data.get('policy_attempts') or {})
        if 'original_prompt' not in data:
            original = Path(str(base) + '.prompt.md')
            if not original.exists():
                original = Path(run['prompt_file'])
            data['original_prompt'] = original.read_text()
        if data.get('pending') and a.alive(data.get('pid'), data.get('identity')):
            if action != 'restart' or data.get('action') != 'stall':
                return True
            segment_run = s.read(str(base.parent / (data.get('segment') or base.name)) + '.run.json') or run
            from godfather_supervisor import refuse_signal
            if refuse_signal(segment_run):
                return False
            os.kill(data['pid'], signal.SIGTERM)
        if data.get('pending') and not a.alive(data.get('pid'), data.get('identity')):
            data['pending'] = False
            s.atomic(path, data)
            s.event(base, 'ESCALATE', data.get('action', action) + ' remediation process vanished')
        if data['attempts'].get(action, 0) >= config['attempts']:
            return False
        data['attempts'][action] = data['attempts'].get(action, 0) + 1
        from godfather_supervisor import interruption_prompt
        reason_class = reason_class or dict(stream='broken_stream', stall='stall_silent',
                                           restart='crash', died='crash').get(action, action)
        data.update(interruption=interruption_prompt(run, reason_class, reason, progress),
                    pending=True, action=action, requested=time.time(), reason=reason,
                    segment=run['name'], segment_id=run.get('segment_id'))
        s.atomic(path, data)
        with open(str(base) + '.healing.log', 'a') as out:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'heal', str(base)],
                                     env=s.original_environment(), stdout=out, stderr=out, start_new_session=True)
        data.update(pid=child.pid, identity=a.processes().get(child.pid, {}).get('identity'))
        s.atomic(path, data)
    return True


def heal(base):
    path = str(base) + '.healing.json'
    # Wait until the claiming process publishes our pid under the lock.
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = s.read(path)
    action, reason = data['action'], data['reason']
    run = s.read(str(base.parent / data['segment']) + '.run.json')
    rc = 1
    try:
        from godfather_supervisor import refuse_signal, interruption_prompt
        blocked = refuse_signal(run)
        if blocked:
            raise ValueError('refusing to kill a working process tree: ' + blocked)
        context = data.get('interruption') or interruption_prompt(run, action, reason)
        if action in ('stream', 'stall'):
            message = ('Continue where you left off; write large files in pieces under ~400 lines.' if action == 'stream'
                       else 'You appear stuck; report status, then continue.')
            rc = a.nudge_main([run['name'], context + message])
        else:
            # Stop only verified owners before starting a fresh handoff.
            route = s.read(str(base) + '.route.json')
            if route.get('status') == 'running' and a.alive(route.get('router_pid'), route.get('router_identity')):
                os.kill(route['router_pid'], signal.SIGTERM)
            elif a.run_alive(run):
                procs = a.processes()
                supervisor = run.get('supervisor_pid')
                if procs.get(run['pid'], {}).get('ppid') != supervisor or procs.get(supervisor, {}).get('ppid') != run.get('launcher_pid'):
                    raise ValueError('cannot verify process ownership')
                os.kill(supervisor, signal.SIGTERM)
                if a.alive(run.get('launcher_pid'), run.get('launcher_identity')):
                    os.kill(run['launcher_pid'], signal.SIGTERM)
            import godfather_router as r
            original = data.get('original_prompt') or Path(run['prompt_file']).read_text()
            prompt = context + r.handoff(original, run['cwd'], base.parent / run['name'], run)
            name = base.name + '#heal-' + uuid.uuid4().hex[:6]
            env = dict(s.original_environment(), **run.get('launcher_env', {}), CWD=run['cwd'])
            env['GODFATHER_HEAL_PARENT'] = base.name
            env.pop('GODFATHER_LOCK_HELD', None)
            argv = [str(Path(__file__).with_name('godfather')), 'run', '--no-isolate', '--wait-lock', '-p', prompt, name]
            if env.get('GODFATHER_SELF') == '1':
                argv.insert(2, '--self')
            if env.get('GODFATHER_CHECK'):
                argv[2:2] = ['--check', env['GODFATHER_CHECK']]
            child = subprocess.Popen(argv, env=env)
            rc = child.wait()
            replacement = s.read(str(base.parent / name) + '.route.json')
            if replacement.get('active_name'):
                run.update(status='replaced', ended_at=a.now())
                s.registry_write(str(base.parent / run['name']) + '.run.json', run)
                route.update(active_name=replacement['active_name'], status='finished', ended_at=a.now())
                s.atomic(str(base) + '.route.json', route)
    except (Exception, SystemExit) as exc:
        reason += '; ' + str(exc)
    finally:
        current = s.read(path)
        if current.get('pid') == os.getpid():
            data.update(pending=False, exit_code=rc, finished=time.time())
            s.atomic(path, data)
            kind = 'HEALED' if rc == 0 else 'ESCALATE'
            detail = action + ' (' + reason + ')' if rc == 0 else action + ' failed (' + reason + ')'
            s.event(base, kind, detail)
            print(kind + ' ' + base.name + ' ' + detail, flush=True)
    return rc


def important(kind, detail, name):
    run = s.read(str(a.runs_dir() / name) + '.run.json')
    name = run.get('router_name') or name
    if kind in ('NEEDS_INPUT', 'DENY', 'ESCALATE'):
        return True
    if kind == 'CHECK':
        return detail.startswith('fail')
    if kind == 'NUDGE':
        return detail.startswith('expired')
    if kind == 'QUOTA_WAIT':
        return True
    if kind == 'QUOTA':
        return logical_status(name) in ('quota', 'waiting_quota')
    if kind in ('DONE', 'DIED'):
        status = logical_status(name)
        return status not in ACTIVE and (kind != 'DIED' or status in ('died', 'failed'))
    return False


def since_seconds(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([smhdw])', value)
    if not match:
        raise ValueError('--since expects a duration such as 24h or 7d')
    return float(match[1]) * dict(s=1, m=60, h=3600, d=86400, w=604800)[match[2]]


def lines(path):
    if path.exists():
        for line in path.read_text(errors='replace').splitlines():
            try:
                yield json.loads(line)
            except ValueError:
                continue


def doctor_report(since='24h'):
    cutoff = time.time() - since_seconds(since)
    incidents = [v for v in lines(state_dir() / 'incidents.jsonl') if v.get('time', 0) >= cutoff]
    events = {}
    for pattern in ('*.events.jsonl', '*.monitor.jsonl'):
        for path in a.runs_dir().glob(pattern):
            for e in lines(path):
                if e.get('at', 0) >= cutoff:
                    events[e.get('id', str(e))] = e
    counts = collections.Counter(e['kind'] for e in events.values())
    asks = collections.defaultdict(list)
    stalls = collections.Counter()
    for item in incidents:
        if item['verdict'] == 'ask' and item.get('answer'):
            asks[json.dumps(item['command'])].append(item['answer'])
    candidates = [cmd for cmd, answers in asks.items() if all(v in ('allow', 'allow+remember') for v in answers)]
    for e in events.values():
        if e['kind'] == 'STALL':
            run = s.read(str(a.runs_dir() / e['name']) + '.run.json')
            stalls[run.get('cli', '?') + ':' + run.get('model', '?')] += 1
    switches = sum(len(s.read(p).get('switches', [])) for p in a.runs_dir().glob('*.route.json') if a.epoch(s.read(p).get('started_at')) >= cutoff)
    ledger = [v for v in lines(state_dir() / 'ledger.jsonl') if a.epoch(v.get('ended_at')) >= cutoff]
    text = ['Godfather doctor (since ' + since + ')',
            'Incidents: {} asks, {} denies, {} BLOCKED'.format(sum(v['verdict'] == 'ask' and not v.get('answer') for v in incidents), sum(v['verdict'] == 'deny' for v in incidents), counts['BLOCKED']),
            'Heals: {}; escalations: {}; quota exhaustion: {}; fallback switches: {}'.format(counts['HEALED'], counts['ESCALATE'], max(counts['QUOTA'], sum(bool(v.get('quota_exhausted')) for v in ledger)), switches),
            'False-positive candidates (always allowed): ' + (', '.join(candidates) or 'none'),
            'Recurring stalls by model: ' + (', '.join(k + '=' + str(v) for k, v in stalls.items()) or 'none')]
    sources = collections.Counter(e.get('source') for e in events.values() if e.get('kind') == 'FAILURE')
    text.append('Failure classification: structured={}; text fallback={}'.format(sources['structured'], sources['text']))
    text.append('Suggestions:')
    if candidates:
        text.append('- Review these approvals and use answer allow --remember for safe recurring effects.')
    if stalls:
        text.append('- Inspect model transcripts for repeated stalls; split large writes and tune [healing] thresholds.')
    if counts['ESCALATE']:
        text.append('- Inspect .healing.log for failed remediations and add regression tests for root causes.')
    if switches or any(v.get('quota_exhausted') for v in ledger):
        text.append('- Review quota cooldowns and add an authenticated fallback model if capacity runs out.')
    if text[-1] == 'Suggestions:':
        text.append('- Keep acceptance checks enabled and review incidents periodically; no recurring issue found.')
    return '\n'.join(text) + '\n'


def improve(args):
    report = doctor_report(args.since)
    brief = ('Improve godfather itself from the evidence below. Read README, bin/, tests/, and git log. '
             'Fix root causes with regression tests; preserve working behavior. Never edit secrets, merge, push, or deploy. '
             'Run ./tests/smoke.sh and report changes and remaining risks.\n\n' + report)
    if args.dry_run:
        print(brief, end='')
        return 0
    from godfather_safety import SOURCE_BIN
    source = Path(SOURCE_BIN).parent if SOURCE_BIN else Path(__file__).resolve().parent.parent
    if not (source / 'tests/smoke.sh').exists():
        raise ValueError('source checkout unavailable; reinstall from its persistent checkout')
    name = 'improve-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:4]
    path = state_dir() / (name + '.brief.md')
    path.write_text(brief)
    rc = subprocess.call([str(Path(__file__).with_name('godfather')), 'run', '--self', '--isolate', '--check', './tests/smoke.sh', str(path), name], env=dict(s.original_environment(), CWD=str(source)))
    print('Review branch godfather/' + name + '; never merged automatically.')
    return rc


if __name__ == '__main__':
    if sys.argv[1] == 'orphan':
        sys.exit(orphan_owner(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4:]))
    if sys.argv[1] == 'launch':
        sys.exit(attached_command(sys.argv[2:], sys.argv[-1], s.original_environment()))
    if sys.argv[1] == 'worker':
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        os.environ['GODFATHER_LAUNCHER_PID'] = str(os.getpid())
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    if sys.argv[1] == 'quota':
        sys.exit(execute_quota(Path(sys.argv[2])))
    if sys.argv[1] == 'nudge':
        sys.exit(execute_nudge(Path(sys.argv[2])))
    if sys.argv[1] == 'heal':
        sys.exit(heal(Path(sys.argv[2])))
    sys.exit(daemon())
