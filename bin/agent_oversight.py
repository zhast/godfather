#!/usr/bin/env python3
"""Local CLI oversight; standard library only. Registry writes are atomic."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import select
import signal
import shutil
import subprocess
import sys
import time
import uuid

from godfather_store import NONTERMINAL, TERMINAL

HOME = Path.home()
CODEX_APP = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
from godfather_errors import REGEX, AGY_STARTUP, diagnostic_noise
QUOTA = REGEX['quota']
ERROR = re.compile(r'\berror\b|\bfailed\b|stream was interrupted|not logged in|unauthorized|authentication failed|invalid.api.key|permission denied|\b401\b|\b403\b', re.I)


def backend_home(cli, run=None):
    """Resolve the selected home from saved run metadata before the caller's env."""
    run = run or {}
    if cli == 'cursor':
        from godfather_cursor import home
        return home(run.get('cursor_home') or run.get('launcher_env', {}).get('CURSOR_HOME'))
    variable = cli.upper() + '_HOME'
    value = (run.get(cli + '_home') or run.get('launcher_env', {}).get(variable) or
             os.getenv(variable) or str(Path.home() / ('.' + cli)))
    return Path(os.path.abspath(os.path.expanduser(value)))


def codex_home(run=None):
    return backend_home('codex', run)


def grok_home(run=None):
    return backend_home('grok', run)


def display_model(run):
    key = run.get('model_key')
    if run.get('cli') == 'cursor':
        return run.get('model', '?')
    return key.split(':', 1)[1] if key else run.get('model', '?')


def find_codex():
    """Explicit override wins, then PATH, then the macOS desktop bundle."""
    override = os.getenv('CODEX_BIN')
    candidate = (shutil.which(os.path.expanduser(override)) if override else
                 shutil.which('codex') or (CODEX_APP if os.access(CODEX_APP, os.X_OK) else None))
    if not candidate:
        raise FileNotFoundError('Codex not found; install codex or set CODEX_BIN to an executable')
    return str(Path(candidate).resolve())


def codex_model():
    """Never guess an unsupported model when no catalog is available."""
    configured = os.getenv('CODEX_MODEL', 'auto')
    if configured not in ('', 'auto'):
        return configured
    cache = codex_home() / 'models_cache.json'
    data = read(cache)
    models = data.get('models', []) if isinstance(data, dict) else []
    if any(isinstance(m, dict) and m.get('slug') == 'gpt-6.1-sol' for m in models):
        return 'gpt-6.1-sol'
    return 'default'


def saved_file(path):
    """True when a credential or state file exists. Never read its contents."""
    try:
        return path.is_file() and path.stat().st_size > 2
    except OSError:
        return False


def cli_binary(cli):
    """Locate a CLI without executing it."""
    try:
        if cli == 'codex':
            return find_codex()
    except FileNotFoundError:
        return None
    if cli == 'cursor':
        candidate = shutil.which(os.path.expanduser(os.getenv('CURSOR_AGENT_BIN', 'cursor-agent')))
        return str(Path(candidate).resolve()) if candidate else None
    return shutil.which(cli)


def passive_login(cli):
    """Login evidence from credential or state files. This does not launch a CLI."""
    home = Path.home()
    if cli == 'codex':
        root = Path(os.getenv('CODEX_HOME', str(home / '.codex')))
        if saved_file(root / 'auth.json'):
            return 'saved login present (not remotely verified)'
        return 'no saved login detected'
    if cli == 'cursor':
        from godfather_cursor import home as cursor_home
        if saved_file(cursor_home() / 'auth.json'):
            return 'saved login present (not remotely verified)'
        if os.getenv('CURSOR_API_KEY'):
            return 'API credential configured (not verified)'
        # Do not fetch keychain secret values for a passive doctor check.
        return 'no saved auth file detected; keychain login may be configured (not verified)'
    if cli == 'agy':
        gemini = home / '.gemini'
        if saved_file(gemini / 'oauth_creds.json') or saved_file(gemini / 'google_accounts.json'):
            return 'saved login present (not remotely verified)'
        if os.getenv('GEMINI_API_KEY'):
            return 'API credential configured (not verified)'
        state = gemini / 'antigravity-cli'
        if saved_file(state / 'settings.json') or saved_file(state / 'installation_id'):
            return 'local state present (not remotely verified)'
        return 'no credential file on disk (not remotely verified)'
    if saved_file(grok_home() / 'auth.json'):
        return 'saved login present (not remotely verified)'
    if os.getenv('XAI_API_KEY'):
        return 'API credential configured (not verified)'
    return 'no saved login detected; external auth may still be configured'


def live_login(cli, binary):
    """The only CLI launches doctor performs, and only when --live is set."""
    try:
        if cli == 'codex':
            check = subprocess.run([binary, 'login', 'status'], capture_output=True, text=True, timeout=5)
            detail = (check.stdout + check.stderr).lower()
            return ('logged in' if check.returncode == 0 else
                    'not logged in' if 'not logged in' in detail else 'unknown (status check failed)')
        if cli == 'agy':
            # Slash commands outside a project do not start a model task.
            import tempfile
            with tempfile.TemporaryDirectory(prefix='godfather-auth-') as temp:
                check = subprocess.run([binary, '-p', '/usage'], cwd=temp,
                                       capture_output=True, text=True, timeout=8)
            detail = (check.stdout + check.stderr).lower()
            return ('logged in' if check.returncode == 0 else
                    'not logged in' if any(s in detail for s in ('not logged in', 'unauthenticated', 'sign in'))
                    else 'unknown (quota/auth check failed)')
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown (status check unavailable or timed out)'
    return passive_login(cli)


def install_drift(prefix=None):
    """Compare manifest-owned scripts with their source, without changing either."""
    from godfather_safety import SOURCE_BIN
    prefix = Path(prefix) if prefix is not None else Path(__file__).resolve().parent
    manifest = prefix / '.godfather-install.json'
    try:
        owned = json.loads(manifest.read_text())
        if not isinstance(owned, dict):
            raise ValueError('expected a file inventory')
    except (OSError, ValueError) as exc:
        print('install drift check unavailable: cannot read ' + str(manifest) + ' (' + str(exc) + ')')
        return
    source = owned.get('_source_bin') or SOURCE_BIN
    if not source:
        print('install drift check unavailable: source directory not recorded; reinstall from its source checkout')
        return
    clean = True
    for name in sorted(owned):
        if name == '_source_bin':
            continue
        if Path(name).name != name or name in ('.', '..'):
            clean = False
            print('install drift check: invalid manifest filename ' + repr(name))
            continue
        try:
            installed, current = (prefix / name).read_bytes(), (Path(source) / name).read_bytes()
            if name == 'godfather_safety.py':
                installed, current = [re.sub(rb'^SOURCE_BIN = [^\r\n]*', b'SOURCE_BIN = <install-stamp>',
                                             data, flags=re.M) for data in (installed, current)]
            if installed != current:
                clean = False
                print('install differs from source: ' + name)
        except OSError as exc:
            clean = False
            print('install drift check: ' + name + ' unavailable (' + str(exc) + ')')
    if clean:
        print('install matches source')


def doctor(live=False):
    """Report auth without printing credential values.

    Defaults read local credential presence only. --live adds Cursor status/about,
    codex login status and agy usage. Grok stays passive.
    """
    install_drift()
    print('CLI auth (live probes):' if live else 'CLI auth (passive; no CLI launched):')
    for cli in ('codex', 'agy', 'grok', 'cursor'):
        binary = cli_binary(cli)
        if not binary:
            print(f'{cli}: missing; login: unavailable')
            continue
        login = live_login(cli, binary) if live and cli not in ('grok', 'cursor') else passive_login(cli)
        if cli == 'cursor' and live:
            from godfather_cursor import environment
            try:
                result = subprocess.run([binary, 'status'], env=environment(), capture_output=True, text=True, timeout=10)
                login = 'not logged in' if result.returncode or 'not logged in' in (result.stdout + result.stderr).lower() else 'logged in'
                about = subprocess.run([binary, 'about'], env=environment(), capture_output=True, text=True, timeout=10)
                tier = next((line.strip() for line in about.stdout.splitlines() if 'Subscription Tier' in line), '')
                login += ('; ' + tier) if tier else ''
            except (OSError, subprocess.TimeoutExpired):
                login = 'status unavailable'
        print(f'{cli}: installed; login: {login}')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def epoch(value):
    if isinstance(value, (int, float)):
        return value
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return time.time()


def compact(value, limit=79):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return re.sub(r'\s+', ' ', re.sub(r'\x1b\[[0-9;]*m', '', value)).strip()[:limit]


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def write(path, data):
    path = Path(path)
    if path.name.endswith('.run.json'):
        from godfather_safety import registry_write
        registry_write(path, data)
        return
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temp, path)
    from godfather_registry import note_write
    note_write(path, data)


def processes():
    result = {}
    out = subprocess.run(['ps', '-axo', 'pid=,ppid=,pgid=,stat=,lstart=,args='], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split(None, 9)
        if len(parts) == 10:
            pid = int(parts[0])
            try:
                sid = os.getsid(pid)
            except OSError:
                sid = None
            result[pid] = dict(ppid=int(parts[1]), pgid=int(parts[2]), sid=sid, stat=parts[3],
                               identity=' '.join(parts[4:9]), command=parts[9])
    return result


def alive(pid, identity=None, procs=None):
    p = (procs if procs is not None else processes()).get(pid)
    return bool(p and 'Z' not in p['stat'] and (not identity or p['identity'] == identity))


def run_alive(run, procs=None):
    return alive(run.get('pid'), run.get('pid_identity'), procs)


def session_members(run, procs):
    sid = run.get('process_session_id')
    leader = procs.get(sid, {})
    if (not sid or sid != run.get('pid') or sid == os.getsid(0)
            or (leader and leader.get('identity') != run.get('pid_identity'))):
        return set()
    return {pid for pid, row in procs.items() if row.get('sid') == sid and alive(pid, row.get('identity'), procs)}


def owners_alive(run, procs=None, root=None):
    procs = processes() if procs is None else procs
    if any(alive(run.get(key), run.get(identity), procs) for key, identity in
           (('pid', 'pid_identity'), ('launcher_pid', 'launcher_identity'), ('supervisor_pid', 'supervisor_identity'))):
        return True
    if session_members(run, procs):
        return True
    route = read(str(Path(root or runs_dir()) / (run.get('router_name') or run['name'])) + '.route.json')
    return route.get('status') == 'running' and alive(route.get('router_pid'), route.get('router_identity'), procs)


def transcript(session):
    return HOME / '.gemini/antigravity-cli/brain' / session / '.system_generated/logs/transcript.jsonl'


def discover(run):
    """Only accept a session ID from this child's private output/log, never newest-mtime."""
    cli = run['cli']
    session = run.get('conversation_id') or run.get('session_id')
    if not session:
        source = run.get('discovery_path')
        try:
            with open(source, 'rb') as f:
                text = f.read(256000).decode(errors='replace')
        except (OSError, TypeError):
            text = ''
        pattern = (r'session id:\s*(' + UUID + ')' if cli == 'codex' else
                   r'(?:Print mode: conversation=|Streaming conversation |resuming conversation |created conversation )(' + UUID + ')')
        found = re.search(pattern, text, re.I)
        if cli == 'codex':
            # Native exec --json separates CLI errors from shell-tool results.
            for line in text.splitlines():
                try:
                    event = json.loads(line)
                    if event.get('type') == 'thread.started' and re.fullmatch(UUID, event.get('thread_id', '')):
                        run['codex_stream'] = True
                        session = event['thread_id']
                        run['session_id'] = session
                        break
                except (ValueError, AttributeError, TypeError):
                    pass
        if cli == 'cursor':
            for line in text.splitlines():
                try:
                    event = json.loads(line)
                    session = event.get('session_id') or event.get('chat_id') or event.get('chatId')
                    if session:
                        run['session_id'] = session
                        break
                except (ValueError, AttributeError):
                    pass
        if found:
            session = found[1]
            run['conversation_id' if cli == 'agy' else 'session_id'] = session
    paths = run['progress_paths']
    if session:
        session_root = codex_home(run) / 'sessions'
        if cli == 'codex' and not run.get('codex_stream') and not any(str(session_root) + '/' in p for p in paths):
            for p in session_root.glob(f'**/*{session}.jsonl'):
                # Verify metadata, in addition to the UUID in the filename.
                try:
                    with p.open() as f:
                        meta = json.loads(f.readline()).get('payload', {})
                    if (meta.get('id') or meta.get('session_id')) == session:
                        paths.append(str(p))
                        break
                except (OSError, ValueError):
                    pass
        elif cli == 'agy':
            p = str(transcript(session))
            if p not in paths:
                paths.append(p)
        elif cli == 'grok' and not run.get('storage_path'):
            for p in (grok_home(run) / 'sessions').glob(f'*/{session}/updates.jsonl'):
                # Retained for inspection/resume. Parse streaming output, not both copies.
                run['storage_path'] = str(p.parent)
                break
    return run


class Progress:
    """Incremental readers retain incomplete JSON lines and detect truncation/replacement."""
    def __init__(self, run, audit_base=None):
        self.audit_base = audit_base
        self.offsets = dict(run.get('progress_offsets', {}))
        self.inodes = {}
        self.last_at = epoch(run.get('segment_started_at', run['started_at']))
        self.action = 'starting'
        self.steps = 0
        self.tokens_in = self.tokens_out = self.tokens_cached = None
        self.stream_at = run.get('model_stream_at', 0)
        self.errors = []
        self.events = []
        self.seen_errors = set()
        self.previous_action = None
        self.repeated = 0
        self.loop_reported = False
        self.prose = 0
        self.prose_reported = False
        self.calls = {}
        self.agy_steps = set()
        self.grok_usage = {}
        self.cli = run['cli']
        self.pending_auth = None
        self.failure = None
        self.codex_log_context = None
        self.tails = {}
        self.book = None
        self.heartbeat_root = None

    def error(self, detail, event=None):
        detail = compact(detail, 1000)
        # Strip glog timestamps, thread IDs and callsites before deduplication.
        detail = re.sub(r'^[IWEF]\d{4} \S+ \d+ [^]]+\] ', '', detail)
        from godfather_errors import classify
        failure = classify(self.cli, event if event is not None else detail)
        noise = diagnostic_noise(self.cli, detail)
        if failure['source'] == 'text' and noise:
            if noise == 'silent_auth':
                # Silent auth often recovers. Only surface this if the CLI fails.
                self.pending_auth = 'You are not logged into Antigravity (silent authentication may recover)'
            return
        from godfather_stream import diagnostic_level
        level = diagnostic_level(detail)
        if failure['source'] == 'text' and level != 'error':
            if detail not in self.seen_errors:
                self.seen_errors.add(detail)
                self.events.append((level.upper(), compact(detail, 240)))
            return
        if failure['transient']:
            return
        self.failure = failure
        detail = compact(detail, 240)
        if detail and detail not in self.seen_errors:
            self.seen_errors.add(detail)
            self.errors.append(detail)
            self.events.append(('QUOTA' if failure['category'] == 'quota' else 'ERROR', detail))

    def tool(self, name, args):
        if self.audit_base:
            from godfather_safety import audit
            audit(self.audit_base, 'cli:' + name, args)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                pass
        label = name + ' ' + compact(args, 1000)
        value = args
        if isinstance(args, dict):
            value = next((args[k] for k in ('AbsolutePath', 'TargetFile', 'FilePath', 'file_path', 'path', 'CommandLine', 'command', 'cmd', 'code', 'toolSummary', 'toolAction') if args.get(k)), args)
        self.action = compact(name + ' ' + compact(value, 1000))
        self.steps += 1
        self.repeated = self.repeated + 1 if label == self.previous_action else 1
        if label != self.previous_action:
            self.loop_reported = False
        self.previous_action = label
        if self.repeated >= 5 and not self.loop_reported:
            self.events.append(('LOOP', f'same action repeated {self.repeated} times: {self.action}'))
            self.loop_reported = True
        self.prose = 0
        self.prose_reported = False

    def _book(self, run):
        import godfather_stream as stream
        if self.book is None or self.book.cli != run.get('cli'):
            self.book = stream.Book(run.get('cli') or 'codex', started_at=self.last_at)
        return self.book

    def _structured_events(self, filename, path, offset):
        import godfather_stream as stream
        tail = self.tails.get(filename)
        if tail is None:
            tail = stream.LineReader(stream.MAX_LINE_BYTES, str(path) + '.spill')
            self.tails[filename] = tail
        events = []
        with path.open('rb') as handle:
            handle.seek(offset)
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    self.offsets[filename] = handle.tell()
                    break
                events.extend(tail.feed(chunk))
        return events

    def flush_heartbeats(self, run):
        root = getattr(self, 'heartbeat_root', None)
        book = getattr(self, 'book', None)
        if not root or book is None or not book.pending or not run.get('segment_id'):
            return
        try:
            import godfather_stream as stream
            if stream.persist(root, run['segment_id'], book.pending):
                book.pending = []
        except Exception:
            return

    def text(self, value):
        if value:
            self.action = compact(value)
            self.prose += len(str(value))
            if self.prose >= 16000 and not self.prose_reported:
                self.events.append(('LOOP', '16000+ characters of output/thinking without a tool call'))
                self.prose_reported = True

    def parse(self, d, cli):
        if not isinstance(d, dict):
            return
        typ = d.get('type', '')
        if cli == 'cursor':
            from godfather_stream import classify
            beat = classify(cli, d)
            if beat and beat['kind'] == 'tool':
                if beat.get('tool_state') == 'open':
                    self.tool(beat['detail'], {})
            elif beat and beat['kind'] == 'error':
                self.error(beat['detail'], d)
            elif beat and beat['kind'] in ('text', 'thought'):
                self.text(beat.get('message') or beat['detail'])
            return
        if cli == 'agy':
            key = d.get('step_index')
            if key is not None:
                if key in self.agy_steps:
                    return
                self.agy_steps.add(key)
            if typ in ('ERROR_MESSAGE', 'error') or d.get('is_error') or d.get('status') in ('ERROR', 'FAILED'):
                self.error(d.get('content') or d.get('error') or d, d)
            if 'input_tokens' in d:
                self.tokens_in = (self.tokens_in or 0) + d['input_tokens']
                self.tokens_out = (self.tokens_out or 0) + d.get('output_tokens', 0)
            cached = d.get('cached_input_tokens', d.get('cache_read_input_tokens'))
            if cached is not None:
                self.tokens_cached = (self.tokens_cached or 0) + cached
            if typ == 'PLANNER_RESPONSE':
                self.text(d.get('thinking') or d.get('content'))
                for call in d.get('tool_calls', []):
                    self.tool(call.get('name', 'tool'), call.get('args', {}))
            elif typ == 'GENERIC':
                content = d.get('content', '')
                match = re.search(r'(?:File Path:|Command:|Error:)\s*(.+)', content)
                if match:
                    self.action = compact(match[1])
                # Only actual diagnostics, not quoted/read source files.
                if re.match(r'(?:Error:|Tool (?:error|failed)|Command failed)', content, re.I):
                    self.error(content)
                match = re.search(r'(?:exit code|exited with code)[: ]+([1-9]\d*)', content, re.I)
                if match:
                    self.error('tool ' + match[0])
            self.steps = len(self.agy_steps)
            return
        if cli == 'codex':
            if typ in ('error', 'turn.failed'):
                self.error(d.get('message') or (d.get('error') or {}).get('message') or d, d)
            elif typ == 'turn.completed':
                usage = d.get('usage', {})
                self.tokens_in = (self.tokens_in or 0) + usage.get('input_tokens', 0)
                self.tokens_out = (self.tokens_out or 0) + usage.get('output_tokens', 0)
                if 'cached_input_tokens' in usage:
                    self.tokens_cached = (self.tokens_cached or 0) + usage['cached_input_tokens']
            elif typ == 'item.started':
                item = d.get('item', {})
                if item.get('type') in ('command_execution', 'mcp_tool_call', 'file_change', 'web_search'):
                    self.tool(item['type'], item.get('command') or item)
            elif typ == 'item.completed':
                item = d.get('item', {})
                if item.get('type') in ('agent_message', 'reasoning'):
                    self.text(item.get('text', ''))
                # command_execution.exit_code is an agent tool result, never a
                # CLI failure. Only native error/turn.failed events report ERROR.
            p = d.get('payload', {})
            if typ == 'event_msg':
                t = p.get('type')
                if t == 'token_count':
                    usage = (p.get('info') or {}).get('total_token_usage') or {}
                    self.tokens_in = usage.get('input_tokens', self.tokens_in)
                    self.tokens_out = usage.get('output_tokens', self.tokens_out)
                    self.tokens_cached = usage.get('cached_input_tokens', self.tokens_cached)
                elif t in ('error', 'stream_error', 'turn_aborted'):
                    self.error(p.get('message') or p.get('reason') or p, p)
            elif typ == 'response_item':
                t = p.get('type')
                if t in ('function_call', 'custom_tool_call'):
                    self.tool(p.get('name', 'tool'), p.get('arguments', p.get('input', '')))
                    self.calls[p.get('call_id')] = self.action
                elif t in ('function_call_output', 'custom_tool_call_output'):
                    # Nonzero commands are normal inside an agent's tool loop.
                    pass
                elif t == 'reasoning':
                    self.text(' '.join(v.get('text', '') for v in p.get('summary', [])))
                elif t == 'message' and p.get('role') == 'assistant':
                    self.text(' '.join(v.get('text', '') for v in p.get('content', [])))
            return
        # Grok native streaming-json.
        if typ == 'tool_call':
            self.calls[d.get('toolCallId')] = d.get('toolName') or d.get('title', 'tool')
            self.tool(self.calls[d.get('toolCallId')], d.get('rawInput', {}))
        elif typ == 'tool_call_update':
            output = d.get('rawOutput', {})
            if d.get('status') == 'failed' or (isinstance(output, dict) and (output.get('is_error') or output.get('exit_code', output.get('exitCode', 0)) not in (0, None))):
                self.error(f"tool failed ({self.calls.get(d.get('toolCallId'), 'tool')}): {compact(output, 160)}")
        elif typ in ('text', 'thought'):
            self.text(d.get('data'))
        elif typ == 'error' or d.get('is_error'):
            self.error(d.get('message') or d, d)
        if typ == 'usage':
            self.grok_usage[d.get('messageId', str(len(self.grok_usage)))] = d.get('usage', {})
            self.tokens_in = sum(v.get('input_tokens', 0) for v in self.grok_usage.values())
            self.tokens_out = sum(v.get('output_tokens', 0) for v in self.grok_usage.values())
            if any('cached_input_tokens' in v for v in self.grok_usage.values()):
                self.tokens_cached = sum(v.get('cached_input_tokens', 0) for v in self.grok_usage.values())
        if typ == 'end':
            usage = d.get('usage', {})
            self.tokens_in = usage.get('input_tokens', self.tokens_in)
            self.tokens_out = usage.get('output_tokens', self.tokens_out)
            self.tokens_cached = usage.get('cached_input_tokens', self.tokens_cached)

    def update(self, run):
        self.events = []
        for filename in dict.fromkeys(run.get('progress_paths', []) + run.get('diagnostic_paths', [])):
            p = Path(filename)
            try:
                st = p.stat()
                offset = self.offsets.get(filename, 0)
                if (filename in self.inodes and self.inodes[filename] != st.st_ino) or st.st_size < offset:
                    offset = 0
                    self.tails.pop(filename, None)
                self.inodes[filename] = st.st_ino
                if st.st_size <= offset:
                    continue
                if filename in run.get('progress_paths', []):
                    self.last_at = max(self.last_at, st.st_mtime)
                structured = filename.endswith('.jsonl') or (run['cli'] == 'codex' and run.get('codex_stream') and filename == run.get('discovery_path'))
                book = self._book(run)
                if structured:
                    # Chunked, so a multi-megabyte tool frame is spilled instead of loaded.
                    events = self._structured_events(filename, p, offset)
                    lines = []
                    for kind, line in events:
                        if kind == 'oversized':
                            book.note_oversized(time.time())
                        else:
                            lines.append(line)
                    data = b''
                else:
                    with p.open('rb') as handle:
                        handle.seek(offset)
                        data = handle.read()
                    self.offsets[filename] = offset + len(data)
                    lines = data.decode(errors='replace').splitlines()
                for line in lines:
                    if run['cli'] == 'agy' and 'streamGenerateContent' in line:
                        # Only model stream markers count, never unrelated cache/auth chatter.
                        stamp = re.search(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)', line)
                        glog = re.match(r'[IWEF](\d\d)(\d\d) (\d\d:\d\d:\d\d(?:\.\d+)?)', line)
                        when = epoch(stamp[0]) if stamp else (dt.datetime.fromisoformat(
                            '{}-{}-{}T{}'.format(dt.datetime.fromtimestamp(st.st_mtime).year, glog[1], glog[2], glog[3])).timestamp() if glog else st.st_mtime)
                        self.stream_at = max(self.stream_at, when)
                        book.note_stream(when)
                    if structured:
                        book.observe_line(line, time.time())
                        try:
                            self.parse(json.loads(line), run['cli'])
                        except (ValueError, TypeError):
                            if run.get('codex_stream') and re.match(
                                    r'(?:\d{4}-\d\d-\d\dT\S+\s+ERROR|Error:|error:|ERROR:)', line):
                                self.error(line)
                            continue
                    elif ERROR.search(line) or QUOTA.search(line):
                        # Codex prints the user prompt verbatim; ignore it through the first assistant/tool marker.
                        if run['cli'] == 'codex':
                            continue
                        if not re.match(r'^I\d{4}\s', line):
                            self.error(line)
                if run['cli'] == 'codex' and not structured:
                    # Error diagnostics outside the echoed user prompt.
                    text = data.decode(errors='replace')
                    for line in text.splitlines():
                        if line.strip() in ('user', 'exec', 'codex', 'thinking', 'tokens used'):
                            self.codex_log_context = line.strip()
                            continue
                        # Human-readable logs include echoed prompts and shell output.
                        # Neither is a CLI failure, even if a line starts with Error:.
                        if self.codex_log_context not in ('user', 'exec', 'codex', 'thinking') and re.match(
                                r'(?:\d{4}-\d\d-\d\dT\S+\s+ERROR|Error:|error:|ERROR:)', line):
                            self.error(line)
                        elif self.codex_log_context != 'exec' and re.match(
                                r'\d{4}-\d\d-\d\dT\S+\s+ERROR', line):
                            self.error(line)
            except OSError:
                continue
        if self.pending_auth and run.get('status') in ('failed', 'quota'):
            detail, self.pending_auth = self.pending_auth, None
            if detail not in self.seen_errors:
                self.seen_errors.add(detail)
                self.errors.append(detail)
                self.events.append(('ERROR', detail))
        self.flush_heartbeats(run)
        return self

    def fields(self):
        if self.book and self.book.cli == 'cursor':
            self.tokens_in, self.tokens_cached, self.tokens_out = self.book.tokens_in, self.book.tokens_cached, self.book.tokens_out
        return dict(last_action=self.action, last_progress_at=self.last_at, steps=self.steps,
                    tokens_in=self.tokens_in, tokens_cached=self.tokens_cached, tokens_out=self.tokens_out,
                    model_stream_at=self.stream_at, errors=self.errors, failure=self.failure,
                    rate_limits=self.book.rate_limits if self.book else None,
                    rate_limits_at=self.book.rate_limits_at if self.book else None)


def supervise(args):
    # A conductor can disappear while Bash is waiting here. Own a separate
    # session and ignore hangups; explicit TERM/INT below still stop the CLI.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if os.getsid(0) != os.getpid():
        os.setsid()
    import godfather_safety as safety
    cwd = Path(os.getenv('CWD', os.getcwd())).resolve()
    try:
        safety.protect_cwd(cwd, os.getenv('GODFATHER_SELF') == '1')
        if os.getenv('GODFATHER_LOCK_HELD') == str(cwd):
            return _supervise(args)
        with safety.cwd_lock(cwd):
            return _supervise(args)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _supervise(args):
    path = Path(args.base + '.run.json')
    # Serialize starts/segments; a live launcher owns the name until it exits.
    with open(args.base + '.run.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = read(path)
        owner = os.getppid()
        if old.get('status') == 'running' and old.get('launcher_pid') != owner and (
                run_alive(old) or alive(old.get('launcher_pid'), old.get('launcher_identity'))):
            print(f"Run name already active: {old['name']}", file=sys.stderr)
            return 2
        continuing = old.get('launcher_pid') == owner and old.get('status') == 'running'
        run = old if continuing else dict(name=Path(args.base).name, cli=args.cli, model=args.model,
            cwd=os.getcwd(), prompt_file=str(Path(args.prompt_file).resolve()), pid=None,
            started_at=now(), status='running', exit_code=None, ended_at=None, session_id=None,
            conversation_id=None, progress_paths=[], progress_offsets={}, run_id=str(uuid.uuid4()),
            launcher_pid=owner, launcher_identity=processes().get(owner, {}).get('identity'))
        run['model'] = args.model
        run['discovery_path'] = args.discovery or args.stdout
        if args.session:
            run['conversation_id' if args.cli == 'agy' else 'session_id'] = args.session
            if args.cli == 'agy':
                p = transcript(args.session)
                run['progress_offsets'][str(p)] = p.stat().st_size if p.exists() else 0
        # agy stderr can contain continual startup/cache diagnostics. Counting its
        # mtime as task progress masks a stalled transcript forever. Keep native
        # transcript progress separate from diagnostics, and persist segment logs.
        run['progress_paths'] = [] if args.cli == 'agy' else list(dict.fromkeys([args.stdout, args.stderr]))
        run['diagnostic_paths'] = list(dict.fromkeys(([args.stdout, args.stderr] if args.cli == 'agy' else []) + ([args.discovery] if args.discovery else [])))
        run.update(segment_id=str(uuid.uuid4()), segment_started_at=now(), model_stream_at=0)
        from godfather_supervisor import git_head, finished_report
        initial_head = git_head(run['cwd'])
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        extra_dirs = [command[i + 1] for i, token in enumerate(command[:-1]) if token == '--add-dir' and command[i + 1] != str(Path(args.base).parent)]
        run.setdefault('launcher_env', {})['ADD_DIRS'] = ':'.join(extra_dirs) if extra_dirs else os.getenv('ADD_DIRS', '')
        with open(args.stdout, 'ab' if continuing and run.get('pid') else 'wb') as out:
            err = out if args.stderr == args.stdout else open(args.stderr, 'ab' if continuing and run.get('pid') else 'wb')
            try:
                from godfather_safety import prepare
                command, child_env, effective = prepare(args.base, run, command)
                if args.cli == 'codex' and child_env.get('GODFATHER_CODEX_PROTOCOL', 'exec') == 'app-server':
                    run.setdefault('launcher_env', {})['GODFATHER_CODEX_PROTOCOL'] = 'app-server'
                    command = [sys.executable, str(Path(__file__).with_name('godfather_codex_bridge.py')), args.base] + command
                with effective.open('rb') as task_input:
                    child = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True,
                                             cwd=run['cwd'], env=child_env,
                                             stdin=task_input if args.cli in ('codex', 'cursor') else subprocess.DEVNULL)
                # Quiet is measured from the CLI's first chance to write, not from
                # the truncate that happens before prepare().
                now_stamp = time.time()
                for handle in {out, err}:
                    try:
                        os.utime(handle.fileno(), (now_stamp, now_stamp))
                    except OSError:
                        pass
            except (OSError, ValueError) as exc:
                run.update(status='failed', exit_code=127, ended_at=now(), errors=[str(exc)])
                write(path, run)
                print(exc, file=sys.stderr)
                return 127
            finally:
                if err is not out:
                    err.close()
        snapshot = processes()
        run.update(pid=child.pid, process_session_id=child.pid, process_group_id=child.pid,
                   supervisor_pid=os.getpid(), supervisor_identity=snapshot.get(os.getpid(), {}).get('identity'),
                   pid_identity=snapshot.get(child.pid, {}).get('identity'), child_exit_code=None)
        write(path, discover(run))
    from godfather_supervisor import git_head, finished_report
    idle_completed = None
    killed = False
    def stop(sig, frame):
        nonlocal killed
        killed = True
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    stopped_at = None
    progress = Progress(run, audit_base=args.base)
    progress.heartbeat_root = str(Path(args.base).resolve().parent)
    while child.poll() is None:
        if killed or idle_completed:
            stopped_at = stopped_at or time.monotonic()
            if time.monotonic() - stopped_at > 8:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        discover(run)
        progress.update(run)
        run.update(progress.fields())
        if not idle_completed and not killed:
            idle_completed = finished_report(run, progress, args.base, initial_head)
            if idle_completed:
                run.update(idle_finished=True, final_message=idle_completed['report'], commit=idle_completed['commit'])
                Path(args.base + '.last.md').write_text(idle_completed['report'] + '\n')
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        write(path, run)
        time.sleep(1)
    rc = 0 if idle_completed and not killed else child.returncode
    rc = 128 - rc if rc < 0 else rc
    run.update(child_exit_code=rc, killed=killed or rc in (130, 137, 143))
    discover(run)
    progress.update(run)
    run.update(progress.fields())
    write(path, run)
    from godfather_usage import append_segment
    if run['cli'] != 'grok':
        append_segment(run, rc, progress.fields())
    return rc


def finish(base, rc, quota=False):
    path = base + '.run.json'
    run = read(path)
    # A delayed trap from an earlier launcher may never finalize a replacement.
    if not run or run.get('launcher_pid') != os.getppid():
        return
    if rc == 0 and run.get('pid') is None:
        rc = 1
    stable_log = base + '.log'
    if Path(stable_log).exists() and stable_log not in run['progress_paths']:
        run['progress_paths'].append(stable_log)
    progress = Progress(run).update(run)
    run.update(progress.fields())
    from godfather_safety import control
    questions = control(base).get('questions', [])
    pending = any(q.get('answer') is None or not q.get('delivered', True) for q in questions)
    from godfather_registry import completion_failure
    completion, reason = completion_failure(base, questions)
    if rc == 0 and completion:
        run.update(failure_class=completion, failure_reason=reason)
        rc = 1
    if not pending and not run.get('killed') and rc not in (130, 137, 143):
        from godfather_supervisor import segment_outcome
        failure, reason = segment_outcome(base, rc, run)
        if failure:
            run.update(failure_class=failure, failure_reason=reason)
            run['errors'] = [reason] + [e for e in run.get('errors', []) if e != reason]
            rc = rc or 1
    from godfather_supervisor import provider_failure
    failure = provider_failure(base, run, final=bool(rc))
    if failure and rc:
        run.update(failure=failure, category=failure['category'], source=failure['source'])
        from godfather_safety import event
        event(base, 'FAILURE', failure['category'], category=failure['category'], source=failure['source'])
    run.update(status='killed' if run.get('killed') or rc in (130, 137, 143) else
               'done' if rc == 0 else 'quota' if ((failure and failure['category'] == 'quota') or (not failure and quota)) else 'failed',
               exit_code=rc, ended_at=now())
    # Capture deferred startup auth failures only after the actual outcome is known.
    if progress.pending_auth and run['status'] == 'failed':
        run['errors'].append(progress.pending_auth)
    if run['status'] == 'quota' and run['model'] != 'pending':
        from godfather_router import failure_detail, mark_exhausted
        try:
            mark_exhausted(run.get('model_key') or run['cli'] + ':' + run['model'], failure_detail(base, run))
        except (OSError, ValueError) as exc:
            print('Cannot persist quota cooldown: ' + str(exc), file=sys.stderr)
    # Temporary segment files have already been copied to the stable outputs.
    stable_log = base + '.log'
    run['progress_paths'] = list(dict.fromkeys([p for p in run['progress_paths'] if Path(p).exists()] + ([stable_log] if Path(stable_log).exists() else [])))
    if rc == 0 and pending:
        run['status'] = 'needs_input'
    elif rc == 0 and os.getenv('GODFATHER_CHECK'):
        run['status'] = 'checking'
    write(path, run)
    if run['cli'] == 'grok' and run.get('segment_id'):
        from godfather_usage import append_segment
        append_segment(run, rc, progress.fields())
    return rc


def reconcile_run(run, root=None, procs=None):
    """Finalize abandoned owners; a live launcher may still be checking or routing."""
    root = Path(root or runs_dir())
    if run.get('status') not in ('running', 'needs_input', 'checking', 'reviewing') or not run.get('pid'):
        return run
    procs = processes() if procs is None else procs
    if owners_alive(run, procs, root):
        return run
    base = root / run['name']
    route = read(str(root / (run.get('router_name') or run['name'])) + '.route.json')
    current = read(str(base) + '.run.json')
    if current.get('run_id') and current['run_id'] != run.get('run_id'):
        return run
    questions = read(str(base) + '.control.json').get('questions', run.get('questions', []))
    from godfather_registry import exited_record
    updated = exited_record(current or run, base, questions)
    if updated is None:
        return run
    status = updated['status']
    from godfather_safety import control
    control(base, lambda data: data.update(questions=updated['questions']))
    write(str(base) + '.run.json', updated)
    from godfather_safety import event
    event(base, 'DIED' if status == 'failed' else 'DONE', '' if status == 'failed' else status + ' (reconciled exited process)')
    if route and route.get('status') == 'running':
        route.update(status='finished', result=status, ended_at=updated['ended_at'])
        write(str(root / (run.get('router_name') or run['name'])) + '.route.json', route)
    return updated


def registry(root, procs=None, reconcile=True):
    from godfather_store import load_runs
    visible = NONTERMINAL
    result = []
    procs = processes() if procs is None else procs
    for run in load_runs(root):
        if reconcile:
            run = reconcile_run(run, root, procs)
        run = decision_state(run, root)
        if run.get('status') in visible or time.time() - epoch(run.get('ended_at')) <= 3600:
            result.append(run)
    return result


def decision_state(run, root=None):
    """Control is authoritative; delivery to a monitor is not an agent answer."""
    base = Path(root or runs_dir()) / run['name']
    from godfather_registry import expire_terminal_questions
    from godfather_store import TERMINAL
    if run.get('status') in TERMINAL:
        expire_terminal_questions(str(base) + '.run.json')
    current = read(str(base) + '.run.json')
    if current and (current.get('run_id') or current.get('started_at')) == (run.get('run_id') or run.get('started_at')):
        run = dict(run, **read(str(base) + '.control.json'))
    if run.get('status') == 'done':
        from godfather_registry import completion_failure
        failure, reason = completion_failure(base, run.get('questions', []))
        if failure:
            run = dict(run, status='failed', failure_class=failure, failure_reason=reason, exit_code=1)
    if run.get('status') in NONTERMINAL:
        pending = [q for q in run.get('questions', []) if q.get('answer') is None or not q.get('delivered', True)]
        run['pending_decision'] = bool(pending)
        if pending:
            run['status'] = 'needs_input'
        elif run['status'] == 'needs_input' and not run.get('ended_at'):
            run['status'] = 'running'
    return run


def unregistered(registered, procs):
    known = {r.get('pid') for r in registered}
    result = []
    for pid, p in procs.items():
        command = p['command']
        first = command.split()[0]
        cli = Path(first).name
        if pid in known or cli not in ('agy', 'codex', 'grok'):
            continue
        if cli == 'codex' and not re.search(r'(?:^|\s)exec(?:\s|$)', command):
            continue
        if any(token in command.split() for token in ('--help', '-h', '/usage')) or ' sessions list' in command:
            continue
        session = re.search(r'--(?:conversation|resume)[= ]+(' + UUID + ')', command)
        model = re.search(r'(?:--model|-m)[= ]+(\S+)', command)
        try:
            cwd = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], capture_output=True, text=True, timeout=3).stdout
            cwd = next((s[1:] for s in cwd.splitlines() if s.startswith('n')), '?')
        except (OSError, subprocess.TimeoutExpired):
            cwd = '?'
        started = dt.datetime.strptime(p['identity'], '%a %b %d %H:%M:%S %Y').astimezone().isoformat()
        r = dict(name=f'unregistered-{pid}', cli=cli, model=model[1] if model else '?', status='unregistered',
                 pid=pid, pid_identity=p['identity'], started_at=started, cwd=cwd, progress_paths=[],
                 session_id=session[1] if session and cli != 'agy' else None,
                 conversation_id=session[1] if session and cli == 'agy' else None)
        if cli == 'codex':
            output = re.search(r'(?:-o|--output-last-message) +([^ ]+)\.last\.md', command)
            if output:
                r['discovery_path'] = output[1] + '.log'
                r['progress_paths'] = [r['discovery_path']]
                discover(r)
        if cli == 'agy' and session:
            r['progress_paths'] = [str(transcript(session[1]))]
        result.append(r)
    return result


def duration(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600}h{seconds % 3600 // 60:02}m' if seconds >= 3600 else f'{seconds // 60}m{seconds % 60:02}s'


def snapshot(root):
    rows = registry(root)
    from godfather_store import load_queue
    for job in load_queue(root):
        if job.get('kind') != 'quota' and job.get('status') in ('queued', 'launching', 'dependency_failed'):
            rows = [r for r in rows if r['name'] != job['name']]
            rows.append(dict(name=job['name'], cli='queue', model='-', status='queued' if job['status'] != 'dependency_failed' else 'dependency_failed', started_at=now(), progress_paths=[], last_action=job.get('error') or 'after ' + ','.join(job['dependencies'])))
    procs = processes()
    rows += unregistered(rows, procs)
    for r in rows:
        queue_action = r.get('last_action') if r.get('cli') == 'queue' else None
        progress = Progress(r).update(r)
        # Final snapshots survive removed temporary outputs.
        if r.get('status') not in ('running', 'unregistered') and r.get('last_progress_at'):
            for k, v in progress.fields().items():
                if k not in r:
                    r[k] = v
        else:
            r.update(progress.fields())
        if r.get('failure_reason'):
            r['errors'] = [r['failure_reason']] + [e for e in r.get('errors', []) if e != r['failure_reason']]
        r['process_alive'] = run_alive(r, procs)
        r['elapsed'] = duration(epoch(r.get('ended_at')) - epoch(r['started_at']) if r.get('ended_at') else time.time() - epoch(r['started_at']))
        r['seconds_since_progress'] = max(0, int(time.time() - r.get('last_progress_at', epoch(r['started_at']))))
        if queue_action:
            r['last_action'] = queue_action
        if r.get('pending_decision'):
            question = next(q for q in reversed(r.get('questions', [])) if q.get('answer') is None or not q.get('delivered', True))
            r['last_action'] = ('OVERDUE question: ' if r['seconds_since_progress'] > 900 else 'Question: ') + question.get('question', '')
    return rows


def conductor_environment():
    from godfather_safety import original_environment
    original = original_environment()
    for key in ('PATH', 'BASH_ENV', 'ZDOTDIR'):
        if key in original:
            os.environ[key] = original[key]
        else:
            os.environ.pop(key, None)


def status_main():
    conductor_environment()
    p = argparse.ArgumentParser(description='Snapshot active runs and runs ended in the last hour, including unregistered CLI processes.')
    p.add_argument('--json', action='store_true', help='emit the complete snapshot as JSON')
    args = p.parse_args()
    rows = snapshot(runs_dir())
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    headers = ['NAME', 'CLI', 'MODEL', 'STATUS', 'ELAPSED', 'IDLE(s)', 'LAST ACTION', 'STEPS', 'TOKENS IN/OUT', 'ERRORS']
    table = [headers]
    for r in rows:
        table.append([r['name'], r['cli'], display_model(r), ('waiting for quota, resumes ' + time.strftime('%H:%M', time.localtime(r['resume_at'])) if r['status'] == 'waiting_quota' else r['status']), r['elapsed'], str(r['seconds_since_progress']),
                      compact(r.get('last_action', 'unknown')), str(r.get('steps', '?')),
                      f"{r.get('tokens_in') if r.get('tokens_in') is not None else '?'}/{r.get('tokens_out') if r.get('tokens_out') is not None else '?'}",
                      compact('; '.join(r.get('errors', [])), 100) or '-'])
    widths = [max(len(str(row[i])) for row in table) for i in range(len(headers))]
    for row in table:
        print('  '.join(str(value).ljust(widths[i]) for i, value in enumerate(row)).rstrip())


def try_heal_lock():
    """Exclusive lock for the one watcher allowed to heal. None when another holds it."""
    from godfather_policy import state_dir
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / 'watch.lock', 'a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def parent_gone(ppid=None):
    """The watcher was reparented to init. Its conductor is gone."""
    current = os.getppid() if ppid is None else ppid
    return current == 1


def stdout_hungup(stream=None):
    """True when the reader's end of stdout has closed (EPIPE / POLLHUP)."""
    stream = sys.stdout if stream is None else stream
    if getattr(stream, 'closed', False):
        return True
    try:
        fd = stream.fileno()
    except (AttributeError, ValueError):
        return True
    poller = select.poll()
    poller.register(fd, select.POLLHUP)
    return any(flags & select.POLLHUP for _fd, flags in poller.poll(0))


def watch_config_stamp():
    from godfather_policy import config_dir
    path = config_dir() / 'godfather.toml'
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def watch_config_changed(previous):
    """Reload healing settings when godfather.toml changes. None when it has not."""
    stamp = watch_config_stamp()
    if previous == stamp:
        return None
    from godfather_lifecycle import settings
    healing, ignored = settings()
    return healing, ignored, stamp


def watch_main():
    conductor_environment()
    p = argparse.ArgumentParser(description='Print flushed, one-line START/SWITCH/STALL/ERROR/QUOTA/LOOP/DONE/DIED events until killed.')
    p.add_argument('--stall-secs', type=float, default=float(os.getenv('STALL_SECS', '120')), help='idle threshold (default: STALL_SECS or 120); repeats at 2x and 4x only')
    p.add_argument('--poll-secs', type=float, default=5, help='poll interval in seconds (default: 5)')
    from godfather_lifecycle import settings, request_heal, important, logical_status, ensure_daemon
    from godfather_supervisor import inflight_protected, policies_active, schedule
    healing, ignored = settings()
    p.add_argument('--important', action='store_true', help='only actionable decisions, failures, exhausted quota and completion')
    p.add_argument('--replay', action='store_true', help='replay historical events and diagnostics (default: only undelivered events)')
    p.add_argument('--consumer', help='independent persistent monitor id; default: lowest available watch slot')
    p.add_argument('--cursor', help='resume a named subscriber; an unknown name is rejected')
    p.add_argument('--no-heal', action='store_true', help='observe without automatic remediation')
    p.add_argument('--hard-stall-secs', type=float, default=float(os.getenv('HARD_STALL_SECS', str(healing['hard_limit_secs']))), help='hard idle limit even with an active model stream (default: 900)')
    args = p.parse_args()
    if args.stall_secs <= 0 or args.poll_secs <= 0 or args.hard_stall_secs <= 0:
        p.error('threshold and poll interval must be positive')
    stall_explicit = '--stall-secs' in sys.argv or 'STALL_SECS' in os.environ
    hard_explicit = '--hard-stall-secs' in sys.argv
    if not stall_explicit:
        args.stall_secs = healing['stall_secs']
    user_no_heal = args.no_heal
    config_stamp = watch_config_stamp()
    # One healer. A second watcher keeps printing events and does not remediate.
    heal_handle = None
    if not user_no_heal and healing.get('enabled', True):
        heal_handle = try_heal_lock()
        if heal_handle is None:
            args.no_heal = True
            print('observe only; another watcher holds the heal lock', file=sys.stderr, flush=True)
    started = time.time()
    from godfather_store import UnknownCursor, events_after, load_routes, meta_get, meta_set, claim_watch, release_watch, save_cursor
    root = runs_dir()
    if args.consumer and args.cursor:
        p.error('--consumer and --cursor are mutually exclusive')
    try:
        subscriber, cursor = claim_watch(root, args.cursor or args.consumer, require_existing=bool(args.cursor))
        if args.replay:
            cursor = 0
    except (UnknownCursor, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    old_runs = {r['name'] for r in registry(root, reconcile=False) if r['status'] in TERMINAL and epoch(r.get('ended_at')) < started}
    states = {}
    switches = set()
    pending_events = {}
    consumed = set()
    def visible(kind, name, detail=''):
        return (not args.important or important(kind, detail.lstrip(': '), name)) and (kind in ('NEEDS_INPUT', 'DENY', 'ESCALATE', 'DONE') or not any(pattern.lower() in detail.lower() for pattern in ignored))
    def emit(kind, name, detail='', delivery_id=None, record=True):
        from godfather_safety import event, done_detail
        from godfather_store import claim_delivery
        import hashlib
        run = read(str(root / name) + '.run.json')
        identity = run.get('segment_id') or run.get('run_id') or run.get('started_at') or name
        payload = '' if kind in ('START', 'DONE', 'DIED') else (delivery_id or detail)
        key = hashlib.sha256(json.dumps([identity, kind, name, payload], sort_keys=True).encode()).hexdigest()
        if not args.replay and not claim_delivery(root, subscriber, key):
            return
        if kind == 'DONE':
            detail = done_detail(root / name, detail)
        if record:
            event(runs_dir() / name, kind, detail, suffix='.monitor.jsonl')
        if not visible(kind, name, detail):
            return
        print(f'{kind} {compact(name, 120)}' + (('' if detail.startswith(':') else ' ') + compact(detail, 500) if detail else ''), flush=True)
    try:
        while True:
            if parent_gone() or stdout_hungup():
                return 0
            changed = watch_config_changed(config_stamp)
            if changed:
                healing, ignored, config_stamp = changed
                if not stall_explicit:
                    args.stall_secs = healing['stall_secs']
                if not hard_explicit:
                    args.hard_stall_secs = healing['hard_limit_secs']
                if not user_no_heal:
                    args.no_heal = not healing.get('enabled', True) or heal_handle is None
            ensure_daemon()
            procs = processes()
            fresh = events_after(root, cursor)
            for e in list(pending_events.values()) + fresh:
                seq = e['seq']
                if seq in consumed:
                    continue
                detail = e.get('detail') or ''
                name = e.get('name') or ''
                if not args.replay and e.get('at', 0) < started and name in old_runs:
                    consumed.add(seq)
                    cursor = max(cursor, seq)
                    continue
                if visible(e['kind'], name, detail):
                    delivery = 'event:' + str(seq)
                    expired_question = False
                    if e['kind'] == 'NEEDS_INPUT':
                        if not args.replay:
                            from godfather_registry import expire_terminal_questions
                            expire_terminal_questions(str(root / name) + '.run.json', procs=procs)
                        questions = read(str(root / name) + '.control.json').get('questions', [])
                        question = next((q for q in reversed(questions) if q.get('question') and q['question'] in detail), None)
                        if question:
                            delivery = 'question:' + str(question.get('id'))
                            expired_question = not args.replay and question.get('reason') == 'expired: run ended'
                    if not expired_question:
                        emit(e['kind'], name, detail, delivery_id=delivery, record=False)
                if args.important and e['kind'] in ('DONE', 'QUOTA') and logical_status(name) in NONTERMINAL + ('healing',):
                    pending_events[seq] = e
                else:
                    pending_events.pop(seq, None)
                    consumed.add(seq)
                cursor = max(cursor, seq)
                save_cursor(root, subscriber, cursor)
            save_cursor(root, subscriber, cursor)
            routes = {item.get('name'): item for item in load_routes(root)}
            for route in routes.values():
                if not args.replay and route.get('name') in old_runs:
                    continue
                if route.get('status') != 'running' and time.time() - epoch(route.get('ended_at')) > 3600:
                    continue
                remembered = 0 if args.replay else int(meta_get(root, 'switches:' + subscriber + ':' + str(route.get('run_id') or route.get('name'))) or 0)
                for index, switch in enumerate(route.get('switches', [])):
                    if index < remembered:
                        continue
                    key = (route.get('run_id'), index)
                    if key not in switches:
                        emit('SWITCH', switch['name'], f"{switch['source']} -> {switch['target']} ({switch['reason']})")
                        switches.add(key)
                        if not args.replay:
                            meta_set(root, 'switches:' + subscriber + ':' + str(route.get('run_id') or route.get('name')), str(index + 1))
            for r in registry(root, procs, reconcile=args.no_heal):
                if r.get('status') == 'needs_input':
                    r = reconcile_run(r, root, procs)
                if not args.replay and r['name'] in old_runs:
                    continue
                route_info = routes.get(r.get('router_name') or r['name']) or {}
                if route_info.get('active_name') and route_info['active_name'] != r['name'] and r['status'] == 'running':
                    continue
                if r.get('pid') is None and r['status'] == 'running' and time.time() - epoch(r['started_at']) < args.stall_secs:
                    continue
                key = (r['name'], r.get('segment_id') or r.get('run_id', r['started_at']))
                if key not in states:
                    states[key] = dict(progress=Progress(r), done=False, died=False, stalls=set(), last_progress=0, slow=False)
                    emit('START', r['name'], f"{r['cli']} {display_model(r)}")
                s = states[key]
                progress = s['progress'].update(r)
                for kind, detail in progress.events:
                    emit(kind, r['name'], detail)
                from godfather_supervisor import pause_for_decision
                if r.get('pending_decision'):
                    question = next(q for q in reversed(r.get('questions', [])) if q.get('answer') is None or not q.get('delivered', True))
                    emit('NEEDS_INPUT', r['name'], ': ' + question.get('question', ''), delivery_id='question:' + str(question.get('id')))
                if pause_for_decision(progress, r):
                    s['stalls'].clear()
                    s['last_progress'] = time.time()
                    continue
                if r['status'] in NONTERMINAL and r['status'] not in ('running', 'retrying'):
                    continue
                if r['status'] in TERMINAL:
                    if args.important:
                        logical_name = r.get('router_name') or r['name']
                        if logical_status(logical_name) in NONTERMINAL + ('healing',):
                            continue
                        if route_info.get('active_name') and route_info['active_name'] != r['name']:
                            continue
                    if not s['done']:
                        if (r['status'] == 'quota' or r.get('legacy_status') == 'quota') and not any(QUOTA.search(error) for error in progress.errors):
                            emit('QUOTA', r['name'], 'launcher recorded exhausted quota')
                        emit('DONE', r['name'], f"exit={r.get('exit_code')} {duration(epoch(r.get('ended_at')) - epoch(r['started_at']))} tokens={progress.tokens_in if progress.tokens_in is not None else (r.get('tokens_in') if r.get('tokens_in') is not None else '?')}/{progress.tokens_out if progress.tokens_out is not None else (r.get('tokens_out') if r.get('tokens_out') is not None else '?')}")
                        s['done'] = True
                    continue
                # A segment may have exited while the launcher is checking quota/routing.
                living = run_alive(r, procs)
                owner_alive = alive(r.get('launcher_pid'), r.get('launcher_identity'), procs)
                # The supervisor polls at 1s. A faster watcher can observe a
                # just-exited child before the supervisor records its exit.
                supervisor = procs.get(r.get('supervisor_pid'), {})
                finalizing = bool(supervisor) and supervisor.get('ppid') == r.get('launcher_pid')
                expected_gap = (r.get('pid') is None or r.get('child_exit_code') is not None or finalizing) and owner_alive
                if r['status'] == 'retrying' and owners_alive(r, procs, root):
                    expected_gap = True
                if not living and not expected_gap:
                    s.setdefault('missing_since', time.monotonic())
                    if not s['died'] and time.monotonic() - s['missing_since'] >= max(.15, args.poll_secs):
                        # A vanished pid whose children are still working is not a crash.
                        from godfather_supervisor import descendant_block
                        busy = descendant_block(r.get('pid'))
                        healed = not busy and not args.no_heal and request_heal(r, 'died', 'process vanished')
                        if not healed:
                            emit('DIED', r['name'])
                        s['died'] = True
                    continue
                s.pop('missing_since', None)
                # begin() publishes the run before the CLI process exists.
                # Quota checks and prepare run in that gap. It is not a quiet stall.
                if r.get('pid') is None and owner_alive:
                    continue
                if r.get('pid') != s.get('alive_pid'):
                    s['alive_pid'] = r.get('pid')
                    s['alive_since'] = time.monotonic()
                if progress.last_at > s['last_progress']:
                    s['stalls'].clear()
                    s['last_progress'] = progress.last_at
                    s['slow'] = False
                # sleep N, or a child still using CPU, is tool execution — not a stall.
                if inflight_protected(progress, r):
                    s['stalls'].clear()
                    s['slow'] = False
                    continue
                errors = progress.errors + r.get('errors', [])
                stream_error = any(re.search(r'stream.{0,30}interrupt', error, re.I) for error in errors)
                # The log is truncated before prepare(), so its mtime and
                # segment_started_at can already exceed stall_secs when the CLI
                # pid first appears, before any stream error is readable.
                # Waiting out that gap must not consume the 1x/2x/4x notices.
                if time.monotonic() - s.get('alive_since', time.monotonic()) < args.stall_secs and not stream_error:
                    continue
                idle = time.time() - progress.last_at
                streaming = progress.stream_at >= progress.last_at - 1 and time.time() - progress.stream_at < args.hard_stall_secs
                if (living or expected_gap) and idle >= args.stall_secs and streaming and idle < args.hard_stall_secs:
                    if not s['slow']:
                        emit('SLOW', r['name'], f'active model stream; no transcript step for {int(idle)}s')
                        s['slow'] = True
                    continue
                for multiple in (1, 2, 4):
                    if (living or expected_gap) and idle >= args.stall_secs * multiple and multiple not in s['stalls']:
                        if not args.no_heal and living and policies_active():
                            # [[retry]] replaces the mtime/quiet/restart heuristic.
                            schedule(r, progress, procs)
                        elif not args.no_heal and living:
                            paths = [Path(p) for p in r.get('diagnostic_paths', []) + [str(runs_dir() / r['name']) + '.log']]
                            existing = [p for p in paths if p.exists()]
                            # A missing or empty log was truncated before the CLI wrote.
                            # Its mtime is not silence, and a stall nudge cannot see a session yet.
                            quiet = bool(existing) and all(p.stat().st_size > 0 and time.time() - p.stat().st_mtime >= args.stall_secs for p in existing)
                            if stream_error:
                                request_heal(r, 'stream', 'stream was interrupted')
                            elif not streaming and quiet or idle >= args.hard_stall_secs:
                                base = runs_dir() / (r.get('router_name') or r['name'])
                                prior = read(str(base) + '.healing.json')
                                resumed = r.get('segment_id') != prior.get('segment_id')
                                failed = prior.get('pending') is False and prior.get('exit_code', 0) != 0
                                action = 'restart' if prior.get('attempts', {}).get('stall') and (resumed or failed) else 'stall'
                                request_heal(r, action, 'no active model stream' if quiet else 'hard streaming limit')
                        emit('STALL', r['name'], f'no progress for {int(idle)}s (process alive; last: {progress.action})',
                             delivery_id='stall:{}:{}'.format(s['last_progress'], multiple))
                        # If first attached late, emit one notice, not three at once.
                        s['stalls'].update(m for m in (1, 2, 4) if idle >= args.stall_secs * m)
                        break
            time.sleep(args.poll_secs)
    except KeyboardInterrupt:
        return
    except BrokenPipeError:
        return 0
    finally:
        release_watch(root, subscriber)


def runs_dir():
    return Path(os.getenv('AGENT_RUNS_DIR', str(HOME / '.agent-runs'))).expanduser()


def preserve_report(base):
    """Launchers truncate .last.md when they start; keep the report a resume would replace."""
    report = Path(str(base) + '.last.md')
    try:
        text = report.read_bytes()
    except OSError:
        return None
    if not text.strip():
        return None
    ordinal = 1
    while True:
        archive = Path(f'{base}.last.{ordinal}.md')
        try:
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            ordinal += 1
            continue
        with os.fdopen(fd, 'wb') as f:
            f.write(text)
        return archive


def nudge_main(argv=None, run_checks=True, wait_for_completion=True, expected_instance=None):
    conductor_environment()
    p = argparse.ArgumentParser(description='Deliver a corrective instruction and resume the exact session in the background.')
    p.add_argument('name', help='registered run name')
    p.add_argument('message', help='corrective instruction (quote it as one argument)')
    p.add_argument('--wait', action='store_true', help='wait for the resumed run to finish')
    p.add_argument('--remember', action='store_true', help='also save safe message lines as project lessons')
    args = p.parse_args(argv)
    if args.remember:
        from godfather_safety import filter_lessons
        args.message = '\n'.join(filter_lessons(args.message))
        if not args.message:
            p.error('no safe lesson lines to remember; nudge not delivered')
    if not wait_for_completion:
        from godfather_lifecycle import deliver_nudge
        try:
            rc = deliver_nudge(args.name, args.message, remember=True) if args.remember else deliver_nudge(args.name, args.message)
            if not args.wait:
                return rc
            from godfather_safety import resolve_name
            path = str(resolve_name(args.name)) + '.nudge.json'
            while True:
                request = read(path)
                if request.get('expired'):
                    print('nudge expired: ' + request.get('reason', 'run ended before delivery'), file=sys.stderr)
                    return 1
                if not request.get('pending'):
                    return request.get('exit_code', 2)
                time.sleep(.1)
        except (ValueError, OSError) as exc:
            p.error(str(exc))
    if not args.name or '/' in args.name or args.name in ('.', '..'):
        p.error('name must be a filename without slashes')
    root = runs_dir().resolve()
    from godfather_safety import resolve_name
    base = resolve_name(args.name)
    args.name = base.name
    r = read(str(base) + '.run.json')
    if not r:
        p.error('run is not registered')
    from godfather_supervisor import pending_question
    if pending_question(r):
        p.error('run has a pending question; answer it instead of nudging')
    r['cwd'] = str(Path(r['cwd']).resolve())
    if expected_instance is not None and (r.get('run_id') != expected_instance or r.get('stop_requested') or r.get('status') == 'stopped'):
        return 0  # An automatic quota resume never reopens a stop or replacement.
    if r.get('stop_requested'):
        from godfather_safety import control
        control(base, lambda data: data.pop('stop_requested', None))
        r.pop('stop_requested', None)
    r = discover(r)
    session = r.get('conversation_id') if r['cli'] == 'agy' else r.get('session_id')
    if not session:
        p.error('session is not known yet; run left untouched')
    # exec CLIs cannot accept stdin mid-turn. Wait for the natural boundary;
    # never terminate a live segment to inject a corrective message.
    instance = r.get('run_id')
    while True:
        procs = processes()
        if not (run_alive(r, procs) or alive(r.get('launcher_pid'), r.get('launcher_identity'), procs)
                or alive(r.get('supervisor_pid'), r.get('supervisor_identity'), procs)):
            break
        time.sleep(.1)
        current = read(str(base) + '.run.json')
        if current.get('run_id') != instance or current.get('stop_requested'):
            p.error('nudge cancelled or run instance replaced; session left untouched')
        r = current
    if pending_question(r):
        p.error('run has a pending question; answer it instead of nudging')
    if args.remember:
        from godfather_safety import add_lesson
        add_lesson(r.get('project_cwd') or r.get('launcher_env', {}).get('GODFATHER_PROJECT_CWD') or r['cwd'], args.message, root)
    prompt = root / f'{args.name}.nudge-{uuid.uuid4().hex[:8]}.md'
    prompt.write_text(args.message + '\n')
    preserved = preserve_report(base)
    with open(str(base) + '.models.log', 'a') as f:
        f.write(f'[{now()}] NUDGE session={session} message={json.dumps(args.message)}\n')
        if preserved:
            f.write(f'[{now()}] NUDGE previous report preserved at {preserved}\n')
    env = dict(os.environ, **r.get('launcher_env', {}))
    env.update(CWD=r['cwd'], AGENT_RUNS_DIR=str(root), AGENT_RESUME_ID=session, AGENT_NUDGE='1')
    if r.get('router_name'):
        env.update(AGENT_ROUTER_NAME=r['router_name'], AGENT_ROUTER_ID=r.get('router_id') or '')
    if r['cli'] in ('codex', 'grok', 'cursor'):
        env[r['cli'].upper() + '_MODEL'] = r['model']
    # Router-owned fixes retain its CWD lock; external nudges acquire their own.
    if os.getenv('GODFATHER_LOCK_HELD') == r['cwd']:
        env['GODFATHER_LOCK_HELD'] = r['cwd']
    else:
        env.pop('GODFATHER_LOCK_HELD', None)
    launcher = Path(__file__).with_name(r['cli'] + '-run')
    from godfather_safety import cwd_lock
    def resume():
        env['GODFATHER_LOCK_HELD'] = r['cwd']
        rc = subprocess.call([str(launcher), str(prompt), args.name], env=env)
        settings = r.get('launcher_env', {})
        current = read(str(base) + '.run.json')
        if run_checks and rc == 0 and current.get('status') != 'needs_input' and (settings.get('GODFATHER_CHECK') or settings.get('GODFATHER_REVIEW_BY')):
            from godfather_router import verify, cross_review
            from types import SimpleNamespace
            options = SimpleNamespace(check=settings.get('GODFATHER_CHECK') or None, run_cwd=Path(r['cwd']),
                max_fix_rounds=int(settings.get('GODFATHER_MAX_FIX_ROUNDS', 2)), name=r.get('router_name') or args.name,
                review_by=settings.get('GODFATHER_REVIEW_BY') or None, review_fix=settings.get('GODFATHER_REVIEW_FIX') == '1',
                review_rounds=int(settings.get('GODFATHER_REVIEW_ROUNDS', 0)),
                review_round=int(settings.get('GODFATHER_REVIEW_ROUND', 0)),
                review_threshold=settings.get('GODFATHER_REVIEW_THRESHOLD', 'high'),
                review_head=settings.get('GODFATHER_REVIEW_HEAD') or 'HEAD', branch=None, self=settings.get('GODFATHER_SELF') == '1')
            if options.check:
                rc = verify(options, base)
            if rc == 0 and options.review_by and read(str(base) + '.run.json').get('status') != 'needs_input':
                task = Path(settings['GODFATHER_REVIEW_TASK']).read_text()
                original_base = root / options.name
                options.review_before = Path(str(original_base) + '.before.diff').read_text()
                return cross_review(options, base, task)
        if run_checks and settings.get('GODFATHER_REVIEW_PARENT') and read(str(base) + '.run.json').get('status') != 'needs_input':
            from godfather_router import complete_review
            return complete_review(Path(settings['GODFATHER_REVIEW_PARENT']), Path(settings['GODFATHER_REVIEW_SEGMENT']), r.get('router_name') or args.name, rc)
        return rc
    if os.getenv('GODFATHER_LOCK_HELD') == r['cwd']:
        return resume()
    # The route owner may still be unwinding after the terminated launcher.
    deadline = time.monotonic() + 15
    while True:
        try:
            with cwd_lock(r['cwd']):
                return resume()
        except ValueError as exc:
            if 'CWD is locked' not in str(exc) or time.monotonic() >= deadline:
                p.error(str(exc))
            time.sleep(.1)


def internal_main():
    conductor_environment()
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='operation', required=True)
    sub.add_parser('find-cursor')
    sub.add_parser('find-codex')
    sub.add_parser('codex-model')
    auth = sub.add_parser('doctor')
    auth.add_argument('--live', action='store_true', help='run codex login status and agy -p /usage')
    b = sub.add_parser('begin')
    b.add_argument('base')
    b.add_argument('cli')
    b.add_argument('model')
    b.add_argument('prompt_file')
    r = sub.add_parser('run')
    for name in ('base', 'cli', 'model', 'prompt-file', 'stdout', 'stderr'):
        r.add_argument('--' + name, required=True)
    r.add_argument('--session')
    r.add_argument('--discovery')
    r.add_argument('command', nargs=argparse.REMAINDER)
    f = sub.add_parser('finish')
    f.add_argument('base')
    f.add_argument('rc', type=int)
    f.add_argument('--quota', action='store_true')
    g = sub.add_parser('grok-result')
    g.add_argument('base')
    args = p.parse_args()
    if args.operation == 'find-cursor':
        binary = cli_binary('cursor')
        if binary:
            print(binary)
        return 0 if binary else 127
    if args.operation == 'find-codex':
        try:
            print(find_codex())
            return 0
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return 127
    if args.operation == 'codex-model':
        print(codex_model())
        return 0
    if args.operation == 'doctor':
        doctor(live=args.live)
        return 0
    if args.operation == 'begin':
        with open(args.base + '.run.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            old = read(args.base + '.run.json')
            procs = processes()
            if old.get('status') == 'waiting_quota' and not os.getenv('AGENT_RESUME_ID'):
                print('Run name already active: ' + old['name'] + ' (waiting for quota; stop it first)', file=sys.stderr)
                return 2
            if old.get('status') == 'running' and (run_alive(old, procs) or alive(old.get('launcher_pid'), old.get('launcher_identity'), procs)):
                print('Run name already active: ' + old['name'], file=sys.stderr)
                return 2
            from godfather_safety import atomic
            if os.getenv('AGENT_NUDGE') != '1' and not os.getenv('AGENT_RESUME_ID'):
                atomic(args.base + '.control.json', {})
            owner = os.getppid()
            resume = os.getenv('AGENT_RESUME_ID')
            offsets = {}
            if resume and args.cli == 'agy':
                tp = transcript(resume)
                offsets[str(tp)] = tp.stat().st_size if tp.exists() else 0
            run = dict(name=Path(args.base).name, cli=args.cli, model=args.model,
                task_type=os.getenv('GODFATHER_TASK_TYPE') or 'edit',
                cwd=str(Path(os.getenv('CWD', os.getcwd())).resolve()), prompt_file=str(Path(args.prompt_file).resolve()),
                pid=None, started_at=now(), status='running', exit_code=None, ended_at=None,
                session_id=resume if args.cli != 'agy' else None, conversation_id=resume if args.cli == 'agy' else None, progress_paths=[], progress_offsets=offsets,
                run_id=str(uuid.uuid4()), launcher_pid=owner, launcher_identity=procs.get(owner, {}).get('identity'),
                codex_stream=args.cli == 'codex', healing_parent=os.getenv('GODFATHER_HEAL_PARENT'),
                launcher_env={key: os.environ[key] for key in ('CURSOR_ALLOW_NO_ZDR', 'CURSOR_FAST', 'CURSOR_AGENT_BIN', 'CURSOR_MODEL', 'CURSOR_EFFORT', 'CURSOR_HOME', 'CURSOR_MODE', 'MIN_PCT', 'AGY_MODELS', 'AGY_EFFORT', 'GROK_EFFORT', 'GROK_HOME', 'CODEX_BIN', 'CODEX_MODEL', 'CODEX_EFFORT', 'CODEX_SERVICE_TIER', 'CODEX_SANDBOX', 'CODEX_HOME', 'GODFATHER_MODEL_KEY', 'GODFATHER_PROJECT_CWD', 'ADD_DIRS', 'GODFATHER_CHECK', 'GODFATHER_SELF', 'GODFATHER_BRANCH', 'GODFATHER_MAX_FIX_ROUNDS', 'GODFATHER_CONTEXT', 'GODFATHER_HEAL_PARENT', 'GODFATHER_REVIEW_BY', 'GODFATHER_REVIEW_FIX', 'GODFATHER_REVIEW_ROUNDS', 'GODFATHER_REVIEW_ROUND', 'GODFATHER_REVIEW_THRESHOLD', 'GODFATHER_REVIEW_HEAD', 'GODFATHER_REVIEW_TASK', 'GODFATHER_REVIEW_PARENT', 'GODFATHER_REVIEW_SEGMENT', 'GODFATHER_TASK_TYPE') if key in os.environ})
            if os.getenv('AGENT_ROUTER_NAME'):
                run.update(router_name=os.environ['AGENT_ROUTER_NAME'], router_id=os.getenv('AGENT_ROUTER_ID'))
            from godfather_safety import adopt_workspace
            if (os.getenv('AGENT_NUDGE') == '1' or resume) and 'resources' in old:
                run['resources'] = old.get('resources', [])
                for key in ('repo', 'base_branch', 'base_sha'):
                    if key in old:
                        run[key] = old[key]
            elif os.getenv('GODFATHER_RESOURCES'):
                run['resources'] = json.loads(os.environ['GODFATHER_RESOURCES'])
                run.update(json.loads(os.getenv('GODFATHER_RESOURCE_CONTEXT', '{}')))
            else:
                run['resources'] = []
                adopt_workspace(run['cwd'], run['resources'])
            # A queued dependent may bind immediately after this publication.
            # Never expose a routed segment as a different logical generation.
            write(args.base + '.run.json', run)
        return 0
    if args.operation == 'run':
        return supervise(args)
    if args.operation == 'finish':
        return finish(args.base, args.rc, args.quota)
    if args.operation == 'grok-result':
        stream_path = Path(args.base + '.stream.jsonl')
        if not stream_path.is_file():
            # supervise() returns 2 before opening the stream when the working
            # directory is locked or refused. That used to traceback here.
            print('Error: Grok stream file is missing: ' + str(stream_path), file=sys.stderr)
            return 1
        events = []
        for line in stream_path.read_text(errors='replace').splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        end = next((e for e in reversed(events) if e.get('type') == 'end'), {})
        idle = read(args.base + '.run.json')
        from godfather_stream import diagnostic_level
        errors = [e for e in events if e.get('type') == 'error' and
                  diagnostic_level(e.get('message') or e.get('error') or e) == 'error']
        result = dict(end, type='error' if errors else 'result', is_error=bool(errors),
                      text=''.join(e.get('data', '') for e in events if e.get('type') == 'text'))
        if idle.get('idle_finished') and not errors:
            result['text'] = idle.get('final_message') or result['text']
        if errors:
            result['errors'] = errors
        write(args.base + '.json', result)
        if not end and not read(args.base + '.run.json').get('idle_finished'):
            print('Error: Grok stream ended without terminal end event', file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(internal_main())
