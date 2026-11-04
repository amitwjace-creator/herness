"""Persistent per-project cosine search using SQLite and the real sqlite-vec index.

Embedding generation belongs to the caller. An index binds to one embedding
identity and dimensionality until explicitly cleared; identities must describe
the model/configuration and must never contain provider credentials.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import sqlite3
import stat
import struct
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

_SCHEMA_VERSION = 1
_APPLICATION_ID = 0x48455256
_MAX_DIMENSIONS = 4096
_MAX_CHUNKS = 100_000
_MAX_SOURCE_CHUNKS = 2000
_LOCK = threading.RLock()


class VectorStoreUnavailable(ValueError):
    """An optional vector dependency is missing or cannot load."""


def _string(value, name, maximum, empty=False):
    if not isinstance(value, str) or len(value.encode('utf-8')) > maximum or '\x00' in value or (not empty and not value.strip()):
        raise ValueError(f'{name} must be text of at most {maximum} bytes')
    return value


def normalize_vector(vector, dimensions=None):
    """Return little-endian normalized float32 bytes without overflow in norms."""
    if not isinstance(vector, (list, tuple)) or not 1 <= len(vector) <= _MAX_DIMENSIONS:
        raise ValueError('Vectors must contain 1–4096 finite numbers')
    if dimensions is not None and len(vector) != dimensions:
        raise ValueError(f'Embedding dimension mismatch: expected {dimensions}, got {len(vector)}')
    numbers = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('Vectors must contain finite numbers, not booleans or strings')
        try:
            number = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError('Vector values must be finite numbers') from exc
        if not math.isfinite(number):
            raise ValueError('Vector values must be finite numbers')
        numbers.append(number)
    scale = max(map(abs, numbers))
    if scale == 0:
        raise ValueError('Zero vectors have no cosine similarity')
    scaled = [value / scale for value in numbers]
    norm = math.sqrt(math.fsum(value * value for value in scaled))
    return struct.pack('<' + str(len(numbers)) + 'f', *(value / norm for value in scaled))


class VectorStore:
    """One private database and namespace per project; connections are short-lived."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError('Vector-store project root must be an existing directory')
        self.directory = self.root / '.herness'
        self.path = self.directory / 'vectors.sqlite3'

    @staticmethod
    def _extension():
        try:
            return importlib.import_module('sqlite_vec')
        except ImportError as exc:
            raise VectorStoreUnavailable('Vector search requires sqlite-vec. Install the project dependencies or run: pip install sqlite-vec') from exc

    def _secure_paths(self, create=False):
        if self.directory.is_symlink():
            raise ValueError('Vector-state directory must not be a symlink')
        if not self.directory.exists():
            if not create:
                return
            self.directory.mkdir(mode=0o700)
        if not self.directory.is_dir():
            raise ValueError('Vector-state path must be a directory')
        os.chmod(self.directory, 0o700)
        for suffix in ('', '-journal', '-wal', '-shm'):
            candidate = Path(str(self.path) + suffix)
            try:
                details = candidate.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_nlink != 1:
                raise ValueError('Vector database and sidecars must be regular private files, not links')
            os.chmod(candidate, 0o600)
        if create and not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except FileExistsError:
                self._secure_paths()
            else:
                os.close(descriptor)

    @contextmanager
    def _connection(self):
        extension = self._extension()
        with _LOCK:
            self._secure_paths(create=True)
            connection = None
            try:
                uri = 'file:' + quote(str(self.path), safe='/') + '?mode=rw'
                connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA trusted_schema=OFF')
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA busy_timeout=5000')
                # DELETE journaling keeps all durable data in the named database;
                # sidecar paths are validated before SQLite can create/open them.
                connection.execute('PRAGMA journal_mode=DELETE')
                connection.execute('PRAGMA synchronous=FULL')
                try:
                    connection.enable_load_extension(True)
                    extension.load(connection)
                except (AttributeError, OSError, sqlite3.Error) as exc:
                    raise VectorStoreUnavailable('sqlite-vec could not load into this Python SQLite build') from exc
                finally:
                    try:
                        connection.enable_load_extension(False)
                    except AttributeError:
                        pass
                self._initialize(connection)
                yield connection
            except sqlite3.Error as exc:
                raise ValueError('Vector database operation failed: ' + str(exc)) from exc
            finally:
                if connection is not None:
                    connection.close()
                self._secure_paths()

    @staticmethod
    @contextmanager
    def _transaction(connection):
        connection.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        else:
            connection.execute('COMMIT')

    def _initialize(self, connection):
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        application = connection.execute('PRAGMA application_id').fetchone()[0]
        if version == 0 and application == 0:
            existing = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if existing:
                raise ValueError('Existing file is not a Herness vector database')
            with self._transaction(connection):
                connection.execute('CREATE TABLE vector_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID')
                connection.execute('CREATE TABLE sources (source_id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, source_hash TEXT NOT NULL, updated REAL NOT NULL)')
                connection.execute('CREATE TABLE chunks (rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE, kind TEXT NOT NULL, title TEXT NOT NULL, location TEXT NOT NULL, text TEXT NOT NULL, source_hash TEXT NOT NULL, chunk_index INTEGER NOT NULL)')
                connection.execute('CREATE INDEX chunks_source ON chunks(source_id)')
                connection.execute('CREATE INDEX chunks_kind ON chunks(kind)')
                connection.execute('INSERT INTO vector_metadata(key,value) VALUES (?,?)', ('namespace', uuid.uuid4().hex))
                connection.execute(f'PRAGMA application_id={_APPLICATION_ID}')
                connection.execute(f'PRAGMA user_version={_SCHEMA_VERSION}')
        elif version != _SCHEMA_VERSION or application != _APPLICATION_ID:
            raise ValueError('Unsupported vector database schema version or application identity')
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise ValueError('Unexpected triggers in vector database')
        metadata = self._metadata(connection)
        if not metadata.get('namespace'):
            raise ValueError('Vector database namespace is missing')
        if 'dimensions' in metadata:
            try:
                dimensions = int(metadata['dimensions'])
            except ValueError as exc:
                raise ValueError('Invalid stored vector dimensions') from exc
            if not 1 <= dimensions <= _MAX_DIMENSIONS:
                raise ValueError('Invalid stored vector dimensions')
            key = metadata.get('embedding_key')
            if not isinstance(key, str) or hashlib.sha256(key.encode()).hexdigest() != metadata.get('embedding_fingerprint'):
                raise ValueError('Invalid stored embedding identity')

    @staticmethod
    def _metadata(connection):
        return {row['key']: row['value'] for row in connection.execute('SELECT key,value FROM vector_metadata')}

    def stats(self):
        try:
            with self._connection() as connection:
                metadata = self._metadata(connection)
                return {'available': True, 'backend': 'sqlite-vec', 'schema_version': _SCHEMA_VERSION,
                        'sqlite_vec_version': connection.execute('SELECT vec_version()').fetchone()[0],
                        'chunks': connection.execute('SELECT COUNT(*) FROM chunks').fetchone()[0],
                        'sources': connection.execute('SELECT COUNT(*) FROM sources').fetchone()[0],
                        'dimensions': int(metadata['dimensions']) if 'dimensions' in metadata else None,
                        'embedding_key': metadata.get('embedding_key'), 'embedding_fingerprint': metadata.get('embedding_fingerprint'),
                        'namespace': metadata['namespace'], 'scope': 'project', 'metric': 'cosine'}
        except VectorStoreUnavailable as exc:
            return {'available': False, 'backend': 'sqlite-vec', 'chunks': 0, 'sources': 0,
                    'dimensions': None, 'embedding_key': None, 'error': str(exc)}

    @staticmethod
    def _prepare_chunks(source_id, chunks):
        _string(source_id, 'Source ID', 512)
        if not isinstance(chunks, list) or len(chunks) > _MAX_SOURCE_CHUNKS:
            raise ValueError('A source can contain at most 2000 chunks')
        prepared, identities, positions = [], set(), set()
        source_hash = None
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise ValueError('Chunk metadata must be an object')
            identity = _string(chunk.get('id'), 'Chunk ID', 512)
            if identity in identities:
                raise ValueError('Chunk IDs must be unique')
            identities.add(identity)
            if chunk.get('source_id', source_id) != source_id:
                raise ValueError('Chunk source ID does not match the replacement source')
            position = chunk.get('chunk_index', index)
            if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= 1_000_000 or position in positions:
                raise ValueError('Chunk indices must be unique nonnegative integers')
            positions.add(position)
            entry = {'id': identity, 'source_id': source_id,
                     'kind': _string(chunk.get('kind', 'document'), 'Source kind', 64),
                     'title': _string(chunk.get('title', source_id), 'Source title', 2000, empty=True),
                     'location': _string(chunk.get('location', ''), 'Chunk location', 2000, empty=True),
                     'text': _string(chunk.get('text'), 'Chunk text', 32000), 'chunk_index': position}
            if prepared and (entry['kind'], entry['title']) != (prepared[0]['kind'], prepared[0]['title']):
                raise ValueError('Chunks for a source must share their kind and title')
            if 'source_hash' in chunk:
                provided = _string(chunk['source_hash'], 'Source hash', 256)
                if source_hash is not None and source_hash != provided:
                    raise ValueError('Chunks for a source must share their source hash')
                source_hash = provided
            prepared.append(entry)
        if source_hash is None:
            source_hash = hashlib.sha256(json.dumps([entry['text'] for entry in prepared], ensure_ascii=False).encode()).hexdigest()
        for entry in prepared:
            entry['source_hash'] = source_hash
        return prepared

    def replace_source(self, source_id, chunks, vectors, embedding_key):
        """Validate the complete batch, then replace metadata and vec0 rows atomically."""
        _string(embedding_key, 'Embedding identity', 512)
        prepared = self._prepare_chunks(source_id, chunks)
        if not isinstance(vectors, list) or len(vectors) != len(prepared):
            raise ValueError('Each chunk requires exactly one embedding vector')
        dimensions = len(vectors[0]) if vectors and isinstance(vectors[0], (list, tuple)) else None
        normalized = [normalize_vector(vector, dimensions) for vector in vectors]
        with self._connection() as connection, self._transaction(connection):
            metadata = self._metadata(connection)
            if metadata.get('embedding_key') not in (None, embedding_key):
                raise ValueError('Embedding identity mismatch; clear and reindex before changing models')
            stored_dimensions = int(metadata['dimensions']) if 'dimensions' in metadata else None
            if stored_dimensions is not None and dimensions is not None and stored_dimensions != dimensions:
                raise ValueError(f'Embedding dimension mismatch: expected {stored_dimensions}, got {dimensions}')
            existing = connection.execute('SELECT COUNT(*) FROM chunks WHERE source_id=?', (source_id,)).fetchone()[0]
            count = connection.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
            if count - existing + len(prepared) > _MAX_CHUNKS:
                raise ValueError('Project vector database is limited to 100000 chunks')
            if dimensions is not None and stored_dimensions is None:
                connection.execute(f'CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{dimensions}] distance_metric=cosine)')
                connection.executemany('INSERT INTO vector_metadata(key,value) VALUES (?,?)',
                    [('dimensions', str(dimensions)), ('embedding_key', embedding_key),
                     ('embedding_fingerprint', hashlib.sha256(embedding_key.encode()).hexdigest())])
            if stored_dimensions is not None or dimensions is not None:
                connection.execute('DELETE FROM vec_chunks WHERE rowid IN (SELECT rowid FROM chunks WHERE source_id=?)', (source_id,))
            connection.execute('DELETE FROM sources WHERE source_id=?', (source_id,))
            if prepared:
                first = prepared[0]
                connection.execute('INSERT INTO sources(source_id,kind,title,source_hash,updated) VALUES (?,?,?,?,?)',
                                   (source_id, first['kind'], first['title'], first['source_hash'], time.time()))
                for chunk, vector in zip(prepared, normalized):
                    cursor = connection.execute('INSERT INTO chunks(id,source_id,kind,title,location,text,source_hash,chunk_index) VALUES (?,?,?,?,?,?,?,?)',
                        tuple(chunk[key] for key in ('id', 'source_id', 'kind', 'title', 'location', 'text', 'source_hash', 'chunk_index')))
                    connection.execute('INSERT INTO vec_chunks(rowid,embedding) VALUES (?,?)', (cursor.lastrowid, vector))
            return {'source_id': source_id, 'chunks': len(prepared), 'dimensions': dimensions or stored_dimensions,
                    'embedding_key': embedding_key if dimensions is not None else metadata.get('embedding_key')}

    @staticmethod
    def _filter(values, label, maximum):
        if values is None:
            return None
        if not isinstance(values, list) or len(values) > 1000:
            raise ValueError(f'{label} must be a list with at most 1000 values')
        return list(dict.fromkeys(_string(value, label, maximum) for value in values))

    def search(self, vector, k=5, kinds=None, source_ids=None):
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100:
            raise ValueError('Search result count must be 1–100')
        kinds = self._filter(kinds, 'Source kinds', 64)
        source_ids = self._filter(source_ids, 'Source IDs', 512)
        packed = normalize_vector(vector)
        with self._connection() as connection:
            metadata = self._metadata(connection)
            dimensions = int(metadata['dimensions']) if 'dimensions' in metadata else None
            result = {'matches': [], 'dimensions': dimensions, 'embedding_key': metadata.get('embedding_key')}
            if dimensions is None:
                return result
            if len(vector) != dimensions:
                raise ValueError(f'Embedding dimension mismatch: expected {dimensions}, got {len(vector)}')
            if kinds == [] or source_ids == []:
                return result
            clauses, parameters = [], []
            for column, values in [('kind', kinds), ('source_id', source_ids)]:
                if values is not None:
                    clauses.append(column + ' IN (' + ','.join('?' for _ in values) + ')')
                    parameters.extend(values)
            constraint = ''
            if clauses:
                constraint = ' AND v.rowid IN (SELECT rowid FROM chunks WHERE ' + ' AND '.join(clauses) + ')'
            # Filtering is inside the KNN query, so nearest excluded rows do not
            # consume the result limit. vec0 performs actual cosine search.
            rows = connection.execute('SELECT c.id,c.source_id,c.kind,c.title,c.location,c.text,c.source_hash,c.chunk_index,v.distance '
                'FROM vec_chunks v JOIN chunks c ON c.rowid=v.rowid WHERE v.embedding MATCH ? AND v.k=?' +
                constraint + ' ORDER BY v.distance,c.id', [packed, k, *parameters]).fetchall()
            for row in rows:
                match = dict(row)
                match['distance'] = max(0.0, min(2.0, float(match['distance'])))
                match['score'] = 1.0 - match['distance']
                result['matches'].append(match)
            return result

    def list_sources(self):
        with self._connection() as connection:
            rows = connection.execute('SELECT s.source_id,s.kind,s.title,s.source_hash,s.updated,COUNT(c.rowid) AS chunks '
                                      'FROM sources s JOIN chunks c ON c.source_id=s.source_id GROUP BY s.source_id ORDER BY s.source_id')
            return {'sources': [dict(row) for row in rows]}

    def delete_source(self, source_id):
        _string(source_id, 'Source ID', 512)
        with self._connection() as connection, self._transaction(connection):
            count = connection.execute('SELECT COUNT(*) FROM chunks WHERE source_id=?', (source_id,)).fetchone()[0]
            if 'dimensions' in self._metadata(connection):
                connection.execute('DELETE FROM vec_chunks WHERE rowid IN (SELECT rowid FROM chunks WHERE source_id=?)', (source_id,))
            connection.execute('DELETE FROM sources WHERE source_id=?', (source_id,))
            return {'source_id': source_id, 'deleted': count}

    def clear(self):
        with self._connection() as connection, self._transaction(connection):
            count = connection.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
            if 'dimensions' in self._metadata(connection):
                connection.execute('DROP TABLE vec_chunks')
            connection.execute('DELETE FROM sources')
            connection.execute("DELETE FROM vector_metadata WHERE key!='namespace'")
            return {'cleared': count}
