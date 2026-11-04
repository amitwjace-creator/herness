"""Bounded project inspection and recoverable, conflict-aware file changes."""
from __future__ import annotations

import ast
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .state import ProjectStore

_LOCK = threading.RLock()
_SKIP = {'.git', '.herness', '.ssh', '.aws', '.venv', 'venv', 'node_modules', '__pycache__', 'build', 'dist', '.next'}
_MAX_FILE = 256_000


def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest() if content is not None else 'missing'


def unified(before, after, path):
    lines = difflib.unified_diff((before or '').splitlines(keepends=True),
                                (after or '').splitlines(keepends=True),
                                fromfile=path + ' (before)', tofile=path + ' (after)')
    return ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines)


def protected(parts):
    return any(p in {'.git', '.herness', '.ssh', '.aws'} or p == '.env' or p.startswith('.env.')
               or p.endswith(('.pem', '.key')) for p in parts)


class Workbench:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.store = ProjectStore(self.root)

    def path(self, value):
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise ValueError('Use a nonempty path relative to the project')
        raw = Path(value)
        resolved = (self.root / raw).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError('Path leaves the project folder')
        if protected(raw.parts) or protected(resolved.relative_to(self.root).parts):
            raise ValueError('Protected credential or agent-state path')
        return resolved

    def ignored(self):
        try:
            data = json.loads(self.store.read('project.json', '{}'))
            patterns = data.get('ignored_paths', [])
            return [p for p in patterns if isinstance(p, str)] if isinstance(patterns, list) else []
        except (ValueError, AttributeError):
            return []

    def files(self, path='.', limit=1000):
        base = self.path(path)
        if not base.is_dir():
            raise ValueError('Path is not a directory')
        patterns = self.ignored()
        result = []
        truncated = False
        for directory, dirs, names in os.walk(base, followlinks=False):
            relative = Path(directory).relative_to(self.root)
            dirs[:] = sorted(d for d in dirs if d not in _SKIP and not protected((d,))
                             and not (Path(directory) / d).is_symlink()
                             and not any(fnmatch.fnmatch(str(relative / d), p) for p in patterns))
            for name in sorted(names):
                candidate = Path(directory) / name
                rel = candidate.relative_to(self.root).as_posix()
                if candidate.is_symlink() or protected(candidate.relative_to(self.root).parts):
                    continue
                if any(fnmatch.fnmatch(rel, pattern) for pattern in patterns):
                    continue
                try:
                    if not candidate.is_file():
                        continue
                    metadata = candidate.stat()
                except OSError:
                    continue
                if len(result) >= limit:
                    truncated = True
                    break
                result.append({'path': rel, 'size': metadata.st_size, 'modified': metadata.st_mtime})
            if truncated:
                break
        return {'files': result, 'truncated': truncated}

    def search(self, query, path='.', case_sensitive=False):
        if not isinstance(query, str) or not query or len(query) > 500:
            raise ValueError('Search text must contain 1–500 characters')
        inventory = self.files(path, 2000)
        matches = []
        skipped = 0
        needle = query if case_sensitive else query.casefold()
        for item in inventory['files']:
            if item['size'] > _MAX_FILE:
                skipped += 1
                continue
            candidate = self.path(item['path'])
            try:
                with candidate.open('rb') as source:
                    raw = source.read(_MAX_FILE + 1)
                if len(raw) > _MAX_FILE:
                    skipped += 1
                    continue
                content = raw.decode('utf-8')
                if '\0' in content:
                    skipped += 1
                    continue
            except (UnicodeError, OSError):
                skipped += 1
                continue
            for line_number, line in enumerate(content.splitlines(), 1):
                if needle in (line if case_sensitive else line.casefold()):
                    matches.append({'path': item['path'], 'line': line_number, 'text': line[:1000]})
                    if len(matches) >= 200:
                        return {'matches': matches, 'truncated': True, 'skipped_files': skipped}
        return {'matches': matches, 'truncated': inventory['truncated'], 'skipped_files': skipped}

    def symbols(self, path='.'):
        inventory = self.files(path, 1500)
        result = []
        for item in inventory['files']:
            suffix = Path(item['path']).suffix.lower()
            if suffix not in {'.py', '.js', '.jsx', '.ts', '.tsx', '.rs', '.go', '.java'} or item['size'] > _MAX_FILE:
                continue
            try:
                content = self.read_text(self.path(item['path']))
                if content is None:
                    continue
                if suffix == '.py':
                    tree = ast.parse(content)
                    symbols = [(node.name, node.lineno, 'class' if isinstance(node, ast.ClassDef) else 'function')
                               for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                else:
                    pattern = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:pub\s+)?(?:async\s+)?(class|interface|function|fn|func|type|struct|enum)\s+([A-Za-z_$][\w$]*)')
                    symbols = [(match.group(2), index, match.group(1)) for index, line in enumerate(content.splitlines(), 1)
                               if (match := pattern.search(line))]
                result.extend({'path': item['path'], 'name': name, 'line': line, 'kind': kind} for name, line, kind in symbols)
                if len(result) >= 1000:
                    return {'symbols': result[:1000], 'truncated': True, 'method': 'Python AST; declaration scanning for other languages'}
            except (ValueError, OSError, UnicodeError, SyntaxError):
                continue
        return {'symbols': result, 'truncated': inventory['truncated'], 'method': 'Python AST; declaration scanning for other languages'}

    def _git(self, arguments):
        command = ['git', '--no-optional-locks', '--literal-pathspecs', '-c', 'core.fsmonitor=false',
                   '-c', 'core.hooksPath=/dev/null', '-C', str(self.root), *arguments]
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=15, stdin=subprocess.DEVNULL)
            except FileNotFoundError:
                return {'error': 'Git is not installed'}
            except subprocess.TimeoutExpired:
                return {'error': 'Git inspection timed out'}
            output.seek(0)
            data = output.read(64_001)
        return {'exit_code': process.returncode, 'output': data[:64_000].decode(errors='replace'), 'truncated': len(data) > 64_000}

    def git(self, kind='status'):
        if kind == 'status':
            return self._git(['status', '--porcelain=v1', '--branch'])
        if kind != 'diff':
            raise ValueError('Git inspection must be status or diff')
        # Ask Git for tracked changes so deleted files are included. An unborn
        # branch has no HEAD: Git's empty tree object also works before a commit.
        base = 'HEAD'
        head = self._git(['rev-parse', '--verify', 'HEAD'])
        if head.get('exit_code') != 0:
            empty = self._git(['hash-object', '-t', 'tree', '--stdin'])
            if empty.get('exit_code') != 0:
                return empty
            base = empty['output'].strip()
        changed = self._git(['diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--name-only', '-z', base, '--'])
        if changed.get('exit_code') != 0:
            return changed
        files, omitted = [], 0
        patterns = self.ignored()
        for relative in changed['output'].split('\0'):
            if not relative:
                continue
            try:
                self.path(relative)
                if any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
                    continue
                if len(files) >= 1500:
                    omitted += 1
                    continue
                files.append(relative)
            except ValueError:
                omitted += 1
        # Explicit literal pathspecs and disabled rename detection prevent a
        # protected source path appearing as the other half of a safe rename.
        if not files:
            return {'exit_code': 0, 'output': '', 'truncated': changed['truncated'], 'omitted_files': omitted}
        result = self._git(['diff', '--no-ext-diff', '--no-textconv', '--no-renames', base, '--', *files])
        result['truncated'] = result.get('truncated', False) or changed['truncated'] or len(files) >= 1500
        result['omitted_files'] = omitted
        return result

    def _checkpoint_dir(self):
        directory = self.store.directory / 'checkpoints'
        if directory.is_symlink():
            raise ValueError('Checkpoint storage must not be a symlink')
        directory.mkdir(exist_ok=True, mode=0o700)
        return directory

    def _record_path(self, checkpoint_id):
        if not isinstance(checkpoint_id, str) or not re.fullmatch('[a-f0-9]{32}', checkpoint_id):
            raise ValueError('Invalid checkpoint ID')
        path = self._checkpoint_dir() / (checkpoint_id + '.json')
        if path.is_symlink():
            raise ValueError('Checkpoint must not be a symlink')
        return path

    def _record_write(self, record):
        target = self._record_path(record['id'])
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.checkpoint-')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(record, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def checkpoints(self):
        records = []
        for path in self._checkpoint_dir().glob('*.json'):
            if path.is_symlink():
                continue
            try:
                record = json.loads(path.read_text())
                if record.get('status') in {'committed', 'undone'}:
                    records.append({key: record.get(key) for key in ('id', 'path', 'created', 'status', 'change_set', 'before_hash', 'after_hash')})
            except (ValueError, OSError, AttributeError):
                continue
        return {'checkpoints': sorted(records, key=lambda record: record['created'], reverse=True)[:200]}

    @staticmethod
    def read_text(path):
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > _MAX_FILE:
            raise ValueError('File must be regular UTF-8 text under 256 KB')
        with path.open('rb') as source:
            raw = source.read(_MAX_FILE + 1)
        if len(raw) > _MAX_FILE:
            raise ValueError('File must be regular UTF-8 text under 256 KB')
        return raw.decode('utf-8')

    def write(self, relative, before, after, change_set=None):
        """Call only after approval; preserve a checkpoint and refuse stale writes."""
        if not isinstance(after, str) or len(after.encode()) > _MAX_FILE:
            raise ValueError('Write exceeds 256 KB limit')
        with _LOCK:
            path = self.path(relative)
            if self.read_text(path) != before:
                raise ValueError('File changed during approval; retry to review a fresh diff')
            record = {'id': uuid.uuid4().hex, 'path': path.relative_to(self.root).as_posix(), 'created': time.time(), 'status': 'pending',
                      'change_set': change_set, 'before': before, 'after': after,
                      'before_hash': digest(before), 'after_hash': digest(after),
                      'mode': stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644}
            self._record_write(record)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_file(path, after, record['mode'])
            record['status'] = 'committed'
            self._record_write(record)
            return {'written': relative, 'bytes': len(after.encode()), 'checkpoint_id': record['id']}

    @staticmethod
    def _atomic_file(path, text, mode):
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.herness-write-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def undo_preview(self, checkpoint_id):
        record = json.loads(self._record_path(checkpoint_id).read_text())
        if record.get('status') != 'committed':
            raise ValueError('Only committed checkpoints can be undone')
        path = self.path(record['path'])
        if path.relative_to(self.root).as_posix() != record['path']:
            raise ValueError('Checkpoint target is now a symlink to a different file')
        actual = self.read_text(path)
        if digest(actual) != record['after_hash']:
            raise ValueError('File has newer changes; undo would overwrite them')
        return {'id': checkpoint_id, 'path': record['path'], 'expected_hash': record['after_hash'],
                'diff': unified(actual, record['before'], record['path']), 'deletes_file': record['before'] is None}

    def undo(self, checkpoint_id, expected_hash):
        with _LOCK:
            preview = self.undo_preview(checkpoint_id)
            if expected_hash != preview['expected_hash']:
                raise ValueError('Review the current undo preview first')
            record = json.loads(self._record_path(checkpoint_id).read_text())
            path = self.path(record['path'])
            if record['before'] is None:
                path.unlink()
            else:
                self._atomic_file(path, record['before'], record.get('mode', 0o644))
            record['status'], record['undone_at'] = 'undone', time.time()
            self._record_write(record)
            return {'undone': checkpoint_id, 'path': record['path']}

    def sandbox(self):
        docker = shutil.which('docker')
        available = False
        if docker:
            try:
                available = subprocess.run([docker, 'info', '--format', '{{.ServerVersion}}'], stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, timeout=3).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        return {'docker_installed': bool(docker), 'docker_daemon_available': available,
                'execution_backend': 'local', 'sandbox_enforced': False,
                'note': 'Commands currently run on the local machine. Container execution is not configured.'}

    def handle(self, method, path, data, state):
        if not path.startswith('/api/workbench/'):
            return None
        suffix = path[len('/api/workbench/'):]
        data = data or {}
        if method == 'GET' and suffix == 'files':
            return self.files()
        if method == 'POST' and suffix == 'search':
            return self.search(data.get('query'), data.get('path', '.'), data.get('case_sensitive') is True)
        if method in {'GET', 'POST'} and suffix == 'symbols':
            return self.symbols(data.get('path', '.'))
        if method == 'POST' and suffix == 'git':
            return self.git(data.get('kind', 'status'))
        if method == 'GET' and suffix == 'checkpoints':
            return self.checkpoints()
        if method == 'POST' and suffix == 'undo-preview':
            return self.undo_preview(data.get('id'))
        if method == 'POST' and suffix == 'undo':
            if getattr(state, 'busy', False):
                raise ValueError('Wait for the active turn to finish before undoing changes')
            if data.get('confirmed') is not True:
                raise ValueError('Confirm the reviewed undo before applying it')
            return self.undo(data.get('id'), data.get('expected_hash'))
        if method == 'GET' and suffix == 'sandbox':
            return self.sandbox()
        raise ValueError('Unknown workbench action or method')
