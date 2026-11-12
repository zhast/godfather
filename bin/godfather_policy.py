"""Resolve command effects before making policy decisions. Standard library only."""
import ast
import fnmatch
import glob
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid

DEFAULT_POLICY = '''# Effects inside the run workspace and temporary/scratch roots are allowed.
# Secrets and godfather itself (without --self) are always protected.
# Optional overrides: [[rules]] with binary, args, targets, verdict and id.
# args match argv (flags stay exact); targets are resolved path globs.
# Optional [devbox] names a CLI that provisions a remote dev machine a run may
# own; optional [[outward]] tables declare publish, deploy, or spend commands
# of other CLIs. Both are documented in ARCHITECTURE.md and inactive when absent.
writable_roots = []
process_inspection = true
rules = []
'''
DEVBOX = dict(binary='', stop=['stop', 'down', 'rm', 'delete', 'destroy'], create=['create', 'up', 'start'],
              value_flags=[], id_keys=['id', 'name'], detached_error='',
              created_pattern=r'(?i)\bcreated\s+(?:a\s+)?(?:devbox|box)\s*[:=]?\s*([\w.-]+)')
OUTWARD = dict(binary='', commands=[], read_only=['list', 'get', 'retrieve', 'help'], preview=False)
STREAM_TARGETS = {'/dev/null', '/dev/stdout', '/dev/stderr'}

EXTERNAL_EFFECTS = {'publish commits to a remote', 'publish, deploy, mutate production, or spend money',
                    'execute downloaded shell code', 'resolve an ambiguous shell write',
                    'write downloaded files', 'resolve unknown curl output'}


def config_dir():
    return Path(os.getenv('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'godfather'


def state_dir():
    path = Path(os.getenv('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'godfather'
    path.mkdir(parents=True, exist_ok=True)
    return path


def document(text):
    """Small TOML subset, also on Python 3.8: scalar/array keys and tables."""
    try:
        import tomllib
    except ImportError:
        result, current = {}, None
        pending = ''
        for raw in text.splitlines():
            line = re.sub(r'''("(?:[^"\\]|\\.)*"|'[^']*')|(#.*)''', lambda m: m[1] or '', raw).strip()
            if not line:
                continue
            pending += line
            if pending.startswith('[[') and pending.endswith(']]'):
                current = {}
                result.setdefault(pending[2:-2], []).append(current)
            elif pending.startswith('[') and pending.endswith(']'):
                current = result.setdefault(pending[1:-1], {})
            else:
                key, sep, value = pending.partition('=')
                if not sep:
                    raise ValueError('invalid configuration: ' + pending)
                try:
                    value = re.sub(r'''("(?:[^"\\]|\\.)*"|'[^']*')|\b(true|false)\b''',
                                   lambda m: m[1] or {'true': 'True', 'false': 'False'}[m[2]], value)
                    parsed = ast.literal_eval(value.strip())
                except (ValueError, SyntaxError):
                    if value.strip().startswith('[') and not value.strip().endswith(']'):
                        continue
                    raise ValueError('invalid configuration: ' + pending)
                target = result if current is None else current
                if key.strip() in target:
                    raise ValueError('duplicate configuration key: ' + key)
                target[key.strip()] = parsed
            pending = ''
        if pending:
            raise ValueError('incomplete configuration')
        return result
    return tomllib.loads(text)


def tool_settings(table, defaults, name):
    """Fill one declared tool table from its defaults; values keep the default's type."""
    if not isinstance(table, dict) or set(table) - set(defaults):
        raise ValueError('unknown %s setting' % name)
    merged = dict(defaults, **table)
    for key, default in defaults.items():
        value = merged[key]
        if type(value) is not type(default) or (isinstance(value, list) and not all(isinstance(v, str) for v in value)):
            raise ValueError('%s.%s must be a %s' % (name, key, 'string array' if isinstance(default, list) else type(default).__name__))
    if not re.fullmatch(r'[A-Za-z0-9_.+-]+', merged['binary']):
        raise ValueError(name + '.binary must be a literal executable name')
    return merged


def devbox_settings(table):
    box = tool_settings(table, DEVBOX, 'devbox')
    try:
        groups = re.compile(box['created_pattern']).groups
    except re.error as exc:
        raise ValueError('devbox.created_pattern is not a regular expression') from exc
    if groups != 1 or not box['stop']:
        raise ValueError('devbox needs a stop verb and one capture group in created_pattern')
    return box


def load_policy():
    root = config_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'policy.toml'
    try:
        with path.open('x') as out:
            out.write(DEFAULT_POLICY)
    except FileExistsError:
        pass
    result = {'rules': [], 'writable_roots': [], 'process_inspection': True, 'devbox': None, 'outward': []}
    for source, file in [('user', path), ('learned', root / 'policy.learned.toml')]:
        if not file.exists():
            continue
        data = document(file.read_text())
        # Old configs retain their explicit decisions, but are surfaced as
        # migration-only rules; built-in command patterns no longer drive effects.
        unknown = set(data) - {'rules', 'allow', 'deny', 'guards', 'writable_roots', 'process_inspection', 'devbox', 'outward'}
        if unknown:
            raise ValueError('unknown policy keys: ' + ', '.join(sorted(unknown)))
        if 'devbox' in data:
            result['devbox'] = devbox_settings(data['devbox'])
        if not isinstance(data.get('outward', []), list):
            raise ValueError('outward must be an array of tables')
        result['outward'] += [tool_settings(table, OUTWARD, 'outward') for table in data.get('outward', [])]
        if 'process_inspection' in data:
            if not isinstance(data['process_inspection'], bool):
                raise ValueError('process_inspection must be a boolean')
            result['process_inspection'] = data['process_inspection']
        roots = data.get('writable_roots', [])
        if not isinstance(roots, list) or not all(isinstance(p, str) and p for p in roots):
            raise ValueError('writable_roots must be an array of directory paths')
        for value in roots:
            path = Path(os.path.expandvars(value)).expanduser()
            if not path.is_absolute() or any(c in str(path) for c in '*?[]$'):
                raise ValueError('writable_roots requires absolute paths or ~/ paths, without globs or unset variables')
            resolved_root = str(path.resolve())
            if resolved_root not in result['writable_roots']:
                result['writable_roots'].append(resolved_root)
        for verdict in ('deny', 'allow'):
            for pattern in data.get(verdict, []):
                result['rules'].append(dict(legacy=pattern, verdict='ask' if verdict == 'deny' else 'allow', source=source))
        for rule in data.get('rules', []):
            if set(rule) - {'id', 'binary', 'args', 'targets', 'verdict', 'cwd', 'scope'} or rule.get('verdict') not in ('allow', 'ask', 'deny'):
                raise ValueError('invalid effect rule')
            if rule.get('scope') not in (None, 'cli-tree', 'download-targets'):
                raise ValueError('invalid effect rule')
            if not isinstance(rule.get('binary'), str) or not all(isinstance(rule.get(k, []), list) and all(isinstance(v, str) for v in rule.get(k, [])) for k in ('args', 'targets')):
                raise ValueError('rule requires binary and string arrays')
            result['rules'].append(dict(rule, source=source))
    return result


def incident(base, command, cwd, targets, verdict, answer=None, **extra):
    import fcntl
    from godfather_safety import read
    run = read(str(base) + '.run.json')
    record = dict(id=uuid.uuid4().hex, run=Path(base).name, command=command, cwd=str(cwd), targets=targets,
                  verdict=verdict, answer=answer, time=time.time(), model_key=run.get('model_key'), **extra)
    with (state_dir() / 'incidents.jsonl').open('a') as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        out.write(json.dumps(record) + '\n')
    from godfather_registry import note_incident
    note_incident(record)
    return record


def resolved(value, cwd):
    value = os.path.expandvars(os.path.expanduser(value))
    path = str(Path(cwd) / value)
    if os.path.normpath(path) in STREAM_TARGETS:
        return [os.path.normpath(path)]
    return [str(Path(p).resolve()) for p in (glob.glob(path, recursive=True) or [path])]


def curl_outputs(args, cwd):
    """Collect every explicit curl output; implicit names/configs need a decision."""
    writes = {'-o', '--output', '-D', '--dump-header', '-c', '--cookie-jar',
              '--trace', '--trace-ascii', '--libcurl', '--stderr', '--output-dir',
              '--etag-save', '--hsts', '--alt-svc'}
    unknown_flags = {'-K', '--config', '-O', '--remote-name', '--remote-name-all',
                     '-J', '--remote-header-name'}
    # Short options with arguments end a cluster. Their values are never flags.
    values = set('AbcCdDeEFHKmoPQrtTuUwxyYzX')
    targets, unknown = [], False
    outputs, output_dir, create_dirs = [], None, False
    def finish_outputs():
        directory = str(Path(cwd) / output_dir) if output_dir is not None else cwd
        for value in outputs:
            for target in resolved(value, directory):
                if target not in STREAM_TARGETS:
                    targets.append(target)
                    parent = Path(target).parent
                    if create_dirs and not parent.exists():
                        targets.append(str(parent))
    index = 0
    while index < len(args):
        arg = args[index]
        options = []
        if arg == '--':
            break
        if arg in ('--next', '-:'):
            finish_outputs()
            outputs, output_dir, create_dirs = [], None, False
            index += 1
            continue
        if arg == '--create-dirs':
            create_dirs = True
        elif arg == '--no-create-dirs':
            create_dirs = False
        if arg.startswith('--'):
            flag, sep, value = arg.partition('=')
            if flag in unknown_flags:
                unknown = True
            if flag in writes or flag == '--write-out':
                if not sep:
                    index += 1
                    value = args[index] if index < len(args) else None
                options.append((flag, value))
        elif arg.startswith('-') and arg != '-':
            offset = 1
            while offset < len(arg):
                flag = '-' + arg[offset]
                if flag in unknown_flags:
                    unknown = True
                if arg[offset] in values:
                    value = arg[offset + 1:]
                    if not value:
                        index += 1
                        value = args[index] if index < len(args) else None
                    options.append((flag, value))
                    break
                offset += 1
        for flag, value in options:
            if value is None:
                unknown = True
            elif flag in ('-o', '--output'):
                if value != '-':
                    outputs.append(value)
            elif flag in writes:
                if flag == '--output-dir':
                    output_dir = value
                if flag in ('--hsts', '--alt-svc') and not value:
                    continue  # empty cache names are explicitly memory-only
                if value != '-' or flag in ('--etag-save', '--hsts', '--alt-svc'):
                    targets += [p for p in resolved(value, cwd) if p not in STREAM_TARGETS]
            elif flag in ('-w', '--write-out') and not value.startswith('@'):
                for output in re.findall(r'%output\{(.*?)\}', value):
                    output = output[2:] if output.startswith('>>') else output
                    if output != '-':
                        targets += [p for p in resolved(output, cwd) if p not in STREAM_TARGETS]
        index += 1
    finish_outputs()
    return targets, unknown


def _index_only(args):
    """git restore --staged (or -S) without --worktree touches only the index."""
    staged = worktree = False
    index = 1
    while index < len(args):
        arg = args[index]
        if arg == '--':
            break
        if arg == '--staged':
            staged = True
        elif arg == '--worktree':
            worktree = True
        elif arg in ('--source', '-s', '--pathspec-from-file') and index + 1 < len(args):
            index += 1
        elif arg.startswith('--source=') or arg.startswith('--pathspec-from-file='):
            pass
        elif arg.startswith('-') and not arg.startswith('--'):
            if 'S' in arg:
                staged = True
            if 'W' in arg:
                worktree = True
            if arg.endswith('s'):
                index += 1
        index += 1
    return staged and not worktree


def operands(args, value_flags=()):
    values, skip, literal = [], False, False
    for arg in args:
        if skip:
            skip = False
        elif arg == '--' and not literal:
            literal = True
        elif not literal and arg in value_flags:
            skip = True
        elif literal or not arg.startswith('-'):
            values.append(arg)
    return values


def effects(argv, cwd, declared=()):
    """Return (resolved writes, effect description). Empty writes imply read-only."""
    cmd, args = Path(argv[0]).name, list(argv[1:])
    targets, reason = [], ''
    if args == ['--help'] or args == ['-h']:
        return targets, reason
    if cmd == 'git':
        while args and args[0].startswith('-'):
            flag = args.pop(0)
            if flag in ('-C', '-c', '--work-tree', '--git-dir', '--config-env') and args:
                value = args.pop(0)
                if flag in ('-C', '--work-tree'):
                    cwd = str((Path(cwd) / value).resolve())
            elif flag.startswith('--work-tree='):
                cwd = str((Path(cwd) / flag.split('=', 1)[1]).resolve())
        if args and args[0] == 'restore' and _index_only(args):
            # --staged without --worktree rewrites the index only. Pathspecs
            # such as `:/` must not be expanded into worktree files.
            return [], 'update the git index'
        if args and args[0] == 'push':
            reason = 'publish commits to a remote'
        if args and args[0] in ('clean', 'reset', 'restore', 'checkout'):
            verb = args[0]
            destructive = (verb == 'clean' and not any(v == '--dry-run' or v.startswith('-') and not v.startswith('--') and 'n' in v for v in args[1:]) or
                           verb == 'reset' and any(v in args for v in ('--hard', '--merge', '--keep')) or
                           verb == 'restore' or verb == 'checkout' and '--' in args)
            if destructive:
                from godfather_safety import original_environment
                if verb == 'reset':
                    try:
                        repo = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                                              env=original_environment(), capture_output=True, text=True, timeout=5)
                        if repo.returncode == 0:
                            cwd = repo.stdout.strip()
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                paths = args[args.index('--') + 1:] if '--' in args else operands(args[1:], ('--source', '-s')) if verb == 'restore' else []
                targets = [p for value in (paths or ['.']) for p in resolved(value, cwd)]
                reason = 'discard files'
                command = ['git', '-C', str(cwd), 'ls-files', '-z']
                if verb == 'clean':
                    command += ['--others'] + ([] if any('x' in flag for flag in args[1:] if flag.startswith('-')) else ['--exclude-standard'])
                command += ['--'] + (paths or ['.'])
                try:
                    listing = subprocess.run(command, env=original_environment(), capture_output=True, timeout=5)
                    if listing.returncode == 0:
                        targets += [str((Path(cwd) / name).resolve()) for name in listing.stdout.decode(errors='replace').split('\0') if name]
                except (OSError, subprocess.TimeoutExpired):
                    pass
    elif cmd in ('kill', 'pkill', 'killall'):
        reason = 'signal processes'
    elif cmd in ('chmod', 'chown'):
        values = operands(args, ('--reference', '--from'))
        files = values[1:]
        targets = [p for value in files for p in resolved(value, cwd)]
        reason = 'change file metadata' if files else ''
    elif cmd in ('rm', 'mv', 'cp', 'truncate', 'tee', 'touch', 'install', 'sed'):
        value_flags = ('-t', '--target-directory', '-S', '--suffix') if cmd in ('cp', 'mv') else ('-s', '--size', '-r', '--reference') if cmd == 'truncate' else ('-m', '--mode', '-o', '-g', '-t', '--target-directory') if cmd == 'install' else ()
        values = operands(args, value_flags)
        target_dir = next((args[i + 1] for i, v in enumerate(args[:-1]) if v in ('-t', '--target-directory')), None)
        target_dir = next((v.split('=', 1)[1] for v in args if v.startswith('--target-directory=')), target_dir)
        sources = list(values if target_dir else values[:-1]) if cmd in ('cp', 'mv', 'install') else []
        if cmd == 'sed':
            values = values[1:] if any(v == '-i' or v.startswith('-i') for v in args) else []
        if cmd in ('cp', 'install'):
            values = [] if target_dir else values[-1:]
        if cmd in ('cp', 'mv', 'install'):
            for flag in ('-t', '--target-directory'):
                if flag in args:
                    values += [args[args.index(flag) + 1]]
            values += [v.split('=', 1)[1] for v in args if v.startswith('--target-directory=')]
        targets = [p for value in values for p in resolved(value, cwd)]
        if cmd in ('cp', 'mv', 'install') and values:
            dest_value = target_dir or values[-1]
            dest = Path(cwd) / dest_value
            for value in sources:
                for source in resolved(value, cwd):
                    source_path = Path(source)
                    output = dest / source_path.name if dest.is_dir() else dest
                    targets.append(str(output.resolve()))
                    if source_path.is_dir():
                        for directory, dirs, files in os.walk(source_path):
                            dirs[:] = [d for d in dirs if not Path(directory, d).is_symlink()]
                            targets.extend(str((output / Path(directory).relative_to(source_path) / f).resolve()) for f in files)
        if cmd in ('rm', 'mv'):
            for target in list(targets):
                if Path(target).is_dir():
                    for directory, dirs, files in os.walk(target):
                        dirs[:] = [d for d in dirs if not Path(directory, d).is_symlink()]
                        targets.extend(str(Path(directory, f).resolve()) for f in files)
        reason = 'delete or overwrite files' if targets else ''
    elif cmd == 'curl':
        targets, unknown = curl_outputs(args, cwd)
        reason = 'resolve unknown curl output' if unknown else ('write downloaded files' if targets else '')
    # Strip known global options before classifying outward actions.
    globals_ = {'-R', '--repo', '--hostname', '--registry', '--prefix', '--workspace', '-w', '--account', '--api-key', '--context', '--namespace', '-n', '--kubeconfig', '--server', '-s', '--cwd', '-C', '--stack'}
    rest = operands(args, globals_)
    prefix = tuple(rest[:2])
    outward = (cmd == 'gh' and prefix in (('repo', 'create'), ('repo', 'delete'), ('pr', 'merge')) or
               cmd == 'npm' and rest[:1] == ['publish'] or cmd == 'terraform' and rest[:1] == ['apply'] or
               cmd == 'pulumi' and rest[:1] == ['up'] or cmd == 'kubectl' and rest[:1] == ['delete'] or
               cmd == 'docker' and prefix == ('system', 'prune') or
               cmd in ('vercel', 'netlify', 'fly', 'wrangler') and 'deploy' in rest and '--preview' not in args or
               any(cmd == rule['binary'] and len(rest) > 1 and rest[0] in rule['commands'] and
                   rest[1] not in rule['read_only'] and not (rule['preview'] and '--preview' in args) for rule in declared))
    # Help-only probes are an allow, classified before publish, deploy, or spend.
    if args and args[-1] in ('--help', '-h') and all(not v.startswith('-') for v in args[:-1]):
        return [], ''
    if outward:
        reason = 'publish, deploy, mutate production, or spend money'
    if cmd in ('sh', 'bash', 'zsh', 'dash', 'ksh'):
        for i, arg in enumerate(args[:-1]):
            if arg.startswith('-') and 'c' in arg:
                command = args[i + 1]
                if re.search(r'\bcurl\b[^\n]*\|\s*(?:/\S*/)?(?:ba)?sh\b', command):
                    reason = 'execute downloaded shell code'
                lex = shlex.shlex(command, posix=True, punctuation_chars=';&|()<>')
                lex.whitespace_split = True
                tokens = list(lex)
                segment = []
                for index, token in enumerate(tokens + [';']):
                    if token in ('>', '>>', '>|', '&>', '&>>') and index + 1 < len(tokens):
                        if not tokens[index + 1].startswith('&'):
                            writes = [p for p in resolved(tokens[index + 1], cwd) if p not in STREAM_TARGETS]
                            targets += writes
                            if writes:
                                reason = reason or 'write redirected output'
                            if '$' in tokens[index + 1] or '`' in tokens[index + 1]:
                                reason = 'resolve an ambiguous shell write'
                    if token and all(c in ';&|()<>' for c in token):
                        if segment:
                            while segment and re.match(r'^\w+=', segment[0]):
                                segment.pop(0)
                            if segment:
                                if segment[0] == 'cd' and len(segment) == 2:
                                    cwd = str((Path(cwd) / segment[1]).resolve())
                                else:
                                    paths, effect = effects(segment, cwd, declared)
                                    targets += paths
                                    if effect and reason not in EXTERNAL_EFFECTS:
                                        reason = effect
                        segment = []
                    else:
                        segment.append(token)
                break
    # Detect literal interpreter file writes where possible, without claiming to
    # analyze arbitrary code or direct file-tool operations.
    if cmd in ('python', 'python3', 'node', 'perl', 'ruby') and any(v in args for v in ('-c', '-e')):
        code = args[-1]
        if re.search(r'write|unlink|remove|truncate', code) or re.search(r'''open\([^)]*,\s*["'][^"']*[wax+][^"']*["']''', code):
            for value in re.findall(r'''(?:Path|open|writeFileSync|writeFile|unlink|remove|truncate)\(\s*["']([^"'\n]+)["']''', code):
                targets += resolved(value, cwd)
            reason = 'write files through an interpreter' if targets else reason
    if cmd not in ('rm', 'mv', 'chmod', 'chown', 'sh', 'bash', 'zsh', 'dash', 'ksh'):
        had_targets = bool(targets)
        targets = [p for p in targets if p not in STREAM_TARGETS]
        if had_targets and not targets and reason not in EXTERNAL_EFFECTS:
            reason = ''
    return sorted(set(targets)), reason


def secret(value):
    return any(part.startswith('.env') or part in ('.ssh', '.gnupg', 'keys', 'credentials', 'credentials.json', 'credentials.toml', 'id_rsa', 'id_ed25519', '.netrc', '.npmrc', 'auth.json', 'secrets.json', 'secrets.toml') or part.endswith(('.pem', '.key', '.p12', '.pfx', '.keystore', '_creds.json', 'access_tokens.db')) for part in Path(value).parts)


def writable(target, context):
    from godfather_safety import inside
    from godfather_permissions import created_by_run, scratch_roots
    path = Path(target)
    if context.get('notes_path') and path.resolve() == Path(context['notes_path']).resolve():
        return True
    roots = [context['cwd'], '/tmp', os.getenv('TMPDIR', '/tmp')]
    roots += glob.glob('/var/folders/*/*/T') + glob.glob('/var/folders/*/T')
    roots += context.get('scratch', [])
    if any(inside(path, Path(root).resolve()) for root in roots):
        return True
    if any(inside(path, root.resolve()) for root in scratch_roots()) and created_by_run(path, context):
        return True
    base = context.get('base')
    return bool(base and path.parent == Path(base).parent and path.name.startswith(Path(base).name)) or bool(base and any(inside(path, p) for p in Path(base).parent.glob(Path(base).name + '*') if p.is_dir()))


def safe_download_options(argv):
    """Generalize only understood download options, never an added upload/file effect."""
    flags = {'-s', '-S', '-L', '-f', '-k', '-I', '--silent', '--show-error', '--location',
             '--fail', '--insecure', '--head', '--compressed'}
    values = {'-o', '--output', '-w', '--write-out', '--url', '--connect-timeout', '--max-time',
              '-m', '--retry', '--retry-delay', '-H', '--header'}
    args = iter(argv[1:])
    for arg in args:
        if not arg.startswith('-') or arg in flags:
            continue
        if arg in values:
            value = next(args, None)
            if value is None or (arg in ('-w', '--write-out') and '%output{' in value):
                return False
        elif arg.startswith('--output=') or (arg.startswith('-o') and len(arg) > 2):
            continue
        elif arg.startswith('-') and not arg.startswith('--') and set(arg[1:]) <= set('sSLfkI'):
            continue
        else:
            return False
    return True


def evaluate(argv, context, policy=None, cwd=None):
    from godfather_safety import inside, SOURCE_BIN
    cwd = str(Path(cwd or os.getcwd()).resolve())
    policy = policy or load_policy()
    targets, reason = effects(argv, cwd, policy.get('outward', ()))
    result = dict(targets=targets, effect=reason or 'read files', verdict='allow', cwd=cwd)
    box = policy.get('devbox')
    stops = devbox_stops(argv, box) if box else []
    if stops:
        owned = {r.get('id') for r in context.get('resources', [])
                 if r.get('kind') == 'devbox' and r.get('owned') is True}
        for args in stops:
            ids = [v for v in args if not v.startswith('-')]
            ids += [v.split('=', 1)[1] for v in args if v.startswith(('--id=', '--name='))]
            if not ids:
                from godfather_safety import attached_devbox, devbox_id
                ids = [devbox_id(attached_devbox(cwd), box['id_keys'])]
            if any(v in args for v in ('--all', '-a')) or not all(v in owned for v in ids):
                return dict(result, verdict='deny', rule='devbox-ownership',
                            effect='stop or destroy a devbox this run did not create (adopted or unknown devbox)')
    installs = [Path(__file__).resolve().parent]
    if SOURCE_BIN:
        installs += [Path(SOURCE_BIN).resolve().parent]
    elif (installs[0].parent / 'install.sh').exists():
        installs += [installs[0].parent]
    if any(secret(p) for p in targets):
        return dict(result, verdict='deny', effect='write a secret file')
    if not context.get('self_allowed') and any(inside(Path(p), root) for p in targets for root in installs):
        return dict(result, verdict='deny', effect='change godfather without --self')
    if reason == 'update the git index':
        return dict(result, verdict='allow', effect=reason)
    if any(not writable(p, context) for p in targets) or (reason and not targets) or reason in EXTERNAL_EFFECTS:
        result['verdict'] = 'ask'
    for rule in policy['rules']:
        if rule.get('scope') == 'cli-tree':
            from godfather_permissions import flag_args, under_tree
            matches = (Path(argv[0]).name == rule['binary'] and flag_args(argv, targets, cwd) == rule.get('args', []) and
                       bool(targets) and all(under_tree(p, rule.get('targets', [])) for p in targets))
        elif rule.get('scope') == 'download-targets':
            matches = (Path(argv[0]).name == rule['binary'] == 'curl' and safe_download_options(argv) and
                       reason == 'write downloaded files' and bool(targets) and
                       all(any(fnmatch.fnmatchcase(p, g) and Path(p).parent == Path(g).parent
                               for g in rule.get('targets', [])) for p in targets))
        elif 'legacy' in rule:
            rendered = ' '.join(shlex.quote(v) for v in argv)
            matches = fnmatch.fnmatchcase(rendered, rule['legacy'])
        else:
            matches = (Path(argv[0]).name == rule['binary'] and len(argv[1:]) == len(rule.get('args', [])) and all((value == pattern if value.startswith('-') else fnmatch.fnmatchcase(value, pattern)) for value, pattern in zip(argv[1:], rule.get('args', []))) and
                       (not rule.get('cwd') or cwd == rule['cwd']) and len(targets) == len(rule.get('targets', [])) and
                       all((Path(p).parent == Path(g).parent and fnmatch.fnmatchcase(Path(p).name, Path(g).name) if rule['source'] == 'learned' else fnmatch.fnmatchcase(p, g)) for p, g in zip(targets, rule.get('targets', []))))
        if matches:
            result['verdict'] = rule['verdict']
            result['rule'] = rule.get('id', rule.get('legacy'))
    return result


def devbox_stops(argv, box):
    """Ownership is a hard rule, including literal commands in shell wrappers."""
    cmd, args = Path(argv[0]).name, list(argv[1:])
    if cmd == box['binary']:
        # Help belongs to this invocation, never to a wrapping shell's argv.
        # Top-level help is dispatched before global flags are parsed.
        if args and (args[0] in ('help', '--help', '-h') or
                     args[0] in box['stop'] and any(v in args[1:] for v in ('--help', '-h'))):
            return []
        index = 0
        while index < len(args):
            value = args[index]
            if value in box['value_flags']:
                index += 2
                continue
            if value.startswith('-'):
                index += 1
                continue
            if value in box['stop']:
                return [args[index + 1:]]
            break  # Another command owns the remaining positional arguments.
    if cmd in ('sh', 'bash', 'zsh', 'dash', 'ksh'):
        for index, value in enumerate(args[:-1]):
            if value.startswith('-') and 'c' in value:
                lex = shlex.shlex(args[index + 1], posix=True, punctuation_chars=';&|()<>')
                lex.whitespace_split = True
                found, segment = [], []
                for token in list(lex) + [';']:
                    if token and all(c in ';&|()<>' for c in token):
                        while segment and re.match(r'^\w+=', segment[0]):
                            segment.pop(0)
                        if segment:
                            found.extend(devbox_stops(segment, box))
                        segment = []
                    else:
                        segment.append(token)
                return found
    return []


def remember(question, base):
    import fcntl
    from godfather_permissions import flag_args, owned_tree
    effect = question.get('effect')
    if not effect:
        return None
    argv = effect['command']
    trees = []
    for target in effect['targets']:
        root = owned_tree(target)
        if root is None:
            trees = None
            break
        trees.append(root)
    tree = trees[0] if trees and all(root == trees[0] for root in trees) else None
    if Path(argv[0]).name == 'curl' and effect.get('effect') == 'write downloaded files' and effect['targets']:
        targets = [str(Path(p).parent / (Path(p).stem + '*' + Path(p).suffix)) for p in effect['targets']]
        rule = dict(id=uuid.uuid4().hex[:12], binary='curl', targets=targets, verdict='allow',
                    scope='download-targets', summary='curl downloads under ' + ', '.join(targets))
    elif tree is not None:
        # Same binary and flags, anywhere under that CLI-owned tree. The
        # summary is what the conductor sees, so a wide tree is visible.
        args = flag_args(argv, effect['targets'], effect['cwd'])
        targets = [str(tree) + '/**']
        summary = '%s %s under %s/**' % (Path(argv[0]).name, ' '.join(args), tree)
        rule = dict(id=uuid.uuid4().hex[:12], binary=Path(argv[0]).name, args=args, targets=targets,
                    verdict='allow', scope='cli-tree', summary=summary)
    else:
        # Outside a CLI-owned tree, keep a single basename wildcard.
        def narrow_glob(value):
            path = Path(value)
            stem = re.sub(r'[-_.]?(?:\d+|[a-f0-9]{8,})$', '', path.stem) or path.stem
            return str(path.parent / (stem + '*' + path.suffix))
        targets = [narrow_glob(p) for p in effect['targets']]
        args = [narrow_glob(v) if not v.startswith('-') and any(p in effect['targets'] for p in resolved(v, effect['cwd'])) else v for v in argv[1:]]
        summary = '%s %s under %s' % (Path(argv[0]).name, ' '.join(args), ', '.join(targets))
        rule = dict(id=uuid.uuid4().hex[:12], binary=Path(argv[0]).name, args=args, targets=targets, cwd=effect['cwd'], verdict='allow', summary=summary)
    path = config_dir() / 'policy.learned.toml'
    path.parent.mkdir(parents=True, exist_ok=True)
    with (config_dir() / 'policy.learned.lock').open('a') as lock, path.open('a') as out:
        fcntl.flock(lock, fcntl.LOCK_EX)
        out.write('\n# Approved from run {} question {}\n[[rules]]\n'.format(Path(base).name, question['id']))
        for key, value in rule.items():
            if key == 'summary':
                continue
            out.write(key + ' = ' + json.dumps(value) + '\n')
    return rule


def policy_main(args):
    policy = load_policy()
    if args.action == 'forget':
        import fcntl
        path = config_dir() / 'policy.learned.toml'
        with (config_dir() / 'policy.learned.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = document(path.read_text()) if path.exists() else {'rules': []}
            rules = [r for r in data.get('rules', []) if r.get('id') != args.id]
            if len(rules) == len(data.get('rules', [])):
                raise ValueError('unknown learned rule: ' + str(args.id))
            temp = path.with_suffix('.tmp')
            temp.write_text('\n'.join('\n[[rules]]\n' + '\n'.join(k + ' = ' + json.dumps(v) for k, v in r.items()) for r in rules))
            os.replace(temp, path)
        return 0
    print('default: allow read-only and writable-root effects; ask external destruction and outward actions; deny secrets and self edits')
    for root in policy['writable_roots']:
        print('native writable root: ' + root)
    for rule in policy['rules']:
        print(rule['source'] + ': ' + json.dumps(rule, sort_keys=True))
    return 0
