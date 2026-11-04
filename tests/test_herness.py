import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from herness.cli import DemoClient
from herness.core import Agent, ModelError, OpenAIClient, Session
from herness.tools import ToolRunner
from herness.web import make_server


def tool_call(name, args, identity="call-1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runner = ToolRunner(self.root, lambda _: True)

    def call(self, name, **args):
        return self.runner.execute(name, json.dumps(args))

    def test_write_read_and_list(self):
        self.assertEqual(self.call("write_file", path="notes/a.txt", content="hello")['bytes'], 5)
        self.assertEqual(self.call("read_file", path="notes/a.txt")['content'], "hello")
        self.assertIn("notes/", self.call("list_files", path=".")['entries'])

    def test_denied_actions_have_no_side_effects(self):
        self.runner.approve = lambda _: False
        self.assertTrue(self.call("write_file", path="no.txt", content="no")['denied'])
        self.assertTrue(self.call("run_command", command="touch no.txt")['denied'])
        self.assertFalse((self.root / "no.txt").exists())

    def test_path_escape_and_sensitive_files(self):
        (self.root / "outside").symlink_to(self.root.parent, target_is_directory=True)
        for path in ["../file", "/etc/passwd", "outside/file", ".env", ".git/config", ".herness/sessions/default.json", "key.pem"]:
            with self.subTest(path=path):
                self.assertIn('error', self.call("read_file", path=path))

    def test_sensitive_symlink_target(self):
        (self.root / ".env").write_text("secret")
        (self.root / "innocent").symlink_to(self.root / ".env")
        self.assertIn('error', self.call("read_file", path="innocent"))

    def test_bad_arguments_and_unknown_tool(self):
        for name, args in [("read_file", '{'), ("read_file", '[]'),
                           ("read_file", '{"path": 5}'), ("oops", '{}')]:
            self.assertIn('error', self.runner.execute(name, args))

    def test_shell_exit_status_and_output(self):
        result = self.call("run_command", command="printf 'hello'; exit 7")
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['output'], 'hello')
        self.assertFalse(result['timed_out'])

    def test_read_truncation(self):
        (self.root / "large").write_text("a" * 40_000)
        result = self.call("read_file", path="large")
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['content']), 32_000)

    def test_shell_timeout_and_output_limit(self):
        self.runner.command_timeout = .05
        result = self.call("run_command", command="sleep 5")
        self.assertTrue(result['timed_out'])
        self.assertNotEqual(result['exit_code'], 0)
        result = self.call("run_command", command="head -c 40000 /dev/zero")
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['output']), 32_000)

    def test_utf8_boundary_truncation(self):
        (self.root / 'unicode').write_text('a' * 31_999 + '💡' * 10)
        result = self.call('read_file', path='unicode')
        self.assertTrue(result['truncated'])
        self.assertNotIn('error', result)


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_real_tool_loop_and_restored_session(self):
        (self.root / "hello.txt").write_text("hi")
        session = Session(self.root, "test")
        result = Agent(DemoClient(), ToolRunner(self.root, lambda _: False), session, lambda _: None).run("list")
        self.assertIn("hello.txt", result)
        restored = Session(self.root, "test")
        self.assertEqual(session.messages, restored.messages)
        self.assertEqual([m['role'] for m in restored.messages], ['system', 'user', 'assistant', 'tool', 'assistant'])

    def test_step_limit(self):
        class Endless:
            def complete(self, messages):
                return tool_call("list_files", {"path": "."})
        session = Session(self.root, "test")
        answer = Agent(Endless(), ToolRunner(self.root, lambda _: False), session, lambda _: None, 2).run("go")
        self.assertIn("step limit", answer)
        self.assertEqual(sum(m['role'] == 'tool' for m in session.messages), 2)

    def test_interrupt_completes_tool_batch(self):
        class Client:
            def complete(self, messages):
                message = tool_call("run_command", {"command": "echo hi"})
                message['tool_calls'] += tool_call("write_file", {"path": "no", "content": "no"}, "call-2")['tool_calls']
                return message
        def interrupt(_):
            raise KeyboardInterrupt
        session = Session(self.root, "test")
        with self.assertRaises(KeyboardInterrupt):
            Agent(Client(), ToolRunner(self.root, interrupt), session, lambda _: None).run("go")
        self.assertEqual(sum(m['role'] == 'tool' for m in Session(self.root, "test").messages), 2)
        self.assertFalse((self.root / 'no').exists())

    def test_reject_session_traversal_and_symlink(self):
        with self.assertRaises(ValueError):
            Session(self.root, "../oops")
        (self.root / '.herness').symlink_to(self.root.parent)
        with self.assertRaises(ValueError):
            Session(self.root, "oops")


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def launch(self, client=None):
        self.server = make_server(self.root, client or DemoClient(), port=0, demo=client is None)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, data=None, token=True, headers=None):
        combined = {'Content-Type': 'application/json'}
        if token:
            combined['X-Herness-Token'] = self.server.workspace.token
        combined.update(headers or {})
        request = urllib.request.Request(self.url + path, headers=combined,
                                         data=json.dumps(data).encode() if data is not None else None)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read()

    def status(self):
        return json.loads(self.request('/api/status'))

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.status()
            if predicate(state):
                return state
            time.sleep(.01)
        self.fail('Agent did not reach expected state')

    def test_page_and_demo_chat(self):
        self.launch()
        html = self.request('/').decode()
        self.assertIn('aria-label="Message Herness"', html)
        self.assertNotIn('__TOKEN__', html)
        self.request('/api/chat', {'prompt': 'inspect'})
        state = self.until(lambda s: not s['busy'])
        self.assertIn('Offline demo complete', state['messages'][-1]['text'])
        self.assertTrue((self.root / '.herness/sessions/default.json').exists())

    def test_require_token_and_reject_foreign_origin_and_host(self):
        self.launch()
        for path, data, token, headers in [('/api/status', None, False, {}),
                ('/api/chat', {'prompt': 'bad'}, True, {'Origin': 'https://evil.example'}),
                ('/', None, False, {'Host': 'evil.example'})]:
            with self.subTest(path=path, headers=headers), self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path, data, token, headers)
            self.assertEqual(caught.exception.code, 403)

    def test_write_approval_busy_guard_and_session_switch(self):
        class Writer:
            def complete(self, messages):
                if messages[-1]['role'] == 'tool':
                    return {'role': 'assistant', 'content': 'Finished.'}
                return tool_call('write_file', {'path': 'note.txt', 'content': 'approved'})
        self.launch(Writer())
        self.request('/api/chat', {'prompt': 'write'})
        state = self.until(lambda s: s['approval'] is not None)
        self.assertFalse((self.root / 'note.txt').exists())
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/chat', {'prompt': 'overlap'})
        self.assertEqual(caught.exception.code, 409)
        self.request('/api/approve', {'id': state['approval']['id'], 'allow': True})
        self.until(lambda s: not s['busy'])
        self.assertEqual((self.root / 'note.txt').read_text(), 'approved')
        self.request('/api/session', {'name': 'other'})
        self.assertEqual(self.status()['messages'], [])
        self.request('/api/session', {'name': 'default'})
        self.assertEqual(self.status()['messages'][-1]['text'], 'Finished.')

    def test_denied_web_action(self):
        class Writer:
            def complete(self, messages):
                if messages[-1]['role'] == 'tool':
                    return {'role': 'assistant', 'content': messages[-1]['content']}
                return tool_call('write_file', {'path': 'no.txt', 'content': 'no'})
        self.launch(Writer())
        self.request('/api/chat', {'prompt': 'write'})
        state = self.until(lambda s: s['approval'] is not None)
        self.request('/api/approve', {'id': state['approval']['id'], 'allow': False})
        state = self.until(lambda s: not s['busy'])
        self.assertIn('denied', state['messages'][-1]['text'])
        self.assertFalse((self.root / 'no.txt').exists())

    def test_conversation_history_titles_and_reopen(self):
        self.launch()
        self.request('/api/chat', {'prompt': 'Help me plan my project'})
        self.until(lambda s: not s['busy'])
        self.request('/api/session', {'name': 'second'})
        self.request('/api/chat', {'prompt': 'Explore these files'})
        self.until(lambda s: not s['busy'])
        items = json.loads(self.request('/api/sessions'))['sessions']
        self.assertEqual([item['name'] for item in items], ['second', 'default'])
        self.assertEqual(items[0]['title'], 'Explore these files')
        self.assertGreater(items[0]['updated'], 0)
        self.request('/api/session', {'name': items[1]['name']})
        self.assertEqual(self.status()['messages'][0]['text'], 'Help me plan my project')

    def test_history_skips_empty_corrupt_and_symlink_sessions(self):
        self.launch()
        directory = self.server.workspace.session.directory
        (directory / 'broken.json').write_text('{')
        (directory / 'wrong.json').write_text('[]')
        (directory / 'wrongversion.json').write_text('{"version":9,"messages":[]}')
        (directory / 'empty.json').write_text('{"version":1,"messages":[]}')
        (directory / 'linked.json').symlink_to(directory / 'empty.json')
        self.assertEqual(json.loads(self.request('/api/sessions'))['sessions'], [])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/sessions', token=False)
        self.assertEqual(caught.exception.code, 403)


class ModelTransportTest(unittest.TestCase):
    def test_actual_http_transport_and_tool_round_trip(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers.get('Authorization'), payload))
                message = (tool_call('list_files', {'path': '.'}) if len(requests) == 1
                           else {'role': 'assistant', 'content': 'Verified project files.'})
                body = json.dumps({'choices': [{'message': message}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = OpenAIClient('test-model', f'http://127.0.0.1:{server.server_port}/v1', 'fake-test-key')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            answer = Agent(client, ToolRunner(root, lambda _: False), Session(root, 'test'), lambda _: None).run('inspect')
        self.assertEqual(answer, 'Verified project files.')
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][0], '/v1/chat/completions')
        self.assertEqual(requests[0][1], 'Bearer fake-test-key')
        self.assertEqual(requests[1][2]['messages'][-1]['role'], 'tool')
        self.assertEqual(requests[0][2]['model'], 'test-model')

    def test_remote_http_and_embedded_credentials_rejected(self):
        for base in ['http://example.com/v1', 'https://user:secret@example.com/v1', 'https://example.com/?token=secret']:
            with self.subTest(base=base), self.assertRaises(ValueError):
                OpenAIClient('test', base)


if __name__ == '__main__':
    unittest.main()
