import assert from 'node:assert/strict';
import worker, { parseState } from './worker.mjs';
const encode = r => 'wg1.' + Buffer.from(JSON.stringify({ n: 'a'.repeat(43), r })).toString('base64url');
const good = 'https://box.tail1234.ts.net/wizard-google/oauth/callback';
assert.equal(parseState(encode(good)).href, good);
assert.equal(parseState(encode('https://wizard.example.org:8443/oauth/callback')).host, 'wizard.example.org:8443');
for (const r of ['http://box.tail1234.ts.net/wizard-google/oauth/callback', 'https://u@box.tail1234.ts.net/wizard-google/oauth/callback',
  'https://box.tail1234.ts.net/evil', 'https://box.tail1234.ts.net/wizard-google/oauth/callback?x=1', 'https://box.tail1234.ts.net/wizard-google/oauth/callback#x',
  'javascript:void(0)', 'https://127.0.0.1/oauth/callback']) assert.throws(() => parseState(encode(r)), r);
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

// ---- sign-in service: v1/client and v1/token (fake Google token endpoint) ----
const ID = '123456789012-abcdefghijklmnop.apps.googleusercontent.com';
const env = { GOOGLE_CLIENT_ID: ID, GOOGLE_CLIENT_SECRET: 'GOCSPX-test-secret-value' };
const base = 'https://www.aiwizards.com/wizard-google/auth/';
assert.equal((await worker.fetch(new Request(base + 'v1/client'), {})).status, 503, 'unconfigured client');
let r = await worker.fetch(new Request(base + 'v1/client'), env);
assert.equal(r.status, 200); const offered = await r.json();
assert.deepEqual(offered, { client_id: ID, redirect_uri: base + 'callback', name: 'Wizard', verified: false });
assert(!JSON.stringify(offered).includes('GOCSPX'));
let sent = null;
globalThis.fetch = async (url, init) => { sent = { url, body: new URLSearchParams(init.body) }; return new Response('{"access_token":"ya29.x","token_type":"Bearer","expires_in":3599}', { status: 200 }); };
const post = (body, type = 'application/x-www-form-urlencoded') => worker.fetch(new Request(base + 'v1/token', { method: 'POST', headers: { 'Content-Type': type }, body: new URLSearchParams(body).toString() }), env);
const verifier = 'v'.repeat(64);
r = await post({ grant_type: 'authorization_code', client_id: ID, code: '4/0Afake', code_verifier: verifier, redirect_uri: base + 'callback' });
assert.equal(r.status, 200); assert.equal(sent.url, 'https://oauth2.googleapis.com/token');
assert.equal(sent.body.get('client_secret'), env.GOOGLE_CLIENT_SECRET); assert.equal(sent.body.get('code_verifier'), verifier);
sent = null;
for (const bad of [
  { grant_type: 'authorization_code', client_id: ID, code: '4/0Afake', redirect_uri: base + 'callback' },                       // no PKCE
  { grant_type: 'authorization_code', client_id: ID, code: '4/0Afake', code_verifier: verifier, redirect_uri: 'https://evil.example/cb' },
  { grant_type: 'client_credentials', client_id: ID },
  { grant_type: 'refresh_token', client_id: ID, refresh_token: 'x' },
]) { const res = await post(bad); assert.equal(res.status, 400, JSON.stringify(bad)); }
assert.equal((await post({ grant_type: 'refresh_token', client_id: 'other-client', refresh_token: '1//refresh-token-value' })).status, 401);
assert.equal((await post({ grant_type: 'refresh_token', client_id: ID, refresh_token: '1//x' }, 'application/json')).status, 415);
assert.equal(sent, null, 'refused requests never reach Google');
r = await post({ grant_type: 'refresh_token', client_id: ID, refresh_token: '1//refresh-token-value' });
assert.equal(r.status, 200); assert.equal(sent.body.get('refresh_token'), '1//refresh-token-value'); assert.equal(sent.body.get('client_secret'), env.GOOGLE_CLIENT_SECRET);
globalThis.fetch = async () => new Response('{"error":"invalid_grant","error_description":"Bad Request"}', { status: 400 });
r = await post({ grant_type: 'refresh_token', client_id: ID, refresh_token: '1//refresh-token-value' });
assert.equal(r.status, 400); assert.equal((await r.json()).error, 'invalid_grant', 'Google errors pass through for the box to classify');
assert.equal((await worker.fetch(new Request(base + 'v1/token'), env)).status, 405);
const about = await (await worker.fetch(new Request(base + 'privacy'), env)).text();
assert.match(about, /Limited Use/); assert(!about.includes('GOCSPX'));
console.log('sign-in service: public client info only, PKCE + exact return required, secret added server-side, errors pass through PASS');
