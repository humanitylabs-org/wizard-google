# Agent write approvals

## Contract

Standing grants establish eligibility per caller × account × operation. Apps continue to use those grants for reads and writes. Agents use the same grants for reads, but **all catalogue operations marked `writes: true` additionally require a new owner decision for each action**. Today those are `gmail.send` and `calendar.write`; this is not a two-name exception list.

Without `X-Wizard-Agent`, every MCP client is `agent:main` (Main agent). Until the Agent Service sends this header, its agents therefore share Main's grants and request history. With a header, that agent's grants apply. Callers cannot pass an approval flag, use a chat acknowledgement, or adopt an app identity through MCP.

## Propose → review → execute

1. The write tool checks the existing grant, selects the allowed account and checks its Google scope. It validates arguments locally (no Google call), JSON-copies and normalizes them (reject unknown fields, strip text as the typed handler does, show bare ASCII envelope addresses rather than display names, materialize the default calendar). It hashes this canonical packet of `{client, account, operation, args}` with SHA-256. The exact packet lives in the existing encrypted store; the content-free audit contains no message/event payload. A future write without an explicit review schema fails closed.
2. MCP returns `{status: "pending", request: {id, kind: "write", client, account, operations, args, payload_hash, created, expires, status}, tell_owner, app_link}`. **Nothing has been sent.** An identical still-pending packet deduplicates to the same card. There are at most five pending requests per caller; active cards cannot be evicted by history pruning.
3. The app home shows the caller, sending account, every supplied field (recipients/Cc, subject, full text; calendar, times, guests, location, description) and expiry with **Approve / Deny**. It uses existing Appearance v1 surfaces, dark standalone fallback, text-only content rendering, and re-dresses live with the shell's skin. Read-only home polling brings in new cards while the app is open.
4. Only the authenticated owner route can decide: `POST /api/requests/<id>/approve` or `/decline` with `{payload_hash}` (optionally the same internal account ID). Same-origin and CSRF controls apply **in addition to** a private `WIZARD_OWNER_TOKEN` injected as `X-Wizard-Owner-Token` only by the owner-authenticated gateway (plus the configured Tailscale login in Tailscale mode). Host/Origin/login headers alone are not authentication from a local agent. Production fails closed without the gateway secret; the gateway must not inject it on MCP/consumer routes or expose it to agents, browser code or responses. Approval cannot carry replacement arguments; a wrong hash/account or a recomputed packet mismatch is `409 payload_changed`. The browser sends the hash from the card it displayed.
5. Under the account lock and state lock, the broker checks pending status, exact hash and `now < expires`, then **durably saves `executing` before making any Google submission**. It rechecks account, grant and scope before submitting. The approval is consumed regardless of outcome. There is no agent-facing approval/execution capability and no public bypass switch on `call`.
6. A successful Google response records `executed` and its result. Denial records `declined`; at 15 minutes pending becomes `expired`. Either sends nothing. Revoked grant, disconnected account or definitive rejection becomes `failed`. A timeout/5xx or a stranded `executing` on startup is `unknown`, not safe to retry. No background replay occurs. Consumed requests drop their stored argument bodies; hash and receipt remain in the bounded history.
7. Agents inspect **the same request ID** in `google_status.write_requests` (last 20 retained per caller), rather than calling the write tool again. The owner sees recent receipts on home. Repeated Approve returns `409 request_<status>`; a changed payload gets a new pending request and cannot borrow the old approval. Even the same payload after completion requires a new card.

## Boundaries and proof

- The connector gives **at-most-one logical submission per approval** in its single-process server. It does not promise provider-side exactly-once delivery, independent readback verification, cross-process file locking or crash-proof proof that Google accepted a request. Google error/response status is distinguished from independent verification. Check Google before proposing anything again after `unknown`.
- Host administrators/root, the connector's private executor, protected owner gateway, Agent Service identity headers and the App Manager grant provider remain trusted. This prototype is not an OS sandbox; another same-privilege actor with the store key or owner credentials is out of scope. Never run two copies over the same data folder. The loopback-only development fixture deliberately bypasses owner authentication and must never be used with real credentials/untrusted agents. Protected gateway token injection must be integrated before a real installation; this change does not alter the live shell.
- Reads and app standing-grant writes are unchanged. Neither setting an agent grant nor approving an access request executes a pending write.
- **Browser origin isolation is a release gate.** Same-origin/CSRF controls cannot isolate this owner UI from arbitrary other apps' JavaScript hosted on the same Wizard origin. A real integration must use a dedicated protected Google UI origin (with gateway token injection only there), or an equivalent browser isolation/owner ceremony that has been proved against same-origin app access. The existing same-origin hosted QA proxy tests Appearance transport and trusted owner interactions, not hostile same-origin apps. Do not interpret these prototype screenshots as proof of that deployment boundary; no shell/proxy changes were authorized here.
- Unit/HTTP tests use a labelled fake Google and count actual fake transport submissions. Browser QA exercises the real server/UI on the same-origin Appearance host proxy, including mobile-UA/touch, Approve/Deny, live skin change, reload/no replay and incoming cards. This is not a live Wizard install or a physical-device test, and real Google interoperability still needs an owner test client.

Run:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=app:tests python3 -m unittest discover -s tests -v
node relay/test.mjs
PLAYWRIGHT_PATH=/path/to/playwright QA_SHOTS=/path/to/shots PYTHON=/path/to/python node qa/browser.cjs
```

Browser QA belongs on the existing remote QA host, with its checkout, Python environment, Chromium TMPDIR and screenshots on the external work volume. No live app, container or shell changes are needed. The return-address setup uses only the box's own HTTPS callback; no hosted return service is part of this flow.
