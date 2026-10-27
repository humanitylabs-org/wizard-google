# Google for Wizard

Gmail, Calendar, Drive (with Docs and Sheets), Contacts and YouTube for your Wizard agent. Sign in once per Google account, then switch on, per account, what your agent may use. Several accounts side by side (work and personal).

- **Quick sign-in (default):** Connect Google → tick services → sign in on Google → Return to Wizard. Uses Wizard's shared Google app through the stateless sign-in service in [`relay/`](relay/worker.mjs). Why, and what Google verification it needs: [docs/OAUTH_DECISION.md](docs/OAUTH_DECISION.md).
- **Your own Google client (optional):** the original 7-step flow, [docs/SETUP_FLOW.md](docs/SETUP_FLOW.md). No shared Google app involved.
- **Agents use gog.** MCP `google_run` runs one [gogcli](https://github.com/steipete/gogcli) command (pinned v0.45.0, SHA-256 checked at build) with a fresh access token for one account and `--enable-commands` set to the services the owner switched on. gog runs in an empty temp folder with no refresh token, secret or app data. `google_status` and `google_help` (gog's own command schema) complete the set.
- **Apps** keep typed operations per account and operation at `/consumer/v1` (Wizard grant or access key), as in 0.1.
- **Status for the shell** at `GET /api/connection` (`wizard.google-connection/1`; never a token).

## Install

The recipe needs only your Wizard shell's owner settings (same as other external apps):

- `WIZARD_OWNER_LOGINS`: your Tailscale login.
- `WIZARD_TRUSTED_HOSTS` / `WIZARD_TRUSTED_ORIGINS`: your shell's exact host(s) and `https://` origin(s).

Nothing about Google is an install-form field. Quick sign-in needs nothing; an own client is pasted inside the app, checked and stored encrypted. `GOOGLE_SIGNIN_SERVICE=off` allows only own clients.

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
- Owner page: Tailscale Serve identity (exactly one `Tailscale-User-Login`, must be in `WIZARD_OWNER_LOGINS`) or a gateway token; exact Host/Origin; CSRF on every write; `frame-ancestors` restricted; no request logging (callback URLs carry codes).
- MCP: box-only (no browser, cookie, forwarding or tailnet headers), or an admin bearer.
- App API: refuses owner cookies/origins; identity from the App Manager grant or a hashed access key.
- Host administrators and root on the box are trusted; they can read the folder and the process.

## Development

```sh
PYTHONPATH=app:tests python3 -m unittest discover -s tests -v   # broker, quick sign-in, services, gog gate, HTTP, MCP, packaging (fake Google)
GOG_TEST_BIN=/path/to/gog PYTHONPATH=app:tests python3 -m unittest tests.test_services.RealGog   # the real gog binary
node relay/test.mjs                                             # sign-in service: return page, client info, token passthrough
PLAYWRIGHT_PATH=…/playwright node qa/browser.cjs                # real browser: skins, Back, setup (fake Google)
python3 qa/preview.py --scenario quick-connected --port 18890 --core <wizard-ux>/web/core   # look at it: http://127.0.0.1:18890/__qa/host.html?skin=grove
docker build -f app/Dockerfile .
```

Runtime: Python 3.12 standard library + `cryptography`. UI: plain DOM on the Wizard style kit (`web/boot.js`, `tokens.css`, `ui.css`, `appearance-client.js` copied unchanged from `wizard-os/style`).

## What is proven, and what needs a real Google client

Proven here, with a labelled fake Google: every broker path (setup validation, sign-in, partial consent, reconnect, refresh/rotation, expiry, Testing detection, API-disabled errors, grants, requests, keys, disconnect, custody, data-folder move), HTTP guards, MCP and the consumer API, the relay page, and the UI in a real browser on several skins with Back.

Needs a real Google test client (see the report for the list): the console deep links landing on the right pages, the ts.net redirect URI and authorized-domain acceptance, the real consent round trip and the unverified-app screen, `refresh_token_expires_in` on a Testing app, `include_granted_scopes` scope reporting, real Gmail/Calendar/Drive/People/YouTube responses, and revocation.

## Runtime packaging

`runtime.package: build` builds `app/Dockerfile` from the App Manager's pinned
repository. Cryptography and cffi require native wheels, so they are not vendored
as source. `requirements.txt` remains the dependency input; `requirements.lock`
pins the complete resolution and hashes, including musllinux x86_64 and aarch64
wheels for Python 3.12. Installation uses `--require-hashes --only-binary=:all:`
to refuse source compilation or unhashed artifacts. Regenerate with:

```sh
uv pip compile requirements.txt --generate-hashes --universal --python-version 3.12 --no-header -o requirements.lock
```

The official Python Alpine base is pinned by its multi-architecture index digest.
There is no compose `image:` override: App Manager names the local image. The
compose runs as root with the existing capability/security restrictions so a
fresh root-owned `/data` bind works; all persistent state still uses that bind,
and application-enforced private file permissions and encryption are unchanged.
Requires an App Manager release containing wizard-app-manager#153.
