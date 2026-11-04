import base64
import io
import json
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from herness.core import Agent, Cancellation, ModelError, OpenAIClient, Session, TurnCancelled
from herness.state import ProjectStore, attachment_content
from herness.tools import ToolRunner
from herness.web import Workspace
from herness.cli import DemoClient
import test_herness as existing

tool_call = existing.tool_call


def encoded(name, raw):
    return {'name': name, 'data': base64.b64encode(raw).decode()}


class Provider:
    def __init__(self, behavior=None):
        self.requests = []
        self.behavior = behavior or (lambda payload: ['Hello ', 'from the model.'])
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(payload)
                if not payload.get('stream'):
                    body = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                try:
                    for chunk in owner.behavior(payload):
                        choice = chunk if isinstance(chunk, dict) else {'delta': {'content': chunk}}
                        self.wfile.write(('data: ' + json.dumps({'choices': [choice]}) + '\n\n').encode())
                        self.wfile.flush()
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FeaturesTest(unittest.TestCase):
    setUp = existing.ServerTest.setUp
    launch = existing.ServerTest.launch
    request = existing.ServerTest.request
    status = existing.ServerTest.status
    until = existing.ServerTest.until

    def provider(self, behavior=None):
        provider = Provider(behavior)
        self.addCleanup(provider.close)
        return provider

    def test_settings_probe_save_redaction_and_restore(self):
        provider = self.provider()
        self.launch()
        config = {'base_url': provider.base, 'model': 'local-test', 'api_key': 'private-test-key', 'demo': False}
        result = json.loads(self.request('/api/settings/test', config))
        self.assertIn('successful', result['message'])
        self.assertTrue(json.loads(self.request('/api/settings'))['demo'])  # Testing does not save.
        result = self.request('/api/settings', config).decode()
        self.assertNotIn('private-test-key', result)
        self.assertTrue(json.loads(result)['key_present'])
        saved = (self.root / '.herness/settings.json').read_text()
        self.assertNotIn('private-test-key', saved)
        restored = Workspace(self.root, OpenAIClient('initial', 'https://api.openai.com/v1'), False)
        self.assertEqual(restored.client.model, 'local-test')
        self.assertFalse(restored.settings_public()['key_present'])
        self.request('/api/settings', {**config, 'api_key': '', 'clear_key': True})
        self.assertFalse(json.loads(self.request('/api/settings'))['key_present'])

    def test_explicit_demo_overrides_saved_live_settings(self):
        store = ProjectStore(self.root)
        store.write('settings.json', json.dumps({'base_url': 'https://api.openai.com/v1', 'model': 'test', 'demo': False}))
        workspace = Workspace(self.root, DemoClient(), True, force_demo=True)
        self.assertTrue(workspace.demo)
        self.assertIsInstance(workspace.client, DemoClient)
        self.assertTrue(workspace.settings_public()['demo'])
        self.assertFalse(store.settings()['demo'])  # The override does not change saved preferences.

    def test_endpoint_change_does_not_reuse_key(self):
        self.launch()
        self.request('/api/settings', {'base_url': 'https://api.openai.com/v1', 'model': 'test', 'api_key': 'private', 'demo': False})
        result = json.loads(self.request('/api/settings', {'base_url': 'https://other.example/v1', 'model': 'test', 'demo': False}))
        self.assertFalse(result['key_present'])
        self.assertEqual(self.server.workspace.client.api_key, '')

    def test_invalid_settings_and_auth(self):
        self.launch()
        for data in [{'base_url': {}}, {'demo': 'yes'}, {'base_url': 'http://remote.example/v1'}, {'api_key': 'bad\nkey'}]:
            with self.subTest(data=data), self.assertRaises(urllib.error.HTTPError) as caught:
                self.request('/api/settings', data)
            self.assertEqual(caught.exception.code, 400)
        for path in ['/api/settings', '/api/memory']:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path, token=False)
            self.assertEqual(caught.exception.code, 403)

    def test_memory_persists_in_future_turns_and_can_be_deleted(self):
        class Capture:
            def __init__(self):
                self.contexts = []
            def complete(self, messages):
                self.contexts.append(messages[0]['content'])
                return {'role': 'assistant', 'content': 'Done'}
        client = Capture()
        self.launch(client)
        memory = json.loads(self.request('/api/memory'))
        memory = json.loads(self.request('/api/memory', {'revision': memory['revision'], 'content': 'Prefer Python and concise answers.'}))
        self.request('/api/chat', {'prompt': 'help'})
        self.until(lambda s: not s['busy'])
        self.assertIn('Prefer Python', client.contexts[-1])
        self.request('/api/memory/delete', {'revision': memory['revision']})
        self.assertFalse((self.root / '.herness/memory.md').exists())
        self.request('/api/chat', {'prompt': 'help again'})
        self.until(lambda s: not s['busy'])
        self.assertNotIn('Prefer Python', client.contexts[-1])

    def test_memory_conflict_preserves_newer_edit(self):
        self.launch()
        old = json.loads(self.request('/api/memory'))
        self.request('/api/memory', {**old, 'content': 'Newer edit'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/memory', {**old, 'content': 'Stale edit'})
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(json.loads(self.request('/api/memory'))['content'], 'Newer edit')

    def test_agent_memory_change_requires_approval(self):
        store = ProjectStore(self.root)
        store.save_memory('Old preference\n')
        proposals = []
        runner = ToolRunner(self.root, lambda proposal: proposals.append(proposal) or False)
        result = runner.execute('remember', json.dumps({'content': 'New preference\n'}))
        self.assertTrue(result['denied'])
        self.assertEqual(store.memory(), 'Old preference\n')
        self.assertIn('-Old preference', proposals[0]['diff'])
        self.assertIn('+New preference', proposals[0]['diff'])
        runner.approve = lambda _: True
        self.assertTrue(runner.execute('remember', '{"content":"New preference"}')['remembered'])
        self.assertEqual(store.memory(), 'New preference')

    def test_diff_approval_and_concurrent_file_change(self):
        target = self.root / 'note.txt'
        target.write_text('before\n')
        proposals = []
        def approve(proposal):
            proposals.append(proposal)
            target.write_text('edited externally\n')
            return True
        result = ToolRunner(self.root, approve).execute('write_file', '{"path":"note.txt","content":"after\\n"}')
        self.assertIn('-before', proposals[0]['diff'])
        self.assertIn('+after', proposals[0]['diff'])
        self.assertIn('changed during approval', result['error'])
        self.assertEqual(target.read_text(), 'edited externally\n')

    def test_attachments_reach_model_and_restore_metadata(self):
        provider = self.provider()
        self.launch(OpenAIClient('vision-test', provider.base))
        image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZkAAAAASUVORK5CYII=')
        self.request('/api/chat', {'prompt': 'Review these', 'attachments': [encoded('code.py', b'print("hello")'), encoded('pixel.png', image)]})
        self.until(lambda s: not s['busy'])
        content = provider.requests[-1]['messages'][-1]['content']
        self.assertIn('print("hello")', content[1]['text'])
        self.assertEqual(content[-1]['type'], 'image_url')
        self.request('/api/session', {'name': 'other'})
        self.request('/api/session', {'name': 'default'})
        message = self.status()['messages'][0]
        self.assertEqual(message['text'], 'Review these')
        self.assertEqual([f['name'] for f in message['attachments']], ['code.py', 'pixel.png'])
        self.assertNotIn('base64', json.dumps(self.status()))

    def test_attachment_validation_and_pdf_extraction(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 20 100 Td (Project roadmap) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        output = io.BytesIO()
        writer.write(output)
        content, meta = attachment_content('read', [encoded('roadmap.pdf', output.getvalue())])
        self.assertIn('Project roadmap', content[1]['text'])
        self.assertEqual(meta[0]['kind'], 'pdf')
        for files in [[encoded('.env', b'secret')], [encoded('binary', b'\0binary')], [{'name': 'bad', 'data': '*'}], [encoded('too-big', b'a' * 4_000_001)]]:
            with self.assertRaises(ValueError):
                attachment_content('read', files)

    def test_streaming_fragments_text_and_tool_arguments(self):
        def behavior(payload):
            if payload['messages'][-1]['role'] == 'tool':
                yield 'File '
                yield 'created.'
            else:
                yield {'delta': {'content': 'I will write it.'}}
                yield {'delta': {'tool_calls': [{'index': 0, 'id': 'call-write', 'function': {'name': 'write_', 'arguments': '{"path":"result.txt",'}}]}}
                yield {'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'file', 'arguments': '"content":"created"}'}}]}}
                yield {'delta': {}, 'finish_reason': 'tool_calls'}
        provider = self.provider(behavior)
        self.launch(OpenAIClient('test', provider.base))
        self.request('/api/chat', {'prompt': 'write'})
        state = self.until(lambda s: s['approval'] is not None)
        self.assertIn('+created', state['approval']['diff'])
        self.request('/api/approve', {'id': state['approval']['id'], 'allow': True})
        state = self.until(lambda s: not s['busy'])
        self.assertEqual((self.root / 'result.txt').read_text(), 'created')
        self.assertEqual(state['messages'][-1]['text'], 'File created.')
        self.assertEqual(provider.requests[-1]['messages'][-1]['role'], 'tool')

    def test_stop_stream_preserves_partial_history(self):
        def behavior(payload):
            yield 'First visible words. '
            for _ in range(200):
                time.sleep(.02)
                yield 'More. '
        provider = self.provider(behavior)
        self.launch(OpenAIClient('slow-test', provider.base))
        self.request('/api/chat', {'prompt': 'slow answer'})
        self.until(lambda s: 'First visible' in s['streaming'])
        self.request('/api/stop', {})
        state = self.until(lambda s: not s['busy'])
        self.assertIn('Stopped by you', state['messages'][-1]['text'])
        self.assertEqual(state['streaming'], '')
        self.assertIn('First visible', Session(self.root, 'default').messages[-1]['content'])

    def test_stop_pending_approval_never_writes(self):
        class Writer:
            def complete(self, messages):
                return tool_call('write_file', {'path': 'no.txt', 'content': 'no'})
        self.launch(Writer())
        self.request('/api/chat', {'prompt': 'write'})
        self.until(lambda s: s['approval'] is not None)
        self.request('/api/stop', {})
        self.until(lambda s: not s['busy'])
        self.assertFalse((self.root / 'no.txt').exists())
        saved = Session(self.root, 'default').messages
        self.assertEqual(saved[-1]['role'], 'tool')
        self.assertIn('denied', saved[-1]['content'])

    def test_stop_running_command_kills_process(self):
        cancel = Cancellation()
        runner = ToolRunner(self.root, lambda _: True, cancel=cancel)
        result = []
        thread = threading.Thread(target=lambda: result.append(runner.execute('run_command', '{"command":"sleep 10; touch should-not-exist"}')))
        thread.start()
        time.sleep(.1)
        cancel.stop()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result[0]['cancelled'])
        self.assertFalse((self.root / 'should-not-exist').exists())


if __name__ == '__main__':
    unittest.main()
