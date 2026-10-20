import assert from 'node:assert/strict';
import worker, { parseState } from './worker.mjs';
const encode = r => 'wg1.' + Buffer.from(JSON.stringify({ n: 'a'.repeat(43), r })).toString('base64url');
const good = 'https://box.tail1234.ts.net/wizard-google/oauth/callback';
assert.equal(parseState(encode(good)).href, good);
assert.equal(parseState(encode('https://wizard.example.org:8443/oauth/callback')).host, 'wizard.example.org:8443');
for (const r of ['http://box.tail1234.ts.net/wizard-google/oauth/callback', 'https://u@box.tail1234.ts.net/wizard-google/oauth/callback',
  'https://box.tail1234.ts.net/evil', 'https://box.tail1234.ts.net/wizard-google/oauth/callback?x=1', 'https://box.tail1234.ts.net/wizard-google/oauth/callback#x',
  'javascript:alert(1)', 'https://127.0.0.1/oauth/callback']) assert.throws(() => parseState(encode(r)), r);
const call = q => worker.fetch(new Request('https://www.aiwizards.com/wizard-google/auth/callback?' + q));
const valid = new URLSearchParams({ state: encode(good), code: '4/0Afake-code', scope: 'openid email' });
let response = await call(valid);
assert.equal(response.status, 200); assert.equal(response.headers.get('location'), null);
const html = await response.text();
assert.match(html, /Continue only if/); assert(!html.includes('<script')); assert.match(html, /box\.tail1234\.ts\.net\/wizard-google\/oauth\/callback\?state=/);
assert.equal(response.headers.get('cache-control'), 'no-store'); assert.equal(response.headers.get('referrer-policy'), 'no-referrer');
for (const q of ['state=bad&code=x', valid + '&code=y', valid + '&error=denied', new URLSearchParams({ state: encode(good) }), valid + '&other=x'])
  assert.equal((await call(q)).status, 400, String(q));
assert.equal((await call(new URLSearchParams({ state: encode(good), error: 'access_denied' }))).status, 200);
console.log('relay: target rules, query validation, explicit click, no script, security headers PASS');
