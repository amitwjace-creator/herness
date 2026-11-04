"""HTTP authorization, input-error, and native-provider configuration checks."""
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_herness as existing


class APIBoundaryTest(unittest.TestCase):
    setUp = existing.ServerTest.setUp
    launch = existing.ServerTest.launch
    request = existing.ServerTest.request
    status = existing.ServerTest.status
    until = existing.ServerTest.until

    def test_extended_routes_require_auth_and_same_origin(self):
        self.launch()
        for path in ['/api/library', '/api/project', '/api/projects', '/api/documents',
                     '/api/automation/schedules', '/api/connections/status', '/api/run/options']:
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path, token=False)
            self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/project', {'title': 'Untrusted origin'}, headers={'Origin': 'https://untrusted.example'})
        self.assertEqual(caught.exception.code, 403)
        self.assertNotEqual(json.loads(self.request('/api/project'))['title'], 'Untrusted origin')

    def test_extended_http_routes_smoke_and_error_json(self):
        self.launch()
        artifact = json.loads(self.request('/api/documents/artifact', {'action': 'create', 'title': 'API test', 'content': 'Saved through HTTP'}))
        listing = json.loads(self.request('/api/documents'))
        self.assertEqual(listing['artifacts'][0]['id'], artifact['id'])
        self.assertEqual(json.loads(self.request('/api/connections/status'))['sources'], 0)
        self.assertEqual(json.loads(self.request('/api/automation/schedules'))['schedules'], [])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/export?session=missing')
        self.assertEqual(caught.exception.code, 400)
        self.assertIn('error', json.loads(caught.exception.read()))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/does-not-exist')
        self.assertEqual(caught.exception.code, 404)

    def test_malformed_route_identifiers_return_400_and_server_survives(self):
        self.launch()
        cases = [('/api/projects', {'action': 'switch', 'id': []}),
                 ('/api/projects', {'action': 'add', 'path': str(self.root), 'title': {}}),
                 ('/api/library', {'action': 'update', 'names': [{}]}),
                 ('/api/branch', {'index': []}),
                 ('/api/bookmark', {'index': [], 'enabled': True}),
                 ('/api/project', {'pinned_files': [True]}),
                 ('/api/files/read', {'path': []}),
                 ('/api/run/options', {'mode': []}),
                 ('/api/connections/mcp/list', {'name': []}),
                 ('/api/connections/search/config', {'provider': []}),
                 ('/api/connections/sources', {'action': 'delete', 'id': {}}),
                 ('/api/documents/artifact', {'action': 'get', 'id': []}),
                 ('/api/automation/schedules', {'action': 'delete', 'id': []}),
                 ('/api/settings', {'protocol': []})]
        for path, data in cases:
            with self.subTest(path=path, data=data):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.request(path, data)
                self.assertEqual(caught.exception.code, 400)
                self.assertIsInstance(json.loads(caught.exception.read())['error'], str)
        self.assertFalse(self.status()['busy'])

    def test_native_protocol_probe_settings_and_secret_redaction(self):
        captured = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append({'path': self.path, 'body': body, 'headers': dict(self.headers)})
                if self.path.endswith('/messages'):
                    reply = {'content': [{'type': 'text', 'text': 'OK'}], 'stop_reason': 'end_turn'}
                elif ':generateContent' in self.path:
                    reply = {'candidates': [{'content': {'parts': [{'text': 'OK'}]}, 'finishReason': 'STOP'}]}
                elif self.path.endswith('/responses'):
                    reply = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'OK'}]}]}
                else:
                    reply = {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]}
                raw = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        self.launch()
        endpoint = f'http://127.0.0.1:{provider.server_port}/v1'
        for protocol, suffix, payload_field in [('anthropic', '/messages', 'messages'),
                                                ('gemini', ':generateContent', 'contents'),
                                                ('responses', '/responses', 'input'),
                                                ('chat_completions', '/chat/completions', 'messages')]:
            with self.subTest(protocol=protocol):
                config = {'protocol': protocol, 'base_url': endpoint, 'model': 'local-test', 'demo': False, 'api_key': 'private-probe-key'}
                probe = json.loads(self.request('/api/settings/test', config))
                self.assertIn('successful', probe['message'])
                self.assertTrue(captured[-1]['path'].endswith(suffix))
                self.assertIn(payload_field, captured[-1]['body'])
                saved = self.request('/api/settings', config).decode()
                self.assertEqual(json.loads(saved)['protocol'], protocol)
                self.assertNotIn('private-probe-key', saved)
                self.assertNotIn('private-probe-key', (self.root / '.herness/settings.json').read_text())
        self.request('/api/settings', {'base_url': 'https://other.example/v1', 'model': 'test', 'demo': False})
        self.assertFalse(json.loads(self.request('/api/settings'))['key_present'])


if __name__ == '__main__':
    unittest.main()
