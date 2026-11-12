"""Run-local decisions, policy shims, locks, and verification. No dependencies."""
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

# Installer records the source script directory as well as its copied prefix.
SOURCE_BIN = None

PREAMBLE = '''Godfather oversight: Use godfather ask "question" [--options "a|b|c"]
[--blocking] when you need a decision. Ask before destructive, outward-facing,
or ambiguous actions: pushes, deploys, deleting files you did not create,
or spending money. Never edit `.env` files or other secret files.
Prefer path-scoped `git status -- <paths>` for files you are working on.
For browser work, use headless Playwright; never drive the user's own Chrome
or any browser extension or MCP attached to it.
Otherwise proceed without asking.
A nonblocking ask means finish independent work and stop; a blocking ask prints
the answer in this session. A policy denial means ask; never bypass the guard.
If work remains unfinished, start the first line of your final report with
INCOMPLETE, BLOCKED, or FAILED. Exit code 0 does not override this result.

'''
from godfather_policy import DEFAULT_POLICY, DEVBOX, load_policy


def notes_path(base, run):
    """One notes file per logical run, independent of its CLI or segment."""
    base = Path(base).resolve()
    name = run.get('router_name') or re.sub(r'#(?:[2-9]|[1-9][0-9]+)$', '', base.name)
    return base.parent / (name + '.notes.md')


def notes_preamble(path):
    return ('Running notes: ' + str(path) + ' (GODFATHER_NOTES). Create it at the start '
            'if missing; update briefly in place after each meaningful step. Use sections: Done, '
            'In progress, Next, Decisions (and why), Ruled out, Checks run (command and result), '
            'Open questions. Keep updating this same file across resumes and handoffs.\n\n')


LESSONS_CAP = 2048
# The policy's secret() checks filenames, not content. Never echo rejected text.
LESSON_SECRET = re.compile(
    r'\bsk-[A-Za-z0-9_-]{20,}|gh[pousr]_|github_pat_|xox[bap]-|(?:AKIA|ASIA)[A-Z0-9]{16}|-----BEGIN|'
    r'password\s*=|(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/])', re.I)
LESSON_OVERRIDE = re.compile(
    r'ignore\s+(?:all\s+)?previous\s+instructions|you\s+are\s+now|'
    r'disregard\s+(?:the\s+)?policy|system\s+prompt', re.I)


def filter_lessons(text):
    """One non-empty line per lesson, with safe diagnostics for unsafe lines."""
    lessons = []
    for number, line in enumerate(text.splitlines(), 1):
        # Normalize controls before matching so they cannot hide an override.
        line = ''.join(c for c in line if c.isprintable() or c == '\t').strip()
        reason = 'possible secret' if LESSON_SECRET.search(line) else (
            'instruction override' if LESSON_OVERRIDE.search(line) else None)
        if reason:
            print('lessons: dropped line %d (%s)' % (number, reason), file=sys.stderr)
            continue
        if not line or line.startswith('#'):
            continue
        line = re.sub(r'^(?:[-*+]\s+|\d+[.)]\s+)', '', line).strip()
        if line:
            lessons.append(line)
    return lessons


def lesson_project(cwd):
    """Git common metadata unifies linked worktrees; non-git uses resolved CWD."""
    cwd = Path(cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError('--cwd must name an existing directory')
    identity, repo = cwd, cwd
    try:
        result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--git-common-dir'],
                                env=original_environment(), capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            identity = (cwd / result.stdout.strip()).resolve()
            top = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                                 env=original_environment(), capture_output=True, text=True, timeout=15)
            if top.returncode == 0:
                repo = Path(top.stdout.strip()).resolve()
    except (OSError, subprocess.TimeoutExpired):
        # Discard partial discovery too: a failure of either query must use
        # the same fallback identity as a project without Git.
        identity, repo = cwd, cwd
        print('lessons: git project discovery unavailable; using CWD identity', file=sys.stderr)
    name = identity.parent.name if identity.name == '.git' else identity.name
    name = ''.join(c for c in name if c.isprintable())
    if LESSON_SECRET.search(name) or LESSON_OVERRIDE.search(name):
        name = 'project'
    return dict(key=hashlib.sha256(str(identity).encode()).hexdigest(), name=name,
                repo=repo, cwd=cwd)


def lesson_path(project, root=None):
    if root is None:
        from agent_oversight import runs_dir
        root = runs_dir()
    return Path(root).expanduser().resolve() / 'lessons' / (project['key'] + '.md')


def read_lessons(path):
    try:
        return filter_lessons(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        # Exception messages can contain file contents (e.g. decode errors).
        print('lessons: cannot read %s (%s); skipping file' % (path, type(exc).__name__), file=sys.stderr)
        return []


@contextlib.contextmanager
def lesson_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_lessons(path, project, lessons):
    # Atomic publication and a separate lock avoid lost concurrent appends.
    text = '# Project lessons: ' + project['name'] + '\n\n'
    text += ''.join('- ' + line + '\n' for line in lessons)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(text)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def add_lesson(cwd, text, root=None):
    project = lesson_project(cwd)
    path = lesson_path(project, root)
    added = filter_lessons(text)
    if not added:
        raise ValueError('no safe lesson lines to add')
    with lesson_lock(path):
        write_lessons(path, project, read_lessons(path) + added)
    return path


def lessons_main(args):
    project = lesson_project(args.cwd or os.getenv('CWD') or os.getcwd())
    path = lesson_path(project)
    if args.action == 'list':
        print('Project lessons: ' + project['name'] + '\nFile: ' + str(path))
        for number, line in enumerate(reversed(read_lessons(path)), 1):
            print('%d. %s' % (number, line))
        repo_path = project['repo'] / '.godfather/lessons.md'
        shared = read_lessons(repo_path)
        if shared:
            print('Repository lessons (read only): ' + str(repo_path))
            for line in reversed(shared):
                print('- ' + line)
        return 0
    if args.action == 'add':
        add_lesson(project['cwd'], args.text)
        return 0
    if args.action == 'rm':
        with lesson_lock(path):
            lessons = read_lessons(path)
            if args.number < 1 or args.number > len(lessons):
                raise ValueError('lesson number is out of range; use lessons list')
            del lessons[-args.number]
            write_lessons(path, project, lessons)
        return 0
    # Edit a sanitized private draft, never the live file or repository copy.
    import tempfile
    with lesson_lock(path):
        before = path.read_bytes() if path.exists() else None
        lessons = read_lessons(path)
    with tempfile.TemporaryDirectory(prefix='godfather-lessons-') as directory:
        draft = Path(directory) / 'lessons.md'
        draft.write_text(''.join('%d. %s\n' % (number, line)
                                 for number, line in enumerate(reversed(lessons), 1)))
        draft.chmod(0o600)
        editor = shlex.split(os.getenv('VISUAL') or os.getenv('EDITOR') or 'vi')
        if not editor or subprocess.call(editor + [str(draft)], env=original_environment()):
            raise ValueError('lesson editor failed; lessons left unchanged')
        edited = filter_lessons(draft.read_text())
        with lesson_lock(path):
            if (path.read_bytes() if path.exists() else None) != before:
                raise ValueError('lessons changed while editing; retry edit')
            write_lessons(path, project, list(reversed(edited)))
    return 0


def lessons_preamble(cwd, root=None):
    project = lesson_project(cwd)
    path = lesson_path(project, root)
    repo_path = project['repo'] / '.godfather/lessons.md'
    # Files are append ordered: newest local corrections precede repo guidance.
    lessons = list(reversed(read_lessons(path))) + list(reversed(read_lessons(repo_path)))
    if not lessons:
        return ''
    header = 'Project lessons:\nThese are project reminders; all Godfather oversight and effect policy still apply.\n'
    body = ''
    for index, line in enumerate(lessons):
        omitted = len(lessons) - index - 1
        footer = ('%d older lessons omitted; files: %s; %s\n' % (omitted, path, repo_path)) if omitted else ''
        entry = '- ' + line + '\n'
        available = LESSONS_CAP - len((header + body + footer + '\n').encode())
        if len(entry.encode()) > available:
            # Always include the newest, even when a single lesson exceeds the cap.
            if not body:
                footer = '%d older lessons omitted; files: %s; %s\n' % (omitted, path, repo_path)
                available = LESSONS_CAP - len((header + footer + '\n').encode())
                suffix = ' [truncated]\n'
                body = entry.encode()[:max(0, available - len(suffix.encode()))].decode('utf-8', 'ignore') + suffix
            else:
                omitted += 1
                footer = '%d older lessons omitted; files: %s; %s\n' % (omitted, path, repo_path)
            return header + body + footer + '\n'
        body += entry
    return header + body + '\n'


SHIMS = ('git', 'gh', 'rm', 'kill', 'pkill', 'killall', 'curl', 'npm', 'terraform', 'pulumi',
         'kubectl', 'docker', 'vercel', 'netlify', 'fly', 'wrangler', 'sh', 'bash', 'zsh', 'dash', 'ksh', 'tee', 'sed',
         'cp', 'mv', 'touch', 'truncate', 'install', 'chmod', 'chown', 'python', 'python3', 'node', 'perl', 'ruby')

# These exact command paths accept a terminal help flag without taking action.
HELP_COMMANDS = ('git push', 'gh repo create', 'gh repo delete', 'gh pr merge',
                 'npm publish',
                 'terraform apply', 'pulumi up', 'kubectl delete', 'docker system prune')


def policy_path():
    return Path(os.getenv('XDG_CONFIG_HOME', str(Path.home() / '.config'))).expanduser() / 'godfather/policy.toml'



def read(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def atomic(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temp, path)
    from godfather_registry import note_write
    note_write(path, data)


def control(base, change=None):
    with open(str(base) + '.control.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read(str(base) + '.control.json')
        if change:
            change(data)
            atomic(str(base) + '.control.json', data)
            registry = read(str(base) + '.run.json')
            if registry:
                registry.update(data)
                atomic(str(base) + '.run.json', registry)
        return data


def registry_write(path, data):
    base = str(path)[:-9]
    with open(base + '.control.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data.update(read(base + '.control.json'))
        if data.get('stop_requested'):
            data.update(status='stopped', exit_code=143)
        atomic(path, data)


def done_detail(base, detail):
    """Append the first report line without changing DONE's existing fields."""
    if ' report=' in detail:
        return detail
    try:
        with Path(str(base) + '.last.md').open(errors='replace') as report:
            for line in report:
                text = re.sub(r'\s+', ' ', re.sub(r'\x1b\[[0-9;]*m', '', line)).strip()
                if text:
                    return detail + ' report=' + json.dumps(text[:160], ensure_ascii=False)
    except OSError:
        pass
    return detail


def event(base, kind, detail, suffix='.events.jsonl', **fields):
    if kind == 'DONE':
        detail = done_detail(base, detail)
    if kind == 'BLOCKED':
        from godfather_policy import incident
        run = read(str(base) + '.run.json')
        incident(base, detail, run.get('cwd', os.getcwd()), [], 'BLOCKED')
    run = read(str(base) + '.run.json')
    record = dict(id=uuid.uuid4().hex, at=time.time(), kind=kind, name=Path(base).name, detail=detail,
                  model_key=run.get('model_key'))
    record.update(fields)
    if suffix == '.events.jsonl':
        from godfather_registry import note_event
        record['seq'] = note_event(record, Path(base).resolve().parent)
    with open(str(base) + suffix, 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(record) + '\n')


def audit(base, source, command, **extra):
    run = read(str(base) + '.run.json')
    record = dict(at=time.time(), source=source, segment=Path(base).name,
                  cwd=os.getcwd() if source == 'shim' else run.get('cwd', os.getcwd()), command=command,
                  model_key=run.get('model_key'))
    record.update(extra)
    targets = [str(base)]
    if run.get('router_name') and run['router_name'] != Path(base).name:
        targets.append(str(Path(base).parent / run['router_name']))
    for target in targets:
        with open(target + '.audit.log', 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(record) + '\n')


def resolve_name(name):
    from agent_oversight import runs_dir
    if not name or '/' in name or name in ('.', '..'):
        raise ValueError('name must be a filename without slashes')
    base = runs_dir().resolve() / name
    route = read(str(base) + '.route.json')
    if route.get('active_name'):
        return base.parent / route['active_name']
    if not read(str(base) + '.run.json'):
        from godfather_store import plan_task
        task = plan_task(base.parent, name)
        child = json.loads(task.get('payload') or '{}').get('child_name') if task else None
        if child and child != name:
            child_base = base.parent / child
            child_route = read(str(child_base) + '.route.json')
            return base.parent / (child_route.get('active_name') or child)
    return base


def ask(args):
    base = os.getenv('GODFATHER_BASE')
    if not base or not read(base + '.run.json'):
        raise ValueError('godfather ask must be called inside a registered run')
    if args.timeout <= 0:
        raise ValueError('--timeout must be positive')
    from godfather_policy import incident
    effect = getattr(args, 'effect', None)
    question = dict(effect=effect, id=uuid.uuid4().hex, question=args.question, options=args.options,
                    blocking=args.blocking, waiting=args.blocking, delivered=False, answer=None, asked_at=time.time())
    def add(data):
        if any(q.get('answer') is None for q in data.get('questions', [])):
            raise ValueError('an unanswered question already exists')
        data.setdefault('questions', []).append(question)
    control(base, add)
    incident(base, effect['command'] if effect else args.question, effect['cwd'] if effect else os.getcwd(), effect['targets'] if effect else [], 'ask', question_id=question['id'])
    event(base, 'NEEDS_INPUT', ': ' + args.question + (' [' + args.options + ']' if args.options else ''))
    if not args.blocking:
        return 0
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        q = next(q for q in control(base)['questions'] if q['id'] == question['id'])
        if q['answer'] is not None:
            def delivered(data):
                next(q for q in data['questions'] if q['id'] == question['id']).update(delivered=True, waiting=False)
            control(base, delivered)
            print(q['answer'], flush=True)
            return 0
        time.sleep(.1)
    def timeout(data):
        next(q for q in data['questions'] if q['id'] == question['id']).update(waiting=False, delivered=True, answer='timeout', answered_at=time.time())
    control(base, timeout)
    incident(base, effect['command'] if effect else args.question, effect['cwd'] if effect else os.getcwd(), effect['targets'] if effect else [], 'ask', 'timeout', question_id=question['id'])
    print('Timed out waiting for godfather answer', file=sys.stderr)
    return 1


def answer(args):
    from agent_oversight import alive, run_alive, nudge_main
    from godfather_permissions import parse_choice
    base = resolve_name(args.name)
    from godfather_policy import remember, incident
    learned = {}
    def record(data):
        pending = [q for q in data.get('questions', []) if q.get('answer') is None]
        if not pending:
            raise ValueError('run has no unanswered question')
        options = pending[-1].get('options') or ''
        # Only a verdict question rejects free text. Other choices, including
        # `a|b`, still accept the conductor's words as the resume text.
        if set(part for part in options.split('|') if part) == {'allow', 'deny', 'allow+remember'}:
            choice, reason = parse_choice(args.answer, options)
        else:
            choice, reason = args.answer, None
        if getattr(args, 'remember', False):
            if choice != 'allow' or not pending[-1].get('effect'):
                raise ValueError('--remember requires allow on a policy question')
            choice = 'allow+remember'
        args.answer = choice
        if choice == 'allow+remember':
            learned['rule'] = remember(pending[-1], base)
        pending[-1].update(answer=choice, reason=reason, answered_at=time.time())
    control(base, record)
    if learned.get('rule'):
        print('Learned rule %s: %s' % (learned['rule']['id'], learned['rule']['summary']), file=sys.stderr)
    # A blocking caller keeps its session. For nonblocking asks wait for the
    # independent work and launcher to finish, then resume exactly once.
    run = read(str(base) + '.run.json')
    pending = control(base)['questions'][-1]
    effect = pending.get('effect')
    incident(base, effect['command'] if effect else pending['question'], effect['cwd'] if effect else run.get('cwd', os.getcwd()), effect['targets'] if effect else [], 'ask', args.answer, question_id=pending['id'], reason=pending.get('reason'))
    if effect:
        return 0
    if pending.get('waiting') and (run_alive(run) or alive(run.get('launcher_pid'), run.get('launcher_identity'))):
        return 0
    deadline = time.monotonic() + 1800
    while run_alive(run) or alive(run.get('launcher_pid'), run.get('launcher_identity')):
        if time.monotonic() > deadline:
            raise ValueError('answer saved; run still active, resume later with godfather nudge')
        time.sleep(.2)
        run = read(str(base) + '.run.json')
    control(base, lambda data: next(q for q in data['questions'] if q['id'] == pending['id']).update(delivered=True))
    return nudge_main([base.name, args.answer])


SCRATCH_SKIP = frozenset(('node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache',
                          '.mypy_cache', '.next', 'dist', 'build', 'target', '.gradle',
                          '.cache', 'DerivedData', '.DS_Store'))


def copy_scratch(src, dest, resources=None):
    """Copy a non-git directory for one run. Refuse a path that already exists."""
    source = Path(src).resolve()
    destination = Path(dest)
    target = destination.parent.resolve() / destination.name
    if not source.is_dir():
        raise ValueError('Cannot create scratch copy: source is not a directory')
    fix = '; pick the project folder, or use --no-isolate'
    if source == Path(source.anchor) or source == Path.home().resolve():
        raise ValueError('Cannot create scratch copy of home or a filesystem root' + fix)
    repos = 0
    for child in source.iterdir():
        if child.is_dir() and (child / '.git').exists():
            repos += 1
            if repos >= 2:
                raise ValueError('Cannot create scratch copy: source directly contains multiple git repos' + fix)
    if target.exists() or target.is_symlink():
        raise ValueError('Cannot create scratch copy: refusing existing path: ' + str(target))
    if target == source or target in source.parents:
        raise ValueError('Cannot create scratch copy: destination contains the source')
    def ignore(directory, names):
        base = Path(directory).resolve()
        skipped = []
        for name in names:
            path = base / name
            if (name == '.DS_Store' or (name in SCRATCH_SKIP and path.is_dir())
                    or path == target or path in target.parents):
                skipped.append(name)
        return skipped

    try:
        cap_mb = float(os.getenv('GODFATHER_SCRATCH_MAX_MB', '2048'))
        if not math.isfinite(cap_mb) or cap_mb <= 0:
            raise ValueError
    except ValueError as exc:
        raise ValueError('GODFATHER_SCRATCH_MAX_MB must be a positive finite number') from exc
    total = 0
    def walk_error(exc):
        raise exc
    try:
        for directory, dirs, files in os.walk(source, onerror=walk_error):
            skipped = set(ignore(directory, dirs + files))
            dirs[:] = [name for name in dirs if name not in skipped]
            for name in files + [name for name in dirs if Path(directory, name).is_symlink()]:
                if name in skipped:
                    continue
                total += Path(directory, name).lstat().st_size
                if total > cap_mb * 1024 * 1024:
                    raise ValueError('Cannot create scratch copy: measured at least {} bytes ({:.2f} MiB) exceeds '
                                     'GODFATHER_SCRATCH_MAX_MB={} MiB'.format(total, total / 1024 / 1024, cap_mb) + fix)
    except OSError as exc:
        raise ValueError('Cannot size scratch copy: ' + str(exc)) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    # Claim only after preflight so refusals leave no partial destination.
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise ValueError('Cannot create scratch copy: refusing existing path: ' + str(target)) from exc
    try:
        shutil.copytree(source, target, symlinks=True, ignore=ignore, dirs_exist_ok=True)
    except BaseException as exc:
        shutil.rmtree(target, ignore_errors=True)
        if isinstance(exc, OSError):
            raise ValueError('Cannot create scratch copy: ' + str(exc)) from exc
        raise
    add_resource(resources, 'scratch', target, True)
    return target


@contextlib.contextmanager
def git_lock(cwd, timeout=120):
    """Serialize worktree/branch mutations across all checkouts of one repo."""
    from agent_oversight import runs_dir
    result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--git-common-dir'],
                            env=original_environment(), capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError('Cannot resolve git common directory: ' + result.stderr.strip())
    common = (Path(cwd) / result.stdout.strip()).resolve()
    root = runs_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(common).encode()).hexdigest()
    with (root / ('git-' + key + '.lock')).open('a') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('Timed out after {} s waiting for git lock: {}'.format(timeout, common))
                time.sleep(.1)
        yield


@contextlib.contextmanager
def cwd_lock(cwd, wait=False):
    from agent_oversight import runs_dir
    root = runs_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()
    with (root / ('cwd-' + key + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait or os.getenv('GODFATHER_WAIT_LOCK') == '1' else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ValueError('CWD is locked by another run: ' + str(cwd) + '; wait for it to finish, or omit --no-isolate')
        yield


def protect_cwd(cwd, self_allowed=False):
    install = Path(__file__).resolve().parent
    target = Path(cwd).resolve()
    roots = [install] + ([Path(SOURCE_BIN)] if SOURCE_BIN else [])
    if not self_allowed and any(inside(target, path) for path in roots):
        raise ValueError('Refusing to run in godfather installed scripts; use --self explicitly')


def snapshot(cwd):
    # Record every preexisting path, including untracked files; not just git HEAD.
    paths = []
    for directory, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d != '.git' and not Path(directory, d).is_symlink()]
        paths.extend(str(Path(directory, f).resolve()) for f in files)
    return paths


def inside(path, root):
    return path == root or root in path.parents


def add_resource(ledger, kind, value, owned):
    if ledger is None:
        return
    key = 'path' if kind in ('worktree', 'scratch') else 'id'
    value = str(Path(value).resolve()) if key == 'path' else str(value)
    if not any(r.get('kind') == kind and r.get(key) == value for r in ledger):
        ledger.append(dict(kind=kind, owned=owned, **{key: value}))


def devbox_tool():
    """The remote-box CLI declared in policy.toml, or None: the feature is then inactive."""
    return load_policy()['devbox']


def devbox_id(status, keys):
    """First non-empty string among the configured dotted paths, in their order."""
    for key in keys:
        value = status
        for part in key.split('.'):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


def attached_devbox(cwd):
    """Best-effort adoption probe; explicit isolation uses the stricter router check."""
    from godfather_router import check_devbox_isolation
    try:
        return check_devbox_isolation(cwd, explicit=False)
    except ValueError:
        return None


def devbox_status(cwd):
    box = devbox_tool()
    env = original_environment()
    binary = box and shutil.which(box['binary'], path=env['PATH'])
    if not binary:
        return None
    try:
        result = subprocess.run([binary, 'status', '--json'], cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        status = json.loads(result.stdout)
        if isinstance(status, dict) and status.get('attached') in (True, False):
            return status
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def adopt_workspace(cwd, ledger, attached=None):
    if ledger is None:
        return
    from godfather_router import _inside_git, git_output
    if _inside_git(cwd):
        top = git_output(cwd, 'rev-parse', '--show-toplevel').strip()
        add_resource(ledger, 'worktree', top or cwd, False)
        branch = git_output(cwd, 'symbolic-ref', '--short', '-q', 'HEAD').strip()
        if branch:
            add_resource(ledger, 'branch', branch, False)
    else:
        add_resource(ledger, 'scratch', cwd, False)
    attached = attached if attached is not None else attached_devbox(cwd)
    if attached:
        add_resource(ledger, 'devbox', devbox_id(attached, (devbox_tool() or DEVBOX)['id_keys']) or 'unknown', False)


def original_environment():
    """Only workers inherit policy shims; conductor housekeeping never does."""
    env = dict(os.environ)
    env['PATH'] = env.get('GODFATHER_ORIGINAL_PATH') or os.pathsep.join(
        p for p in env.get('PATH', os.defpath).split(os.pathsep)
        if not (Path(p).name.endswith('.shims') and (Path(p) / 'godfather').is_file()))
    for key in ('BASH_ENV', 'ZDOTDIR'):
        if env.get('GODFATHER_ORIGINAL_' + key):
            env[key] = env['GODFATHER_ORIGINAL_' + key]
        elif '.shims' in env.get(key, ''):
            env.pop(key, None)
    return env


def denial(argv, context, policy):
    """Compatibility accessor; the runtime consumes three-way verdicts."""
    from godfather_policy import evaluate
    effect = evaluate(argv, context, policy)
    return effect['effect'] if effect['verdict'] != 'allow' else None


def prepare(base, run, command):
    policy = load_policy()
    if 'resources' not in run:
        run['resources'] = json.loads(os.getenv('GODFATHER_RESOURCES', '[]'))
        if not os.getenv('GODFATHER_RESOURCES'):
            adopt_workspace(run['cwd'], run['resources'])
    if run['cli'] in ('codex', 'grok', 'cursor'):
        from agent_oversight import backend_home
        cli = run['cli']
        run[cli + '_home'] = str(backend_home(cli, run))
        # Freshly logged-in homes may have no transcripts yet. Materialize only
        # the session tree so workspace-write can grant it on the first launch.
        (Path(run[cli + '_home']) / 'sessions').mkdir(parents=True, exist_ok=True)
        run.setdefault('launcher_env', {})[cli.upper() + '_HOME'] = run[cli + '_home']
    run['model_key'] = os.getenv('GODFATHER_MODEL_KEY') or run.get('model_key') or run['cli'] + ':' + run.get('model', '?')
    run.setdefault('launcher_env', {})['GODFATHER_MODEL_KEY'] = run['model_key']
    notes = notes_path(base, run)
    run['notes_path'] = str(notes)
    # Materialize the file so file-only sandbox grants and bind mounts work on
    # the first launch too. Never truncate a previous segment's notes.
    fd = os.open(str(notes), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    project_cwd = run.get('project_cwd') or run.get('launcher_env', {}).get('GODFATHER_PROJECT_CWD') or run['cwd']
    run['project_cwd'] = project_cwd
    run['launcher_env']['GODFATHER_PROJECT_CWD'] = project_cwd
    preamble = PREAMBLE + notes_preamble(notes) + lessons_preamble(project_cwd, Path(base).parent)
    if run['cli'] == 'codex' and os.getenv('CODEX_SANDBOX') == 'read-only':
        preamble += ('Codex read-only exception: read the existing notes; include progress updates '
                     'in your final report because this sandbox permits no file writes.\n\n')
    root = Path(str(base) + '.shims')
    root.mkdir(exist_ok=True)
    context_path = Path(str(base) + '.safety.json')
    origin = Path(os.getenv('GODFATHER_CONTEXT', str(context_path)))
    context = read(origin) if origin != context_path or os.getenv('AGENT_NUDGE') == '1' else {}
    if not context:
        context = dict(cwd=run['cwd'], existing=snapshot(run['cwd']))
    context['policy'] = policy
    context['base'] = str(base)
    context['notes_path'] = str(notes)
    context['started_at'] = run.get('started_at')
    context['scratch'] = []
    extra_shims = {tool['binary'] for tool in policy['outward'] + [policy['devbox']] if tool}
    for rule in policy['rules']:
        if rule.get('binary'):
            if not re.fullmatch(r'[A-Za-z0-9_.+-]+', rule['binary']):
                raise ValueError('policy binary must be a literal executable name')
            extra_shims.add(rule['binary'])
    for pattern in [r['legacy'] for r in policy['rules'] if 'legacy' in r]:
        first = pattern.split()[0] if pattern.split() else ''
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+', first):
            raise ValueError('policy patterns must start with a literal executable name: ' + pattern)
        extra_shims.add(first)
    shim_names = sorted(set(SHIMS) | extra_shims)
    original = original_environment()
    context['real'] = {cmd: shutil.which(cmd, path=original['PATH']) for cmd in shim_names}
    context['self_allowed'] = os.getenv('GODFATHER_SELF') == '1'
    atomic(context_path, context)
    helper = Path(__file__).resolve()
    for name in shim_names:
        script = root / name
        script.write_text('#!' + sys.executable + '\nimport sys\nsys.path.insert(0, ' + repr(str(helper.parent)) + ')\nfrom godfather_safety import shim\nsys.exit(shim(' + repr(str(base)) + ', ' + repr(name) + '))\n')
        script.chmod(0o700)
    gf = root / 'godfather'
    gf.write_text('#!' + sys.executable + '\nimport sys\nsys.path.insert(0, ' + repr(str(helper.parent)) + ')\nfrom godfather_router import main\nsys.exit(main())\n')
    gf.chmod(0o700)
    env = dict(original, GODFATHER_BASE=str(base), GODFATHER_NOTES=str(notes), GODFATHER_ORIGINAL_PATH=original['PATH'],
               PATH=str(root) + os.pathsep + str(helper.parent) + os.pathsep + original['PATH'])
    # This is launcher metadata, retained in launcher_env for retries. A worker
    # starting another run in a different project must derive that run's CWD.
    env.pop('GODFATHER_PROJECT_CWD', None)
    if run['cli'] in ('codex', 'grok', 'cursor'):
        env[run['cli'].upper() + '_HOME'] = run[run['cli'] + '_home']
    for key in ('BASH_ENV', 'ZDOTDIR'):
        env['GODFATHER_ORIGINAL_' + key] = original.get(key, '')
    exports = ('export PATH=' + shlex.quote(env['PATH']) + '\nexport GODFATHER_BASE=' + shlex.quote(str(base)) +
               '\nexport GODFATHER_NOTES=' + shlex.quote(str(notes)) + '\n')
    bash_env = root / 'bash-env'
    bash_env.write_text(exports)
    for startup in ('.zshenv', '.zprofile', '.zshrc'):
        (root / startup).write_text(exports)
    env.update(BASH_ENV=str(bash_env), ZDOTDIR=str(root))
    # Prepend at the actual CLI boundary so direct launches and resumes are covered.
    prompt = Path(str(base) + '.effective.md')
    prompt.write_text(preamble + Path(run['prompt_file']).read_text())
    if run['cli'] == 'agy':
        command[command.index('-p') + 1] = preamble + command[command.index('-p') + 1]
    elif run['cli'] == 'cursor':
        from godfather_cursor import environment
        env.update({key: value for key, value in environment(run.get('cursor_home')).items() if key in ('CURSOR_CONFIG_DIR', 'CURSOR_DATA_DIR')})
        # Cursor reads the effective prompt from stdin; keep it out of argv.
    elif run['cli'] == 'grok':
        command[command.index('--prompt-file') + 1] = str(prompt)
        for kind in ('Edit', 'Write'):
            command += ['--deny', kind + '(**/.env*)']
    else:
        # Codex receives stdin from supervise instead of the launcher's raw file.
        # The launcher chooses full access or CODEX_SANDBOX's opt-in sandbox.
        # Preserve command shims through the CLI's shell environment. Do not
        # nest sandbox-exec around Codex's own optional sandbox.
        command += ['-c', 'shell_environment_policy.inherit="all"', '-c', 'shell_environment_policy.set=' + '{PATH=' + json.dumps(env['PATH']) + ',GODFATHER_BASE=' + json.dumps(str(base)) + ',GODFATHER_NOTES=' + json.dumps(str(notes)) + ',AGENT_RUNS_DIR=' + json.dumps(str(Path(base).parent)) + ',BASH_ENV=' + json.dumps(str(bash_env)) + ',ZDOTDIR=' + json.dumps(str(root)) + '}']
    # Direct secret file-tool writes are covered by the preamble and, on Grok,
    # native Edit/Write denies. The permissions JSON records the policy and
    # tradeoffs. A container, when requested, is the hard boundary.
    from godfather_permissions import apply_native, wrap_container, write_profile
    saved = run.setdefault('launcher_env', {})
    for key in ('GODFATHER_NATIVE', 'GODFATHER_CONTAINER', 'GODFATHER_TASK_TYPE', 'GODFATHER_CONTAINER_IMAGE', 'CODEX_SANDBOX'):
        if os.getenv(key):
            saved[key] = os.environ[key]
    run['base'] = str(base)
    run['task_type'] = run.get('task_type') or os.getenv('GODFATHER_TASK_TYPE') or 'edit'
    profile = write_profile(base, run)
    command = apply_native(command, run, profile)
    if os.getenv('GODFATHER_CONTAINER') == '1' or run.get('container'):
        command = wrap_container(command, run)
        audit(base, 'container', command)
    audit(base, 'launch', command)
    return command, env, prompt


def shim(base, name):
    context = read(str(base) + '.safety.json')
    argv = [name] + sys.argv[1:]
    from godfather_permissions import shim_mode
    from godfather_policy import evaluate, incident
    from types import SimpleNamespace
    from agent_oversight import discover
    current_run = read(str(base) + '.run.json')
    current_run.setdefault('progress_paths', [])
    current_run = discover(current_run)
    context['resources'] = current_run.get('resources', [])
    session = current_run.get('conversation_id') or current_run.get('session_id')
    if session:
        home = Path.home()
        context.setdefault('scratch', []).append(str(home / '.gemini/antigravity-cli/brain' / session))
        from agent_oversight import codex_home, grok_home
        for root in (codex_home(current_run) / 'sessions', grok_home(current_run) / 'sessions'):
            for path in root.glob('**/*' + session + '*'):
                context.setdefault('scratch', []).append(str(path.resolve()))
    effect = evaluate(argv, context)
    if name in ('sh', 'bash', 'zsh', 'dash', 'ksh') and not sys.stdin.isatty():
        args = argv[1:]
        reads_stdin = '-s' in args or not any(not value.startswith('-') for value in args)
        if reads_stdin and effect['verdict'] != 'deny':
            effect.update(verdict='ask', effect='execute piped shell input (possibly downloaded code)')
    text = ' '.join(shlex.quote(v) for v in argv)
    mode = shim_mode()
    audit(base, 'shim', argv, targets=effect['targets'], verdict=effect['verdict'], effect=effect['effect'], mode=mode, cwd=effect['cwd'])
    if mode == 'audit':
        # Record the verdict the shim would have given, then exec. Do not ask or delay.
        incident(base, argv, effect['cwd'], effect['targets'], 'audit', effect['verdict'], effect=effect['effect'], source='shim-audit')
    elif effect['verdict'] == 'deny':
        incident(base, argv, effect['cwd'], effect['targets'], 'deny', 'deny', effect=effect['effect'])
        event(base, 'DENY', ': ' + text + ' (' + effect['effect'] + ')')
        print('Godfather DENY: ' + text + ' (' + effect['effect'] + ')', file=sys.stderr)
        return 126
    elif effect['verdict'] == 'ask':
        question = 'Allow `' + text + '`? It would ' + effect['effect'] + '.'
        # Use the same blocking decision path as the public ask command.
        rc = ask(SimpleNamespace(question=question, options='allow|deny|allow+remember', blocking=True,
                                 timeout=float(os.getenv('ASK_TIMEOUT', '900')), effect=dict(effect, command=argv)))
        q = control(base)['questions'][-1]
        answer_text = q.get('answer') or 'timeout'
        if rc or answer_text not in ('allow', 'allow+remember'):
            event(base, 'DENY', ': ' + text + ' (' + answer_text + ')')
            incident(base, argv, effect['cwd'], effect['targets'], 'deny', answer_text)
            print('Godfather DENY: ' + text + ' (' + answer_text + ')', file=sys.stderr)
            return 126
    real = context['real'].get(name)
    if not real:
        print('Command not installed: ' + name, file=sys.stderr)
        return 127
    box = devbox_tool()
    if box and name == box['binary']:
        return run_devbox(base, real, sys.argv[1:], box)
    os.execv(real, [real] + sys.argv[1:])


def run_devbox(base, real, args, box):
    """Forward both streams unchanged; record creation only with positive evidence."""
    import threading
    before = devbox_status(os.getcwd())
    chunks = []
    child = subprocess.Popen([real] + args, env=original_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def forward(source, dest):
        forwarding = True
        try:
            while True:
                chunk = source.read1(65536)
                if not chunk:
                    break
                if sum(map(len, chunks)) < 1024 * 1024:
                    chunks.append(chunk)
                if forwarding:
                    try:
                        dest.buffer.write(chunk)
                        dest.buffer.flush()
                    except (OSError, ValueError):
                        # A closed consumer must not stop draining the child:
                        # either pipe can otherwise fill and deadlock wait().
                        forwarding = False
        finally:
            source.close()
    threads = [threading.Thread(target=forward, args=pair) for pair in
               ((child.stdout, sys.stdout), (child.stderr, sys.stderr))]
    for thread in threads:
        thread.start()
    rc = child.wait()
    for thread in threads:
        thread.join()
    creating = not args or any(v in args for v in box['create'])
    if rc == 0 and creating:
        text = b''.join(chunks).decode(errors='replace')
        after = attached_devbox(os.getcwd())
        identifier = devbox_id(after, box['id_keys'])
        # Status may return exit 1 for an attached session. A creation message
        # supplies evidence when the CLI does not expose an unattached status.
        created = re.search(box['created_pattern'], text)
        if not identifier and created:
            identifier = created.group(1)
        if not identifier and before and before.get('attached') is False:
            named = re.search(r'(?i)\b(?:devbox|box)(?:\s+id)?\s*[:=]\s*([\w.-]+)', text)
            if named:
                identifier = named.group(1)
        prior = devbox_id(before, box['id_keys'])
        creation_matches = created and created.group(1) == identifier
        if identifier:
            def record(data):
                ledger = data.setdefault('resources', read(str(base) + '.run.json').get('resources', []))
                # An unattached CWD can attach a reused machine. Only the
                # creation message proves that this run started a new one.
                add_resource(ledger, 'devbox', identifier, bool(identifier != prior and creation_matches))
            control(base, record)
    return rc


def gc_duration(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([smhdw])', value)
    if not match:
        raise ValueError('--older-than must be a duration such as 7d, 24h, or 30m')
    return float(match[1]) * dict(s=1, m=60, h=3600, d=86400, w=604800)[match[2]]


def gc_runs(root):
    """Include plan rows and historical generations, not only visible status rows."""
    import godfather_store as store
    conn = store.connect(root)
    try:
        result = []
        for row in conn.execute('SELECT * FROM runs ORDER BY name, generation'):
            data = json.loads(row['payload'] or '{}')
            for key in row.keys():
                if key != 'payload' and row[key] is not None:
                    if key not in data or key in ('status', 'ended_at', 'updated_at'):
                        data[key] = row[key]
            if row['plan_id']:
                plan = conn.execute('SELECT repo FROM plans WHERE id=?', (row['plan_id'],)).fetchone()
                if plan:
                    data.setdefault('repo', plan['repo'])
            result.append(data)
        # Routing can provision resources and then find no eligible worker.
        # Its record also protects the gap before a segment is published.
        for route in store.load_routes(root):
            if 'resources' in route:
                data = dict(route)
                if data.get('status') == 'finished':
                    data['status'] = 'failed'
                result.append(data)
        return result
    finally:
        conn.close()


def gc_path(path, root, kind):
    """Reject symlinks in every path component, including the pool itself."""
    pool = root / ('worktrees' if kind == 'worktree' else 'scratch')
    target = Path(os.path.abspath(path))
    if target == pool or not inside(target, pool):
        return 'path outside runs ' + pool.name + ' root'
    for part in (target, *target.parents):
        if part.is_symlink():
            return 'symlink path refused'
        if part == root:
            break
    if not inside(target.resolve(), pool.resolve()):
        return 'path outside runs root'
    return ''


def gc_resources(run, root):
    if 'resources' in run:
        return run['resources']
    # Before the ledger, the reserved pool and branch namespace were ownership.
    resources = []
    for value in (run.get('worktree'), run.get('cwd')):
        if not value:
            continue
        target = Path(os.path.abspath(value))
        for kind, pool in (('worktree', 'worktrees'), ('scratch', 'scratch')):
            if target != root / pool and inside(target, root / pool):
                resources.append(dict(kind=kind, path=str(target), owned=True))
    branch = run.get('branch') or run.get('launcher_env', {}).get('GODFATHER_BRANCH')
    if not branch:
        branch = 'godfather/' + run['name'].split('#', 1)[0]
    if branch.startswith('godfather/'):
        add_resource(resources, 'branch', branch, True)
    return resources


def gc_git(repo, *args):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args],
                            env=original_environment(), capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'git inspection failed')
    return result.stdout.strip()


def gc_repo(run):
    for path in (run.get('repo'), run.get('cwd'), run.get('worktree')):
        if path and Path(path).is_dir():
            try:
                common = gc_git(path, 'rev-parse', '--git-common-dir')
                return (Path(path) / common).resolve()
            except ValueError:
                pass
    raise ValueError('repository unavailable')


def gc_git_reason(repo, run, resource, resources):
    kind = resource['kind']
    value = resource.get('path') or resource.get('id')
    listing = gc_git(repo, 'worktree', 'list', '--porcelain')
    entries = [dict(line.split(' ', 1) for line in block.splitlines() if ' ' in line)
               for block in listing.split('\n\n') if block]
    if kind == 'worktree':
        matching = [e for e in entries if e.get('worktree') == value]
        if not matching:
            return 'not a registered worktree'
        if gc_git(value, 'status', '--porcelain', '--untracked-files=all'):
            return 'dirty'
        head = gc_git(value, 'rev-parse', 'HEAD')
        branch = matching[0].get('branch', '')
    else:
        if not value.startswith('godfather/'):
            return 'branch outside godfather/ namespace'
        head = 'refs/heads/' + value
        # A checked-out branch shares the worktree's cleanliness requirement.
        for entry in entries:
            if entry.get('branch') == head:
                path = entry['worktree']
                if gc_git(path, 'status', '--porcelain', '--untracked-files=all'):
                    return 'dirty'
                owned_paths = {r.get('path') for r in resources if r.get('kind') == 'worktree' and r.get('owned') is True}
                if path not in owned_paths:
                    return 'checked out in another worktree'
        gc_git(repo, 'rev-parse', '--verify', head)
        branch = head
    if gc_git(repo, 'rev-parse', '--is-shallow-repository') == 'true':
        return 'shallow repository; reachability uncertain'
    base = run.get('base_branch')
    refs = []
    if base:
        ref = base if base.startswith('refs/') else 'refs/heads/' + base
        if ref != branch:
            refs.append(ref)
    elif run.get('base_sha'):
        refs.append(run['base_sha'])
    else:
        main = entries[0].get('branch') if entries else None
        if main and main != branch and not main.startswith('refs/heads/godfather/'):
            refs.append(main)
    refs += gc_git(repo, 'for-each-ref', '--format=%(refname)', 'refs/remotes/').splitlines()
    if not refs:
        return 'base branch and remote refs unavailable'
    for ref in refs:
        gc_git(repo, 'rev-parse', '--verify', ref + '^{commit}')
    count = int(gc_git(repo, 'rev-list', '--count', head, '--not', *refs))
    if count:
        return '{} unmerged unpushed commits'.format(count)
    return ''


def gc_bytes(path):
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not Path(directory, d).is_symlink()]
        for name in files:
            total += Path(directory, name).lstat().st_size
    return total


def gc_recheck(root, kind, value, cutoff, repo=None):
    """A run may reopen while GC waits for a repository lock."""
    from godfather_store import TERMINAL, epoch
    found = []
    for run in gc_runs(root):
        resources = gc_resources(run, root)
        matches = any(r.get('kind') == kind and (r.get('path') or r.get('id')) == value for r in resources)
        if kind in ('worktree', 'scratch') and run.get('cwd'):
            matches = matches or inside(Path(os.path.abspath(run['cwd'])), Path(value))
        if matches and kind == 'branch':
            try:
                matches = gc_repo(run) == repo
            except (OSError, ValueError, subprocess.TimeoutExpired):
                matches = True  # uncertainty protects the resource
        if matches:
            found.append(run)
    if not found:
        return 'run record changed; retry cleanup'
    if any(run['status'] not in TERMINAL for run in found):
        return 'run is not terminal'
    if any((epoch(run.get('ended_at')) or epoch(run.get('updated_at'))) >= cutoff for run in found):
        return 'newer than cutoff'
    return ''


def gc_main(args):
    from agent_oversight import runs_dir
    from godfather_store import TERMINAL, epoch
    root = runs_dir().resolve()
    cutoff = time.time() - gc_duration(args.older_than)
    runs = gc_runs(root)
    candidates = {}
    for run in runs:
        resources = gc_resources(run, root)
        for resource in resources:
            kind = resource.get('kind')
            value = resource.get('path') or resource.get('id')
            if kind not in ('worktree', 'scratch', 'branch', 'devbox') or not value:
                continue
            try:
                repo_key = str(gc_repo(run)) if kind == 'branch' else ''
            except (OSError, ValueError, subprocess.TimeoutExpired):
                repo_key = run.get('repo') or run.get('cwd') or ''
            key = (repo_key, kind, value)
            candidates.setdefault(key, []).append((run, resource, resources))
    print(('APPLY' if args.yes else 'DRY RUN') + ' — resources older than ' + args.older_than)
    print('RUN\tKIND\tRESOURCE\tACTION\tREASON')
    reclaimed = 0
    # Worktrees precede branches so branch deletion never needs --force on a checkout.
    for key, references in sorted(candidates.items(), key=lambda item: (dict(worktree=0, scratch=1, branch=2, devbox=3)[item[0][1]], item[0][2])):
        _, kind, value = key
        run, resource, resources = next((r for r in references if r[1].get('owned') is True), references[0])
        action, reason = 'kept', ''
        try:
            if resource.get('owned') is not True:
                reason = 'adopted; not owned'
            elif any(r.get('status') not in TERMINAL for r, _, _ in references):
                reason = 'run is not terminal'
            elif any((epoch(r.get('ended_at')) or epoch(r.get('updated_at'))) >= cutoff for r, _, _ in references):
                reason = 'newer than cutoff'
            elif kind == 'devbox':
                box = devbox_tool()
                reason = ('consider `%s %s %s` (manual only)' % (box['binary'], box['stop'][0], value) if box
                          else 'stop it yourself; policy.toml declares no [devbox] tool (manual only)')
            elif kind in ('worktree', 'scratch') and gc_path(value, root, kind):
                reason = gc_path(value, root, kind)
            elif kind in ('worktree', 'scratch') and not Path(value).exists():
                reason = 'already absent'
            elif kind == 'scratch':
                reason = gc_recheck(root, kind, value, cutoff)
                if reason:
                    raise ValueError(reason)
                size = gc_bytes(value)
                if args.yes:
                    shutil.rmtree(value)
                    reclaimed += size
                action, reason = ('removed' if args.yes else 'remove'), 'old owned scratch copy'
            else:
                # Keep the common-dir identity discovered before worktree
                # removal: legacy records may only know the removed CWD.
                repo = Path(key[0]) if kind == 'branch' and key[0] else gc_repo(run)
                with git_lock(repo):
                    reason = gc_recheck(root, kind, value, cutoff, repo) or gc_git_reason(repo, run, resource, resources)
                    if not reason:
                        size = gc_bytes(value) if kind == 'worktree' else 0
                        if args.yes:
                            if kind == 'worktree':
                                gc_git(repo, 'worktree', 'remove', '--', value)
                            else:
                                gc_git(repo, 'branch', '-D', '--', value)
                            reclaimed += size
                        action, reason = ('removed' if args.yes else 'remove'), 'clean; commits reachable from base or remote'
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            reason = str(exc).replace('\n', ' ')
        print('{}\t{}\t{}\t{}\t{}'.format(run['name'], kind, value, action, reason))
    print('Total bytes reclaimed: {}'.format(reclaimed))
    return 0
