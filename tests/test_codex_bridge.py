"""Hermetic app-server regressions: only a fake stdio JSON-RPC executable."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import godfather_codex_bridge as bridge
import godfather_router as router
import godfather_stream as stream
import godfather_errors as errors
from codex_schema_fixture import validate

FAKE = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
mode = os.environ.get('FAKE_MODE', '')
def send(x):
 print(json.dumps(x), flush=True)
if sys.argv[1] != 'app-server':
 Path(os.environ['FALLBACK']).write_text(sys.stdin.read())
 sys.exit(0)
for line in sys.stdin:
 p=json.loads(line)
 with open(os.environ['RPC_LOG'], 'a') as f: f.write(json.dumps(p)+'\\n')
 m=p.get('method'); i=p.get('id')
 if m=='initialize':
  if mode=='bad': send({'id':i,'error':{'code':-1,'message':'no protocol'}})
  elif mode=='hang': continue
  else: send({'id':i,'result':{}})
 elif m in ('thread/start','thread/resume'):
  send({'id':i,'result':{'thread':{'id':'00000000-0000-4000-8000-000000000006'}}})
 elif m=='turn/start':
  send({'id':i,'result':{'turn':{'id':'turn-test'}}})
  send({'method':'turn/started','params':{'turn':{'id':'turn-test'}}})
  send({'method':'account/rateLimits/updated','params':{'rateLimits':{'primary':{'usedPercent':100,'resetsAt':4102444800}}}})
  send({'method':'thread/tokenUsage/updated','params':{'tokenUsage':{'total':{'inputTokens':int(os.environ.get('INPUT_TOKENS',100)),'cachedInputTokens':20,'outputTokens':8}}}})
  if mode=='sparse':
   send({'method':'account/rateLimits/updated','params':{'rateLimits':{'primary':{'usedPercent':100,'resetsAt':None},'secondary':{'usedPercent':100,'resetsAt':4102444900}}}})
   send({'method':'account/rateLimits/updated','params':{'rateLimits':{'primary':None,'secondary':{'resetsAt':None},'credits':None}}})
   send({'method':'account/rateLimits/updated','params':{'rateLimits':{}}})
  if mode in ('retry','terminal-error'):
   send({'method':'error','params':{'willRetry':mode=='retry','error':{'codexErrorInfo':'responseStreamDisconnected','message':'network error'}}})
  if mode=='crash': sys.exit(1)
  if mode=='quota':
   send({'method':'turn/completed','params':{'turn':{'id':'turn-test','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded','message':'failed'}}}})
   continue
  if mode=='approval':
   send({'id':'approve','method':os.environ.get('APPROVAL_METHOD','item/commandExecution/requestApproval'),'params':json.loads(os.environ.get('APPROVAL_PARAMS', json.dumps({'command':'touch .env','cwd':os.getcwd()})))})
   continue
  if mode=='live': continue
  send({'method':'item/started','params':{'item':{'id':'cmd','type':'commandExecution','command':'pwd'}}})
  send({'method':'item/completed','params':{'item':{'id':'cmd','type':'commandExecution','command':'pwd'}}})
  send({'method':'item/completed','params':{'item':{'id':'msg','type':'agentMessage','text':'Finished task'}}})
  send({'method':'turn/completed','params':{'turn':{'id':'turn-test','status':'completed'}}})
 elif m in ('turn/steer','turn/interrupt') or i=='approve':
  if m: send({'id':i,'result':{}})
  if m=='turn/interrupt' or i=='approve':
   send({'method':'turn/completed','params':{'turn':{'id':'turn-test','status':'completed'}}})
'''


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / 'run'
        self.fake = self.root / 'fake-codex'
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        Path(str(self.base) + '.permissions.json').write_text(json.dumps(dict(sandbox='workspace-write')))
        Path(str(self.base) + '.safety.json').write_text(json.dumps(dict(cwd=str(self.root))))
        self.env = dict(os.environ, RPC_LOG=str(self.root / 'rpc'), FALLBACK=str(self.root / 'fallback'),
                        XDG_STATE_HOME=str(self.root / 'state'), XDG_CONFIG_HOME=str(self.root / 'config'),
                        AGENT_RUNS_DIR=str(self.root), GODFATHER_CODEX_HANDSHAKE_TIMEOUT='3')
        self.command = [sys.executable, bridge.__file__, str(self.base), str(self.fake), 'exec',
                        '-m', 'test-model', '-c', 'model_reasoning_effort="high"', '-c',
                        'sandbox_workspace_write.writable_roots=["/tmp"]', '-']

    def run_bridge(self, mode='', command=None):
        offset = len(self.rpc()) if (self.root / 'rpc').exists() else 0
        result = subprocess.run(command or self.command, input='task prompt', text=True,
                                capture_output=True, cwd=self.root, env=dict(self.env, FAKE_MODE=mode), timeout=10)
        if (self.root / 'rpc').exists():
            for frame in self.rpc()[offset:]:
                if frame.get('id') == 'approve':
                    validate(self.env.get('APPROVAL_METHOD', 'item/commandExecution/requestApproval'), frame['result'])
        return result

    def rpc(self):
        return [json.loads(line) for line in (self.root / 'rpc').read_text().splitlines()]

    def test_translate_and_settings(self):
        result = self.run_bridge()
        self.assertEqual(result.returncode, 0, result.stderr)
        frames = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(Path(str(self.base) + '.last.md').read_text(), 'Finished task')
        self.assertEqual(json.loads(Path(str(self.base) + '.app-server.json').read_text())['thread_id'], '00000000-0000-4000-8000-000000000006')
        start = next(p['params'] for p in self.rpc() if p.get('method') == 'thread/start')
        self.assertEqual(start['sandbox'], 'workspace-write')
        self.assertEqual(start['approvalPolicy'], 'on-request')
        self.assertEqual(start['model'], 'test-model')
        self.assertEqual(start['config']['sandbox_workspace_write.writable_roots'], ['/tmp'])
        book = stream.Book('codex')
        for i, line in enumerate(result.stdout.splitlines()):
            book.feed((line + '\n').encode(), at=100+i)
        self.assertEqual(book.rate_limits['primary']['resetsAt'], 4102444800)
        self.assertEqual((book.tokens_in, book.tokens_cached, book.tokens_out), (100, 20, 8))
        self.assertEqual(errors.reset_at(book.rate_limits, now=100), 4102444800)
        self.assertFalse(book.open_items)
        self.assertIsNotNone(book.end_at)
        beat = stream.classify('codex', dict(type='bridge.notification', method='item/agentMessage/delta'))
        self.assertEqual(beat['kind'], 'stream')
        self.assertFalse(beat['progress'])
        self.assertTrue(any(f['type'] == 'item.started' for f in frames))

    def test_handshake_fallback_and_timeout(self):
        for mode in ('bad', 'hang'):
            result = self.run_bridge(mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('bridge.fallback', result.stdout)
            self.assertEqual((self.root / 'fallback').read_text(), 'task prompt')

    def test_no_replay_after_turn_started(self):
        result = self.run_bridge('crash')
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / 'fallback').exists())
        self.assertIn('responseStreamDisconnected', result.stdout)

    def test_resume_subtracts_saved_usage(self):
        self.assertEqual(self.run_bridge().returncode, 0)
        self.env['INPUT_TOKENS'] = '150'
        cmd = self.command[:5] + ['resume', '00000000-0000-4000-8000-000000000006'] + self.command[5:]
        result = self.run_bridge(command=cmd)
        self.assertEqual(result.returncode, 0, result.stderr)
        book = stream.Book('codex')
        book.feed(result.stdout.encode(), at=100)
        self.assertEqual(book.tokens_in, 50)

    def test_structured_quota_failure(self):
        result = self.run_bridge('quota')
        self.assertEqual(result.returncode, 1, result.stderr)
        frame = next(json.loads(line) for line in result.stdout.splitlines() if json.loads(line)['type'] == 'turn.failed')
        failure = errors.classify('codex', frame)
        self.assertEqual((failure['source'], failure['category']), ('structured', 'quota'))

    def test_resume(self):
        cmd = self.command[:5] + ['resume', 'saved-thread'] + self.command[5:]
        result = self.run_bridge(command=cmd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(next(p['params']['threadId'] for p in self.rpc() if p.get('method') == 'thread/resume'), 'saved-thread')

    def test_secret_approval_denied_and_audited(self):
        result = self.run_bridge('approval')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(next(p['result']['decision'] for p in self.rpc() if p.get('id') == 'approve'), 'decline')
        self.assertIn('write a secret file', result.stdout)
        self.assertIn('bridge.approval', result.stdout)

    def test_all_approval_response_shapes(self):
        cases = [
            ('execCommandApproval', dict(command=['pwd']), 'approved'),
            ('applyPatchApproval', dict(fileChanges={'.env': dict(type='add')}), dict(denied=dict(rejection='write a secret file'))),
            ('item/commandExecution/requestApproval', dict(command='pwd'), 'accept'),
            ('item/fileChange/requestApproval', dict(itemId='file'), 'decline'),
            ('item/permissions/requestApproval', dict(permissions=dict(fileSystem=dict(write=['.env']))), None),
        ]
        for method, params, decision in cases:
            with self.subTest(method=method):
                # Modern file approvals obtain paths from the preceding item event.
                fake = self.fake.read_text()
                if method == 'item/fileChange/requestApproval':
                    fake = fake.replace("if mode=='approval':", "if mode=='approval':\n   send({'method':'item/started','params':{'item':{'id':'file','type':'fileChange','changes':[{'path':'.env','kind':{'type':'add'}}]}}})")
                    self.fake.write_text(fake)
                self.env.update(APPROVAL_METHOD=method, APPROVAL_PARAMS=json.dumps(params))
                result = self.run_bridge('approval')
                self.assertEqual(result.returncode, 0, result.stderr)
                response = [p['result'] for p in self.rpc() if p.get('id') == 'approve'][-1]
                if decision:
                    self.assertEqual(response['decision'], decision)
                else:
                    self.assertEqual(response['permissions'], {})

    def test_rename_and_grant_secret_targets_are_denied(self):
        with patch.dict(os.environ, self.env, clear=True):
            renamed = bridge.approval(self.base, 'item/fileChange/requestApproval', dict(itemId='file'),
                dict(file=dict(changes=[dict(path='safe.txt', kind=dict(type='update', move_path='.env'))])))
            grant = bridge.approval(self.base, 'item/fileChange/requestApproval',
                dict(itemId='file', grantRoot='.env'), dict(file=dict(changes=[dict(path='safe.txt', kind=dict(type='add'))])))
        self.assertEqual(renamed['decision'], 'decline')
        self.assertEqual(grant['decision'], 'decline')

    def test_ask_routes_to_blocking_public_command(self):
        from types import SimpleNamespace
        with patch.dict(os.environ, self.env, clear=True), patch.object(bridge.subprocess, 'run',
                return_value=SimpleNamespace(returncode=0, stdout='allow')) as ask:
            reply = bridge.approval(self.base, 'item/commandExecution/requestApproval',
                                    dict(command='git push'), {})
        self.assertEqual(reply['decision'], 'accept')
        self.assertIn('--blocking', ask.call_args[0][0])
        self.assertIn('ask', ask.call_args[0][0])

    def test_live_steer_and_interrupt(self):
        child = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, cwd=self.root, env=dict(self.env, FAKE_MODE='live'))
        try:
            child.stdin.write('task'); child.stdin.close(); child.stdin = None
            deadline = time.monotonic() + 5
            while not Path(bridge.endpoint(self.base)).exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(bridge.control(self.base, 'turn/steer', 'correction')['ok'])
            self.assertTrue(bridge.control(self.base, 'turn/interrupt')['ok'])
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            steer = next(p['params'] for p in self.rpc() if p.get('method') == 'turn/steer')
            self.assertEqual(steer['expectedTurnId'], 'turn-test')
            self.assertEqual(steer['input'][0]['text'], 'correction')
        finally:
            if child.poll() is None:
                child.kill(); child.wait()

    def test_launcher_opt_in(self):
        env = dict(self.env, CWD=str(self.root), CODEX_BIN=str(self.fake), CODEX_MODEL='test-model',
                   CODEX_HOME=str(self.root / 'codex-home'), GODFATHER_CODEX_PROTOCOL='app-server')
        # Drop the containing real run's identity and shims from this isolated launch.
        for key in list(env):
            if key.startswith(('AGENT_', 'GODFATHER_')) and key not in (
                    'AGENT_RUNS_DIR', 'GODFATHER_CODEX_PROTOCOL', 'GODFATHER_CODEX_HANDSHAKE_TIMEOUT'):
                env.pop(key)
        env['PATH'] = os.environ.get('GODFATHER_ORIGINAL_PATH', env['PATH'])
        env.pop('BASH_ENV', None); env.pop('ZDOTDIR', None)
        prompt = self.root / 'prompt.md'
        prompt.write_text('task prompt')
        result = subprocess.run([str(Path(bridge.__file__).with_name('codex-run')), str(prompt), 'run'],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        turn = next(p['params'] for p in self.rpc() if p.get('method') == 'turn/start')
        self.assertIn('Godfather oversight', turn['input'][0]['text'])
        self.assertIn('task prompt', turn['input'][0]['text'])
        config = next(p['params']['config'] for p in self.rpc() if p.get('method') == 'thread/start')
        self.assertIsInstance(config['shell_environment_policy.set'], dict)
        self.assertIn('GODFATHER_BASE', config['shell_environment_policy.set'])
        saved = json.loads(Path(str(self.base) + '.run.json').read_text())
        self.assertEqual(saved['launcher_env']['GODFATHER_CODEX_PROTOCOL'], 'app-server')
        self.assertEqual(saved['session_id'], '00000000-0000-4000-8000-000000000006')

    def test_supervisor_nudge_uses_live_control(self):
        import godfather_lifecycle as life
        import godfather_supervisor as supervisor
        run = dict(cli='codex', session_id='saved', status='running')
        with patch.object(life.s, 'resolve_name', return_value=self.base), \
                patch.object(life.s, 'read', return_value=run), \
                patch.object(life.a, 'discover', return_value=run), \
                patch.object(supervisor, 'pending_question', return_value=False), \
                patch.object(life.s, 'event'), patch.object(bridge, 'control') as control:
            self.assertEqual(life.deliver_nudge('run', 'correct course'), 0)
        control.assert_called_once_with(self.base, 'turn/steer', 'correct course')
        self.assertFalse(Path(str(self.base) + '.nudge.json').exists())

    def injected_bridge(self, setup):
        code = ('import sys; sys.path.insert(0, ' + repr(str(Path(bridge.__file__).parent)) + '); '
                'import godfather_codex_bridge as b; ' + setup +
                '; sys.argv=[b.__file__]+sys.argv[1:]; sys.exit(b.main())')
        return [sys.executable, '-c', code] + self.command[2:]

    def test_unclear_answers_reply_with_denial_and_original_reason(self):
        for answer in ('no, please stop', '', 'allowish', 'deny (too broad)', 'allow (approved)'):
            with self.subTest(answer=answer):
                self.env['APPROVAL_PARAMS'] = json.dumps(dict(command='git push'))
                cmd = self.injected_bridge('from types import SimpleNamespace; '
                    'b.subprocess.run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=' + repr(answer) + ')')
                result = self.run_bridge('approval', command=cmd)
                self.assertEqual(result.returncode, 0, result.stderr)
                reply = [p['result'] for p in self.rpc() if p.get('id') == 'approve'][-1]
                self.assertEqual(reply['decision'], 'accept' if answer == 'allow (approved)' else 'decline')
                self.assertIn('; answer: ' + answer, result.stdout)

    def test_approval_exceptions_always_answer_every_rpc_shape(self):
        from codex_schema_fixture import SCHEMAS
        for method in SCHEMAS:
            with self.subTest(method=method):
                self.env['APPROVAL_METHOD'] = method
                cmd = self.injected_bridge("b.approval=lambda *a: (_ for _ in ()).throw(RuntimeError('policy unavailable'))")
                result = self.run_bridge('approval', command=cmd)
                self.assertEqual(result.returncode, 0, result.stderr)
                reply = [p['result'] for p in self.rpc() if p.get('id') == 'approve'][-1]
                if method == 'item/permissions/requestApproval':
                    self.assertEqual(reply['permissions'], {})
                elif method in ('execCommandApproval', 'applyPatchApproval'):
                    self.assertIn('policy unavailable', reply['decision']['denied']['rejection'])
                else:
                    self.assertEqual(reply['decision'], 'decline')
                self.assertIn('policy unavailable', json.dumps(self.rpc()))

    def test_audit_failure_denies_instead_of_stranding_approval(self):
        self.env['APPROVAL_PARAMS'] = json.dumps(dict(command='pwd'))
        cmd = self.injected_bridge("import godfather_policy as policy; "
            "policy.incident=lambda *a, **k: (_ for _ in ()).throw(OSError('audit disk unavailable'))")
        result = self.run_bridge('approval', command=cmd)
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = [p['result'] for p in self.rpc() if p.get('id') == 'approve'][-1]
        self.assertEqual(reply['decision'], 'decline')
        self.assertIn('audit disk unavailable', result.stdout)
        self.assertIn('bridge.approval', result.stdout)

    def test_all_emitted_approval_variants_match_generated_schemas(self):
        from codex_schema_fixture import SCHEMAS
        for method in SCHEMAS:
            for allowed in (False, True):
                with self.subTest(method=method, allowed=allowed):
                    reply = bridge.approval_response(method, allowed, 'policy reason', dict(network=dict(enabled=True)))
                    reply.pop('_reason')
                    validate(method, reply)
        for method in ('execCommandApproval', 'applyPatchApproval'):
            with self.assertRaises(AssertionError):
                validate(method, dict(decision='denied'))
            with self.assertRaises(AssertionError):
                validate(method, dict(decision=dict(denied={})))

    def test_failed_live_steer_is_queued_for_delivery(self):
        import godfather_lifecycle as life
        import godfather_supervisor as supervisor
        from types import SimpleNamespace
        run = dict(cli='codex', session_id='saved', status='running', run_id='instance')
        for failure in (OSError('gone'), ValueError('RPC rejected'), TimeoutError('late'),
                        ValueError('control acknowledgement timed out'), RuntimeError('unexpected control failure')):
            with self.subTest(failure=failure), \
                    patch.object(life.s, 'resolve_name', return_value=self.base), \
                    patch.object(life.s, 'read', side_effect=lambda p: run if str(p).endswith('.run.json') else {}), \
                    patch.object(life.a, 'discover', return_value=run), \
                    patch.object(supervisor, 'pending_question', return_value=False), \
                    patch.object(life.s, 'event'), \
                    patch.object(life.a, 'processes', return_value={}), \
                    patch.object(life.subprocess, 'Popen', return_value=SimpleNamespace(pid=1234)), \
                    patch.object(bridge, 'control', side_effect=failure):
                self.assertEqual(life.deliver_nudge('run', 'persist this correction'), 0)
                queued = json.loads(Path(str(self.base) + '.nudge.json').read_text())
                self.assertTrue(queued['pending'])
                self.assertEqual(queued['message'], 'persist this correction')
                self.assertEqual(queued['run_id'], 'instance')

    def test_sparse_rate_limit_updates_preserve_windows_and_resets(self):
        result = self.run_bridge('sparse')
        self.assertEqual(result.returncode, 0, result.stderr)
        book = stream.Book('codex')
        book.feed(result.stdout.encode(), at=100)
        self.assertEqual(book.rate_limits['primary']['resetsAt'], 4102444800)
        self.assertEqual(book.rate_limits['secondary']['resetsAt'], 4102444900)
        self.assertEqual(errors.reset_at(book.rate_limits, now=100), 4102444900)

    def test_retry_notification_is_transient_and_report_can_finish(self):
        import godfather_supervisor as supervisor
        from types import SimpleNamespace
        for mode, expected in (('retry', None), ('terminal-error', 'crash')):
            with self.subTest(mode=mode):
                result = self.run_bridge(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                book = stream.Book('codex')
                book.feed(result.stdout.encode(), at=100)
                self.assertEqual(book.failure_class, expected)
                failure = next(json.loads(line) for line in result.stdout.splitlines()
                               if json.loads(line)['type'] == 'error')
                self.assertEqual(errors.classify('codex', failure)['transient'], mode == 'retry')
                self.assertFalse(errors.classify('codex', dict(failure, type='turn.failed'))['transient'])
                run = dict(cwd=str(self.root), pid=None)
                with patch.object(supervisor, 'pending_question', return_value=False), \
                        patch.object(supervisor, 'descendant_block', return_value=False), \
                        patch.object(supervisor, 'git_head', return_value='new-commit'):
                    report = supervisor.finished_report(run, SimpleNamespace(book=book), self.base,
                                                        'old-commit', now=10000)
                self.assertEqual(report is not None, mode == 'retry')

    def test_model_protocol_validation(self):
        config = '[[models]]\nbackend="codex"\nmodel="test"\neffort="high"\nprotocol="app-server"\n'
        self.assertEqual(router.parse_models(config)[0]['protocol'], 'app-server')
        with self.assertRaises(ValueError):
            router.parse_models(config.replace('codex', 'grok'))
        with self.assertRaises(ValueError):
            router.parse_models(config.replace('app-server', 'typo'))


if __name__ == '__main__':
    unittest.main()
