"""Thin MCP interface. Official SDK when present; dependency-free stdio fallback."""
import concurrent.futures
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from godfather_store import NONTERMINAL, TERMINAL
from godfather_registry import STATUS_MAP

import agent_oversight as a
import godfather_safety as safety


def schema(properties=None, required=()):
    return dict(type='object', properties=properties or {}, required=list(required), additionalProperties=False)


S = {'type': 'string'}
TOOLS = [
    dict(name='run', description='Start a routed task in the background and return its run id immediately. Isolation defaults on (a git worktree, or a scratch copy outside git); pass isolate=false to use cwd.', inputSchema=schema(
        dict(prompt=S, name=S, cwd=S, isolate={'type': 'boolean'}, check=S, review_by=S), ('prompt', 'name', 'cwd'))),
    dict(name='status', description='Structured active/recent run status.', inputSchema=schema(dict(name=S))),
    dict(name='events', description='Poll events since the returned byte cursor. Omit since to read all retained events.', inputSchema=schema(dict(since={'type': 'integer', 'minimum': 0}))),
    dict(name='wait', description='Wait for every segment and pending fallback to finish; timeout returns current state.', inputSchema=schema(dict(name=S, timeout_s={'type': 'number', 'minimum': 0}), ('name', 'timeout_s'))),
    dict(name='answer', description='Answer a pending question; a stopped session resumes in the background.', inputSchema=schema(dict(name=S, answer=S), ('name', 'answer'))),
    dict(name='nudge', description='Resume the exact session with a corrective message in the background.', inputSchema=schema(dict(name=S, message=S), ('name', 'message'))),
    dict(name='cancel', description='Stop only the verified processes owned by this registered run.', inputSchema=schema(dict(name=S), ('name',))),
    dict(name='quota', description='Persisted quota cooldowns.', inputSchema=schema()),
    dict(name='usage', description='Token totals, estimated USD, quota burn and latest agy /usage snapshot.', inputSchema=schema(dict(period={'type': 'string', 'enum': ['today', 'week', 'all']}))),
]


def validate(name, values):
    tool = next((t for t in TOOLS if t['name'] == name), None)
    if not tool:
        raise ValueError('Unknown tool: ' + str(name))
    spec = tool['inputSchema']
    if not isinstance(values, dict) or set(values) - set(spec['properties']) or set(spec['required']) - set(values):
        raise ValueError('Invalid tool arguments')
    for key, value in values.items():
        rule = spec['properties'][key]
        typ = rule['type']
        valid = (isinstance(value, str) if typ == 'string' else isinstance(value, bool) if typ == 'boolean' else
                 isinstance(value, int) and not isinstance(value, bool) if typ == 'integer' else
                 isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))
        if not valid or ('minimum' in rule and value < rule['minimum']) or ('enum' in rule and value not in rule['enum']):
            raise ValueError('Invalid argument: ' + key)


def named_base(name):
    if not name or '/' in name or '\0' in name or name in ('.', '..'):
        raise ValueError('name must be a filename without slashes')
    root = a.runs_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def background(argv, base, env=None):
    with open(str(base) + '.mcp.log', 'ab') as output:
        child = subprocess.Popen([str(Path(__file__).with_name('godfather'))] + argv,
                                 env=env or safety.original_environment(), stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=output, start_new_session=True)
    # Reap without making the server the lifetime owner; work survives stdio EOF.
    threading.Thread(target=child.wait, daemon=True).start()
    return child


def start_run(values):
    base = named_base(values['name'])
    cwd = Path(values['cwd']).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError('cwd must be an existing directory')
    with open(str(base) + '.mcp.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        job = a.read(str(base) + '.job.json')
        run = a.read(str(safety.resolve_name(values['name'])) + '.run.json')
        if a.alive(job.get('pid'), job.get('identity')) or a.run_alive(run) or (run.get('status') in NONTERMINAL and a.owners_alive(run)):
            raise ValueError('Run name already active: ' + values['name'])
        run_id = str(uuid.uuid4())
        env = dict(safety.original_environment(), CWD=str(cwd), GODFATHER_ROUTE_ID=run_id)
        argv = ['run', '-p', values['prompt']]
        for option in ('check', 'review_by'):
            if values.get(option):
                argv += ['--' + option.replace('_', '-'), values[option]]
        if 'isolate' in values:
            argv.append('--isolate' if values['isolate'] else '--no-isolate')
        argv.append(values['name'])
        child = background(argv, base, env)
        a.write(str(base) + '.job.json', dict(run_id=run_id, pid=child.pid,
                identity=a.processes().get(child.pid, {}).get('identity'), started_at=time.time()))
    return dict(name=values['name'], run_id=run_id, status='queued')


def report(name):
    base = named_base(name)
    from godfather_lifecycle import logical_status
    logical = logical_status(name)
    if logical in ('queued', 'dependency_failed', 'healing'):
        return dict(name=name, status=logical)
    route = a.read(str(base) + '.route.json')
    job = a.read(str(base) + '.job.json')
    if job and job['run_id'] != route.get('run_id'):
        living = a.alive(job['pid'], job.get('identity'))
        return dict(name=name, run_id=job['run_id'], status='queued' if living else 'failed',
                    log=str(base) + '.mcp.log')
    active = safety.resolve_name(name)
    run = a.read(str(active) + '.run.json')
    if not run:
        if route.get('status') == 'running':
            return dict(name=name, run_id=route.get('run_id'), status='running')
        if route:
            return dict(name=name, run_id=route.get('run_id'), status='failed', log=str(base) + '.models.log')
        raise ValueError('Run is not registered: ' + name)
    result = dict(run)
    result['name'] = name
    if run.get('review_pending'):
        reviewing = report(run['review_name'])
        result.update(status=reviewing['status'], review=reviewing)
        if reviewing.get('question'):
            result['question'] = reviewing['question']
        return result
    pending = [q for q in run.get('questions', []) if q.get('answer') is None or not q.get('delivered', True)]
    from godfather_store import TERMINAL
    if logical in TERMINAL:
        result['status'] = logical
    elif pending:
        result.update(status='needs_input', question=pending[-1])
    elif logical == 'died':
        result['status'] = 'died'
    # A live router can be checking/reviewing after its launcher completes.
    elif route.get('status') == 'running':
        result['status'] = 'running' if run.get('status') == 'done' else run['status']
    if result['status'] in TERMINAL or result['status'] == 'died':
        result['report'] = __import__('godfather_router').tail(str(active) + '.last.md', 10000)
        if run.get('review_file'):
            result['review'] = __import__('godfather_router').tail(run['review_file'], 10000)
    return result


def cancel_run(name):
    base = named_base(name)
    from godfather_lifecycle import logical_status
    from godfather_store import TERMINAL
    status = logical_status(name)
    if status in TERMINAL:
        # Durable completion precedes the last owner's exit. Do not turn a
        # completed run into a cancellation during that finalization window.
        return dict(name=name, status=status, cancelled=False)
    route = a.read(str(base) + '.route.json')
    if a.alive(route.get('router_pid'), route.get('router_identity')):
        os.kill(route['router_pid'], signal.SIGTERM)
        return dict(name=name, status='cancelling')
    job = a.read(str(base) + '.job.json')
    if a.alive(job.get('pid'), job.get('identity')):
        os.kill(job['pid'], signal.SIGTERM)
        return dict(name=name, status='cancelling')
    run = a.read(str(safety.resolve_name(name)) + '.run.json')
    procs = a.processes()
    supervisor = run.get('supervisor_pid')
    if (a.run_alive(run, procs) and supervisor and procs.get(supervisor, {}).get('ppid') == run.get('launcher_pid') and
            procs[run['pid']]['ppid'] == supervisor):
        os.kill(supervisor, signal.SIGTERM)
        return dict(name=name, status='cancelling')
    if not run:
        raise ValueError('Run is not registered')
    return dict(name=name, status=run.get('status'), cancelled=False)


def events(since=0):
    root = a.runs_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'mcp-events.jsonl'
    with open(root / 'mcp-events.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        seen = set()
        if path.exists():
            for line in path.read_text().splitlines():
                seen.add(json.loads(line)['id'])
        collected = []
        for source in list(root.glob('*.events.jsonl')) + list(root.glob('*.monitor.jsonl')):
            for line in source.read_text(errors='replace').splitlines():
                try:
                    collected.append(json.loads(line))
                except ValueError:
                    pass
        for source in root.glob('*.run.json'):
            run = a.read(source)
            if not run:
                continue
            uid = run.get('run_id', run['started_at'])
            collected.append(dict(id=uid + ':start', at=a.epoch(run['started_at']), kind='START', name=run['name'], detail=run['cli'] + ' ' + run['model']))
            if STATUS_MAP.get(run.get('status'), run.get('status')) in TERMINAL:
                collected.append(dict(id=uid + ':done', at=a.epoch(run.get('ended_at')), kind='DONE', name=run['name'],
                                      detail=safety.done_detail(root / run['name'], 'exit=' + str(run.get('exit_code')))))
        for source in root.glob('*.route.json'):
            route = a.read(source)
            for i, switch in enumerate(route.get('switches', [])):
                collected.append(dict(id=route['run_id'] + ':switch:' + str(i), at=a.epoch(route['started_at']), kind='SWITCH', name=route['name'], detail=switch))
        with path.open('ab') as output:
            for entry in sorted(collected, key=lambda e: (e['at'], e['id'])):
                if entry['id'] not in seen:
                    output.write((json.dumps(entry) + '\n').encode())
                    seen.add(entry['id'])
        with path.open('rb') as source:
            if since < 0 or since > path.stat().st_size:
                raise ValueError('Invalid event cursor')
            source.seek(since)
            result = [json.loads(line) for line in source]
            return dict(events=result, cursor=source.tell())


def call_tool(name, values):
    validate(name, values)
    if name == 'run':
        return start_run(values)
    if name == 'status':
        return report(values['name']) if values.get('name') else a.snapshot(a.runs_dir())
    if name == 'events':
        return events(values.get('since', 0))
    if name == 'wait':
        from godfather_lifecycle import wait_run
        status, timed_out = wait_run(values['name'], values['timeout_s'])
        return dict(report(values['name']), status=status, timed_out=timed_out)
    if name in ('answer', 'nudge'):
        base = named_base(values['name'])
        run = report(values['name'])
        target = run.get('review_name') if run.get('review_pending') else values['name']
        text = values['answer' if name == 'answer' else 'message']
        child = background([name, target, text], base)
        return dict(name=values['name'], status='submitted', pid=child.pid)
    if name == 'cancel':
        return cancel_run(values['name'])
    if name == 'quota':
        from godfather_router import quota_state
        with quota_state() as data:
            return {key: dict(value, active=a.epoch(value['until']) > time.time()) for key, value in data.items()}
    if name == 'usage':
        from godfather_usage import summary
        return summary(values.get('period'))


def tool_result(name, values):
    try:
        data = call_tool(name, values)
        result = dict(content=[dict(type='text', text=json.dumps(data))], isError=False)
        if isinstance(data, dict):
            result['structuredContent'] = data
        return result
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return dict(content=[dict(type='text', text=str(exc))], isError=True)


def serve_fallback():
    """MCP 2025-06-18 stdio: one JSON-RPC message per line, stdout protocol only."""
    output_lock = threading.Lock()
    def send(message):
        with output_lock:
            print(json.dumps(message), flush=True)
    def handle(request):
        uid = request.get('id')
        method = request.get('method')
        if uid is None:
            return  # Initialized/cancel notifications have no response.
        response = dict(jsonrpc='2.0', id=uid)
        params = request.get('params', {})
        if not isinstance(params, dict):
            response['error'] = dict(code=-32602, message='Invalid params')
            send(response)
            return
        if method == 'initialize':
            requested = params.get('protocolVersion')
            response['result'] = dict(protocolVersion=requested if requested in ('2024-11-05', '2025-03-26', '2025-06-18') else '2025-06-18',
                                      capabilities=dict(tools=dict(listChanged=False)), serverInfo=dict(name='godfather', version='1.0'))
        elif method == 'ping':
            response['result'] = {}
        elif method == 'tools/list':
            response['result'] = dict(tools=TOOLS)
        elif method == 'tools/call':
            response['result'] = tool_result(params.get('name'), params.get('arguments', {}))
        else:
            response['error'] = dict(code=-32601, message='Method not found')
        send(response)
    # wait must not prevent another request (answer/cancel/status) being serviced.
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
                    send(dict(jsonrpc='2.0', id=None, error=dict(code=-32600, message='Invalid Request')))
                    continue
                pool.submit(handle, request)
            except ValueError:
                send(dict(jsonrpc='2.0', id=None, error=dict(code=-32700, message='Parse error')))
    return 0


def serve():
    try:
        import mcp.types as types
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError:
        return serve_fallback()
    import asyncio
    server = Server('godfather')
    @server.list_tools()
    async def list_tools():
        return [types.Tool(**tool) for tool in TOOLS]
    @server.call_tool()
    async def call(name, arguments):
        # Blocking CLI waits run off the event loop, so answer/cancel still work.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, tool_result, name, arguments)
        return types.CallToolResult(**result)
    async def run_server():
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    asyncio.run(run_server())
    return 0
