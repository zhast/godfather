"""Execute a stored plan: worktrees or scratch copies, checks, fix rounds, and merge.

The scheduler lives here so the plan file parser stays separate. `drive` is
the only entry. It is called with the plan lock already held.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import time

import godfather_store as store
from godfather_plan import (
    BAD, INTEGRATE_BRIEF, RESTART, _allocate, _check_limit, _contract, _default_rounds,
    _git, _is_git, _latest, _load_spec, _order, _save_spec, _say, default_executor,
)


def _with_integrate(spec, git_repo):
    tasks = [dict(task) for task in spec['tasks']]
    has = any(task['task_type'] == 'integrate' for task in tasks)
    if spec['merge'] and git_repo and not has:
        tasks.append(dict(
            id='integrate', brief=INTEGRATE_BRIEF,
            deps=[task['id'] for task in tasks], check=None, isolate=True, task_type='integrate',
            model_hints=[], container=False, name=spec['name'] + '-integrate', max_fix_rounds=0))
    elif spec['merge'] and not git_repo and not has:
        _say('PLAN %s merge skipped: not a git repository' % spec['name'])
    if len({task['name'] for task in tasks}) != len(tasks):
        raise ValueError('task names must be unique, including the integrator')
    spec = dict(spec, tasks=tasks)
    spec['tasks'] = _order(tasks)
    return spec

def _insert_task(root, plan, task, rounds):
    prior = [row for row in store.plan_runs(root, plan['id']) if row['task_id'] == task['id']]
    suffix = '' if not prior else '~%d' % (len(prior) + 1)
    isolated = bool(task['isolate']) or (task['task_type'] == 'integrate' and plan['git'])
    payload = json.dumps(dict(deps=task['deps'], model_hints=task['model_hints'], max_fix_rounds=rounds, attempt=0,
                              cli='plan', model=task['task_type'], progress_paths=[]))
    store.insert_run(
        root, plan['id'] + '/' + task['id'] + suffix, task['name'], 'queued',
        plan_id=plan['id'], task_id=task['id'], cwd=plan['repo'], prompt=task['brief'],
        isolate=isolated, container=task['container'], task_type=task['task_type'],
        check_cmd=task.get('check'), payload=payload, artifact_dir=str(root),
        base_sha=plan['base_sha'], reason='plan enqueue')

def _move(root, run_id, target, reason):
    found = store.transition(root, run_id, target, reason)
    if found != target:
        raise ValueError('run %s stayed %s' % (run_id, found))
    return found

def _settle(root, run_id, target, reason, exit_code, failure=None, error=None):
    _move(root, run_id, target, reason)
    fields = dict(exit_code=exit_code, ended_at=time.time())
    if failure:
        fields['failure_class'] = failure
    if error:
        fields['error'] = error[:1000]
    store.update_run(root, run_id, **fields)

def _prepare(root, plan, task, row):
    status, run_id = row['status'], row['id']
    if status in ('failed', 'check_failed', 'review_failed', 'review_findings', 'timed_out', 'cancelled', 'needs_input'):
        _move(root, run_id, 'running', 'resume')
        status = 'running'
    elif status == 'checking':
        _move(root, run_id, 'retrying', 'resume')
        _move(root, run_id, 'running', 'resume')
        status = 'running'
    elif status == 'retrying':
        _move(root, run_id, 'running', 'resume')
        status = 'running'
    elif status == 'queued':
        _move(root, run_id, 'provisioning', 'claim')
        status = 'provisioning'
    if status == 'provisioning':
        if task['container']:
            from godfather_permissions import container_refusal
            refusal = container_refusal()
            if refusal:
                _settle(root, run_id, 'failed', 'container refused', 1, 'other', refusal)
                return 'failed', None
        cwd, branch = _provision(plan, task, row)
        _move(root, run_id, 'running', 'start')
        store.update_run(root, run_id, cwd=str(cwd), worktree=None if cwd == Path(plan['repo']) else str(cwd),
                         branch=branch, base_sha=plan['base_sha'] or None)
        return 'running', cwd
    if status != 'running':
        raise ValueError('cannot start %s from %s' % (task['id'], status))
    if row.get('isolate') or (task['task_type'] == 'integrate' and plan['git']):
        if row.get('worktree'):
            return 'running', Path(row['worktree'])
        cwd, branch = _provision(plan, task, row)
        store.update_run(root, run_id, cwd=str(cwd), worktree=str(cwd), branch=branch, base_sha=plan['base_sha'] or None)
        return 'running', cwd
    return 'running', Path(row['cwd']) if row.get('cwd') else Path(plan['repo'])

def _provision(plan, task, row):
    repo = Path(plan['repo'])
    from godfather_safety import add_resource, adopt_workspace
    payload = json.loads(row.get('payload') or '{}')
    payload.setdefault('repo', str(repo))
    if plan['git']:
        payload.setdefault('base_branch', _git(repo, 'symbolic-ref', '--short', '-q', 'HEAD', check=False).stdout.strip())
    resources = payload.setdefault('resources', [])
    if plan['git'] and (row.get('isolate') or task['task_type'] == 'integrate'):
        target = Path(plan['root']) / 'worktrees' / plan['id'] / task['id']
        created = not target.exists()
        result = _worktree(repo, plan['root'], plan['id'], task['id'], plan['base_sha'],
                           Path(plan['root']) / (row['name'] + '.models.log'))
        add_resource(resources, 'worktree', result[0], created)
        add_resource(resources, 'branch', result[1], created)
    elif row.get('isolate'):
        target = Path(plan['root']) / 'scratch' / plan['id'] / task['id']
        created = not target.exists()
        result = _scratch(repo, plan['root'], plan['id'], task['id'])
        add_resource(resources, 'scratch', result[0], created)
    else:
        result = repo, None
        adopt_workspace(repo, resources)
    row['payload'] = json.dumps(payload)
    if row.get('id'):
        store.update_run(plan['root'], row['id'], payload=row['payload'])
    return result

def _scratch(repo, root, plan_id, task_id):
    path = Path(root) / 'scratch' / plan_id / task_id
    if path.exists():
        return path, None
    from godfather_safety import copy_scratch
    copy_scratch(repo, path)
    return path, None

def _worktree(repo, root, plan_id, task_id, base_sha, models_log=None):
    branch = 'godfather/%s/%s' % (plan_id, task_id)
    path = Path(root) / 'worktrees' / plan_id / task_id
    if path.exists():
        listed = _git(repo, 'worktree', 'list', '--porcelain', check=False).stdout
        if str(path.resolve()) not in listed:
            raise ValueError('refusing existing path: ' + str(path))
        return path, branch
    if _git(repo, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch, check=False).returncode == 0:
        raise ValueError('refusing existing branch: ' + branch)
    # Share hook suppression and rollback with standalone runs.
    from godfather_router import create_worktree
    return create_worktree(repo, path, branch, base_sha, models_log=models_log)

def _run_check(command, cwd, limit):
    try:
        result = subprocess.run(command, shell=True, cwd=str(cwd), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=limit, text=True)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ''
        if isinstance(output, bytes):
            output = output.decode('utf-8', 'replace')
        return False, output + '\ncheck timed out\n'
    return result.returncode == 0, result.stdout or ''

def _tail(text, lines=80):
    return '\n'.join((text or '').splitlines()[-lines:])

def _deps_ready(task, runs):
    return all(runs[dep]['status'] in ('done', 'merging') for dep in task['deps'])

def _block(root, tasks, runs):
    changed = False
    for task in tasks:
        row = runs[task['id']]
        if row['status'] != 'queued':
            continue
        bad = [dep for dep in task['deps'] if runs[dep]['status'] in BAD]
        if not bad:
            continue
        _move(root, row['id'], 'dependency_failed', 'dependency failed: ' + ','.join(bad))
        store.update_run(root, row['id'], exit_code=1, ended_at=time.time(), failure_class='other',
                         error='dependency failed: ' + ','.join(bad))
        row['status'] = 'dependency_failed'
        changed = True
    return changed

def _rounds(task, override):
    if override is not None:
        return override
    if task.get('max_fix_rounds') is not None:
        return task['max_fix_rounds']
    return _default_rounds()

def _execute(root, plan, task, row, launch, override):
    try:
        status, cwd = _prepare(root, plan, task, row)
    except ValueError as exc:
        if row['status'] == 'provisioning' or _latest(root, plan['id'])[task['id']]['status'] == 'provisioning':
            _settle(root, row['id'], 'failed', 'provision failed', 1, 'other', str(exc))
            return 'failed'
        raise
    if status != 'running':
        return status
    if task['task_type'] == 'integrate':
        return _integrate(root, plan, task, row)
    limit = _rounds(task, override)
    current = _latest(root, plan['id'])[task['id']]
    payload = json.loads(current.get('payload') or '{}')
    first_attempt = payload.get('attempt', 0)
    prompt = payload.get('fix_prompt') or task['brief']
    prior_brief = payload.get('brief')
    if prior_brief and prior_brief != task['brief']:
        prompt = task['brief'] + (prompt[len(prior_brief):] if prompt.startswith(prior_brief) else '')
    if first_attempt > limit:
        _settle(root, row['id'], 'check_failed', 'fix rounds spent', 1, 'check')
        return 'check_failed'
    for attempt in range(first_attempt, limit + 1):
        payload = json.loads(_latest(root, plan['id'])[task['id']].get('payload') or '{}')
        payload.update(attempt=attempt, fix_prompt=prompt, brief=task['brief'])
        store.update_run(root, row['id'], payload=json.dumps(payload))
        try:
            code = launch(dict(task, run_name=task['name'], attempt=attempt, project_cwd=plan['repo'], plan_run_id=row['id'], plan_id=plan['id'],
                               plan_root=str(root), reattach=row['status'] in ('running', 'checking', 'provisioning', 'needs_input') and attempt == first_attempt), prompt, cwd, attempt)
        except Exception as exc:
            _settle(root, row['id'], 'failed', 'executor failed', 1, 'other', str(exc))
            return 'failed'
        if launch is default_executor:
            current = _latest(root, plan['id'])[task['id']]
            _sync_question(root, current)
            current = _latest(root, plan['id'])[task['id']]
            if current['status'] == 'needs_input':
                return 'needs_input'
        if code:
            _settle(root, row['id'], 'failed', 'executor exited %s' % code, code, 'other')
            return 'failed'
        if not task.get('check'):
            target = _after_success(plan, task, row)
            _settle(root, row['id'], target, 'task finished', 0)
            return target
        _move(root, row['id'], 'checking', 'acceptance check')
        passed, output = _run_check(task['check'], cwd, _check_limit())
        store.add_event(root, row['id'], 'CHECK', '%s (round %s)' % ('pass' if passed else 'fail', attempt), name=task['name'])
        _say('CHECK %s %s (round %s)' % (task['name'], 'pass' if passed else 'fail', attempt))
        if passed:
            target = _after_success(plan, task, row)
            _settle(root, row['id'], target, 'check passed', 0)
            return target
        if attempt == limit:
            _settle(root, row['id'], 'check_failed', 'fix rounds spent', 1, 'check', _tail(output))
            return 'check_failed'
        prompt = (task['brief'] + '\n\nThe acceptance check failed: ' + task['check'] +
                  '\nFix the failure and verify your changes. Last 80 output lines:\n' + _tail(output))
        payload = json.loads(_latest(root, plan['id'])[task['id']].get('payload') or '{}')
        payload.update(attempt=attempt + 1, fix_prompt=prompt)
        store.update_run(root, row['id'], payload=json.dumps(payload))
        _move(root, row['id'], 'retrying', 'fix round')
        _move(root, row['id'], 'running', 'fix')
    return 'check_failed'

def _after_success(plan, task, row):
    """Isolated tasks wait in merging until the integrator merges their branch."""
    if not (row.get('isolate') and plan['git']):
        return 'done'
    if any(other['task_type'] == 'integrate' and task['id'] in other['deps'] for other in plan['tasks']):
        return 'merging'
    return 'done'

def _integrate(root, plan, task, row):
    current = _latest(root, plan['id'])[task['id']]
    cwd = Path(current.get('worktree') or current.get('cwd') or plan['repo'])
    if not plan['git']:
        _say('PLAN %s merge skipped: not a git repository' % plan['name'])
        return _integrate_check(root, plan, task, current, cwd)
    found = store.cas_plan(root, plan['id'], 'open', 'merging')
    if found not in ('merging',):
        _settle(root, row['id'], 'failed', 'plan is ' + found, 1, 'merge')
        return 'failed'
    runs = _latest(root, plan['id'])
    wanted = set(task['deps'])
    for other in plan['tasks']:
        if other['id'] not in wanted or other['task_type'] == 'integrate':
            continue
        current_dep = runs[other['id']]
        if current_dep['status'] == 'done':
            continue
        if current_dep['status'] != 'merging':
            _settle(root, row['id'], 'failed', 'task %s is %s' % (other['id'], current_dep['status']), 1, 'merge')
            _fail_plan(root, plan['id'])
            return 'failed'
        if not current_dep.get('branch'):
            _settle(root, current_dep['id'], 'done', 'nothing to merge', 0)
            continue
        outcome = _merge_one(root, plan, cwd, current_dep)
        if outcome != 'done':
            _settle(root, row['id'], 'failed', 'merge failed: ' + other['id'], 1, 'merge')
            _fail_plan(root, plan['id'])
            return 'failed'
    return _integrate_check(root, plan, task, current, cwd)

def _integrate_check(root, plan, task, row, cwd):
    if not task.get('check'):
        _settle(root, row['id'], 'done', 'merged', 0)
        return 'done'
    _move(root, row['id'], 'checking', 'acceptance check')
    passed, output = _run_check(task['check'], cwd, _check_limit())
    detail = '%s (round 0)' % ('pass' if passed else 'fail')
    store.add_event(root, row['id'], 'CHECK', detail, name=task['name'])
    _say('CHECK %s %s' % (task['name'], detail))
    if passed:
        _settle(root, row['id'], 'done', 'check passed', 0)
        return 'done'
    _settle(root, row['id'], 'check_failed', 'fix rounds spent', 1, 'check', _tail(output))
    _fail_plan(root, plan['id'])
    return 'check_failed'

def _merge_one(root, plan, cwd, row):
    repo, branch, base = Path(plan['repo']), row['branch'], plan['base_sha']
    worktree = Path(row['worktree']) if row.get('worktree') else None
    ahead = _git(repo, 'rev-list', '--count', '%s..%s' % (base, branch), check=False)
    count = int(ahead.stdout.strip() or '0') if ahead.returncode == 0 else 0
    dirty = False
    if worktree and worktree.exists():
        dirty = bool(_git(worktree, 'status', '--porcelain', check=False).stdout.strip())
    if dirty:
        _settle(root, row['id'], 'failed', 'uncommitted work', 1, 'merge',
                'isolated task has no clean commit to merge')
        store.add_event(root, row['id'], 'MERGE', 'uncommitted', name=row['name'])
        return 'failed'
    if count == 0:
        _settle(root, row['id'], 'done', 'nothing to merge', 0)
        return 'done'
    merged = _git(cwd, 'merge', '--no-ff', '--no-edit', '-m', 'Merge ' + branch, branch, check=False)
    if merged.returncode == 0:
        _settle(root, row['id'], 'done', 'merged', 0)
        store.add_event(root, row['id'], 'MERGE', 'ok', name=row['name'])
        _say('MERGE %s ok' % row['name'])
        return 'done'
    paths = _git(cwd, 'diff', '--name-only', '--diff-filter=U', check=False).stdout.split()
    _git(cwd, 'merge', '--abort', check=False)
    detail = 'conflict ' + ', '.join(paths)
    _settle(root, row['id'], 'failed', 'merge conflict', 1, 'merge', detail)
    store.add_event(root, row['id'], 'MERGE', detail, name=row['name'])
    _say('MERGE %s %s' % (row['name'], detail))
    return 'failed'

def _fail_plan(root, plan_id):
    current = next(row['status'] for row in store.load_plans(root) if row['id'] == plan_id)
    if current == 'failed':
        return
    expect = 'merging' if current == 'merging' else 'open'
    store.cas_plan(root, plan_id, expect, 'failed')

def _finish(root, plan, tasks):
    runs = _latest(root, plan['id'])
    pending = [row['status'] for row in runs.values()
               if row['status'] in store.NONTERMINAL and row['status'] not in ('queued', 'merging')]
    if pending:
        _say('PLAN %s %s (open)' % (plan['name'], pending[0]))
        return 1
    if any(runs[task['id']]['status'] != 'done' for task in tasks):
        _fail_plan(root, plan['id'])
        status = next(row['status'] for row in store.load_plans(root) if row['id'] == plan['id'])
        _say('PLAN %s %s' % (plan['name'], status))
        return 1
    current = next(row['status'] for row in store.load_plans(root) if row['id'] == plan['id'])
    if current == 'open':
        store.cas_plan(root, plan['id'], 'open', 'done')
    elif current == 'merging':
        store.cas_plan(root, plan['id'], 'merging', 'done')
    elif current != 'done':
        _fail_plan(root, plan['id'])
        _say('PLAN %s failed' % plan['name'])
        return 1
    _say('PLAN %s done' % plan['name'])
    return 0

def _drive(spec, root, launch, override):
    git_repo = _is_git(spec['repo'])
    base_sha = ''
    if git_repo:
        base_sha = _git(spec['repo'], 'rev-parse', 'HEAD').stdout.strip()
    chosen, resuming = _allocate(root, spec['name'])
    if resuming:
        stored = _load_spec(root, chosen['id'])
        if _contract(stored) != _contract(spec):
            raise ValueError('open plan %s does not match %s' % (spec['name'], spec['source']))
        by_id = {task['id']: task for task in spec['tasks']}
        for task in stored['tasks']:
            fresh = by_id.get(task['id'])
            if fresh:
                task['brief'] = fresh['brief']
                task['model_hints'] = fresh['model_hints']
                task['max_fix_rounds'] = fresh['max_fix_rounds']
        stored['max_parallel'] = spec.get('max_parallel', 2)
        spec = stored
        _save_spec(root, chosen['id'], spec)
        if chosen['status'] == 'failed':
            store.cas_plan(root, chosen['id'], 'failed', 'open')
        plan_row = chosen
        base_sha = plan_row['base_sha'] or base_sha
    else:
        spec = _with_integrate(spec, git_repo)
        branch = 'godfather/%s/integrate' % chosen['id'] if spec['merge'] and git_repo else ''
        store.insert_plan(root, chosen['id'], spec['name'], spec['repo'], base_sha, branch, 'open')
        _save_spec(root, chosen['id'], spec)
    plan = dict(id=chosen['id'], name=spec['name'], repo=spec['repo'], base_sha=base_sha,
                merge=bool(spec['merge'] and git_repo), git=git_repo, root=root, tasks=spec['tasks'],
                override=override)
    _say('PLAN %s open id=%s' % (plan['name'], plan['id']))
    for task in spec['tasks']:
        if task['id'] not in _latest(root, plan['id']):
            _insert_task(root, plan, task, _rounds(task, override))
    runs = _latest(root, plan['id'])
    resumed = {task_id for task_id, row in runs.items() if row['status'] in RESTART or row['status'] in ('running', 'provisioning')}
    import agent_oversight as oversight
    owner = dict(launcher_pid=os.getpid(), launcher_identity=oversight.processes().get(os.getpid(), {}).get('identity'))
    for row in runs.values():
        if row['status'] not in store.TERMINAL or row['task_id'] in resumed:
            payload = json.loads(row.get('payload') or '{}')
            payload.update(owner)
            store.update_run(root, row['id'], payload=json.dumps(payload))
    active = {}
    # Futures track only local execution handles. Every scheduling/dependency
    # decision below is made from freshly loaded SQLite rows.
    with ThreadPoolExecutor(max_workers=spec.get('max_parallel', 2)) as pool:
        while True:
            for task_id, future in list(active.items()):
                if future.done():
                    future.result()
                    del active[task_id]
                    row = _latest(root, plan['id'])[task_id]
                    _say('TASK %s %s' % (row['name'], row['status']))
            runs = _latest(root, plan['id'])
            for row in runs.values():
                _sync_question(root, row)
            runs = _latest(root, plan['id'])
            if _unblock(root, plan, runs):
                continue
            runs = _latest(root, plan['id'])
            if _block(root, spec['tasks'], runs):
                continue
            ready = [task for task in spec['tasks']
                     if task['id'] not in active
                     and runs[task['id']]['status'] not in ('done', 'dependency_failed', 'stopped', 'needs_input')
                     and not (runs[task['id']]['status'] == 'merging' and task['task_type'] != 'integrate')
                     and _deps_ready(task, runs)
                     and not (runs[task['id']]['status'] in RESTART and task['id'] not in resumed)]
            ready.sort(key=lambda task: runs[task['id']]['status'] == 'queued')
            if any(task['task_type'] == 'integrate' and runs[task['id']]['status'] in BAD
                   and task['id'] not in resumed for task in spec['tasks']):
                ready = []
            launched = False
            for task in ready:
                # A paused task can resume through `answer` outside this pool,
                # so keep its slot reserved even after its local future exits.
                paused = sum(row['status'] == 'needs_input' and task_id not in active for task_id, row in runs.items())
                if len(active) + paused >= spec.get('max_parallel', 2):
                    break
                running_tasks = [other for other in spec['tasks']
                                 if other['id'] in active or runs[other['id']]['status'] == 'needs_input']
                if any(other['task_type'] == 'integrate' for other in running_tasks):
                    break
                if task['task_type'] == 'integrate' and running_tasks:
                    break
                if any(_conflicts(root, plan, task, runs[task['id']], other, runs[other['id']]) for other in running_tasks):
                    continue
                resumed.discard(task['id'])
                active[task['id']] = pool.submit(_execute, root, plan, task, runs[task['id']], launch, override)
                launched = True
                if task['task_type'] == 'integrate':
                    break
            if not active and not launched and not any(row['status'] == 'needs_input' for row in runs.values()):
                break
            time.sleep(.02)
    return _finish(root, plan, spec['tasks'])



def _sync_question(root, row):
    """Expose the child's authoritative question without copying its decision owner."""
    if row['status'] not in ('running', 'needs_input'):
        return
    payload = json.loads(row.get('payload') or '{}')
    child = payload.get('child_name')
    if not child:
        return
    import agent_oversight as oversight
    from godfather_safety import resolve_name
    base = resolve_name(child)
    run = oversight.read(str(base) + '.run.json')
    if not run:
        return
    questions = oversight.read(str(base) + '.control.json').get('questions', run.get('questions', []))
    pending = [question for question in questions if question.get('answer') is None or not question.get('delivered', True)]
    if pending:
        store.update_run_payload(root, row['id'],
                                 dict(questions=questions, question_child=base.name, pending_decision=True), pending_decision=True)
        if row['status'] != 'needs_input':
            _move(root, row['id'], 'needs_input', 'child needs input: ' + child)
            store.add_event(root, row['id'], 'NEEDS_INPUT', ': ' + pending[-1]['question'], name=row['name'])
    elif payload.get('question_child') and run.get('status') != 'needs_input':
        store.update_run_payload(root, row['id'],
                                 dict(questions=questions, question_child=None, pending_decision=False), pending_decision=False)
        if row['status'] == 'needs_input':
            _move(root, row['id'], 'running', 'child question answered')


def _conflicts(root, plan, task, row, other, other_row):
    directory = _directory(root, plan, task, row)
    other_directory = _directory(root, plan, other, other_row)
    repo = Path(plan['repo']).resolve()
    # Provisioning reads the shared tree for scratch contents and worktree
    # includes. Reserve that read for the task's lifetime, including resumes.
    reads = {repo} if directory != repo else set()
    other_reads = {repo} if other_directory != repo else set()
    return bool({directory} & ({other_directory} | other_reads) or {other_directory} & reads)

def _directory(root, plan, task, row):
    """Reserve the real directory before provisioning, including on restart."""
    if row.get('worktree'):
        return Path(row['worktree']).resolve()
    if row.get('isolate') or (task['task_type'] == 'integrate' and plan['git']):
        kind = 'worktrees' if plan['git'] else 'scratch'
        return (Path(root) / kind / plan['id'] / task['id']).resolve()
    return Path(row.get('cwd') or plan['repo']).resolve()

def _unblock(root, plan, runs):
    """A dependency_failed row cannot reopen, so a recovered dep gets a new run."""
    created = False
    for task in plan['tasks']:
        row = runs.get(task['id'])
        if row is None or row['status'] != 'dependency_failed':
            continue
        if not task['deps'] or not all(runs[dep]['status'] in ('done', 'merging') for dep in task['deps']):
            continue
        _insert_task(root, plan, task, _rounds(task, plan.get('override')))
        created = True
    return created


def drive(spec, root, launch, override):
    """Run spec to a terminal plan status. Returns 0 when the plan is done."""
    return _drive(spec, root, launch, override)

