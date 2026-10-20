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

    // ---- 5. Agent writes: exact payload cards, two skins, one tap and no replay ----
    procs.push(await preview('approval', 18894));
    const mobile = await browser.newContext({ ...pw.devices['Pixel 7'] });
    page = await mobile.newPage();
    app = await hosted(page, 18894, 'nebula');
    const writes = async () => (await page.request.get('http://127.0.0.1:18894/__qa/writes')).json();
    ok('pending agent writes do not call either fake Google mutation', JSON.stringify(await writes()) === JSON.stringify({ sent: 0, calendar_posts: 0 }));
    ok('approval QA uses mobile-UA/touch and actual phone-width frame',
      await app.evaluate(width => navigator.maxTouchPoints > 0 && innerWidth === width, pw.devices['Pixel 7'].viewport.width));
    const mail = app.locator('.write-approval').filter({ hasText: 'Thursday lunch' });
    const calendar = app.locator('.write-approval').filter({ hasText: 'Lunch with Anna' });
    await mail.waitFor();
    ok('card includes exact account, recipient, subject and complete message',
      /sam@gmail.com/.test(await mail.textContent()) && /anna@example.com/.test(await mail.textContent()) &&
      /Does noon on Thursday work for lunch\?/.test(await mail.textContent()));
    const originalRequest = await mail.getAttribute('data-request-id');
    await app.evaluate(() => { window.__approvalDocument = 'same'; });
    for (const skin of ['nebula', 'ember']) {
      if (skin !== 'nebula') {
        await page.evaluate(s => { window.reskin(s); document.body.style.background = s.backdrop; }, { name: skin, ...skins[skin] });
        await app.waitForFunction(() => document.documentElement.dataset.skin === 'ember');
      }
      await mail.scrollIntoViewIfNeeded();
      await shot(page, `12-agent-send-approval-${skin}`);
      await calendar.scrollIntoViewIfNeeded();
      await shot(page, `13-agent-calendar-approval-${skin}`);
    }
    ok('pending cards survive live skin change without navigation or payload substitution',
      await app.evaluate(() => window.__approvalDocument) === 'same' && await mail.getAttribute('data-request-id') === originalRequest);
    await mail.getByRole('button', { name: 'Approve', exact: true }).click();
    await mail.waitFor({ state: 'detached' });
    ok('one owner tap sends once and displays an executed receipt', (await writes()).sent === 1 &&
      /Executed once/.test(await app.textContent('#view')));
    await calendar.getByRole('button', { name: 'Deny', exact: true }).click();
    await calendar.waitFor({ state: 'detached' });
    ok('one owner tap denies the event and creates nothing', (await writes()).calendar_posts === 0 &&
      /Denied · nothing sent/.test(await app.textContent('#view')));
    await page.reload();
    app = page.frame({ url: /:\d+\/$/ });
    await app.waitForSelector('.wz-sec');
    ok('reload does not replay an approved action', (await writes()).sent === 1 && await app.locator('.write-approval').count() === 0);
    // A fresh MCP proposal while the owner screen is already open appears via read-only polling.
    const agentHttp = await pw.request.newContext(); // MCP must never receive the owner's browser cookie
    const incoming = await (await agentHttp.post('http://127.0.0.1:18894/mcp', { data: {
      jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'google_gmail_send', arguments: {
        to: ['anna@example.com'], subject: 'Another exact proposal', body: '<script>not HTML</script>' } } } })).json();
    assert.equal(incoming.result.structuredContent.status, 'pending');
    await agentHttp.dispose();
    const freshCard = app.locator('.write-approval').filter({ hasText: 'Another exact proposal' });
    await freshCard.waitFor({ timeout: 10000 });
    ok('already-open home receives a new card without another write invocation', (await writes()).sent === 1);
    ok('agent-provided content is rendered as text, never HTML', await freshCard.locator('script').count() === 0 &&
      (await freshCard.textContent()).includes('<script>not HTML</script>'));
    await freshCard.scrollIntoViewIfNeeded();
    await shot(page, '14-new-proposal-after-approval');
    await mobile.close();

    console.log(JSON.stringify({ pass: checks.length, checks, shots }, null, 1));
  } finally {
    await browser.close();
    for (const p of procs) p.kill();
  }
})().catch(e => { console.error(e); process.exit(1); });
