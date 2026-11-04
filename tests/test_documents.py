import base64
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from herness.documents import Documents, extract_document, static_html


class DocumentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = Documents(self.root)

    def call(self, endpoint, **data):
        return self.docs.handle('POST', '/api/documents/' + endpoint, data)

    def test_artifact_versions_conflict_diff_and_delete(self):
        first = self.call('artifact', action='create', title='Plan', content='First version\n')
        second = self.call('artifact', action='update', id=first['id'], revision=1, content='Second version\n')
        self.assertEqual(second['revision'], 2)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.call('artifact', action='update', id=first['id'], revision=1, content='Overwrite')
        diff = self.call('artifact', action='diff', id=first['id'])['diff']
        self.assertIn('-First version', diff)
        self.assertIn('+Second version', diff)
        restored = Documents(self.root).handle('GET', '/api/documents', {})
        self.assertEqual(restored['artifacts'][0]['versions'], 2)
        self.call('artifact', action='delete', id=first['id'], revision=2)
        self.assertEqual(self.docs.overview()['artifacts'], [])

    def test_static_preview_removes_active_content(self):
        preview = static_html('<h1 onclick="bad()">Title</h1><script>alert(1)</script><img src=x onerror="bad()"><a href="javascript:bad()">link</a><iframe src="https://evil.example"></iframe>', 'Safe', 'html')
        self.assertIn('<h1>Title</h1>', preview)
        self.assertNotIn('<script', preview)
        self.assertNotIn('onclick', preview)
        self.assertNotIn('onerror', preview)
        self.assertNotIn('javascript:', preview)
        self.assertNotIn('<iframe', preview)
        self.assertIn('Content-Security-Policy', preview)
        escaped = static_html('<script>attack</script>', 'Code', 'text')
        self.assertIn('&lt;script&gt;', escaped)
        malformed = static_html('<a href="https://[broken">Readable label</a>', 'Safe', 'html')
        self.assertIn('<a>Readable label</a>', malformed)

    def test_docx_zip_and_pdf_exports_are_real_documents(self):
        item = self.call('artifact', action='create', title='Project brief', content='Useful project notes\nSecond line')
        export = self.call('artifact', action='export', id=item['id'], export_format='docx')
        raw = base64.b64decode(export['data'])
        parsed = extract_document(export['filename'], raw)
        self.assertIn('Useful project notes', parsed['text'])
        bundle = self.call('artifact', action='export', id=item['id'], export_format='zip')
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle['data']))) as archive:
            self.assertEqual(set(archive.namelist()), {'artifact.json', 'content.txt', 'preview.html'})
        if self.docs.overview()['capabilities']['pdf_export']:
            pdf = self.call('artifact', action='export', id=item['id'], export_format='pdf')
            parsed = extract_document('report.pdf', base64.b64decode(pdf['data']))
            self.assertIn('Useful project notes', parsed['text'])
            self.assertEqual(parsed['units'][0]['label'], 'Page 1')

    def test_library_deduplicates_searches_and_deletes_independently(self):
        raw = b'Project alpha is launching next week.'
        upload = self.call('upload', name='../notes.txt', data=base64.b64encode(raw).decode())
        self.assertEqual(upload['name'], 'notes.txt')
        duplicate = self.call('upload', name='copy.txt', data=base64.b64encode(raw).decode())
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['id'], upload['id'])
        hits = self.call('library', action='search', query='alpha')['results']
        self.assertEqual(hits[0]['citation'], 'Document')
        self.assertIn('alpha', hits[0]['excerpt'])
        downloaded = self.call('library', action='download', id=upload['id'])
        self.assertEqual(base64.b64decode(downloaded['data']), raw)
        sessions = self.root / '.herness/sessions'
        sessions.mkdir()
        (sessions / 'default.json').write_text('unrelated history')
        self.call('library', action='delete', id=upload['id'])
        self.assertEqual(self.docs.overview()['library'], [])
        self.assertEqual((sessions / 'default.json').read_text(), 'unrelated history')

    def test_office_spreadsheet_and_slides_extract_citations(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('xl/sharedStrings.xml', '<sst><si><t>Revenue</t></si></sst>')
            archive.writestr('xl/worksheets/sheet1.xml', '<worksheet><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row></sheetData></worksheet>')
        result = extract_document('book.xlsx', output.getvalue())
        self.assertIn('A1: Revenue', result['text'])
        self.assertIn('B1: 42', result['text'])
        self.assertEqual(result['units'][0]['label'], 'Sheet 1')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('ppt/slides/slide1.xml', '<slide><p><t>Launch plan</t></p></slide>')
            archive.writestr('ppt/slides/slide2.xml', '<slide><p><t>Next steps</t></p></slide>')
        result = extract_document('slides.pptx', output.getvalue())
        self.assertEqual(result['units'][1]['label'], 'Slide 2')
        self.assertIn('Next steps', result['text'])

    def test_reject_invalid_library_files_and_traversal_state(self):
        for name, data in [('.env', b'secret'), ('bad.bin', b'\x00abc')]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.call('upload', name=name, data=base64.b64encode(data).decode())
        with self.assertRaises(ValueError):
            self.call('upload', name='file.txt', data='invalid***')
        (self.root / '.herness/documents.json').symlink_to(self.root / 'outside.json')
        with self.assertRaises(ValueError):
            self.docs.overview()

    def test_csv_profile_filter_compare_and_export(self):
        content = 'name,value\nalpha,10\nbeta,20\nbeta,20\ngamma,\n'
        profile = self.call('csv', content=content, action='profile')
        self.assertEqual(profile['row_count'], 4)
        self.assertEqual(profile['duplicate_rows'], 1)
        self.assertEqual(profile['profile']['value']['missing'], 1)
        self.assertAlmostEqual(profile['profile']['value']['mean'], 50 / 3)
        filtered = self.call('csv', content=content, action='filter', column='value', operator='gt', value='15')
        self.assertEqual(filtered['row_count'], 2)
        comparison = self.call('csv', content=content, other='name,value\nalpha,11\nbeta,20\n', action='compare')
        self.assertEqual(comparison['added_count'], 1)
        self.assertEqual(comparison['removed_count'], 3)
        export = self.call('csv', content=filtered['content'], action='export')
        self.assertIn(b'beta,20', base64.b64decode(export['data']))

    def test_large_numeric_profile_remains_valid_json(self):
        profile = self.call('csv', content='value\n1e308\n1e308\n', action='profile')
        self.assertEqual(profile['profile']['value']['median'], 1e308)
        self.assertIsNone(profile['profile']['value']['sum'])
        json.dumps(profile, allow_nan=False)

    def test_chart_escapes_labels_and_creates_artifact(self):
        result = self.call('csv', content='label,value\n<script>,5\nsafe,10\n', action='chart', x='label', y='value')
        self.assertEqual(result['artifact']['format'], 'svg')
        self.assertNotIn('<script>', result['artifact']['content'])
        self.assertIn('&lt;script&gt;', result['artifact']['content'])
        self.assertEqual(result['rows_used'], 2)
        self.assertEqual(len(self.docs.overview()['artifacts']), 1)

    def test_csv_rejects_ambiguous_headers_and_non_numeric_chart(self):
        for content in ['name,name\na,b\n', 'name,value\na,b,c\n']:
            with self.assertRaises(ValueError):
                self.call('csv', content=content)
        with self.assertRaisesRegex(ValueError, 'numeric'):
            self.call('csv', content='name,value\na,hello\n', action='chart', x='name', y='value')

    def test_sql_schema_queries_and_write_denial(self):
        path = self.root / 'example.db'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE projects(name TEXT, cost REAL)')
            connection.executemany('INSERT INTO projects VALUES (?, ?)', [('a', 10), ('b', 20), ('c', 30)])
        schema = self.call('sql', action='schema', path='example.db')
        self.assertEqual(schema['tables'][0]['name'], 'projects')
        result = self.call('sql', path='example.db', sql='SELECT sum(cost) AS total FROM projects')
        self.assertEqual(result['rows'], [[60.0]])
        truncated = self.call('sql', path='example.db', sql='SELECT * FROM projects', limit=1)
        self.assertTrue(truncated['truncated'])
        for query in ['DELETE FROM projects', "SELECT load_extension('/tmp/bad')", 'WITH x AS (SELECT 1) DELETE FROM projects']:
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.call('sql', path='example.db', sql=query)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM projects').fetchone()[0], 3)
        with self.assertRaises(ValueError):
            self.call('sql', path='../example.db', sql='SELECT 1')

    def test_unknown_routes_do_not_swallow_other_modules(self):
        self.assertIsNone(self.docs.handle('GET', '/api/context', {}))
        with self.assertRaises(ValueError):
            self.docs.handle('POST', '/api/documents/missing', {})


if __name__ == '__main__':
    unittest.main()
