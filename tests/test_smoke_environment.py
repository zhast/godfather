"""The suite wrapper cannot inherit paths or callbacks from a live conductor."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SmokeEnvironmentTests(unittest.TestCase):
    def test_smoke_devbox_probe_is_fake_and_mutations_trip_the_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'python3'
            output = root / 'probe.json'
            fake.write_text('#!' + sys.executable + '\nimport json,os,subprocess\nfrom pathlib import Path\n'
                            'probe=subprocess.run(["devbox","status","--json"],capture_output=True,text=True)\n'
                            'Path(os.environ["SMOKE_CAPTURE"]).write_text(json.dumps([probe.returncode,probe.stderr]))\n'
                            'if os.getenv("TRY_STOP"): subprocess.run(["devbox","stop","fake-box"])\n')
            fake.chmod(0o700)
            for stop in ('', '1'):
                env = dict(os.environ, PATH=str(root) + os.pathsep + os.defpath,
                           SMOKE_CAPTURE=str(output), TRY_STOP=stop)
                env.pop('BASH_ENV', None)
                env.pop('ZDOTDIR', None)
                result = subprocess.run(['bash', str(ROOT / 'tests/smoke.sh')], env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(1 if stop else 0, result.returncode, result.stderr)
                self.assertEqual([1, 'devbox must be run inside a project\n'], json.loads(output.read_text()))
                if stop:
                    self.assertIn('devbox stop fake-box', result.stderr)

    def test_smoke_drops_live_conductor_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'environment.json'
            fake = root / 'python3'
            fake.write_text('#!' + sys.executable + '\nimport json,os\nfrom pathlib import Path\n'
                            'Path(os.environ["SMOKE_CAPTURE"]).write_text(json.dumps({k:v for k,v in os.environ.items() '
                            'if k.startswith(("AGENT_","GODFATHER_","CURSOR_")) or k in ("XDG_CONFIG_HOME","XDG_STATE_HOME","BASH_ENV","ZDOTDIR","CODEX_SANDBOX","CODEX_HOME","GROK_HOME")}))\n')
            fake.chmod(0o700)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.defpath,
                       AGENT_RUNS_DIR=str(root / 'live-runs'), AGENT_ROUTER_NAME='live',
                       GODFATHER_BASE=str(root / 'live-runs/live'), GODFATHER_CONTEXT=str(root / 'context'),
                       GODFATHER_REVIEW_PARENT=str(root / 'live-runs/parent'),
                       GODFATHER_ORIGINAL_PATH='/nonexistent', CODEX_SANDBOX='seatbelt',
                       CODEX_HOME=str(root / 'live-codex'), GROK_HOME=str(root / 'live-grok'),
                       CURSOR_FAST='false', CURSOR_ALLOW_NO_ZDR='true', CURSOR_MODEL='live-model', CURSOR_EFFORT='high', CURSOR_MODE='ask',
                       SMOKE_CAPTURE=str(output))
            startup = root / 'live.shims/bash-env'
            startup.parent.mkdir()
            startup.write_text('export GODFATHER_BASE=live\n')
            env.update(BASH_ENV=str(startup), ZDOTDIR=str(startup.parent))
            result = subprocess.run(['bash', str(ROOT / 'tests/smoke.sh')], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stderr)
            captured = json.loads(output.read_text())
            self.assertEqual({'AGENT_RUNS_DIR', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME'}, set(captured))
            self.assertNotEqual(env['AGENT_RUNS_DIR'], captured['AGENT_RUNS_DIR'])
            self.assertEqual(1, len({str(Path(value).parent) for value in captured.values()}))
