"""Exercise approval boundaries, recoverable writes and real workspace inspection."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from herness.tools import ToolRunner
from herness.workbench import Workbench


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proposals = []
        self.runner = ToolRunner(self.root, lambda proposal: self.proposals.append(proposal) or True,
                                 change_set='turn-1')
        self.workbench = self.runner.workbench

    def call(self, name, **arguments):
        return self.runner.execute(name, json.dumps(arguments))

    def test_targeted_patch_diff_checkpoint_and_undo(self):
        target = self.root / 'code.py'
        target.write_bytes(b'# keep this\r\nvalue = 1\r\n# keep that\r\n')
        target.chmod(0o755)
        result = self.call('patch_file', path='code.py', old='value = 1', new='value = 2')
        self.assertNotIn('error', result)
        self.assertEqual(target.read_bytes(), b'# keep this\r\nvalue = 2\r\n# keep that\r\n')
        self.assertIn('-value = 1', self.proposals[0]['diff'])
        self.assertIn('+value = 2', self.proposals[0]['diff'])
        checkpoint = self.workbench.checkpoints()['checkpoints'][0]
        self.assertEqual(checkpoint['change_set'], 'turn-1')
        self.assertNotIn('before', checkpoint)
        undo = self.call('undo_change', checkpoint_id=result['checkpoint_id'])
        self.assertEqual(undo['undone'], result['checkpoint_id'])
        self.assertEqual(target.read_bytes(), b'# keep this\r\nvalue = 1\r\n# keep that\r\n')
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.proposals[-1]['kind'], 'undo')
        self.assertIn('error', self.call('undo_change', checkpoint_id=result['checkpoint_id']))

    def test_ambiguous_patch_and_denial_preserve_file(self):
        target = self.root / 'code.py'
        target.write_text('same same')
        result = self.call('patch_file', path='code.py', old='same', new='different')
        self.assertIn('exactly once', result['error'])
        self.assertEqual(self.proposals, [])
        self.runner.approve = lambda _: False
        self.assertTrue(self.call('patch_file', path='code.py', old='same same', new='different')['denied'])
        self.assertEqual(target.read_text(), 'same same')
        self.assertEqual(self.workbench.checkpoints()['checkpoints'], [])

    def test_checkpoint_precedes_file_change(self):
        original = self.workbench._atomic_file
        observed = []
        def inspect_checkpoint(path, text, mode):
            pending = list((self.root / '.herness' / 'checkpoints').glob('*.json'))
            self.assertEqual(len(pending), 1)
            record = json.loads(pending[0].read_text())
            self.assertEqual(record['status'], 'pending')
            self.assertIsNone(record['before'])
            self.assertFalse(path.exists())
            observed.append(record['id'])
            original(path, text, mode)
        with patch.object(self.workbench, '_atomic_file', side_effect=inspect_checkpoint):
            result = self.call('write_file', path='new.txt', content='created')
        self.assertEqual(observed, [result['checkpoint_id']])
        self.call('undo_change', checkpoint_id=result['checkpoint_id'])
        self.assertFalse((self.root / 'new.txt').exists())

    def test_undo_preserves_external_edits_and_rejects_changed_symlink(self):
        target = self.root / 'note.txt'
        target.write_text('before')
        result = self.call('write_file', path='note.txt', content='after')
        checkpoint_id = result['checkpoint_id']
        preview = self.workbench.undo_preview(checkpoint_id)
        target.write_text('newer edit')
        with self.assertRaisesRegex(ValueError, 'newer changes'):
            self.workbench.undo(checkpoint_id, preview['expected_hash'])
        self.assertEqual(target.read_text(), 'newer edit')
        target.unlink()
        other = self.root / 'other.txt'
        other.write_text('after')
        target.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.workbench.undo_preview(checkpoint_id)
        self.assertEqual(other.read_text(), 'after')

    def test_undo_revalidates_after_model_approval(self):
        target = self.root / 'note.txt'
        target.write_text('before')
        result = self.call('write_file', path='note.txt', content='after')
        def approve(_):
            target.write_text('external edit during approval')
            return True
        self.runner.approve = approve
        self.assertIn('newer changes', self.call('undo_change', checkpoint_id=result['checkpoint_id'])['error'])
        self.assertEqual(target.read_text(), 'external edit during approval')

    def test_ui_undo_requires_review_confirmation_and_idle_workspace(self):
        result = self.call('write_file', path='new.txt', content='new')
        payload = {'id': result['checkpoint_id']}
        state = SimpleNamespace(busy=False)
        preview = self.workbench.handle('POST', '/api/workbench/undo-preview', payload, state)
        with self.assertRaisesRegex(ValueError, 'Confirm'):
            self.workbench.handle('POST', '/api/workbench/undo', payload, state)
        with self.assertRaisesRegex(ValueError, 'preview'):
            self.workbench.handle('POST', '/api/workbench/undo', {**payload, 'confirmed': True}, state)
        reviewed = {**payload, 'confirmed': True, 'expected_hash': preview['expected_hash']}
        with self.assertRaisesRegex(ValueError, 'active turn'):
            self.workbench.handle('POST', '/api/workbench/undo', reviewed, SimpleNamespace(busy=True))
        self.assertEqual(self.workbench.handle('POST', '/api/workbench/undo', reviewed, state)['undone'], payload['id'])
        self.assertIsNone(self.workbench.handle('GET', '/api/other', {}, state))

    def test_plan_and_explicit_scopes_apply_to_advertised_and_executed_tools(self):
        (self.root / 'safe.txt').write_text('hello')
        self.runner = ToolRunner(self.root, lambda _: self.fail('Read-only mode requested approval'), mode='plan')
        names = {entry['function']['name'] for entry in self.runner.schemas()}
        self.assertEqual(names, {'list_files', 'read_file', 'search_files', 'project_symbols', 'git_status', 'git_diff'})
        self.assertEqual(len(self.call('search_files', path='.', query='hello')['matches']), 1)
        for name, args in [('write_file', {'path': 'no', 'content': 'no'}),
                           ('run_command', {'command': 'touch no'}), ('remember', {'content': 'no'})]:
            self.assertIn('scope is disabled', self.call(name, **args)['error'])
        with self.assertRaisesRegex(ValueError, 'scope is disabled'):
            self.runner.command('touch no')
        self.runner = ToolRunner(self.root, lambda _: True, scopes=set())
        self.assertEqual(self.runner.schemas(), [])
        self.assertIn('error', self.call('read_file', path='safe.txt'))
        self.assertFalse((self.root / 'no').exists())

    def test_search_ignores_sensitive_paths_dependencies_symlinks_and_project_ignores(self):
        for relative in ['src/main.py', '.env', '.env.local', 'node_modules/dep/a.js', 'hidden/private.txt', 'key.pem']:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('NEEDLE\n')
        (self.root / 'alias').symlink_to(self.root / '.env')
        self.workbench.store.write('project.json', json.dumps({'ignored_paths': ['hidden']}))
        matches = self.workbench.search('needle')['matches']
        self.assertEqual([(item['path'], item['line']) for item in matches], [('src/main.py', 1)])
        self.assertEqual(self.workbench.search('needle', case_sensitive=True)['matches'], [])
        for path in ['../outside', '.herness', '.git', '.env', 'alias']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.workbench.path(path)

    def test_symbols_report_real_lines_and_do_not_parse_comments_as_python(self):
        (self.root / 'app.py').write_text('# def fake():\nclass Service:\n    async def run(self):\n        return 1\n')
        (self.root / 'app.ts').write_text('export interface Task {}\nexport async function launch() {}\n')
        symbols = self.workbench.symbols()['symbols']
        self.assertEqual({(item['name'], item['line']) for item in symbols}, {('Service', 2), ('run', 3), ('Task', 1), ('launch', 2)})

    def test_streaming_callback_runs_before_command_finishes(self):
        signalled = threading.Event()
        chunks, results = [], []
        def output(text):
            chunks.append(text)
            if 'ready' in ''.join(chunks):
                signalled.set()
        runner = ToolRunner(self.root, lambda _: True, on_output=output)
        worker = threading.Thread(target=lambda: results.append(runner.command("printf ready; sleep .4; printf done")))
        worker.start()
        self.addCleanup(worker.join)
        self.assertTrue(signalled.wait(2))
        self.assertTrue(worker.is_alive())
        worker.join(2)
        self.assertEqual(results[0]['output'], 'readydone')
        self.assertEqual(''.join(chunks), 'readydone')

    def test_run_tests_reports_real_success_failure_and_denial(self):
        self.assertTrue(self.call('run_tests', command='printf tested')['passed'])
        failure = self.call('run_tests', command='exit 3')
        self.assertFalse(failure['passed'])
        self.assertEqual(failure['exit_code'], 3)
        self.runner.approve = lambda _: False
        denied = self.call('run_tests', command='touch must-not-exist')
        self.assertTrue(denied['denied'])
        self.assertFalse(denied['passed'])
        self.assertFalse((self.root / 'must-not-exist').exists())

    def test_sandbox_is_honest_about_local_execution(self):
        with patch('herness.workbench.shutil.which', return_value=None):
            capabilities = self.workbench.sandbox()
        self.assertFalse(capabilities['sandbox_enforced'])
        self.assertFalse(capabilities['docker_installed'])
        self.assertEqual(capabilities['execution_backend'], 'local')

    @unittest.skipUnless(shutil.which('git'), 'Git is required')
    def test_git_diff_includes_deletions_and_excludes_secrets_and_external_helpers(self):
        def git(*arguments):
            subprocess.run(['git', '-C', str(self.root), *arguments], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git('init')
        git('config', 'user.email', 'tests@example.invalid')
        git('config', 'user.name', 'Test')
        for name, content in [('tracked.txt', 'before\n'), ('deleted.txt', 'deleted content\n'), ('.env', 'PRIVATE_BEFORE\n')]:
            (self.root / name).write_text(content)
        git('add', 'tracked.txt', 'deleted.txt', '.env')
        git('commit', '-m', 'initial')
        (self.root / 'tracked.txt').write_text('after\n')
        (self.root / 'deleted.txt').unlink()
        (self.root / '.env').write_text('PRIVATE_AFTER\n')
        git('config', 'diff.external', 'touch external-diff-ran')
        status = self.workbench.git('status')
        self.assertEqual(status['exit_code'], 0)
        self.assertIn('tracked.txt', status['output'])
        result = self.workbench.git('diff')
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('+after', result['output'])
        self.assertIn('-deleted content', result['output'])
        self.assertNotIn('PRIVATE', result['output'])
        self.assertNotIn('.env', result['output'])
        self.assertFalse((self.root / 'external-diff-ran').exists())

    @unittest.skipUnless(shutil.which('git'), 'Git is required')
    def test_git_diff_works_on_unborn_branch(self):
        subprocess.run(['git', '-C', str(self.root), 'init'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.root / 'first.txt').write_text('first commit\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'first.txt'], check=True)
        result = self.workbench.git('diff')
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('+first commit', result['output'])


if __name__ == '__main__':
    unittest.main()
