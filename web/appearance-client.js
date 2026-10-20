/* @wizard/ui Appearance v1. Colors only; no shell internals or agent/data rights.
 * Canonical source: humanitylabs-org/wizard-os/style/. Same-origin pairing only.
 * Load boot.js before CSS, tokens.css + ui.css, then this client. */
(() => {
  'use strict';
  if (window.WizardTheme) return;
  const root = document.documentElement;
  const NAMES = ['canvas','base','chrome','overlay','surface','surface-raised','text','text-secondary','text-muted','border','accent','accent-soft','on-accent','reader-surface'];
  const trustedHost = location.origin;
  let current = null, connected = false;
  const isColor = v => typeof v === 'string' && v.length > 0 && v.length < 96 && !/[;{}]|\b(?:url|var|env|attr)\s*\(/i.test(v) && !/^(?:inherit|initial|unset|revert|revert-layer|currentcolor)$/i.test(v.trim()) && !!window.CSS?.supports('color', v);
  function valid(a) {
    if (!a || a.version !== 1 || !Number.isSafeInteger(a.revision) || a.revision < 0) return false;
    if (!['window','immersive'].includes(a.presentation)) return false;
    if (!['dark','light'].includes(a.colorScheme) || typeof a.skin !== 'string' || !/^[a-z0-9-]{1,40}$/.test(a.skin)) return false;
    if (!a.tokens || typeof a.tokens !== 'object' || Array.isArray(a.tokens)) return false;
    // Transparent hosted canvas is invariant, even if an otherwise-valid host sends opaque paint.
    return a.tokens.canvas === 'transparent' && NAMES.every(k => isColor(a.tokens[k]));
  }
  function apply(a) {
    if (!valid(a) || (current && a.revision <= current.revision)) return false;
    for (const k of NAMES) root.style.setProperty('--wz-' + k, a.tokens[k]);
    root.style.colorScheme = a.colorScheme;
    root.dataset.skin = a.skin; root.dataset.presentation = a.presentation; root.dataset.colorScheme = a.colorScheme;
    root.setAttribute('data-wz-hosted', ''); root.classList.add('wz-hosted');
    // Expose only the validated payload, including frozen tokens.
    current = Object.freeze({ version: 1, revision: a.revision, skin: a.skin, colorScheme: a.colorScheme, presentation: a.presentation, tokens: Object.freeze(Object.fromEntries(NAMES.map(k => [k, a.tokens[k]]))) });
    connected = true;
    dispatchEvent(new CustomEvent('wizard:theme', { detail: current }));
    return true;
  }
  root.dataset.presentation = 'standalone';
  root.dataset.skin ||= 'nebula';
  root.dataset.colorScheme = 'dark';
  addEventListener('message', e => {
    if (parent === window || e.source !== parent || e.origin !== trustedHost || !e.data || e.data.type !== 'wizard:appearance') return;
    const a = e.data.appearance;
    // Re-acknowledge a retransmission without mutating state or emitting another theme event.
    if (valid(a) && current && a.revision === current.revision && NAMES.every(k => a.tokens[k] === current.tokens[k]) && a.skin === current.skin && a.colorScheme === current.colorScheme && a.presentation === current.presentation) {
      parent.postMessage({ type: 'wizard:appearance:ack', version: 1, revision: current.revision }, trustedHost); return;
    }
    if (apply(a)) parent.postMessage({ type: 'wizard:appearance:ack', version: 1, revision: current.revision }, trustedHost);
  });
  window.WizardTheme = { version: 1, get current() { return current; }, get connected() { return connected; } };
  if (parent !== window) parent.postMessage({ type: 'wizard:appearance:ready', version: 1, app: root.dataset.app || '' }, trustedHost);
})();
