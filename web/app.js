/* Google for Wizard: connect Google accounts once, then switch on what your agent may use.
 * Connector pattern (APP-DESIGN-RULES rule 5): status card, accounts, one primary action, details collapsed.
 * Plain DOM, no framework. Back: an open sheet closes first (sheet.js), then a nested page, then the app stays home. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const view = $('view'), sheet = $('sheet'), status = $('status');
  let csrf = null, home = null, stack = [], polling = null, seq = 0, busy = false, queue = [];
  const CHECK = '<i><svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></svg></i>';

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
    if (tile != null) r.append(typeof tile === 'string' ? el('span', tile, 'wz-tile', { 'aria-hidden': 'true' }) : tile);
    const main = el('span', null, 'wz-main'); main.append(el('b', title)); if (sub) main.append(el('small', sub)); r.append(main);
    if (trailing) r.append(trailing); else if (onclick) r.append(el('span', '›', 'chev', { 'aria-hidden': 'true' }));
    return r;
  }
  function section(title, ...children) { const s = el('section', null, 'wz-sec'); if (title) s.append(el('h2', title, 'wz-sec-h')); s.append(...children); return s; }
  function pill(kind, text) { return el('span', text, `wz-pill ${kind}`); }
  function say(text, note) { status.textContent = text || ''; status.classList.toggle('note', !!note); if (note && text) setTimeout(() => { if (status.textContent === text) say(''); }, 3500); }
  function toggle(on, title, sub, onclick, disabled) {
    const t = el('button', null, 'wz-row toggle', { type: 'button', 'aria-pressed': String(on) });
    const check = el('span', null, 'wz-check', { 'aria-hidden': 'true' }); check.innerHTML = CHECK;
    const main = el('span', null, 'wz-main'); main.append(el('b', title)); if (sub) main.append(el('small', sub));
    t.append(check, main); t.onclick = onclick; if (disabled) t.disabled = true;
    return t;
  }
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
  const accountName = id => { const a = home.accounts.find(x => x.id === id); return a ? (a.label ? `${a.label} (${a.email})` : a.email) : 'any account'; };
  const initial = text => (text || '?').trim().charAt(0).toUpperCase();

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

  // ---------- navigation and Back (domain only; the shell owns root/exit) ----------
  function go(next) { stack.push(next); sheetClose(); say(''); render(); view.scrollTop = 0; }
  function back() { if (!stack.length) return false; stack.pop(); render(); return true; }
  if (window.WizardApp?.version === 1) {
    WizardApp.ready({ name: 'wizard-google', version: '0.3.5' });
    WizardApp.back.register({ canGoBack: () => stack.length > 0, back });
  }

  // ---------- render ----------
  function render() {
    if (!home) return;
    const top = stack[stack.length - 1];
    view.replaceChildren();
    if (!top) return renderHome();
    if (top.kind === 'setup') return renderSetup();
    if (top.kind === 'quick') return renderQuick();
    if (top.kind === 'caller') return renderCaller(top.id);
  }
  function crumb(title) {
    const c = el('div', null, 'crumb');
    c.append(button('‹', back, 'back', { 'aria-label': 'Back' }), el('h2', title));
    return c;
  }

  const allAccounts = () => [...home.accounts, ...home.quick.accounts];
  function overall() {
    const all = allAccounts(), active = all.filter(a => a.status === 'active');
    if (!all.length) return ['off', 'Not connected'];
    if (active.length < all.length) return ['warn', 'Needs attention'];
    return ['ok', 'Connected'];
  }
  function servicesLine(a) {
    const on = a.services.filter(s => s.on).map(s => s.label);
    return on.length ? on.join(' · ') : 'Nothing switched on';
  }
  const ready = () => home.sign_in.mode === 'wizard' || (!home.client && home.sign_in.wizard_available) || (home.client && home.setup.steps.every(s => s.id === 'connect' || s.done));

  function renderHome() {
    // 1. Status card
    const [kind, word] = overall();
    const card = el('div', null, 'wz-conn');
    const tile = el('span', null, 'wz-tile sigil', { 'aria-hidden': 'true' }); tile.append(el('img', null, null, { src: 'icon.png', alt: '' }));
    const main = el('span', null, 'wz-main');
    const head = el('span', null, 'conn-head'); head.append(el('b', 'Google'), pill(kind, word));
    const all = allAccounts();
    const on = new Set(all.flatMap(a => a.services.filter(s => s.on).map(s => s.label)));
    main.append(head, el('small', all.length
      ? (on.size ? `Your agent can use ${[...on].join(', ')} on ${all.length === 1 ? 'your account' : `${all.length} accounts`}.` : 'Switch on what your agent may use.')
      : 'Sign in once. Then choose what your agent may use: Gmail, Calendar, Drive, Contacts, YouTube.'));
    card.append(tile, main);
    view.append(section(null, card));

    for (const w of home.setup.warnings.filter(w => w.code === 'testing_expiry')) view.append(section(null, warning(w)));
    if (home.requests.length) view.append(section('Waiting for you', ...home.requests.map(requestCard)));

    // 2. Accounts, each with its service switches
    if (all.length) {
      const grid = el('div', null, 'wz-grid accounts');
      for (const a of home.accounts) grid.append(accountCard(a));
      for (const a of home.quick.accounts) grid.append(quickCard(a));
      view.append(section('Accounts', grid));
    }

    // 3. One primary action: choose how to connect (Quick connect or your own Google client)
    const acts = el('div', null, 'primary');
    const setup = home.setup, step = setup.steps.find(s => s.id === setup.current);
    if (!ready() && step && setup.steps.some(s => s.done)) acts.append(el('p', `Your own client: step ${step.n} of ${setup.count}, ${step.title}`, 'muted'));
    acts.append(button(all.length ? 'Add another Google account' : 'Connect Google', () => waySheet()));
    view.append(acts);

    // 4. Details, collapsed
    const more = el('details', null, 'wz-more');
    more.append(el('summary', 'More'));
    const list = el('div', null, 'wz-list');
    const mode = home.sign_in.mode || (home.sign_in.wizard_available ? 'wizard' : 'own');
    list.append(row('Quick connect', home.quick.keys.length ? `Through Composio · ${home.quick.keys.length === 1 ? '1 key' : `${home.quick.keys.length} keys`}` : 'Through Composio · not set up',
      () => go({ kind: 'quick' }), '✦'));
    list.append(row('Sign-in', mode === 'wizard' ? 'Wizard\'s Google app' : (home.client ? `Your own Google client · ${home.client.project_id || home.client.project_number}` : 'Your own Google client (not set up)'),
      mode === 'own' || !home.accounts.length ? () => signInSheet() : null, '⚙'));
    for (const c of home.clients.filter(c => c.kind === 'app')) list.append(row(c.label, callerLine(c), () => go({ kind: 'caller', id: c.id }), '▣'));
    if (home.accounts.length) list.append(row('Give an app access', 'For other Wizard apps that use Google', () => newCallerSheet(), '+'));
    more.append(list);
    if (home.audit.length) {
      const log = el('div', null, 'wz-list');
      for (const item of home.audit.slice(0, 12)) log.append(row(`${item.operation}`, `${new Date(item.at * 1000).toLocaleString()} · ${accountName(item.account)} · ${item.outcome}`, null, null));
      more.append(el('h3', 'Recent use', 'group'), log);
    }
    more.append(el('p', 'Your Google sign-ins stay encrypted on this box. Your agent works through gog (gogcli) inside this app and never sees a password or token.', 'muted pad'));
    view.append(section(null, more));
  }

  function accountCard(a) {
    const box = el('div', null, 'wz-list account');
    const tile = el('span', initial(a.label || a.email), 'wz-tile', { 'aria-hidden': 'true' });
    box.append(row(a.label || a.email || 'Google account', a.label ? a.email : (a.status === 'active' ? servicesLine(a) : null), () => accountSheet(a), tile,
      a.status === 'active' ? pill('ok', 'Connected') : pill('warn', 'Sign in again')));
    if (a.status !== 'active') {
      box.append(row('Google ended this sign-in', 'Sign in again to keep things working.', () => startSignIn(a, a.services.filter(s => s.on).map(s => s.id)), '!'));
      return box;
    }
    for (const s of a.services) {
      box.append(toggle(s.on, s.label, s.on ? s.what : (s.granted ? 'Off' : 'Off · Google asks you once'), async e => {
        const t = e.currentTarget; if (busy) return; busy = true; t.disabled = true;
        try { await api(`api/accounts/${a.id}/service`, { service: s.id, on: !s.on }); await refresh(); say(`${s.label} ${s.on ? 'off' : 'on'} for ${a.label || a.email}.`, true); }
        catch (err) {
          if (err.code === 'consent_needed') { busy = false; return startSignIn(a, [...a.services.filter(x => x.on).map(x => x.id), s.id]); }
          say(err.message); t.disabled = false;
        } finally { busy = false; }
      }));
    }
    return box;
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
    const card = el('div', null, 'wz-card ask');
    card.append(el('p', `${r.label} wants to: ${r.operations.map(opLabel).join(', ')}`, 'ask-title'),
      el('p', r.account ? `On ${accountName(r.account)}` : 'On an account you choose', 'muted'));
    if (r.reason) card.append(el('p', `“${r.reason}”`, 'reason'));
    const acts = el('div', null, 'actions');
    acts.append(button('Allow', () => r.account || home.accounts.length === 1 ? decide(r, true, r.account || home.accounts[0].id) : chooseAccountSheet(r)),
      button('Decline', () => decide(r, false), 'wz-button secondary'));
    card.append(acts);
    return card;
  }
  async function decide(r, yes, account) {
    try { await api(`api/requests/${r.id}/${yes ? 'approve' : 'decline'}`, yes ? { account } : {}); sheetClose(); say(yes ? 'Allowed.' : 'Declined.', true); await refresh(); }
    catch (e) { say(e.message); }
  }

  // ---------- own Google client setup (advanced) ----------
  function renderSetup() {
    const setup = home.setup;
    view.append(crumb('Your own Google client'));
    view.append(el('p', `${setup.count} steps, about ten minutes. Steps marked Google Cloud open in a new tab; come back here after each one.`, 'intro'));
    for (const w of setup.warnings) view.append(warning(w));
    const steps = el('ol', null, 'steps');
    for (const s of setup.steps) steps.append(stepCard(s, s.id === setup.current, s.id === 'paste' && setup.current === 'client'));
    view.append(steps);
    if (home.client && home.client.mode === 'own') {
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
    const save = el('button', home.client && home.client.mode === 'own' ? 'Replace client' : 'Check and save', 'wz-button', { type: 'submit' });
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
    if (home.client && home.client.mode === 'own') form.append(el('p', `Saved: ${home.client.client_id.split('-')[0]}-…  ·  secret ${home.client.secret_hint}`, 'muted'));
    form.append(area, file, acts, result);
    return form;
  }

  // ---------- choose a way to connect ----------
  function waySheet() {
    const list = el('div', null, 'wz-list');
    list.append(row('Quick connect', 'About a minute. Gmail and Calendar, through Composio.', () => { sheetClose(); go({ kind: 'quick' }); }, '✦',
      home.quick.ready ? pill('ok', 'Ready') : null));
    const own = ready() ? (home.client ? 'Ready. Gmail, Calendar, Drive, Contacts, YouTube.' : 'Quickest when available.')
      : 'About ten minutes in Google Cloud. Nobody else is involved.';
    list.append(row('Your own Google client', own, () => { sheetClose(); if (ready()) connectSheet(); else go({ kind: 'setup' }); }, '⚙',
      ready() && home.client ? pill('ok', 'Ready') : null));
    openSheet(allAccounts().length ? 'Add a Google account' : 'Connect Google', list,
      el('p', 'You can use both, and switch any time.', 'muted'));
  }

  // ---------- Quick connect (Composio) ----------
  function renderQuick() {
    const q = home.quick;
    view.append(crumb('Quick connect'));
    view.append(el('p', q.note, 'quick-note'));
    const keys = el('section', null, 'wz-sec');
    keys.append(el('h3', q.keys.length ? 'Your Composio key' + (q.keys.length > 1 ? 's' : '') : 'Composio account', 'sec-title'));
    if (!q.keys.length) {
      keys.append(el('p', 'Quick connect asks you to sign in to Composio (or create a free account) in the same window. The key is saved by itself.', 'sec-note'));
      keys.append(padded(button('Paste a key instead', () => openSheet('Paste a Composio key', el('p', q.key_help, 'muted'), keyForm(true)), 'wz-button secondary')));
    } else {
      const list = el('div', null, 'wz-list');
      for (const k of q.keys) list.append(keyRow(k));
      keys.append(list, padded(button('Add another key', () => openSheet('Add a Composio key', el('p', `Several keys are fine, for example a second free account. ${q.key_help}`, 'muted'), keyForm(true)), 'wz-button secondary')));
    }
    view.append(keys);
    const go2 = el('section', null, 'wz-sec');
    go2.append(el('h3', 'Connect', 'sec-title'));
    go2.append(el('p', 'One window: sign in to Google (and Composio the first time). Gmail and Calendar each ask once.', 'sec-note'));
    const acts = padded(button('Quick connect', () => quickSheet(null), 'wz-button'));
    go2.append(acts);
    if (q.accounts.length) go2.append(el('p', `Connected: ${q.accounts.map(a => a.email || 'Google account').join(', ')}.`, 'sec-note'));
    view.append(go2);
  }
  function keyRow(k) {
    const r = el('div', null, 'key-row');
    r.append(el('span', initial(k.label), 'wz-tile', { 'aria-hidden': 'true' }));
    const main = el('span', null, 'wz-main');
    main.append(el('b', `${k.label} · ${k.hint}`));
    const used = `${k.calls_this_month.toLocaleString()} calls this month (about ${k.free_calls.toLocaleString()} free)`;
    main.append(el('small', k.status === 'ok' ? used : (k.error || 'Not checked yet') + ' · ' + used));
    r.append(main, pill(k.status === 'ok' ? 'ok' : (k.status === 'unchecked' ? 'off' : 'warn'), k.status === 'ok' ? 'Working' : (k.status === 'unchecked' ? 'Not checked' : 'Not working')));
    const rm = button('Remove', async () => {
      if (rm.dataset.armed !== '1') { rm.dataset.armed = '1'; rm.textContent = 'Remove?'; return; }
      try { await api(`api/quick/keys/${k.id}/remove`, {}); await refresh(); say('Key removed.', true); } catch (e) { say(e.message); rm.dataset.armed = ''; rm.textContent = 'Remove'; }
    }, 'wz-button secondary small rm');
    r.append(rm);
    return r;
  }
  function keyForm(inSheet) {
    const form = el('form', null, 'key-form', { autocomplete: 'off' });
    const id = inSheet ? 'quick-key-2' : 'quick-key';
    const input = el('input', null, 'wz-field', { id, type: 'password', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', 'aria-describedby': id + '-msg' });
    const label = el('label', 'Composio API key', null, { for: id });
    const msg = el('p', 'Saved on this box only. Wizard checks it with Composio first.', 'hint', { id: id + '-msg' });
    const save = el('button', 'Save key', 'wz-button', { type: 'submit' });
    form.onsubmit = async e => {
      e.preventDefault(); save.disabled = true; input.removeAttribute('aria-invalid'); msg.classList.remove('bad'); msg.textContent = 'Checking with Composio…';
      try {
        await api('api/quick/keys', { key: input.value.trim() });
        input.value = ''; if (inSheet) sheetClose(); await refresh(); say('Composio accepted the key.', true);
      } catch (err) {
        input.setAttribute('aria-invalid', 'true'); msg.classList.add('bad'); msg.textContent = err.message;
      } finally { save.disabled = false; }
    };
    const acts = el('div', null, 'actions'); acts.append(save);
    form.append(label, input, msg, acts);
    return form;
  }
  function quickSheet(account) {
    const chosen = new Set(account ? [] : ['gmail', 'calendar']);
    const list = el('div', null, 'wz-list');
    for (const s of home.quick.services) {
      const t = toggle(chosen.has(s.id), s.label, s.what, () => { chosen.has(s.id) ? chosen.delete(s.id) : chosen.add(s.id); t.setAttribute('aria-pressed', String(chosen.has(s.id))); });
      list.append(t);
    }
    openSheet('Quick connect', el('p', 'What may your agent use on this account? Each one signs in once on Composio\'s page.', 'muted'), list,
      actionsOf(button('Continue', () => { const picked = home.quick.services.map(s => s.id).filter(id => chosen.has(id)); queue = []; if (picked.length) runFlow(picked, account); })));
  }
  // One popup does it all: Composio sign-in when no key works yet, then each Google sign-in (contract doc, "Connecting from another app").
  function startQuick(service, account) { return runFlow([service, ...queue.splice(0)], account); }
  async function runFlow(services, account) {
    stopPolling();
    // Inside Wizard, a window for this address would open as a Wizard sheet, where Google refuses to sign in. The window
    // goes to the box's other address instead (out.go_out), which Wizard hands to the browser. On its own, a blank tab
    // opened inside the tap keeps the popup allowed while the sign-in is prepared.
    const framed = window.top !== window;
    const tab = framed ? null : window.open('', '_blank');
    try {
      const out = await api('api/quick/flow', { services, account: account ? account.id : null });
      const go = new URL((framed && out.go_out) || out.go, location.href).href;
      if (tab) { tab.opener = null; tab.location.href = go; }
      flowSheet(out.flow, go, tab, null, out);
      pollFlow(out.flow, go, tab, out);
    } catch (e) { if (tab) tab.close(); sheetClose(); say(e.message); }
  }
  function flowSheet(flow, go, tab, v, out) {
    const first = !tab && !v;
    const label = v && v.label ? v.label : 'Google';
    const where = v ? v.where : (out && out.steps && out.steps[0] && out.steps[0].id);
    const text = first ? 'Open the sign-in window to continue. It closes by itself when you are done.'
      : where === 'composio' ? 'In the new window, sign in to Composio or create a free account. Your key is saved by itself, then Google asks for access.'
        : `In the new window, choose your Google account and allow access for ${label}. This page updates by itself.`;
    const steps = el('div', null, 'wz-list');
    for (const s of ((v || out || {}).steps || [])) steps.append(row(s.label, s.done ? 'Done' : (v && s.id === v.step ? 'Now' : 'Next'), null, s.done ? '✓' : '·'));
    const again = link(first ? 'Open the sign-in window ↗' : 'Open the window again ↗', go, first ? 'wz-button' : 'wz-button secondary');
    again.addEventListener('click', e => { if (!framed()) { e.preventDefault(); const t = window.open(go, '_blank'); if (t) { t.opener = null; pollFlow(flow, go, t, out); } } });
    openSheet(first ? 'Connect Google' : 'Finish in the new window', el('p', text, 'muted'), steps, first ? '' : el('div', null, 'spinner', { 'aria-hidden': 'true' }),
      actionsOf(again, button('Cancel', () => { stopPolling(); api('api/quick/flow/cancel', { flow }).catch(() => {}); if (tab && !tab.closed) tab.close(); sheetClose(); }, 'wz-button secondary')));
  }
  function framed() { return window.top !== window; }
  function pollFlow(flow, go, tab, out) {
    stopPolling();
    const mine = ++seq;
    let shown = '';
    polling = setInterval(async () => {
      try {
        const v = await api('api/quick/flow?flow=' + encodeURIComponent(flow));
        if (mine !== seq) return;
        if (v.state === 'done') { stopPolling(); if (tab && !tab.closed) tab.close(); sheetClose(); await refresh(); say(`Connected ${(v.result && v.result.email) || 'your account'}.`, true); return; }
        if (v.state !== 'running') { stopPolling(); if (tab && !tab.closed) tab.close(); sheetClose(); if (v.error) say(v.error.message); await refresh(); return; }
        // The window itself moves from Composio to the next sign-in (signin.js); here the sheet only follows along.
        const sig = v.step + v.where;
        if (sig !== shown) { shown = sig; flowSheet(flow, go, tab, v, out); }
      } catch { /* keep waiting */ }
    }, 1500);
  }
  function quickCard(a) {
    const box = el('div', null, 'wz-list account');
    const tile = el('span', initial(a.label || a.email), 'wz-tile', { 'aria-hidden': 'true' });
    box.append(row(a.label || a.email || 'Google account', `${a.label ? a.email + ' · ' : ''}Quick connect`, () => quickAccountSheet(a), tile,
      a.status === 'active' ? pill('ok', 'Connected') : pill('warn', 'Sign in again')));
    if (a.status !== 'active') {
      box.append(row('This sign-in ended', 'Quick connect again to keep things working.', () => { queue = a.services.filter(s => s.linked).map(s => s.id); startQuick(queue.shift(), a); }, '!'));
      return box;
    }
    for (const s of a.services) {
      box.append(toggle(s.on, s.label, s.on ? s.what : (s.linked ? 'Off' : 'Off · signs in once'), async e => {
        const t = e.currentTarget; if (busy) return; busy = true; t.disabled = true;
        try { await api(`api/quick/accounts/${a.id}/service`, { service: s.id, on: !s.on }); await refresh(); say(`${s.label} ${s.on ? 'off' : 'on'} for ${a.label || a.email}.`, true); }
        catch (err) {
          if (err.code === 'consent_needed') { busy = false; queue = []; return startQuick(s.id, a); }
          say(err.message); t.disabled = false;
        } finally { busy = false; }
      }));
    }
    return box;
  }
  function quickAccountSheet(a) {
    const input = el('input', null, 'wz-field', { value: a.label || '', placeholder: 'Work, Personal…', 'aria-label': 'Name', maxlength: '40', enterkeyhint: 'done' });
    const form = el('form', null, 'label-form'); form.append(input);
    const save = async () => { try { await api(`api/quick/accounts/${a.id}/label`, { label: input.value }); sheetClose(); await refresh(); say('Saved.', true); } catch (e) { say(e.message); } };
    form.onsubmit = e => { e.preventDefault(); save(); };
    const chips = el('div', null, 'chips');
    for (const name of ['Work', 'Personal']) chips.append(button(name, () => { input.value = name; save(); }, 'wz-chip'));
    openSheet(a.email || 'Google account', el('p', 'Connected through Composio (Quick connect). Name it so you and your agent can tell accounts apart.', 'muted'), form, chips,
      actionsOf(button('Save name', save, 'wz-button secondary'),
        button('Disconnect', () => openSheet(`Disconnect ${a.email}?`, el('p', 'Wizard forgets this account and asks Composio to remove the sign-in.', 'muted'),
          actionsOf(button('Disconnect', async () => {
            try { const out = await api(`api/quick/accounts/${a.id}/disconnect`, {}); sheetClose(); await refresh();
              say(out.removed_at_composio ? 'Disconnected.' : 'Disconnected here. Remove access at myaccount.google.com/connections too.', true); }
            catch (e) { say(e.message); }
          }, 'wz-button danger'), button('Cancel', sheetClose, 'wz-button secondary'))), 'wz-button secondary danger')));
  }

  // ---------- one app (typed operations for other Wizard apps) ----------
  function renderCaller(id) {
    const c = home.clients.find(x => x.id === id) || { id, label: id.split(':')[1], kind: id.split(':')[0], grants: [], keys: [] };
    view.append(crumb(c.label));
    view.append(el('p', 'Tick what this app may do on each account. Wizard makes every Google call for it; the app never sees a Google password or token.', 'intro'));
    for (const a of home.accounts) {
      const g = c.grants.find(x => x.account === a.id);
      const list = el('div', null, 'wz-list');
      for (const op of home.operations) {
        const allowedByGoogle = a.allowed.includes(op.id);
        const on = !!g && g.operations.includes(op.id);
        list.append(toggle(on, `${op.group}: ${op.label}`, !allowedByGoogle ? 'Switch the service on for this account first' : (op.writes ? 'Makes changes' : null), async e => {
          const t = e.currentTarget; const next = new Set(g ? g.operations : []); on ? next.delete(op.id) : next.add(op.id);
          t.disabled = true;
          try { await api('api/grants', { client: c.id, account: a.id, operations: [...next] }); await refresh(); } catch (err) { say(err.message); t.disabled = false; }
        }, !allowedByGoogle && !on));
      }
      view.append(section(a.label || a.email, list));
    }
    const keys = el('div', null, 'wz-list');
    for (const k of c.keys) keys.append(row('Access key', `Made ${new Date(k.created * 1000).toLocaleDateString()}`, null, '⚿',
      button('Revoke', async () => { try { await api(`api/keys/${k.id}/revoke`, {}); await refresh(); } catch (e) { say(e.message); } }, 'wz-chip linkish')));
    keys.append(el('p', 'Apps on Wizard grants need no key. An older app can use a key instead.', 'muted pad'));
    view.append(section('Access key', keys, padded(button('Make a key', makeKey(c), 'wz-button secondary'))));
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
    if (window.WzSheet) WzSheet.open(sheet); else if (!sheet.open) sheet.showModal();
  }
  function sheetClose() { if (sheet.open) sheet.close(); stopPolling(); }
  sheet.addEventListener('close', stopPolling);
  function actionsOf(...buttons) { const a = el('div', null, 'actions'); a.append(...buttons); return a; }
  function padded(...buttons) { const a = actionsOf(...buttons); a.classList.add('pad'); return a; }

  function connectSheet() {
    const chosen = new Set(home.services.filter(s => s.default).map(s => s.id));
    const list = el('div', null, 'wz-list');
    for (const s of home.services) {
      const t = toggle(chosen.has(s.id), s.label, s.what, () => { chosen.has(s.id) ? chosen.delete(s.id) : chosen.add(s.id); t.setAttribute('aria-pressed', String(chosen.has(s.id))); });
      list.append(t);
    }
    const mode = home.sign_in.mode || (home.sign_in.wizard_available ? 'wizard' : 'own');
    openSheet(home.accounts.length ? 'Add a Google account' : 'Connect Google',
      el('p', 'What may your agent use on this account? You can change this any time.', 'muted'), list,
      el('p', mode === 'wizard' && !home.sign_in.verified
        ? 'Google opens in a new tab. Choose your account, then Continue. If Google says it hasn\'t verified the app, tap Advanced, then Go to Wizard.'
        : 'Google opens in a new tab. Choose your account and allow access.', 'muted hint'),
      actionsOf(button('Continue to Google', () => startSignIn(null, [...chosen]))));
  }
  function accountSheet(a) {
    const input = el('input', null, 'wz-field', { value: a.label || '', placeholder: 'Work, Personal…', 'aria-label': 'Name', maxlength: '40', enterkeyhint: 'done' });
    const form = el('form', null, 'label-form'); form.append(input);
    const save = async () => { try { await api(`api/accounts/${a.id}/label`, { label: input.value }); sheetClose(); await refresh(); say('Saved.', true); } catch (e) { say(e.message); } };
    form.onsubmit = e => { e.preventDefault(); save(); };
    const chips = el('div', null, 'chips');
    for (const name of ['Work', 'Personal']) chips.append(button(name, () => { input.value = name; save(); }, 'wz-chip'));
    openSheet(a.email || 'Google account',
      el('p', 'Name it so you and your agent can tell accounts apart.', 'muted'), form, chips,
      actionsOf(button('Save name', save, 'wz-button secondary'),
        button('Sign in again', () => startSignIn(a, a.services.filter(s => s.on).map(s => s.id)), 'wz-button secondary'),
        button('Disconnect', () => confirmDisconnect(a), 'wz-button secondary danger')));
  }
  function confirmDisconnect(a) {
    openSheet(`Disconnect ${a.email}?`, el('p', 'Wizard forgets this sign-in, your agent and apps lose access to it, and Google is asked to revoke it.', 'muted'),
      actionsOf(button('Disconnect', async () => {
        try {
          const out = await api(`api/accounts/${a.id}/disconnect`, {}); sheetClose();
          say(out.revoked_at_google ? 'Disconnected.' : 'Disconnected here. Remove Wizard at myaccount.google.com/connections too.', true); await refresh();
        } catch (e) { say(e.message); }
      }, 'wz-button danger'), button('Cancel', sheetClose, 'wz-button secondary')));
  }
  function signInSheet() {
    const wizard = home.sign_in.wizard_available || home.sign_in.mode === 'wizard';
    const list = el('div', null, 'wz-list');
    list.append(row('Wizard\'s Google app', wizard ? 'Quickest. Nothing to set up: just sign in.' : 'Not available right now', wizard && home.sign_in.mode !== 'wizard' ? async () => {
      try { await api('api/setup/wizard', {}); sheetClose(); await refresh(); } catch (e) { say(e.message); }
    } : null, '✦', home.sign_in.mode === 'wizard' ? pill('ok', 'In use') : null));
    list.append(row('Your own Google client', 'About ten minutes in Google Cloud. Nobody else\'s Google app is involved.', () => { sheetClose(); go({ kind: 'setup' }); }, '⚙',
      home.sign_in.mode === 'own' ? pill('ok', 'In use') : null));
    openSheet('How Wizard signs in to Google', list,
      el('p', home.accounts.length ? 'Disconnect your accounts before changing this: each sign-in belongs to the Google app that made it.' : 'Either way, your sign-ins and data stay on this box.', 'muted'));
  }

  async function startSignIn(account, services) {
    // Open the tab inside the tap so it is not blocked, then point it at Google.
    const tab = framed() ? null : window.open('', '_blank'); // inside Wizard the Open Google link below hands Google to the browser
    try {
      const out = await api('api/oauth/start', { account: account ? account.id : null, services });
      if (tab) { tab.opener = null; tab.location.href = out.authorization_url; }
      openSheet('Finish in the Google tab', el('p', 'Choose your account and press Continue or Allow. When Google sends you back, tap Return to Wizard. This page updates by itself.', 'muted'),
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
          stopPolling(); sheetClose();
          say(s.result.declined_services?.length ? `Connected ${s.result.email}. Google did not allow everything; switch it on again to retry.` : `Connected ${s.result.email}.`, true);
          await api(`api/accounts/${s.result.account}/check`, {}).catch(() => {});
          await refresh();
        } else if (s.status === 'failed' || s.status === 'cancelled') { stopPolling(); sheetClose(); if (s.status === 'failed') say(s.result.message); }
      } catch { /* keep waiting */ }
    }, 1500);
  }
  function stopPolling() { if (polling) { clearInterval(polling); polling = null; } }
  function chooseAccountSheet(r) {
    openSheet(`${r.label}: which account?`, ...home.accounts.map(a => row(a.label || a.email, a.label ? a.email : null, () => decide(r, true, a.id), initial(a.email))));
  }
  function newCallerSheet() {
    const input = el('input', null, 'wz-field', { placeholder: 'App id, e.g. music', 'aria-label': 'App id', autocapitalize: 'off', spellcheck: 'false' });
    const form = el('form'); form.append(input);
    form.onsubmit = e => { e.preventDefault(); const id = input.value.trim().toLowerCase(); if (/^[a-z0-9][a-z0-9_-]{0,62}$/.test(id)) { sheetClose(); go({ kind: 'caller', id: 'app:' + id }); } else say('Use the app id from its Wizard listing.'); };
    openSheet('Give an app access', el('p', 'Apps usually ask by themselves; their request appears here. To set it up ahead, type the app id.', 'muted'), form,
      actionsOf(button('Next', () => form.requestSubmit())));
  }

  // ---------- start ----------
  addEventListener('focus', () => { if (csrf && !sheet.open) refresh().catch(() => {}); });
  document.addEventListener('visibilitychange', () => { if (csrf && document.visibilityState === 'visible' && !sheet.open) refresh().catch(() => {}); });
  fetch('api/session', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Wizard-Request': 'bootstrap' }, body: '{}' })
    .then(async r => { const v = await r.json(); if (!r.ok) throw new Error(v.error?.message || 'Unavailable.'); csrf = v.csrf; await refresh();
      // Deep links from Mail and Calendar: ?start=own opens your-own-client setup, ?start=quick the Quick connect page.
      const start = new URLSearchParams(location.search).get('start');
      if (start === 'own') { if (ready() && home.client) connectSheet(); else go({ kind: 'setup' }); } else if (start === 'quick') go({ kind: 'quick' }); })
    .catch(e => { view.replaceChildren(el('div', null, 'wz-empty')); view.firstChild.append(el('strong', 'Google is not available'), el('p', e.message)); });
})();
