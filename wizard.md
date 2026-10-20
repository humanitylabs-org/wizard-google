---
wizard: 0.2
id: wizard-google
name: Google
type: external
category: system
version: 0.1.0
summary: Your Google accounts on your own Google client. Apps and agents get only the operations you allow.
license: GPL-3.0-only
source: https://github.com/humanitylabs-org/wizard-google
icon: icon.png
runtime:
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

Connect Gmail, Calendar, Drive, Contacts and YouTube to your Wizard with **your own Google Cloud OAuth client**. There is no AI Wizards server in the middle: the sign-in returns to your box, tokens never leave it, and only this app ever holds them.

- **Setup inside the app:** seven counted steps (five short visits to Google Cloud, each deep-linked with the values to paste, plus two in Wizard). Wizard checks the client with Google the moment you paste it, spots a Testing-mode app, and links straight to any API that still needs turning on.
- **Several accounts.** Personal and work side by side.
- **Per app and per agent grants.** You tick which operations each app or agent may use on which account (for example: Main agent may search mail on your personal account; Meeting Recorder may see events on your work calendar). Wizard makes the Google call; no other app ever holds a Google token. Apps and agents ask, and the request waits for you on the home screen.
- **Moves with the box.** Your client, encrypted sign-ins, the store key and every grant live in this app's data folder (`${APP_DATA_DIR}/data`), so a backup restored on a new box needs no reconnect. Stop the old box before starting the new one.

MCP at `/mcp` for agents on this box (`google_status`, `google_setup_guide`, `google_request_access` and one tool per operation). Apps use `/consumer/v1` with their Wizard grant (`X-Wizard-Grant`) or an access key made in the app. Read-only status for the shell at `/api/connection` (`wizard.google-connection/1`).
