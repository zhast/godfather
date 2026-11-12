"""Ownership, devbox guards and conservative cleanup; no live workers/devboxes."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import agent_oversight as a
import godfather_policy as policy
import godfather_router as router
import godfather_safety as safety
import godfather_store as store
import godfather_plan_run as plan_run
import test_bb_hardening as hardening
import test_safety as fixture
import test_router as router_fixture

BOX = policy.devbox_settings(policy.document(fixture.DEVBOX_POLICY)['devbox'])


class ResourceTests(unittest.TestCase):
    setUp = hardening.HardeningTests.setUp
    git = hardening.HardeningTests.git
    init_repo = hardening.HardeningTests.init_repo

    def setUp(self):
        hardening.HardeningTests.setUp(self)
        fake = self.root / 'fake'
        fake.mkdir()
        box = fake / 'devbox'
        box.write_text('#!/bin/sh\nprintf \'{"attached":false}\\n\'\n')
        box.chmod(0o755)
        policy.config_dir().mkdir(parents=True)
        (policy.config_dir() / 'policy.toml').write_text(fixture.DEVBOX_POLICY)
        os.environ['PATH'] = str(fake) + os.pathsep + os.environ['PATH']

    def record(self, name, resources=None, status='done', age=8 * 86400, **extra):
        data = dict(name=name, run_id=name, cwd=str(self.repo), repo=str(self.repo), cli='codex',
                    model='fake', status=status, exit_code=0, started_at=time.time() - age - 10,
                    ended_at=time.time() - age, base_branch='main', progress_paths=[])
        if resources is not None:
            data['resources'] = resources
        data.update(extra)
        safety.atomic(self.runs / (name + '.run.json'), data)
        return data

    def tree(self, name):
        resources = []
        target, branch = router.create_worktree(self.repo, self.runs / 'worktrees' / name,
                                                'godfather/' + name, resources=resources)
        return target, branch, resources

    def init_repo(self):
        hardening.HardeningTests.init_repo(self)
        self.git('branch', '-M', 'main')

    def gc(self, yes=False, older='7d'):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, safety.gc_main(SimpleNamespace(yes=yes, older_than=older)))
        return out.getvalue()

    def commit(self, cwd):
        (cwd / 'new.txt').write_text('new commit\n')
        self.git('add', 'new.txt', cwd=cwd)
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'new', cwd=cwd)

    def test_created_and_adopted_ledgers_and_scratch(self):
        self.init_repo()
        target, branch, resources = self.tree('owned')
        self.assertEqual([dict(kind='worktree', path=str(target), owned=True),
                          dict(kind='branch', id=branch, owned=True)], resources)
        adopted = []
        safety.adopt_workspace(target, adopted, dict(attached=True, session=dict(machineId='existing-box', devSessionId='existing-session')))
        self.assertTrue(all(r['owned'] is False for r in adopted))
        self.assertIn(dict(kind='devbox', id='existing-box', owned=False), adopted)
        source = self.root / 'nongit'
        source.mkdir()
        (source / 'file').write_text('copy')
        ledger = []
        dest = safety.copy_scratch(source, self.runs / 'scratch/copy', resources=ledger)
        self.assertEqual([dict(kind='scratch', path=str(dest), owned=True)], ledger)

    def test_clean_merged_worktree_and_branch_dry_run_then_apply(self):
        self.init_repo()
        target, branch, resources = self.tree('clean')
        self.record('clean', resources)
        out = self.gc()
        self.assertIn('DRY RUN', out)
        self.assertEqual(2, out.count('\tremove\t'))
        self.assertTrue(target.exists())
        self.assertIn('Total bytes reclaimed: 0', out)
        with mock.patch.object(os, 'kill', side_effect=AssertionError('gc must never kill')):
            out = self.gc(yes=True)
        self.assertEqual(2, out.count('\tremoved\t'), out)
        self.assertFalse(target.exists())
        self.assertEqual('', self.git('branch', '--list', branch).stdout)
        self.assertRegex(out, r'Total bytes reclaimed: [1-9][0-9]*')

    def test_dirty_and_unpushed_commits_kept(self):
        self.init_repo()
        for name in ('dirty', 'unpushed'):
            target, branch, resources = self.tree(name)
            self.record(name, resources)
            if name == 'dirty':
                (target / 'untracked').write_text('precious')
            else:
                self.commit(target)
        out = self.gc(yes=True)
        self.assertEqual(2, out.count('\tkept\tdirty'), out)
        self.assertEqual(2, out.count('1 unmerged unpushed commits'), out)
        self.assertTrue((self.runs / 'worktrees/unpushed').exists())

    def test_remote_reachability_allows_pushed_commit_without_fetch(self):
        self.init_repo()
        target, branch, resources = self.tree('pushed')
        self.commit(target)
        self.git('update-ref', 'refs/remotes/origin/saved', self.git('rev-parse', 'HEAD', cwd=target).stdout.strip())
        self.record('pushed', resources)
        self.assertEqual(2, self.gc(yes=True).count('\tremoved\t'))

    def test_base_branch_merge_allows_cleanup_but_other_local_branch_and_tag_do_not(self):
        self.init_repo()
        target, branch, resources = self.tree('local')
        self.commit(target)
        self.git('branch', 'local-backup', branch)
        self.git('tag', 'local-tag', branch)
        self.record('local', resources)
        self.assertIn('1 unmerged unpushed commits', self.gc())
        self.git('merge', '--ff-only', branch)
        self.assertEqual(2, self.gc(yes=True).count('\tremoved\t'))

    def test_scratch_age_status_adoption_and_manual_devbox(self):
        for name, state, age, owned in (('old', 'done', 8*86400, True), ('young', 'done', 60, True),
                                       ('live', 'running', 8*86400, True), ('adopted', 'done', 8*86400, False),
                                       ('waiting', 'needs_input', 8*86400, True)):
            target = self.runs / 'scratch' / name
            target.mkdir(parents=True)
            (target / 'data').write_bytes(b'abc')
            self.record(name, [dict(kind='scratch', path=str(target), owned=owned)], state, age)
        self.record('devbox', [dict(kind='devbox', id='owned-box', owned=True)])
        out = self.gc(yes=True)
        self.assertIn('consider `devbox stop owned-box`', out)
        self.assertIn('newer than cutoff', out)
        self.assertIn('adopted; not owned', out)
        self.assertIn('run is not terminal', out)
        self.assertFalse((self.runs / 'scratch/old').exists())
        self.assertTrue((self.runs / 'scratch/live').exists())
        self.assertIn('Total bytes reclaimed: 3', out)

    def test_legacy_paths_and_branch_and_explicit_empty_ledger(self):
        self.init_repo()
        target, branch, _ = self.tree('legacy')
        self.record('legacy', cwd=str(target), branch=branch)
        ignored = self.runs / 'scratch/no-ledger-owner'
        ignored.mkdir(parents=True)
        self.record('empty', [], cwd=str(ignored))
        out = self.gc(yes=True)
        self.assertEqual(2, out.count('\tremoved\t'), out)
        self.assertTrue(ignored.exists())

    def test_legacy_branch_cleanup_retains_repo_identity_after_its_cwd_is_removed(self):
        self.init_repo()
        target, branch, _ = self.tree('legacy-cwd')
        self.record('legacy-cwd', cwd=str(target), repo=None, base_branch=None,
                    launcher_env=dict(GODFATHER_BRANCH=branch))
        out = self.gc(yes=True)
        self.assertEqual(2, out.count('\tremoved\t'), out)
        self.assertFalse(target.exists())
        self.assertEqual('', self.git('branch', '--list', branch).stdout)

    def test_symlinks_and_outside_paths_refused_without_following_targets(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'precious').write_text('keep')
        pool = self.runs / 'scratch'
        pool.mkdir()
        link = pool / 'link'
        link.symlink_to(outside, target_is_directory=True)
        for name, value in (('link', link), ('outside', outside), ('nested', link / 'nested')):
            self.record(name, [dict(kind='scratch', path=str(value), owned=True)])
        out = self.gc(yes=True)
        self.assertIn('symlink path refused', out)
        self.assertIn('path outside runs', out)
        self.assertTrue((outside / 'precious').exists())
        self.assertTrue(link.is_symlink())

    def test_scratch_internal_symlink_not_followed_or_counted(self):
        target = self.runs / 'scratch/safe'
        target.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'precious').write_bytes(b'abcde')
        (target / 'linked').symlink_to(outside, target_is_directory=True)
        self.record('safe', [dict(kind='scratch', path=str(target), owned=True)])
        out = self.gc(yes=True)
        self.assertFalse(target.exists())
        self.assertTrue((outside / 'precious').exists())
        self.assertIn('Total bytes reclaimed: 0', out)

    def test_live_adopter_protects_owned_resource(self):
        self.init_repo()
        target, branch, resources = self.tree('owner')
        self.record('owner', resources)
        self.record('adopter', [dict(r, owned=False) for r in resources], status='queued')
        out = self.gc(yes=True)
        self.assertEqual(2, out.count('run is not terminal'), out)
        self.assertTrue(target.exists())

    def test_git_inspection_and_removal_share_repo_lock(self):
        self.init_repo()
        _, _, resources = self.tree('lock')
        self.record('lock', resources)
        real, held, calls = safety.gc_git, [], []
        @contextlib.contextmanager
        def lock(repo):
            held.append(repo)
            yield
            held.pop()
        def git(repo, *args):
            if args[:2] in (('worktree', 'list'), ('worktree', 'remove'), ('branch', '-D')):
                self.assertTrue(held)
                calls.append(args)
            return real(repo, *args)
        with mock.patch.object(safety, 'git_lock', side_effect=lock), mock.patch.object(safety, 'gc_git', side_effect=git):
            self.gc(yes=True)
        self.assertTrue(any(c[:2] == ('branch', '-D') for c in calls))

    def test_gc_rechecks_reopened_run_after_waiting_for_git_lock(self):
        self.init_repo()
        target, _, resources = self.tree('reopened')
        self.record('reopened', resources)
        @contextlib.contextmanager
        def lock(repo):
            store.transition(self.runs, 'reopened', 'running', 'resume')
            yield
        with mock.patch.object(safety, 'git_lock', side_effect=lock):
            out = self.gc(yes=True)
        self.assertIn('run is not terminal', out)
        self.assertTrue(target.exists())
        self.assertNotIn('\tremoved\t', out)

    def test_router_ledger_without_worker_is_collectable_only_after_router_finishes(self):
        self.init_repo()
        target, _, resources = self.tree('no-worker')
        route = dict(name='no-worker', run_id='router-only', cwd=str(target), repo=str(self.repo),
                     base_branch='main', resources=resources, status='running',
                     started_at=time.time()-9*86400, ended_at=time.time()-8*86400)
        path = self.runs / 'no-worker.route.json'
        safety.atomic(path, route)
        self.assertIn('run is not terminal', self.gc(yes=True))
        self.assertTrue(target.exists())
        route['status'] = 'finished'
        safety.atomic(path, route)
        self.assertEqual(2, self.gc(yes=True).count('\tremoved\t'))

    def test_unavailable_base_ref_keeps_worktree_and_branch(self):
        self.init_repo()
        target, _, resources = self.tree('missing-base')
        self.record('missing-base', resources, base_branch='missing')
        out = self.gc(yes=True)
        self.assertNotIn('\tremoved\t', out)
        self.assertTrue(target.exists())

    def test_plan_integration_branch_cannot_count_as_its_own_base(self):
        self.init_repo()
        target, branch, resources = self.tree('integrate')
        base_sha = self.git('rev-parse', 'HEAD').stdout.strip()
        self.commit(target)
        store.insert_plan(self.runs, 'plan', 'plan', str(self.repo), base_sha, branch)
        store.insert_run(self.runs, 'plan/integrate', 'integrate', 'done', plan_id='plan',
                         cwd=str(target), worktree=str(target), branch=branch, base_sha=base_sha,
                         ended_at=time.time()-8*86400, payload=json.dumps(dict(resources=resources)))
        out = self.gc(yes=True)
        self.assertIn('1 unmerged unpushed commits', out)
        self.assertTrue(target.exists())
        # Even an erroneous self-referencing base cannot authorize deletion.
        self.record('self-base', resources, base_branch=branch)
        self.assertNotIn('\tremoved\t', self.gc(yes=True))

    def test_plan_provision_records_ownership_in_payload_and_preserves_it_on_reuse(self):
        self.init_repo()
        store.insert_run(self.runs, 'p/t', 'p-t', 'provisioning', cwd=str(self.repo), isolate=True)
        spec = dict(repo=str(self.repo), root=str(self.runs), id='p', git=True, base_sha='HEAD')
        task = dict(id='t', task_type='edit')
        row = dict(id='p/t', name='p-t', isolate=True, payload='{}')
        target, branch = plan_run._provision(spec, task, row)
        ledger = json.loads(row['payload'])['resources']
        self.assertEqual([dict(kind='worktree', path=str(target), owned=True),
                          dict(kind='branch', id=branch, owned=True)], ledger)
        plan_run._provision(spec, task, row)
        self.assertEqual(ledger, json.loads(row['payload'])['resources'])
        stored = next(r for r in safety.gc_runs(self.runs) if r['name'] == 'p-t')
        self.assertEqual(ledger, stored['resources'])
        adopted = dict(name='existing', isolate=True)
        plan_run._provision(spec, task, adopted)
        self.assertTrue(all(not r['owned'] for r in json.loads(adopted['payload'])['resources']))

    def test_attached_auto_adoption_uses_cwd_lock_with_wait_semantics(self):
        self.init_repo()
        attached = dict(attached=True, id='existing')
        args = SimpleNamespace(name='auto', self=True, max_fix_rounds=0, review_fix=False,
                               review_by=None, isolate=True, isolate_explicit=False, wait_lock=True)
        seen = []
        @contextlib.contextmanager
        def lock(cwd, wait=False):
            self.assertEqual(self.repo, cwd)
            self.assertTrue(wait)
            seen.append('locked')
            yield
            seen.append('released')
        with mock.patch.dict(os.environ, CWD=str(self.repo)), \
                mock.patch.object(router, 'check_devbox_isolation', return_value=attached), \
                mock.patch.object(safety, 'cwd_lock', side_effect=lock), \
                mock.patch.object(router, '_route', side_effect=lambda options: seen.append('routed') or 0), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, router.route(args))
        self.assertEqual(['locked', 'routed', 'released'], seen)
        self.assertIn(dict(kind='devbox', id='existing', owned=False), args.resources)

    def test_duration_validation(self):
        self.assertEqual(7*86400, safety.gc_duration('7d'))
        self.assertEqual(5400, safety.gc_duration('1.5h'))
        for invalid in ('-1d', 'NaNd', '7', '', '1year'):
            with self.assertRaises(ValueError):
                safety.gc_duration(invalid)

    def test_configured_session_ids_prefer_machine_then_dev_session(self):
        for session, expected in ((dict(machineId='machine-1', devSessionId='session-1'), 'machine-1'),
                                  (dict(machineId=None, devSessionId='session-1'), 'session-1'),
                                  (dict(devSessionId='session-1'), 'session-1')):
            with self.subTest(session=session):
                status = dict(contractVersion=1, attached=True, id='envelope-id', session=session)
                self.assertEqual(expected, safety.devbox_id(status, BOX['id_keys']))
                ledger = []
                safety.adopt_workspace(self.repo, ledger, status)
                self.assertIn(dict(kind='devbox', id=expected, owned=False), ledger)

    def test_global_flags_and_shell_help_cannot_bypass_devbox_ownership(self):
        context = dict(resources=[dict(kind='devbox', id='own', owned=True)])
        allow = dict(rules=[dict(legacy='*', verdict='allow')], devbox=BOX)
        for verb in ('stop', 'down', 'rm', 'delete', 'destroy'):
            for flags in (['--json'], ['--warm'], ['--verbose', '--data', 'shared']):
                for identifier, expected in (('own', 'allow'), ('other', 'deny')):
                    argv = ['devbox'] + flags + [verb, identifier, '--yes']
                    with self.subTest(argv=argv):
                        self.assertEqual(expected, policy.evaluate(argv, context, allow)['verdict'])
        for flag in ('-h', '--help'):
            for shell in ('bash', 'sh', 'zsh'):
                argv = [shell, '-c', 'devbox --json stop other --yes', flag]
                self.assertEqual('deny', policy.evaluate(argv, context, allow)['verdict'])
                argv = [shell, '-c', 'devbox stop own --help; devbox --warm stop other -y', flag]
                self.assertEqual('deny', policy.evaluate(argv, context, allow)['verdict'])
            self.assertEqual('allow', policy.evaluate(['devbox', 'stop', 'other', flag], context, allow)['verdict'])
            self.assertEqual('allow', policy.evaluate(['devbox', flag], context, allow)['verdict'])
            # A raw help dispatcher does not treat a trailing
            # help flag behind global flags as a top-level help request.
            self.assertEqual('deny', policy.evaluate(['devbox', '--json', 'stop', 'other', flag], context, allow)['verdict'])
        self.assertEqual([], policy.devbox_stops(['devbox', 'ssh', 'stop', 'other'], BOX))

    def test_reused_machine_attached_after_start_is_adopted(self):
        fake = self.root / 'fake/devbox'
        fake.write_text('#!/bin/sh\nprintf "reused machine existing-machine\\n"\n')
        fake.chmod(0o755)
        self.record('reused', [])
        sink = SimpleNamespace(buffer=io.BytesIO())
        with mock.patch.object(safety, 'devbox_status', return_value=dict(attached=False)), \
                mock.patch.object(safety, 'attached_devbox', return_value=dict(attached=True,
                    session=dict(machineId='existing-machine', devSessionId='existing-session'))), \
                mock.patch.object(sys, 'stdout', sink), mock.patch.object(sys, 'stderr', sink):
            self.assertEqual(0, safety.run_devbox(self.runs / 'reused', str(fake), ['--warm'], BOX))
        ledger = safety.read(self.runs / 'reused.run.json')['resources']
        self.assertIn(dict(kind='devbox', id='existing-machine', owned=False), ledger)

    def test_broken_devbox_consumers_keep_draining_both_pipes_and_record_creation(self):
        fake = self.root / 'fake/devbox'
        fake.write_text('#!' + sys.executable + '\nimport os\n'
                        'os.write(2,b"new warm machine machine-from-output\\n")\n'
                        'for fd in (1,2):\n'
                        ' for i in range(64): os.write(fd,b"x"*65536)\n')
        fake.chmod(0o755)
        code = '''import json, sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
import godfather_policy as policy
import godfather_safety as safety
class BrokenSink:
    @property
    def buffer(self): return self
    def write(self, data):
        if sys.argv[4] == 'write': raise BrokenPipeError('consumer closed')
        return len(data)
    def flush(self): raise OSError('consumer unavailable')
sink = BrokenSink()
with mock.patch.object(safety, 'devbox_status', return_value=None), \\
     mock.patch.object(safety, 'attached_devbox', return_value=None), \\
     mock.patch.object(sys, 'stdout', sink), mock.patch.object(sys, 'stderr', sink):
    rc = safety.run_devbox(sys.argv[2], sys.argv[3], ['--warm'], policy.load_policy()['devbox'])
assert rc == 0, rc
assert dict(kind='devbox', id='machine-from-output', owned=True) in safety.read(sys.argv[2]+'.run.json')['resources']
print('drained and recorded')
'''
        for failure in ('write', 'flush'):
            name = 'broken-' + failure
            self.record(name, [])
            result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'bin'), str(self.runs / name),
                                     str(fake), failure], env=safety.original_environment(),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual('drained and recorded\n', result.stdout)
            self.assertEqual('', result.stderr)

    def test_devbox_ownership_hard_rule_covers_verbs_ids_none_shell_and_overrides(self):
        context = dict(cwd=str(self.repo), resources=[dict(kind='devbox', id='own', owned=True),
                                                     dict(kind='devbox', id='adopted', owned=False)])
        allow = dict(rules=[dict(legacy='*', verdict='allow')], devbox=BOX)
        for verb in ('stop', 'down', 'rm', 'delete', 'destroy'):
            for identifier in ('adopted', 'other'):
                result = policy.evaluate(['devbox', verb, identifier], context, allow)
                self.assertEqual('deny', result['verdict'])
                self.assertEqual('devbox-ownership', result['rule'])
            self.assertEqual('allow', policy.evaluate(['devbox', verb, 'own'], context, allow)['verdict'])
        for args in (['devbox', 'stop', '--all'], ['sh', '-c', 'devbox stop other; echo done'],
                     ['devbox', 'stop', 'own', 'other']):
            self.assertEqual('deny', policy.evaluate(args, context, allow)['verdict'])
        with mock.patch.object(safety, 'attached_devbox', return_value=dict(attached=True, id='own')):
            self.assertEqual('allow', policy.evaluate(['devbox', 'stop'], context, allow)['verdict'])
        with mock.patch.object(safety, 'attached_devbox', return_value=dict(attached=True, id='adopted')):
            self.assertEqual('deny', policy.evaluate(['devbox', 'down'], context, allow)['verdict'])
        self.assertEqual('allow', policy.evaluate(['devbox', 'stop', '--help'], context, allow)['verdict'])


class RoutedResourceTests(unittest.TestCase):
    def setUp(self):
        router_fixture.RouterTests.setUp(self)
        for cli in ('codex', 'agy', 'grok'):
            (self.root / 'fake' / cli).write_text(fixture.SAFETY_FAKE)
        self.config_models(('codex', 'fake'))

    config_models = fixture.SafetyTests.config_models
    command = fixture.SafetyTests.command
    run_state = fixture.SafetyTests.run_state
    calls = fixture.SafetyTests.calls
    launch = fixture.SafetyTests.launch

    def test_ledger_visible_in_run_and_status(self):
        project = self.root / 'project'
        project.mkdir()
        result = self.command('run', '-p', 'OK', 'owned', CWD=str(project))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        run = self.run_state('owned')
        self.assertTrue(run['resources'][0]['owned'])
        result = self.command('status', '--json')
        row = next(r for r in json.loads(result.stdout) if r['name'] == 'owned')
        self.assertEqual(run['resources'], row['resources'])
        result = self.command('run', '--no-isolate', '-p', 'OK', 'adopted')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(all(not r['owned'] for r in self.run_state('adopted')['resources']))

    def fake_box(self, attached=False):
        (self.config.parent / 'policy.toml').write_text(fixture.DEVBOX_POLICY)
        fake = self.root / 'fake/devbox'
        fake.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path.home() / 'box-state.json'
args = sys.argv[1:]
if args == ['status', '--json']:
    print(state.read_text() if state.exists() else '{"attached":false}')
elif args == ['--warm']:
    if state.exists():
        print('Reusing devbox: existing-box')
    else:
        state.write_text(json.dumps(dict(attached=True, session=dict(machineId='created-box', devSessionId='created-session'))))
        print('new warm machine created-box', file=sys.stderr)
elif 'stop' in args:
    with (Path.home() / 'box-stops').open('a') as out:
        out.write(' '.join(args) + '\\n')
else:
    sys.exit(3)
''')
        fake.chmod(0o755)
        if attached:
            (self.root / 'box-state.json').write_text(json.dumps(dict(attached=True, session=dict(machineId='existing-box', devSessionId='existing-session'))))
        snippet = '''import subprocess
if os.getenv('BOX_EXERCISE'):
    subprocess.run(['devbox', '--warm'], check=True)
    result = subprocess.run(['devbox', '--json', 'stop', os.environ['BOX_STOP'], '--yes'])
    (Path.home() / ('box-result-' + model)).write_text(str(result.returncode))
'''
        for cli in ('codex', 'grok'):
            (self.root / 'fake' / cli).write_text(router_fixture.FAKE.replace(
                "if cli == 'codex' and os.getenv('CODEX_NATIVE'):", snippet + "\nif cli == 'codex' and os.getenv('CODEX_NATIVE'):", 1))

    def test_created_box_can_stop_and_owned_ledger_survives_fallback(self):
        self.fake_box()
        self.config_models(('codex', 'first'), ('grok', 'second'))
        result = self.launch('box', BOX_EXERCISE='1', BOX_STOP='created-box',
                             QUOTA_MODELS='first', GODFATHER_SHIMS='enforce')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for name in ('box', 'box#2'):
            self.assertIn(dict(kind='devbox', id='created-box', owned=True), self.run_state(name)['resources'])
        self.assertEqual('0', (self.root / 'box-result-first').read_text())
        self.assertEqual('0', (self.root / 'box-result-second').read_text())
        self.assertEqual(['--json stop created-box --yes', '--json stop created-box --yes'], (self.root / 'box-stops').read_text().splitlines())

    def test_agy_session_fallback_and_nudge_preserve_owned_workspace(self):
        self.config_models(('agy', 'gemini-4-argon-high'), ('agy', 'claude-opus-5-5-high'))
        project = self.root / 'project'
        project.mkdir()
        result = self.command('run', '-p', 'OK', 'agy-ledger', CWD=str(project),
                              QUOTA_MODELS='gemini-4-argon-high')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        first = self.run_state('agy-ledger')['resources']
        self.assertEqual(first, self.run_state('agy-ledger#2')['resources'])
        self.assertTrue(first[0]['owned'])
        result = self.command('nudge', '--wait', 'agy-ledger', 'Verify ownership.')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(first, self.run_state('agy-ledger#2')['resources'])

    def test_reused_attached_box_is_adopted_and_stop_is_blocked(self):
        self.fake_box(attached=True)
        result = self.launch('adopt-box', BOX_EXERCISE='1', BOX_STOP='existing-box', GODFATHER_SHIMS='enforce')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(dict(kind='devbox', id='existing-box', owned=False), self.run_state('adopt-box')['resources'])
        self.assertEqual('126', (self.root / 'box-result-fake').read_text())
        self.assertFalse((self.root / 'box-stops').exists())

    def test_gc_cli_dry_fixture(self):
        runroot = self.root / 'runs'
        target = runroot / 'scratch/old'
        target.mkdir(parents=True)
        (target / 'data').write_text('old report workspace')
        data = dict(name='old', cli='codex', model='fake', cwd=str(target), status='done', exit_code=0,
                    run_id='old', started_at=a.now(), ended_at=time.time()-8*86400, progress_paths=[],
                    resources=[dict(kind='scratch', path=str(target), owned=True)])
        with mock.patch.dict(os.environ, self.env, clear=True):
            safety.atomic(runroot / 'old.run.json', data)
        result = self.command('gc')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('old\tscratch\t', result.stdout)
        self.assertIn('\tremove\told owned scratch copy', result.stdout)
        self.assertTrue(target.exists())
        self.assertEqual(2, self.command('gc', '--older-than', 'bad').returncode)


if __name__ == '__main__':
    unittest.main(verbosity=2)
