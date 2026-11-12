"""Incident-replay regression tests. Each fixture is a recorded failure."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_engine
from godfather_policy import document

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
REQUIRED = (
    'argon-oversized-write', 'atomic-install', 'auth-blip', 'chmod-grok-metadata',
    'codex-sandbox-ps-git', 'committed-idle', 'deploy-help', 'diagnostic-noise', 'final-report-cap', 'git-restore-staged', 'grok-think-16m',
    'hermetic-suite', 'inflight-sleep', 'learned-rule-cli-tree', 'missing-stream', 'orphan-watcher', 'partial-segment',
    'pending-question', 'stale-needs-input', 'stream-devices', 'verdict-allow-reason', 'watch-cursor',
)


def _same(got, want):
    if isinstance(want, bool):
        return bool(got) is want
    if isinstance(want, (int, float)) and not isinstance(want, bool) and isinstance(got, (int, float)) and not isinstance(got, bool):
        return float(got) == float(want)
    return got == want


class ReplayTests(unittest.TestCase):
    def test_every_recorded_incident_has_a_fixture(self):
        found = tuple(sorted(path.name for path in FIXTURES.iterdir() if (path / 'fixture.toml').exists()))
        self.assertEqual(REQUIRED, found)

    def test_fixtures_replay_without_a_model_or_the_live_registry(self):
        for name in REQUIRED:
            with self.subTest(name):
                directory = FIXTURES / name
                root = Path(tempfile.mkdtemp(prefix='replay-'))
                self.addCleanup(lambda path=root: __import__('shutil').rmtree(path, True))
                runs = root / 'runs'
                runs.mkdir()
                env = dict(os.environ)
                env.update(HOME=str(root), XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'),
                           AGENT_RUNS_DIR=str(runs), PYTHONDONTWRITEBYTECODE='1', GODFATHER_MCP_SDK='0')
                for key in ('GODFATHER_DB', 'GODFATHER_SHIMS', 'GODFATHER_NATIVE', 'CODEX_HOME', 'CODEX_SANDBOX', 'STALL_SECS', 'HARD_STALL_SECS'):
                    env.pop(key, None)
                expect = document((directory / 'expect.toml').read_text())
                with mock.patch.dict(os.environ, env, clear=True):
                    result = replay_engine.replay(directory, runs)
                for key, want in expect.items():
                    self.assertIn(key, result, key)
                    self.assertTrue(_same(result[key], want), '%s: %r != %r' % (key, result[key], want))
