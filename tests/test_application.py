"""Cross-module application route tests with isolated local workspaces."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from herness.application import Application
from herness.automation import Automation
from herness.cli import DemoClient
from herness.web import Workspace


class ApplicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.state = Workspace(self.root, DemoClient(), demo=True)
        self.app = getattr(self.state, 'application', None) or Application(self.state)
        self.addCleanup(self.close)

    def close(self):
        for module in self.app.modules:
            if hasattr(module, 'close'):
                module.close()

    def call(self, path, data=None):
        return self.app.handle('POST' if data is not None else 'GET', path, data or {})

    def conversation(self):
        self.state.session.messages += [
            {'role': 'user', 'content': 'Explain nebula colors'},
            {'role': 'assistant', 'content': 'Emission lines reveal their composition.'}]
        self.state.session.save()
        self.state.messages = self.state.saved_messages()

    def test_library_search_metadata_trash_restore_and_export(self):
        self.conversation()
        self.call('/api/library', {'action': 'update', 'names': ['default'],
                                   'values': {'title': 'Astronomy', 'folder': 'Science', 'tags': ['space'], 'pinned': True}})
        result = self.call('/api/library?q=emission')
        self.assertEqual(result['sessions'][0]['title'], 'Astronomy')
        self.assertEqual(result['folders'], ['Science'])
        self.assertTrue(result['sessions'][0]['pinned'])
        exported = self.call('/api/export?session=default&format=json')
        self.assertEqual(json.loads(exported['content'])['messages'][-1]['content'], 'Emission lines reveal their composition.')
        self.call('/api/library', {'action': 'trash', 'names': ['default']})
        self.assertEqual(self.call('/api/library')['sessions'], [])
        self.assertEqual(len(self.call('/api/library?trash=true')['sessions']), 1)
        self.call('/api/library', {'action': 'restore', 'names': ['default']})
        self.assertEqual(len(self.call('/api/library')['sessions']), 1)
        self.state.busy = True
        with self.assertRaisesRegex(ValueError, 'active task'):
            self.call('/api/library', {'action': 'trash', 'names': ['default']})
        self.assertFalse(self.app.library.metadata['default']['trashed'])

    def test_branch_and_retry_keep_original_and_attachment_content(self):
        content = [{'type': 'text', 'text': 'Describe attached chart'},
                   {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,eA=='}}]
        self.state.session.messages += [{'role': 'user', 'content': content},
                                        {'role': 'assistant', 'content': 'First answer'}]
        self.state.session.attachments = {'1': [{'name': 'chart.png', 'kind': 'image'}]}
        self.state.session.save()
        original = self.state.session
        with patch.object(self.state, 'start', return_value=True) as start:
            result = self.call('/api/retry', {})
        self.assertNotEqual(result['session'], 'default')
        self.assertEqual(len(original.messages), 3)
        self.assertEqual(len(self.state.session.messages), 1)
        self.assertEqual(start.call_args.kwargs['prepared_content'], content)
        self.assertEqual(start.call_args.kwargs['prepared_summaries'][0]['name'], 'chart.png')
        start.call_args.kwargs['prepared_content'][0]['text'] = 'Changed copy'
        self.assertEqual(original.messages[1]['content'][0]['text'], 'Describe attached chart')
        self.state.session = original
        self.state.session_name = 'default'
        self.call('/api/branch', {'index': 2})
        self.assertEqual(len(self.state.session.messages), 3)
        self.assertEqual(self.state.session.attachments['1'][0]['name'], 'chart.png')

    def test_project_settings_and_switch_isolate_data(self):
        self.conversation()
        saved = self.call('/api/project', {'title': 'First project', 'instructions': 'Use clear explanations',
                                          'goals': 'Learn astronomy', 'pinned_files': ['notes.md']})
        self.assertEqual(saved['title'], 'First project')
        self.assertEqual(self.state.project['instructions'], 'Use clear explanations')
        other = Path(self.temp.name) / 'other'
        other.mkdir()
        projects = self.call('/api/projects', {'action': 'add', 'path': str(other), 'title': 'Second project'})
        identity = next(p['id'] for p in projects['projects'] if p['path'] == str(other))
        old_token = self.state.token
        switched = self.call('/api/projects', {'action': 'switch', 'id': identity})
        self.assertEqual(switched['active'], identity)
        self.assertEqual(self.state.root, other)
        self.assertEqual(self.state.token, old_token)
        self.assertEqual(self.call('/api/library')['sessions'], [])
        self.call('/api/projects', {'action': 'switch', 'id': 'default'})
        self.assertEqual(self.call('/api/project')['title'], 'First project')
        self.assertEqual(len(self.call('/api/library')['sessions']), 1)

    def test_documents_dispatch_version_conflict_upload_search(self):
        first = self.call('/api/documents/artifact', {'action': 'create', 'title': 'Research notes', 'content': 'Initial evidence'})
        changed = self.call('/api/documents/artifact', {'action': 'update', 'id': first['id'], 'revision': 1, 'content': 'Reviewed evidence'})
        self.assertEqual(changed['revision'], 2)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.call('/api/documents/artifact', {'action': 'update', 'id': first['id'], 'revision': 1, 'content': 'Stale edit'})
        diff = self.call('/api/documents/artifact', {'action': 'diff', 'id': first['id']})
        self.assertIn('+Reviewed evidence', diff['diff'])
        doc = self.call('/api/documents/upload', {'name': 'notes.txt', 'data': base64.b64encode(b'Verified astronomy evidence').decode()})
        result = self.call('/api/documents/library', {'action': 'search', 'query': 'astronomy'})
        self.assertEqual(result['results'][0]['id'], doc['id'])
        self.assertEqual(len(self.call('/api/documents')['library']), 1)

    def test_scheduler_dispatches_once_and_records_completion(self):
        self.state.start_automation = Mock(return_value=True)
        with patch('herness.automation.time.time', return_value=1000):
            created = self.call('/api/automation/schedules', {'title': 'One review', 'prompt': 'Review source notes',
                               'schedule': {'kind': 'once', 'at': 1100}, 'confirmed': True, 'enabled': True})
        self.assertFalse(created['approval_required'])
        with patch('herness.automation.time.time', return_value=1101):
            self.app.tick()
            self.app.tick()
        self.state.start_automation.assert_called_once()
        record = self.call('/api/automation/runs')['runs'][0]
        self.assertEqual(record['status'], 'running')
        module = next(m for m in self.app.modules if isinstance(m, Automation))
        module.complete(record['id'], summary='Review finished')
        self.assertEqual(self.call('/api/automation/runs')['runs'][0]['summary'], 'Review finished')
        self.assertFalse(self.call('/api/automation/schedules')['schedules'][0]['enabled'])

    def test_run_options_validation_and_connection_status_dispatch(self):
        with self.assertRaises(ValueError):
            self.call('/api/run/options', {'max_cost_usd': 0.25})
        result = self.call('/api/run/options', {'mode': 'plan', 'max_steps': 4, 'max_cost_usd': 0.25,
                                              'input_usd_per_million': 1, 'output_usd_per_million': 2})
        self.assertEqual(result['max_steps'], 4)
        with self.assertRaises(ValueError):
            self.call('/api/run/options', {'max_steps': True})
        self.assertEqual(self.call('/api/run/options')['max_steps'], 4)
        self.assertEqual(self.call('/api/connections/status')['sources'], 0)
        self.assertIsNone(self.call('/api/unrecognized'))


if __name__ == '__main__':
    unittest.main()
