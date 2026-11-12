"""Resume argv contracts captured from the installed CLIs' --help output."""
import json
import os
import subprocess
import unittest
from unittest import mock

import test_router as fixture
import godfather_permissions as permissions

SESSION = '33333333-3333-3333-3333-333333333333'
# Flag arities, not permissive fakes: exec resume rejects exec-only options
# even when they precede the resume subcommand.
ACCEPTED = {
    'codex': {'--json': 0, '-m': 1, '-c': 1, '--disable': 1,
              '--skip-git-repo-check': 0, '-o': 1},
    'agy': {'--model': 1, '--mode': 1, '--settings': 1, '--add-dir': 1, '--conversation': 1,
            '--effort': 1, '--log-file': 1, '-p': 1, '--dangerously-skip-permissions': 0},
    'grok': {'--cwd': 1, '--model': 1, '--reasoning-effort': 1,
             '--always-approve': 0, '--no-plan': 0, '--sandbox': 1,
             '--output-format': 1, '--prompt-file': 1, '--resume': 1,
             '--deny': 1, '--allow': 1},
}


class ResumeArgvTests(unittest.TestCase):
    setUp = fixture.RouterTests.setUp
    calls = fixture.RouterTests.calls

    def check_argv(self, cli, args):
        if cli == 'codex':
            self.assertEqual(['exec', 'resume', SESSION], args[:3])
            self.assertEqual(1, args.count('-'))
            args = [a for a in args[3:] if a != '-']
        index = 0
        while index < len(args):
            flag = args[index]
            self.assertIn(flag, ACCEPTED[cli], (cli, args))
            index += 1 + ACCEPTED[cli][flag]
        self.assertEqual(len(args), index)

    def test_each_launcher_builds_only_supported_resume_flags(self):
        extra = self.root / 'additional workspace'
        extra.mkdir()
        for cli in ACCEPTED:
            for native in ('', '1'):
                with self.subTest(cli=cli, native=native):
                    name = cli + ('-native' if native else '-direct')
                    env = dict(self.env, AGENT_RESUME_ID=SESSION, GODFATHER_NATIVE=native,
                               CODEX_MODEL='fake', AGY_MODELS='claude-fake', GROK_MODEL='fake',
                               ADD_DIRS=str(extra) if cli != 'grok' else '')
                    result = subprocess.run([str(fixture.ROOT / 'bin' / (cli + '-run')),
                                             str(self.prompt), name], env=env,
                                            text=True, capture_output=True, timeout=15)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    call = self.calls()[-1]
                    self.check_argv(cli, call['args'])
                    if cli == 'codex':
                        self.assertIn('sandbox_mode="danger-full-access"', call['args'])
                        self.assertIn('approval_policy="never"', call['args'])
                        roots = [json.loads(a.split('=', 1)[1]) for a in call['args']
                                 if a.startswith('sandbox_workspace_write.writable_roots=')]
                        self.assertTrue(roots)
                        self.assertIn(str(extra), roots[-1])
                        self.assertIn(str(self.root / 'runs'), roots[-1])
                        self.assertEqual(str(extra), fixture.a.read(
                            self.root / ('runs/' + name + '.run.json'))['launcher_env']['ADD_DIRS'])

    def test_resume_empty_cwd_array_is_safe_with_bash32_nounset(self):
        result = subprocess.run([str(fixture.ROOT / 'bin/codex-run'), str(self.prompt), 'forced'],
                                env=dict(self.env, AGENT_RESUME_ID=SESSION, CODEX_MODEL='fake',
                                         CODEX_SANDBOX='workspace-write', GODFATHER_NATIVE='1'),
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        args = self.calls()[-1]['args']
        self.check_argv('codex', args)
        self.assertIn('sandbox_mode="workspace-write"', args)
        self.assertIn('sandbox_workspace_write.network_access=true', args)

    def test_workspace_resume_carries_git_tool_and_scratch_roots_without_profile(self):
        runs = self.root / 'runs'
        runs.mkdir()
        extra = self.root / 'additional workspace'
        extra.mkdir()
        with mock.patch.dict(os.environ, dict(self.env, GODFATHER_NATIVE='1',
                             CODEX_SANDBOX='workspace-write', ADD_DIRS=str(extra)), clear=True):
            run = dict(cli='codex', cwd=str(self.root), base=str(runs / 'resume'))
            profile = permissions.compile_profile(run)
            argv = permissions.apply_native(['codex', 'exec', 'resume', SESSION, '-'], run, profile)
        roots = json.loads(next(a.split('=', 1)[1] for a in argv
                                if a.startswith('sandbox_workspace_write.writable_roots=')))
        for path in (str(runs), str(extra), str(self.root)):
            self.assertIn(path, roots)
        self.assertIn('sandbox_mode="workspace-write"', argv)
        self.assertIn('sandbox_workspace_write.network_access=true', argv)
        self.assertFalse(any(a.startswith(('permissions.', 'default_permissions=')) for a in argv))
        self.assertNotIn('--add-dir', argv)
