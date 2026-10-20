# Setup flow: the owner brings their own Google client

Goal: an owner with an ordinary Google account connects Gmail, Calendar, Drive, Contacts and YouTube to their Wizard **without any AI Wizards server in the middle**, in as few steps as Google allows, all started from inside the Google app.

## The steps (7: five in Google Cloud, two in Wizard)

| # | Step | Where | How | What Wizard does for the owner |
|---|---|---|---|---|
| 1 | Create a Google Cloud project | Google Cloud | Deep link `console.cloud.google.com/projectcreate` | Copy value "Wizard". Explains the one-time Cloud terms/country prompt. "Already have one? Tick." |
| 2 | Name your Google app (Google Auth Platform "Get started": name, support email, **External**, contact email, agree; then Branding → Authorized domains) | Google Cloud | Deep link `auth/overview` | Copy values: app name, and the **authorized domain** computed from the box's redirect host (`tailXXXX.ts.net`, a public-suffix registrable domain). |
| 3 | Publish the app ("Publish app" on Audience) | Google Cloud | Deep link `auth/audience` | **Detected automatically after the first sign-in.** A Testing-mode app gets `refresh_token_expires_in ≈ 604799` (7 days); Wizard then shows "still in Testing" with the fix link. No refresh-token field = published (or Internal). |
| 4 | Create the sign-in client (Web application; paste the redirect URI; Create; **Download JSON**) | Google Cloud | Deep link `auth/clients/create` | Copy value: the **exact redirect URI**, auto-detected from the address the owner opened Wizard on (`https://<box>.<tailnet>.ts.net/wizard-google/oauth/callback`). Steps 4 and 5 are shown open together. |
| 5 | Give the client to Wizard | Wizard | Upload the file or paste it | Validates shape (Web vs Desktop client, ID format, secret present, token URI), reads `project_id`, warns if the redirect URI isn't on the client, and **asks Google whether the ID + secret are real without any sign-in** (token endpoint with a dummy code: `invalid_client` = wrong, `invalid_grant` = good). Stored encrypted on the box. |
| 6 | Turn on the APIs | Google Cloud | One deep link `apis/enableflow?apiid=gmail…,calendar-json…,drive…,people…,youtube…&project=<project_id>` | Because the client JSON names the project, this link points at **exactly the project that owns the client** (the one whose APIs count). One "Enable" press. Verified automatically after sign-in by a cheap read per API; a disabled API shows "Turn on ↗" with a one-API link. |
| 7 | Connect a Google account | Wizard → Google consent | In-app button | Owner ticks what Wizard may use (grouped, writes marked). Google's sign-in opens in a new tab (Google refuses to run inside a frame). The tab returns to the box, the app window notices and finishes. Explains the expected "Google hasn't verified this app → Advanced → Go to Wizard" screen. |

Every further account: **one step** (Connect another account). Allowing more for an account: one step (Allow more… = incremental consent with `include_granted_scopes`, `login_hint`, and a check that the same Google account came back).

Time: about five minutes for a first-timer; most of it is reading Google's own forms.

### Why this order

- Name and publish come **before** the client because Google won't create a client until the app is named, and publishing early means no first sign-in is ever a 7-day one.
- APIs come **after** the client is pasted because the client file tells Wizard the exact project. Enabling APIs in the wrong project (an owner with several) is a common failure that this order removes.

## What is automated, deep-linked or manual, and why

| | What | Why |
|---|---|---|
| **Automated (no owner action)** | Redirect URI and authorized domain (from the box's own address); client validation with Google; project id; Testing-mode detection; API-enabled check; scope check per account; wrong-account refusal on reconnect | Everything Wizard can learn from the address bar, the client file, the token endpoint and the API error bodies. |
| **Deep-linked (one page, one press)** | Create project, Turn on APIs | Console URLs accept the API list and `project=`. |
| **Manual on a deep-linked page** | Name the app (consent screen), Publish, Create client | **Google offers no API for these.** The only API that ever came close, the IAP OAuth Admin API (`projects.brands`, `identityAwareProxyClients`), only made Internal-brand IAP clients and was **shut down on 19 March 2026**. `gcloud iam oauth-clients` is Workforce Identity Federation and cannot do Gmail/Drive consent for Google accounts. There is no Google Auth Platform API. |

### Could the app do more via API after a one-time owner login?

Partly, and we chose not to:

- Projects (`cloudresourcemanager` v3 `projects.create`) and API enablement (`serviceusage` `batchEnable`) do work with a `cloud-platform` user token. But getting that token needs an OAuth client first: either Google's own gcloud client (not ours to embed) or an **AI Wizards-owned client**, which is exactly the middleman James ruled out, and which would need Google verification for the `cloud-platform` scope. It would also save little: "Create project" and "Turn on APIs" are already one page each. An account that has never accepted the Cloud terms gets "Callers must accept Terms of Service" from the API anyway, so the owner must visit the console once regardless.
- The three steps that cost the owner the most (consent screen, publish, client) cannot be done by any API. Browser automation of the console (as some community tools do) is brittle and would mean driving the owner's signed-in Google session from our code; we don't.

## The return address (redirect URI)

**The box's own HTTPS address is the only supported redirect route**, `https://<box>.<tailnet>.ts.net/wizard-google/oauth/callback`. Google never fetches the redirect itself (it's a browser redirect), so a tailnet-only address works for the owner's devices. Nothing leaves the box except the call to Google. The real Google acceptance of the exact hostname/authorized domain still needs a real-client check; the prototype's tests use a labelled fake Google.

A box move to a new address only affects *new* sign-ins: existing refresh tokens don't use the redirect. The setup screen shows the new address to add to the client. There is no AI Wizards-hosted return page to configure or deploy.

## Things Google may show the owner

- "Google hasn't verified this app": expected for a personal app; Advanced → Go to Wizard. Google's personal-use exemption (fewer than 100 users) covers unverified apps, including restricted Gmail/Drive scopes; no security assessment applies.
- Testing mode: 7-day sign-ins. Detected and explained (step 3).
- Unused clients are deleted after 6 months: `deleted_client` is shown as "This client was deleted in Google Cloud".
- The client secret is shown only once (since 2025): if the JSON was lost, add a new secret on the client and download again; the app says this when the secret is missing.
