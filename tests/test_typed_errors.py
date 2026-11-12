"""Typed provider failures and durable reset waits; no real CLI is used."""
import contextlib
import fcntl
import io
import json
import os
import subprocess
from types import SimpleNamespace
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import agent_oversight as a
import godfather_errors as errors
import godfather_lifecycle as life
import godfather_plan as plan
import godfather_registry as registry
import godfather_router as router
import godfather_safety as safety
import godfather_store as store
import godfather_stream as stream
import godfather_supervisor as supervisor
import test_router as fixture
import watch_fixture


class TaxonomyTests(unittest.TestCase):
    def test_codex_codes_and_http_status(self):
        cases = dict(contextWindowExceeded='context_window', usageLimitExceeded='quota',
                     rateLimitExceeded='quota', sessionBudgetExceeded='quota', serverOverloaded='capacity', modelNotFound='model_not_found',
                     unauthorized='auth', badRequest='bad_request', sandboxError='permission_denied',
                     cyberPolicy='policy', misalignmentPolicyViolation='policy', tooManyDenials='policy',
                     internalServerError='internal', flexUnavailable='capacity', policyCapacityUnavailable='capacity',
                     httpConnectionFailed='network', responseStreamDisconnected='network', other='other')
        for code, category in cases.items():
            with self.subTest(code=code):
                result = errors.classify('codex', dict(error=dict(codexErrorInfo=code, message='401 quota overload')))
                self.assertEqual((category, 'structured'), (result['category'], result['source']))
                self.assertIn('evidence', result)
                self.assertIsInstance(result['retryable'], bool)
        for status, category in ((401, 'auth'), (403, 'auth'), (429, 'quota'), (402, 'quota'), (500, 'internal'), (503, 'capacity'), (400, 'bad_request'), (0, 'network')):
            result = errors.classify('codex', dict(error=dict(codexErrorInfo=dict(httpConnectionFailed=dict(httpStatusCode=status)))))
            self.assertEqual(category, result['category'])

    def test_structured_agy_grok_and_unknown_code_never_guess_from_text(self):
        for cli in ('agy', 'grok'):
            for key in ('code', 'status', 'type'):
                event = dict(type='error', error={key: 'RESOURCE_EXHAUSTED', 'message': '401 overloaded'})
                result = errors.classify(cli, event)
                self.assertEqual(('quota', 'structured'), (result['category'], result['source']))
            self.assertEqual('other', errors.classify(cli, dict(type='error', code='unknown_new_code', message='401'))['category'])
            self.assertEqual('permission_denied', errors.classify(cli, dict(type='error', code=403, message='write_file tool auto-denied in headless mode: 401'))['category'])

    def test_agy_stream_errors_without_step_index_keep_structured_fields(self):
        progress = a.Progress(dict(cli='agy', started_at=a.now(), progress_paths=[]))
        for code, category in (('UNAUTHENTICATED', 'auth'), ('RESOURCE_EXHAUSTED', 'quota')):
            event = dict(type='error', error=dict(code=code, message='provider failed'))
            beat = stream.classify('agy', event)
            self.assertEqual((category, 'structured'), (beat['category'], beat['source']))
            progress.parse(event, 'agy')
            self.assertEqual((category, 'structured'), (progress.failure['category'], progress.failure['source']))

    def test_grok_embedded_provider_json_from_recorded_error(self):
        event = dict(type='error', message='Internal error: {\n  "message": "API error (status 402 Payment Required): Grok Build usage balance exhausted",\n  "http_status": 402\n}')
        result = errors.classify('grok', event)
        self.assertEqual(('quota', 'structured'), (result['category'], result['source']))
        self.assertEqual(event, result['evidence'])
        self.assertEqual('quota', errors.classify('grok', 'Grok Build usage balance exhausted')['category'])
        plain = errors.classify('grok', dict(type='error', message="Couldn't start session: ..."))
        self.assertEqual(('other', 'text'), (plain['category'], plain['source']))

    def test_text_fallback_and_transient_reconnects(self):
        cases = {'permission denied for tool 401': 'permission_denied', 'model at capacity 429': 'capacity',
                 'weekly limit reached': 'quota', 'authentication failed': 'auth', 'context window exceeded': 'context_window',
                 'model not found': 'model_not_found', 'content policy refusal': 'policy', 'bad request': 'bad_request',
                 'request timed out': 'network', 'broken stream': 'broken_stream', 'internal server error': 'internal', 'unknown': 'other'}
        for message, category in cases.items():
            self.assertEqual((category, 'text'), tuple(errors.classify('codex', message)[key] for key in ('category', 'source')))
        for attempt in (1, 4, 5):
            event = dict(type='error', message='Reconnecting... %s/5 (request timed out)' % attempt)
            beat = stream.classify('codex', event)
            self.assertEqual(('stream', None), (beat['kind'], beat['failure_class']))
        final = stream.classify('codex', dict(type='turn.failed', error=dict(message='request timed out')))
        self.assertEqual(('network', 'text', 'crash'), (final['category'], final['source'], final['failure_class']))

    def test_fixture_sources_retain_text_fallback(self):
        for cli, event, category in (
            ('grok', dict(type='error', message="Couldn't start session: ..."), 'other'),
            ('agy', dict(type='ERROR_MESSAGE', status='DONE', error='The stream was interrupted.'), 'broken_stream'),
            ('codex', dict(type='turn.failed', error=dict(message='Selected model is at capacity.')), 'capacity')):
            beat = stream.classify(cli, event)
            self.assertEqual((category, 'text'), (beat['category'], beat['source']))

    def test_reset_windows_and_relative_text(self):
        obj = dict(primary=dict(usedPercent=100, resetsAt=1100), secondary=dict(usedPercent=40, resetsAt=9000))
        self.assertEqual(1100, errors.reset_at(obj, 1000))
        obj['secondary']['usedPercent'] = 100
        self.assertEqual(9000, errors.reset_at(obj, 1000))
        obj['secondary']['resetsAt'] = None
        self.assertIsNone(errors.reset_at(obj, 1000))
        self.assertEqual(1120, errors.reset_at('try again in 2 minutes', 1000))
        self.assertIsNone(errors.reset_at('quota exhausted', 1000))


class QuotaWaitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        env = dict(os.environ, HOME=str(self.root), AGENT_RUNS_DIR=str(self.runs),
                   XDG_STATE_HOME=str(self.root / 'state'), XDG_CONFIG_HOME=str(self.root / 'config'),
                   GODFATHER_QUEUE_DAEMON='1', GODFATHER_QUOTA_MAX_WAIT='21600')
        env.pop('GODFATHER_DB', None)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.base = self.runs / 'task'
        self.run = dict(name='task', run_id='instance', status='quota', cli='codex', model='fake',
                        cwd=str(self.root), started_at=a.now(), session_id='saved-session', errors=[], progress_paths=[])
        safety.atomic(str(self.base) + '.run.json', self.run)

    def schedule(self, reset=1100):
        return life.schedule_quota(self.base, self.run, reset, now=(time.time() if reset and reset > 1e9 else 1000))

    def test_wait_persists_imports_and_gate_survives_process_restart(self):
        self.assertTrue(self.schedule())
        run = safety.read(str(self.base) + '.run.json')
        self.assertEqual(('waiting_quota', 1160), (run['status'], run['resume_at']))
        self.assertEqual('waiting_quota', store.load_runs(self.runs)[0]['status'])
        self.assertFalse(registry._abandoned(dict(run, pid=987654321)))
        job = safety.read(life.queue_path('task'))
        self.assertEqual('waiting', life.quota_ready(job, 1159))
        self.assertEqual('ready', life.quota_ready(json.loads(json.dumps(job)), 1160))
        self.assertEqual('waiting_quota', life.logical_status('task'))
        self.assertEqual(['task'], [r['name'] for r in a.registry(self.runs)])

    def test_missing_reset_cap_past_reset_and_missing_session_keep_failure(self):
        for reset in (None, 999, 1000, 1000 + 21600):
            self.assertFalse(self.schedule(reset))
        with mock.patch.dict(os.environ, GODFATHER_QUOTA_MAX_WAIT='0'):
            self.assertFalse(self.schedule())
        self.run.pop('session_id')
        self.assertFalse(self.schedule())
        self.assertEqual('quota', safety.read(str(self.base) + '.run.json')['status'])

    def test_configured_cap_persists_for_repeated_quota_waits(self):
        self.assertTrue(life.schedule_quota(self.base, self.run, time.time() + 100, max_wait=300))
        job = safety.read(life.queue_path('task'))
        self.assertEqual(300, job['max_wait'])
        job['resume_at'] = time.time() - 1
        safety.atomic(life.queue_path('task'), job)
        def limited_again(*args, **kwargs):
            current = safety.read(str(self.base) + '.run.json')
            safety.registry_write(str(self.base) + '.run.json', dict(current, status='quota',
                                  failure=dict(resets_at=time.time() + 1000)))
            return 1
        with mock.patch.object(a, 'nudge_main', side_effect=limited_again):
            self.assertEqual(1, life.execute_quota(self.base))
        self.assertEqual('quota', safety.read(str(self.base) + '.run.json')['status'])
        self.assertEqual(job, safety.read(life.queue_path('task')))

    def test_pending_decision_prevents_automatic_wait(self):
        self.run['questions'] = [dict(id='q', question='Choose a target', answer=None)]
        self.assertFalse(self.schedule())
        self.assertEqual('quota', safety.read(str(self.base) + '.run.json')['status'])

    def test_stop_cancels_wait_and_pending_nudge(self):
        self.assertTrue(self.schedule())
        life.deliver_nudge('task', 'remember the regression')
        life.stop_run('task')
        self.assertEqual('stopped', safety.read(str(self.base) + '.run.json')['status'])
        self.assertEqual('stopped', safety.read(life.queue_path('task'))['status'])
        self.assertFalse(safety.read(str(self.base) + '.nudge.json')['pending'])
        self.assertEqual('cancelled', life.quota_ready(safety.read(life.queue_path('task')), 2000))

    def test_cancelled_wait_does_not_shadow_a_later_explicit_reopen(self):
        self.assertTrue(self.schedule())
        life.stop_run('task')
        safety.control(self.base, lambda data: data.pop('stop_requested', None))
        reopened = dict(self.run, run_id='explicit-nudge', status='done', exit_code=0)
        reopened.pop('stop_requested', None)
        safety.registry_write(str(self.base) + '.run.json', reopened)
        self.assertEqual('done', life.logical_status('task'))
        self.assertEqual('stopped', safety.read(life.queue_path('task'))['status'])

    def test_nudge_is_deferred_and_exact_session_resume_includes_it(self):
        self.assertTrue(self.schedule(time.time() + 100))
        with mock.patch.object(life.subprocess, 'Popen', side_effect=AssertionError('early resume')):
            life.deliver_nudge('task', 'also check the migration')
        job = safety.read(life.queue_path('task'))
        job['resume_at'] = time.time() - 1
        safety.atomic(life.queue_path('task'), job)
        with mock.patch.object(a, 'nudge_main', return_value=0) as resume:
            self.assertEqual(0, life.execute_quota(self.base))
        args = resume.call_args.args[0]
        self.assertEqual('task', args[0])
        self.assertIn('also check the migration', args[1])
        self.assertEqual('saved-session', safety.read(str(self.base) + '.run.json')['session_id'])
        self.assertFalse(safety.read(str(self.base) + '.nudge.json')['pending'])

    def test_replacement_never_resumes(self):
        self.assertTrue(self.schedule())
        safety.atomic(str(self.base) + '.run.json', dict(self.run, run_id='replacement'))
        with mock.patch.object(a, 'nudge_main', side_effect=AssertionError('replacement resumed')):
            self.assertEqual(0, life.execute_quota(self.base))

    def test_structured_preflight_avoids_tmux_and_is_account_scoped(self):
        now = time.time()
        native = dict(self.run, rate_limits_at=now, rate_limits=dict(primary=dict(used_percent=100, resets_at=now + 100)))
        safety.atomic(str(self.base) + '.run.json', native)
        with mock.patch.object(supervisor, 'capture_tui', side_effect=AssertionError('scraped despite structured data')):
            result = supervisor.quota_preflight('codex', '/fake', now=now)
        self.assertEqual(('cooldown', 'structured'), (result['status'], result['source']))
        self.assertEqual(now + 100, result['resets_at'])
        self.assertIsNone(supervisor.structured_quota('codex', self.root / 'other-account', now))
        self.assertIsNone(supervisor.structured_quota('codex', self.root / '.codex', now + 301))

    def test_doctor_reports_sources_and_status_displays_resume_clock(self):
        safety.event(self.base, 'FAILURE', 'quota', category='quota', source='structured')
        safety.event(self.base, 'FAILURE', 'auth', category='auth', source='text')
        self.assertIn('structured=1; text fallback=1', life.doctor_report())
        self.assertIn('no recurring issue found', life.doctor_report())
        self.assertTrue(self.schedule(time.time() + 100))
        with mock.patch.object(sys, 'argv', ['agents-status']), contextlib.redirect_stdout(io.StringIO()) as out:
            a.status_main()
        self.assertIn('waiting for quota, resumes', out.getvalue())

    def test_daemon_reclaims_wait_after_restart_and_dispatches_once(self):
        self.assertTrue(self.schedule(time.time() + 100))
        job = safety.read(life.queue_path('task'))
        job.update(resume_at=time.time() - 1, status='running', pid=987654321, identity='dead')
        safety.atomic(life.queue_path('task'), job)
        def complete(command, **kwargs):
            self.assertEqual([sys.executable, str(Path(life.__file__).resolve()), 'quota', str(self.base)], command)
            current = safety.read(str(self.base) + '.run.json')
            safety.registry_write(str(self.base) + '.run.json', dict(current, status='done', exit_code=0))
            return SimpleNamespace(pid=987654322, poll=lambda: 0)
        with mock.patch.object(a, 'processes', return_value={}), \
                mock.patch.object(life.subprocess, 'Popen', side_effect=complete) as launch, mock.patch.object(life.time, 'sleep'):
            self.assertEqual(0, life.daemon())
        self.assertEqual(1, launch.call_count)
        self.assertEqual('done', safety.read(life.queue_path('task'))['status'])

    def test_plan_executor_waits_for_quota_before_success(self):
        task = dict(run_name='plan-task', task_type='edit', id='task')
        with mock.patch.dict(os.environ, GODFATHER_PLAN_EXEC=''), \
                mock.patch.object(plan.subprocess, 'call', return_value=0), \
                mock.patch.object(life, 'logical_status', return_value='waiting_quota'), \
                mock.patch.object(life, 'wait_run', return_value=('done', False)) as wait:
            self.assertEqual(0, plan.default_executor(task, 'brief', self.root, 0))
        wait.assert_called_once_with('plan-task.try0')

    def test_automatic_resume_does_not_reopen_stopped_or_replaced_run(self):
        for current in (dict(self.run, status='stopped', stop_requested=time.time()), dict(self.run, run_id='new-instance')):
            safety.registry_write(str(self.base) + '.run.json', current)
            with mock.patch.object(a.subprocess, 'call', side_effect=AssertionError('cancelled resume launched')):
                self.assertEqual(0, a.nudge_main(['task', 'resume'], expected_instance='instance'))
            self.assertEqual(current['run_id'], safety.read(str(self.base) + '.run.json')['run_id'])

    def test_grok_native_error_fields_survive_result_extraction(self):
        result = dict(is_error=True, errors=[dict(type='error', code='RESOURCE_EXHAUSTED', message='not an auth error 401')])
        Path(str(self.base) + '.json').write_text(json.dumps(result))
        failure = supervisor.provider_failure(self.base, dict(self.run, cli='grok'))
        self.assertEqual(('quota', 'structured'), (failure['category'], failure['source']))

    def test_terminal_tool_denial_is_not_relabelled_by_stale_auth_warning(self):
        Path(str(self.base) + '.log').write_text('Error: authentication failed: 401\n')
        run = dict(self.run, cli='agy', failure_class='tool_denied',
                   failure_reason='write_file auto-denied in headless mode')
        failure = supervisor.provider_failure(self.base, run)
        self.assertEqual('permission_denied', failure['category'])
        self.assertFalse(router.auth_failure(dict(run, failure=failure), '401'))

    def test_native_quota_window_is_saved_even_on_non_error_frames(self):
        book = stream.Book('codex', started_at=1000)
        book.observe_line(json.dumps(dict(type='event_msg', payload=dict(type='token_count',
                          rate_limits=dict(primary=dict(used_percent=100, resets_at=1100))))), 1000)
        self.assertEqual(1000, book.rate_limits_at)
        self.assertEqual(1100, errors.reset_at(book.rate_limits, 1000))

    def test_review_provider_failure_prefers_result_to_discovery_noise(self):
        result = self.runs / 'task.segment-1.stderr'
        discovery = self.runs / 'task.segment-1.cli.log'
        noise = ('E1111 12:00:01.000001 123 auth.go:1] You are not logged into Antigravity.\n'
                 'E1111 12:00:01.000002 123 cache.go:2] Failed to refresh cache in background: 401\n'
                 'E1111 12:00:01.000003 123 cache.go:3] Failed to get cs path\n'
                 'E1111 12:00:01.000004 123 discovery.go:4] discovery request 401 failed\n'
                 'E1111 12:00:01.000005 123 discovery.go:5] unrelated discovery diagnostic\n')
        discovery.write_text(noise)
        run = dict(self.run, cli='agy', diagnostic_paths=[str(result), str(discovery)], discovery_path=str(discovery))
        for category, message in (('quota', 'RESOURCE_EXHAUSTED; try again in 2 minutes'),
                                  ('capacity', 'Selected model is at capacity'), ('auth', 'authentication failed')):
            for structured in (False, True):
                with self.subTest(category=category, structured=structured):
                    code = dict(quota='RESOURCE_EXHAUSTED', capacity='serverOverloaded', auth='UNAUTHENTICATED')[category]
                    event = dict(type='ERROR_MESSAGE', status='DONE', error=dict(code=code, message=message))
                    line = json.dumps(event) if structured else 'Error: ' + message
                    result.write_text(line + '\n' + json.dumps(dict(type='error', code='unrecognized_diagnostic',
                                      message='unrelated trailing diagnostic')) + '\nError: another unrelated line\n')
                    failure = supervisor.provider_failure(self.base, run)
                    self.assertEqual((category, 'structured' if structured else 'text'),
                                     (failure['category'], failure['source']))
        # A current failing result also outranks quota from an earlier segment.
        Path(str(self.base) + '.log').write_text('Error: quota exhausted\n')
        result.write_text('Error: tool failed\n')
        self.assertEqual('other', supervisor.provider_failure(self.base, run)['category'])
        # Typed provider evidence in a discovery file still beats incidental output.
        discovery.write_text(json.dumps(dict(type='ERROR_MESSAGE', error=dict(
            code='RESOURCE_EXHAUSTED', message='usage limit reached'))))
        failure = supervisor.provider_failure(self.base, run)
        self.assertEqual(('quota', 'structured'), (failure['category'], failure['source']))

    def test_review_ignored_agy_diagnostics_are_not_provider_failures(self):
        log = Path(str(self.base) + '.log')
        log.write_text('E1111 12:00:01.000001 123 auth.go:1] You are not logged into Antigravity.\n'
                       'E1111 12:00:01.000002 123 cache.go:2] Failed to refresh cache in background: 401\n')
        run = dict(self.run, cli='agy')
        progress = a.Progress(run)
        for line in log.read_text().splitlines():
            progress.error(line)
        self.assertEqual([], progress.errors)
        self.assertIsNone(supervisor.provider_failure(self.base, run))

    def test_review_queue_write_failure_leaves_run_outside_waiting(self):
        original = safety.atomic
        def fail_queue(path, data):
            if Path(path) == life.queue_path('task'):
                raise OSError('simulated queue write failure')
            return original(path, data)
        with mock.patch.object(safety, 'atomic', side_effect=fail_queue):
            with self.assertRaisesRegex(OSError, 'queue write failure'):
                self.schedule()
        self.assertEqual('quota', safety.read(str(self.base) + '.run.json')['status'])
        self.assertFalse(life.queue_path('task').exists())

    def test_review_job_is_durable_before_waiting_publication(self):
        def crash_before_publication(path, data):
            job = safety.read(life.queue_path('task'))
            self.assertEqual(('quota', 'instance', 1160), (job['kind'], job['run_id'], job['resume_at']))
            raise SystemExit('simulated crash before run publication')
        with mock.patch.object(safety, 'registry_write', side_effect=crash_before_publication):
            with self.assertRaisesRegex(SystemExit, 'simulated crash'):
                self.schedule()
        self.assertEqual('quota', safety.read(str(self.base) + '.run.json')['status'])
        self.assertTrue(life.queue_path('task').exists())

    def test_review_queue_publication_and_daemon_read_share_lock(self):
        original_write = safety.registry_write
        original_read = safety.read
        def assert_locked():
            with open(str(life.queue_path('task')) + '.lock', 'a') as probe:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def publish(path, data):
            assert_locked()
            return original_write(path, data)
        with mock.patch.object(safety, 'registry_write', side_effect=publish):
            self.assertTrue(self.schedule())
        job = original_read(life.queue_path('task'))
        def read(path):
            if str(path) == str(self.base) + '.run.json':
                assert_locked()
            return original_read(path)
        with mock.patch.object(safety, 'read', side_effect=read):
            self.assertEqual('waiting', life.quota_ready(job, 1000))

    def test_review_important_watch_shows_quota_wait(self):
        self.assertTrue(self.schedule(time.time() + 100))
        self.assertTrue(life.important('QUOTA', 'no fallback model remains', 'task'))
        self.assertTrue(life.important('QUOTA_WAIT', 'waiting for quota', 'task'))
        watch = watch_fixture.launch([str(ROOT / 'bin/agents-watch'), '--important', '--replay',
                                      '--no-heal', '--poll-secs', '.03'], dict(os.environ))
        output = b''
        try:
            output = watch_fixture.read_until(watch, b'QUOTA_WAIT task')
        finally:
            if watch.poll() is None:
                watch.terminate()
            remaining, error = watch.communicate(timeout=5)
            output += remaining
        self.assertIn(b'waiting for quota, resumes', output, error)

    def test_package_inventory_includes_classifier(self):
        self.assertIn("'godfather_errors.py'", (ROOT / 'install.sh').read_text())



class RoutedQuotaTests(unittest.TestCase):
    setUp = fixture.RouterTests.setUp
    config_models = fixture.RouterTests.config_models
    launch = fixture.RouterTests.launch
    calls = fixture.RouterTests.calls

    def test_exhaustion_without_fallback_waits_on_known_reset(self):
        self.config_models(('codex', 'fake'))
        fake = fixture.FAKE.replace("quota = model in", '''
if cli == 'codex':
    print(json.dumps(dict(type='thread.started', thread_id='33333333-3333-3333-3333-333333333333')))
    print(json.dumps(dict(type='turn.failed', error=dict(message='quota exhausted; try again in 2 minutes'))))
    sys.exit(1)
quota = model in''')
        (self.root / 'fake/codex').write_text(fake)
        result = self.launch('wait-reset')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        run = fixture.a.read(self.root / 'runs/wait-reset.run.json')
        self.assertEqual(('waiting_quota', 'quota', 'text'), (run['status'], run['category'], run['source']))
        self.assertGreater(run['resume_at'], time.time() + 160)
        self.assertEqual(1, len(self.calls()))
        job = fixture.a.read(self.root / 'state/godfather/queue/wait-reset.json')
        self.assertEqual(('quota', 'queued'), (job['kind'], job['status']))

    def test_wait_resumes_with_daemon_after_conductor_exits_and_runs_check(self):
        self.config_models(('codex', 'fake'))
        fake = fixture.FAKE.replace("quota = model in", '''
if cli == 'codex' and 'resume' not in args:
    print(json.dumps(dict(type='thread.started', thread_id='33333333-3333-3333-3333-333333333333')))
    print(json.dumps(dict(type='turn.failed', error=dict(message='quota exhausted; try again in 2 minutes'))))
    sys.exit(1)
quota = model in''')
        (self.root / 'fake/codex').write_text(fake)
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '--check',
                                 'echo checked > checked.txt', str(self.prompt), 'durable'],
                                env=dict(self.env, GODFATHER_QUEUE_DAEMON='1'), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.root / 'checked.txt').exists())
        job_path = self.root / 'state/godfather/queue/durable.json'
        job = fixture.a.read(job_path)
        job['resume_at'] = time.time() - 1
        with mock.patch.dict(os.environ, self.env, clear=True):
            safety.atomic(job_path, job)
            life.deliver_nudge('durable', 'Include the queued correction.')
        daemon = subprocess.run([sys.executable, str(ROOT / 'bin/godfather_lifecycle.py'), 'daemon'],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, daemon.returncode, daemon.stdout + daemon.stderr)
        run = fixture.a.read(self.root / 'runs/durable.run.json')
        self.assertEqual('done', run['status'])
        self.assertEqual('checked\n', (self.root / 'checked.txt').read_text())
        self.assertEqual(2, len(self.calls()))
        self.assertIn('resume', self.calls()[1]['args'])
        self.assertIn('33333333-3333-3333-3333-333333333333', self.calls()[1]['args'])
        self.assertIn('Include the queued correction.', self.calls()[1]['prompt'])
        self.assertEqual('done', fixture.a.read(job_path)['status'])
        self.assertEqual('finished', fixture.a.read(self.root / 'runs/durable.route.json')['status'])

    def test_network_retries_once_then_falls_back(self):
        self.config_models(('codex', 'fake'), ('grok', 'backup'))
        fake = fixture.FAKE.replace("quota = model in", '''
if cli == 'codex':
    print(json.dumps(dict(type='turn.failed', error=dict(codexErrorInfo='responseStreamDisconnected', message='network failed'))))
    sys.exit(1)
quota = model in''')
        (self.root / 'fake/codex').write_text(fake)
        result = self.launch('network')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['codex', 'codex', 'grok'], [call['cli'] for call in self.calls()])
        self.assertEqual('network', fixture.a.read(self.root / 'runs/network.run.json')['category'])


    def test_earlier_quota_account_can_wait_after_auth_fallback_is_spent(self):
        self.config_models(('codex', 'fake'), ('grok', 'backup'))
        fake = fixture.FAKE.replace("quota = model in", '''
if cli == 'codex':
    print(json.dumps(dict(type='thread.started', thread_id='33333333-3333-3333-3333-333333333333')))
    print(json.dumps(dict(type='turn.failed', error=dict(message='quota exhausted; try again in 2 minutes'))))
    sys.exit(1)
quota = model in''')
        (self.root / 'fake/codex').write_text(fake)
        result = self.launch('earlier-quota', TASK_AUTH_FAILURE='grok', GODFATHER_QUEUE_DAEMON='1')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        route = fixture.a.read(self.root / 'runs/earlier-quota.route.json')
        self.assertEqual(('waiting_quota', 'earlier-quota'), (route['status'], route['active_name']))
        self.assertEqual('waiting_quota', fixture.a.read(self.root / 'runs/earlier-quota.run.json')['status'])
        again = self.launch('earlier-quota')
        self.assertNotEqual(0, again.returncode)
        self.assertIn('already active', again.stdout + again.stderr)

    def test_review_agy_quota_with_discovery_noise_waits_and_cools_down(self):
        self.config_models(('agy', 'gemini-test'))
        fake = fixture.FAKE.replace("quota = model in", '''
if cli == 'agy':
    log = Path(args[args.index('--log-file') + 1])
    log.write_text('Print mode: conversation=44444444-4444-4444-4444-444444444444\\n'
                   'E1111 12:00:01.000001 123 auth.go:1] You are not logged into Antigravity.\\n'
                   'E1111 12:00:01.000002 123 cache.go:2] Failed to refresh cache in background: 401\\n'
                   'E1111 12:00:01.000003 123 discovery.go:3] unrelated discovery diagnostic\\n')
    print(json.dumps(dict(type='ERROR_MESSAGE', status='DONE', error=dict(
        code='RESOURCE_EXHAUSTED', message='usage limit reached; try again in 2 minutes'))), file=sys.stderr)
    sys.exit(1)
quota = model in''')
        # Keep /usage fixtures working; the injected block runs only for task invocations.
        (self.root / 'fake/agy').write_text(fake)
        result = self.launch('agy-noise', GODFATHER_QUEUE_DAEMON='1')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        run = fixture.a.read(self.root / 'runs/agy-noise.run.json')
        self.assertEqual(('waiting_quota', 'quota', 'structured'),
                         (run['status'], run['category'], run['source']))
        state = fixture.a.read(self.root / 'state/godfather/quota.json')
        self.assertTrue(state['agy:gemini-test']['reset_known'])
        self.assertIn('COOLDOWN agy:gemini-test', (self.root / 'runs/agy-noise.models.log').read_text())
        self.assertTrue((self.root / 'state/godfather/queue/agy-noise.json').exists())



if __name__ == '__main__':
    unittest.main(verbosity=2)
