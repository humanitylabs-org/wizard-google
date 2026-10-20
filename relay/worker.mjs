// Optional stateless return page for boxes that have no https address of their own.
// No storage, no logs, no outbound fetch, no token handling. It sees the sign-in code and the box address,
// and cannot use the code: the exchange needs the owner's client secret and the PKCE verifier, both held
// only by the box. The owner must click to continue; nothing redirects automatically.
const PREFIX = '/wizard-google/auth/';
const headers = { 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'" };
const escape = s => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const style = 'body{margin:0;font:16px/1.45 system-ui,sans-serif;background:#05030c;color:#f3efff}main{max-width:30rem;margin:12vh auto;padding:0 24px}a{display:inline-block;margin-top:8px;padding:12px 20px;border-radius:14px;background:#b99bff;color:#05030c;font-weight:600;text-decoration:none}strong{overflow-wrap:anywhere}';

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
export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method !== 'GET') return page(405, '<p>Not available.</p>');
    if (url.pathname === PREFIX) return page(200, '<p>Sign-in return page for Wizard\'s Google app. It stores nothing and cannot use what passes through it: your box finishes the sign-in with your own Google client.</p>');
    if (url.pathname !== PREFIX + 'callback') return page(404, '<p>Not found.</p>');
    try {
      const allowed = ['state', 'code', 'error', 'scope', 'authuser', 'prompt', 'hd', 'iss', 'error_description', 'error_uri'];
      if ([...url.searchParams.keys()].some(k => !allowed.includes(k)) || ['state', 'code', 'error'].some(k => url.searchParams.getAll(k).length > 1)) throw Error('query');
      const state = url.searchParams.get('state'), target = parseState(state);
      const code = url.searchParams.get('code'), error = url.searchParams.get('error');
      if (Boolean(code) === Boolean(error) || code && !/^[\x21-\x7e]{1,4096}$/.test(code) || error && !/^[a-z_]{1,80}$/.test(error)) throw Error('response');
      target.searchParams.set('state', state); target.searchParams.set(code ? 'code' : 'error', code || error);
      return page(200, `<p>Return to your Wizard at <strong>${escape(target.host)}</strong>.</p><p>Continue only if this is your own Wizard address.</p><a rel="noreferrer" href="${escape(target.href)}">Return to Wizard</a>`);
    } catch { return page(400, '<p>This sign-in return is not valid. Start again from the Google app in your Wizard.</p>'); }
  },
};
