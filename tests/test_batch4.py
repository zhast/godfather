"""Review remediation and concurrent plan regressions. Model CLIs are fakes."""
import json
import os
from pathlib import Path
import subprocess
import selectors
import threading
import time
import unittest
from unittest import mock

import test_router as fixtures
import test_features as features
import test_plan as plans
import agent_oversight as oversight
import godfather_router as router
import godfather_plan as plan
import godfather_plan_run as runner
import godfather_store as store

ROOT = fixtures.ROOT

REVIEW = r'''
is_review = model == 'review'
if is_review:
    marker = Path.home() / 'review-count'
    count = int(marker.read_text()) if marker.exists() else 0
    marker.write_text(str(count + 1))
    severity = os.getenv('FINDING_SEVERITY', 'high')
    if os.getenv('CLEAN_AFTER_FIX'):
        (Path.home() / 'clean-enabled').touch()
    findings = [] if (Path.home() / 'clean-enabled').exists() and (Path.home() / 'fixed').exists() else [dict(severity=severity, file='changed.txt', line=1, problem='clear bug')]
    reply = '```json\n' + json.dumps(dict(findings=findings)) + '\n```'
    if os.getenv('BAD_REVIEW_JSON'):
        reply = '```json\n{"findings":[{"severity":"bogus"}]}\n```\n- [high] changed.txt:1 - legacy finding'
else:
    if os.getenv('WORKER_ASK'):
        (Path.home() / 'worker-ask-enabled').touch()
    if os.getenv('PLAN_GATE'):
        import time
        while not (Path.home() / 'gate').exists():
            time.sleep(.02)
    reply = 'OK'
    target = Path.home() / 'fixed'
    if 'Fix them or explain' in prompt or 'acceptance check failed' in prompt:
        target.write_text('fixed')
        if (Path.home() / 'worker-ask-enabled').exists() and not (Path.home() / 'asked-worker').exists():
            (Path.home() / 'asked-worker').touch()
            __import__('subprocess').run(['godfather', 'ask', 'Worker decision?'], check=True)
    if os.getenv('SAVE_SESSION') and cli == 'codex':
        session = Path.home() / '.codex/sessions/session-33333333-3333-3333-3333-333333333333.jsonl'
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(json.dumps(dict(payload=dict(id='33333333-3333-3333-3333-333333333333'))) + '\n')
if is_review and os.getenv('REVIEW_ASK') and '--resume' not in args and not (Path.home() / 'asked-review').exists():
    (Path.home() / 'asked-review').touch()
    __import__('subprocess').run(['godfather', 'ask', 'Review decision?'], check=True)
'''
FAKE = fixtures.FAKE.replace('if quota:\n', REVIEW + '\nif quota:\n', 1)
FAKE = FAKE.replace(".write_text('OK\\n')", ".write_text(reply + '\\n')")
FAKE = FAKE.replace("dict(type='text', data='OK')", "dict(type='text', data=reply)")


class ReviewRounds(unittest.TestCase):
    setUp = fixtures.RouterTests.setUp
    config_models = fixtures.RouterTests.config_models
    calls = fixtures.RouterTests.calls
    command = features.FeatureTests.command

    def fake(self):
        for cli in ('codex', 'grok', 'agy'):
            (self.root / 'fake' / cli).write_text(FAKE)
        self.config_models(('codex', 'work'), ('grok', 'review'))

    def run_review(self, *extra, **env):
        return self.command('run', '--no-isolate', '-p', 'Fix task', '--review-by', 'auto', *extra, 'task', **env)

    def test_worker_resumes_exact_session_then_checks_and_reviews_again(self):
        self.fake()
        check = "echo check >> checks; test ! -f review-count || test -f fixed"
        result = self.run_review('--review-rounds', '2', '--check', check, CLEAN_AFTER_FIX='1')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['work', 'review', 'work', 'review'], [c['model'] for c in self.calls()])
        resumed = self.calls()[2]
        self.assertIn('resume', resumed['args'])
        self.assertIn('33333333-3333-3333-3333-333333333333', resumed['args'])
        self.assertIn('[high] changed.txt:1 - clear bug', resumed['prompt'])
        self.assertIn('explain precisely why', resumed['prompt'])
        self.assertEqual(['check', 'check'], (self.root / 'checks').read_text().splitlines())
        self.assertIn('REVIEW task round 1: 1 findings (high)', result.stdout)
        self.assertIn('REVIEW task round 2: 0 findings (none)', result.stdout)
        run = oversight.read(self.root / 'runs/task.run.json')
        self.assertEqual(('done', 'json', 1), (run['status'], run['review_parse_method'], run['review_round']))
        self.assertTrue((self.root / 'runs/task.last.1.md').exists())
        self.assertIn('Do not edit any files', self.calls()[1]['prompt'])
        self.assertIn('```json', self.calls()[1]['prompt'])
        self.assertIn('## Worker response to the previous review\nOK', self.calls()[3]['prompt'])
        self.assertIn('Evaluate any explanation', self.calls()[3]['prompt'])

    def test_exhausted_findings_are_terminal_for_status_wait_watch(self):
        self.fake()
        result = self.run_review('--review-rounds', '1', BAD_REVIEW_JSON='1')
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertEqual(4, len(self.calls()))
        run = oversight.read(self.root / 'runs/task.run.json')
        self.assertEqual(('review_findings', 'lines'), (run['status'], run['review_parse_method']))
        waited = self.command('wait', 'task', '--timeout', '1')
        self.assertNotEqual(0, waited.returncode)
        self.assertIn('review_findings', waited.stdout)
        status = self.command('status', '--json')
        self.assertIn('review_findings', status.stdout)
        with mock.patch.dict(os.environ, self.env, clear=True):
            reviews = [event for event in store.events_after(self.root / 'runs', 0) if event['kind'] == 'REVIEW']
        self.assertIn('round 2: 1 findings', reviews[-1]['detail'])
        watcher = subprocess.Popen([str(ROOT / 'bin/godfather'), 'watch', '--replay', '--no-heal', '--poll-secs', '.03'],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = b''
        try:
            deadline = time.monotonic() + 8
            with selectors.DefaultSelector() as selector:
                selector.register(watcher.stdout, selectors.EVENT_READ)
                while b'DONE task exit=1' not in output and time.monotonic() < deadline:
                    for key, _ in selector.select(.1):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            break
                        output += chunk
        finally:
            watcher.terminate()
            out, err = watcher.communicate(timeout=5)
        self.assertIn(b'DONE task exit=1', output + out, output + out + err)

    def test_default_zero_and_severity_threshold(self):
        self.fake()
        result = self.run_review()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, len(self.calls()))
        result = self.command('run', '--no-isolate', '-p', 'Task', '--review-by', 'auto',
                              '--review-rounds', '1', 'below', FINDING_SEVERITY='medium')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(4, len(self.calls()))
        result = self.command('run', '--no-isolate', '-p', 'Task', '--review-by', 'auto',
                              '--review-rounds', '1', '--review-threshold', 'medium', 'at', FINDING_SEVERITY='medium')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(8, len(self.calls()))

    def test_two_fix_rounds_stop_at_budget_and_record_each_parser(self):
        self.fake()
        result = self.run_review('--review-rounds', '2')
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['work', 'review'] * 3, [c['model'] for c in self.calls()])
        self.assertIn('round 3: 1 findings (high)', result.stdout)
        run = oversight.read(self.root / 'runs/task.run.json')
        self.assertEqual([1, 2, 3], [item['round'] for item in run['review_history']])
        self.assertEqual({'json'}, {item['parse_method'] for item in run['review_history']})

    def test_reviewer_question_continues_original_worker_loop(self):
        self.fake()
        result = self.run_review('--review-rounds', '1', CLEAN_AFTER_FIX='1', REVIEW_ASK='1')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('needs_input', oversight.read(self.root / 'runs/task.run.json')['status'])
        answered = self.command('answer', 'task.review', 'continue')
        self.assertEqual(0, answered.returncode, answered.stdout + answered.stderr)
        self.assertEqual('done', oversight.read(self.root / 'runs/task.run.json')['status'])
        self.assertEqual(['work', 'review', 'review', 'work', 'review'], [c['model'] for c in self.calls()])

    def test_worker_question_preserves_round_budget(self):
        self.fake()
        result = self.run_review('--review-rounds', '1', CLEAN_AFTER_FIX='1', WORKER_ASK='1')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('needs_input', oversight.read(self.root / 'runs/task.run.json')['status'])
        answered = self.command('answer', 'task', 'continue')
        self.assertEqual(0, answered.returncode, answered.stdout + answered.stderr)
        self.assertIn('REVIEW task round 2: 0 findings', answered.stdout)
        self.assertEqual('done', oversight.read(self.root / 'runs/task.run.json')['status'])

    def test_failed_worker_check_prevents_second_review(self):
        self.fake()
        result = self.run_review('--review-rounds', '2', '--check', 'test ! -f review-count', '--max-fix-rounds', '0')
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['work', 'review', 'work'], [c['model'] for c in self.calls()])
        self.assertEqual('check_failed', oversight.read(self.root / 'runs/task.run.json')['status'])

    def test_argument_validation_before_launch(self):
        self.fake()
        for args in (('--review-rounds', '-1'), ('--review-rounds', '1')):
            result = self.command('run', '--no-isolate', '-p', 'Task', *args, 'invalid')
            self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.root / 'calls.jsonl').exists())

    def test_json_validation_and_line_fallback(self):
        item = dict(severity='critical', file='file with spaces.py', line=12, problem='bad branch')
        self.assertEqual(([item], 'json'), router.parse_findings('prose\n```json\n' + json.dumps(dict(findings=[item])) + '\n```'))
        self.assertEqual(([], 'json'), router.parse_findings('```json\n{"findings":[]}\n```\n- [high] ignored:1 - text'))
        for bad in ('null', '[]', '{}', '{"findings":null}', '{"findings":[false]}',
                    '{"findings":[{"severity":"high","file":"x","line":true,"problem":"x"}]}'):
            with self.subTest(bad=bad):
                found, method = router.parse_findings('```json\n' + bad + '\n```\n- [high] x:2 - fallback')
                self.assertEqual(('lines', 'fallback'), (method, found[0]['problem']))


class ParallelPlans(unittest.TestCase):
    setUp = plans.PlanCase.setUp
    write_plan = plans.PlanCase.write_plan
    repo = plans.PlanCase.repo
    git = plans.PlanCase.git

    def spec(self, isolate=True, parallel=2, integrate=False):
        repo = self.root / 'project'
        repo.mkdir(exist_ok=True)
        body = 'schema=1\n[plan]\nname="parallel"\nrepo="%s"\nmax_parallel=%s\n' % (repo, parallel)
        for task in ('a', 'b', 'c'):
            body += '\n[[tasks]]\nid="%s"\nbrief="fake task"\ndeps=[]\ntask_type="edit"\nisolate=%s\n' % (task, str(isolate).lower())
        if integrate:
            body += '\n[[tasks]]\nid="join"\nbrief="merge task"\ndeps=["a"]\ntask_type="integrate"\n'
        return self.write_plan(body)

    def test_distinct_directories_overlap_and_limit_is_enforced(self):
        path = self.spec()
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active, peak, directories = set(), [], []

        def execute(task, prompt, cwd, attempt):
            with lock:
                active.add(task['id'])
                peak.append(len(active))
                directories.append(cwd)
            if task['id'] in ('a', 'b'):
                barrier.wait(timeout=5)
            with lock:
                active.remove(task['id'])
            return 0

        self.assertEqual(0, plan.run_plan(path, executor=execute))
        self.assertEqual(2, max(peak))
        self.assertEqual(3, len(set(directories)))
        self.assertEqual({'done'}, {row['status'] for row in store.plan_runs(self.runs, 'parallel')})

    def test_shared_directories_and_max_parallel_one_stay_serial(self):
        for isolate, override in ((False, None), (True, 1)):
            with self.subTest(isolate=isolate):
                path = self.spec(isolate=isolate)
                active, peak = [], []

                def execute(task, prompt, cwd, attempt):
                    active.append(task['id'])
                    peak.append(len(active))
                    time.sleep(.03)
                    active.remove(task['id'])
                    return 0

                self.assertEqual(0, plan.run_plan(path, executor=execute, max_parallel=override))
                self.assertEqual(1, max(peak))

    def test_integrate_runs_alone_even_with_unrelated_ready_tasks(self):
        path = self.spec(integrate=True)
        active = set()
        lock = threading.Lock()
        original = runner._integrate

        def execute(task, prompt, cwd, attempt):
            with lock:
                active.add(task['id'])
            time.sleep(.04)
            with lock:
                active.remove(task['id'])
            return 0

        def integrate(*args):
            self.assertEqual(set(), active)
            return original(*args)

        with mock.patch.object(runner, '_integrate', side_effect=integrate):
            self.assertEqual(0, plan.run_plan(path, executor=execute))

    def test_concurrent_failure_blocks_dependents_and_resume_keeps_done_tasks(self):
        path = self.spec()
        path.write_text(path.read_text().replace('id="c"\nbrief="fake task"\ndeps=[]', 'id="c"\nbrief="fake task"\ndeps=["a"]'))
        seen = []

        def execute(task, prompt, cwd, attempt):
            seen.append(task['id'])
            return int(task['id'] == 'a' and seen.count('a') == 1)

        self.assertEqual(1, plan.run_plan(path, executor=execute))
        rows = plan._latest(self.runs, 'parallel')
        self.assertEqual(('failed', 'done', 'dependency_failed'), tuple(rows[t]['status'] for t in 'abc'))
        self.assertEqual(0, plan.run_plan(path, executor=execute))
        self.assertEqual((2, 1, 1), tuple(seen.count(t) for t in 'abc'))

    def test_resume_uses_updated_brief_without_resetting_attempt(self):
        path = self.spec(isolate=False)
        seen = []

        def execute(task, prompt, cwd, attempt):
            seen.append((task['id'], prompt, attempt))
            return int(task['id'] == 'a' and sum(item[0] == 'a' for item in seen) == 1)

        self.assertEqual(1, plan.run_plan(path, executor=execute))
        path.write_text(path.read_text().replace('brief="fake task"', 'brief="updated task"'))
        self.assertEqual(0, plan.run_plan(path, executor=execute))
        self.assertEqual(('a', 'updated task', 0), seen[-1])

    def test_parallel_validation_and_review_findings_block(self):
        for invalid in ('0', '-1', 'true', '"two"'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    plan.parse_plan(self.spec(parallel=invalid))
        with self.assertRaises(ValueError):
            plan.run_plan(self.spec(), executor=lambda *args: 0, max_parallel=0)
        self.assertIn('review_findings', plan.BAD)
        self.assertIn('review_findings', store.TERMINAL)

    def test_duplicate_names_and_implicit_integrator_collisions_are_rejected(self):
        path = self.spec()
        path.write_text(path.read_text().replace('brief="fake task"', 'brief="fake task"\nname="duplicate"'))
        with self.assertRaisesRegex(ValueError, 'names must be unique'):
            plan.parse_plan(path)
        spec = dict(name='merge', merge=True, tasks=[dict(id='a', name='merge-integrate', deps=[], task_type='edit')])
        with self.assertRaisesRegex(ValueError, 'including the integrator'):
            runner._with_integrate(spec, True)

    def test_terminal_index_migrates_in_place_and_allows_reused_names(self):
        conn = store.connect(self.runs)
        conn.execute('DROP INDEX runs_one_live_name')
        conn.execute("CREATE UNIQUE INDEX runs_one_live_name ON runs(name) WHERE status NOT IN ('done','failed')")
        conn.execute('PRAGMA user_version=3')
        conn.close()
        inode = store.db_path(self.runs).stat().st_ino
        conn = store.connect(self.runs)
        self.assertEqual(4, conn.execute('PRAGMA user_version').fetchone()[0])
        conn.close()
        self.assertEqual(inode, store.db_path(self.runs).stat().st_ino)
        store.insert_run(self.runs, 'first', 'reuse', 'review_findings')
        store.insert_run(self.runs, 'second', 'reuse', 'running')
        self.assertEqual('review_findings', store.machine_status(self.runs, 'first')['status'])


class PlanSessions(unittest.TestCase):
    setUp = fixtures.RouterTests.setUp
    config_models = fixtures.RouterTests.config_models
    calls = fixtures.RouterTests.calls

    def setup_plan(self, tasks=1):
        for cli in ('codex', 'grok', 'agy'):
            (self.root / 'fake' / cli).write_text(FAKE)
        self.config_models(('codex', 'work'))
        project = self.root / 'project'
        project.mkdir()
        path = self.root / 'plan.toml'
        body = 'schema=1\n[plan]\nname="sessions"\nrepo="%s"\nmax_parallel=2\n' % project
        for i in range(tasks):
            body += ('\n[[tasks]]\nid="t%s"\nbrief="fake worker task"\ndeps=[]\ntask_type="edit"\n' % i)
            if tasks == 1:
                body += 'check="test -f %s"\nmax_fix_rounds=1\n' % (self.root / 'fixed')
        path.write_text(body)
        return path

    def test_plan_fixes_resume_saved_session(self):
        path = self.setup_plan()
        with mock.patch.dict(os.environ, dict(self.env, SAVE_SESSION='1'), clear=True):
            self.assertEqual(0, plan.run_plan(path))
        self.assertEqual(2, len(self.calls()))
        self.assertIn('resume', self.calls()[1]['args'])
        self.assertIn('33333333-3333-3333-3333-333333333333', self.calls()[1]['args'])
        self.assertTrue((self.root / 'runs/sessions-t0.try0.last.1.md').exists())
        self.assertFalse((self.root / 'runs/sessions-t0.try1.run.json').exists())

    def test_missing_session_starts_next_try_and_cli_change_disallows_resume(self):
        path = self.setup_plan()
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(0, plan.run_plan(path))
            previous = oversight.read(self.root / 'runs/sessions-t0.try0.run.json')
            sessions = self.root / '.codex/sessions'
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / ('test-' + previous['session_id'] + '.jsonl')).write_text('{}')
            self.assertFalse(plan._resumable(previous, dict(model_hints=['grok:review'])))
            self.assertTrue(plan._resumable(previous, dict(model_hints=['grok:review'], previous_model_hints=['grok:review'])))
        self.assertEqual(2, len(self.calls()))
        self.assertNotIn('resume', self.calls()[1]['args'])
        self.assertTrue((self.root / 'runs/sessions-t0.try1.run.json').exists())

    def test_crashed_parallel_scheduler_reattaches_without_duplicate_workers(self):
        path = self.setup_plan(tasks=2)
        env = dict(self.env, PLAN_GATE='1')
        first = subprocess.Popen([str(ROOT / 'bin/godfather'), 'plan', 'run', str(path)], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        deadline = time.monotonic() + 20
        while not (self.root / 'calls.jsonl').exists() or len(self.calls()) < 2:
            self.assertLess(time.monotonic(), deadline, 'parallel fake workers did not start')
            time.sleep(.03)
        first.kill()  # Kill only this test-owned scheduler, leaving its durable fake workers.
        first.wait(timeout=5)
        second = subprocess.Popen([str(ROOT / 'bin/godfather'), 'plan', 'run', str(path)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: second.poll() is None and second.kill())
        time.sleep(.3)
        self.assertEqual(2, len(self.calls()))
        (self.root / 'gate').touch()
        out, err = second.communicate(timeout=25)
        self.assertEqual(0, second.returncode, out + err)
        self.assertEqual(2, len(self.calls()))
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual({'done'}, {row['status'] for row in plan._latest(self.root / 'runs', 'sessions').values()})
