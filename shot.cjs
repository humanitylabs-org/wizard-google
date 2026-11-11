// Before/after: open the agent's "Dentist" event in a real Calendar fed by the real wizard-google broker.
const {chromium} = require(process.env.HOME + '/work/pw/node_modules/playwright');
(async () => {
  const [base, out] = process.argv.slice(2);
  const b = await chromium.launch({headless: true, executablePath: process.env.BROWSER_PATH});
  const ctx = await b.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  await ctx.clock.setFixedTime(new Date('2026-11-04T10:00:00+07:00'));
  const page = await ctx.newPage();
  page.on('pageerror', e => console.log('pageerror', String(e)));
  await page.goto(base + '/');
  await page.waitForFunction(() => /visible events/.test(document.querySelector('[data-status]')?.textContent || ''), null, {timeout: 20000});
  const dayBtn = page.locator('[data-mode="day"]');
  if (await dayBtn.count()) await dayBtn.first().click();
  await page.waitForTimeout(800);
  const ev = page.locator('[data-ev]', {hasText: 'Dentist'}).first();
  console.log('dentist blocks', await page.locator('[data-ev]', {hasText: 'Dentist'}).count());
  await ev.click();
  await page.waitForSelector('[data-sheet]:not([hidden])');
  await page.waitForTimeout(600);
  console.log('by-agent rows', await page.locator('[data-by-agent]').count());
  console.log('sheet', (await page.locator('[data-sheet]').innerText()).replace(/\s+/g, ' ').slice(0, 400));
  await page.screenshot({path: out});
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
