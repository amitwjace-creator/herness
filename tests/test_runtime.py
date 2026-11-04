import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from herness.core import Agent, Cancellation, ModelError, OpenAIClient, Session
from herness.providers import create_client
from herness.models import ModelSettings
from herness.web import Workspace
from herness.runtime import RuntimeLedger, context_snapshot, redact
from herness.privacy import Privacy
from herness.state import ProjectStore
from herness.tools import ToolRunner


def call(name='read_file', args=None, identity='c1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args or {'path': 'hello.txt'})}}]}


class SequenceClient:
    model = 'test-model'
    def __init__(self, replies):
        self.replies, self.count, self.histories = replies, 0, []
        self.last_usage = {'input_tokens': 100, 'output_tokens': 20}
    def complete(self, messages):
        self.histories.append(json.loads(json.dumps(messages)))
        reply = self.replies[min(self.count, len(self.replies) - 1)]
        self.count += 1
        return reply


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'hello.txt').write_text('hello')
        self.session = Session(self.root, 'runtime')
        self.runner = ToolRunner(self.root, lambda _: True)
    def agent(self, client, **options):
        return Agent(client, self.runner, self.session, lambda _: None, **options)

    def test_plan_enforced_even_if_provider_requests_write(self):
        client = SequenceClient([call('write_file', {'path': 'danger.txt', 'content': 'write'}), {'role': 'assistant', 'content': 'Plan ready'}])
        agent = self.agent(client, mode='plan')
        self.assertEqual(agent.run('inspect'), 'Plan ready')
        self.assertFalse((self.root / 'danger.txt').exists())
        self.assertTrue(json.loads(self.session.messages[3]['content'])['denied'])
        self.assertNotIn('write_file', [schema['function']['name'] for schema in client.tool_schemas])
        self.assertTrue(any(event['kind'] == 'usage' for event in agent.record['events']))

    def test_context_sources_and_usage_records(self):
        (self.root / 'AGENTS.md').write_text('Run useful checks.')
        ProjectStore(self.root).save_memory('Prefer concise updates.')
        client = SequenceClient([{'role': 'assistant', 'content': 'Ready'}])
        agent = self.agent(client, project={'instructions': 'Use Python.'}, runtime_options={'input_usd_per_million': 2, 'output_usd_per_million': 4})
        agent.run('private prompt sk-this_is_a_long_secret_key')
        system = client.histories[0][0]['content']
        self.assertIn('Use Python.', system)
        self.assertIn('Run useful checks.', system)
        self.assertIn('Prefer concise updates.', system)
        snapshot = context_snapshot(self.root, self.session.messages, {'instructions': 'Use Python.'})
        self.assertEqual({s['kind'] for s in snapshot['sources']}, {'project', 'repository', 'memory'})
        self.assertGreater(snapshot['estimated_tokens'], 0)
        self.assertNotIn('private prompt', json.dumps(snapshot['messages']))
        record = RuntimeLedger(self.root).get_run(agent.record['id'])
        self.assertEqual(record['usage']['input_tokens'], 100)
        self.assertAlmostEqual(record['usage']['estimated_usd'], .00028)
        self.assertNotIn('private prompt', json.dumps(record))
        self.assertEqual(record['status'], 'completed')

    def test_structured_memory_only_includes_active_unconflicted_entries(self):
        privacy = Privacy(self.root)
        for label, content, extra in [('style', 'Keep replies brief', {}),
                                       ('old', 'Expired content', {'expires_at': 1}),
                                       ('private', 'Excluded content', {'excluded': True}),
                                       ('choice', 'Conflicting A', {}), ('choice', 'Conflicting B', {})]:
            privacy.mutate_memory({'revision': privacy.memory()['revision'], 'action': 'save',
                                   'label': label, 'content': content, **extra})
        client = SequenceClient([{'role': 'assistant', 'content': 'Ready'}])
        self.agent(client).run('use memory')
        system = client.histories[0][0]['content']
        self.assertIn('Keep replies brief', system)
        for excluded in ('Expired content', 'Excluded content', 'Conflicting A', 'Conflicting B'):
            self.assertNotIn(excluded, system)

    def test_pinned_context_is_bounded_and_rejects_private_paths(self):
        (self.root / 'reference.txt').write_text('trusted reference' + 'x' * 20_000)
        (self.root / '.env').write_text('secret credential')
        (self.root / 'alias.txt').symlink_to(self.root / '.env')
        (self.root / 'ignored.txt').write_text('ignored content')
        project = {'goals': 'Ship a useful web app', 'pinned_files': ['reference.txt', '.env', 'alias.txt', '../outside', 'ignored.txt'],
                   'ignored_paths': ['ignored.txt']}
        client = SequenceClient([{'role': 'assistant', 'content': 'Ready'}])
        self.agent(client, project=project).run('inspect the context')
        system = client.histories[0][0]['content']
        self.assertIn('Ship a useful web app', system)
        self.assertIn('trusted reference', system)
        self.assertIn('File context truncated', system)
        self.assertNotIn('secret credential', system)
        self.assertNotIn('ignored content', system)
        snapshot = context_snapshot(self.root, self.session.messages, project)
        self.assertEqual(sum(source.get('excluded', False) for source in snapshot['sources']), 4)

    def test_budget_and_context_limits_prevent_provider_call(self):
        for options in ({'max_context_tokens': 1}, {'max_cost_usd': .000001, 'input_usd_per_million': 10}):
            with self.subTest(options=options):
                client = SequenceClient([{'role': 'assistant', 'content': 'unreachable'}])
                with self.assertRaises(ModelError):
                    self.agent(client, runtime_options=options).run('large context')
                self.assertEqual(client.count, 0)
        with self.assertRaises(ValueError):
            self.agent(SequenceClient([]), runtime_options={'max_cost_usd': float('nan')})

    def test_identical_tool_loop_stops_before_fourth_execution(self):
        client = SequenceClient([call()])
        agent = self.agent(client)
        with patch.object(self.runner, 'execute', wraps=self.runner.execute) as execute:
            with self.assertRaisesRegex(ModelError, 'repeated identical'):
                agent.run('read repeatedly')
        self.assertEqual(execute.call_count, 3)
        self.assertEqual(client.count, 4)
        record = agent.ledger.get_run(agent.record['id'])
        self.assertTrue(record['checkpoint']['result_saved'])
        self.assertEqual(record['status'], 'failed')

    def test_tool_arguments_and_results_excluded_from_trace(self):
        client = SequenceClient([call('write_file', {'path': 'private.txt', 'content': 'private content'}), {'role': 'assistant', 'content': 'Saved'}])
        agent = self.agent(client)
        agent.run('personal information')
        saved = agent.ledger.get_run(agent.record['id'])
        self.assertNotIn('private content', json.dumps(saved))
        self.assertNotIn('personal information', json.dumps(saved))
        self.assertEqual(saved['checkpoint']['tool'], 'write_file')
        self.assertEqual(redact({'api_key': 'sensitive', 'detail': 'Bearer an_auth_token'}), {'api_key': '[REDACTED]', 'detail': '[REDACTED]'})

    def test_recovery_closes_pending_batch_without_replaying(self):
        self.session.messages.extend([{'role': 'user', 'content': 'old request'}, call('write_file', {'path': 'never.txt', 'content': 'no'})])
        self.session.save()
        restored = Session(self.root, 'runtime')
        self.assertEqual(len(restored.messages), 3)  # Reading a session cannot mutate an active run.
        self.session = restored
        client = SequenceClient([{'role': 'assistant', 'content': 'Review the previous action first.'}])
        agent = self.agent(client)
        agent.run('what happened?')
        self.assertFalse((self.root / 'never.txt').exists())
        recovered = client.histories[0][3]
        self.assertEqual(recovered['tool_call_id'], 'c1')
        self.assertIn('not replayed', recovered['content'])
        self.assertTrue(any(event['kind'] == 'recovery' for event in agent.record['events']))

    def test_abandoned_run_is_marked_without_replay(self):
        ledger = RuntimeLedger(self.root)
        record = ledger.begin('runtime', 'test', 'act')
        with patch('herness.runtime.os.kill', side_effect=ProcessLookupError):
            self.assertEqual(ledger.recover_abandoned(), [record['id']])
        self.assertEqual(ledger.get_run(record['id'])['status'], 'interrupted')


class ProviderContractTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.replies = []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                outer.requests.append((self.path, dict(self.headers), None))
                status, reply = outer.replies.pop(0)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode())
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append((self.path, dict(self.headers), payload))
                status, reply = outer.replies.pop(0)
                self.send_response(status)
                self.send_header('Content-Type', 'text/event-stream' if isinstance(reply, list) else 'application/json')
                if status == 429:
                    self.send_header('Retry-After', '0')
                self.end_headers()
                body = ''.join('data: ' + json.dumps(item) + '\n\n' for item in reply) if isinstance(reply, list) else json.dumps(reply)
                self.wfile.write(body.encode())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port) + '/v1'
        self.messages = [{'role': 'system', 'content': 'Be helpful'}, {'role': 'user', 'content': 'read a file'}]
    def client(self, protocol):
        return create_client(protocol, 'test-model', self.base, 'secret-key')

    def test_native_complete_and_tool_roundtrip_contracts(self):
        cases = [
            ('anthropic', {'content': [{'type': 'tool_use', 'id': 'a1', 'name': 'read_file', 'input': {'path': 'hello.txt'}}], 'usage': {'input_tokens': 10, 'output_tokens': 3}}, '/v1/messages', 'X-Api-Key'),
            ('gemini', {'candidates': [{'content': {'parts': [{'functionCall': {'name': 'read_file', 'args': {'path': 'hello.txt'}}, 'thoughtSignature': 'opaque-signature'}]}, 'finishReason': 'STOP'}], 'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 3}}, '/v1/models/test-model:generateContent', 'X-Goog-Api-Key'),
            ('responses', {'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'r1', 'name': 'read_file', 'arguments': '{"path":"hello.txt"}'}], 'usage': {'input_tokens': 10, 'output_tokens': 3}}, '/v1/responses', 'Authorization')]
        for protocol, reply, endpoint, header in cases:
            with self.subTest(protocol=protocol):
                self.replies.append((200, reply))
                client = self.client(protocol)
                message = client.complete(self.messages)
                tool = message['tool_calls'][0]
                self.assertEqual(tool['function']['name'], 'read_file')
                self.assertEqual(client.last_usage['input_tokens'], 10)
                path, headers, payload = self.requests[-1]
                self.assertEqual(path, endpoint)
                self.assertIn('secret-key', headers[header])
                self.assertNotIn('secret-key', path)
                history = self.messages + [message, {'role': 'tool', 'tool_call_id': tool['id'], 'content': '{"content":"hello"}'}]
                replay = client.payload(history)
                if protocol == 'anthropic':
                    self.assertEqual(replay['messages'][-1]['content'][0]['tool_use_id'], tool['id'])
                    self.assertIn('input_schema', replay['tools'][0])
                elif protocol == 'gemini':
                    self.assertEqual(replay['contents'][-1]['parts'][0]['functionResponse']['name'], 'read_file')
                    self.assertEqual(replay['contents'][-2]['parts'][0]['thoughtSignature'], 'opaque-signature')
                else:
                    self.assertEqual(replay['input'][-1]['call_id'], tool['id'])
                    self.assertFalse(replay['store'])

    def test_native_streams_accumulate_text_tools_and_usage(self):
        cases = [
            ('anthropic', [
                {'type': 'message_start', 'message': {'usage': {'input_tokens': 5}}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Hello'}},
                {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'a1', 'name': 'read_file', 'input': {}}},
                {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"path":'}},
                {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '"hello.txt"}'}},
                {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 7}},
                {'type': 'message_stop'}]),
            ('gemini', [
                {'candidates': [{'content': {'parts': [{'text': 'hidden', 'thought': True}, {'text': 'Hello'}]}}]},
                {'candidates': [{'content': {'parts': [{'functionCall': {'name': 'read_file', 'args': {'path': 'hello.txt'}}}]}, 'finishReason': 'STOP'}], 'usageMetadata': {'promptTokenCount': 5, 'candidatesTokenCount': 7}}]),
            ('responses', [
                {'type': 'response.output_text.delta', 'delta': 'Hello'},
                {'type': 'response.completed', 'response': {'status': 'completed', 'output': [
                    {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Hello'}]},
                    {'type': 'function_call', 'call_id': 'r1', 'name': 'read_file', 'arguments': '{"path":"hello.txt"}'}],
                    'usage': {'input_tokens': 5, 'output_tokens': 7}}}])]
        for protocol, events in cases:
            with self.subTest(protocol=protocol):
                self.replies.append((200, events))
                client = self.client(protocol)
                deltas = []
                message = client.stream(self.messages, deltas.append, Cancellation())
                self.assertEqual(''.join(deltas), 'Hello')
                self.assertEqual(message['content'], 'Hello')
                self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments'])['path'], 'hello.txt')
                self.assertEqual(client.last_usage['output_tokens'], 7)
                self.assertNotIn('hidden', json.dumps(message))

    def test_native_image_conversion_and_truncated_stream_rejection(self):
        messages = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aGVsbG8='}}]}]
        for protocol in ('anthropic', 'gemini', 'responses'):
            with self.subTest(protocol=protocol):
                client = self.client(protocol)
                self.assertIn('aGVsbG8=', json.dumps(client.payload(messages)))
                self.replies.append((200, []))
                with self.assertRaisesRegex(ModelError, 'before completion'):
                    client.stream(self.messages, lambda _: None, Cancellation())

    def test_profiles_persist_without_keys_and_select_with_provider_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = Workspace(root, self.client('anthropic'))
            models = ModelSettings(root)
            profile = models.profile({'action': 'save', 'name': 'Fast model',
                                      'defaults': {'input_usd_per_million': 2}, 'api_key': 'do-not-store'}, state)['profile']
            self.assertNotIn('do-not-store', (root / '.herness/model-profiles.json').read_text())
            self.assertNotIn('secret-key', (root / '.herness/model-profiles.json').read_text())
            restored = ModelSettings(root)
            self.assertEqual(restored.list()['profiles'][0]['id'], profile['id'])
            state.configure({'protocol': 'responses', 'base_url': self.base + '/other', 'model': 'different'})
            self.assertEqual(state.client.api_key, '')
            restored.profile({'action': 'select', 'id': profile['id']}, state)
            self.assertEqual(state.client.protocol, 'anthropic')
            self.assertEqual(state.client.api_key, 'secret-key')
            self.assertEqual(state.run_options['input_usd_per_million'], 2)
            state.busy = True
            with self.assertRaisesRegex(ValueError, 'Finish the active'):
                restored.profile({'action': 'select', 'id': profile['id']}, state)

    def test_malformed_model_ids_and_protocols_are_validation_errors(self):
        for protocol in ([], {}, None):
            with self.assertRaises(ValueError):
                create_client(protocol, 'model', self.base)
        with tempfile.TemporaryDirectory() as temporary:
            models = ModelSettings(Path(temporary))
            for identity in ([], {}):
                with self.assertRaises(ValueError):
                    models.profile({'action': 'select', 'id': identity}, None)

    def test_model_discovery_uses_current_provider_and_credential_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Workspace(Path(temporary), self.client('gemini'))
            models = ModelSettings(Path(temporary))
            self.replies.append((200, {'models': [
                {'name': 'models/gemini-model', 'displayName': 'Gemini model', 'inputTokenLimit': 1000, 'supportedGenerationMethods': ['generateContent']},
                {'name': 'models/embedding', 'supportedGenerationMethods': ['embedContent']}], 'nextPageToken': 'next'}))
            result = models.handle('POST', '/api/models/discover', {'base_url': 'https://untrusted.example'}, state)
            self.assertEqual(result['models'], [{'id': 'gemini-model', 'name': 'Gemini model', 'context_tokens': 1000}])
            self.assertEqual(result['next_cursor'], 'next')
            path, headers, _ = self.requests[-1]
            self.assertEqual(path, '/v1/models')
            self.assertEqual(headers['X-Goog-Api-Key'], 'secret-key')
            self.assertNotIn('secret-key', json.dumps(result))

    def test_rate_limit_retry_and_nonretryable_error(self):
        self.replies.extend([(429, {'error': 'limited'}), (200, {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]})])
        client = OpenAIClient('test', self.base, 'key')
        self.assertEqual(client.complete(self.messages)['content'], 'OK')
        self.assertEqual(len(self.requests), 2)
        self.replies.append((401, {'error': 'denied'}))
        with self.assertRaisesRegex(ModelError, 'HTTP 401'):
            client.complete(self.messages)
        self.assertEqual(len(self.requests), 3)


if __name__ == '__main__':
    unittest.main()
