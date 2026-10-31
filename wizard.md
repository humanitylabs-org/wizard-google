---
wizard: 0.2
id: wizard-google
name: Google
type: external
category: system
version: 0.3.3
summary: Connect Google and choose what your agent may use. Quick connect (Gmail, Calendar) via Composio, or your own Google client.
license: MIT
source: https://github.com/humanitylabs-org/wizard-google
icon: icon.png
runtime:
  package: image
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
provides: [google-connection, mail-calendar]
system: true
uses: []
needs: []
health: /healthz
---

# Google

Connect Gmail, Calendar, Drive (with Docs and Sheets), Contacts and YouTube to your Wizard. Sign in once per Google account, then switch on, per account, what your agent may use. Work and personal accounts sit side by side.

- **Quick connect (Gmail and Calendar, about a minute).** Goes through [Composio](https://composio.dev), a third party whose Google sign-in is already approved, so there is no Google Cloud setup. Tap Quick connect: one window asks you to sign in to Composio (or create a free account; the key is saved by itself, or paste one; several keys allowed), then to Google for Gmail and Calendar. Composio holds the sign-in keys, so technically it can access the account; free for normal personal use, heavy use can cost money. You can switch any time.
- **Your own Google client (works today).** More → Sign-in → Your own Google client walks you through Google Cloud (about ten minutes). No shared Google app is involved and nothing leaves this box except calls to Google.
- **Quick sign-in (built, switched off).** A one-minute sign-in through Wizard's shared Google app is built but not turned on: the sign-in service is not deployed and the approach is still being evaluated. When it is unavailable, the app uses your own client automatically.
- **Your agent works through gog.** Agents call `google_run` with a [gogcli](https://github.com/steipete/gogcli) command (pinned v0.45.0, checksum-verified). The app hands gog a short-lived access token for one account and only the command groups you switched on, in an empty folder; gog never sees a refresh token, a secret or this app's data.
- **Apps** (other Wizard apps) use typed operations at `/consumer/v1` with their Wizard grant or an access key, per account and operation, as before.
- **Moves with the box.** Sign-ins, the store key and every switch live in this app's data folder (`${APP_DATA_DIR}/data`), so a backup restored on a new box needs no reconnect. Stop the old box before starting the new one.

MCP at `/mcp` for agents on this box: `google_status`, `google_run`, `google_help`, and mail/calendar tools that work for every account however it was connected: `mail_search`, `mail_get`, `mail_draft`, `mail_send` (sends an approved draft), `mail_update` (read/archive/trash), `calendar_list_events`, `calendar_create_event`, `calendar_update_event`, `calendar_delete_event`, `calendar_respond` (contract `wizard.mail-calendar/1`, shared with the Microsoft app; the same tools over HTTP at `/v1/<tool>` for scripts and apps on this box; Mail and Calendar run Quick connect from their own window). Read-only status for the shell at `/api/connection` (`wizard.google-connection/1`).
