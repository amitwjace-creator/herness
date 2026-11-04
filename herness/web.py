"""Local-only web UI with explicit approvals and one active agent turn."""

import json
import hashlib
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import Agent, Session, OpenAIClient, Cancellation, TurnCancelled, ModelError
from .state import ProjectStore, attachment_content, display_text
from .tools import ToolRunner


class Workspace:
    def __init__(self, root, client, demo=False, force_demo=False):
        self.root, self.client, self.demo = root, client, demo
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.busy = False
        self.approval = None
        self.event = threading.Event()
        self.session_name = "default"
        self.session = Session(root, self.session_name)
        self.messages = self.saved_messages()
        self.store = ProjectStore(root)
        self.run_options = {'mode': 'act'}
        self.project = {}
        self.run_events = []
        self.command_output = ''
        self.streaming = ''
        self.cancel = Cancellation()
        self.api_keys = {}
        if isinstance(client, OpenAIClient):
            self.api_keys[client.base_url] = client.api_key
        self.model_config = {'base_url': getattr(client, 'base_url', os.environ.get('HERNESS_API_BASE', 'https://api.openai.com/v1')),
                             'model': getattr(client, 'model', os.environ.get('HERNESS_MODEL', 'gpt-4.1-mini')), 'demo': demo,
                             'protocol': getattr(client, 'protocol', 'chat_completions')}
        saved = self.store.settings()
        if saved:
            self.configure(saved)
        if force_demo:
            from .cli import DemoClient
            self.client, self.demo = DemoClient(), True
            self.model_config['demo'] = True


    def settings_public(self):
        return {**self.model_config, 'key_present': bool(self.api_keys.get(self.model_config['base_url']))}

    def candidate(self, data):
        from .providers import create_client
        config = {key: data.get(key, self.model_config[key]) for key in ('base_url', 'model', 'demo', 'protocol')}
        if not isinstance(config['demo'], bool):
            raise ValueError('Demo must be a boolean')
        if not isinstance(config['base_url'], str) or not isinstance(config['model'], str):
            raise ValueError('API URL and model must be strings')
        if 'clear_key' in data and not isinstance(data['clear_key'], bool):
            raise ValueError('clear_key must be a boolean')
        config['base_url'] = config['base_url'].rstrip('/')
        key = data.get('api_key', '')
        if not isinstance(key, str) or len(key) > 4096 or '\n' in key or '\r' in key:
            raise ValueError('Invalid API key')
        if not key and not data.get('clear_key', False):
            key = self.api_keys.get(config['base_url'], '')
        client = create_client(config['protocol'], config['model'], config['base_url'], key)
        config['base_url'] = client.base_url
        if config['demo']:
            from .cli import DemoClient
            return config, DemoClient(), key
        return config, client, key

    def configure(self, data, persist=False):
        config, client, key = self.candidate(data)
        if persist:
            self.store.write('settings.json', json.dumps(config, indent=2))
        self.model_config, self.client, self.demo = config, client, config['demo']
        self.api_keys[config['base_url']] = key

    def memory_public(self):
        text = self.store.memory()
        return {'content': text, 'revision': hashlib.sha256(text.encode()).hexdigest()}

    def saved_messages(self):
        return [{'role': m['role'], 'text': display_text(m['content']),
                 'attachments': self.session.attachments.get(str(index), []), 'index': index}
                for index, m in enumerate(self.session.messages)
                if m['role'] in {'user', 'assistant'} and m.get('content')]

    def conversations(self):
        """List local conversations without following user-created symlinks."""
        conversations = []
        for path in self.session.directory.glob('*.json'):
            if path.is_symlink() or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', path.stem):
                continue
            try:
                if path.stat().st_size > 64_000_000:
                    continue
                data = json.loads(path.read_text())
                if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('messages'), list):
                    continue
                title = next((display_text(m.get('content')) for m in data['messages']
                              if isinstance(m, dict) and m.get('role') == 'user'
                              and display_text(m.get('content')).strip()), None)
                if title:
                    conversations.append({'name': path.stem, 'title': ' '.join(title.split())[:100],
                                          'updated': path.stat().st_mtime})
            except (OSError, ValueError):
                continue
        return sorted(conversations, key=lambda item: item['updated'], reverse=True)[:200]

    def emit(self, message):
        with self.lock:
            self.streaming = ''
            role = 'tool' if message.startswith('[tool:') else 'assistant'
            index = next((i for i in range(len(self.session.messages)-1,0,-1)
                          if self.session.messages[i].get('role') == 'assistant' and
                          display_text(self.session.messages[i].get('content')) == message), None) if role == 'assistant' else None
            self.messages.append({'role': role, 'text': message, 'index': index})

    def public_messages(self):
        marked = getattr(self, 'application', None)
        bookmarks = marked.library.metadata.get(self.session_name, {}).get('bookmarks', []) if marked else []
        return [{**m, 'bookmarked': m.get('index') in bookmarks} for m in self.messages]

    def runtime_event(self, event):
        with self.lock:
            self.run_events.append(event)
            self.run_events = self.run_events[-200:]

    def command_delta(self, text):
        with self.lock:
            self.command_output = (self.command_output + text)[-32000:]

    def delta(self, text):
        with self.lock:
            self.streaming += text

    def approve(self, description):
        with self.lock:
            if self.cancel.event.is_set():
                return False
            self.event.clear()
            detail = description if isinstance(description, dict) else {'kind': 'command', 'description': description}
            approval = {**detail, 'id': secrets.token_urlsafe(12), 'allowed': False}
            self.approval = approval
        self.event.wait(timeout=180)
        with self.lock:
            self.approval = None
            return approval['allowed'] and not self.cancel.event.is_set()

    def stop(self):
        with self.lock:
            self.cancel.stop()
            if self.approval:
                self.approval['allowed'] = False
            self.event.set()

    def start(self, prompt, files=None, prepared_content=None, prepared_summaries=None,
              runtime_options=None, completed=None):
        with self.lock:
            if self.busy:
                return False
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
                raise ValueError('Enter a message of 1–32000 characters')
            content, summaries = (prepared_content, prepared_summaries or []) if prepared_content is not None else attachment_content(prompt, [] if files is None else files)
            options = {**self.run_options, **(runtime_options or {})}
            mode = options.get('mode', 'act')
            self.session.recover_pending_tools()
            self.active_mode = mode
            self.busy = True
            self.streaming = ''
            self.command_output = ''
            self.run_events = []
            self.cancel = Cancellation()
            self.messages.append({'role': 'user', 'text': prompt, 'attachments': summaries, 'index': len(self.session.messages)})
            client = self.client
        def work():
            outcome, error = 'completed', ''
            try:
                self.session.attachments[str(len(self.session.messages))] = summaries
                from .privacy import Privacy
                from .agent_tools import ExtensionRunner
                Agent(client, ExtensionRunner(self.root, self.approve, cancel=self.cancel, mode=mode,
                                         application=getattr(self, 'application', None),
                                         on_output=self.command_delta, scopes=Privacy(self.root).tool_scopes()), self.session,
                      self.emit, delta=self.delta, cancel=self.cancel, mode=mode,
                      max_steps=options.get('max_steps', 12), runtime_options=options,
                      project=self.project, event=self.runtime_event).run(content)
            except TurnCancelled:
                outcome = 'cancelled'
                with self.lock:
                    partial = self.streaming
                self.emit((partial + '\n\n' if partial else '') + 'Stopped by you.')
            except Exception as exc:
                outcome, error = 'failed', str(exc)
                with self.lock:
                    partial = self.streaming
                self.emit((partial + '\n\n' if partial else '') + f'Turn failed: {exc}')
            finally:
                with self.lock:
                    self.busy = False
                if completed:
                    completed(outcome, error)
        threading.Thread(target=work, daemon=True).start()
        return True

    def start_automation(self, prompt, run_id, options):
        with self.lock:
            if self.busy:
                return False
            self.session_name = 'auto-' + run_id
            self.session = Session(self.root, self.session_name)
            self.messages = self.saved_messages()
            module = next(m for m in self.application.modules if hasattr(m, 'complete'))
            def completed(status, error):
                module.complete(run_id, status=status, error=error,
                                summary='Open conversation auto-' + run_id + ' for the result.')
            return self.start(prompt, runtime_options={**options, 'mode': options.get('mode', 'plan')}, completed=completed)


def make_server(root, client, port=8765, demo=False, force_demo=False, mobile_policy=None):
    if mobile_policy is not None:
        from .mobile import MobilePolicy
        if not isinstance(mobile_policy, MobilePolicy):
            raise ValueError('Native access requires a validated MobilePolicy')
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError('Native API port must be an integer from 0 to 65535')
    state = Workspace(root, client, demo, force_demo)
    from .application import Application
    application = Application(state)
    template = Path(__file__).with_name("index.html").read_text() if mobile_policy is None else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not log prompts, authorization tokens, or request bodies.

        def send(self, status, payload, html=False):
            body = payload.encode() if html else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            policy = ("default-src 'none'; frame-ancestors 'none'; base-uri 'none'" if mobile_policy is not None else
                      "default-src 'self'; script-src 'nonce-" + state.token + "'; style-src 'unsafe-inline'; img-src 'self' data: blob:; frame-src 'self' blob:; frame-ancestors 'none'; connect-src 'self'; object-src 'none'; base-uri 'none'")
            self.send_header("Content-Security-Policy", policy)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def send_error(self, code, message=None, explain=None):
            if mobile_policy is not None:
                return self.send(code, {'error': self.responses.get(code, ('Request failed',))[0]})
            return super().send_error(code, message, explain)

        def valid_host(self):
            if mobile_policy is not None:
                return mobile_policy.valid_host(self.headers, self.server.server_port)
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def authorized(self):
            if mobile_policy is not None:
                return mobile_policy.authorized(self.headers, self.server.server_port)
            return self.valid_host() and secrets.compare_digest(
                self.headers.get("X-Herness-Token", ""), state.token)

        def expected_session(self, data):
            # Called while holding state.lock through the guarded mutation.
            if mobile_policy is None:
                return True
            expected = data.get('expected_session')
            if not isinstance(expected, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', expected):
                self.send(400, {'error': 'expected_session must identify the current conversation'})
                return False
            if expected != state.session_name:
                self.send(409, {'error': 'The active conversation changed. Refresh before retrying this action.'})
                return False
            return True

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {"error": "Invalid host"})
            if mobile_policy is not None and mobile_policy.browser_request(self.headers):
                return self.send(403, {'error': 'Native API does not accept browser requests'})
            if self.path == "/":
                if mobile_policy is not None:
                    return self.send(404, {'error': 'Not found'})
                return self.send(200, template.replace("__TOKEN__", state.token), html=True)
            if not self.authorized():
                return self.send(403, {"error": "Unauthorized"})
            if mobile_policy is not None and self.path == '/api/mobile/info':
                return self.send(200, mobile_policy.info())
            try:
                result = application.handle('GET', self.path, {})
                if result is not None:
                    return self.send(200, result)
            except (ValueError, OSError) as exc:
                return self.send(400, {'error': str(exc)})
            if self.path == "/api/status":
                with state.lock:
                    return self.send(200, {"busy": state.busy, "messages": state.public_messages(),
                        "approval": state.approval, "workspace": str(state.root), "demo": state.demo,
                        "session": state.session_name, "streaming": state.streaming,
                        "mode": getattr(state, 'active_mode', 'act') if state.busy else state.run_options.get('mode', 'act'), 'events': state.run_events, 'command_output': state.command_output,
                        "stopping": state.busy and state.cancel.event.is_set(),
                        "model": getattr(state.client, "model", "Offline demo")})
            if self.path == '/api/settings':
                with state.lock:
                    return self.send(200, state.settings_public())
            if self.path == '/api/memory':
                with state.lock:
                    return self.send(200, state.memory_public())
            if self.path == "/api/sessions":
                with state.lock:
                    return self.send(200, {"sessions": state.conversations()})
            self.send(404, {"error": "Not found"})

        def do_POST(self):
            if not self.authorized():
                return self.send(403, {"error": "Unauthorized"})
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                return self.send(403, {"error": "Invalid origin"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 11_000_000:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                result = application.handle('POST', self.path, data)
                if result is not None:
                    return self.send(200, result)
                if self.path in {'/api/settings', '/api/settings/test'}:
                    with state.lock:
                        if state.busy:
                            return self.send(409, {'error': 'Stop or finish the active turn before changing models'})
                        if self.path == '/api/settings':
                            state.configure(data, persist=True)
                            return self.send(200, state.settings_public())
                        config, candidate, _ = state.candidate(data)
                    message = 'Offline demo selected. No API connection is used.' if config['demo'] else candidate.probe()
                    return self.send(200, {'message': message})
                if self.path in {'/api/memory', '/api/memory/delete'}:
                    with state.lock:
                        if data.get('revision') != state.memory_public()['revision']:
                            return self.send(409, {'error': 'Memory changed. Reload it before saving or deleting.'})
                        if self.path == '/api/memory/delete':
                            state.store.path('memory.md').unlink(missing_ok=True)
                        else:
                            state.store.save_memory(data.get('content'))
                        application.vectors.invalidate_sources()
                        return self.send(200, state.memory_public())
                if self.path == '/api/stop':
                    with state.lock:
                        if not self.expected_session(data):
                            return
                        state.stop()
                        return self.send(200, {'ok': True})
                if self.path == "/api/chat":
                    with state.lock:
                        if not self.expected_session(data):
                            return
                        prompt = data.get("prompt")
                        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_000:
                            raise ValueError("Enter a message of 1–32000 characters")
                        if not state.start(prompt, data.get("attachments", [])):
                            return self.send(409, {"error": "A turn is already running"})
                        return self.send(202, {"ok": True})
                if self.path == "/api/approve":
                    with state.lock:
                        if not self.expected_session(data):
                            return
                        if not state.approval or state.approval["id"] != data.get("id") or state.event.is_set():
                            return self.send(409, {"error": "Approval expired or already answered"})
                        state.approval["allowed"] = data.get("allow") is True
                        state.event.set()
                    return self.send(200, {"ok": True})
                if self.path == "/api/session":
                    with state.lock:
                        if not self.expected_session(data):
                            return
                        if state.busy:
                            return self.send(409, {"error": "Wait for the active turn to finish"})
                        name = data.get("name")
                        if not isinstance(name, str):
                            raise ValueError("Session name is required")
                        session = Session(state.root, name)
                        state.session, state.session_name = session, name
                        state.messages = state.saved_messages()
                    return self.send(200, {"ok": True})
                self.send(404, {"error": "Not found"})
            except ModelError as exc:
                self.send(502, {"error": str(exc)})
            except (ValueError, OSError) as exc:
                self.send(400, {"error": str(exc)})

        def unsupported(self):
            if mobile_policy is None:
                return self.send_error(501, 'Unsupported method')
            if not self.authorized():
                return self.send(403, {'error': 'Unauthorized'})
            return self.send(405, {'error': 'Method not allowed'})

        do_HEAD = do_OPTIONS = do_PUT = do_PATCH = do_DELETE = do_TRACE = do_CONNECT = unsupported

    stop_scheduler = threading.Event()
    class Server(ThreadingHTTPServer):
        def server_close(self):
            stop_scheduler.set()
            state.stop()
            application.close()
            super().server_close()
    server = Server(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.workspace = state
    def scheduler():
        while not stop_scheduler.wait(2):
            try:
                application.tick()
            except (ValueError, OSError) as exc:
                state.runtime_event({'kind': 'scheduler_error', 'error': str(exc)})
    threading.Thread(target=scheduler, daemon=True).start()
    return server


def serve(root, client, port=8765, demo=False):
    server = make_server(root, client, port, demo, force_demo=demo)
    print(f"Herness web app listening on loopback port {server.server_port}.")
    print("Offline scripted demo" if demo else "Live model mode; API usage may incur charges.")
    print("Press Ctrl+C to stop. Keep this server local; it has access to your machine.")
    try:
        server.serve_forever()
    finally:
        server.server_close()
