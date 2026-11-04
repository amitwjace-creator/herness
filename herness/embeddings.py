"""Real embedding provider requests with project-local, credential-free settings.

Chat provider settings and credentials are deliberately separate. Ollama needs
its model installed on the configured server; OpenAI-compatible providers need
an embedding-capable model. Neither errors nor missing configuration generate
synthetic replacement vectors.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import threading
import urllib.error
import urllib.parse
import urllib.request

from .state import ProjectStore

MAX_BATCH_TEXTS = 64
MAX_TEXT_CHARACTERS = 32_000
MAX_TEXT_BYTES = 128_000
MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 8_000_000
MAX_DIMENSIONS = 4096
REQUEST_TIMEOUT = 20
DEFAULT_CONFIG = {'protocol': 'ollama', 'base_url': 'http://127.0.0.1:11434',
                  'model': 'nomic-embed-text', 'enabled': False, 'batch_size': 16}
_CONFIG_FIELDS = frozenset(DEFAULT_CONFIG)


class EmbeddingError(ValueError):
    """Safe, user-readable configuration or embedding-provider failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _invalid_json_constant(_):
    raise ValueError('Nonfinite JSON value')


def _validate_config(data):
    protocol = data.get('protocol')
    if not isinstance(protocol, str) or protocol not in {'openai', 'ollama'}:
        raise EmbeddingError('Choose the openai or ollama embedding protocol')
    base_url = data.get('base_url')
    if (not isinstance(base_url, str) or not 1 <= len(base_url) <= 1000 or not base_url.isascii()
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in base_url)
            or '\\' in base_url):
        raise EmbeddingError('Enter a valid embedding API base URL')
    try:
        parsed = urllib.parse.urlsplit(base_url)
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError()
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == 'localhost'
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and loopback):
            raise ValueError()
    except ValueError:
        raise EmbeddingError('Embedding URLs require HTTPS, or explicit loopback HTTP, without credentials, query, or fragment') from None
    model = data.get('model')
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(character) < 32 or ord(character) == 127 for character in model):
        raise EmbeddingError('Enter an embedding model name under 200 characters')
    try:
        model.encode('utf-8')
    except UnicodeError:
        raise EmbeddingError('Embedding model names must contain valid Unicode') from None
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        raise EmbeddingError('Embedding enabled must be a boolean')
    batch_size = data.get('batch_size')
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_BATCH_TEXTS:
        raise EmbeddingError('Embedding batch size must be an integer from 1 to 64')
    return {'protocol': protocol, 'base_url': base_url.rstrip('/'), 'model': model.strip(),
            'enabled': enabled, 'batch_size': batch_size}


def _endpoint(config):
    return config['base_url'] + ('/embeddings' if config['protocol'] == 'openai' else '/api/embed')


def _fingerprint(config):
    identity = {key: config[key] for key in ('protocol', 'base_url', 'model')}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Embeddings:
    def __init__(self, root):
        self.store = ProjectStore(root)
        self.lock = threading.RLock()
        # Keys are scoped to the complete request destination, including path.
        # They never come from the chat client, settings file, or environment.
        self._keys = {}
        self._dimensions = None
        self._generation = 0
        try:
            saved = json.loads(self.store.read('embedding-settings.json', '{}'))
        except (ValueError, OSError):
            raise EmbeddingError('Embedding settings could not be loaded') from None
        if not isinstance(saved, dict) or set(saved) - _CONFIG_FIELDS:
            raise EmbeddingError('Embedding settings contain unsupported fields')
        self.config = _validate_config({**DEFAULT_CONFIG, **saved})

    def public(self):
        with self.lock:
            return {**self.config, 'key_present': bool(self._keys.get(_endpoint(self.config))),
                    'dimensions': self._dimensions, 'max_dimensions': MAX_DIMENSIONS,
                    'max_text_characters': MAX_TEXT_CHARACTERS, 'max_batch_texts': MAX_BATCH_TEXTS,
                    'max_request_bytes': MAX_REQUEST_BYTES}

    def fingerprint(self):
        with self.lock:
            return _fingerprint(self.config)

    def reset_dimensions(self):
        """Forget the expected dimensions after an explicit index reset.

        Invalidate requests already in flight so they cannot reestablish the
        previous dimensions after the caller has cleared its persisted index.
        """
        with self.lock:
            self._dimensions = None
            self._generation += 1
            return self.public()

    def configure(self, data):
        if not isinstance(data, dict) or set(data) - (_CONFIG_FIELDS | {'api_key', 'clear_key'}):
            raise EmbeddingError('Embedding settings must be an object with supported fields')
        key = data.get('api_key', '')
        if (not isinstance(key, str) or len(key) > 4096 or not key.isascii()
                or any(ord(character) < 32 or ord(character) == 127 for character in key)):
            raise EmbeddingError('Enter a valid embedding API key')
        clear = data.get('clear_key', False)
        if not isinstance(clear, bool) or (clear and key):
            raise EmbeddingError('Choose either a new embedding API key or clear key')
        with self.lock:
            config = _validate_config({**self.config, **{name: value for name, value in data.items() if name in _CONFIG_FIELDS}})
            endpoint = _endpoint(config)
            selected_key = '' if clear else key or self._keys.get(endpoint, '')
            self.store.write('embedding-settings.json', json.dumps(config, indent=2))
            identity_changed = _fingerprint(config) != _fingerprint(self.config)
            if identity_changed or config['enabled'] != self.config['enabled']:
                self._generation += 1
            if identity_changed:
                self._dimensions = None
            self.config = config
            if selected_key:
                self._keys[endpoint] = selected_key
            else:
                self._keys.pop(endpoint, None)
            return self.public()

    @staticmethod
    def _texts(texts):
        if not isinstance(texts, list) or not 1 <= len(texts) <= MAX_BATCH_TEXTS:
            raise EmbeddingError('Embed between 1 and 64 texts at a time')
        for text in texts:
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARACTERS:
                raise EmbeddingError('Each embedding text must contain 1–32000 characters')
            try:
                if len(text.encode('utf-8')) > MAX_TEXT_BYTES:
                    raise EmbeddingError('Embedding text exceeds the UTF-8 byte limit')
            except UnicodeError:
                raise EmbeddingError('Embedding text must contain valid Unicode') from None
        return list(texts)

    @staticmethod
    def _vectors(data, protocol, count, expected_dimensions):
        if not isinstance(data, dict) or data.get('error'):
            raise EmbeddingError('The embedding provider returned an invalid response')
        if protocol == 'openai':
            entries = data.get('data')
            if not isinstance(entries, list) or len(entries) != count:
                raise EmbeddingError('The provider returned an unexpected embedding count')
            vectors = [None] * count
            seen = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    raise EmbeddingError('The provider returned an invalid embedding entry')
                index = entry.get('index')
                if isinstance(index, bool) or not isinstance(index, int) or index in seen or not 0 <= index < count:
                    raise EmbeddingError('The provider returned invalid embedding indexes')
                seen.add(index)
                vectors[index] = entry.get('embedding')
        else:
            vectors = data.get('embeddings')
            if not isinstance(vectors, list) or len(vectors) != count:
                raise EmbeddingError('The provider returned an unexpected embedding count')
        validated = []
        dimensions = expected_dimensions
        for vector in vectors:
            if not isinstance(vector, list) or not 1 <= len(vector) <= MAX_DIMENSIONS:
                raise EmbeddingError('Embeddings must have between 1 and 4096 dimensions')
            if dimensions is None:
                dimensions = len(vector)
            if len(vector) != dimensions:
                raise EmbeddingError('The provider changed embedding dimensions; rebuild the index with a consistent model')
            values = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise EmbeddingError('Embedding vectors must contain finite numbers')
                try:
                    value = float(value)
                except (OverflowError, ValueError):
                    raise EmbeddingError('Embedding vectors must contain finite numbers') from None
                if not math.isfinite(value):
                    raise EmbeddingError('Embedding vectors must contain finite numbers')
                values.append(value)
            norm = math.hypot(*values)
            if not math.isfinite(norm) or norm == 0:
                raise EmbeddingError('Embedding vectors must have a finite, nonzero length')
            validated.append(values)
        return validated, dimensions

    @staticmethod
    def _request(config, key, texts, cancel=None):
        payload = {'model': config['model'], 'input': texts}
        if config['protocol'] == 'openai':
            payload['encoding_format'] = 'float'
        else:
            # Avoid Ollama silently truncating text beyond its model context.
            payload['truncate'] = False
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(body) > MAX_REQUEST_BYTES:
            raise EmbeddingError('Embedding request exceeds the 1 MB size limit')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        request = urllib.request.Request(_endpoint(config), data=body, headers=headers)
        response, sock = None, None
        try:
            if cancel:
                cancel.check()
            # build_opener preserves platform proxy and TLS trust configuration.
            response = urllib.request.build_opener(_NoRedirect).open(request, timeout=REQUEST_TIMEOUT)
            if cancel:
                # Preserve the socket reference even if registration notices a
                # cancellation and raises before returning it.
                sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                cancel.register(response)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if cancel:
                cancel.check()
            if len(raw) > MAX_RESPONSE_BYTES:
                raise EmbeddingError('Embedding response exceeds the 8 MB size limit')
            return json.loads(raw, parse_constant=_invalid_json_constant)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if cancel:
                cancel.check()
            if 300 <= status < 400:
                raise EmbeddingError('Embedding endpoint redirects are disabled; configure the final API URL') from None
            raise EmbeddingError(f'Embedding API returned HTTP {status}. Check the model, endpoint, and embedding API key.') from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            if cancel:
                cancel.check()
            raise EmbeddingError('Cannot reach the embedding API. Check the endpoint and connectivity.') from None
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, EmbeddingError):
                raise
            if cancel:
                cancel.check()
            raise EmbeddingError('The embedding provider returned invalid JSON') from None
        finally:
            if cancel:
                cancel.unregister(sock)
            if response:
                response.close()

    def embed(self, texts, cancel=None):
        texts = self._texts(texts)
        # Validate the whole call before sending any paid requests.
        if len(json.dumps(texts, ensure_ascii=False).encode('utf-8')) > MAX_REQUEST_BYTES - 1024:
            raise EmbeddingError('Embedding texts exceed the 1 MB request size limit')
        with self.lock:
            config = dict(self.config)
            if not config['enabled']:
                raise EmbeddingError('Enable and configure an embedding provider first')
            key = self._keys.get(_endpoint(config), '')
            dimensions = self._dimensions
            generation = self._generation
        fingerprint = _fingerprint(config)
        output = []
        for start in range(0, len(texts), config['batch_size']):
            if cancel:
                cancel.check()
            with self.lock:
                if generation != self._generation:
                    raise EmbeddingError('Embedding settings or index changed during the request; retry with the current model')
            batch = texts[start:start + config['batch_size']]
            data = self._request(config, key, batch, cancel)
            vectors, dimensions = self._vectors(data, config['protocol'], len(batch), dimensions)
            output.extend(vectors)
        with self.lock:
            if generation != self._generation or fingerprint != _fingerprint(self.config) or not self.config['enabled']:
                raise EmbeddingError('Embedding settings changed during the request; retry with the current model')
            if self._dimensions is not None and dimensions != self._dimensions:
                raise EmbeddingError('The provider changed embedding dimensions; rebuild the index with a consistent model')
            self._dimensions = dimensions
        return output
