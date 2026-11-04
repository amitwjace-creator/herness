"""Authenticated Knowledge API integration with local embeddings and sqlite-vec."""
import base64
import json
import time
import unittest
import urllib.error
from pathlib import Path

import test_herness as existing
import test_vectors


class VectorAPITest(unittest.TestCase):
    setUp = existing.ServerTest.setUp
    launch = existing.ServerTest.launch
    request = existing.ServerTest.request

    def setup_service(self, client=None):
        self.provider = test_vectors.EmbeddingServer()
        self.addCleanup(self.provider.close)
        self.launch(client)
        self.config = {'protocol': 'openai', 'base_url': self.provider.base+'/v1',
                       'model': 'test-vectors', 'enabled': True, 'batch_size': 2}
        return self.call('/api/vectors/settings', self.config)

    def call(self, path='/api/vectors', data=None):
        return json.loads(self.request(path, data))

    def upload(self, content, name='source.txt'):
        return self.call('/api/documents/upload', {'name': name, 'data': base64.b64encode(content.encode()).decode()})

    def indexed(self, source_ids):
        self.call('/api/vectors/index', {'source_ids': source_ids})
        result = test_vectors.await_condition(lambda: (value if (value := self.call())['indexing']['status']
                                                        in {'completed', 'failed', 'cancelled'} else None))
        self.assertEqual(result['indexing']['status'], 'completed', result['indexing'])
        return result

    def test_default_off_authentication_origin_and_settings_secret_redaction(self):
        self.provider = test_vectors.EmbeddingServer()
        self.addCleanup(self.provider.close)
        self.launch()
        initial = self.call()
        self.assertFalse(initial['configured'])
        self.assertFalse(initial['settings']['key_present'])
        self.assertEqual(self.provider.requests, [])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/vectors', token=False)
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/vectors/settings', {'enabled': True}, headers={'Origin': 'https://untrusted.example'})
        self.assertEqual(caught.exception.code, 403)
        endpoint = self.provider.base+'/v1'
        self.call('/api/settings', {'base_url': endpoint, 'model': 'chat-model', 'api_key': 'private-chat-key', 'demo': True})
        self.assertFalse(self.call()['settings']['key_present'])
        saved = self.call('/api/vectors/settings', {'protocol': 'openai', 'base_url': endpoint,
                          'model': 'test-vectors', 'enabled': True, 'api_key': 'private-vector-key'})
        self.assertTrue(saved['key_present'])
        self.assertNotIn('private-vector-key', json.dumps(saved))
        tested = self.call('/api/vectors/test', {})
        self.assertEqual(tested['dimensions'], 8)
        self.assertEqual(self.provider.requests[-1]['authorization'], 'Bearer private-vector-key')
        settings_text = (self.root/'.herness/embedding-settings.json').read_text()
        self.assertNotIn('private-vector-key', settings_text)
        self.assertNotIn('private-chat-key', settings_text)
        self.assertNotIn('private-vector-key', json.dumps(self.call()))
        changed = self.call('/api/vectors/settings', {'base_url': self.provider.base+'/different'})
        self.assertFalse(changed['key_present'])
        from herness.vectors import VectorKnowledge
        restarted = VectorKnowledge(self.root)
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.status()['settings']['key_present'])

    def test_http_index_search_citations_delete_and_clear_preserve_originals(self):
        self.setup_service()
        source = self.upload('Astronomy studies the galaxy and moon orbit.', 'astronomy.txt')
        self.indexed(['doc:'+source['id']])
        result = self.call('/api/vectors/search', {'query': 'moon orbit', 'limit': 3, 'kinds': ['document']})
        self.assertEqual(result['results'][0]['source_id'], 'doc:'+source['id'])
        self.assertEqual(result['results'][0]['title'], 'astronomy.txt')
        self.assertTrue(result['results'][0]['location'])
        deleted = self.call('/api/vectors/delete', {'source_id': 'doc:'+source['id']})
        self.assertTrue(deleted['original_preserved'])
        self.assertEqual(self.call()['stats']['chunks'], 0)
        self.indexed(['doc:'+source['id']])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/vectors/clear', {})
        self.assertEqual(caught.exception.code, 400)
        self.call('/api/vectors/clear', {'confirmed': True})
        self.assertEqual(self.call()['stats']['chunks'], 0)
        original = self.call('/api/documents/library', {'action': 'get', 'id': source['id']})
        self.assertEqual(original['name'], 'astronomy.txt')

    def test_api_memory_exclusion_and_document_deletion_prune_immediately(self):
        self.setup_service()
        revision = self.call('/api/privacy/memory')['revision']
        memory = self.call('/api/privacy/memory', {'revision': revision, 'label': 'Style', 'content': 'Prefer concise project memory.'})
        entry = memory['entries'][0]
        source = self.upload('Astronomy moon orbit source.')
        self.indexed(['memory:'+entry['id'], 'doc:'+source['id']])
        self.call('/api/privacy/memory', {'revision': memory['revision'], 'id': entry['id'], 'excluded': True})
        self.call('/api/documents/library', {'action': 'delete', 'id': source['id']})
        self.assertEqual(self.call('/api/vectors/search', {'query': 'memory moon'})['results'], [])
        self.assertEqual(self.call()['stats']['chunks'], 0)

    def test_project_switch_isolation_and_cancel_old_background_job(self):
        self.setup_service()
        source = self.upload('Astronomy moon source in the first project.', 'first.txt')
        self.indexed(['doc:'+source['id']])
        other = self.root/'second-project'
        other.mkdir()
        listing = self.call('/api/projects', {'action': 'add', 'path': str(other), 'title': 'Second project'})
        identity = next(project['id'] for project in listing['projects'] if project['path'] == str(other))
        self.call('/api/projects', {'action': 'switch', 'id': identity})
        second = self.call()
        self.assertFalse(second['configured'])
        self.assertEqual(second['stats']['chunks'], 0)
        self.assertEqual(second['available_sources'], [])
        self.call('/api/vectors/settings', self.config)
        new_source = self.upload('A bread recipe in the second cooking project.', 'second.txt')
        self.indexed(['doc:'+new_source['id']])
        self.assertNotIn('first.txt', json.dumps(self.call('/api/vectors/search', {'query': 'moon'})))
        self.call('/api/projects', {'action': 'switch', 'id': 'default'})
        self.assertEqual(self.call()['stats']['sources'], 1)
        self.assertNotIn('second.txt', json.dumps(self.call('/api/vectors/search', {'query': 'bread'})))
        pending = self.upload('Another astronomy galaxy source pending indexing.', 'pending.txt')
        old_service = self.server.workspace.application.vectors
        self.provider.entered.clear()
        self.provider.release.clear()
        self.call('/api/vectors/index', {'source_ids': ['doc:'+pending['id']]})
        self.assertTrue(self.provider.entered.wait(2))
        switched = self.call('/api/projects', {'action': 'switch', 'id': identity})
        self.assertEqual(switched['active'], identity)
        self.provider.release.set()
        terminal = test_vectors.await_condition(lambda: (value if (value := old_service.status())['indexing']['status']
                                                          in {'completed', 'failed', 'cancelled'} else None))
        self.assertEqual(terminal['indexing']['status'], 'cancelled')
        self.assertNotIn('pending.txt', json.dumps(self.call()))
        self.assertEqual(self.call()['stats']['sources'], 1)

    def test_malformed_objects_and_unsafe_settings_return_400_without_network(self):
        self.setup_service()
        cases = [('/api/vectors/settings', []),
                 ('/api/vectors/settings', {'protocol': []}),
                 ('/api/vectors/settings', {'enabled': 'yes'}),
                 ('/api/vectors/settings', {'base_url': 'http://external.example/v1'}),
                 ('/api/vectors/settings', {'base_url': 'https://user:password@example.com/v1'}),
                 ('/api/vectors/settings', {'api_key': 'bad\nkey'}),
                 ('/api/vectors/settings', {'batch_size': True}),
                 ('/api/vectors/settings', {'unknown': 'value'}),
                 ('/api/vectors/index', {'source_ids': {}}),
                 ('/api/vectors/index', {'source_ids': [[]]}),
                 ('/api/vectors/index', {'source_ids': ['missing'], 'force': []}),
                 ('/api/vectors/search', {'query': []}),
                 ('/api/vectors/search', {'query': 'moon', 'limit': True}),
                 ('/api/vectors/search', {'query': 'moon', 'kinds': [['document']]}),
                 ('/api/vectors/search', {'query': 'moon', 'kinds': ['private-files']}),
                 ('/api/vectors/delete', {'source_id': {}}),
                 ('/api/vectors/clear', {'confirmed': 1})]
        request_count = len(self.provider.requests)
        for path, data in cases:
            with self.subTest(path=path, data=data):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.request(path, data)
                self.assertEqual(caught.exception.code, 400)
                self.assertIsInstance(json.loads(caught.exception.read())['error'], str)
        self.assertEqual(len(self.provider.requests), request_count)
        self.assertTrue(self.call()['configured'])

    def test_chat_agent_can_retrieve_cited_evidence_in_plan_mode(self):
        class Client:
            def complete(self, messages):
                if messages[-1]['role'] == 'tool':
                    payload = json.loads(messages[-1]['content'])
                    if not payload.get('results'):
                        return {'role': 'assistant', 'content': 'No evidence retrieved: '+json.dumps(payload)}
                    result = payload['results'][0]
                    return {'role': 'assistant', 'content': result['title']+' | '+result['location']+' | '+result['text']}
                return existing.tool_call('semantic_search', {'query': 'moon orbit astronomy'})
        client = Client()
        self.setup_service(client)
        source = self.upload('The moon follows an orbit. Astronomy evidence for the agent.', 'agent-reference.txt')
        self.indexed(['doc:'+source['id']])
        self.call('/api/run/options', {'mode': 'plan'})
        self.call('/api/chat', {'prompt': 'Find cited astronomy evidence from my knowledge library.'})
        status = test_vectors.await_condition(lambda: (value if not (value := self.call('/api/status'))['busy'] else None))
        answer = next(message['text'] for message in reversed(status['messages']) if message['role'] == 'assistant')
        self.assertIn('agent-reference.txt', answer)
        self.assertIn('characters', answer)
        self.assertIn('Astronomy evidence', answer)
        names = {item['function']['name'] for item in client.tool_schemas}
        self.assertIn('semantic_search', names)
        self.assertNotIn('artifact_save', names)
        self.assertNotIn('run_command', names)
