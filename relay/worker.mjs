// Wizard's Google sign-in service, and the optional return page for own-client boxes without https.
//
// Stateless. No storage, no logs, no analytics. Three jobs:
//   GET  callback    Google's fixed return address. Shows "Return to your Wizard at <host>"; the owner clicks.
//                    Nothing redirects automatically.
//   GET  v1/client   The shared "Wizard" client ID and this return address (both public).
//   POST v1/token    Adds the shared client secret to a box's code exchange or token refresh and passes
//                    Google's answer straight back. A code needs the box's PKCE verifier, so this service
//                    cannot use one on its own; refresh tokens pass through and are never kept.
// Secrets (wrangler secret put): GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET. Optional var GOOGLE_VERIFIED = "true".
// Without them, v1/* answers 503 and boxes fall back to the owner's own Google client.
const PREFIX = '/wizard-google/auth/';
const TOKEN_URL = 'https://oauth2.googleapis.com/token';
const headers = { 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'" };
const escape = s => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const style = 'body{margin:0;font:16px/1.5 system-ui,sans-serif;background:#05030c;color:#f3efff}main{max-width:32rem;margin:10vh auto;padding:0 24px}a.go{display:inline-block;margin-top:8px;padding:14px 22px;border-radius:14px;background:#b99bff;color:#05030c;font-weight:650;text-decoration:none}a{color:#cbb6ff}strong{overflow-wrap:anywhere}h2{font-size:18px;margin-top:28px}';
const CLIENT_ID = /^\d{6,20}-[a-z0-9]{8,64}\.apps\.googleusercontent\.com$/;

export function parseState(state) {
  if (typeof state !== 'string' || state.length > 1024 || !/^wg1\.[A-Za-z0-9_-]+$/.test(state)) throw Error('state');
  const data = JSON.parse(atob(state.slice(4).replace(/-/g, '+').replace(/_/g, '/')));
  if (Object.keys(data).sort().join(',') !== 'n,r' || !/^[A-Za-z0-9_-]{43}$/.test(data.n) || typeof data.r !== 'string') throw Error('state');
  const target = new URL(data.r);
  if (target.protocol !== 'https:' || target.username || target.password || target.search || target.hash || target.port && !/^\d{2,5}$/.test(target.port)
      || !/^(\/[a-z0-9-]{1,64})*\/oauth\/callback$/.test(target.pathname) || !/^[a-z0-9.-]+\.[a-z]{2,}$/.test(target.hostname) || target.href !== data.r) throw Error('target');
  return target;
}
function page(status, body) {
  return new Response(`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Wizard · Google</title><style>${style}</style><main><h1>Wizard · Google</h1>${body}</main></html>`,
    { status, headers: { ...headers, 'Content-Type': 'text/html; charset=utf-8' } });
}
function json(status, value) {
  return new Response(JSON.stringify(value), { status, headers: { ...headers, 'Content-Type': 'application/json' } });
}
const configured = env => !!env && CLIENT_ID.test(env.GOOGLE_CLIENT_ID || '') && typeof env.GOOGLE_CLIENT_SECRET === 'string' && env.GOOGLE_CLIENT_SECRET.length >= 8;
const callbackOf = url => `${url.origin}${PREFIX}callback`;

export const ABOUT = `<p>This page lets a Wizard box sign in to Google with the shared <strong>Wizard</strong> Google app, so its owner does not need a Google Cloud project of their own.</p>
<h2>What happens to your data</h2>
<p>Your Google sign-in is stored only on your own Wizard box, encrypted. Your mail, calendar, files and contacts travel only between Google and your box. Only your own agent and the apps you allow can use them, and only for the Google services you switch on.</p>
<p>This service keeps nothing: no database, no request logs, no analytics. It shows the return link after you sign in, and adds the Wizard app's client secret when your box exchanges or renews its sign-in. It cannot use a sign-in code on its own: that needs a one-time secret only your box holds.</p>
<p>You can remove Wizard's access at any time with Disconnect in the Google app inside Wizard, or at <a href="https://myaccount.google.com/connections">myaccount.google.com/connections</a>.</p>
<p>Wizard's use and transfer of information received from Google APIs adheres to the <a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>, including the Limited Use requirements. Google data is used only to do what you ask your own agent to do. It is never sold, never used for advertising and never used to train AI models.</p>
<p>Source: <a href="https://github.com/humanitylabs-org/wizard-google">github.com/humanitylabs-org/wizard-google</a>.</p>`;

async function token(request, env, url) {
  if (!configured(env)) return json(503, { error: 'temporarily_unavailable', error_description: 'Quick sign-in is not set up yet.' });
  if ((request.headers.get('Content-Type') || '').split(';')[0].trim() !== 'application/x-www-form-urlencoded') return json(415, { error: 'invalid_request' });
  const raw = await request.text();
  if (raw.length > 8192) return json(413, { error: 'invalid_request' });
  const form = new URLSearchParams(raw);
  for (const key of form.keys()) if (form.getAll(key).length > 1) return json(400, { error: 'invalid_request' });
  if (form.get('client_id') !== env.GOOGLE_CLIENT_ID) return json(401, { error: 'invalid_client', error_description: 'Unknown client.' });
  const out = new URLSearchParams({ client_id: env.GOOGLE_CLIENT_ID, client_secret: env.GOOGLE_CLIENT_SECRET });
  const grant = form.get('grant_type');
  if (grant === 'authorization_code') {
    const code = form.get('code') || '', verifier = form.get('code_verifier') || '';
    // PKCE is mandatory, and the code must have come back to this exact return page.
    if (!/^[\x21-\x7e]{1,4096}$/.test(code) || !/^[A-Za-z0-9._~-]{43,128}$/.test(verifier) || form.get('redirect_uri') !== callbackOf(url))
      return json(400, { error: 'invalid_request' });
    out.set('grant_type', grant); out.set('code', code); out.set('code_verifier', verifier); out.set('redirect_uri', callbackOf(url));
  } else if (grant === 'refresh_token') {
    const refresh = form.get('refresh_token') || '';
    if (!/^[\x21-\x7e]{10,2048}$/.test(refresh)) return json(400, { error: 'invalid_request' });
    out.set('grant_type', grant); out.set('refresh_token', refresh);
  } else return json(400, { error: 'unsupported_grant_type' });
  const answer = await fetch(TOKEN_URL, { method: 'POST', body: out, redirect: 'manual',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json', 'User-Agent': 'wizard-google-signin' } });
  const body = await answer.text();
  return new Response(body.slice(0, 65536), { status: answer.status, headers: { ...headers, 'Content-Type': 'application/json' } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === PREFIX + 'v1/token') return request.method === 'POST' ? token(request, env, url) : json(405, { error: 'invalid_request' });
    if (request.method !== 'GET') return page(405, '<p>Not available.</p>');
    if (url.pathname === PREFIX + 'v1/client') {
      if (!configured(env)) return json(503, { error: 'not_configured' });
      return json(200, { client_id: env.GOOGLE_CLIENT_ID, redirect_uri: callbackOf(url), name: 'Wizard', verified: env.GOOGLE_VERIFIED === 'true' });
    }
    if (url.pathname === PREFIX || url.pathname === PREFIX + 'privacy') return page(200, ABOUT);
    if (url.pathname !== PREFIX + 'callback') return page(404, '<p>Not found.</p>');
    try {
      const allowed = ['state', 'code', 'error', 'scope', 'authuser', 'prompt', 'hd', 'iss', 'error_description', 'error_uri'];
      if ([...url.searchParams.keys()].some(k => !allowed.includes(k)) || ['state', 'code', 'error'].some(k => url.searchParams.getAll(k).length > 1)) throw Error('query');
      const state = url.searchParams.get('state'), target = parseState(state);
      const code = url.searchParams.get('code'), error = url.searchParams.get('error');
      if (Boolean(code) === Boolean(error) || code && !/^[\x21-\x7e]{1,4096}$/.test(code) || error && !/^[a-z_]{1,80}$/.test(error)) throw Error('response');
      target.searchParams.set('state', state); target.searchParams.set(code ? 'code' : 'error', code || error);
      return page(200, `<p>Return to your Wizard at <strong>${escape(target.host)}</strong> to finish.</p><p>Continue only if this is your own Wizard address.</p><a class="go" rel="noreferrer" href="${escape(target.href)}">Return to Wizard</a>`);
    } catch { return page(400, '<p>This sign-in return is not valid. Start again from the Google app in your Wizard.</p>'); }
  },
};
