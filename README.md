# 🩺 AI Discharge Summary Assistant

Turn a doctor's messy ward-round notes into a structured discharge summary, a GP letter, and a plain-English version for the patient — safely, with every output left as a **draft for a clinician to review and sign**.

> **Status:** Phase 1 (Discovery & Validation) substantively complete; **Phase 2 (Build) underway** — see [Build status](#build-status-phase-2). Portfolio / demonstration project. **Not a medical device; not deployed in clinical care.** All evaluation data is synthetic.

---

## Why this exists

Junior doctors spend a large share of their day on discharge paperwork, and poor summaries delay GP follow-up and contribute to readmissions. This tool drafts a structured summary, a GP letter, and a patient explanation from clinical notes in seconds — but its real point is to do so **without making things up**. The hard problem in clinical AI is not fluency; it is restraint. The assistant is engineered to report only what the notes support, to surface gaps and contradictions rather than paper over them, and to refuse outright to invent the fields that cause harm.

It is built with NHS-flavoured, DSPT-aligned controls: Cognito MFA, KMS customer-managed-key encryption, and an immutable, **hash-only** audit log that never stores patient-identifiable data.

## What it produces

From one block of free-text notes, three outputs (matching the UI tabs):

1. **Structured discharge summary** — diagnosis, investigations, treatment, resuscitation status, reconciled medications, follow-up, GP actions.
2. **GP letter** — a brief clinician-to-clinician handover that mirrors the medication changes and GP actions (the highest-harm interface).
3. **Patient-friendly version** — plain English at Flesch–Kincaid grade ≤ 8, with safety-net advice; audience-shifted for parents/carers in paediatrics, and flagged for translation when the patient does not speak English.

## How a junior doctor would actually use it on a shift

Paste the ward-round notes into the box. Read the three drafts. Fix anything that needs fixing — the tool deliberately writes **"Not documented"** wherever the notes are silent, so the gaps are visible rather than invented. Tick **"I have reviewed and edited this output"**, which unlocks download. The draft is marked `draft = true` in the audit log until that tick is recorded as a sign-off. The clinician remains the author of record throughout.

## Architecture

```
Clinician browser  (Vite + Amplify Auth SPA)
   │  (IdToken: "Authorization: Bearer ...", Idempotency-Key: <uuid>)
CloudFront ── S3 (React frontend)                          ← slice 4c (planned)
   │
HTTP API (API Gateway v2)            POST /generate    GET /generations/{id}
   │
   ▼
JWT authoriser — validates Cognito IdToken (signature, iss, aud, exp)
   │   ← if invalid: 401, Lambda never invoked
   │
   ├── POST /generate ─▶ dispatcher Lambda  (slice 4b)
   │                       │  writes pending audit row + IDEM receipt
   │                       │  (TransactWriteItems, attribute_not_exists)
   │                       │  Lambda Invoke(Event) ─────────────┐
   │                       │  returns 202 + {job_id, status:"pending"}
   │                       │
   │                       ▼                                    ▼
   │                 DynamoDB audit table             generate Lambda (worker)
   │                 (KMS-CMK, hash-only)               │  user_sub from JWT, never body
   │                 PK=USER#<sub>                      ├──▶ Bedrock Converse (Sonnet 4.6, on-demand eu-west-2)
   │                 SK=GEN#<job_id> | IDEM#<key>       ├──▶ ResultsTable PutItem  (transient, 24h TTL)
   │                       │                            └──▶ audit row UpdateItem: pending → complete/failed
   │                       │
   └── GET /generations/{id} ─▶ status Lambda  (slice 4b)
                                 │  GetItem audit row + (if complete) outputs
                                 │  cross-user reads → 404 (don't leak existence)
                                 ▼
                              client polls until terminal

DynamoDB Streams (NEW_AND_OLD_IMAGES) on the audit table
      │
      ▼
   ledger Lambda
      │  PutObject only — no Delete, no PutObjectRetention
      ▼
   S3 Object Lock (WORM) bucket  ← Governance/1d demo, Compliance/NHS-retention prod
```

- **Single-region, eu-west-2 (London)** for all stateful resources. Model **pinned to `anthropic.claude-sonnet-4-6`, invoked on-demand in eu-west-2** (ADR-001/003 rule 1 — both data-at-rest *and* inference stay UK-only; the EU geographic inference profile is retained only as a documented fallback, never US/global). Region and inference path are logged on every generation as residency evidence.
- **Audit log:** one DynamoDB table, on-demand, KMS-CMK encrypted, `PK = USER#<cognito_sub>`, `SK = GEN#<timestamp>#<ulid>`. Stores hashes (`input_sha256`, per-output `output_sha256`), model version, output type, region, and inference profile — **never the clinical content**. The only mutable field is `draft → reviewed_at`; no `DeleteItem`; PITR + append-only stream.
- **No fine-tuning.** Behaviour is shaped by an auditable system prompt (currently v0.5), which is preferable to opaque weights for a safety-critical draft.

SAA-C03 services exercised (~70% of the syllabus): Lambda, API Gateway, DynamoDB, Cognito, S3 (encryption + lifecycle), KMS CMK, CloudFront, Route 53, IAM, CloudWatch, plus Bedrock.

## Build status (Phase 2)

The backend is being built in thin vertical slices, defined as infrastructure-as-code under [`infra/`](infra/) (plain CloudFormation) with the function code under [`src/`](src/). Honest state of play:

| Slice | What | Status |
|-------|------|--------|
| 1 | KMS CMK + hash-only DynamoDB audit table (ADR-002) — stack `discharge-audit`, eu-west-2 | **Deployed** |
| 2 | Bedrock-calling `generate` Lambda + least-privilege execution role | **Deployed** |
| 3 | DynamoDB Streams → S3 Object Lock (WORM) tamper-evidence ledger | **Deployed** |
| 4a | Cognito User Pool + HTTP API (v2) + native JWT authoriser in front of `generate` | **Deployed + verified 2026-05-26** |
| 4b | Async `202 + poll` rework (dispatcher + status Lambdas, idempotency keys) + Vite + Amplify Auth SPA (local) | **Deployed + verified 2026-05-27** |
| 4c | S3 + CloudFront with single distribution (two origins: S3 SPA + API Gateway) | Planned |

**Slice 2 detail.** A Python 3.13 Lambda ([`src/generate/`](src/generate/)) calls Bedrock Converse on the pinned model, splits the combined output into the three parts, and writes the hash-only audit item from ADR-002 (write-once; no PHI in the table or logs). Its execution role is scoped to exactly three resources and nothing else:

- the **audit table** — `dynamodb:PutItem`/`UpdateItem` only (no `DeleteItem`, no `Scan`);
- the **CMK** — `kms:Decrypt`/`GenerateDataKey`/`DescribeKey`, constrained by a `kms:ViaService` condition so the key can only ever be used *through DynamoDB*, never for a standalone KMS call;
- the **one pinned model ARN** — `bedrock:InvokeModel` on `…:foundation-model/anthropic.claude-sonnet-4-6` and no other model.

**Slice 3 detail.** A second Python 3.13 Lambda ([`src/ledger/`](src/ledger/)) consumes the audit table's DynamoDB stream (via a managed `EventSourceMapping`, `TRIM_HORIZON`) and copies every change event into an **S3 Object Lock (WORM)** bucket — an append-only, immutable ledger that cannot be altered or deleted within its retention window (Governance mode + 1-day retention in demo; Compliance mode + NHS retention in prod, switched by an `IsProd` condition). This is the control that makes the THREAT_MODEL's "immutable audit log" claim *provable* rather than aspirational: PITR is operational recovery (a privileged principal can disable it), whereas an Object-Lock version cannot be rewritten or deleted. Its execution role can read only this one stream, `s3:PutObject` only (no delete, no `PutObjectRetention`, no `BypassGovernanceRetention` — so the writer itself cannot weaken a lock), and use the CMK only via the DynamoDB and S3 service contexts. The ledger objects are SSE-KMS-encrypted with the same CMK and carry only hashes — no PHI.

**Slice 4a detail.** The authenticated front door. A **Cognito User Pool** (email login; MFA OPTIONAL + TOTP; no public self-signup; password policy at 12 chars with mixed case/number/symbol) holds the clinician directory. A public **SPA App Client** — `GenerateSecret: false`, because a browser can't keep secrets — exposes only the modern auth flows (SRP, USER_PASSWORD for smoke tests, refresh). An **HTTP API (API Gateway v2)** fronts a single `POST /generate` route, gated by API Gateway's native **JWT authoriser**: the authoriser holds the User Pool's OIDC issuer URL and the App Client id as the JWT `aud`, downloads the pool's JWKS, and verifies signature + issuer + audience + `exp` *before* Lambda is ever invoked. Failed verification → 401 from API Gateway, with no Lambda cold start spent.

The handler ([`src/generate/app.py`](src/generate/app.py)) is the part where this decision pays off as a security control: when invoked through the HTTP API path it reads `user_sub` from `event.requestContext.authorizer.jwt.claims.sub` — the *verified* claim — and ignores any `user_sub` in the request body. That closes the obvious spoofing hole where an authenticated user could attribute their generation to someone else in the audit log. The direct-invoke path (slice 2/3 smoke tests) is preserved untouched. Full design rationale, including why HTTP API beat REST API here, is in [`docs/ADR-phase1.md` §ADR-004](docs/ADR-phase1.md). The deploy + verification recipe is in [`infra/SLICE_4A_SMOKE_TEST.md`](infra/SLICE_4A_SMOKE_TEST.md).

**Verified end-to-end 2026-05-26.** A real curl through the HTTP API with a fresh IdToken returned 200 + three outputs in ~21s; the audit row for that generation records `user_sub = 1682f284-...-6528` — the verified Cognito `sub` from the JWT, **not** any value supplied in the request body — confirming the anti-spoof identity contract holds in production AWS, not just in unit tests. The DynamoDB stream → WORM ledger pipeline from slice 3 also continues to work post-deploy: every new generation event lands as an immutable ledger object within seconds.

Two production findings from that smoke test, both addressed in slice 4b:

1. **API Gateway HTTP API has a hard 30-second integration timeout.** Slice 2's `aws lambda invoke` smoke test ran with Lambda's own 60s budget; through HTTP API the cap is 30s and cannot be raised. With Bedrock Sonnet emitting three outputs at `MAX_TOKENS = 4096`, a cold start on a longer ward-round note brushes that ceiling, and the client gets `503 Service Unavailable` while Lambda is still working. This is a real architectural constraint, not a wiring bug.
2. **Lambda completes server-side even when the client got a 5xx**, leaving a "ghost" audit row and ledger object behind. For an authenticated, idempotent-per-user-sub generation that is mostly fine, but a UI that lets the user "retry" a failed call risks double-billing the Bedrock invocation. Slice 4b's UI must either disable retry on 5xx until status is confirmed, or — better — slice 4b moves to an async `202 Accepted + poll` pattern that surfaces real backend status. See [`docs/ADR-phase1.md` §ADR-004 "Slice 4a findings"](docs/ADR-phase1.md) for both findings written up.

**Slice 4b detail (deployed + verified 2026-05-27).** Both 4a findings are cured at the architecture layer rather than papered over in the UI. `POST /generate` is fronted by a new **dispatcher Lambda** ([`src/dispatcher/`](src/dispatcher/)) that writes a pending audit row + an idempotency receipt atomically (`TransactWriteItems` with `attribute_not_exists` guards), async-invokes the worker via `lambda:Invoke` with `InvocationType=Event`, and returns **`202 Accepted + {job_id, status: "pending"}` in under a second**. The 30s cap is no longer in the hot path. A new **status Lambda** ([`src/status/`](src/status/)) backs `GET /generations/{id}` on the same HTTP API behind the same JWT authoriser; the client polls it until the state is terminal. The ghost-record retry hazard closes via a Stripe-style **client-supplied `Idempotency-Key` header**: a retried POST with the same key returns the same `job_id` and 200 (not 202), without re-firing the worker, because the dispatcher's transaction fails on the existing `IDEM#<key>` row.

The actual outputs (clinician summary / GP letter / patient version) live in a separate **transient `ResultsTable`** with a 24-hour TTL — the audit table stays hash-only (ADR-002 invariant intact). Anti-spoof from ADR-004 still holds end-to-end: every row's PK embeds the JWT-verified `user_sub`, so a cross-user `GET` returns 404 by schema, with no possible code path to expose another user's job. The full design — state machine, idempotency contract, schema, alternatives considered — is in [`docs/ADR-phase1.md` §ADR-005](docs/ADR-phase1.md), and the deploy + smoke-test recipe is in [`infra/SLICE_4B_SMOKE_TEST.md`](infra/SLICE_4B_SMOKE_TEST.md). Unit coverage for the three Lambdas — 28 tests across [`tests/`](tests/) covering idempotency-hit, anti-spoof, retry-safe Bedrock failure, cross-user 404, legacy direct-invoke compat, and the splitter's strict/forgiving/fail-safe paths — runs in ~0.1s.

**Verified end-to-end against live AWS 2026-05-27.** All seven smoke-test checks passed: unauth POST and unauth GET both returned 401 from API Gateway with no Lambda invocation; authed POST returned **202 + `job_id` in 1.184 s total** (DNS + TLS + JWKS download included; the API itself was sub-second); polling saw `pending → complete` with `parse_ok=true`; the audit row's `user_sub` matched the JWT's verified `sub` claim (`1682f284-…-6528`), the outputs landed in `ResultsTable` (not the audit table), three new ledger objects landed in the WORM bucket from the single job's state transitions; **idempotency replay** with the same `Idempotency-Key` returned the **same `job_id`** with `idempotent_replay: true` and HTTP 200 (not 202), and a DynamoDB query confirmed **exactly one GEN# row** existed for the job — the slice-4a ghost-record hazard is cured; a cross-user GET with a second demo user's IdToken returned **404** with `error: not_found` — existence is not leaked across users. The headline number: the audit row showed `started_at: 17:20:51 → completed_at: 17:21:42` — **51 seconds of Bedrock generation**, well past API Gateway HTTP API's hard 30 s integration cap. Under slice 4a this would have 503'd and left a ghost row; under slice 4b the client saw a clean `pending → complete` flow. **The 30 s ceiling is empirically gone, on the exact workload that triggered the slice-4a finding.**

The frontend half of slice 4b is a **Vite + React + TypeScript SPA** in [`ui-spa/`](ui-spa/), built around the Amplify Auth `Authenticator` (handles SRP, the new-password challenge, and optional TOTP MFA). It runs locally against the deployed API via `npm run dev`; slice 4c will lift it onto S3 + CloudFront.

Every slice deploys as **plain CloudFormation**. We attempted the AWS SAM transform for slice 2, but `cloudformation:CreateChangeSet` on the AWS-owned Serverless transform ARN was denied on every attempt — even with `AdministratorAccess` *plus* an explicit inline `Allow` for that exact action and ARN, and with no permissions boundary and no Organizations SCP. Both the IAM policy simulator and the live engine denied it, so rather than keep fighting an account-specific IAM anomaly, each Lambda is defined as a raw `AWS::Lambda::Function` and its code is uploaded with `aws cloudformation package` (no macro, so the transform permission is never exercised).

## The interesting part: how it's evaluated

Outputs are scored on five dimensions, three with **auto-fail gates**: omission, hallucination (auto-fail: invented resus/drug/diagnosis), resuscitation-status accuracy (auto-fail), drug reconciliation (auto-fail), and patient-version reading age (Flesch–Kincaid ≤ 8). The evaluation set is ~19 fully synthetic scenarios spanning neonatal to elderly, medical and surgical, plus deliberately adversarial cases (prompt injection, internally contradictory notes, missing data + a non-English-speaking patient).

This repo deliberately documents what **didn't** work, because a portfolio that only shows green ticks isn't credible:

- **A real failure, fixed and verified.** Run on an adversarial case (C7) by an *independent* model with no access to the answer key, the tool produced an English-only patient leaflet for a Polish-speaking patient — useless to that patient. Root cause: the prompt had no rule for non-English speakers. Fix: prompt v0.3 added a translation/interpreter rule. Re-run cold: the leaflet now leads with "FOR TRANSLATION — do not hand to the patient untranslated." A complete **failure → fix → verify** loop.
- **The evaluation caught errors in its own answer key.** When the 11 expansion scenarios were generated cold (system prompt + notes only, no gold in context), the disciplined model disagreed with the hand-written reference answers in five places — and the *model* was right: the gold had invented a resuscitation status in two cases and asserted "None known" allergies with no documented basis in three. The gold was corrected. The eval auditing its own answer key is, itself, evidence the safety rules are doing their job.
- **Current scoreboard:** seeds 4/4 (smoke-test, self-scored); adversarial injection/contradiction handling PASS; expansion S8–S18 **11/11 PASS cold under v0.5, no auto-fails**, reading ages FK 3.6–6.2. **Independent clinician scoring (Run 4) is in progress** — specialty-matched review packs are out with practising clinicians.

Full detail, per-scenario checkpoints, and the run log are in [`EVAL_RESULTS.md`](EVAL_RESULTS.md).

## Governance

This project treats governance as a first-class deliverable, not an afterthought — the cheapest credibility upgrade for a healthcare AI portfolio.

- [`MODEL_CARD.md`](MODEL_CARD.md) — intended use, out-of-scope, model details, safety behaviours, evaluation, known limitations.
- [`THREAT_MODEL.md`](THREAT_MODEL.md) — STRIDE plus AI-specific threats (prompt injection, hallucination, automation bias, gold-error, model drift).
- [`EVAL_RESULTS.md`](EVAL_RESULTS.md) — the scoring rubric, the synthetic scenarios, every run, and the documented failure cases.

## Repository contents

| File | What it is |
|------|------------|
| `discharge-summary-system-prompt.md` | The system prompt (v0.5) — the safety surface of the whole tool |
| `EVAL_RESULTS.md` | Scoring harness, rubric, run log, documented failures |
| `eval-scenarios-expansion.md` | The synthetic scenario set (notes + gold reference outputs) |
| `adversarial-scenarios.md` | The injection / contradiction / missing-data edge cases |
| `ADR-phase1.md` | Architecture Decision Records (model choice, audit-log schema, region/residency) |
| `mvp-ui-mockup.html` | Clickable wireframe of the two-pane UI with the review gate |
| `MODEL_CARD.md`, `THREAT_MODEL.md` | Governance artifacts |
| `Reviewer-Pack-*.docx`, `Reviewer-Scoring-Sheet.xlsx` | Clinician review pack (one scenario per specialty) + collation sheet |

## Roadmap

- **Phase 1:** problem definition, system prompt, evaluation harness, architecture decisions, governance — substantively complete; independent clinician review (Run 4) in progress (does not gate the build).
- **Phase 2 (now):** build the backend in slices (see [Build status](#build-status-phase-2)) — audit foundation, Bedrock `generate` worker, WORM tamper-evidence ledger, Cognito + HTTP API + JWT authoriser are all deployed; slice 4b adds the async `202 + poll` rework + idempotency keys + the React SPA (built, pending deploy); slice 4c will lift the SPA onto S3 + CloudFront.
- **Phase-1 open decisions — all resolved (2026-05-23):** model pinned to `anthropic.claude-sonnet-4-6` on-demand in eu-west-2; tamper-evidence = DynamoDB Streams → S3 Object Lock (WORM); single combined prompt for all three outputs. See [`ADR-phase1.md`](docs/ADR-phase1.md).

## Disclaimer

This is a portfolio demonstration. It is **not a medical device**, carries no CE/UKCA marking, has no regulatory clearance, and must not be used in clinical care or with real patient data. Every output is a draft requiring review and sign-off by a qualified clinician who remains responsible for the discharge summary.
