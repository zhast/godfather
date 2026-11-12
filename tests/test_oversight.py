#!/usr/bin/env python3
"""Offline regression tests; never invoke real agent services or signal existing runs."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import agent_oversight as a

BIN = Path(__file__).resolve().parents[1] / 'bin'


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / 'events.jsonl'
        self.run = dict(cli='agy', started_at=a.now(), progress_paths=[str(self.file)])
        self.progress = a.Progress(self.run)

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, event):
        with self.file.open('a') as f:
            f.write(json.dumps(event) + '\n')

    def test_partial_line_and_no_duplicate_error(self):
        self.file.write_text('{"step_index":1,"type":"ERROR_MESSAGE","content":"stream was interrupted"}')
        self.progress.update(self.run)
        self.assertEqual([], self.progress.errors)
        with self.file.open('a') as f:
            f.write('\n')
        self.assertEqual([('ERROR', 'stream was interrupted')], self.progress.update(self.run).events)
        self.assertEqual([], self.progress.update(self.run).events)
        self.append(dict(step_index=2, type='ERROR_MESSAGE', content='stream was interrupted'))
        self.assertEqual([], self.progress.update(self.run).events)

    def test_agy_usage_actions_loop_and_tool_failure(self):
        for i in range(5):
            self.append(dict(step_index=i, type='PLANNER_RESPONSE', input_tokens=10, output_tokens=2,
                             tool_calls=[dict(name='view_file', args={'AbsolutePath': '/tmp/x'})]))
        self.append(dict(step_index=5, type='GENERIC', content='Command failed. Exit code: 1'))
        self.progress.update(self.run)
        self.assertEqual((50, 10, 6), (self.progress.tokens_in, self.progress.tokens_out, self.progress.steps))
        self.assertEqual(1, sum(k == 'LOOP' for k, _ in self.progress.events))
        self.assertTrue(any(k == 'ERROR' for k, _ in self.progress.events))

    def test_codex_usage_and_failed_tool(self):
        self.run['cli'] = 'codex'
        self.append(dict(type='response_item', payload=dict(type='function_call', name='exec_command', call_id='1', arguments='{"cmd":"false"}')))
        self.append(dict(type='response_item', payload=dict(type='function_call_output', call_id='1', output='Process exited with code 1')))
        self.append(dict(type='event_msg', payload=dict(type='token_count', info=dict(total_token_usage=dict(input_tokens=30, output_tokens=4)))))
        self.progress.update(self.run)
        self.assertEqual((30, 4, 1), (self.progress.tokens_in, self.progress.tokens_out, self.progress.steps))
        self.assertEqual([], self.progress.events)
        self.append(dict(type='event_msg', payload=dict(type='stream_error', message='API stream was interrupted')))
        self.assertEqual([('ERROR', 'API stream was interrupted')], self.progress.update(self.run).events)

    def test_grok_usage_end_authoritative_and_quota(self):
        self.run['cli'] = 'grok'
        self.append(dict(type='usage', messageId='1', usage=dict(input_tokens=10, output_tokens=4)))
        self.append(dict(type='usage', messageId='2', usage=dict(input_tokens=20, output_tokens=5)))
        self.append(dict(type='end', usage=dict(input_tokens=28, output_tokens=9)))
        self.append(dict(type='error', message='quota exhausted'))
        self.progress.update(self.run)
        self.assertEqual((28, 9), (self.progress.tokens_in, self.progress.tokens_out))
        self.assertEqual([('QUOTA', 'quota exhausted')], self.progress.events)

    def test_resume_offsets_ignore_old_errors(self):
        self.append(dict(step_index=0, type='ERROR_MESSAGE', content='old error'))
        self.run['progress_offsets'] = {str(self.file): self.file.stat().st_size}
        progress = a.Progress(self.run)
        self.append(dict(step_index=1, type='PLANNER_RESPONSE', content='OK', input_tokens=8, output_tokens=1))
        progress.update(self.run)
        self.assertEqual([], progress.errors)
        self.assertEqual((8, 1), (progress.tokens_in, progress.tokens_out))

    def test_diagnostics_do_not_prevent_stall_and_auth_deduplicates(self):
        self.file.write_text('')
        diag = self.root / 'cli.log'
        self.run['diagnostic_paths'] = [str(diag)]
        initial = self.progress.last_at
        diag.write_text('E1019 13:00:00.000001 1 cache.go:1] You are not logged into Antigravity.\n'
                        'E1019 13:00:00.000002 2 cache.go:2] You are not logged into Antigravity.\n')
        self.progress.update(self.run)
        self.assertEqual([], self.progress.events)
        self.assertEqual(initial, self.progress.last_at)
        self.run['status'] = 'failed'
        self.assertEqual(1, len(self.progress.update(self.run).events))

    def test_prose_loop(self):
        self.run['cli'] = 'grok'
        self.append(dict(type='thought', data='x' * 17000))
        self.progress.update(self.run)
        self.assertEqual('LOOP', self.progress.events[0][0])


class MonitorTests(unittest.TestCase):
    def test_stall_escalation_error_once_died_and_done(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            progress = root / 'progress.jsonl'
            progress.write_text(json.dumps(dict(step_index=1, type='ERROR_MESSAGE', content='stream was interrupted')) + '\n')
            child = subprocess.Popen(['sleep', '30'])
            watch = None
            try:
                run = dict(name='fixture', cli='agy', model='fake', pid=child.pid, started_at=a.now(), status='running', progress_paths=[str(progress)])
                path = root / 'fixture.run.json'
                a.write(path, run)
                watch = subprocess.Popen([str(BIN / 'agents-watch'), '--no-heal', '--stall-secs', '1', '--poll-secs', '.04'],
                                         env=dict(os.environ, AGENT_RUNS_DIR=temp), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                selector = selectors.DefaultSelector()
                selector.register(watch.stdout, selectors.EVENT_READ)
                lines = []
                pending = b''
                def until(predicate, timeout=8):
                    nonlocal pending
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if selector.select(.05):
                            pending += os.read(watch.stdout.fileno(), 65536)
                            while b'\n' in pending:
                                line, pending = pending.split(b'\n', 1)
                                lines.append(line.decode().strip())
                        if predicate():
                            return
                    self.fail('missing expected event: ' + repr(lines))
                until(lambda: sum(l.startswith('STALL ') for l in lines) == 3)
                self.assertEqual(1, sum(l.startswith('ERROR ') for l in lines))
                child.terminate()
                child.wait()
                until(lambda: 'DIED fixture' in lines)
                until(lambda: any(l.startswith('DONE fixture exit=1') for l in lines))
                self.assertEqual('failed', a.read(path)['status'])
                self.assertEqual(3, sum(l.startswith('STALL ') for l in lines))
                self.assertEqual(1, sum(l.startswith('DIED ') for l in lines))
                selector.close()
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait()
                if watch:
                    watch.terminate()
                    watch.communicate(timeout=5)


FAKE_AGY = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
home = Path.home()
if args[args.index('-p') + 1] == '/usage':
    if os.getenv('FAKE_USAGE_FAILURE'):
        print('error: /usage failed: service unavailable (503)', file=sys.stderr)
        sys.exit(1)
    print('Gemini Models\tWeekly Limit Remaining\t100%')
    print('Claude and GPT models\tWeekly Limit Remaining\t100%')
    sys.exit(0)
model = args[args.index('--model') + 1]
session = args[args.index('--conversation') + 1] if '--conversation' in args else '11111111-1111-1111-1111-111111111111'
log = Path(args[args.index('--log-file') + 1])
log.write_text('Print mode: conversation=' + session + '\n')
p = home / '.gemini/antigravity-cli/brain' / session / '.system_generated/logs/transcript.jsonl'
p.parent.mkdir(parents=True, exist_ok=True)
with (home / 'calls.jsonl').open('a') as f:
    f.write(json.dumps(dict(model=model, session=session, prompt=args[args.index('-p') + 1]))+'\n')
with p.open('a') as f:
    f.write(json.dumps(dict(step_index=int(time.time_ns()), type='PLANNER_RESPONSE', content='OK', input_tokens=5, output_tokens=1))+'\n')
if os.getenv('FAKE_BLOCK') and '--conversation' not in args:
    time.sleep(3)
    (home / 'original-turn-finished').write_text('finished naturally')
if os.getenv('FAKE_FAILURE') == '1' or (os.getenv('FAKE_FAILURE') == 'after-quota' and '--conversation' in args):
    print('Error: tool failed', file=sys.stderr)
    sys.exit(7)
if model.startswith('gemini') and '--conversation' not in args:
    print('Error: quota exhausted', file=sys.stderr)
    sys.exit(1)
print('OK')
'''


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fake = self.root / 'bin'
        fake.mkdir()
        (fake / 'agy').write_text(FAKE_AGY)
        (fake / 'agy').chmod(0o755)
        self.prompt = self.root / 'prompt.md'
        self.prompt.write_text('test')
        self.env = dict(HOME=str(self.root), CWD=str(self.root), AGENT_RUNS_DIR=str(self.root), PYTHONDONTWRITEBYTECODE='1', PATH=str(fake) + ':' + os.environ['PATH'])
        self.launcher = BIN / ('agy_run.sh' if (BIN / 'agy_run.sh').exists() else 'agy-run')

    def tearDown(self):
        self.tmp.cleanup()

    def test_quota_fallback_resumes_exact_conversation(self):
        result = subprocess.run([str(self.launcher), str(self.prompt), 'route'], env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        run = a.read(self.root / 'route.run.json')
        self.assertEqual('done', run['status'])
        self.assertEqual('claude-opus-5-5-high', run['model'])
        calls = [json.loads(l) for l in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0]['session'], calls[1]['session'])
        ledger = self.root / '.local/state/godfather/ledger.jsonl'
        records = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual([5, 5], [r['tokens_in'] for r in records])
        self.assertEqual([False, True], [r['quota_fallback'] for r in records])


    def test_reused_name_cannot_discover_old_conversation(self):
        old = self.root / 'reuse.segment-1.cli.log'
        old.write_text('Print mode: conversation=22222222-2222-2222-2222-222222222222\n')
        result = subprocess.run([str(self.launcher), str(self.prompt), 'reuse'], env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('11111111-1111-1111-1111-111111111111', a.read(self.root / 'reuse.run.json')['conversation_id'])

    def test_nonquota_failure_stops(self):
        result = subprocess.run([str(self.launcher), str(self.prompt), 'fail'], env=dict(self.env, FAKE_FAILURE='1'), capture_output=True, text=True, timeout=15)
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertEqual('failed', a.read(self.root / 'fail.run.json')['status'])
        self.assertEqual(1, len((self.root / 'calls.jsonl').read_text().splitlines()))

    def test_nonquota_after_quota_is_failed(self):
        result = subprocess.run([str(self.launcher), str(self.prompt), 'fail-after-quota'], env=dict(self.env, FAKE_FAILURE='after-quota'), capture_output=True, text=True, timeout=15)
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertEqual('failed', a.read(self.root / 'fail-after-quota.run.json')['status'])

    def test_failed_quota_preflight_proceeds_with_unknown_quota(self):
        result = subprocess.run([str(self.launcher), str(self.prompt), 'usage-failure'], env=dict(self.env, FAKE_USAGE_FAILURE='1'), capture_output=True, text=True, timeout=25)
        self.assertEqual(0, result.returncode, result.stderr)
        run = a.read(self.root / 'usage-failure.run.json')
        self.assertEqual('done', run['status'])
        self.assertIn('proceeding with unknown quota', (self.root / 'usage-failure.models.log').read_text())
        self.assertTrue((self.root / 'calls.jsonl').exists())

    def test_dry_run_writes_no_registry_or_outputs(self):
        result = subprocess.run([str(self.launcher), str(self.prompt), 'dry'], env=dict(self.env, DRY_RUN='1'), capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('Chosen model:', result.stdout)
        self.assertEqual([], list(self.root.glob('dry.*')))

    def test_live_nudge_reopens_natural_completion_and_preserves_session(self):
        launcher = subprocess.Popen([str(self.launcher), str(self.prompt), 'live'], env=dict(self.env, FAKE_BLOCK='1'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        old = {}
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                old = a.read(self.root / 'live.run.json')
                if old.get('conversation_id'):
                    break
                time.sleep(.05)
            self.assertTrue(old.get('conversation_id'))
            # Also verify reusing a live name fails before truncating any outputs.
            log = self.root / 'live.models.log'
            before = log.read_text()
            duplicate = subprocess.run([str(self.launcher), str(self.prompt), 'live'], env=self.env, capture_output=True, text=True, timeout=5)
            self.assertEqual(2, duplicate.returncode)
            self.assertEqual(before, log.read_text())
            result = subprocess.run([str(BIN / 'agents-nudge'), '--wait', 'live', 'Correct course; reply OK'], env=self.env, capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            launcher.communicate(timeout=5)
            self.assertEqual(0, launcher.returncode)
            self.assertTrue((self.root / 'original-turn-finished').exists())
            self.assertFalse(a.run_alive(old))
            self.assertEqual('done', a.read(self.root / 'live.run.json')['status'])
            self.assertEqual('OK', (self.root / 'live.last.md').read_text().strip())
            self.assertNotIn('NUDGE_EXPIRED', log.read_text())
            safety = __import__('godfather_safety')
            preamble = safety.PREAMBLE + safety.notes_preamble((self.root / 'live.notes.md').resolve())
            calls = [json.loads(l) for l in (self.root / 'calls.jsonl').read_text().splitlines()]
            self.assertEqual(preamble + 'Correct course; reply OK', calls[-1]['prompt'])
            self.assertEqual(old['conversation_id'], a.read(self.root / 'live.run.json')['conversation_id'])
            self.assertEqual('OK', (self.root / 'live.last.1.md').read_text().strip())
            reopened = subprocess.run([str(BIN / 'agents-nudge'), '--wait', 'live', 'Correct course; reply OK'], env=self.env, capture_output=True, text=True, timeout=20)
            self.assertEqual(0, reopened.returncode, reopened.stderr)
            final = a.read(self.root / 'live.run.json')
            self.assertEqual('done', final['status'])
            self.assertEqual(old['conversation_id'], final['conversation_id'])
            self.assertEqual('OK', (self.root / 'live.last.2.md').read_text().strip())
            self.assertRegex(log.read_text(), r'NUDGE previous report preserved at \S*/live\.last\.1\.md\n')
            calls = [json.loads(l) for l in (self.root / 'calls.jsonl').read_text().splitlines()]
            self.assertEqual(preamble + 'Correct course; reply OK', calls[-1]['prompt'])
        finally:
            if launcher.poll() is None:
                if old.get('supervisor_pid'):
                    os.kill(old['supervisor_pid'], signal.SIGTERM)
                launcher.terminate()
                launcher.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main(verbosity=2)
