---
wizard: 0.2
id: wizard-google
name: Google
type: external
category: system
version: 0.2.0
summary: Sign in to Google once, then choose what your agent may use. Gmail, Calendar, Drive, Docs, Sheets, Contacts, YouTube; several accounts.
license: MIT
source: https://github.com/humanitylabs-org/wizard-google
icon: icon.png
runtime:
  package: build
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 18812
  path: /consumer/v1
  spec: openapi.json
mcp:
  path: /mcp
  port: 18812
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 18812
provides: [google-connection]
system: true
uses: []
needs: []
health: /healthz
---

# Google

Connect Gmail, Calendar, Drive (with Docs and Sheets), Contacts and YouTube to your Wizard. Sign in once per Google account, then switch on, per account, what your agent may use. Work and personal accounts sit side by side.

- **Quick sign-in (default).** Tap **Connect Google**, tick what your agent may use, sign in on Google's page, tap **Return to Wizard**. About a minute, on your phone or laptop. It uses Wizard's shared Google app through a stateless sign-in service (`relay/`): your sign-in is stored only on this box, encrypted; the service keeps nothing and cannot use a sign-in code without the one-time secret only your box holds.
- **Your own Google client (optional).** Prefer that no shared Google app is involved? More → Sign-in → Your own Google client walks you through Google Cloud (about ten minutes). Nothing else changes.
- **Your agent works through gog.** Agents call `google_run` with a [gogcli](https://github.com/steipete/gogcli) command (pinned v0.45.0, checksum-verified). The app hands gog a short-lived access token for one account and only the command groups you switched on, in an empty folder; gog never sees a refresh token, a secret or this app's data.
- **Apps** (other Wizard apps) use typed operations at `/consumer/v1` with their Wizard grant or an access key, per account and operation, as before.
- **Moves with the box.** Sign-ins, the store key and every switch live in this app's data folder (`${APP_DATA_DIR}/data`), so a backup restored on a new box needs no reconnect. Stop the old box before starting the new one.

MCP at `/mcp` for agents on this box: `google_status`, `google_run`, `google_help`. Read-only status for the shell at `/api/connection` (`wizard.google-connection/1`).
