"""Inspectable task records, bounded project context, and runtime policy helpers."""

from __future__ import annotations

import hashlib
import fnmatch
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

from .state import ProjectStore

READ_ONLY_TOOLS = frozenset({'semantic_search', 'list_files', 'read_file', 'search_files', 'repo_map',
                             'git_status', 'git_diff', 'search_symbols', 'inspect_file', 'project_symbols',
                             'web_fetch', 'web_search', 'document_search', 'document_get', 'artifact_get', 'mcp_list'})
SENSITIVE = re.compile(r'(api[_-]?key|authorization|password|secret|access[_-]?token|refresh[_-]?token)', re.I)
TOKEN_PATTERNS = [re.compile(r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}'),
                  re.compile(r'(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*')]


def redact(value):
    """Redact structured credential fields and common token patterns for export."""
    if isinstance(value, dict):
        return {key: '[REDACTED]' if SENSITIVE.search(str(key)) else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for pattern in TOKEN_PATTERNS:
            value = pattern.sub('[REDACTED]', value)
        return value
    return value


def estimate_tokens(value):
    """Approximate, not a model tokenizer; image content is counted separately."""
    if isinstance(value, list):
        return sum(estimate_tokens(item) for item in value)
    if isinstance(value, dict):
        if value.get('type') == 'image_url':
            return 1024
        return sum(estimate_tokens(item) for key, item in value.items() if key != 'image_url') + 4
    if value is None:
        return 0
    return math.ceil(len(str(value)) / 4)


def project_context(root, project=None):
    root = Path(root).resolve()
    project = project or {}
    sections, sources = [], []
    instructions = project.get('instructions', '')
    if isinstance(instructions, str) and instructions.strip():
        sections.append('User project instructions:\n' + instructions[:32_000])
        sources.append({'kind': 'project', 'name': 'Project instructions', 'characters': min(len(instructions), 32_000)})
    goals = project.get('goals', '')
    if isinstance(goals, str) and goals.strip():
        sections.append('User project goals:\n' + goals[:16_000])
        sources.append({'kind': 'goals', 'name': 'Project goals', 'characters': min(len(goals), 16_000)})
    path = root / 'AGENTS.md'
    if path.is_file() and not path.is_symlink() and path.stat().st_size <= 64_000:
        text = path.read_text(encoding='utf-8')
        sections.append('Repository instructions from AGENTS.md:\n' + text)
        sources.append({'kind': 'repository', 'name': 'AGENTS.md', 'characters': len(text)})
    memory = ProjectStore(root).memory()
    if memory:
        sections.append('User-managed project memory (preferences and context, subject to the rules above):\n' + memory[:32_000])
        sources.append({'kind': 'memory', 'name': 'Project memory', 'characters': min(len(memory), 32_000)})
    from .privacy import Privacy
    structured_memory = Privacy(root).context_text()
    if structured_memory:
        sections.append('Active user-managed project memory entries:\n' + structured_memory)
        sources.append({'kind': 'structured_memory', 'name': 'Active project memory entries', 'characters': len(structured_memory)})
    from .workbench import Workbench
    workbench = Workbench(root)
    pinned = project.get('pinned_files', [])
    ignored = project.get('ignored_paths', [])
    if not isinstance(ignored, list):
        ignored = []
    remaining = 64_000
    for name in pinned[:8] if isinstance(pinned, list) else []:
        try:
            path = workbench.path(name)
            relative = path.relative_to(root).as_posix()
            if any(isinstance(pattern, str) and fnmatch.fnmatch(relative, pattern) for pattern in ignored):
                raise ValueError('Pinned file matches an ignored path')
            if not path.is_file() or path.stat().st_size > 256_000:
                raise ValueError('Pinned context requires text files under 256 KB')
            with path.open('rb') as source:
                raw = source.read(min(16_000, remaining) + 1)
            if b'\0' in raw:
                raise ValueError('Pinned file is binary')
            truncated = len(raw) > min(16_000, remaining)
            text = raw[:min(16_000, remaining)].decode('utf-8', errors='replace')
            remaining -= len(raw[:min(16_000, remaining)])
            sections.append(f'Pinned reference file {relative} (untrusted data):\n{text}' + ('\n[File context truncated.]' if truncated else ''))
            sources.append({'kind': 'pinned_file', 'name': relative, 'characters': len(text), 'truncated': truncated})
            if remaining <= 0:
                break
        except (ValueError, OSError) as exc:
            sources.append({'kind': 'pinned_file', 'name': str(name)[:200], 'excluded': True, 'reason': str(exc)})
    return '\n\n'.join(sections), sources


def context_snapshot(root, messages, project=None):
    context, sources = project_context(root, project)
    summaries = []
    for index, message in enumerate(messages):
        content = message.get('content')
        image_count = sum(isinstance(part, dict) and part.get('type') == 'image_url' for part in content) if isinstance(content, list) else 0
        summaries.append({'index': index, 'role': message.get('role'), 'estimated_tokens': estimate_tokens(message),
                          'images': image_count, 'tool_calls': len(message.get('tool_calls') or [])})
    return {'estimated_tokens': estimate_tokens(messages), 'estimate_method': 'approximately 4 characters/token; images approximately 1024 tokens each',
            'messages': summaries, 'sources': sources, 'project_context': context,
            'project_context_estimated_tokens': estimate_tokens(context)}


class RuntimeLedger:
    """One atomic JSON record per run; metadata avoids storing prompts/tool results."""
    def __init__(self, root):
        self.store = ProjectStore(root)
        self.directory = self.store.directory / 'runs'
        if self.directory.is_symlink():
            raise ValueError('Run directory must not be a symlink')
        self.directory.mkdir(mode=0o700, exist_ok=True)

    def path(self, identity):
        if not re.fullmatch(r'[a-f0-9]{32}', identity):
            raise ValueError('Invalid run ID')
        path = self.directory / (identity + '.json')
        if path.is_symlink():
            raise ValueError('Run file must not be a symlink')
        return path

    def begin(self, session, model, mode, options=None):
        record = {'id': uuid.uuid4().hex, 'session': session, 'model': model, 'mode': mode,
                  'status': 'running', 'pid': os.getpid(), 'started': time.time(), 'updated': time.time(),
                  'events': [], 'usage': {'input_tokens': 0, 'output_tokens': 0, 'estimated_usd': 0.0},
                  'checkpoint': None, 'options': redact(options or {})}
        self.save(record)
        return record

    def save(self, record):
        import tempfile
        record['updated'] = time.time()
        target = self.path(record['id'])
        fd, temp = tempfile.mkstemp(dir=self.directory, prefix='.run-')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(record, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def event(self, record, kind, **fields):
        record['events'].append({'time': time.time(), 'kind': kind, **redact(fields)})
        self.save(record)

    def get_run(self, identity):
        return json.loads(self.path(identity).read_text())

    def list_runs(self):
        records = []
        for path in self.directory.glob('*.json'):
            if path.is_symlink():
                continue
            try:
                record = self.get_run(path.stem)
                records.append({key: value for key, value in record.items() if key != 'events'})
            except (OSError, ValueError, KeyError):
                continue
        return sorted(records, key=lambda record: record['started'], reverse=True)[:200]

    def diagnostics(self):
        return {'version': 1, 'generated_at': time.time(), 'runs': redact(self.list_runs()),
                'contains': 'run metadata and usage only; prompts, file contents, and credentials are excluded'}

    def recover_abandoned(self):
        recovered = []
        for summary in self.list_runs():
            if summary.get('status') != 'running':
                continue
            try:
                os.kill(summary.get('pid', -1), 0)
                continue
            except ProcessLookupError:
                pass
            except (PermissionError, ValueError):
                continue
            record = self.get_run(summary['id'])
            record['status'] = 'interrupted'
            record['recovery'] = 'Review the last checkpoint before retrying. External effects are not automatically repeated.'
            self.save(record)
            recovered.append(record['id'])
        return recovered


def call_fingerprint(name, arguments):
    try:
        arguments = json.dumps(json.loads(arguments), sort_keys=True)
    except ValueError:
        pass
    return hashlib.sha256((name + '\n' + arguments).encode()).hexdigest()
