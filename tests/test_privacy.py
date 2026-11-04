"""Privacy operations act on reviewed local state and never import credentials."""
import base64
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from herness.privacy import Privacy
from herness.state import ProjectStore


class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.privacy = Privacy(self.root)
        self.state = SimpleNamespace(busy=False, lock=threading.RLock(), session_name='active')

    def request(self, action, data=None, method='POST'):
        return self.privacy.handle(method, '/api/privacy/' + action, data or {}, self.state)

    def save(self, **data):
        return self.request('memory', {'revision': self.privacy.memory()['revision'], **data})

    def archive(self, files):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps({'format': 'herness-project-backup', 'version': 1,
                'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}))
            for name, content in files.items():
                archive.writestr('state/' + name, content)
        return base64.b64encode(stream.getvalue()).decode()

    def test_memory_revision_versions_restore_and_delete(self):
        initial = self.save(label='style', content='Concise please')
        entry = initial['entries'][0]
        self.assertEqual(entry['source'], 'user')
        changed = self.save(id=entry['id'], content='Explain carefully')
        self.assertEqual(changed['entries'][0]['version'], 2)
        self.assertEqual(changed['entries'][0]['versions'][0]['content'], 'Concise please')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.request('memory', {'revision': initial['revision'], 'id': entry['id'], 'content': 'stale'})
        restored = self.save(action='restore', id=entry['id'], version=1)
        self.assertEqual(restored['entries'][0]['content'], 'Concise please')
        self.assertEqual(restored['entries'][0]['version'], 3)
        self.assertEqual(self.save(action='delete', id=entry['id'])['entries'], [])

    def test_expiry_exclusion_conflicts_and_context_bound(self):
        self.save(label='active', content='useful memory')
        self.save(label='expired', content='expired secret', expires_at=1)
        self.save(label='excluded', content='excluded secret', excluded=True)
        self.save(label='choice', content='first conflicting value')
        conflict = self.save(label='CHOICE', content='second conflicting value')
        self.assertEqual(len(conflict['conflicts']), 1)
        self.assertEqual(self.privacy.context_text(), 'active: useful memory')
        for index in range(8):
            self.save(label='large-' + str(index), content='漢' * 1000)
        self.assertLessEqual(len(self.privacy.context_text().encode()), 16000)

    def test_memory_import_merges_with_provenance_and_preserves_legacy(self):
        ProjectStore(self.root).save_memory('Legacy memory stays here')
        self.save(label='preference', content='old')
        exported = self.request('memory/export', method='GET')
        self.save(label='new preference', content='new')
        imported = self.request('memory/import', {'revision': self.privacy.memory()['revision'], 'content': exported['content']})
        self.assertEqual(imported['imported'], 0)
        payload = json.loads(exported['content'])
        payload['entries'][0]['content'] = 'changed import'
        imported = self.request('memory/import', {'revision': self.privacy.memory()['revision'], 'content': json.dumps(payload)})
        self.assertEqual(imported['imported'], 1)
        self.assertEqual(imported['entries'][-1]['source'], 'import')
        self.assertEqual(len(imported['conflicts']), 1)
        self.assertEqual(ProjectStore(self.root).memory(), 'Legacy memory stays here')

    def test_memory_history_is_bounded(self):
        identity = self.save(label='changing', content='version0')['entries'][0]['id']
        for number in range(15):
            result = self.save(id=identity, content='version' + str(number + 1))
        self.assertEqual(len(result['entries'][0]['versions']), 10)
        self.assertEqual(result['entries'][0]['version'], 16)

    def test_profiles_persist_and_busy_changes_are_rejected(self):
        result = self.request('permissions', {'profile': 'read-only'})
        self.assertEqual(result['scopes'], ['read'])
        self.assertEqual(Privacy(self.root).tool_scopes(), ['read'])
        self.state.busy = True
        with self.assertRaisesRegex(ValueError, 'active task'):
            self.request('permissions', {'profile': 'standard'})
        self.assertEqual(self.privacy.tool_scopes(), ['read'])

    def test_diagnostics_and_audit_do_not_include_contents_or_credentials(self):
        self.save(label='secret label', content='sk-sensitive-user-content')
        self.privacy.store.write('settings.json', '{"api_key":"secret-key"}')
        diagnostics = json.dumps(self.request('diagnostics', method='GET'))
        audit = json.dumps(self.request('audit', method='GET'))
        for secret in ('secret label', 'sk-sensitive-user-content', 'secret-key', str(self.root)):
            self.assertNotIn(secret, diagnostics + audit)
        self.assertIn('memory.save', audit)

    def test_backup_export_excludes_credentials_and_live_configuration(self):
        self.save(label='preference', content='retained')
        self.privacy.store.save_memory('legacy')
        for name in ('settings.json', 'connections.json', 'automations.json', 'privacy-settings.json'):
            self.privacy.store.write(name, '{"private":"DO_NOT_EXPORT"}')
        exported = self.request('backup/export', method='GET')
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported['data']))) as archive:
            names = archive.namelist()
            self.assertIn('state/memory.md', names)
            self.assertIn('state/memory-entries.json', names)
            self.assertFalse(any(name.endswith('settings.json') for name in names))
            self.assertNotIn('DO_NOT_EXPORT', ''.join(archive.read(name).decode() for name in names))
        self.assertTrue(exported['contains_private_content'])

    def test_backup_collision_review_stale_preview_and_confirmed_restore(self):
        self.privacy.store.save_memory('current')
        encoded = self.archive({'memory.md': b'from backup', 'project.json': b'{"title":"Restored"}'})
        preview = self.request('backup/preview', {'data': encoded})
        self.assertEqual(preview['conflicts'], ['memory.md'])
        payload = {'data': encoded, 'preview_token': preview['preview_token']}
        with self.assertRaisesRegex(ValueError, 'Confirm'):
            self.request('backup/restore', payload)
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            self.request('backup/restore', {**payload, 'confirmed': True})
        self.privacy.store.save_memory('newer local change')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.request('backup/restore', {**payload, 'confirmed': True, 'overwrite': True})
        preview = self.request('backup/preview', {'data': encoded})
        result = self.request('backup/restore', {'data': encoded, 'preview_token': preview['preview_token'],
                                                'confirmed': True, 'overwrite': True})
        self.assertEqual(set(result['restored']), {'memory.md', 'project.json'})
        self.assertEqual(self.privacy.store.memory(), 'from backup')

    def test_backup_rejects_traversal_settings_invalid_json_and_manifest_tampering(self):
        invalid = [{'../outside': b'x'}, {'settings.json': b'{}'}, {'project.json': b'not json'},
                   {'project.json': b'{"pinned_files":5}'}, {'sessions/test.json': b'{"version":1,"messages":[]}'},
                   {'memory-entries.json': b'{"version":1,"entries":[{}]}'}]
        for files in invalid:
            with self.subTest(files=files), self.assertRaises(ValueError):
                self.request('backup/preview', {'data': self.archive(files)})
        with self.assertRaises(ValueError):
            self.request('backup/preview', {'data': 'not a ZIP'})
        encoded = self.archive({'memory.md': b'good'})
        stream = io.BytesIO(base64.b64decode(encoded))
        with zipfile.ZipFile(stream, 'a') as archive:
            archive.writestr('state/other.txt', b'bad')
        with self.assertRaises(ValueError):
            self.request('backup/preview', {'data': base64.b64encode(stream.getvalue()).decode()})
        self.assertFalse((self.root / 'outside').exists())

    def test_backup_rejects_compressed_bombs_and_private_symlinks(self):
        encoded = self.archive({'memory.md': b'a' * 8_300_000})
        with self.assertRaisesRegex(ValueError, 'Expanded'):
            self.request('backup/preview', {'data': encoded})
        external = self.root / 'external.txt'
        external.write_text('outside')
        self.privacy.store.path('memory.md').symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.request('backup/preview', {'data': self.archive({'memory.md': b'replaced'})})
        self.assertEqual(external.read_text(), 'outside')

    def test_backup_rolls_back_written_state_on_restore_failure(self):
        self.privacy.store.save_memory('original')
        encoded = self.archive({'memory.md': b'changed', 'project.json': b'{}'})
        preview = self.request('backup/preview', {'data': encoded})
        original_write = self.privacy._write
        def fail(name, content):
            if name == 'project.json':
                raise OSError('simulated storage error')
            return original_write(name, content)
        with patch.object(self.privacy, '_write', side_effect=fail), self.assertRaises(OSError):
            self.request('backup/restore', {'data': encoded, 'preview_token': preview['preview_token'],
                                            'confirmed': True, 'overwrite': True})
        self.assertEqual(self.privacy.store.memory(), 'original')
        self.assertFalse(self.privacy.store.path('project.json').exists())

    def test_backup_rechecks_changes_after_decode_before_writing(self):
        self.privacy.store.save_memory('original')
        encoded = self.archive({'memory.md': b'backup'})
        preview = self.request('backup/preview', {'data': encoded})
        original_decode = self.privacy._decode_backup
        calls = []
        def concurrent_change(data):
            result = original_decode(data)
            calls.append(True)
            if len(calls) == 2:
                self.privacy.store.save_memory('concurrent newer edit')
            return result
        with patch.object(self.privacy, '_decode_backup', side_effect=concurrent_change), self.assertRaisesRegex(ValueError, 'changed'):
            self.request('backup/restore', {'data': encoded, 'preview_token': preview['preview_token'],
                                            'confirmed': True, 'overwrite': True})
        self.assertEqual(self.privacy.store.memory(), 'concurrent newer edit')

    def test_restore_refreshes_live_session_library_and_project_without_losing_keys(self):
        from herness.cli import DemoClient
        from herness.web import make_server
        server = make_server(self.root, DemoClient(), port=0, demo=True)
        self.addCleanup(server.server_close)
        state = server.workspace
        state.api_keys['https://provider.invalid'] = 'in-memory-key'
        state.session.messages.append({'role': 'user', 'content': 'original conversation'})
        state.session.save()
        state.store.write('project.json', '{"title":"Original project"}')
        state.store.write('library.json', '{"default":{"title":"Original title"}}')
        state.store.write('documents.json', '{"artifacts":{},"library":{}}')
        exported = state.application.handle('GET', '/api/privacy/backup/export', {})
        state.session.messages.append({'role': 'assistant', 'content': 'new message'})
        state.session.save()
        state.application.library.metadata['default'] = {'title': 'Changed title'}
        state.application.library.save()
        state.application.project.update({'title': 'Changed project'})
        preview = state.application.handle('POST', '/api/privacy/backup/preview', {'data': exported['data']})
        state.application.handle('POST', '/api/privacy/backup/restore', {'data': exported['data'],
            'preview_token': preview['preview_token'], 'confirmed': True, 'overwrite': True})
        self.assertEqual(state.messages[-1]['text'], 'original conversation')
        self.assertEqual(state.project['title'], 'Original project')
        self.assertEqual(state.application.library.metadata['default']['title'], 'Original title')
        self.assertEqual(state.api_keys['https://provider.invalid'], 'in-memory-key')
        self.assertEqual(state.application.handle('GET', '/api/documents', {})['artifacts'], [])

    def test_retention_excludes_current_conversation_running_tasks_and_new_records(self):
        for name, contents in [('sessions/active.json', '{}'), ('sessions/old.json', '{}'),
                               ('runs/working.json', '{"status":"running"}'),
                               ('runs/old.json', '{"status":"completed"}'), ('runs/new.json', '{}')]:
            path = self.privacy._private(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
            if name != 'runs/new.json':
                os.utime(path, (1, 1))
        self.save(label='expired', content='remove this', expires_at=1)
        self.save(label='active', content='keep this')
        options = {'days': 30, 'categories': ['sessions', 'runs', 'expired-memory']}
        preview = self.request('retention/preview', options)
        self.assertEqual({item['path'] for item in preview['files']}, {'sessions/old.json', 'runs/old.json'})
        self.assertEqual(len(preview['memory_ids']), 1)
        with self.assertRaisesRegex(ValueError, 'Confirm'):
            self.request('retention/apply', {**options, 'preview_token': preview['preview_token']})
        result = self.request('retention/apply', {**options, 'preview_token': preview['preview_token'], 'confirmed': True})
        self.assertEqual(result['deleted_files'], 2)
        self.assertEqual(result['deleted_memory_entries'], 1)
        self.assertTrue(self.privacy._private('sessions/active.json').exists())
        self.assertTrue(self.privacy._private('runs/working.json').exists())
        self.assertEqual(self.privacy.context_text(), 'active: keep this')

    def test_retention_stale_candidates_are_not_deleted(self):
        path = self.privacy._private('runs/old.json')
        path.parent.mkdir()
        path.write_text('{"status":"completed"}')
        os.utime(path, (1, 1))
        options = {'days': 30, 'categories': ['runs']}
        preview = self.request('retention/preview', options)
        path.write_text('{"status":"updated"}')
        os.utime(path, (1, 1))
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.request('retention/apply', {**options, 'preview_token': preview['preview_token'], 'confirmed': True})
        self.assertTrue(path.exists())

    def test_invalid_api_input_types_raise_validation_errors(self):
        invalid = [('permissions', {'profile': []}), ('permissions', {'profile': {}}),
                   ('retention/preview', {'days': True}), ('retention/preview', {'categories': [[]]}),
                   ('memory', {'revision': 'missing', 'action': []}),
                   ('memory', {'revision': 'missing', 'label': 'label', 'content': 'text', 'expires_at': float('nan')}),
                   ('memory', {'revision': 'missing', 'label': 'label', 'content': 'text', 'excluded': 'true'}),
                   ('backup/preview', {'data': []})]
        for action, data in invalid:
            with self.subTest(action=action, data=data), self.assertRaises(ValueError):
                self.request(action, data)
        with self.assertRaises(ValueError):
            self.privacy.handle('POST', '/api/privacy/memory', ['invalid'], self.state)


if __name__ == '__main__':
    unittest.main()
