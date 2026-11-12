"""Stream heartbeats from trimmed real CLI recordings.

Sources, redacted and shortened:
  grok-think.jsonl   ~/.agent-runs/godfather_phaseC.stream.jsonl
  grok-work.jsonl    ~/.agent-runs/phaseD_D1.stream.jsonl
  grok-error.jsonl   ~/.agent-runs/grok_run.log
  codex-exec.jsonl   ~/.agent-runs/sbfix.log
  agy-transcript.jsonl  Antigravity transcript.jsonl
  agy-stream.log     Antigravity cli log streamGenerateContent marker
"""
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
import agent_oversight as oversight
import godfather_stream as stream
import godfather_store as store

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


class StreamCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.addCleanup(self.tmp.cleanup)
        env = dict(os.environ)
        env.pop('GODFATHER_DB', None)
        env['AGENT_RUNS_DIR'] = str(self.runs)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)

    def segment(self, cli, name='seg'):
        if store.machine_status(self.runs, 'run-1') is None:
            store.insert_run(self.runs, 'run-1', 'task', 'running', cwd='/work', prompt='p')
        ordinal = 1 if name == 'seg' else 2
        store.insert_segment(self.runs, name, 'run-1', 'streaming', cli=cli, model='m', ordinal=ordinal)
        return name

    def save(self, book, segment_id):
        self.assertTrue(stream.persist(self.runs, segment_id, book.pending))
        book.pending = []
        return store.segment_heartbeat(self.runs, segment_id)

    def test_grok_thoughts_do_not_count_as_progress(self):
        book = stream.Book('grok', started_at=1000)
        beats = book.replay(FIXTURES / 'grok-think.jsonl', 1000, 60)
        self.assertEqual(['stream', 'thought', 'thought', 'thought', 'thought', 'thought'], [b['kind'] for b in beats])
        self.assertIsNone(book.last_tool_at)
        early = book.assess(1000 + 179)
        self.assertIsNone(early['failure_class'])
        self.assertLess(early['thought_only_secs'], stream.THINK_WITHOUT_TOOL_SECS)
        fired = book.assess(1000 + 180)
        self.assertEqual('stall_thinking', fired['failure_class'])
        self.assertGreaterEqual(fired['thought_only_secs'], 180)
        self.assertFalse(fired['progress'])
        # A tool, then only thoughts: thought time is separate from that progress.
        worked = stream.Book('grok', started_at=0)
        worked.feed(b'{"type":"tool_call","toolName":"list_dir","toolCallId":"t","status":"completed","rawInput":{}}\n', 0)
        worked.replay(FIXTURES / 'grok-think.jsonl', 60, 60)
        self.assertEqual(0, worked.last_tool_at)
        stalled = worked.assess(180)
        self.assertEqual('stall_thinking', stalled['failure_class'])
        self.assertEqual(180, stalled['thought_only_secs'])
        self.assertTrue(stalled['progress'])
        row = self.save(book, self.segment('grok'))
        self.assertEqual('thought', row['last_heartbeat_kind'])
        self.assertIsNone(row['last_tool_at'])
        self.assertEqual('streaming', row['status'])

    def test_grok_tools_usage_end_and_real_error(self):
        book = stream.Book('grok', started_at=0)
        beats = book.replay(FIXTURES / 'grok-work.jsonl', 0, 10)
        self.assertEqual(['tool', 'tool', 'tool', 'tool', 'text', 'usage', 'end'], [b['kind'] for b in beats])
        self.assertTrue(any(b['file_change'] and 'search_replace' in b['detail'] for b in beats))
        self.assertEqual(30, book.last_tool_at)
        self.assertEqual('end', book.last_kind)
        self.assertTrue(book.saw_end)
        self.assertEqual((72959, 662144, 24008), (book.tokens_in, book.tokens_cached, book.tokens_out))
        self.assertEqual(0, len(book.open_tools))
        row = self.save(book, self.segment('grok'))
        self.assertEqual('end', row['last_heartbeat_kind'])
        self.assertEqual(30, row['last_tool_at'])
        self.assertEqual(72959, row['tokens_in'])
        self.assertEqual(662144, row['tokens_cached'])
        self.assertEqual(24008, row['tokens_out'])
        self.assertNotEqual('exited', row['status'])
        err = stream.Book('grok', started_at=0)
        got = err.replay(FIXTURES / 'grok-error.jsonl', 5, 0)
        self.assertEqual('error', got[0]['kind'])
        self.assertIsNone(got[0]['failure_class'])
        auth = stream.Book('grok', started_at=0)
        auth.feed(b'{"type":"error","message":"authentication failed: not logged in"}\n', 10)
        self.assertEqual('auth', auth.assess(20)['failure_class'])
        self.assertEqual('stall_silent', auth.assess(10 + stream.SILENCE_SECS)['failure_class'])
        saved = self.save(auth, self.segment('grok', 'auth'))
        self.assertEqual('error', saved['last_heartbeat_kind'])
        self.assertEqual('auth', saved['failure_class'])

    def test_codex_exec_json_file_change_and_usage(self):
        book = stream.Book('codex', started_at=0)
        beats = book.replay(FIXTURES / 'codex-exec.jsonl', 0, 1)
        kinds = [b['kind'] for b in beats]
        self.assertEqual(['stream', 'stream', 'text', 'tool', 'tool', 'text', 'tool', 'tool', 'tool', 'thought', 'usage'], kinds)
        self.assertTrue(any(b['file_change'] for b in beats))
        self.assertLess(book.last_tool_at, beats[-1]['at'])
        self.assertEqual('usage', beats[-1]['kind'])
        self.assertEqual((51051, 34432, 57), (book.tokens_in, book.tokens_cached, book.tokens_out))
        self.assertFalse(beats[-1]['progress'])
        self.assertEqual('thought', beats[-2]['kind'])
        self.assertFalse(beats[-2]['progress'])
        row = self.save(book, self.segment('codex'))
        self.assertEqual('usage', row['last_heartbeat_kind'])
        self.assertEqual(book.last_tool_at, row['last_tool_at'])
        self.assertEqual(51051, row['tokens_in'])

    def test_agy_transcript_and_stream_marker(self):
        book = stream.Book('agy', started_at=0)
        beats = book.replay(FIXTURES / 'agy-transcript.jsonl', 0, 30)
        self.assertEqual(['tool', 'thought', 'error'], [b['kind'] for b in beats])
        self.assertEqual('view_file', beats[0]['detail'].split()[0])
        self.assertTrue(beats[0]['progress'])
        self.assertFalse(beats[1]['progress'])
        self.assertEqual(0, book.last_tool_at)
        self.assertEqual('broken_stream', beats[2]['failure_class'])
        self.assertEqual((13134 + 100930, 0, 219 + 28403), (book.tokens_in, book.tokens_cached, book.tokens_out))
        marker = stream.Book('agy', started_at=0)
        marker.replay(FIXTURES / 'agy-transcript.jsonl', 0, 1)
        before = marker.last_tool_at
        extra = marker.replay(FIXTURES / 'agy-stream.log', 50, 1)
        self.assertEqual(['stream'], [b['kind'] for b in extra])
        self.assertEqual(before, marker.last_tool_at)
        self.assertEqual('stream', marker.last_kind)
        row = self.save(marker, self.segment('agy'))
        self.assertEqual('stream', row['last_heartbeat_kind'])
        self.assertEqual(before, row['last_tool_at'])
        self.assertEqual('broken_stream', row['failure_class'])

    def test_partial_line_updates_nothing_and_oversized_is_not_an_exit(self):
        seg = self.segment('grok')
        book = stream.Book('grok', started_at=0, max_line_bytes=64, spill_path=str(self.root / 'frame.spill'))
        line = (FIXTURES / 'grok-work.jsonl').read_bytes().splitlines()[0]
        self.assertGreater(len(line), 64)
        self.assertEqual([], book.feed(line[:40], 1))
        self.assertTrue(book.reader.pending)
        self.assertFalse(stream.persist(self.runs, seg, book.pending))
        self.assertIsNone(store.segment_heartbeat(self.runs, seg)['last_heartbeat_at'])
        # The rest of a real tool frame, still without a newline, must not parse as list_dir.
        self.assertEqual([], book.feed(line[40:], 2))
        self.assertTrue(book.reader.skipping)
        self.assertEqual(0, len(book.reader.buf))
        made = book.feed(b'\n', 3)
        self.assertEqual('oversized', made[0]['detail'])
        self.assertEqual('tool', made[0]['kind'])
        self.assertTrue(made[0]['progress'])
        self.assertFalse(book.saw_end)
        book.reader.close()
        spill = (self.root / 'frame.spill').read_bytes()
        self.assertIn(b'tool_call', spill)
        self.assertNotIn(b'\n', spill[:-1] if spill.endswith(b'\n') else spill + b'x')
        row = self.save(book, seg)
        self.assertEqual('tool', row['last_heartbeat_kind'])
        self.assertEqual(3, row['last_tool_at'])
        self.assertEqual('streaming', row['status'])
        self.assertEqual('broken_stream', store.mark_broken_stream(self.runs, seg, 'dropped'))
        row = store.segment_heartbeat(self.runs, seg)
        self.assertEqual('broken_stream', row['status'])
        self.assertEqual('broken_stream', row['failure_class'])
        self.assertNotEqual('exited', row['status'])

    def test_oversized_chunks_are_not_held_in_memory(self):
        book = stream.Book('grok', started_at=0, max_line_bytes=1000, spill_path=str(self.root / 'big.spill'))
        chunk = b'{"type":"tool_call","toolName":"search_replace","rawInput":{"new_string":"' + (b'x' * 4000)
        for start in range(0, len(chunk), 1024):
            self.assertEqual([], book.feed(chunk[start:start + 1024], 1))
            self.assertLessEqual(len(book.reader.buf), 1000)
        made = book.feed(b'"}}\n', 2)
        self.assertEqual(['oversized'], [b['detail'] for b in made])
        self.assertEqual(0, len(book.reader.buf))
        book.reader.close()
        self.assertGreater((self.root / 'big.spill').stat().st_size, 4000)

    def test_open_tool_stays_healthy_until_the_tool_budget(self):
        book = stream.Book('grok', started_at=0)
        book.feed(b'{"type":"tool_call","toolName":"list_dir","toolCallId":"open","status":"pending","rawInput":{}}\n', 0)
        self.assertEqual(1, book.assess(100)['open_tools'])
        self.assertIsNone(book.assess(100)['failure_class'])
        self.assertEqual('stall_silent', book.assess(stream.TOOL_SECS)['failure_class'])

    def test_progress_hook_ignores_mtime(self):
        seg = self.segment('grok', 'hook')
        path = self.runs / 'hook.stream.jsonl'
        path.write_text('{"type":"thought","data":"I will stop planning"}\n')
        run = dict(cli='grok', started_at=time.time(), segment_id=seg, progress_paths=[str(path)], name='task')
        progress = oversight.Progress(run)
        progress.heartbeat_root = str(self.runs)
        progress.update(run)
        row = store.segment_heartbeat(self.runs, seg)
        self.assertEqual('thought', row['last_heartbeat_kind'])
        self.assertIsNone(row['last_tool_at'])
        stamped = row['last_heartbeat_at']
        os.utime(path, (time.time() + 50, time.time() + 50))
        progress.update(run)
        again = store.segment_heartbeat(self.runs, seg)
        self.assertEqual(stamped, again['last_heartbeat_at'])
        self.assertIsNone(again['last_tool_at'])
        with path.open('a') as handle:
            handle.write('{"type":"tool_call","toolName":"search_replace","toolCallId":"w","status":"pending",'
                         '"rawInput":{"file_path":"/work/a.py","old_string":"a","new_string":"b"}}\n')
        progress.update(run)
        touched = store.segment_heartbeat(self.runs, seg)
        self.assertEqual('tool', touched['last_heartbeat_kind'])
        self.assertIsNotNone(touched['last_tool_at'])

    def test_registry_refresh_keeps_heartbeat_columns(self):
        import godfather_safety as safety
        seg = self.segment('grok', 'keep')
        book = stream.Book('grok', started_at=0)
        book.replay(FIXTURES / 'grok-work.jsonl', 0, 1)
        self.save(book, seg)
        path = self.runs / 'task.run.json'
        safety.atomic(path, dict(name='task', cli='grok', model='m', status='running', cwd='/work',
                                  run_id='run-1', segment_id=seg, pid=os.getpid(), started_at=time.time()))
        row = store.segment_heartbeat(self.runs, seg)
        self.assertEqual('end', row['last_heartbeat_kind'])
        self.assertIsNotNone(row['last_tool_at'])
        self.assertEqual(72959, row['tokens_in'])


if __name__ == '__main__':
    unittest.main()
