---
name: wizard-google
description: "Use when the owner asks about their Gmail, Google Calendar, Drive, Contacts or YouTube, or wants to connect Google to Wizard."
---
# Google

The Google app holds the owner's Google sign-ins on this box. You never see a token or password, and you may only do what the owner allowed for you, per account. Wizard link to the app: `/wizard-os-home/?open=wizard-google`.

## Every time

1. Call `google_status` first. It lists the connected accounts and, for each one, the operations **you** may use.
2. If the operation you need is not listed for any account, call `google_request_access` with the operations (and the account email if the owner named one) plus one short reason the owner will see. Then tell the owner in one sentence: "I've asked for access; tap Allow in the Google app." Never retry in a loop.
3. Call the operation tool. Pass `account` (an email) when more than one account is allowed for that operation.

| Tool | Operation | Notes |
|---|---|---|
| `google_gmail_search` | gmail.search | Gmail search syntax in `query` (`from:anna newer_than:7d`). Sender, subject, date, preview only. |
| `google_gmail_read` | gmail.read | One message as plain text, by `id` from a search. |
| `google_gmail_send` | gmail.send | Only an email the owner asked for, with the recipients and text they approved in chat. |
| `google_calendar_events` | calendar.read | `time_min`/`time_max` as RFC 3339 (`2026-10-21T00:00:00Z`). |
| `google_calendar_create` | calendar.write | `summary`, `start`, `end` (date or RFC 3339). Confirm details with the owner first. |
| `google_drive_search` | drive.search | File names, types and links. |
| `google_drive_read` | drive.read | Docs, Sheets (CSV), Slides and text files as text. |
| `google_contacts_search` | contacts.read | Name or email in `query`. |
| `google_youtube_video` | youtube.read | 11-character video id. |

## Errors and what to say

- `grant_required`: you are not allowed yet. Use `google_request_access` (step 2).
- `google_permission_missing`: the owner allowed you, but Google has not been allowed this for the account. Tell the owner: open the Google app, choose the account, then "Allow more…".
- `api_not_enabled`: the owner's Google project has that API switched off. Give the owner the `fix_url` from the error (one page, press Enable).
- `reauth_required`: Google ended the sign-in. The owner reconnects in the Google app. If the message mentions Testing, the owner's Google app is still in Testing (7-day sign-ins): the `fix_url` opens the page with "Publish app".
- `account_required`: several accounts are allowed; ask which one and pass `account`.

## Setting Google up with the owner

If `google_status` shows `setup_complete: false`, offer to walk the owner through it. Call `google_setup_guide`: it returns seven numbered steps, which are done, the `current` one, a `url` for each Google Cloud step and `copy` values (app name, authorized domain, the exact redirect URI). Go one step at a time:

1. Say the step's title and give its `url`; read out the `copy` values they will paste. Keep the `detail` lines short.
2. Ask them to come back when done and tick "Done" in the Google app (or finish it there). Call `google_setup_guide` again to confirm before moving on.
3. Step 5 (the client file) happens **only in the Google app**: they upload or paste the downloaded JSON there. Never ask for the client secret, the JSON, a token or a password in chat. If they paste one into chat anyway, tell them to delete that message and use the app.
4. Expect the "Google hasn't verified this app" screen at sign-in: it is their own app; they choose Advanced, then "Go to Wizard".
5. Mention `warnings` (for example: still in Testing, redirect not on the client) with their `fix_url`.

What Google forces by hand (no API exists): naming the app (consent screen), publishing it, and creating the OAuth client. Do not offer to automate those.
