"""Compile one effect policy into native CLI settings and an optional container.

PATH shims default to audit. They record the verdict they would have given
and then exec the real binary. GODFATHER_SHIMS=enforce keeps the blocking
ask/deny path. The container, when requested, is the hard boundary: the repo
and the run's exact notes file are mounted read-write; nothing else is.
"""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess

DENY_GLOBS = ('**/.env', '**/.env.*', '**/.env*', '**/*.pem', '**/*.key', '**/*.p12',
              '**/*.pfx', '**/.ssh/**', '**/.gnupg/**', '**/credentials.json',
              '**/credentials.toml', '**/id_rsa', '**/id_ed25519', '**/.netrc', '**/.npmrc')
INSPECTION_PREFIXES = (('ps',), ('/bin/ps',), ('pgrep',), ('/usr/bin/pgrep',),
                       ('lsof', '-p'), ('/usr/sbin/lsof', '-p'),
                       ('top', '-l', '1'), ('/usr/bin/top', '-l', '1'))
RULES = ''.join('prefix_rule(pattern = ' + json.dumps(list(prefix)) +
                ', decision = "allow", justification = "read-only process inspection")\n'
                for prefix in INSPECTION_PREFIXES)
# Codex deliberately cannot split shell words containing expansions. In
# particular `ps -p $$` never matches the ps prefix, and macOS refuses the
# setuid /bin/ps inside Seatbelt. Declare these exact, read-only shell scripts;
# never allow a shell prefix, interpolation, or an arbitrary script suffix.
SELF_INSPECTION_SCRIPTS = tuple(
    'ps -p ' + pid + suffix
    for pid in ('$$', '"$$"', '${$}', '"${$}"')
    for suffix in ('', ' -o pid,comm', ' -o pid,comm=', ' -o pid=,comm=',
                   ' -o pid,ppid,pgid,sess,comm', ' -o pid,etime,command'))
RULES += ('prefix_rule(pattern = ' + json.dumps([
    ['sh', '/bin/sh', 'bash', '/bin/bash', 'zsh', '/bin/zsh'], ['-c', '-lc'],
    list(SELF_INSPECTION_SCRIPTS)]) +
    ', decision = "allow", justification = "read-only self process inspection")\n')
SIGNAL_RULES = ''.join('prefix_rule(pattern = ' + json.dumps([binary]) +
                     ', decision = "prompt", justification = "ask before signaling processes")\n'
                     for binary in ('kill', '/bin/kill', 'pkill', '/usr/bin/pkill'))
TRADEOFFS = {
    'codex': (
        'Every Codex run defaults to danger-full-access with approval_policy=never, including routed runs, plans, resumes, nudges, fixes, and reviews. The user config loads normally. The preamble directs decisions through godfather ask; command shims and the audit log are the safety layer.',
        'CODEX_SANDBOX opts into workspace-write with network on, or read-only. No Codex permissions table is injected and secret reads are not denied by Godfather. Shims default to audit; GODFATHER_SHIMS=enforce enables blocking command checks.',
        'The supervisor collects process identity with ps. Godfather does not wrap the CLI in sandbox-exec.',
    ),
    'cursor': (
        'Cursor --force --sandbox disabled relies on the standard oversight preamble and PATH command shims. Shims retain audit/enforce mode; no native secret file-tool deny is injected. Review-only selection excludes Cursor because team controls can disable its sandbox. Direct ask/plan runs omit --force and request --sandbox enabled. The optional container is the hard boundary.',
    ),
    'grok': (
        'Grok has no native approval callback. --always-approve is the allow path inside the writable roots, and --deny rules for Edit and Write cover secret globs. Deny wins over always-approve. The preamble tells the agent to call `godfather ask` for outward actions. The audit row records the command.',
        '--sandbox stays off. Grok\'s OS sandbox is not this boundary. chmod is not an Edit or Write, so a mode change on a secret is classified by the effect model and blocked only when shims are in enforce mode, or when the container does not mount the path.',
    ),
    'agy': (
        'Routed runs pass --mode accept-edits with per-session --settings allow rules for reads anywhere, any shell command, and write_file only inside writable roots. Existing settings are merged; user settings files stay untouched. --dangerously-skip-permissions is stripped. The preamble directs decisions through godfather ask; command shims and the audit log are the safety layer.',
        'Secret reads are not denied, as with Codex. Profile deny rules are passed as permissions.deny, but deny-glob enforcement is unverified. Shell commands can also write outside the write_file roots; these rules are not a filesystem sandbox. Shims retain their configured guard mode; only the optional container is a hard boundary.',
    ),
    'container': (
        'The container mounts the repository and the exact run notes file read-write, with no other host mounts: no home directory, no credential files, no .ssh, no .env outside the repo, and no docker socket. The image must already contain the CLI (GODFATHER_CONTAINER_IMAGE). A missing runtime or image fails provisioning and does not run on the host.',
        'Network is off for task_type=research and on for every other task type. Stdout stays attached to the host supervisor. Artifact files written only under the home directory are not visible inside the container.',
    ),
    'shims': (
        'Shims default to audit. They log argv, cwd, resolved targets, and the verdict they would have given, then exec the real binary. They do not ask, deny, or delay. GODFATHER_SHIMS=enforce restores today\'s blocking shims for one migration release. Audit mode is not a security boundary.',
    ),
}


def shim_mode():
    return 'enforce' if os.getenv('GODFATHER_SHIMS', 'audit').strip().lower() == 'enforce' else 'audit'


def scratch_roots(run=None):
    """CLI-owned trees the effect model may treat as writable for files this run created."""
    home = Path.home()
    from agent_oversight import codex_home, grok_home
    codex = codex_home(run)
    grok = grok_home(run)
    # Keep the historical default scratch grant, but an alternate account only
    # needs its sessions tree. Never grant a new account's entire home.
    grok_scratch = grok if grok == home / '.grok' else grok / 'sessions'
    return [grok_scratch, codex / 'sessions', home / '.gemini' / 'antigravity-cli' / 'brain']


def owned_tree(path):
    """The CLI-owned directory that contains path, if the path is inside one."""
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        return None
    for root in scratch_roots():
        try:
            root = root.resolve()
        except OSError:
            continue
        if target == root or root in target.parents:
            return root
    return None


def created_by_run(path, context):
    """True when path was created at or after the run started, or does not exist yet."""
    started = context.get('started_at')
    if started in (None, ''):
        return False
    if isinstance(started, str):
        try:
            started = dt.datetime.fromisoformat(started.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return False
    try:
        started = float(started)
    except (TypeError, ValueError):
        return False
    try:
        st = Path(path).stat()
    except OSError:
        return True
    born = getattr(st, 'st_birthtime', st.st_ctime)
    return born >= started


def flag_args(argv, targets, cwd):
    """Non-path argv, so a learned rule keeps the mode or flags and drops the filename."""
    from godfather_policy import resolved
    hits = set(targets)
    flags = []
    for value in argv[1:]:
        if value.startswith('-'):
            flags.append(value)
            continue
        try:
            found = set(resolved(value, cwd))
        except (OSError, ValueError):
            found = set()
        if value in hits or found & hits:
            continue
        flags.append(value)
    return flags


def under_tree(path, patterns):
    try:
        target = Path(path).resolve()
    except OSError:
        return False
    for pattern in patterns:
        if str(pattern).endswith('/**'):
            try:
                root = Path(str(pattern)[:-3]).resolve()
            except OSError:
                continue
            if target == root or root in target.parents:
                return True
        elif str(target) == pattern:
            return True
    return False


def parse_choice(answer, options):
    """Accept an exact option, optionally followed by a reason. Reject anything else."""
    if not options:
        return answer, None
    choices = options.split('|')
    if answer in choices:
        return answer, None
    for choice in sorted((item for item in choices if item), key=len, reverse=True):
        if not answer.startswith(choice) or len(answer) == len(choice):
            continue
        if answer[len(choice)] not in ' \t(':
            continue
        reason = answer[len(choice):].strip()
        if len(reason) >= 2 and reason[0] == '(' and reason[-1] == ')':
            reason = reason[1:-1].strip()
        return choice, reason or None
    raise ValueError('unrecognized answer %r; expected one of: %s' % (answer, options))


def _is_shim(path):
    try:
        text = Path(path).read_text(errors='replace')[:400]
    except OSError:
        return True
    return 'godfather_safety' in text or '.shims' in str(path)


def _lookups(name):
    found = []
    for entry in os.environ.get('PATH', os.defpath).split(os.pathsep):
        path = Path(entry) / name
        if path.is_file() and os.access(path, os.X_OK) and not _is_shim(path):
            found.append(str(path))
    return found


def container_runtime():
    """A working Docker or compatible `container` CLI, skipping godfather shims."""
    for name in ('docker', 'container'):
        for path in _lookups(name):
            try:
                result = subprocess.run([path, 'version'], capture_output=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0:
                return path
    return None


def container_image():
    image = os.getenv('GODFATHER_CONTAINER_IMAGE', '').strip()
    return image or None


def container_refusal():
    if not container_runtime():
        return 'container runtime is missing; refusing to run on the host'
    if not container_image():
        return 'GODFATHER_CONTAINER_IMAGE is not set; refusing to run on the host'
    return None


def container_argv(runtime, image, repo, command, task_type, notes_path=None):
    """Mount the repo plus only the run's notes file, never its artifact directory."""
    network = 'none' if task_type == 'research' else 'bridge'
    repo = str(Path(repo).resolve())
    binary = Path(command[0]).name
    argv = [runtime, 'run', '--rm', '-i', '--network', network,
            '-v', repo + ':' + repo, '-w', repo,
            '--user', '%s:%s' % (os.getuid(), os.getgid())]
    if notes_path:
        notes = str(Path(notes_path).resolve())
        argv += ['-v', notes + ':' + notes, '--env', 'GODFATHER_NOTES=' + notes]
    return argv + [image, binary] + list(command[1:])


def install_roots():
    from godfather_safety import SOURCE_BIN
    roots = [str(Path(__file__).resolve().parent)]
    if SOURCE_BIN:
        roots.append(str(Path(SOURCE_BIN).resolve().parent))
    return roots


def git_writable_roots(cwd):
    from godfather_safety import original_environment
    try:
        result = subprocess.run(
            ['git', '-C', str(cwd), 'rev-parse', '--path-format=absolute', '--git-common-dir', '--git-dir'],
            env=original_environment(), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    return list(dict.fromkeys(str(Path(p).resolve()) for p in result.stdout.splitlines() if p))


def writable_roots(run, tool_state_roots=()):
    roots = []

    def add(path):
        if not path:
            return
        text = str(Path(path).resolve())
        if text not in roots:
            roots.append(text)

    workspace = run.get('worktree') or run.get('cwd') or os.getcwd()
    add(workspace)
    if run.get('cwd'):
        add(run['cwd'])
    for root in (run.get('launcher_env', {}).get('ADD_DIRS') or os.getenv('ADD_DIRS', '')).split(os.pathsep):
        if root:
            add(root)
    for root in git_writable_roots(workspace):
        add(root)
    base = run.get('base')
    add(run.get('artifact_dir') or (str(Path(base).parent) if base else None))
    # The default registry database and shim incident log live outside runs/.
    # Workers need both for guard logging and the godfather ask/answer path.
    from godfather_policy import state_dir
    add(state_dir())
    add(os.getenv('TMPDIR') or '/tmp')
    add('/tmp')
    for root in scratch_roots(run):
        add(root)
    for root in tool_state_roots:
        add(root)
    return roots


def _grok_rules(profile):
    deny, allow = [], []
    for glob in profile['deny_globs']:
        deny.append('Edit(' + glob + ')')
        deny.append('Write(' + glob + ')')
    for root in profile['install_roots']:
        deny.append('Edit(' + root + '/**)')
        deny.append('Write(' + root + '/**)')
    for root in profile['writable_roots']:
        allow.append('Edit(' + root + '/**)')
        allow.append('Write(' + root + '/**)')
    for path in profile['writable_files']:
        allow.append('Edit(' + path + ')')
        allow.append('Write(' + path + ')')
    return deny, allow


def compile_profile(run):
    """One policy, expressed as the native settings each CLI can actually honor."""
    from godfather_policy import load_policy
    policy = load_policy()
    tool_state_roots = policy['writable_roots']
    cli = run.get('cli') or 'codex'
    from godfather_safety import notes_path
    notes = str(notes_path(run['base'], run)) if run.get('base') else None
    profile = dict(
        cli=cli, writable_roots=writable_roots(run, tool_state_roots), tool_state_roots=tool_state_roots,
        writable_files=[notes] if notes else [],
        scratch_roots=[str(p) for p in scratch_roots(run)],
        deny_globs=list(DENY_GLOBS), install_roots=install_roots(),
        process_inspection=policy['process_inspection'],
        allow_bins=[p[0] for p in INSPECTION_PREFIXES if len(p) == 1] if policy['process_inspection'] else [],
        allow_prefixes=[list(p) for p in INSPECTION_PREFIXES] if policy['process_inspection'] else [],
        git='read-write', nested_sandbox_exec=False, shim_mode=shim_mode(),
        approval_callback='godfather ask',
        container=bool(run.get('container') or os.getenv('GODFATHER_CONTAINER') == '1'),
        task_type=run.get('task_type') or os.getenv('GODFATHER_TASK_TYPE') or 'edit')
    if cli == 'codex':
        sandbox = os.getenv('CODEX_SANDBOX') or 'danger-full-access'
        if sandbox not in ('danger-full-access', 'workspace-write', 'read-only'):
            raise ValueError('CODEX_SANDBOX must be danger-full-access, workspace-write, or read-only: ' + sandbox)
        profile.update(sandbox=sandbox, network=sandbox != 'read-only',
                       approval_policy='never', honors_deny_globs=False)
        if sandbox == 'read-only':
            profile['writable_files'] = []
    elif cli == 'grok':
        profile.update(sandbox='off', approval_policy='always-approve', honors_deny_globs=True,
                       permission_mode='bypassPermissions')
    else:
        profile.update(permission_mode='accept-edits', skip_permissions=False, sandbox='host',
                       honors_deny_globs=False)
    deny, allow = _grok_rules(profile)
    profile['grok_deny'] = deny
    profile['grok_allow'] = allow
    profile['agy_deny'] = ['write_file(' + glob + ')' for glob in profile['deny_globs']]
    profile['agy_allow'] = ['write_file(' + root + '/)' for root in profile['writable_roots']]
    profile['agy_allow'] += ['write_file(' + path + ')' for path in profile['writable_files']]
    profile['tradeoffs'] = list(TRADEOFFS.get(cli, ())) + list(TRADEOFFS['shims'])
    if profile['container']:
        profile['tradeoffs'] += list(TRADEOFFS['container'])
    return profile


def write_profile(base, run):
    run['base'] = str(base)
    profile = compile_profile(run)
    for root in profile['tool_state_roots']:
        Path(root).mkdir(parents=True, exist_ok=True)
    rules = Path(str(base) + '.permissions.rules')
    blob = Path(str(base) + '.permissions.json')
    rules.write_text((RULES if profile['process_inspection'] else '') + SIGNAL_RULES)
    profile['rules_file'] = str(rules)
    if run.get('cli') == 'codex' and os.getenv('GODFATHER_NATIVE') == '1':
        # Codex loads rules/ beside active config layers. execpolicy.user_rules
        # is not a supported config key and was silently ignored.
        # https://developers.openai.com/codex/rules/
        from agent_oversight import codex_home
        native = codex_home(run) / 'rules/godfather-process-inspection.rules'
        native.parent.mkdir(parents=True, exist_ok=True)
        temp_rules = native.with_name(native.name + '.' + str(os.getpid()) + '.tmp')
        temp_rules.write_text(rules.read_text())
        os.replace(temp_rules, native)
        profile['native_rules_file'] = str(native)
    temp = blob.with_name(blob.name + '.tmp')
    temp.write_text(json.dumps(profile, indent=2) + '\n')
    os.replace(temp, blob)
    run['permission_profile'] = str(blob)
    return profile


def _before_stdin(command, extra):
    if command and command[-1] == '-':
        return command[:-1] + extra + ['-']
    return command + extra


def apply_native(command, run, profile):
    """Rewrite a launcher argv so the CLI sees the compiled profile."""
    cli = Path(command[0]).name if command else run.get('cli')
    if run.get('cli') == 'codex' or cli == 'codex':
        return _apply_codex(command, profile)
    if os.getenv('GODFATHER_NATIVE') != '1':
        return command
    if cli == 'grok':
        return _apply_grok(command, profile)
    if cli == 'agy':
        return _apply_agy(command, profile)
    return command


def _apply_codex(command, profile):
    command = list(command)
    resuming = command[1:3] == ['exec', 'resume']
    if '-s' in command:
        command[command.index('-s') + 1] = profile['sandbox']
    elif resuming and not any(part.startswith('sandbox_mode=') for part in command):
        command = _before_stdin(command, ['-c', 'sandbox_mode=' + json.dumps(profile['sandbox'])])
    elif not resuming:
        command = _before_stdin(command, ['-s', profile['sandbox']])
    replaced = False
    for index, part in enumerate(command):
        if isinstance(part, str) and part.startswith('approval_policy='):
            command[index] = 'approval_policy="never"'
            replaced = True
    extra = []
    if not replaced:
        extra += ['-c', 'approval_policy="never"']
    if profile['sandbox'] == 'workspace-write':
        existing = [root for root in profile['writable_roots'] if Path(root).is_dir()]
        files = [path for path in profile.get('writable_files', []) if Path(path).is_file()]
        extra += ['-c', 'sandbox_workspace_write.network_access=true',
                  '-c', 'sandbox_workspace_write.exclude_slash_tmp=false',
                  '-c', 'sandbox_workspace_write.exclude_tmpdir_env_var=false',
                  '-c', 'sandbox_workspace_write.writable_roots=' + json.dumps(existing + files)]
        for root in existing:
            if not resuming and root not in command:
                extra += ['--add-dir', root]
    return _before_stdin(command, extra)


def _apply_grok(command, profile):
    command = list(command)
    if '--always-approve' not in command:
        command.append('--always-approve')
    present = set(command)
    for rule in profile['grok_deny']:
        if rule not in present:
            command += ['--deny', rule]
            present.add(rule)
    for rule in profile['grok_allow']:
        command += ['--allow', rule]
    return command


def _apply_agy(command, profile):
    mode = profile.get('permission_mode') or 'accept-edits'
    if mode == 'auto':
        # Auto is account-restricted and silently falls back to prompting.
        mode = 'accept-edits'
    settings, permissions = {}, {}
    remaining = []
    parts = iter(command[1:])
    for part in parts:
        if part == '--dangerously-skip-permissions':
            continue
        if part == '--mode' or part.startswith('--mode='):
            if part == '--mode':
                next(parts)
        elif part == '--settings' or part.startswith('--settings='):
            value = next(parts) if part == '--settings' else part.split('=', 1)[1]
            supplied = json.loads(value)
            if not isinstance(supplied, dict) or not isinstance(supplied.get('permissions', {}), dict):
                raise ValueError('agy --settings and permissions must be JSON objects')
            settings.update(supplied)
            for key, value in supplied.get('permissions', {}).items():
                if key in ('allow', 'deny'):
                    if not isinstance(value, list):
                        raise ValueError('agy permissions.' + key + ' must be a list')
                    permissions.setdefault(key, []).extend(value)
                else:
                    permissions[key] = value
        else:
            remaining.append(part)
            if part in ('-p', '--prompt'):
                # Prompt text is opaque, even when it looks like a flag.
                remaining.append(next(parts))
            elif part == '--':
                remaining.extend(parts)
                break
    for key, rules in (('allow', ['read_file(/)', 'command(*)'] + profile['agy_allow']),
                       ('deny', profile['agy_deny'])):
        permissions[key] = list(dict.fromkeys(permissions.get(key, []) + rules))
    settings['permissions'] = permissions
    return command[:1] + ['--mode', mode, '--settings', json.dumps(settings)] + remaining


def wrap_container(command, run):
    refusal = container_refusal()
    if refusal:
        raise ValueError(refusal)
    repo = run.get('worktree') or run.get('cwd') or os.getcwd()
    task_type = run.get('task_type') or os.getenv('GODFATHER_TASK_TYPE') or 'edit'
    from godfather_safety import notes_path
    notes = notes_path(run['base'], run) if run.get('base') else None
    return container_argv(container_runtime(), container_image(), repo, command, task_type, notes)
