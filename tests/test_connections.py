import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from herness.connections import Connections, ConnectionError, public_destination


class ConnectionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.page = '<html><head><title>Research page</title><meta property="article:published_time" content="2026-01-02"></head><body><nav>Ignore navigation</nav><h1>Evidence</h1><p>A useful source paragraph.</p><script>secret script</script></body></html>'
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def reply(self, body, media='application/json', headers=None, status=200):
                raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', media)
                self.send_header('Content-Length', str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                if self.path == '/redirect':
                    return self.reply('', headers={'Location': '/page'}, status=302)
                if self.path == '/private-redirect':
                    return self.reply('', headers={'Location': 'https://169.254.169.254/latest/meta-data/'}, status=302)
                if self.path.startswith('/malformed-search'):
                    return self.reply({'web': {'results': 'invalid'}})
                if self.path.startswith('/search'):
                    owner.calls.append({'search_key': self.headers.get('X-Subscription-Token'), 'path': self.path})
                    return self.reply({'web': {'results': [
                        {'title': 'A', 'url': 'https://example.com/a', 'description': 'Trusted excerpt', 'page_age': '2026-01-01'},
                        {'title': 'B', 'url': 'https://other.example/b', 'description': 'Other excerpt'}]}})
                if self.path == '/binary':
                    return self.reply('binary', 'application/octet-stream')
                return self.reply(owner.page, 'text/html; charset=utf-8')
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append({'payload': payload, 'authorization': self.headers.get('Authorization'), 'session': self.headers.get('Mcp-Session-Id')})
                if self.path == '/redirect-api':
                    return self.reply('', headers={'Location': '/mcp'}, status=307)
                if self.path == '/tavily':
                    return self.reply({'results': [{'title': 'Tavily source', 'url': 'https://example.com/source', 'content': 'Source excerpt', 'published_date': '2026-01-02'}]})
                method = payload['method']
                if method == 'initialize':
                    result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'test', 'version': '1'}}
                    return self.reply({'jsonrpc': '2.0', 'id': payload['id'], 'result': result}, headers={'Mcp-Session-Id': 'test-session'})
                if method == 'notifications/initialized':
                    return self.reply('', status=204)
                if method == 'tools/list':
                    result = {'tools': [{'name': 'echo', 'description': 'Echo approved text', 'inputSchema': {'type': 'object'}}]}
                    if self.path == '/malformed-mcp':
                        result = []
                else:
                    result = {'content': [{'type': 'text', 'text': payload['params']['arguments']['text']}]}
                document = {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}
                if self.path == '/sse':
                    return self.reply('data: '+json.dumps(document)+'\n\n', 'text/event-stream')
                self.reply(document)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.connections = Connections(self.root, allow_local_fetch=True)

    def test_fetch_extracts_readable_text_and_citations(self):
        source = self.connections.fetch(self.base+'/redirect', 'Project')
        self.assertEqual(source['title'], 'Research page')
        self.assertEqual(source['published_at'], '2026-01-02')
        self.assertIn('A useful source paragraph.', source['text'])
        self.assertNotIn('Ignore navigation', source['text'])
        self.assertNotIn('secret script', source['text'])
        self.assertEqual(source['url'], self.base+'/page')
        self.assertTrue(source['citations'])
        self.assertEqual(Connections(self.root, allow_local_fetch=True).sources[source['id']]['collection'], 'Project')

    def test_source_refresh_export_delete(self):
        source = self.connections.fetch(self.base+'/page', 'Notes')
        self.page = '<title>Changed</title><p>Updated evidence.</p>'
        result = self.connections.source_action({'action': 'refresh', 'id': source['id']})
        self.assertTrue(result['changed'])
        exported = self.connections.source_action({'action': 'export', 'collection': 'Notes'})
        self.assertIn('Updated evidence', exported['content'])
        self.assertIn(self.base+'/page', exported['content'])
        self.connections.source_action({'action': 'delete', 'id': source['id']})
        self.assertEqual(self.connections.sources, {})

    def test_invalid_fetch_targets_and_media(self):
        for url in ['file:///etc/passwd', 'https://user:password@example.com/', 'http://external.example/']:
            with self.subTest(url=url), self.assertRaises(ConnectionError):
                self.connections.fetch(url)
        with self.assertRaises(ConnectionError):
            Connections(self.root).fetch(self.base+'/page')
        with self.assertRaises(ConnectionError):
            self.connections.fetch(self.base+'/binary')

    def test_private_fetch_destinations_and_redirects_are_blocked(self):
        for url in ['https://127.0.0.1/', 'https://[::1]/', 'https://10.0.0.1/', 'https://169.254.169.254/', 'https://localhost./', 'https://service.internal/']:
            with self.subTest(url=url), self.assertRaises(ConnectionError):
                public_destination(url)
        with patch('herness.connections.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.1.1', 443))]):
            with self.assertRaises(ConnectionError):
                public_destination('https://public-looking.example/')
        with self.assertRaises(ConnectionError):
            self.connections.fetch(self.base+'/private-redirect')

    def test_search_domain_filter_key_privacy_and_dates(self):
        self.connections.SEARCH_ENDPOINTS = {'brave': self.base+'/search', 'tavily': 'https://api.tavily.com/search'}
        self.connections.configure_search({'provider': 'brave', 'api_key': 'private-key'})
        result = self.connections.search({'query': 'facts', 'domains': ['example.com'], 'from_date': '2026-01-01', 'to_date': '2026-02-01'})
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['domain'], 'example.com')
        self.assertIn('freshness=', self.calls[-1]['path'])
        self.assertEqual(self.calls[-1]['search_key'], 'private-key')
        self.assertNotIn('private-key', json.dumps(self.connections.status()))
        self.assertNotIn('private-key', (self.root/'.herness/connections.json').read_text())
        for data in [{'query': 'test', 'from_date': 'bad'}, {'query': 'test', 'domains': ['https://example.com']}, {'query': ''}]:
            with self.assertRaises(ConnectionError):
                self.connections.search(data)

    def test_search_requires_key(self):
        with self.assertRaisesRegex(ConnectionError, 'search key'):
            self.connections.search({'query': 'question'})

    def test_tavily_request_and_result(self):
        self.connections.SEARCH_ENDPOINTS = {'brave': 'https://api.search.brave.com/res/v1/web/search', 'tavily': self.base+'/tavily'}
        self.connections.configure_search({'provider': 'tavily', 'api_key': 'tavily-secret'})
        result = self.connections.search({'query': 'sources', 'domains': ['EXAMPLE.COM'], 'from_date': '2026-01-01'})
        self.assertEqual(result['results'][0]['snippet'], 'Source excerpt')
        self.assertEqual(self.calls[-1]['authorization'], 'Bearer tavily-secret')
        self.assertEqual(self.calls[-1]['payload']['include_domains'], ['example.com'])
        self.assertEqual(self.calls[-1]['payload']['start_date'], '2026-01-01')

    def test_malformed_remote_responses_and_identifiers(self):
        self.connections.SEARCH_ENDPOINTS = {'brave': self.base+'/malformed-search', 'tavily': 'https://api.tavily.com/search'}
        self.connections.configure_search({'provider': 'brave', 'api_key': 'test'})
        with self.assertRaisesRegex(ConnectionError, 'invalid result list'):
            self.connections.search({'query': 'sources'})
        self.connections.configure_mcp({'name': 'test', 'url': self.base+'/malformed-mcp'})
        with self.assertRaisesRegex(ConnectionError, 'invalid tool list'):
            self.connections.mcp_call({'name': 'test', 'tool': 'echo', 'confirmed': True})
        with self.assertRaisesRegex(ConnectionError, 'server name'):
            self.connections.mcp_tools([])
        with self.assertRaisesRegex(ConnectionError, 'Source id'):
            self.connections.source_action({'action': 'delete', 'id': []})

    def test_mcp_lists_tools_and_requires_explicit_confirmation(self):
        self.connections.configure_mcp({'name': 'test', 'url': self.base+'/mcp', 'api_key': 'mcp-private'})
        tools = self.connections.mcp_tools('test')
        self.assertEqual(tools['tools'][0]['name'], 'echo')
        proposal = self.connections.mcp_call({'name': 'test', 'tool': 'echo', 'arguments': {'text': 'hello'}})
        self.assertTrue(proposal['approval_required'])
        self.assertFalse(any(c.get('payload', {}).get('method') == 'tools/call' for c in self.calls))
        result = self.connections.mcp_call({'name': 'test', 'tool': 'echo', 'arguments': {'text': 'hello'}, 'confirmed': True})
        self.assertEqual(result['result']['content'][0]['text'], 'hello')
        call = next(c for c in self.calls if c.get('payload', {}).get('method') == 'tools/call')
        self.assertEqual(call['authorization'], 'Bearer mcp-private')
        self.assertEqual(call['session'], 'test-session')
        self.assertNotIn('mcp-private', (self.root/'.herness/connections.json').read_text())
        self.assertNotIn('mcp-private', json.dumps(self.connections.status()))

    def test_mcp_denial_unknown_tool_and_disable(self):
        self.connections.configure_mcp({'name': 'test', 'url': self.base+'/mcp'})
        result = self.connections.mcp_call({'name': 'test', 'tool': 'echo', 'arguments': {'text': 'hello'}}, approve=lambda _: False)
        self.assertTrue(result['denied'])
        self.assertFalse(any(c.get('payload', {}).get('method') == 'tools/call' for c in self.calls))
        with self.assertRaisesRegex(ConnectionError, 'not listed'):
            self.connections.mcp_call({'name': 'test', 'tool': 'unknown', 'arguments': {}, 'confirmed': True})
        self.connections.configure_mcp({'name': 'test', 'url': self.base+'/mcp', 'enabled': False})
        with self.assertRaisesRegex(ConnectionError, 'Enable'):
            self.connections.mcp_tools('test')

    def test_mcp_finite_sse_and_no_credential_redirect(self):
        self.connections.configure_mcp({'name': 'sse', 'url': self.base+'/sse'})
        self.assertEqual(self.connections.mcp_tools('sse')['tools'][0]['name'], 'echo')
        self.connections.configure_mcp({'name': 'redirect', 'url': self.base+'/redirect-api', 'api_key': 'secret'})
        with self.assertRaisesRegex(ConnectionError, '307'):
            self.connections.mcp_tools('redirect')

    def test_destination_change_drops_key_and_handlers_work(self):
        self.connections.configure_mcp({'name': 'test', 'url': self.base+'/mcp', 'api_key': 'secret'})
        self.connections.configure_mcp({'name': 'test', 'url': self.base+'/sse'})
        self.assertFalse(self.connections.mcp_public('test')['key_present'])
        self.assertEqual(self.connections.handle('GET', '/api/connections/status', {})['sources'], 0)
        result = self.connections.handle('POST', '/api/connections/fetch-many', {'urls': [self.base+'/page', 'file:///etc/passwd']})
        self.assertIn('source', result['results'][0])
        self.assertIn('error', result['results'][1])
        self.assertIsNone(self.connections.handle('GET', '/api/other', {}))


if __name__ == '__main__':
    unittest.main()
