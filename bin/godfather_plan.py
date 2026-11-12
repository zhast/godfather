"""Run a TOML task DAG and keep its state in the SQLite store.

`godfather plan run <plan.toml>` schedules tasks by dependency, runs each
acceptance check, and retries a failed check up to its fix-round budget.
Isolated tasks get a git worktree on `godfather/<plan>/<task>`, or a scratch
copy when the repo is not a git checkout. An integrate task merges those
branches, stops on conflict, and runs its check. It does not push. `merge = true`
appends that task when the plan does not declare one. A second `plan run` of
an open, merging, or failed plan continues unfinished tasks. A done plan
starts a new generation.

Set GODFATHER_PLAN_EXEC to an executable to replace the agent launch. It is
called as `exec <cwd> <attempt> <task-id> <prompt-file>` and its exit code is
the task result. Unset, the task is `godfather run` in that directory.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import godfather_policy as policy
import godfather_store as store

TASK_TYPES = ('research', 'write', 'edit', 'review', 'integrate', 'test')
TOKEN = r'[A-Za-z0-9_.-]+'
BAD = ('failed', 'check_failed', 'review_failed', 'review_findings', 'cancelled', 'timed_out', 'dependency_failed')
RESTART = ('failed', 'check_failed', 'review_failed', 'review_findings', 'timed_out', 'cancelled', 'needs_input', 'checking', 'retrying')
PLAN_KEYS = ('name', 'repo', 'merge', 'container', 'max_fix_rounds', 'max_parallel')
TASK_KEYS = ('id', 'brief', 'deps', 'check', 'isolate', 'task_type', 'model_hints', 'container', 'name', 'max_fix_rounds')
INTEGRATE_BRIEF = (
    'Merge each completed task branch into the integrate worktree with git merge --no-ff, '
    'in dependency order then id order. Do not push, rebase, or delete task branches.'
)


def _token(value, label):
    if not isinstance(value, str) or not re.fullmatch(TOKEN, value):
        raise ValueError(label + ' must be a token')
    return value


def _bool(value, label):
    if not isinstance(value, bool):
        raise ValueError(label + ' must be true or false')
    return value


def _count(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(label + ' must be a nonnegative integer')
    return value


def _string_list(value, label):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(label + ' must be an array of strings')
    return list(value)


def parse_plan(path):
    """Read a plan file. `repo` and brief paths are relative to the file."""
    path = Path(path)
    try:
        raw = policy.document(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('cannot read plan: ' + str(exc))
    if not isinstance(raw, dict) or raw.get('schema') != 1:
        raise ValueError('plan schema must be 1')
    header = raw.get('plan')
    if not isinstance(header, dict):
        raise ValueError('plan needs a [plan] table')
    unknown = set(header) - set(PLAN_KEYS)
    if unknown:
        raise ValueError('unknown plan field: ' + ', '.join(sorted(unknown)))
    for key in ('name', 'repo'):
        if key not in header:
            raise ValueError('plan needs ' + key)
    name = _token(header['name'], 'plan name')
    repo_field = header['repo']
    if not isinstance(repo_field, str) or not repo_field:
        raise ValueError('plan repo must be a path')
    repo = Path(repo_field)
    repo = repo.resolve() if repo.is_absolute() else (path.parent / repo).resolve()
    if not repo.is_dir():
        raise ValueError('plan repo is not a directory: ' + str(repo))
    merge = _bool(header.get('merge', False), 'merge')
    container = _bool(header.get('container', False), 'container')
    rounds = _count(header['max_fix_rounds'], 'max_fix_rounds') if 'max_fix_rounds' in header else None
    parallel = _count(header.get('max_parallel', 2), 'max_parallel')
    if parallel < 1:
        raise ValueError('max_parallel must be positive')
    bodies = raw.get('tasks')
    if not isinstance(bodies, list) or not bodies:
        raise ValueError('plan needs at least one task')
    tasks, seen = [], set()
    for body in bodies:
        if not isinstance(body, dict):
            raise ValueError('tasks must be tables')
        unknown = set(body) - set(TASK_KEYS)
        if unknown:
            raise ValueError('unknown task field: ' + ', '.join(sorted(unknown)))
        for key in ('id', 'brief', 'deps', 'task_type'):
            if key not in body:
                raise ValueError('task needs ' + key)
        task_id = _token(body['id'], 'task id')
        if task_id in seen:
            raise ValueError('duplicate task id: ' + task_id)
        seen.add(task_id)
        if body['task_type'] not in TASK_TYPES:
            raise ValueError('task_type must be research, write, edit, review, integrate, or test')
        brief = body['brief']
        if not isinstance(brief, str) or not brief.strip():
            raise ValueError('task brief must be a string')
        deps = _string_list(body['deps'], 'deps')
        if task_id in deps:
            raise ValueError('a task cannot depend on itself: ' + task_id)
        check = body.get('check')
        if check is not None and (not isinstance(check, str) or not check.strip()):
            raise ValueError('check must be a shell command')
        hints = _string_list(body.get('model_hints', []), 'model_hints')
        for hint in hints:
            if not re.fullmatch(r'[A-Za-z0-9_.-]+:[A-Za-z0-9_.:/-]+', hint):
                raise ValueError('model_hints entries must be cli:model')
        run_name = body.get('name')
        if run_name is None:
            run_name = name + '-' + task_id
        else:
            _token(run_name, 'task name')
        task_rounds = _count(body['max_fix_rounds'], 'max_fix_rounds') if 'max_fix_rounds' in body else rounds
        tasks.append(dict(
            id=task_id, brief=_load_brief(brief, path.parent, repo), deps=deps, check=check,
            isolate=_bool(body.get('isolate', True), 'isolate'), task_type=body['task_type'],
            model_hints=hints, container=_bool(body['container'], 'container') if 'container' in body else container,
            name=run_name, max_fix_rounds=task_rounds))
    if len({task['name'] for task in tasks}) != len(tasks):
        raise ValueError('task names must be unique')
    missing = [dep for task in tasks for dep in task['deps'] if dep not in seen]
    if missing:
        raise ValueError('unknown dependency: ' + ', '.join(missing))
    integrators = [task['id'] for task in tasks if task['task_type'] == 'integrate']
    if len(integrators) > 1:
        raise ValueError('a plan can have only one integrate task')
    if merge and 'integrate' in seen and 'integrate' not in integrators:
        raise ValueError('task id integrate is reserved when merge is true')
    _order(tasks)
    return dict(schema=1, name=name, repo=str(repo), merge=merge, container=container,
                max_parallel=parallel, max_fix_rounds=rounds, source=str(path.resolve()), tasks=tasks)


def _load_brief(brief, plan_dir, repo):
    if any(char.isspace() for char in brief):
        return brief
    for base in (plan_dir, repo):
        candidate = Path(brief)
        path = candidate if candidate.is_absolute() else base / candidate
        if path.is_file():
            return path.read_text()
    return brief


def _order(tasks):
    pending = {task['id']: set(task['deps']) for task in tasks}
    by_id = {task['id']: task for task in tasks}
    ordered = []
    while pending:
        ready = sorted(task_id for task_id, deps in pending.items() if not deps)
        if not ready:
            raise ValueError('plan has a dependency cycle')
        nxt = ready[0]
        ordered.append(by_id[nxt])
        del pending[nxt]
        for deps in pending.values():
            deps.discard(nxt)
    return ordered


def _contract(spec):
    rows = []
    for task in spec['tasks']:
        if task['task_type'] == 'integrate':
            continue
        rows.append((task['id'], tuple(task['deps']), task['task_type'], bool(task['isolate']),
                     task.get('check') or '', task['name'], bool(task['container'])))
    return (bool(spec['merge']), tuple(rows))


def _git(repo, *args, check=True):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo)] + list(args),
                            capture_output=True, text=True)
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or 'git failed')
    return result


def _is_git(repo):
    result = _git(repo, 'rev-parse', '--is-inside-work-tree', check=False)
    return result.returncode == 0 and result.stdout.strip() == 'true'


def _default_rounds():
    try:
        from godfather_supervisor import load_policies
        for row in load_policies()['retry']:
            if row['class_'] == 'check':
                return int(row['attempts'])
    except (OSError, ValueError, KeyError):
        pass
    return 2


def _check_limit():
    try:
        from godfather_supervisor import load_policies
        return float(load_policies()['timeouts']['check_secs'])
    except (OSError, ValueError, KeyError):
        return 600.0


@contextlib.contextmanager
def plan_lock(root, name):
    directory = Path(root) / '.plan-locks'
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / name).open('a')
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('plan already active: ' + name)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def default_executor(task, prompt, cwd, attempt):
    """Launch the task. GODFATHER_PLAN_EXEC replaces `godfather run`."""
    override = os.getenv('GODFATHER_PLAN_EXEC')
    if override:
        fd, path = tempfile.mkstemp(prefix='plan-prompt-', suffix='.md')
        try:
            os.write(fd, prompt.encode())
            os.close(fd)
            result = subprocess.run([override, str(cwd), str(attempt), task['id'], path], cwd=str(cwd))
            return result.returncode
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    binary = Path(__file__).with_name('godfather')
    env = dict(os.environ, CWD=str(cwd), GODFATHER_PROJECT_CWD=str(task.get('project_cwd') or cwd))
    if task.get('model_hints'):
        env['GODFATHER_MODEL_HINTS'] = ','.join(task['model_hints'])
    root = store.runs_root(task.get('plan_root'))
    env['AGENT_RUNS_DIR'] = str(root)
    run_id = task.get('plan_run_id')
    row = next((row for row in store.plan_runs(root, task['plan_id']) if row['id'] == run_id), None) if run_id else None
    payload = json.loads(row.get('payload') or '{}') if row else {}
    child = payload.get('child_name')
    child_attempt = payload.get('child_attempt')
    if child:
        from godfather_lifecycle import logical_status, ACTIVE
        from godfather_safety import resolve_name
        import agent_oversight as oversight
        # A crashed scheduler may leave its durable child alive. Attach first;
        # never start another CLI for an invocation already recorded in the store.
        status = logical_status(child)
        if status == 'needs_input':
            return 1
        if status in ACTIVE and status != 'needs_input':
            code = subprocess.call([str(binary), 'wait', child], env=env)
            if child_attempt == attempt or code:
                return code
        elif child_attempt == attempt and status == 'done' and task.get('reattach'):
            return 0
        previous = oversight.read(str(resolve_name(child)) + '.run.json')
        if _resumable(previous, dict(task, previous_model_hints=payload.get('child_model_hints'))):
            # Keep the previous attempt until delivery completes. A crash before
            # this write re-delivers the fix rather than mistaking old 'done' for it.
            code = subprocess.call([str(binary), 'nudge', '--wait', child, prompt], env=env)
            if run_id and code == 0:
                store.update_run_payload(root, run_id, dict(child_attempt=attempt))
            return code
    ordinal = payload.get('child_ordinal', -1) + 1
    name = task['run_name'] + '.try' + str(ordinal)
    if run_id:
        payload.update(child_name=name, child_attempt=attempt, child_ordinal=ordinal, child_model_hints=task.get('model_hints') or [])
        store.update_run(root, run_id, payload=json.dumps(payload))
    # The plan already chose this directory. A nested worktree would hide the task branch.
    command = [str(binary), 'run', '--no-isolate', '--task-type', task['task_type']]
    if task.get('container'):
        command.append('--container')
    command += ['-p', prompt, name]
    rc = subprocess.call(command, env=env)
    from godfather_lifecycle import logical_status, wait_run
    if logical_status(name) == 'waiting_quota':
        status, _ = wait_run(name)
        return 0 if status == 'done' else 1
    return rc


def _resumable(run, task):
    """Require a saved session on disk in its original CLI/account."""
    import agent_oversight as oversight
    cli = run.get('cli')
    if cli not in ('codex', 'grok', 'agy', 'cursor') or run.get('status') == 'needs_input':
        return False
    hints = task.get('model_hints') or []
    if hints and hints != task.get('previous_model_hints') and hints[0].split(':', 1)[0] != cli:
        return False
    session = run.get('conversation_id') if cli == 'agy' else run.get('session_id')
    if not session:
        return False
    if cli == 'agy':
        return oversight.transcript(session).is_file()
    if cli == 'codex':
        return any((oversight.codex_home(run) / 'sessions').glob('**/*' + session + '.jsonl'))
    if cli == 'cursor':
        # Cursor stores chats by absolute workspace path, then the stream's ID.
        import hashlib
        workspace = hashlib.md5(os.path.abspath(run['cwd']).encode()).hexdigest()
        return (oversight.backend_home(cli, run) / 'chats' / workspace / session / 'store.db').is_file()
    return any((oversight.grok_home(run) / 'sessions').glob('*/' + session + '/updates.jsonl'))


def run_plan(path, executor=None, max_fix_rounds=None, root=None, max_parallel=None):
    """Execute a plan. Returns 0 when the plan reaches done."""
    spec = parse_plan(path)
    root = store.runs_root(root)
    root.mkdir(parents=True, exist_ok=True)
    if max_fix_rounds is not None and max_fix_rounds < 0:
        raise ValueError('--max-fix-rounds must be nonnegative')
    if max_parallel is not None:
        if type(max_parallel) is not int or max_parallel < 1:
            raise ValueError('--max-parallel must be positive')
        spec['max_parallel'] = max_parallel
    launch = executor or default_executor
    with plan_lock(root, spec['name']):
        from godfather_plan_run import drive
        return drive(spec, root, launch, max_fix_rounds)


def _say(message):
    print(message, flush=True)


def _spec_key(plan_id):
    return 'plan-spec:' + plan_id


def _load_spec(root, plan_id):
    raw = store.meta_get(root, _spec_key(plan_id))
    if not raw:
        raise ValueError('plan spec missing: ' + plan_id)
    return json.loads(raw)


def _save_spec(root, plan_id, spec):
    store.meta_set(root, _spec_key(plan_id), json.dumps(spec))


def _allocate(root, name):
    rows = [row for row in store.load_plans(root) if row['name'] == name]
    resumable = [row for row in rows if row['status'] in ('open', 'merging', 'failed')]
    if resumable:
        return resumable[-1], True
    plan_id = name if not rows else '%s~%d' % (name, len(rows) + 1)
    return dict(id=plan_id), False


def _latest(root, plan_id):
    found = {}
    for row in store.plan_runs(root, plan_id):
        current = found.get(row['task_id'])
        if current is None or row['generation'] > current['generation']:
            found[row['task_id']] = row
    return found


def _public_task(row):
    try:
        payload = json.loads(row.get('payload') or '{}')
    except ValueError:
        payload = {}
    return dict(id=row['task_id'], name=row['name'], status=row['status'], task_type=row['task_type'],
                isolate=row['isolate'], deps=payload.get('deps') or [], error=row.get('error'),
                worktree=row.get('worktree'), branch=row.get('branch'), generation=row['generation'],
                questions=payload.get('questions') or [], question_child=payload.get('question_child'))


def status_text(name=None, json_out=False, root=None):
    root = store.runs_root(root)
    plans = store.load_plans(root)
    if name:
        plans = [plan for plan in plans if plan['name'] == name or plan['id'] == name]
        if not plans:
            raise ValueError('unknown plan: ' + name)
    if json_out:
        payload = []
        for plan in plans:
            item = dict(plan)
            item['tasks'] = [_public_task(row) for row in _latest(root, plan['id']).values()]
            item['tasks'].sort(key=lambda task: task['id'] or '')
            payload.append(item)
        return json.dumps(payload, indent=2) + '\n'
    if not plans:
        return 'No plans.\n'
    lines = []
    for plan in plans:
        lines.append('PLAN %s id=%s status=%s repo=%s base=%s' % (
            plan['name'], plan['id'], plan['status'], plan['repo'], plan['base_sha'] or '-'))
        rows = list(_latest(root, plan['id']).values())
        rows.sort(key=lambda row: row['task_id'] or '')
        for row in rows:
            task = _public_task(row)
            lines.append('TASK %s id=%s status=%s type=%s isolate=%s deps=%s' % (
                task['name'], task['id'], task['status'], task['task_type'], task['isolate'], ','.join(task['deps'])))
            for question in task['questions']:
                if question.get('answer') is None:
                    lines.append('NEEDS_INPUT %s: %s' % (task['name'], question['question']))
    return '\n'.join(lines) + '\n'


def plan_main(args):
    if args.plan_command == 'status':
        sys.stdout.write(status_text(args.name, json_out=args.json))
        return 0
    rounds = args.max_fix_rounds
    if rounds is not None and rounds < 0:
        raise ValueError('--max-fix-rounds must be nonnegative')
    return run_plan(args.plan_file, max_fix_rounds=rounds, max_parallel=args.max_parallel)
