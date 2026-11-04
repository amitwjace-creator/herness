"""Versioned artifacts, document library, CSV analysis, and read-only SQLite.

All operations are local. HTML previews are static and additionally require an
iframe with an empty sandbox attribute. PDF export is an optional capability.
"""
from __future__ import annotations

import base64
import csv
import difflib
import hashlib
import html
import importlib.util
import io
import json
import math
import re
import secrets
import sqlite3
import statistics
import threading
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from .state import ProjectStore

MAX_FILE = 4_000_000
MAX_TEXT = 1_000_000
TEMPLATES = {
    'project-brief': '# Project brief\n\n## Goal\n\n## Audience\n\n## Scope\n\n## Acceptance criteria\n',
    'meeting-notes': '# Meeting notes\n\n## Participants\n\n## Decisions\n\n## Action items\n\n## Open questions\n',
    'decision-record': '# Decision record\n\n## Context\n\n## Options\n\n## Decision\n\n## Consequences\n',
}


def _text(value, name='content', limit=MAX_TEXT):
    if not isinstance(value, str) or len(value.encode()) > limit:
        raise ValueError(f'{name} must be text under {limit} bytes')
    return value


def _download(name, mime, raw):
    return {'filename': name, 'mime': mime, 'data': base64.b64encode(raw).decode()}


def _integer(value, name, low=1, high=500):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f'{name} must be an integer between {low} and {high}')
    return value


class _StaticHTML(HTMLParser):
    tags = {'p', 'div', 'span', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li',
            'table', 'thead', 'tbody', 'tr', 'th', 'td', 'pre', 'code', 'blockquote',
            'strong', 'em', 'b', 'i', 'br', 'hr', 'a', 'section', 'article'}
    hidden = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.depth += 1
        if self.depth or tag not in self.tags:
            return
        safe = ''
        if tag == 'a':
            for key, value in attrs:
                try:
                    allowed = key == 'href' and value and urlsplit(value).scheme in {'https', 'http'}
                except ValueError:
                    allowed = False
                if allowed:
                    safe = ' href="' + html.escape(value, quote=True) + '" rel="noreferrer noopener"'
        self.output.append('<' + tag + safe + '>')

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.depth = max(0, self.depth - 1)
            return
        if not self.depth and tag in self.tags:
            self.output.append('</' + tag + '>')

    def handle_data(self, data):
        if not self.depth:
            self.output.append(html.escape(data))


def static_html(content, title, source_format='text'):
    if source_format == 'html':
        parser = _StaticHTML()
        parser.feed(content)
        body = ''.join(parser.output)
    else:
        body = '<pre>' + html.escape(content) + '</pre>'
    # No remote resources, scripts, frames, or forms. Links require explicit user navigation.
    return ('<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" '
            'content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;; form-action &#39;none&#39;">'
            '<title>' + html.escape(title) + '</title><style>body{font:16px/1.7 system-ui;max-width:900px;'
            'margin:40px auto;padding:0 24px;color:#242424}pre{white-space:pre-wrap;overflow-wrap:anywhere}'
            'table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:8px}</style></head><body>'
            + body + '</body></html>')


def _zip(raw):
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        infos = archive.infolist()
        if len(infos) > 1500 or sum(i.file_size for i in infos) > 20_000_000:
            raise ValueError('Office document expands beyond the 20 MB limit')
        if any(i.flag_bits & 1 for i in infos):
            raise ValueError('Encrypted Office documents are not supported')
        return archive
    except zipfile.BadZipFile:
        raise ValueError('Invalid Office document archive') from None


def _xml(archive, name):
    try:
        return ET.fromstring(archive.read(name))
    except (KeyError, ET.ParseError):
        raise ValueError('Office document has missing or invalid XML') from None


def _local(element):
    return element.tag.rsplit('}', 1)[-1]


def extract_document(name, raw):
    suffix = Path(name).suffix.lower()
    units = []
    if raw.startswith(b'%PDF-'):
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                raise ValueError('Encrypted PDFs are not supported')
            if len(reader.pages) > 100:
                raise ValueError('PDFs must have at most 100 pages')
            for index, page in enumerate(reader.pages):
                units.append({'label': f'Page {index + 1}', 'text': page.extract_text() or ''})
        except ValueError:
            raise
        except Exception:
            raise ValueError('Could not extract PDF text') from None
        kind = 'pdf'
        if not any(unit['text'].strip() for unit in units):
            raise ValueError('No PDF text found; OCR is required for scanned documents')
    elif suffix in {'.docx', '.xlsx', '.pptx'}:
        with _zip(raw) as archive:
            if suffix == '.docx':
                root = _xml(archive, 'word/document.xml')
                paragraphs = [''.join(node.text or '' for node in paragraph.iter() if _local(node) == 't')
                              for paragraph in root.iter() if _local(paragraph) == 'p']
                units = [{'label': f'Paragraph {i + 1}', 'text': value} for i, value in enumerate(paragraphs) if value]
            elif suffix == '.pptx':
                slides = sorted((n for n in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)),
                                key=lambda n: int(re.search(r'slide(\d+)', n).group(1)))
                for index, name in enumerate(slides):
                    root = _xml(archive, name)
                    units.append({'label': f'Slide {index + 1}', 'text': '\n'.join(n.text or '' for n in root.iter() if _local(n) == 't')})
            else:
                shared = []
                if 'xl/sharedStrings.xml' in archive.namelist():
                    shared = [''.join(n.text or '' for n in item.iter() if _local(n) == 't')
                              for item in _xml(archive, 'xl/sharedStrings.xml') if _local(item) == 'si']
                sheets = sorted(n for n in archive.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n))
                for index, name in enumerate(sheets):
                    root = _xml(archive, name)
                    cells = []
                    for cell in root.iter():
                        if _local(cell) != 'c':
                            continue
                        value = next((n.text or '' for n in cell if _local(n) == 'v'), '')
                        if cell.get('t') == 's':
                            try:
                                value = shared[int(value)]
                            except (ValueError, IndexError):
                                raise ValueError('Invalid spreadsheet shared string reference') from None
                        elif cell.get('t') == 'inlineStr':
                            value = ''.join(n.text or '' for n in cell.iter() if _local(n) == 't')
                        cells.append(f"{cell.get('r', '?')}: {value}")
                    units.append({'label': f'Sheet {index + 1}', 'text': '\n'.join(cells)})
            kind = suffix[1:]
    elif raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff') or (raw[:4] == b'RIFF' and raw[8:12] == b'WEBP'):
        return {'kind': 'image', 'units': [], 'text': '', 'note': 'Original image retained; visual analysis requires a vision model.'}
    else:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('Supported formats: UTF-8 text/code, PDF, DOCX, XLSX, PPTX, PNG, JPEG, WebP') from None
        if '\0' in text:
            raise ValueError('Binary document format is unsupported')
        units = [{'label': 'Document', 'text': text}]
        kind = 'text'
    combined = '\n\n'.join(f"[{unit['label']}]\n{unit['text']}" for unit in units)
    if len(combined.encode()) > MAX_TEXT:
        raise ValueError('Extracted text exceeds 1 MB')
    return {'kind': kind, 'units': units, 'text': combined}


def _docx(content):
    paragraphs = ''.join('<w:p><w:r><w:t xml:space="preserve">' + html.escape(line) + '</w:t></w:r></w:p>'
                         for line in content.split('\n'))
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + paragraphs + '</w:body></w:document>')
    return output.getvalue()


def _csv(content):
    _text(content)
    try:
        reader = csv.DictReader(io.StringIO(content))
        columns = reader.fieldnames or []
        if not columns or len(columns) > 100 or len(set(columns)) != len(columns) or any(not c for c in columns):
            raise ValueError('CSV needs 1–100 unique, nonempty column names')
        rows = []
        for row in reader:
            if len(rows) >= 10_000:
                raise ValueError('CSV exceeds 10,000 rows')
            if None in row:
                raise ValueError('CSV row has more fields than its header')
            rows.append({key: value or '' for key, value in row.items()})
        return columns, rows
    except csv.Error:
        raise ValueError('Invalid CSV') from None


def _csv_text(columns, rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


class Documents:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.store = ProjectStore(self.root)
        self.lock = threading.RLock()

    def _state(self):
        state = json.loads(self.store.read('documents.json', '{"artifacts":{},"library":{}}'))
        if not isinstance(state, dict) or not isinstance(state.get('artifacts'), dict) or not isinstance(state.get('library'), dict):
            raise ValueError('Invalid document library state')
        return state

    def _save(self, state):
        encoded = json.dumps(state)
        if len(encoded.encode()) > 32_000_000:
            raise ValueError('Document library exceeds 32 MB; delete older items before adding more')
        self.store.write('documents.json', encoded)

    def overview(self):
        state = self._state()
        return {'capabilities': {'formats': ['text', 'pdf', 'docx', 'xlsx', 'pptx', 'png', 'jpeg', 'webp'],
                                 'pdf_export': importlib.util.find_spec('reportlab') is not None,
                                 'ocr': False, 'python_execution': False, 'sql': 'read-only'},
                'artifacts': [{key: item[key] for key in ('id', 'title', 'format', 'revision', 'updated')}
                              | {'versions': len(item['versions'])} for item in state['artifacts'].values()],
                'library': [{key: item[key] for key in ('id', 'name', 'kind', 'size', 'created')}
                            | {'units': len(item['units'])} for item in state['library'].values()],
                'templates': [{'id': key, 'content': value} for key, value in TEMPLATES.items()]}

    def artifact(self, data):
        state = self._state()
        action = data.get('action', 'get')
        identity = data.get('id')
        if action == 'create':
            content = _text(data.get('content', TEMPLATES.get(data.get('template'), '')))
            title = _text(data.get('title', 'Untitled'), 'title', 200).strip()
            if not title:
                raise ValueError('Artifact title is required')
            format_ = data.get('format', 'markdown')
            if format_ not in {'markdown', 'text', 'html', 'svg', 'json'}:
                raise ValueError('Unsupported artifact format')
            identity = secrets.token_hex(12)
            item = {'id': identity, 'title': title, 'format': format_, 'content': content,
                    'revision': 1, 'updated': time.time(), 'versions': [{'revision': 1, 'content': content}]}
            state['artifacts'][identity] = item
            self._save(state)
            return item
        if not isinstance(identity, str) or identity not in state['artifacts']:
            raise ValueError('Artifact not found')
        item = state['artifacts'][identity]
        if action == 'get':
            return item
        if action in {'update', 'delete'}:
            if data.get('revision') != item['revision']:
                raise ValueError('Artifact changed; reload before editing or deleting')
            if action == 'delete':
                del state['artifacts'][identity]
                self._save(state)
                return {'deleted': identity}
            content = _text(data.get('content'))
            title = _text(data.get('title', item['title']), 'title', 200).strip()
            if not title:
                raise ValueError('Artifact title is required')
            if len(item['versions']) >= 100:
                raise ValueError('Artifact reached 100 versions; export or create a new artifact')
            item.update(content=content, title=title, revision=item['revision'] + 1, updated=time.time())
            item['versions'].append({'revision': item['revision'], 'content': content})
            self._save(state)
            return item
        if action == 'diff':
            left = _integer(data.get('from_version', max(1, item['revision'] - 1)), 'from_version', 1, item['revision'])
            right = _integer(data.get('to_version', item['revision']), 'to_version', 1, item['revision'])
            contents = {version['revision']: version['content'] for version in item['versions']}
            return {'diff': '\n'.join(difflib.unified_diff(contents[left].splitlines(), contents[right].splitlines(),
                                                         fromfile=f'Version {left}', tofile=f'Version {right}', lineterm=''))}
        if action == 'preview':
            return {'html': static_html(item['content'], item['title'], item['format']), 'sandbox': '',
                    'note': 'Static preview; scripts, embedded resources, and forms are removed.'}
        if action == 'export':
            return self.export(item, data.get('export_format', item['format']))
        raise ValueError('Unknown artifact action')

    def export(self, item, format_):
        name = re.sub(r'[^A-Za-z0-9_-]+', '-', item['title']).strip('-')[:80] or 'artifact'
        content = item['content']
        if format_ in {'markdown', 'text', 'json', 'svg'}:
            extension, mime = {'markdown': ('md', 'text/markdown'), 'text': ('txt', 'text/plain'),
                               'json': ('json', 'application/json'), 'svg': ('svg', 'image/svg+xml')}[format_]
            if format_ == 'json':
                content = json.dumps(item, indent=2)
            return _download(name + '.' + extension, mime, content.encode())
        if format_ == 'html':
            return _download(name + '.html', 'text/html', static_html(content, item['title'], item['format']).encode())
        if format_ == 'docx':
            return _download(name + '.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', _docx(content))
        if format_ == 'pdf':
            try:
                from reportlab.lib.styles import getSampleStyleSheet
                from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            except ImportError:
                raise ValueError('PDF export requires the optional reportlab package') from None
            output = io.BytesIO()
            style = getSampleStyleSheet()
            blocks = [Paragraph(html.escape(item['title']), style['Title'])]
            for line in content.splitlines():
                blocks.append(Paragraph(html.escape(line) or '&#160;', style['BodyText']))
                blocks.append(Spacer(1, 4))
            SimpleDocTemplate(output).build(blocks)
            return _download(name + '.pdf', 'application/pdf', output.getvalue())
        if format_ == 'zip':
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('artifact.json', json.dumps(item, indent=2))
                archive.writestr('content.txt', content)
                archive.writestr('preview.html', static_html(content, item['title'], item['format']))
            return _download(name + '.zip', 'application/zip', output.getvalue())
        raise ValueError('Unsupported export format')

    def upload(self, data):
        name = _text(data.get('name'), 'filename', 200).replace('\\', '/').split('/')[-1]
        if not name or name == '.env' or name.startswith('.env.') or name.endswith(('.pem', '.key')):
            raise ValueError('Invalid or protected filename')
        encoded = data.get('data')
        if not isinstance(encoded, str) or len(encoded) > 5_400_000:
            raise ValueError('Upload must be base64 data under 4 MB')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError('Invalid base64 upload') from None
        if not raw or len(raw) > MAX_FILE:
            raise ValueError('Upload must be nonempty and at most 4 MB')
        identity = hashlib.sha256(raw).hexdigest()
        state = self._state()
        if identity in state['library']:
            return {'id': identity, 'duplicate': True, 'name': state['library'][identity]['name']}
        document = extract_document(name, raw)
        item = {'id': identity, 'name': name, 'size': len(raw), 'data': encoded,
                'created': time.time(), **document}
        state['library'][identity] = item
        self._save(state)
        return {key: value for key, value in item.items() if key != 'data'} | {'duplicate': False}

    def library(self, data):
        state = self._state()
        action = data.get('action', 'get')
        if action == 'search':
            query = _text(data.get('query', ''), 'query', 200).casefold()
            results = []
            for item in state['library'].values():
                for unit in item['units']:
                    index = unit['text'].casefold().find(query)
                    if index >= 0:
                        results.append({'id': item['id'], 'name': item['name'], 'citation': unit['label'],
                                        'excerpt': unit['text'][max(0, index - 100):index + 350]})
            return {'results': results[:100], 'truncated': len(results) > 100}
        identity = data.get('id')
        if not isinstance(identity, str) or identity not in state['library']:
            raise ValueError('Library document not found')
        item = state['library'][identity]
        if action == 'get':
            return {key: value for key, value in item.items() if key != 'data'}
        if action == 'download':
            return _download(item['name'], 'application/octet-stream', base64.b64decode(item['data']))
        if action == 'delete':
            del state['library'][identity]
            self._save(state)
            return {'deleted': identity, 'note': 'Removed from the document library. Existing chat attachments remain in their conversations.'}
        raise ValueError('Unknown library action')

    def csv(self, data):
        columns, rows = _csv(data.get('content'))
        action = data.get('action', 'profile')
        limit = _integer(data.get('limit', 100), 'limit', 1, 1000)
        if action == 'profile':
            profiles = {}
            for column in columns:
                values = [row[column] for row in rows]
                present = [value for value in values if value.strip()]
                numeric = []
                for value in present:
                    try:
                        number = float(value)
                        if not math.isfinite(number):
                            break
                        numeric.append(number)
                    except ValueError:
                        break
                numeric_column = bool(present) and len(numeric) == len(present)
                profiles[column] = {'missing': len(values) - len(present), 'unique': len(set(present)),
                                    'type': 'number' if numeric_column else 'text'}
                if numeric_column:
                    ordered = sorted(numeric)
                    middle = len(ordered) // 2
                    median = ordered[middle] if len(ordered) % 2 else ordered[middle - 1] / 2 + ordered[middle] / 2
                    total = sum(numeric)
                    profiles[column].update(min=min(numeric), max=max(numeric), mean=statistics.mean(numeric),
                                             median=median, sum=total if math.isfinite(total) else None)
                    if not math.isfinite(total):
                        profiles[column]['note'] = 'Sum exceeds the numeric range; individual values and other statistics remain available.'
            unique_rows = {tuple(row[column] for column in columns) for row in rows}
            return {'columns': columns, 'rows': rows[:limit], 'row_count': len(rows),
                    'duplicate_rows': len(rows) - len(unique_rows), 'profile': profiles, 'truncated': len(rows) > limit}
        if action == 'filter':
            column, operator, value = data.get('column'), data.get('operator', 'contains'), data.get('value', '')
            if column not in columns or operator not in {'contains', 'equals', 'not_equals', 'gt', 'lt'}:
                raise ValueError('Choose a valid column and filter operator')
            value = str(value)
            def keep(row):
                if operator == 'contains':
                    return value.casefold() in row[column].casefold()
                if operator in {'equals', 'not_equals'}:
                    return (row[column] == value) == (operator == 'equals')
                try:
                    left, right = float(row[column]), float(value)
                    return math.isfinite(left) and math.isfinite(right) and (left > right if operator == 'gt' else left < right)
                except ValueError:
                    return False
            filtered = [row for row in rows if keep(row)]
            return {'columns': columns, 'rows': filtered[:limit], 'row_count': len(filtered),
                    'content': _csv_text(columns, filtered), 'truncated': len(filtered) > limit}
        if action == 'compare':
            other_columns, other = _csv(data.get('other'))
            if columns != other_columns:
                raise ValueError('CSV comparison requires matching columns in the same order')
            from collections import Counter
            left = Counter(tuple(row[c] for c in columns) for row in rows)
            right = Counter(tuple(row[c] for c in columns) for row in other)
            added, removed = list((right - left).elements()), list((left - right).elements())
            return {'columns': columns, 'added': [dict(zip(columns, row)) for row in added[:limit]],
                    'removed': [dict(zip(columns, row)) for row in removed[:limit]],
                    'added_count': len(added), 'removed_count': len(removed)}
        if action == 'export':
            return _download('data.csv', 'text/csv', _csv_text(columns, rows).encode())
        if action == 'chart':
            x, y = data.get('x'), data.get('y')
            if x not in columns or y not in columns or not rows:
                raise ValueError('Choose existing x/y columns in a nonempty dataset')
            selected = rows[:40]
            try:
                values = [float(row[y]) for row in selected]
            except ValueError:
                raise ValueError('The chart value column must be numeric') from None
            if any(not math.isfinite(v) or v < 0 for v in values):
                raise ValueError('Bar charts currently require finite, nonnegative values')
            scale = max(values) or 1
            width = 560 / len(values)
            bars = []
            for index, (row, value) in enumerate(zip(selected, values)):
                h = value / scale * 220
                xpos = 40 + index * width
                bars.append(f'<rect x="{xpos:.2f}" y="{250-h:.2f}" width="{max(1,width-4):.2f}" height="{h:.2f}" fill="#4d6bfe"><title>{html.escape(row[x])}: {value:g}</title></rect>')
                bars.append(f'<text x="{xpos:.2f}" y="270" font-size="9">{html.escape(row[x][:12])}</text>')
            svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 300"><rect width="640" height="300" fill="white"/><text x="40" y="22" font-size="16">' + html.escape(y + ' by ' + x) + '</text>' + ''.join(bars) + '</svg>'
            item = self.artifact({'action': 'create', 'title': y + ' by ' + x, 'format': 'svg', 'content': svg})
            return {'artifact': item, 'rows_used': len(selected), 'truncated': len(rows) > 40,
                    'note': 'Static SVG bar chart. Hover bars to inspect their full labels and values.'}
        raise ValueError('Unknown CSV action')

    def sql(self, data):
        value = data.get('path')
        if not isinstance(value, str) or Path(value).is_absolute():
            raise ValueError('Choose a database path relative to the project')
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root) or any(p.startswith('.') for p in path.relative_to(self.root).parts) or not path.is_file():
            raise ValueError('Database must be an existing non-hidden file inside the project')
        if path.stat().st_size > 100_000_000:
            raise ValueError('Database exceeds the 100 MB local query limit')
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
        connection.enable_load_extension(False)
        connection.execute('PRAGMA query_only = ON')
        deadline = time.monotonic() + 2
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        safe_functions = {'count', 'sum', 'avg', 'min', 'max', 'abs', 'round', 'lower', 'upper', 'length', 'substr',
                          'substring', 'coalesce', 'ifnull', 'nullif', 'trim', 'ltrim', 'rtrim', 'replace', 'date',
                          'datetime', 'strftime', 'julianday', 'unixepoch', 'typeof', 'total', 'group_concat',
                          'row_number', 'rank', 'dense_rank', 'lag', 'lead', 'like', 'glob'}
        def authorize(action, arg1, arg2, db, trigger):
            if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or '').lower() in safe_functions:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        connection.set_authorizer(authorize)
        try:
            if data.get('action', 'query') == 'schema':
                rows = connection.execute("SELECT name, type, sql FROM sqlite_schema WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 100").fetchall()
                return {'tables': [{'name': r[0], 'type': r[1], 'sql': r[2]} for r in rows], 'read_only': True}
            query = _text(data.get('sql'), 'SQL', 20_000)
            if not re.match(r'^\s*(SELECT|WITH)\b', query, re.I):
                raise ValueError('Only SELECT and read-only WITH queries are accepted')
            limit = _integer(data.get('limit', 100), 'limit', 1, 500)
            cursor = connection.execute(query)
            columns = [column[0] for column in cursor.description or []]
            if len(columns) > 100:
                raise ValueError('Query returns too many columns')
            rows = cursor.fetchmany(limit + 1)
            def cell(value):
                if isinstance(value, bytes):
                    return f'[binary: {len(value)} bytes]'
                return value[:4000] if isinstance(value, str) else value
            return {'columns': columns, 'rows': [[cell(v) for v in row] for row in rows[:limit]],
                    'truncated': len(rows) > limit, 'read_only': True}
        except sqlite3.Error as exc:
            raise ValueError('Read-only SQL failed: ' + str(exc)) from None
        finally:
            connection.close()

    def handle(self, method, path, data, state=None):
        if path != '/api/documents' and not path.startswith('/api/documents/'):
            return None
        with self.lock:
            if method == 'GET' and path == '/api/documents':
                return self.overview()
            if method != 'POST':
                raise ValueError('This document action requires POST')
            if not isinstance(data, dict):
                raise ValueError('Document action must be a JSON object')
            routes = {'/api/documents/artifact': self.artifact, '/api/documents/upload': self.upload,
                      '/api/documents/library': self.library, '/api/documents/csv': self.csv,
                      '/api/documents/sql': self.sql}
            if path not in routes:
                raise ValueError('Unknown document endpoint')
            return routes[path](data)
