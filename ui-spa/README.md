# `ui-spa/` — slice 4b React + Amplify Auth SPA

Vite + React + TypeScript single-page app that authenticates against the
Cognito User Pool and talks to the slice-4b async API:

- `POST /generate` (returns 202 + `job_id` immediately, dispatcher fires the
  Bedrock worker asynchronously)
- `GET /generations/{job_id}` (polled until status is terminal)

This is **slice 4b's deliverable**. It runs locally against the deployed API.
Slice 4c will move the build to S3 + CloudFront with a custom domain — the
allow-listed origin in `infra/template.yaml`'s `CorsConfiguration` will be
tightened then.

## Prereqs

- Node 20+
- The discharge-audit stack deployed in eu-west-2 (see
  `infra/SLICE_4B_SMOKE_TEST.md`)
- A demo Cognito user (the slice 4a recipe creates one)

## Run

```bash
cd ui-spa
cp .env.example .env.local
# Fill .env.local with the CFN stack outputs:
#   VITE_USER_POOL_ID, VITE_USER_POOL_CLIENT_ID, VITE_API_BASE
npm install
npm run dev
```

Open <http://localhost:5173/>, sign in with the demo user, paste a ward-round
note, click **Generate drafts**.

## What you should see

- The "Submit" button POSTs to `/generate` with an `Idempotency-Key` header.
- The response comes back in **&lt; 1 second** with `{job_id, status: "pending"}`.
- The status panel begins polling `/generations/{job_id}`. While the worker
  runs the Bedrock call (~20–25 s warm), the badge says **pending**.
- When the worker finishes, the badge flips to **complete** and the three
  outputs render in tabbed view — discharge summary, GP letter, patient
  version — alongside the input + per-output SHA-256 hashes that match
  what's in the audit table.

## What the SPA does NOT do (yet)

- The "I have reviewed and edited this output" checkbox + sign-off
  transition (ADR-002's `draft -> reviewed_at` flip) — that's a later UI
  slice.
- The S3 + CloudFront hosting (slice 4c).
- A custom Cognito sign-in screen — we use the
  `@aws-amplify/ui-react` `Authenticator` drop-in, which handles
  email/password, the new-password challenge, and TOTP MFA enrolment for
  free (matching `MfaConfiguration: OPTIONAL + SOFTWARE_TOKEN_MFA` on the
  User Pool).

## Anti-spoof + idempotency, at the UI level

- Submit is disabled while a POST is in flight (single click → single
  request).
- The same `Idempotency-Key` is reused across retries of a given submission
  (a network error then a re-click). It is regenerated on **Generate
  another**.
- The user's identity (`user_sub`) is taken from the verified JWT by the
  dispatcher Lambda — the SPA does **not** send a user_sub in the body.
  Even if the user tried to spoof one in browser devtools, the dispatcher
  would ignore it.
