"""SQLite run store: state machine, import, and watch cursors."""
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
import godfather_registry as registry
import godfather_safety as safety
import godfather_store as store


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.state = self.root / 'state'
        self.env = dict(os.environ, AGENT_RUNS_DIR=str(self.runs), XDG_STATE_HOME=str(self.state),
                        XDG_CONFIG_HOME=str(self.root / 'config'), PYTHONDONTWRITEBYTECODE='1')
        self._mock = mock.patch.dict(os.environ, self.env, clear=True)
        self._mock.start()
        self.addCleanup(self._mock.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_watch_lease_migration_and_pid_identity_reuse(self):
        conn = store.connect(self.runs)
        conn.execute('DROP TABLE watch_leases')
        conn.execute('PRAGMA user_version = 2')
        conn.close()
        cursor = store.open_cursor(self.runs, 'watch', create=True)
        self.assertTrue(store.claim_delivery(self.runs, 'watch', 'already-delivered'))
        conn = store.connect(self.runs)
        conn.execute('INSERT INTO watch_leases VALUES(?,?,?,?)', ('watch', os.getpid(), 'old process identity', time.time()))
        self.assertEqual(store.SCHEMA_VERSION, conn.execute('PRAGMA user_version').fetchone()[0])
        conn.close()
        subscriber, seq = store.claim_watch(self.runs)
        self.assertEqual(('watch', cursor), (subscriber, seq))
        self.assertFalse(store.claim_delivery(self.runs, subscriber, 'already-delivered'))
        with self.assertRaisesRegex(ValueError, 'live watcher'):
            store.claim_watch(self.runs, 'watch')
        self.assertEqual('watch-2', store.claim_watch(self.runs)[0])
        store.release_watch(self.runs, 'watch')
        store.release_watch(self.runs, 'watch-2')
        with self.assertRaises(store.UnknownCursor):
            store.claim_watch(self.runs, 'missing', require_existing=True)

    def test_all_prior_schema_versions_keep_quota_waits_and_review_terminals(self):
        import sqlite3
        for version in (1, 2, 3):
            with self.subTest(version=version):
                runs = self.root / ('v%d' % version)
                store.insert_run(runs, 'waiting', 'quota', 'waiting_quota')
                cursor = store.open_cursor(runs, 'watch', create=True)
                store.claim_delivery(runs, 'watch', 'kept')
                conn = store.connect(runs)
                if version < 3:
                    conn.execute('DROP TABLE watch_leases')
                conn.execute('DROP INDEX runs_one_live_name')
                conn.execute("CREATE UNIQUE INDEX runs_one_live_name ON runs(name) WHERE status NOT IN ('done','failed')")
                conn.execute('PRAGMA user_version = %d' % version)
                conn.close()
                inode = store.db_path(runs).stat().st_ino
                conn = store.connect(runs)
                self.assertEqual(4, conn.execute('PRAGMA user_version').fetchone()[0])
                self.assertEqual('4', conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
                self.assertEqual(0, conn.execute('SELECT count(*) FROM watch_leases').fetchone()[0])
                conn.close()
                self.assertEqual(inode, store.db_path(runs).stat().st_ino)
                self.assertEqual(cursor, store.open_cursor(runs, 'watch', create=False))
                self.assertFalse(store.claim_delivery(runs, 'watch', 'kept'))
                self.assertEqual('waiting_quota', store.machine_status(runs, 'waiting')['status'])
                store.insert_run(runs, 'duplicate', 'another', 'running')
                conn = store.connect(runs)
                try:
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute("UPDATE runs SET name='quota' WHERE id='duplicate'")
                finally:
                    conn.close()
                store.insert_run(runs, 'review', 'reuse', 'review_findings')
                store.insert_run(runs, 'worker', 'reuse', 'running')
                store.transition(runs, 'waiting', 'retrying', 'quota reset')
                store.transition(runs, 'waiting', 'running', 'resume saved session')
                store.transition(runs, 'waiting', 'reviewing', 'review')
                store.transition(runs, 'waiting', 'review_findings', 'findings remain')

    def test_database_path_follows_the_registry(self):
        custom = store.db_path(self.runs)
        self.assertEqual(self.runs / '.godfather.sqlite', custom)
        default = store.db_path(Path.home() / '.agent-runs')
        self.assertEqual(self.state / 'godfather' / 'godfather.sqlite', default)

    def test_illegal_run_and_segment_transitions_raise(self):
        store.insert_run(self.runs, 'run-1', 'task', 'queued')
        with self.assertRaises(store.IllegalTransition):
            store.transition(self.runs, 'run-1', 'running', 'skip')
        self.assertEqual('queued', store.machine_status(self.runs, 'run-1')['status'])
        self.assertEqual(0, store.count_events(self.runs))
        store.transition(self.runs, 'run-1', 'provisioning', 'claim')
        store.transition(self.runs, 'run-1', 'running', 'start')
        store.transition(self.runs, 'run-1', 'done', 'finished')
        store.transition(self.runs, 'run-1', 'running', 'nudge')
        with self.assertRaises(store.IllegalTransition):
            store.transition(self.runs, 'run-1', 'dependency_failed', 'not an edge')
        store.insert_run(self.runs, 'blocked', 'blocked', 'dependency_failed')
        with self.assertRaises(store.IllegalTransition):
            store.transition(self.runs, 'blocked', 'running', 'reopen')
        store.insert_segment(self.runs, 'seg-1', 'run-1', 'starting')
        store.transition_segment(self.runs, 'seg-1', 'streaming', 'bytes')
        store.transition_segment(self.runs, 'seg-1', 'exited', 'closed')
        with self.assertRaises(store.IllegalTransition):
            store.transition_segment(self.runs, 'seg-1', 'streaming', 'backwards')
        self.assertEqual('exited', store.segment_status(self.runs, 'seg-1'))

    def test_lost_compare_and_set_does_not_emit(self):
        store.insert_run(self.runs, 'run-1', 'task', 'running')
        before = store.count_events(self.runs)
        found = store.transition(self.runs, 'run-1', 'done', 'stale', expect='queued')
        self.assertEqual('running', found)
        self.assertEqual('running', store.machine_status(self.runs, 'run-1')['status'])
        self.assertEqual(before, store.count_events(self.runs))
        store.transition(self.runs, 'run-1', 'done', 'finished', expect='running')
        self.assertEqual('done', store.machine_status(self.runs, 'run-1')['status'])
        self.assertEqual(before + 1, store.count_events(self.runs))
        seqs = [event['seq'] for event in store.events_after(self.runs, 0)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_import_is_idempotent_and_dead_pid_is_crash(self):
        (self.runs / 'live.run.json').write_text(json.dumps(dict(
            name='live', cli='codex', model='m', status='running', cwd='/work', run_id='live',
            pid=999999999, started_at=time.time())) + '\n')
        (self.runs / 'live.events.jsonl').write_text(
            json.dumps(dict(id='e1', at=time.time(), kind='CHECK', name='live', detail='fail (round 0)')) + '\n{truncated\n')
        (self.state / 'godfather' / 'queue').mkdir(parents=True)
        (self.state / 'godfather' / 'queue' / 'later.json').write_text(json.dumps(dict(
            name='later', status='queued', dependencies=['live'], any_status=False, env={}, created=time.time())) + '\n')
        warnings = []
        conn = store.connect(self.runs, migrate=False)
        try:
            warnings = registry.import_registries(conn, self.runs)
            again = registry.import_registries(conn, self.runs)
        finally:
            conn.close()
        self.assertTrue(any('truncated' in item for item in warnings))
        self.assertEqual(len(warnings), len(again))
        self.assertEqual('failed', store.machine_status(self.runs, 'live')['status'])
        self.assertEqual('crash', store.machine_status(self.runs, 'live')['failure_class'])
        self.assertEqual(1, len([event for event in store.events_after(self.runs, 0) if event['legacy_id'] == 'e1']))
        self.assertEqual('queued', next(job['status'] for job in store.load_queue(self.runs) if job['name'] == 'later'))
        self.assertTrue((self.runs / 'live.run.json').exists())

    def test_json_mirror_keeps_the_file_and_status_reads_the_store(self):
        path = self.runs / 'task.run.json'
        safety.atomic(path, dict(name='task', cli='codex', model='m', status='running', cwd='/work',
                                  run_id='task-1', pid=os.getpid(), started_at=time.time(), segment_id='seg'))
        self.assertEqual('running', json.loads(path.read_text())['status'])
        store.transition(self.runs, 'task-1', 'checking', 'acceptance')
        rows = json.loads(subprocess.run([str(ROOT / 'bin/agents-status'), '--json'], env=self.env,
                                          capture_output=True, text=True, timeout=20).stdout)
        task = next(row for row in rows if row['name'] == 'task')
        self.assertEqual('checking', task['status'])
        self.assertEqual('running', json.loads(path.read_text())['status'])

    def test_dead_needs_input_reconciles_a_delivered_answer_and_report(self):
        import agent_oversight as a
        import godfather_lifecycle as life
        base = self.runs / 'polish_final'
        run = dict(name=base.name, cli='codex', model='m', cwd=str(self.root), run_id='stale',
                   status='needs_input', pid=987654321, exit_code=0, started_at=time.time(),
                   questions=[dict(id='q', answer='continue', delivered=True)])
        Path(str(base) + '.run.json').write_text(json.dumps(run))
        Path(str(base) + '.last.md').write_text('Committed and verified.\n')
        self.assertEqual('done', life.logical_status(base.name))
        self.assertEqual('done', store.machine_status(self.runs, 'stale')['status'])
        self.assertEqual('done', a.read(str(base) + '.run.json')['status'])
        self.assertEqual(('done', False), life.wait_run(base.name, .01))
        # A real unanswered nonblocking question remains answerable.
        run.update(run_id='pending', status='needs_input', questions=[dict(id='new', answer=None)])
        safety.control(base, lambda data: data.update(questions=run['questions']))
        safety.atomic(str(base) + '.run.json', run)
        self.assertEqual('needs_input', a.reconcile_run(run)['status'])
        # A dead process without a successful exit is a crash, even with a report prefix.
        run.update(run_id='crashed', status='running', exit_code=None, questions=[])
        safety.control(base, lambda data: data.update(questions=run['questions']))
        safety.atomic(str(base) + '.run.json', run)
        self.assertEqual('failed', a.reconcile_run(run)['status'])

    def test_stop_kills_owned_tree_and_stays_terminal_after_late_finish(self):
        import agent_oversight as a
        import godfather_lifecycle as life
        child = subprocess.Popen([sys.executable, '-c', 'import subprocess,time; subprocess.Popen(["sleep","30"]); time.sleep(30)'], start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        time.sleep(.1)
        procs = a.processes()
        kids = [pid for pid, row in procs.items() if row['ppid'] == child.pid]
        base = self.runs / 'stopme'
        run = dict(name='stopme', cli='grok', model='m', status='needs_input', cwd=str(self.root),
                   run_id='stopme', pid=child.pid, pid_identity=procs[child.pid]['identity'],
                   started_at=time.time(), questions=[dict(id='q', answer=None)])
        safety.atomic(str(base) + '.run.json', run)
        self.assertEqual(0, life.stop_run('stopme'))
        child.wait(timeout=3)
        self.assertTrue(all(not a.alive(pid, procs[pid]['identity']) for pid in kids))
        # A delayed launcher trap must not resurrect a stopped run.
        safety.registry_write(str(base) + '.run.json', dict(run, status='running'))
        self.assertEqual('stopped', life.logical_status('stopme'))
        self.assertEqual('stopped', store.machine_status(self.runs, 'stopme')['status'])
        self.assertEqual(('stopped', False), life.wait_run('stopme', .01))
        import godfather_mcp as mcp
        import godfather_top as top
        self.assertEqual('stopped', mcp.report('stopme')['status'])
        row = dict(run, status='stopped', elapsed='0s', seconds_since_progress=0)
        self.assertFalse(top.row_text(row)[1])
        store.insert_run(self.runs, 'new-stopme', 'stopme', 'running')
        self.assertEqual('running', store.machine_status(self.runs, 'new-stopme')['status'])

    def test_fresh_import_reconciles_finished_input_and_preserves_open_questions(self):
        for name, questions, expected in [('answered', [{'id':'a','answer':'yes','delivered':True}], 'done'),
                                           ('pending', [{'id':'p','answer':None}], 'needs_input')]:
            base = self.runs / name
            Path(str(base) + '.run.json').write_text(json.dumps(dict(name=name, run_id=name, cli='codex',
                status='needs_input', pid=987654321, exit_code=0, started_at=time.time(), questions=questions)))
            Path(str(base) + '.last.md').write_text('Final reply.\n')
        conn = store.connect(self.runs)
        conn.close()
        self.assertEqual('done', store.machine_status(self.runs, 'answered')['status'])
        self.assertEqual('needs_input', store.machine_status(self.runs, 'pending')['status'])

    def test_watch_cursor_does_not_replay(self):
        base = self.runs / 'task'
        safety.event(base, 'DONE', 'old completion')
        first = self.watch()
        time.sleep(1)
        first.terminate()
        output, error = first.communicate(timeout=5)
        self.assertEqual(b'', output, error)
        safety.event(base, 'NEEDS_INPUT', ': Approve?')
        second = self.watch()
        time.sleep(1)
        second.terminate()
        output, error = second.communicate(timeout=5)
        self.assertIn(b'NEEDS_INPUT task: Approve?', output, error)
        self.assertNotIn(b'old completion', output)
        third = self.watch()
        time.sleep(1)
        third.terminate()
        output, error = third.communicate(timeout=5)
        self.assertNotIn(b'Approve?', output, error)
        replay = self.watch('--replay')
        time.sleep(1)
        replay.terminate()
        output, error = replay.communicate(timeout=5)
        self.assertIn(b'old completion', output)
        self.assertIn(b'Approve?', output)
        unknown = subprocess.run([str(ROOT / 'bin/agents-watch'), '--cursor', 'missing', '--poll-secs', '.05'],
                                 env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(2, unknown.returncode, unknown.stderr)
        self.assertIn('unknown cursor', unknown.stderr)

    def watch(self, *args):
        return subprocess.Popen([str(ROOT / 'bin/agents-watch'), '--no-heal', '--poll-secs', '.05'] + list(args),
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


if __name__ == '__main__':
    unittest.main()
