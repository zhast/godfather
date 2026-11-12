"""Transient provider errors must not become tool denials or quota cooldowns."""
import json
import os
import unittest
from unittest import mock

import test_router as fixture
import godfather_supervisor as supervisor
import godfather_stream as stream


class CapacityTests(unittest.TestCase):
    setUp = fixture.RouterTests.setUp
    config_models = fixture.RouterTests.config_models
    launch = fixture.RouterTests.launch
    calls = fixture.RouterTests.calls

    def configure(self, attempts=2):
        self.config_models(('codex', 'fake'), ('grok', 'backup'))
        (self.config.parent / 'godfather.toml').write_text(
            '[[retry]]\nclass="capacity"\nattempts=%d\nbackoff_secs=0.01\naction="resume"\n' % attempts)
        fake = fixture.FAKE.replace("quota = model in", '''
if cli == 'codex':
    counter = Path.home() / 'capacity-count'
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    if count <= int(os.getenv('CAPACITY_FAILURES', '0')):
        # An earlier permission denial must not mask a terminal capacity error.
        print(json.dumps(dict(type='item.completed', item=dict(type='command_execution',
            aggregated_output='tool permission denied in headless mode'))))
        print(json.dumps(dict(type='turn.failed', error=dict(message=os.environ.get(
            'CAPACITY_MESSAGE', 'Selected model is at capacity. Please try a different model.')))))
        sys.exit(1)
quota = model in''')
        (self.root / 'fake/codex').write_text(fake)

    def test_capacity_recovers_on_same_model_without_fallback_or_cooldown(self):
        self.configure()
        result = self.launch('recover', CAPACITY_FAILURES='2')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['codex'] * 3, [c['cli'] for c in self.calls()])
        self.assertEqual('done', fixture.a.read(self.root / 'runs/recover.run.json')['status'])
        self.assertNotIn('fake', fixture.a.read(self.root / 'state/godfather/quota.json'))
        log = (self.root / 'runs/recover.models.log').read_text()
        self.assertEqual(2, log.count('RETRY capacity'))
        self.assertNotIn('FALLBACK', log)
        self.assertIn('Original task', self.calls()[1]['prompt'])
        self.assertIn('supervisor, not the user', self.calls()[1]['prompt'])
        self.assertIn('Reason class: capacity', self.calls()[1]['prompt'])
        self.assertIn("don't redo completed steps", self.calls()[1]['prompt'])

    def test_overload_exhausts_bounded_retries_then_falls_back_as_capacity(self):
        self.configure()
        result = self.launch('exhaust', CAPACITY_FAILURES='99', CAPACITY_MESSAGE='Server overloaded')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['codex'] * 3 + ['grok'], [c['cli'] for c in self.calls()])
        run = fixture.a.read(self.root / 'runs/exhaust.run.json')
        self.assertEqual(('failed', 'capacity'), (run['status'], run['failure_class']))
        self.assertIn('FALLBACK capacity', (self.root / 'runs/exhaust.models.log').read_text())
        self.assertEqual({}, fixture.a.read(self.root / 'state/godfather/quota.json'))

    def test_capacity_prose_and_tool_output_are_not_provider_failures(self):
        self.configure()
        base = self.root / 'unrelated'
        base.with_suffix('.log').write_text(json.dumps(dict(type='item.completed', item=dict(
            type='command_execution', aggregated_output='model at capacity; headless tool denied'))))
        base.with_suffix('.last.md').write_text('A report discussing an overloaded model.')
        self.assertEqual((None, None), supervisor.segment_outcome(base, 1, dict(cli='codex')))
        self.assertEqual('capacity', stream._failure('Selected model is at capacity.'))
        self.assertEqual('capacity', stream._failure('overloaded_error'))

    def test_overload_with_429_does_not_poison_same_model_with_quota_cooldown(self):
        self.configure()
        result = self.launch('overload-429', CAPACITY_FAILURES='99',
                             CAPACITY_MESSAGE='Server overloaded (HTTP 429, RESOURCE_EXHAUSTED)')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['codex'] * 3 + ['grok'], [c['cli'] for c in self.calls()])
        self.assertEqual('failed', fixture.a.read(self.root / 'runs/overload-429.run.json')['status'])
        self.assertEqual({}, fixture.a.read(self.root / 'state/godfather/quota.json'))
        log = (self.root / 'runs/overload-429.models.log').read_text()
        self.assertIn('FALLBACK capacity', log)
        self.assertNotIn('COOLDOWN', log)

    def test_default_budget_backs_off_twice_and_explicit_stop_is_respected(self):
        rules = supervisor.load_policies('')
        row = next(r for r in rules['retry'] if r['class_'] == 'capacity')
        self.assertEqual(2, row['attempts'])
        self.assertGreater(row['backoff_secs'], 0)
        self.configure(attempts=0)
        (self.config.parent / 'godfather.toml').write_text(
            '[[retry]]\nclass="capacity"\nattempts=0\nbackoff_secs=0\naction="escalate"\n[fallback]\non=[]\n')
        result = self.launch('stop', CAPACITY_FAILURES='99')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(1, len(self.calls()))
