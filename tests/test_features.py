"""Ledger, review, dashboard, monitoring and MCP regressions; all workers are fake."""
import datetime as dt
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import test_router as fixtures
import agent_oversight as a
import godfather_router as router
import godfather_safety as safety
import godfather_usage as usage
import godfather_mcp as mcp
import godfather_top as top

ROOT = fixtures.ROOT
FEATURE = r'''
import time
is_review = 'Review this completed task independently.' in prompt or cli == 'grok' and '--resume' in args
reply = '- [high] changed.txt:1 - clear bug' if is_review else 'OK'
if os.getenv('WORK_FILE'):
    target = Path(os.environ['WORK_FILE'])
    if not is_review:
        target.write_text('changed by worker\n')
    elif 'Fix only clear bugs.' in prompt:
        target.write_text('fixed by reviewer\n')
if cli == 'codex':
    print(json.dumps(dict(type='turn.completed', usage=dict(input_tokens=100, cached_input_tokens=40, output_tokens=10))))
elif cli == 'agy':
    transcript = Path.home() / '.gemini/antigravity-cli/brain/44444444-4444-4444-4444-444444444444/.system_generated/logs/transcript.jsonl'
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with transcript.open('a') as out:
        out.write(json.dumps(dict(step_index=time.time_ns(), type='PLANNER_RESPONSE', content=reply,
                                 input_tokens=100, cached_input_tokens=40, output_tokens=10)) + '\n')
    if os.getenv('MONITOR_MODE'):
        while True:
            with log.open('a') as out:
                out.write(('streamGenerateContent request active' if os.getenv('MONITOR_MODE') == 'slow' else 'refreshing cache in background') + '\n')
            print('routine diagnostics', file=sys.stderr, flush=True)
            time.sleep(.08)
if is_review and os.getenv('REVIEW_ASK') and '--resume' not in args:
    subprocess = __import__('subprocess')
    subprocess.run(['godfather', 'ask', 'Review decision?'], check=True)
'''
FAKE = fixtures.FAKE.replace('if quota:\n', FEATURE + '\nif quota:\n', 1)
FAKE = FAKE.replace(".write_text('OK\\n')", ".write_text(reply + '\\n')")
FAKE = FAKE.replace("dict(type='text', data='OK')", "dict(type='text', data=reply)")
FAKE = FAKE.replace("dict(type='end')", "dict(type='end', usage=dict(input_tokens=100, cached_input_tokens=40, output_tokens=10))")


class FeatureTests(unittest.TestCase):
    setUp = fixtures.RouterTests.setUp
    config_models = fixtures.RouterTests.config_models
    calls = fixtures.RouterTests.calls
    launch = fixtures.RouterTests.launch

    def command(self, *args, **env):
        return subprocess.run([str(ROOT / 'bin/godfather')] + list(args), env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=30)

    def fake_features(self):
        for cli in ('agy', 'codex', 'grok'):
            (self.root / 'fake' / cli).write_text(FAKE)

    def ledger(self):
        return [json.loads(line) for line in (self.root / 'state/godfather/ledger.jsonl').read_text().splitlines()]

    def git_workspace(self):
        work = self.root / 'project'
        work.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(work)] + list(args), check=True, capture_output=True, text=True)
        git('init', '-q')
        (work / 'changed.txt').write_text('base\n')
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        return work

    def test_ledger_all_clis_pricing_grouping_and_snapshot(self):
        self.fake_features()
        for cli, model in [('codex', 'test-codex'), ('grok', 'test-grok'), ('agy', 'gemini-test')]:
            self.config_models((cli, model))
            with self.config.open('a') as f:
                f.write('input_per_million=2\ncached_per_million=0.5\noutput_per_million=8\n')
            result = self.launch(cli)
            self.assertEqual(0, result.returncode, result.stderr)
        records = self.ledger()
        self.assertEqual(3, len(records))
        for record in records:
            self.assertEqual((100, 40, 10), tuple(record[k] for k in ('tokens_in', 'tokens_cached', 'tokens_out')))
            self.assertAlmostEqual(.00022, record['estimated_usd'])
            self.assertEqual(0, record['exit_status'])
            self.assertGreaterEqual(a.epoch(record['ended_at']), a.epoch(record['started_at']))
            self.assertFalse(record['quota_fallback'])
        result = self.command('usage', '--today', '--by', 'cli', '--json')
        data = json.loads(result.stdout)
        self.assertEqual({'agy', 'codex', 'grok'}, set(data['groups']))
        self.assertIn('Weekly Limit Remaining', data['agy_usage']['text'])
        self.assertIsInstance(data['agy_usage']['at'], (int, float))
        self.assertLess(data['agy_usage']['age_seconds'], usage.SNAPSHOT_MAX_AGE)
        self.assertFalse(data['agy_usage']['refreshed'])
        week_out = self.command('usage', '--week').stdout
        self.assertIn('$0.000660', week_out)
        self.assertIn('(live):', week_out)
        self.assertEqual({'agy', 'codex', 'grok'}, set(json.loads(self.command('usage', '--by', 'run', '--json').stdout)['groups']))

    def test_usage_refreshes_stale_snapshot_and_labels_failed_refresh_stale(self):
        snap_path = self.root / 'state/godfather/agy-usage.json'
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        stale_at = time.time() - 3 * 86400
        a.write(snap_path, dict(at=stale_at, text='Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t12%\n'))
        refreshed = self.command('usage', '--week', ARGON_PCT='88')
        self.assertEqual(0, refreshed.returncode, refreshed.stderr)
        self.assertIn('No usage recorded for this period.', refreshed.stdout)
        self.assertIn('Latest agy /usage snapshot at ', refreshed.stdout)
        self.assertIn('(live):', refreshed.stdout)
        self.assertIn('88%', refreshed.stdout)
        a.write(snap_path, dict(at=stale_at, text='Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t12%\n'))
        refreshed_json = json.loads(self.command('usage', '--json', ARGON_PCT='77').stdout)['agy_usage']
        self.assertTrue(refreshed_json['refreshed'])
        self.assertGreater(refreshed_json['at'], stale_at)
        self.assertLess(refreshed_json['age_seconds'], usage.SNAPSHOT_MAX_AGE)
        self.assertIn('77%', refreshed_json['text'])
        a.write(snap_path, dict(at=stale_at, text='Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t12%\n'))
        with mock.patch.dict(os.environ, dict(self.env, LOGGED_OUT='agy'), clear=True), mock.patch.object(router.time, 'sleep'):
            failed_data = usage.summary('week')
        self.assertFalse(failed_data['agy_usage']['refreshed'])
        self.assertEqual(stale_at, failed_data['agy_usage']['at'])
        self.assertGreaterEqual(failed_data['agy_usage']['age_seconds'], 3 * 86400)
        a.write(snap_path, dict(at=stale_at, text='Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t12%\n'))
        (self.root / 'fake/agy').unlink()
        stale_out = self.command('usage', '--week')
        self.assertEqual(0, stale_out.returncode, stale_out.stderr)
        self.assertIn('Stale agy /usage snapshot at ', stale_out.stdout)
        self.assertIn('3 days old', stale_out.stdout)
        self.assertIn('12%', stale_out.stdout)
        self.assertNotIn('Latest agy /usage snapshot', stale_out.stdout)

    def test_quota_segment_tokens_are_not_counted_twice(self):
        self.fake_features()
        self.config_models(('agy', 'gemini-test'), ('agy', 'claude-test'))
        result = self.launch(QUOTA_MODELS='gemini-test')
        self.assertEqual(0, result.returncode, result.stderr)
        records = self.ledger()
        self.assertEqual(2, len(records))
        self.assertEqual([100, 100], [r['tokens_in'] for r in records])
        self.assertEqual([False, True], [r['quota_fallback'] for r in records])
        self.assertEqual([True, False], [r['quota_exhausted'] for r in records])
        self.assertEqual(1, len(set(r['run_id'] for r in records)))
        self.assertEqual(2, len(set(r['segment_id'] for r in records)))

    def test_auth_failure_routes_to_next_cli_without_quota_cooldown(self):
        self.config_models(('agy', 'gemini-test'), ('agy', 'claude-test'), ('codex', 'test-codex'))
        result = self.launch(TASK_AUTH_FAILURE='agy')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(['agy', 'agy', 'codex'], [c['cli'] for c in self.calls()])
        self.assertTrue(all(not r['quota_fallback'] for r in self.ledger()))
        state = json.loads((self.root / 'runs/task.route.json').read_text())
        self.assertEqual(1, len(state['switches']))
        self.assertIn('authentication failed', state['switches'][0]['reason'])

    def test_review_auto_after_check_with_task_and_diff(self):
        self.fake_features()
        self.config_models(('codex', 'work'), ('codex', 'same-cli'), ('grok', 'review'))
        work = self.git_workspace()
        result = self.command('run', '--no-isolate', '-p', 'Original task', '--check', 'test -f changed.txt', '--review-by', 'auto', 'task',
                              CWD=str(work), WORK_FILE=str(work / 'changed.txt'))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertLess(result.stdout.index('CHECK task pass'), result.stdout.index('REVIEW task round 1: 1 findings (high)'))
        self.assertEqual(['codex', 'grok'], [c['cli'] for c in self.calls()])
        prompt = self.calls()[1]['prompt']
        for value in ('Original task', '+changed by worker', 'changed.txt', 'Do not edit any files'):
            self.assertIn(value, prompt)
        self.assertIn('[high] changed.txt:1', (self.root / 'runs/task.review.md').read_text())
        self.assertEqual('changed by worker\n', (work / 'changed.txt').read_text())
        self.assertEqual(2, len(self.ledger()))

    def test_explicit_review_fix_rechecks_and_unavailable_fails(self):
        self.fake_features()
        self.config_models(('codex', 'work'), ('codex', 'review'))
        work = self.git_workspace()
        result = self.command('run', '--no-isolate', '-p', 'Task', '--review-by', 'review', '--review-fix', '--check', 'test -f changed.txt', 'task',
                              CWD=str(work), WORK_FILE=str(work / 'changed.txt'))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('fixed by reviewer\n', (work / 'changed.txt').read_text())
        self.assertIn('CHECK task.review pass', result.stdout)
        self.assertEqual(['work', 'review'], [c['model'] for c in self.calls()])
        result = self.command('run', '--no-isolate', '-p', 'Task', '--review-by', 'auto', 'unavailable')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('review_failed', a.read(self.root / 'runs/unavailable.run.json')['status'])

    def test_failed_check_prevents_review(self):
        self.config_models(('codex', 'work'), ('grok', 'review'))
        result = self.command('run', '--no-isolate', '-p', 'Task', '--review-by', 'auto', '--check', 'exit 1', '--max-fix-rounds', '0', 'task')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(1, len(self.calls()))
        self.assertFalse((self.root / 'runs/task.review.md').exists())

    def test_review_question_resume_finishes_original_report(self):
        self.fake_features()
        self.config_models(('codex', 'work'), ('grok', 'review'))
        result = self.command('run', '--no-isolate', '-p', 'Task', '--review-by', 'auto', 'task', REVIEW_ASK='1')
        self.assertEqual(0, result.returncode, result.stderr)
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual('needs_input', mcp.report('task')['status'])
        result = self.command('answer', 'task.review', 'continue')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('REVIEW task round 1: 1 findings (high)', result.stdout)
        self.assertEqual('done', a.read(self.root / 'runs/task.run.json')['status'])
        self.assertIn('[high]', (self.root / 'runs/task.review.md').read_text())

    def test_plain_top_and_pending_question(self):
        self.config_models(('codex', 'work'))
        self.assertEqual(0, self.launch().returncode)
        result = self.command('top', '--once')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('TOKENS(in/cached/out)', result.stdout)
        self.assertIn('START task', result.stdout)
        self.assertIn('DONE task', result.stdout)
        row = dict(name='task', model='work', cli='codex', status='running', elapsed='1m00s', seconds_since_progress=4,
                   questions=[dict(question='Approve?', answer=None)])
        text, pending = top.row_text(row)
        self.assertTrue(pending)
        self.assertIn('NEEDS_INPUT', text)
        self.assertIn('Approve?', text)

    def monitor(self, mode):
        self.fake_features()
        self.config_models(('agy', 'gemini-test'))
        child = subprocess.Popen([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '-p', 'Task', 'task'], env=dict(self.env, MONITOR_MODE=mode),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = a.read(self.root / 'runs/task.run.json')
            if state.get('pid') and state.get('progress_paths'):
                break
            time.sleep(.03)
        watcher = subprocess.Popen([str(ROOT / 'bin/agents-watch'), '--no-heal', '--stall-secs', '.3', '--hard-stall-secs', '3', '--poll-secs', '.03'],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(watcher.stdout, selectors.EVENT_READ)
                output = b''
                deadline = time.monotonic() + 8
                while b'STALL task ' not in output and time.monotonic() < deadline:
                    if selector.select(.1):
                        output += os.read(watcher.stdout.fileno(), 65536)
                self.assertIn(b'STALL task ', output)
                state = a.read(self.root / 'runs/task.run.json')
                self.assertTrue(state['router_name'])
                procs = a.processes()
                route = a.read(self.root / 'runs/task.route.json')
                self.assertEqual(route['router_pid'], procs[state['launcher_pid']]['ppid'])
                # The durable router's owner is orphaned, rather than a
                # descendant of this expendable attachment client.
                ancestor = route['router_pid']
                seen = set()
                while ancestor in procs and ancestor not in seen:
                    seen.add(ancestor)
                    ancestor = procs[ancestor]['ppid']
                self.assertNotIn(child.pid, seen)
                self.assertTrue(all('agy-run.' not in p for p in state['progress_paths']))
                if mode == 'slow':
                    self.assertIn(b'SLOW task ', output)
                    self.assertLess(output.index(b'SLOW task '), output.index(b'STALL task '))
                else:
                    self.assertNotIn(b'SLOW task ', output)
        finally:
            # Closing the attachment intentionally leaves durable work alive.
            self.command('stop', 'task')
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=12)
            watcher.terminate()
            watcher.communicate(timeout=5)

    def test_routed_agy_stall_ignores_continual_diagnostic_output(self):
        self.monitor('stall')

    def test_active_model_stream_slow_then_hard_stall(self):
        self.monitor('slow')

    def test_cancel_stops_acceptance_check_process_group(self):
        self.config_models(('codex', 'work'))
        child = subprocess.Popen([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '-p', 'Task', '--check',
            'python3 -c "import time; time.sleep(30)"', 'task'], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if a.read(self.root / 'runs/task.run.json').get('status') == 'checking':
                    break
                time.sleep(.03)
            # checking can be published by the launcher just before verify starts.
            time.sleep(.15)
            with mock.patch.dict(os.environ, self.env, clear=True):
                self.assertEqual('cancelling', mcp.cancel_run('task')['status'])
            output, error = child.communicate(timeout=10)
            self.assertEqual(143, child.returncode, error)
            self.assertIn(b'CHECK task cancelled', output)
            self.assertEqual('killed', a.read(self.root / 'runs/task.run.json')['status'])
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=12)

    def test_usage_probe_retries_with_backoff(self):
        good = subprocess.CompletedProcess([], 0, 'quota snapshot', '')
        bad = subprocess.CompletedProcess([], 1, '', 'auth timed out')
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(router, 'query', side_effect=[bad, bad, good]) as query, mock.patch.object(router.time, 'sleep') as sleep:
            self.assertEqual('quota snapshot', router.agy_usage('fake'))
            self.assertEqual(3, query.call_count)
            self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)

    def test_scripted_json_rpc_session(self):
        self.fake_features()
        self.config_models(('codex', 'work'), ('grok', 'review'))
        env = dict(self.env)
        if os.getenv('TEST_MCP_PYTHON'):
            env['GODFATHER_MCP_PYTHON'] = os.environ['TEST_MCP_PYTHON']
        server = subprocess.Popen([str(ROOT / 'bin/godfather'), 'mcp'], env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        count = 0
        def request(method, params=None):
            nonlocal count
            count += 1
            server.stdin.write(json.dumps(dict(jsonrpc='2.0', id=count, method=method, params=params or {})) + '\n')
            server.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(server.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(20), 'MCP response timeout')
            response = json.loads(server.stdout.readline())
            self.assertEqual(count, response['id'])
            self.assertNotIn('error', response, response)
            return response['result']
        def call(tool_name, **arguments):
            result = request('tools/call', dict(name=tool_name, arguments=arguments))
            self.assertFalse(result.get('isError'), result)
            return json.loads(result['content'][0]['text'])
        try:
            init = request('initialize', dict(protocolVersion='2025-06-18', capabilities={}, clientInfo=dict(name='test', version='1')))
            self.assertEqual('godfather', init['serverInfo']['name'])
            server.stdin.write(json.dumps(dict(jsonrpc='2.0', method='notifications/initialized')) + '\n')
            server.stdin.flush()
            tools = request('tools/list')['tools']
            self.assertEqual({'run', 'status', 'events', 'wait', 'answer', 'nudge', 'cancel', 'quota', 'usage'}, {t['name'] for t in tools})
            self.assertFalse(any(r['status'] != 'unregistered' for r in call('status')))
            started = call('run', prompt='Reply OK', name='rpc', cwd=str(self.root), isolate=False, check='exit 0', review_by='auto')
            self.assertTrue(started['run_id'])
            result = call('wait', name='rpc', timeout_s=15)
            self.assertEqual('done', result['status'], result)
            self.assertEqual('OK', result['report'].strip())
            self.assertIn('[high]', result['review'])
            self.assertEqual(started['run_id'], a.read(self.root / 'runs/rpc.route.json')['run_id'])
            polled = call('events')
            self.assertTrue(any(e['kind'] == 'REVIEW' for e in polled['events']))
            self.assertEqual([], call('events', since=polled['cursor'])['events'])
            self.assertEqual({}, call('quota'))
            self.assertEqual(2, call('usage', period='today')['groups']['total']['segments'])
            self.assertEqual('done', call('cancel', name='rpc')['status'])
            result = request('tools/call', dict(name='run', arguments=dict(prompt='x', name='../invalid', cwd=str(self.root))))
            self.assertTrue(result['isError'])
        finally:
            server.stdin.close()
            server.wait(timeout=5)
            self.assertEqual('', server.stderr.read())
            server.stdout.close()
            server.stderr.close()

    def test_top_keyboard_controls_confirm_kill_and_dispatch(self):
        screen = mock.MagicMock()
        screen.getmaxyx.return_value = (24, 180)
        screen.getch.side_effect = [ord('k'), ord('k'), ord('a'), ord('n'), ord('l'), ord('q')]
        screen.getstr.side_effect = [b'no', b'yes', b'answer text', b'nudge text']
        curses = mock.MagicMock()
        curses.A_BOLD, curses.A_REVERSE, curses.KEY_DOWN, curses.KEY_UP = 1, 2, 258, 259
        curses.error = RuntimeError
        row = dict(name='task', model='work', cli='codex', status='needs_input', elapsed='1m00s',
                   seconds_since_progress=4, questions=[dict(question='Approve?', answer=None)])
        with mock.patch.dict(sys.modules, curses=curses), mock.patch.object(a, 'snapshot', return_value=[row]), \
                mock.patch.object(top, 'recent_events', return_value=[]), mock.patch.object(top, 'cancel_run', return_value={}) as cancel, \
                mock.patch.object(top, 'background') as background, mock.patch.object(top, 'named_base', return_value=self.root / 'runs/task'), \
                mock.patch.object(top.subprocess, 'run') as pager:
            self.assertEqual(0, top.interactive(screen))
            cancel.assert_called_once_with('task')
            self.assertEqual([['answer', 'task', 'answer text'], ['nudge', 'task', 'nudge text']],
                             [c.args[0] for c in background.call_args_list])
            self.assertEqual(str(self.root / 'runs/task.log'), pager.call_args.args[0][-1])


class UsageUnitTests(unittest.TestCase):
    def test_unknown_prices_partial_usage_and_cached_pricing(self):
        record = dict(tokens_in=100, tokens_cached=None, tokens_out=10)
        self.assertIsNone(usage.estimate(record, {}))
        self.assertIsNone(usage.estimate(record, dict(input_per_million=2, cached_per_million=1, output_per_million=8)))
        self.assertAlmostEqual(.00028, usage.estimate(record, dict(input_per_million=2, output_per_million=8)))
        self.assertIsNone(usage.estimate(dict(tokens_in=None, tokens_out=10), dict(input_per_million=2, output_per_million=8)))

    def test_period_boundaries_and_group_totals(self):
        with __import__('tempfile').TemporaryDirectory() as temp, mock.patch.dict(os.environ, XDG_STATE_HOME=temp):
            root = usage.state_dir()
            root.mkdir()
            today = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            week = today - dt.timedelta(days=today.weekday())
            records = [dict(started_at=stamp, model='m', cli='codex', run='r', tokens_in=10, tokens_cached=None,
                            tokens_out=2, estimated_usd=None) for stamp in
                       (week.timestamp() - 1, today.timestamp(), time.time())]
            (root / 'ledger.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records) + '{incomplete')
            self.assertEqual(3, usage.summary()['groups']['total']['segments'])
            for period in ('today', 'week'):
                row = usage.summary(period, 'model')['groups']['m']
                self.assertEqual(2, row['segments'])
                self.assertEqual(20, row['tokens_in'])
                self.assertIsNone(row['tokens_cached'])
                self.assertIsNone(row['estimated_usd'])

    def test_price_validation(self):
        base = '[[models]]\nbackend="codex"\nmodel="x"\neffort="high"\n'
        for bad in ('input_per_million=-1', 'output_per_million=true', 'cached_per_million=NaN'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                router.parse_models(base + bad)
