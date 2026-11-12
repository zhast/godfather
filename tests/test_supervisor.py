"""Declarative retry policy, stall clocks, and headless quota preflight."""
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import godfather_policy as policy
import godfather_router as router
import godfather_store as store
import godfather_stream as stream
import godfather_supervisor as sup

FIXTURE = ROOT / 'tests/fixtures/inflight-sleep/stream.jsonl'
STARTED = 1_000_000.0


def _snap(**extra):
    base = dict(started_at=STARTED, run_started_at=STARTED, last_heartbeat_at=STARTED,
                last_heartbeat_kind='thought', last_tool_at=None, open_tools=0, tool_started_at=None,
                thought_only_secs=0, failure_class=None, declared_wait_secs=None, children_cpu=False,
                confidence='medium', same_cli='grok', wait_process=False)
    base.update(extra)
    return base


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.env = dict(os.environ, HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / 'config'),
                        XDG_STATE_HOME=str(self.root / 'state'), AGENT_RUNS_DIR=str(self.runs),
                        PYTHONDONTWRITEBYTECODE='1')
        sup._CACHE.clear()
        sup._PS.clear()
        self.addCleanup(sup._CACHE.clear)
        self.addCleanup(sup._PS.clear)

    def _isolated(self):
        return mock.patch.dict(os.environ, self.env, clear=False)

    def test_pending_question_pauses_clocks_and_blocks_healing(self):
        import agent_oversight as a
        import godfather_lifecycle as life
        import godfather_safety as safety
        book = stream.Book('grok', started_at=STARTED)
        book.note_stream(STARTED)
        progress = mock.Mock(book=book)
        run = dict(name='polish_final', status='running', started_at=STARTED)
        with self._isolated():
            safety.atomic(self.runs / 'polish_final.control.json', {'questions': [{'answer': None}]})
            self.assertTrue(sup.pause_for_decision(progress, run, STARTED + 9000))
            self.assertEqual('pending question', sup.refuse_signal(run))
            self.assertFalse(life.request_heal(run, 'stall', 'idle'))
            safety.atomic(self.runs / 'polish_final.control.json', {'questions': [{'answer': 'continue'}]})
            self.assertFalse(sup.pause_for_decision(progress, run, STARTED + 9001))
            self.assertEqual((None, None), sup.classify(sup.snapshot_from_book(book, STARTED + 9001), sup.load_policies(''), STARTED + 9001))
        decision = sup.evaluate(_snap(pending_decision=True), sup.load_policies(''), STARTED + 9000)
        self.assertIsNone(decision['action'])

    def test_real_codex_final_report_requires_turn_end_or_long_silence(self):
        frames = [dict(type='thread.started', thread_id='t'), dict(type='turn.started'),
                  dict(type='item.started', item=dict(id='c', type='command_execution', command='git commit -m done')),
                  dict(type='item.completed', item=dict(id='c', type='command_execution', exit_code=0)),
                  dict(type='item.completed', item=dict(id='m', type='agent_message', text='Committed and tested.'))]
        with self._isolated(), mock.patch.object(sup, 'git_head', return_value='new-commit'):
            run = dict(name='idle', cli='codex', cwd=str(self.root))
            book = stream.Book('codex', started_at=STARTED)
            for offset, frame in enumerate(frames):
                book.observe_line(json.dumps(frame), STARTED + offset)
            progress = mock.Mock(book=book)
            for age in (35, 120, 603):
                self.assertIsNone(sup.finished_report(run, progress, self.runs / 'idle', 'old', STARTED + age))
            self.assertEqual('new-commit', sup.finished_report(run, progress, self.runs / 'idle', 'old', STARTED + 604)['commit'])
            book.observe_line('{"type":"turn.completed","usage":{"input_tokens":20,"output_tokens":5}}', STARTED + 125)
            self.assertIsNone(sup.finished_report(run, progress, self.runs / 'idle', 'old', STARTED + 154))
            self.assertEqual('new-commit', sup.finished_report(run, progress, self.runs / 'idle', 'old', STARTED + 155)['commit'])
            # A later turn must not inherit the previous end signal.
            book.observe_line('{"type":"turn.started"}', STARTED + 156)
            self.assertIsNone(sup.finished_report(run, progress, self.runs / 'idle', 'old', STARTED + 190))

    def test_open_codex_items_and_grok_tools_block_idle_completion(self):
        for cli, opening, closing, report, end in (
                ('codex', dict(type='item.started', item=dict(id='r', type='reasoning')),
                 dict(type='item.completed', item=dict(id='r', type='reasoning')),
                 dict(type='item.completed', item=dict(id='m', type='agent_message', text='Done.')),
                 dict(type='turn.completed')),
                ('grok', dict(type='tool_call', toolCallId='c', toolName='shell'),
                 dict(type='tool_call_update', toolCallId='c', toolName='shell', status='completed'),
                 dict(type='text', data='Done.'), dict(type='end'))):
            with self.subTest(cli=cli), self._isolated(), mock.patch.object(sup, 'git_head', return_value='new'):
                run = dict(name='idle', cli=cli, cwd=str(self.root))
                book = stream.Book(cli, started_at=STARTED)
                for offset, frame in enumerate((opening, report, end)):
                    book.observe_line(json.dumps(frame), STARTED + offset)
                progress = mock.Mock(book=book)
                self.assertIsNone(sup.finished_report(run, progress, self.runs / 'idle', 'old', STARTED + 900))
                for offset, frame in enumerate((closing, report, end)):
                    book.observe_line(json.dumps(frame), STARTED + 901 + offset)
                self.assertEqual('new', sup.finished_report(run, progress, self.runs / 'idle', 'old', STARTED + 933)['commit'])

    def test_grok_default_model_is_fast(self):
        self.assertEqual(('grok', 'grok-4.7-build-fast', 'high'), router.DEFAULTS[3])

    def test_retry_table_replaces_healing_heuristics(self):
        healing = sup.load_policies('[healing]\nenabled = true\nstall_secs = 1\n')
        self.assertFalse(healing['active'])
        self.assertEqual(120, healing['heartbeat']['silence_secs'])
        rules = sup.load_policies(
            '[heartbeat]\nsilence_secs = 15\nstart_to_first_tool_secs = 40\n'
            '[[retry]]\nclass = "stall_silent"\nattempts = 3\nbackoff_secs = 0\naction = "nudge_status"\n')
        self.assertTrue(rules['active'])
        self.assertEqual(15, rules['heartbeat']['silence_secs'])
        self.assertEqual(40, rules['heartbeat']['start_to_first_tool_secs'])
        row = next(item for item in rules['retry'] if item['class_'] == 'stall_silent')
        self.assertEqual(3, row['attempts'])
        self.assertEqual(10, len(rules['retry']))
        with self.assertRaises(ValueError):
            sup.load_policies('[heartbeat]\nnope_secs = 1\n')

    def test_policy_loads_when_tomllib_is_missing(self):
        text = ('[heartbeat]\nsilence_secs = 9\n[[retry]]\nclass = "auth"\nattempts = 2\n'
                'backoff_secs = 1\naction = "resume"\n')
        with mock.patch.dict(sys.modules, {'tomllib': None}):
            rules = sup.load_policies(text)
        self.assertTrue(rules['active'])
        self.assertEqual(9, rules['heartbeat']['silence_secs'])
        self.assertEqual(2, next(item['attempts'] for item in rules['retry'] if item['class_'] == 'auth'))

    def test_auth_budget_follows_retry_and_ignores_healing(self):
        path = self.root / 'config/godfather/godfather.toml'
        path.parent.mkdir(parents=True)
        path.write_text('[healing]\nenabled = false\nauth_retries = 5\nauth_retry_secs = 9\n'
                        '[[retry]]\nclass = "auth"\nattempts = 2\nbackoff_secs = 0.5\naction = "resume"\n')
        with self._isolated():
            sup._CACHE.clear()
            self.assertEqual((2, 0.5), sup.auth_retry_budget())
            path.write_text('[healing]\nenabled = false\n')
            sup._CACHE.clear()
            self.assertEqual((0, 0.0), sup.auth_retry_budget())

    def test_declared_sleep_is_not_a_stall_until_the_tool_budget(self):
        rules = sup.load_policies('')
        book = stream.Book('grok', started_at=STARTED)
        book.replay(FIXTURE, STARTED)
        self.assertEqual(180, sup.declared_wait(book.beats[-1]['detail']))
        for age in (120, 150):
            failure, root = sup.classify(sup.snapshot_from_book(book, STARTED + age), rules, STARTED + age)
            self.assertIsNone(failure, age)
            self.assertIsNone(root, age)
        late = STARTED + 601
        failure, root = sup.classify(sup.snapshot_from_book(book, late), rules, late)
        self.assertEqual(('stall_silent', 'tool_timeout'), (failure, root))
        busy = sup.snapshot_from_book(book, late, children_cpu=True)
        self.assertEqual((None, None), sup.classify(busy, rules, late))
        short = sup.load_policies('')
        short['heartbeat']['tool_secs'] = 120
        self.assertIsNone(sup.classify(sup.snapshot_from_book(book, STARTED + 150), short, STARTED + 150)[0])
        self.assertEqual('stall_silent', sup.classify(sup.snapshot_from_book(book, STARTED + 181), short, STARTED + 181)[0])

    def test_cpu_children_and_declared_wait_block_signals(self):
        cpu = [dict(pid=20, ppid=10, pcpu=4.0, state='S', command='python3 poll_graders.py')]
        waiting = [dict(pid=21, ppid=10, pcpu=0.0, state='S', command='sleep 180')]
        with mock.patch.object(sup, '_ps_rows', return_value=cpu):
            self.assertEqual('cpu-active children', sup.descendant_block(10))
            self.assertTrue(sup.inflight_protected(mock.Mock(book=None), {'pid': 10}))
            self.assertEqual('cpu-active children', sup.refuse_signal({'pid': 10}))
        with mock.patch.object(sup, '_ps_rows', return_value=waiting):
            self.assertEqual('declared wait', sup.descendant_block(10))
        with mock.patch.object(sup, '_ps_rows', return_value=[]):
            self.assertIsNone(sup.descendant_block(10))

    def test_clocks_retries_and_the_kill_bar(self):
        rules = sup.load_policies('')
        silent, root = sup.classify(_snap(last_heartbeat_at=None, last_heartbeat_kind=None), rules, STARTED + 120)
        self.assertEqual(('stall_silent', 'stall_silent'), (silent, root))
        thinking, root = sup.classify(_snap(thought_only_secs=180, last_heartbeat_at=STARTED + 179), rules, STARTED + 180)
        self.assertEqual(('stall_thinking', 'start_to_first_tool'), (thinking, root))
        auth, _ = sup.classify(_snap(failure_class='auth', open_tools=1, tool_started_at=STARTED), rules, STARTED + 10)
        self.assertEqual('auth', auth)
        first = sup.evaluate(_snap(thought_only_secs=200, confidence='high'), rules, STARTED + 20, {})
        self.assertEqual(('nudge_tool', 'start_to_first_tool', False), (first['action'], first['root_cause'], first['kill']))
        again = sup.evaluate(_snap(thought_only_secs=200, confidence='high'), rules, STARTED + 20, {'stall_thinking': 1})
        self.assertEqual(('handoff', True), (again['action'], again['kill']))
        live = sup.evaluate(_snap(thought_only_secs=200, confidence='medium'), rules, STARTED + 20, {'stall_thinking': 1})
        self.assertEqual(('escalate', False), (live['action'], live['kill']))
        crash = sup.evaluate(_snap(failure_class='crash', confidence='low'), rules, STARTED + 5, {})
        self.assertEqual(('escalate', False), (crash['action'], crash['kill']))
        dead = sup.evaluate(_snap(failure_class='crash', confidence='high'), rules, STARTED + 5, {})
        self.assertEqual(('restart', True), (dead['action'], dead['kill']))
        timed = sup.evaluate(_snap(), rules, STARTED + 3600, {})
        self.assertEqual(('timed_out', 'run_timeout', 'escalate'), (timed['failure_class'], timed['root_cause'], timed['action']))
        checked = sup.evaluate(_snap(failure_class='check'), rules, STARTED + 5, {'check': 2})
        self.assertEqual('escalate', checked['action'])

    def test_actions_record_one_incident_with_root_cause(self):
        with self._isolated():
            run = dict(run_id='run-1', name='sleeper', cwd=str(self.root), base=self.runs / 'sleeper')
            decision = dict(public='nudge', action='nudge_tool', claim='stall_thinking#1',
                            root_cause='start_to_first_tool', failure_class='stall_thinking')
            self.assertTrue(sup.commit_action(run, decision, self.runs))
            self.assertFalse(sup.commit_action(run, decision, self.runs))
            record = json.loads((policy.state_dir() / 'incidents.jsonl').read_text().splitlines()[-1])
            self.assertEqual('start_to_first_tool', record['root_cause'])
            self.assertEqual('start_to_first_tool', record['effect'])
            self.assertEqual('policy', record['source'])
            self.assertEqual('nudge', record['action'])
            conn = store.connect(self.runs)
            try:
                stored = conn.execute('SELECT effect, source FROM incidents').fetchone()
            finally:
                conn.close()
            self.assertEqual(('start_to_first_tool', 'policy'), (stored['effect'], stored['source']))
            self.assertEqual({'stall_thinking': 1}, sup.stored_attempts(self.runs, 'run-1'))

    def test_schedule_restarts_only_a_dead_process(self):
        path = self.root / 'config/godfather/godfather.toml'
        path.parent.mkdir(parents=True)
        path.write_text('[healing]\nenabled = true\n[[retry]]\nclass = "crash"\nattempts = 1\n'
                        'backoff_secs = 0\naction = "restart"\n')
        book = stream.Book('grok', started_at=time.time() - 5)
        book.failure_class = 'crash'
        book.last_at = book.started_at
        book.last_kind = 'error'
        progress = mock.Mock(book=book)
        run = dict(name='dead', run_id='dead', pid=987654321, cli='grok', cwd=str(self.root))
        with self._isolated():
            sup._CACHE.clear()
            with mock.patch('godfather_lifecycle.request_heal') as heal:
                with mock.patch.object(sup, '_ps_rows', return_value=[
                        dict(pid=50, ppid=987654321, pcpu=20.0, state='R', command='python3 grade.py')]):
                    skipped = sup.schedule(run, progress, {})
                self.assertIsNone(skipped['public'])
                heal.assert_not_called()
                chosen = sup.schedule(run, progress, {})
            self.assertEqual('restart', chosen['action'])
            self.assertTrue(chosen['kill'])
            heal.assert_called_once()
            self.assertEqual('died', heal.call_args[0][1])
            live = dict(run, name='live', run_id='live', pid=424242)
            procs = {424242: dict(ppid=1, stat='Ss', identity='test', command='python')}
            with mock.patch.object(sup, '_ps_rows', return_value=[]):
                with mock.patch('godfather_lifecycle.request_heal') as heal:
                    held = sup.schedule(live, progress, procs)
            self.assertEqual('escalate', held['action'])
            self.assertFalse(held['kill'])
            heal.assert_not_called()

    def test_effort_rows_resolve_specific_before_wildcards(self):
        path = self.root / 'config/godfather/godfather.toml'
        path.parent.mkdir(parents=True)
        path.write_text('[[effort]]\nmodel = "grok-4.7-build-fast"\ntask_type = "research"\neffort = "low"\n'
                        '[[effort]]\nmodel = "*"\ntask_type = "research"\neffort = "medium"\n')
        model_rows = [
            dict(model='grok-4.7-build-fast', task_type='research', effort='xhigh'),
            dict(model='*', task_type='*', effort='minimal'),
            dict(model='gpt-6.1-sol', task_type='edit', effort='low'),
        ]
        with self._isolated():
            sup._CACHE.clear()
            self.assertEqual('low', sup.resolve_effort('grok-4.7-build-fast', 'research', 'high', model_rows))
            self.assertEqual('medium', sup.resolve_effort('claude-opus-5-5-high', 'research', 'high', model_rows))
            self.assertEqual('low', sup.resolve_effort('gpt-6.1-sol', 'edit', 'high', model_rows))
            self.assertEqual('minimal', sup.resolve_effort('other', 'edit', 'high', model_rows))
            self.assertEqual('high', sup.resolve_effort('other', 'plan', 'high', []))

    def test_quota_probe_parses_caches_and_cools_down(self):
        grok = 'Weekly limit (SuperGrok Heavy)\n40%\nresets in 2 days'
        self.assertEqual(60, sup.parse_tui_quota('grok', grok)['remaining'])
        self.assertEqual(40, sup.parse_tui_quota('grok', 'Weekly limit (SuperGrok Heavy) 40% left')['remaining'])
        codex = sup.parse_tui_quota('codex', 'Weekly limit: 25% left (resets Nov 1)')
        self.assertEqual(25, codex['remaining'])
        self.assertIn('Nov 1', codex['reset'])
        self.assertIsNone(sup.parse_tui_quota('grok', '   '))
        self.assertEqual('parse', sup.parse_tui_quota('grok', 'not a quota screen'))
        self.assertEqual('parse', sup.parse_tui_quota('codex', 'Weekly limit: 150% left'))
        calls = []

        def capture(binary, slash):
            calls.append((binary, slash))
            return ''

        with self._isolated():
            unknown = sup.quota_preflight('codex', 'codex', capture=capture, now=1_000)
            cached = sup.quota_preflight('codex', 'codex', capture=capture, now=1_100)
            self.assertEqual('unknown', unknown['status'])
            self.assertEqual('cooldown clear; headless remaining-quota query unavailable', unknown['detail'])
            self.assertTrue(cached.get('cached'))
            self.assertEqual(1, len(calls))
            fresh = sup.quota_preflight('codex', 'codex', capture=capture, now=1_000 + sup.PROBE_TTL + 1)
            self.assertEqual(2, len(calls))
            self.assertFalse(fresh.get('cached'))
            cooled = sup.quota_preflight('grok', 'grok', capture=lambda binary, slash: 'garbage', now=2_000)
            self.assertEqual('unknown', cooled['status'])
            os.environ['GODFATHER_QUOTA_PROBE'] = '0'
            try:
                forced = sup.quota_preflight('grok', 'grok', capture=lambda binary, slash: grok, now=3_000)
            finally:
                os.environ.pop('GODFATHER_QUOTA_PROBE', None)
            self.assertEqual('unknown', forced['status'])

    def test_live_padded_quota_screens(self):
        for cli, remaining in (('codex', 62), ('grok', 59)):
            screen = (ROOT / ('tests/fixtures/' + cli + '-quota-tui.txt')).read_text()
            self.assertEqual(remaining, sup.parse_tui_quota(cli, screen)['remaining'])

    def test_unreadable_and_timed_out_probes_stay_eligible_and_expire(self):
        for cli, capture in (('codex', lambda *a: 'trust this directory?'),
                             ('grok', mock.Mock(side_effect=subprocess.TimeoutExpired('grok', 8)))):
            with self._isolated():
                first = sup.quota_preflight(cli, cli, capture=capture, now=1000)
                self.assertEqual('unknown', first['status'])
                self.assertNotIn('remaining', first)
                self.assertTrue(sup.quota_preflight(cli, cli, capture=capture, now=1299)['cached'])
                self.assertFalse(sup.quota_preflight(cli, cli, capture=capture, now=1301).get('cached', False))
                zero = sup.quota_preflight(cli, cli, capture=lambda *a: 'Weekly limit: 0% left' if cli == 'codex'
                                          else 'Weekly limit (Heavy) 100%', now=1602)
                self.assertEqual('cooldown', zero['status'])

    def test_legacy_parse_cooldown_is_reprobed(self):
        with self._isolated():
            path = policy.state_dir() / 'quota-probe.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'codex': dict(status='cooldown', detail='quota probe parse failed', at=1000)}))
            result = sup.quota_preflight('codex', 'codex', capture=lambda *a: 'Weekly limit: 70% left', now=1001)
            self.assertEqual('ok', result['status'])

    def test_capture_submits_text_and_enter_separately(self):
        with mock.patch.object(sup.shutil, 'which', return_value='/fake/tmux'), \
                mock.patch.object(sup.time, 'sleep'), \
                mock.patch.object(sup.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='screen')) as run:
            self.assertEqual('screen', sup.capture_tui('/fake/codex', '/status'))
            self.assertEqual('screen', sup.capture_tui('/fake/codex', '/status'))
        commands = [call.args[0] for call in run.call_args_list]
        sends = [cmd for cmd in commands if cmd[1] == 'send-keys']
        self.assertEqual(['-l', '/status'], sends[0][-2:])
        self.assertEqual('Enter', sends[1][-1])
        self.assertNotEqual(sends[0][3], sends[2][3])

    def test_segment_outcomes_require_a_real_final_report(self):
        base = self.runs / 'output'
        report = Path(str(base) + '.last.md')
        log = Path(str(base) + '.log')
        denial = 'The command tool required approval that headless mode cannot prompt for, so it was auto-denied.'
        self.assertEqual('no_output', sup.segment_outcome(base, 0, {})[0])
        report.write_text(' \n')
        log.write_text('Optional startup cache warning')
        self.assertEqual('no_output', sup.segment_outcome(base, 0, {})[0])
        log.write_text(denial)
        self.assertEqual('tool_denied', sup.segment_outcome(base, 0, {})[0])
        self.assertEqual('tool_denied', sup.segment_outcome(base, 1, {})[0])
        report.write_text(denial)
        log.write_text('')
        self.assertEqual('tool_denied', sup.segment_outcome(base, 0, {})[0])
        report.write_text('200\n')
        log.write_text(denial)
        self.assertEqual((None, None), sup.segment_outcome(base, 0, {}))
        report.write_text('Summary: Recovered and finished the task.\n')
        self.assertEqual((None, None), sup.segment_outcome(base, 0, {}))
        report.write_text('I fixed a CLI that printed "' + denial + '"')
        self.assertEqual((None, None), sup.segment_outcome(base, 0, {}))
        report.write_text('')
        log.write_text('Error: usage limit reached')
        self.assertEqual('quota', sup.segment_outcome(base, 1, {})[0])

    def test_output_failure_uses_declarative_retry_defaults(self):
        rules = sup.load_policies('')
        for failure in ('no_output', 'tool_denied'):
            row = next(item for item in rules['retry'] if item['class_'] == failure)
            self.assertEqual((0, 'fallback'), (row['attempts'], row['action']))
            decision = sup.evaluate(_snap(failure_class=failure, confidence='high'), rules, STARTED + 5)
            self.assertEqual('handoff', decision['action'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
