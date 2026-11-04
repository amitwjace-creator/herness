"""Credential-free model profiles and discovery at the active provider only."""
from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .core import ModelError, OpenAIClient
from .providers import create_client
from .state import ProjectStore


class ModelSettings:
    def __init__(self, root):
        self.store = ProjectStore(root)
        self.lock = threading.RLock()
        self.profiles = json.loads(self.store.read('model-profiles.json', '{}'))
        if not isinstance(self.profiles, dict):
            raise ValueError('Invalid model profiles')

    def list(self):
        return {'profiles': list(self.profiles.values()),
                'note': 'Profiles contain endpoint, model, and optional user-provided prices. API keys remain in memory.'}

    def profile(self, data, state):
        if not isinstance(data, dict):
            raise ValueError('Model profile must be an object')
        action = data.get('action', 'save')
        if not isinstance(action, str):
            raise ValueError('Model action must be text')
        if action == 'list':
            return self.list()
        identity = data.get('id')
        if identity is not None and (not isinstance(identity, str) or len(identity) > 100):
            raise ValueError('Invalid model profile ID')
        if action in {'select', 'delete'}:
            if identity not in self.profiles:
                raise ValueError('Model profile not found')
            if action == 'delete':
                del self.profiles[identity]
            else:
                if state is None:
                    raise ValueError('Selecting a model requires an active workspace')
                with state.lock:
                    if state.busy:
                        raise ValueError('Finish the active task before switching models')
                    profile = self.profiles[identity]
                    options = {key: value for key, value in state.run_options.items()
                               if key not in {'max_output_tokens', 'max_context_tokens', 'input_usd_per_million', 'output_usd_per_million'}}
                    options.update(profile['defaults'])
                    if options.get('max_cost_usd') is not None and not any(options.get(key) for key in ('input_usd_per_million', 'output_usd_per_million')):
                        raise ValueError('This profile needs token prices before it can use the active task budget')
                    state.configure(profile['config'], persist=True)
                    state.run_options = options
                    state.store.write('run-options.json', json.dumps(state.run_options, indent=2))
                return {'selected': identity, 'settings': state.settings_public(), 'run_options': state.run_options}
        elif action == 'save':
            if len(self.profiles) >= 100 and identity not in self.profiles:
                raise ValueError('Save at most 100 model profiles')
            name = data.get('name')
            if not isinstance(name, str) or not name.strip() or len(name) > 100:
                raise ValueError('Enter a profile name under 100 characters')
            current = state.model_config if state is not None else {}
            config = {key: data.get(key, current.get(key)) for key in ('protocol', 'model', 'base_url')}
            client = create_client(config['protocol'], config['model'], config['base_url'])
            config['base_url'], config['demo'] = client.base_url, False
            defaults = data.get('defaults', {})
            if not isinstance(defaults, dict):
                raise ValueError('Profile defaults must be an object')
            allowed = {'max_output_tokens': (16, 64000), 'max_context_tokens': (100, 2000000),
                       'input_usd_per_million': (0, 1000), 'output_usd_per_million': (0, 1000)}
            clean = {}
            for key, value in defaults.items():
                if key not in allowed:
                    raise ValueError('Unsupported profile default: ' + key)
                low, high = allowed[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError('Invalid profile default: ' + key)
                clean[key] = int(value) if key.endswith('tokens') else value
            if identity is not None and identity not in self.profiles:
                raise ValueError('Model profile not found')
            identity = identity or uuid.uuid4().hex
            self.profiles[identity] = {'id': identity, 'name': name.strip(), 'config': config, 'defaults': clean}
        else:
            raise ValueError('Choose save, list, select, or delete')
        self.store.write('model-profiles.json', json.dumps(self.profiles, indent=2))
        return {'profile': self.profiles.get(identity), **self.list()}

    def discover(self, state, cursor=None):
        if state is None or state.demo or not isinstance(state.client, OpenAIClient):
            raise ValueError('Configure a live provider before discovering models')
        # Only the current validated client decides destination and headers. The
        # request cannot supply a new URL or borrow another provider's API key.
        with state.lock:
            client = state.client
            base_url, protocol = client.base_url, client.protocol
            headers = client.request_headers()
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2000):
            raise ValueError('Invalid discovery cursor')
        url = base_url + '/models'
        if cursor:
            if protocol == 'gemini':
                url += '?' + urllib.parse.urlencode({'pageToken': cursor})
            elif protocol == 'anthropic':
                url += '?' + urllib.parse.urlencode({'after_id': cursor})
            else:
                raise ValueError('This provider does not support discovery pagination')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError('Model list exceeded the response size limit')
                result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise ModelError(f'Model discovery returned HTTP {exc.code}. This provider may not support model listing.') from None
        except (urllib.error.URLError, OSError):
            raise ModelError('Cannot reach the current provider for model discovery.') from None
        except (json.JSONDecodeError, UnicodeError):
            raise ModelError('The provider returned an invalid model list.') from None
        if not isinstance(result, dict):
            raise ModelError('The provider returned an invalid model list.')
        items = result.get('models' if protocol == 'gemini' else 'data')
        if not isinstance(items, list):
            raise ModelError('The provider returned an invalid model list.')
        models = []
        for item in items[:1000]:
            if not isinstance(item, dict):
                continue
            identity = item.get('name' if protocol == 'gemini' else 'id')
            if not isinstance(identity, str):
                continue
            if protocol == 'gemini':
                if 'generateContent' not in item.get('supportedGenerationMethods', ['generateContent']):
                    continue
                identity = identity.removeprefix('models/')
            models.append({'id': identity, 'name': item.get('displayName', item.get('display_name', identity)),
                           **({'context_tokens': item['inputTokenLimit']} if isinstance(item.get('inputTokenLimit'), int) else {})})
        return {'models': models, 'protocol': protocol, 'base_url': base_url,
                'next_cursor': result.get('nextPageToken') if protocol == 'gemini' else result.get('last_id') if result.get('has_more') else None,
                'note': 'Provider-reported availability; each model still needs a connection test. Pricing is not inferred.'}

    def handle(self, method, path, data, state=None):
        if path not in {'/api/models/profiles', '/api/models/discover'}:
            return None
        if not isinstance(data, dict):
            raise ValueError('Model action must be an object')
        if path == '/api/models/profiles':
            with self.lock:
                if method == 'GET':
                    return self.list()
                if method == 'POST':
                    return self.profile(data, state)
        if path == '/api/models/discover' and method == 'POST':
            return self.discover(state, data.get('cursor'))
        raise ValueError('Unsupported model action method')
