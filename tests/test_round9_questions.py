"""Terminal question expiry, including already-imported legacy controls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

import test_round6 as fixture
import watch_fixture
import agent_oversight as a
import godfather_registry as registry
import godfather_safety as safety
import godfather_store as store
import godfather_mcp as mcp

ROOT = fixture.ROOT


class TerminalQuestionTests(unittest.TestCase):
    def setUp(self):
        fixture.Round6Tests.setUp(self)
        store.connect(self.runs).close()

    run_record = fixture.Round6Tests.run_record

    def ended(self, name='ended', status='failed', **fields):
        values = dict(status=status, ended_at=time.time(), pid=99999999, pid_identity='gone')
        values.update(fields)
        run = self.run_record(name, **values)
        questions = [dict(id=name + str(i), question='Old decision %d?' % i,
                          waiting=True, blocking=True, answer=None, delivered=False)
                     for i in range(17)]
        questions.append(dict(id=name + 'answered', question='Answered?', waiting=True,
                              blocking=True, answer='yes', delivered=True))
        questions.append(dict(id=name + 'undelivered', question='Saved answer?', waiting=False,
                              blocking=False, answer='no', delivered=False))
        safety.control(self.runs / name, lambda d: d.update(questions=questions))
        return run

    def test_existing_terminal_controls_expire_in_status_and_mcp(self):
        self.ended()
        # The database has already imported the bad legacy control, as on the host.
        self.assertTrue(store.load_runs(self.runs)[0]['questions'][0]['waiting'])
        row = a.snapshot(self.runs)[0]
        self.assertFalse(row['pending_decision'])
        self.assertNotIn('question:', row['last_action'])
        control = safety.control(self.runs / 'ended')
        for q in control['questions']:
            self.assertFalse(q['waiting'])
            self.assertTrue(q['delivered'])
            self.assertEqual('expired: run ended', q['reason'])
        self.assertEqual('expired', control['questions'][0]['answer'])
        self.assertEqual('yes', control['questions'][-2]['answer'])
        self.assertNotIn('expired_at', control['questions'][-2])
        self.assertEqual('no', control['questions'][-1]['answer'])
        self.assertEqual(control['questions'], store.load_runs(self.runs)[0]['questions'])
        self.assertFalse(mcp.call_tool('status', {})[0]['pending_decision'])
        self.assertNotIn('question', mcp.report('ended'))
        self.assertEqual(0, registry.expire_ended_asks(self.runs))

    def test_doctor_fix_cleans_old_hidden_runs_and_is_idempotent(self):
        for status in store.TERMINAL + ('killed', 'replaced'):
            self.ended(status, status, ended_at=time.time() - 86400)
        cmd = [str(ROOT / 'bin/godfather'), 'doctor', '--fix']
        result = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('Expired asks in %d ended runs.' % (len(store.TERMINAL) + 2), result.stdout)
        again = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertIn('Expired asks in 0 ended runs.', again.stdout)
        self.assertTrue(all(not r.get('pending_decision') for r in a.snapshot(self.runs)))

    def test_terminal_live_owner_or_session_is_never_expired(self):
        identity = a.processes()[os.getpid()]['identity']
        for owner in ('pid', 'launcher_pid', 'supervisor_pid', 'router_pid', 'session'):
            run = self.ended(owner)
            if owner == 'router_pid':
                safety.atomic(self.runs / (owner + '.route.json'), dict(name=owner, status='running',
                              router_pid=os.getpid(), router_identity=identity))
            elif owner == 'session':
                # A child of a dead session leader is still an owner.
                run.update(pid=99999999, process_session_id=99999999)
                safety.atomic(self.runs / (owner + '.run.json'), run)
            else:
                run[owner] = os.getpid()
                run[owner.replace('_pid', '_identity') if owner != 'pid' else 'pid_identity'] = identity
                safety.atomic(self.runs / (owner + '.run.json'), run)
            procs = a.processes()
            if owner == 'session':
                procs[12345] = dict(sid=99999999, stat='S', identity='live orphan')
            with self.subTest(owner=owner):
                self.assertIsNone(registry.expire_terminal_questions(self.runs / (owner + '.run.json'), procs=procs))
                self.assertTrue(safety.control(self.runs / owner)['questions'][0]['waiting'])

    def test_nonterminal_asks_stay_answerable_and_reused_pid_does_not_block_expiry(self):
        for status in ('running', 'needs_input', 'checking', 'reviewing', 'queued', 'retrying'):
            self.ended(status, status)
            self.assertIsNone(registry.expire_terminal_questions(self.runs / (status + '.run.json')))
            self.assertIsNone(safety.control(self.runs / status)['questions'][0]['answer'])
        self.ended('reused', launcher_pid=os.getpid(), launcher_identity='different process')
        self.assertIsNotNone(registry.expire_terminal_questions(self.runs / 'reused.run.json'))

    def test_completed_answer_with_stale_waiting_flag_stays_successful(self):
        self.ended('done', 'done')
        safety.control(self.runs / 'done', lambda d: d.update(questions=[d['questions'][-2]]))
        (self.runs / 'done.last.md').write_text('DONE\n')
        registry.expire_terminal_questions(self.runs / 'done.run.json')
        self.assertEqual('done', mcp.report('done')['status'])

    def test_watch_does_not_surface_expired_questions(self):
        self.assert_watch_does_not_surface_expired_questions()

    def test_watch_expires_questions_with_slow_startup(self):
        self.assert_watch_does_not_surface_expired_questions(startup_delay=1)

    def assert_watch_does_not_surface_expired_questions(self, startup_delay=0):
        # Keep this terminal record in the watch event window, so its old-run
        # suppression cannot mask a stale NEEDS_INPUT event.
        self.ended(ended_at=time.time() + 60)
        safety.event(self.runs / 'ended', 'NEEDS_INPUT', ': Old decision 0?')
        watch = watch_fixture.launch([str(ROOT / 'bin/agents-watch'), '--no-heal', '--poll-secs', '.03'],
                                     self.env, startup_delay)
        output = b''
        try:
            # DONE proves startup, terminal reconciliation and the run scan all
            # completed; stopping after a fixed sleep can skip all three.
            output = watch_fixture.read_until(watch, b'DONE ended')
            watch_fixture.wait_for(
                watch, lambda: not safety.control(self.runs / 'ended')['questions'][0]['waiting'],
                'terminal question expiry')
        finally:
            if watch.poll() is None:
                watch.terminate()
            remaining, error = watch.communicate(timeout=5)
            output += remaining
        self.assertIn(b'DONE ended', output, error)
        self.assertNotIn(b'NEEDS_INPUT ended', output)
        self.assertFalse(safety.control(self.runs / 'ended')['questions'][0]['waiting'])
