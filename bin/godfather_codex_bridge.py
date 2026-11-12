"""Opt-in Codex app-server stdio adapter. No third-party dependencies."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import socket
import stat
import subprocess
import sys
import threading
import time


def endpoint(base):
    # macOS AF_UNIX paths must fit 104 bytes, including long run directories.
    return '/tmp/godfather-codex-' + hashlib.sha256(str(base).encode()).hexdigest()[:24] + '.sock'


def control(base, method, message=None):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(endpoint(base))
        sock.sendall((json.dumps(dict(method=method, message=message)) + '\n').encode())
        reply = json.loads(sock.makefile().readline())
        if 'error' in reply:
            raise ValueError(reply['error'])
        return reply


def emit(typ, **fields):
    print(json.dumps(dict(type=typ, **fields)), flush=True)


def approval_response(method, allowed, reason, permissions=None):
    if method == 'item/permissions/requestApproval':
        return dict(permissions=permissions if allowed else {}, scope='turn', _reason=reason)
    if method in ('execCommandApproval', 'applyPatchApproval'):
        decision = 'approved' if allowed else dict(denied=dict(rejection=reason))
    else:
        decision = 'accept' if allowed else 'decline'
    return dict(decision=decision, _reason=reason)


def merge_snapshot(previous, update):
    """Quota notifications are patches; missing/null fields retain known values."""
    result = dict(previous)
    for key, value in (update or {}).items():
        if isinstance(value, dict):
            result[key] = merge_snapshot(result.get(key) or {}, value)
        elif value is not None:
            result[key] = value
    return result


def approval(base, method, params, items):
    import godfather_policy as policy
    import godfather_safety as safety
    from godfather_permissions import parse_choice
    context = safety.read(str(base) + '.safety.json')
    cwd = params.get('cwd') or context.get('cwd') or os.getcwd()
    command = params.get('command')
    paths = dict(params.get('fileChanges') or {})
    if 'fileChange' in method:
        paths = {c['path']: c for c in items.get(params.get('itemId'), {}).get('changes', [])}
    for path, change in list(paths.items()):
        if isinstance(change, dict):
            destination = change.get('move_path') or (change.get('kind') or {}).get('move_path')
            if destination:
                paths[destination] = dict(type='add')
    if command:
        argv = command if isinstance(command, list) else ['sh', '-c', command]
        effect = policy.evaluate(argv, context, cwd=cwd)
    elif paths:
        effects = [policy.evaluate(['rm' if isinstance(change, dict) and
                    (change.get('type') == 'delete' or (change.get('kind') or {}).get('type') == 'delete') else 'touch', path], context, cwd=cwd)
                   for path, change in paths.items()]
        effect = max(effects, key=lambda e: {'allow': 0, 'ask': 1, 'deny': 2}[e['verdict']])
        argv = ['apply_patch'] + list(paths)
    else:
        argv = ['request_permissions']
        effect = dict(verdict='ask', effect='grant requested permissions or approve an unrecognized action', targets=[], cwd=cwd)
    if effect['verdict'] == 'allow' and (params.get('grantRoot') or params.get('networkApprovalContext') or params.get('kind', 'command') != 'command'):
        effect.update(verdict='ask', effect='grant broader permissions or approve command input')
    # Permissions may be broad directory grants. Always ask, but retain hard secret denies.
    permissions = params.get('permissions') or {}
    filesystem = permissions.get('fileSystem') or {}
    writes = list(filesystem.get('write') or [])
    for entry in filesystem.get('entries') or []:
        if entry.get('access') == 'write':
            path = entry.get('path') or {}
            writes.append(path.get('path') or path.get('pattern') or '')
    if params.get('grantRoot'):
        writes.append(params['grantRoot'])
    if any(policy.secret(str(path)) for path in writes):
        effect.update(verdict='deny', effect='write a secret file')
    verdict, reason = effect['verdict'], effect['effect']
    if verdict == 'ask':
        # Public ask owns persistence, blocking answers, and incident learning.
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('godfather')), 'ask',
            'Allow Codex action? ' + json.dumps(params) + ' Policy: ' + reason,
            '--options', 'allow|deny', '--blocking'], stdout=subprocess.PIPE, text=True)
        answer = result.stdout.strip()
        try:
            choice = parse_choice(answer, 'allow|deny')[0]
        except ValueError:
            choice = 'deny'
        verdict = 'allow' if result.returncode == 0 and choice == 'allow' else 'deny'
        reason += '; answer: ' + answer
    policy.incident(base, argv, cwd, effect['targets'], verdict, reason, source='app-server')
    safety.event(base, 'APPROVAL', verdict + ': ' + reason)
    emit('bridge.approval', verdict=verdict, reason=reason, method=method)
    allowed = verdict == 'allow'
    return approval_response(method, allowed, reason, params.get('permissions'))


class Bridge:
    def __init__(self, base, command, prompt):
        self.base, self.command, self.prompt = base, command, prompt
        self.inbox = queue.Queue()
        self.pending = {}
        self.counter = 0
        self.thread = self.turn = None
        self.items = {}
        self.finished = False
        self.code = 0
        self.started = False
        self.bound = False
        self.baseline = {}
        self.totals = {}
        self.rate_limits = {}
        self.resume = command[1:3] == ['exec', 'resume']
        self.baseline_known = not self.resume
        self.last = Path(str(base) + '.last.md')
        self.state = Path(str(base) + '.app-server.json')
        self.write_lock = threading.Lock()

    def send(self, value):
        with self.write_lock:
            self.child.stdin.write(json.dumps(value) + '\n')
            self.child.stdin.flush()

    def request(self, method, params):
        self.counter += 1
        key = self.counter
        self.send(dict(id=key, method=method, params=params))
        return key

    def wait(self, key):
        deadline = time.monotonic() + float(os.getenv('GODFATHER_CODEX_HANDSHAKE_TIMEOUT', '15'))
        while key not in self.pending:
            self.pump(max(0, deadline - time.monotonic()))
        result = self.pending.pop(key)
        if 'error' in result:
            raise ValueError(json.dumps(result['error']))
        return result['result']

    def reader(self):
        try:
            for line in self.child.stdout:
                self.inbox.put(json.loads(line))
        except (ValueError, OSError) as exc:
            self.inbox.put(exc)
        finally:
            self.inbox.put(EOFError('app-server closed stdout'))

    def pump(self, timeout=None):
        try:
            msg = self.inbox.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError('app-server handshake timed out')
        if isinstance(msg, Exception):
            raise msg
        if 'method' not in msg:
            self.pending[msg['id']] = msg
        elif 'id' in msg:
            method = msg['method']
            known = method in ('execCommandApproval', 'applyPatchApproval', 'item/commandExecution/requestApproval',
                               'item/fileChange/requestApproval', 'item/permissions/requestApproval')
            if known:
                handler_failed = False
                try:
                    result = approval(self.base, method, msg.get('params', {}), self.items)
                    reason = result.pop('_reason')
                except Exception as exc:
                    # Policy, ask, parsing and audit errors must still answer the RPC.
                    reason = 'Approval handler failed; denied: ' + str(exc)
                    result = approval_response(method, False, reason)
                    result.pop('_reason')
                    handler_failed = True
                self.send(dict(id=msg['id'], result=result))
                if handler_failed:
                    # Answer first: even a broken audit sink cannot strand the server.
                    with contextlib.suppress(Exception):
                        emit('bridge.approval', verdict='deny', reason=reason, method=method)
                decision = result.get('decision')
                if (decision == 'decline' or isinstance(decision, dict) and 'denied' in decision or
                        result.get('permissions') == {}):
                    # Decision enums have no reason slot; provide the audit reason as turn input.
                    if self.thread and self.turn:
                        self.request('turn/steer', dict(threadId=self.thread, expectedTurnId=self.turn,
                            input=[dict(type='text', text='Godfather denied the requested action: ' + reason, text_elements=[])]))
            else:
                self.send(dict(id=msg['id'], error=dict(code=-32601, message='Unsupported client request: ' + method)))
                emit('bridge.unsupported', method=method)
        else:
            self.notification(msg['method'], msg.get('params', {}))

    def notification(self, method, p):
        if method == 'turn/started':
            self.turn = p['turn']['id']
            emit('turn.started')
        elif method in ('item/started', 'item/completed'):
            item = p['item']
            self.items[item['id']] = item
            typ = {'agentMessage': 'agent_message', 'commandExecution': 'command_execution',
                   'fileChange': 'file_change', 'mcpToolCall': 'mcp_tool_call',
                   'webSearch': 'web_search'}.get(item['type'], item['type'])
            mapped = dict(item, type=typ)
            emit('item.started' if method.endswith('started') else 'item.completed', item=mapped)
            if method == 'item/completed' and typ == 'agent_message':
                self.last.write_text(item.get('text', ''))
        elif method == 'thread/tokenUsage/updated':
            usage = p['tokenUsage']['total']
            self.totals = usage
            if not self.baseline_known:
                last = p['tokenUsage'].get('last')
                if last:
                    self.baseline = {key: max(0, value - last.get(key, 0)) for key, value in usage.items() if isinstance(value, (int, float))}
                self.baseline_known = True
            from godfather_safety import atomic
            atomic(self.state, dict(thread_id=self.thread, token_totals=usage))
            usage = {key: max(0, value - self.baseline.get(key, 0)) for key, value in usage.items() if isinstance(value, (int, float))}
            emit('event_msg', payload=dict(type='token_count', info=dict(total_token_usage=dict(
                input_tokens=usage.get('inputTokens', 0), cached_input_tokens=usage.get('cachedInputTokens', 0),
                output_tokens=usage.get('outputTokens', 0)))))
        elif method == 'account/rateLimits/updated':
            self.rate_limits = merge_snapshot(self.rate_limits, p.get('rateLimits'))
            emit('bridge.rate_limits', rate_limits=self.rate_limits)
        elif method == 'error':
            emit('error', error=p['error'], willRetry=p.get('willRetry', False))
        elif method == 'turn/completed':
            turn = p['turn']
            self.finished = True
            self.code = 0 if turn['status'] == 'completed' else 1
            if self.code:
                emit('turn.failed', error=turn.get('error') or dict(message='Turn ' + turn['status']))
            else:
                # token_count already carries cumulative usage; do not count twice.
                emit('turn.completed', usage={})
        else:
            # Deltas and experimental notifications are heartbeats, never lost silence.
            emit('bridge.notification', method=method)

    def controls(self, listener):
        while not self.finished:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    p = json.loads(conn.makefile().readline())
                    if not self.turn:
                        raise ValueError('no active turn')
                    method = p['method']
                    if method not in ('turn/steer', 'turn/interrupt'):
                        raise ValueError('unsupported control')
                    params = dict(threadId=self.thread)
                    if method == 'turn/steer':
                        params.update(expectedTurnId=self.turn, input=[dict(type='text', text=p['message'], text_elements=[])])
                    else:
                        params['turnId'] = self.turn
                    key = self.request(method, params)
                    # Main loop keeps draining notifications and resolves RPC errors.
                    deadline = time.monotonic() + 4
                    while key not in self.pending and time.monotonic() < deadline:
                        time.sleep(.01)
                    reply = self.pending.pop(key, dict(error=dict(message='control acknowledgement timed out')))
                    result = dict(error=json.dumps(reply['error'])) if 'error' in reply else dict(ok=True)
                except Exception as exc:
                    result = dict(error=str(exc))
                conn.sendall((json.dumps(result) + '\n').encode())

    def run(self):
        if self.command[1:2] != ['exec']:
            raise ValueError('wrapped/container commands retain exec transport')
        config = {}
        for i, arg in enumerate(self.command[:-1]):
            if arg == '-c':
                key, value = self.command[i + 1].split('=', 1)
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
                if key == 'shell_environment_policy.set' and isinstance(value, str):
                    value = {k: json.loads(v) for k, v in re.findall(r'(\w+)=("(?:[^"\\]|\\.)*")', value)}
                config[key] = value
        profile = json.loads(Path(str(self.base) + '.permissions.json').read_text())
        config.update(approval_policy='on-request', model_reasoning_effort=config.get('model_reasoning_effort', 'high'))
        config['features.fast_mode'] = False
        params = dict(cwd=os.getcwd(), sandbox=profile['sandbox'], approvalPolicy='on-request', config=config)
        if '-m' in self.command:
            params['model'] = self.command[self.command.index('-m') + 1]
        params['serviceTier'] = config.get('service_tier', 'default')
        self.child = subprocess.Popen([self.command[0], 'app-server'], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=sys.stderr, text=True, bufsize=1)
        threading.Thread(target=self.reader, daemon=True).start()
        self.wait(self.request('initialize', dict(clientInfo=dict(name='godfather', version='0.1'),
                                                  capabilities=dict(experimentalApi=True))))
        self.send(dict(method='initialized'))
        resume = self.command[3] if self.command[1:3] == ['exec', 'resume'] else None
        if resume:
            params['threadId'] = resume
        result = self.wait(self.request('thread/resume' if resume else 'thread/start', params))
        self.thread = result['thread']['id']
        from godfather_safety import atomic, read
        saved = read(self.state)
        if resume and saved.get('thread_id') == self.thread:
            self.baseline = saved.get('token_totals') or {}
            self.baseline_known = bool(self.baseline)
        atomic(self.state, dict(thread_id=self.thread, token_totals=self.baseline))
        emit('thread.started', thread_id=self.thread)
        # From this point a server might have accepted work: never replay via exec.
        self.started = True
        result = self.wait(self.request('turn/start', dict(threadId=self.thread,
            effort=config['model_reasoning_effort'], input=[dict(type='text', text=self.prompt, text_elements=[])])))
        self.turn = result['turn']['id']
        path = endpoint(self.base)
        if os.path.lexists(path):
            info = os.lstat(path)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('unsafe existing control endpoint')
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.connect(path)
                except ConnectionRefusedError:
                    os.unlink(path)
                else:
                    raise ValueError('control endpoint already active')
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(endpoint(self.base))
            self.bound = True
            os.chmod(endpoint(self.base), 0o600)
            listener.listen(4)
            listener.settimeout(.2)
            controller = threading.Thread(target=self.controls, args=(listener,), daemon=True)
            controller.start()
            while not self.finished:
                self.pump()
            controller.join(timeout=5)
        return self.code


def main():
    base, command = Path(sys.argv[1]), sys.argv[2:]
    prompt = sys.stdin.read()
    bridge = Bridge(base, command, prompt)
    try:
        return bridge.run()
    except (OSError, ValueError, KeyError, TimeoutError, EOFError) as exc:
        if bridge.started:
            emit('turn.failed', error=dict(codexErrorInfo='responseStreamDisconnected', message=str(exc)))
            return 1
        reason = 'app-server startup/handshake failed; falling back to exec: ' + str(exc)
        emit('bridge.fallback', message=reason)
        with open(str(base) + '.models.log', 'a') as log:
            log.write(reason + '\n')
    finally:
        if hasattr(bridge, 'child'):
            bridge.child.terminate()
            try:
                bridge.child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                bridge.child.kill()
                bridge.child.wait()
        if bridge.bound:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(endpoint(base))
    return subprocess.run(command, input=prompt, text=True).returncode


if __name__ == '__main__':
    sys.exit(main())
