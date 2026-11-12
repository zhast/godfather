"""Fixture teardown must own durable test work, including a pending queue."""
import os
import json
import subprocess
import time
import unittest
from unittest import mock

import test_router as fixture
import godfather_lifecycle as lifecycle


class CleanupTests(unittest.TestCase):
    setUp = fixture.RouterTests.setUp
    config_models = fixture.RouterTests.config_models

    def test_cleanup_never_signals_caller_ancestors_in_other_process_groups(self):
        snapshot = fixture.a.processes()
        parent = os.getppid()
        snapshot[parent] = dict(snapshot[parent], pgid=os.getpgrp() + 1)
        runs = self.root / 'runs'
        runs.mkdir()
        (runs / 'ancestor.run.json').write_text(json.dumps(dict(
            name='ancestor', status='running', launcher_pid=parent,
            launcher_identity=snapshot[parent]['identity'])))
        with mock.patch.object(fixture.a, 'processes', return_value=snapshot), \
                mock.patch.object(lifecycle, 'stop_run') as stop:
            fixture.RouterTests.cleanup_fixture(self)
        stop.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_cleanup_stops_own_detached_writer_and_pending_queue_before_removing_directory(self):
        self.config_models(('codex', 'fake'))
        (self.root / 'fake/codex').write_text(fixture.FAKE.replace(
            "quota = model in", "import time; time.sleep(30)\nquota = model in"))
        command = [str(fixture.ROOT / 'bin/godfather')]
        client = subprocess.Popen(command + ['run', '--no-isolate', str(self.prompt), 'cleanup-owned'],
                                  env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            record = {}
            while time.monotonic() < deadline:
                record = fixture.a.read(self.root / 'runs/cleanup-owned.run.json')
                if record.get('pid'):
                    break
                time.sleep(.03)
            self.assertTrue(record.get('pid'))
            queued = subprocess.run(command + ['run', '--after', 'not-registered', '-p', 'later', 'pending'],
                                    env=self.env, capture_output=True, timeout=10)
            self.assertEqual(0, queued.returncode, queued.stderr)
            fixture.RouterTests.cleanup_fixture(self)
            self.assertFalse(fixture.a.alive(record['pid'], record['pid_identity']))
            self.assertFalse(self.root.exists())
            client.wait(timeout=5)
        finally:
            if client.poll() is None:
                with mock.patch.dict(os.environ, self.env, clear=True):
                    if self.root.exists():
                        lifecycle.stop_run('cleanup-owned')
                client.wait(timeout=5)
