import base64
import json
import unittest
from types import SimpleNamespace

from herness.agent_tools import ExtensionRunner
from herness.core import Agent, Cancellation, Session
from herness.documents import Documents
import test_connections
from test_herness import tool_call


class AgentServicesTest(unittest.TestCase):
    def setUp(self):
        test_connections.ConnectionsTest.setUp(self)
        self.documents = Documents(self.root)
        self.application = SimpleNamespace(modules=[self.connections, self.documents])
        self.proposals = []
        self.runner = ExtensionRunner(self.root, lambda value: self.proposals.append(value) or True,
                                      application=self.application)

    def call(self, tool_name, **args):
        return self.runner.execute(tool_name, json.dumps(args))

    def test_agent_can_fetch_search_and_read_uploaded_documents(self):
        fetched = self.call('web_fetch', url=self.base+'/page')
        self.assertIn('useful source', fetched['text'])
        self.assertTrue(fetched['citations'])
        self.connections.SEARCH_ENDPOINTS = {'brave': self.base+'/search', 'tavily': 'https://api.tavily.com/search'}
        self.connections.configure_search({'provider': 'brave', 'api_key': 'hidden-key'})
        searched = self.call('web_search', query='facts')
        self.assertTrue(searched['results'])
        self.assertNotIn('hidden-key', json.dumps(searched))
        uploaded = self.documents.upload({'name': 'source.txt', 'data': base64.b64encode(b'Primary evidence for the project').decode()})
        found = self.call('document_search', query='Primary evidence')
        self.assertEqual(found['results'][0]['id'], uploaded['id'])
        document = self.call('document_get', id=uploaded['id'])
        self.assertIn('Primary evidence', document['units'][0]['text'])
        self.assertEqual(self.proposals, [])

    def test_artifact_write_requires_approval_and_reads_are_available(self):
        self.runner.approve = lambda _: False
        denied = self.call('artifact_save', title='Draft', format='markdown', content='Private draft')
        self.assertTrue(denied['denied'])
        self.assertEqual(self.documents.handle('GET', '/api/documents', {})['artifacts'], [])
        self.runner.approve = lambda proposal: self.proposals.append(proposal) or True
        saved = self.call('artifact_save', title='Draft', format='markdown', content='Reviewed draft')
        self.assertEqual(self.proposals[-1]['content'], 'Reviewed draft')
        self.assertEqual(self.call('artifact_get', id=saved['id'])['content'], 'Reviewed draft')

    def test_mcp_denial_and_approval_with_destination_arguments(self):
        self.connections.configure_mcp({'name': 'test', 'url': self.base+'/mcp', 'api_key': 'hidden-mcp'})
        self.assertEqual(self.call('mcp_list', name='test')['tools'][0]['name'], 'echo')
        self.runner.approve = lambda _: False
        self.assertTrue(self.call('mcp_call', name='test', tool='echo', arguments={'text': 'Approved payload'})['denied'])
        self.assertFalse(any(c.get('payload', {}).get('method') == 'tools/call' for c in self.calls))
        self.runner.approve = lambda proposal: self.proposals.append(proposal) or True
        result = self.call('mcp_call', name='test', tool='echo', arguments={'text': 'Approved payload'})
        self.assertEqual(result['result']['content'][0]['text'], 'Approved payload')
        self.assertIn(self.base+'/mcp', self.proposals[-1]['description'])
        self.assertIn('Approved payload', self.proposals[-1]['description'])
        self.assertNotIn('hidden-mcp', json.dumps(result))

    def test_changed_mcp_destination_during_approval_prevents_action(self):
        self.connections.configure_mcp({'name': 'test', 'url': self.base+'/mcp', 'api_key': 'old-key'})
        def approval(_):
            self.connections.configure_mcp({'name': 'test', 'url': self.base+'/sse', 'api_key': 'new-key'})
            return True
        self.runner.approve = approval
        result = self.call('mcp_call', name='test', tool='echo', arguments={'text': 'Blocked'})
        self.assertIn('changed during approval', result['error'])
        self.assertFalse(any(c.get('payload', {}).get('method') == 'tools/call' for c in self.calls))

    def test_plan_and_scopes_hide_and_deny_mutations(self):
        planner = ExtensionRunner(self.root, lambda _: self.fail('Plan must not request write approval'),
                                  application=self.application, mode='plan')
        names = {item['function']['name'] for item in planner.schemas()}
        self.assertIn('web_fetch', names)
        self.assertIn('document_search', names)
        self.assertNotIn('artifact_save', names)
        self.assertNotIn('mcp_call', names)
        self.assertIn('error', planner.execute('artifact_save', json.dumps({'title': 'No', 'format': 'text', 'content': 'No'})))
        self.assertIn('error', planner.execute('mcp_call', json.dumps({'name': 'test', 'tool': 'echo', 'arguments': {}})))
        unavailable = ExtensionRunner(self.root, lambda _: True)
        self.assertNotIn('web_fetch', {item['function']['name'] for item in unavailable.schemas()})
        self.assertIn('error', unavailable.execute('web_fetch', json.dumps({'url': self.base+'/page'})))

    def test_argument_validation_cancellation_and_output_limits(self):
        for name, args in [('web_fetch', {'url': []}), ('document_get', {'id': '../private'}),
                           ('mcp_call', {'name': 'test', 'tool': 'echo', 'arguments': []}),
                           ('artifact_save', {'title': 'No', 'format': 'unknown', 'content': 'No'}),
                           ('web_search', {'query': 'ok', 'api_key': 'injected'})]:
            with self.subTest(name=name):
                self.assertIn('error', self.runner.execute(name, json.dumps(args)))
        self.page = '<p>' + '界' * 35000 + '</p>'
        bounded = self.call('web_fetch', url=self.base+'/page')
        self.assertTrue(bounded['truncated'])
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False).encode()), 32000)
        cancel = Cancellation()
        def approval(_):
            cancel.stop()
            return True
        stopped = ExtensionRunner(self.root, approval, cancel=cancel, application=self.application)
        result = stopped.execute('artifact_save', json.dumps({'title': 'Cancelled', 'format': 'text', 'content': 'No change'}))
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.documents.handle('GET', '/api/documents', {})['artifacts'], [])

    def test_actual_agent_plan_tool_loop_reads_source(self):
        url = self.base+'/page'
        class Client:
            def complete(self, messages):
                if messages[-1]['role'] == 'tool':
                    source = json.loads(messages[-1]['content'])
                    return {'role': 'assistant', 'content': 'Evidence: ' + source['title']}
                return tool_call('web_fetch', {'url': url})
        client = Client()
        runner = ExtensionRunner(self.root, lambda _: False, mode='plan', application=self.application)
        result = Agent(client, runner, Session(self.root, 'test'), emit=lambda _: None, mode='plan').run('Inspect this source')
        self.assertEqual(result, 'Evidence: Research page')
        names = {item['function']['name'] for item in client.tool_schemas}
        self.assertIn('web_fetch', names)
        self.assertNotIn('mcp_call', names)


if __name__ == '__main__':
    unittest.main()
