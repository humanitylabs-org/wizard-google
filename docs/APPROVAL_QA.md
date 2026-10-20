# Agent write approval QA (prototype)

Scope: `feature/agent-write-approval`, based on `feature/byo-google-client` (`12035d7`). No live deployment, Docker operation, shared shell/spec edit or real Google credential was used.

## Automated evidence

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=app:tests python3 -m unittest discover -s tests -v`: **72 tests pass**.
- `node relay/test.mjs`: **PASS** (retained inactive compatibility code).
- Existing `qa/browser.cjs`, run remotely with the real app server/UI and labelled fake Google: **21 checks pass**. Existing setup, dark fallback and Back checks remain green. New checks cover pending/no mutation, phone-UA/touch/frame width, exact payload, live Nebula → Ember skin change without replacing the document/request, one-tap Approve, one-tap Deny, reload/no replay, an incoming MCP card on already-open home, and HTML rendered as text.
- `git diff --check`: clean.

Required scenarios are executable in `tests/test_write_approval.py` and `tests/test_http.py`: agent mail without approval does not send/refresh; approve sends once; concurrent/repeated approve cannot send twice; altered packet/hash/account rejected; a changed proposal cannot borrow an old approval; expiry boundary; denial; app standing-grant send without a card. Additional checks cover all catalogue writes, Calendar, new write schema fail-closed, restart, ambiguous accepted-then-timeout, revocation/disconnect, canonical addresses/defaults/unknown-field decoys, owner-header forgery, and production-style gateway-token approval.

## Visual evidence

Screenshots are delivered outside Git in the requested `shots/` evidence directory:

- `12-agent-send-approval-nebula.png`
- `12-agent-send-approval-ember.png`
- `13-agent-calendar-approval-nebula.png`
- `13-agent-calendar-approval-ember.png`
- `14-new-proposal-after-approval.png`

The two mail screenshots and Ember calendar screenshot were visually inspected: complete account, payload and Approve/Deny controls are readable on dark Appearance surfaces. Other cards may be partially visible at the viewport edge while the target card is scrolled into view; the target card is not clipped. This is a same-origin hosted Appearance QA proxy with mobile-UA/touch, **not** a live Wizard install, dedicated-origin security proof or physical-device coverage.

## Independent review

A read-only Opus source-packet review found two blockers, both fixed before delivery: forgeable owner login headers (production now also requires a protected gateway token), and raw/unknown input fields masquerading as the reviewed payload (packets are now canonicalized and unknown fields refused). Focused re-review returned **PASS for the bounded prototype**, conditional on the unchanged app-auth and transport implementations. Those conditions were checked: `consumer_caller` authenticates `X-Wizard-Grant` via hashed App Manager secrets or a hashed owner-created `wgk_` key; it never trusts an app-name header. `Transport.request` maps network/timeouts to 502, which the consumed approval records as `unknown`, never automatically retrying. The review also prompted identifier-only receipts, account-lock serialization of revocation, and detached post-decision returns to avoid pruning races. The full second review attempt timed out; the successful re-review was intentionally focused on the changed security paths, not represented as another full audit.

## Release gates (not hidden by passing tests)

- Integrate protected `X-Wizard-Owner-Token` injection only after actual owner authentication, keep the secret out of agent/browser environments, and never inject it on MCP/consumer routes. Production fails closed while unconfigured. Loopback-owner development mode is only a fake fixture.
- Prove dedicated-origin or equivalent browser isolation from hostile sibling apps: ordinary same-origin CSRF cannot distinguish their JavaScript from this owner UI. No live shell/proxy change was authorized here.
- Exercise a real owner Google client for consent/HTTPS callback and real Gmail/Calendar behaviour. Fake provider acceptance is not independent Google readback or a provider-side exactly-once guarantee.

The existing optional callback compatibility implementation remains inactive by default. Supported setup documents and the install form offer only the box's own HTTPS callback.
