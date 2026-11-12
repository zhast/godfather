"""Portable launcher/install integration: all CLI invocations use fake binaries."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import agent_oversight as a
from installer_fixture import copy_installer

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
cli = Path(sys.argv[0]).name
args = sys.argv[1:]
if args == ['login', 'status']:
    print('Logged in'); sys.exit(0)
if args == ['-p', '/usage']:
    print('Gemini Models\tWeekly Limit Remaining\t100%')
    print('Claude and GPT models\tWeekly Limit Remaining\t100%')
    sys.exit(0)
with (Path.home() / (cli + '-args.json')).open('w') as f:
    json.dump(args, f)
if cli == 'codex':
    print('session id: 33333333-3333-3333-3333-333333333333')
    Path(args[args.index('-o') + 1]).write_text('OK\n')
    sys.stdin.read()
elif cli == 'grok':
    print(json.dumps(dict(type='text', data='OK')))
    if not os.getenv('FAKE_MISSING_END'):
        print(json.dumps(dict(type='end', usage=dict(input_tokens=3, output_tokens=1))))
elif cli == 'agy':
    Path(args[args.index('--log-file') + 1]).write_text(
        'Print mode: conversation=44444444-4444-4444-4444-444444444444\n')
    print('OK')
'''


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.installer = copy_installer(ROOT, self.root / 'installer-source')
        self.fake = self.root / 'fake'
        self.fake.mkdir()
        for cli in ('codex', 'agy', 'grok'):
            p = self.fake / cli
            p.write_text(FAKE)
            p.chmod(0o755)
        self.prompt = self.root / 'brief.md'
        self.prompt.write_text('Reply OK')
        # Whitelist environment; do not inherit credentials, user model choices or resume state.
        self.env = {key: os.environ[key] for key in ('PATH', 'TMPDIR', 'SYSTEMROOT') if key in os.environ}
        self.env.update(HOME=str(self.root), CWD=str(self.root), AGENT_RUNS_DIR=str(self.root / 'runs'),
                        CODEX_HOME=str(self.root / '.codex'), GODFATHER_MCP_SDK='0', PYTHONDONTWRITEBYTECODE='1',
                        PATH=str(self.fake) + os.pathsep + self.env.get('PATH', os.defpath))

    def tearDown(self):
        self.tmp.cleanup()

    def run_command(self, command, **env):
        return subprocess.run(command, env=dict(self.env, **env), capture_output=True,
                              text=True, timeout=20)

    def launch(self, cli, name, **env):
        result = self.run_command([str(ROOT / 'bin' / (cli + '-run')), str(self.prompt), name], **env)
        self.assertEqual(0, result.returncode, result.stderr)
        run = json.loads((self.root / 'runs' / (name + '.run.json')).read_text())
        self.assertEqual(('done', cli, 0), (run['status'], run['cli'], run['exit_code']))
        self.assertEqual(str(self.root), run['cwd'])
        self.assertTrue(run['pid'])
        self.assertEqual('OK', (self.root / 'runs' / (name + '.last.md')).read_text().strip())
        return run, json.loads((self.root / (cli + '-args.json')).read_text())

    def test_committed_final_report_finishes_idle_codex_and_grok(self):
        config = self.root / '.config/godfather/godfather.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[timeouts]\nfinal_idle_secs = 0.1\n')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        commit = ['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-qm', 'initial']
        subprocess.run(commit, check=True)
        for cli in ('codex', 'grok'):
            fake = self.fake / cli
            # Replace the CLI with a deterministic committed-report-then-idle worker.
            frame = dict(type='text', data='Committed, tests passed.') if cli == 'grok' else dict(type='item.completed', item=dict(type='agent_message', text='Committed, tests passed.'))
            fake.write_text('#!/usr/bin/env python3\nimport json,subprocess,time,sys\n' +
                            ('sys.stdin.read()\n' if cli == 'codex' else '') +
                            'subprocess.run(' + repr(commit[:-1] + ['finished']) + ', check=True)\n' +
                            'print(json.dumps(' + repr(frame) + '), flush=True)\n' +
                            'print(json.dumps(' + repr(dict(type='end' if cli == 'grok' else 'turn.completed')) + '), flush=True)\ntime.sleep(30)\n')
            result = self.run_command([str(ROOT / 'bin' / (cli + '-run')), str(self.prompt), 'idle-' + cli])
            self.assertEqual(0, result.returncode, result.stderr)
            run = json.loads((self.root / ('runs/idle-' + cli + '.run.json')).read_text())
            self.assertEqual(('done', 0, True), (run['status'], run['exit_code'], run['idle_finished']))
            self.assertEqual('Committed, tests passed.', (self.root / ('runs/idle-' + cli + '.last.md')).read_text().strip())

    def test_installer_fixture_never_executes_the_checkout_installer(self):
        self.assertNotEqual(ROOT / 'install.sh', self.installer)
        self.assertTrue(self.installer.is_relative_to(self.root))
        self.assertEqual((ROOT / 'install.sh').read_bytes(), self.installer.read_bytes())
        self.assertTrue((self.installer.parent / 'bin/godfather').is_file())

    def test_all_launchers_registry_status_and_help(self):
        for cli in ('codex', 'agy', 'grok'):
            self.launch(cli, cli)
        result = self.run_command([str(ROOT / 'bin/agents-status')])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('TOKENS IN/OUT', result.stdout)
        for cli in ('codex', 'agy', 'grok'):
            self.assertIn(cli, result.stdout)
        result = self.run_command([str(ROOT / 'bin/agents-status'), '--json'])
        self.assertEqual(0, result.returncode, result.stderr)
        registered = [r for r in json.loads(result.stdout) if r['status'] != 'unregistered']
        self.assertEqual(3, len(registered))
        for name in ('codex-run', 'agy-run', 'grok-run', 'agents-status', 'agents-watch', 'agents-nudge'):
            result = self.run_command([str(ROOT / 'bin' / name), '--help'])
            self.assertEqual(0, result.returncode, result.stderr)

    def test_atomic_reinstall_preserves_running_launcher_inode(self):
        import time
        source = self.root / 'source'
        shutil.copytree(ROOT / 'bin', source / 'bin')
        shutil.copyfile(ROOT / 'install.sh', source / 'install.sh')
        launcher = source / 'bin/codex-run'
        gate, release = self.root / 'gate', self.root / 'release'
        launcher.write_text('#!/bin/bash\nprintf ready > ' + str(gate) + '\nwhile [[ ! -e ' + str(release) + ' ]]; do /bin/sleep .02; done\n' + '# padding keeps the tail outside the shell read buffer\n' * 2000 + 'printf "OLD\\n"\n')
        prefix = self.root / 'atomic-bin'
        command = ['bash', str(source / 'install.sh')]
        result = self.run_command(command, PREFIX=str(prefix))
        self.assertEqual(0, result.returncode, result.stderr)
        old_inode = (prefix / 'codex-run').stat().st_ino
        child = subprocess.Popen([str(prefix / 'codex-run')], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not gate.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(gate.exists())
            launcher.write_text('#!/bin/bash\nprintf "NEW\\n"\n')
            result = self.run_command(command, PREFIX=str(prefix))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotEqual(old_inode, (prefix / 'codex-run').stat().st_ino)
            release.touch()
            out, err = child.communicate(timeout=10)
            self.assertEqual((0, b'OLD\n', b''), (child.returncode, out, err))
            self.assertEqual('NEW\n', self.run_command([str(prefix / 'codex-run')]).stdout)
        finally:
            if child.poll() is None:
                child.kill(); child.communicate()

    def test_codex_auto_default_override_and_resume(self):
        run, args = self.launch('codex', 'default')
        self.assertEqual('default', run['model'])
        self.assertNotIn('-m', args)
        cache = self.root / '.codex/models_cache.json'
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(dict(models=[dict(slug='gpt-6.1-sol')])))
        run, args = self.launch('codex', 'cached')
        self.assertEqual('gpt-6.1-sol', args[args.index('-m') + 1])
        run, args = self.launch('codex', 'custom', CODEX_MODEL='custom-model',
                                CODEX_EFFORT='medium', CODEX_SERVICE_TIER='flex', CODEX_BIN=str(self.fake / 'codex'))
        self.assertIn('model_reasoning_effort=medium', args)
        self.assertIn('service_tier=flex', args)
        self.assertEqual('custom-model', run['model'])
        result = self.run_command([str(ROOT / 'bin/agents-nudge'), '--wait', 'custom', 'Continue carefully'])
        self.assertEqual(0, result.returncode, result.stderr)
        args = json.loads((self.root / 'codex-args.json').read_text())
        self.assertIn('resume', args)
        self.assertIn('sandbox_mode="danger-full-access"', args)
        self.assertIn('approval_policy="never"', args)
        self.assertNotIn('-s', args)
        self.assertNotIn('--ignore-user-config', args)
        self.assertFalse(any(a.startswith(('permissions.', 'default_permissions=')) for a in args))
        self.assertIn(run['session_id'], args)
        self.assertIn('model_reasoning_effort=medium', args)
        self.assertIn('custom-model', args)

    def test_codex_sandbox_defaults_to_full_access_and_honors_override(self):
        run, args = self.launch('codex', 'sandbox-default')
        self.assertEqual('danger-full-access', args[args.index('-s') + 1])
        self.assertIn('approval_policy="never"', args)
        self.assertNotIn('--ignore-user-config', args)
        self.assertFalse(any(a.startswith(('permissions.', 'default_permissions=')) for a in args))
        self.assertNotIn('sandbox_workspace_write.network_access=true', args)
        self.assertIn('sandbox=danger-full-access', (self.root / 'runs/sandbox-default.models.log').read_text())
        run, args = self.launch('codex', 'sandbox-write', CODEX_SANDBOX='workspace-write')
        self.assertEqual('workspace-write', args[args.index('-s') + 1])
        self.assertIn('sandbox_workspace_write.network_access=true', args)
        run, args = self.launch('codex', 'sandbox-ro', CODEX_SANDBOX='read-only')
        self.assertEqual('read-only', args[args.index('-s') + 1])
        self.assertNotIn('sandbox_workspace_write.network_access=true', args)
        result = self.run_command([str(ROOT / 'bin/codex-run'), str(self.prompt), 'sandbox-bad'], CODEX_SANDBOX='off')
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn('CODEX_SANDBOX', result.stderr)
        self.assertFalse((self.root / 'runs/sandbox-bad.run.json').exists())

    def test_grok_configuration_and_invalid_stream(self):
        run, args = self.launch('grok', 'custom', GROK_MODEL='custom-grok', GROK_EFFORT='low')
        self.assertEqual('custom-grok', args[args.index('--model') + 1])
        self.assertEqual('low', args[args.index('--reasoning-effort') + 1])
        self.assertIn('Edit(**/.env*)', args)
        self.assertIn('Write(**/.env*)', args)
        self.assertFalse(any(a.startswith('Read(') for a in args))
        result = self.run_command([str(ROOT / 'bin/grok-run'), str(self.prompt), 'invalid'], FAKE_MISSING_END='1')
        self.assertNotEqual(0, result.returncode)
        run = json.loads((self.root / 'runs/invalid.run.json').read_text())
        self.assertEqual('failed', run['status'])
        records = [json.loads(line) for line in (self.root / '.local/state/godfather/ledger.jsonl').read_text().splitlines()]
        self.assertNotEqual(0, records[-1]['exit_status'])

    def test_agy_model_order_override(self):
        run, args = self.launch('agy', 'ordered', AGY_MODELS='claude-custom gemini-custom')
        self.assertEqual('claude-custom', run['model'])
        self.assertEqual('claude-custom', args[args.index('--model') + 1])

    def test_binary_discovery_order_and_missing_override(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(str(self.fake / 'codex'), a.find_codex())
            with mock.patch('agent_oversight.shutil.which', return_value=None), mock.patch(
                    'agent_oversight.os.access', return_value=True):
                self.assertEqual(a.CODEX_APP, a.find_codex())
            with mock.patch.dict(os.environ, CODEX_BIN='missing-codex-binary'):
                with self.assertRaises(FileNotFoundError):
                    a.find_codex()
        result = self.run_command([str(ROOT / 'bin/codex-run'), str(self.prompt), 'missing'], CODEX_BIN='/nonexistent/codex')
        self.assertEqual(127, result.returncode)

    def test_missing_other_clis_and_empty_status_watch(self):
        # A restricted PATH with utilities only prevents accidentally running installed CLIs.
        utilities = self.root / 'utilities'
        utilities.mkdir()
        for name in ('python3', 'dirname', 'mkdir', 'awk', 'ps'):
            target = shutil.which(name)
            self.assertIsNotNone(target)
            (utilities / name).symlink_to(target)
        for cli in ('agy', 'grok'):
            result = self.run_command([str(ROOT / 'bin' / (cli + '-run')), str(self.prompt), 'missing-' + cli], PATH=str(utilities))
            self.assertEqual(127, result.returncode, result.stderr)
        result = self.run_command([str(ROOT / 'bin/agents-status'), '--json'], PATH=str(utilities))
        self.assertEqual(0, result.returncode, result.stderr)
        watch = subprocess.Popen([str(ROOT / 'bin/agents-watch'), '--poll-secs', '.05'],
                                 env=dict(self.env, PATH=str(utilities)), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                watch.wait(timeout=.3)
        finally:
            watch.terminate()
            _, stderr = watch.communicate(timeout=3)
        self.assertEqual(b'', stderr)

    def test_install_idempotent_help_uninstall_preserves_modified_files(self):
        prefix = self.root / 'prefix'
        for _ in range(2):
            result = self.run_command([str(self.installer)], PREFIX=str(prefix))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('godfather doctor', result.stdout)
            self.assertNotIn('login: logged in', result.stdout)
            self.assertNotIn('agy -p /usage', result.stdout)
        packaged = {path.name for path in (ROOT / 'bin').iterdir() if path.is_file()}
        installed = {path.name for path in prefix.iterdir() if path.name != '.godfather-install.json'}
        self.assertEqual(packaged, installed)
        manifest = json.loads((prefix / '.godfather-install.json').read_text())
        self.assertEqual(packaged, set(manifest) - {'_source_bin'})
        for name in ('cursor-run', 'godfather_cursor.py', 'godfather_cursor_result.py', 'godfather_codex_bridge.py'):
            self.assertEqual(hashlib.sha256((prefix / name).read_bytes()).hexdigest(), manifest[name])
        self.assertIn('godfather_permissions.py', installed)
        names = ('godfather', 'codex-run', 'agy-run', 'grok-run', 'cursor-run', 'agents-status', 'agents-watch', 'agents-nudge')
        for name in names:
            result = self.run_command([str(prefix / name), '--help'])
            self.assertEqual(0, result.returncode, result.stderr)
        result = self.run_command([str(self.installer), '--uninstall'], PREFIX=str(prefix))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], list(prefix.iterdir()))
        self.run_command([str(self.installer)], PREFIX=str(prefix))
        edited = prefix / 'codex-run'
        edited.chmod(edited.stat().st_mode | 0o200)
        edited.write_text('local edit\n')
        result = self.run_command([str(self.installer)], PREFIX=str(prefix))
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Refusing to overwrite', result.stderr)
        result = self.run_command([str(self.installer), '--uninstall'], PREFIX=str(prefix))
        self.assertEqual(0, result.returncode)
        self.assertEqual('local edit\n', (prefix / 'codex-run').read_text())

    def test_installed_files_are_readonly_and_reinstall_updates_them(self):
        prefix = self.root / 'readonly-prefix'
        result = self.run_command([str(self.installer)], PREFIX=str(prefix))
        self.assertEqual(0, result.returncode, result.stderr)
        names = ('codex-run', 'agy-run', 'grok-run', 'agents-status', 'agents-watch',
                 'agents-nudge', 'agent_oversight.py', 'godfather', 'godfather_router.py',
                 'godfather_safety.py')
        for name in names:
            path = prefix / name
            mode = path.stat().st_mode & 0o777
            if name.endswith('.py'):
                self.assertEqual(0o444, mode, name)
            else:
                self.assertEqual(0o555, mode, name)
        target = prefix / 'godfather'
        with self.assertRaises(PermissionError):
            target.write_text('should not land\n')
        target.chmod(target.stat().st_mode | 0o200)
        target.write_text(target.read_text() + '\n# older-install\n')
        target.chmod(target.stat().st_mode & ~0o222)
        manifest_path = prefix / '.godfather-install.json'
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(str(self.installer.parent / 'bin'), manifest['_source_bin'])
        manifest['godfather'] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        result = self.run_command([str(self.installer)], PREFIX=str(prefix))
        self.assertEqual(0, result.returncode, result.stderr)
        text = target.read_text()
        self.assertNotIn('older-install', text)
        self.assertNotIn('should not land', text)
        self.assertEqual(0o555, target.stat().st_mode & 0o777)
        self.assertEqual((ROOT / 'bin/godfather').read_text(), text)

    def test_doctor_reads_files_unless_live(self):
        home = self.root / 'doctor-home'
        (home / '.codex').mkdir(parents=True)
        (home / '.codex/auth.json').write_text('{"ok":true}\n')
        (home / '.grok').mkdir()
        (home / '.grok/auth.json').write_text('{"ok":true}\n')
        (home / '.gemini').mkdir()
        (home / '.gemini/oauth_creds.json').write_text('{"ok":true}\n')
        env = dict(self.env, HOME=str(home), XDG_STATE_HOME=str(self.root / 'state'),
                   XDG_CONFIG_HOME=str(self.root / 'config'), AGENT_RUNS_DIR=str(self.root / 'runs'))
        for key in ('CODEX_BIN', 'CODEX_HOME', 'GEMINI_API_KEY', 'XAI_API_KEY'):
            env.pop(key, None)
        env['CURSOR_AGENT_BIN'] = str(self.root / 'missing-cursor-agent')
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch('agent_oversight.subprocess.run', side_effect=AssertionError('doctor launched a CLI')):
                from io import StringIO
                buf = StringIO()
                with mock.patch('sys.stdout', buf):
                    a.doctor()
            text = buf.getvalue()
        self.assertIn('CLI auth (passive; no CLI launched):', text)
        for cli in ('codex', 'agy', 'grok'):
            self.assertIn(cli + ': installed; login: saved login present (not remotely verified)', text)
        passive = self.run_command([str(ROOT / 'bin/godfather'), 'doctor', '--since', '1h'], **{k: env[k] for k in env if k != 'PATH'})
        self.assertEqual(0, passive.returncode, passive.stderr)
        self.assertIn('CLI auth (passive; no CLI launched):', passive.stdout)
        self.assertFalse((self.root / 'agy-args.json').exists())
        self.assertFalse((self.root / 'codex-args.json').exists())
        self.assertFalse((self.root / 'grok-args.json').exists())
        live = self.run_command([sys.executable, str(ROOT / 'bin/agent_oversight.py'), 'doctor', '--live'], HOME=str(home), CURSOR_AGENT_BIN=env['CURSOR_AGENT_BIN'])
        self.assertEqual(0, live.returncode, live.stderr)
        self.assertIn('CLI auth (live probes):', live.stdout)
        self.assertIn('codex: installed; login: logged in', live.stdout)
        self.assertIn('agy: installed; login: logged in', live.stdout)
        self.assertIn('grok: installed; login: saved login present (not remotely verified)', live.stdout)
        self.assertFalse((self.root / 'grok-args.json').exists())
