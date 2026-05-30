# Architecture Decision Record — Discharge Summary Assistant (Phase 1)

Status: **Proposed** (Phase 1 discovery — to be ratified before Phase 3 build)
Date: 2026-05-21
Author: Shina (drafted with Cowork)
Scope: three decisions that shape the serverless build — (1) Bedrock model choice,
(2) DynamoDB audit-log schema, (3) single-region eu-west-2 vs Bedrock regional availability.

> Format note: each ADR records Context → Decision → Consequences, so the reasoning
> survives even if the decision is later reversed. Verify model/region facts against
> the AWS console at build time — Bedrock availability changes month to month.

---

## ADR-001 — Foundation model: Claude on Amazon Bedrock

### Context
The app drafts three text outputs (discharge summary, GP letter, patient version)
from messy clinical notes. The model must follow a long, rule-heavy system prompt
faithfully (resus-change flagging, drug reconciliation, the "Not documented" rule,
a Flesch–Kincaid ≤ 8 patient version) and resist prompt injection embedded in the
notes field. The Notion tracker already commits to Bedrock for the AWS-portfolio
value (Cognito/KMS/serverless around it), so the decision is *which* Bedrock model,
not Bedrock vs. a direct API.

### Decision
Use **Anthropic Claude on Amazon Bedrock** as the primary model, accessed through a
**system-defined inference profile** (not a raw on-demand model ID). Standardise on a
**Claude Sonnet-class** model for the quality/cost balance the rule-following demands,
and pin the exact model + version string in config (not hard-coded) so it can be
upgraded deliberately and recorded in the audit log.

Rationale:
- Instruction-following and structured-output fidelity matter more here than raw
  speed; a Sonnet-class model is the sweet spot. Haiku-class is a fallback for the
  patient-version simplification pass if cost becomes a concern; Opus-class is
  reserved for eval/grading, not the hot path.
- Bedrock keeps the call inside the AWS account boundary (IAM, CloudTrail, VPC
  endpoints, KMS) — exactly the controls the governance section requires.
- Llama (also available in-region) is kept as a documented alternative for the
  model card's "models considered" note, but Claude is chosen for output quality.

**Model pinned — 2026-05-23 (eu-west-2 CLI check).**
`aws bedrock list-foundation-models --region eu-west-2 --by-provider anthropic`
returned these Sonnet-class options: `claude-sonnet-4-6` (**ON_DEMAND** + inference
profile), `claude-sonnet-4-5-20250929-v1:0` (inference-profile only), and the older
`claude-3-7-sonnet-20250219-v1:0` / legacy `claude-3-sonnet-20240229-v1:0` (on-demand).
**Pinned: `anthropic.claude-sonnet-4-6`** — the current-generation Sonnet that is
*also* available on-demand in London, so it satisfies ADR-003 rule 1 (UK-only
inference). This **supersedes this ADR's original assumption** that access would be
via an inference profile: the build-time check revealed on-demand is available
in-region, which is the stronger residency posture. `claude-opus-4-6-v1` is likewise
ON_DEMAND in eu-west-2 and is the candidate eval/grading model (kept off the hot path
per the cost stance above). Haiku-class (`claude-haiku-4-5-…`) is inference-profile-only
in-region, so a future Haiku patient-version pass would need the EU profile (ties to
open question #3). **On-demand invocation confirmed 2026-05-25** — invoking first required
clearing the Bedrock **Anthropic use-case gate** (a one-time, account-level use-case form,
submitted via the Model catalog since the standalone "Model access" page is retired). After
that, a `Converse` call to `anthropic.claude-sonnet-4-6` in eu-west-2 (on-demand) returned
successfully and the Phase-2 generate Lambda produced all three outputs with a hash-only
audit write. (An earlier draft of this ADR claimed the gate was cleared on 2026-05-23; that
was incorrect — it was actually cleared on 2026-05-25.) ADR-001 is now fully locked.

### Consequences
- Model version becomes an audited field — every generation logs the model+version
  used (already in the audit schema, ADR-002).
- Bedrock **fine-tuning is not available in eu-west-2**, so the design must rely on
  prompt engineering + few-shot, not fine-tuning. This is acceptable and arguably
  preferable for a portfolio (cheaper, no training-data governance burden).
- Newer Claude versions may only be reachable via cross-region/global inference
  profiles rather than single-region on-demand — see ADR-003.
- Prompt-injection mitigation lives in the application layer (input sanitisation +
  the "treat notes as data" system-prompt rule), not the model.

---

## ADR-002 — DynamoDB audit-log schema (hash-only, immutable)

### Context
NHS DSPT alignment and the model card require an **immutable, KMS-CMK-encrypted**
audit trail of every generation request that captures *who/when/what-model* — but
explicitly **not raw clinical content** (PHI must not land in the log or in
CloudWatch). We need to answer "who generated what, when, with which model, and was
it reviewed before download" without ever storing the patient notes.

### Decision
A single DynamoDB table, on-demand capacity, encrypted with a **customer-managed KMS
key**, with the following item shape:

| Attribute            | Type | Notes |
|----------------------|------|-------|
| `PK`                 | S    | `USER#<cognito_sub>` — partition by clinician |
| `SK`                 | S    | `GEN#<ISO8601_timestamp>#<ulid>` — sortable, unique per generation |
| `generation_id`      | S    | ULID, also returned to the UI |
| `user_sub`           | S    | Cognito subject (pseudonymous id, not name/email) |
| `timestamp`          | S    | ISO-8601 UTC |
| `input_sha256`       | S    | SHA-256 of the input notes — **hash only, never raw text** |
| `output_sha256`      | M    | one hash per output: `{summary, gp_letter, patient}` |
| `model_version`      | S    | e.g. `anthropic.claude-sonnet-4-6 (eu-west-2, on-demand)` |
| `output_type`        | S    | which tabs were generated |
| `draft`              | BOOL | `true` until the clinician ticks "I have reviewed this" |
| `reviewed_at`        | S    | ISO-8601, set when draft→false (nullable) |
| `request_region`     | S    | source region of the API call (eu-west-2) |
| `inference_profile`  | S    | the Bedrock profile ARN used (residency evidence) |
| `schema_version`     | N    | for forward migration |

Immutability & integrity:
- Application IAM role is granted `dynamodb:PutItem` and `UpdateItem` **only for the
  `draft`/`reviewed_at` transition** — no `DeleteItem`, no general overwrite. Deny
  delete in the resource policy.
- **Tamper-evidence mechanism — DECIDED 2026-05-23.** The table emits a
  **DynamoDB Stream**; a consumer (Lambda, or Kinesis Firehose for batching) writes
  each change event to an **S3 bucket with Object Lock (WORM)**. That immutable,
  append-only S3 ledger — keyed to the `input_sha256`/`output_sha256` hashes — is the
  tamper-evidence control, because in Object Lock the record cannot be altered or
  deleted by a compromised IAM principal.
    - **Demo:** Object Lock **Governance mode**, ~1-day retention — immutable to
      normal principals, but a specially-permissioned admin can override, so the demo
      can be torn down without long-term storage lock-in.
    - **Production:** Object Lock **Compliance mode** with the NHS-required retention
      — undeletable even by the root account until retention expires.
- **Point-in-Time Recovery stays enabled, but is the operational recovery net, NOT
  the tamper-evidence control.** PITR is recovery (restore the table after accidental
  corruption/deletion within 35 days), not immutability — it does not detect or prevent
  alteration of live items, and a privileged principal can disable it. Relying on
  PITR alone would make the "immutable audit log" claim in `THREAT_MODEL.md` /
  `README.md` an overclaim; the S3 WORM ledger is what makes that claim true.
- KMS CMK with a tight key policy; CloudTrail data events on the table.
- The only mutable field after creation is the review transition (`draft`,
  `reviewed_at`); everything else is write-once.

### Consequences
- The log proves usage and review compliance **without holding PHI** — a clean
  story for the THREAT_MODEL.md (Repudiation + Information-disclosure rows).
- Hash-only means you cannot reconstruct the note from the log (by design); if a
  future feature needs content retention, it requires a separate, explicitly
  consented, encrypted store and a new ADR.
- The `draft` flag wires directly to the UI gate (ADR-adjacent: the human-in-the-loop
  control), giving an auditable link between "reviewed" tick and the record.
- ULID in `SK` gives time-ordering for cheap per-clinician history queries.

---

## ADR-003 — Region strategy: single-region eu-west-2 vs Bedrock regional availability

### Context
NHS / UK-GDPR data-residency expectations push toward keeping processing in the UK
(eu-west-2, London). But Bedrock's newest Claude models are increasingly offered
**only through cross-region inference profiles** (e.g. an EU geographic profile), not
as single-region on-demand endpoints. So there is a real tension: *strict single-region
(UK-only) limits model choice; the EU geographic profile widens model choice but
processes transiently across EU regions.*

Verified during Phase 1 discovery:
- eu-west-2 hosts Bedrock with Anthropic Claude models (on-demand available for some
  versions).
- An **EU geographic cross-region inference profile** is available from eu-west-2. It
  keeps processing **within the EU geography** and routes across EU regions
  (e.g. Ireland, Frankfurt, Spain, and others per the profile version) for throughput.
- **Data at rest** (logs, S3 outputs, DynamoDB) stays in the **source region you
  deploy to** — the cross-region behaviour is about transient inference compute only.
- Fine-tuning is not available in eu-west-2 (reinforces ADR-001's no-fine-tune stance).

### Decision
**Deploy all stateful resources single-region in eu-west-2 (London)** — S3, DynamoDB,
Cognito, KMS, CloudFront origin, logs. For the Bedrock call, **default to single-region
on-demand in eu-west-2 where the chosen model is available; otherwise use the EU
geographic inference profile** (processing stays within the EU, an adequate
jurisdiction under UK GDPR). Record the actual `inference_profile` / region used on
every audit item (ADR-002) as residency evidence.

Decision rule, in priority order:
1. If the target model is available **on-demand in eu-west-2** → use it (UK-only
   processing, the cleanest residency story).
2. Else if available via the **EU geographic profile** → use it, and document in the
   model card that inference may transiently process in other EU regions while all
   data at rest remains in eu-west-2.
3. **Never** fall back to a US/global profile for this app — that would break the
   residency posture. If a model is only available US/global, it is out of scope.

**Resolved 2026-05-23 — rule 1 applies.** The pinned model
`anthropic.claude-sonnet-4-6` is available **on-demand in eu-west-2**, so the app uses
single-region on-demand invocation: both data at rest *and* inference stay in London.
The EU geographic profile is retained only as a documented fallback (rule 2) for the
case where on-demand capacity is unavailable at runtime. Every audit item records
`request_region = eu-west-2` and the on-demand path as residency evidence.

### Consequences
- Single-region stateful design keeps cost, latency, and the IAM/threat-model story
  simple — appropriate for an MVP and the SAA-C03 syllabus. No multi-region DR is in
  scope for v1 (document as a known limitation).
- Model choice is constrained to what eu-west-2 / the EU profile offers; if the
  preferred Sonnet version is EU-profile-only, that's accepted under rule 2.
- The audit log's `request_region` + `inference_profile` fields turn residency from a
  claim into evidence — useful for the DSPT/governance narrative.
- A documented residency boundary ("data at rest UK; inference within EU") is a
  defensible, honest position for a portfolio project and a real NHS conversation.

---

## ADR-004 — Front door: HTTP API (v2) + Cognito JWT authoriser

**Status:** Accepted (2026-05-26, slice 4a).

### Context

The generate Lambda (slice 2) is currently invoked directly (CLI/console). To
put it behind the React UI (slices 4b/4c) it needs a public, authenticated HTTP
endpoint. Two AWS choices exist for "Lambda behind Cognito-authenticated HTTP":

1. **API Gateway REST API (v1)** + **Cognito user-pool authoriser** (or a
   Lambda authoriser that calls Cognito).
2. **API Gateway HTTP API (v2)** + the **native JWT authoriser**.

### Decision

Use **API Gateway HTTP API (v2)** with the native **JWT authoriser**, configured
against the Cognito User Pool's OIDC issuer URL and the SPA App Client id as
the JWT `aud`. The route is a single `POST /generate` on the `$default` stage
with `AutoDeploy: true`.

### Rationale

- **Cost.** HTTP API is ~70% cheaper per million requests than REST API. For a
  portfolio demo this is small in absolute terms, but the right-size choice is
  itself the SAA-credible answer — not picking the more expensive product
  "because more features".
- **Latency.** HTTP API has lower per-request overhead and skips the v1
  request/response transformation pipeline we are not using anyway.
- **Native JWT support.** API Gateway v2 validates the IdToken's signature,
  issuer, audience, and `exp` *before* invoking the integration. A v1 REST API
  achieves the equivalent only via either (a) the Cognito user-pool authoriser,
  which is less flexible and ties us to v1, or (b) a custom Lambda authoriser,
  which adds a second Lambda invocation (and a second cold start) on every
  request. The v2 JWT authoriser removes that latency without removing the
  control.
- **Surface area we don't need.** REST API's bigger feature set — usage plans,
  API keys, request validators, x-amz-mock integrations, EDGE endpoints — is
  exam material but unrelated to this app's hot path. Keeping the front door
  narrow keeps the threat model narrow.

### Identity is asserted by the authoriser, never by the client

The handler must take the clinician's Cognito `sub` from
`event.requestContext.authorizer.jwt.claims.sub` — populated by API Gateway
**after** the JWT is cryptographically verified — and must **never** trust a
`user_sub` field in the request body. The body is client-controlled, so any
authenticated user could otherwise attribute their generation to another user
in the audit log. This is enforced in `src/generate/app.py`: the HTTP-API code
path ignores body identity and reads only the verified claim; the direct-invoke
path (slice 2/3 smoke tests) keeps the body-supplied `user_sub` because there
is no JWT to verify against.

### Cognito posture for the demo

- **MFA OPTIONAL + TOTP (SOFTWARE_TOKEN_MFA).** Users *may* enrol TOTP MFA; the
  pool does not force it for every sign-in. This is the honest middle ground:
  the README can truthfully say "Cognito MFA supported" without making the
  recorded demo painful. Flipping `MfaConfiguration` to `ON` in prod is a
  one-property change.
- **No self-signup.** `AllowAdminCreateUserOnly: true`. For synthetic-data
  demos this prevents the public API from doubling as an account-creation
  endpoint.
- **Public SPA app client.** `GenerateSecret: false` — a browser cannot keep
  secrets. Auth flows are SRP, USER_PASSWORD (smoke test), and refresh.
- **`PreventUserExistenceErrors: ENABLED`** so the API does not differentiate
  "user not found" from "wrong password" — a defensive default that costs
  nothing.

### Consequences

- The hot path is now `Browser → CloudFront (slice 4c) → HTTP API → JWT
  authoriser → generate Lambda → Bedrock + DynamoDB + S3 WORM ledger.` Every
  step except the browser is in eu-west-2 (CloudFront is global by design).
- The frontend (slice 4b) uses Amplify Auth (SRP), gets an IdToken, and
  passes it as `Authorization: Bearer <IdToken>`. The IdToken (not the access
  token) is required, because Cognito puts the App Client id in `aud` only on
  the IdToken.
- CORS is permissive (`http://localhost:5173`) for the dev loop in 4a/4b. In
  4c the app and the API share a single CloudFront hostname, so the browser
  sees them as same-origin and CORS becomes a no-op.

### Alternatives considered

- **REST API + Cognito user-pool authoriser.** Functionally fine, but slower
  and more expensive with no benefit for this app. Kept as a documented
  alternative in case a future requirement (e.g. usage plans for partner
  organisations) forces it.
- **REST API + Lambda authoriser.** Adds a Lambda invocation per request just
  to do what the v2 native authoriser does for free. Rejected on latency.
- **AppSync (GraphQL) + Cognito.** Overkill for a single mutation endpoint;
  GraphQL caching/subscriptions are not in scope.

### Slice 4a findings (2026-05-26 smoke test)

Slice 4a was verified end-to-end against the live stack on 2026-05-26 using
the recipe in `infra/SLICE_4A_SMOKE_TEST.md`: unauth call → 401, malformed
token → 401, authed call → 200 + three outputs, audit row's `user_sub`
matched the JWT's `sub` claim (not anything supplied in the request body),
new WORM ledger object landed within seconds. The JWT-as-trusted-identity
contract holds in real AWS, not just in unit tests.

Two findings worth recording honestly before slice 4b begins:

1. **HTTP API has a hard 30-second integration timeout.** Bedrock Sonnet
   emitting three outputs at `MAX_TOKENS = 4096` runs ~20–25 s on warm
   containers and longer on a cold start. The first smoke-test call (a
   multi-line NSTEMI ward-round note on a cold container) hit the cap; the
   client got `503 Service Unavailable` with no Lambda logs visible in the
   first 30 s, because Lambda was still mid-Bedrock call when API Gateway
   cut the integration. Warm + shorter input (a one-line UTI note) returned
   200 in 20.7 s. Mitigations, in order of preference:

   - **Async pattern (preferred for production).** `POST /generate` returns
     `202 Accepted` with a job id; Lambda kicks off the Bedrock call via
     `EventBridge` or a second `Invoke` (`InvocationType: Event`); the
     client polls a second `GET /generations/{id}` endpoint that reads from
     the audit table. Removes the timeout entirely and gives the UI an
     honest progress indicator. This is what slice 4b will move to.
   - **Reduce `MAX_TOKENS`** from 4096 to ~1500. Faster, but truncates
     longer admissions — clinically suboptimal.
   - **Lambda Function URL + response streaming**, fronted by CloudFront.
     Bypasses API Gateway's 30 s cap. Trade-off: no built-in JWT
     authoriser, so Cognito auth would have to move to CloudFront
     functions / Lambda@Edge — heavier and a step away from the AWS
     "default" stack for this shape of app.

2. **Lambda completes server-side even when the client gets a 5xx**, so the
   first failed call left behind a "ghost" audit row and a ghost WORM
   ledger object. This is correct behaviour at the data layer — the
   audit log should reflect *attempts*, not just *successes* — but it has
   two consequences for the UI in slice 4b:

   - A naïve "retry" button would re-invoke Bedrock and duplicate the
     audit row. The UI must therefore either disable retry until status
     is confirmed via a `GET` (the async pattern again), or compute a
     client-side idempotency key sent with the request and refuse to
     write a second row with the same key.
   - When clinicians review the audit log they will see rows whose
     `parse_ok` and `output_sha256` reflect a generation the *client*
     never saw. The Model Card and UI copy should make this honest:
     **"every attempt is logged, including ones the system failed to
     deliver."**

These are recorded here rather than as a separate ADR-005 because they are
direct consequences of the ADR-004 architecture choice (HTTP API + sync
Lambda) — anyone reviewing the architecture should encounter them at the
same place they encounter the decision. The **fix** to both of them — moving
the hot path to an async `202 + poll` pattern with client-supplied
idempotency — is recorded in ADR-005 below.

---

## ADR-005 — Async `202 + poll` pattern with client-supplied idempotency

**Status:** Accepted (2026-05-26, slice 4b).

### Context

ADR-004 / slice 4a put the generate Lambda behind API Gateway HTTP API
synchronously. The slice-4a smoke test surfaced two consequences that the
production hot path cannot ship with:

1. **API Gateway HTTP API has a hard 30 s integration timeout.** It cannot
   be raised. Bedrock Sonnet emitting three outputs at `MAX_TOKENS = 4096`
   runs ~20–25 s warm and longer on a cold start. Even shaving `MAX_TOKENS`
   would truncate longer admissions — clinically the wrong trade. The cap is
   load-bearing.
2. **Lambda completes server-side even when API Gateway has 503'd the
   client.** The first failed call left a "ghost" audit row + WORM ledger
   object behind, and a naïve client "retry" would double-invoke Bedrock —
   double-billing, double-logging, and (since the audit table is write-once)
   visibly distinct rows that look like the user submitted twice.

The same constraints apply to anything else in this app whose latency is
naturally variable and uncapped (later: optional second-pass patient-version
regeneration; future: longer Opus eval-grading runs).

### Decision

Move `POST /generate` to the **async `202 + poll` pattern**, with
client-supplied **idempotency keys**:

```
client ──POST /generate──▶ Dispatcher Lambda  ──Invoke(Event)──▶ Generate (worker)
                                │  writes pending audit row              │
                                │  returns {job_id, status:"pending"}    │
                                │  HTTP 202 in <1s                       │
client ──GET /generations/{id}──▶ Status Lambda                          │
                                │  reads audit row + (if complete) outputs
                                │  returns {status, outputs?, error?}    │
                                                                         │
                  worker UpdateItem-s row to status=complete (with hashes,
                  parse_ok, tokens) and PutItem-s outputs into ResultsTable
```

Three Lambdas — `DispatcherFunction`, `StatusFunction`, and the worker
(`GenerateFunction`, slice-2 code refactored). Two routes on the same HTTP
API, gated by the same JWT authoriser. One additional DynamoDB table for the
transient outputs.

### Rationale

- **The 30 s ceiling stops being load-bearing.** The dispatcher does one DDB
  transaction + one async invoke — sub-second. The worker runs for as long
  as Bedrock needs (Lambda's own timeout is the only ceiling, set at 60 s
  here). The status endpoint is two `GetItem`s — also sub-second.
- **Ghost records are converted into honest pending rows.** The dispatcher
  writes the row in `status=pending` BEFORE invoking the worker. If the
  worker fails or the network drops mid-poll, the row tells you the truth:
  there's a job in flight that you should expect to terminate. There is no
  scenario where the client sees an error but the audit/ledger show a
  completed generation, because completion is no longer how the client
  finds out.
- **Idempotency is enforced server-side.** Each `POST` carries an
  `Idempotency-Key` (a client UUID). The dispatcher runs a
  `TransactWriteItems` that puts the `IDEM#<key>` row AND the
  `GEN#<job_id>` pending row atomically, with `attribute_not_exists` guards.
  A retried POST with the same key fails the IDEM `ConditionCheck`, rolls
  the transaction back, and the dispatcher returns the EXISTING `job_id`
  with a `200` (not `202`). The worker is fired exactly once.
- **JWT-as-identity contract from ADR-004 holds.** Both new Lambdas read
  `user_sub` from `event.requestContext.authorizer.jwt.claims.sub`. The
  status Lambda's `GetItem` uses `PK = USER#<jwt.sub>`, so a cross-user
  read returns nothing — surfaced as 404 (uniform with "no such job"), not
  403 (which would confirm existence).
- **Hash-only audit invariant from ADR-002 holds.** The actual outputs are
  not in the audit table — they live in a separate `ResultsTable` with
  `PK = USER#<sub>, SK = RES#<job_id>` and a **24-hour TTL**. The audit
  table continues to carry only hashes, who/when, and operational metadata.
  ResultsTable is a delivery buffer; it can be wiped at any time and the
  audit log is intact.

### Data model changes

The audit table (ADR-002) gains two SK prefixes (one new, one repurposed):

| PK | SK | What |
|----|----|------|
| `USER#<sub>` | `GEN#<ulid>` | One row per generation. **slice 4b: SK uses the bare ULID** (was `GEN#<timestamp>#<ulid>` in slice 2). ULIDs are already time-sortable, so dropping the timestamp prefix lets the status Lambda do a direct `GetItem` by `job_id`. The legacy slice-2 direct-invoke path keeps writing the old SK shape for backwards compatibility. |
| `USER#<sub>` | `IDEM#<key>` | Idempotency receipt. TTL ~24 h. Maps `(user_sub, idempotency_key) -> job_id`. The GEN# row it points to has NO TTL — only the receipt expires. |

DynamoDB TTL is enabled at the table level on attribute `ttl`. GEN# rows
omit the attribute → never expire. IDEM# rows carry it → expire after 24 h
(best-effort, within 48 h of expiry per AWS docs).

The new `ResultsTable`:

| PK | SK | Attrs |
|----|----|-------|
| `USER#<sub>` | `RES#<job_id>` | `summary`, `gp_letter`, `patient`, `completed_at`, `ttl` |

SSE-KMS with the same CMK as the audit table; NO Streams (it's not part of
the audit trail); NO PITR (transient by design); 24 h TTL.

### Idempotency contract

- The header is `Idempotency-Key: <UUID>`. UUIDv4 is the expected shape; the
  dispatcher validates the canonical 8-4-4-4-12 hex form and rejects
  anything else with 400.
- The header is **optional**. If absent, each POST is a fresh job (no
  idempotency receipt is written).
- If present and seen before for the same `user_sub`, the dispatcher returns
  the **current** status of the existing job (not the status at first
  submission). Status `pending` on replay is fine — the client polls anyway.
- Idempotency keys are **scoped per user_sub**. Two different users using
  the same UUID get different jobs.
- The body sent on replay is **not** compared against the original — the
  dispatcher records the input hash on the IDEM# row for forensics but does
  not enforce equality, in line with the Stripe idempotency-key contract.

### Status state machine

```
        ┌─ complete ──── (outputs available; draft=true, awaiting review)
        │
pending ┼─ failed ────── (error_code + error_message; no outputs)
        │
        └─ expired ──── (status=complete in audit, but ResultsTable TTL’d)
```

- `pending` is the only state the dispatcher writes. Only the worker flips
  it (with `ConditionExpression: status = pending`, so a Lambda async retry
  cannot clobber a finalized row).
- `expired` is synthesised by the **status** Lambda when the audit row says
  `complete` but the ResultsTable row is missing. The audit trail survives;
  the outputs do not.

### Anti-spoof

- POST: `user_sub` comes from JWT claims. Body-supplied `user_sub` is
  silently ignored (and not echoed in any response or log).
- GET: the PK of the lookup embeds the JWT sub. A client cannot read
  another user's job even if they know the `job_id`.
- Cross-user reads return 404 (uniform with "no such job"), not 403 —
  matching the User Pool's `PreventUserExistenceErrors` posture from
  ADR-004.

### Consequences

- The hot path now spans **three Lambdas** instead of one. The dispatcher
  and status functions are tiny (256 MB, ≤10 s timeouts, no third-party
  deps), so the cost and cold-start surface area increase is modest.
- The HTTP API surface grows by **one route** (`GET /generations/{id}`)
  using the same JWT authoriser.
- The slice-4a `aws lambda invoke` smoke test path is **preserved**. The
  worker detects the dispatcher-event shape vs the legacy direct-invoke
  shape and behaves correctly for both. An HTTP-API-shaped event arriving
  at the worker by accident returns `410 Gone` rather than silently
  regressing to the 30 s problem.
- The model card / README copy from slice 4a ("every attempt is logged,
  including ones the system failed to deliver") remains true and is now
  **observable** through the status endpoint: a client that polls a
  pending job and never sees `complete` knows precisely that the attempt
  was logged.

### Alternatives considered

- **Lambda Function URL + response streaming.** Bypasses the 30 s cap, but
  the JWT authoriser is API Gateway-only — Cognito auth would have to move
  to CloudFront Functions / Lambda@Edge. A heavier, less standard stack
  for a portfolio shape.
- **Reduce `MAX_TOKENS` to 1500 to fit in 30 s.** Truncates longer
  admissions (the late-stages NSTEMI / GI bleed / multi-co-morbidity cases)
  — clinically the wrong direction.
- **`input_sha256`-keyed dedupe instead of a client header.** Surprising
  behaviour: two genuinely separate identical requests would collapse into
  one job. Breaks the "every attempt is logged" invariant. Rejected.
- **Separate "jobs" table.** Considered, but the audit table is already
  the system of record for "did this generation happen and for whom".
  Reusing it (with two SK prefixes) keeps the WORM ledger seeing state
  transitions for free — the immutable evidence covers `pending →
  complete/failed` without an extra stream.

---

## Open questions to close before Phase 3
- ~~Confirm the exact Claude model + version available on-demand in eu-west-2~~ —
  **RESOLVED 2026-05-23:** pinned `anthropic.claude-sonnet-4-6`, **ON_DEMAND** in
  eu-west-2 (satisfies ADR-003 rule 1 — UK-only inference). `claude-opus-4-6-v1` also
  on-demand in-region as the eval/grading candidate. See ADR-001 / ADR-003.
- ~~Decide the tamper-evidence mechanism for the audit log~~ — **RESOLVED 2026-05-23:**
  DynamoDB Streams → consumer → S3 Object Lock (WORM) is the tamper-evidence control
  (demo = Governance mode + ~1-day retention; prod = Compliance mode + NHS retention);
  PITR retained as operational recovery, not the immutability control. See ADR-002.
- ~~Confirm whether the patient-version simplification runs as a second model pass
  or a single combined prompt~~ — **RESOLVED 2026-05-23: Option A, single combined
  prompt.** One inference emits all three outputs (clinician summary, GP letter,
  patient version). Evidence-led: the Run 3 cold evals (S8–S18) produced patient
  versions at **Flesch–Kincaid 3.6–6.2**, comfortably under the ≤ 8 target, using the
  single combined v0.5 prompt — so a separate simplification pass is not needed to hit
  the reading-age bar. Combined-prompt also keeps the whole hot path UK-only on-demand
  (a Haiku second pass would be inference-profile-only in eu-west-2, leaving the
  strict residency posture). **Future enhancement (v2):** a second pass that
  regenerates the patient version from the *clinician-reviewed* summary — justified by
  clinical safety (anchoring the leaflet to approved content), NOT by reading age. This
  ties to the human-in-the-loop control and the Model Card's "confidence in outputs"
  theme. **Built 2026-05-30 (v2a, flag-gated `PATIENT_V2_SECOND_PASS`, off by default):**
  the worker now regenerates PART C in a separate Bedrock pass whose only input is the
  curated PART A — architectural belt-and-braces over the v0.6 prompt rule, on the same
  Sonnet on-demand model (residency unchanged). Anchoring to the *clinician-edited*
  summary via a review-gated endpoint (v2b) remains the follow-on. See
  `docs/PATIENT_V2_DESIGN.md`.

---

## ADR-006 — Static SPA delivery: single CloudFront distribution, two origins

**Status:** Accepted (2026-05-27, slice 4c).

### Context

Slice 4b ended with a Vite + Amplify Auth SPA running locally against the
deployed HTTP API, with API Gateway CORS allow-listing `localhost:5173`.
For a portfolio demo the SPA must be reachable from a hosted URL, served
over HTTPS, behind the same JWT-authenticated path the API uses. The
constraints inherited from earlier ADRs:

- ADR-002: no PHI may sit in static assets or in cache.
- ADR-003: inference + data-at-rest remain in `eu-west-2`.
- ADR-004 / ADR-005: the HTTP API + Cognito JWT authoriser stay as-is;
  this slice must not weaken or re-architect them.

### Decision

A **single CloudFront distribution** with **two origins**:

- **S3 (private, OAC-only)** — serves the Vite-built SPA. Default cache
  behaviour, `CachingOptimized` policy, SPA-routing handled via
  `CustomErrorResponses` (403/404 → `/index.html` 200).
- **API Gateway HTTP API (eu-west-2, imported)** — serves `/generate` and
  `/generations/*`. Path-pattern cache behaviours, `CachingDisabled`,
  `AllViewerExceptHostHeader` origin-request policy.

Lives in a **separate `discharge-web` CloudFormation stack** in the same
region (eu-west-2), importing `HttpApiId` + `HttpApiDomain` via
`Fn::ImportValue` from the existing `discharge-audit` stack.

Default `*.cloudfront.net` hostname for now — no custom domain, no ACM cert
in this slice. The us-east-1 ACM hooks are present as commented-out
scaffolding in `infra/web-template.yaml` for a later cutover.

### Why a single distribution, two origins (vs S3 and API on separate hostnames)

The alternative is the classic SPA-on-S3-website + API-on-`*.execute-api.*`
shape with CORS allow-listing the SPA's domain on the API.

The single-distribution shape wins on three independent axes:

1. **Eliminates CORS preflight for every API call.** Browsers only enforce
   CORS for *cross-origin* requests. When the SPA at
   `https://x.cloudfront.net/` calls `fetch('/generate')`, both the page
   origin and the request destination are `https://x.cloudfront.net` —
   same origin — so the browser skips preflight entirely. The
   `idempotency-key` and `authorization` headers go in the actual POST,
   not in a separate `OPTIONS` round-trip. One less request, one less
   API Gateway invoice line item.
2. **One TLS cert, one access-log story, one URL.** Cuts ops surface area
   and removes "which domain is the API on?" from the demo narration.
3. **No CORS configuration drift.** The CORS allow-list on the API still
   contains only `http://localhost:5173` (for `npm run dev`); we
   deliberately do NOT add the CloudFront hostname there because, by the
   same-origin argument above, it would be dead config. Adding it would
   invite a future engineer to assume the API was *meant* to be reachable
   cross-origin and drop the allow-list when convenient. Same-origin =
   no CORS = no allow-list entry needed.

### Why OAC over OAI

**Origin Access Identity (2009)** was the legacy mechanism: CloudFront
authenticated to S3 as a special user-like principal listed in the bucket
policy. It did not support SSE-KMS-encrypted buckets, struggled with newer
S3 regions, and was permitted only on GET/HEAD.

**Origin Access Control (2022)** uses **SigV4**: CloudFront signs each
origin request with its own service credentials. Supports SSE-KMS, all
regions, all HTTP methods, and removes the "special principal" footgun.
AWS-recommended for all new work since 2023. There is no scenario in 2026
where new OAI is the right answer.

The bucket policy uses an `aws:SourceArn` condition pinning access to this
specific distribution's ARN. Without that condition, *any* CloudFront
distribution in *any* account that knew the bucket name could read the
bucket — the classic confused-deputy hole. With it, only `discharge-web`'s
distribution can. Same pattern as SNS→Lambda, EventBridge→target, SES→S3;
once internalised it pays dividends across the SAA-C03 syllabus.

### Why path-pattern behaviours with `AllViewerExceptHostHeader`

CloudFront evaluates cache behaviours top-down by path pattern; the default
behaviour runs only when nothing else matches. `/generate` (POST) and
`/generations/*` (GET) sit above the default, routing to the API origin
with `CachingDisabled` because per-user JWT-authenticated responses must
never be shared across viewers.

The AWS-managed origin-request policy `AllViewerExceptHostHeader`
(ID `b689b0a8-53d0-40ab-baf2-68738e2966ac`) forwards every viewer header —
Authorization, Idempotency-Key, Content-Type, cookies, query string — to
the API origin **except** the `Host` header. CloudFront rewrites `Host` to
the origin's hostname (`*.execute-api.eu-west-2.amazonaws.com`). Without
this rewrite, API Gateway HTTP API responds 403 because the Host doesn't
match its own DNS — a frustrating silent failure that's the first thing to
suspect if a slice-4c smoke test returns 403 on `/generate` while the same
call works against the API directly.

### Why custom error responses 403/404 → /index.html 200

The SPA does client-side routing. Reloading `/some/deep-link` hits S3,
which returns 404 because no such object exists. Without intervention the
viewer sees a CloudFront error page. With `CustomErrorResponses` rewriting
both 403 (OAC-readable bucket, key missing) and 404 (any other missing key)
to `/index.html` with status 200, the SPA's router takes over and renders
the right view.

This rewrite is *distribution-wide*, but the more specific cache
behaviours for `/generate` and `/generations/*` intercept the path before
the rewrite has a chance to fire — verified in §6.4 of
`infra/SLICE_4C_DEPLOY_AND_SMOKE.md`.

### The us-east-1 ACM rule (deferred, documented)

CloudFront viewer certificates **must live in us-east-1**, regardless of
where the distribution, origins, or any other resources are. CloudFront is
a global service whose control plane reads ACM from one region only — a
cert in eu-west-2 is invisible to it.

This slice does not exercise the rule (no custom domain), but the future
cutover is fully documented as a commented block at the bottom of
`web-template.yaml`: a second stack in us-east-1 owns the cert, its ARN is
passed into the eu-west-2 web stack as a parameter (cross-region
`Fn::ImportValue` does not exist), and the existing `ViewerCertificate`
block is swapped for `AcmCertificateArn` + `Aliases`. Same applies to WAFv2
web ACLs for CloudFront (`SCOPE=CLOUDFRONT`, region us-east-1) and to any
Lambda@Edge functions.

### Why a separate `discharge-web` stack

CloudFront distribution updates take 5–15 minutes to propagate to all edge
PoPs; the stack does not return `UPDATE_COMPLETE` until propagation
finishes. Bundling CloudFront into the existing `discharge-audit` stack
would mean every backend iteration (a Lambda env-var bump, a CORS tweak)
paid the full propagation cost. Splitting keeps the backend iteration
loop sub-minute and confines the slow update path to genuine CloudFront
changes, which are rare.

Both stacks live in eu-west-2, so cross-stack values use the normal
`Fn::ImportValue` against same-region exports. If we'd put CloudFront in
its own region (which we briefly considered, since the resource itself is
global), we'd need either an SSM Parameter Store cross-region read or a
stack parameter — another reason to keep things in eu-west-2 for now.

### Defence-in-depth response headers

A `ResponseHeadersPolicy` is attached to both API path behaviours:
HSTS (1 year, includeSubdomains), `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`,
and a minimal CSP that allows same-origin scripts/styles, same-origin XHR
(covering both SPA and API since they share the hostname), and Cognito IdP
endpoints for the Amplify SRP exchange. Cheap to add, expected by any
security reviewer, and a useful talking point for the demo.

### Consequences

- The SPA gets HTTPS, HTTP/2+HTTP/3, edge caching, and SPA routing for
  free. First-byte latency from a UK viewer should be ~30-60 ms (UK edge
  PoP) for static assets and dominated by Bedrock for `/generate`.
- The HTTP API CORS allow-list keeps `http://localhost:5173` (for `npm
  run dev`) and gains nothing else, because the production path is
  same-origin.
- The `discharge-audit` stack now has exports it can't change while
  `discharge-web` is importing them. This is the export safety lock and
  is intentional — it's the guard against accidentally bricking the web
  stack via a backend edit.
- CloudFront PriceClass_100 limits PoPs to NA + EU. Sufficient for a UK
  NHS demo audience; bump to `_200` if the audience ever shifts.
- The S3 bucket is `DeletionPolicy: Retain` with versioning on, so
  accidental object deletes leave delete-markers (recoverable) and a
  stack-delete leaves the bucket alive.

### Alternatives considered

- **CloudFront with the S3 *website* endpoint as origin.** Rejected: the
  website endpoint is HTTP-only and bypasses bucket policy ACLs, so the
  bucket would have to be public. Defeats the whole point of OAC and
  fails any reasonable threat-model review.
- **AWS Amplify Hosting.** Faster initial setup but bundles deploy +
  CloudFront + auth into one product, opaquely. The portfolio learning
  value is in seeing the explicit OAC + bucket policy + custom error
  responses + cache behaviours wired by hand.
- **S3 + CloudFront for the SPA, API kept on its `*.execute-api.*`
  hostname with CORS.** Working, common, but adds a preflight to every
  state-changing API call and means two TLS certs/two hostnames to
  document. No upside over the chosen shape.
- **Adding the CloudFront hostname to API Gateway CORS allow-list.**
  Considered for completeness, rejected: it would be dead config (the
  prod call is same-origin) and would invite future devs to assume the
  API was meant to be cross-origin from the SPA — leading to the
  drop-the-allow-list change that would silently break only the dev
  workflow. Same-origin in prod means CORS is genuinely not in the
  picture; the config should reflect that.

### Verification (post-deployment, 2026-05-27 12:17 UTC)

The stack deployed clean and a representative generation job completed
through the full new path. Live identifiers captured for reproducibility:

| Resource | Value |
|---|---|
| CloudFront distribution domain | `d97dn8vzuz1u0.cloudfront.net` |
| CloudFront distribution ID | `E1GQ7L05AVH8YI` |
| Private SPA bucket | `discharge-web-spabucket-d5rb2e4ujkrd` |
| Stack | `discharge-web` (eu-west-2) |

Each of the three architectural payoffs of this design was verified
directly in the browser via Chrome DevTools' Network panel, rather than
taken on trust:

1. **Same-origin routing.** The POST issued by the SPA showed its request
   URL as `https://d97dn8vzuz1u0.cloudfront.net/generate` — the
   CloudFront hostname, not `*.execute-api.eu-west-2.amazonaws.com`.
   Confirms the SPA is calling the same origin it was served from, and
   that the path-pattern cache behaviour routed `/generate` to the API
   origin without exposing the API hostname to the browser.
2. **Zero preflight.** No `OPTIONS` row immediately preceded the POST,
   despite the request carrying the custom `Idempotency-Key` header.
   Confirms the browser treated this as a same-origin request and
   skipped CORS preflight entirely — the practical benefit on which the
   single-distribution design rests.
3. **Defence-in-depth headers attached to the API behaviour.** The POST
   response headers included `content-security-policy: ...`,
   `x-content-type-options: nosniff`, and
   `strict-transport-security: max-age=31536000; includeSubDomains`.
   Confirms the `ResponseHeadersPolicy` is attached to the `/generate`
   cache behaviour, not only to the default S3 one — an easy thing to
   miss when wiring multiple behaviours and worth specifically checking
   in any future review.

Data-layer evidence corroborated the network view. A `GEN#` row appeared
in `AuditTable` for the test user (sub `1682f284-…-6528`) with
`SK=GEN#01KSMNVNG9CMNKV8FZVAV34CSM` (ULID format = slice-4b async path,
not legacy direct-invoke), `draft=true`, all three `output_sha256`
populated, `completed_at=2026-05-27T12:17:54.941018+00:00`. The
hash-only audit invariant held (no PHI in the row) and the corresponding
ledger objects landed in the WORM bucket within seconds. The full chain
SPA → CloudFront → API Gateway → JWT auth → Dispatcher → Worker →
Bedrock → DynamoDB → Streams → S3 Object Lock therefore executed
end-to-end through the new CloudFront perimeter without weakening any of
the earlier slices.

### Implementation notes (gotchas worth recording)

These do not change the decision, but cost real time during the build
and are easy to repeat. Recorded here so the next person — or a
recruiter reading the source — sees what was learned:

- **`PublicAccessBlock` is named `PublicAccessBlockConfiguration` in
  CloudFormation.** The S3 web console labels the feature "Public access
  block", which is the obvious property name to guess. CFN templates
  fail validation if you guess.
- **cfn-lint `E1029` on `${...}` inside Parameter `Description` fields.**
  `Description` is not an `Fn::Sub` context, so `${ThingName}` in there
  triggers the lint rule. Escape with placeholder notation like
  `<ThingName>` in descriptions; reserve `${...}` for `Fn::Sub`
  contexts.
- **Zsh treats `<` and `>` as redirection.** Smoke-test recipes that
  contain `<YOUR_THING>` placeholders break in zsh as soon as the user
  copy-pastes the example. Use plain `YOUR_THING` (or quote the line)
  in any pasted-in shell snippet.
- **DynamoDB attribute names are case-sensitive.** A query against
  `PK=...` fails with `ValidationException: Query condition missed key
  schema element: PK` if the table actually declares `pk`. Mixing the
  two cost a smoke-test round-trip. Stick to one convention and check it
  against the table's actual schema, not the recipe.
- **Stack `Description` has a hard 1024-character limit** (slice 4b
  finding, still applies). A descriptive multi-line block can quickly
  exceed it and is rejected at change-set creation with
  `ValidationError: 'Description' length is greater than 1024`. Trim
  before committing.
