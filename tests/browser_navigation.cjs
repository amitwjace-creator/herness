/* Real-browser navigation regression for the compact Herness interface.
 * Run: node tests/browser_navigation.cjs
 * Requires Playwright + Chromium. Uses a temporary offline demo project.
 * Optional HERNESS_SCREENSHOT_DIR stores desktop/mobile review screenshots.
 * Menus, categories and action forms are opened only through visible clicks or
 * keyboard input; the test never force-clicks or programmatically opens them.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'herness-navigation-'));
  fs.writeFileSync(path.join(temporary, 'README.md'), '# Navigation fixture\nVisible workspace forms can read this file.\n');
  const server = spawn(process.env.PYTHON || 'python', ['-u', '-c',
    'import sys; from pathlib import Path; from herness.web import make_server; from herness.cli import DemoClient; s=make_server(Path(sys.argv[1]),DemoClient(),0,demo=True,force_demo=True); print(s.server_port,flush=True); s.serve_forever()', temporary],
    { cwd: path.resolve(__dirname, '..'), stdio: ['ignore', 'pipe', 'pipe'] });
  let browser, page, serverError = '';
  server.stderr.on('data', chunk => { serverError += chunk.toString(); });
  const screenshotDirectory = process.env.HERNESS_SCREENSHOT_DIR;
  try {
    const port = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('App did not start: ' + serverError)), 15000);
      server.stdout.once('data', chunk => { clearTimeout(timeout); resolve(Number(chunk.toString().trim())); });
      server.once('error', error => { clearTimeout(timeout); reject(error); });
      server.once('exit', code => { clearTimeout(timeout); reject(new Error('App exited ' + code + ': ' + serverError)); });
    });
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    // Standard browser Speech API fixtures exercise real UI event handlers
    // without microphone hardware, audio autoplay, or external recognition.
    await page.addInitScript(() => {
      const fixture = { spoken: [], cancellations: 0, starts: 0, stops: 0, recognition: null };
      class Recognition {
        constructor() { fixture.recognition = this; }
        start() { fixture.starts += 1; this.onstart?.(); }
        stop() { fixture.stops += 1; this.onend?.(); }
        abort() { this.stop(); }
      }
      Object.defineProperty(window, 'SpeechRecognition', { configurable: true, value: Recognition });
      Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
        speaking: false, pending: false,
        getVoices: () => [], addEventListener() {}, removeEventListener() {},
        speak(utterance) { fixture.spoken.push(utterance.text); this.speaking = true; utterance.onstart?.(); },
        cancel() { fixture.cancellations += 1; this.speaking = false; },
      } });
      fixture.finishRecognition = text => {
        const result = [{ transcript: text, confidence: 1 }];
        result.isFinal = true;
        fixture.recognition.onresult?.({ resultIndex: 0, results: [result] });
        fixture.recognition.onend?.();
      };
      window.__speechFixture = fixture;
    });
    const errors = [], mutations = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.method() === 'POST' && new URL(request.url()).pathname.startsWith('/api/')) mutations.push(new URL(request.url()).pathname);
    });
    await page.goto('http://127.0.0.1:' + port);
    await page.waitForFunction(() => /demo/i.test(document.getElementById('mode')?.textContent || ''));
    await page.evaluate(() => document.fonts.ready);
    async function screenshot(name) {
      if (!screenshotDirectory) return;
      fs.mkdirSync(screenshotDirectory, { recursive: true });
      await page.screenshot({ path: path.join(screenshotDirectory, name + '.png'), fullPage: true, animations: 'disabled' });
    }
    async function focusIs(selector) {
      await page.waitForFunction(selector => document.activeElement === document.querySelector(selector), selector);
    }
    async function openMenu(trigger, menu) {
      await page.locator(trigger).click();
      await page.locator(menu).waitFor({ state: 'visible' });
      assert.equal(await page.locator(trigger).getAttribute('aria-expanded'), 'true');
      assert.equal(await page.locator(menu).getAttribute('role'), 'menu');
    }
    async function profileDestination(id, dialog) {
      await openMenu('#profile-menu-button', '#profile-menu');
      await page.locator('#' + id).click();
      await page.locator(dialog + '[open]').waitFor();
      assert.equal(await page.locator('#profile-menu').isVisible(), false);
      assert.equal(await page.locator(dialog).evaluate(node => node.contains(document.activeElement)), true);
      if (dialog === '#preferences-dialog') {
        await page.locator('#theme-choice').press('Space');
        await page.keyboard.press('Escape');
        assert.equal(await page.locator(dialog + '[open]').count(), 1, 'Escape closes the native select before its enclosing dialog');
      }
      await page.keyboard.press('Escape');
      await page.locator(dialog).waitFor({ state: 'hidden' });
      await focusIs('#profile-menu-button');
    }
    async function headerDestination(id, dialog) {
      await openMenu('#header-menu-button', '#header-menu');
      await page.locator('#' + id).click();
      await page.locator(dialog + '[open]').waitFor();
      assert.equal(await page.locator('#header-menu').isVisible(), false);
      await page.keyboard.press('Escape');
      await page.locator(dialog).waitFor({ state: 'hidden' });
      await focusIs('#header-menu-button');
    }
    async function withinViewport(selector) {
      assert.equal(await page.locator(selector).evaluate(node => {
        const rect = node.getBoundingClientRect();
        return rect.left >= -1 && rect.right <= innerWidth + 1 && rect.top >= -1 && rect.bottom <= innerHeight + 1;
      }), true, selector + ' stays inside the viewport');
    }

    // The first screen keeps the principal destinations visible and hides the
    // long tail until a person opens an appropriate menu.
    for (const id of ['new', 'search', 'knowledge-open', 'features-open', 'profile-menu-button']) {
      assert.equal(await page.locator('#' + id).isVisible(), true);
    }
    for (const id of ['model-settings', 'project-open', 'files-open', 'preferences-open', 'memory', 'find-chat', 'context-open', 'manage-chat']) {
      assert.equal(await page.locator('#' + id).isVisible(), false, id + ' is initially tucked into a menu');
    }
    async function assertWelcomeVisible() {
      assert.equal(await page.locator('#welcome h1').evaluate(node => {
        const rect = node.getBoundingClientRect();
        const top = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
        return node === top || node.contains(top);
      }), true, 'The welcome heading is visible above the composer, not covered by it');
    }
    async function assertHomeControlsUsable() {
      await assertWelcomeVisible();
      for (const selector of ['#form', '#prompt', '#composer-menu-button', '#dictate', '#mode', '#read-aloud']) {
        await withinViewport(selector);
      }
      for (const selector of ['#prompt', '#composer-menu-button', '#dictate', '#mode', '#read-aloud']) {
        assert.equal(await page.locator(selector).evaluate(node => {
          const rect = node.getBoundingClientRect();
          const top = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
          return node === top || node.contains(top);
        }), true, selector + ' remains visible and clickable');
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
    await assertHomeControlsUsable();
    assert.equal(await page.locator('#read-aloud').getAttribute('aria-pressed'), 'false', 'Reading replies aloud is opt-in');
    assert.equal(await page.locator('#dictate').isVisible(), true, 'Supported speech input is offered for an empty composer');
    await screenshot('ui-desktop-home');
    await page.setViewportSize({ width: 390, height: 844 });
    await assertHomeControlsUsable();
    await screenshot('ui-mobile-home');
    await openMenu('#mode', '#model-menu');
    await withinViewport('#model-menu');
    assert.equal(await page.locator('#demo-note').isVisible(), true);
    assert.match(await page.locator('#demo-note').textContent(), /demo.*scripted/i, 'The model picker identifies scripted demo replies honestly');
    await screenshot('ui-mobile-model-menu');
    await page.keyboard.press('Escape');
    await focusIs('#mode');
    await page.setViewportSize({ width: 390, height: 520 });
    await page.locator('#prompt').click();
    await assertHomeControlsUsable();
    await screenshot('ui-mobile-short-home');
    await page.setViewportSize({ width: 390, height: 844 });

    const beforeShortcuts = mutations.length;
    await page.locator('#new-project-shortcut').click();
    await page.locator('#project-dialog[open]').waitFor();
    await page.keyboard.press('Escape');
    await page.locator('#project-dialog').waitFor({ state: 'hidden' });
    for (const title of ['Make a plan', 'Write something', 'Explore a project']) {
      await page.locator('#starters').getByRole('button', { name: title, exact: true }).click();
      assert((await page.locator('#prompt').inputValue()).length > 0, title + ' fills a useful draft');
      assert.equal(await page.locator('#send').isVisible(), true);
      assert.equal(await page.locator('#dictate').isVisible(), false, 'Typed content exposes Send instead of the microphone');
      await focusIs('#prompt');
      await page.locator('#prompt').fill('');
    }
    assert.equal(mutations.length, beforeShortcuts, 'Shortcuts prepare work without submitting a message or changing the project');
    await page.locator('#dictate').click();
    assert.equal(await page.evaluate(() => window.__speechFixture.starts), 1);
    await page.evaluate(() => window.__speechFixture.finishRecognition('A voice draft for this project'));
    assert.equal(await page.locator('#prompt').inputValue(), 'A voice draft for this project');
    assert.equal(await page.locator('#send').isEnabled(), true);
    assert.equal(mutations.length, beforeShortcuts, 'Speech input creates a draft without sending it');
    await page.locator('#prompt').fill('');
    await page.setViewportSize({ width: 1440, height: 1000 });
    await openMenu('#profile-menu-button', '#profile-menu');
    await withinViewport('#profile-menu');
    await screenshot('ui-desktop-profile-menu');
    const firstItem = page.locator('#profile-menu').getByRole('menuitem').first();
    assert.equal(await firstItem.evaluate(node => node === document.activeElement), true);
    await page.keyboard.press('ArrowDown');
    assert.equal(await firstItem.evaluate(node => node === document.activeElement), false);
    assert.equal(await page.locator('#profile-menu').evaluate(node => node.contains(document.activeElement)), true);
    await page.keyboard.press('Escape');
    await page.locator('#profile-menu').waitFor({ state: 'hidden' });
    await focusIs('#profile-menu-button');
    assert.equal(await page.locator('#profile-menu-button').getAttribute('aria-expanded'), 'false');

    await openMenu('#profile-menu-button', '#profile-menu');
    await page.locator('#prompt').click();
    await page.locator('#profile-menu').waitFor({ state: 'hidden' });
    await focusIs('#prompt');
    // Shift+Enter keeps a draft; Enter sends a real turn from the slim composer.
    const chatsBefore = mutations.filter(url => url === '/api/chat').length;
    await page.locator('#prompt').fill('Show me the project files');
    await page.locator('#prompt').press('Shift+Enter');
    assert.match(await page.locator('#prompt').inputValue(), /\n$/);
    assert.equal(mutations.filter(url => url === '/api/chat').length, chatsBefore);
    assert.deepEqual(await page.evaluate(() => window.__speechFixture.spoken), []);
    await page.locator('#read-aloud').click();
    assert.equal(await page.locator('#read-aloud').getAttribute('aria-pressed'), 'true');
    await page.locator('#prompt').press('Enter');
    await page.waitForFunction(() => document.getElementById('messages').textContent.includes('Offline demo complete') && document.getElementById('stop').classList.contains('hidden'));
    await page.waitForFunction(() => window.__speechFixture.spoken.some(text => text.includes('Offline demo complete')));
    await page.waitForResponse(response => response.url().endsWith('/api/status'));
    assert.equal(await page.evaluate(() => window.__speechFixture.spoken.filter(text => text.includes('Offline demo complete')).length), 1, 'Polling does not repeat a completed reply');
    const cancellations = await page.evaluate(() => window.__speechFixture.cancellations);
    await page.locator('#read-aloud').click();
    assert.equal(await page.locator('#read-aloud').getAttribute('aria-pressed'), 'false');
    assert((await page.evaluate(() => window.__speechFixture.cancellations)) > cancellations, 'Disabling readout cancels current speech');
    assert.equal(await page.evaluate(() => speechSynthesis.speaking), false);
    await page.locator('#read-aloud').click();
    await page.reload();
    await page.waitForFunction(() => document.getElementById('messages').textContent.includes('Offline demo complete'));
    await page.waitForResponse(response => response.url().endsWith('/api/status'));
    assert.equal(await page.locator('#read-aloud').getAttribute('aria-pressed'), 'true');
    assert.deepEqual(await page.evaluate(() => window.__speechFixture.spoken), [], 'Reloading saved history never reads old replies aloud');
    await page.locator('#read-aloud').click();
    await openMenu('#profile-menu-button', '#profile-menu');
    await openMenu('#header-menu-button', '#header-menu');
    assert.equal(await page.locator('#profile-menu').isVisible(), false, 'Only one popover is open');
    assert.equal(await page.locator('[role="menu"]:visible').count(), 1);
    await page.keyboard.press('Escape');
    await focusIs('#header-menu-button');

    // Every former sidebar settings destination remains reachable.
    for (const [id, dialog] of [['model-settings', '#settings-dialog'], ['project-open', '#project-dialog'],
      ['files-open', '#files-dialog'], ['preferences-open', '#preferences-dialog'], ['memory', '#memory-dialog'],
      ['palette-open', '#palette-dialog'], ['workspace-settings', '#details']]) {
      await profileDestination(id, dialog);
    }
    await openMenu('#mode', '#model-menu');
    await withinViewport('#model-menu');
    await screenshot('ui-desktop-model-menu');
    await page.locator('#model-configure').click();
    await page.locator('#settings-dialog[open]').waitFor();
    await page.keyboard.press('Escape');
    await page.locator('#settings-dialog').waitFor({ state: 'hidden' });
    await focusIs('#mode');
    await headerDestination('context-open', '#context-dialog');
    await headerDestination('header-settings', '#details');
    await openMenu('#header-menu-button', '#header-menu');
    await page.locator('#find-chat').click();
    await page.locator('#chat-query').waitFor({ state: 'visible' });
    await focusIs('#chat-query');
    await page.locator('#close-chat-search').click();

    // Visiting the complete catalog exposes exactly one form per chosen action
    // and never executes any of the API operations just by navigating to them.
    const catalogResponse = page.waitForResponse(response => response.url().endsWith('/api/features'));
    await page.locator('#features-open').click();
    const modules = (await (await catalogResponse).json()).modules;
    await page.locator('#features-dialog[open]').waitFor();
    assert.equal(await page.locator('#workspace-categories [data-feature-module]:not([data-feature-module="all"])').count(), modules.length);
    const beforeNavigation = mutations.length;
    let visitedActions = 0;
    for (const module of modules) {
      await page.locator('#feature-query').fill('');
      await page.locator('#workspace-categories button[data-feature-module=' + JSON.stringify(module.id) + ']').click();
      assert.equal(await page.locator('button[data-feature-action]').count(), module.actions.length, module.title + ' action list');
      for (const action of module.actions) {
        await page.locator('button[data-feature-action=' + JSON.stringify(action.label) + ']').click();
        const form = page.locator('.feature-action');
        assert.equal(await form.count(), 1, 'Only the selected action is rendered');
        await form.getByRole('heading', { name: action.label, exact: true }).waitFor();
        visitedActions += 1;
        await page.locator('#workspace-back').click();
        await page.locator('#workspace-categories button[data-feature-module=' + JSON.stringify(module.id) + ']').click();
      }
    }
    assert(visitedActions >= 100, 'The existing catalog remains accessible');
    assert.equal(mutations.length, beforeNavigation, 'Browsing catalog actions performs no writes or model requests');
    await page.locator('#feature-query').fill('Read a file');
    await page.locator('button[data-feature-action="Read a file"]').click();
    await page.locator('.feature-action').getByLabel('Relative file path', { exact: true }).fill('README.md');
    await page.locator('.feature-action button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('.feature-action .dialog-status')?.textContent === 'Complete.');
    assert.match(await page.locator('.feature-result').textContent(), /Visible workspace forms can read this file/);
    await screenshot('ui-desktop-workspace');
    // Dialog boundary handling keeps keyboard focus in the visible modal.
    for (let count = 0; count < 4; count += 1) {
      await page.keyboard.press('Tab');
      assert.equal(await page.locator('#features-dialog').evaluate(node => node.contains(document.activeElement)), true);
    }
    await page.keyboard.press('Escape');
    await page.locator('#features-dialog').waitFor({ state: 'hidden' });
    await focusIs('#features-open');

    await openMenu('#composer-menu-button', '#composer-menu');
    await page.locator('#tools').click();
    await page.locator('#details[open]').waitFor();
    await page.keyboard.press('Escape');
    await focusIs('#composer-menu-button');
    await openMenu('#composer-menu-button', '#composer-menu');
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/run/options') && response.request().method() === 'POST'),
      page.locator('#run-mode').press('ArrowDown'),
    ]);
    assert.equal(await page.locator('#run-mode').inputValue(), 'plan', 'ArrowDown changes the native mode selector inside the menu');
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/run/options') && response.request().method() === 'POST'),
      page.locator('#run-mode').press('ArrowUp'),
    ]);
    assert.equal(await page.locator('#run-mode').inputValue(), 'act');
    await page.locator('#run-mode').press('Space');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#composer-menu').isVisible(), true, 'Escape closes the native selector before the surrounding menu');
    await page.keyboard.press('Escape');
    await page.locator('#composer-menu').waitFor({ state: 'hidden' });
    await focusIs('#composer-menu-button');

    const assistant = page.locator('#messages .assistant').last();
    const copy = assistant.getByRole('button', { name: 'Copy', exact: true });
    assert.equal(await copy.locator('svg').count(), 1, 'Copy begins as an icon button');
    const copyHandle = await copy.elementHandle();
    await copy.click();
    await page.waitForFunction(node => /copied/i.test(node.textContent + ' ' + node.title + ' ' + node.getAttribute('aria-label')), copyHandle);
    assert(await copyHandle.$('svg'), 'Copied feedback preserves the SVG icon');
    const more = assistant.getByRole('button', { name: 'More message actions', exact: true });
    await more.click();
    await page.getByRole('menuitem', { name: 'Save message', exact: true }).waitFor();
    await page.keyboard.press('Escape');
    assert.equal(await more.evaluate(node => document.activeElement === node), true);
    await more.click();
    await page.getByRole('menuitem', { name: 'Save message', exact: true }).click();
    await page.locator('#messages .assistant.bookmarked').waitFor();
    await page.locator('#messages .assistant').last().getByRole('button', { name: 'More message actions', exact: true }).click();
    await page.getByRole('menuitem', { name: 'Quote', exact: true }).click();
    assert.match(await page.locator('#prompt').inputValue(), /Offline demo complete/);
    await page.locator('#prompt').fill('');
    await headerDestination('manage-chat', '#conversation-dialog');
    await screenshot('ui-desktop-conversation');

    // Narrow-screen controls use the same visible menus and preserve a usable
    // drawer, dialog and composer without horizontal overflow.
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await screenshot('ui-mobile-conversation');
    await page.locator('#menu').click();
    await page.locator('body.sidebar-open').waitFor();
    await openMenu('#profile-menu-button', '#profile-menu');
    await withinViewport('#profile-menu');
    await screenshot('ui-mobile-profile-menu');
    await page.keyboard.press('Escape');
    await page.locator('#profile-menu').waitFor({ state: 'hidden' });
    await focusIs('#profile-menu-button');
    assert.equal(await page.locator('body.sidebar-open').count(), 1, 'Escape dismisses the submenu before its drawer');
    await openMenu('#profile-menu-button', '#profile-menu');
    await page.locator('#preferences-open').click();
    await page.locator('#preferences-dialog[open]').waitFor();
    assert.equal(await page.locator('body.sidebar-open').count(), 0);
    await page.keyboard.press('Escape');
    await page.locator('#preferences-dialog').waitFor({ state: 'hidden' });
    await page.locator('#menu').click();
    await page.locator('#features-open').click();
    await page.locator('#features-dialog[open]').waitFor();
    assert.equal(await page.locator('body.sidebar-open').count(), 0);
    await page.locator('#feature-query').fill('Read a file');
    await page.locator('button[data-feature-action="Read a file"]').click();
    assert.equal(await page.locator('#features-dialog').evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
    await screenshot('ui-mobile-workspace');
    await page.keyboard.press('Escape');
    await page.locator('#features-dialog').waitFor({ state: 'hidden' });
    await page.locator('#menu').click();
    await page.locator('#backdrop').click({ position: { x: 385, y: 400 } });
    assert.equal(await page.locator('body.sidebar-open').count(), 0);
    await page.locator('#menu').click();
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('body.sidebar-open').count(), 0);
    await focusIs('#menu');
    await page.locator('#menu').click();
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.waitForFunction(() => !document.body.classList.contains('sidebar-open') && !document.querySelector('main').inert);
    await page.locator('#prompt').click();
    await focusIs('#prompt');
    await page.locator('#prompt').fill('Desktop remains interactive after resizing an open mobile drawer');
    assert.match(await page.locator('#prompt').inputValue(), /Desktop remains interactive/);
    await page.locator('#prompt').fill('');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.locator('#error').textContent(), '');

    // Browsers without speech APIs keep a usable composer and do not advertise
    // an enabled voice control that can never work.
    const unsupported = await browser.newPage({ viewport: { width: 390, height: 844 } });
    unsupported.on('pageerror', error => errors.push(error.message));
    await unsupported.addInitScript(() => {
      delete window.SpeechRecognition;
      delete window.webkitSpeechRecognition;
      delete window.speechSynthesis;
    });
    await unsupported.goto('http://127.0.0.1:' + port);
    await unsupported.waitForFunction(() => /demo/i.test(document.getElementById('mode')?.textContent || ''));
    assert.equal(await unsupported.locator('#dictate').isVisible(), false);
    assert.equal(await unsupported.locator('#send').isVisible(), true);
    const unavailableSpeaker = unsupported.locator('#read-aloud');
    assert(!(await unavailableSpeaker.isVisible()) || await unavailableSpeaker.isDisabled(), 'Unsupported readout is unavailable rather than a working-looking control');
    assert.equal(await unsupported.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await unsupported.close();
    assert.deepEqual(errors, []);
    console.log('PASS: responsive home/header model/shortcuts, speech controls with browser fixtures, Enter/Shift+Enter, profile/header/composer/message menus, keyboard and outside-click dismissal, focus restoration, all ' + visitedActions + ' catalog actions, one-form Workspace, desktop/mobile navigation and screenshots.');
  } catch (error) {
    if (page && screenshotDirectory) {
      fs.mkdirSync(screenshotDirectory, { recursive: true });
      await page.screenshot({ path: path.join(screenshotDirectory, 'navigation-failure.png'), fullPage: true }).catch(() => {});
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    await new Promise(resolve => {
      if (server.exitCode !== null || server.signalCode !== null) return resolve();
      const timeout = setTimeout(resolve, 3000);
      server.once('exit', () => { clearTimeout(timeout); resolve(); });
    });
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
