"""Project memory, explicit retention, bounded state backups and local audit metadata.

Backups deliberately exclude provider settings, credentials, connectors, and
scheduled automations. This module provides neither encryption nor user auth.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import os
import platform
import re
import stat
import threading
import time
import uuid
import zipfile
from contextlib import nullcontext
from pathlib import Path, PurePosixPath

from .state import ProjectStore

_LOCK = threading.RLock()
_MEMORY = 'memory-entries.json'
_MAX_TOTAL = 8_000_000
_MAX_ARCHIVE = 6_000_000
_PROFILES = {'standard': ['read', 'write', 'execute', 'memory'], 'read-only': ['read']}
_BACKUP_FILES = {'memory.md', _MEMORY, 'project.json', 'library.json', 'documents.json'}
_CATEGORIES = {'sessions', 'runs', 'checkpoints', 'expired-memory'}


def _hash(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else 'missing'


def _token(value):
    return _hash(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def _text(value, name, limit, empty=False):
    if not isinstance(value, str) or len(value.encode()) > limit or (not empty and not value.strip()):
        raise ValueError(f'{name} must be text of at most {limit} bytes')
    return value.strip() if name != 'Memory content' else value


class Privacy:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.store = ProjectStore(self.root)

    def _private(self, name):
        relative = PurePosixPath(name)
        if relative.is_absolute() or not relative.parts or '..' in relative.parts or '\\' in name:
            raise ValueError('Invalid private-state path')
        candidate = self.store.directory
        if candidate.is_symlink():
            raise ValueError('Private-state directory must not be a symlink')
        for part in relative.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError('Private-state paths must not be symlinks')
        if not candidate.resolve().is_relative_to(self.store.directory.resolve()):
            raise ValueError('Private-state path leaves the project')
        return candidate

    def _read(self, name):
        path = self._private(name)
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > _MAX_TOTAL:
            raise ValueError('Private state exceeds the 8 MB operation limit')
        with path.open('rb') as source:
            raw = source.read(_MAX_TOTAL + 1)
        if len(raw) > _MAX_TOTAL:
            raise ValueError('Private state exceeds the 8 MB operation limit')
        return raw

    def _write(self, name, raw):
        path = self._private(name)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store.write(name, raw.decode('utf-8'))

    def record(self, action, identity=None, count=None):
        """Record metadata only; callers must never pass prompts or file content."""
        if not isinstance(action, str) or not re.fullmatch(r'[a-z0-9_.-]{1,60}', action):
            raise ValueError('Invalid audit action')
        entry = {'id': uuid.uuid4().hex, 'time': time.time(), 'action': action}
        if identity is not None:
            if not isinstance(identity, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}', identity):
                raise ValueError('Audit targets must be identifiers')
            entry['target'] = identity
        if count is not None:
            entry['count'] = int(count)
        with _LOCK:
            raw = self._read('privacy-audit.json')
            rows = json.loads(raw) if raw else []
            if not isinstance(rows, list):
                rows = []
            self._write('privacy-audit.json', json.dumps((rows + [entry])[-1000:]).encode())

    def _memory(self):
        raw = self._read(_MEMORY)
        document = json.loads(raw) if raw else {'version': 1, 'entries': []}
        self._validate_memory(document)
        return document, _hash(raw)

    def _validate_entry(self, entry):
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not re.fullmatch('[a-f0-9]{32}', entry['id']):
            raise ValueError('Invalid memory entry')
        _text(entry.get('label'), 'Memory label', 120)
        _text(entry.get('content'), 'Memory content', 4000)
        if not isinstance(entry.get('excluded', False), bool):
            raise ValueError('Memory exclusion must be a boolean')
        version_number = entry.get('version')
        if isinstance(version_number, bool) or not isinstance(version_number, int) or not 1 <= version_number <= 2 ** 53:
            raise ValueError('Invalid memory version number')
        if entry.get('source') not in ('user', 'import'):
            raise ValueError('Memory provenance must be user or import')
        expiry = entry.get('expires_at')
        if expiry is not None and (isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry < 0):
            raise ValueError('Memory expiry must be a Unix timestamp or null')
        for key in ('created', 'updated'):
            value = entry.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('Invalid memory timestamp')
        versions = entry.get('versions', [])
        if not isinstance(versions, list) or len(versions) > 10:
            raise ValueError('Memory keeps at most 10 previous versions')
        for version in versions:
            if not isinstance(version, dict) or not isinstance(version.get('version'), int):
                raise ValueError('Invalid memory version')
            _text(version.get('content'), 'Memory content', 4000)
        return entry

    def _validate_memory(self, document):
        if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('entries'), list) or len(document['entries']) > 200:
            raise ValueError('Memory must contain at most 200 project entries')
        ids = set()
        for entry in document['entries']:
            self._validate_entry(entry)
            if entry['id'] in ids:
                raise ValueError('Duplicate memory entry ID')
            ids.add(entry['id'])
        if len(json.dumps(document).encode()) > _MAX_TOTAL:
            raise ValueError('Memory history exceeds 8 MB; export and remove older entries')

    @staticmethod
    def _eligible(entry, now):
        return not entry.get('excluded') and (entry.get('expires_at') is None or entry['expires_at'] > now)

    def memory(self):
        document, revision = self._memory()
        now = time.time()
        groups = {}
        for entry in document['entries']:
            if self._eligible(entry, now):
                groups.setdefault(entry['label'].strip().casefold(), []).append(entry)
        conflicts = [{'label': key, 'ids': [entry['id'] for entry in entries]}
                     for key, entries in groups.items() if len({entry['content'] for entry in entries}) > 1]
        conflicted = {identity for group in conflicts for identity in group['ids']}
        entries = [{**entry, 'active': self._eligible(entry, now) and entry['id'] not in conflicted,
                    'expired': entry.get('expires_at') is not None and entry['expires_at'] <= now,
                    'conflicted': entry['id'] in conflicted} for entry in document['entries']]
        return {'scope': 'project', 'revision': revision, 'entries': entries, 'conflicts': conflicts,
                'context_limit_bytes': 16000, 'legacy_memory_separate': True}

    def context_text(self):
        """Only active, nonconflicting entries; a UTF-8 bound applies to whole lines."""
        lines, size = [], 0
        for entry in sorted(self.memory()['entries'], key=lambda entry: entry['updated'], reverse=True):
            if not entry['active']:
                continue
            line = entry['label'] + ': ' + entry['content']
            added = len(line.encode()) + (1 if lines else 0)
            if size + added > 16000:
                continue
            lines.append(line)
            size += added
        return '\n'.join(lines)

    def mutate_memory(self, data):
        with _LOCK:
            document, revision = self._memory()
            if data.get('revision') != revision:
                raise ValueError('Memory changed; reload before saving')
            action = data.get('action', 'save')
            if not isinstance(action, str):
                raise ValueError('Invalid memory action')
            identity = data.get('id')
            entry = next((entry for entry in document['entries'] if entry['id'] == identity), None)
            if action == 'delete':
                if not entry:
                    raise ValueError('Memory entry not found')
                document['entries'].remove(entry)
            elif action in {'save', 'restore'}:
                if identity is not None and entry is None:
                    raise ValueError('Memory entry not found')
                if entry is None:
                    entry = {'id': uuid.uuid4().hex, 'created': time.time(), 'updated': time.time(),
                             'label': '', 'content': '', 'source': 'user', 'excluded': False,
                             'expires_at': None, 'version': 0, 'versions': []}
                    document['entries'].append(entry)
                previous = {key: entry.get(key) for key in ('version', 'content', 'updated')}
                if action == 'restore':
                    version = next((item for item in entry['versions'] if item['version'] == data.get('version')), None)
                    if version is None:
                        raise ValueError('Memory version not found')
                    entry['content'] = version['content']
                else:
                    for key in ('label', 'content', 'excluded', 'expires_at'):
                        if key in data:
                            entry[key] = data[key]
                if previous['version']:
                    entry['versions'] = (entry['versions'] + [previous])[-10:]
                entry['updated'], entry['version'] = time.time(), entry['version'] + 1
                self._validate_entry(entry)
                identity = entry['id']
            else:
                raise ValueError('Memory action must be save, delete, or restore')
            self._validate_memory(document)
            self._write(_MEMORY, json.dumps(document).encode())
            self.record('memory.' + action, identity)
            return self.memory()

    def import_memory(self, data):
        content = _text(data.get('content'), 'Imported JSON', 1_000_000)
        imported = json.loads(content)
        self._validate_memory(imported)
        with _LOCK:
            document, revision = self._memory()
            if revision != data.get('revision'):
                raise ValueError('Memory changed; reload before importing')
            added = 0
            existing = {(entry['label'], entry['content']) for entry in document['entries']}
            for item in imported['entries']:
                if (item['label'], item['content']) in existing:
                    continue
                entry = {**item, 'id': uuid.uuid4().hex, 'source': 'import', 'imported_at': time.time(), 'versions': []}
                document['entries'].append(entry)
                existing.add((item['label'], item['content']))
                added += 1
            self._validate_memory(document)
            self._write(_MEMORY, json.dumps(document).encode())
            self.record('memory.import', count=added)
            return {**self.memory(), 'imported': added}

    def permissions(self):
        raw = self._read('privacy-settings.json')
        config = json.loads(raw) if raw else {}
        if not isinstance(config, dict):
            raise ValueError('Invalid stored permission settings')
        profile = config.get('profile', 'standard')
        if not isinstance(profile, str) or profile not in _PROFILES:
            raise ValueError('Invalid stored permission profile')
        return {'profile': profile, 'scopes': list(_PROFILES[profile]),
                'profiles': [{'id': key, 'scopes': scopes} for key, scopes in _PROFILES.items()],
                'scope': 'project', 'execution_backend': 'local', 'sandbox_enforced': False}

    def tool_scopes(self):
        return self.permissions()['scopes']

    def set_permissions(self, data, state):
        profile = data.get('profile')
        if not isinstance(profile, str) or profile not in _PROFILES:
            raise ValueError('Choose standard or read-only')
        self._idle(state)
        self._write('privacy-settings.json', json.dumps({'profile': profile}).encode())
        self.record('permissions.update', profile)
        return self.permissions()

    @staticmethod
    def _idle(state):
        if state is not None and getattr(state, 'busy', False):
            raise ValueError('Finish or stop the active task first')

    @staticmethod
    def _backup_allowed(name):
        return name in _BACKUP_FILES or bool(re.fullmatch(r'sessions/[A-Za-z0-9_-]{1,64}\.json', name))

    def _backup_inventory(self):
        paths = [name for name in sorted(_BACKUP_FILES) if self._private(name).is_file()]
        directory = self._private('sessions')
        if directory.is_dir():
            paths += ['sessions/' + path.name for path in sorted(directory.glob('*.json'))
                      if self._backup_allowed('sessions/' + path.name) and not path.is_symlink()]
        if len(paths) > 1000:
            raise ValueError('Backups are limited to 1000 files')
        files, size = {}, 0
        for name in paths:
            raw = self._read(name)
            if raw is None:
                continue
            size += len(raw)
            if size > _MAX_TOTAL:
                raise ValueError('Backup exceeds 8 MB; export individual conversations or documents instead')
            files[name] = raw
        return files

    def backup_export(self):
        with _LOCK:
            files = self._backup_inventory()
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', json.dumps({'format': 'herness-project-backup', 'version': 1,
                    'created': time.time(), 'files': {name: _hash(raw) for name, raw in files.items()}}))
                for name, raw in files.items():
                    archive.writestr('state/' + name, raw)
            zipped = buffer.getvalue()
            if len(zipped) > _MAX_ARCHIVE:
                raise ValueError('Compressed backup exceeds 6 MB')
            self.record('backup.export', count=len(files))
            return {'filename': 'herness-project-backup.zip', 'mime': 'application/zip',
                    'data': base64.b64encode(zipped).decode(), 'files': len(files), 'bytes': len(zipped),
                    'contains_private_content': True,
                    'excluded': ['provider settings and keys', 'connectors', 'automations', 'checkpoints', 'audit', 'permission settings']}

    def _validate_backup_file(self, name, raw):
        try:
            content = raw.decode('utf-8')
            if name == 'memory.md':
                if len(raw) > 32000:
                    raise ValueError('Legacy memory exceeds 32 KB')
                return
            document = json.loads(content)
            if not isinstance(document, dict):
                raise ValueError('Backup state must be a JSON object')
            if name == _MEMORY:
                self._validate_memory(document)
            elif name.startswith('sessions/'):
                messages = document.get('messages')
                if document.get('version') != 1 or not isinstance(messages, list) or not messages or not isinstance(document.get('attachments', {}), dict):
                    raise ValueError('Invalid backed-up conversation')
                if any(not isinstance(item, dict) or item.get('role') not in {'system', 'user', 'assistant', 'tool'} for item in messages) or messages[0]['role'] != 'system':
                    raise ValueError('Invalid backed-up conversation messages')
            elif name == 'documents.json':
                if not isinstance(document.get('artifacts'), dict) or not isinstance(document.get('library'), dict):
                    raise ValueError('Invalid backed-up document library')
            elif name == 'library.json':
                if any(not isinstance(item, dict) for item in document.values()):
                    raise ValueError('Invalid conversation metadata')
            elif name == 'project.json':
                for key in ('title', 'instructions', 'goals'):
                    if key in document and (not isinstance(document[key], str) or len(document[key]) > 16000):
                        raise ValueError('Invalid project metadata')
                for key in ('ignored_paths', 'pinned_files'):
                    if key in document and (not isinstance(document[key], list) or len(document[key]) > 100 or
                                            any(not isinstance(value, str) or len(value) > 500 for value in document[key])):
                        raise ValueError('Invalid project paths')
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('Backup contains invalid text or JSON: ' + name) from exc

    def _decode_backup(self, encoded):
        if not isinstance(encoded, str) or len(encoded) > _MAX_ARCHIVE * 4 // 3 + 8:
            raise ValueError('Backup must be base64 ZIP data under 6 MB')
        try:
            raw = base64.b64decode(encoded, validate=True)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                if len(infos) > 1001 or len({info.filename for info in infos}) != len(infos):
                    raise ValueError('Backup has too many or duplicate paths')
                if sum(info.file_size for info in infos) > _MAX_TOTAL + 200000:
                    raise ValueError('Expanded backup exceeds 8 MB')
                for info in infos:
                    if info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
                        raise ValueError('Encrypted entries and symlinks are not supported')
                    if info.filename != 'manifest.json' and (not info.filename.startswith('state/') or not self._backup_allowed(info.filename[6:])):
                        raise ValueError('Backup contains a disallowed path')
                manifest = json.loads(archive.read('manifest.json'))
                if not isinstance(manifest, dict) or manifest.get('format') != 'herness-project-backup' or manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
                    raise ValueError('Unsupported backup format')
                files = {info.filename[6:]: archive.read(info) for info in infos if info.filename.startswith('state/')}
                if set(files) != set(manifest['files']) or any(_hash(content) != manifest['files'][name] for name, content in files.items()):
                    raise ValueError('Backup manifest does not match its contents')
                if sum(map(len, files.values())) > _MAX_TOTAL:
                    raise ValueError('Expanded backup exceeds 8 MB')
                for name, content in files.items():
                    self._validate_backup_file(name, content)
                return files
        except (binascii.Error, zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
            raise ValueError('Invalid project backup') from exc

    def backup_preview(self, data):
        files = self._decode_backup(data.get('data'))
        entries = []
        for name, content in files.items():
            current = self._read(name)
            entries.append({'path': name, 'bytes': len(content), 'before_hash': _hash(current), 'after_hash': _hash(content),
                            'action': 'same' if current == content else 'create' if current is None else 'replace'})
        return {'files': entries, 'conflicts': [entry['path'] for entry in entries if entry['action'] == 'replace'],
                'preview_token': _token(entries), 'contains_private_content': True}

    def _reload(self, state):
        if state is None:
            return
        if hasattr(state, 'session_name') and callable(getattr(state, 'saved_messages', None)):
            from .core import Session
            state.session = Session(self.root, state.session_name)
            state.messages = state.saved_messages()
        application = getattr(state, 'application', None)
        if application is not None:
            from .library import Library, Project
            application.library = Library(self.root)
            application.project = Project(self.root)
            state.project = application.project.read()

    def backup_restore(self, data, state):
        self._idle(state)
        if data.get('confirmed') is not True:
            raise ValueError('Confirm the reviewed backup restore first')
        with _LOCK:
            preview = self.backup_preview(data)
            if data.get('preview_token') != preview['preview_token']:
                raise ValueError('Backup or local state changed; review a fresh preview')
            if preview['conflicts'] and data.get('overwrite') is not True:
                raise ValueError('Backup conflicts with local state; explicitly choose overwrite after reviewing')
            files = self._decode_backup(data['data'])
            previous = {name: self._read(name) for name in files}
            expected = {entry['path']: entry['before_hash'] for entry in preview['files']}
            if any(_hash(previous[name]) != expected[name] for name in files):
                raise ValueError('Local state changed during restore preparation; review a fresh preview')
            changed = []
            try:
                for name, content in files.items():
                    if previous[name] != content:
                        if self._read(name) != previous[name]:
                            raise ValueError('Local state changed during restore; review a fresh preview')
                        self._write(name, content)
                        changed.append(name)
                self._reload(state)
            except Exception:
                for name in reversed(changed):
                    if previous[name] is None:
                        self._private(name).unlink(missing_ok=True)
                    else:
                        self._write(name, previous[name])
                self._reload(state)
                raise
            self.record('backup.restore', count=len(changed))
            return {'restored': changed, 'reloaded': True, 'settings_restored': False}

    def retention_preview(self, data, state=None):
        days = data.get('days', 90)
        categories = data.get('categories', ['runs', 'checkpoints', 'expired-memory'])
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise ValueError('Retention age must be 1–3650 days')
        if not isinstance(categories, list) or not categories or any(not isinstance(category, str) or category not in _CATEGORIES for category in categories):
            raise ValueError('Choose valid retention categories')
        cutoff, candidates = time.time() - days * 86400, []
        current_session = getattr(state, 'session_name', None)
        for category in sorted(set(categories) - {'expired-memory'}):
            directory = self._private(category)
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob('*.json')):
                if path.is_symlink() or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}\.json', path.name):
                    continue
                if category == 'sessions' and path.stem == current_session:
                    continue
                metadata = path.stat()
                if metadata.st_mtime >= cutoff:
                    continue
                relative = category + '/' + path.name
                raw = self._read(relative)
                if raw is None:
                    continue
                if category == 'runs':
                    try:
                        if json.loads(raw).get('status') == 'running':
                            continue
                    except (ValueError, AttributeError):
                        continue
                candidates.append({'path': relative, 'bytes': len(raw), 'hash': _hash(raw), 'modified': metadata.st_mtime})
                if len(candidates) > 1000:
                    raise ValueError('At most 1000 records can be removed at once')
        memory = self.memory()
        expired = [entry['id'] for entry in memory['entries'] if entry['expired']] if 'expired-memory' in categories else []
        review = {'files': candidates, 'memory_ids': expired, 'memory_revision': memory['revision'],
                  'days': days, 'categories': sorted(set(categories))}
        return {**review, 'preview_token': _token(review), 'bytes': sum(item['bytes'] for item in candidates),
                'note': 'Deletion is explicit. The active conversation and running task records are excluded; no timer deletes data.'}

    def retention_apply(self, data, state):
        self._idle(state)
        if data.get('confirmed') is not True:
            raise ValueError('Confirm the reviewed retention deletion first')
        with _LOCK:
            preview = self.retention_preview(data, state)
            if data.get('preview_token') != preview['preview_token']:
                raise ValueError('Retention candidates changed; review a fresh preview')
            if any(_hash(self._read(item['path'])) != item['hash'] for item in preview['files']):
                raise ValueError('Retention files changed; review a fresh preview')
            for item in preview['files']:
                self._private(item['path']).unlink()
            if preview['memory_ids']:
                document, revision = self._memory()
                document['entries'] = [entry for entry in document['entries'] if entry['id'] not in preview['memory_ids']]
                self._write(_MEMORY, json.dumps(document).encode())
            count = len(preview['files']) + len(preview['memory_ids'])
            self.record('retention.apply', count=count)
            self._reload(state)
            return {'deleted_files': len(preview['files']), 'deleted_memory_entries': len(preview['memory_ids']), 'bytes': preview['bytes']}

    def diagnostics(self):
        memory = self.memory()
        return {'python': platform.python_version(), 'platform': platform.system(), 'scope': 'project',
                'memory_entries': len(memory['entries']), 'active_memory_entries': sum(entry['active'] for entry in memory['entries']),
                'conflicts': len(memory['conflicts']), 'permission_profile': self.permissions()['profile'],
                'backup_limits': {'compressed_bytes': _MAX_ARCHIVE, 'expanded_bytes': _MAX_TOTAL, 'files': 1000},
                'encryption': False, 'remote_sync': False, 'automatic_deletion': False,
                'audit_scope': 'Privacy, memory, backup and permission actions; no prompt or content logging',
                'includes_private_content': False}

    def handle(self, method, path, data, state):
        if not path.startswith('/api/privacy/'):
            return None
        data = data or {}
        if not isinstance(data, dict):
            raise ValueError('Privacy request must be a JSON object')
        action = path[len('/api/privacy/'):]
        lock = getattr(state, 'lock', None)
        with lock if lock is not None else nullcontext(), _LOCK:
            if method == 'GET':
                if action == 'memory':
                    return self.memory()
                if action == 'memory/export':
                    document, revision = self._memory()
                    return {'filename': 'herness-project-memory.json', 'mime': 'application/json', 'content': json.dumps(document, indent=2), 'revision': revision}
                if action == 'permissions':
                    return self.permissions()
                if action == 'backup/export':
                    self._idle(state)
                    return self.backup_export()
                if action == 'audit':
                    return {'events': json.loads(self._read('privacy-audit.json') or b'[]'), 'scope': 'privacy actions', 'limit': 1000}
                if action == 'diagnostics':
                    return self.diagnostics()
            if method == 'POST':
                if action == 'memory':
                    return self.mutate_memory(data)
                if action == 'memory/import':
                    return self.import_memory(data)
                if action == 'permissions':
                    return self.set_permissions(data, state)
                if action == 'backup/preview':
                    return self.backup_preview(data)
                if action == 'backup/restore':
                    return self.backup_restore(data, state)
                if action == 'retention/preview':
                    return self.retention_preview(data, state)
                if action == 'retention/apply':
                    return self.retention_apply(data, state)
            raise ValueError('Unknown privacy action or method')
