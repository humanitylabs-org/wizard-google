/* Google for Wizard: setup, accounts and who may use them. Plain DOM, no framework.
 * Back: the open sheet closes first, then the nested view, then the app stays at home. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const view = $('view'), sheet = $('sheet'), status = $('status');
  let csrf = null, home = null, stack = [], polling = null, seq = 0;

  // ---------- small DOM helpers ----------
  function el(tag, text, cls, attrs) {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    return n;
  }
  function button(text, onclick, cls = 'wz-button', attrs) { const b = el('button', text, cls, { type: 'button', ...attrs }); b.onclick = onclick; return b; }
  function link(text, href, cls = 'wz-button secondary') { return el('a', text, cls, { href, target: '_blank', rel: 'noopener noreferrer' }); }
  function row(title, sub, onclick, tile, trailing) {
    const r = el(onclick ? 'button' : 'div', null, 'wz-row', onclick ? { type: 'button' } : {});
    if (onclick) r.onclick = onclick;
    if (tile != null) r.append(el('span', tile, 'wz-tile', { 'aria-hidden': 'true' }));
    const main = el('span', null, 'wz-main'); main.append(el('b', title)); if (sub) main.append(el('small', sub)); r.append(main);
    if (trailing) r.append(trailing); else if (onclick) r.append(el('span', '›', 'chev', { 'aria-hidden': 'true' }));
    return r;
  }
  function section(title, ...children) { const s = el('section', null, 'wz-sec'); if (title) s.append(el('h2', title, 'wz-sec-h')); s.append(...children); return s; }
  function say(text, note) { status.textContent = text || ''; status.classList.toggle('note', !!note); if (note && text) setTimeout(() => { if (status.textContent === text) say(''); }, 3500); }
  function copyRow(label, value) {
    const wrap = el('div', null, 'copy');
    wrap.append(el('small', label), el('code', value || '—'));
    wrap.append(button('Copy', async () => {
      try { await navigator.clipboard.writeText(value); say('Copied.', true); }
      catch { const r = document.createRange(); r.selectNodeContents(wrap.querySelector('code')); getSelection().removeAllRanges(); getSelection().addRange(r); say('Selected. Copy it with your keyboard.', true); }
    }, 'wz-button secondary small'));
    return wrap;
  }
  const opLabel = id => (home.operations.find(o => o.id === id) || { label: id }).label;
  const accountName = id => (home.accounts.find(a => a.id === id) || { email: 'any account' }).email;

  // ---------- API ----------
  async function api(path, body) {
    const init = body === undefined ? { credentials: 'same-origin' }
      : { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body) };
    const r = await fetch(path, init);
    const value = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(value.error?.message || 'Something went wrong.'); Object.assign(e, value.error || {}); throw e; }
    return value;
  }
  async function refresh() { home = await api('api/home'); render(); }

  // ---------- navigation and Back ----------
  function go(next) { stack.push(next); sheetClose(); say(''); render(); view.scrollTop = 0; guardHistory(); }
  function back() { if (sheet.open) { sheetClose(); return true; } if (!stack.length) return false; stack.pop(); render(); return true; }
  window.__wizardCloseOverlay = back;  // the shell calls this for Back: handled = true, at root = false
  addEventListener('message', e => { if (e.source === parent && e.origin === location.origin && e.data?.type === 'wizard:back') back(); });

  // Standalone tab: Back steps inside the app. At home, the first Back only warns.
  const hosted = () => document.documentElement.classList.contains('wz-hosted');
  let armedAt = 0;
  function guardHistory() { if (!hosted() && history.state?.wg !== 'guard') history.pushState({ wg: 'guard' }, ''); }
  addEventListener('popstate', () => {
    if (hosted()) return;
    if (back()) { guardHistory(); return; }
    if (Date.now() - armedAt < 2000) { history.back(); return; }
    armedAt = Date.now(); say('Press back again to exit', true); guardHistory();
  });

  // ---------- render ----------
  function render() {
    if (!home) return;
    const top = stack[stack.length - 1];
    view.replaceChildren();
    if (!top) return renderHome();
    if (top.kind === 'setup') return renderSetup();
    if (top.kind === 'account') return renderAccount(top.id);
    if (top.kind === 'caller') return renderCaller(top.id);
  }
  function crumb(title) {
    const c = el('div', null, 'crumb');
    c.append(button('‹', back, 'back', { 'aria-label': 'Back' }), el('h2', title));
    return c;
  }

  function renderHome() {
    const setup = home.setup;
    if (!setup.complete) {
      const step = setup.steps.find(s => s.id === setup.current);
      const card = el('div', null, 'wz-card hero');
      card.append(el('p', home.accounts.length ? 'Finish setup' : 'Connect Google to your Wizard', 'hero-title'),
        el('p', 'Your own Google sign-in, on your own box. Mail, calendar and files travel only between Google and this box.', 'muted'),
        progress(setup), el('p', `Step ${step.n} of ${setup.count}: ${step.title}`, 'next'),
        button(setup.steps.some(s => s.done) ? 'Continue setup' : 'Start setup', () => go({ kind: 'setup' })));
      view.append(section(null, card));
    }
    for (const w of setup.warnings.filter(w => w.code === 'testing_expiry')) view.append(section(null, warning(w)));
    if (home.requests.length) {
      view.append(section('Waiting for you', ...home.requests.map(requestCard)));
    }
    if (home.write_receipts?.length) {
      const list = el('div', null, 'wz-list');
      for (const r of home.write_receipts) list.append(row(`${r.label} · ${opLabel(r.operations[0])}`,
        `${writeOutcome(r)} · ${accountName(r.account)}`, null, '✦'));
      view.append(section('Recent agent actions', list));
    }
    if (home.client) {
      const list = el('div', null, 'wz-list');
      for (const a of home.accounts) list.append(row(a.email || a.name || 'Google account', accountLine(a), () => go({ kind: 'account', id: a.id }), initial(a.email)));
      list.append(row(home.accounts.length ? 'Connect another account' : 'Connect a Google account', null, () => connectSheet(), '+'));
      view.append(section('Accounts', list));
    }
    if (home.accounts.length) {
      const list = el('div', null, 'wz-list');
      for (const c of home.clients) list.append(row(c.label, callerLine(c), () => go({ kind: 'caller', id: c.id }), c.kind === 'agent' ? '✦' : '▣'));
      list.append(row('Give another app access', null, () => newCallerSheet(), '+'));
      view.append(section('Who can use Google', list));
    }
    if (setup.complete || home.client) view.append(section(null, row('Setup', home.client ? `Project ${home.client.project_id || home.client.project_number}` : 'Not finished', () => go({ kind: 'setup' }), '⚙')));
  }
  function progress(setup) {
    const bar = el('div', null, 'progress', { role: 'img', 'aria-label': `${setup.steps.filter(s => s.done).length} of ${setup.count} steps done` });
    for (const s of setup.steps) bar.append(el('i', null, s.done ? 'done' : s.id === setup.current ? 'now' : ''));
    return bar;
  }
  function initial(text) { return (text || '?').trim().charAt(0).toUpperCase(); }
  function accountLine(a) {
    if (a.status !== 'active') return 'Needs to be connected again';
    const groups = [...new Set(a.allowed.map(op => (home.operations.find(o => o.id === op) || {}).group))];
    return groups.length ? groups.join(', ') : 'Signed in, nothing allowed yet';
  }
  function callerLine(c) {
    if (!c.grants.length) return 'No access';
    return c.grants.map(g => `${g.operations.map(opLabel).join(', ')} · ${accountName(g.account)}`).join('; ');
  }
  function warning(w) {
    const card = el('div', null, 'wz-card warn');
    card.append(el('p', w.message));
    if (w.fix_url) card.append(link('Fix in Google Cloud ↗', w.fix_url));
    return card;
  }
  function requestCard(r) {
    const write = r.kind === 'write';
    const card = el('div', null, `wz-card ask${write ? ' write-approval' : ''}`, { 'data-request-id': r.id });
    card.append(el('p', `${r.label} wants to: ${r.operations.map(opLabel).join(', ')}`, 'ask-title'),
      el('p', r.account ? `On ${accountName(r.account)}` : 'On an account you choose', 'muted'));
    if (write) {
      card.append(el('p', `Just this action · expires ${new Date(r.expires * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`, 'muted'));
      const details = el('dl', null, 'write-payload');
      const labels = { to: 'To', cc: 'Cc', subject: 'Subject', body: 'Message', thread_id: 'Reply thread',
        summary: 'Event', start: 'Starts', end: 'Ends', description: 'Description', location: 'Location', attendees: 'Guests', calendar_id: 'Calendar' };
      if (r.operations[0] === 'calendar.write' && !r.args.calendar_id) details.append(el('dt', 'Calendar'), el('dd', 'primary'));
      for (const [key, value] of Object.entries(r.args)) {
        details.append(el('dt', labels[key] || key), el('dd', Array.isArray(value) ? value.join(', ') : typeof value === 'object' ? JSON.stringify(value) : String(value)));
      }
      card.append(details, el('p', 'Nothing happens until you approve. This does not allow future actions.', 'muted'));
    }
    if (r.reason) card.append(el('p', `“${r.reason}”`, 'reason'));
    const acts = el('div', null, 'actions');
    acts.append(button(write ? 'Approve' : 'Allow', () => r.account || home.accounts.length === 1 ? decide(r, true, r.account || home.accounts[0].id) : chooseAccountSheet(r)),
      button(write ? 'Deny' : 'Decline', () => decide(r, false), 'wz-button secondary'));
    card.append(acts);
    return card;
  }
  function writeOutcome(r) {
    return { executed: 'Executed once', declined: 'Denied · nothing sent', expired: 'Expired · nothing sent',
      executing: 'Executing · do not resubmit', failed: `Not executed: ${r.error?.message || 'Rejected'}`,
      unknown: 'Outcome unknown · check Google before trying again' }[r.status] || r.status;
  }
  async function decide(r, yes, account) {
    const buttons = view.querySelectorAll(`[data-request-id="${r.id}"] button`);
    buttons.forEach(b => { b.disabled = true; });
    try {
      const body = yes ? { account } : {};
      if (r.kind === 'write') body.payload_hash = r.payload_hash;
      const out = await api(`api/requests/${r.id}/${yes ? 'approve' : 'decline'}`, body);
      sheetClose(); say(r.kind === 'write' ? writeOutcome(out.request) : yes ? 'Allowed.' : 'Declined.', true); await refresh();
    } catch (e) { say(e.message); await refresh().catch(() => {}); }
    finally { buttons.forEach(b => { b.disabled = false; }); }
  }

  // ---------- setup ----------
  function renderSetup() {
    const setup = home.setup;
    view.append(crumb('Setup'));
    view.append(el('p', `${setup.count} steps, about five minutes. Steps marked Google Cloud open in a new tab; come back here after each one.`, 'intro'));
    for (const w of setup.warnings) view.append(warning(w));
    const steps = el('ol', null, 'steps');
    // Creating the client and handing it over are one sitting: show both together.
    for (const s of setup.steps) steps.append(stepCard(s, s.id === setup.current, s.id === 'paste' && setup.current === 'client'));
    view.append(steps);
    if (home.client) {
      const foot = el('div', null, 'foot');
      foot.append(link('Your client in Google Cloud ↗', home.client.console_url, 'wz-chip linkish'));
      if (!home.accounts.length) foot.append(button('Use a different client', async () => { try { await api('api/setup/client/forget', {}); await refresh(); } catch (e) { say(e.message); } }, 'wz-chip linkish'));
      view.append(foot);
    }
  }
  function stepCard(s, current, alsoOpen) {
    const li = el('li', null, `step${s.done ? ' done' : ''}${current ? ' current' : ''}`);
    const head = el('button', null, 'step-head', { type: 'button', 'aria-expanded': String(current || alsoOpen) });
    head.append(el('span', s.done ? '✓' : String(s.n), 'num', { 'aria-hidden': 'true' }), el('span', s.title, 'step-title'),
      el('span', s.where === 'google' ? 'Google Cloud' : 'Wizard', 'wz-chip where'));
    const body = el('div', null, 'step-body');
    body.hidden = !(current || alsoOpen);
    head.onclick = () => { body.hidden = !body.hidden; head.setAttribute('aria-expanded', String(!body.hidden)); };
    const list = el('ul', null, 'detail'); for (const d of s.detail) list.append(el('li', d)); body.append(list);
    for (const c of s.copy || []) body.append(copyRow(c.label, c.value));
    const acts = el('div', null, 'actions');
    if (s.id === 'paste') body.append(pasteForm());
    if (s.id === 'connect') acts.append(button(s.done ? 'Connect another account' : 'Connect Google account', () => connectSheet(), s.done ? 'wz-button secondary' : 'wz-button', home.client ? {} : { disabled: '' }));
    if (s.url) acts.append(link(s.done ? 'Open again ↗' : 'Open Google Cloud ↗', s.url, s.done ? 'wz-button secondary' : 'wz-button'));
    if (['project', 'branding', 'publish', 'apis'].includes(s.id) && !(s.done && home.client && s.id !== 'publish' && s.id !== 'apis'))
      acts.append(button(s.done ? 'Not done yet' : 'Done', () => tick(s.id, !s.done), 'wz-button secondary'));
    if (s.detected === 'testing') body.append(el('p', 'Google says this app is still in Testing.', 'detected'));
    if (acts.childElementCount) body.append(acts);
    li.append(head, body);
    return li;
  }
  async function tick(step, done) { try { await api('api/setup/step', { step, done }); await refresh(); } catch (e) { say(e.message); } }
  function pasteForm() {
    const form = el('form', null, 'paste');
    const area = el('textarea', null, 'wz-input area', { rows: '4', placeholder: '{"web":{"client_id":"…", "client_secret":"…"}}', 'aria-label': 'Client JSON', spellcheck: 'false', autocapitalize: 'off' });
    const file = el('input', null, 'wz-sr', { type: 'file', accept: 'application/json,.json', id: 'client-file' });
    const pick = el('label', 'Upload the file', 'wz-button secondary', { for: 'client-file' });
    file.onchange = async () => { if (file.files[0]) { area.value = await file.files[0].text(); form.requestSubmit(); } };
    const result = el('p', null, 'result');
    const save = el('button', home.client ? 'Replace client' : 'Check and save', 'wz-button', { type: 'submit' });
    form.onsubmit = async e => {
      e.preventDefault(); result.textContent = ''; save.disabled = true;
      try {
        const out = await api('api/setup/client', { json: area.value.trim() });
        area.value = '';
        say(out.check.result === 'ok' ? 'Google accepted the client.' : 'Saved. Google could not be asked right now; the first sign-in will check it.', true);
        await refresh();
      } catch (err) {
        result.textContent = err.message;
        if (err.fix_url) result.append(' ', link('Open in Google Cloud ↗', err.fix_url, 'inline'));
      } finally { save.disabled = false; }
    };
    const acts = el('div', null, 'actions'); acts.append(save, pick);
    if (home.client) form.append(el('p', `Saved: ${home.client.client_id.split('-')[0]}-…  ·  secret ${home.client.secret_hint}`, 'muted'));
    form.append(area, file, acts, result);
    return form;
  }

  // ---------- account ----------
  function renderAccount(id) {
    const a = home.accounts.find(x => x.id === id);
    if (!a) { stack.pop(); return render(); }
    view.append(crumb(a.email || 'Google account'));
    if (a.status !== 'active') {
      const card = el('div', null, 'wz-card warn');
      card.append(el('p', 'Google ended this sign-in. Connect it again to keep things working.'), button('Reconnect', () => connectSheet(a)));
      view.append(section(null, card));
    }
    const chips = el('div', null, 'chips');
    for (const op of a.allowed) chips.append(el('span', opLabel(op), 'wz-chip'));
    if (!a.allowed.length) chips.append(el('span', 'Nothing yet', 'muted'));
    view.append(section('Google lets Wizard', chips, padded(button('Allow more…', () => connectSheet(a), 'wz-button secondary'))));
    const apis = el('div', null, 'wz-list');
    for (const [api_, name] of Object.entries(home.apis)) {
      const state = a.apis[api_];
      const trailing = state === 'not_enabled' ? link('Turn on ↗', home.setup.steps.find(s => s.id === 'apis').url, 'wz-chip linkish')
        : el('span', { ok: 'On', not_allowed: 'Not allowed', error: 'Error' }[state] || 'Not checked', `wz-chip state-${state || 'none'}`);
      apis.append(row(name, null, null, null, trailing));
    }
    const check = button('Check now', async () => { check.disabled = true; try { await api(`api/accounts/${a.id}/check`, {}); await refresh(); } catch (e) { say(e.message); } finally { check.disabled = false; } }, 'wz-button secondary');
    view.append(section('Google APIs', apis, padded(check)));
    const who = el('div', null, 'wz-list');
    for (const c of home.clients) {
      const g = c.grants.find(x => x.account === a.id);
      if (g) who.append(row(c.label, g.operations.map(opLabel).join(', '), () => go({ kind: 'caller', id: c.id }), c.kind === 'agent' ? '✦' : '▣'));
    }
    if (!who.childElementCount) who.append(el('p', 'No app or agent can use this account yet.', 'muted pad'));
    view.append(section('Who can use it', who));
    view.append(section(null, button('Disconnect', () => confirmSheet(`Disconnect ${a.email}?`, 'Wizard forgets this sign-in and every app and agent loses access to it. Google is asked to revoke it too.', 'Disconnect', async () => {
      const out = await api(`api/accounts/${a.id}/disconnect`, {}); stack.pop();
      say(out.revoked_at_google ? 'Disconnected.' : 'Disconnected here. Remove Wizard at myaccount.google.com/connections too.', true); await refresh();
    }), 'wz-button secondary danger')));
  }

  // ---------- one app or agent ----------
  function renderCaller(id) {
    const c = home.clients.find(x => x.id === id) || { id, label: id.split(':')[1], kind: id.split(':')[0], grants: [], keys: [] };
    view.append(crumb(c.label));
    view.append(el('p', c.kind === 'agent' ? 'Tick what this agent may use on each account. Reads run with this grant; every write still asks you to Approve or Deny the exact action here. Agents without their own identity share Main agent grants.' : 'Tick what this app may do on each account. These are standing grants, including writes. Wizard makes every Google call for it; the app never sees a Google password or token.', 'intro'));
    for (const a of home.accounts) {
      const g = c.grants.find(x => x.account === a.id);
      const list = el('div', null, 'wz-list');
      for (const op of home.operations) {
        const allowedByGoogle = a.allowed.includes(op.id);
        const on = !!g && g.operations.includes(op.id);
        const tog = el('button', null, 'wz-row toggle', { type: 'button', 'aria-pressed': String(on) });
        tog.disabled = !allowedByGoogle && !on;
        const main = el('span', null, 'wz-main'); main.append(el('b', `${op.group}: ${op.label}`));
        if (!allowedByGoogle) main.append(el('small', 'Allow it in Google first (account › Allow more)'));
        else if (op.writes) main.append(el('small', c.kind === 'agent' ? 'Asks for approval every time' : 'Makes changes · standing grant'));
        const check = el('span', null, 'wz-check', { 'aria-hidden': 'true' }); check.innerHTML = '<i><svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></svg></i>';
        tog.append(check, main);
        tog.onclick = async () => {
          const next = new Set(g ? g.operations : []); on ? next.delete(op.id) : next.add(op.id);
          tog.disabled = true;
          try { await api('api/grants', { client: c.id, account: a.id, operations: [...next] }); await refresh(); } catch (e) { say(e.message); tog.disabled = false; }
        };
        list.append(tog);
      }
      view.append(section(a.email, list));
    }
    if (c.kind === 'app') {
      const keys = el('div', null, 'wz-list');
      for (const k of c.keys) keys.append(row('Access key', `Made ${new Date(k.created * 1000).toLocaleDateString()}`, null, '⚿',
        button('Revoke', async () => { try { await api(`api/keys/${k.id}/revoke`, {}); await refresh(); } catch (e) { say(e.message); } }, 'wz-chip linkish')));
      keys.append(el('p', 'Apps on Wizard grants need no key. An older app can use a key instead.', 'muted pad'));
      view.append(section('Access key', keys, padded(button('Make a key', makeKey(c), 'wz-button secondary'))));
    }
  }
  function makeKey(c) {
    return async () => {
      try {
        const out = await api('api/keys', { client: c.id });
        openSheet(`Key for ${c.label}`, el('p', 'Shown once. Put it in the app\'s settings as its Google key.', 'muted'), copyRow('Access key', out.key),
          actionsOf(button('Done', () => { sheetClose(); refresh(); })));
      } catch (e) { say(e.message); }
    };
  }

  // ---------- sheets ----------
  function openSheet(title, ...children) {
    sheet.replaceChildren(el('h2', title, null, { id: 'sheet-title' }), ...children);
    if (!sheet.open) sheet.showModal();
  }
  function sheetClose() { if (sheet.open) sheet.close(); stopPolling(); }
  sheet.addEventListener('cancel', e => { e.preventDefault(); back(); });
  sheet.addEventListener('close', stopPolling);
  function actionsOf(...buttons) { const a = el('div', null, 'actions'); a.append(...buttons); return a; }
  function padded(...buttons) { const a = actionsOf(...buttons); a.classList.add('pad'); return a; }

  function connectSheet(account) {
    const chosen = new Set(account ? account.allowed : ['gmail.search', 'gmail.read', 'calendar.read', 'drive.search']);
    const groups = {};
    for (const op of home.operations) (groups[op.group] ||= []).push(op);
    const list = el('div', null, 'wz-list ops');
    for (const [group, ops] of Object.entries(groups)) {
      list.append(el('h3', group, 'group'));
      for (const op of ops) {
        const t = el('button', null, 'wz-row toggle', { type: 'button', 'aria-pressed': String(chosen.has(op.id)) });
        const main = el('span', null, 'wz-main'); main.append(el('b', op.label)); if (op.writes) main.append(el('small', 'Makes changes'));
        const check = el('span', null, 'wz-check', { 'aria-hidden': 'true' }); check.innerHTML = '<i><svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></svg></i>';
        t.append(check, main);
        t.onclick = () => { chosen.has(op.id) ? chosen.delete(op.id) : chosen.add(op.id); t.setAttribute('aria-pressed', String(chosen.has(op.id))); };
        list.append(t);
      }
    }
    const go_ = button('Continue to Google', () => startSignIn(account, [...chosen]));
    openSheet(account ? `Allow more on ${account.email}` : 'What may Wizard use?',
      el('p', 'Google asks you to confirm each item. Apps and agents still need your permission here before they can use any of it.', 'muted'),
      list, actionsOf(go_, button('Cancel', back, 'wz-button secondary')));
  }
  async function startSignIn(account, operations) {
    // Open the tab inside the click so it is not blocked, then point it at Google.
    const tab = window.open('', '_blank');
    try {
      const out = await api('api/oauth/start', { account: account ? account.id : null, operations });
      if (tab) { tab.opener = null; tab.location.href = out.authorization_url; }
      // Google cannot open inside a frame. If the tab was blocked, this link opens it with one more tap.
      openSheet('Finish in the Google tab', el('p', 'Choose your account and allow access. If Google says the app is not verified, choose Advanced, then “Go to Wizard”. It is your own app.', 'muted'),
        el('div', null, 'spinner', { 'aria-hidden': 'true' }),
        actionsOf(link(tab ? 'Open Google again ↗' : 'Open Google ↗', out.authorization_url, tab ? 'wz-button secondary' : 'wz-button'),
          button('Cancel', async () => { await api('api/oauth/cancel', {}).catch(() => {}); sheetClose(); }, 'wz-button secondary')));
      startPolling();
    } catch (e) { if (tab) tab.close(); sheetClose(); say(e.message); }
  }
  function startPolling() {
    stopPolling();
    const mine = ++seq;
    polling = setInterval(async () => {
      try {
        const s = await api('api/oauth/status');
        if (mine !== seq) return;
        if (s.status === 'connected') {
          stopPolling(); sheetClose(); say(`Connected ${s.result.email}.`, true);
          await api(`api/accounts/${s.result.account}/check`, {}).catch(() => {});
          await refresh();
        } else if (s.status === 'failed' || s.status === 'cancelled') { stopPolling(); sheetClose(); if (s.status === 'failed') say(s.result.message); }
      } catch { /* keep waiting */ }
    }, 1500);
  }
  function stopPolling() { if (polling) { clearInterval(polling); polling = null; } }
  function chooseAccountSheet(r) {
    openSheet(`${r.label}: which account?`, ...home.accounts.map(a => row(a.email, null, () => decide(r, true, a.id), initial(a.email))),
      actionsOf(button('Cancel', back, 'wz-button secondary')));
  }
  function newCallerSheet() {
    const input = el('input', null, 'wz-input', { placeholder: 'App id, e.g. music', 'aria-label': 'App id', autocapitalize: 'off', spellcheck: 'false' });
    const form = el('form'); form.append(input);
    form.onsubmit = e => { e.preventDefault(); const id = input.value.trim().toLowerCase(); if (/^[a-z0-9][a-z0-9_-]{0,62}$/.test(id)) { sheetClose(); go({ kind: 'caller', id: 'app:' + id }); } else say('Use the app id from its Wizard listing.'); };
    openSheet('Give an app access', el('p', 'Apps usually ask by themselves; their request appears on the home screen. To set it up ahead, type the app id.', 'muted'), form,
      actionsOf(button('Next', () => form.requestSubmit()), button('Cancel', back, 'wz-button secondary')));
  }
  function confirmSheet(title, text, action, run) {
    openSheet(title, el('p', text, 'muted'), actionsOf(button(action, async () => { try { await run(); sheetClose(); } catch (e) { say(e.message); } }, 'wz-button danger'),
      button('Cancel', back, 'wz-button secondary')));
  }

  // ---------- start ----------
  addEventListener('focus', () => { if (csrf && !sheet.open) refresh().catch(() => {}); });
  // An agent can ask while this screen is already open. Update only changed cards, not a
  // focused form/sheet; no polling of the write tool itself (which would propose again).
  let checkingRequests = false;
  setInterval(async () => {
    if (!csrf || !home || document.hidden || sheet.open || stack.length || checkingRequests) return;
    checkingRequests = true;
    try {
      const next = await api('api/home');
      if (!sheet.open && !stack.length && JSON.stringify([home.requests, home.write_receipts]) !== JSON.stringify([next.requests, next.write_receipts])) {
        home = next; render();
      }
    } catch { /* focus or next poll recovers */ }
    finally { checkingRequests = false; }
  }, 5000);
  fetch('api/session', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Wizard-Request': 'bootstrap' }, body: '{}' })
    .then(async r => { const v = await r.json(); if (!r.ok) throw new Error(v.error?.message || 'Unavailable.'); csrf = v.csrf; await refresh(); guardHistory(); })
    .catch(e => { view.replaceChildren(el('div', null, 'wz-empty')); view.firstChild.append(el('strong', 'Google is not available'), el('p', e.message)); });
})();
