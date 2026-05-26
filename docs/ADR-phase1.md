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
same place they encounter the decision.

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
  strict residency posture). **Documented future enhancement (v2):** a second pass that
  regenerates the patient version from the *clinician-reviewed* summary — justified by
  clinical safety (anchoring the leaflet to approved content), NOT by reading age. This
  ties to the human-in-the-loop control and the Model Card's "confidence in outputs"
  theme.
