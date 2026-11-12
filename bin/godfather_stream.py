"""Heartbeats parsed from CLI streams.

A frame counts only after its newline. A partial line updates nothing.
A line longer than MAX_LINE_BYTES is spilled and becomes one tool heartbeat
with detail "oversized". It is not parsed and it is not a segment exit.

Progress is a tool call or a file change (last_tool_at). Thought, text,
usage, and stream markers are heartbeats, and they do not move that clock.
"""
import json
from pathlib import Path
import re
import time

import godfather_errors as errors
from godfather_policy import config_dir, document

_OPTIONAL_MCP = {}


def optional_mcp():
    """Hosts from [watch].optional_mcp in godfather.toml, reread when the file changes."""
    path = config_dir() / 'godfather.toml'
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return []
    if _OPTIONAL_MCP.get('stamp') != stamp:
        hosts = document(path.read_text()).get('watch', {}).get('optional_mcp', [])
        if not isinstance(hosts, list) or not all(isinstance(host, str) and host for host in hosts):
            raise ValueError('watch.optional_mcp must be an array of host names')
        _OPTIONAL_MCP.update(stamp=stamp, hosts=[host.lower() for host in hosts])
    return _OPTIONAL_MCP['hosts']


MAX_LINE_BYTES = 1048576
SILENCE_SECS = 120
THINK_WITHOUT_TOOL_SECS = 180
TOOL_SECS = 600
NON_PROGRESS = ('thought', 'text', 'usage', 'stream')
CODEX_TOOLS = ('command_execution', 'file_change', 'mcp_tool_call', 'web_search')
FILE_TOOLS = ('search_replace', 'write', 'edit', 'Edit', 'Write')
AUTH_RE = errors.REGEX['auth']
BROKEN_RE = errors.REGEX['broken_stream']
CAPACITY_RE = errors.REGEX['capacity']


def _triple(usage):
    if not isinstance(usage, dict):
        return None
    inn = usage.get('input_tokens', usage.get('inputTokens'))
    out = usage.get('output_tokens', usage.get('outputTokens'))
    cached = usage.get('cached_input_tokens', usage.get('cache_read_input_tokens',
                       usage.get('cache_read_tokens', usage.get('cacheReadTokens', usage.get('cacheReadInputTokens')))))
    if inn is None and out is None and cached is None:
        return None
    return (int(inn or 0), int(cached or 0), int(out or 0))


def _short(value, limit=160):
    text = '' if value is None else str(value)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit]


def _target(raw):
    if not isinstance(raw, dict):
        return ''
    for key in ('file_path', 'path', 'target_directory', 'AbsolutePath', 'FilePath', 'TargetFile'):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return Path(value).name
    for key in ('command', 'cmd', 'CommandLine'):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return _short(value, 80)
    return ''


def _failure(message):
    failure = errors.classify('', message)
    return errors.public_class(failure) if failure['category'] != 'other' and not failure['transient'] else None


def _beat(kind, detail, progress=False, failure_class=None, tool_id=None, tool_state=None,
          file_change=False, token_mode=None, token_values=None, grok_key=None):
    return dict(kind=kind, detail=_short(detail), progress=bool(progress), failure_class=failure_class,
                tool_id=tool_id, tool_state=tool_state, file_change=bool(file_change),
                token_mode=token_mode, token_values=token_values, grok_key=grok_key)


def _codex_detail(item):
    kind = item.get('type') or 'tool'
    if kind == 'file_change':
        changes = item.get('changes') or []
        if isinstance(changes, list) and changes and isinstance(changes[0], dict):
            return 'file_change ' + Path(str(changes[0].get('path') or '')).name
        return 'file_change'
    if kind == 'command_execution':
        return 'command_execution ' + _short(item.get('command') or '', 80)
    if kind == 'web_search':
        return 'web_search ' + _short(item.get('query') or '', 80)
    if kind == 'mcp_tool_call':
        return 'mcp_tool_call ' + _short(item.get('tool') or item.get('name') or item.get('server') or '', 80)
    return kind


def diagnostic_level(message):
    """Known optional/startup/tool-lifecycle diagnostics are not run failures."""
    text = str(message)
    if re.search(r'permission denied|authentication failed|unauthorized|invalid.api.key|quota exhausted|rate.limit|\b429\b', text, re.I):
        return 'error'
    if re.search(r'Unable to start git event watcher', text, re.I):
        return 'warning'
    if re.search(r'\bPhase\s+2\s*[:,-]?\s*no changes\b', text, re.I):
        return 'info'
    if re.search(r'write_stdin failed:\s*Unknown process id\b', text, re.I):
        return 'warning'
    if any(host in text.lower() for host in optional_mcp()) and re.search(
            r'unreachable|connection (?:refused|failed)|failed to (?:connect|initialize|resolve|send request)|(?:startup|transport) (?:error|failed)|timed? out', text, re.I):
        return 'warning'
    return 'error'


def _error_beat(message, cli='', event=None):
    text = _short(message, 240)
    failure = errors.classify(cli, event if event is not None else message)
    if failure['transient'] or (failure['source'] == 'text' and diagnostic_level(message) != 'error'):
        return _beat('stream', text)
    beat = _beat('error', text or 'error', failure_class=(errors.public_class(failure)
                 if failure['category'] != 'other' else None))
    beat.update(failure=failure, category=failure['category'], source=failure['source'])
    return beat


def classify(cli, obj):
    """One JSON object to one heartbeat, or None when the frame is not a heartbeat."""
    if not isinstance(obj, dict):
        return None
    if cli == 'cursor':
        return _cursor(obj)
    if cli in ('grok', 'codex'):
        info = _grok(obj) if cli == 'grok' else _codex(obj)
        if info and info['kind'] == 'text':
            info['message'] = obj.get('data') if cli == 'grok' else (obj.get('item') or {}).get('text')
        return info
    if cli == 'agy':
        return _agy(obj)
    return None


def _cursor(obj):
    typ = obj.get('type')
    if typ == 'error' or obj.get('is_error') or (typ == 'result' and obj.get('subtype') != 'success'):
        return _error_beat(obj.get('message') or obj.get('error') or obj.get('result') or obj, 'cursor', obj)
    if typ == 'tool_call':
        raw = obj.get('tool_call') or {}
        name = next((key for key, value in raw.items() if key.endswith('ToolCall') and isinstance(value, dict) and value), 'tool')
        detail = raw.get(name) or {}
        args = detail.get('args', {}) if isinstance(detail, dict) else {}
        return _beat('tool', name + ' ' + _target(args), progress=True,
                     tool_id=obj.get('call_id') or name,
                     tool_state='close' if obj.get('subtype') == 'completed' else 'open',
                     file_change=any(word in name.lower() for word in ('write', 'edit', 'delete')))
    if typ == 'assistant':
        message = ''.join(c.get('text', '') for c in (obj.get('message') or {}).get('content', []) if isinstance(c, dict))
        beat = _beat('text', message)
        beat['message'] = message
        return beat
    if typ == 'thinking':
        return _beat('thought', obj.get('text') or 'thinking')
    if typ == 'usage':
        return _beat('usage', 'usage', token_mode='grok', token_values=_triple(obj.get('usage')), grok_key=obj.get('model_call_id'))
    if typ == 'result':
        return _beat('end', 'result', token_mode='set', token_values=_triple(obj.get('usage')))
    return _beat('stream', typ or 'unknown')


def _grok(obj):
    typ = obj.get('type') or ''
    if typ in ('tool_call', 'tool_call_update'):
        name = obj.get('toolName') or obj.get('title') or 'tool'
        raw = obj.get('rawInput') if typ == 'tool_call' else None
        target = _target(raw or {})
        file_change = name in FILE_TOOLS or (isinstance(raw, dict) and any(raw.get(k) for k in ('file_path', 'path')))
        status = obj.get('status')
        closed = status in ('completed', 'failed', 'error')
        state = 'close' if (typ == 'tool_call_update' and closed) or (typ == 'tool_call' and closed) else 'open'
        if typ == 'tool_call_update' and not closed:
            state = 'open'
        detail = (name + (' ' + target if target else '')).strip()
        return _beat('tool', detail, progress=True, file_change=file_change and typ == 'tool_call',
                     tool_id=obj.get('toolCallId') or name, tool_state=state)
    if typ == 'thought':
        return _beat('thought', obj.get('data') or 'thought')
    if typ == 'text':
        return _beat('text', obj.get('data') or 'text')
    if typ == 'plan':
        return _beat('thought', 'plan')
    if typ == 'usage':
        usage = obj.get('usage') if isinstance(obj.get('usage'), dict) else obj
        key = obj.get('messageId') or None
        return _beat('usage', 'usage', token_mode='grok', token_values=_triple(usage), grok_key=key)
    if typ == 'end':
        return _beat('end', obj.get('stopReason') or 'end', token_mode='set', token_values=_triple(obj.get('usage')))
    if typ == 'available_commands':
        return _beat('stream', 'available_commands')
    if typ == 'error' or obj.get('is_error'):
        return _error_beat(obj.get('message') or obj.get('error') or obj, 'grok', obj)
    return None


def _codex(obj):
    typ = obj.get('type') or ''
    item = obj.get('item') if isinstance(obj.get('item'), dict) else {}
    itype = item.get('type')
    if typ.startswith('bridge.'):
        return _beat('stream', obj.get('method') or obj.get('reason') or obj.get('message') or typ)
    if typ == 'item.started' and itype in CODEX_TOOLS:
        return _beat('tool', _codex_detail(item), progress=True, file_change=itype == 'file_change',
                     tool_id=item.get('id') or itype, tool_state='open')
    if typ == 'item.completed' and itype in CODEX_TOOLS:
        return _beat('tool', _codex_detail(item), progress=True, file_change=itype == 'file_change',
                     tool_id=item.get('id') or itype, tool_state='close')
    if typ == 'item.completed' and itype == 'reasoning':
        return _beat('thought', item.get('text') or 'reasoning')
    if typ == 'item.completed' and itype == 'agent_message':
        return _beat('text', item.get('text') or 'agent_message')
    if typ == 'turn.completed':
        return _beat('usage', 'turn.completed', token_mode='add', token_values=_triple(obj.get('usage')))
    if typ in ('thread.started', 'turn.started'):
        return _beat('stream', typ)
    if typ in ('error', 'turn.failed'):
        err = obj.get('error') if isinstance(obj.get('error'), dict) else {}
        return _error_beat(obj.get('message') or err.get('message') or obj.get('error') or typ, 'codex', obj)
    payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
    if typ == 'event_msg':
        ptype = payload.get('type')
        if ptype == 'token_count':
            usage = ((payload.get('info') or {}).get('total_token_usage') or {})
            return _beat('usage', 'token_count', token_mode='set', token_values=_triple(usage))
        if ptype in ('error', 'stream_error', 'turn_aborted'):
            return _error_beat(payload.get('message') or payload.get('reason') or ptype, 'codex', payload)
        return None
    if typ == 'response_item':
        ptype = payload.get('type')
        if ptype in ('function_call', 'custom_tool_call'):
            raw = payload.get('arguments', payload.get('input', ''))
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = {}
            target = _target(raw)
            detail = (payload.get('name') or ptype) + (' ' + target if target else '')
            return _beat('tool', detail, progress=True,
                         tool_id=payload.get('call_id') or payload.get('id') or ptype, tool_state='open')
        if ptype in ('function_call_output', 'custom_tool_call_output'):
            return _beat('tool', ptype, progress=True,
                         tool_id=payload.get('call_id') or payload.get('id'), tool_state='close')
        if ptype == 'reasoning':
            summary = payload.get('summary') or []
            text = ' '.join(part.get('text', '') for part in summary if isinstance(part, dict))
            return _beat('thought', text or 'reasoning')
        if ptype == 'message' and payload.get('role') == 'assistant':
            content = payload.get('content') or []
            text = ' '.join(part.get('text', '') for part in content if isinstance(part, dict))
            return _beat('text', text or 'message')
    return None


def _agy(obj):
    typ = obj.get('type') or ''
    if typ in ('ERROR_MESSAGE', 'error') or obj.get('is_error') or obj.get('status') in ('ERROR', 'FAILED'):
        return _error_beat(obj.get('error') or obj.get('content') or obj.get('message') or typ, 'agy', obj)
    if typ == 'PLANNER_RESPONSE':
        calls = obj.get('tool_calls') or []
        if obj.get('input_tokens') is None and obj.get('output_tokens') is None and obj.get('cache_read_tokens') is None and obj.get('cached_input_tokens') is None:
            triple = None
        else:
            triple = (int(obj.get('input_tokens') or 0), int(obj.get('cache_read_tokens') or obj.get('cached_input_tokens') or 0),
                      int(obj.get('output_tokens') or 0))
        if calls:
            first = calls[0] if isinstance(calls[0], dict) else {}
            name = first.get('name') or 'tool'
            target = _target(first.get('args') or {})
            detail = name + ((' ' + target) if target else '')
            if len(calls) > 1:
                detail += ' +%d' % (len(calls) - 1)
            file_change = name in FILE_TOOLS or name in ('write_file', 'replace_file_content', 'edit_file')
            return _beat('tool', detail, progress=True, file_change=file_change, tool_id=name, tool_state='open',
                         token_mode='add', token_values=triple)
        return _beat('thought', obj.get('thinking') or obj.get('content') or 'planner',
                     token_mode='add' if triple else None, token_values=triple)
    if typ == 'GENERIC':
        return _beat('thought', obj.get('content') or 'content')
    if typ in ('USER_INPUT', 'SYSTEM_MESSAGE', 'CHECKPOINT'):
        return None
    return None


class LineReader:
    """Newline framer. Oversized lines are spilled and never held as one value."""

    def __init__(self, max_line_bytes=MAX_LINE_BYTES, spill_path=None):
        self.max = max_line_bytes
        self.spill_path = spill_path
        self.buf = bytearray()
        self.skipping = False
        self._spill = None

    @property
    def pending(self):
        return self.skipping or len(self.buf) > 0

    def close(self):
        if self._spill is not None:
            self._spill.close()
            self._spill = None

    def feed(self, data):
        out = []
        if not data:
            return out
        buf = self.buf
        index = 0
        limit = len(data)
        while index < limit:
            if self.skipping:
                newline = data.find(b'\n', index)
                if newline < 0:
                    self._write(data[index:])
                    break
                self._write(data[index:newline + 1])
                index = newline + 1
                self.skipping = False
                out.append(('oversized', None))
                continue
            newline = data.find(b'\n', index)
            if newline < 0:
                rest = data[index:]
                if len(buf) + len(rest) > self.max:
                    self._write(buf)
                    self._write(rest)
                    buf.clear()
                    self.skipping = True
                else:
                    buf.extend(rest)
                break
            length = len(buf) + (newline - index)
            if length > self.max:
                self._write(buf)
                self._write(data[index:newline + 1])
                buf.clear()
                out.append(('oversized', None))
            else:
                if buf:
                    buf.extend(data[index:newline])
                    line = bytes(buf).decode('utf-8', 'replace')
                    buf.clear()
                else:
                    line = data[index:newline].decode('utf-8', 'replace')
                out.append(('line', line.rstrip('\r')))
            index = newline + 1
        return out

    def _write(self, blob):
        if not blob or not self.spill_path:
            return
        try:
            if self._spill is None:
                self._spill = open(self.spill_path, 'ab')
            self._spill.write(blob)
            self._spill.flush()
        except OSError:
            self.spill_path = None


class Book:
    """Apply frames in order and keep the two clocks: silence and thought-only time."""

    def __init__(self, cli, started_at=None, max_line_bytes=MAX_LINE_BYTES, spill_path=None):
        self.cli = cli
        self.started_at = time.time() if started_at is None else float(started_at)
        self.reader = LineReader(max_line_bytes, spill_path)
        self.pending = []
        self.beats = []
        self.last_kind = None
        self.last_at = None
        self.last_event_at = None
        self.end_at = None
        self.open_items = set()
        self.last_tool_at = None
        self.thought_origin = None
        self.tokens_in = self.tokens_cached = self.tokens_out = None
        self.open_tools = {}
        self.agy_steps = set()
        self.grok_usage = {}
        self.failure_class = None
        self.failure = None
        self.rate_limits = None
        self.rate_limits_at = None
        self.saw_end = False
        self.report_text = ''
        self.report_at = None
        self._grok_anon = 0

    def feed(self, data, at):
        made = []
        for kind, line in self.reader.feed(data):
            if kind == 'oversized':
                made.append(self.note_oversized(at))
            else:
                beat = self.observe_line(line, at)
                if beat:
                    made.append(beat)
        return made

    def replay(self, path, started_at, step=0):
        """Feed a fixture. Each complete line advances the synthetic clock by step."""
        data = Path(path).read_bytes()
        made = []
        at = float(started_at)
        for part in data.splitlines(keepends=True):
            made.extend(self.feed(part, at))
            if part.endswith(b'\n'):
                at += step
        return made

    def observe_line(self, line, at):
        if line is None:
            return None
        text = line.strip()
        if not text:
            return None
        if self.cli == 'agy' and 'streamGenerateContent' in text and not text.startswith('{'):
            return self.note_stream(at)
        try:
            obj = json.loads(text)
        except ValueError:
            if self.cli == 'codex' and re.match(r'(?:\d{4}-\d\d-\d\dT\S+\s+ERROR|Error:|error:|ERROR:)', text):
                return self._remember(_error_beat(text), at)
            return None
        if isinstance(obj, dict):
            self.last_event_at = max(float(at), self.last_event_at or float(at))
        if isinstance(obj, dict):
            payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
            limits = obj.get('rate_limits') or payload.get('rate_limits')
            if limits:
                self.rate_limits = limits
                self.rate_limits_at = float(at)
            typ = obj.get('type')
            if self.cli == 'codex':
                item = obj.get('item') or {}
                key = item.get('id') or item.get('type')
                if typ == 'item.started':
                    self.open_items.add(key)
                elif typ == 'item.completed':
                    self.open_items.discard(key)
                if typ == 'turn.completed':
                    self.end_at = float(at)
                elif typ in ('turn.started', 'item.started', 'item.completed', 'turn.failed', 'error'):
                    self.end_at = None
            elif self.cli == 'cursor':
                if typ == 'result' and obj.get('subtype') == 'success' and not obj.get('is_error'):
                    self.end_at = float(at)
                elif typ in ('tool_call', 'thinking', 'assistant', 'error'):
                    self.end_at = None
            elif self.cli == 'grok':
                if typ == 'end':
                    self.end_at = float(at)
                elif typ in ('tool_call', 'tool_call_update', 'thought', 'text', 'error'):
                    self.end_at = None
        # Agy transcripts have no verified end-of-turn frame; use the long silence.
        if self.cli == 'agy' and isinstance(obj, dict) and 'step_index' in obj:
            step = obj.get('step_index')
            if step in self.agy_steps:
                return None
            self.agy_steps.add(step)
        info = classify(self.cli, obj)
        if not info:
            return None
        if info.get('token_mode') == 'grok' and not info.get('grok_key'):
            info['grok_key'] = 'm%d' % self._grok_anon
            self._grok_anon += 1
        return self._remember(info, at)

    def note_oversized(self, at):
        return self._remember(_beat('tool', 'oversized', progress=True), at)

    def note_stream(self, at, detail='streamGenerateContent'):
        return self._remember(_beat('stream', detail), at)

    def _remember(self, info, at):
        self._apply_tokens(info)
        beat = dict(kind=info['kind'], at=float(at), detail=info.get('detail') or info['kind'],
                    progress=bool(info.get('progress')), failure_class=info.get('failure_class'),
                    file_change=bool(info.get('file_change')), tool_id=info.get('tool_id'),
                    tool_state=info.get('tool_state'))
        if self.tokens_in is not None or self.tokens_out is not None or self.tokens_cached is not None:
            beat['tokens'] = (self.tokens_in, self.tokens_cached, self.tokens_out)
        if info.get('failure'):
            self.failure = info['failure']
            beat.update(failure=info['failure'], category=info['category'], source=info['source'])
        self.beats.append(beat)
        self.pending.append(beat)
        self.last_kind = beat['kind']
        self.last_at = beat['at']
        if beat.get('failure_class'):
            self.failure_class = beat['failure_class']
        if beat['kind'] == 'text' and info.get('message'):
            self.report_text = (self.report_text + info['message']) if self.cli in ('grok', 'cursor') else info['message']
            self.report_at = float(at)
        if beat['progress']:
            self.report_text = ''
            self.report_at = None
            self.last_tool_at = beat['at']
            self.thought_origin = None
        elif beat['kind'] in NON_PROGRESS and self.thought_origin is None:
            self.thought_origin = self.last_tool_at if self.last_tool_at is not None else self.started_at
        if beat.get('tool_state') == 'open' and beat.get('tool_id'):
            self.open_tools[beat['tool_id']] = beat['at']
        elif beat.get('tool_state') == 'close' and beat.get('tool_id'):
            self.open_tools.pop(beat['tool_id'], None)
        if beat['kind'] == 'end':
            self.saw_end = True
        return beat

    def _apply_tokens(self, info):
        triple = info.get('token_values')
        mode = info.get('token_mode')
        if not triple or not mode:
            return
        if mode == 'grok':
            self.grok_usage[info.get('grok_key') or 'm'] = triple
            self.tokens_in = sum(item[0] for item in self.grok_usage.values())
            self.tokens_cached = sum(item[1] for item in self.grok_usage.values())
            self.tokens_out = sum(item[2] for item in self.grok_usage.values())
        elif mode == 'add':
            self.tokens_in = (self.tokens_in or 0) + triple[0]
            self.tokens_cached = (self.tokens_cached or 0) + triple[1]
            self.tokens_out = (self.tokens_out or 0) + triple[2]
        elif mode == 'set':
            self.tokens_in, self.tokens_cached, self.tokens_out = triple

    def silence_secs(self, now):
        if self.last_at is None:
            return max(0.0, now - self.started_at)
        return max(0.0, now - self.last_at)

    def thought_only_secs(self, now):
        """Time since the last tool, while heartbeats stayed off the tool clock."""
        if self.last_kind not in NON_PROGRESS or self.thought_origin is None:
            return 0.0
        return max(0.0, now - self.thought_origin)

    def assess(self, now, silence_secs=SILENCE_SECS, think_secs=THINK_WITHOUT_TOOL_SECS, tool_secs=TOOL_SECS):
        silence = self.silence_secs(now)
        think = self.thought_only_secs(now)
        healthy = bool(self.open_tools and self.last_tool_at is not None and (now - self.last_tool_at) < tool_secs)
        failure = None
        if not healthy:
            if self.last_kind == 'error' and self.failure_class in ('auth', 'broken_stream') and silence < silence_secs:
                failure = self.failure_class
            elif think >= think_secs and silence < silence_secs:
                failure = 'stall_thinking'
            elif silence >= silence_secs:
                failure = 'stall_silent'
        return dict(failure_class=failure, silence_secs=silence, thought_only_secs=think,
                    open_tools=len(self.open_tools), last_tool_at=self.last_tool_at, last_kind=self.last_kind,
                    progress=self.last_tool_at is not None, saw_end=self.saw_end)


def persist(root, segment_id, beats):
    """Write heartbeat columns. False when the segment does not exist yet."""
    import godfather_store as store
    return store.record_heartbeats(root, segment_id, beats)
