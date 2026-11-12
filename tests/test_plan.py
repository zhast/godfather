"""Plan DAG: dependencies, checks, fix rounds, worktrees, merge, and resume."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import godfather_plan as plan
import godfather_store as store


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.state = self.root / 'state'
        self.config = self.root / 'gitconfig'
        self.config.write_text(textwrap.dedent('''\
            [user]
            \tname = Godfather Test
            \temail = test@example.com
            [commit]
            \tgpgsign = false
            [init]
            \tdefaultBranch = main
            '''))
        self.env = dict(os.environ, AGENT_RUNS_DIR=str(self.runs), XDG_STATE_HOME=str(self.state),
                        XDG_CONFIG_HOME=str(self.root / 'config'), PYTHONDONTWRITEBYTECODE='1',
                        GIT_CONFIG_GLOBAL=str(self.config), GIT_CONFIG_SYSTEM='/dev/null')
        self._mock = mock.patch.dict(os.environ, self.env, clear=True)
        self._mock.start()
        self.addCleanup(self._mock.stop)
        self.addCleanup(self.tmp.cleanup)

    def write_plan(self, body, name='plan.toml'):
        path = self.root / name
        path.write_text(textwrap.dedent(body).lstrip('\n'))
        return path

    def repo(self, files=None):
        path = self.root / 'repo'
        path.mkdir()
        for name, body in (files or {'README.md': 'base\n'}).items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        self.git('init', '-b', 'main', cwd=path)
        self.git('add', '-A', cwd=path)
        self.git('commit', '-m', 'init', cwd=path)
        return path

    def git(self, *args, cwd):
        subprocess.run(['git', *args], cwd=str(cwd), check=True, capture_output=True, text=True)

    def test_example_plan_parses(self):
        spec = plan.parse_plan(ROOT / 'examples' / 'plan.toml')
        self.assertEqual('example', spec['name'])
        self.assertEqual(['readme', 'architecture'], [task['id'] for task in spec['tasks']])
        self.assertEqual(['readme'], spec['tasks'][1]['deps'])
        self.assertFalse(spec['tasks'][0]['isolate'])
        self.assertFalse(spec['merge'])
        self.assertEqual(ROOT.resolve(), Path(spec['repo']))

    def test_rejects_cycles_missing_fields_and_bad_types(self):
        with self.assertRaises(ValueError) as caught:
            plan.parse_plan(self.write_plan('''
                schema = 1
                [plan]
                name = "bad"
                repo = "."
                [[tasks]]
                id = "a"
                brief = "a"
                deps = ["a"]
                task_type = "edit"
                '''))
        self.assertIn('itself', str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            plan.parse_plan(self.write_plan('''
                schema = 1
                [plan]
                name = "bad"
                repo = "."
                [[tasks]]
                id = "a"
                brief = "a"
                deps = ["b"]
                task_type = "edit"
                [[tasks]]
                id = "b"
                brief = "b"
                deps = ["a"]
                task_type = "edit"
                '''))
        self.assertIn('cycle', str(caught.exception))
        with self.assertRaises(ValueError):
            plan.parse_plan(self.write_plan('''
                schema = 2
                [plan]
                name = "bad"
                repo = "."
                [[tasks]]
                id = "a"
                brief = "a"
                deps = []
                task_type = "edit"
                '''))
        with self.assertRaises(ValueError):
            plan.parse_plan(self.write_plan('''
                schema = 1
                [plan]
                name = "bad"
                repo = "."
                [[tasks]]
                id = "a"
                brief = "a"
                deps = []
                task_type = "publish"
                '''))

    def test_runs_dependencies_in_order_and_stores_task_type(self):
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "order"
            repo = "."
            merge = false
            [[tasks]]
            id = "c"
            brief = "third"
            deps = ["a", "b"]
            task_type = "test"
            isolate = false
            [[tasks]]
            id = "a"
            brief = "first"
            deps = []
            task_type = "write"
            isolate = false
            model_hints = ["grok:grok-4.7"]
            [[tasks]]
            id = "b"
            brief = "second"
            deps = ["a"]
            task_type = "edit"
            isolate = false
            ''')
        seen = []

        def execute(task, prompt, cwd, attempt):
            seen.append(task['id'])
            return 0

        self.assertEqual(0, plan.run_plan(path, executor=execute))
        self.assertEqual(['a', 'b', 'c'], seen)
        text = plan.status_text('order')
        self.assertIn('status=done', text)
        self.assertIn('id=b status=done type=edit', text)
        self.assertIn('deps=a,b', text)
        rows = store.plan_runs(self.runs, 'order')
        hints = json.loads(next(row['payload'] for row in rows if row['task_id'] == 'a'))
        self.assertEqual(['grok:grok-4.7'], hints['model_hints'])
        self.assertEqual('write', next(row['task_type'] for row in rows if row['task_id'] == 'a'))

    def test_check_fix_rounds_then_pass(self):
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "fix"
            repo = "."
            merge = false
            [[tasks]]
            id = "a"
            brief = "create the marker"
            deps = []
            check = "test -s ready"
            task_type = "edit"
            isolate = false
            max_fix_rounds = 2
            ''')
        prompts = []

        def execute(task, prompt, cwd, attempt):
            prompts.append(prompt)
            if attempt == 2:
                (Path(cwd) / 'ready').write_text('ok\n')
            return 0

        self.assertEqual(0, plan.run_plan(path, executor=execute))
        self.assertEqual(3, len(prompts))
        self.assertIn('test -s ready', prompts[1])
        self.assertEqual('done', store.load_plans(self.runs)[0]['status'])

    def test_exhausted_check_blocks_dependents(self):
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "block"
            repo = "."
            merge = false
            [[tasks]]
            id = "a"
            brief = "fail the check"
            deps = []
            check = "test -s missing"
            task_type = "edit"
            isolate = false
            max_fix_rounds = 1
            [[tasks]]
            id = "b"
            brief = "should not run"
            deps = ["a"]
            task_type = "edit"
            isolate = false
            ''')
        seen = []

        def execute(task, prompt, cwd, attempt):
            seen.append(task['id'])
            return 0

        self.assertEqual(1, plan.run_plan(path, executor=execute))
        self.assertEqual(['a', 'a'], seen)
        rows = {row['task_id']: row for row in store.plan_runs(self.runs, 'block')}
        self.assertEqual('check_failed', rows['a']['status'])
        self.assertEqual('check', rows['a']['failure_class'])
        self.assertEqual('dependency_failed', rows['b']['status'])
        self.assertEqual('failed', store.load_plans(self.runs)[0]['status'])

    def test_resume_does_not_rerun_finished_tasks(self):
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "resume"
            repo = "."
            merge = false
            [[tasks]]
            id = "a"
            brief = "done once"
            deps = []
            task_type = "edit"
            isolate = false
            [[tasks]]
            id = "b"
            brief = "fails once"
            deps = ["a"]
            task_type = "edit"
            isolate = false
            ''')
        calls = []

        def execute(task, prompt, cwd, attempt):
            calls.append(task['id'])
            if task['id'] == 'b' and calls.count('b') == 1:
                raise RuntimeError('died')
            return 0

        self.assertEqual(1, plan.run_plan(path, executor=execute))
        self.assertEqual(0, plan.run_plan(path, executor=execute))
        self.assertEqual(['a', 'b', 'b'], calls)
        self.assertEqual('done', store.load_plans(self.runs)[0]['status'])
        self.assertEqual(1, len(store.load_plans(self.runs)))

    def test_done_plan_starts_a_new_generation(self):
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "again"
            repo = "."
            merge = false
            [[tasks]]
            id = "a"
            brief = "once"
            deps = []
            task_type = "research"
            isolate = false
            ''')
        calls = []
        self.assertEqual(0, plan.run_plan(path, executor=lambda *args: calls.append(args[0]['id']) or 0))
        self.assertEqual(0, plan.run_plan(path, executor=lambda *args: calls.append(args[0]['id']) or 0))
        self.assertEqual(['a', 'a'], calls)
        ids = [row['id'] for row in store.load_plans(self.runs)]
        self.assertEqual(['again', 'again~2'], ids)

    def test_container_is_refused_instead_of_running_on_the_host(self):
        os.environ.pop('GODFATHER_CONTAINER_IMAGE', None)
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "boxed"
            repo = "."
            container = true
            [[tasks]]
            id = "a"
            brief = "stay inside"
            deps = []
            task_type = "research"
            isolate = false
            ''')
        calls = []
        self.assertEqual(1, plan.run_plan(path, executor=lambda *args: calls.append(1) or 0))
        self.assertEqual([], calls)
        row = store.plan_runs(self.runs, 'boxed')[0]
        self.assertEqual('failed', row['status'])
        self.assertIn('host', row['error'])

    def test_container_runs_when_a_runtime_and_image_exist(self):
        bindir = self.root / 'bin'
        bindir.mkdir()
        docker = bindir / 'docker'
        docker.write_text('#!/bin/sh\n[ "$1" = version ] && exit 0\nexit 1\n')
        docker.chmod(0o755)
        os.environ['PATH'] = str(bindir) + os.pathsep + os.environ.get('PATH', '')
        os.environ['GODFATHER_CONTAINER_IMAGE'] = 'godfather-test:cli'
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "boxed"
            repo = "."
            container = true
            merge = false
            [[tasks]]
            id = "a"
            brief = "stay inside"
            deps = []
            task_type = "edit"
            isolate = false
            ''')
        calls = []
        self.assertEqual(0, plan.run_plan(path, executor=lambda *args: calls.append(args[0]['id']) or 0))
        self.assertEqual(['a'], calls)
        row = store.plan_runs(self.runs, 'boxed')[0]
        self.assertEqual('done', row['status'])

    def test_check_timeout_uses_the_policy_budget(self):
        config = self.root / 'config' / 'godfather'
        config.mkdir(parents=True)
        (config / 'godfather.toml').write_text('[timeouts]\ncheck_secs = 1\n')
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "slow"
            repo = "."
            merge = false
            [[tasks]]
            id = "a"
            brief = "sleeps"
            deps = []
            check = "sleep 5"
            task_type = "test"
            isolate = false
            max_fix_rounds = 0
            ''')
        self.assertEqual(1, plan.run_plan(path, executor=lambda *args: 0))
        row = store.plan_runs(self.runs, 'slow')[0]
        self.assertEqual('check_failed', row['status'])
        self.assertIn('timed out', row['error'])

    def test_isolated_tasks_merge_in_dependency_order(self):
        repo = self.repo()
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "merge"
            repo = "%s"
            merge = true
            [[tasks]]
            id = "b"
            brief = "write b"
            deps = []
            task_type = "edit"
            [[tasks]]
            id = "a"
            brief = "write a"
            deps = []
            task_type = "edit"
            ''' % repo)

        def execute(task, prompt, cwd, attempt):
            filename = task['id'] + '.txt'
            (Path(cwd) / filename).write_text(task['id'] + '\n')
            self.git('add', filename, cwd=cwd)
            self.git('commit', '-m', task['id'], cwd=cwd)
            return 0

        self.assertEqual(0, plan.run_plan(path, executor=execute))
        integrate = self.runs / 'worktrees' / 'merge' / 'integrate'
        self.assertEqual('a\n', (integrate / 'a.txt').read_text())
        self.assertEqual('b\n', (integrate / 'b.txt').read_text())
        self.git('show-ref', '--verify', 'refs/heads/godfather/merge/a', cwd=repo)
        self.git('show-ref', '--verify', 'refs/heads/godfather/merge/b', cwd=repo)
        self.git('merge-base', '--is-ancestor', 'godfather/merge/a', 'godfather/merge/integrate', cwd=repo)
        rows = {row['task_id']: row for row in store.plan_runs(self.runs, 'merge')}
        self.assertEqual('done', rows['a']['status'])
        self.assertEqual('done', rows['b']['status'])
        self.assertEqual('done', rows['integrate']['status'])
        self.assertTrue(rows['a']['worktree'])
        self.assertEqual('done', store.load_plans(self.runs)[0]['status'])

    def test_merge_conflict_stops_and_records_paths(self):
        repo = self.repo()
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "clash"
            repo = "%s"
            merge = true
            [[tasks]]
            id = "a"
            brief = "write a"
            deps = []
            task_type = "edit"
            [[tasks]]
            id = "b"
            brief = "write b"
            deps = []
            task_type = "edit"
            ''' % repo)

        def execute(task, prompt, cwd, attempt):
            (Path(cwd) / 'README.md').write_text(task['id'] + '\n')
            self.git('add', 'README.md', cwd=cwd)
            self.git('commit', '-m', task['id'], cwd=cwd)
            return 0

        self.assertEqual(1, plan.run_plan(path, executor=execute))
        rows = {row['task_id']: row for row in store.plan_runs(self.runs, 'clash')}
        self.assertEqual('done', rows['a']['status'])
        self.assertEqual('failed', rows['b']['status'])
        self.assertEqual('merge', rows['b']['failure_class'])
        self.assertIn('README.md', rows['b']['error'])
        self.git('show-ref', '--verify', 'refs/heads/godfather/clash/b', cwd=repo)
        events = store.events_after(self.runs, 0)
        self.assertTrue(any(event['kind'] == 'MERGE' and 'README.md' in event['detail'] for event in events))
        self.assertEqual('failed', store.load_plans(self.runs)[0]['status'])

    def test_dirty_worktree_is_not_merged(self):
        repo = self.repo()
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "dirty"
            repo = "%s"
            merge = true
            [[tasks]]
            id = "a"
            brief = "forget to commit"
            deps = []
            task_type = "edit"
            ''' % repo)

        def execute(task, prompt, cwd, attempt):
            (Path(cwd) / 'extra.txt').write_text('x\n')
            return 0

        self.assertEqual(1, plan.run_plan(path, executor=execute))
        row = next(item for item in store.plan_runs(self.runs, 'dirty') if item['task_id'] == 'a')
        self.assertEqual('failed', row['status'])
        self.assertIn('clean commit', row['error'])

    def test_merge_is_skipped_outside_git(self):
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "plain"
            repo = "."
            merge = true
            [[tasks]]
            id = "a"
            brief = "local"
            deps = []
            task_type = "write"
            ''')
        seen = []
        self.assertEqual(0, plan.run_plan(path, executor=lambda task, prompt, cwd, attempt: seen.append(task['id']) or 0))
        self.assertEqual(['a'], seen)
        self.assertFalse((self.runs / 'worktrees').exists())
        self.assertEqual('done', store.load_plans(self.runs)[0]['status'])

    def test_second_runner_is_refused_while_the_lock_is_held(self):
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "held"
            repo = "."
            merge = false
            [[tasks]]
            id = "a"
            brief = "wait"
            deps = []
            task_type = "edit"
            isolate = false
            ''')
        with plan.plan_lock(self.runs, 'held'):
            with self.assertRaises(ValueError) as caught:
                plan.run_plan(path, executor=lambda *args: 0)
        self.assertIn('already active', str(caught.exception))

    def test_cli_run_and_status(self):
        script = self.root / 'exec.sh'
        script.write_text('#!/bin/sh\nprintf "%s\\n" "$3" >> "$1/order"\nprintf ok > "$1/$3.ok"\nexit 0\n')
        script.chmod(0o755)
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "cli"
            repo = "."
            merge = false
            [[tasks]]
            id = "a"
            brief = "do a"
            deps = []
            check = "test -s a.ok"
            task_type = "edit"
            isolate = false
            [[tasks]]
            id = "b"
            brief = "do b"
            deps = ["a"]
            check = "test -s b.ok"
            task_type = "edit"
            isolate = false
            ''')
        env = dict(self.env, GODFATHER_PLAN_EXEC=str(script))
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'plan', 'run', str(path)],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual('a\nb\n', (self.root / 'order').read_text())
        status = subprocess.run([str(ROOT / 'bin/godfather'), 'plan', 'status', 'cli'],
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn('id=cli status=done', status.stdout)
        self.assertIn('id=b status=done', status.stdout)
        missing = subprocess.run([str(ROOT / 'bin/godfather'), 'plan', 'status', 'missing'],
                                 env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(0, missing.returncode)
        self.assertIn('unknown plan', missing.stderr)
        listed = subprocess.run([str(ROOT / 'bin/godfather'), 'plan', 'status', '--json'],
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual('cli', json.loads(listed.stdout)[0]['name'])

    def test_integrate_task_merges_then_runs_its_check(self):
        repo = self.repo()
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "gate"
            repo = "%s"
            merge = false
            [[tasks]]
            id = "a"
            brief = "write a"
            deps = []
            task_type = "edit"
            [[tasks]]
            id = "ship"
            brief = "merge the branch and prove the file landed"
            deps = ["a"]
            check = "test -s a.txt"
            task_type = "integrate"
            ''' % repo)

        def execute(task, prompt, cwd, attempt):
            (Path(cwd) / 'a.txt').write_text('a\n')
            self.git('add', 'a.txt', cwd=cwd)
            self.git('commit', '-m', 'a', cwd=cwd)
            return 0

        self.assertEqual(0, plan.run_plan(path, executor=execute))
        ship = self.runs / 'worktrees' / 'gate' / 'ship'
        self.assertEqual('a\n', (ship / 'a.txt').read_text())
        self.git('merge-base', '--is-ancestor', 'godfather/gate/a', 'godfather/gate/ship', cwd=repo)
        rows = {row['task_id']: row for row in store.plan_runs(self.runs, 'gate')}
        self.assertEqual('done', rows['ship']['status'])
        self.assertEqual('done', rows['a']['status'])
        events = store.events_after(self.runs, 0)
        self.assertTrue(any(event['kind'] == 'CHECK' and event['detail'].startswith('pass') for event in events))
        self.assertEqual('done', store.load_plans(self.runs)[0]['status'])

    def test_integrate_conflict_stops_before_the_check(self):
        repo = self.repo()
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "stop"
            repo = "%s"
            [[tasks]]
            id = "a"
            brief = "write a"
            deps = []
            task_type = "edit"
            [[tasks]]
            id = "b"
            brief = "write b"
            deps = []
            task_type = "edit"
            [[tasks]]
            id = "ship"
            brief = "merge"
            deps = ["a", "b"]
            check = "touch should-not-run"
            task_type = "integrate"
            ''' % repo)

        def execute(task, prompt, cwd, attempt):
            (Path(cwd) / 'README.md').write_text(task['id'] + '\n')
            self.git('add', 'README.md', cwd=cwd)
            self.git('commit', '-m', task['id'], cwd=cwd)
            return 0

        self.assertEqual(1, plan.run_plan(path, executor=execute))
        ship = self.runs / 'worktrees' / 'stop' / 'ship'
        self.assertFalse((ship / 'should-not-run').exists())
        rows = {row['task_id']: row for row in store.plan_runs(self.runs, 'stop')}
        self.assertEqual('failed', rows['b']['status'])
        self.assertEqual('merge', rows['b']['failure_class'])
        self.assertIn('README.md', rows['b']['error'])
        self.assertEqual('failed', rows['ship']['status'])
        events = store.events_after(self.runs, 0)
        self.assertFalse(any(event['kind'] == 'CHECK' for event in events))
        self.assertEqual('failed', store.load_plans(self.runs)[0]['status'])

    def test_integrate_check_failure_stops_the_plan(self):
        repo = self.repo()
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "reject"
            repo = "%s"
            [[tasks]]
            id = "a"
            brief = "write a"
            deps = []
            task_type = "edit"
            [[tasks]]
            id = "ship"
            brief = "merge"
            deps = ["a"]
            check = "test -s missing"
            task_type = "integrate"
            ''' % repo)

        def execute(task, prompt, cwd, attempt):
            (Path(cwd) / 'a.txt').write_text('a\n')
            self.git('add', 'a.txt', cwd=cwd)
            self.git('commit', '-m', 'a', cwd=cwd)
            return 0

        self.assertEqual(1, plan.run_plan(path, executor=execute))
        rows = {row['task_id']: row for row in store.plan_runs(self.runs, 'reject')}
        self.assertEqual('done', rows['a']['status'])
        self.assertEqual('check_failed', rows['ship']['status'])
        self.assertEqual('check', rows['ship']['failure_class'])
        self.assertEqual('a\n', (self.runs / 'worktrees' / 'reject' / 'ship' / 'a.txt').read_text())
        self.assertEqual('failed', store.load_plans(self.runs)[0]['status'])

    def test_non_git_tasks_get_separate_scratch_copies(self):
        (self.root / 'marker').write_text('origin\n')
        path = self.write_plan('''
            schema = 1
            [plan]
            name = "copies"
            repo = "."
            merge = false
            [[tasks]]
            id = "a"
            brief = "edit a"
            deps = []
            task_type = "edit"
            [[tasks]]
            id = "b"
            brief = "edit b"
            deps = []
            task_type = "edit"
            ''')
        seen = []

        def execute(task, prompt, cwd, attempt):
            seen.append(Path(cwd).resolve())
            (Path(cwd) / 'marker').write_text(task['id'] + '\n')
            return 0

        self.assertEqual(0, plan.run_plan(path, executor=execute))
        self.assertEqual(2, len(seen))
        self.assertNotEqual(self.root.resolve(), seen[0])
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual('origin\n', (self.root / 'marker').read_text())
        self.assertEqual({'a\n', 'b\n'}, {(cwd / 'marker').read_text() for cwd in seen})
        self.assertFalse((self.runs / 'worktrees').exists())
