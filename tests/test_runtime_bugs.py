"""Regressions for incidents observed during real harness supervision."""
import argparse
import json
import os
import signal
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
import godfather_router as router
import godfather_safety as safety
import godfather_policy as policy
import godfather_store as store

BOX = policy.devbox_settings(dict(binary='devbox', detached_error='devbox must be run inside a project'))


class RuntimeBugs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.env = dict(os.environ, HOME=str(self.root), CWD=str(self.root), AGENT_RUNS_DIR=str(self.runs),
                        XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                        PYTHONDONTWRITEBYTECODE='1', GODFATHER_QUEUE_DAEMON='1')
        self.env.pop('GODFATHER_DB', None)
        self.patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root)] + list(args), check=True, capture_output=True, text=True)

    def repo(self):
        self.git('init', '-q')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-qm', 'base')

    def test_empty_finish_fails_before_check_but_preserves_a_pending_decision(self):
        for pending, expected in ((False, 'failed'), (True, 'needs_input')):
            base = str(self.runs / ('finish-' + str(pending)))
            a.write(base + '.run.json', dict(name=Path(base).name, cli='codex', model='gpt-test',
                    cwd=str(self.root), launcher_pid=os.getppid(), pid=123456789, started_at=a.now(),
                    status='running', progress_paths=[]))
            Path(base + '.last.md').write_text(' \n')
            if pending:
                safety.atomic(base + '.control.json', {'questions': [{'answer': None}]})
            with mock.patch.dict(os.environ, {'GODFATHER_CHECK': 'true'}):
                self.assertEqual(0 if pending else 1, a.finish(base, 0))
            run = a.read(base + '.run.json')
            self.assertEqual(expected, run['status'])
            if not pending:
                self.assertEqual('no_output', run['failure_class'])
                self.assertIn('empty', run['failure_reason'])

    def test_dead_undelivered_blocking_fixture_wait_returns(self):
        fixtures = ROOT / 'tests/fixtures/stale-needs-input-undelivered'
        for name in ('polish_final', 'devbox_finish', 'devbox_live_search'):
            for suffix in ('.run.json', '.control.json'):
                data = json.loads((fixtures / (name + suffix)).read_text())
                if suffix == '.run.json':
                    data.update(cwd=str(self.root), prompt_file=str(self.root / 'brief'),
                                discovery_path=str(self.root / 'missing'), storage_path=str(self.root / 'missing'))
                (self.runs / (name + suffix)).write_text(json.dumps(data))
            (self.runs / (name + '.last.md')).write_text('Completed and verified.\n')
            result = subprocess.run([str(ROOT / 'bin/godfather'), 'wait', name, '--timeout', '.2'],
                                    env=self.env, capture_output=True, text=True, timeout=5)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertEqual('failed', a.read(self.runs / (name + '.run.json'))['status'])
            questions = a.read(self.runs / (name + '.control.json'))['questions']
            self.assertFalse(any(q.get('answer') is None or not q.get('delivered') for q in questions))
            self.assertTrue(any(q.get('expired_at') for q in questions))
        # Import and status must resolve the same unanswered shape, including failure.
        for rc, report, expected in ((0, True, 'failed'), (1, True, 'failed'), (None, False, 'failed')):
            directory = self.root / ('import-' + str(rc)); directory.mkdir()
            name = 'devbox_live_search'
            run = json.loads((fixtures / (name + '.run.json')).read_text())
            run.update(exit_code=rc, child_exit_code=rc, ended_at=None, cwd=str(self.root))
            (directory / (name + '.run.json')).write_text(json.dumps(run))
            if report:
                (directory / (name + '.last.md')).write_text('Complete.\n')
            rows = a.registry(directory)
            self.assertEqual(expected, rows[0]['status'])
            self.assertFalse(rows[0].get('pending_decision'))

    def test_worktree_checkout_has_no_timeout_and_streams_progress(self):
        self.repo()
        real = subprocess.run
        calls = []
        def run(command, **kwargs):
            if 'add' in command and 'worktree' in command:
                calls.append(kwargs)
            return real(command, **kwargs)
        with mock.patch.object(router.subprocess, 'run', side_effect=run):
            path, branch = router.create_worktree(self.root, self.runs / 'worktree', 'godfather/big')
        self.assertTrue((path / '.git').is_file())
        self.assertNotIn('timeout', calls[0])
        self.assertNotIn('capture_output', calls[0])
        self.assertNotIn('stderr', calls[0])
        with self.assertRaisesRegex(ValueError, 'refusing existing'):
            router.create_worktree(self.root, path, branch)
        self.assertTrue((path / '.git').is_file())

    def test_devbox_isolation_probe_fails_closed_on_unknown_status(self):
        statuses = [(0, '{}', ''), (0, '[]', ''), (0, 'invalid', ''),
                    (1, '{"attached": false}', 'unavailable'), (1, '', 'connection error')]
        with mock.patch.object(safety, 'devbox_tool', return_value=BOX), \
                mock.patch.object(router.shutil, 'which', return_value='/fake/devbox'):
            for code, output, error in statuses:
                with self.subTest(output=output, error=error), mock.patch.object(router.subprocess, 'run',
                        return_value=subprocess.CompletedProcess([], code, output, error)):
                    with self.assertRaisesRegex(ValueError, '--no-isolate'):
                        router.check_devbox_isolation(self.root)
            with mock.patch.object(router.subprocess, 'run', side_effect=subprocess.TimeoutExpired('devbox', 15)):
                with self.assertRaisesRegex(ValueError, '--no-isolate'):
                    router.check_devbox_isolation(self.root)

    def test_devbox_probe_uses_original_environment_and_allows_unsupported_repos(self):
        with mock.patch.object(safety, 'devbox_tool', return_value=BOX), \
                mock.patch.object(router.shutil, 'which', return_value='/fake/devbox') as which:
            with mock.patch.object(router.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 1, '', 'devbox must be run inside a project\n')) as probe:
                with mock.patch.dict(os.environ, {'GODFATHER_ORIGINAL_PATH': '/conductor/bin'}):
                    router.check_devbox_isolation(self.root)
        self.assertEqual('/conductor/bin', which.call_args.kwargs['path'])
        self.assertEqual('/conductor/bin', probe.call_args.kwargs['env']['PATH'])
        self.assertEqual(self.root, probe.call_args.kwargs['cwd'])
        self.assertEqual(['/fake/devbox', 'status', '--json'], probe.call_args.args[0])
        self.assertEqual('devbox', which.call_args.args[0])
        with mock.patch.object(router.shutil, 'which') as which:
            self.assertIsNone(router.check_devbox_isolation(self.root))
        which.assert_not_called()

    def test_failed_locked_worktree_removes_its_branch_and_partial_checkout(self):
        self.repo()
        real = subprocess.run
        def fail(command, **kwargs):
            if 'add' in command and 'worktree' in command:
                real(command, check=True, capture_output=True)
                self.git('worktree', 'lock', str(self.runs / 'partial'))
                return subprocess.CompletedProcess(command, 1)
            return real(command, **kwargs)
        with mock.patch.object(router.subprocess, 'run', side_effect=fail):
            with self.assertRaisesRegex(ValueError, 'git exited 1'):
                router.create_worktree(self.root, self.runs / 'partial', 'godfather/partial')
        self.assertFalse((self.runs / 'partial').exists())
        self.assertNotIn('godfather/partial', self.git('branch', '--list').stdout)
        self.assertNotIn('/partial', self.git('worktree', 'list', '--porcelain').stdout)

    def test_worktree_and_plan_suppress_git_hooks(self):
        import godfather_plan as plan
        self.repo()
        hooks = self.root / '.git/hooks'
        hooks.mkdir(parents=True, exist_ok=True)
        marker = self.root / 'hook-ran'
        for name in ('post-checkout', 'post-merge', 'prepare-commit-msg', 'commit-msg'):
            hook = hooks / name
            hook.write_text('#!/bin/sh\necho ' + name + ' >> "' + str(marker) + '"\nexit 1\n')
            hook.chmod(0o755)
        path, branch = router.create_worktree(self.root, self.runs / 'hooked', 'godfather/hooked')
        self.assertTrue((path / '.git').is_file())
        self.assertEqual('godfather/hooked', branch)
        self.assertFalse(marker.exists())
        self.assertEqual(0, plan._git(self.root, 'status', '--porcelain').returncode)
        plan_file = self.root / 'plan.toml'
        plan_file.write_text('schema = 1\n[plan]\nname = "hooked-plan"\nrepo = "' + str(self.root) + '"\nmerge = true\n'
                             '[[tasks]]\nid = "a"\nbrief = "write a"\ndeps = []\ntask_type = "edit"\n')
        def execute(task, prompt, cwd, attempt):
            (Path(cwd) / 'a.txt').write_text('a\n')
            subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(cwd), 'add', 'a.txt'], check=True)
            subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(cwd), '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.com', 'commit', '-qm', 'a'], check=True)
            return 0
        self.assertEqual(0, plan.run_plan(plan_file, executor=execute))
        self.assertFalse(marker.exists())

    def test_review_fix_requires_review_by_before_provisioning_and_unstarted_scratch_rolls_back(self):
        self.repo()
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--review-fix', '-p', 'task', 'bad-review'],
                                env=dict(self.env, CWD=str(self.root)), capture_output=True, text=True, timeout=5)
        self.assertEqual(2, result.returncode)
        self.assertIn('--review-fix requires --review-by', result.stderr)
        self.assertFalse((self.runs / 'worktrees/bad-review').exists())
        self.assertNotIn('godfather/bad-review', self.git('branch', '--list').stdout)
        plain = self.root / 'plain'
        plain.mkdir()
        (plain / 'file.txt').write_text('keep\n')
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--review-fix', '-p', 'task', 'bad-scratch'],
                                env=dict(self.env, CWD=str(plain)), capture_output=True, text=True, timeout=5)
        self.assertEqual(2, result.returncode)
        self.assertIn('--review-fix requires --review-by', result.stderr)
        self.assertFalse((self.runs / 'scratch/bad-scratch').exists())
        missing = subprocess.run([str(ROOT / 'bin/godfather'), 'run', str(self.root / 'missing.md'), 'unstarted'],
                                 env=dict(self.env, CWD=str(plain)), capture_output=True, text=True, timeout=5)
        self.assertEqual(2, missing.returncode)
        self.assertFalse((self.runs / 'scratch/unstarted').exists())
        existing = self.runs / 'scratch/existing'
        existing.mkdir(parents=True)
        (existing / 'keep.txt').write_text('survives\n')
        with self.assertRaisesRegex(ValueError, 'refusing existing path'):
            safety.copy_scratch(plain, existing)
        self.assertEqual('survives\n', (existing / 'keep.txt').read_text())
        dangling = self.runs / 'scratch/dangling'
        dangling.symlink_to(self.root / 'nonexistent-target')
        with self.assertRaisesRegex(ValueError, 'refusing existing path'):
            safety.copy_scratch(plain, dangling)
        self.assertTrue(dangling.is_symlink())

    def test_pending_dependency_names_wait_then_launch_or_fail_with_timeout(self):
        import godfather_lifecycle as life
        for flag in ('--after', '--after-any'):
            result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', flag, 'future',
                                     '--dependency-timeout', '2', '--container', '-p', 'task', flag[2:]],
                                    env=dict(self.env, CWD=str(self.root)), capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stderr)
            job = safety.read(life.queue_path(flag[2:]))
            self.assertIn('--container', job['argv'])
            self.assertEqual('waiting', life.dependency_gate(job, job['created'] + 1)[0])
            gate, detail = life.dependency_gate(job, job['created'] + 3)
            self.assertEqual('dependency_failed', gate)
            self.assertIn('future', detail)
            self.assertIn('timed out', detail)
            with mock.patch.object(life, 'dependency_instance', return_value=('new', job['created'] + .1)), mock.patch.object(life, 'logical_status', return_value='done'):
                self.assertEqual('ready', life.dependency_gate(job, job['created'] + 1)[0])
            with mock.patch.object(life, 'dependency_instance', return_value=('new', job['created'] + .1)), mock.patch.object(life, 'logical_status', return_value='failed'):
                self.assertEqual('ready' if flag == '--after-any' else 'dependency_failed', life.dependency_gate(job)[0])

    def test_reused_parent_dependency_binds_new_instance(self):
        import godfather_lifecycle as life
        base = self.runs / 'parent'
        old = dict(name='parent', run_id='old', status='done', exit_code=0, started_at=time.time() - 60,
                   cli='codex', cwd=str(self.root), progress_paths=[])
        safety.atomic(str(base) + '.run.json', old)
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--after', 'parent', '-p', 'task', 'dependent'],
                                env=dict(self.env, CWD=str(self.root)), capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        job = safety.read(life.queue_path('dependent'))
        self.assertEqual('waiting', life.dependency_gate(job)[0])
        self.assertFalse((self.runs / 'dependent.run.json').exists())
        # A new parent starts after enqueue; its live instance must still block.
        parent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.4)'], start_new_session=True)
        try:
            new = dict(old, run_id='new', status='running', pid=parent.pid, started_at=time.time())
            safety.atomic(str(base) + '.run.json', new)
            self.assertEqual('waiting', life.dependency_gate(job)[0])
            self.assertEqual('new', safety.read(life.queue_path('dependent'))['dependency_instances']['parent'])
            parent.wait(timeout=3)
            safety.atomic(str(base) + '.run.json', dict(new, status='done', exit_code=0))
            self.assertEqual('ready', life.dependency_gate(job)[0])
            safety.atomic(str(base) + '.run.json', dict(new, run_id='third', status='done'))
            self.assertEqual('dependency_failed', life.dependency_gate(job)[0])
        finally:
            if parent.poll() is None:
                parent.terminate()
            parent.wait(timeout=3)

    def test_running_parent_binds_at_enqueue_and_survives_daemon_restart(self):
        import godfather_lifecycle as life
        worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        daemon = None
        try:
            base = self.runs / 'parent'
            run = dict(name='parent', run_id='running-parent', status='running', pid=worker.pid,
                       started_at=time.time() - 60, cli='codex', cwd=str(self.root), progress_paths=[])
            safety.atomic(str(base) + '.run.json', run)
            result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--after', 'parent', '-p', 'task', 'bound'],
                                    env=dict(self.env, CWD=str(self.root)), capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stderr)
            path = life.queue_path('bound')
            # This must already be durable before the first daemon/gate invocation.
            job = safety.read(path)
            self.assertEqual({'parent': 'running-parent'}, job['dependency_instances'])
            job['argv'] = ['status', '--json']
            safety.atomic(path, job)
            def launch_daemon():
                return subprocess.Popen([sys.executable, str(ROOT / 'bin/godfather_lifecycle.py'), 'daemon'],
                                        env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            daemon = launch_daemon()
            time.sleep(.3)
            self.assertEqual('queued', safety.read(path)['status'])
            daemon.terminate()
            daemon.communicate(timeout=3)
            safety.atomic(str(base) + '.run.json', dict(run, status='done', exit_code=0))
            # Restart with the parent now finished; the persisted binding releases it.
            daemon = launch_daemon()
            daemon.communicate(timeout=5)
            job = safety.read(path)
            self.assertEqual('running-parent', job['dependency_instances']['parent'])
            self.assertNotEqual('queued', job['status'])
            self.assertTrue(any(e['kind'] == 'START' and e['name'] == 'bound' for e in store.events_after(self.runs, 0)))
        finally:
            if daemon is not None and daemon.poll() is None:
                daemon.terminate()
                daemon.communicate(timeout=3)
            if worker.poll() is None:
                worker.terminate()
            worker.wait(timeout=3)

    def test_enqueue_binds_every_nonterminal_dependency(self):
        import godfather_lifecycle as life
        for status in ('running', 'queued', 'provisioning', 'needs_input', 'checking',
                       'reviewing', 'healing', 'retrying', 'merging'):
            with self.subTest(status=status), mock.patch.object(life, 'logical_status', side_effect=lambda name: status if name == 'parent' else 'unknown'), mock.patch.object(life, 'ensure_daemon'):
                name = 'child-' + status
                args = argparse.Namespace(name=name, after='parent', after_any=None, prompt='task',
                                          check=None, review_by=None, max_fix_rounds=None, review_fix=False,
                                          wait_lock=False)
                setattr(args, 'self', False)
                with mock.patch.object(life, 'dependency_instance', return_value=('live', time.time() - 60)):
                    life.enqueue(args)
                job = safety.read(life.queue_path(name))
                self.assertEqual('live', job['dependency_instances']['parent'])
                with mock.patch.object(life, 'dependency_instance', return_value=('live', time.time() - 60)):
                    self.assertEqual('waiting', life.dependency_gate(job)[0])

    def test_queued_dependency_keeps_identity_through_router_start(self):
        import godfather_lifecycle as life
        def enqueue(name, parent):
            result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--after', parent, '-p', 'task', name],
                                    env=dict(self.env, CWD=str(self.root)), capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stderr)
            return safety.read(life.queue_path(name))
        parent = enqueue('parent', 'future')
        child = enqueue('child', 'parent')
        self.assertEqual(parent['run_id'], child['dependency_instances']['parent'])
        route = dict(name='parent', run_id=parent['run_id'], started_at=a.now(), status='running', router_pid=os.getpid())
        parent.update(status='running', pid=os.getpid())
        safety.atomic(life.queue_path('parent'), parent)
        safety.atomic(self.runs / 'parent.route.json', route)
        self.assertEqual('waiting', life.dependency_gate(child)[0])
        route.update(status='done', result='done')
        safety.atomic(self.runs / 'parent.route.json', route)
        self.assertEqual('ready', life.dependency_gate(child)[0])

    def test_watch_restart_deduplicates_synthesized_and_sequenced_events(self):
        base = self.runs / 'watching'
        log = Path(str(base) + '.jsonl')
        log.write_text('{"type":"error","message":"real failure"}\n')
        safety.atomic(str(base) + '.run.json', dict(name='watching', cli='grok', model='fake',
                      cwd=str(self.root), status='running', run_id='watching', pid=os.getpid(),
                      started_at=time.time(), progress_paths=[str(log)]))
        def watch(*flags):
            proc = subprocess.Popen([str(ROOT / 'bin/agents-watch'), '--no-heal', '--consumer', 'watch', '--poll-secs', '.02'] + list(flags),
                                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(1)
            proc.terminate()
            output, error = proc.communicate(timeout=3)
            self.assertEqual(b'', error)
            return output
        store.open_cursor(self.runs, 'watch', create=True)
        safety.event(base, 'NEEDS_INPUT', ': Choose?')
        safety.event(base, 'ESCALATE', 'needs attention')
        safety.event(base, 'DONE', 'finished')
        first = watch()
        for kind in (b'NEEDS_INPUT', b'ESCALATE', b'DONE', b'ERROR'):
            self.assertIn(kind, first)
        second = watch()
        for kind in (b'NEEDS_INPUT', b'ESCALATE', b'DONE', b'ERROR'):
            self.assertNotIn(kind, second)
        replay = watch('--replay')
        for kind in (b'NEEDS_INPUT', b'ESCALATE', b'DONE', b'ERROR'):
            self.assertIn(kind, replay)

    def test_concurrent_watch_consumers_and_restart(self):
        worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        def stop_worker():
            if worker.poll() is None:
                worker.terminate()
            worker.wait(timeout=3)
        self.addCleanup(stop_worker)
        watchers = []
        def launch(consumer):
            proc = subprocess.Popen([str(ROOT / 'bin/agents-watch'), '--no-heal', '--consumer', consumer,
                                     '--poll-secs', '.02', '--stall-secs', '.15'], env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            watchers.append(proc)
            return proc
        def cleanup():
            for proc in watchers:
                if proc.poll() is None:
                    proc.terminate()
                proc.communicate(timeout=3)
        self.addCleanup(cleanup)
        for consumer in ('one', 'two'):
            store.open_cursor(self.runs, consumer, create=True)
        first, second = launch('one'), launch('two')
        time.sleep(.25)
        base = self.runs / 'live'
        log = Path(str(base) + '.jsonl')
        log.write_text('{"type":"error","message":"real failure"}\n')
        run = dict(name='live', cli='grok', model='fake', cwd=str(self.root), status='running',
                   run_id='live', pid=worker.pid, started_at=time.time(), progress_paths=[str(log)])
        safety.atomic(str(base) + '.run.json', run)
        safety.event(base, 'NEEDS_INPUT', ': decision')
        time.sleep(.9)
        safety.atomic(str(base) + '.run.json', dict(run, status='done', exit_code=0, ended_at=a.now()))
        time.sleep(.3)
        for proc in (first, second):
            proc.terminate()
            out, err = proc.communicate(timeout=3)
            self.assertEqual(b'', err)
            for kind in (b'START', b'ERROR', b'DONE', b'NEEDS_INPUT'):
                self.assertEqual(1, out.count(kind + b' live'), out)
            self.assertIn(b'STALL live', out)
            self.assertLessEqual(out.count(b'STALL live'), 3, out)
        restarted = launch('one')
        time.sleep(.3)
        restarted.terminate()
        out, err = restarted.communicate(timeout=3)
        self.assertEqual(b'', err)
        self.assertEqual(b'', out)

    def test_default_watch_restart_from_new_shell_does_not_repeat_live_errors(self):
        base = self.runs / 'still-live'
        log = Path(str(base) + '.jsonl')
        log.write_text('{"type":"error","message":"real failure"}\n')
        safety.atomic(str(base) + '.run.json', dict(name=base.name, cli='grok', model='fake',
                      cwd=str(self.root), status='running', run_id='still-live', pid=os.getpid(),
                      started_at=time.time(), progress_paths=[str(log)]))
        command = [str(ROOT / 'bin/agents-watch'), '--no-heal', '--poll-secs', '.02']
        # Each wrapper is a new invoking shell/process. It only stops its own watcher.
        script = ("import os,selectors,subprocess,sys,time\n"
                  "sys.path.insert(0," + repr(str(ROOT / 'bin')) + ")\n"
                  "import godfather_store as store\n"
                  "p=subprocess.Popen(" + repr(command) + ",stdout=subprocess.PIPE,stderr=subprocess.PIPE)\n"
                  "out=b''\n"
                  "try:\n"
                  " deadline=time.monotonic()+8\n"
                  " with selectors.DefaultSelector() as selector:\n"
                  "  selector.register(p.stdout,selectors.EVENT_READ)\n"
                  "  if sys.argv[1]=='first':\n"
                  "   while b'START still-live' not in out or b'ERROR still-live' not in out:\n"
                  "    assert time.monotonic()<deadline, out\n"
                  "    for key,_ in selector.select(.05): out+=os.read(key.fd,65536)\n"
                  "  barrier=store.add_event(store.runs_root(),None,'POLL','refresh cache in background (poll barrier)')\n"
                  "  while True:\n"
                  "   assert time.monotonic()<deadline, out\n"
                  "   conn=store.connect()\n"
                  "   try:\n"
                  "    row=conn.execute('SELECT c.seq FROM cursors c JOIN watch_leases l ON l.subscriber=c.subscriber WHERE l.pid=?',(p.pid,)).fetchone()\n"
                  "    if row and row[0]>=barrier: break\n"
                  "   finally: conn.close()\n"
                  "   for key,_ in selector.select(.03): out+=os.read(key.fd,65536)\n"
                  "finally:\n"
                  " p.terminate(); tail,err=p.communicate(timeout=3); out+=tail\n"
                  "sys.stdout.buffer.write(out); sys.stderr.buffer.write(err)\n")
        def watch(phase):
            result = subprocess.run([sys.executable, '-c', script, phase], env=self.env, capture_output=True, timeout=12)
            self.assertEqual((0, b''), (result.returncode, result.stderr))
            return result.stdout
        # Observe output and durable polling rather than assuming startup fits .7s.
        first = watch('first')
        self.assertIn(b'START still-live', first)
        self.assertIn(b'ERROR still-live', first)
        self.assertEqual(b'', watch('restart'))

    def test_default_watch_slots_reclaim_without_replay_across_ten_restarts(self):
        watchers = []
        def launch(*flags):
            proc = subprocess.Popen([str(ROOT / 'bin/agents-watch'), '--no-heal', '--poll-secs', '.02',
                                     '--stall-secs', '99999', *flags], env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            watchers.append(proc)
            return proc
        def stop(proc):
            if proc.poll() is None:
                proc.kill()  # test-owned watcher; exercise stale lease reclamation
            return proc.communicate(timeout=3)
        def cleanup():
            for proc in watchers:
                stop(proc)
        self.addCleanup(cleanup)
        def leased(proc, slot):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                conn = store.connect(self.runs)
                try:
                    row = conn.execute('SELECT pid FROM watch_leases WHERE subscriber=?', (slot,)).fetchone()
                finally:
                    conn.close()
                if row and row['pid'] == proc.pid:
                    return
                self.assertIsNone(proc.poll(), proc.communicate() if proc.poll() is not None else '')
                time.sleep(.02)
            self.fail('watcher did not lease ' + slot)
        first = launch()
        leased(first, 'watch')
        second = launch()
        leased(second, 'watch-2')
        base = self.runs / 'live'
        log = Path(str(base) + '.jsonl')
        log.write_text('{"type":"error","message":"real failure"}\n')
        run = dict(name='live', cli='grok', model='fake', cwd=str(self.root), status='running',
                   run_id='live', pid=os.getpid(), started_at=time.time(), progress_paths=[str(log)])
        safety.atomic(str(base) + '.run.json', run)
        safety.event(base, 'NEEDS_INPUT', ': decision')
        time.sleep(.8)
        out, err = stop(first)
        self.assertEqual(b'', err)
        for kind in (b'START', b'ERROR', b'NEEDS_INPUT'):
            self.assertEqual(1, out.count(kind + b' live'), out)
        replacement = launch()
        leased(replacement, 'watch')
        time.sleep(.3)
        safety.event(base, 'DONE', 'completed segment')
        time.sleep(.4)
        out, err = stop(replacement)
        self.assertEqual(b'', err)
        self.assertEqual(1, out.count(b'DONE live'), out)
        for kind in (b'START', b'ERROR', b'NEEDS_INPUT'):
            self.assertNotIn(kind + b' live', out)
        # The run stays live, so restarts must also deduplicate synthesized output.
        conn = store.connect(self.runs)
        delivery_count = conn.execute("SELECT count(*) FROM meta WHERE key LIKE 'delivery:%'").fetchone()[0]
        conn.close()
        for _ in range(10):
            replacement = launch()
            leased(replacement, 'watch')
            time.sleep(.15)
            out, err = stop(replacement)
            self.assertEqual((b'', b''), (out, err))
        out, err = stop(second)
        self.assertEqual(b'', err)
        for kind in (b'START', b'ERROR', b'NEEDS_INPUT', b'DONE'):
            self.assertEqual(1, out.count(kind + b' live'), out)
        conn = store.connect(self.runs)
        try:
            self.assertEqual(['watch', 'watch-2'], [r[0] for r in conn.execute('SELECT subscriber FROM cursors ORDER BY subscriber')])
            self.assertEqual(2, conn.execute('SELECT count(*) FROM watch_leases').fetchone()[0])
            self.assertEqual(delivery_count, conn.execute("SELECT count(*) FROM meta WHERE key LIKE 'delivery:%'").fetchone()[0])
        finally:
            conn.close()

    def test_explicit_watch_consumer_refuses_live_holder(self):
        first = subprocess.Popen([str(ROOT / 'bin/agents-watch'), '--no-heal', '--consumer', 'named',
                                  '--poll-secs', '.02'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                conn = store.connect(self.runs)
                try:
                    row = conn.execute("SELECT pid FROM watch_leases WHERE subscriber='named'").fetchone()
                finally:
                    conn.close()
                if row and row['pid'] == first.pid:
                    break
                time.sleep(.02)
            self.assertIsNotNone(row)
            for flag in ('--consumer', '--cursor'):
                result = subprocess.run([str(ROOT / 'bin/agents-watch'), '--no-heal', flag, 'named'],
                                        env=self.env, capture_output=True, timeout=3)
                self.assertEqual(2, result.returncode)
                self.assertIn(b'already held by a live watcher', result.stderr)
        finally:
            if first.poll() is None:
                first.terminate()
            first.communicate(timeout=3)

    def test_known_noise_is_downgraded_without_hiding_real_failures(self):
        import godfather_stream as stream
        (policy.config_dir() / 'godfather.toml').parent.mkdir(parents=True, exist_ok=True)
        (policy.config_dir() / 'godfather.toml').write_text('[watch]\noptional_mcp = ["mcp.example.com"]\n')
        cases = [
            ('agy', 'Unable to start git event watcher: failed to initialize', 'warning'),
            ('codex', 'ERROR: Phase 2 no changes', 'info'),
            ('codex', 'MCP mcp.example.com startup failed: connection refused', 'warning'),
            ('grok', 'write_stdin failed: Unknown process id 123', 'warning'),
            ('codex', 'permission denied writing source file', 'error'),
            ('codex', 'MCP required.example.test startup failed: connection refused', 'error'),
            ('grok', 'authentication failed', 'error'),
            ('codex', 'Phase 2 no changes; permission denied committing', 'error'),
            ('grok', 'quota exhausted', 'error')]
        for cli, message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(expected, stream.diagnostic_level(message))
                progress = a.Progress(dict(cli=cli, started_at=time.time()))
                progress.error(message)
                self.assertEqual(expected == 'error', bool(progress.errors))
                frame = dict(type='ERROR_MESSAGE', content=message) if cli == 'agy' else dict(type='error', message=message)
                beat = stream.classify(cli, frame)
                self.assertEqual('error' if expected == 'error' else 'stream', beat['kind'])

    def test_run_outlives_launching_shell_and_closed_output(self):
        fake = self.root / 'codex'
        fake.write_text('#!/usr/bin/env python3\nimport os,sys,time\nfrom pathlib import Path\n'
                        'if sys.argv[1:] == ["login","status"]: sys.exit(0)\n'
                        'sys.stdin.read()\nPath(os.environ["HOME"],"ready").touch()\n'
                        'time.sleep(2)\nPath(sys.argv[sys.argv.index("-o")+1]).write_text("Finished after shell exit.\\n")\n')
        fake.chmod(0o700)
        config = self.root / 'config/godfather/models.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[[models]]\nbackend = "codex"\nmodel = "fake"\neffort = "high"\n')
        env = dict(self.env, CWD=str(self.root), CODEX_BIN=str(fake), GODFATHER_QUOTA_PROBE='0')
        script = ('import os,subprocess,time,signal\n'
                  'signal.signal(signal.SIGHUP, signal.SIG_DFL)\n'
                  'p=subprocess.Popen(' + repr([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '-p', 'task', 'orphan']) + ', stdout=subprocess.PIPE,stderr=subprocess.PIPE)\n'
                  'from pathlib import Path\n'
                  'deadline=time.monotonic()+8\n'
                  'while not Path(os.environ["HOME"],"ready").exists() and time.monotonic()<deadline: time.sleep(.02)\n'
                  'time.sleep(30)\n')
        shell = subprocess.Popen([sys.executable, '-c', script], env=env, start_new_session=True)
        self.addCleanup(lambda: shell.poll() is None and shell.terminate())
        deadline = time.monotonic() + 8
        while not (self.root / 'ready').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue((self.root / 'ready').exists())
        self.assertNotEqual(os.getpgrp(), shell.pid)
        os.killpg(shell.pid, signal.SIGHUP)
        self.assertEqual(-1, shell.wait(timeout=10))
        from godfather_lifecycle import wait_run
        self.assertEqual(('done', False), wait_run('orphan', 10))
        self.assertEqual('Finished after shell exit.', (self.runs / 'orphan.last.md').read_text().strip())

    def test_launcher_group_sigterm_detaches_and_cleans_attach_files(self):
        import signal
        from godfather_lifecycle import wait_run
        fake = self.root / 'fake-codex'
        fake.write_text('#!/usr/bin/env python3\nimport os,sys,time\nfrom pathlib import Path\n'
                        'if sys.argv[1:] == ["login","status"]: sys.exit(0)\n'
                        'sys.stdin.read()\nPath(os.environ["HOME"],"ready").touch()\n'
                        'time.sleep(2)\nPath(sys.argv[sys.argv.index("-o")+1]).write_text("Completed after detach.\\n")\n')
        fake.chmod(0o700)
        config = self.root / 'config/godfather/models.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[[models]]\nbackend = "codex"\nmodel = "fake"\neffort = "high"\n')
        env = dict(self.env, CWD=str(self.root), CODEX_BIN=str(fake), GODFATHER_QUOTA_PROBE='0')
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=sig):
                ready = self.root / 'ready'
                ready.unlink(missing_ok=True)
                name = 'detach-' + str(sig)
                command = [str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '-p', 'task', name]
                # A supervised suite inherits ignored HUP. The expendable
                # launching process must restore defaults to exercise death.
                script = ('import subprocess,time,signal; '
                          'signal.signal(' + str(int(sig)) + ', signal.SIG_DFL); '
                          'subprocess.Popen(' + repr(command) +
                          ', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(30)')
                shell = subprocess.Popen([sys.executable, '-c', script], env=env, start_new_session=True)
                try:
                    deadline = time.monotonic() + 8
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.exists())
                    self.assertNotEqual(os.getpgrp(), shell.pid)
                    os.killpg(shell.pid, sig)  # Only the launch group this test created.
                    shell.wait(timeout=3)
                    self.assertEqual(-sig, shell.returncode)
                    self.assertEqual(('done', False), wait_run(name, 8))
                    run = safety.read(self.runs / (name + '.run.json'))
                    self.assertEqual(('done', 0), (run['status'], run['exit_code']))
                    self.assertEqual([], list(self.runs.glob('.' + name + '.attach-*')))
                finally:
                    if shell.poll() is None:
                        shell.terminate()
                    shell.wait(timeout=3)

    def test_stop_reparented_session_child_escalates_and_verifies(self):
        import signal
        import godfather_lifecycle as life
        for exit_parent in (False, True):
            with self.subTest(exit_parent=exit_parent):
                marker = self.root / ('detached-' + str(exit_parent) + '.json')
                launcher = subprocess.Popen([sys.executable, str(ROOT / 'tests/fixtures/detached-session/worker.py'),
                                             str(marker)], start_new_session=True)
                descendant = None
                identity = None
                try:
                    deadline = time.monotonic() + 3
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(marker.exists())
                    descendant = json.loads(marker.read_text())['pid']
                    snapshot = a.processes()
                    identity = snapshot[descendant]['identity']
                    self.assertNotEqual(launcher.pid, snapshot[descendant]['ppid'])
                    self.assertNotEqual(launcher.pid, snapshot[descendant]['pgid'])
                    self.assertEqual(launcher.pid, snapshot[descendant]['sid'])
                    base = self.runs / ('detached-' + str(exit_parent))
                    run = dict(name=base.name, run_id=base.name, cli='codex', cwd=str(self.root), status='running',
                               pid=launcher.pid, pid_identity=snapshot[launcher.pid]['identity'],
                               process_session_id=launcher.pid, process_group_id=launcher.pid, started_at=a.now())
                    safety.atomic(str(base) + '.run.json', run)
                    if exit_parent:
                        marker.with_suffix('.release').touch()
                        launcher.wait(timeout=3)
                        self.assertEqual('running', a.reconcile_run(run)['status'])
                    self.assertEqual(0, life.stop_run(base.name))
                    launcher.wait(timeout=3)
                    self.assertFalse(a.alive(descendant, identity))
                    self.assertEqual(('stopped', False), life.wait_run(base.name, .1))
                finally:
                    # These identities belong only to processes this test spawned.
                    if descendant and a.alive(descendant, identity):
                        os.kill(descendant, signal.SIGKILL)
                    if launcher.poll() is None:
                        launcher.kill()
                    launcher.wait(timeout=3)

    def test_failed_background_nudge_preserves_live_run(self):
        import godfather_lifecycle as life
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.8)'], start_new_session=True)
        try:
            base = self.runs / 'live-nudge'
            run = dict(name=base.name, run_id='live', cli='codex', status='running', pid=child.pid,
                       pid_identity=a.processes()[child.pid]['identity'], started_at=a.now(), cwd=str(self.root))
            safety.atomic(str(base) + '.run.json', run)
            request = dict(id='nudge', run_id='live', message='Continue', pending=True)
            safety.atomic(str(base) + '.nudge.json', request)
            with mock.patch.object(a, 'owners_alive', side_effect=[False, True]), mock.patch.object(a, 'nudge_main', side_effect=ValueError('resume failed')):
                self.assertEqual(2, life.execute_nudge(base))
            self.assertEqual('running', safety.read(str(base) + '.run.json')['status'])
            self.assertNotIn('exit_code', safety.read(str(base) + '.run.json'))
            self.assertEqual(2, safety.read(str(base) + '.nudge.json')['exit_code'])
            child.wait(timeout=3)
            safety.atomic(str(base) + '.nudge.json', dict(request, pending=True))
            with mock.patch.object(a, 'nudge_main', return_value=2):
                life.execute_nudge(base)
            self.assertEqual('failed', safety.read(str(base) + '.run.json')['status'])
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)

    def test_nudge_returns_on_delivery_and_wait_tracks_background_resume(self):
        from godfather_lifecycle import wait_run
        fake = self.root / 'codex'
        fake.write_text('#!/usr/bin/env python3\nimport os,sys,time\nfrom pathlib import Path\n'
                        'sys.stdin.read()\nPath(os.environ["HOME"],"resumed").touch()\n'
                        'time.sleep(3)\nPath(sys.argv[sys.argv.index("-o")+1]).write_text("Resume done.\\n")\n')
        fake.chmod(0o700)
        base = self.runs / 'nudge-me'
        run = dict(name=base.name, cli='codex', model='fake', cwd=str(self.root), run_id='original',
                   status='done', started_at=time.time(), exit_code=0, progress_paths=[],
                   session_id='33333333-3333-3333-3333-333333333333', launcher_env={'CODEX_BIN': str(fake)})
        safety.atomic(str(base) + '.run.json', run)
        started = time.monotonic()
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'nudge', base.name, 'Continue'],
                                env=self.env, capture_output=True, text=True, timeout=2)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn('queued', result.stdout)
        self.assertEqual(('running', True), wait_run(base.name, .01))
        self.assertEqual(('done', False), wait_run(base.name, 10), Path(str(base) + '.nudge.log').read_text())
        self.assertTrue((self.root / 'resumed').exists())
        self.assertEqual('Resume done.', Path(str(base) + '.last.md').read_text().strip())
        from godfather_lifecycle import stop_run
        stop_run(base.name)
        resumed = subprocess.run([str(ROOT / 'bin/godfather'), 'nudge', '--wait', base.name, 'Continue after stop'],
                                 env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual('done', safety.read(str(base) + '.run.json')['status'])
        safety.control(base, lambda data: data.update(questions=[{'id':'q','answer':None}]))
        rejected = subprocess.run([str(ROOT / 'bin/godfather'), 'nudge', base.name, 'Continue'], env=self.env,
                                  capture_output=True, text=True, timeout=2)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn('pending question', rejected.stderr)

    def test_done_delivery_uses_identity_even_when_legacy_metadata_is_incomplete(self):
        safety.atomic(self.runs / 'old.run.json', dict(name='old', run_id='old', cli='grok', model='fake',
                      status='done', exit_code=0, started_at=time.time() - 5, ended_at=None, progress_paths=[]))
        outputs = []
        for _ in range(2):
            proc = subprocess.Popen([str(ROOT / 'bin/agents-watch'), '--no-heal', '--consumer', 'watch', '--poll-secs', '.03'],
                                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(1)
            proc.terminate()
            out, err = proc.communicate(timeout=3)
            self.assertEqual(b'', err)
            outputs.append(out)
        self.assertIn(b'DONE old', outputs[0])
        self.assertNotIn(b'DONE old', outputs[1])

    def test_grok_result_uses_the_same_noise_classifier(self):
        base = self.runs / 'grok-noise'
        for message, fatal in [('write_stdin failed: Unknown process id 123', False),
                               ('permission denied', True)]:
            Path(str(base) + '.stream.jsonl').write_text('\n'.join(json.dumps(frame) for frame in
                [dict(type='error', message=message), dict(type='text', data='Final reply.'), dict(type='end')]) + '\n')
            result = subprocess.run([sys.executable, str(ROOT / 'bin/agent_oversight.py'), 'grok-result', str(base)],
                                    env=self.env, capture_output=True, text=True, timeout=3)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(fatal, safety.read(str(base) + '.json')['is_error'])
