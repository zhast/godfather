"""Live incident regressions. All workers and registries here are hermetic."""
import argparse
import json
import os
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
import godfather_safety as safety
import godfather_store as store


class Round6Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.env = dict(os.environ, HOME=str(self.root), AGENT_RUNS_DIR=str(self.runs),
                        XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                        GODFATHER_QUEUE_DAEMON='1', PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('GODFATHER_DB', None)
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def run_record(self, name='task', **extra):
        run = dict(name=name, run_id=name, cli='codex', model='fake', cwd=str(self.root),
                   status='running', started_at=time.time() - 2000, pid=os.getpid(),
                   pid_identity=a.processes()[os.getpid()]['identity'], progress_paths=[])
        run.update(extra)
        safety.atomic(self.runs / (name + '.run.json'), run)
        return run

    def test_blocking_ask_surfaces_in_status_watch_and_wait_until_consumed(self):
        for routed in (False, True):
            name = 'routed' if routed else 'plain'
            self.run_record(name)
            base = self.runs / name
            if routed:
                safety.atomic(str(base) + '.route.json', dict(name=name, run_id='route', status='running',
                              active_name=name, router_pid=os.getpid(), router_identity=a.processes()[os.getpid()]['identity']))
            # Reproduce a persisted question with no NEEDS_INPUT event (e.g. ask
            # interrupted after the durable control write). delivery is to the agent.
            safety.control(base, lambda d: d.update(questions=[dict(id=name, question='Proceed with inspection?',
                           answer=None, delivered=False, waiting=True, blocking=True, asked_at=time.time()-1900)]))
            self.assertEqual('needs_input', store.load_runs(self.runs)[-1]['status'])
            row = next(r for r in a.snapshot(self.runs) if r['name'] == name)
            self.assertEqual('needs_input', row['status'])
            self.assertIn('OVERDUE question: Proceed', row['last_action'])
            self.assertEqual(('needs_input', False), life.wait_run(name, .01))
            watch = subprocess.Popen([str(ROOT / 'bin/agents-watch'), '--no-heal', '--poll-secs', '.03'],
                                     env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                import selectors
                selector = selectors.DefaultSelector()
                selector.register(watch.stdout, selectors.EVENT_READ)
                output = b''
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and ('NEEDS_INPUT ' + name).encode() not in output:
                    if selector.select(.1):
                        output += os.read(watch.stdout.fileno(), 65536)
                selector.close()
                self.assertIn(('NEEDS_INPUT ' + name).encode(), output)
            finally:
                watch.terminate()
                watch.communicate(timeout=5)
            safety.control(base, lambda d: d['questions'][0].update(answer='yes'))
            self.assertEqual('needs_input', life.logical_status(name))
            import godfather_mcp as mcp
            self.assertEqual('needs_input', mcp.report(name)['status'])
            safety.control(base, lambda d: d['questions'][0].update(delivered=True, waiting=False))
            self.assertEqual('running', life.logical_status(name))
            row = next(r for r in a.snapshot(self.runs) if r['name'] == name)
            self.assertEqual('running', row['status'])

    def test_stop_queued_run_with_retired_placeholder_and_broken_store(self):
        import sqlite3
        import godfather_registry as registry
        for broken in (False, True):
            name = 'broken' if broken else 'queued'
            child = subprocess.Popen([sys.executable, '-c', 'import subprocess,time; subprocess.Popen(["sleep","30"]); time.sleep(30)'],
                                     start_new_session=True)
            try:
                job = dict(name=name, status='queued', dependencies=[], created=time.time())
                life.queue_path(name).parent.mkdir(parents=True, exist_ok=True)
                safety.atomic(life.queue_path(name), job)
                safety.atomic(life.queue_path(name), dict(job, status='running'))
                run = self.run_record(name, pid=child.pid, pid_identity=a.processes()[child.pid]['identity'],
                                      process_session_id=child.pid)
                snapshot = a.processes()
                descendants = {pid: row['identity'] for pid, row in snapshot.items() if row.get('sid') == child.pid}
                with mock.patch.object(registry, 'note_write', side_effect=sqlite3.IntegrityError('broken mirror')) if broken else __import__('contextlib').nullcontext():
                    self.assertEqual(0, life.stop_run(name))
                child.wait(timeout=3)

                self.assertFalse(any(a.alive(pid, identity) for pid, identity in descendants.items()))
                self.assertEqual('stopped', safety.read(self.runs / (name + '.run.json'))['status'])
                self.assertTrue(safety.read(self.runs / (name + '.control.json'))['stop_requested'])
                if not broken:
                    self.assertEqual('stopped', store.machine_status(self.runs, name)['status'])
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, __import__('signal').SIGKILL)
                child.wait(timeout=3)

    def test_incomplete_reports_and_unanswered_blocking_exits_keep_dependents_queued(self):
        import godfather_registry as registry
        for label in ('INCOMPLETE', 'BLOCKED', 'FAILED', 'pending'):
            base = self.runs / label
            run = self.run_record(label, launcher_pid=os.getppid())
            Path(str(base) + '.last.md').write_text('Completed.\n' if label == 'pending' else label + ': sync failed; work remains.\n')
            if label == 'pending':
                safety.control(base, lambda d: d.update(questions=[dict(id='q', blocking=True, waiting=True,
                               answer=None, delivered=False, question='Proceed?')]))
            self.assertEqual(1, a.finish(str(base), 0))
            failed = safety.read(str(base) + '.run.json')
            self.assertEqual('failed', failed['status'])
            self.assertEqual('pending_question' if label == 'pending' else 'incomplete', failed['failure_class'])
            # Reconciliation cannot wash the failure away by expiring the ask.
            dead = registry.exited_record(dict(run, exit_code=0), base, safety.read(str(base) + '.control.json').get('questions', []))
            self.assertEqual('failed', dead['status'])
            job = dict(name='child-' + label, dependencies=[label], dependency_instances={label: label},
                       status='queued', created=time.time(), any_status=False)
            life.queue_path(job['name']).parent.mkdir(parents=True, exist_ok=True)
            gate, detail = life.dependency_gate(job)
            self.assertEqual('waiting', gate)
            self.assertIn('blocking question' if label == 'pending' else label + ': sync failed', detail)
            self.assertEqual('ready', life.dependency_gate(dict(job, any_status=True))[0])
            row = next(r for r in a.snapshot(self.runs) if r['name'] == job['name'])
            self.assertEqual('queued', row['status'])
            self.assertIn(label, row['last_action'])
        self.assertIn('INCOMPLETE, BLOCKED, or FAILED', safety.PREAMBLE)

    def test_live_codex_nudge_reopens_after_natural_finish_without_signals(self):
        fake = self.root / 'codex'
        fake.write_text('''#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path
root = Path(os.environ['CWD'])
session = '33333333-3333-3333-3333-333333333333'
prompt = sys.stdin.read()
if 'resume' in sys.argv:
    assert session in sys.argv, sys.argv
    assert (root / 'uncommitted-work').read_text() == 'preserved'
    assert (root / 'original-finished').exists()
    assert 'Deliver this queued correction' in prompt
    (root / 'resumed-session').write_text(session)
    result = 'Queued correction received.'
else:
    def terminated(*_):
        (root / 'received-signal').touch()
        sys.exit(99)
    signal.signal(signal.SIGTERM, terminated)
    print(json.dumps({'type':'thread.started','thread_id':session}), flush=True)
    (root / 'uncommitted-work').write_text('preserved')
    while not (root / 'release-turn').exists():
        time.sleep(.03)
    (root / 'original-finished').touch()
    result = 'Original turn completed.'
Path(sys.argv[sys.argv.index('-o')+1]).write_text(result)
''')
        fake.chmod(0o700)
        prompt = self.root / 'prompt.md'
        prompt.write_text('Do the work.')
        env = dict(self.env, CWD=str(self.root), CODEX_BIN=str(fake), CODEX_MODEL='fake')
        launcher = subprocess.Popen([str(ROOT / 'bin/codex-run'), str(prompt), 'live'], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not (self.root / 'uncommitted-work').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((self.root / 'uncommitted-work').exists())
            result = subprocess.run([str(ROOT / 'bin/godfather'), 'nudge', 'live', 'Deliver this queued correction'],
                                    env=env, capture_output=True, text=True, timeout=3)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('queued', result.stdout)
            self.assertIsNone(launcher.poll())
            self.assertFalse((self.root / 'resumed-session').exists())
            self.assertFalse((self.root / 'received-signal').exists())
            (self.root / 'release-turn').touch()
            launcher.communicate(timeout=10)
            self.assertEqual(0, launcher.returncode)
            self.assertEqual(('done', False), life.wait_run('live', 10))
            self.assertTrue((self.root / 'resumed-session').exists())
            self.assertEqual('Queued correction received.', (self.runs / 'live.last.md').read_text())
            self.assertEqual('Original turn completed.', (self.runs / 'live.last.1.md').read_text())
            request = safety.read(self.runs / 'live.nudge.json')
            self.assertFalse(request['pending'])
            self.assertNotIn('expired', request)
            log = (self.runs / 'live.models.log').read_text()
            self.assertNotIn('NUDGE_EXPIRED', log)
            self.assertIn('NUDGE session=', log)
            self.assertEqual('preserved', (self.root / 'uncommitted-work').read_text())
            self.assertFalse((self.root / 'received-signal').exists())
            # Nudging the finished run is a deliberate reopen of the same session.
            reopened = subprocess.run([str(ROOT / 'bin/godfather'), 'nudge', '--wait', 'live', 'Deliver this queued correction'],
                                      env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(0, reopened.returncode, reopened.stderr)
            self.assertEqual('33333333-3333-3333-3333-333333333333', (self.root / 'resumed-session').read_text())
            self.assertEqual('Queued correction received.', (self.runs / 'live.last.md').read_text())
            self.assertEqual('Original turn completed.', (self.runs / 'live.last.1.md').read_text())
            self.assertEqual('Queued correction received.', (self.runs / 'live.last.2.md').read_text())
        finally:
            (self.root / 'release-turn').touch()
            launcher.communicate(timeout=10)
