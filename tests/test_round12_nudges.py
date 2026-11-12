"""Queued corrections reopen natural completions; stops/replacements expire."""
import json
import os
from pathlib import Path
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


class QueuedNudgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        env = dict(os.environ, HOME=str(self.root), AGENT_RUNS_DIR=str(self.runs),
                   XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                   GODFATHER_QUEUE_DAEMON='1', PYTHONDONTWRITEBYTECODE='1')
        env.pop('GODFATHER_DB', None)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def queued(self, name, status, **request):
        base = self.runs / name
        safety.atomic(str(base) + '.run.json', dict(name=name, run_id=name, cli='codex', model='fake',
                      cwd=str(self.root), status=status, started_at=a.now(), exit_code=0, progress_paths=[],
                      session_id='44444444-4444-4444-4444-444444444444'))
        Path(str(base) + '.last.md').write_text('DONE: committed abc123.\n')
        safety.atomic(str(base) + '.nudge.json', dict(dict(id='n-' + name, run_id=name, pending=True,
                      message='Also merge the stacked branch.', requested=time.time() - 60), **request))
        return base

    def test_mid_run_nudge_reopens_natural_terminal_outcomes(self):
        for status in ('done', 'failed', 'check_failed', 'review_failed', 'timed_out', 'cancelled', 'dependency_failed'):
            with self.subTest(status=status):
                base = self.queued('mid-' + status, status, reopen=False, queued_status='running')
                with mock.patch.object(a, 'nudge_main', return_value=0) as resume:
                    self.assertEqual(0, life.execute_nudge(base))
                resume.assert_called_once_with([base.name, 'Also merge the stacked branch.'])
                request = safety.read(str(base) + '.nudge.json')
                self.assertFalse(request['pending'])
                self.assertNotIn('expired', request)

    def test_mid_run_nudge_expires_on_user_stop(self):
        for status in ('stopped', 'running'):
            with self.subTest(status=status):
                base = self.queued('mid-' + status, status, reopen=False, queued_status='running')
                if status == 'running':
                    run = safety.read(str(base) + '.run.json')
                    safety.atomic(str(base) + '.run.json', dict(run, stop_requested=True))
                with mock.patch.object(a, 'nudge_main', side_effect=AssertionError('stale nudge resumed')):
                    self.assertEqual(0, life.execute_nudge(base))
                request = safety.read(str(base) + '.nudge.json')
                self.assertEqual((False, True, 0), (request['pending'], request['expired'], request['exit_code']))
                self.assertIn('run ended ' + status, request['reason'])
                run = safety.read(str(base) + '.run.json')
                self.assertEqual(status, run['status'])
                self.assertEqual('DONE: committed abc123.\n', Path(str(base) + '.last.md').read_text())
                log = Path(str(base) + '.models.log').read_text()
                self.assertIn('NUDGE_EXPIRED status=' + status, log)
                self.assertIn(json.dumps('Also merge the stacked branch.'), log)
                events = [json.loads(line) for line in Path(str(base) + '.events.jsonl').read_text().splitlines()]
                self.assertEqual('NUDGE', events[-1]['kind'])
                self.assertTrue(events[-1]['detail'].startswith('expired: run ended ' + status))

    def test_legacy_request_without_reopen_flag_is_delivered(self):
        base = self.queued('legacy', 'done')
        with mock.patch.object(a, 'nudge_main', return_value=0) as resume:
            self.assertEqual(0, life.execute_nudge(base))
        resume.assert_called_once_with([base.name, 'Also merge the stacked branch.'])
        self.assertNotIn('expired', safety.read(str(base) + '.nudge.json'))

    def test_replaced_run_expires_without_touching_replacement(self):
        base = self.queued('replaced', 'running', run_id='old')
        before = safety.read(str(base) + '.run.json')
        with mock.patch.object(a, 'nudge_main', side_effect=AssertionError('replacement resumed')):
            self.assertEqual(0, life.execute_nudge(base))
        self.assertEqual(before, safety.read(str(base) + '.run.json'))
        self.assertIn('instance replaced', safety.read(str(base) + '.nudge.json')['reason'])
        self.assertIn('NUDGE_EXPIRED', Path(str(base) + '.models.log').read_text())

    def test_stop_command_expires_pending_nudge_with_audit_log(self):
        base = self.queued('stop-command', 'running', reopen=False)
        life.stop_run(base.name)
        request = safety.read(str(base) + '.nudge.json')
        self.assertTrue(request['expired'])
        self.assertTrue(request['cancelled'])
        self.assertFalse(request['pending'])
        self.assertIn('NUDGE_EXPIRED status=stopped', Path(str(base) + '.models.log').read_text())

    def test_reopen_and_still_active_runs_are_delivered(self):
        for name, status, reopen in (('reopen', 'retrying', True), ('died', 'running', False)):
            with self.subTest(name=name):
                base = self.queued(name, status, reopen=reopen)
                with mock.patch.object(a, 'nudge_main', return_value=0) as resume:
                    self.assertEqual(0, life.execute_nudge(base))
                resume.assert_called_once_with([base.name, 'Also merge the stacked branch.'])
                request = safety.read(str(base) + '.nudge.json')
                self.assertNotIn('expired', request)
                self.assertEqual((False, 0), (request['pending'], request['exit_code']))

    def test_status_is_judged_after_owners_exit(self):
        base = self.queued('race', 'running', reopen=False)
        def owners_alive(_):
            run = safety.read(str(base) + '.run.json')
            safety.atomic(str(base) + '.run.json', dict(run, status='done'))
            return False
        with mock.patch.object(a, 'owners_alive', side_effect=owners_alive), \
                mock.patch.object(a, 'nudge_main', return_value=0) as resume:
            self.assertEqual(0, life.execute_nudge(base))
        resume.assert_called_once_with([base.name, 'Also merge the stacked branch.'])
        self.assertNotIn('expired', safety.read(str(base) + '.nudge.json'))

    def test_resume_archives_each_previous_report(self):
        base = self.runs / 'reports'
        report = Path(str(base) + '.last.md')
        self.assertIsNone(a.preserve_report(base))
        report.write_text(' \n')
        self.assertIsNone(a.preserve_report(base))
        for ordinal, text in ((1, 'DONE first.\n'), (2, 'DONE second.\n')):
            report.write_text(text)
            archive = a.preserve_report(base)
            self.assertEqual(Path(str(base) + '.last.{}.md'.format(ordinal)), archive)
            self.assertEqual(text, archive.read_text())
            self.assertEqual(0o600, archive.stat().st_mode & 0o777)
        self.assertEqual('DONE second.\n', report.read_text())

    def test_preamble_steers_browser_work_to_headless_playwright(self):
        self.assertIn('headless Playwright', safety.PREAMBLE)
        self.assertIn("never drive the user's own Chrome", safety.PREAMBLE)


if __name__ == '__main__':
    unittest.main(verbosity=2)
