"""Project tools with scoped permissions and explicit approval of mutations."""
from __future__ import annotations

import codecs
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from .state import ProjectStore
from .workbench import Workbench, unified


def schema(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': required, 'additionalProperties': False}}}


TOOL_SCHEMAS = [
    schema('list_files', 'List up to 200 names in a project directory.', {'path': {'type': 'string'}}, ['path']),
    schema('read_file', 'Read a UTF-8 project file (at most 32 KB).', {'path': {'type': 'string'}}, ['path']),
    schema('search_files', 'Search literal text in project files; excludes credentials, agent state, dependency/build folders and project ignored paths.',
           {'path': {'type': 'string'}, 'query': {'type': 'string'}}, ['path', 'query']),
    schema('project_symbols', 'Map Python function/class definitions and declarations in common source languages. Returns filenames and line numbers.',
           {'path': {'type': 'string'}}, ['path']),
    schema('git_status', 'Read Git branch and working-tree status without changing files.', {}, []),
    schema('git_diff', 'Read tracked Git changes, excluding protected files and external diff helpers.', {}, []),
    schema('write_file', 'Create or replace a UTF-8 project file after diff approval. Creates a recoverable checkpoint.',
           {'path': {'type': 'string'}, 'content': {'type': 'string'}}, ['path', 'content']),
    schema('patch_file', 'Replace exactly one matching text region in an existing file after diff approval; preserve unrelated changes.',
           {'path': {'type': 'string'}, 'old': {'type': 'string'}, 'new': {'type': 'string'}}, ['path', 'old', 'new']),
    schema('undo_change', 'Undo one checkpoint after approval, only if no newer file changes would be overwritten.',
           {'checkpoint_id': {'type': 'string'}}, ['checkpoint_id']),
    schema('remember', 'Replace project memory with preferences or decisions after approval. Read current memory in the system context first.',
           {'content': {'type': 'string'}}, ['content']),
    schema('run_command', 'Run a shell command after approval. Local execution is not sandboxed; timeout 30 seconds.',
           {'command': {'type': 'string'}}, ['command']),
    schema('run_tests', 'Run a project test command after approval and return the actual exit code and output. Local execution is not sandboxed.',
           {'command': {'type': 'string'}}, ['command']),
]

_REQUIRED = {entry['function']['name']: set(entry['function']['parameters']['required']) for entry in TOOL_SCHEMAS}
_TOOL_SCOPE = {'list_files': 'read', 'read_file': 'read', 'search_files': 'read', 'project_symbols': 'read',
               'git_status': 'read', 'git_diff': 'read', 'write_file': 'write', 'patch_file': 'write',
               'undo_change': 'write', 'remember': 'memory', 'run_command': 'execute', 'run_tests': 'execute'}


class ToolRunner:
    def __init__(self, root: Path, approve: Callable, command_timeout: float = 30, cancel=None,
                 mode='act', scopes=None, on_output=None, change_set=None):
        self.root = Path(root).resolve()
        self.approve, self.command_timeout, self.cancel = approve, command_timeout, cancel
        if mode not in {'act', 'plan', 'read-only'}:
            raise ValueError('Tool mode must be act, plan, or read-only')
        self.mode = mode
        self.scopes = set(scopes) if scopes is not None else {'read', 'write', 'execute', 'memory'}
        if not self.scopes <= {'read', 'write', 'execute', 'memory'}:
            raise ValueError('Unknown tool permission scope')
        if mode in {'plan', 'read-only'}:
            self.scopes &= {'read'}
        self.on_output, self.change_set = on_output, change_set
        self.workbench = Workbench(self.root)

    def schemas(self):
        return [entry for entry in TOOL_SCHEMAS if _TOOL_SCOPE[entry['function']['name']] in self.scopes]

    def _require(self, scope):
        if scope not in self.scopes:
            raise ValueError(f'The {scope} tool scope is disabled in this session')

    def path(self, value):
        return self.workbench.path(value)

    def cancelled(self):
        return bool(self.cancel and self.cancel.event.is_set())

    def execute(self, name: str, arguments: str) -> dict:
        try:
            args = json.loads(arguments)
            if name not in _REQUIRED:
                raise ValueError('Unknown tool')
            if not isinstance(args, dict) or set(args) != _REQUIRED[name] or any(not isinstance(v, str) for v in args.values()):
                raise ValueError('Invalid tool arguments')
            self._require(_TOOL_SCOPE[name])
            if self.cancelled():
                return {'cancelled': True}
            if name == 'search_files':
                return self.workbench.search(args['query'], args['path'])
            if name == 'project_symbols':
                return self.workbench.symbols(args['path'])
            if name in {'git_status', 'git_diff'}:
                return self.workbench.git('status' if name == 'git_status' else 'diff')
            if name == 'undo_change':
                preview = self.workbench.undo_preview(args['checkpoint_id'])
                if not self.approve({'kind': 'undo', 'description': 'Undo change to ' + preview['path'], **preview}):
                    return {'denied': True}
                if self.cancelled():
                    return {'cancelled': True}
                return self.workbench.undo(preview['id'], preview['expected_hash'])
            if name == 'remember':
                store = ProjectStore(self.root)
                before, after = store.memory(), args['content']
                if len(after.encode()) > 32_000:
                    raise ValueError('Memory exceeds 32 KB')
                if not self.confirm('memory', 'Project memory', before, after):
                    return {'denied': True}
                if store.memory() != before:
                    raise ValueError('Memory changed during approval; retry to review a fresh diff')
                store.save_memory(after)
                return {'remembered': True}
            if name in {'run_command', 'run_tests'}:
                result = self.command(args['command'])
                if name == 'run_tests':
                    result['test_command'] = args['command']
                    result['passed'] = result.get('exit_code') == 0 and not result.get('timed_out') and not result.get('cancelled')
                return result
            path = self.path(args['path'])
            if name == 'list_files':
                names = sorted(p.name + ('/' if p.is_dir() else '') for p in path.iterdir())
                return {'entries': names[:200], 'truncated': len(names) > 200}
            if name == 'read_file':
                if not path.is_file():
                    raise ValueError('Path is not a regular file')
                with path.open('rb') as source:
                    content = source.read(32_001)
                decoder = codecs.getincrementaldecoder('utf-8')()
                text = decoder.decode(content[:32_000], final=len(content) <= 32_000)
                return {'content': text, 'truncated': len(content) > 32_000}
            before = self.workbench.read_text(path)
            if name == 'patch_file':
                if before is None:
                    raise ValueError('Patches require an existing file')
                old = args['old']
                if not old or before.count(old) != 1:
                    raise ValueError('Patch old text must match exactly once; read the current file and retry')
                after = before.replace(old, args['new'], 1)
            else:
                after = args['content']
            if len(after.encode()) > 256_000:
                raise ValueError('Write exceeds 256 KB limit')
            if before == after:
                return {'unchanged': args['path']}
            if not self.confirm('write', args['path'], before, after):
                return {'denied': True, 'message': 'User did not approve this write'}
            if self.path(args['path']) != path:
                raise ValueError('File path changed during approval')
            return self.workbench.write(args['path'], before, after, self.change_set)
        except (ValueError, OSError, UnicodeError) as exc:
            return {'error': str(exc)}

    def confirm(self, kind, path, before, after):
        self._require('memory' if kind == 'memory' else 'write')
        allowed = self.approve({'kind': kind, 'path': path, 'diff': unified(before, after, path),
                                'description': ('Create ' if before is None else 'Update ') + path,
                                'new_file': before is None})
        return allowed and not self.cancelled()

    def command(self, command: str) -> dict:
        self._require('execute')
        if not isinstance(command, str) or not command.strip() or len(command) > 16_000:
            raise ValueError('Command must contain 1–16000 characters')
        if not self.approve(f'SHELL in {self.root}\n{command}\nThis command can access your machine and network; it is not sandboxed.'):
            return {'denied': True, 'message': 'User did not approve this command'}
        if self.cancelled():
            return {'cancelled': True}
        # Temporary output capture avoids pipe deadlocks and unbounded in-memory capture.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, shell=True, cwd=self.root, stdout=output,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            timed_out, offset = False, 0
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            def flush_output(final=False):
                nonlocal offset
                if not self.on_output:
                    return
                chunk = os.pread(output.fileno(), min(8192, 32_000 - offset), offset) if offset < 32_000 else b''
                offset += len(chunk)
                text = decoder.decode(chunk, final=final)
                if text:
                    self.on_output(text)
            try:
                deadline = time.monotonic() + self.command_timeout
                while process.poll() is None:
                    flush_output()
                    if self.cancel and self.cancel.event.wait(.05):
                        break
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, self.command_timeout)
                    try:
                        process.wait(timeout=.05)
                    except subprocess.TimeoutExpired:
                        pass
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            # Drain up to the same 32 KB limit used by returned output.
            while self.on_output and offset < min(os.fstat(output.fileno()).st_size, 32_000):
                flush_output()
            flush_output(final=True)
            output.seek(0)
            content = output.read(32_001)
        return {'exit_code': process.returncode, 'timed_out': timed_out, 'cancelled': self.cancelled(),
                'output': content[:32_000].decode('utf-8', errors='replace'), 'truncated': len(content) > 32_000}
