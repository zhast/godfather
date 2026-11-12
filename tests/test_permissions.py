"""Native permission profiles, audit shims, and the effect-model fixes they rely on."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import agent_oversight as a
import godfather_permissions as perms
import godfather_policy as policy
import godfather_safety as safety


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        # OS temp roots are writable, so the CLI-scratch cases live outside them.
        self.home = Path(tempfile.mkdtemp(prefix='godfather-perm-', dir=str(Path.home())))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.base = self.runs / 'task'
        gitconfig = self.root / 'gitconfig'
        gitconfig.write_text('[user]\n\tname = Godfather Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n')
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.root / 'config'),
                        XDG_STATE_HOME=str(self.root / 'state'), AGENT_RUNS_DIR=str(self.runs),
                        PYTHONDONTWRITEBYTECODE='1', GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_SYSTEM='/dev/null')
        for key in ('CODEX_HOME', 'CODEX_SANDBOX', 'GODFATHER_NATIVE', 'GODFATHER_SHIMS', 'GODFATHER_CONTAINER',
                    'GODFATHER_CONTAINER_IMAGE', 'GODFATHER_TASK_TYPE'):
            self.env.pop(key, None)
        self.patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.fake = self.root / 'fake-bin'
        self.fake.write_text('#!/bin/sh\nprintf "EXECUTED\\n"\n')
        self.fake.chmod(0o700)
        safety.atomic(str(self.base) + '.run.json', dict(name='task', cli='codex', status='running', cwd=str(self.work),
                                                          started_at=a.now(), launcher_pid=os.getpid()))
        self.context = dict(cwd=str(self.work), base=str(self.base), scratch=[],
                            real={'chmod': str(self.fake), 'tee': str(self.fake), 'rm': str(self.fake)})
        safety.atomic(str(self.base) + '.safety.json', self.context)

    def evaluate(self, *argv, cwd=None, **context):
        merged = dict(self.context, **context)
        return policy.evaluate(list(argv), merged, cwd=cwd or merged['cwd'])

    def command(self, *args):
        return subprocess.run([str(ROOT / 'bin/godfather')] + list(args), env=self.env, capture_output=True, text=True, timeout=10)

    def shim(self, *args, enforce=False):
        script = 'import sys; sys.path.insert(0, ' + repr(str(ROOT / 'bin')) + '); from godfather_safety import shim; sys.exit(shim(' + repr(str(self.base)) + ', sys.argv.pop(1)))'
        env = dict(self.env, GODFATHER_BASE=str(self.base), ASK_TIMEOUT='5')
        if enforce:
            env['GODFATHER_SHIMS'] = 'enforce'
        return subprocess.Popen([sys.executable, '-c', script] + list(args), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def born(self, path):
        st = path.stat()
        return getattr(st, 'st_birthtime', st.st_ctime)

    def test_stream_devices_are_not_file_writes(self):
        for target in ('/dev/null', '/dev/stdout', '/dev/stderr'):
            for argv in (['curl', '-o', target, '-w', '%{http_code}', 'https://example.test'],
                         ['curl', '--output=' + target, 'https://example.test'],
                         ['curl', '-o' + target, 'https://example.test'],
                         ['tee', target], ['zsh', '-c', 'curl -w "%{http_code}" https://example.test 2>' + target],
                         ['bash', '-c', 'curl -o ' + target + ' https://example.test 2>/dev/null']):
                with self.subTest(argv=argv):
                    result = self.evaluate(*argv)
                    self.assertEqual(('allow', []), (result['verdict'], result['targets']))
        self.assertEqual('ask', self.evaluate('zsh', '-c', 'git push 2>/dev/null')['verdict'])
        self.assertEqual('ask', self.evaluate('rm', '/dev/null')['verdict'])
        self.assertEqual('ask', self.evaluate('zsh', '-c', 'rm /dev/null')['verdict'])
        self.assertEqual('deny', self.evaluate('curl', '-o', str(self.home / '.env'))['verdict'])

    def test_curl_all_output_channels_and_option_forms(self):
        url = 'https://example.test'
        pairs = [('-o', '--output'), ('-D', '--dump-header'), ('-c', '--cookie-jar'),
                 (None, '--trace'), (None, '--trace-ascii'), (None, '--libcurl'),
                 (None, '--stderr'), (None, '--output-dir')]
        for short, long in pairs:
            for target in ('/dev/null', '/dev/stdout', '/dev/stderr', '-', 'hdrs.txt'):
                forms = [[long, target], [long + '=' + target]]
                if short:
                    forms += [[short, target], [short + target], ['-sS' + short[1:], target], ['-s' + short[1:] + target]]
                for form in forms:
                    with self.subTest(form=form):
                        result = self.evaluate('curl', '-o', '/dev/null', *form, url)
                        self.assertEqual('ask' if target == 'hdrs.txt' else 'allow', result['verdict'])
        for flag in ('-w', '--write-out'):
            for target in ('/dev/null', '/dev/stdout', '/dev/stderr', '-', 'log.txt'):
                for prefix in ('', '>>'):
                    value = '%output{' + prefix + target + '}done'
                    forms = [[flag, value], [flag + value] if flag == '-w' else [flag + '=' + value]]
                    for form in forms:
                        with self.subTest(form=form):
                            self.assertEqual('ask' if target == 'log.txt' else 'allow',
                                             self.evaluate('curl', '-o', '/dev/null', *form, url)['verdict'])
            self.assertEqual('allow', self.evaluate('curl', '-o', '/dev/null', flag, '@format.txt', url)['verdict'])
        for form in (['-O'], ['--remote-name'], ['--remote-name-all'], ['-J'],
                     ['--remote-header-name'], ['-sSLo', '/dev/null'], ['-sD', 'hdrs.txt'], ['-Dhdrs.txt'],
                     ['-K', '/dev/null'], ['--config', '/dev/null'], ['--config=/dev/null'], ['-Kconfig']):
            with self.subTest(form=form):
                expected = 'allow' if form == ['-sSLo', '/dev/null'] else 'ask'
                self.assertEqual(expected, self.evaluate('curl', '-o', '/dev/null', *form, url)['verdict'])

    def test_curl_cache_writes_and_output_dir_resolution(self):
        url = 'https://example.test'
        for flag in ('--etag-save', '--hsts', '--alt-svc'):
            for target in ('/dev/null', '/dev/stdout', '/dev/stderr', 'cache.txt', '/external/cache.txt', '-'):
                for form in ([flag, target], [flag + '=' + target]):
                    with self.subTest(form=form):
                        result = self.evaluate('curl', '-o', '/dev/null', *form, url)
                        self.assertEqual('allow' if target.startswith('/dev/') else 'ask', result['verdict'])
                        if target in ('cache.txt', '-'):
                            self.assertIn(str(self.work / target), result['targets'])
            self.assertEqual('deny', self.evaluate('curl', '-o', '/dev/null', flag, str(self.home / '.env'), url)['verdict'])
        for flag in ('--hsts', '--alt-svc'):
            self.assertEqual('allow', self.evaluate('curl', flag, '', url)['verdict'])
        for directory in (str(self.work / 'downloads'), '/external/downloads'):
            for form in (['--output-dir', directory], ['--output-dir=' + directory]):
                for output in (['-o', 'nested/file.txt'], ['-onested/file.txt'], ['--output=nested/file.txt']):
                    for arguments in (form + output, output + form):
                        with self.subTest(arguments=arguments):
                            result = self.evaluate('curl', '--create-dirs', *arguments, url)
                            self.assertEqual('ask', result['verdict'])
                            self.assertIn(directory + '/nested/file.txt', result['targets'])
                            self.assertIn(directory + '/nested', result['targets'])
                            self.assertNotIn(str(self.work / 'nested/file.txt'), result['targets'])
        self.assertEqual('allow', self.evaluate('curl', '--output-dir=/dev/null', '-o/dev/null', url)['verdict'])
        result = self.evaluate('curl', '--output-dir', '/external/first', '-o', 'one', url,
                               '--next', '-o', 'two', url)
        self.assertEqual('ask', result['verdict'])
        self.assertIn('/external/first/one', result['targets'])
        self.assertIn(str(self.work / 'two'), result['targets'])
        # Cache/header paths stay relative to CWD; only body output uses output-dir.
        result = self.evaluate('curl', '--output-dir', '/external/body', '-o', 'file', '--hsts', 'cache', url)
        self.assertIn(str(self.work / 'cache'), result['targets'])
        self.assertNotIn('/external/body/cache', result['targets'])

    def test_learned_download_rule_generalizes_argv_but_not_targets(self):
        question = dict(id='download', effect=dict(command=['curl', '-o', '/external/report.txt', 'https://one.test'],
                        targets=['/external/report.txt'], cwd=str(self.work), effect='write downloaded files'))
        policy.remember(question, self.base)
        self.assertEqual('allow', self.evaluate('curl', '-s', '-w', '%{http_code}', '-o', '/external/report2.txt', 'https://two.test')['verdict'])
        self.assertEqual('ask', self.evaluate('curl', '-o', '/elsewhere/report.txt', 'https://two.test')['verdict'])
        self.assertEqual('deny', self.evaluate('curl', '-o', str(self.home / '.env'), 'https://two.test')['verdict'])
        self.assertEqual('ask', self.evaluate('rm', '/external/report.txt')['verdict'])
        # A changed argv may introduce a second file write or upload; that is a new decision.
        self.assertEqual('ask', self.evaluate('curl', '-o', '/external/report.txt', '--cookie-jar', '/elsewhere/cookies', 'https://two.test')['verdict'])
        self.assertEqual('ask', self.evaluate('curl', '-o', '/external/report.txt', '--upload-file', 'payload', 'https://two.test')['verdict'])
        self.assertEqual('ask', self.evaluate('curl', '-o', '/external/report.txt', '-w', '%output{/elsewhere/log}done', 'https://two.test')['verdict'])

    def test_chmod_of_a_file_created_under_cli_scratch_is_metadata(self):
        script = self.home / '.grok' / 'long-running-background-tasks' / 'watch_index.sh'
        script.parent.mkdir(parents=True)
        script.write_text('#!/bin/sh\n')
        created = self.born(script) - 5
        effect = self.evaluate('chmod', '755', str(script), started_at=created)
        self.assertEqual('allow', effect['verdict'])
        self.assertEqual('change file metadata', effect['effect'])
        self.assertNotEqual('delete or overwrite files', effect['effect'])
        self.assertEqual('ask', self.evaluate('chmod', '+x', str(script), started_at=self.born(script) + 5)['verdict'])
        self.assertEqual('ask', self.evaluate('chmod', '+x', str(script), started_at=None)['verdict'])
        missing = self.home / '.grok' / 'not-written-yet.sh'
        self.assertEqual('allow', self.evaluate('chmod', '+x', str(missing), started_at=created)['verdict'])
        self.assertEqual('allow', self.evaluate('chmod', '+x', str(self.work / 'local.sh'))['verdict'])

    def test_learned_rule_covers_the_cli_tree_and_prints_it(self):
        script = self.home / '.grok' / 'long-running-background-tasks' / 'watch_index.sh'
        script.parent.mkdir(parents=True)
        script.write_text('#!/bin/sh\n')
        other = self.home / '.grok' / 'other' / 'helper.sh'
        other.parent.mkdir(parents=True)
        other.write_text('#!/bin/sh\n')
        self.context['started_at'] = self.born(script) + 5
        safety.atomic(str(self.base) + '.safety.json', self.context)
        child = self.shim('chmod', '+x', str(script), enforce=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                questions = safety.control(self.base).get('questions', [])
                if questions and questions[-1].get('answer') is None:
                    break
                time.sleep(.02)
            else:
                self.fail('no pending decision')
            rejected = self.command('answer', 'task', 'allowish')
            self.assertEqual(2, rejected.returncode, rejected.stderr)
            self.assertIn('unrecognized', rejected.stderr)
            self.assertIsNone(safety.control(self.base)['questions'][-1].get('answer'))
            accepted = self.command('answer', 'task', 'allow+remember (indexer under grok scratch)')
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertIn('Learned rule', accepted.stderr)
            self.assertIn(str(self.home / '.grok') + '/**', accepted.stderr)
            self.assertIn('chmod +x under', accepted.stderr)
            out, err = child.communicate(timeout=5)
            self.assertEqual(0, child.returncode, err)
            self.assertIn('EXECUTED', out)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
        saved = safety.control(self.base)['questions'][-1]
        self.assertEqual('allow+remember', saved['answer'])
        self.assertEqual('indexer under grok scratch', saved['reason'])
        rule = policy.load_policy()['rules'][0]
        self.assertEqual('cli-tree', rule['scope'])
        self.assertEqual(['+x'], rule['args'])
        self.assertEqual([str((self.home / '.grok').resolve()) + '/**'], rule['targets'])
        self.assertNotIn('summary', (policy.config_dir() / 'policy.learned.toml').read_text())
        self.assertEqual('allow', self.evaluate('chmod', '+x', str(other), started_at=self.born(other) + 5)['verdict'])
        self.assertEqual('ask', self.evaluate('chmod', '-R', '+x', str(other), started_at=self.born(other) + 5)['verdict'])
        self.assertEqual('deny', self.evaluate('chmod', '+x', str(self.home / '.grok' / '.env'))['verdict'])
        records = [json.loads(line) for line in (policy.state_dir() / 'incidents.jsonl').read_text().splitlines()]
        self.assertTrue(any(row.get('reason') == 'indexer under grok scratch' and row.get('answer') == 'allow+remember' for row in records))

    def test_paths_outside_a_cli_tree_stay_narrow(self):
        question = dict(id='reviewed', effect=dict(command=['chmod', '+x', '/external/file'], targets=['/external/file'], cwd=str(self.work)))
        rule = policy.remember(question, self.base)
        self.assertNotIn('scope', rule)
        self.assertEqual('allow', self.evaluate('chmod', '+x', '/external/file-next')['verdict'])
        self.assertEqual('ask', self.evaluate('chmod', '+x', '/external/sub/file')['verdict'])
        self.assertNotIn('cli-tree', (policy.config_dir() / 'policy.learned.toml').read_text())

    def test_verdict_answer_accepts_a_reason_and_rejects_unknown_text(self):
        self.assertEqual(('allow', 'the file is ours'), perms.parse_choice('allow (the file is ours)', 'allow|deny|allow+remember'))
        self.assertEqual(('allow', None), perms.parse_choice('allow', 'allow|deny|allow+remember'))
        self.assertEqual(('make the file', None), perms.parse_choice('make the file', ''))
        with self.assertRaises(ValueError):
            perms.parse_choice('allowish', 'allow|deny|allow+remember')
        child = self.shim('rm', '/external/file', enforce=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                questions = safety.control(self.base).get('questions', [])
                if questions and questions[-1].get('answer') is None:
                    break
                time.sleep(.02)
            else:
                self.fail('no pending decision')
            rejected = self.command('answer', 'task', 'allow (not quite)')
            self.assertEqual(0, rejected.returncode, rejected.stderr)
            self.assertEqual('allow', safety.control(self.base)['questions'][-1]['answer'])
            self.assertEqual('not quite', safety.control(self.base)['questions'][-1]['reason'])
            out, err = child.communicate(timeout=5)
            self.assertEqual(0, child.returncode, err)
            self.assertIn('EXECUTED', out)
            self.assertNotIn('DENY', err)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def test_index_only_restore_is_allowed_and_worktree_restore_is_not(self):
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
        (repo / '.env').write_text('SECRET=1\n')
        (repo / 'keep.txt').write_text('ok\n')
        subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
        staged = self.evaluate('git', 'restore', '--staged', ':/', cwd=str(repo))
        self.assertEqual('allow', staged['verdict'])
        self.assertEqual([], staged['targets'])
        self.assertEqual('update the git index', staged['effect'])
        short = self.evaluate('git', 'restore', '-S', ':/', cwd=str(repo))
        self.assertEqual('allow', short['verdict'])
        self.assertEqual([], short['targets'])
        combined = self.evaluate('git', 'restore', '--staged', '--worktree', ':/', cwd=str(repo))
        self.assertEqual('deny', combined['verdict'])
        self.assertIn(str((repo / '.env').resolve()), combined['targets'])
        both = self.evaluate('git', 'restore', '-SW', 'keep.txt', cwd=str(repo))
        self.assertEqual('discard files', both['effect'])
        self.assertTrue(both['targets'])
        policy.config_dir().mkdir(parents=True, exist_ok=True)
        (policy.config_dir() / 'policy.toml').write_text('[[outward]]\nbinary = "acme"\ncommands = ["apps"]\n')
        self.assertEqual('allow', self.evaluate('acme', 'apps', 'deploy', '--help')['verdict'])
        self.assertEqual('ask', self.evaluate('acme', 'apps', 'deploy')['verdict'])

    def test_audit_execs_a_secret_write_and_enforce_still_denies_it(self):
        target = self.home / '.env.audit'
        child = self.shim('tee', str(target))
        out, err = child.communicate(input='secret', timeout=5)
        self.assertEqual(0, child.returncode, err)
        self.assertIn('EXECUTED', out)
        self.assertFalse(target.exists())
        entry = json.loads((self.base.parent / 'task.audit.log').read_text().splitlines()[-1])
        self.assertEqual('audit', entry['mode'])
        self.assertEqual('deny', entry['verdict'])
        record = json.loads((policy.state_dir() / 'incidents.jsonl').read_text().splitlines()[-1])
        self.assertEqual('audit', record['verdict'])
        self.assertEqual('deny', record['answer'])
        self.assertEqual('shim-audit', record['source'])
        denied = self.shim('tee', str(target), enforce=True)
        out, err = denied.communicate(input='secret', timeout=5)
        self.assertEqual(126, denied.returncode, err)
        self.assertIn('Godfather DENY', err)
        self.assertNotIn('EXECUTED', out)
        self.assertFalse(target.exists())

    def test_process_inspection_policy_is_declarative_and_keeps_other_guards(self):
        os.environ['GODFATHER_NATIVE'] = '1'
        profile = perms.write_profile(self.base, dict(cli='codex', cwd=str(self.work)))
        native = Path(profile['native_rules_file'])
        self.assertIn('pattern = ["pgrep"]', native.read_text())
        config = policy.config_dir() / 'policy.toml'
        config.write_text('process_inspection = false\n')
        disabled = perms.write_profile(self.base, dict(cli='codex', cwd=str(self.work)))
        self.assertEqual([], disabled['allow_bins'])
        self.assertEqual(perms.SIGNAL_RULES, native.read_text())
        self.assertEqual('ask', self.evaluate('git', 'push')['verdict'])
        self.assertEqual('deny', self.evaluate('tee', str(self.home / '.env'))['verdict'])
        config.write_text('process_inspection = "all"\n')
        with self.assertRaisesRegex(ValueError, 'boolean'):
            policy.load_policy()

    def test_inspection_rules_cover_self_expansion_without_allowing_shell_effects(self):
        # Starlark rules use this Python-compatible subset. Exercise the actual
        # generated declarations without invoking a real model CLI in the suite.
        rules = []
        exec(perms.RULES + perms.SIGNAL_RULES, {'prefix_rule': lambda **rule: rules.append(rule)})

        def decisions(argv):
            return [rule['decision'] for rule in rules
                    if len(argv) >= len(rule['pattern']) and all(
                        value in pattern if isinstance(pattern, list) else value == pattern
                        for value, pattern in zip(argv, rule['pattern']))]

        for argv in (['ps', '-p', '123', '-o', 'pid,comm'], ['ps', '-o', 'pid,comm'],
                     ['pgrep', '-f', 'godfather'], ['pgrep', '-l', 'godfather'],
                     ['/bin/zsh', '-lc', 'ps -p $$ -o pid,comm'],
                     ['/bin/bash', '-c', 'ps -p "$$" -o pid,comm']):
            self.assertIn('allow', decisions(argv), argv)
        for script in ('ps -p $$; kill 123', 'ps -p $(touch owned)',
                       'ps -p $$ > file', 'ps -p $$ && git push', 'pgrep -f "$PATTERN"'):
            self.assertNotIn('allow', decisions(['/bin/zsh', '-lc', script]), script)
        for binary in ('kill', '/bin/kill', 'pkill', '/usr/bin/pkill'):
            self.assertEqual(['prompt'], decisions([binary, '123']))
            self.assertEqual('ask', self.evaluate(binary, '123')['verdict'])
        self.assertEqual('ask', self.evaluate('bash', '-c', 'kill 123')['verdict'])
        self.assertEqual('ask', self.evaluate('git', 'push')['verdict'])
        self.assertEqual('deny', self.evaluate('tee', str(self.home / '.env'))['verdict'])

    def test_native_profiles_and_container_command(self):
        os.environ['GODFATHER_NATIVE'] = '1'
        base = self.runs / 'codex'
        run = dict(cli='codex', cwd=str(self.work), base=str(base), task_type='edit')
        profile = perms.write_profile(base, run)
        self.assertEqual('danger-full-access', profile['sandbox'])
        self.assertEqual('never', profile['approval_policy'])
        self.assertFalse(profile['honors_deny_globs'])
        self.assertIn('ps', (Path(str(base) + '.permissions.rules')).read_text())
        native = Path(profile['native_rules_file'])
        self.assertEqual(self.home / '.codex/rules/godfather-process-inspection.rules', native)
        self.assertEqual(perms.RULES + perms.SIGNAL_RULES, native.read_text())
        for prefix in (['ps'], ['/bin/ps'], ['pgrep'], ['/usr/bin/pgrep'], ['lsof', '-p'], ['top', '-l', '1']):
            self.assertIn('pattern = ' + json.dumps(prefix), native.read_text())
        self.assertNotIn('pattern = ["top"]', native.read_text())
        self.assertIn('pattern = ["pkill"], decision = "prompt"', native.read_text())
        command = ['codex', 'exec', '-s', 'danger-full-access', '-c', 'approval_policy="never"', '-']
        rewritten = perms.apply_native(command, run, profile)
        self.assertIn('danger-full-access', rewritten)
        self.assertIn('approval_policy="never"', rewritten)
        self.assertFalse(any(part.startswith(('permissions.', 'default_permissions=')) for part in rewritten))
        self.assertFalse(any(part.startswith('execpolicy.user_rules') for part in rewritten))
        self.assertEqual('-', rewritten[-1])
        self.assertFalse(any('sandbox-exec' in str(part) for part in rewritten))
        self.assertEqual(str(base) + '.permissions.json', run['permission_profile'])
        os.environ['CODEX_SANDBOX'] = 'danger-full-access'
        forced = perms.compile_profile(run)
        self.assertFalse(forced['honors_deny_globs'])
        self.assertEqual(command, perms.apply_native(list(command), run, forced))
        os.environ.pop('CODEX_SANDBOX')
        grok = dict(run, cli='grok')
        grok_profile = perms.compile_profile(grok)
        grok_cmd = perms.apply_native(['grok', '--sandbox', 'off', '--prompt-file', 'brief.md'], grok, grok_profile)
        self.assertIn('--always-approve', grok_cmd)
        self.assertEqual('off', grok_cmd[grok_cmd.index('--sandbox') + 1])
        self.assertIn('--deny', grok_cmd)
        self.assertIn('--allow', grok_cmd)
        self.assertIn('Edit(**/.env*)', grok_cmd)
        agy = dict(run, cli='agy')
        agy_profile = perms.compile_profile(agy)
        agy_cmd = perms.apply_native(['agy', '--model', 'gemini', '--dangerously-skip-permissions', '-p', 'go'], agy, agy_profile)
        self.assertEqual('accept-edits', agy_profile['permission_mode'])
        self.assertEqual(['agy', '--mode', 'accept-edits'], agy_cmd[:3])
        self.assertNotIn('--dangerously-skip-permissions', agy_cmd)
        self.assertFalse(agy_profile['honors_deny_globs'])
        settings = json.loads(agy_cmd[agy_cmd.index('--settings') + 1])
        self.assertEqual(['read_file(/)', 'command(*)'] + agy_profile['agy_allow'],
                         settings['permissions']['allow'])
        self.assertEqual(agy_profile['agy_deny'], settings['permissions']['deny'])
        os.environ.pop('GODFATHER_NATIVE')
        self.assertEqual(command, perms.apply_native(list(command), run, profile))
        repo = str(self.work.resolve())
        boxed = perms.container_argv('/usr/bin/docker', 'cli:test', repo, ['/host/bin/codex', 'exec'], 'research')
        self.assertEqual(['/usr/bin/docker', 'run', '--rm', '-i', '--network', 'none', '-v', repo + ':' + repo, '-w', repo], boxed[:10])
        self.assertEqual('--user', boxed[10])
        self.assertEqual('codex', boxed[13])
        self.assertEqual(['exec'], boxed[14:])
        self.assertEqual(1, boxed.count('-v'))
        self.assertNotIn(str(self.home), boxed)
        editing = perms.container_argv('/usr/bin/docker', 'cli:test', repo, ['/host/bin/grok'], 'edit')
        self.assertEqual('bridge', editing[editing.index('--network') + 1])
        with self.assertRaises(ValueError) as caught:
            perms.wrap_container(['codex', 'exec'], dict(cwd=str(self.work)))
        self.assertIn('host', str(caught.exception))

    def test_agy_merges_session_settings_and_replaces_auto_mode(self):
        profile = dict(agy_allow=['write_file(/workspace/)'], agy_deny=['write_file(**/.env)'])
        existing = dict(theme='dark', permissions=dict(
            allow=['read_file(/)', 'command(echo)'], deny=['write_file(/protected/)'],
            extra='preserved'))
        original = json.dumps(existing)
        for mode in (None, 'auto', 'accept-edits', 'default'):
            for inline in (False, True):
                with self.subTest(mode=mode, inline=inline):
                    supplied = dict(profile, permission_mode=mode)
                    flags = (['--mode=auto', '--settings=' + json.dumps(existing)] if inline else
                             ['--mode', 'auto', '--settings', json.dumps(existing)])
                    command = ['agy', *flags, '--dangerously-skip-permissions', '-p', '--settings']
                    argv = perms._apply_agy(command, supplied)
                    self.assertEqual(mode if mode and mode != 'auto' else 'accept-edits', argv[2])
                    self.assertEqual(['-p', '--settings'], argv[-2:])
                    self.assertNotIn('--dangerously-skip-permissions', argv)
                    settings = json.loads(argv[argv.index('--settings') + 1])
                    self.assertEqual(dict(theme='dark', permissions=dict(
                        allow=['read_file(/)', 'command(echo)', 'command(*)', 'write_file(/workspace/)'],
                        deny=['write_file(/protected/)', 'write_file(**/.env)'], extra='preserved')), settings)
                    self.assertEqual(argv, perms._apply_agy(argv, supplied))
                    self.assertIn('--dangerously-skip-permissions', command)
        self.assertEqual(original, json.dumps(existing))

    def test_agy_combines_settings_flags_without_losing_permissions(self):
        profile = dict(agy_allow=['write_file(/workspace/)'], agy_deny=['write_file(**/.env)'])
        argv = perms._apply_agy([
            'agy', '--settings', json.dumps(dict(theme='dark', permissions=dict(allow=['command(echo)']))),
            '--settings=' + json.dumps(dict(permissions=dict(deny=['write_file(/protected/)']))),
            '-p', 'go'], profile)
        self.assertEqual(1, argv.count('--settings'))
        self.assertEqual(dict(theme='dark', permissions=dict(
            allow=['command(echo)', 'read_file(/)', 'command(*)', 'write_file(/workspace/)'],
            deny=['write_file(/protected/)', 'write_file(**/.env)'])), json.loads(argv[4]))

    def test_agy_rejects_invalid_settings_without_dropping_them(self):
        profile = dict(agy_allow=[], agy_deny=[])
        for settings in ('not json', '[]', '{"permissions": []}', '{"permissions": {"allow": "command(*)"}}'):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                perms._apply_agy(['agy', '--settings', settings, '-p', 'go'], profile)

    def test_codex_loads_user_and_project_configs_without_profile_override(self):
        config = self.home / '.codex/config.toml'
        config.parent.mkdir()
        config.write_text('sandbox_mode="workspace-write"\nmodel_reasoning_effort="low"\n')
        project = self.work / '.codex/config.toml'
        project.parent.mkdir()
        project.write_text('sandbox_mode="read-only"\n')
        run = dict(cli='codex', cwd=str(self.work), base=str(self.base))
        for native in ('0', '1'):
            with self.subTest(native=native), mock.patch.dict(os.environ, GODFATHER_NATIVE=native):
                argv = perms.apply_native(['custom-codex', 'exec', '-'], run, perms.compile_profile(run))
                self.assertEqual('danger-full-access', argv[argv.index('-s') + 1])
                self.assertIn('approval_policy="never"', argv)
                self.assertNotIn('--ignore-user-config', argv)
                self.assertFalse(any(a.startswith(('permissions.', 'default_permissions=')) for a in argv))
        self.assertIn('sandbox_mode="workspace-write"', config.read_text())
        self.assertIn('sandbox_mode="read-only"', project.read_text())

    def test_native_roots_include_guard_state_and_default_registry(self):
        from godfather_store import db_path
        with mock.patch.dict(os.environ, {'AGENT_RUNS_DIR': str(self.home / '.agent-runs')}):
            run = dict(cli='codex', cwd=str(self.work), base=str(self.home / '.agent-runs/task'))
            profile = perms.compile_profile(run)
            self.assertIn(str(policy.state_dir().resolve()), profile['writable_roots'])
            self.assertIn(str(db_path().parent.resolve()), profile['writable_roots'])
            self.assertNotIn(str(policy.config_dir().resolve()), profile['writable_roots'])
            os.environ['GODFATHER_NATIVE'] = '1'
            profile['sandbox'] = 'workspace-write'
            argv = perms.apply_native(['codex', 'exec', '-'], run, profile)
            self.assertIn(str(policy.state_dir().resolve()), argv)

    def test_notes_grant_is_exact_for_native_profiles_and_segment_shims(self):
        os.environ['GODFATHER_NATIVE'] = '1'
        base = self.home / 'artifacts/task#2'
        base.parent.mkdir()
        notes = base.parent / 'task.notes.md'
        notes.touch()
        run = dict(cwd=str(self.work), base=str(base), router_name='task')
        for cli in ('agy', 'codex', 'grok'):
            with self.subTest(cli=cli):
                profile = perms.compile_profile(dict(run, cli=cli))
                self.assertEqual([str(notes)], profile['writable_files'])
                self.assertNotIn(str(notes), profile['writable_roots'])
                self.assertEqual(perms.writable_roots(run), profile['writable_roots'])
                self.assertIn('write_file(' + str(notes) + ')', profile['agy_allow'])
                self.assertNotIn('write_file(' + str(notes) + '/)', profile['agy_allow'])
                self.assertIn('Write(' + str(notes) + ')', profile['grok_allow'])
                self.assertIn('Edit(' + str(notes) + ')', profile['grok_allow'])
                self.assertNotIn('Write(' + str(notes) + '/**)', profile['grok_allow'])
                if cli == 'codex':
                    profile['sandbox'] = 'workspace-write'
                    for command in (['codex', 'exec', '-'], ['codex', 'exec', 'resume', 'session', '-']):
                        argv = perms.apply_native(command, dict(run, cli=cli), profile)
                        roots = json.loads(next(v.split('=', 1)[1] for v in argv if
                                                v.startswith('sandbox_workspace_write.writable_roots=')))
                        self.assertIn(str(notes), roots)
                        self.assertNotIn(str(notes) + '.other', roots)
                        self.assertNotIn(str(notes), [argv[i + 1] for i, value in enumerate(argv[:-1])
                                                     if value == '--add-dir'])
                elif cli == 'agy':
                    argv = perms.apply_native(['agy', '-p', 'go'], dict(run, cli=cli), profile)
                    settings = json.loads(argv[argv.index('--settings') + 1])
                    self.assertIn('write_file(' + str(notes) + ')', settings['permissions']['allow'])
                else:
                    argv = perms.apply_native(['grok'], dict(run, cli=cli), profile)
                    self.assertIn('Write(' + str(notes) + ')', argv)
        context = dict(self.context, base=str(base), notes_path=str(notes))
        self.assertEqual('allow', policy.evaluate(['tee', str(notes)], context)['verdict'])
        for sibling in ('task.notes.md.other', 'other.notes.md'):
            self.assertEqual('ask', policy.evaluate(['tee', str(base.parent / sibling)], context)['verdict'])

    def test_read_only_codex_keeps_its_explicit_no_write_exception(self):
        os.environ['CODEX_SANDBOX'] = 'read-only'
        run = dict(cli='codex', cwd=str(self.work), prompt_file=str(self.work / 'task.md'))
        Path(run['prompt_file']).write_text('Inspect only')
        argv, env, prompt = safety.prepare(self.base, run, ['codex', 'exec', '-'])
        profile = json.loads(Path(run['permission_profile']).read_text())
        self.assertEqual('read-only', profile['sandbox'])
        self.assertEqual([], profile['writable_files'])
        self.assertEqual('read-only', argv[argv.index('-s') + 1])
        self.assertFalse(any(a.startswith('sandbox_workspace_write.writable_roots=') for a in argv))
        self.assertIn('Codex read-only exception:', prompt.read_text())
        self.assertEqual(str(self.runs / 'task.notes.md'), env['GODFATHER_NOTES'])

    def test_container_mounts_only_repo_and_exact_shared_notes_file(self):
        notes = self.runs / 'task.notes.md'
        notes.touch()
        run = dict(cli='agy', cwd=str(self.work), base=str(self.runs / 'task#2'))
        with mock.patch.object(perms, 'container_runtime', return_value='/fake/docker'), \
                mock.patch.object(perms, 'container_image', return_value='cli:test'):
            argv = perms.wrap_container(['agy', '-p', 'go'], run)
        mounts = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == '-v']
        self.assertEqual([str(self.work) + ':' + str(self.work), str(notes) + ':' + str(notes)], mounts)
        self.assertIn('GODFATHER_NOTES=' + str(notes), argv)
        self.assertNotIn(str(self.runs) + ':' + str(self.runs), argv)
        self.assertEqual(['cli:test', 'agy', '-p', 'go'], argv[-4:])

    def test_native_roots_include_shared_and_private_git_dirs_from_worktree_subdir(self):
        repo = self.home / 'repo'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                                  text=True, env=self.env).stdout.strip()
        git('init', '-q')
        git('commit', '--allow-empty', '-qm', 'base')
        worktree = self.home / 'linked'
        git('worktree', 'add', '-b', 'linked', str(worktree))
        nested = worktree / 'nested'
        nested.mkdir()
        self.assertTrue((worktree / '.git').is_file())
        common = str((repo / '.git').resolve())
        private = git('-C', str(nested), 'rev-parse', '--absolute-git-dir')
        run = dict(cli='codex', cwd=str(nested), base=str(self.base))
        profile = perms.compile_profile(run)
        for root in (common, private):
            self.assertIn(root, profile['writable_roots'])
            self.assertEqual(1, profile['writable_roots'].count(root))
        os.environ['GODFATHER_NATIVE'] = '1'
        profile['sandbox'] = 'workspace-write'
        argv = perms.apply_native(['codex', 'exec', '-'], run, profile)
        roots = json.loads(next(v.split('=', 1)[1] for v in argv
                                if v.startswith('sandbox_workspace_write.writable_roots=')))
        for root in (common, private):
            self.assertIn(root, roots)
            self.assertIn(root, [argv[i + 1] for i, v in enumerate(argv[:-1]) if v == '--add-dir'])
        self.assertEqual([common], perms.git_writable_roots(repo))
        self.assertEqual([], perms.git_writable_roots(self.work))

    def test_git_roots_use_conductor_path_instead_of_worker_shims(self):
        with mock.patch.object(perms.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, '/outside/common\n/outside/common/worktrees/linked\n')) as query:
            with mock.patch.dict(os.environ, {'GODFATHER_ORIGINAL_PATH': '/conductor/bin'}):
                self.assertEqual(['/outside/common', '/outside/common/worktrees/linked'],
                                 perms.git_writable_roots(self.work))
        self.assertEqual('/conductor/bin', query.call_args.kwargs['env']['PATH'])

    def test_policy_tool_state_roots_reach_native_profile_without_approving_commands(self):
        config = policy.config_dir()
        config.mkdir(parents=True)
        declarations = ['~/.config/devbox', '~/Library/Application Support/acme-dev',
                        '~/Library/Caches/acme-dev', '~/.acme-dev']
        (config / 'policy.toml').write_text('writable_roots = ' + json.dumps(declarations) + '\n')
        roots = [str(Path(p).expanduser().resolve()) for p in declarations]
        self.assertTrue(all(not Path(p).exists() for p in roots))
        run = dict(cli='codex', cwd=str(self.work), base=str(self.base))
        profile = perms.write_profile(self.base, run)
        self.assertEqual(roots, profile['tool_state_roots'])
        os.environ['GODFATHER_NATIVE'] = '1'
        argv = perms.apply_native(['codex', 'exec', '-'], run, profile)
        self.assertEqual('danger-full-access', argv[argv.index('-s') + 1])
        self.assertNotIn('--add-dir', argv)
        with mock.patch.dict(os.environ, CODEX_SANDBOX='workspace-write'):
            profile = perms.compile_profile(run)
            argv = perms.apply_native(['codex', 'exec', '-'], run, profile)
        self.assertEqual('workspace-write', argv[argv.index('-s') + 1])
        for root in roots:
            self.assertIn(root, profile['writable_roots'])
            self.assertIn(root, [argv[i + 1] for i, v in enumerate(argv[:-1]) if v == '--add-dir'])
            self.assertTrue(Path(root).is_dir())
        helper = Path(roots[1]) / 'mutagen-ssh/bin/ssh'
        helper.parent.mkdir(parents=True)
        helper.write_text('helper')
        self.assertEqual('ask', self.evaluate('rm', '-rf', roots[1])['verdict'])
        self.assertEqual('ask', self.evaluate('git', 'push', 'origin', 'main')['verdict'])
        self.assertEqual('deny', self.evaluate('tee', str(Path(roots[0]) / '.env'))['verdict'])
        self.assertEqual('deny', self.evaluate('tee', str(self.home / '.ssh/id_ed25519'))['verdict'])
        listed = self.command('policy')
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertIn('native writable root: ' + roots[1], listed.stdout)

    def test_policy_tool_state_roots_validate_expand_and_deduplicate(self):
        config = policy.config_dir()
        config.mkdir(parents=True)
        path = config / 'policy.toml'
        for value in ('relative/path', '~/.config/*', '$UNSET_GF_ROOT', '', 1):
            path.write_text('writable_roots = ' + json.dumps([value]) + '\n')
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.load_policy()
        path.write_text('writable_roots = "~/state"\n')
        with self.assertRaises(ValueError):
            policy.load_policy()
        path.write_text('writable_roots = ["$GF_TEST_STATE", "~/tool-state"]\n')
        with mock.patch.dict(os.environ, {'GF_TEST_STATE': str(self.home / 'tool-state')}):
            self.assertEqual([str(self.home / 'tool-state')], policy.load_policy()['writable_roots'])

    def test_prepare_keeps_native_mode_for_a_later_nudge(self):
        os.environ['GODFATHER_NATIVE'] = '1'
        os.environ['GODFATHER_TASK_TYPE'] = 'research'
        prompt = self.root / 'brief.md'
        prompt.write_text('inspect\n')
        run = dict(cli='codex', cwd=str(self.work), prompt_file=str(prompt), started_at=time.time())
        command, env, _prompt = safety.prepare(self.base, run, ['codex', 'exec', '-s', 'danger-full-access', '-c', 'approval_policy="never"', '-'])
        self.assertEqual('1', run['launcher_env']['GODFATHER_NATIVE'])
        self.assertEqual('research', run['launcher_env']['GODFATHER_TASK_TYPE'])
        self.assertIn('danger-full-access', command)
        self.assertNotIn('sandbox-exec', env.get('PATH', ''))
        self.assertTrue(run['permission_profile'].endswith('.permissions.json'))
        saved = json.loads(Path(run['permission_profile']).read_text())
        self.assertTrue(saved['tradeoffs'])
        self.assertIn('audit', ' '.join(saved['tradeoffs']).lower())
