# Google for Wizard (prototype)

Gmail, Calendar, Drive, Contacts and YouTube for your Wizard, signed in with **your own Google Cloud OAuth client**. No AI Wizards server sits in the middle: Google sends the sign-in back to your box, the box exchanges it with your client secret and a PKCE verifier, and the tokens never leave the box. Only this app ever holds a Google token; other apps and agents get typed operations on the accounts you allow, and nothing else.

Status: **prototype on branch `feature/agent-write-approval`.** Tested against a fake Google (see "What is proven"). Not published to the app index; no image is published.

## What it does

- **Setup inside the app**: 7 counted steps, 5 of them short visits to Google Cloud, each deep-linked with the values to paste. The design and the reasons are in [docs/SETUP_FLOW.md](docs/SETUP_FLOW.md). The agent can walk you through the same steps (`google_setup_guide`).
- **Several Google accounts**, each with its own Google permissions (granular consent respected; "Allow more…" adds scopes later).
- **Grants per app and per agent, per account, per operation.** Operations: `gmail.search`, `gmail.read`, `gmail.send`, `calendar.read`, `calendar.write`, `drive.search`, `drive.read`, `contacts.read`, `youtube.read`. Nothing is allowed by default. Apps and agents can ask (`google_request_access`, `POST /consumer/v1/request`); the request waits on the home screen for Allow / Decline.
- **Every agent write asks again**, even with a grant: send mail and create events return a pending request, not a sent message/event. The owner sees the account and complete payload with one-tap **Approve / Deny** in the Google app. Approval runs only that packet, once, within 15 minutes. App callers keep standing grants; reads are unchanged. [Protocol and safety boundaries](docs/AGENT_WRITE_APPROVAL.md).
- **Callers**:
  - Agents through MCP at `/mcp` (box-only). **All agents share Main agent grants until the Agent Service supplies `X-Wizard-Agent`.** No header means `agent:main`; a supplied identity has separate grants. This connector does not infer an agent from chat text.
  - Apps through `/consumer/v1` with their App Manager grant (`X-Wizard-Grant`, [PR #6](https://github.com/humanitylabs-org/wizard-os/pull/6) `grants.json`), or an access key you make in the app for apps installed before grants exist.
- **Status for the shell** at `GET /api/connection` (`wizard.google-connection/1`, the same shape as the AI connection apps; never a token).

## Install (when published)

The recipe needs only your Wizard shell's owner settings (same as other external apps):

- `WIZARD_OWNER_LOGINS`: your Tailscale login.
- `WIZARD_TRUSTED_HOSTS` / `WIZARD_TRUSTED_ORIGINS`: your shell's exact host(s) and `https://` origin(s).
- `WIZARD_OWNER_TOKEN`: a private gateway-to-connector secret. The owner-authenticated gateway must overwrite/inject `X-Wizard-Owner-Token` on the owner UI routes, **never** on MCP or consumer routes. Keep it outside agent environments, tools, browser code and responses. Tailscale identity headers alone are forgeable from the box; the production UI fails closed without this token. Integrating that protected injection is a release prerequisite, not done on a live shell by this prototype change.

Google client values are **not** install-form fields: you paste the client file inside the app, where it is checked and stored encrypted.

Before a real installation, the owner UI also needs a dedicated protected origin (or an equivalently proved browser isolation boundary). Other apps' JavaScript on the same origin could otherwise bootstrap and approve as the browser's owner. The same-origin QA frame is a styling/interaction proxy, **not** proof of isolation from hostile sibling apps. No live shell or gateway integration is shipped here.

## Where the data lives, and moving boxes

Everything is in `${APP_DATA_DIR}/data` (a bind mount; **no Docker named volumes**, which App Manager backups skip):

| File | What |
|---|---|
| `store.key` | Fernet key (0600), generated on first start (or `GOOGLE_STORE_KEY`). |
| `google.enc` | One encrypted snapshot: your client (ID, secret, project), accounts and their tokens, grants, access-key hashes, pending requests, a content-free call log. |

Move a box: back up or copy the app's data folder, restore it on the new box, start the app. Sign-ins, grants and the client come with it; nothing to reconnect. **Stop the old box before you start the new one.** Google may rotate refresh tokens (the app saves a new one before using it), two boxes refreshing the same sign-in can knock each other out, and a Disconnect on either box revokes the sign-in for both. If the new box has a different address, add its redirect URI to your client before the next *new* sign-in (existing sign-ins keep working).

The key sits next to the data so a move needs no extra step. Encryption at rest therefore protects against a stray copy of `google.enc` alone, not against someone holding the whole folder; protect backups like the box itself. A wrong or missing key never overwrites existing sign-ins: the app refuses to start instead. Runtipi can loosen app-data permissions on lifecycle actions; the app restores 0700/0600 on every start.

## Security notes

- OAuth: authorization code + PKCE S256, `state` one-use and bound to the owner's browser session (HttpOnly cookie), nonce checked in the ID token, 10-minute flows, wrong-account reconnect refused, disconnect cancels in-flight sign-ins and revokes at Google.
- ID token comes straight from Google's token endpoint over TLS; per OpenID Connect Core 3.1.3.7 the TLS check stands in for the signature, and issuer, audience, azp, expiry, nonce and subject are all checked.
- Transport: fixed Google hosts only, no proxies, no redirects, bounded responses; callers never supply a URL.
- Owner page: private owner-authenticated gateway token, **plus** Tailscale Serve identity in `tailscale-serve` mode (exactly one `Tailscale-User-Login`, must be in `WIZARD_OWNER_LOGINS`); exact Host/Origin; CSRF on every write; `frame-ancestors` restricted; no request logging (callback URLs carry codes). `GOOGLE_DEV_MODE=loopback-single-owner` intentionally bypasses owner authentication for local fixtures; never enable it around untrusted agents or real sign-ins.
- MCP: box-only (no browser, cookie, forwarding or tailnet headers), or an admin bearer.
- App API: refuses owner cookies/origins; identity from the App Manager grant or a hashed access key.
- Agent write approvals: encrypted immutable packet, SHA-256 binds caller + account + operation + arguments; expiry and atomic durable single-use claim checked before sending; grant/account/scope rechecked. An interrupted or ambiguous submission is **unknown**, never automatically retried. Host administrators remain trusted; this is not an OS sandbox or a guarantee of provider-side exactly-once delivery.
- Google returns only to the box's own HTTPS address. There is no hosted return page in the supported setup.
- Host administrators and root on the box are trusted; they can read the folder and the process.

## Development

```sh
PYTHONPATH=app:tests python3 -m unittest discover -s tests -v   # broker, HTTP, MCP, consumer API, packaging (fake Google)
node relay/test.mjs                                             # retained inactive compatibility-code tests
PLAYWRIGHT_PATH=…/playwright node qa/browser.cjs                # real browser: skins, Back, setup (fake Google)
python3 qa/preview.py --scenario connected --port 18890         # look at it: http://127.0.0.1:18890/__qa/host.html?skin=grove
docker build -f app/Dockerfile .
```

Runtime: Python 3.12 standard library + `cryptography`. UI: plain DOM on the Wizard style kit (`web/boot.js`, `tokens.css`, `ui.css`, `appearance-client.js` copied unchanged from `wizard-os/style`).

## What is proven, and what needs a real Google client

Proven here, with a labelled fake Google: every broker path (setup validation, sign-in, partial consent, reconnect, refresh/rotation, expiry, Testing detection, API-disabled errors, grants, requests, keys, disconnect, custody, data-folder move), agent write approval/deny/expiry/hash binding/replay/concurrency/restart, standing app writes, HTTP guards, MCP and the consumer API, retained compatibility code, and the UI in a real browser on several skins with Back. Browser QA uses the repository's same-origin Appearance host proxy, not a live Wizard installation or physical phone.

Needs a real Google test client (see the report for the list): the console deep links landing on the right pages, the ts.net redirect URI and authorized-domain acceptance, the real consent round trip and the unverified-app screen, `refresh_token_expires_in` on a Testing app, `include_granted_scopes` scope reporting, real Gmail/Calendar/Drive/People/YouTube responses, and revocation.
