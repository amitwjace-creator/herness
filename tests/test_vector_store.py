"""Exercise the real sqlite-vec extension, transaction boundaries and private paths."""
import importlib.util
import json
import math
import os
import sqlite3
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herness.vector_store import VectorStore, VectorStoreUnavailable, normalize_vector


class VectorValidationTest(unittest.TestCase):
    def test_normalization_handles_large_and_tiny_finite_components(self):
        for vector in ([3, 4], [1e308, 1e308], [1e-300, 1e-300]):
            normalized = struct.unpack('<2f', normalize_vector(vector))
            self.assertAlmostEqual(sum(value * value for value in normalized), 1, places=6)
        normalized = struct.unpack('<2f', normalize_vector([3, 4]))
        self.assertAlmostEqual(normalized[0], .6)
        self.assertAlmostEqual(normalized[1], .8)

    def test_rejects_zero_nonfinite_wrong_types_and_dimension_limits(self):
        for vector in ([], [0, 0], [float('nan')], [float('inf')], ['1'], [True], [1] * 4097, None, {'x': 1}, [10 ** 1000]):
            with self.subTest(vector=str(vector)[:50]), self.assertRaises(ValueError):
                normalize_vector(vector)
        with self.assertRaisesRegex(ValueError, 'dimension mismatch'):
            normalize_vector([1, 2], 3)
        self.assertEqual(len(normalize_vector([1] * 4096)), 4096 * 4)

    def test_missing_optional_extension_does_not_break_construction_or_import(self):
        with tempfile.TemporaryDirectory() as directory, patch('herness.vector_store.importlib.import_module', side_effect=ImportError('not installed')):
            store = VectorStore(directory)
            stats = store.stats()
            self.assertFalse(stats['available'])
            self.assertIn('sqlite-vec', stats['error'])
            self.assertFalse((Path(directory) / '.herness').exists())
            with self.assertRaises(VectorStoreUnavailable):
                store.replace_source('source', [{'id': 'one', 'text': 'text'}], [[1, 0]], 'model')


@unittest.skipUnless(importlib.util.find_spec('sqlite_vec'), 'Install sqlite-vec to run real vector database tests')
class VectorStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = VectorStore(self.root)

    def insert(self, source, items, vectors, kind='document', key='provider/model-v1'):
        return self.store.replace_source(source, [{'id': identity, 'text': text, 'kind': kind,
            'title': source, 'location': 'line ' + str(index + 1), 'source_hash': 'hash-' + source,
            'chunk_index': index} for index, (identity, text) in enumerate(items)], vectors, key)

    def test_actual_vec0_cosine_ranking_and_persistence(self):
        self.insert('notes', [('east', 'East'), ('north', 'North'), ('west', 'West')], [[3, 0, 0], [0, 2, 0], [-9, 0, 0]])
        matches = self.store.search([100, 0, 0], k=3)['matches']
        self.assertEqual([match['id'] for match in matches], ['east', 'north', 'west'])
        self.assertEqual([round(match['score'], 5) for match in matches], [1, 0, -1])
        self.assertEqual(matches[0]['location'], 'line 1')
        self.assertEqual(matches[0]['source_hash'], 'hash-notes')
        reopened = VectorStore(self.root)
        self.assertEqual(reopened.search([1, 0, 0], k=1)['matches'][0]['text'], 'East')
        stats = reopened.stats()
        self.assertEqual((stats['chunks'], stats['sources'], stats['dimensions']), (3, 1, 3))
        self.assertEqual(stats['embedding_key'], 'provider/model-v1')
        self.assertEqual(len(stats['embedding_fingerprint']), 64)
        with sqlite3.connect(self.store.path) as database:
            sql = database.execute("SELECT sql FROM sqlite_master WHERE name='vec_chunks'").fetchone()[0]
            self.assertIn('USING vec0', sql)
            self.assertIn('distance_metric=cosine', sql)

    def test_filters_apply_before_nearest_neighbor_limit(self):
        self.insert('a', [('a1', 'closest'), ('a2', 'second closest')], [[1, 0], [1, .01]], kind='document')
        self.insert('b', [('b1', 'selected third'), ('b2', 'selected fourth')], [[.5, .5], [.1, .9]], kind='memory')
        self.insert('c', [('c1', 'other source')], [[.4, .6]], kind='memory')
        self.assertEqual([item['id'] for item in self.store.search([1, 0], k=2, source_ids=['b'])['matches']], ['b1', 'b2'])
        self.assertEqual([item['id'] for item in self.store.search([1, 0], k=1, kinds=['memory'])['matches']], ['b1'])
        self.assertEqual([item['id'] for item in self.store.search([1, 0], k=2, kinds=['memory'], source_ids=['c'])['matches']], ['c1'])
        self.assertEqual(self.store.search([1, 0], kinds=[])['matches'], [])
        self.assertEqual(self.store.search([1, 0], source_ids=['missing'])['matches'], [])

    def test_replace_updates_source_index_and_delete_removes_vectors(self):
        self.insert('one', [('a', 'old'), ('b', 'old second')], [[1, 0], [0, 1]])
        self.insert('two', [('c', 'keep')], [[-1, 0]], kind='memory')
        self.insert('one', [('d', 'new')], [[0, 1]])
        self.assertEqual({item['id'] for item in self.store.search([1, 0], k=10)['matches']}, {'c', 'd'})
        sources = self.store.list_sources()['sources']
        self.assertEqual([(source['source_id'], source['chunks']) for source in sources], [('one', 1), ('two', 1)])
        self.assertEqual(sources[0]['source_hash'], 'hash-one')
        self.assertEqual(self.store.delete_source('one')['deleted'], 1)
        self.assertEqual([item['id'] for item in self.store.search([1, 0])['matches']], ['c'])
        self.assertEqual(self.store.delete_source('missing')['deleted'], 0)
        self.assertEqual(self.store.stats()['chunks'], 1)

    def test_embedding_identity_and_dimensions_cannot_be_mixed(self):
        self.insert('source', [('a', 'original')], [[1, 0]])
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            self.insert('source', [('b', 'wrong model')], [[1, 0]], key='other/model')
        with self.assertRaisesRegex(ValueError, 'dimension mismatch'):
            self.insert('source', [('b', 'wrong size')], [[1, 0, 0]])
        with self.assertRaisesRegex(ValueError, 'dimension mismatch'):
            self.store.search([1, 0, 0])
        self.assertEqual(self.store.search([1, 0])['matches'][0]['text'], 'original')
        namespace = self.store.stats()['namespace']
        self.assertEqual(self.store.clear()['cleared'], 1)
        self.assertIsNone(self.store.stats()['embedding_key'])
        self.insert('fresh', [('new', 'new model')], [[1, 0, 0]], key='other/model')
        self.assertEqual(self.store.stats()['dimensions'], 3)
        self.assertEqual(self.store.stats()['namespace'], namespace)

    def test_real_constraint_failure_rolls_back_deleted_and_inserted_metadata_and_vectors(self):
        self.insert('alpha', [('old-alpha', 'Alpha original')], [[1, 0]])
        self.insert('beta', [('belongs-to-beta', 'Beta protected')], [[0, 1]])
        with self.assertRaisesRegex(ValueError, 'UNIQUE'):
            self.insert('alpha', [('new-alpha', 'partial new insert'), ('belongs-to-beta', 'conflict')], [[-1, 0], [0, -1]])
        self.assertEqual(self.store.stats()['chunks'], 2)
        self.assertEqual(self.store.stats()['sources'], 2)
        matches = self.store.search([1, 0], k=10)['matches']
        self.assertEqual({match['id'] for match in matches}, {'old-alpha', 'belongs-to-beta'})
        self.assertEqual(matches[0]['text'], 'Alpha original')
        self.assertEqual(self.store.list_sources()['sources'][0]['chunks'], 1)

    def test_invalid_batch_leaves_existing_source_and_embedding_binding_unchanged(self):
        self.insert('source', [('a', 'original')], [[1, 0]])
        for chunks, vectors in [([{'id': 'b', 'text': 'bad'}], [[0, 0]]),
                                ([{'id': 'b', 'text': 'one'}, {'id': 'b', 'text': 'two'}], [[1, 0], [0, 1]]),
                                ([{'id': 'b', 'text': 'one'}, {'id': 'c', 'text': 'two'}], [[1, 0], [1, 0, 0]]),
                                ([{'id': 'b', 'text': 'bad', 'source_id': 'wrong'}], [[1, 0]])]:
            with self.subTest(chunks=chunks), self.assertRaises(ValueError):
                self.store.replace_source('source', chunks, vectors, 'provider/model-v1')
        self.assertEqual(self.store.stats()['chunks'], 1)
        self.assertEqual(self.store.search([1, 0])['matches'][0]['id'], 'a')

    def test_empty_replacement_and_clear_on_empty_store(self):
        self.assertEqual(self.store.clear()['cleared'], 0)
        self.assertEqual(self.store.search([1, 0])['matches'], [])
        self.store.replace_source('absent', [], [], 'model')
        self.assertIsNone(self.store.stats()['dimensions'])
        self.insert('source', [('a', 'remove')], [[1, 0]])
        self.store.replace_source('source', [], [], 'provider/model-v1')
        self.assertEqual(self.store.stats()['chunks'], 0)
        self.assertEqual(self.store.stats()['sources'], 0)
        self.assertEqual(self.store.search([1, 0])['matches'], [])

    def test_private_permissions_and_project_namespaces(self):
        self.store.stats()
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        other = self.root / 'other-project'
        other.mkdir()
        second = VectorStore(other)
        self.assertNotEqual(self.store.stats()['namespace'], second.stats()['namespace'])
        self.insert('only-here', [('a', 'private')], [[1, 0]])
        self.assertEqual(second.stats()['chunks'], 0)

    def test_rejects_database_and_sidecar_links_without_touching_targets(self):
        target = self.root / 'target.txt'
        target.write_text('do not change')
        self.store.directory.mkdir()
        for suffix in ('', '-journal', '-wal', '-shm'):
            candidate = Path(str(self.store.path) + suffix)
            candidate.symlink_to(target)
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, 'links'):
                self.store.stats()
            candidate.unlink()
            self.assertEqual(target.read_text(), 'do not change')
        os.link(target, self.store.path)
        with self.assertRaisesRegex(ValueError, 'links'):
            self.store.stats()
        self.assertEqual(target.read_text(), 'do not change')

    def test_rejects_linked_private_directory_and_new_sidecar_after_open(self):
        external = self.root / 'external'
        external.mkdir()
        self.store.directory.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.store.stats()
        self.assertEqual(list(external.iterdir()), [])
        self.store.directory.unlink()
        self.store.stats()
        target = external / 'untouched'
        target.write_text('untouched')
        Path(str(self.store.path) + '-journal').symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'links'):
            self.insert('source', [('a', 'text')], [[1, 0]])
        self.assertEqual(target.read_text(), 'untouched')

    def test_future_schema_and_unrelated_database_are_rejected(self):
        self.store.stats()
        with sqlite3.connect(self.store.path) as database:
            database.execute('PRAGMA user_version=999')
        with self.assertRaisesRegex(ValueError, 'schema version'):
            self.store.stats()
        self.store.path.unlink()
        with sqlite3.connect(self.store.path) as database:
            database.execute('CREATE TABLE unrelated (value TEXT)')
            database.execute("INSERT INTO unrelated VALUES ('kept')")
        with self.assertRaisesRegex(ValueError, 'not a Herness'):
            self.store.stats()
        with sqlite3.connect(self.store.path) as database:
            self.assertEqual(database.execute('SELECT value FROM unrelated').fetchone()[0], 'kept')

    def test_query_validation_and_sql_injection_remain_data(self):
        self.insert("quoted' source", [('safe', 'safe')], [[1, 0]])
        self.assertEqual(self.store.search([1, 0], source_ids=["quoted' source"])['matches'][0]['id'], 'safe')
        for kwargs in ({'k': True}, {'k': 0}, {'k': 101}, {'kinds': 'document'}, {'source_ids': [[]]}, {'kinds': [True]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.search([1, 0], **kwargs)
        self.assertEqual(self.store.stats()['chunks'], 1)


if __name__ == '__main__':
    unittest.main()
