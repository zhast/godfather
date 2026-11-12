"""Cross-CLI recovery retains the conductor brief, exact state, and access."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import test_router as fixture
import godfather_permissions as permissions
import godfather_safety as safety


class HandoffTests(unittest.TestCase):
    setUp = fixture.RouterTests.setUp
    config_models = fixture.RouterTests.config_models
    launch = fixture.RouterTests.launch
    calls = fixture.RouterTests.calls

    def git(self, *args, check=True):
        return subprocess.run(['git', '-C', str(self.root)] + list(args),
                              env=self.env, text=True, capture_output=True, check=check)

    def test_handoff_records_branch_merge_index_brief_and_full_last_report(self):
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        tracked = self.root / 'tracked'
        tracked.write_text('base\n')
        self.git('add', 'tracked')
        self.git('commit', '-qm', 'base')
        self.git('checkout', '-qb', 'other')
        tracked.write_text('other\n')
        self.git('commit', '-qam', 'other')
        self.git('checkout', '-qb', 'current', 'HEAD~')
        tracked.write_text('current\n')
        self.git('commit', '-qam', 'current')
        self.assertNotEqual(0, self.git('merge', 'other', check=False).returncode)
        (self.root / 'staged').write_text('preserve this staging\n')
        self.git('add', 'staged')
        base = self.root / 'previous'
        report = 'Critical first line\n' + 'report detail\n' * 50 + 'Critical final line'
        base.with_suffix('.last.md').write_text(report)
        with mock.patch.dict(os.environ, self.env, clear=True):
            handoff = fixture.r.handoff(self.prompt.read_text(), self.root, base, dict(cli='codex'))
        for expected in (self.prompt.read_text(), 'Branch: current', 'Merge in progress: ',
                         self.git('rev-parse', 'MERGE_HEAD').stdout.strip(),
                         'A  staged', 'UU tracked', report, 'ps -p <pid>', 'pgrep -f godfather'):
            self.assertIn(expected, handoff)
        self.assertIn('UU tracked', self.git('status', '--short').stdout)

    def test_codex_to_grok_preserves_roots_and_inspection_rules(self):
        self.config_models(('codex', 'fake'), ('grok', 'backup'))
        extra = self.root / 'additional workspace'
        extra.mkdir()
        result = self.launch('fallback-access', QUOTA_MODELS='fake', CODEX_NATIVE='1', ADD_DIRS=str(extra))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['codex', 'grok'], [c['cli'] for c in self.calls()])
        source = fixture.a.read(self.root / 'runs/fallback-access.permissions.json')
        target = fixture.a.read(self.root / 'runs/fallback-access#2.permissions.json')
        self.assertEqual(source['writable_roots'], target['writable_roots'])
        self.assertIn(str(extra), target['writable_roots'])
        self.assertEqual(source['allow_prefixes'], target['allow_prefixes'])
        self.assertTrue(target['process_inspection'])
        self.assertIn('Write(' + str(extra) + '/**)', self.calls()[-1]['args'])
        self.assertIn(self.prompt.read_text(), self.calls()[-1]['prompt'])
        self.assertIn('Changed the tracked file.', self.calls()[-1]['prompt'])
        self.assertIn(str(extra), self.calls()[-1]['prompt'])
        self.assertIn('Never edit `.env`', self.calls()[-1]['prompt'])
        self.assertEqual('done', fixture.a.read(self.root / 'runs/fallback-access#2.run.json')['status'])

    def test_invalid_grok_extra_root_still_fails_before_launch(self):
        result = subprocess.run([str(fixture.ROOT / 'bin/grok-run'), str(self.prompt), 'invalid'],
                                env=dict(self.env, ADD_DIRS=str(self.root / 'missing')),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(2, result.returncode)
        self.assertIn('not a directory', result.stderr)
        self.assertFalse((self.root / 'calls.jsonl').exists())

    def test_notes_shared_across_same_cli_resume_and_cross_cli_handoff(self):
        first, second, third = 'gemini-4-argon-high', 'claude-opus-5-5-high', 'grok-4.7'
        self.config_models(('agy', first), ('agy', second), ('grok', third))
        result = self.launch('notes', QUOTA_MODELS=first + ',' + second, WRITE_NOTES='1')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual([first, second, third], [c['model'] for c in calls])
        notes = self.root / 'runs/notes.notes.md'
        self.assertEqual([str(notes)] * 3, [c['notes_path'] for c in calls])
        self.assertEqual('', calls[0]['notes_before'])
        self.assertIn(first, calls[1]['notes_before'])
        self.assertIn('--conversation', calls[1]['args'])
        self.assertIn("## Previous agent's notes", calls[2]['prompt'])
        self.assertIn('Done: inspected with ' + first, calls[2]['prompt'])
        self.assertIn('Done: inspected with ' + second, calls[2]['prompt'])
        for call in calls:
            self.assertIn(str(notes), call['prompt'])
            self.assertIn('Checks run (command and result)', call['prompt'])
        for name in ('notes', 'notes#2', 'notes#3'):
            run = fixture.a.read(self.root / ('runs/' + name + '.run.json'))
            self.assertEqual(str(notes), run['notes_path'])
            profile = fixture.a.read(run['permission_profile'])
            self.assertEqual([str(notes)], profile['writable_files'])
        self.assertIn(third, notes.read_text())
        self.assertFalse((self.root / 'runs/notes#2.notes.md').exists())

    def test_direct_launches_name_notes_and_export_environment(self):
        for cli in ('agy', 'codex', 'grok'):
            with self.subTest(cli=cli):
                result = subprocess.run([str(fixture.ROOT / ('bin/' + cli + '-run')),
                                         str(self.prompt), cli], env=dict(self.env, CODEX_MODEL='gpt-6.1-sol'),
                                        text=True, capture_output=True, timeout=15)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                call = self.calls()[-1]
                notes = self.root / ('runs/' + cli + '.notes.md')
                self.assertEqual(str(notes), call['notes_path'])
                self.assertIn(safety.notes_preamble(notes), call['prompt'])
                self.assertTrue(notes.is_file())
                if cli == 'codex':
                    self.assertTrue(any('GODFATHER_NOTES=' + json.dumps(str(notes)) in arg
                                        for arg in call['args']))

    def test_notes_path_uses_logical_name_including_large_segment_numbers(self):
        for segment in ('task', 'task#2', 'task#10', 'task#123'):
            self.assertEqual(self.root / 'task.notes.md', safety.notes_path(self.root / segment, {}))
        self.assertEqual(self.root / 'literal#2.notes.md', safety.notes_path(
            self.root / 'literal#2#3', dict(router_name='literal#2')))

    def test_handoff_notes_keep_newest_bytes_before_report(self):
        base = self.root / 'notes#2'
        notes = self.root / 'notes.notes.md'
        report = 'Existing full report'
        Path(str(base) + '.last.md').write_text(report)
        notes.write_text('Oldest decision\n' + 'history\n' * 6000 + 'Newest decision: continue here')
        with mock.patch.dict(os.environ, self.env, clear=True):
            handoff = fixture.r.handoff('original task', self.root, base, dict(cli='codex'))
        self.assertIn('[Truncated; newest notes retained]', handoff)
        self.assertNotIn('Oldest decision', handoff)
        self.assertIn('Newest decision: continue here', handoff)
        self.assertLess(handoff.index("## Previous agent's notes"), handoff.index('## Last report'))
        self.assertIn(report, handoff)
        self.assertIn(str(notes), handoff)
        section = handoff.split("## Previous agent's notes\n", 1)[1].split('\n## Last report', 1)[0]
        self.assertLess(len(section), fixture.r.NOTES_HANDOFF_BYTES + 100)

    def test_short_notes_are_complete_and_unreadable_notes_still_fall_back(self):
        base = self.root / 'previous'
        notes = self.root / 'previous.notes.md'
        content = 'Done\nInvestigated café parser.\nDecisions (and why)\nKeep the current format.'
        notes.write_text(content)
        Path(str(base) + '.last.md').write_text('Report fallback')
        with mock.patch.dict(os.environ, self.env, clear=True):
            handoff = fixture.r.handoff('original', self.root, base, dict(cli='codex'))
            self.assertIn(content, handoff)
            self.assertNotIn('[Truncated;', handoff)
            notes.unlink()
            notes.mkdir()  # Simulate an unreadable notes artifact without chmod/root assumptions.
            handoff = fixture.r.handoff('original', self.root, base, dict(cli='codex'))
            self.assertNotIn("## Previous agent's notes", handoff)
            self.assertIn('Report fallback', handoff)

    def test_missing_and_empty_notes_preserve_message_fallbacks(self):
        base = self.root / 'previous'
        notes = self.root / 'previous.notes.md'
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text(json.dumps(dict(type='PLANNER_RESPONSE', content='Transcript lesson')) + '\n')
        for content in (None, '', '  \n'):
            if content is not None:
                notes.write_text(content)
            for cli, paths, expected in (('agy', [str(transcript)], 'Transcript lesson'),
                                         ('codex', [], 'Diagnostic fallback'),
                                         ('grok', [], 'Grok partial lesson')):
                with self.subTest(notes=content, cli=cli):
                    Path(str(base) + '.log').write_text('Diagnostic fallback')
                    Path(str(base) + '.json').write_text(json.dumps(dict(text='Grok partial lesson')))
                    with mock.patch.dict(os.environ, self.env, clear=True):
                        handoff = fixture.r.handoff('original', self.root, base,
                                                    dict(cli=cli, progress_paths=paths))
                    self.assertNotIn("## Previous agent's notes", handoff)
                    self.assertIn(expected, handoff)
                    self.assertIn('## Last messages from previous run', handoff)
