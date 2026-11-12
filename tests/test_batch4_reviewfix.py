"""Regressions for all seven batch 4 review findings; only fake CLIs run."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
import time
import unittest
from unittest import mock

import test_router as fixtures
import test_features as features
import test_plan as plans
import test_batch4 as batch
import agent_oversight as a
import godfather_lifecycle as life
import godfather_mcp as mcp
import godfather_plan as plan
import godfather_plan_run as runner
import godfather_router as router
import godfather_safety as safety
import godfather_store as store

ROOT = fixtures.ROOT


class IntermediateStates(unittest.TestCase):
    setUp = fixtures.RouterTests.setUp

    def set_record(self, name, status, **fields):
        record = dict(name=name, run_id=name, cli='codex', model='fake', cwd=str(self.root),
                      started_at=a.now(), status=status, exit_code=None, ended_at=None,
                      pid=os.getpid(), launcher_pid=os.getpid(), progress_paths=[])
        record.update(fields)
        (self.root / 'runs').mkdir(exist_ok=True)
        store.connect(self.root / 'runs').close()
        safety.atomic(str(self.root / 'runs' / name) + '.run.json', record)
        return record

    def test_mcp_never_journals_intermediate_done_and_emits_real_done_once(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            for status in store.NONTERMINAL:
                self.set_record(status, status)
            result = mcp.events()
            self.assertFalse(any(event['kind'] == 'DONE' for event in result['events']))
            record = a.read(self.root / 'runs/retrying.run.json')
            record.update(status='done', exit_code=0, ended_at=a.now())
            safety.atomic(self.root / 'runs/retrying.run.json', record)
            finished = mcp.events(result['cursor'])
            done = [event for event in finished['events'] if event['kind'] == 'DONE']
            self.assertEqual(['retrying'], [event['name'] for event in done])
            self.assertFalse(mcp.events(finished['cursor'])['events'])

    def test_watch_never_announces_intermediate_done_then_emits_completion(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            for status in store.NONTERMINAL:
                self.set_record(status, status)
        watcher = subprocess.Popen([str(ROOT / 'bin/godfather'), 'watch', '--no-heal', '--replay', '--poll-secs', '.02'],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = b''
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(watcher.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 8
                while b'START retrying ' not in output or b'START running ' not in output:
                    self.assertLess(time.monotonic(), deadline, output)
                    for key, _ in selector.select(.1):
                        output += os.read(key.fd, 65536)
                self.assertNotIn(b'DONE ', output)
                with mock.patch.dict(os.environ, self.env, clear=True):
                    record = a.read(self.root / 'runs/retrying.run.json')
                    (self.root / 'runs/retrying.last.md').write_text('OK\n')
                    record.update(status='done', exit_code=0, ended_at=a.now())
                    safety.atomic(self.root / 'runs/retrying.run.json', record)
                while b'DONE retrying exit=0' not in output:
                    self.assertLess(time.monotonic(), deadline, output)
                    for key, _ in selector.select(.1):
                        output += os.read(key.fd, 65536)
        finally:
            watcher.terminate()
            out, err = watcher.communicate(timeout=5)
        self.assertEqual(b'', err)
        done = [line for line in (output + out).splitlines() if line.startswith(b'DONE ')]
        self.assertEqual(1, len(done), done)
        self.assertIn(b'DONE retrying exit=0', done[0])

    def test_quota_wait_stays_active_after_launchers_exit(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.set_record('quota', 'waiting_quota', pid=99999998, launcher_pid=99999997)
            safety.atomic(self.root / 'runs/quota.route.json', dict(name='quota', run_id='route', active_name='quota',
                          status='waiting_quota', router_pid=99999996))
            self.assertEqual('waiting_quota', life.logical_status('quota'))
            self.assertEqual(('waiting_quota', True), life.wait_run('quota', timeout=.02))
            self.assertEqual('waiting_quota', mcp.report('quota')['status'])
            self.assertFalse(any(event['kind'] == 'DONE' for event in mcp.events()['events']))
            self.assertEqual('waiting_quota', a.registry(self.root / 'runs')[0]['status'])

    def test_dead_retry_owner_unblocks_wait_and_mcp_reports_died(self):
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(life, 'ensure_daemon'):
            self.set_record('retry', 'retrying', pid=99999998, launcher_pid=99999997)
            safety.atomic(self.root / 'runs/retry.route.json', dict(name='retry', run_id='route', active_name='retry',
                          status='running', router_pid=99999996))
            self.assertEqual('died', life.logical_status('retry'))
            self.assertEqual(('died', False), life.wait_run('retry', timeout=.2))
            self.assertEqual('died', mcp.report('retry')['status'])

    def test_watch_reports_dead_retry_owner_without_done(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.set_record('retry', 'retrying', pid=99999998, launcher_pid=99999997)
        watcher = subprocess.Popen([str(ROOT / 'bin/godfather'), 'watch', '--no-heal', '--replay', '--poll-secs', '.02'],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = b''
        try:
            deadline = time.monotonic() + 8
            with selectors.DefaultSelector() as selector:
                selector.register(watcher.stdout, selectors.EVENT_READ)
                while b'DIED retry' not in output:
                    self.assertLess(time.monotonic(), deadline, output)
                    for key, _ in selector.select(.1):
                        output += os.read(key.fd, 65536)
        finally:
            watcher.terminate()
            out, err = watcher.communicate(timeout=5)
        self.assertEqual(b'', err)
        self.assertNotIn(b'DONE retry', output + out)

    def test_live_router_keeps_retry_handoff_active(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.set_record('retry', 'retrying', pid=99999998, launcher_pid=99999997)
            safety.atomic(self.root / 'runs/retry.route.json', dict(name='retry', run_id='route', active_name='retry',
                          status='running', router_pid=os.getpid()))
            self.assertEqual('running', life.logical_status('retry'))
            self.assertNotIn('report', mcp.report('retry'))

    def test_successful_review_keeps_findings_when_worker_resume_fails(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            base = self.root / 'runs/task'
            self.set_record('task', 'reviewing')
            safety.atomic(str(base) + '.review.route.json', dict(active_name='reviewer'))
            safety.atomic(self.root / 'runs/reviewer.run.json', dict(status='done'))
            (self.root / 'runs/reviewer.last.md').write_text('```json\n' + json.dumps(dict(findings=[
                dict(severity='high', file='x.py', line=2, problem='bad branch')])) + '\n```')
            safety.atomic(str(base) + '.review-state.json', dict(original='Task', options=dict(
                review_round=0, review_rounds=1, review_threshold='high', run_cwd=str(self.root), check=None)))
            with mock.patch.object(router, 'resume_worker', return_value=7):
                self.assertEqual(7, router.complete_review(base, base, 'task.review', 0))
            record = a.read(str(base) + '.run.json')
            self.assertEqual(('failed', 'resume', 0, 7), (record['status'], record['failure_class'],
                             record['review_exit_status'], record['review_resume_exit_status']))
            self.assertEqual('bad branch', record['review_findings'][0]['problem'])
            self.assertIn('bad branch', Path(str(base) + '.review.md').read_text())


class PlanNudgeAndSources(unittest.TestCase):
    setUp = plans.PlanCase.setUp
    write_plan = plans.PlanCase.write_plan
    repo = plans.PlanCase.repo
    git = plans.PlanCase.git

    def test_crash_before_nudge_delivery_replays_fix_prompt(self):
        store.insert_plan(self.runs, 'p', 'p', str(self.root), '', '')
        payload = dict(child_name='p-a.try0', child_attempt=0, attempt=1)
        store.insert_run(self.runs, 'p/a', 'p-a', 'running', plan_id='p', task_id='a', payload=json.dumps(payload))
        child = self.runs / 'p-a.try0'
        session = '33333333-3333-3333-3333-333333333333'
        account = self.root / 'fake-codex'
        (account / 'sessions').mkdir(parents=True)
        (account / 'sessions' / ('session-' + session + '.jsonl')).write_text('{}')
        safety.atomic(str(child) + '.run.json', dict(name=child.name, run_id='child', cli='codex', model='fake',
                      cwd=str(self.root), status='done', exit_code=0, started_at=a.now(), ended_at=a.now(),
                      progress_paths=[], session_id=session, codex_home=str(account)))
        Path(str(child) + '.last.md').write_text('OK\n')
        task = dict(id='a', run_name='p-a', plan_id='p', plan_run_id='p/a', plan_root=str(self.runs),
                    task_type='edit', model_hints=[], reattach=True)
        with mock.patch.object(plan.subprocess, 'call', side_effect=RuntimeError('scheduler crashed before delivery')):
            with self.assertRaisesRegex(RuntimeError, 'before delivery'):
                plan.default_executor(task, 'Fix acceptance failure', self.root, 1)
        self.assertEqual(0, json.loads(store.plan_runs(self.runs, 'p')[0]['payload'])['child_attempt'])
        with mock.patch.object(plan.subprocess, 'call', return_value=0) as launch:
            self.assertEqual(0, plan.default_executor(task, 'Fix acceptance failure', self.root, 1))
            self.assertIn('nudge', launch.call_args.args[0])
            self.assertIn('Fix acceptance failure', launch.call_args.args[0])
        self.assertEqual(1, json.loads(store.plan_runs(self.runs, 'p')[0]['payload'])['child_attempt'])
        with mock.patch.object(plan.subprocess, 'call', side_effect=AssertionError('already delivered')):
            self.assertEqual(0, plan.default_executor(task, 'Fix acceptance failure', self.root, 1))

    def test_question_and_attempt_updates_preserve_each_other(self):
        store.insert_run(self.runs, 'task', 'task', 'running', payload=json.dumps(dict(child_attempt=0)))
        store.update_run_payload(self.runs, 'task', dict(question_child='task.try0', questions=[dict(question='Choose?')]), True)
        store.update_run_payload(self.runs, 'task', dict(child_attempt=1))
        record = next(row for row in store.load_runs(self.runs) if row['run_id'] == 'task')
        self.assertEqual(1, record['child_attempt'])
        self.assertEqual('Choose?', record['questions'][0]['question'])
        conn = store.connect(self.runs)
        try:
            self.assertEqual(1, conn.execute('SELECT pending_decision FROM runs WHERE id=?', ('task',)).fetchone()[0])
        finally:
            conn.close()

    def test_source_reads_exclude_shared_writer_in_both_launch_orders(self):
        for first_isolated in (False, True):
            repo = self.root / ('project-' + str(first_isolated))
            repo.mkdir()
            path = self.write_plan('schema=1\n[plan]\nname="mixed-%s"\nrepo="%s"\n' % (first_isolated, repo) +
                ''.join('\n[[tasks]]\nid="%s"\nbrief="fake task"\ndeps=[]\ntask_type="edit"\nisolate=%s\n' %
                        (name, str(isolated).lower()) for name, isolated in (('a', first_isolated), ('b', not first_isolated))))
            active = set()
            lock = threading.Lock()
            original = runner._provision

            def provision(*args):
                with lock:
                    self.assertFalse(active, 'provisioning source read overlapped a sibling writer/reader')
                return original(*args)

            def execute(task, prompt, cwd, attempt):
                with lock:
                    self.assertFalse(active, 'shared source writer overlapped isolated sibling')
                    active.add(task['id'])
                time.sleep(.05)
                with lock:
                    active.remove(task['id'])
                return 0

            with mock.patch.object(runner, '_provision', side_effect=provision):
                self.assertEqual(0, plan.run_plan(path, executor=execute))

    def test_worktree_include_reader_conflicts_with_repo_writer(self):
        repo = self.repo()
        graph = dict(repo=str(repo), id='p', git=True)
        isolated = dict(id='a', task_type='edit')
        shared = dict(id='b', task_type='edit')
        iso_row = dict(isolate=True, cwd=str(repo))
        shared_row = dict(isolate=False, cwd=str(repo))
        self.assertTrue(runner._conflicts(self.runs, graph, isolated, iso_row, shared, shared_row))
        self.assertTrue(runner._conflicts(self.runs, graph, shared, shared_row, isolated, iso_row))

    def test_plan_task_lookup_does_not_create_registry_or_database(self):
        missing = self.root / 'absent-registry'
        self.assertIsNone(store.plan_task(missing, 'unknown'))
        self.assertFalse(missing.exists())
        database = store.db_path(self.runs, create_parent=False)
        self.assertFalse(database.exists())
        self.assertIsNone(store.plan_task(self.runs, 'unknown'))
        self.assertFalse(database.exists())
        store.insert_plan(self.runs, 'p', 'p', str(self.root), '', '')
        store.insert_run(self.runs, 'p/a', 'p-a', 'done', plan_id='p', task_id='a')
        self.assertIsNotNone(store.plan_task(self.runs, 'p-a'))
        store.insert_run(self.runs, 'queued-next', 'p-a', 'queued')
        self.assertIsNone(store.plan_task(self.runs, 'p-a'))

    def test_existing_plan_rows_are_visible_to_status_without_native_metadata(self):
        store.insert_plan(self.runs, 'old', 'old', str(self.root), '', '')
        store.insert_run(self.runs, 'old/a', 'old-a', 'needs_input', plan_id='old', task_id='a', cwd=str(self.root),
                         payload=json.dumps(dict(questions=[dict(question='Choose?', answer=None)])))
        record = next(row for row in a.snapshot(self.runs) if row['name'] == 'old-a')
        self.assertEqual(('plan', 'needs_input', 'Question: Choose?'), (record['cli'], record['status'], record['last_action']))

    def test_finish_does_not_fail_pending_plan(self):
        for status in store.NONTERMINAL:
            if status in ('queued', 'merging'):
                continue
            with self.subTest(status=status):
                name = 'pending-' + status
                store.insert_plan(self.runs, name, name, str(self.root), '', '')
                store.insert_run(self.runs, name + '/a', name + '-a', status, plan_id=name, task_id='a')
                self.assertEqual(1, runner._finish(self.runs, dict(id=name, name=name), [dict(id='a')]))
                row = next(row for row in store.load_plans(self.runs) if row['id'] == name)
                self.assertEqual('open', row['status'])

    def test_finish_fails_terminal_review_findings(self):
        store.insert_plan(self.runs, 'review', 'review', str(self.root), '', '')
        store.insert_run(self.runs, 'review/a', 'review-a', 'review_findings', plan_id='review', task_id='a')
        self.assertEqual(1, runner._finish(self.runs, dict(id='review', name='review'), [dict(id='a')]))
        self.assertEqual('failed', store.load_plans(self.runs)[0]['status'])


ASK_FAKE = batch.FAKE.replace('if quota:\n', r'''
if model == 'work' and 'resume' not in args and not (Path.home() / 'plan-asked').exists():
    (Path.home() / 'plan-asked').touch()
    __import__('subprocess').run(['godfather', 'ask', 'Pick the plan approach?', '--options', 'small|full'], check=True)
''' + '\nif quota:\n', 1)


class PlanQuestions(unittest.TestCase):
    setUp = fixtures.RouterTests.setUp
    config_models = fixtures.RouterTests.config_models
    calls = fixtures.RouterTests.calls
    command = features.FeatureTests.command

    def test_question_on_plan_task_is_answerable_and_scheduler_continues(self):
        self.exercise_question(False)

    def test_blocking_child_question_uses_same_plan_answer_path(self):
        self.exercise_question(True)

    def test_pending_question_reserves_parallel_slot_across_restart(self):
        self.exercise_question(False, independent=True)

    def exercise_question(self, blocking, independent=False):
        for cli in ('codex', 'grok', 'agy'):
            fake = ASK_FAKE.replace("'--options', 'small|full'", "'--options', 'small|full', '--blocking', '--timeout', '30'") if blocking else ASK_FAKE
            (self.root / 'fake' / cli).write_text(fake)
        self.config_models(('codex', 'work'))
        project = self.root / 'project'
        project.mkdir()
        path = self.root / 'plan.toml'
        path.write_text('schema=1\n[plan]\nname="asked"\nrepo="%s"\nmax_parallel=1\n' % project +
                        '\n[[tasks]]\nid="a"\nbrief="fake question task"\ndeps=[]\ntask_type="edit"\n'
                        'check="echo checked > accepted"\n'
                        '\n[[tasks]]\nid="b"\nbrief="fake dependent task"\ndeps=["a"]\ntask_type="edit"\n')
        if independent:
            path.write_text(path.read_text() + '\n[[tasks]]\nid="z"\nbrief="fake independent task"\ndeps=[]\ntask_type="edit"\n')
        scheduler = subprocess.Popen([str(ROOT / 'bin/godfather'), 'plan', 'run', str(path)], env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if scheduler.poll() is None:
                scheduler.terminate()
            scheduler.wait(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 15
        with mock.patch.dict(os.environ, self.env, clear=True):
            while True:
                rows = plan._latest(self.root / 'runs', 'asked')
                if rows.get('a', {}).get('status') == 'needs_input':
                    break
                self.assertLess(time.monotonic(), deadline, 'question never reached plan task')
                time.sleep(.03)
            self.assertIsNone(scheduler.poll())
            self.assertEqual('open', store.load_plans(self.root / 'runs')[0]['status'])
            public = json.loads(plan.status_text('asked', json_out=True))[0]['tasks'][0]
            self.assertEqual('Pick the plan approach?', public['questions'][-1]['question'])
            self.assertEqual('needs_input', life.logical_status('asked-a'))
            self.assertEqual('Pick the plan approach?', mcp.report('asked-a')['question']['question'])
        status = self.command('status', '--json')
        self.assertEqual(0, status.returncode, status.stderr)
        status_row = next(row for row in json.loads(status.stdout) if row['name'] == 'asked-a')
        self.assertEqual('Pick the plan approach?', status_row['questions'][-1]['question'])
        if not blocking:
            scheduler.terminate()
            scheduler.wait(timeout=5)
            scheduler = subprocess.Popen([str(ROOT / 'bin/godfather'), 'plan', 'run', str(path)], env=self.env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with mock.patch.dict(os.environ, self.env, clear=True):
                deadline = time.monotonic() + 10
                while json.loads(plan._latest(self.root / 'runs', 'asked')['a']['payload']).get('launcher_pid') != scheduler.pid:
                    self.assertLess(time.monotonic(), deadline, 'plan did not resume the pending question')
                    time.sleep(.03)
        if independent:
            # Let several polls occur while the question remains unanswered.
            time.sleep(.2)
            with mock.patch.dict(os.environ, self.env, clear=True):
                self.assertEqual('queued', plan._latest(self.root / 'runs', 'asked')['z']['status'])
            self.assertEqual(1, len(self.calls()))
        answered = self.command('answer', 'asked-a', 'small')
        self.assertEqual(0, answered.returncode, answered.stdout + answered.stderr)
        self.assertEqual(0, scheduler.wait(timeout=20))
        with mock.patch.dict(os.environ, self.env, clear=True):
            rows = plan._latest(self.root / 'runs', 'asked')
            self.assertEqual({'done'}, {row['status'] for row in rows.values()})
            self.assertEqual('done', store.load_plans(self.root / 'runs')[0]['status'])
            self.assertFalse(json.loads(rows['a']['payload'])['pending_decision'])
        self.assertEqual((2 if blocking else 3) + int(independent), len(self.calls()))
        if not blocking:
            self.assertIn('resume', self.calls()[1]['args'])
        self.assertTrue((self.root / 'runs/scratch/asked/a/accepted').exists())
