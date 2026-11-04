"""The web agent retrieves real vector-index results through its read-only tool."""
import json
import tempfile
import unittest
from pathlib import Path

from herness.agent_tools import ExtensionRunner
from herness.application import Application
from herness.cli import DemoClient
from herness.core import Agent, Session
from herness.vectors import source_chunks
from herness.web import Workspace
from test_vectors import EmbeddingServer, await_condition, uploaded


class VectorAgentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = EmbeddingServer()
        self.addCleanup(self.provider.close)
        self.state = Workspace(self.root, DemoClient(), demo=True)
        self.app = Application(self.state)
        self.addCleanup(self.app.close)

    def index(self):
        knowledge = self.app.vectors
        knowledge.configure({'protocol': 'openai', 'base_url': self.provider.base + '/v1',
                             'model': 'test-vectors', 'enabled': True})
        document = uploaded(knowledge.documents, 'Astronomy observations place the moon in orbit.', 'observations.txt')
        knowledge.start_index({'source_ids': ['doc:' + document['id']]})
        await_condition(lambda: knowledge.status()['indexing']['status'] != 'running')
        self.assertEqual(knowledge.job['status'], 'completed')
        return document

    def test_semantic_tool_uses_citations_in_plan_mode_and_preserves_policy(self):
        document = self.index()
        runner = ExtensionRunner(self.root, lambda _: self.fail('A read tool requested approval'),
                                 application=self.app, mode='plan')
        class Client:
            model = 'fixture'
            def complete(inner, messages):
                if messages[-1]['role'] == 'tool':
                    result = json.loads(messages[-1]['content'])
                    self.assertEqual(result['results'][0]['source_id'], 'doc:' + document['id'])
                    self.assertIn('observations.txt', result['results'][0]['title'])
                    self.assertIn('characters', result['results'][0]['location'])
                    self.assertIn('untrusted', result['content_policy'])
                    return {'role': 'assistant', 'content': 'Found cited project evidence.'}
                return {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'semantic', 'type': 'function', 'function': {
                        'name': 'semantic_search', 'arguments': json.dumps({'query': 'moon orbit'})}}]}
        result = Agent(Client(), runner, Session(self.root, 'knowledge'), lambda _: None, mode='plan').run('Find project evidence about the moon.')
        self.assertEqual(result, 'Found cited project evidence.')
        self.assertIn('semantic_search', {s['function']['name'] for s in runner.schemas()})
        self.assertFalse((self.root / 'observations.txt').exists())

    def test_disabling_or_removing_read_scope_hides_and_denies_tool(self):
        self.index()
        restricted = ExtensionRunner(self.root, lambda _: False, application=self.app, scopes=set())
        self.assertNotIn('semantic_search', {s['function']['name'] for s in restricted.schemas()})
        self.assertIn('error', restricted.execute('semantic_search', '{"query":"moon"}'))
        self.app.vectors.configure({'enabled': False})
        runner = ExtensionRunner(self.root, lambda _: False, application=self.app)
        self.assertNotIn('semantic_search', {s['function']['name'] for s in runner.schemas()})
        self.assertIn('error', runner.execute('semantic_search', '{"query":"moon"}'))

    def test_explicit_clear_allows_same_model_with_new_dimensions(self):
        document = self.index()
        knowledge = self.app.vectors
        self.provider.dimensions = 3
        source_id = 'doc:' + document['id']
        knowledge.start_index({'source_ids': [source_id], 'force': True})
        await_condition(lambda: knowledge.job['status'] != 'running')
        self.assertEqual(knowledge.job['status'], 'failed')
        self.assertEqual(knowledge.store.stats()['dimensions'], 8)
        knowledge.clear(True)
        knowledge.start_index({'source_ids': [source_id]})
        await_condition(lambda: knowledge.job['status'] != 'running')
        self.assertEqual(knowledge.job['status'], 'completed', knowledge.job)
        self.assertEqual(knowledge.store.stats()['dimensions'], 3)
        self.assertTrue(knowledge.search('moon')['results'])
        deleted = knowledge.delete(source_id)
        self.assertEqual(deleted['deleted'], 1)
        self.assertTrue(deleted['original_preserved'])
        self.assertEqual(knowledge.documents.library({'action': 'get', 'id': document['id']})['name'], 'observations.txt')

    def test_chunking_is_bounded_and_preserves_original_citation_boundaries(self):
        first = 'Astronomy\n' * 1000
        second = 'Cooking recipes\n' * 350
        chunks = source_chunks({'id': 'doc:test', 'hash': 'hash', 'kind': 'document', 'title': 'Two pages',
                                'units': [{'label': 'Page 1', 'text': first}, {'label': 'Page 2', 'text': second}]})
        self.assertGreater(len(chunks), 3)
        self.assertEqual(len({c['id'] for c in chunks}), len(chunks))
        for chunk in chunks:
            self.assertLessEqual(len(chunk['text']), 3000)
            self.assertIn('characters', chunk['location'])
            if chunk['location'].startswith('Page 1'):
                self.assertNotIn('Cooking', chunk['text'])
            else:
                self.assertNotIn('Astronomy', chunk['text'])


if __name__ == '__main__':
    unittest.main()
