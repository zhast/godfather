"""Bounded isolation, interruption context, and conductor-facing diagnostics."""
import contextlib
import io
import json
import os
import select
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
import godfather_lifecycle as life
import godfather_mcp as mcp
import godfather_plan_run as plan_run
import godfather_router as router
import godfather_safety as safety
import godfather_stream as stream
import godfather_supervisor as supervisor
import test_router as fixture


class HardeningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.repo = self.root / 'project'
        self.repo.mkdir()
        env = safety.original_environment()
        for key in list(env):
            if key.startswith(('GODFATHER_', 'AGENT_')) or key in ('BASH_ENV', 'ZDOTDIR'):
                env.pop(key, None)
        env.update(HOME=str(self.root), AGENT_RUNS_DIR=str(self.runs),
                   XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                   GODFATHER_QUEUE_DAEMON='1', PYTHONDONTWRITEBYTECODE='1')
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '-C', str(cwd or self.repo), *args],
                              env=safety.original_environment(), capture_output=True, text=True, check=True)

    def init_repo(self):
        self.git('init', '-q')
        (self.repo / 'tracked.txt').write_text('base\n')
        self.git('add', 'tracked.txt')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')

    def test_scratch_refuses_home_root_and_collection_before_copying(self):
        for source in (self.root, Path(self.root.anchor)):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'project folder.*--no-isolate'):
                safety.copy_scratch(source, self.runs / 'scratch')
        for name in ('one', 'two'):
            child = self.repo / name
            child.mkdir()
            # Recognize both normal checkouts and linked worktrees.
            if name == 'one':
                (child / '.git').mkdir()
            else:
                (child / '.git').write_text('gitdir: elsewhere\n')
        with mock.patch.object(safety.shutil, 'copytree', side_effect=AssertionError('preflight failed')):
            with self.assertRaisesRegex(ValueError, 'multiple git repos.*project folder.*--no-isolate'):
                safety.copy_scratch(self.repo, self.runs / 'scratch')
        self.assertFalse((self.runs / 'scratch').exists())

    def test_scratch_skips_rebuildable_names_at_every_depth(self):
        for parent in (self.repo, self.repo / 'nested'):
            parent.mkdir(exist_ok=True)
            (parent / 'keep.txt').write_text('keep')
            for name in safety.SCRATCH_SKIP:
                if name == '.DS_Store':
                    (parent / name).write_bytes(b'x' * 2048)
                else:
                    (parent / name).mkdir()
                    (parent / name / 'large').write_bytes(b'x' * 2048)
        (self.repo / 'scripts').mkdir()
        (self.repo / 'scripts/build').write_text('script')
        # Skipped bytes must not count towards this tiny cap either.
        with mock.patch.dict(os.environ, GODFATHER_SCRATCH_MAX_MB='0.001'):
            target = safety.copy_scratch(self.repo, self.runs / 'scratch')
        self.assertEqual(['keep.txt', 'nested/keep.txt', 'scripts/build'], sorted(
            str(path.relative_to(target)) for path in target.rglob('*') if path.is_file()))

    def test_scratch_size_cap_stops_walk_early_and_leaves_no_destination(self):
        (self.repo / 'large').write_bytes(b'x' * 4096)
        def walk(*args, **kwargs):
            yield str(self.repo), [], ['large']
            raise AssertionError('continued walking after cap was exceeded')
        with mock.patch.dict(os.environ, GODFATHER_SCRATCH_MAX_MB='0.001'), \
                mock.patch.object(safety.os, 'walk', side_effect=walk), \
                mock.patch.object(safety.shutil, 'copytree', side_effect=AssertionError('copied over cap')):
            with self.assertRaisesRegex(ValueError, 'measured at least .*MiB.*GODFATHER_SCRATCH_MAX_MB'):
                safety.copy_scratch(self.repo, self.runs / 'scratch')
        self.assertFalse((self.runs / 'scratch').exists())
        for cap in ('-1', 'nan', 'inf', 'invalid', '0'):
            with self.subTest(cap=cap), mock.patch.dict(os.environ, GODFATHER_SCRATCH_MAX_MB=cap):
                with self.assertRaisesRegex(ValueError, 'GODFATHER_SCRATCH_MAX_MB'):
                    safety.copy_scratch(self.repo, self.runs / 'invalid')

    def test_scratch_default_cap_and_nested_destination(self):
        with mock.patch.object(safety.os, 'walk', return_value=[(str(self.repo), [], ['large'])]), \
                mock.patch.object(Path, 'lstat', return_value=mock.Mock(st_size=2048 * 1024 * 1024 + 1)):
            with self.assertRaisesRegex(ValueError, 'GODFATHER_SCRATCH_MAX_MB=2048'):
                safety.copy_scratch(self.repo, self.runs / 'large')
        (self.repo / 'keep').write_text('small')
        target = safety.copy_scratch(self.repo, self.repo / 'copies' / 'one')
        self.assertEqual('small', (target / 'keep').read_text())
        self.assertFalse((target / 'copies').exists())

    def test_worktree_include_git_patterns_untracked_ignored_and_audit(self):
        self.init_repo()
        (self.repo / '.gitignore').write_text('local/\n')
        (self.repo / '.worktreeinclude').write_text('# local config\nlocal/*\n!local/omit.txt\n*.cfg\ntracked.txt\n')
        (self.repo / 'local').mkdir()
        (self.repo / 'local/settings.json').write_text('local settings')
        (self.repo / 'local/omit.txt').write_text('omit')
        (self.repo / 'plain.cfg').write_text('untracked config')
        (self.repo / 'tracked.txt').write_text('dirty source')
        (self.repo / 'unlisted.txt').write_text('omit')
        target, _ = router.create_worktree(self.repo, self.runs / 'included', 'godfather/included')
        self.assertEqual('base\n', (target / 'tracked.txt').read_text())
        self.assertEqual('local settings', (target / 'local/settings.json').read_text())
        self.assertEqual('untracked config', (target / 'plain.cfg').read_text())
        self.assertFalse((target / 'unlisted.txt').exists())
        self.assertFalse((target / 'local/omit.txt').exists())
        log = (self.runs / 'included.models.log').read_text()
        self.assertEqual(2, log.count('WORKTREE_INCLUDE'))
        self.assertIn('"local/settings.json"', log)
        self.assertIn('"plain.cfg"', log)

    def test_include_uses_main_checkout_even_from_linked_worktree_and_plan_run_name(self):
        self.init_repo()
        linked, _ = router.create_worktree(self.repo, self.runs / 'linked', 'godfather/linked')
        (self.repo / '.worktreeinclude').write_text('local.cfg\n')
        (self.repo / 'local.cfg').write_text('main')
        (linked / 'local.cfg').write_text('linked')
        target, _ = plan_run._provision(dict(repo=str(linked), root=str(self.runs), id='plan', git=True,
                        base_sha='HEAD'), dict(id='task', task_type='test'), dict(name='custom-task', isolate=True))
        self.assertEqual('main', (target / 'local.cfg').read_text())
        self.assertIn('WORKTREE_INCLUDE "local.cfg"', (self.runs / 'custom-task.models.log').read_text())
        self.assertFalse((self.runs / 'task.models.log').exists())

    def test_include_missing_file_is_unchanged_and_symlinks_are_not_followed(self):
        self.init_repo()
        target, _ = router.create_worktree(self.repo, self.runs / 'absent', 'godfather/absent')
        self.assertFalse((self.runs / 'absent.models.log').exists())
        outside = self.root / 'outside.cfg'
        outside.write_text('outside')
        (self.repo / 'link.cfg').symlink_to(outside)
        (self.repo / 'linked-dir').symlink_to(self.root, target_is_directory=True)
        (self.repo / '.worktreeinclude').write_text('*.cfg\nlinked-dir/\n')
        router.copy_worktree_includes(self.repo, target, self.runs / 'absent.models.log')
        self.assertFalse((target / 'link.cfg').exists())
        self.assertFalse((target / 'linked-dir').exists())
        # An untracked source may also collide with a path in the target commit.
        self.git('rm', '--cached', 'tracked.txt')
        (self.repo / '.worktreeinclude').write_text('tracked.txt\n')
        router.copy_worktree_includes(self.repo, target, self.runs / 'absent.models.log')
        self.assertEqual('base\n', (target / 'tracked.txt').read_text())

    def test_env_includes_require_literal_full_path_without_wildcards(self):
        for path in ('.env', '.env.local', 'config/.env.test', '.envdir/settings'):
            for patterns in (['*'], ['**'], ['.env*'], ['config/'], ['**/.env*'], ['!.env'], ['# .env']):
                with self.subTest(path=path, patterns=patterns):
                    self.assertFalse(router.explicit_env_include(path, patterns))
            self.assertTrue(router.explicit_env_include(path, [path]))
            self.assertTrue(router.explicit_env_include(path, ['/' + path]))
        self.assertTrue(router.explicit_env_include('normal.cfg', ['*.cfg']))
        self.assertFalse(router.explicit_env_include('config/.env', ['.env']))

    def lock_holder(self, cwd):
        code = ('import sys; sys.path.insert(0, sys.argv[1]); import godfather_safety as s\n'
                'with s.git_lock(sys.argv[2]):\n print("locked", flush=True); sys.stdin.readline()\n')
        child = subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'bin'), str(cwd)],
                                 env=safety.original_environment(), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def finish():
            if child.poll() is None:
                child.communicate('\n', timeout=5)
        self.addCleanup(finish)
        self.assertEqual('locked\n', child.stdout.readline())
        return child

    def test_git_lock_shared_across_worktrees_waits_times_out_and_releases(self):
        self.init_repo()
        linked, _ = router.create_worktree(self.repo, self.runs / 'linked', 'godfather/linked')
        holder = self.lock_holder(linked)
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'Timed out.*waiting for git lock:'):
            with safety.git_lock(self.repo, timeout=.1):
                self.fail('acquired held common-dir lock')
        self.assertGreaterEqual(time.monotonic() - started, .1)
        # Work in an unrelated repository must remain independent.
        other = self.root / 'other'
        other.mkdir()
        self.git('init', '-q', cwd=other)
        with safety.git_lock(other, timeout=0):
            pass
        holder.communicate('\n', timeout=5)
        with safety.git_lock(self.repo, timeout=.1):
            pass
        self.assertEqual(2, len(list(self.runs.glob('git-*.lock'))))

    def test_worktree_mutations_and_rollback_hold_one_git_lock(self):
        self.init_repo()
        held = []
        @contextlib.contextmanager
        def lock(cwd):
            held.append(cwd)
            try:
                yield
            finally:
                held.pop()
        real = subprocess.run
        mutations = []
        def run(argv, **kwargs):
            if any(word in argv for word in ('add', 'remove', 'prune', '-D')):
                self.assertTrue(held, argv)
                mutations.append(argv)
            result = real(argv, **kwargs)
            if 'worktree' in argv and 'add' in argv:
                return subprocess.CompletedProcess(argv, 1)
            return result
        with mock.patch.object(safety, 'git_lock', side_effect=lock), \
                mock.patch.object(router.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'git exited 1'):
                router.create_worktree(self.repo, self.runs / 'broken', 'godfather/broken')
        self.assertFalse(held)
        self.assertEqual(4, len(mutations))
        self.assertFalse((self.runs / 'broken').exists())
        self.assertEqual('', self.git('branch', '--list', 'godfather/broken').stdout)

    def test_worktree_creation_waits_for_other_checkout_then_succeeds(self):
        self.init_repo()
        holder = self.lock_holder(self.repo)
        target = self.runs / 'waiting'
        code = ('import sys; sys.path.insert(0, sys.argv[1]); import godfather_router as r\n'
                'r.create_worktree(sys.argv[2], sys.argv[3], "godfather/waiting")\n')
        child = subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'bin'), str(self.repo), str(target)],
                                 env=safety.original_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                child.communicate(timeout=.2)
            self.assertFalse(target.exists())
            holder.communicate('\n', timeout=5)
            out, err = child.communicate(timeout=5)
            self.assertEqual(0, child.returncode, out + err)
            self.assertTrue((target / '.git').is_file())
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=5)

    def test_interruption_names_open_tool_and_ignores_closed_calls(self):
        for cli, opened, closed in (
            ('codex', dict(type='item.started', item=dict(id='x', type='command_execution', command='python3 migrate.py')),
             dict(type='item.completed', item=dict(id='x', type='command_execution', command='python3 migrate.py'))),
            ('grok', dict(type='tool_call', toolCallId='x', toolName='bash', rawInput=dict(command='python3 migrate.py')),
             dict(type='tool_call_update', toolCallId='x', status='completed')),
            ('codex', dict(type='response_item', payload=dict(type='function_call', call_id='x', name='exec_command',
                          arguments=json.dumps(dict(cmd='python3 migrate.py')))),
             dict(type='response_item', payload=dict(type='function_call_output', call_id='x', output='completed'))),
            ('codex', dict(type='response_item', payload=dict(type='custom_tool_call', call_id='x', name='exec_command',
                          input=json.dumps(dict(cmd='python3 migrate.py')))),
             dict(type='response_item', payload=dict(type='custom_tool_call_output', call_id='x', output='completed'))),
        ):
            with self.subTest(cli=cli):
                book = stream.Book(cli)
                book.observe_line(json.dumps(opened), time.time())
                progress = mock.Mock(book=book)
                text = supervisor.interruption_prompt({}, 'stall_silent', 'tool_timeout', progress)
                self.assertIn('python3 migrate.py', text)
                self.assertIn('supervisor, not the user', text)
                self.assertIn('Reason class: stall_silent', text)
                self.assertIn("check its effects before rerunning it; don't redo completed steps", text)
                book.observe_line(json.dumps(closed), time.time())
                text = supervisor.interruption_prompt({}, 'broken_stream', progress=progress)
                self.assertIn('unknown; check state first', text)
                self.assertNotIn('python3 migrate.py', text)
        book = stream.Book('agy')
        book.observe_line(json.dumps(dict(type='PLANNER_RESPONSE', tool_calls=[dict(name='run_command',
                          args=dict(CommandLine='npm test'))])), time.time())
        self.assertIn('run_command npm test', supervisor.interruption_prompt({}, 'broken_stream', progress=mock.Mock(book=book)))

    def test_interruption_recovers_tool_from_saved_stream(self):
        source = self.runs / 'interrupted.stream.jsonl'
        source.write_text(json.dumps(dict(type='item.started', item=dict(id='x', type='command_execution',
                                       command='npm run check'))) + '\n')
        run = dict(name='interrupted', cli='codex', started_at=a.now(), codex_stream=True,
                   progress_paths=[str(source)])
        self.assertIn('npm run check', supervisor.interruption_prompt(run, 'auth'))
        source.write_text('')
        self.assertIn('unknown; check state first', supervisor.interruption_prompt(run, 'auth'))

    def test_heal_resume_receives_captured_context(self):
        base = self.runs / 'heal'
        safety.atomic(str(base) + '.run.json', dict(name='heal', cli='codex', started_at=a.now(),
                      progress_paths=[], cwd=str(self.repo)))
        safety.atomic(str(base) + '.healing.json', dict(segment='heal', action='stream', reason='broken stream',
                      pid=os.getpid(), interruption='Captured open tool: npm test. Supervisor interrupted it.\n'))
        with mock.patch.object(supervisor, 'refuse_signal', return_value=None), \
                mock.patch.object(a, 'nudge_main', return_value=0) as resume:
            self.assertEqual(0, life.heal(base))
        self.assertIn('Captured open tool: npm test', resume.call_args.args[0][1])
        self.assertIn('under ~400 lines', resume.call_args.args[0][1])

    def test_doctor_compares_source_ignoring_only_installer_stamp_read_only(self):
        source, prefix = self.root / 'source/bin', self.root / 'prefix'
        source.mkdir(parents=True)
        prefix.mkdir()
        manifest = {'_source_bin': str(source), 'godfather_safety.py': 'saved hash', 'godfather': 'saved hash'}
        (prefix / '.godfather-install.json').write_text(json.dumps(manifest))
        (source / 'godfather_safety.py').write_text('SOURCE_BIN = None\nprint("ok")\n')
        (prefix / 'godfather_safety.py').write_text('SOURCE_BIN = "/installed/source/bin"\nprint("ok")\n')
        for root in (source, prefix):
            (root / 'godfather').write_text('#!/bin/sh\necho ok\n')
        before = {p: p.read_bytes() for p in prefix.iterdir()}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(subprocess, 'run', side_effect=AssertionError('not passive')):
            a.install_drift(prefix)
        self.assertEqual('install matches source\n', buf.getvalue())
        self.assertEqual(before, {p: p.read_bytes() for p in prefix.iterdir()})
        (source / 'godfather').write_text('changed source\n')
        with (prefix / 'godfather_safety.py').open('a') as out:
            out.write('# local change\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            a.install_drift(prefix)
        self.assertIn('install differs from source: godfather\n', buf.getvalue())
        self.assertIn('install differs from source: godfather_safety.py\n', buf.getvalue())
        self.assertNotIn('install matches source', buf.getvalue())
        # Historical manifests locate source using the installed stamp.
        manifest.pop('_source_bin')
        (prefix / '.godfather-install.json').write_text(json.dumps(manifest))
        buf = io.StringIO()
        with mock.patch.object(safety, 'SOURCE_BIN', str(source)), contextlib.redirect_stdout(buf):
            a.install_drift(prefix)
        self.assertIn('install differs from source: godfather\n', buf.getvalue())

    def test_doctor_missing_source_reports_each_file_and_never_claims_clean(self):
        prefix = self.root / 'prefix'
        prefix.mkdir()
        (prefix / '.godfather-install.json').write_text(json.dumps(dict(
            _source_bin=str(self.root / 'missing'), godfather='hash', **{'godfather_safety.py': 'hash'})))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            a.install_drift(prefix)
        self.assertIn('godfather unavailable', buf.getvalue())
        self.assertIn('godfather_safety.py unavailable', buf.getvalue())
        self.assertNotIn('install matches source', buf.getvalue())

    def test_done_excerpt_is_first_nonempty_line_bounded_and_consumer_safe(self):
        base = self.runs / 'report'
        line = 'Verified ' + 'x' * 200
        Path(str(base) + '.last.md').write_text(' \n\n' + line + '\nsecond line\n')
        detail = safety.done_detail(base, 'exit=0 3s tokens=1/2')
        self.assertTrue(detail.startswith('exit=0 3s tokens=1/2 report='))
        self.assertEqual(line[:160], json.loads(detail.split(' report=', 1)[1]))
        self.assertNotIn('\n', detail)
        self.assertEqual(detail, safety.done_detail(base, detail))
        safety.event(base, 'DONE', 'done')
        record = json.loads(Path(str(base) + '.events.jsonl').read_text())
        self.assertEqual(('DONE', 'report'), (record['kind'], record['name']))
        self.assertEqual(safety.done_detail(base, 'done'), record['detail'])
        self.assertEqual('done', safety.done_detail(self.runs / 'absent', 'done'))
        Path(str(base) + '.last.md').write_text(' \n\n')
        self.assertEqual('done', safety.done_detail(base, 'done'))

    def test_mcp_done_notification_contains_report_excerpt(self):
        base = self.runs / 'notify'
        safety.atomic(str(base) + '.run.json', dict(name='notify', run_id='notify', cli='codex', model='fake',
                      cwd=str(self.repo), status='done', exit_code=0, started_at=a.now(), ended_at=a.now(), progress_paths=[]))
        Path(str(base) + '.last.md').write_text('\nShipped the bounded copy.\nMore details.\n')
        events = mcp.events()['events']
        done = next(e for e in events if e['kind'] == 'DONE')
        self.assertEqual('exit=0 report="Shipped the bounded copy."', done['detail'])


class RoutedHardeningTests(unittest.TestCase):
    setUp = fixture.RouterTests.setUp
    config_models = fixture.RouterTests.config_models
    launch = fixture.RouterTests.launch
    calls = fixture.RouterTests.calls

    def test_agy_routed_and_direct_quota_resumes_keep_interruption_context(self):
        self.config_models(('agy', 'gemini-4-argon-high'), ('agy', 'claude-opus-5-5-high'))
        result = self.launch('routed-quota', QUOTA_MODELS='gemini-4-argon-high')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        prompt = self.calls()[-1]['prompt']
        self.assertIn('supervisor, not the user', prompt)
        self.assertIn('Reason class: quota', prompt)
        self.assertIn("don't redo completed steps", prompt)
        result = subprocess.run([str(ROOT / 'bin/agy-run'), str(self.prompt), 'direct-quota'],
                                env=dict(self.env, AGY_MODELS='gemini-4-argon-high claude-opus-5-5-high',
                                         QUOTA_MODELS='gemini-4-argon-high'), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        prompt = self.calls()[-1]['prompt']
        self.assertIn('supervisor, not the user', prompt)
        self.assertIn('Reason class: quota', prompt)
        self.assertIn("don't redo completed steps", prompt)

    def test_agy_capacity_and_auth_resumes_receive_retry_prompt(self):
        self.config_models(('agy', 'gemini-4-argon-high'))
        (self.config.parent / 'godfather.toml').write_text(
            '[[retry]]\nclass="capacity"\nattempts=1\nbackoff_secs=0\naction="resume"\n'
            '[[retry]]\nclass="auth"\nattempts=1\nbackoff_secs=0\naction="resume"\n')
        fake = fixture.FAKE.replace('if quota:\n', '''if os.getenv('FAILURE_ONCE') and '--conversation' not in args:
    error = 'Error: model at capacity' if os.environ['FAILURE_ONCE'] == 'capacity' else 'Error: authentication failed: not logged in'
    print(error, file=sys.stderr)
    sys.exit(1)
if quota:
''', 1)
        (self.root / 'fake/agy').write_text(fake)
        for reason in ('capacity', 'auth'):
            with self.subTest(reason=reason):
                result = self.launch('retry-' + reason, FAILURE_ONCE=reason)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                prompt = self.calls()[-1]['prompt']
                self.assertIn('supervisor, not the user', prompt)
                self.assertIn('Reason class: ' + reason, prompt)
                self.assertIn("don't redo completed steps", prompt)

    def test_queued_nudge_reopens_finished_session_and_preserves_report(self):
        self.config_models(('codex', 'fake'))
        result = self.launch('queued')
        self.assertEqual(0, result.returncode, result.stderr)
        base = self.root / 'runs/queued'
        with mock.patch.dict(os.environ, self.env, clear=True):
            run = safety.read(str(base) + '.run.json')
            run['status'] = 'running'
            safety.atomic(str(base) + '.run.json', run)
            with mock.patch.object(life.subprocess, 'Popen', return_value=mock.Mock(pid=987654321)), \
                    mock.patch.object(a, 'processes', return_value={}), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                life.deliver_nudge('queued', 'Also finish the regression check.')
            self.assertIn('reopens on completion', output.getvalue())
            self.assertNotIn('terminal status first', output.getvalue())
            run['status'] = 'done'
            safety.atomic(str(base) + '.run.json', run)
            self.assertEqual(0, life.execute_nudge(base))
        self.assertEqual(2, len(self.calls()))
        self.assertIn('Also finish the regression check.', self.calls()[-1]['prompt'])
        self.assertIn('resume', self.calls()[-1]['args'])
        self.assertTrue(Path(str(base) + '.last.1.md').read_text().strip())
        self.assertEqual('done', safety.read(str(base) + '.run.json')['status'])
        self.assertFalse(safety.read(str(base) + '.nudge.json')['pending'])

    def test_included_paths_survive_router_log_initialization(self):
        self.config_models(('codex', 'fake'))
        repo = self.root / 'repo'
        repo.mkdir()
        for args in (('init', '-q'), ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                    'commit', '--allow-empty', '-qm', 'base')):
            subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
        (repo / '.worktreeinclude').write_text('local.cfg\n')
        (repo / 'local.cfg').write_text('local config')
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '-p', 'Inspect local config.', 'include-log'],
                                env=dict(self.env, CWD=str(repo)), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('WORKTREE_INCLUDE "local.cfg"', (self.root / 'runs/include-log.models.log').read_text())

    def test_watch_done_line_appends_excerpt_and_status_stays_parseable(self):
        self.config_models(('codex', 'fake'))
        self.assertEqual(0, self.launch('visible').returncode)
        base = self.root / 'runs/visible'
        Path(str(base) + '.last.md').write_text('\nFixed the scratch size cap.\nDetail.\n')
        watcher = subprocess.Popen([str(ROOT / 'bin/godfather'), 'watch', '--replay', '--no-heal', '--poll-secs', '.05'],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = []
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if select.select([watcher.stdout], [], [], .2)[0]:
                    line = watcher.stdout.readline()
                    lines.append(line)
                    if line.startswith('DONE visible '):
                        break
        finally:
            watcher.terminate()
            out, err = watcher.communicate(timeout=5)
        out = ''.join(lines) + out
        self.assertEqual('', err)
        self.assertIn('DONE visible ', out)
        done = next(line for line in out.splitlines() if line.startswith('DONE visible '))
        self.assertIn('exit=0', done)
        self.assertIn('report="Fixed the scratch size cap."', done)
        status = subprocess.run([str(ROOT / 'bin/godfather'), 'status', '--json'], env=self.env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertEqual('done', next(row for row in json.loads(status.stdout) if row['name'] == 'visible')['status'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
