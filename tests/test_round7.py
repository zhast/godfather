"""Session restart regressions: real process groups, hermetic model workers."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
import unittest
from unittest import mock

import test_router as fixture
import agent_oversight as a
import godfather_lifecycle as life
import godfather_mcp as mcp

ROOT = fixture.ROOT


class SessionSurvivalTests(unittest.TestCase):
    def setUp(self):
        fixture.RouterTests.setUp(self)
        self.env['GODFATHER_QUEUE_DAEMON'] = '1'
        fixture.RouterTests.config_models(self, ('codex', 'fake'))
        fake = fixture.FAKE.replace("with (Path.home() / 'calls.jsonl').open('a') as f:", '''
if os.getenv('SURVIVAL_READY'):
    import signal, time
    descendant = None
    if os.getenv('SURVIVAL_CHILD'):
        import subprocess
        descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']).pid
    Path(os.environ['SURVIVAL_READY']).write_text(json.dumps(dict(
        pid=os.getpid(), sid=os.getsid(0), pgid=os.getpgrp(),
        descendant=descendant,
        hup_ignored=signal.getsignal(signal.SIGHUP) == signal.SIG_IGN,
        tty=[os.isatty(fd) for fd in (0, 1, 2)])))
    Path(os.environ['SURVIVAL_READY'] + '.scratch').write_text('uncommitted work')
    while not Path(os.environ['SURVIVAL_RELEASE']).exists(): time.sleep(.02)
with (Path.home() / 'calls.jsonl').open('a') as f:''')
        for cli in ('codex', 'agy', 'grok'):
            (self.root / 'fake' / cli).write_text(fake)

    def await_ready(self, marker, shell):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if marker.exists():
                try:
                    worker = json.loads(marker.read_text())
                    records = list((self.root / 'runs').glob('*.run.json'))
                    run = next((a.read(p) for p in records if a.read(p).get('pid') == worker['pid']), {})
                    if run.get('supervisor_pid'):
                        return worker, run
                except ValueError:
                    pass
            if shell.poll() is not None:
                out, err = shell.communicate()
                self.fail('launch shell exited before the worker registered: ' + repr((out, err)))
            time.sleep(.03)
        self.fail('worker did not become ready')

    def test_routed_and_all_direct_launchers_survive_parent_group_hup_and_term(self):
        for backend in ('routed', 'codex', 'agy', 'grok'):
            for sig in (signal.SIGHUP, signal.SIGTERM):
                with self.subTest(backend=backend, signal=sig):
                    name = backend + '-' + str(sig)
                    marker, release = self.root / (name + '.ready'), self.root / (name + '.release')
                    env = dict(self.env, SURVIVAL_READY=str(marker), SURVIVAL_RELEASE=str(release),
                               CODEX_MODEL='fake', AGY_MODELS='claude-fake', GROK_MODEL='fake')
                    command = ([str(ROOT / 'bin/godfather'), 'run', '--no-isolate', str(self.prompt), name]
                               if backend == 'routed' else
                               [str(ROOT / 'bin' / (backend + '-run')), str(self.prompt), name])
                    # Pipes deliberately belong to the launching shell. Both
                    # it and the attachment client disappear on group death.
                    script = shlex.join(command) + ' & wait'
                    def reset_signals():
                        signal.signal(signal.SIGHUP, signal.SIG_DFL)
                        signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    shell = subprocess.Popen(['/bin/bash', '-c', script], env=env,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             start_new_session=True, preexec_fn=reset_signals)
                    run = {}
                    try:
                        worker, run = self.await_ready(marker, shell)
                        self.assertNotEqual(shell.pid, worker['sid'])
                        self.assertEqual(worker['pid'], worker['sid'])
                        self.assertTrue(worker['hup_ignored'])
                        self.assertEqual([False] * 3, worker['tty'])
                        snapshot = a.processes()
                        for key in ('launcher_pid', 'supervisor_pid'):
                            self.assertNotEqual(shell.pid, snapshot[run[key]]['sid'])
                            ancestor = run[key]
                            seen = set()
                            while ancestor in snapshot and ancestor not in seen:
                                seen.add(ancestor)
                                ancestor = snapshot[ancestor]['ppid']
                            self.assertNotIn(shell.pid, seen)
                            self.assertNotIn(os.getpid(), seen)
                        # A desktop may target descendants directly, in
                        # addition to sending a hangup to its terminal group.
                        for key in ('pid', 'launcher_pid', 'supervisor_pid'):
                            os.kill(run[key], signal.SIGHUP)
                        os.killpg(shell.pid, sig)
                        shell.communicate(timeout=4)
                        self.assertEqual(-sig, shell.returncode)
                        self.assertTrue(a.alive(run['pid'], run['pid_identity']))
                        self.assertEqual('uncommitted work', Path(str(marker) + '.scratch').read_text())
                        release.touch()
                        with mock.patch.dict(os.environ, env, clear=True):
                            result = life.wait_run(name, 12)
                            diagnostic = {p.name: p.read_text(errors='replace')[-2500:]
                                          for p in (self.root / 'runs').glob(name + '.*')
                                          if p.suffix in ('.json', '.log') and '.safety.' not in p.name and p.is_file()}
                            self.assertEqual(('done', False), result, diagnostic)
                        final = a.read(self.root / 'runs' / (name + '.run.json'))
                        self.assertEqual(('done', 0), (final['status'], final['exit_code']))
                        self.assertIn('OK', (self.root / 'runs' / (name + '.last.md')).read_text())
                    finally:
                        release.touch()
                        if shell.poll() is None:
                            os.killpg(shell.pid, signal.SIGKILL)
                        shell.communicate(timeout=4)
                        # Terminal JSON is published just before finish() and
                        # its SQLite mirror exit. Drain owners before cleanup.
                        deadline = time.monotonic() + 5
                        while run and time.monotonic() < deadline and a.alive(run.get('launcher_pid')):
                            time.sleep(.03)
                        with mock.patch.dict(os.environ, env, clear=True):
                            if run and (a.alive(run.get('pid')) or a.alive(run.get('launcher_pid'))):
                                life.stop_run(name)

    def test_done_publication_wins_over_stale_running_snapshot_after_owners_exit(self):
        base = self.root / 'runs' / 'published'
        base.parent.mkdir(exist_ok=True)
        run = dict(name='published', run_id='generation', status='done', pid=999999999)
        (base.with_suffix('.run.json')).write_text(json.dumps(run))
        stale = dict(run, status='running')
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(a, 'reconcile_run', return_value=stale), \
                mock.patch.object(a, 'alive', return_value=False):
            self.assertEqual('done', life.logical_status('published'))

    def test_stop_still_kills_detached_launcher_supervisor_and_cli_tree(self):
        name = 'stop-detached'
        marker, release = self.root / 'stop.ready', self.root / 'stop.release'
        env = dict(self.env, SURVIVAL_READY=str(marker), SURVIVAL_RELEASE=str(release),
                   SURVIVAL_CHILD='1', CODEX_MODEL='fake')
        shell = subprocess.Popen([str(ROOT / 'bin/codex-run'), str(self.prompt), name], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        run = {}
        try:
            worker, run = self.await_ready(marker, shell)
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(0, life.stop_run(name))
                self.assertEqual(('stopped', False), life.wait_run(name, .2))
            for key, identity in (('pid', 'pid_identity'), ('launcher_pid', 'launcher_identity'),
                                  ('supervisor_pid', 'supervisor_identity')):
                self.assertFalse(a.alive(run[key], run.get(identity)))
            self.assertFalse(a.alive(worker['descendant']))
        finally:
            release.touch()
            if shell.poll() is None:
                shell.terminate()
            shell.communicate(timeout=4)

    def test_mcp_cancel_does_not_signal_an_owner_after_terminal_publication(self):
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(life, 'logical_status', return_value='done'), \
                mock.patch.object(a, 'alive', return_value=True), \
                mock.patch.object(mcp.os, 'kill') as kill:
            self.assertEqual(dict(name='finished', status='done', cancelled=False), mcp.cancel_run('finished'))
            kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
