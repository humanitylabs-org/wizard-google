# How Wizard signs in to Google

## Status (29 Oct 2026): own client only; shared sign-in not approved

The owner's own Google client is the only active flow. The shared sign-in service below is built but not deployed, and the app falls back to own clients when it is unreachable. James has not approved a shared Wizard sign-in: running one only to save a few known owners ten minutes is low value, and the requirement is onboarding that scales. Under evaluation ([issue #7](https://github.com/humanitylabs-org/wizard-google/issues/7)): a third-party managed connection service (for example Pipedream Connect or Composio) as an optional easy path, versus a properly verified Wizard-owned service, with own client kept as the independent option. Activating the relay needs a new decision.

The rest of this page is the original 27 Oct proposal, kept for reference, with corrections.

## Original proposal: Wizard's own Google app by default, the owner's own client as an option

| | Wizard's Google app (default) | Owner's own Google client (option) |
|---|---|---|
| Owner effort | Tap Connect, pick account, Allow, Return to Wizard. About a minute per account, phone or laptop. | About ten minutes in Google Cloud: project, consent screen, publish, web client, APIs. Too technical for most owners. |
| Where tokens live | On the owner's box, encrypted. Never on an AI Wizards server. | Same. |
| What AI Wizards runs | `relay/worker.mjs` at `www.aiwizards.com/wizard-google/auth/*`: the fixed return page and a token passthrough that adds the shared client secret. No storage, no logs. | Nothing. |
| Google review | Needed before more than 100 owners (see below). | None: personal-use apps under 100 users are exempt. Owner sees "Google hasn't verified this app" once. |
| Risk we carry | One client for every owner: if Google suspends it, every owner re-connects. We must keep the Limited Use promises and pass the yearly security assessment. | Per owner. |

Why the secret stays in the service: a Google **web** client's secret must stay confidential, so it cannot ship inside a public app image. The service adds it to the box's code exchange and token refresh. The relay code persists nothing it passes along, but OAuth credentials do pass through it: the box sends the PKCE verifier with the code exchange, and every refresh token on each renewal, and the token responses come back through it. So "tokens never leave the box" is wrong; the accurate claim is "tokens are stored only on the box and not persisted by the relay code". A compromised relay could capture them.

### Open security items before any activation

- The return page checks the destination address's syntax only, not that it belongs to a registered box, and the token endpoint does not authenticate the box. Anyone could ride the shared client to run consent screens in Wizard's name. Needs box registration or signed state before go-live.
- One shared client: if Google suspends it, every owner on it is cut off, and reconnecting to the same client does not fix it.
- The 100-user cap for unverified sensitive/restricted scopes is for the project's lifetime and is not a compliance exemption for a commercial pilot. Restricted-scope verification and the yearly assessment are an ongoing duty, priced by Google's assigned tier and an assessor's quote.
- An isolated AI reader in front of Gmail lowers prompt-injection risk but guarantees nothing and does not by itself meet Google's data-use rules.

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
