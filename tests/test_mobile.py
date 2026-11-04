"""Native bearer transport uses the real server without weakening browser auth.

These tests connect to loopback, as an HTTPS reverse proxy would. They do not
claim to provision a proxy, exercise iOS Keychain, or test remote TLS itself.
"""
import base64
import contextlib
import http.client
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from herness.cli import DemoClient, main
from herness.mobile import MobilePolicy, generate_mobile_key, read_mobile_key, validate_public_origin
from herness.web import make_server


KEY = 'A' * 43
OTHER_KEY = 'B' * 43
ORIGIN = 'https://native.example.test'


class FastDemo:
    """Use the real offline tool loop without artificial streaming delays."""
    complete = DemoClient.complete


class FileWriter:
    def complete(self, messages):
        if messages[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': 'Tool finished: ' + messages[-1]['content']}
        return {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'native-write', 'type': 'function', 'function': {'name': 'write_file',
            'arguments': json.dumps({'path': 'native-note.txt', 'content': 'Approved by the native client.'})}}]}


def headers(*pairs):
    result = Message()
    for name, value in pairs:
        result[name] = value
    return result


class MobilePolicyTest(unittest.TestCase):
    def test_https_origins_are_canonical_without_changing_authority(self):
        cases = {'https://NATIVE.example.test:443/': ORIGIN,
                 'https://native.example.test:8443/': ORIGIN + ':8443',
                 'https://localhost/': 'https://localhost',
                 'https://127.0.0.1:8443/': 'https://127.0.0.1:8443',
                 'https://[2001:db8::1]:443/': 'https://[2001:db8::1]'}
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(validate_public_origin(original), expected)

    def test_rejects_insecure_ambiguous_and_non_origin_urls(self):
        invalid = [None, 3, '', 'http://native.example.test', 'ftp://native.example.test',
                   '//native.example.test', 'https://', 'https://user:password@native.example.test',
                   'https://native.example.test/path', 'https://native.example.test//',
                   'https://native.example.test?token=secret', 'https://native.example.test?',
                   'https://native.example.test#fragment', 'https://native.example.test#',
                   'https://native.example.test:0', 'https://native.example.test:65536',
                   'https://native.example.test:abc', 'https://native.example.test:',
                   'https://native.example.test\n', ' https://native.example.test',
                   'https://native.example.test\\evil.test', 'https://native.example.test.',
                   'https://bad_host.test', 'https://éxample.test', 'https://[fe80::1%eth0]',
                   'https://' + 'a' * 2000]
        for origin in invalid:
            with self.subTest(origin=str(origin)[:70]), self.assertRaises(ValueError):
                validate_public_origin(origin)

    def test_device_key_format_is_required_before_server_use(self):
        for key in [None, 4, '', 'a' * 42, 'a' * 44, 'a' * 42 + '=', 'a' * 42 + '/', KEY + '\n', ' ' + KEY]:
            with self.subTest(key=repr(key)[:55]), self.assertRaises(ValueError):
                MobilePolicy(ORIGIN, key)
        self.assertEqual(MobilePolicy(ORIGIN, KEY).info()['protocol'], 'herness-mobile')

    def test_only_one_host_and_authorization_are_accepted(self):
        policy = MobilePolicy(ORIGIN, KEY)
        valid = [('Host', 'native.example.test'), ('Authorization', 'Bearer ' + KEY)]
        self.assertTrue(policy.authorized(headers(*valid), 8766))
        for additions in [[('Host', 'native.example.test')], [('Authorization', 'Bearer ' + KEY)],
                          [('Origin', '')], [('Sec-Fetch-Site', '')], [('X-Herness-Token', '')]]:
            with self.subTest(additions=additions):
                self.assertFalse(policy.authorized(headers(*valid, *additions), 8766))

    def test_proxy_authority_and_loopback_host_allowlist(self):
        policy = MobilePolicy(ORIGIN, KEY)
        for host in ['native.example.test', 'NATIVE.EXAMPLE.TEST', 'native.example.test:443',
                     '127.0.0.1:8766', 'localhost:8766', '[::1]:8766']:
            self.assertTrue(policy.valid_host(headers(('Host', host)), 8766), host)
        for host in ['', 'evil.example.test', 'native.example.test.evil', 'native.example.test:80',
                     '127.0.0.1:8765', 'native.example.test.', 'native.example.test, evil.test']:
            self.assertFalse(policy.valid_host(headers(('Host', host)), 8766), host)
        policy = MobilePolicy(ORIGIN + ':8443', KEY)
        self.assertTrue(policy.valid_host(headers(('Host', 'native.example.test:8443')), 8766))
        self.assertFalse(policy.valid_host(headers(('Host', 'native.example.test')), 8766))


class MobileKeyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'native.key'

    def test_generated_key_is_private_random_readable_and_exclusive(self):
        result = generate_mobile_key(self.path)
        self.assertEqual(result, self.path)
        first = read_mobile_key(self.path)
        self.assertRegex(first, r'^[A-Za-z0-9_-]{43}$')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            generate_mobile_key(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        second = self.root / 'second.key'
        generate_mobile_key(second)
        self.assertNotEqual(read_mobile_key(second), first)

    def test_format_allows_only_43_ascii_characters_and_one_optional_newline(self):
        self.path.write_bytes(KEY.encode())
        self.path.chmod(0o600)
        self.assertEqual(read_mobile_key(self.path), KEY)
        self.path.write_bytes((KEY + '\n').encode())
        self.path.chmod(0o400)
        self.assertEqual(read_mobile_key(self.path), KEY)
        self.path.chmod(0o600)
        for raw in [b'', b'a' * 42, b'a' * 45, (KEY + '\r\n').encode(), (KEY + ' ').encode(),
                    b'a' * 42 + b'\xff', b'a' * 42 + b'=', b'a' * 42 + b'/', (KEY + '\n\n').encode()]:
            self.path.write_bytes(raw)
            with self.subTest(raw=repr(raw)[:60]), self.assertRaises(ValueError):
                read_mobile_key(self.path)

    def test_rejects_public_or_executable_file_permissions(self):
        generate_mobile_key(self.path)
        for mode in [0o644, 0o640, 0o660, 0o700, 0o666, 0o604]:
            self.path.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(ValueError, 'permissions'):
                read_mobile_key(self.path)
        self.path.chmod(0o600)
        self.assertEqual(read_mobile_key(self.path), self.path.read_text().strip())

    def test_rejects_symlink_file_symlink_parent_and_hard_links(self):
        target = self.root / 'original.key'
        generate_mobile_key(target)
        original = target.read_bytes()
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            read_mobile_key(self.path)
        with self.assertRaises((ValueError, OSError)):
            generate_mobile_key(self.path)
        self.assertEqual(target.read_bytes(), original)
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaisesRegex(ValueError, 'hard links'):
            read_mobile_key(self.path)
        self.path.unlink()
        linked = self.root / 'linked'
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            read_mobile_key(linked / target.name)
        with self.assertRaises(ValueError):
            generate_mobile_key(linked / 'new.key')
        self.assertFalse((self.root / 'new.key').exists())

    def test_rejects_special_files_and_missing_parent_without_creating_it(self):
        self.path.mkdir()
        with self.assertRaises((ValueError, OSError)):
            read_mobile_key(self.path)
        self.path.rmdir()
        if hasattr(os, 'mkfifo'):
            os.mkfifo(self.path, 0o600)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                read_mobile_key(self.path)
            self.path.unlink()
        with self.assertRaises((ValueError, OSError)):
            generate_mobile_key(self.root / 'missing' / 'new.key')
        self.assertFalse((self.root / 'missing').exists())

    @unittest.skipUnless(hasattr(os, 'geteuid'), 'POSIX ownership check')
    def test_key_owner_must_match_server_user(self):
        generate_mobile_key(self.path)
        with patch('herness.mobile.os.geteuid', return_value=self.path.stat().st_uid + 1):
            with self.assertRaisesRegex(ValueError, 'current server user'):
                read_mobile_key(self.path)

    def test_policy_validates_origin_before_reading_key_file(self):
        with patch('herness.mobile.read_mobile_key', side_effect=AssertionError('Must not read key')):
            with self.assertRaises(ValueError):
                MobilePolicy.from_key_file('http://insecure.example.test', self.path)
        generate_mobile_key(self.path)
        policy = MobilePolicy.from_key_file(ORIGIN, self.path)
        self.assertTrue(policy.authorized(headers(('Host', 'native.example.test'),
            ('Authorization', 'Bearer ' + read_mobile_key(self.path))), 8766))


class MobileServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def launch(self, client=None, native=True):
        options = {'mobile_policy': MobilePolicy(ORIGIN, KEY)} if native else {}
        server = make_server(self.root, client or FastDemo(), port=0, demo=True, **options)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server
        return server

    def request(self, path, data=None, *, method=None, bearer=KEY, host='native.example.test', extra=(), server=None):
        server = server or self.server
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        body = None if data is None else json.dumps(data).encode()
        try:
            connection.putrequest(method or ('POST' if data is not None else 'GET'), path,
                                  skip_host=True, skip_accept_encoding=True)
            if host is not None:
                connection.putheader('Host', host)
            if bearer is not None:
                connection.putheader('Authorization', 'Bearer ' + bearer)
            if body is not None:
                connection.putheader('Content-Type', 'application/json')
                connection.putheader('Content-Length', str(len(body)))
            for name, value in extra:
                connection.putheader(name, value)
            connection.endheaders(body)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), raw
        finally:
            connection.close()

    def json_request(self, path, data=None, status=200, **kwargs):
        actual, response_headers, raw = self.request(path, data, **kwargs)
        self.assertEqual(actual, status, (path, raw.decode(errors='replace')))
        self.assertTrue(response_headers['Content-Type'].startswith('application/json'))
        return json.loads(raw)

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.json_request('/api/status')
            if predicate(state):
                return state
            time.sleep(.01)
        self.fail('Native server did not reach the expected state')

    def test_native_info_chat_and_conversation_history_use_real_routes(self):
        server = self.launch()
        self.assertEqual(server.server_address[0], '127.0.0.1')
        self.assertNotEqual(server.workspace.token, KEY)
        info = self.json_request('/api/mobile/info')
        self.assertEqual((info['protocol'], info['version']), ('herness-mobile', 1))
        self.assertEqual(info['public_origin'], ORIGIN)
        self.assertFalse(self.json_request('/api/status')['busy'])
        self.json_request('/api/chat', {'prompt': 'Inspect the native workspace', 'expected_session': 'default'}, status=202)
        state = self.until(lambda state: not state['busy'])
        self.assertIn('Offline demo complete', state['messages'][-1]['text'])
        library = self.json_request('/api/library')
        self.assertEqual(library['sessions'][0]['name'], 'default')
        self.json_request('/api/session', {'name': 'native-second', 'expected_session': 'default'})
        self.assertEqual(self.json_request('/api/status')['messages'], [])
        self.json_request('/api/session', {'name': 'default', 'expected_session': 'native-second'})
        self.assertIn('Offline demo complete', self.json_request('/api/status')['messages'][-1]['text'])

    def test_native_knowledge_access_and_upload_keep_real_source_metadata(self):
        self.launch()
        before = self.json_request('/api/vectors')
        self.assertFalse(before['configured'])
        self.json_request('/api/documents/upload', {'name': 'native-evidence.txt',
            'data': base64.b64encode(b'Native clients can search this project evidence.').decode()})
        knowledge = self.json_request('/api/vectors')
        self.assertTrue(any(source['title'] == 'native-evidence.txt' for source in knowledge['available_sources']))
        self.assertEqual(self.json_request('/api/documents')['library'][0]['name'], 'native-evidence.txt')
        self.json_request('/api/vectors/search', {'query': 'project evidence'}, status=400)
        memory = self.json_request('/api/memory')
        self.json_request('/api/memory', {**memory, 'content': 'Native preference'})
        self.assertEqual(self.json_request('/api/memory')['content'], 'Native preference')

    def test_native_approval_requires_matching_authenticated_decision(self):
        self.launch(FileWriter())
        self.json_request('/api/chat', {'prompt': 'Write the note', 'expected_session': 'default'}, status=202)
        state = self.until(lambda state: state['approval'] is not None)
        identity = state['approval']['id']
        self.assertFalse((self.root / 'native-note.txt').exists())
        self.json_request('/api/approve', {'id': identity, 'allow': True, 'expected_session': 'default'}, status=403, bearer=OTHER_KEY)
        self.json_request('/api/approve', {'id': 'stale-id', 'allow': True, 'expected_session': 'default'}, status=409)
        self.json_request('/api/approve', {'id': identity, 'allow': True, 'expected_session': 'stale-session'}, status=409)
        self.assertEqual(self.json_request('/api/status')['approval']['id'], identity)
        self.assertFalse((self.root / 'native-note.txt').exists())
        self.json_request('/api/approve', {'id': identity, 'allow': False, 'expected_session': 'default'})
        self.until(lambda state: not state['busy'])
        self.assertFalse((self.root / 'native-note.txt').exists())
        self.json_request('/api/chat', {'prompt': 'Write with approval', 'expected_session': 'default'}, status=202)
        state = self.until(lambda state: state['approval'] is not None)
        self.json_request('/api/approve', {'id': state['approval']['id'], 'allow': True, 'expected_session': 'default'})
        self.until(lambda state: not state['busy'])
        self.assertEqual((self.root / 'native-note.txt').read_text(), 'Approved by the native client.')
        self.json_request('/api/approve', {'id': state['approval']['id'], 'allow': True, 'expected_session': 'default'}, status=409)

    def test_native_stop_cancels_pending_approval_without_writing(self):
        self.launch(FileWriter())
        self.json_request('/api/chat', {'prompt': 'Wait for my approval', 'expected_session': 'default'}, status=202)
        self.until(lambda state: state['approval'] is not None)
        self.json_request('/api/stop', {}, status=403, bearer=None)
        self.assertTrue(self.json_request('/api/status')['busy'])
        self.json_request('/api/stop', {'expected_session': 'stale-session'}, status=409)
        self.assertTrue(self.json_request('/api/status')['busy'])
        self.json_request('/api/stop', {'expected_session': 'default'})
        state = self.until(lambda state: not state['busy'])
        self.assertIsNone(state['approval'])
        self.assertFalse((self.root / 'native-note.txt').exists())

    def test_native_mutations_require_current_session_without_changing_state_on_failure(self):
        self.launch()
        actions = [('/api/chat', {'prompt': 'must not start'}), ('/api/stop', {}),
                   ('/api/approve', {'id': 'absent', 'allow': True}), ('/api/session', {'name': 'other'})]
        for path, data in actions:
            with self.subTest(path=path, expected='missing'):
                self.json_request(path, data, status=400)
            for invalid in [None, '', 4, [], {}]:
                with self.subTest(path=path, expected=invalid):
                    self.json_request(path, {**data, 'expected_session': invalid}, status=400)
            with self.subTest(path=path, expected='stale'):
                self.json_request(path, {**data, 'expected_session': 'stale-session'}, status=409)
        state = self.json_request('/api/status')
        self.assertEqual(state['session'], 'default')
        self.assertFalse(state['busy'])
        self.assertIsNone(state['approval'])
        self.assertEqual(state['messages'], [])
        self.json_request('/api/session', {'name': 'other', 'expected_session': 'default'})
        self.json_request('/api/chat', {'prompt': 'stale view must not write in other', 'expected_session': 'default'}, status=409)
        self.json_request('/api/session', {'name': 'default', 'expected_session': 'default'}, status=409)
        self.assertEqual(self.json_request('/api/status')['session'], 'other')
        self.assertEqual(self.json_request('/api/status')['messages'], [])

    def test_all_api_families_require_device_bearer_before_dispatch(self):
        self.launch()
        for path in ['/api/mobile/info', '/api/status', '/api/library', '/api/settings', '/api/vectors',
                     '/api/documents', '/api/privacy/memory', '/api/context', '/api/files', '/api/features']:
            with self.subTest(path=path):
                self.json_request(path, status=403, bearer=None)
        for path, data in [('/api/chat', {'prompt': 'must not start'}), ('/api/approve', {'id': 'none', 'allow': True}),
                           ('/api/settings', {'demo': False}), ('/api/memory', {'content': 'must not persist'}),
                           ('/api/documents/upload', {'name': 'no.txt', 'data': 'bm8='}),
                           ('/api/vectors/clear', {'confirmed': True}), ('/api/stop', {})]:
            with self.subTest(path=path):
                self.json_request(path, data, status=403, bearer=OTHER_KEY)
        self.assertEqual(self.json_request('/api/status')['messages'], [])
        self.assertEqual(self.json_request('/api/documents')['library'], [])
        self.assertEqual(self.json_request('/api/memory')['content'], '')

    def test_browser_tokens_provider_keys_and_ambiguous_bearers_are_not_device_auth(self):
        self.launch()
        token = self.server.workspace.token
        self.server.workspace.api_keys['https://provider.example.test'] = OTHER_KEY
        for bearer in [None, OTHER_KEY, token, KEY[:-1], KEY + 'A', KEY + ' ', ' ' + KEY]:
            with self.subTest(bearer=repr(bearer)[:10]):
                self.json_request('/api/status', status=403, bearer=bearer)
        for extra, bearer in [([('X-Herness-Token', token)], None), ([('X-Herness-Token', KEY)], None),
                              ([('X-Herness-Token', token)], KEY), ([('X-Herness-Token', '')], KEY),
                              ([('Authorization', 'Bearer ' + KEY)], KEY),
                              ([('Authorization', 'Basic ' + KEY)], None),
                              ([('Authorization', 'Bearer ' + KEY + ', Bearer ' + KEY)], None)]:
            with self.subTest(extra=extra, bearer=bearer is not None):
                self.json_request('/api/status', status=403, bearer=bearer, extra=extra)

    def test_all_origin_and_fetch_metadata_headers_are_rejected_for_get_and_post(self):
        self.launch()
        for name, value in [('Origin', ORIGIN), ('Origin', 'null'), ('Origin', ''),
                            ('Origin', f'http://127.0.0.1:{self.server.server_port}'),
                            ('Sec-Fetch-Site', 'same-origin'), ('Sec-Fetch-Mode', 'cors'),
                            ('Sec-Fetch-Dest', 'empty'), ('Sec-Fetch-User', '?1'), ('Sec-Fetch-Custom', '')]:
            for path, data in [('/api/status', None), ('/api/chat', {'prompt': 'browser request'})]:
                with self.subTest(name=name, value=value, path=path):
                    self.json_request(path, data, status=403, extra=[(name, value)])
        self.assertEqual(self.json_request('/api/status')['messages'], [])

    def test_public_and_loopback_hosts_work_but_forwarded_headers_never_bypass_host_checks(self):
        self.launch()
        for host in ['native.example.test', 'native.example.test:443',
                     f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}']:
            self.json_request('/api/status', host=host)
        for host in [None, '', 'evil.example.test', 'native.example.test.evil', 'native.example.test:80', '127.0.0.1:1']:
            with self.subTest(host=host):
                self.json_request('/api/status', status=403, host=host,
                    extra=[('X-Forwarded-Host', 'native.example.test'), ('X-Forwarded-Proto', 'https')])
        self.json_request('/api/status', status=403, extra=[('Host', 'native.example.test')])

    def test_native_server_never_serves_bootstrap_html_or_web_token(self):
        self.launch()
        for bearer in [None, KEY, OTHER_KEY]:
            code, response_headers, raw = self.request('/', bearer=bearer)
            self.assertEqual(code, 404)
            self.assertTrue(response_headers['Content-Type'].startswith('application/json'))
            combined = raw.decode() + json.dumps(response_headers)
            self.assertNotIn('<html', combined.lower())
            self.assertNotIn(self.server.workspace.token, combined)
            self.assertNotIn(KEY, combined)
            self.assertNotIn('nonce-', combined)
            self.assertNotIn('Access-Control-Allow-Origin', response_headers)
        self.json_request('/index.html', status=404)

    def test_native_unsupported_methods_are_json_and_authorized_before_method_dispatch(self):
        self.launch()
        for method in ['OPTIONS', 'DELETE', 'PUT', 'PATCH', 'TRACE']:
            with self.subTest(method=method):
                self.json_request('/api/status', method=method, status=403, bearer=None)
                self.json_request('/api/status', method=method, status=405)
                self.json_request('/api/status', method=method, status=403, extra=[('Origin', ORIGIN)])
        code, response_headers, raw = self.request('/api/status', method='HEAD')
        self.assertEqual(code, 405)
        self.assertEqual(raw, b'')
        self.assertTrue(response_headers['Content-Type'].startswith('application/json'))

    def test_default_web_server_keeps_local_bootstrap_and_browser_auth(self):
        server = self.launch(native=False)
        host = f'127.0.0.1:{server.server_port}'
        code, response_headers, raw = self.request('/', bearer=None, host=host)
        self.assertEqual(code, 200)
        self.assertTrue(response_headers['Content-Type'].startswith('text/html'))
        self.assertIn(server.workspace.token, raw.decode())
        self.json_request('/api/status', status=403, host=host)
        self.json_request('/api/status', bearer=None, host=host, extra=[('X-Herness-Token', server.workspace.token)])
        self.json_request('/api/status', bearer=None, host='native.example.test', status=403,
                          extra=[('X-Herness-Token', server.workspace.token)])
        self.json_request('/api/chat', {'prompt': 'not from this origin'}, bearer=None, host=host, status=403,
            extra=[('X-Herness-Token', server.workspace.token), ('Origin', ORIGIN)])


class MobileCLITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_key_command_writes_secret_without_printing_it_or_overwriting(self):
        key_file = self.root / 'cli.key'
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(main(['mobile-key', '--output', str(key_file)]), 0)
            original = key_file.read_bytes()
            self.assertEqual(main(['mobile-key', '--output', str(key_file)]), 1)
        self.assertEqual(key_file.read_bytes(), original)
        self.assertNotIn(read_mobile_key(key_file), output.getvalue() + errors.getvalue())
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertIn(str(key_file), output.getvalue())

    def test_mobile_parser_dispatches_explicit_key_origin_and_loopback_port(self):
        key_file = self.root / 'cli.key'
        generate_mobile_key(key_file)
        with patch('herness.mobile.serve_mobile') as serve:
            result = main(['--workspace', str(self.root), 'mobile', '--public-origin', ORIGIN,
                           '--key-file', str(key_file), '--port', '8769', '--demo'])
        self.assertEqual(result, 0)
        args = serve.call_args.args
        self.assertEqual(args[0], self.root)
        self.assertIsInstance(args[1], DemoClient)
        self.assertEqual(args[2:], (ORIGIN, str(key_file), 8769, True))

    def test_invalid_origin_fails_before_binding_or_exposing_key(self):
        key_file = self.root / 'cli.key'
        generate_mobile_key(key_file)
        errors = io.StringIO()
        with patch('herness.web.make_server') as make, contextlib.redirect_stderr(errors):
            result = main(['--workspace', str(self.root), 'mobile', '--public-origin', 'http://unsafe.test',
                           '--key-file', str(key_file), '--demo'])
        self.assertEqual(result, 1)
        make.assert_not_called()
        self.assertNotIn(read_mobile_key(key_file), errors.getvalue())
        self.assertIn('HTTPS', errors.getvalue())

    def test_mobile_requires_explicit_pairing_inputs_and_web_parser_stays_compatible(self):
        for arguments in [['mobile'], ['mobile', '--public-origin', ORIGIN], ['mobile-key']]:
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(arguments)
            self.assertEqual(caught.exception.code, 2)
        with patch('herness.web.serve') as serve:
            self.assertEqual(main(['--workspace', str(self.root), 'web', '--port', '8768', '--demo']), 0)
        self.assertEqual(serve.call_args.args[0], self.root)
        self.assertEqual(serve.call_args.args[2:], (8768, True))


if __name__ == '__main__':
    unittest.main()
