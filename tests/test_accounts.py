"""Offline multi-account routing, session discovery, and permission regressions."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_router as fixture
import agent_oversight as a
import godfather_permissions as permissions
import godfather_policy as policy
import godfather_router as router
import godfather_supervisor as supervisor
import godfather_safety as safety


class AccountTests(unittest.TestCase):
    setUp = fixture.RouterTests.setUp
    launch = fixture.RouterTests.launch
    calls = fixture.RouterTests.calls
    state = fixture.RouterTests.state

    def configure(self, *homes, backend='codex', model='gpt-6.1-sol'):
        self.config.write_text('\n'.join(
            '[[models]]\nbackend=' + json.dumps(backend) + '\nmodel=' + json.dumps(model) + '\neffort="high"\n' +
            ('home=' + json.dumps(home) + '\n' if home is not None else '') for home in homes))

    def models(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return router.parse_models(self.config.read_text())

    def test_parse_home_and_legacy_keys(self):
        self.configure(None, '~/.codex-2')
        with mock.patch.dict(os.environ, self.env, clear=True):
            models = router.parse_models(self.config.read_text())
            self.assertEqual(str(self.root / '.codex-2'), models[1]['home'])
            self.assertEqual('codex:gpt-6.1-sol', router.model_key(models[0]))
            self.assertEqual('codex:gpt-6.1-sol@codex-2', router.model_key(models[1]))
            with mock.patch.dict(os.environ, CODEX_HOME=str(self.root / '.codex-env')):
                self.assertEqual('codex:gpt-6.1-sol', router.model_key(models[0]))
            for backend in ('agy',):
                with self.assertRaisesRegex(ValueError, 'home.*only.*codex'):
                    router.parse_models(self.config.read_text().replace('backend="codex"', 'backend="' + backend + '"'))
            for value in ('""', '12', 'false'):
                with self.assertRaisesRegex(ValueError, 'home.*path string'):
                    router.parse_models(self.config.read_text().replace('"~/.codex-2"', value))
            one = dict(models[1], home=str(self.root / 'a/account'))
            two = dict(models[1], home=str(self.root / 'b/account'))
            self.assertNotEqual(router.model_key(one), router.model_key(two))

    def test_cross_home_quota_handoff_preserves_notes_and_resume_home(self):
        self.configure('~/.codex-1', '~/.codex-2')
        first, second = (str(self.root / ('.codex-' + n)) for n in ('1', '2'))
        result = self.launch(QUOTA_HOME=first, WRITE_NOTES='1', CODEX_NATIVE='1', CODEX_SANDBOX='workspace-write')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual([first, second], [c['home'] for c in calls])
        for call in calls:
            self.assertIn(str(Path(call['home']) / 'sessions'), call['args'])
        self.assertTrue(all('resume' not in c['args'] for c in calls))
        self.assertEqual(calls[0]['notes_path'], calls[1]['notes_path'])
        self.assertIn("Previous agent's notes", calls[1]['prompt'])
        self.assertIn('Done: inspected with gpt-6.1-sol', calls[1]['prompt'])
        self.assertIn('Original task:', calls[1]['prompt'])
        self.assertEqual({'codex:gpt-6.1-sol@codex-1'}, set(self.state()))
        log = (self.root / 'runs/task.models.log').read_text()
        self.assertIn('codex:gpt-6.1-sol@codex-1 -> codex:gpt-6.1-sol@codex-2', log)
        saved = a.read(self.root / 'runs/task#2.run.json')
        self.assertEqual(second, saved['codex_home'])
        self.assertEqual(second, saved['launcher_env']['CODEX_HOME'])
        self.assertEqual('codex:gpt-6.1-sol@codex-2', saved['model_key'])
        audit = [json.loads(line) for line in (self.root / 'runs/task#2.audit.log').read_text().splitlines()]
        self.assertTrue(any(row.get('model_key') == saved['model_key'] for row in audit))
        status = subprocess.run([str(fixture.ROOT / 'bin/godfather'), 'status'], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn('gpt-6.1-sol@codex-2', status.stdout)
        quota = subprocess.run([str(fixture.ROOT / 'bin/godfather'), 'quota'], env=self.env,
                               capture_output=True, text=True, timeout=10)
        self.assertIn('gpt-6.1-sol@codex-1 eligible=no', quota.stdout)
        self.assertIn('gpt-6.1-sol@codex-2 eligible=yes', quota.stdout)
        # Deliberately resume from a caller pointing at a different home.
        nudge = subprocess.run([str(fixture.ROOT / 'bin/godfather'), 'nudge', '--wait', 'task#2', 'Verify again'],
                               env=dict(self.env, CODEX_HOME=first), capture_output=True, text=True, timeout=15)
        self.assertEqual(0, nudge.returncode, nudge.stdout + nudge.stderr)
        self.assertEqual(second, self.calls()[-1]['home'])
        self.assertIn('resume', self.calls()[-1]['args'])
        again = self.launch('next', CODEX_NATIVE='1')
        self.assertEqual(0, again.returncode, again.stdout + again.stderr)
        self.assertEqual(second, self.calls()[-1]['home'])

    def test_logged_out_home_does_not_skip_next_account(self):
        self.configure('~/.codex-1', '~/.codex-2')
        first = str(self.root / '.codex-1')
        result = self.launch(LOGGED_OUT_HOME=first)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([str(self.root / '.codex-2')], [c['home'] for c in self.calls()])
        self.assertIn('SKIP codex:gpt-6.1-sol@codex-1 not logged in',
                      (self.root / 'runs/task.models.log').read_text())
        checks = [json.loads(line)['home'] for line in (self.root / 'logins.jsonl').read_text().splitlines()]
        self.assertIn(first, checks)
        self.assertIn(str(self.root / '.codex-2'), checks)

    def test_same_home_fallback_keeps_fresh_handoff_behavior(self):
        self.configure('~/.codex-2')
        self.config.write_text(self.config.read_text() + self.config.read_text().replace('gpt-6.1-sol', 'backup'))
        result = self.launch(QUOTA_MODELS='gpt-6.1-sol', WRITE_NOTES='1')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['gpt-6.1-sol', 'backup'], [c['model'] for c in self.calls()])
        self.assertEqual(1, len({c['home'] for c in self.calls()}))
        self.assertNotIn('resume', self.calls()[-1]['args'])
        self.assertIn("Previous agent's notes", self.calls()[-1]['prompt'])

    def test_reset_one_account_preserves_other_cooldown_and_probe(self):
        self.configure('~/.codex-1', '~/.codex-2')
        with mock.patch.dict(os.environ, self.env, clear=True):
            keys = [router.model_key(m) for m in router.parse_models(self.config.read_text())]
            for key in keys:
                router.mark_exhausted(key, 'weekly limit exhausted')
            path, _ = supervisor._probe_cache(1000)
            probe_keys = ['codex@' + str(self.root / ('.codex-' + n)) for n in ('1', '2')]
            path.write_text(json.dumps({key: dict(status='cooldown', at=1000) for key in probe_keys}))
            args = type('Args', (), dict(reset=keys[0]))()
            router.quota_main(args)
            self.assertIsNone(router.cooldown(keys[0]))
            self.assertIsNotNone(router.cooldown(keys[1]))
            self.assertEqual({probe_keys[1]}, set(json.loads(path.read_text())))

    def test_task_auth_failure_is_scoped_to_home(self):
        self.configure('~/.codex-1', '~/.codex-2')
        result = self.launch(AUTH_FAILURE_HOME=str(self.root / '.codex-1'))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(str(self.root / '.codex-2'), self.calls()[-1]['home'])

    def test_default_home_inherits_env_without_changing_quota_key(self):
        self.configure(None)
        home = str(self.root / '.codex-env')
        result = self.launch(CODEX_HOME=home, QUOTA_HOME=home)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(home, self.calls()[0]['home'])
        self.assertEqual({'codex:gpt-6.1-sol'}, set(self.state()))

    def test_probe_cache_and_tmux_command_are_home_specific(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            one = str(self.root / '.codex-1')
            two = str(self.root / '.codex-2')
            calls = []
            def capture(binary, slash):
                calls.append(slash)
                return 'Weekly limit: 0% left' if len(calls) == 1 else 'Weekly limit: 90% left'
            self.assertEqual('cooldown', supervisor.quota_preflight('codex', 'codex', home=one, capture=capture, now=1000)['status'])
            self.assertEqual('ok', supervisor.quota_preflight('codex', 'codex', home=two, capture=capture, now=1001)['status'])
            self.assertTrue(supervisor.quota_preflight('codex', 'codex', home=one, capture=capture, now=1002)['cached'])
            self.assertEqual(2, len(calls))
            result = subprocess.CompletedProcess([], 0, stdout='screen')
            with mock.patch.object(supervisor.shutil, 'which', return_value='/fake/tmux'), mock.patch.object(
                    supervisor.subprocess, 'run', return_value=result) as run, mock.patch.object(supervisor.time, 'sleep'):
                supervisor.capture_tui('/fake/codex', '/status', home=two)
            command = next(c.args[0] for c in run.call_args_list if c.args[0][1] == 'new-session')
            self.assertIn('CODEX_HOME=' + two, command[-1])

    def test_discovery_and_permissions_use_saved_home(self):
        # Keep account paths outside /tmp, which is already a general writable
        # root on Linux as well as macOS.
        account_root = Path(tempfile.mkdtemp(prefix='godfather-account-', dir=str(Path.home())))
        self.addCleanup(shutil.rmtree, account_root)
        selected = account_root / '.codex-2'
        session = '33333333-3333-3333-3333-333333333333'
        transcript = selected / 'sessions/2026' / ('rollout-' + session + '.jsonl')
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps(dict(payload=dict(id=session))) + '\n')
        run = dict(name='discovery', cli='codex', model='fake', cwd=str(self.root / 'workspace'),
                   base=str(self.root / 'runs/discovery'), codex_home=str(selected), session_id=session,
                   progress_paths=[], launcher_env={'CODEX_HOME': str(selected)})
        Path(run['cwd']).mkdir()
        with mock.patch.dict(os.environ, dict(self.env, CODEX_HOME=str(account_root / '.codex-other')), clear=True):
            a.discover(run)
            self.assertIn(str(transcript), run['progress_paths'])
            profile = permissions.compile_profile(run)
            self.assertIn(str(selected / 'sessions'), profile['writable_roots'])
            self.assertNotIn(str(account_root / '.codex-other/sessions'), profile['writable_roots'])
            for home in (selected, account_root / '.codex-other', account_root / '.codex'):
                auth = home / 'auth.json'
                self.assertFalse(any(auth == Path(root) or Path(root) in auth.parents for root in profile['writable_roots']))
                self.assertEqual('deny', policy.evaluate(['tee', str(auth)], dict(cwd=run['cwd']))['verdict'])
            with mock.patch.dict(os.environ, CODEX_SANDBOX='workspace-write', GODFATHER_NATIVE='1'):
                Path(run['base']).parent.mkdir(parents=True, exist_ok=True)
                profile = permissions.write_profile(run['base'], run)
                command = permissions.apply_native(['codex', 'exec', '-'], run, profile)
            self.assertIn(str(selected / 'sessions'), command)
            self.assertEqual(str(selected / 'rules/godfather-process-inspection.rules'), profile['native_rules_file'])
            self.assertFalse((account_root / '.codex-other/rules').exists())

    def test_grok_home_parsing_and_legacy_keys(self):
        self.configure(None, '~/.grok-2', backend='grok', model='grok-4.7-build-fast')
        with mock.patch.dict(os.environ, self.env, clear=True):
            models = router.parse_models(self.config.read_text())
            self.assertEqual(str(self.root / '.grok-2'), models[1]['home'])
            self.assertEqual('grok:grok-4.7-build-fast', router.model_key(models[0]))
            self.assertEqual('grok:grok-4.7-build-fast@grok-2', router.model_key(models[1]))
            with mock.patch.dict(os.environ, GROK_HOME=str(self.root / '.grok-env')):
                self.assertEqual('grok:grok-4.7-build-fast', router.model_key(models[0]))
            self.assertNotEqual(router.model_key(dict(models[1], home=str(self.root / 'a/account'))),
                                router.model_key(dict(models[1], home=str(self.root / 'b/account'))))

    def test_grok_cross_home_quota_handoff_and_resume_use_installed_binary(self):
        self.configure('~/.grok-1', '~/.grok-2', backend='grok', model='grok-4.7-build-fast')
        first, second = (str(self.root / ('.grok-' + n)) for n in ('1', '2'))
        downloads = self.root / '.grok/downloads/test-version'
        downloads.mkdir(parents=True)
        installed = downloads / 'grok'
        installed.write_text(fixture.FAKE)
        installed.chmod(0o700)
        self.env['PATH'] = str(downloads) + os.pathsep + self.env['PATH']
        self.env['XAI_API_KEY'] = 'offline-test-key'
        result = self.launch(QUOTA_HOME=first, WRITE_NOTES='1')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual([first, second], [c['home'] for c in calls])
        self.assertTrue(all(c['binary'] == str(installed) for c in calls))
        self.assertTrue(all('--resume' not in c['args'] for c in calls))
        sessions = [c['args'][c['args'].index('--session-id') + 1] for c in calls]
        self.assertNotEqual(*sessions)
        self.assertEqual(calls[0]['notes_path'], calls[1]['notes_path'])
        self.assertIn("Previous agent's notes", calls[1]['prompt'])
        self.assertIn('Done: inspected with grok-4.7-build-fast', calls[1]['prompt'])
        self.assertEqual({'grok:grok-4.7-build-fast@grok-1'}, set(self.state()))
        saved = a.read(self.root / 'runs/task#2.run.json')
        self.assertEqual(second, saved['grok_home'])
        self.assertEqual(second, saved['launcher_env']['GROK_HOME'])
        self.assertEqual('grok:grok-4.7-build-fast@grok-2', saved['model_key'])
        self.assertIn('grok:grok-4.7-build-fast@grok-1 -> grok:grok-4.7-build-fast@grok-2',
                      (self.root / 'runs/task.models.log').read_text())
        nudge = subprocess.run([str(fixture.ROOT / 'bin/godfather'), 'nudge', '--wait', 'task#2', 'Verify again'],
                               env=dict(self.env, GROK_HOME=first), capture_output=True, text=True, timeout=15)
        self.assertEqual(0, nudge.returncode, nudge.stdout + nudge.stderr)
        self.assertEqual(second, self.calls()[-1]['home'])
        self.assertEqual(str(installed), self.calls()[-1]['binary'])
        self.assertIn('--resume', self.calls()[-1]['args'])
        again = self.launch('next')
        self.assertEqual(0, again.returncode, again.stdout + again.stderr)
        self.assertEqual(second, self.calls()[-1]['home'])

    def test_grok_missing_login_is_skipped_and_preflight_never_reads_auth(self):
        self.configure('~/.grok-missing', None, backend='grok', model='grok-4.7-build-fast')
        result = self.launch()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([str(self.root / '.grok')], [c['home'] for c in self.calls()])
        self.assertIn('SKIP grok:grok-4.7-build-fast@grok-missing no saved login',
                      (self.root / 'runs/task.models.log').read_text())
        selected = str(self.root / '.grok-2')
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(
                Path, 'is_file', autospec=True, side_effect=lambda path: path == Path(selected) / 'auth.json') as exists, mock.patch.object(
                Path, 'stat', return_value=type('Stat', (), {'st_size': 10})()), mock.patch.object(
                Path, 'open', side_effect=AssertionError('must never read credentials')):
            binary, reason = router.backend_ready('grok', selected)
            self.assertIsNotNone(binary, reason)
            self.assertIn(mock.call(Path(selected) / 'auth.json'), exists.call_args_list)

    def test_grok_env_default_keeps_legacy_quota_key(self):
        self.configure(None, backend='grok', model='grok-4.7-build-fast')
        home = str(self.root / '.grok-env')
        result = self.launch(GROK_HOME=home, QUOTA_HOME=home, XAI_API_KEY='offline-test-key')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('waiting_quota', a.read(self.root / 'runs/task.run.json')['status'])
        self.assertEqual(home, self.calls()[0]['home'])
        self.assertEqual({'grok:grok-4.7-build-fast'}, set(self.state()))

    def test_grok_probe_cache_tmux_and_reset_are_home_specific(self):
        self.configure('~/.grok-1', '~/.grok-2', backend='grok', model='grok-4.7-build-fast')
        with mock.patch.dict(os.environ, self.env, clear=True):
            one, two = (str(self.root / ('.grok-' + n)) for n in ('1', '2'))
            self.assertEqual('cooldown', supervisor.quota_preflight('grok', 'grok', home=one,
                             capture=lambda *args: 'Weekly limit (Heavy) 100%', now=1000)['status'])
            self.assertEqual('ok', supervisor.quota_preflight('grok', 'grok', home=two,
                             capture=lambda *args: 'Weekly limit (Heavy) 10%', now=1001)['status'])
            self.assertTrue(supervisor.quota_preflight('grok', 'grok', home=one, now=1002)['cached'])
            result = subprocess.CompletedProcess([], 0, stdout='screen')
            with mock.patch.object(supervisor.shutil, 'which', return_value='/fake/tmux'), mock.patch.object(
                    supervisor.subprocess, 'run', return_value=result) as run, mock.patch.object(supervisor.time, 'sleep'):
                supervisor.capture_tui('/installed/grok', '/usage', home=two, home_variable='GROK_HOME')
            command = next(c.args[0] for c in run.call_args_list if c.args[0][1] == 'new-session')
            self.assertIn('GROK_HOME=' + two, command[-1])
            self.assertTrue(command[-1].endswith('/installed/grok'))
            keys = [router.model_key(m) for m in router.parse_models(self.config.read_text())]
            for key in keys:
                router.mark_exhausted(key, 'weekly limit exhausted')
            router.quota_main(type('Args', (), dict(reset=keys[0]))())
            self.assertIsNone(router.cooldown(keys[0]))
            self.assertIsNotNone(router.cooldown(keys[1]))
            _, cache = supervisor._probe_cache(1003)
            self.assertNotIn('grok@' + one, cache)
            self.assertIn('grok@' + two, cache)

    def test_grok_saved_home_discovery_safety_and_permissions(self):
        account_root = Path(tempfile.mkdtemp(prefix='godfather-grok-', dir=str(Path.home())))
        self.addCleanup(shutil.rmtree, account_root)
        selected, other = account_root / '.grok-2', account_root / '.grok-other'
        session = '33333333-3333-3333-3333-333333333333'
        transcript = selected / 'sessions/project' / session / 'updates.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{}\n')
        work = self.root / 'workspace'
        work.mkdir()
        run = dict(name='grok-session', cli='grok', model='fake', cwd=str(work), started_at=a.now(),
                   base=str(self.root / 'runs/grok-session'), grok_home=str(selected), session_id=session,
                   progress_paths=[], launcher_env={'GROK_HOME': str(selected)})
        with mock.patch.dict(os.environ, dict(self.env, GROK_HOME=str(other), GODFATHER_SHIMS='enforce'), clear=True):
            a.discover(run)
            self.assertEqual(str(transcript.parent), run['storage_path'])
            profile = permissions.compile_profile(run)
            self.assertIn(str(selected / 'sessions'), profile['writable_roots'])
            self.assertNotIn(str(other / 'sessions'), profile['writable_roots'])
            self.assertIn('Write(' + str(selected / 'sessions') + '/**)', profile['grok_allow'])
            for home in (selected, other, account_root / '.grok'):
                auth = home / 'auth.json'
                self.assertFalse(any(auth == Path(root) or Path(root) in auth.parents for root in profile['writable_roots']))
                self.assertEqual('deny', policy.evaluate(['tee', str(auth)], dict(cwd=str(work)))['verdict'])
            base = Path(run['base'])
            base.parent.mkdir(parents=True, exist_ok=True)
            safety.atomic(str(base) + '.run.json', run)
            safety.atomic(str(base) + '.safety.json', dict(cwd=str(work), scratch=[], real={}))
            with mock.patch.object(sys, 'argv', ['shim', str(transcript)]), mock.patch.object(
                    policy, 'evaluate', return_value=dict(verdict='deny', targets=[], cwd=str(work), effect='test')) as evaluate:
                self.assertEqual(126, safety.shim(base, 'tee'))
            scratch = evaluate.call_args.args[1]['scratch']
            self.assertTrue(any(str(selected / 'sessions') in path for path in scratch))
            self.assertFalse(any(str(other) in path for path in scratch))


if __name__ == '__main__':
    unittest.main()
