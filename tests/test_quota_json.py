"""Subscription quota fixtures; no provider CLI, tmux or network is permitted."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import agent_oversight as a
import godfather_cursor as cursor
import godfather_router as router
import godfather_supervisor as supervisor
import godfather_usage as usage

FIX = ROOT / 'tests/fixtures/quota'
NOW = dt.datetime(2026, 11, 11, 18, tzinfo=dt.timezone.utc).timestamp()
CAPTURE_TUI = supervisor.capture_tui


class QuotaJSONTests(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = dict(HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / 'config'),
                   XDG_STATE_HOME=str(self.root / 'state'), AGENT_RUNS_DIR=str(self.root / 'runs'),
                   PATH=os.environ['PATH'], PYTHONDONTWRITEBYTECODE='1')
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        for module, name in [(subprocess, 'run'), (cursor.urllib.request, 'urlopen'), (supervisor, 'capture_tui')]:
            guard = mock.patch.object(module, name, side_effect=AssertionError('live provider access forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.clock = mock.patch('time.time', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.root.joinpath('runs').mkdir()
        usage.state_dir().mkdir(parents=True)
        config = router.config_path()
        config.parent.mkdir(parents=True)
        config.write_text((FIX / 'models.toml').read_text())
        self.models = router.load_models()
        session = self.root / '.codex-2/sessions/2026/11/11/rollout-quota.jsonl'
        session.parent.mkdir(parents=True)
        session.write_text((FIX / 'codex.jsonl').read_text())
        usage.save_snapshot((FIX / 'agy.txt').read_text())
        self.cursor_data = json.loads((FIX / 'cursor.json').read_text())
        cursor.cached('usage', 300, lambda: self.cursor_data)
        cursor.cached('models', 3600, lambda: {'opus': 'Opus', 'opus-fast': 'Opus Fast'})
        self.binary = mock.patch.object(a, 'cli_binary', return_value='/fixture/provider')
        self.binary.start()
        self.addCleanup(self.binary.stop)
        supervisor.quota_preflight('grok', '/fixture/grok', now=NOW,
                                  capture=lambda *args: (FIX / 'grok-used.txt').read_text())

    def report(self, persisted=None, **kwargs):
        return usage.quota_report(self.models, persisted or {}, now=NOW, **kwargs)

    def test_order_grouping_windows_values_and_private_fields(self):
        result, rows = self.report()
        accounts = result['accounts']
        self.assertEqual(list(range(1, 8)), [r['order'] for r in accounts])
        self.assertEqual(['codex', 'agy', 'grok', 'agy', 'cursor', 'agy', 'codex'], [r['backend'] for r in accounts])
        self.assertEqual('codex@~/.codex-2', accounts[0]['key'])
        self.assertEqual('Codex · company', accounts[0]['label'])
        self.assertEqual(['gpt-6.1-sol', 'backup'], accounts[0]['models'])
        self.assertEqual(['claude-opus-5-5-high', 'claude-sonnet-5-5-high', 'gpt-oss-120b-medium'], accounts[1]['models'])
        self.assertEqual(('self_serve_business_prolite', 'weekly', 3.0, 'session-log'),
                         tuple(accounts[0][k] for k in ('plan', 'window', 'used_percent', 'source')))
        self.assertEqual([3, 12], [w['used_percent'] for w in accounts[0]['windows']])
        self.assertEqual(usage.quota_iso(1794743064), accounts[0]['resets_at'])
        self.assertEqual(usage.quota_iso(NOW), accounts[0]['observed_at'])
        self.assertEqual((18, 31, 56, 25, 9), tuple(accounts[i]['used_percent'] for i in (1, 2, 3, 4, 5)))
        self.assertEqual(dict(used_cents=2500, limit_cents=10000, kind='grant'), accounts[4]['money'])
        self.assertEqual('2026-12-01T00:00:00+00:00', accounts[4]['resets_at'])
        self.assertEqual(1234, accounts[4]['on_demand']['overallUsed'])
        self.assertEqual(('unknown', None, None), (accounts[-1]['status'], accounts[-1]['used_percent'], accounts[-1]['observed_at']))
        self.assertTrue(accounts[-1]['eligible'])
        self.assertEqual([m['backend'] for m in self.models], [line.split(' ', 1)[0] for line in rows])
        encoded = json.dumps(result, allow_nan=False)
        for secret in ('never@example.com', 'must-not-appear', 'access_token', 'accessToken'):
            self.assertNotIn(secret, encoded)

    def test_cli_json_and_text_use_identical_eligibility(self):
        router.mark_exhausted(router.model_key(self.models[0]), 'HTTP 402 bearer secret api_key=hidden never@example.com')
        streams = []
        for args in (['quota', '--json'], ['quota']):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(sys, 'argv', ['godfather'] + args):
                self.assertEqual(0, router.main())
            streams.append(out.getvalue())
        result = json.loads(streams[0])
        row = result['accounts'][0]
        self.assertTrue(row['eligible'])  # The other model in the pool is clear.
        self.assertFalse(row['model_eligibility']['gpt-6.1-sol'])
        self.assertIn('codex gpt-6.1-sol@codex-2 eligible=no', streams[1])
        self.assertIn('codex backup@codex-2 eligible=yes', streams[1])
        self.assertIn('cooldown until=', row['detail'])
        for secret in ('secret', 'hidden', 'never@example.com'):
            self.assertNotIn(secret, ''.join(streams))

    def test_newer_app_server_and_account_isolation(self):
        a.write(self.root / 'runs/bridge.run.json', dict(cli='codex', codex_home=str(self.root / '.codex-2'),
                    rate_limits_at=NOW + 1, rate_limits=dict(primary=dict(usedPercent=100, windowDurationMins=300, resetsAt=NOW + 500))))
        report, _ = self.report()
        first, other = report['accounts'][0], report['accounts'][-1]
        self.assertEqual(('api', 100, 'exhausted', False), (first['source'], first['used_percent'], first['status'], first['eligible']))
        self.assertEqual('unknown', other['status'])
        # A newer file with older event timestamps must not supersede the log.
        a.write(self.root / 'runs/bridge.run.json', dict(cli='codex', codex_home=str(self.root / '.codex-2'),
                    rate_limits_at=NOW - 1, rate_limits=dict(primary=dict(usedPercent=50))))
        self.assertEqual(3, self.report()[0]['accounts'][0]['used_percent'])

    def test_label_on_later_block_and_invalid_labels(self):
        self.models[0].pop('label')
        self.models[8]['label'] = 'Shared subscription'
        self.assertEqual('Shared subscription', self.report()[0]['accounts'][0]['label'])
        for label in ('1', '""', 'false'):
            with self.assertRaisesRegex(ValueError, 'label'):
                router.parse_models('[[models]]\nbackend="codex"\nmodel="gpt"\neffort="high"\nlabel=' + label)
        self.assertEqual(1, len(router.parse_models('[[models]]\nbackend="codex"\nmodel="gpt"\neffort="high"')))

    def test_grok_used_exhausted_normalized_crosscheck_and_next_local_year(self):
        for fixture, remaining in (('grok-used.txt', 69), ('grok-exhausted.txt', 0)):
            screen = (FIX / fixture).read_text()
            for text in (screen, ' '.join(screen.split()), '\x1b[31m' + screen + '\x1b[0m'):
                parsed = supervisor.parse_tui_quota('grok', text, now=NOW)
                self.assertEqual(remaining, parsed['remaining'])
                self.assertEqual('SuperGrok Heavy', parsed['plan'])
                self.assertGreater(parsed['resets_at'], NOW)
                self.assertTrue(parsed['consistent'])
        screen = (FIX / 'grok-used.txt').read_text() + '\nWeekly limit left: 0%'
        parsed = supervisor.parse_tui_quota('grok', screen, now=NOW)
        self.assertEqual(69, parsed['remaining'])
        self.assertFalse(parsed['consistent'])
        local_now = dt.datetime(2026, 12, 31, 10).timestamp()
        reset = supervisor.parse_tui_quota('grok', 'Weekly limit (Heavy) 31% Resets: January 1, 14:30', now=local_now)['resets_at']
        self.assertEqual(dt.datetime(2027, 1, 1, 14, 30), dt.datetime.fromtimestamp(reset))
        self.assertEqual('parse', supervisor.parse_tui_quota('grok', 'Weekly limit (Heavy) 101%'))
        self.assertEqual(10, supervisor.parse_tui_quota('grok', 'Weekly limit left: 10%')['remaining'])

    def test_preflight_force_cache_minimum_and_exhausted_metadata(self):
        capture = mock.Mock(return_value=(FIX / 'grok-exhausted.txt').read_text())
        self.assertTrue(supervisor.quota_preflight('grok', 'fixture', now=NOW, capture=capture)['cached'])
        self.assertEqual(0, capture.call_count)
        low = supervisor.quota_preflight('grok', 'fixture', now=NOW, minimum=70, capture=capture)
        self.assertEqual('ineligible', low['status'])
        result = supervisor.quota_preflight('grok', 'fixture', now=NOW, capture=capture, force=True)
        self.assertEqual(1, capture.call_count)
        self.assertEqual(('cooldown', 100, 'SuperGrok Heavy'), (result['status'], result['used_percent'], result['plan']))
        self.assertGreater(result['resets_at'], NOW)
        self.assertEqual(69, supervisor.quota_preflight('grok', 'fixture', now=NOW + 301,
                         capture=lambda *args: (FIX / 'grok-used.txt').read_text())['remaining'])

    def test_grok_log_fallback_scope_timestamp_and_no_raw_error(self):
        # Force a failed probe. Quota evidence must stay inside the account.
        supervisor.quota_preflight('grok', None, now=NOW, capture=lambda *args: '', force=True)
        a.write(self.root / 'runs/grok.run.json', dict(cli='grok', grok_home=str(self.root / '.grok')))
        log = self.root / 'runs/grok.log'
        log.write_text('2026-11-11T17:00:00Z Error: HTTP 402 usage balance exhausted; token=must-not-appear\n')
        report, _ = self.report()
        row = report['accounts'][2]
        self.assertEqual(('exhausted', 'error-log', False, '2026-11-11T17:00:00+00:00'),
                         tuple(row[k] for k in ('status', 'source', 'eligible', 'observed_at')))
        self.assertIsNone(row['used_percent'])
        self.assertNotIn('must-not-appear', json.dumps(report))
        self.models[2]['home'] = str(self.root / '.grok-other')
        supervisor.quota_preflight('grok', None, home=self.models[2]['home'], now=NOW, capture=lambda *args: '', force=True)
        self.assertEqual('unknown', self.report()[0]['accounts'][2]['status'])

    def test_agy_saved_snapshot_no_implicit_refresh_and_live_refresh(self):
        a.write(usage.state_dir() / 'agy-usage.json', dict(at=NOW - 999999, text=(FIX / 'agy.txt').read_text()))
        with mock.patch.object(router, 'agy_usage', side_effect=AssertionError('implicit refresh')):
            self.assertEqual(18, self.report()[0]['accounts'][1]['used_percent'])
        refreshed = (FIX / 'agy.txt').read_text().replace('82%', '0%')
        def refresh(*args):
            usage.save_snapshot(refreshed)
            return refreshed
        with mock.patch.object(router, 'agy_usage', side_effect=refresh) as agy, \
             mock.patch.object(supervisor.shutil, 'which', return_value='/fixture/tmux'), \
             mock.patch.object(supervisor, 'capture_tui', return_value=(FIX / 'grok-used.txt').read_text()) as grok, \
             mock.patch.object(cursor, 'usage', return_value=self.cursor_data) as cur:
            result, _ = self.report(live=True)
        agy.assert_called_once()
        grok.assert_called_once()
        cur.assert_called_once_with(None, force=True)
        self.assertEqual(('exhausted', False), (result['accounts'][1]['status'], result['accounts'][1]['eligible']))

    def test_cooldown_and_low_threshold_match_text(self):
        self.models[0]['min_pct'] = 99
        self.models[8]['min_pct'] = 99
        row = self.report()[0]['accounts'][0]
        self.assertEqual(('low', False), (row['status'], row['eligible']))
        cooldown = dict(until=usage.quota_iso(NOW + 500), reason='quota exhausted')
        persisted = {router.model_key(m): cooldown for m in self.models if m['backend'] == 'codex'}
        row = self.report(persisted)[0]['accounts'][0]
        self.assertEqual(('cooldown', False), (row['status'], row['eligible']))
        self.assertEqual(2, len(row['cooldowns']))

    def test_cursor_cycle_fallback_equal_and_expired_preserve_valid_period(self):
        self.assertEqual(1796083200, cursor.cycle_end(self.cursor_data, NOW))
        self.assertIn('cycle end=2026-12-01T00:00:00+00:00', cursor.describe(self.cursor_data))
        for end in (NOW - 1, NOW, None):
            self.assertEqual(1796083200, cursor.cycle_end(dict(self.cursor_data, billingCycleEnd=end), NOW))
        self.assertEqual(NOW + 1000, cursor.cycle_end(dict(self.cursor_data, billingCycleEnd=(NOW + 1000)*1000), NOW))
        self.assertEqual(1796083200, cursor.cycle_end(dict(self.cursor_data, billingCycleEnd=str(int(NOW*1000))), NOW))

    def test_missing_snapshots_keep_accounts_and_argon_pool(self):
        a.write(usage.state_dir() / 'agy-usage.json', {})
        result, _ = self.report()
        self.assertEqual(7, len(result['accounts']))
        for i in (1, 3, 5):
            self.assertEqual(('unknown', None, True), tuple(result['accounts'][i][k] for k in ('status', 'used_percent', 'eligible')))
        self.assertEqual('agy@Gemini 4 Argon', result['accounts'][3]['key'])

    def test_effective_environment_home_groups_with_explicit_home(self):
        os.environ['CODEX_HOME'] = str(self.root / '.codex-2')
        self.models[-1].pop('home')
        result, _ = self.report()
        self.assertEqual(6, len(result['accounts']))
        self.assertEqual(['gpt-6.1-sol', 'backup'], result['accounts'][0]['models'])

    def test_quoted_credential_and_email_redaction(self):
        text = usage.quota_safe('HTTP 402 {"access_token": "opaque-value", "api_key": "another-value"} never@example.com')
        for secret in ('opaque-value', 'another-value', 'never@example.com'):
            self.assertNotIn(secret, text)

    def test_cursor_grants_only_and_included_floor(self):
        data = dict(self.cursor_data)
        data.pop('planUsage')
        with mock.patch.object(cursor, 'usage', return_value=data):
            row = self.report()[0]['accounts'][4]
        self.assertEqual(('ok', None, True), tuple(row[k] for k in ('status', 'used_percent', 'eligible')))
        self.models[4].update(included_only=True, included_floor_cents=8000)
        with mock.patch.object(cursor, 'usage', return_value=data):
            result, rows = self.report()
        row = result['accounts'][4]
        self.assertEqual(('low', False), (row['status'], row['eligible']))
        self.assertIn('cursor opus eligible=no', rows[4])

    def test_codex_secondary_exhaustion_and_numeric_credit_balance(self):
        a.write(self.root / 'runs/bridge.run.json', dict(cli='codex', codex_home=str(self.root / '.codex-2'),
                    rate_limits_at=NOW + 1, rate_limits=dict(planType='business', credits=dict(balance='12.50', hasCredits=True),
                    primary=dict(usedPercent=3, windowDurationMins=10080), secondary=dict(usedPercent=100, windowDurationMins=300))))
        row = self.report()[0]['accounts'][0]
        self.assertEqual((3, 'weekly', 'exhausted', False), tuple(row[k] for k in ('used_percent', 'window', 'status', 'eligible')))
        self.assertEqual(dict(balance=12.5, hasCredits=True), row['credits'])

    def test_grok_stream_error_timestamp_and_forced_capture_over_native(self):
        supervisor.quota_preflight('grok', None, now=NOW, capture=lambda *args: '', force=True)
        a.write(self.root / 'runs/grok.run.json', dict(cli='grok', status='quota', ended_at=usage.quota_iso(NOW)))
        (self.root / 'runs/grok.stream.jsonl').write_text(json.dumps(dict(type='error', timestamp=usage.quota_iso(NOW - 50), message='HTTP 402 usage balance exhausted')) + '\n')
        row = self.report()[0]['accounts'][2]
        self.assertEqual(usage.quota_iso(NOW - 50), row['observed_at'])
        with mock.patch.object(supervisor, 'structured_quota', return_value=dict(remaining=0)) as native:
            result = supervisor.quota_preflight('grok', 'fixture', now=NOW, force=True,
                         capture=lambda *args: (FIX / 'grok-used.txt').read_text())
        native.assert_not_called()
        self.assertEqual(69, result['remaining'])

    def test_unreadable_grok_does_not_reuse_old_exhaustion(self):
        a.write(self.root / 'runs/grok.run.json', dict(cli='grok', status='quota', ended_at=usage.quota_iso(NOW - 50)))
        (self.root / 'runs/grok.log').write_text('2026-11-11T17:00:00Z HTTP 402 usage balance exhausted\n')
        for capture in (lambda *args: 'not a quota screen', mock.Mock(side_effect=subprocess.TimeoutExpired('grok', 8))):
            result = supervisor.quota_preflight('grok', 'fixture', now=NOW, force=True, capture=capture)
            self.assertFalse(result['unavailable'])
            report, rows = self.report()
            row = report['accounts'][2]
            self.assertEqual(('unknown', 'tui', True, None), tuple(row[k] for k in ('status', 'source', 'eligible', 'used_percent')))
            self.assertEqual([], row['cooldowns'])
            self.assertIn('grok grok-4.7-build-fast eligible=yes', rows[2])
            self.assertIn('unreadable', rows[2])
            self.assertEqual({}, json.loads(router.state_path().read_text()) if router.state_path().exists() else {})
        # The pre-upgrade cached unreadable result must behave the same way.
        path, _ = supervisor._probe_cache(NOW)
        a.write(path, {'grok': dict(status='unknown', detail='quota probe unreadable; remaining quota unknown', at=NOW)})
        self.assertTrue(self.report()[0]['accounts'][2]['eligible'])
        # A probe that cannot start still permits the specified log fallback.
        supervisor.quota_preflight('grok', None, now=NOW, force=True)
        self.assertEqual('error-log', self.report()[0]['accounts'][2]['source'])

    def test_tui_start_failure_is_distinct_from_capture_failure(self):
        for outcome in ('start-failed', 'empty-pane', 'timeout', 'capture-error', 'cleanup-error'):
            with self.subTest(outcome=outcome):
                kills = []
                def command(argv, **kwargs):
                    if argv[1] == 'kill-session':
                        kills.append(argv)
                        if outcome == 'cleanup-error' and len(kills) == 2:
                            raise subprocess.TimeoutExpired('tmux', 3)
                    if argv[1] == 'new-session' and outcome == 'start-failed':
                        return subprocess.CompletedProcess(argv, 1, '', '')
                    if argv[1] == 'capture-pane':
                        if outcome == 'timeout':
                            raise subprocess.TimeoutExpired('tmux', 3)
                        if outcome == 'capture-error':
                            raise OSError('fixture capture failure')
                    return subprocess.CompletedProcess(argv, 0, '', '')
                with mock.patch.object(supervisor.shutil, 'which', return_value='/fixture/tmux'), \
                     mock.patch.object(supervisor.time, 'sleep'), \
                     mock.patch.object(subprocess, 'run', side_effect=command), \
                     mock.patch.object(supervisor, 'capture_tui', side_effect=CAPTURE_TUI):
                    result = supervisor.quota_preflight('grok', 'fixture', now=NOW, force=True)
                self.assertEqual('unknown', result['status'])
                self.assertEqual(outcome == 'start-failed', result['unavailable'])

    def test_blank_started_grok_panes_cannot_reuse_old_quota_errors(self):
        a.write(self.root / 'runs/grok.run.json', dict(cli='grok', status='quota', ended_at=usage.quota_iso(NOW - 50)))
        (self.root / 'runs/grok.log').write_text('2026-11-11T17:00:00Z HTTP 402 usage balance exhausted\n')
        for screen in ('', '   \n  \n\t', '\x1b[2J\x1b[H  \n\t\x1b[0m', '\x1b[0m'):
            with self.subTest(screen=repr(screen)):
                def command(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, 0, screen if argv[1] == 'capture-pane' else '', '')
                with mock.patch.object(supervisor.shutil, 'which', return_value='/fixture/tmux'), \
                     mock.patch.object(supervisor.time, 'sleep'), \
                     mock.patch.object(subprocess, 'run', side_effect=command), \
                     mock.patch.object(supervisor, 'capture_tui', side_effect=CAPTURE_TUI):
                    result = supervisor.quota_preflight('grok', 'fixture', now=NOW, force=True)
                self.assertFalse(result['unavailable'])
                self.assertTrue(supervisor.quota_preflight('grok', 'fixture', now=NOW + 1)['cached'])
                report, rows = self.report()
                row = report['accounts'][2]
                self.assertEqual(('unknown', 'tui', True, None), tuple(row[k] for k in ('status', 'source', 'eligible', 'used_percent')))
                self.assertEqual([], row['cooldowns'])
                self.assertIn('grok grok-4.7-build-fast eligible=yes', rows[2])
                self.assertIn('unreadable', rows[2])

    def test_pool_status_ignores_a_blocked_sibling_when_another_is_eligible(self):
        self.models[0]['min_pct'] = 99
        for models in (self.models, list(reversed(self.models))):
            result, rows = usage.quota_report(models, {}, now=NOW)
            row = next(r for r in result['accounts'] if r['key'] == 'codex@~/.codex-2')
            self.assertEqual(('ok', 3, True), tuple(row[k] for k in ('status', 'used_percent', 'eligible')))
            self.assertEqual({'gpt-6.1-sol': False, 'backup': True}, row['model_eligibility'])
            self.assertTrue(any('codex gpt-6.1-sol@codex-2 eligible=no' in r for r in rows))

    def test_cursor_included_only_gate_is_per_model(self):
        self.models[4].update(included_only=True, included_floor_cents=8000)
        self.models.append(dict(backend='cursor', model='backup', effort='high'))
        with mock.patch.object(cursor, 'resolve', side_effect=lambda m, binary: m['model']):
            for models in (self.models, list(reversed(self.models))):
                result, rows = usage.quota_report(models, {}, now=NOW)
                row = next(r for r in result['accounts'] if r['backend'] == 'cursor')
                self.assertEqual(('ok', 25, True), tuple(row[k] for k in ('status', 'used_percent', 'eligible')))
                self.assertEqual({'opus': False, 'backup': True}, row['model_eligibility'])
                self.assertIn('cursor opus eligible=no', '\n'.join(rows))
                self.assertIn('cursor backup eligible=yes', '\n'.join(rows))

    def test_spent_cursor_grants_do_not_exhaust_the_plan_window(self):
        data = json.loads(json.dumps(self.cursor_data))
        data['activeGrants'][0]['remainingCents'] = 0
        with mock.patch.object(cursor, 'usage', return_value=data):
            report, rows = self.report()
            row = report['accounts'][4]
            self.assertEqual(('ok', 25, True), tuple(row[k] for k in ('status', 'used_percent', 'eligible')))
            self.assertEqual(dict(used_cents=10000, limit_cents=10000, kind='grant'), row['money'])
            self.assertIn('cursor opus eligible=yes', rows[4])
            self.models[4]['included_only'] = True
            row = self.report()[0]['accounts'][4]
            self.assertEqual(('low', 25, False), tuple(row[k] for k in ('status', 'used_percent', 'eligible')))

    def test_cursor_failed_refresh_preserves_cache_and_observation_time(self):
        path = cursor.usage_cache_path()
        for force, age in ((True, 60), (False, 301)):
            for bearer in (None, 'fixture-token'):
                with self.subTest(force=force, bearer=bool(bearer)):
                    saved = dict(at=NOW - age, value=self.cursor_data)
                    a.write(path, saved)
                    before = path.read_bytes()
                    with mock.patch.object(cursor, 'token', return_value=bearer), \
                         mock.patch.object(cursor.urllib.request, 'urlopen', side_effect=OSError('fixture offline')) as http:
                        self.assertEqual(self.cursor_data, cursor.usage(force=force))
                    self.assertEqual(4 if bearer else 0, http.call_count)
                    self.assertEqual(before, path.read_bytes())
                    if force:
                        self.assertEqual(usage.quota_iso(NOW - age), self.report()[0]['accounts'][4]['observed_at'])

    def test_cursor_partial_refresh_retains_failed_endpoint_fields_and_age(self):
        payloads = {
            'GetPlanInfo': dict(planInfo=self.cursor_data['planInfo']),
            'GetCurrentPeriodUsage': dict(planUsage=dict(totalPercentUsed=12), spendLimitUsage=self.cursor_data['spendLimitUsage']),
            'GetUsageLimitStatusAndActiveGrants': dict(activeGrants=self.cursor_data['activeGrants']),
            'GetHardLimit': dict(hardLimit=2000),
        }
        path = cursor.usage_cache_path()
        for age in (60, 301):
            for failed in payloads:
                with self.subTest(failed=failed, age=age):
                    a.write(path, dict(at=NOW - age, value=self.cursor_data))
                    def response(request, timeout):
                        method = request.full_url.rsplit('/', 1)[1]
                        if method == failed:
                            raise OSError('fixture failed endpoint')
                        return io.StringIO(json.dumps(payloads[method]))
                    with mock.patch.object(cursor, 'token', return_value='fixture-token'), \
                         mock.patch.object(cursor.urllib.request, 'urlopen', side_effect=response) as http:
                        result = cursor.usage(force=True)
                        self.assertEqual(result, cursor.usage())
                        self.assertEqual(4 if age < 300 else 8, http.call_count)
                    self.assertEqual(self.cursor_data['planInfo'], result['planInfo'])
                    self.assertEqual(self.cursor_data['activeGrants'], result['activeGrants'])
                    self.assertEqual(25 if failed == 'GetCurrentPeriodUsage' else 12, result['planUsage']['totalPercentUsed'])
                    self.assertEqual(NOW - age, json.loads(path.read_text())['at'])
                    self.assertEqual(result, json.loads(path.read_text())['value'])
        with mock.patch.object(cursor, 'token', return_value='fixture-token'), \
             mock.patch.object(cursor.urllib.request, 'urlopen', side_effect=lambda request, timeout: io.StringIO(json.dumps(payloads[request.full_url.rsplit('/', 1)[1]]))):
            result = cursor.usage(force=True)
        self.assertEqual(12, result['planUsage']['totalPercentUsed'])
        self.assertEqual(NOW, json.loads(path.read_text())['at'])

    def test_cursor_cold_partial_refresh_keeps_available_data(self):
        home = str(self.root / '.cursor-new')
        def response(request, timeout):
            if request.full_url.endswith('/GetPlanInfo'):
                return io.StringIO(json.dumps(dict(planInfo=self.cursor_data['planInfo'])))
            raise OSError('fixture unavailable endpoint')
        with mock.patch.object(cursor, 'token', return_value='fixture-token'), \
             mock.patch.object(cursor.urllib.request, 'urlopen', side_effect=response):
            result = cursor.usage(home, force=True)
        self.assertEqual(dict(planInfo=self.cursor_data['planInfo']), result)
        self.assertEqual(NOW, json.loads(cursor.usage_cache_path(home).read_text())['at'])


if __name__ == '__main__':
    if '--sample' in sys.argv:
        case = QuotaJSONTests()
        case.setUp()
        try:
            with mock.patch.object(sys, 'argv', ['godfather', 'quota', '--json']):
                router.main()
        finally:
            case.doCleanups()
    else:
        unittest.main()
