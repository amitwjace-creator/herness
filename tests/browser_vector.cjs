/* Optional Knowledge UI integration test: node tests/browser_vector.cjs
 * Requires Playwright and Chromium; PLAYWRIGHT_MODULE, CHROMIUM_PATH and PYTHON
 * override their paths. All app state is temporary. Embeddings come from a
 * deterministic local HTTP fixture, so no external provider is contacted.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

function embedding(text) {
  const tokens = String(text).toLowerCase();
  return [
    (tokens.match(/automobile|car|vehicle/g) || []).length,
    (tokens.match(/maintenance|repair|mechanic/g) || []).length,
    (tokens.match(/garden|plant|flower/g) || []).length,
    (tokens.match(/remember|preference|memory/g) || []).length,
    0.2,
  ];
}
async function eventually(check, description, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 30));
  }
  throw new Error('Timed out: ' + description);
}

async function main() {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'herness-vector-browser-'));
  const workspace = path.join(temporary, 'project');
  fs.mkdirSync(workspace);
  const requests = [];
  let embeddingDelay = 0, failEmbeddings = false;
  const embeddingServer = http.createServer((request, response) => {
    let body = '';
    request.on('data', chunk => { body += chunk.toString(); });
    request.on('end', () => {
      let input;
      try { input = JSON.parse(body); } catch { response.writeHead(400); response.end('{}'); return; }
      requests.push({ path: request.url, payload: input });
      setTimeout(() => {
        response.setHeader('Content-Type', 'application/json');
        if (failEmbeddings) { response.writeHead(503); response.end(JSON.stringify({ error: 'Embedding fixture unavailable' })); return; }
        const vectors = (Array.isArray(input.input) ? input.input : [input.input]).map(embedding);
        if (request.url === '/v1/embeddings') response.end(JSON.stringify({ data: vectors.map((vector, index) => ({ index, embedding: vector })) }));
        else if (request.url === '/api/embed') response.end(JSON.stringify({ embeddings: vectors }));
        else { response.writeHead(404); response.end('{}'); }
      }, embeddingDelay);
    });
  });
  await new Promise(resolve => embeddingServer.listen(0, '127.0.0.1', resolve));
  const embeddingBase = 'http://127.0.0.1:' + embeddingServer.address().port;
  const server = spawn(process.env.PYTHON || 'python', ['-u', '-c',
    'import sys; from pathlib import Path; from herness.web import make_server; from herness.cli import DemoClient; s=make_server(Path(sys.argv[1]),DemoClient(),0,demo=True,force_demo=True); print(s.server_port,flush=True); s.serve_forever()', workspace],
    { cwd: path.resolve(__dirname, '..'), stdio: ['ignore', 'pipe', 'pipe'] });
  let browser, page, serverError = '';
  server.stderr.on('data', chunk => { serverError += chunk.toString(); });
  try {
    const port = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('App did not start: ' + serverError)), 15000);
      server.stdout.once('data', chunk => { clearTimeout(timeout); resolve(Number(chunk.toString().trim())); });
      server.once('error', error => { clearTimeout(timeout); reject(error); });
      server.once('exit', code => { clearTimeout(timeout); reject(new Error('App exited ' + code + ': ' + serverError)); });
    });
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:' + port);
    await page.waitForFunction(() => /demo/i.test(document.getElementById('mode')?.textContent || ''));
    const call = (url, data) => page.evaluate(({ url, data }) => api(url, data), { url, data });
    const initialChatSettings = await call('/api/settings');
    const memory = await call('/api/memory');
    await call('/api/memory', { revision: memory.revision, content: 'Project note: prefer concise, tested answers.' });
    const structured = await call('/api/privacy/memory');
    const savedMemory = await call('/api/privacy/memory', { action: 'save', revision: structured.revision, label: 'Response preferences', content: 'Remember: provide concise answers with evidence.' });
    const memoryId = savedMemory.entries.find(entry => entry.label === 'Response preferences').id;

    await page.locator('#knowledge-open').click();
    await page.locator('#knowledge-dialog[open]').waitFor();
    assert.equal(requests.length, 0, 'Opening Knowledge makes no embedding request');
    assert.equal((await call('/api/vectors')).configured, false);
    assert.equal(await page.locator('#knowledge-search-submit').isDisabled(), true);
    // Knowledge remains a first-class sidebar destination, with keyboard tabs.
    await page.locator('#knowledge-tab-search').click();
    await page.locator('#knowledge-tab-search').press('ArrowRight');
    assert.equal(await page.locator('#knowledge-tab-sources').getAttribute('aria-selected'), 'true');
    await page.locator('#knowledge-tab-sources').press('ArrowLeft');
    assert.equal(await page.locator('#knowledge-tab-search').getAttribute('aria-selected'), 'true');
    await page.locator('#knowledge-tab-sources').click();
    async function upload(name, content) {
      await page.locator('#knowledge-upload').setInputFiles({ name, mimeType: 'text/plain', buffer: Buffer.from(content) });
      await eventually(async () => (await call('/api/vectors')).available_sources.some(source => source.title === name), 'uploaded source ' + name);
      await page.locator('#knowledge-refresh').click();
    }
    await upload('Automobile evidence.txt', 'An automobile maintenance guide: a mechanic repairs your vehicle. <script>window.vectorXss=true</script>');
    await upload('Garden evidence.txt', 'Garden flowers and healthy plants need water and soil.');
    assert.equal(requests.length, 0, 'Uploading sources does not start embeddings');
    assert.equal(await page.locator('#knowledge-index').isDisabled(), true);

    await page.locator('#knowledge-tab-settings').click();
    await page.locator('#embedding-provider').selectOption('ollama');
    assert.match(await page.locator('#embedding-url').inputValue(), /11434/);
    assert.equal(await page.locator('#embedding-model').inputValue(), 'nomic-embed-text');
    await page.locator('#embedding-provider').selectOption('openai');
    assert.equal(await page.locator('#embedding-model').inputValue(), 'text-embedding-3-small');
    await page.locator('#embedding-url').fill(embeddingBase + '/v1');
    await page.locator('#embedding-key').fill('test-embedding-secret-never-store');
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/vectors/settings') && response.request().method() === 'POST'),
      page.locator('#embedding-save').click(),
    ]);
    await eventually(async () => (await call('/api/vectors')).configured, 'embedding settings saved');
    assert.equal(requests.length, 0, 'Saving settings does not probe the provider');
    await page.waitForFunction(() => document.getElementById('embedding-key').value === '');
    assert.equal(await page.evaluate(() => Object.values(localStorage).some(value => value.includes('test-embedding-secret-never-store'))), false);
    assert.deepEqual(await call('/api/settings'), initialChatSettings, 'Embedding settings leave chat settings intact');
    await page.locator('#embedding-test').click();
    await eventually(() => requests.length > 0, 'explicit provider probe');
    assert.equal(requests[0].path, '/v1/embeddings');
    await page.waitForFunction(() => !document.getElementById('embedding-test').disabled);
    assert.equal(await page.locator('#embedding-status.failure').count(), 0);

    await page.locator('#knowledge-tab-sources').click();
    await page.locator('#knowledge-select-all').click();
    await page.locator('#knowledge-index').click();
    await eventually(async () => (await call('/api/vectors')).indexing.status === 'completed', 'source indexing completes');
    await page.locator('#knowledge-refresh').click();
    const indexed = await call('/api/vectors');
    assert(indexed.stats.sources >= 4, 'Documents and project memory are indexed');
    assert(indexed.stats.chunks >= 4);
    assert.equal(indexed.stats.dimensions, 5);
    assert(indexed.available_sources.filter(source => source.kind === 'document').every(source => source.indexed && !source.stale));
    assert.match(await page.locator('#knowledge-stats').textContent(), /4/);

    await page.locator('#knowledge-tab-search').click();
    await page.locator('#knowledge-kind').selectOption('document');
    await page.locator('#knowledge-query').fill('car repair');
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/vectors/search') && response.request().method() === 'POST'),
      page.locator('#knowledge-search-submit').click(),
    ]);
    const result = page.locator('#knowledge-results article').first();
    await result.waitFor();
    assert.match(await result.textContent(), /Automobile evidence\.txt/);
    assert.match(await result.textContent(), /automobile maintenance/i);
    assert.equal(await page.locator('#knowledge-results script, #knowledge-results img').count(), 0);
    assert.equal(await page.evaluate(() => window.vectorXss), undefined);
    if (process.env.HERNESS_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.HERNESS_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.HERNESS_SCREENSHOT_DIR, 'knowledge-desktop.png'), fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: path.join(process.env.HERNESS_SCREENSHOT_DIR, 'knowledge-search-mobile.png'), fullPage: true });
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
    await result.getByRole('button', { name: 'Use in chat', exact: true }).click();
    await page.locator('#knowledge-dialog').waitFor({ state: 'hidden' });
    assert.match(await page.locator('#prompt').inputValue(), /Automobile evidence\.txt/);
    assert.match(await page.locator('#prompt').inputValue(), /automobile maintenance/i);

    // Refresh removes a cached memory excerpt as soon as its source changes,
    // before another search can return or quote the obsolete text.
    await page.locator('#knowledge-open').click();
    await page.locator('#knowledge-tab-search').click();
    await page.locator('#knowledge-kind').selectOption('memory');
    await page.locator('#knowledge-query').fill('remember preference');
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/vectors/search') && response.request().method() === 'POST'),
      page.locator('#knowledge-search-submit').click(),
    ]);
    await eventually(async () => (await page.locator('#knowledge-results article').count()) === 1, 'memory search result is displayed');
    assert.match(await page.locator('#knowledge-results').textContent(), /Response preferences/);
    assert.match(await page.locator('#knowledge-results').textContent(), /provide concise answers with evidence/);
    const changedMemory = await call('/api/privacy/memory');
    await call('/api/privacy/memory', { action: 'save', revision: changedMemory.revision, id: memoryId, content: 'Remember: my preference is concise answers and a test for each important change.' });
    await page.locator('#knowledge-tab-sources').click();
    await page.locator('#knowledge-refresh').click();
    await eventually(async () => (await page.locator('#knowledge-results article').count()) === 0, 'cached obsolete excerpt is removed on refresh');
    assert.doesNotMatch(await page.locator('#knowledge-results').textContent(), /provide concise answers with evidence/);
    const changed = (await call('/api/vectors')).available_sources.find(source => source.kind === 'memory');
    assert.equal(changed.indexed, false);
    const memoryMatches = await call('/api/vectors/search', { query: 'remember preference', kinds: ['memory'], limit: 5 });
    assert.equal(memoryMatches.results.length, 0, 'Outdated memory excerpts are excluded from retrieval');

    // Index cancellation and provider failures are real background-job states.
    await upload('Slow source.txt', ('Automobile repair evidence. '.repeat(150)));
    const slow = (await call('/api/vectors')).available_sources.find(source => source.title === 'Slow source.txt');
    const checks = page.locator('#knowledge-sources input[type="checkbox"][data-source-id]');
    for (const check of await checks.all()) if (await check.isChecked()) await check.uncheck();
    await page.locator('#knowledge-sources input[type="checkbox"][data-source-id="' + slow.id + '"]').check();
    embeddingDelay = 2000;
    const beforeCancel = requests.length;
    await page.locator('#knowledge-index').click();
    await eventually(() => requests.length > beforeCancel, 'slow embedding request starts');
    await page.locator('#knowledge-index-stop').click();
    await eventually(async () => (await call('/api/vectors')).indexing.status === 'cancelled', 'index cancellation');
    embeddingDelay = 0;
    failEmbeddings = true;
    await page.locator('#knowledge-refresh').click();
    await page.locator('#knowledge-sources input[type="checkbox"][data-source-id="' + slow.id + '"]').check();
    await page.locator('#knowledge-index').click();
    await eventually(async () => (await call('/api/vectors')).indexing.status === 'failed', 'provider failure is reported');
    await page.locator('#knowledge-refresh').click();
    assert.match(await page.locator('#knowledge-index-status').textContent(), /fail|unavailable|503/i);
    failEmbeddings = false;

    await page.getByRole('button', { name: 'Remove Automobile evidence.txt from index', exact: true }).click();
    await page.locator('#knowledge-remove-confirm').click();
    await eventually(async () => !(await call('/api/vectors')).sources.some(source => source.title === 'Automobile evidence.txt'), 'source vectors removed');
    assert((await call('/api/documents')).library.some(source => source.name === 'Automobile evidence.txt'), 'Removing vectors preserves the source document');
    // Removing sources leaves the embedding-space metadata bound even when no
    // passages remain. Clear must remain usable to permit a different model.
    for (const source of (await call('/api/vectors')).sources) {
      await page.getByRole('button', { name: 'Remove ' + source.title + ' from index', exact: true }).click();
      await page.locator('#knowledge-remove-confirm').click();
      await eventually(async () => !(await call('/api/vectors')).sources.some(item => item.source_id === source.source_id), 'remaining source removed: ' + source.title);
    }
    const emptyIndex = await call('/api/vectors');
    assert.equal(emptyIndex.stats.sources, 0);
    assert.equal(emptyIndex.stats.chunks, 0);
    assert.equal(emptyIndex.stats.dimensions, 5);
    assert(emptyIndex.stats.embedding_key, 'Source deletion retains the embedding model binding');
    await eventually(() => page.locator('#knowledge-clear').isEnabled(), 'Clear remains available for an empty bound index');
    await page.locator('#knowledge-clear').click();
    await page.locator('#knowledge-clear-confirm').click();
    await eventually(async () => {
      const stats = (await call('/api/vectors')).stats;
      return stats.chunks === 0 && stats.dimensions === null && stats.embedding_key === null;
    }, 'clear resets the embedding dimensions and model binding');
    assert.equal((await call('/api/documents')).library.length, 3);

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.locator('#knowledge-dialog').evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
    if (process.env.HERNESS_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.HERNESS_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.HERNESS_SCREENSHOT_DIR, 'knowledge-mobile.png'), fullPage: true });
    }
    await page.locator('[data-close="knowledge-dialog"]').click();
    await page.locator('#menu').click();
    await page.locator('#knowledge-open').click();
    await page.locator('#knowledge-dialog[open]').waitFor();
    assert.equal(await page.locator('body.sidebar-open').count(), 0, 'Choosing Knowledge closes the mobile drawer');
    await page.keyboard.press('Escape');
    await page.locator('#knowledge-dialog').waitFor({ state: 'hidden' });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    console.log('PASS: Knowledge configuration without automatic calls, separate chat settings, uploads/indexing, semantic search/citations, safe excerpts, cached stale-source pruning, cancellation/errors, empty-index model reset, key handling and mobile layout.');
  } catch (error) {
    if (page && process.env.HERNESS_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.HERNESS_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.HERNESS_SCREENSHOT_DIR, 'knowledge-failure.png'), fullPage: true }).catch(() => {});
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    await new Promise(resolve => { if (server.exitCode !== null || server.signalCode !== null) return resolve(); const timeout = setTimeout(resolve, 3000); server.once('exit', () => { clearTimeout(timeout); resolve(); }); });
    await new Promise(resolve => embeddingServer.close(resolve));
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
