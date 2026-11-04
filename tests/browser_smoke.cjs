/* Optional integration test: node tests/browser_smoke.cjs
 * Requires Playwright (or PLAYWRIGHT_MODULE=/absolute/path/to/playwright) and
 * Chromium (CHROMIUM_PATH overrides /usr/bin/chromium). No external API calls.
 * Uses a temporary project and an ephemeral local demo server; removes both.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'herness-browser-'));
  const first = path.join(temporary, 'project'), second = path.join(temporary, 'second');
  fs.mkdirSync(first); fs.mkdirSync(second);
  fs.writeFileSync(path.join(first, 'README.md'), '# Browser fixture\nHerness integration evidence.');
  const server = spawn(process.env.PYTHON || 'python', ['-u', '-c',
    'import sys; from pathlib import Path; from herness.web import make_server; from herness.cli import DemoClient; s=make_server(Path(sys.argv[1]),DemoClient(),0,demo=True,force_demo=True); print(s.server_port,flush=True); s.serve_forever()', first],
    { cwd: path.resolve(__dirname, '..'), stdio: ['ignore', 'pipe', 'pipe'] });
  let browser, page, serverError = '';
  server.stderr.on('data', chunk => { serverError += chunk.toString(); });
  try {
    const port = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Server did not start: ' + serverError)), 15000);
      server.stdout.once('data', chunk => { clearTimeout(timeout); resolve(Number(chunk.toString().trim())); });
      server.once('exit', code => { clearTimeout(timeout); reject(new Error('Server exited ' + code + ': ' + serverError)); });
    });
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:' + port);
    await page.waitForFunction(() => /demo/i.test(document.getElementById('mode')?.textContent || ''));

    // Every secondary destination is reached through its visible navigation menu.
    async function profileAction(id) {
      await page.locator('#profile-menu-button').click();
      await page.locator('#profile-menu').waitFor({ state: 'visible' });
      await page.locator('#' + id).click();
    }
    async function headerAction(id) {
      await page.locator('#header-menu-button').click();
      await page.locator('#header-menu').waitFor({ state: 'visible' });
      await page.locator('#' + id).click();
    }
    async function messageAction(message, name) {
      await message.getByRole('button', { name: 'More message actions', exact: true }).click();
      await page.getByRole('menuitem', { name, exact: true }).click();
    }

    // Renderer uses text nodes, even for adversarial model text and link URLs.
    const rendered = await page.evaluate(() => {
      const node = markdown('## Heading\n\n| Key | Value |\n| --- | --- |\n| One | 1 |\n\n```python\ndef hello():\n    return "world"\n```\n\n<script>window.hacked=true</script>\n\n[unsafe](javascript:alert(1))');
      document.body.append(node);
      const result = { tables: node.querySelectorAll('table').length, keywords: node.querySelectorAll('.token-keyword').length, scripts: node.querySelectorAll('script').length, unsafe: node.querySelectorAll('a[href^="javascript:"]').length };
      node.remove(); return result;
    });
    assert.deepEqual(rendered, { tables: 1, keywords: 2, scripts: 0, unsafe: 0 });

    await page.locator('#prompt').fill('A draft that survives reload');
    await page.reload();
    await page.waitForFunction(() => document.getElementById('prompt').value === 'A draft that survives reload');
    await page.locator('#composer-menu-button').click();
    const attachmentChooser = page.waitForEvent('filechooser');
    await page.locator('#attach').click();
    await (await attachmentChooser).setFiles({ name: 'note.txt', mimeType: 'text/plain', buffer: Buffer.from('Attachment preview evidence') });
    await page.locator('.attachment-name').click();
    assert.match(await page.locator('#attachment-preview').textContent(), /Attachment preview evidence/);
    await page.locator('[data-close="attachment-dialog"]').click();
    await page.locator('[aria-label="Remove note.txt"]').click();
    await page.locator('#prompt').fill('Inspect this project');
    await page.locator('#send').click();
    await page.waitForFunction(() => document.getElementById('messages').textContent.includes('Offline demo complete'));
    await page.waitForFunction(() => document.getElementById('stop').classList.contains('hidden'));
    await messageAction(page.locator('#messages .assistant').last(), 'Save message');
    await page.locator('#messages .assistant.bookmarked').waitFor();
    await headerAction('find-chat');
    await page.locator('#bookmarks-only').click();
    assert.equal(await page.locator('#messages .message:visible').count(), 1);
    await page.locator('#close-chat-search').click();

    await headerAction('manage-chat');
    await page.locator('#chat-title').fill('Browser integration');
    await page.locator('#chat-folder').fill('QA');
    await page.locator('#chat-tags').fill('tested, browser');
    await page.locator('#chat-pinned').check();
    await page.locator('#conversation-form').getByRole('button', { name: 'Save conversation' }).click();
    await page.waitForFunction(() => document.getElementById('conversation-status').textContent === 'Conversation saved.');
    const downloaded = page.waitForEvent('download');
    await page.locator('#export-markdown').click();
    assert.match((await downloaded).suggestedFilename(), /\.md$/);
    await page.locator('[data-close="conversation-dialog"]').click();
    await page.waitForFunction(() => document.getElementById('session-label').textContent === 'Browser integration');

    await page.locator('#prompt').fill('Keep with this conversation');
    await page.locator('#new').click();
    await page.waitForFunction(() => document.getElementById('prompt').value === '');
    await page.locator('.history-item').filter({ hasText: 'Browser integration' }).click();
    await page.waitForFunction(() => document.getElementById('prompt').value === 'Keep with this conversation');
    assert.equal(await page.locator('#chat-tabs .tab').count(), 2);
    await page.locator('#composer-menu-button').click();
    await Promise.all([page.waitForResponse(response => response.url().endsWith('/api/run/options') && response.request().method() === 'POST'), page.locator('#run-mode').selectOption('plan')]);
    if (await page.locator('#composer-menu').isVisible()) await page.keyboard.press('Escape');
    await profileAction('project-open');
    await page.locator('#project-instructions').fill('Prefer tested changes.');
    await page.locator('#project-form').getByRole('button', { name: 'Save project', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('project-status').textContent.includes('saved'));
    await page.locator('[data-close="project-dialog"]').click();
    await page.keyboard.press('Control+Shift+P');
    await page.locator('#palette-query').fill('context');
    await page.locator('#palette-query').press('Enter');
    await page.locator('#context-dialog[open]').waitFor();
    assert.match(await page.locator('#context-data').textContent(), /Prefer tested/);
    await page.locator('[data-close="context-dialog"]').click();
    await profileAction('files-open');
    await page.getByRole('button', { name: 'README.md', exact: false }).click();
    await page.locator('#file-preview:not(.hidden)').waitFor();
    assert.match(await page.locator('#file-preview').textContent(), /integration evidence/);
    await page.locator('#file-quote').click();
    assert.match(await page.locator('#prompt').inputValue(), /README.md/);

    // Catalog operations use real backends; nothing runs a paid model or timer.
    await page.locator('#features-open').click();
    await page.locator('#features-dialog[open]').waitFor();
    assert((await page.locator('#workspace-categories [data-feature-module]').count()) >= 13);
    async function form(title) {
      await page.locator('#feature-query').fill(title);
      await page.locator('button[data-feature-action=' + JSON.stringify(title) + ']').click();
      const found = page.locator('.feature-action').filter({ has: page.getByRole('heading', { name: title, exact: true }) });
      assert.equal(await page.locator('.feature-action').count(), 1, 'Workspace presents one action form at a time');
      await found.getByRole('heading', { name: title, exact: true }).waitFor();
      return found;
    }
    async function submit(found) {
      await found.locator('button[type="submit"]').click();
      await found.locator('.dialog-status').filter({ hasText: /Complete\.|./ }).waitFor();
      await page.waitForFunction(node => node.querySelector('.dialog-status').textContent !== 'Working…', await found.elementHandle());
      assert.equal(await found.locator('.dialog-status').textContent(), 'Complete.');
      const raw = found.locator('.feature-result details').last();
      if (!(await raw.locator('pre').isVisible())) await raw.locator('summary').click();
      return JSON.parse(await raw.locator('pre').textContent());
    }
    const upload = await form('Upload a document');
    await upload.locator('input[type="file"]').setInputFiles({ name: 'evidence.txt', mimeType: 'text/plain', buffer: Buffer.from('Searchable document evidence') });
    await submit(upload);
    const artifact = await form('Create an artifact');
    await artifact.getByLabel('Title', { exact: true }).fill('Browser artifact');
    await artifact.getByLabel('Format', { exact: true }).selectOption('html');
    await artifact.getByLabel('Content', { exact: true }).fill('<h1>Preview evidence</h1><script>parent.hacked=true</script>');
    const created = await submit(artifact);
    const artifactId = created.id || created.artifact?.id;
    assert(artifactId, 'Artifact creation returns an ID');
    const preview = await form('Preview an artifact');
    await preview.getByLabel('Artifact ID', { exact: true }).fill(artifactId);
    await submit(preview);
    assert.equal(await preview.locator('iframe').getAttribute('sandbox'), '');
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => window.hacked), undefined);
    const exportForm = await form('Export an artifact');
    await exportForm.getByLabel('Artifact ID', { exact: true }).fill(artifactId);
    await exportForm.getByLabel('Download format', { exact: true }).selectOption('zip');
    await submit(exportForm);
    assert.equal(await exportForm.locator('.feature-result button').count(), 1);
    const csv = await form('Profile a CSV dataset');
    await csv.getByLabel('CSV content', { exact: true }).fill('name,value\nfirst,1\nsecond,2');
    await submit(csv);
    assert((await csv.locator('.feature-result table').count()) > 0);
    const task = await form('Create a task plan');
    await task.getByLabel('Title', { exact: true }).fill('Browser task');
    await submit(task);
    const skill = await form('Create a skill');
    await skill.getByLabel('Title', { exact: true }).fill('Browser skill');
    await submit(skill);
    const profile = await form('Save a model profile');
    await profile.getByLabel('Profile name', { exact: true }).fill('Browser demo profile');
    await submit(profile);
    const schedule = await form('Preview upcoming schedule times');
    await submit(schedule);
    const project = await form('Update project configuration');
    await project.getByLabel('Project fields (JSON)', { exact: true }).fill('{"goals":"JSON payload works"}');
    await submit(project);
    await page.locator('[data-close="features-dialog"]').click();

    await messageAction(page.locator('#messages .user').first(), 'Edit');
    await page.locator('#edit-message').fill('Inspect again');
    await page.locator('#edit-submit').click();
    await page.waitForFunction(() => document.querySelector('#messages .user .body')?.textContent === 'Inspect again');
    await page.waitForFunction(() => document.getElementById('messages').textContent.includes('Offline demo complete') && document.getElementById('stop').classList.contains('hidden'));
    await profileAction('model-settings');
    await page.locator('#provider').selectOption('anthropic');
    assert.equal(await page.locator('#api-protocol').inputValue(), 'anthropic');
    await page.locator('#provider').selectOption('gemini');
    assert.equal(await page.locator('#api-protocol').inputValue(), 'gemini');
    await page.locator('#close-settings').click();
    await profileAction('preferences-open');
    await page.locator('#theme-choice').selectOption('light');
    assert.equal(await page.locator('body.light').count(), 1);
    await page.locator('#theme-choice').selectOption('dark');
    await page.locator('[data-close="preferences-dialog"]').click();

    await profileAction('project-open');
    await page.locator('#project-path').fill(second);
    await page.locator('#project-add').click();
    await page.waitForFunction(() => document.getElementById('project-status').textContent.includes('Project added'));
    await page.locator('#project-switch').click();
    await page.waitForFunction(expected => document.getElementById('folder').title === expected, second);
    assert.equal(await page.locator('#prompt').inputValue(), '');
    assert.equal(await page.locator('#run-mode').inputValue(), 'act');

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.locator('#menu').click();
    assert.equal(await page.locator('body.sidebar-open').count(), 1);
    await page.locator('#collapse').click();
    assert.equal(await page.locator('body.sidebar-open').count(), 0);
    assert.deepEqual(errors, []);
    assert.equal(await page.locator('#error').textContent(), '');
    console.log('PASS: real-server chat, safe Markdown, drafts/tabs, attachment previews, bookmarks/search, organization/export, context/files, projects/modes, provider controls, catalog document/artifact/CSV/task/skill/profile/schedule forms, preview sandbox, appearance and mobile navigation.');
  } catch (error) {
    if (page && process.env.HERNESS_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.HERNESS_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.HERNESS_SCREENSHOT_DIR, 'smoke-failure.png'), fullPage: true }).catch(() => {});
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    await new Promise(resolve => { if (server.exitCode !== null) return resolve(); server.once('exit', resolve); setTimeout(resolve, 3000); });
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
