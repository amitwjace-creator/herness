import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from herness.core import Cancellation, TurnCancelled
from herness.embeddings import Embeddings, EmbeddingError


class EmbeddingsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requests = []
        self.responses = []
        self.entered = threading.Event()
        self.release = threading.Event()
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                fixture.requests.append({'path': self.path, 'headers': dict(self.headers), 'body': json.loads(body)})
                reply = fixture.responses.pop(0) if fixture.responses else {'json': {'embeddings': [[1, 0]]}}
                status = reply.get('status', 200)
                raw = reply.get('raw', json.dumps(reply.get('json', {})).encode())
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                for key, value in reply.get('headers', {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.flush()
                fixture.entered.set()
                if reply.get('pause'):
                    fixture.release.wait(3)
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.release.set)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.client = Embeddings(self.root)

    def configure(self, protocol='ollama', **fields):
        return self.client.configure({'protocol': protocol, 'base_url': self.base + ('/v1' if protocol == 'openai' else ''),
                                      'model': 'test-embedding', 'enabled': True, **fields})

    def test_disabled_defaults_require_explicit_enable(self):
        status = self.client.public()
        self.assertEqual(status['protocol'], 'ollama')
        self.assertEqual(status['model'], 'nomic-embed-text')
        self.assertFalse(status['enabled'])
        self.assertFalse(status['key_present'])
        with self.assertRaisesRegex(EmbeddingError, 'Enable'):
            self.client.embed(['private text'])
        self.assertEqual(self.requests, [])

    def test_ollama_real_request_and_batch_shapes(self):
        self.configure(batch_size=2)
        self.responses.extend([{'json': {'embeddings': [[1, 0, 0], [0, 1, 0]]}},
                               {'json': {'embeddings': [[0, 0, 1]]}}])
        result = self.client.embed(['first', 'second', 'third'])
        self.assertEqual(result, [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]])
        self.assertEqual([request['path'] for request in self.requests], ['/api/embed', '/api/embed'])
        self.assertEqual(self.requests[0]['body'], {'model': 'test-embedding', 'input': ['first', 'second'], 'truncate': False})
        self.assertEqual(self.requests[1]['body']['input'], ['third'])
        self.assertEqual(self.client.public()['dimensions'], 3)

    def test_openai_requests_float_vectors_and_reorders_indexes(self):
        self.configure('openai', api_key='embedding-only-secret')
        self.responses.append({'json': {'data': [{'index': 1, 'embedding': [.4, .6]}, {'index': 0, 'embedding': [.8, .2]}]}})
        self.assertEqual(self.client.embed(['first', 'second']), [[.8, .2], [.4, .6]])
        request = self.requests[0]
        self.assertEqual(request['path'], '/v1/embeddings')
        self.assertEqual(request['body']['encoding_format'], 'float')
        self.assertEqual(request['headers']['Authorization'], 'Bearer embedding-only-secret')
        self.assertNotIn('embedding-only-secret', json.dumps(request['body']))

    def test_credentials_never_persist_or_cross_destinations(self):
        self.configure('openai', api_key='embedding-secret')
        before = self.client.fingerprint()
        self.assertTrue(self.client.public()['key_present'])
        self.assertNotIn('embedding-secret', (self.root / '.herness/embedding-settings.json').read_text())
        self.assertNotIn('embedding-secret', json.dumps(self.client.public()))
        self.assertFalse(Embeddings(self.root).public()['key_present'])
        self.client.configure({'base_url': self.base + '/another'})
        self.assertFalse(self.client.public()['key_present'])
        self.responses.append({'json': {'data': [{'index': 0, 'embedding': [1, 2]}]}})
        self.client.embed(['test'])
        self.assertNotIn('Authorization', self.requests[-1]['headers'])
        self.assertNotEqual(before, self.client.fingerprint())
        self.client.configure({'base_url': self.base + '/v1'})
        self.assertTrue(self.client.public()['key_present'])
        self.assertEqual(before, self.client.fingerprint())
        self.client.configure({'clear_key': True})
        self.assertFalse(self.client.public()['key_present'])
        with patch.dict(os.environ, {'HERNESS_API_KEY': 'chat-secret', 'OPENAI_API_KEY': 'another-chat-secret'}):
            restored = Embeddings(self.root)
            self.assertFalse(restored.public()['key_present'])
            self.responses.append({'json': {'data': [{'index': 0, 'embedding': [1, 2]}]}})
            restored.embed(['test'])
            self.assertNotIn('Authorization', self.requests[-1]['headers'])

    def test_config_validation_is_atomic_and_rejects_untrusted_urls(self):
        invalid = [{'protocol': []}, {'protocol': 'fake'}, {'base_url': 'http://example.com/v1'},
                   {'base_url': 'https://user:password@example.com'}, {'base_url': 'https://example.com?key=secret'},
                   {'base_url': 'https://example.com/#fragment'}, {'base_url': 'https://example.com:bad'},
                   {'base_url': 'https://example.com\\@evil.com'}, {'base_url': 'https://example.com/\nheader'},
                   {'model': ''}, {'model': []}, {'model': '\ud800'}, {'enabled': 'true'}, {'batch_size': True}, {'batch_size': 65},
                   {'api_key': 'key\r\nBad: header'}, {'api_key': 'new', 'clear_key': True}, {'api_key': ['no']},
                   {'clear_key': 'yes'}, {'unknown_field': True}]
        before = self.client.public()
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(EmbeddingError):
                self.client.configure(config)
            self.assertEqual(self.client.public(), before)
        self.assertEqual(self.requests, [])

    def test_input_validation_precedes_any_request(self):
        self.configure()
        for texts in ('text', [], [1], [''], ['  '], ['x'] * 65, ['x' * 32_001], ['\ud800'], ['x' * 16_000] * 64):
            with self.subTest(kind=type(texts).__name__), self.assertRaises(EmbeddingError):
                self.client.embed(texts)
        self.assertEqual(self.requests, [])

    def test_dimensions_finite_numbers_and_nonzero_vectors_are_enforced(self):
        self.configure()
        malformed = [None, {}, {'embeddings': []}, {'embeddings': [[0, 0]]}, {'embeddings': [[True, 1]]},
                     {'embeddings': [['1', 2]]}, {'embeddings': [[float('nan'), 1]]},
                     {'embeddings': [[float('inf'), 1]]}, {'embeddings': [[1] * 4097]},
                     {'embeddings': [[1e308, 1e308, 1e308, 1e308]]}, {'error': 'private provider body'},
                     {'embedding': [1, 2]}]
        for response in malformed:
            self.responses.append({'json': response})
            with self.subTest(response=str(response)[:100]), self.assertRaises(EmbeddingError):
                self.client.embed(['text'])
            self.assertIsNone(self.client.public()['dimensions'])
        self.responses.append({'json': {'embeddings': [[1, 2]]}})
        self.client.embed(['valid'])
        self.responses.append({'json': {'embeddings': [[1, 2, 3]]}})
        with self.assertRaisesRegex(EmbeddingError, 'dimensions'):
            self.client.embed(['different dimension'])
        self.client.configure({'model': 'a-new-model'})
        self.responses.append({'json': {'embeddings': [[1, 2, 3]]}})
        self.assertEqual(self.client.embed(['new dimension']), [[1., 2., 3.]])

    def test_dimensions_must_match_across_internal_batches(self):
        self.configure(batch_size=1)
        self.responses.extend([{'json': {'embeddings': [[1, 0]]}}, {'json': {'embeddings': [[1, 0, 0]]}}])
        with self.assertRaisesRegex(EmbeddingError, 'dimensions'):
            self.client.embed(['first', 'second'])
        self.assertIsNone(self.client.public()['dimensions'])

    def test_openai_rejects_missing_duplicate_or_invalid_indexes(self):
        self.configure('openai')
        invalid = [[{'embedding': [1, 2]}, {'index': 1, 'embedding': [2, 1]}],
                   [{'index': 0, 'embedding': [1, 2]}, {'index': 0, 'embedding': [2, 1]}],
                   [{'index': False, 'embedding': [1, 2]}, {'index': 1, 'embedding': [2, 1]}],
                   [{'index': 0, 'embedding': [1, 2]}, {'index': 4, 'embedding': [2, 1]}],
                   [{'index': 0, 'embedding': [1, 2]}]]
        for data in invalid:
            self.responses.append({'json': {'data': data}})
            with self.assertRaises(EmbeddingError):
                self.client.embed(['a', 'b'])

    def test_provider_errors_and_redirects_do_not_expose_body_or_send_key(self):
        self.configure('openai', api_key='private-embedding-key')
        self.responses.append({'status': 302, 'headers': {'Location': self.base + '/credential-target'},
                               'raw': b'private-embedding-key'})
        with self.assertRaisesRegex(EmbeddingError, 'redirects are disabled'):
            self.client.embed(['text'])
        self.assertEqual(len(self.requests), 1)
        self.responses.append({'status': 401, 'raw': b'private-embedding-key provider diagnostic'})
        with self.assertRaises(EmbeddingError) as caught:
            self.client.embed(['text'])
        self.assertIn('HTTP 401', str(caught.exception))
        self.assertNotIn('private-embedding-key', str(caught.exception))
        self.assertNotIn('provider diagnostic', str(caught.exception))

    def test_response_byte_limit_and_invalid_json(self):
        self.configure()
        self.responses.append({'raw': b'x' * 101})
        with patch('herness.embeddings.MAX_RESPONSE_BYTES', 100), self.assertRaisesRegex(EmbeddingError, 'response exceeds'):
            self.client.embed(['text'])
        self.responses.append({'raw': b'{not json, contains private information'})
        with self.assertRaisesRegex(EmbeddingError, 'invalid JSON') as caught:
            self.client.embed(['text'])
        self.assertNotIn('private information', str(caught.exception))

    def test_cancellation_before_request_and_during_body_read(self):
        self.configure()
        cancelled = Cancellation()
        cancelled.stop()
        with self.assertRaises(TurnCancelled):
            self.client.embed(['text'], cancelled)
        self.assertEqual(self.requests, [])
        cancel = Cancellation()
        self.responses.append({'json': {'embeddings': [[1, 0]]}, 'pause': True})
        failures = []
        def execute():
            try:
                self.client.embed(['text'], cancel)
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=execute)
        thread.start()
        self.assertTrue(self.entered.wait(2))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with cancel.lock:
                if cancel.sockets:
                    break
            time.sleep(.01)
        cancel.stop()
        thread.join(2)
        self.release.set()
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], TurnCancelled)
        self.assertEqual(cancel.sockets, set())
        self.assertIsNone(self.client.public()['dimensions'])

    def test_cancellation_during_socket_registration_cleans_up(self):
        self.configure()
        cancel = Cancellation()
        original = cancel.register
        def stop_before_register(response):
            cancel.stop()
            return original(response)
        cancel.register = stop_before_register
        self.responses.append({'json': {'embeddings': [[1, 0]]}})
        with self.assertRaises(TurnCancelled):
            self.client.embed(['text'], cancel)
        self.assertEqual(cancel.sockets, set())

    def test_explicit_dimension_reset_accepts_replaced_model_tag(self):
        self.configure()
        fingerprint = self.client.fingerprint()
        self.responses.append({'json': {'embeddings': [[1, 0]]}})
        self.client.embed(['old model tag'])
        self.responses.append({'json': {'embeddings': [[1, 0, 0]]}})
        with self.assertRaisesRegex(EmbeddingError, 'dimensions'):
            self.client.embed(['replaced model tag'])
        self.client.reset_dimensions()
        self.assertIsNone(self.client.public()['dimensions'])
        self.assertEqual(self.client.fingerprint(), fingerprint)
        self.responses.append({'json': {'embeddings': [[1, 0, 0]]}})
        self.assertEqual(self.client.embed(['replaced model tag']), [[1., 0., 0.]])

    def test_dimension_reset_and_enable_toggle_invalidate_inflight_requests(self):
        self.configure()
        for change in ('reset', 'toggle'):
            with self.subTest(change=change):
                self.entered.clear()
                self.release.clear()
                self.responses.append({'json': {'embeddings': [[1, 0]]}, 'pause': True})
                failures = []
                def execute():
                    try:
                        self.client.embed(['old request'])
                    except Exception as exc:
                        failures.append(exc)
                thread = threading.Thread(target=execute)
                thread.start()
                self.assertTrue(self.entered.wait(2))
                if change == 'reset':
                    self.client.reset_dimensions()
                else:
                    self.client.configure({'enabled': False})
                    self.client.configure({'enabled': True})
                self.release.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(failures[0], EmbeddingError)
                self.assertIn('settings changed', str(failures[0]))
                self.assertIsNone(self.client.public()['dimensions'])

    def test_changing_model_during_request_rejects_stale_vectors(self):
        self.configure()
        self.responses.append({'json': {'embeddings': [[1, 0]]}, 'pause': True})
        failures = []
        def execute():
            try:
                self.client.embed(['text'])
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=execute)
        thread.start()
        self.assertTrue(self.entered.wait(2))
        self.client.configure({'model': 'changed-model'})
        self.release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIn('settings changed', str(failures[0]))
        self.assertIsNone(self.client.public()['dimensions'])


if __name__ == '__main__':
    unittest.main()
