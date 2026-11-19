# WS7 entrypoint

The WS7 entrypoint is the job the weekly Fargate task will run ([ADR-011](adr/ADR-011-ws7-scheduled-eval.md),
slice 2). It is being built as six pull requests. This document grows with each one.

| Part | What | Status |
|---|---|---|
| 1 | The client wrapper and the reservation | **Built** |
| 2 | The claim, the ledger, the match check and the run manifest | **Built** |
| 3 | Comparator follow-ups from PR #4 | Not built |
| 4 | The run summary and the candidate baseline | Not built |
| 5 | The modes, the comparator call and publishing | Not built |
| 6 | The image changes | Not built |

Nothing here makes an AWS call or a model call, and nothing has been deployed or run live.
Every reply, usage figure and request text in the tests is invented.

## Part 1: the client wrapper and the reservation

Two modules and one config file:

- `evals/ws7_reserve.py`: the worst-case reservation, the cost of one call, and the config loader.
- `evals/ws7_client.py`: the client wrapper the entrypoint hands each runner.
- `evals/ws7_config.json`: the author's settings.

No runner, no scorer and no file in `src/generate/pipeline/` is changed. The modules read the
runners' and the steps' constants when they run; they never copy them.

### Money units

- Inside WS7, money is integer **nano-dollars** (one dollar is 1,000,000,000). Every rate is a
  whole number of nano-dollars per token, so the arithmetic is exact.
- The ledger and the metrics use **micro-dollars** (`cost_usd_micros`, `docs/WS7-COMPARATOR.md`).
  A conversion from nano to micro rounds up, so a cost is never understated.
- The monthly cap is 1,500 pence at 80 pence a dollar: **18,750,000 micro-dollars**, rounded down.

| Rate | Nano-dollars per token | From |
|---|---|---|
| Input | 3,300 | `evals/run_cold_eval.py:115` ($3.00 per million), times `:116` (1.10) |
| Output | 16,500 | the same ($15.00) |
| Cache write | 4,125 | the same ($3.75) |
| Cache read | 330 | the same ($0.30) |

### The reservation

For every model step of every generation, the reservation counts:

- **output:** the step's `maxTokens`, one attempt, at the output rate;
- **input:** the step's input allowance, times the margin, at the cache-write rate, the highest
  input rate.

| Step | `maxTokens` | Set at | Sent at | Input allowance (tokens) | From |
|---|---|---|---|---|---|
| 2 `extract_facts` | 8,192 | `src/generate/pipeline/extract.py:75` | `:121` | 6,000 | `evals/run_pipeline_eval.py:75` |
| 5a `reconcile_medications` | 4,096 | `src/generate/pipeline/reconcile.py:58` | `:251` | 7,000 | `evals/run_pipeline_eval.py:76` |
| 6 `compose_clinical` | 6,000 | `src/generate/pipeline/compose.py:85` | `:411` | 5,000 | `evals/run_pipeline_eval.py:76` |
| 7 `compose_patient` | 3,000 | `src/generate/pipeline/compose_patient.py:103` | `:431` | 2,500 | `evals/run_pipeline_eval.py:78` |
| v1 `combined` | 4,096 | `evals/run_cold_eval.py:96` | `:333` | 5,600 | `evals/run_cold_eval.py:121` |
| v1 `patient_v2` | 1,500 | `evals/run_cold_eval.py:98` | `:333` | 3,300 | `:122` (8,900) less `:121` (5,600) |

- **The input allowances are provisional.** They are the runners' planning figures, split by step
  as the comment at `evals/run_pipeline_eval.py:75-78` splits them. The pipeline's four add up to
  the runner's own `EST_TOKENS` (`:79`, 20,500). `plan()` refuses to build if they stop adding up.
  The baseline replaces them with measured figures (ADR-011 slice 4).
- **The margin is a multiplier written as a percentage.** The allowance is the base figure times
  `input_allowance_margin_percent / 100`, rounded up to a whole token. **100 means the base figure
  itself, not widened.** The config accepts 100 to 1,000.
- `tests/test_ws7_reserve.py` checks that every cited line still says what this table says.

At the approved margin of 100:

| Run | Generations | Reservation | Output part | Input part |
|---|---|---|---|---|
| Pipeline, 18 + 1 pre-flight | 19 | 8,280,476 micro-USD, £6.62 | £5.34 | £1.29 |
| v1, 18 + 1 pre-flight | 19 | 2,451,884 micro-USD, £1.96 | £1.40 | £0.56 |
| Pipeline baseline, 18 x 3, **excluding** the pre-flight | 54 | 23,533,983 micro-USD, £18.83 | £15.17 | £3.65 |
| Pipeline baseline, 18 x 3 + 1 pre-flight (**the figure used**) | 55 | 23,969,798 micro-USD, £19.18 | £15.46 | £3.72 |

These match ADR-011's Costs table. ADR-011's original £18.83 is 54 generations and excludes the
pre-flight. The baseline as ruled on 18 Nov 2026 (e8) is three batches of 18 with one pre-flight,
55 generations, £19.18, outside the cap. Run `python evals/ws7_reserve.py` to print them.

The dearest single request by the planning figures is step 2: 8,192 output tokens and 6,000 input
tokens, 159,918,000 nano-dollars, about £0.13. **That is an estimate, not an enforced limit.** The
output is capped by `maxTokens`; the input is not capped by anything in the request, so a real
call's input can exceed its allowance. See "How far a run can pass its reservation" below.

### The cost of one call

`read_usage()` prices the `usage` object Bedrock returns: `inputTokens`, `outputTokens`, and
`cacheReadInputTokens` and `cacheWriteInputTokens` when present (absent means zero). A count that
is missing, negative, a float, a string or a boolean makes the usage **unknown**, and the call is
then counted at its worst case. The result agrees with the runners' own `cost_usd()` to the
micro-dollar.

### The config file (`ws7-config/1`)

| Field | Value | Meaning |
|---|---|---|
| `monthly_cap_gbp_pence` | 1500 | The monthly ceiling of model spend (ADR-011 D7) |
| `gbp_pence_per_usd` | 80 | The rate `docs/COST.md` uses |
| `input_allowance_margin_percent` | 100 | See "The margin" above |
| `preflight_scenario` | `S1` | The one scenario run alone before the 18 |
| `wall_clock_limit_s` | `{"scheduled": 7200, "manual": 18000}` | When a run is ended |
| `ledger_cas_attempts` | 4 | Tries **in total** at a ledger write, and at a claim (part 2) |
| `max_start_delay_s` | 3600 | The oldest scheduled time a run may still claim (part 2) |

The values are the author's rulings of 18 Nov 2026, and they change only by pull request. The
loader is strict: a missing or unknown field, a wrong type, a duplicate key or a value out of range
is an error, never a default. The file's sha256 goes into the run manifest (part 2).

### The client wrapper

The entrypoint opens a **batch** on a `Guard` for each runner call (the pre-flight, then the 18),
and passes the runner `client_factory(guard)` through the runner's existing `client_factory`
argument. Every client the factory makes shares the one guard.

**The client beneath.** The wrapper builds the Bedrock client itself, with
`retries={"mode": "standard", "total_max_attempts": 1}`, so botocore sends each `converse` call
once. Its timeouts:

| Target | Read timeout | Connect timeout | From |
|---|---|---|---|
| Pipeline, steps 2 and 6 | 90 s | 10 s | `evals/run_pipeline_eval.py:64`, asked for at `:333`; connect as `live_client`, `:174` |
| Pipeline, steps 5a and 7 | 60 s | 10 s | `evals/run_pipeline_eval.py:65` (`reconcile.py:60`), asked for at `:335` |
| v1, both calls | 60 s | 60 s | Fixed in `evals/ws7_reserve.py`. These are botocore's defaults, which the v1 runner's default client (`evals/run_cold_eval.py:684`) runs on today |

**The endpoint.** Only an explicit `endpoint_url` argument can change where the client sends its
requests:

- The client is built with botocore's `ignore_configured_endpoint_urls=True`. The option "disables
  use of endpoint URLs provided via environment variables and the shared configuration file"
  (botocore 1.43.110, `botocore/config.py:208-213`).
- botocore applies it at `botocore/args.py:387-406`. An explicit `endpoint_url` argument still wins
  (`:388-389`), and the Config value outranks `AWS_IGNORE_CONFIGURED_ENDPOINT_URLS` (`:398-402`), so
  the environment cannot switch it back on.
- **The live path** passes no `endpoint_url`. At construction, before any request, the client must
  resolve to `https://bedrock-runtime.eu-west-2.amazonaws.com` in `eu-west-2`, or it is refused
  with `EndpointRefused` (`ws7_endpoint_refused`). A FIPS or dual-stack variant, or another region,
  is refused.
- **An explicit `endpoint_url`** is for the tests' stub only, and must be `http://127.0.0.1:<port>`.

**Before every call**, in this order. The first rule broken refuses the call, and a refused call
is never sent:

| Check | Refusal code |
|---|---|
| A batch is open | `ws7_no_batch` |
| The wall-clock deadline has not passed | `ws7_deadline` |
| The run is not unmetered (see "The unmetered stop") | `ws7_unmetered` |
| The running total is below the reservation | `ws7_spend_guard` |
| The request is one of the plan's steps: pipeline by the tool it forces, v1 by its `maxTokens`; a v1 request carries no tool | `ws7_unknown_request` |
| A pipeline request's `maxTokens` is the figure reserved for its step | `ws7_max_tokens_differs` |
| The model ID is the plan's | `ws7_model_differs` |
| A later step comes after a first step | `ws7_out_of_order` |
| A new generation does not exceed the batch's planned count | `ws7_plan_exceeded` |
| No call has been refused since the current generation started | `ws7_generation_blocked` |
| The step has not already been called in this generation | `ws7_repeat_refused` |

**How a generation is recognised** (ruling e1, 18 Nov 2026). The wrapper sees requests, not
generations.

- A generation starts at a first-step request (step 2, or v1's `combined` call) whose body differs
  from the current generation's first-step request.
- An identical first-step request is a repeat, and is refused. The code under test retries a
  throttle with the identical request (`src/generate/pipeline/converse.py:33`,
  `evals/run_cold_eval.py:329`).
- Each batch allows only the number of generations it planned.
- Before a run, the entrypoint checks that no two scenarios would send the same first-step request
  (part 5, "Notes for later parts" below).
- Every doubt is a refusal: a refusal costs nothing, and a refused run is a failed run.

**What a refusal does.** The wrapper raises `Ws7Refused`, a `StepError` with the code above.

- The pipeline passes a `StepError` through unchanged (`src/generate/pipeline/converse.py:34`), so
  the step's recorded error code is the refusal code.
- The v1 runner records any exception as `{"code": <class name>, "message": str(exc)}`
  (`evals/run_cold_eval.py:395`), so a v1 record shows `Ws7Refused` and the refusal code.
- **A refusal blocks the rest of its generation.** Once any call is refused, every further call is
  refused (`ws7_generation_blocked`) until a first-step request distinct from the current
  generation's starts the next generation. So after a throttled 5a whose retry was refused, the
  step 6 the runner still asks for is never sent, and step 7 needs step 6. A refused new
  generation (`ws7_plan_exceeded`) blocks the open one too.
- The next scenario starts a new generation and runs all its steps.

**After every call**, the wrapper adds the call's cost to the running total and to its batch's:

- a reply with usable usage: the cost of that usage;
- a reply with no usable usage: the call's worst case (outcome `response_usage_unknown`);
- an exception (a throttle, a 500, a read timeout, anything else): the call's worst case. The
  wrapper re-raises the same exception, so the code under test sees the real error.

**The unmetered stop.** A worst case is an estimate, not a bound: it counts the input at the step's
allowance, and nothing caps a real call's input. So a call whose cost is unknown, and whose model
may have run, makes the run **unmetered**. Every further call in the run is refused
(`ws7_unmetered`), in every generation and batch.

| The call | Unmetered? |
|---|---|
| A reply with usable usage | No: its cost is known |
| A reply with no usable usage (`response_usage_unknown`) | **Yes** |
| A 5xx (`server_error`), a read timeout, any other 4xx (`client_error`, which includes Bedrock's `ModelTimeoutException` and `ModelErrorException`, raised after the model ran), anything else (`other_error`) | **Yes** |
| A throttle (`throttled`): **exactly** an HTTP 429 whose error code is **exactly** `ThrottlingException` | No. AWS: "A 429 response means that the request was not admitted. Inspect the API-specific error type rather than relying on the HTTP status alone." ([Scaling and throughput best practices](https://docs.aws.amazon.com/bedrock/latest/userguide/scaling-throughput-best-practices.html), "Understanding HTTP error responses", read 18 Nov 2026). The page says nothing about billing |
| A 5xx carrying `ThrottlingException`, or a 429 with any other code (the same page names `ModelNotReadyException`) | **Yes**: `server_error` or `client_error` |
| No connection made (`connect_error`), or refused by botocore before sending (`request_invalid`) | No: nothing reached Bedrock |

Every failure, unmetered or not, stays counted at its worst case. Under ruling e2 any failure fails
the run anyway; the unmetered stop is about spending nothing more.

**How far a run can pass its reservation.** Both runners send one request at a time, and every call
before the last is either metered (its true cost is its usage) or did not run the model (its worst
case is above its true cost). So:

- the run's true spend is below its reservation plus **one call**;
- that call's output is capped by its `maxTokens`, but its input is not, and may exceed its
  allowance;
- by the planning figures the dearest such call is about £0.13 (step 2); that is an estimate, not
  an enforced limit;
- if the run is unmetered, its true cost is not known. Its ledger entry holds the guard's counted
  total, which counts the unmetered call at its worst case, and is **unresolved**: it blocks every later scheduled (inside-cap) run until the author
  resolves it on one of two bases, `upper_bound` or `estimate_accepted` (below; ADR-011 D7,
  point 4). An outside-cap manual run is not blocked by it, and still needs the author's yes;
- **AWS Budgets is an alert only, never the reconciliation.** It cannot identify one run's charge:
  the canary also calls Bedrock on this account, and Budgets is a delayed aggregate. Its filter
  must not be Service = Amazon Bedrock alone, because that would miss the model's cost (below;
  ADR-011 D7, point 5).

**How far a month can pass £15.** A run starts only if the month's *recorded* total plus its
reservation is £15 or less, so the recorded total passes £15 by at most the last run's one call.
The *true* spend is no more than the recorded total **only while every settled entry in the month is
either its actual metered cost or a defensible upper bound.** So the monthly bound (the month's spend
passes £15 by at most one call) holds only under that condition. "Actual metered cost" is priced at
the repository's rates, which are not confirmed against AWS's price page (ADR-011, Costs).

An unmetered entry is resolved on one of two bases:

| Basis | The settled figure | The month |
|---|---|---|
| `upper_bound` | A figure from AWS Cost Explorer, defined exactly below. It includes every service's usage on the account that day, the canary's calls among them, so it is above the run's true cost | **Keeps** its monthly assurance |
| `estimate_accepted` | The worst-case estimate, accepted. It may be below the true cost, because the input was not capped | **Assurance waived**: the ledger marks the month, and every run summary and check report for that month shows it |

**The `upper_bound` figure, exactly** (ADR-011 D7, point 4; Codex review of PR #5; the Service
and Region filters removed by the author, 18 Nov 2026):

- AWS Cost Explorer, **unblended** cost;
- **Charge type** = Usage **only**, so credits, refunds and other negative adjustments are
  excluded;
- for the **whole account**, with **no Service filter and no Region filter**;
- for the run's **UTC date or dates** (a run that spans two UTC dates takes both), read once that
  day's figure is complete. Cost Explorer's days are UTC: "All times are in Universal Coordinated
  Time (UTC)" (AWS, [*Modifying your chart*](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-modify.html),
  read 18 Nov 2026).

Why no Service or Region filter:

- Cost Explorer shows Anthropic models on Bedrock under a service name of their own for each
  model, not under "Amazon Bedrock". So a Service = Amazon Bedrock filter returns none of the
  model's cost.
- AWS does not document these names, so the source is secondary: Classmethod DevelopersIO,
  [*Tried aggregating by model using Amazon Bedrock product attributes in Cost Explorer*](https://dev.classmethod.jp/en/articles/bedrock-product-attributes-cost-explorer/)
  (10 Oct 2026), read 18 Nov 2026. It says: "Anthropic and OpenAI models are displayed in Cost
  Explorer under different service names for each model". Its example is "Claude Sonnet 5.5
  (Amazon Bedrock Edition)".
- The service name for our model is inferred from that pattern; it has not been seen. Part 5's
  first-use check is where it is seen.
- With no Service or Region filter, the figure cannot miss the model's rows, whatever service
  name or region they carry.
- The figure is looser: it includes every other service's usage on the account that day. That
  only makes it larger, so it errs only towards blocking.

Why Usage only: a net figure, reduced by credits or refunds, can fall below the run's usage, and
would then not be an upper bound. Cost Explorer lists **Credit** ("Any AWS credits that are applied
to your account") and **Refund** ("Any refunds that you received") as charge types apart from
**Usage** (AWS, [*Filtering the data that you want to view*](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-filtering.html),
read 18 Nov 2026).

The definition assumes no reservation or Savings Plan applies to the model's usage on this account. The same
page defines Usage as "Usage that AWS didn't apply reservation discounts to", and puts Savings Plan
covered usage under a charge type of its own, so Usage alone would then leave part of a run out. If
either is ever bought, the definition is revisited.

**An abandoned reservation, or a run that crashed before its record was written, is unresolved too**
(the author's ruling, 18 Nov 2026). Each is counted at its reservation, which is only an estimate,
just as an unmetered run's counted total is. So each blocks every later inside-cap (scheduled) run until the
author resolves it on one of the two bases above (ADR-011 D7, point 4; part 2).

A runner that sent requests in parallel could pass the reservation by one call per call in flight.
The later orchestrator-driven runner (ADR-011 D2) must revisit this before it is used with this
wrapper.

### The guard's report

`Guard.report()` holds IDs, fixed labels and numbers only. It never holds a request or a
response. A request's body is hashed, in memory, only to tell a repeat from a new generation.

| Field | Meaning |
|---|---|
| `reservation_usd_nanos`, `total_usd_nanos`, `total_usd_micros` | The reservation and the running total |
| `worst_case_part_usd_nanos` | The part of the total counted at worst case |
| `reservation_reached` | The total is at or past the reservation |
| `tripped` | A call was refused because the reservation had been reached |
| `unmetered`, `unmetered_calls` | The run is unmetered, and how many calls made it so (one: the next is refused) |
| `calls_sent`, `responses`, `no_response`, `usage_unknown` | Counts of calls sent and of what came back |
| `refused` | Refusals by code |
| `batches` | Per batch: generations planned and started, and its total |
| `calls` | One row per call: `seq`, `batch`, `generation`, `step`, `outcome`, `code`, the four token counts, `cost_usd_nanos`, `worst_case`, `latency_ms` |

A call's `outcome` is one of: `response`, `response_usage_unknown`, `throttled`, `server_error`,
`client_error`, `read_timeout`, `connect_error`, `request_invalid`, `other_error`, or `refused`.

Under ruling e2 (18 Nov 2026), any call with an outcome other than `response` or
`response_usage_unknown`, and any refusal, fails the run. Part 5 applies that rule; the wrapper
only counts.

### What part 1 does not do

- It does not open the ledger or decide whether a run may start (part 2).
- It does not settle the ledger. Part 2's `settle()` does, from the report's total; part 5
  calls it.
- It does not check that the scenarios' first-step requests are distinct, set the deadline or
  choose the sleep the runners are given (part 5).

## Part 2: the claim, the ledger, the match check and the run manifest

Three modules, and the strict fakes they are tested against:

- `evals/ws7_store.py`: the S3 and SSM clients, S3's conditional writes, the key layout, and
  `Stop`, the one way a WS7 refusal is raised.
- `evals/ws7_ledger.py`: the claim and the monthly ledger.
- `evals/ws7_manifest.py`: the match check and the run manifest.
- `tests/ws7_fakes.py`: a strict S3 fake and a strict SSM fake, on loopback.

Nothing in part 2 calls a model, and nothing has been run against AWS. No runner, no scorer, no
comparator file and no file in `src/generate/pipeline/` is changed, and neither is
`evals/ws7_config.json`. `ws7_manifest.py` calls `pipeline.orchestrate.pipeline_version()` and
each runner's own `parse_args`; it changes neither.

The approved slice 2 plan's part 2 section was not in the repository or on Notion, so the merged
documents were the reference. The names below were approved by the author on 19 Nov 2026.

### The order a run follows

Part 2 supplies the pieces. Part 5 wires them, in this order:

1. **The match check.** It costs nothing and writes nothing. A mismatch stops the run before any
   claim.
2. **The claim.** A duplicate stops here.
3. **Admission.** The reservation is booked in the month's ledger, or the run is refused.
4. **The run**, with the guard's deadline set from the admission (see "The run's own deadline").
5. **Settling**, straight after the last runner returns and before any scoring. If the run has
   no guard report to settle from, it calls `mark_crashed()` instead.
6. **The manifest**, written once at the end, when the batch's hashes are known.

### The S3 layout

Every key is under `ws7/`, the task role's whole reach in the bucket (ADR-011 D6).

| Key | Kept? | Written by |
|---|---|---|
| `ws7/claims/<target>/<YYYY-MM-DD>` | Kept | A scheduled run's claim. The date is the UTC date of the scheduled time |
| `ws7/claims/<target>/manual/<run_id>` | Kept | A manual run's claim |
| `ws7/ledger/<YYYY-MM>.json` | Kept | The monthly ledger: the UTC month of a run's scheduled time |
| `ws7/runs/scheduled/<run_id>/…` | **Expires after 90 days** | A scheduled run's records. `manifest.json` here (part 2); the batch, scores and comparison (parts 4 and 5) |
| `ws7/runs/manual/<run_id>/…` | Kept | A manual run's records: baseline batches and confirming runs |

- ADR-011 D8 expires scheduled runs after 90 days and keeps baselines, claims, ledgers and success
  records. An S3 lifecycle rule selects by prefix, so the rule is `ws7/runs/scheduled/` and
  **nothing that is kept sits under it** (`tests/test_ws7_store.py` checks the keys part 2 makes).
- Part 5's success records and D10's retained incident copies are kept, so they go under prefixes
  of their own, outside `ws7/runs/scheduled/`. Part 5 names them.
- **Run IDs.** A scheduled run is `<UTC date>T<HHMM>-<target>`, for example
  `2026-12-16T0400-pipeline`. A manual run adds `-manual`: `2026-12-16T0130-pipeline-manual`. No
  target is called `manual`, so the two cannot collide.

### The store (`evals/ws7_store.py`)

- **The clients.** `build_client("s3")` and `build_client("ssm")` follow the Bedrock client's
  rules (part 1): one attempt per call, `eu-west-2` only, `ignore_configured_endpoint_urls=True`,
  and a refusal at construction unless the client resolves to the service's own endpoint
  (`https://s3.eu-west-2.amazonaws.com`, `https://ssm.eu-west-2.amazonaws.com`). An explicit
  endpoint is for the tests' fakes and must be `http://127.0.0.1:<port>`. The S3 addressing style
  and the accelerate option are set in code, so an `s3 =` section in a config file cannot change
  them.
- **There is no unconditional write and no delete.** The store has `list_keys`, `get`,
  `put_if_absent` (`If-None-Match: *`) and `put_if_match` (`If-Match: <ETag>`), and `create`,
  which is `put_if_absent` with the read-back below.
- **How S3 answers** (AWS, read 19 Nov 2026):
  [*Conditional write behavior*](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
  and [PutObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html).

| S3's answer | Meaning here |
|---|---|
| 200 | Written. The reply's ETag is the next `If-Match` |
| 412 `PreconditionFailed` | `If-None-Match`: the key is taken. `If-Match`: the object is not the one that was read |
| 409 `ConditionalRequestConflict` | A concurrent conditional write. AWS: "On a 409 failure you should retry the upload" (`If-Match`: "fetch the object's ETag and retry") |
| 404 on `If-Match` | The object is gone. A failure: nothing in WS7 deletes |
| Any other 4xx | Refused outright. It did not land. A failure |
| A 5xx, a timeout, a dropped connection, a 200 with no ETag | **Ambiguous.** The write may have landed |

- **An ambiguous write is never assumed either way.** The caller reads the object back and looks
  for its own token: a claim's `claim_id`, a ledger's `write_id`, a manifest's bytes.
- **Absence is decided from a listing, never from a GET error.**
  - S3 answers a GET for a missing key with 404 only if the caller has `s3:ListBucket`, and with
    403 otherwise ([GetObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html),
    read 19 Nov 2026). AWS does not say which a grant limited by `s3:prefix` gives.
  - So WS7 lists a prefix first, GETs only keys the listing returned, and creates a month's first
    ledger with `If-None-Match: *`. Any GET error on a listed key is a failure
    (`ws7_store_get_failed`).
  - A truncated listing is refused (`ws7_store_list_truncated`): a key it left out would be read
    as absent.
- **An ETag is opaque.** The bucket is KMS-encrypted (ADR-011 D8, D9), so an ETag is not the
  object's MD5. WS7 only hands back the ETag it was given.

### The claim

`claim()` creates one object with `If-None-Match: *`. Its key comes from the scheduled time the
job is given as an argument, never from the clock.

```json
{"schema": "ws7-claim/1", "target": "pipeline", "mode": "scheduled",
 "scheduled_time": "2026-12-16T04:00:00Z", "run_id": "2026-12-16T0400-pipeline",
 "claim_id": "9c1e52aa07b34d1088f2a6c1d0e4b7a3", "claimed_at": "2026-12-16T04:00:37Z"}
```

- **The scheduled time** is ISO 8601 to the second with `Z` or an explicit offset. A time with no
  zone is refused, not guessed.
- **Before any write:** a scheduled time in the future is refused, and so is one older than
  `max_start_delay_s`, for a scheduled and a manual run alike. Exactly at the limit is allowed.
- **A duplicate.** If the key is taken the job raises `Duplicate` (`ws7_duplicate`). It reads no
  ledger, reserves nothing and spends nothing, and it carries the metric name `Duplicate` with a
  count of 1 (no alarm, ADR-011 D7).
- **A scheduled claim is one per target per UTC date.** A second delivery that day is a duplicate
  whatever time it carries.
- **A manual run claims under its own key** (the author's ruling, 19 Nov 2026).
  - Why: with one shared key, a manual run started on a Wednesday before 04:00 would take that
    date's claim. The scheduled delivery would exit as a duplicate, with no alarm, and the
    Thursday check would find a claim that was not the scheduled run's.
  - "Manual runs use the same claim" (ADR-011 D7) means the same mechanism, not the same key.
  - What stops a manual and a scheduled run overlapping is the ledger's open entry.
- **A claim whose reply failed** is read back, listing first. It is this job's only if the object
  carries this job's `claim_id`; if another job's is there, this job is a duplicate.
- The claim is tried `ledger_cas_attempts` times in total (a 409, or a failed reply that did not
  land, is tried again). After that the job stops with `ws7_claim_unavailable`.

### The ledger

One object a month. The month is the **UTC month of the run's scheduled time**, the same time that
keys the claim. For every Wednesday 04:00 London run that is also the London month.

```json
{"schema": "ws7-ledger/1", "month": "2026-12", "rev": 5,
 "write_id": "5f0c1b77e2a94c03b1d2e3f4a5b6c7d8",
 "cap_usd_micros": 18750000, "assurance_waived": false,
 "entries": [
  {"run_id": "2026-12-02T0400-pipeline", "target": "pipeline", "mode": "scheduled",
   "state": "settled", "kind": null, "basis": "metered",
   "reserved_usd_micros": 8280476, "counted_usd_micros": 1903211, "estimate_usd_micros": null,
   "opened_at": "2026-12-02T04:00:30Z", "expires_at": "2026-12-02T06:00:30Z",
   "closed_at": "2026-12-02T04:38:10Z", "resolved_at": null},
  {"run_id": "2026-12-09T0400-pipeline", "target": "pipeline", "mode": "scheduled",
   "state": "unresolved", "kind": "abandoned", "basis": null,
   "reserved_usd_micros": 8280476, "counted_usd_micros": 8280476, "estimate_usd_micros": 8280476,
   "opened_at": "2026-12-09T04:00:30Z", "expires_at": "2026-12-09T06:00:30Z",
   "closed_at": "2026-12-09T06:00:30Z", "resolved_at": null}]}
```

Both examples are invented. The loader is strict, as the config loader is: an unknown or missing
field, a wrong type, a duplicate key, or a state that cannot be (an accepted estimate in a month
that is not flagged, say) is `ws7_ledger_invalid`, and the run does not start. So is any other
object under `ws7/ledger/`.

**What an entry is, and what it counts at.**

| State | `kind` / `basis` | Counts at |
|---|---|---|
| `open` | | Its reservation |
| `settled` | `metered` | The guard's counted total, in micro-dollars, rounded up |
| `settled` | `withdrawn` | 0: the run withdrew before any call (see "One run at a time") |
| `unresolved` | `unmetered` | The guard's counted total (the author's ruling, 19 Nov 2026) |
| `unresolved` | `abandoned`, `crashed` | Its reservation |
| `resolved` | `upper_bound`, `estimate_accepted` | The settled figure. `estimate_usd_micros` keeps what it was resolved from |

- The month's **recorded total** is what its scheduled entries count at. A manual run is outside
  the cap: its entry is in the ledger and is never counted.
- `settled` and `resolved` never change again. Every write is checked against the ledger it
  replaces before it is sent (`_check_transition`): a write that would change one, drop or
  reorder an entry, move an entry any way but open → settled, open → unresolved or
  unresolved → resolved, or clear the month's flag is refused with `ws7_ledger_write_invalid`.

**Admission** (`Ledger.admit`), in this order, decided again on every try:

1. An open entry in **any** month that has not reached its `expires_at` refuses the run, in
   either mode: `ws7_run_in_progress`.
2. An open entry at or past `expires_at` is abandoned (below).
3. A scheduled run is refused while any entry in any month is unresolved:
   `ws7_blocked_unresolved`, with the blocking run IDs. A manual run is not blocked.
4. A scheduled run is admitted only if the month's recorded total plus its reservation is **at
   most the cap**: `ws7_over_cap` otherwise. Equal is admitted; one micro-dollar over is not. A
   manual run is neither checked nor counted.
5. The open entry is written.

A refused run reserves nothing: no entry is written for it.

**Every write is a compare-and-swap.**

- The job lists `ws7/ledger/`, reads every ledger listed, and decides. A month's first ledger is
  created with `If-None-Match: *`; every later write carries `If-Match` with the ETag that was
  read.
- On a 412 or a 409 the job reads everything again and decides again.
- **`ledger_cas_attempts` counts tries in total, not retries.** With the configured 4: three
  losses and then a win starts the run; four losses give `ws7_ledger_conflict`, and the run does
  not start.
- **A write whose reply failed** counts as a try. Its `write_id` is remembered. If the next read
  shows it, the write landed and is this job's. If another writer has written since, the job
  recognises its own entry by its run ID and does not book it twice.
- A ledger that cannot be listed, read or written stops the run: `ws7_ledger_unavailable`, with
  the store's own code.

**One run at a time, across months.** One compare-and-swap covers one object, and two runs at a
month's end could book in two months' objects at once. So after its entry is written, a run reads
every month once more. If it finds another live open entry it withdraws its own at zero
(`settled`, `withdrawn`) and stops with `ws7_run_in_progress`. Each run writes and then reads, so
the later read always sees the earlier write: at least one of the two withdraws, and both may.
If that second read itself fails, the run does not start and its entry is left open, to be
abandoned and resolved.

**Settling** (`Ledger.settle`).

- It takes the guard's report (part 1) and replaces the reservation with `total_usd_micros`: the
  usage Bedrock returned wherever a reply was seen, and the worst case only where none was.
- A report that says `unmetered` is written as `unresolved`, `unmetered`, at that same total, in
  the same write.
- The report must be the run's own: its target and its reservation must be the entry's, and its
  micro-dollar total must be its nano-dollar total rounded up. Otherwise `ws7_report_invalid`,
  and the entry stays open.
- Only an open entry can be settled: `ws7_entry_not_open` otherwise.

**How each unresolved case is recognised.**

| Case | Recognised by | When | Counts at |
|---|---|---|---|
| Unmetered | The run itself: `settle()` is given a report that says `unmetered` | At settling | The guard's counted total |
| Crashed | The run itself: part 5's outermost handler calls `mark_crashed()` when it has no guard report to settle from | At once | Its reservation |
| Abandoned | Whoever looks next: any call that changes the ledger, in any mode, and `expire()` | At or after `expires_at` | Its reservation |

- `expires_at` is the time the entry was opened plus the wall-clock limit for its mode. It is
  stored in the entry, so a later change to the config cannot move it.
- **The overlap.** A run that dies outright cannot settle and cannot report, so nothing marks it
  crashed. Its entry stays open and is found at `expires_at`: it **is** the abandoned case.
- **An entry takes exactly one kind.** Only an open entry can become unresolved, so the first
  label written stands: a run that reported `crashed` is not relabelled at its expiry.
- The kind is a label. All three block the same way and are resolved the same way.
- **At or past `expires_at` the entry is abandoned whoever asks, the run itself included.** A
  late `settle()` or `mark_crashed()` is refused with `ws7_entry_not_open` and the entry stays
  unresolved. Only `resolve()` moves it on.

**The block.** An unresolved entry in any month blocks every later scheduled run, with
`ws7_blocked_unresolved`. The job finds it by listing every ledger, so an entry two months back,
with no ledger for the month between, still blocks. An unresolved **manual** entry blocks
scheduled runs too: the rule is "an unresolved entry", read as written. A manual run is not
blocked.

**Resolving** (`Ledger.resolve(run_id, basis, settled_usd_micros)`; part 5 adds the command).

- The basis is exactly `upper_bound` or `estimate_accepted`: `ws7_basis_unknown` otherwise.
- The figure is a whole number of micro-dollars, zero or more.
- `upper_bound`: the Cost Explorer figure defined in part 1. It cannot be checked here and is
  recorded as the author's statement.
- `estimate_accepted`: the figure must equal the entry's own estimate (`ws7_estimate_differs`
  otherwise), so no other number can be accepted under that label.
- The entry then counts at the settled figure, in its own month.
- **`estimate_accepted` sets the month's `assurance_waived` flag, in the same write, in the
  entry's own month.** `upper_bound` does not. The flag is carried by every later write and is
  never cleared; the write check refuses a ledger that would clear it.
- An entry past its expiry that nobody has marked yet can be resolved in one step.

### The run's own deadline

`admit()` returns an `Admission`. Its `deadline_at` is `expires_at` less `DEADLINE_MARGIN_S`
(120 s).

- **The rule part 5 must follow:** the guard's deadline (part 1) is set from `deadline_at`, so the
  last call is sent at least 120 s before another task may mark the entry abandoned.
- 120 s is v1's 60 s to connect plus 60 s to read, the longest of either target (the pipeline's is
  10 s plus 90 s). A test reads both from the plans and the runner.
- A read timeout bounds each wait for data, not a whole reply. So this is a margin, not a proof
  that no call is in flight at `expires_at`.
- A wall-clock limit of 120 s or less is refused (`ws7_wall_clock_limit_too_short`).
- `expires_at` is compared with other tasks' clocks, so the ledger uses wall-clock UTC. Part 5
  turns `deadline_at` into the guard's monotonic deadline with `Admission.seconds_to_deadline()`.

### The monthly bound, restated

Part 1's argument reasons from the month's *recorded* total. But entries change after later runs
are admitted: a reservation is settled, an entry is resolved. So the proof is restated from what
was booked (Codex P3, PR #5). It is also in `evals/ws7_ledger.py`, beside the code.

Take one month, with cap C. Its scheduled runs are admitted one after another, because every
admission is a compare-and-swap on the month's one object. Call the last one admitted *r*, with
reservation R.

1. **What was booked at r's admission.** Admission rule 4 held inside the write that booked *r*:
   B + R ≤ C, where B is the sum of what every earlier scheduled entry counted at, at that moment.
   - By rules 1 to 3, none of those entries was open or unresolved then. Each was settled or
     resolved.
   - A settled or resolved entry never changes (`_check_transition`).
   - So B is fixed at *r*'s admission. Nothing later alters it: not *r* being settled, and not a
     resolution in another month.
2. **Each term of B is at least that run's true cost, provided the entry is `metered` or
   `upper_bound`.**
   - A metered total prices every reply at the usage Bedrock returned, and counts every call that
     did not run the model at its worst case (part 1).
   - An `upper_bound` figure is the author's statement that it is one.
   - A `withdrawn` entry made no call.
   - An `estimate_accepted` figure may be below the true cost.
3. **The last run's reservation and its final call.** *r*'s true cost is below R plus one call:
   the guard sends a call only while its running total is below R (part 1).

So the month's true scheduled spend is at most B + R plus *r*'s final call, which is at most
**C plus that one call**. The proof uses three things: what was booked at each earlier run's
admission, the last run's reservation, and its final call.

- **The one way step 2 fails** is an `estimate_accepted` resolution, and that is exactly what sets
  the month's `assurance_waived` flag.
- "True cost" is priced at the repository's rates, which are not confirmed against AWS's price
  page (ADR-011, Costs).
- The month is the UTC month of each run's scheduled time, not AWS's invoice month for each call.

The tests that pin each step (`tests/test_ws7_ledger.py`):

| Step | Test |
|---|---|
| Rule 4 at the boundary | `test_a_run_is_admitted_when_the_total_plus_its_reservation_equals_the_cap`, and `…is_one_micro_dollar_over` |
| No admission past an open or unresolved entry | `test_an_open_reservation_refuses_any_other_run`, `test_each_unresolved_case_blocks_the_next_scheduled_run_which_reserves_nothing` |
| B is fixed | `test_what_was_booked_before_a_run_is_fixed_once_that_run_is_admitted`, `test_a_write_the_rules_do_not_allow_is_refused_before_it_is_sent` |
| The bound, with invented costs | `test_with_an_upper_bound_the_months_true_spend_passes_the_cap_by_at_most_one_call` |
| The one way it fails, and the flag | `test_with_an_accepted_estimate_the_bound_can_fail_and_the_month_says_so` |
| The flag is never cleared | `test_the_flag_stays_set_through_every_later_write_to_that_months_ledger` |

### The match check

It runs first and costs nothing (ADR-011 D11). The job works out its own values, reads the
expected ones from SSM Parameter Store, and stops with `ws7_match_differs` if any differs. The
refusal names the fields that differ, never their values.

| Target | Parameter | Fields |
|---|---|---|
| Pipeline | `/discharge-eval/ws7/expected/pipeline` | `pipeline_version`, `model_id`, `prompt_caching` |
| v1 | `/discharge-eval/ws7/expected/v1` | `model_id`, `v1_prompt_sha256` (`system_prompt` and `patient_system_prompt`), `patient_second_pass`, `prompt_caching` |

- Each parameter is one **String** holding strict JSON, with `"schema": "ws7-expected/1"` and
  `"target"`. A missing parameter or a failed read is `ws7_expected_unreadable`. Any other shape,
  or a `SecureString`, is `ws7_expected_invalid`.
- **v1 needs the second pass on and caching on.** An expected record with either off is refused
  (`ws7_expected_invalid`), not followed.
- An invented pipeline value:
  `{"schema": "ws7-expected/1", "target": "pipeline", "pipeline_version": "3f9c…e1", "model_id": "anthropic.claude-sonnet-4-6", "prompt_caching": true}`.

Where the job's own values come from, so that they are what the runner will really do:

| Value | From |
|---|---|
| Model ID | The plan (`evals/ws7_reserve.py`), which is the runner's own constant |
| `pipeline_version` | `pipeline.orchestrate.pipeline_version()`, called, not changed |
| The two v1 prompt hashes | The sha256 of the bytes of the files the v1 runner will load, as its record's `file_sha256` is |
| `prompt_caching`, `patient_second_pass` | The runner's own `parse_args`, on the argv the entrypoint will hand it |

The limit is ADR-011's: the check proves the image agrees with the release record, not that the
running Lambda does.

### The run manifest (`ws7-manifest/1`)

Written once, with `If-None-Match: *`, at `<the run's prefix>/manifest.json`. A manifest already
there is never replaced (`ws7_manifest_exists`).

| Field | Meaning |
|---|---|
| `run_id`, `target`, `mode`, `scheduled_time`, `month` | The claim's |
| `git_commit`, `image_digest` | See below |
| `config_sha256` | Of `evals/ws7_config.json` |
| `client` | `total_max_attempts` (1), `connect_timeout_s`, and `read_timeout_s`: 60 for v1, fixed by WS7; `null` for the pipeline, whose runner names one for each client |
| `keys` | The comparator's `keys` object (`docs/WS7-COMPARATOR.md`), checked by the comparator's own code. For v1 it holds the two prompt hashes, `patient_second_pass` and `prompt_caching` |
| `reservation` | `generations`, `margin_percent`, `total_usd_micros` |
| `month_assurance_waived` | The month's flag when the run ran |
| `baseline_id` | The baseline it was compared with, or `null` |
| `part_c` | How PART C was produced: `pipeline_step_7` or `v1_second_pass` |

- The corpus hash, the gold hashes and the scorer versions come from the finished batch and its
  scores, so parts 4 and 5 pass them in. Part 2 checks their shape.
- A manifest holds IDs, hashes, numbers and flags only.

**The commit and the image digest.** The image has no `.git`, and a container cannot hash its own
image. Both are handed in as environment variables. `identity()` reads them only from the mapping
it is given, and asks no metadata endpoint.

| Variable | Value | Set by |
|---|---|---|
| `WS7_GIT_COMMIT` | The 40-character commit the image was built from | A build argument in the `Dockerfile` (part 6) |
| `WS7_IMAGE_DIGEST` | `sha256:` and 64 hex characters | The ECS task definition, beside the image reference, which pins the image by the same digest (slice 3) |

- A run with either missing or malformed is refused: `ws7_identity_missing`.
- The digest is the deployer's statement. Slice 3's set-up check compares it with the task
  definition's image reference.

### Refusals

Every refusal is a `Stop` with a fixed code. A `Stop` carries the code, IDs (run IDs, months,
fixed field names) and whole-number counts under fixed names. Its constructor refuses anything
else, so no note text, no model output and no AWS message can travel in one. `Stop.line()` is the
one-line form for a log or an alert, for example:

```
ws7 stop code=ws7_blocked_unresolved ids=2026-12-09T0400-pipeline unresolved=1
```

Every `Stop` names a metric: `Duplicate` for a duplicate, `RunRefused` for the rest. Part 5
publishes them.

| Where | Codes |
|---|---|
| The claim | `ws7_duplicate`, `ws7_target_unknown`, `ws7_mode_unknown`, `ws7_scheduled_time_invalid`, `ws7_scheduled_time_future`, `ws7_start_too_late`, `ws7_claim_unavailable` |
| Admission | `ws7_reservation_invalid`, `ws7_wall_clock_limit_too_short`, `ws7_run_in_progress`, `ws7_blocked_unresolved`, `ws7_over_cap`, `ws7_entry_exists` |
| Any ledger write | `ws7_ledger_conflict`, `ws7_ledger_unavailable`, `ws7_ledger_invalid`, `ws7_ledger_write_invalid` |
| Settling and resolving | `ws7_entry_unknown`, `ws7_entry_not_open`, `ws7_entry_not_unresolved`, `ws7_report_invalid`, `ws7_basis_unknown`, `ws7_settled_figure_invalid`, `ws7_estimate_differs` |
| The match check and the manifest | `ws7_runner_argv_invalid`, `ws7_expected_unreadable`, `ws7_expected_invalid`, `ws7_match_differs`, `ws7_identity_missing`, `ws7_manifest_invalid`, `ws7_manifest_exists`, `ws7_manifest_unavailable` |
| The store | `ws7_store_endpoint_refused`, `ws7_store_bucket_refused`, `ws7_store_key_refused`, `ws7_store_list_failed`, `ws7_store_list_truncated`, `ws7_store_get_failed`, `ws7_store_object_too_large`, `ws7_store_put_refused`, `ws7_store_object_missing`, `ws7_store_create_unsure` |

### What the fakes cannot prove

The S3 fake answers a conditional write as AWS documents it, handles one request at a time, and
faults on anything it does not implement: an unconditional PUT, a PUT with both conditions, a
delete, any other method or query parameter. It ignores request headers it does not know, because
botocore adds some and a newer botocore may add more. Its ETags are deliberately not MD5s. It can
answer a GET for a missing key with 404 or with 403, and a test shows WS7 behaves the same either
way. The SSM fake implements `GetParameter` only.

Slice 3's set-up checks must show, on the real bucket and with the task role:

- that real S3 gives the 412, the 409 and the 404 as documented, on a KMS-encrypted bucket, with
  the bucket's versioning as deployed;
- that the role can do all of it: `s3:PutObject` for `If-None-Match`, `s3:GetObject` and
  `s3:PutObject` for `If-Match`, `s3:ListBucket` limited to `ws7/`, and the KMS key;
- **whether a missing key gives 404 or 403** under that limited grant. The code does not depend
  on it; the check records it;
- that a real ETag, as GET returns it, is accepted by `If-Match`;
- two real tasks racing for one claim: exactly one holds it;
- that the role cannot delete, and cannot write outside `ws7/`;
- that the SSM parameters are plain Strings the task can read and cannot write;
- the format of EventBridge Scheduler's scheduled time, and that a duplicate delivery carries the
  same value;
- that `WS7_IMAGE_DIGEST` in the task definition is the digest of the image it runs.

## Notes for later parts

### Part 4: the run summary

- Every run summary written while its month is marked "assurance waived" shows it. The
  comparator's `ws7-run/1` rejects an unknown top-level field, so the flag goes in as a count
  metric, `month_assurance_waived` (0 or 1), in the Run family. An unruled metric is listed and
  changes no verdict (`docs/WS7-COMPARATOR.md`), so no new schema version is needed.

### Part 5: the modes

- **The distinct-scenarios precheck must use a canonical first-request fingerprint, not the raw
  notes hash** (Codex P3, PR #5). Two scenarios whose notes differ only in ways the pipeline
  normalises away, such as line endings, would have different notes hashes but could send the same
  first-step request. The wrapper would then refuse the second as a repeat. So the precheck builds
  the first-step request each scenario would send, after the runner's and the pipeline's own
  normalisation, and compares `ws7_client.fingerprint()` of each: the hash the wrapper itself uses.
  This is not fixed in part 1.
- A run whose report says `unmetered` is a failed run with the fixed code `ws7_unmetered`.
- **The resolve command.** A manual mode, run by the author only, that takes the run ID, the
  **basis** (`upper_bound` or `estimate_accepted`) and the **settled figure** in micro-dollars,
  and records them on an unresolved entry (unmetered, abandoned or crashed; part 2).
  - For `upper_bound` the settled figure is the Cost Explorer figure exactly as defined above. The
    command cannot check that figure, so the basis is recorded as the author's statement.
  - The command makes no Bedrock call.
  - The command is the only way an entry leaves the unresolved state; AWS Budgets never resolves
    one.
  - With `estimate_accepted`, the command also sets the month's "assurance waived" flag (part 2).
- **Before the resolve command is first used**, the author confirms in the Cost Explorer console,
  against real billing rows, that the model's rows carry Charge type = Usage. Pick a day the
  canary ran, then:
  1. group by **Service**, to find the model's own service name;
  2. filter to that service, and group by **Charge type**.

  The `upper_bound` definition rests on this check. Until now the definition had been checked
  only against AWS's documentation, never against real rows. (The console groups by one dimension
  at a time, hence the two steps.)
- **The check-mode report.**
  - The Thursday check reports a run blocked by an unresolved entry by that cause, naming the
    entry's run ID, so the alert says what the author must resolve.
  - It also reports a **waived month**: when the month's ledger is marked "assurance waived", the
    report says so, every week of that month.
- Build one live client before the runner starts, so that a refused endpoint fails the run before
  any batch folder is written.
- **Wiring part 2** ("The order a run follows", above):
  - the scheduled time comes from the schedule as an argument, and the bucket name from the task
    definition; neither comes from the clock or from a default;
  - the match check runs before the claim, and `identity(os.environ)` before any spend;
  - the guard's deadline is set from `Admission.deadline_at`, never later;
  - `settle()` is called straight after the last runner returns, before scoring, and
    `mark_crashed()` from the outermost handler when there is no guard report;
  - a `Duplicate` publishes the `Duplicate` count and exits; every other `Stop` after a claim
    writes the failed record the Thursday check reads, with the stop's code;
  - the check mode looks for the scheduled claim key only, calls `Ledger.expire()`, and reports
    `Ledger.unresolved()` and `Ledger.assurance_waived(month)`;
  - the resolve command calls `Ledger.resolve()`;
  - success records and retained incident copies get prefixes of their own, outside
    `ws7/runs/scheduled/`.

### Part 6: the image

- The `Dockerfile` takes the commit as a build argument and sets `WS7_GIT_COMMIT` from it
  (part 2, "The run manifest").

### Slice 3: the stack

- The task role gains `s3:ListBucket` on the eval bucket, limited to the `ws7/` prefix (ADR-011
  D6; the author's ruling, 19 Nov 2026).
- The lifecycle rule that expires scheduled runs selects `ws7/runs/scheduled/` and nothing else.
- The task definition sets the bucket name and `WS7_IMAGE_DIGEST`, and pins the image by that
  digest.
- The set-up checks in "What the fakes cannot prove" (part 2).

## Tests

```zsh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q tests/test_ws7_reserve.py tests/test_ws7_client.py tests/test_ws7_store.py tests/test_ws7_ledger.py tests/test_ws7_manifest.py
```

- `tests/test_ws7_reserve.py`: the cited lines, the plan, the prices, the reservation against
  ADR-011's figures, the cost of a call, and the config loader. Each refusal has a partner that
  passes.
- `tests/test_ws7_client.py`, at the server: the real runners and the real botocore client,
  against a local stub (`tests/ws7_stub.py`) that counts the requests it receives by step. These
  tests are ADR-011's acceptance check for slice 2.
  - For **both runners** and **every model step**: a throttle, a 500 and a read timeout each give
    exactly one request for that step at the server, and none for any later step of that
    generation. Clean runs give one request per step.
  - A throttle at step 5a, its retry refused: no request for steps 6 or 7, and the next scenario
    sends all four. The partner, with no throttle, sends every step.
  - A refused repeat ends one generation, and the next one runs.
  - A stub reporting inflated input usage: after the first reply, no request reaches the server.
    With honest usage, every request does.
  - A reply with no usage block: no request after it reaches the server (the run is unmetered).
  - Only a 429 with `ThrottlingException` is a throttle, for both runners. A 500 carrying
    `ThrottlingException`, or a 429 carrying `ModelNotReadyException`, makes the run unmetered,
    and nothing after it reaches the server. The partner, a genuine throttle, lets the next
    scenario run.
  - A step whose `maxTokens` differs from the reserved figure sends nothing.
  - With `AWS_ENDPOINT_URL` and `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` set, or an `endpoint_url` in
    the AWS config file, the explicit stub path still reaches the stub.
- `tests/test_ws7_client.py`, the endpoint, without connecting: with each override configured,
  an ordinary boto3 client is moved by it (the positive control), and the live client still
  resolves to Bedrock runtime in eu-west-2. The URL a Converse request would go to is read in a
  `before-send` handler, which stops the request before it leaves. FIPS, dual-stack and another
  region are refused at construction.
- `tests/test_ws7_client.py`, the rules: the guard against a strict in-process client, at each
  boundary (a total equal to the reservation is refused; one nano-dollar below is sent). Unknown
  usage stops the next call; known usage does not. Each kind of failure is checked against the
  unmetered table above. A refusal blocks the rest of its generation; a failure that was not a
  refusal does not.

- `tests/test_ws7_store.py`, at the server: each conditional write and S3's answer to it; an
  ambiguous write, landed and not; `create` and its read-back; absence from the listing with a
  missing key answering 404 and 403; the fake's own strictness (an unconditional PUT is a
  fault). Without connecting: the S3 and SSM clients' endpoints with each override configured,
  with an ordinary client as the positive control. The key layout, and `Stop`.
- `tests/test_ws7_ledger.py`: the claim (a duplicate, a manual run on the scheduled date, two
  deliveries at once, a write that landed but whose reply failed); the compare-and-swap at
  `ledger_cas_attempts` and one below; admission at the cap and one micro-dollar over; the
  expiry at `expires_at` and one second before, with an injected clock; every test in the Part 2
  list for the unresolved state (each case blocks, each resolves on both bases, the month
  before, two months back with a gap, a manual run not blocked, the basis stored and an unknown
  one refused, the flag set, not set and kept); and the monthly bound with invented costs.
- `tests/test_ws7_manifest.py`: the job's own values for both targets; each expected value
  differing, with a matching partner; v1's second pass and caching; a missing parameter; the
  commit and the digest from the mapping given; the manifest for both targets, checked by the
  comparator's own key check; written once.

**Keeping the tests offline.** Every test in the three part 2 files uses `offline_aws`, and every
client they build points at a fake on `127.0.0.1` or is never called. The `offline_aws` fixture:

- sets invented credentials and `AWS_EC2_METADATA_DISABLED=true`, and points the AWS config and
  credentials files at paths that do not exist;
- removes every proxy and endpoint variable;
- lets a socket connect only to an address a test registered (the stub's host and port), and
  resolves no name but the stub's.

Anything else fails the test. Positive controls show the fixture refusing a routable address,
another loopback port and an AWS hostname, and accepting the stub's own address.

**What the stub cannot prove.** Real Bedrock's error shapes, usage fields and timings may differ
from the stub's invented ones. The stub sends the error type that botocore reads
(`x-amzn-ErrorType`) and a JSON body; it does not use the event-stream or any other format.
Slice 3's set-up checks, and the first live run with the author's approval, are where real replies
are first seen.
