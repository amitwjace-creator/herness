"""Read-only research and explicitly configured HTTP tool connections.

Credentials stay in memory (or existing environment bindings). urllib preserves
platform proxy and TLS configuration; redirects never carry authorization.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .state import ProjectStore


class ConnectionError(ValueError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def validate_url(value, local=False):
    if not isinstance(value, str) or not value or len(value) > 4000:
        raise ConnectionError('Enter a valid URL under 4000 characters')
    parsed = urllib.parse.urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ConnectionError('URLs must not include credentials or fragments')
    if parsed.scheme != 'https' and not (local and parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}):
        raise ConnectionError('Use HTTPS; explicitly configured local services may use loopback HTTP')
    return value


def public_destination(url, allow_loopback=False):
    """Reject private fetch destinations, including each redirect destination.

    Uses normal system DNS without replacing the session proxy or TLS trust.
    The configured proxy remains responsible for its own DNS/network routing.
    """
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname.rstrip('.').lower()
    if hostname == 'localhost' or hostname.endswith(('.localhost', '.local', '.internal')):
        if not allow_loopback or hostname != 'localhost':
            raise ConnectionError('Research URLs must point to public internet hosts')
    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(item[4][0])
                         for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
        except (OSError, ValueError):
            raise ConnectionError('Could not validate the public research destination') from None
    if not addresses or any(not address.is_global and not (allow_loopback and address.is_loopback) for address in addresses):
        raise ConnectionError('Research URLs must point to public internet hosts')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ReadablePage(HTMLParser):
    """Extract readable prose, leaving scripts, styles, and navigation behind."""
    ignored = {'script', 'style', 'nav', 'header', 'footer', 'noscript', 'svg'}
    blocks = {'p', 'div', 'section', 'article', 'li', 'h1', 'h2', 'h3', 'h4', 'tr', 'br'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.title_depth = 0
        self.title = []
        self.parts = []
        self.published = None

    def handle_starttag(self, tag, attrs):
        if tag in self.ignored:
            self.depth += 1
        if tag == 'title':
            self.title_depth += 1
        if tag == 'meta':
            attributes = dict(attrs)
            if (attributes.get('property') or attributes.get('name')) in {'article:published_time', 'date', 'datePublished'}:
                self.published = attributes.get('content')
        if not self.depth and tag in self.blocks:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.ignored and self.depth:
            self.depth -= 1
        if tag == 'title' and self.title_depth:
            self.title_depth -= 1
        if not self.depth and tag in self.blocks:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.title_depth:
            self.title.append(data)
        elif not self.depth:
            self.parts.append(data)

    def result(self):
        text = '\n'.join(' '.join(line.split()) for line in ''.join(self.parts).splitlines() if line.strip())
        return ' '.join(''.join(self.title).split()), text, self.published


class Connections:
    SEARCH_ENDPOINTS = {'brave': 'https://api.search.brave.com/res/v1/web/search', 'tavily': 'https://api.tavily.com/search'}

    def __init__(self, root, *, allow_local_fetch=False):
        self.store = ProjectStore(root)
        self.lock = threading.RLock()
        self.keys = {}
        self.allow_local_fetch = allow_local_fetch
        self.config = json.loads(self.store.read('connections.json', '{"search":{"provider":"brave"},"mcp":{}}'))
        self.sources = json.loads(self.store.read('sources.json', '{}'))
        if not isinstance(self.config, dict) or not isinstance(self.sources, dict):
            raise ConnectionError('Invalid saved connection configuration')
        self.config.setdefault('search', {'provider': 'brave'})
        self.config.setdefault('mcp', {})
        if (not isinstance(self.config['search'], dict)
                or self.config['search'].get('provider') not in self.SEARCH_ENDPOINTS
                or not isinstance(self.config['mcp'], dict)):
            raise ConnectionError('Invalid saved connection configuration')
        self.opener = urllib.request.build_opener(NoRedirect)

    def _save(self):
        self.store.write('connections.json', json.dumps(self.config, indent=2))

    def _source_save(self):
        self.store.write('sources.json', json.dumps(self.sources, indent=2))

    def _request(self, url, payload=None, headers=None, *, local=False, allow_redirect=False, public_only=False):
        validate_url(url, local=local)
        request_headers = {'User-Agent': 'Herness/0.3 (research)', 'Accept': 'application/json, text/html;q=0.9, text/plain;q=0.8'}
        request_headers.update(headers or {})
        encoded = None if payload is None else json.dumps(payload).encode()
        if encoded is not None:
            request_headers.setdefault('Content-Type', 'application/json')
        for _ in range(4):
            if public_only:
                public_destination(url, allow_loopback=self.allow_local_fetch)
            request = urllib.request.Request(url, data=encoded, headers=request_headers)
            try:
                with self.opener.open(request, timeout=20) as response:
                    raw = response.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise ConnectionError('Response exceeds the 2 MB limit')
                    return raw, response.headers, url
            except urllib.error.HTTPError as exc:
                if allow_redirect and exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get('Location')
                    if not location:
                        raise ConnectionError('Redirect has no destination') from None
                    url = validate_url(urllib.parse.urljoin(url, location), local=local)
                    # Only unauthenticated fetches use automatic redirects.
                    request_headers.pop('Authorization', None)
                    continue
                raise ConnectionError(f'Remote service returned HTTP {exc.code}; check destination access and credentials') from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise ConnectionError('Could not reach the remote service through the configured network') from None
        raise ConnectionError('Too many redirects')

    @staticmethod
    def _json(raw):
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ConnectionError('Remote service did not return valid JSON') from None
        return value

    def fetch(self, url, collection=None):
        raw, headers, final = self._request(url, local=self.allow_local_fetch, allow_redirect=True, public_only=True)
        media_type = headers.get_content_type()
        charset = headers.get_content_charset() or 'utf-8'
        try:
            text = raw.decode(charset, errors='replace')
        except LookupError:
            text = raw.decode('utf-8', errors='replace')
        title, published = '', None
        if media_type in {'text/html', 'application/xhtml+xml'}:
            parser = ReadablePage()
            parser.feed(text)
            title, text, published = parser.result()
        elif media_type not in {'text/plain', 'text/markdown', 'application/json', 'text/csv'}:
            raise ConnectionError('This URL does not contain supported text or HTML content')
        if not text.strip():
            raise ConnectionError('No readable text found; this page may require browser rendering')
        truncated = len(text) > 100_000
        text = text[:100_000]
        source_id = hashlib.sha256(final.encode()).hexdigest()[:16]
        source = {'id': source_id, 'url': final, 'requested_url': url, 'domain': urllib.parse.urlsplit(final).hostname,
                  'title': title or final, 'retrieved_at': now(), 'published_at': published, 'text': text,
                  'truncated': truncated, 'content_hash': hashlib.sha256(text.encode()).hexdigest(),
                  'citations': [{'id': f'{source_id}:{index + 1}', 'quote': paragraph[:600], 'url': final}
                                for index, paragraph in enumerate(text.splitlines()[:12]) if paragraph.strip()]}
        if collection is not None:
            self.save_source(source, collection)
        return source

    def save_source(self, source, collection='Research'):
        if not isinstance(collection, str) or not collection.strip() or len(collection) > 100:
            raise ConnectionError('Collection name must be 1–100 characters')
        if not isinstance(source, dict) or not isinstance(source.get('url'), str) or not isinstance(source.get('text'), str):
            raise ConnectionError('A fetched source is required')
        source = {key: source.get(key) for key in ('id', 'url', 'requested_url', 'domain', 'title', 'retrieved_at', 'published_at', 'text', 'truncated', 'content_hash', 'citations')}
        validate_url(source['url'], local=self.allow_local_fetch)
        source['id'] = hashlib.sha256(source['url'].encode()).hexdigest()[:16]
        source['text'] = source['text'][:100_000]
        source['collection'] = collection.strip()
        with self.lock:
            if source['id'] not in self.sources and len(self.sources) >= 500:
                raise ConnectionError('At most 500 saved research sources are supported')
            self.sources[source['id']] = source
            self._source_save()
        return source

    def source_action(self, data):
        action = data.get('action', 'list')
        identity = data.get('id')
        if identity is not None and not isinstance(identity, str):
            raise ConnectionError('Source id must be a string')
        if action == 'save':
            return {'source': self.save_source(data.get('source'), data.get('collection', 'Research'))}
        with self.lock:
            if action == 'delete':
                if identity not in self.sources:
                    raise ConnectionError('Source not found')
                del self.sources[identity]
                self._source_save()
                return {'deleted': True}
            selected = self.sources.get(identity)
            sources = [dict(s) for s in self.sources.values() if not data.get('collection') or s.get('collection') == data['collection']]
        if action == 'refresh':
            if not selected:
                raise ConnectionError('Source not found')
            refreshed = self.fetch(selected['url'], selected['collection'])
            return {'source': refreshed, 'changed': refreshed['content_hash'] != selected['content_hash'],
                    'previous_retrieved_at': selected.get('retrieved_at')}
        if action == 'export':
            lines = ['# Research sources', '']
            for index, source in enumerate(sources, 1):
                lines += [f"## [{index}] {source['title']}", f"Source: {source['url']}",
                          f"Retrieved: {source.get('retrieved_at', '')}", '', source['text'], '']
            return {'filename': 'research.md', 'mime': 'text/markdown', 'content': '\n'.join(lines)}
        if action == 'list':
            return {'sources': sources}
        raise ConnectionError('Unknown source action')

    def _search_key(self, provider):
        return self.keys.get('search:' + provider) or os.environ.get('HERNESS_' + provider.upper() + '_API_KEY', '')

    def configure_search(self, data):
        provider = data.get('provider', 'brave')
        if not isinstance(provider, str) or provider not in self.SEARCH_ENDPOINTS:
            raise ConnectionError('Supported search providers: brave, tavily')
        key = data.get('api_key', '')
        if not isinstance(key, str) or len(key) > 4096 or '\n' in key or '\r' in key:
            raise ConnectionError('Invalid API key')
        with self.lock:
            if data.get('clear_key'):
                self.keys.pop('search:' + provider, None)
            elif key:
                self.keys['search:' + provider] = key
            self.config['search'] = {'provider': provider}
            self._save()
        return {'provider': provider, 'key_present': bool(self._search_key(provider))}

    def search(self, data):
        query = data.get('query')
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ConnectionError('Search query must contain 1–2000 characters')
        domains = data.get('domains', [])
        if not isinstance(domains, list) or len(domains) > 10 or any(not isinstance(d, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', d) for d in domains):
            raise ConnectionError('Use at most 10 domain names without schemes or paths')
        domains = [domain.lower() for domain in domains]
        start, end = data.get('from_date'), data.get('to_date')
        try:
            for date in (start, end):
                if date is not None:
                    dt.date.fromisoformat(date)
        except (ValueError, TypeError):
            raise ConnectionError('Dates must use YYYY-MM-DD') from None
        if start and end and start > end:
            raise ConnectionError('Start date must precede end date')
        provider = self.config['search']['provider']
        key = self._search_key(provider)
        if not key:
            raise ConnectionError(f'Configure a {provider} search key before searching')
        endpoint = self.SEARCH_ENDPOINTS[provider]
        if provider == 'brave':
            requested_query = query + (' (' + ' OR '.join('site:' + d for d in domains) + ')' if domains else '')
            params = {'q': requested_query, 'count': 10}
            if start or end:
                params['freshness'] = (start or '1970-01-01') + 'to' + (end or dt.date.today().isoformat())
            raw, _, _ = self._request(endpoint + '?' + urllib.parse.urlencode(params), headers={'X-Subscription-Token': key}, local=self.allow_local_fetch)
            document = self._json(raw)
            if not isinstance(document, dict) or not isinstance(document.get('web', {}), dict):
                raise ConnectionError('Search provider returned an invalid result list')
            hits = document.get('web', {}).get('results', [])
        else:
            payload = {'query': query, 'max_results': 10, 'include_answer': False}
            if domains:
                payload['include_domains'] = domains
            if start:
                payload['start_date'] = start
            if end:
                payload['end_date'] = end
            raw, _, _ = self._request(endpoint, payload, {'Authorization': 'Bearer ' + key}, local=self.allow_local_fetch)
            document = self._json(raw)
            if not isinstance(document, dict):
                raise ConnectionError('Search provider returned an invalid result list')
            hits = document.get('results', [])
        if not isinstance(hits, list):
            raise ConnectionError('Search provider returned an invalid result list')
        results = []
        for hit in hits[:30]:
            if not isinstance(hit, dict):
                continue
            url = hit.get('url', '')
            if not isinstance(url, str):
                continue
            try:
                parsed = urllib.parse.urlsplit(url)
            except ValueError:
                continue
            if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
                continue
            if domains and not any(parsed.hostname == d or parsed.hostname.endswith('.' + d) for d in domains):
                continue
            results.append({'title': str(hit.get('title') or url)[:500], 'url': url, 'domain': parsed.hostname,
                            'snippet': str(hit.get('description') or hit.get('content') or '')[:2000],
                            'published_at': hit.get('page_age') or hit.get('published_date'),
                            'retrieved_at': now()})
        return {'query': query, 'provider': provider, 'results': results[:10],
                'date_filter': {'from': start, 'to': end, 'enforced_by': provider} if start or end else None}

    def configure_mcp(self, data):
        name = data.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
            raise ConnectionError('Server name must contain 1–64 letters, digits, underscores, or hyphens')
        with self.lock:
            if data.get('remove'):
                self.config['mcp'].pop(name, None)
                self.keys.pop('mcp:' + name, None)
                self._save()
                return {'removed': True}
            old = self.config['mcp'].get(name, {})
            url = validate_url(data.get('url', old.get('url')), local=True)
            key = data.get('api_key', '')
            if not isinstance(key, str) or len(key) > 4096 or '\r' in key or '\n' in key:
                raise ConnectionError('Invalid bearer token')
            enabled = data.get('enabled', True)
            if not isinstance(enabled, bool):
                raise ConnectionError('enabled must be a boolean')
            if old.get('url') != url or data.get('clear_key'):
                self.keys.pop('mcp:' + name, None)
            if key:
                self.keys['mcp:' + name] = key
            self.config['mcp'][name] = {'name': name, 'url': url, 'enabled': enabled}
            self._save()
        return {'server': self.mcp_public(name)}

    def mcp_public(self, name):
        return {**self.config['mcp'][name], 'key_present': bool(self.keys.get('mcp:' + name))}

    def _rpc(self, server, method, params=None, session=None, notification=False):
        identity = hashlib.sha256(os.urandom(16)).hexdigest()[:12]
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        if not notification:
            payload['id'] = identity
        headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-03-26'}
        with self.lock:
            if self.config['mcp'].get(server['name']) != server:
                raise ConnectionError('MCP server configuration changed; retry the request')
            token = self.keys.get('mcp:' + server['name'])
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if session:
            headers['Mcp-Session-Id'] = session
        raw, response_headers, _ = self._request(server['url'], payload, headers, local=True)
        if notification:
            return None, response_headers.get('Mcp-Session-Id') or session
        if response_headers.get_content_type() == 'text/event-stream':
            candidates = []
            for event in raw.decode('utf-8').split('\n\n'):
                joined = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
                if joined:
                    candidates.append(self._json(joined))
            document = next((item for item in candidates if isinstance(item, dict) and item.get('id') == identity), None)
        else:
            document = self._json(raw)
        if not isinstance(document, dict) or document.get('id') != identity:
            raise ConnectionError('MCP response did not match the request')
        if document.get('error'):
            raise ConnectionError('MCP server returned an error for ' + method)
        if 'result' not in document:
            raise ConnectionError('MCP response is missing a result')
        return document['result'], response_headers.get('Mcp-Session-Id') or session

    def _mcp_ready(self, name):
        if not isinstance(name, str):
            raise ConnectionError('MCP server name is required')
        server = self.config['mcp'].get(name)
        if not server or not server['enabled']:
            raise ConnectionError('Enable and configure this MCP server first')
        result, session = self._rpc(server, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                                         'clientInfo': {'name': 'Herness', 'version': '0.3'}})
        if not isinstance(result, dict) or not result.get('protocolVersion'):
            raise ConnectionError('MCP initialization returned an invalid result')
        self._rpc(server, 'notifications/initialized', session=session, notification=True)
        return server, session

    def mcp_tools(self, name):
        server, session = self._mcp_ready(name)
        result, _ = self._rpc(server, 'tools/list', {}, session)
        if not isinstance(result, dict) or not isinstance(result.get('tools'), list):
            raise ConnectionError('MCP server returned an invalid tool list')
        return {'server': name, 'tools': result['tools'][:100], 'next_cursor': result.get('nextCursor')}

    def mcp_call(self, data, approve=None):
        name, tool, arguments = data.get('name'), data.get('tool'), data.get('arguments', {})
        if not isinstance(tool, str) or not tool or not isinstance(arguments, dict):
            raise ConnectionError('Tool name and object arguments are required')
        server, session = self._mcp_ready(name)
        with self.lock:
            expected_key = self.keys.get('mcp:' + name)
        listing, _ = self._rpc(server, 'tools/list', {}, session)
        if not isinstance(listing, dict) or not isinstance(listing.get('tools'), list):
            raise ConnectionError('MCP server returned an invalid tool list')
        if tool not in {item['name'] for item in listing['tools']
                        if isinstance(item, dict) and isinstance(item.get('name'), str)}:
            raise ConnectionError('Tool is not listed by this MCP server')
        description = {'kind': 'mcp', 'description': f"Run {tool} on {server['url']}", 'arguments': arguments}
        if approve is not None:
            if not approve({**description, 'description': description['description'] + '\n' + json.dumps(arguments, indent=2)}):
                return {'denied': True}
        elif data.get('confirmed') is not True:
            return {'approval_required': True, 'proposal': description}
        with self.lock:
            if self.config['mcp'].get(name) != server or self.keys.get('mcp:' + name) != expected_key:
                raise ConnectionError('MCP server configuration changed during approval; review the call again')
            result, _ = self._rpc(server, 'tools/call', {'name': tool, 'arguments': arguments}, session)
        return {'server': name, 'tool': tool, 'result': result}

    def github(self, data):
        repo = data.get('repository')
        if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ConnectionError('Repository must use owner/name')
        resource = data.get('resource', 'issues')
        if resource not in {'issues', 'pulls'}:
            raise ConnectionError('Supported GitHub resources: issues, pulls')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        key = os.environ.get('HERNESS_GITHUB_TOKEN', '')
        if key:
            headers['Authorization'] = 'Bearer ' + key
        raw, _, _ = self._request(f'https://api.github.com/repos/{repo}/{resource}?state=open&per_page=50', headers=headers)
        items = self._json(raw)
        if not isinstance(items, list):
            raise ConnectionError('GitHub returned an invalid resource list')
        return {'repository': repo, 'resource': resource, 'items': [
            {key: item.get(key) for key in ('number', 'title', 'html_url', 'state', 'updated_at', 'body')}
            for item in items if isinstance(item, dict)], 'retrieved_at': now()}

    def status(self):
        provider = self.config['search']['provider']
        return {'search': {'provider': provider, 'key_present': bool(self._search_key(provider)), 'supported': list(self.SEARCH_ENDPOINTS)},
                'mcp': [self.mcp_public(name) for name in self.config['mcp']], 'sources': len(self.sources),
                'github': {'configured_token': bool(os.environ.get('HERNESS_GITHUB_TOKEN')), 'capabilities': ['read issues', 'read pull requests']},
                'limitations': ['MCP HTTP JSON and finite SSE responses are supported; stdio and OAuth are not implemented.',
                                'Search needs a Brave or Tavily API key and network permission.',
                                'Calendar, email, Google Drive, and Notion direct adapters are not implemented. Connect a compatible MCP server instead.']}

    def handle(self, method, path, data, state=None):
        path = urllib.parse.urlsplit(path).path
        if not path.startswith('/api/connections/'):
            return None
        data = data or {}
        if not isinstance(data, dict):
            raise ConnectionError('Request body must be a JSON object')
        if method == 'GET':
            if path == '/api/connections/status':
                return self.status()
            if path == '/api/connections/sources':
                return self.source_action({'action': 'list'})
            if path == '/api/connections/mcp':
                return {'servers': [self.mcp_public(name) for name in self.config['mcp']]}
        if method == 'POST':
            if path == '/api/connections/fetch':
                return {'source': self.fetch(data.get('url'), data.get('collection'))}
            if path == '/api/connections/fetch-many':
                urls = data.get('urls')
                if not isinstance(urls, list) or not 1 <= len(urls) <= 8:
                    raise ConnectionError('Provide 1–8 URLs')
                def fetch(url):
                    try:
                        return {'source': self.fetch(url, data.get('collection'))}
                    except ConnectionError as exc:
                        return {'url': url, 'error': str(exc)}
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    return {'results': list(pool.map(fetch, urls))}
            if path == '/api/connections/search/config':
                return self.configure_search(data)
            if path == '/api/connections/search':
                return self.search(data)
            if path == '/api/connections/sources':
                return self.source_action(data)
            if path == '/api/connections/mcp/config':
                return self.configure_mcp(data)
            if path == '/api/connections/mcp/list':
                return self.mcp_tools(data.get('name'))
            if path == '/api/connections/mcp/call':
                return self.mcp_call(data)
            if path == '/api/connections/github':
                return self.github(data)
        return None
