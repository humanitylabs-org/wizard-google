# How Wizard signs in to Google (decision, 27 Oct 2026)

## Recommendation: Wizard's own Google app by default, the owner's own client as an option

| | Wizard's Google app (default) | Owner's own Google client (option) |
|---|---|---|
| Owner effort | Tap Connect, pick account, Allow, Return to Wizard. About a minute per account, phone or laptop. | About ten minutes in Google Cloud: project, consent screen, publish, web client, APIs. Too technical for most owners. |
| Where tokens live | On the owner's box, encrypted. Never on an AI Wizards server. | Same. |
| What AI Wizards runs | `relay/worker.mjs` at `www.aiwizards.com/wizard-google/auth/*`: the fixed return page and a token passthrough that adds the shared client secret. No storage, no logs. | Nothing. |
| Google review | Needed before more than 100 owners (see below). | None: personal-use apps under 100 users are exempt. Owner sees "Google hasn't verified this app" once. |
| Risk we carry | One client for every owner: if Google suspends it, every owner re-connects. We must keep the Limited Use promises and pass the yearly security assessment. | Per owner. |

Why the secret stays in the service: a Google **web** client's secret must stay confidential, so it cannot ship inside a public app image. The service adds it to the box's code exchange and token refresh. It cannot use a code by itself (PKCE: the verifier never leaves the box) and keeps nothing it passes along.

## What Google verification the default needs

The scopes the five services use:

| Service | Scopes | Google class |
|---|---|---|
| Gmail | `gmail.modify`, `gmail.settings.basic` | **Restricted** |
| Drive, Docs, Sheets | `drive`, `documents`, `spreadsheets`, `presentations` | `drive` is **restricted**; the others are sensitive |
| Calendar | `calendar` | Sensitive |
| Contacts | `contacts`, `contacts.other.readonly` | Sensitive |
| YouTube | `youtube.readonly` | Sensitive |

1. **Now, before verification (free):** publish the consent screen "In production". Owners see the unverified-app screen (Advanced → Go to Wizard) and the project is capped at **100 new users for its lifetime**. Enough for the first customers.
2. **Brand verification (free, days):** verified domain `aiwizards.com` in Search Console, home page, privacy policy (`/wizard-google/auth/privacy` has the Limited Use statement), support email, logo.
3. **Sensitive + restricted scope verification (free, weeks):** a written justification per scope and a demo video of the consent flow and each use.
4. **Restricted scopes add a yearly CASA security assessment** by a Google-approved lab: about **$500–$4,500 a year**, 2–3 weeks for tier 2. Google tells us when to start it.

Cheaper path if Gmail send/modify and full Drive are not needed: `gmail.readonly` is still restricted, so Gmail always needs the assessment. Only dropping Gmail and using `drive.file` would avoid it, which defeats the purpose. Plan on the assessment.

Nothing has been submitted to Google and nothing has been paid for. Both need Miguel's go-ahead.
