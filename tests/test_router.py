"""Offline router integration with fake binaries for every installed CLI."""
import json
import errno
import os
from pathlib import Path
import selectors
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
import godfather_router as r

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
cli = Path(sys.argv[0]).name
args = sys.argv[1:]
account_home = os.getenv(cli.upper() + '_HOME')
if args == ['login', 'status']:
    with (Path.home() / 'logins.jsonl').open('a') as f:
        f.write(json.dumps(dict(home=os.getenv('CODEX_HOME'))) + '\n')
    if os.getenv('LOGGED_OUT') == cli or (os.getenv('LOGGED_OUT_HOME') and os.getenv('LOGGED_OUT_HOME') == os.getenv('CODEX_HOME')):
        print('Not logged in', file=sys.stderr); sys.exit(1)
    print('Logged in'); sys.exit(0)
if args == ['-p', '/usage']:
    if os.getenv('LOGGED_OUT') == cli:
        print('Not logged into Antigravity', file=sys.stderr); sys.exit(1)
    print('Gemini Models\tWeekly Limit Remaining\t100%')
    print('Gemini Models\tFive Hour Limit Remaining\t100%')
    print('Claude and GPT models\tWeekly Limit Remaining\t100%')
    print('Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t' + os.getenv('ARGON_PCT', '100') + '%\t2099-01-01T00:00:00Z')
    sys.exit(0)
model = args[args.index('--model') + 1] if '--model' in args else args[args.index('-m') + 1]
if cli == 'codex':
    prompt = sys.stdin.read()
elif cli == 'grok':
    prompt = Path(args[args.index('--prompt-file') + 1]).read_text()
else:
    prompt = args[args.index('-p') + 1]
notes_path = os.environ['GODFATHER_NOTES']
notes_before = Path(notes_path).read_text()
if os.getenv('WRITE_NOTES'):
    with Path(notes_path).open('a') as f:
        f.write('Done: inspected with ' + model + '\n')
with (Path.home() / 'calls.jsonl').open('a') as f:
    f.write(json.dumps(dict(cli=cli, model=model, prompt=prompt, args=args,
                           binary=sys.argv[0], home=account_home, notes_path=notes_path, notes_before=notes_before)) + '\n')
if cli == 'codex' and os.getenv('CODEX_NATIVE'):
    print(json.dumps(dict(type='thread.started', thread_id='33333333-3333-3333-3333-333333333333')))
    print(json.dumps(dict(type='item.started', item=dict(type='command_execution', command='false'))))
    print(json.dumps(dict(type='item.completed', item=dict(type='command_execution', exit_code=1,
                                                         aggregated_output='Error: fake shell command failed'))))
quota = model in os.getenv('QUOTA_MODELS', '').split(',') or (os.getenv('QUOTA_HOME') and os.getenv('QUOTA_HOME') == account_home)
auth = os.getenv('TASK_AUTH_FAILURE') == cli or (os.getenv('AUTH_FAILURE_HOME') and os.getenv('AUTH_FAILURE_HOME') == account_home)
if cli == 'agy':
    log = Path(args[args.index('--log-file') + 1])
    log.write_text('Print mode: conversation=44444444-4444-4444-4444-444444444444\n')
if model in os.getenv('EMPTY_MODELS', '').split(','):
    if cli == 'grok':
        print(json.dumps(dict(type='end')))
    sys.exit(0)
if model in os.getenv('DENIED_MODELS', '').split(',') and not (
        os.getenv('DENIED_ONCE') == '1' and ('--conversation' in args or '--resume' in args or 'resume' in args)):
    if os.getenv('DENIAL_AUTH_WARNING'):
        print('You are not logged into Antigravity', file=sys.stderr)
    tool = os.getenv('DENIED_TOOL', 'command')
    print('jetski: no output produced — a tool required the "' + tool + '" permission that headless mode cannot prompt for, so it was auto-denied.',
          file=sys.stdout if os.getenv('DENIED_STDOUT') else sys.stderr)
    sys.exit(0)
if quota:
    if os.getenv('CHANGE_FILE'):
        with Path(os.environ['CHANGE_FILE']).open('a') as f:
            f.write('work already completed\n')
    error = 'Error: usage limit reached (5-hour limit); retry after 120 seconds'
    if cli == 'grok':
        print(json.dumps(dict(type='text', data='Already inspected the parser; verify the changed files.')))
        print(json.dumps(dict(type='error', message=error)))
        print(json.dumps(dict(type='end')))
    elif cli == 'codex' and os.getenv('CODEX_NATIVE'):
        print(json.dumps(dict(type='error', message=error)))
        print(json.dumps(dict(type='turn.failed', error=dict(message=error))))
    else:
        print(error, file=sys.stderr)
    if cli == 'codex':
        Path(args[args.index('-o') + 1]).write_text('Changed the tracked file. Continue with verification.\n')
    sys.exit(1)
if auth:
    print('Error: authentication failed: not logged in', file=sys.stderr); sys.exit(1)
if cli == 'codex':
    if not os.getenv('CODEX_NATIVE'):
        print('session id: 33333333-3333-3333-3333-333333333333')
    Path(args[args.index('-o') + 1]).write_text('OK\n')
elif cli == 'grok':
    print(json.dumps(dict(type='text', data='OK')))
    print(json.dumps(dict(type='end')))
else:
    print('OK')
'''


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(lambda: RouterTests.cleanup_fixture(self))
        self.root = Path(self.tmp.name).resolve()
        fake = self.root / 'fake'
        fake.mkdir()
        # Restricted PATH: never accidentally invoke the user's real CLIs.
        for utility in ('python3', 'dirname', 'mkdir', 'awk', 'ps', 'cat', 'mktemp', 'date', 'rm', 'tee', 'git', 'bash'):
            (fake / utility).symlink_to(shutil.which(utility))
        for cli in ('codex', 'agy', 'grok'):
            path = fake / cli
            path.write_text(FAKE)
            path.chmod(0o755)
        saved = self.root / '.grok/auth.json'
        saved.parent.mkdir()
        saved.write_text('{"fake":true}')
        self.env = dict(HOME=str(self.root), PATH=str(fake), CWD=str(self.root),
                        CODEX_BIN=str(fake / 'codex'), AGENT_RUNS_DIR=str(self.root / 'runs'),
                        XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                        PYTHONDONTWRITEBYTECODE='1')
        self.config = self.root / 'config/godfather/models.toml'
        self.config.parent.mkdir(parents=True)
        (self.config.parent / 'godfather.toml').write_text('[healing]\nauth_retry_secs=0.01\n')
        self.prompt = self.root / 'task.md'
        self.prompt.write_text('Original task: finish the parser and verify existing work.')

    def cleanup_fixture(self):
        """Terminal JSON can precede detached owners' last registry writes."""
        if not hasattr(self, 'env') or not self.root.exists():
            self.tmp.cleanup()
            return
        import godfather_lifecycle as lifecycle
        with mock.patch.dict(os.environ, self.env, clear=True):
            snapshot = a.processes()
            ancestors, pid = set(), os.getpid()
            while pid in snapshot and pid not in ancestors:
                ancestors.add(pid)
                pid = snapshot[pid]['ppid']
            queue = self.root / 'state/godfather/queue'
            for path in queue.glob('*.json'):
                job = a.read(path)
                if job.get('status') in lifecycle.ACTIVE and job.get('pid') not in ancestors:
                    lifecycle.stop_run(job['name'])
            names = set()
            owned = set()
            for path in (self.root / 'runs').glob('*.json'):
                record = a.read(path)
                owners = [(record.get(key), record.get(identity)) for key, identity in (
                    ('pid', 'pid_identity'), ('pid', 'identity'), ('launcher_pid', 'launcher_identity'),
                    ('supervisor_pid', 'supervisor_identity'), ('router_pid', 'router_identity'))]
                live = [pid for pid, identity in owners if identity and a.alive(pid, identity, snapshot)]
                # In-process routing tests may legitimately record the test
                # runner or its parent. They are never fixture-owned workers.
                if live and not (set(live) & ancestors) and all(
                        snapshot[pid].get('pgid') != os.getpgrp() for pid in live):
                    owned.update((pid, identity) for pid, identity in owners if pid in live and identity)
                    name = record.get('router_name') or record.get('name')
                    if name:
                        names.add(name)
            for name in names:
                lifecycle.stop_run(name)
            deadline = time.monotonic() + 5
            while True:
                snapshot = a.processes()
                attached = list((self.root / 'runs').glob('.*.attach-*.owner'))
                if not attached and not any(a.alive(pid, identity, snapshot) for pid, identity in owned):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('fixture owners did not finish before temporary-directory cleanup')
                time.sleep(.03)
        # A cancelled queue daemon may be in its last poll/write. Never ignore
        # a persistent cleanup error, but allow that bounded shutdown to drain.
        deadline = time.monotonic() + 5
        while True:
            try:
                self.tmp.cleanup()
                return
            except OSError as error:
                if error.errno != errno.ENOTEMPTY or time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    def config_models(self, *models):
        self.config.write_text('\n'.join('[[models]]\nbackend = "{}"\nmodel = "{}"\neffort = "high"\n'.format(*m) for m in models))

    def launch(self, name='task', **env):
        return subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', str(self.prompt), name],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=30)

    def calls(self):
        return [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]

    def state(self):
        return json.loads((self.root / 'state/godfather/quota.json').read_text())

    def test_default_ranking_inline_prompt_and_effort(self):
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '-p', 'reply OK', 'inline'],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(r.DEFAULT_TOML, self.config.read_text())
        call = self.calls()[0]
        safety = __import__('godfather_safety')
        self.assertEqual(('agy', 'gemini-4-argon-high', safety.PREAMBLE +
                          safety.notes_preamble(Path(call['notes_path'])) + 'reply OK'),
                         (call['cli'], call['model'], call['prompt']))
        self.assertIn('--effort', call['args'])
        self.assertEqual('high', call['args'][call['args'].index('--effort') + 1])

    def test_task_type_effort_prefers_the_policy_row(self):
        self.config.write_text(
            '[[models]]\nbackend="grok"\nmodel="grok-4.7-build-fast"\neffort="high"\n'
            '[[effort]]\nmodel="grok-4.7-build-fast"\ntask_type="research"\neffort="medium"\n')
        (self.config.parent / 'godfather.toml').write_text(
            '[healing]\nauth_retry_secs=0.01\n'
            '[[effort]]\nmodel="grok-4.7-build-fast"\ntask_type="research"\neffort="low"\n')
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', '--task-type', 'research', '-p', 'look it up', 'typed'],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        args = self.calls()[0]['args']
        self.assertEqual('low', args[args.index('--reasoning-effort') + 1])
        self.assertEqual('research', a.read(self.root / 'runs/typed.run.json')['task_type'])
        rejected = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '--task-type', '*', '-p', 'nope', 'star'],
                                  env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn('task-type', rejected.stderr)

    def test_empty_zero_exit_falls_back_for_every_backend(self):
        for cli, model, target, target_model in (
                ('codex', 'gpt-6.1-sol', 'grok', 'grok-4.7-build-fast'),
                ('grok', 'grok-4.7-build-fast', 'codex', 'gpt-6.1-sol'),
                ('agy', 'gemini-3.8-flash-high', 'agy', 'claude-sonnet-5-5-high')):
            with self.subTest(cli=cli):
                self.config_models((cli, model), (target, target_model))
                name = 'empty-' + cli
                # Grok still emits its terminal frame, but no assistant text.
                result = self.launch(name, EMPTY_MODELS=model)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                first = a.read(self.root / ('runs/' + name + '.run.json'))
                self.assertEqual(('failed', 'no_output', 1),
                                 (first['status'], first['failure_class'], first['exit_code']))
                final = a.read(self.root / ('runs/' + name + '#2.run.json'))
                self.assertEqual('done', final['status'])
                self.assertEqual('OK', (self.root / ('runs/' + name + '#2.last.md')).read_text().strip())
                self.assertNotIn('--conversation', self.calls()[-1]['args'])

    def test_headless_denial_fails_and_falls_back_without_cooldown(self):
        self.config_models(('agy', 'gemini-3.8-flash-high'), ('agy', 'claude-sonnet-5-5-high'),
                           ('codex', 'gpt-6.1-sol'))
        for warning in ('', '1'):
            for stdout, tool in (('', 'command'), ('1', 'read_file')):
                name = 'denied-' + (stdout or 'stderr') + (warning or 'no-warning')
                with self.subTest(warning=warning, stdout=stdout, tool=tool):
                    result = self.launch(name, DENIED_MODELS='gemini-3.8-flash-high',
                                         DENIED_STDOUT=stdout, DENIED_TOOL=tool, DENIAL_AUTH_WARNING=warning)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    first = a.read(self.root / ('runs/' + name + '.run.json'))
                    self.assertEqual(('failed', 'tool_denied', 1),
                                     (first['status'], first['failure_class'], first['exit_code']))
                    self.assertIn('headless', first['failure_reason'])
                    final = a.read(self.root / ('runs/' + name + '#2.run.json'))
                    self.assertEqual(('done', 'agy', 'claude-sonnet-5-5-high'),
                                     (final['status'], final['cli'], final['model']))
                    log = (self.root / ('runs/' + name + '.models.log')).read_text()
                    self.assertIn('FALLBACK tool_denied', log)
                    self.assertNotIn('authentication failed', log)
                    self.assertNotIn('AUTH_RETRY', (self.root / ('runs/' + name + '.events.jsonl')).read_text())
                    self.assertEqual({}, self.state())

    def test_write_file_headless_denial_classified_and_retried_once_with_worktree_note(self):
        self.config_models(('agy', 'gemini-4-argon-high'), ('agy', 'claude-sonnet-5-5-high'))
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '--allow-empty', '-qm', 'base'], check=True, capture_output=True)
        brief = 'Work only in ' + str(repo)
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '-p', brief, 'write-retry'],
                                env=dict(self.env, CWD=str(repo), DENIED_MODELS='gemini-4-argon-high',
                                         DENIED_TOOL='write_file', DENIED_ONCE='1'),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        isolated = (self.root / 'runs/worktrees/write-retry').resolve()
        note = 'Your working tree is {}; paths to {} in the brief refer to it; do not write elsewhere.'.format(
            isolated, repo.resolve())
        calls = self.calls()
        self.assertEqual(2, len(calls))
        self.assertEqual([('agy', 'gemini-4-argon-high'), ('agy', 'gemini-4-argon-high')],
                         [(c['cli'], c['model']) for c in calls])
        self.assertIn(note + '\n\n' + brief, calls[1]['prompt'])
        log = (self.root / 'runs/write-retry.models.log').read_text()
        self.assertIn('write_file auto-denied in headless mode; no final report', log)
        self.assertIn('RETRY write_file_denied agy:gemini-4-argon-high attempt=1/1 '
                      '(write_file auto-denied in headless mode; no final report)', log)
        self.assertNotIn('FALLBACK', log)
        events = (self.root / 'runs/write-retry.events.jsonl').read_text()
        self.assertIn('WRITE_RETRY', events)
        self.assertIn('HEALED', events)
        # If the same-model retry also fails with write_file auto-denied, fall back to the next model
        # and surface the distinct failure reason in run.json / status / models.log.
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'run', '-p', brief, 'write-fallback'],
                                env=dict(self.env, CWD=str(repo), DENIED_MODELS='gemini-4-argon-high',
                                         DENIED_TOOL='write_file'),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        first = a.read(self.root / 'runs/write-fallback.run.json')
        self.assertEqual(('failed', 'tool_denied', 'write_file auto-denied in headless mode; no final report'),
                         (first['status'], first['failure_class'], first['failure_reason']))
        self.assertEqual('write_file auto-denied in headless mode; no final report', first['errors'][0])
        final = a.read(self.root / 'runs/write-fallback#2.run.json')
        self.assertEqual(('done', 'agy', 'claude-sonnet-5-5-high'),
                         (final['status'], final['cli'], final['model']))
        fallback_log = (self.root / 'runs/write-fallback.models.log').read_text()
        self.assertIn('RETRY write_file_denied agy:gemini-4-argon-high attempt=1/1', fallback_log)
        self.assertIn('FALLBACK tool_denied (write_file auto-denied in headless mode; no final report)', fallback_log)
        status_out = subprocess.run([str(ROOT / 'bin/godfather'), 'status'],
                                    env=self.env, capture_output=True, text=True, timeout=15)
        self.assertIn('write_file auto-denied in headless mode; no final report', status_out.stdout)

    def test_empty_output_exhaustion_ends_failed_and_policy_can_stop_fallback(self):
        self.config_models(('codex', 'gpt-6.1-sol'), ('grok', 'grok-4.7-build-fast'))
        result = self.launch('all-empty', EMPTY_MODELS='gpt-6.1-sol,grok-4.7-build-fast')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('failed', a.read(self.root / 'runs/all-empty#2.run.json')['status'])
        (self.config.parent / 'godfather.toml').write_text(
            '[[retry]]\nclass="no_output"\nattempts=0\nbackoff_secs=0\naction="escalate"\n'
            '[fallback]\non=[]\n')
        result = self.launch('stop-empty', EMPTY_MODELS='gpt-6.1-sol')
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.root / 'runs/stop-empty#2.run.json').exists())

    def test_quota_probe_cooldown_skips_the_cli(self):
        self.config_models(('grok', 'grok-4.7-build-fast'), ('codex', 'gpt-6.1-sol'))
        args = type('Args', (), {})()
        args.self = False
        args.max_fix_rounds = 2
        args.name = 'probe'
        args.isolate = False
        args.review_fix = False
        args.review_by = None
        args.wait_lock = False
        args.task_type = 'edit'
        args.prompt = 'inspect the parser'
        args.prompt_file = None
        args.check = None

        def probe(cli, binary, minimum=0, key=None, now=None, capture=None, home=None):
            if cli == 'grok':
                return dict(status='cooldown', detail='weekly limit exhausted', remaining=0)
            return dict(status='unknown', detail='cooldown clear; headless remaining-quota query unavailable')

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch('godfather_supervisor.quota_preflight', side_effect=probe):
                rc = r.route(args)
        log = (self.root / 'runs/probe.models.log').read_text()
        self.assertEqual(0, rc, log)
        self.assertEqual(['codex'], [call['cli'] for call in self.calls()])
        self.assertIn('grok:grok-4.7-build-fast', self.state())
        self.assertIn('COOLDOWN grok:grok-4.7-build-fast', log)

    def test_default_uses_best_model_even_when_only_one_percent_remains(self):
        result = self.launch(ARGON_PCT='1')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('gemini-4-argon-high', self.calls()[0]['model'])

    def test_preflight_skips_exhausted_agy_and_honors_reset_column(self):
        result = self.launch(ARGON_PCT='0')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('claude-opus-5-5-high', self.calls()[0]['model'])
        entry = self.state()['agy:gemini-4-argon-high']
        self.assertEqual('2099-01-01T00:00:00+00:00', entry['until'])
        self.assertIn('SKIP agy:gemini-4-argon-high /usage', (self.root / 'runs/task.models.log').read_text())

    def test_codex_quota_cooldown_handoff_and_watch_switch(self):
        self.config_models(('codex', 'gpt-6.1-sol'), ('grok', 'grok-4.7'))
        workspace = self.root / 'workspace'
        workspace.mkdir()
        tracked = workspace / 'parser.txt'
        tracked.write_text('original\n')
        def git(*args):
            subprocess.run(['git', '-C', str(workspace)] + list(args), check=True, capture_output=True)
        git('init', '-q')
        git('add', 'parser.txt')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        result = self.launch(CWD=str(workspace), QUOTA_MODELS='gpt-6.1-sol', CHANGE_FILE=str(tracked))
        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls()
        self.assertEqual(['codex', 'grok'], [c['cli'] for c in calls])
        prompt = calls[1]['prompt']
        for value in (self.prompt.read_text(), 'git diff --stat', 'parser.txt |', '1 insertion(+)',
                      'Continue the task rather than restart', 'Verify the existing work first', 'Changed the tracked file'):
            self.assertIn(value, prompt)
        self.assertGreater(a.epoch(self.state()['codex:gpt-6.1-sol']['until']), time.time())
        self.assertLess(a.epoch(self.state()['codex:gpt-6.1-sol']['until']), time.time() + 125)
        first = a.read(self.root / 'runs/task.run.json')
        second = a.read(self.root / 'runs/task#2.run.json')
        self.assertEqual(('quota', 'done'), (first['status'], second['status']))
        self.assertEqual(('grok', 'grok-4.7', 'task'), (second['cli'], second['model'], second['router_name']))
        watch = subprocess.Popen([str(ROOT / 'bin/godfather'), 'watch', '--replay', '--no-heal', '--poll-secs', '.05'], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            selector = selectors.DefaultSelector()
            selector.register(watch.stdout, selectors.EVENT_READ)
            self.addCleanup(selector.close)
            output = b''
            deadline = time.monotonic() + 5
            while b'SWITCH task codex:gpt-6.1-sol -> grok:grok-4.7 (' not in output and time.monotonic() < deadline:
                if selector.select(.1):
                    output += os.read(watch.stdout.fileno(), 65536)
            self.assertIn(b'SWITCH task codex:gpt-6.1-sol -> grok:grok-4.7 (', output)
        finally:
            watch.terminate()
            watch.communicate(timeout=5)
        # A later invocation does not retry exhausted Codex.
        result = self.launch('again')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(['codex', 'grok', 'grok'], [c['cli'] for c in self.calls()])
        # Nudge by the logical name resolves the active segment, retaining its model.
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'nudge', '--wait', 'task', 'Finish verifying'],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('--resume', self.calls()[-1]['args'])
        self.assertEqual('task', a.read(self.root / 'runs/task#2.run.json')['router_name'])
        self.assertEqual('grok', self.calls()[-1]['cli'])

    def test_codex_native_stream_shell_failures_are_routine_and_quota_routes(self):
        self.config_models(('codex', 'gpt-6.1-sol'), ('grok', 'grok-4.7'))
        result = self.launch(CODEX_NATIVE='1')
        self.assertEqual(0, result.returncode, result.stderr)
        run = a.read(self.root / 'runs/task.run.json')
        self.assertTrue(run['codex_stream'])
        self.assertEqual([], run['errors'])
        self.assertEqual('33333333-3333-3333-3333-333333333333', run['session_id'])
        # Native parsing must also work when a nudge supplies an existing session.
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'nudge', '--wait', 'task', 'Verify again'],
                                env=dict(self.env, CODEX_NATIVE='1'), capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(a.read(self.root / 'runs/task.run.json')['codex_stream'])
        self.assertIn('resume', self.calls()[-1]['args'])
        result = self.launch('native-quota', CODEX_NATIVE='1', QUOTA_MODELS='gpt-6.1-sol')
        self.assertEqual(0, result.returncode, result.stderr)
        run = a.read(self.root / 'runs/native-quota.run.json')
        self.assertEqual('quota', run['status'])
        self.assertEqual(['codex', 'codex', 'codex', 'grok'], [call['cli'] for call in self.calls()])
        self.assertIsNotNone(self.state().get('codex:gpt-6.1-sol'))

    def test_same_cli_resumes_and_global_order_crosses_before_flash(self):
        result = self.launch(QUOTA_MODELS='gemini-4-argon-high,claude-opus-5-5-high')
        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls()
        self.assertEqual(['gemini-4-argon-high', 'claude-opus-5-5-high', 'gpt-6.1-sol'], [c['model'] for c in calls])
        self.assertIn('--conversation', calls[1]['args'])
        self.assertIn('44444444-4444-4444-4444-444444444444', calls[1]['args'])
        self.assertIn('Continue the task', calls[2]['prompt'])
        self.assertEqual('quota', a.read(self.root / 'runs/task#2.run.json')['status'])

    def test_cooldown_expires_and_reset_command(self):
        self.config_models(('codex', 'gpt-6.1-sol'), ('grok', 'grok-4.7'))
        with mock.patch.dict(os.environ, self.env, clear=True):
            entry = r.mark_exhausted('codex:gpt-6.1-sol', 'quota exhausted', clock=100)
            self.assertEqual(3700, a.epoch(entry['until']))
            self.assertIsNotNone(r.cooldown('codex:gpt-6.1-sol', clock=3699))
            self.assertIsNone(r.cooldown('codex:gpt-6.1-sol', clock=3700))
        result = self.launch()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('codex', self.calls()[0]['cli'])
        probe = self.root / 'state/godfather/quota-probe.json'
        probe.write_text(json.dumps({'codex': {'status': 'cooldown'}, 'grok': {'status': 'ok'}}))
        result = subprocess.run([str(ROOT / 'bin/godfather'), 'quota', '--reset', 'gpt-6.1-sol'],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({}, self.state())
        self.assertEqual({'grok': {'status': 'ok'}}, json.loads(probe.read_text()))

    def test_quota_default_view_orders_by_rank_hides_expired_and_all_shows_full_dump(self):
        self.config_models(
            ('agy', 'gemini-4-argon-high'),
            ('codex', 'gpt-6.1-sol'),
            ('grok', 'grok-4.7-build-fast'),
            ('agy', 'claude-opus-5-5-high'),
        )
        long_active = 'rate limit hit\n' + ('detail ' * 40)
        long_expired = 'expired cooldown ' + ('old ' * 40)
        with mock.patch.dict(os.environ, self.env, clear=True):
            r.mark_exhausted('grok:grok-4.7-build-fast', long_expired, clock=100)
            r.mark_exhausted('codex:gpt-6.1-sol', long_active + ' resets_at=2099-01-01T00:00:00Z')
            # quota now reads the saved snapshot unless --live is explicit.
            r.agy_usage(str(self.root / 'fake/agy'))
            snapshot_path = self.root / 'state/godfather/agy-usage.json'
            snapshot = a.read(snapshot_path)
            snapshot['text'] = snapshot['text'].replace('Gemini 4 Argon Weekly Limit\t100%',
                                                       'Gemini 4 Argon Weekly Limit\t65%')
            a.write(snapshot_path, snapshot)
        default = subprocess.run([str(ROOT / 'bin/godfather'), 'quota'],
                                 env=dict(self.env, ARGON_PCT='65'), capture_output=True, text=True, timeout=20)
        self.assertEqual(0, default.returncode, default.stderr)
        lines = default.stdout.strip().splitlines()
        self.assertEqual(4, len(lines), default.stdout)
        self.assertEqual(
            [('agy', 'gemini-4-argon-high'), ('codex', 'gpt-6.1-sol'),
             ('grok', 'grok-4.7-build-fast'), ('agy', 'claude-opus-5-5-high')],
            [tuple(line.split()[:2]) for line in lines])
        self.assertIn('eligible=yes', lines[0])
        self.assertIn('Weekly Limit Remaining=100%', lines[0])
        self.assertIn('Five Hour Limit Remaining=100%', lines[0])
        self.assertIn('Gemini 4 Argon Weekly Limit=65%', lines[0])
        self.assertIn('eligible=no', lines[1])
        self.assertIn('cooldown until=2099-01-01T00:00:00+00:00', lines[1])
        reason_part = lines[1].split('cooldown until=2099-01-01T00:00:00+00:00 (', 1)[1].rstrip(')')
        self.assertLessEqual(len(reason_part), 120)
        self.assertNotIn('\n', reason_part)
        self.assertIn('eligible=yes', lines[2])
        self.assertIn('allowance unknown', lines[2])
        self.assertNotIn('expired cooldown', default.stdout)
        self.assertIn('eligible=yes', lines[3])
        self.assertIn('Weekly Limit Remaining=100%', lines[3])
        self.assertNotIn('Gemini 4 Argon Weekly Limit', lines[3])
        exhausted_argon = subprocess.run([str(ROOT / 'bin/godfather'), 'quota', '--live'],
                                         env=dict(self.env, ARGON_PCT='0'), capture_output=True, text=True, timeout=20)
        self.assertIn('agy gemini-4-argon-high eligible=no', exhausted_argon.stdout.splitlines()[0])
        full = subprocess.run([str(ROOT / 'bin/godfather'), 'quota', '--all'],
                              env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, full.returncode, full.stderr)
        dumped = json.loads(full.stdout)
        self.assertFalse(dumped['grok:grok-4.7-build-fast']['active'])
        self.assertIn('expired cooldown', dumped['grok:grok-4.7-build-fast']['reason'])
        self.assertTrue(dumped['codex:gpt-6.1-sol']['active'])
        self.assertGreater(len(dumped['codex:gpt-6.1-sol']['reason']), 120)

    def test_missing_cli_and_logged_out_backends_are_skipped(self):
        (self.root / 'fake/agy').unlink()
        self.config_models(('agy', 'gemini-4-argon-high'), ('codex', 'gpt-6.1-sol'), ('grok', 'grok-4.7'))
        result = self.launch(LOGGED_OUT='codex')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(['grok'], [c['cli'] for c in self.calls()])
        log = (self.root / 'runs/task.models.log').read_text()
        self.assertIn('CLI missing', log)
        self.assertIn('not logged in', log)

    def test_grok_without_credentials_is_skipped(self):
        self.config_models(('grok', 'grok-4.7'), ('codex', 'gpt-6.1-sol'))
        (self.root / '.grok/auth.json').unlink()
        result = self.launch()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('codex', self.calls()[0]['cli'])

    def test_grok_quota_handoff_preserves_partial_reply(self):
        self.config_models(('grok', 'grok-4.7'), ('codex', 'gpt-6.1-sol'))
        result = self.launch(QUOTA_MODELS='grok-4.7')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(['grok', 'codex'], [c['cli'] for c in self.calls()])
        self.assertIn('Already inspected the parser; verify the changed files.', self.calls()[1]['prompt'])
        self.assertIn(self.prompt.read_text(), self.calls()[1]['prompt'])
        self.assertIn('grok:grok-4.7', self.state())

    def test_non_git_handoff_lists_modified_files(self):
        self.config_models(('codex', 'gpt-6.1-sol'), ('grok', 'grok-4.7'))
        changed = self.root / 'edited.txt'
        result = self.launch(QUOTA_MODELS='gpt-6.1-sol', CHANGE_FILE=str(changed))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('Recently modified files', self.calls()[1]['prompt'])
        self.assertIn('edited.txt', self.calls()[1]['prompt'])

    def test_minimum_percentage_reorders_eligibility(self):
        self.config.write_text('[[models]]\nbackend="agy"\nmodel="gemini-4-argon-high"\neffort="high"\nmin_pct=75\n'
                               '[[models]]\nbackend="codex"\nmodel="gpt-6.1-sol"\neffort="medium"\n')
        result = self.launch(ARGON_PCT='50')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('codex', self.calls()[0]['cli'])
        self.assertIn('model_reasoning_effort=medium', self.calls()[0]['args'])
        self.assertNotIn('agy:gemini-4-argon-high', self.state())


class QuotaParsingTests(unittest.TestCase):
    def test_reset_times_and_defaults(self):
        cases = [('quota exhausted', 3600), ('5-hour usage limit reached', 18000),
                 ('Five Hour Limit Remaining\t0%', 18000), ('retry after 120 seconds', 120),
                 ('Retry-After: 60', 60), ('resets in 2 hours', 7200),
                 ('reset time: 2099-01-01T00:00:00Z', 4070908800 - 100),
                 ('Retry-After: Thu, 01 Jan 2099 00:00:00 GMT', 4070908800 - 100), ('try again in 2h 30m', 9000),
                 ('resets_at=2099-01-01T00:00:00Z', 4070908800 - 100),
                 ('{"reset_at": 4070908800}', 4070908800 - 100)]
        for text, seconds in cases:
            with self.subTest(text=text):
                self.assertEqual(100 + seconds, r.reset_time(text, clock=100))

    def test_config_validation(self):
        for text in ('', '[[models]]\nbackend="bogus"', '[[models]]\nbackend="agy"\nmodel="x"',
                     '[[models]]\nbackend="agy"\nmodel="x"\neffort="high"\nmin_pct=101'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                r.parse_models(text)

    def test_routine_agy_startup_and_codex_shell_output_do_not_emit_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / 'cli.log'
            run = dict(cli='agy', started_at=a.now(), progress_paths=[], diagnostic_paths=[str(log)])
            log.write_text('\n'.join([
                'You are not logged into Antigravity (silent authentication may recover)',
                'failed to get cs path', 'Failed to resolve GeminiDir', 'failed to get git info for workspace',
                'Failed to refresh cache in background']) + '\n')
            progress = a.Progress(run)
            self.assertEqual([], progress.update(run).events)
            run['status'] = 'done'
            self.assertEqual([], progress.update(run).events)
            run.update(cli='codex', status='running')
            log.write_text('exec\nfalse exited 1 in 1ms:\nError: test fixture command failed\n')
            progress = a.Progress(run)
            self.assertEqual([], progress.update(run).events)
            with log.open('a') as f:
                f.write('thinking\n2026-10-19T01:00:00Z ERROR codex_core: stream was interrupted\n')
            self.assertEqual('ERROR', progress.update(run).events[0][0])
