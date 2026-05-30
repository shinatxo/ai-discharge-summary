# Wave 3 — synthetic-traffic canary (design sketch)

> Status: **DESIGN / for greenlight** · Author: Cowork session 2026-05-30 · Phase 3, Wave 3
> Relates to: Phase 3 scope theme (4) eval-corpus replay harness; feeds Wave 4 alarms.
> No code here — this exists so the build can be greenlit with the decisions on the table.

## 1. Why

Two jobs in one harness:
1. **Baseline traffic.** Wave 4's CloudWatch alarms (latency, error rate, Bedrock
   throttles) need realistic numbers to set thresholds against. A quiet demo stack
   emits no data, so alarms would be guesswork. A nightly synthetic run lays down a
   steady baseline to calibrate against — start it now so ~5–7 days have accumulated
   by the time Wave 4 lands.
2. **Nightly regression detection.** Replaying the 18 eval scenarios through the
   *deployed* async path catches drift the unit tests can't: a prompt change, a model
   update, an IAM/throttle regression, a parse failure in production conditions.

This is a **custom synthetic canary** — a scheduled job that drives the real stack as a
user would and reports health as metrics.

## 2. Architecture

```
EventBridge Scheduler (nightly 02:00 Europe/London)
        │  invoke
        ▼
  Canary Lambda  (src/canary/app.py)
        │ 1. InitiateAuth (USER_PASSWORD_AUTH) as the synthetic user
        │      -> Cognito IdToken     (password from SSM SecureString)
        │ 2. FAN OUT: POST /generate for every scenario  -> collect job_ids
        │      (async 202 returns immediately; no waiting per job)
        │ 3. POLL: GET /generations/{id} for all job_ids until terminal / timeout
        │ 4. Score each: latency, status, parse_ok, model/patient_version, drift
        │ 5. PutMetricData -> CloudWatch  (namespace DischargeAssistant/Canary)
        │    (optional) archive outputs -> S3 for diffing
        ▼
  CloudWatch metrics  ───────────────►  Wave 4 alarms + SNS
```

The deployed API contract (confirmed in `src/dispatcher`, `src/status`, `ui-spa/src/api.ts`):
- `POST /generate` — headers `authorization: Bearer <IdToken>`, `content-type: application/json`,
  `idempotency-key: <uuid>`; body `{"notes": "..."}` → **202** `{job_id, status, poll_url, ...}`.
- `GET /generations/{job_id}` — header `authorization: Bearer <IdToken>` → `{status:
  pending|complete|failed|expired, parse_ok, model_version, outputs?, output_sha256?, ...}`.

### 2a. Bounded-concurrency submit + poll
Because `/generate` is async (202 in <1s), the canary doesn't block per job. But a naive
"fire all 18 at once" **self-inflicts Bedrock on-demand throttling** — observed on the very
first live run (2026-05-30): 1/18 succeeded, the rest throttled. So the canary uses a
**sliding window**: at most `CANARY_MAX_CONCURRENCY` (default 5) generations in flight at a
time; as each finishes it submits the next, with a small stagger between submissions. This
keeps total Bedrock concurrency under the account quota, is more representative of real
traffic (no user sends 18 simultaneous requests), and still parallelises (wall-clock ≈ a few
waves, not the serial sum). Throttled jobs are **requeued once** (`CANARY_THROTTLE_RETRIES`)
rather than scored as failures — but the throttle is always recorded as a metric, so the
capacity signal for Wave 4 isn't lost. Idempotency keys are fresh per submission.

## 3. Auth + secret

- A **dedicated synthetic Cognito user** (e.g. `canary@synthetic.invalid`), admin-created,
  no MFA, permanent password — isolated from real demo users so its traffic is taggable
  and its blast radius is nil (it can only do what any signed-in user can: generate drafts).
- The canary calls `cognito-idp:InitiateAuth` with `AuthFlow=USER_PASSWORD_AUTH` (enabled on
  the SPA client) and reads the **IdToken** (the JWT authoriser checks `aud` = App Client id,
  which Cognito sets on the IdToken, not the access token).
- Password stored in **SSM Parameter Store SecureString** (KMS-encrypted; free tier; one
  value). Canary role gets `ssm:GetParameter` + `kms:Decrypt` on that one parameter only.

## 4. Metrics (namespace `DischargeAssistant/Canary`)

Emitted per run, with a `Scenario` dimension per scenario plus an `ALL` aggregate:

| Metric | Unit | Feeds (Wave 4) |
|---|---|---|
| `EndToEndLatencyMs` | Milliseconds | latency alarm (p90) |
| `EndToEndSuccess` | Count 1/0 | success-rate / availability alarm |
| `ParseOk` | Count 1/0 | parse-failure alarm |
| `JobFailed` | Count | failure-count alarm |
| `BedrockThrottle` | Count | throttle alarm (capacity) |
| `CanaryRunOk` | Count 1/0 | "did the canary itself run" heartbeat alarm |

`CanaryRunOk` matters: a *missing* metric (canary didn't fire / couldn't auth) should alarm,
or an outage hides behind silence.

## 5. Regression signal — honest scope
Exact `output_sha256` equality is **not** a reliable regression gate: even at temperature 0
Bedrock has minor non-determinism, so byte-identical output across nights isn't guaranteed.
So the canary asserts **structural invariants** instead and emits `ParseOk` / a soft
`OutputDrift` signal rather than failing on a hash diff:
- `parse_ok` true (all three PARTs present and ordered),
- each PART non-empty; patient version present,
- (optional) Flesch–Kincaid of PART C within band (≤ 8).
Raw outputs can be archived to S3 for *manual* diffing when a metric looks off — drift is a
prompt to look, not an automatic failure.

## 6. Scope + cost
- **Scenarios:** the 18 expansion scenarios (S1–S18 inputs), bundled as JSON with the Lambda
  (or read from `evals/scenarios/`). Subsettable via env `CANARY_SCENARIOS`.
- **Cost:** ~18 Bedrock Sonnet generations/night in eu-west-2 (temperature 0, ≤4096 tokens) —
  small. All inference + data stay in eu-west-2 (ADR-003 unchanged).
- **Cadence:** nightly. A weekly full + nightly subset is an option if cost/runtime ever bites.

## 7. Target path — API Gateway direct (v1)
The canary hits the **API Gateway endpoint directly** (`HttpApiEndpoint` output), exercising
Cognito → dispatcher → worker → Bedrock → DynamoDB → ledger — the backend health path the
alarms care about. A separate lightweight **CloudFront GET canary** (does the SPA load over
the CDN?) is a clean follow-on once the custom domain is wired in Wave 4, and is out of scope
here.

## 8. What's IaC vs one-time bootstrap
- **IaC (in `infra/template.yaml`):** Canary Lambda + role, EventBridge Scheduler schedule +
  its invoke role, the SSM parameter resource (value set out-of-band), CloudWatch log group.
- **One-time bootstrap (documented runbook, run by Shina):** create the synthetic Cognito
  user + set a permanent password (`admin-create-user` / `admin-set-user-password`), and put
  the password into the SSM SecureString. Plain CloudFormation can't set a Cognito user
  password, so this stays a short documented manual step — honest and SAA-reasonable.

### IAM least-privilege (canary role)
- `cognito-idp:InitiateAuth` — not resource-scopable to a single client (public-style API);
  accepted as a documented `*` exception, same posture as the existing `dynamodb:ListStreams`.
- `ssm:GetParameter` + `kms:Decrypt` — on the one canary-password parameter only.
- `cloudwatch:PutMetricData` — no resource scoping available; constrained by namespace in
  practice. Own log group. **No** DynamoDB/Bedrock/S3 perms — the canary only talks HTTP to
  the API as a normal user, so it inherits the same least-privilege as any client.

## 9. Out of scope (Wave 4 / later)
- The alarms + SNS topic themselves (Wave 4 — this wave only produces the metrics they watch).
- CloudFront front-door canary; WAF; custom-domain wiring.
- Hard regression-fail on output hash (kept a soft, look-here signal — see §5).
