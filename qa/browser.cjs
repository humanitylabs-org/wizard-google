/* Real-browser QA of the Google app UI against qa/preview.py (FAKE Google).
 * Checks: hosted transparent canvas, live re-skin without reload, dark standalone fallback,
 * Back never leaves the app (sheet → nested view → home → stays), setup paste validation.
 * Usage: PLAYWRIGHT_PATH=/path/to/playwright QA_SHOTS=dir node qa/browser.cjs */
'use strict';
const assert = require('node:assert/strict');
const path = require('node:path'), fs = require('node:fs'), { spawn } = require('node:child_process');
const pw = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const ROOT = path.resolve(__dirname, '..');
const shots = path.resolve(process.env.QA_SHOTS || path.join(ROOT, 'qa', 'shots')); fs.mkdirSync(shots, { recursive: true });
const CLIENT_JSON = JSON.stringify({ web: { client_id: '123456789012-abcdefghijklmnop.apps.googleusercontent.com', project_id: 'wizard-test-123',
  token_uri: 'https://oauth2.googleapis.com/token', client_secret: 'GOCSPX-fake-secret-for-tests', redirect_uris: [] } });
const checks = []; const ok = (name, value) => { assert(value, name); checks.push(name); };

function preview(scenario, port) {
  const child = spawn(process.env.PYTHON || 'python3', [path.join(ROOT, 'qa', 'preview.py'), '--port', String(port), '--scenario', scenario], { stdio: ['ignore', 'pipe', 'inherit'] });
  return new Promise((resolve, reject) => {
    child.stdout.on('data', d => { if (String(d).includes('preview')) resolve(child); });
    child.on('exit', code => reject(new Error(`preview exited ${code}`)));
  });
}
async function hosted(page, port, skin) {
  await page.goto(`http://127.0.0.1:${port}/__qa/host.html?skin=${skin}`);
  await page.waitForFunction(() => window.acks && window.acks.length > 0);
  const frame = page.frame({ url: /:\d+\/$/ });
  await frame.waitForSelector('.wz-sec, .crumb');
  return frame;
}
const shot = (page, name) => page.screenshot({ path: path.join(shots, name + '.png') });

(async () => {
  const procs = [];
  const browser = await pw.chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {});
  try {
    const ctx = await browser.newContext({ viewport: { width: 412, height: 892 }, deviceScaleFactor: 2 });

    // ---- 1. Fresh box: hosted on two skins, live re-skin, Back ----
    procs.push(await preview('fresh', 18891));
    let page = await ctx.newPage();
    let app = await hosted(page, 18891, 'nebula');
    const canvas = await app.evaluate(() => [getComputedStyle(document.documentElement).backgroundColor, getComputedStyle(document.body).backgroundColor, document.documentElement.dataset.skin]);
    ok('hosted canvas is transparent (html + body)', canvas[0] === 'rgba(0, 0, 0, 0)' && canvas[1] === 'rgba(0, 0, 0, 0)' && canvas[2] === 'nebula');
    await shot(page, '01-home-fresh-nebula');
    await app.click('text=Start setup');
    await app.waitForSelector('.steps');
    ok('setup lists 7 steps', (await app.$$('.step')).length === 7);
    await shot(page, '02-setup-nebula');
    const before = await app.evaluate(() => { window.__marker = 1; return getComputedStyle(document.querySelector('.step.current .num')).color; });
    const skins = JSON.parse(fs.readFileSync(path.join(ROOT, 'qa', 'skins.json')));
    await page.evaluate(s => window.reskin(s), { name: 'grove', ...skins.grove });
    await app.waitForFunction(() => document.documentElement.dataset.skin === 'grove');
    const after = await app.evaluate(() => [getComputedStyle(document.querySelector('.step.current .num')).color, window.__marker]);
    ok('live re-skin changes colours without reloading the app', after[0] !== before && after[1] === 1);
    await page.evaluate(b => { document.body.style.background = b; }, skins.grove.backdrop);
    await shot(page, '03-setup-grove-live-reskin');
    ok('Back from setup returns home inside the app', await app.evaluate(() => window.__wizardCloseOverlay()) === true);
    await app.waitForSelector('.hero');
    ok('Back at home is not handled (shell keeps the app open)', await app.evaluate(() => window.__wizardCloseOverlay()) === false);
    ok('app still open after Back at root', await app.$('.hero') !== null);

    // ---- 2. Paste step: validation messages, then accepted client ----
    procs.push(await preview('paste', 18892));
    page = await ctx.newPage();
    app = await hosted(page, 18892, 'grove');
    await app.click('text=Continue setup');
    await app.waitForSelector('.step textarea');
    await app.fill('.step textarea', '{"installed":{"client_id":"x"}}');
    await app.click('text=Check and save');
    await app.waitForSelector('.result:not(:empty)');
    ok('desktop client is explained', /Web application/.test(await app.textContent('.result')));
    await shot(page, '04-setup-paste-error-grove');
    await app.fill('.step textarea', CLIENT_JSON);
    await app.click('text=Check and save');
    await app.waitForFunction(() => document.querySelectorAll('.step.done').length >= 5);
    ok('valid client accepted and steps advance', true);
    await shot(page, '05-setup-after-paste-grove');

    // ---- 3. Connected: home, account, caller, sheet + Back order ----
    procs.push(await preview('connected', 18893));
    for (const skin of ['nebula', 'ember']) {
      page = await ctx.newPage();
      app = await hosted(page, 18893, skin);
      await app.waitForSelector('.wz-card.ask');
      await shot(page, `06-home-connected-${skin}`);
    }
    await app.click('.wz-row:has-text("sam@studio.example")');
    await app.waitForSelector('.crumb');
    await shot(page, '07-account-ember');
    await app.evaluate(() => window.__wizardCloseOverlay());
    await app.click('.wz-row:has-text("Main agent")');
    await app.waitForSelector('.toggle');
    await shot(page, '08-caller-ember');
    await app.evaluate(() => window.__wizardCloseOverlay());
    await app.click('text=Connect another account');
    await app.waitForSelector('dialog[open]');
    await shot(page, '09-connect-sheet-ember');
    ok('Back closes the sheet first', await app.evaluate(() => window.__wizardCloseOverlay()) === true && await app.$('dialog[open]') === null);

    // ---- 4. Standalone tab: dark fallback, browser Back steps inside, root needs a second Back ----
    page = await ctx.newPage();
    await page.goto('http://127.0.0.1:18893/');
    await page.waitForSelector('.wz-sec');
    const solo = await page.evaluate(() => [getComputedStyle(document.body).backgroundColor, document.documentElement.classList.contains('wz-hosted')]);
    ok('standalone paints the dark base', solo[0] === 'rgb(5, 3, 12)' && solo[1] === false);
    await shot(page, '10-standalone-home');
    await page.click('.wz-row:has-text("sam@gmail.com")');
    await page.waitForSelector('.crumb');
    await page.goBack();
    await page.waitForSelector('.wz-card.ask');
    ok('browser Back leaves the nested view, stays in the app', page.url() === 'http://127.0.0.1:18893/');
    await page.goBack();
    await page.waitForFunction(() => /Press back again/.test(document.getElementById('status').textContent));
    ok('first Back at root warns and stays', page.url() === 'http://127.0.0.1:18893/' && await page.$('.wz-card.ask') !== null);
    await shot(page, '11-standalone-back-at-root');

    console.log(JSON.stringify({ pass: checks.length, checks, shots }, null, 1));
  } finally {
    await browser.close();
    for (const p of procs) p.kill();
  }
})().catch(e => { console.error(e); process.exit(1); });
