"""Queued generations bind and release chains without invoking model CLIs."""
import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import godfather_lifecycle as life


class QueuedChainTests(unittest.TestCase):
    def setUp(self):
        self.records = {}
        patches = [mock.patch.object(life.a, 'runs_dir', return_value=Path('/runs')),
                   mock.patch.object(life, 'queue_path', side_effect=lambda name: Path('/queue') / name),
                   mock.patch.object(life.s, 'resolve_name', side_effect=lambda name: Path('/runs') / name),
                   mock.patch.object(life.s, 'read', side_effect=lambda path: copy.deepcopy(
                       self.records.get(str(path), {}))),
                   mock.patch.object(life.s, 'atomic', side_effect=lambda path, data: self.records.update(
                       {str(path): copy.deepcopy(data)}))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def chain(self, any_status):
        jobs = {}
        for index, (name, parent) in enumerate((('a', 'future'), ('b', 'a'), ('c', 'b'))):
            job = dict(name=name, run_id=name + '-generation', status='queued',
                       created=100 + index, dependency_timeout=300, dependencies=[parent],
                       dependency_instances={parent: None}, any_status=any_status)
            self.records['/queue/' + name] = copy.deepcopy(job)
            jobs[name] = job
        return jobs

    def test_queued_chain_recovers_legacy_null_bindings_and_finishes(self):
        for any_status in (False, True):
            with self.subTest(any_status=any_status):
                self.records.clear()
                jobs = self.chain(any_status)
                # A can remain queued for hours: B and C must bind immediately
                # instead of timing out while awaiting a run.json publication.
                for name, parent in (('b', 'a'), ('c', 'b')):
                    self.assertEqual('waiting', life.dependency_gate(jobs[name], 1000)[0])
                    self.assertEqual(jobs[parent]['run_id'],
                                     self.records['/queue/' + name]['dependency_instances'][parent])
                for name, child in (('a', 'b'), ('b', 'c')):
                    self.records['/queue/' + name]['status'] = 'running'
                    self.records['/runs/' + name + '.route.json'] = dict(
                        run_id=jobs[name]['run_id'], started_at=200, status='done', result='done')
                    self.assertEqual('ready', life.dependency_gate(jobs[child], 1001)[0])

    def test_missing_binding_accepts_every_active_generation(self):
        for status in life.ACTIVE:
            with self.subTest(status=status):
                job = self.chain(False)['b']
                with mock.patch.object(life, 'logical_status', return_value=status):
                    self.assertEqual('waiting', life.dependency_gate(job, 1000)[0])
                self.assertEqual('a-generation', job['dependency_instances']['a'])

    def test_stale_completed_parent_still_requires_fresh_registration(self):
        job = self.chain(False)['b']
        self.records['/queue/a']['status'] = 'done'
        self.records['/runs/a.route.json'] = dict(run_id='a-generation', started_at=90,
                                                 status='done', result='done')
        self.assertEqual('waiting', life.dependency_gate(job, 200)[0])
        gate, detail = life.dependency_gate(job, 1000)
        self.assertEqual('dependency_failed', gate)
        self.assertIn('registration timed out', detail)
        self.assertIsNone(job['dependency_instances']['a'])

    def test_restarted_router_binds_before_replacing_completed_run(self):
        for any_status in (False, True):
            with self.subTest(any_status=any_status):
                self.records.clear()
                job = dict(name='child', created=100, dependencies=['parent'],
                           dependency_instances={'parent': None}, any_status=any_status)
                old = dict(name='parent', run_id='old-segment', router_id='old-router',
                           started_at=90, ended_at=95, status='done')
                self.records['/runs/parent.run.json'] = old
                route = dict(run_id='new-router', started_at=110, status='running',
                             router_pid=123)
                self.records['/runs/parent.route.json'] = route
                with mock.patch.object(life.a, 'alive', return_value=True), \
                        mock.patch.object(life.a, 'decision_state', side_effect=lambda run, root: run):
                    # The next invocation restarts the daemon before the new
                    # router has replaced the previous generation's run.json.
                    self.assertEqual('waiting', life.dependency_gate(job, 111)[0])
                    self.assertEqual('new-router', job['dependency_instances']['parent'])
                    job = copy.deepcopy(self.records['/queue/child'])
                    self.records['/runs/parent.run.json'] = dict(
                        name='parent', run_id='new-segment', router_id='new-router',
                        started_at=112, status='running')
                    self.assertEqual('waiting', life.dependency_gate(job, 113)[0])
                    self.records['/runs/parent.run.json'].update(status='done', ended_at=114)
                    route.update(status='finished', result='done')
                    self.assertEqual('ready', life.dependency_gate(job, 115)[0])

    def test_newer_standalone_run_supersedes_old_router(self):
        self.records['/runs/parent.route.json'] = dict(run_id='old-router', started_at=90)
        self.records['/runs/parent.run.json'] = dict(run_id='new-run', started_at=110)
        self.assertEqual(('new-run', 110), life.dependency_instance('parent'))

    def test_bound_chain_refuses_a_replaced_parent_generation(self):
        job = self.chain(False)['c']
        self.assertEqual('waiting', life.dependency_gate(job, 1000)[0])
        self.records['/queue/b']['run_id'] = 'replacement'
        gate, detail = life.dependency_gate(job, 1001)
        self.assertEqual('dependency_failed', gate)
        self.assertIn('replaced', detail)
