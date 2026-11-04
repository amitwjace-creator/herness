"""Real sqlite-vec integration against deterministic, local embedding HTTP servers."""
import base64
import concurrent.futures
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from herness.documents import Documents
from herness.privacy import Privacy
from herness.state import ProjectStore


class EmbeddingServer:
    """Test provider only: deterministic vectors, controllable delays and errors."""
    def __init__(self):
        self.requests = []
        self.inputs = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.failure = None
        self.dimensions = 8
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                inputs = body.get('input', [])
                if isinstance(inputs, str):
                    inputs = [inputs]
                owner.requests.append({'path': self.path, 'body': body, 'authorization': self.headers.get('Authorization')})
                owner.inputs.extend(inputs)
                owner.entered.set()
                owner.release.wait(10)
                if owner.failure:
                    status, response = owner.failure
                else:
                    vectors = [owner.vector(item) for item in inputs]
                    response = ({'embeddings': vectors, 'model': body.get('model')} if self.path.endswith('/api/embed')
                                else {'data': [{'index': index, 'embedding': vector} for index, vector in reversed(list(enumerate(vectors)))],
                                      'model': body.get('model'), 'usage': {'prompt_tokens': len(inputs) * 8, 'total_tokens': len(inputs) * 8}})
                    status = 200
                raw = json.dumps(response).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def vector(self, value):
        words = value.casefold()
        groups = [('astronomy', 'galaxy', 'moon', 'orbit', 'space'),
                  ('recipe', 'bread', 'cooking', 'kitchen'),
                  ('memory', 'concise', 'prefer', 'style'),
                  ('project', 'code', 'python', 'software')]
        vector = [float(sum(words.count(word) for word in group)) for group in groups]
        vector += [0.1, 0.05, 0.025, 0.0125]
        return (vector + [0.001] * self.dimensions)[:self.dimensions]

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def uploaded(documents, text, name='source.txt'):
    return documents.upload({'name': name, 'data': base64.b64encode(text.encode()).decode()})


def save_memory(privacy, **data):
    return privacy.mutate_memory({'revision': privacy.memory()['revision'], **data})


def await_condition(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        threading.Event().wait(0.01)
    raise AssertionError('Background vector operation did not reach the expected state')


class VectorKnowledgeTest(unittest.TestCase):
    def setUp(self):
        from herness.vectors import VectorKnowledge
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = EmbeddingServer()
        self.addCleanup(self.provider.close)
        self.documents = Documents(self.root)
        self.privacy = Privacy(self.root)
        self.knowledge = VectorKnowledge(self.root, documents=self.documents, privacy=self.privacy)
        self.addCleanup(self.knowledge.close)
        self.config = {'protocol': 'openai', 'base_url': self.provider.base+'/v1',
                       'model': 'test-vectors', 'enabled': True, 'batch_size': 2}
        self.knowledge.configure(self.config)

    def index(self, source_ids=None):
        if source_ids is None:
            source_ids = [source['id'] for source in self.knowledge.status()['available_sources'] if source['indexable']]
        self.knowledge.start_index({'source_ids': source_ids})
        status = await_condition(lambda: (value if (value := self.knowledge.status())['indexing']['status']
                                           in {'completed', 'failed', 'cancelled'} else None))
        self.assertEqual(status['indexing']['status'], 'completed', status['indexing'])
        return status

    def test_document_memory_and_note_indexing_citations_real_database_persistence(self):
        astronomy = uploaded(self.documents, 'Astronomy studies the moon, galaxy, and planets in orbit.', 'astronomy.txt')
        uploaded(self.documents, 'A bread recipe belongs in the cooking kitchen.', 'recipe.txt')
        active = save_memory(self.privacy, label='Writing style', content='Prefer concise memory summaries.')['entries'][0]
        ProjectStore(self.root).save_memory('The project uses Python software.')
        state = self.index()
        identities = {item['source_id'] for item in state['sources']}
        self.assertIn('doc:'+astronomy['id'], identities)
        self.assertIn('memory:'+active['id'], identities)
        self.assertIn('note:project', identities)
        self.assertGreaterEqual(state['stats']['chunks'], 4)
        self.assertEqual(state['stats']['dimensions'], 8)
        result = self.knowledge.search('moon astronomy orbit', limit=3)
        self.assertEqual(result['results'][0]['source_id'], 'doc:'+astronomy['id'])
        self.assertIn('Astronomy', result['results'][0]['text'])
        self.assertTrue(result['results'][0]['location'])
        self.assertEqual(result['results'][0]['title'], 'astronomy.txt')
        database = self.root/'.herness/vectors.sqlite3'
        self.assertTrue(database.is_file())
        with sqlite3.connect(database) as connection:
            schema = '\n'.join(row[0] or '' for row in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        self.assertIn('vec0', schema.casefold())
        from herness.vectors import VectorKnowledge
        restored = VectorKnowledge(self.root)
        self.addCleanup(restored.close)
        self.assertEqual(restored.status()['stats']['chunks'], state['stats']['chunks'])
        self.assertEqual(restored.search('moon astronomy', limit=1)['results'][0]['source_id'], 'doc:'+astronomy['id'])

    def test_ineligible_memory_is_not_embedded_or_retrieved(self):
        active = save_memory(self.privacy, label='Active', content='Prefer concise active memory.')['entries'][0]
        save_memory(self.privacy, label='Expired', content='EXPIRED_PRIVATE_CONTENT', expires_at=1)
        save_memory(self.privacy, label='Excluded', content='EXCLUDED_PRIVATE_CONTENT', excluded=True)
        save_memory(self.privacy, label='Choice', content='CONFLICT_A_PRIVATE_CONTENT')
        save_memory(self.privacy, label='CHOICE', content='CONFLICT_B_PRIVATE_CONTENT')
        self.index()
        sent = '\n'.join(self.provider.inputs)
        self.assertIn('Prefer concise', sent)
        for forbidden in ['EXPIRED_PRIVATE_CONTENT', 'EXCLUDED_PRIVATE_CONTENT', 'CONFLICT_A_PRIVATE_CONTENT', 'CONFLICT_B_PRIVATE_CONTENT']:
            self.assertNotIn(forbidden, sent)
            self.assertNotIn(forbidden, json.dumps(self.knowledge.search('memory', limit=10)))
        ids = {hit['source_id'] for hit in self.knowledge.search('memory', limit=10)['results']}
        self.assertEqual(ids, {'memory:'+active['id']})
        save_memory(self.privacy, id=active['id'], excluded=True)
        self.assertEqual(self.knowledge.search('memory', limit=10)['results'], [])
        self.assertEqual(self.knowledge.status()['stats']['chunks'], 0)

    def test_expired_conflicted_and_deleted_memory_pruned_after_index(self):
        items = {}
        for label in ['Expiring', 'Conflict', 'Deleted']:
            entries = save_memory(self.privacy, label=label, content=label+' active memory')['entries']
            items[label] = next(item for item in entries if item['label'] == label)
        self.index()
        save_memory(self.privacy, id=items['Expiring']['id'], expires_at=1)
        save_memory(self.privacy, label='CONFLICT', content='Different conflicting memory')
        save_memory(self.privacy, action='delete', id=items['Deleted']['id'])
        self.assertEqual(self.knowledge.search('memory', limit=10)['results'], [])
        self.assertEqual(self.knowledge.status()['stats']['chunks'], 0)

    def test_changed_legacy_note_and_deleted_document_cannot_return_stale_text(self):
        source = uploaded(self.documents, 'The moon orbits in a galaxy. Old source evidence.', 'old.txt')
        ProjectStore(self.root).save_memory('OLD_LEGACY_MEMORY_VALUE')
        self.index()
        ProjectStore(self.root).save_memory('NEW_LEGACY_MEMORY_VALUE')
        self.documents.library({'action': 'delete', 'id': source['id']})
        hits = self.knowledge.search('moon memory', limit=10)
        self.assertNotIn('Old source evidence', json.dumps(hits))
        self.assertNotIn('OLD_LEGACY_MEMORY_VALUE', json.dumps(hits))
        self.assertEqual(hits['results'], [])
        self.index(['note:project'])
        self.assertIn('NEW_LEGACY_MEMORY_VALUE', json.dumps(self.knowledge.search('memory')))

    def test_model_changes_require_explicit_clear_or_original_configuration(self):
        uploaded(self.documents, 'Astronomy and moon orbit evidence.')
        initial = self.index()
        self.knowledge.configure({**self.config, 'model': 'other-model'})
        self.assertTrue(self.knowledge.status()['requires_rebuild'])
        request_count = len(self.provider.requests)
        with self.assertRaises(ValueError):
            self.knowledge.search('moon')
        with self.assertRaises(ValueError):
            self.knowledge.start_index({'source_ids': [item['id'] for item in self.knowledge.status()['available_sources']]})
        self.assertEqual(len(self.provider.requests), request_count)
        self.assertEqual(self.knowledge.status()['stats']['chunks'], initial['stats']['chunks'])
        self.knowledge.configure(self.config)
        self.assertFalse(self.knowledge.status()['requires_rebuild'])
        self.assertTrue(self.knowledge.search('moon')['results'])
        self.knowledge.configure({**self.config, 'model': 'other-model'})
        with self.assertRaises(ValueError):
            self.knowledge.clear(False)
        self.knowledge.clear(True)
        self.assertEqual(self.knowledge.status()['stats']['chunks'], 0)
        self.assertTrue(self.documents.overview()['library'])
        self.index()
        self.assertFalse(self.knowledge.status()['requires_rebuild'])

    def test_background_cancellation_prevents_pending_source_commit(self):
        source = uploaded(self.documents, 'Astronomy moon galaxy in an interrupted source.')
        self.provider.entered.clear()
        self.provider.release.clear()
        self.knowledge.start_index({'source_ids': ['doc:'+source['id']]})
        self.assertTrue(self.provider.entered.wait(2))
        with self.assertRaises(ValueError):
            self.knowledge.start_index({'source_ids': ['doc:'+source['id']]})
        started = time.monotonic()
        self.knowledge.stop()
        self.assertLess(time.monotonic()-started, 1)
        self.provider.release.set()
        terminal = await_condition(lambda: (value if (value := self.knowledge.status())['indexing']['status']
                                             in {'completed', 'cancelled', 'failed'} else None))
        self.assertEqual(terminal['indexing']['status'], 'cancelled', terminal['indexing'])
        self.assertEqual(terminal['stats']['chunks'], 0)
        self.assertTrue(self.documents.overview()['library'])

    def test_ollama_protocol_and_source_removal_keep_originals(self):
        self.knowledge.configure({'protocol': 'ollama', 'base_url': self.provider.base,
                                  'model': 'local-vectors', 'enabled': True})
        source = uploaded(self.documents, 'A cooking recipe for bread.')
        self.index()
        self.assertTrue(any(request['path'] == '/api/embed' for request in self.provider.requests))
        self.assertEqual(self.knowledge.search('bread recipe')['results'][0]['source_id'], 'doc:'+source['id'])
        self.knowledge.delete('doc:'+source['id'])
        self.assertEqual(self.knowledge.status()['stats']['chunks'], 0)
        self.assertEqual(self.documents.library({'action': 'get', 'id': source['id']})['name'], 'source.txt')

    def test_provider_failure_does_not_claim_success_or_persist_partial_source(self):
        uploaded(self.documents, 'Astronomy moon galaxy evidence.')
        self.provider.failure = (503, {'error': 'Do not echo private remote error details'})
        source_ids = [item['id'] for item in self.knowledge.status()['available_sources']]
        self.knowledge.start_index({'source_ids': source_ids})
        state = await_condition(lambda: (value if (value := self.knowledge.status())['indexing']['status']
                                          in {'completed', 'failed', 'cancelled'} else None))
        self.assertEqual(state['indexing']['status'], 'failed')
        self.assertEqual(state['stats']['chunks'], 0)
        self.assertNotIn('private remote error details', json.dumps(state))

    def test_source_excluded_while_embedding_never_commits(self):
        item = save_memory(self.privacy, label='Preference', content='MEMORY_REVOKED_DURING_INDEX')['entries'][0]
        self.provider.entered.clear()
        self.provider.release.clear()
        self.knowledge.start_index({'source_ids': ['memory:'+item['id']]})
        self.assertTrue(self.provider.entered.wait(2))
        save_memory(self.privacy, id=item['id'], excluded=True)
        self.provider.release.set()
        state = await_condition(lambda: (value if (value := self.knowledge.status())['indexing']['status']
                                          in {'completed', 'failed', 'cancelled'} else None))
        self.assertEqual(state['indexing']['status'], 'failed')
        self.assertEqual(state['stats']['chunks'], 0)
        self.assertEqual(self.knowledge.search('memory')['results'], [])

    def test_document_deleted_while_searching_is_filtered_before_return(self):
        source = uploaded(self.documents, 'Astronomy moon galaxy PRIVATE_SOURCE_REMOVED')
        self.index()
        self.provider.entered.clear()
        self.provider.release.clear()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.knowledge.search, 'astronomy moon')
            self.assertTrue(self.provider.entered.wait(2))
            self.documents.library({'action': 'delete', 'id': source['id']})
            self.provider.release.set()
            answer = result.result(timeout=3)
        self.assertEqual(answer['results'], [])
        self.assertNotIn('PRIVATE_SOURCE_REMOVED', json.dumps(answer))

    def test_filtered_search_and_restarted_dimension_mismatch(self):
        uploaded(self.documents, 'Astronomy moon galaxy orbit evidence.')
        item = save_memory(self.privacy, label='Style', content='Prefer concise project memory.')['entries'][0]
        self.index()
        hits = self.knowledge.search('moon orbit', limit=1, kinds=['memory'])['results']
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['source_id'], 'memory:'+item['id'])
        self.provider.dimensions = 4
        from herness.vectors import VectorKnowledge
        restarted = VectorKnowledge(self.root)
        self.addCleanup(restarted.close)
        with self.assertRaises(ValueError):
            restarted.search('moon orbit')
        self.assertEqual(restarted.status()['stats']['dimensions'], 8)

    def test_pdf_pages_keep_source_citations_and_long_text_is_chunked(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        font_ref = writer._add_object(font)
        for content in ['Astronomy moon orbit galaxy.', 'A bread cooking recipe for the kitchen.']:
            page = writer.add_blank_page(width=300, height=300)
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref})})
            stream = DecodedStreamObject()
            stream.set_data(('BT /F1 12 Tf 20 200 Td ('+content+') Tj ET').encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
        raw = io.BytesIO()
        writer.write(raw)
        pdf = self.documents.upload({'name': 'reference.pdf', 'data': base64.b64encode(raw.getvalue()).decode()})
        self.index(['doc:'+pdf['id']])
        result = self.knowledge.search('bread cooking recipe', limit=1)['results'][0]
        self.assertEqual(result['source_id'], 'doc:'+pdf['id'])
        self.assertIn('page 2', result['location'].casefold())
        self.assertIn('bread', result['text'])
        large = uploaded(self.documents, 'Astronomy moon orbit evidence. ' * 300, 'large.txt')
        state = self.index(['doc:'+large['id']])
        indexed = next(source for source in state['sources'] if source['source_id'] == 'doc:'+large['id'])
        self.assertGreater(indexed['chunks'], 1)
        result = self.knowledge.search('astronomy moon orbit', limit=10)
        self.assertTrue(all(len(hit['text']) <= 3000 for hit in result['results']))
