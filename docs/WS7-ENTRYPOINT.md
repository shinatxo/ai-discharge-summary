# WS7 entrypoint

The WS7 entrypoint is the job the weekly Fargate task will run ([ADR-011](adr/ADR-011-ws7-scheduled-eval.md),
slice 2). It is being built as seven pull requests: part 5 is two. This document grows with
each one.

| Part | What | Status |
|---|---|---|
| 1 | The client wrapper and the reservation | **Built** |
| 2 | The claim, the ledger, the match check and the run manifest | **Built** |
| 3 | The follow-ups from PR #4 (the comparator) and PR #6 (the claim and ledger) | **Built** |
| 4 | The run summary and the candidate baseline | **Built** |
| 5a | The scheduled run, the Thursday check, the comparator call and publishing | **Built** |
| 5b | The manual baseline, the manual confirm and the resolve command | Not built |
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
| `preflight_scenario` | `S1` | The one scenario run alone before the 18. A scenario ID: 1 to 32 letters, digits, `_` or `-`, **not starting with a dash**, so it can never be read as an option such as `--all` (Codex re-check of PR #6) |
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
  (part 5a, "The distinct-scenarios precheck").
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
| `run_id` | The run this guard meters, given when the guard is made (`Guard(..., run_id=...)`), or `null` if none was given. The ledger settles a run only from a report that carries that run's own ID (part 2) |
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
- It does not settle the ledger. Part 2's `settle()` does, from the report's total; part 5a
  calls it.
- It does not check that the scenarios' first-step requests are distinct, set the deadline or
  choose the sleep the runners are given. Part 5a does all three.

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
5. **Settling**, straight after the last runner returns and before any scoring. `settle()` takes
   the `Admission` that `admit()` returned. If the run has no guard report to settle from, it
   calls `mark_crashed()` with that `Admission` instead.
6. **The manifest**, written once at the end, when the batch's hashes are known.

### The S3 layout

Every key WS7 writes is under `ws7/`, the task role's whole reach in the bucket (ADR-011 D6).

| Key | Kept? | Written by |
|---|---|---|
| `ws7/claims/<target>/<YYYY-MM-DD>` | Kept | A scheduled run's claim. The date is the UTC date of the scheduled time |
| `ws7/claims/<target>/manual/<run_id>` | Kept | A manual run's claim |
| `ws7/ledger/<YYYY-MM>.json` | Kept | The monthly ledger: the UTC month of a run's scheduled time |
| `ws7/runs/scheduled/<run_id>/…` | **Expires after 90 days** | A scheduled run's records. `manifest.json` here (part 2); the batch, scores and comparison (parts 4 and 5) |
| `ws7/runs/manual/<run_id>/…` | Kept | A manual run's records: baseline batches and confirming runs |
| `ws7/success/<run_id>.json` | Kept | A scheduled run's success record (part 5a) |
| `ws7/failed/<run_id>.json` | Kept | The failed record of a scheduled run that held its claim (part 5a) |
| `retained/<run_id>/…` | Kept | **Outside `ws7/`.** An incident copy, made by the author by hand. Nothing in WS7 writes it |

- ADR-011 D8 expires scheduled runs after 90 days and keeps baselines, claims, ledgers and success
  records. An S3 lifecycle rule selects by prefix, so the rule is `ws7/runs/scheduled/` and
  **nothing that is kept sits under it** (`tests/test_ws7_store.py` checks the keys part 2 makes).
- Success records and failed records are kept, so they have prefixes of their own, outside
  `ws7/runs/scheduled/` (part 5a).
- **D10's retained incident copies go under `retained/<run_id>/`, outside `ws7/` altogether** (the
  author's ruling, 20 Nov 2026). The task role cannot reach it, the store refuses every key under
  it, and the lifecycle rule does not touch it. The author copies by hand (slice 3's runbook).
- **Run IDs.** A scheduled run is `<UTC date>T<HHMM>-<target>`, for example
  `2026-12-16T0400-pipeline`. A manual run adds `-manual`: `2026-12-16T0130-pipeline-manual`. No
  target is called `manual`, so the two cannot collide.

### The store (`evals/ws7_store.py`)

- **The clients.** `build_client("s3")` and `build_client("ssm")`, and from part 5a
  `build_client("cloudwatch")` (`https://monitoring.eu-west-2.amazonaws.com`) and
  `build_client("sns")` (`https://sns.eu-west-2.amazonaws.com`), follow the Bedrock client's
  rules (part 1): one attempt per call, `eu-west-2` only, `ignore_configured_endpoint_urls=True`,
  and a refusal at construction unless the client resolves to the service's own endpoint
  (`https://s3.eu-west-2.amazonaws.com`, `https://ssm.eu-west-2.amazonaws.com`). An explicit
  endpoint is for the tests' fakes and must be `http://127.0.0.1:<port>`. The S3 addressing style
  and the accelerate option are set in code, so an `s3 =` section in a config file cannot change
  them.
- **There is no unconditional write and no delete.** The store has `list_keys`, `get`,
  `put_if_absent` (`If-None-Match: *`) and `put_if_match` (`If-Match: <ETag>`), and `create`,
  which is `put_if_absent` with the read-back below.
- **An object is stored as JSON, or as Markdown for a report** (part 5a). Any other content type
  is refused before a request is made.
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
- **A WS7 S3 client never follows a redirect, and never sends one call twice** (Codex review of
  PR #6).
  - botocore registers a region redirector on every S3 client (`S3RegionRedirectorv2`, botocore
    1.43.110, `client.py:404`). On a 301, 302 or 307, a `PermanentRedirect`, an
    `AuthorizationHeaderMalformed` that names a Region, or an
    `IllegalLocationConstraintException`, it re-signs the request for the region S3 named,
    remembers that region for the bucket, and tells the endpoint to send again. `endpoint.py`
    (`_needs_retry`, `:352`) then does, whatever `total_max_attempts` says.
  - `build_client("s3")` registers two handlers. The first marks every request as already
    redirected, which is the redirector's own reason to do nothing (`utils.py:1778`): it neither
    re-sends nor remembers another region. The second counts sends and stops a second one within
    one call before it leaves, whatever asked for it.
  - S3 pointing elsewhere is a failure, `ws7_store_wrong_region`. For a write it is not
    ambiguous: a redirect did not land. A stopped second send is `ws7_store_resend_refused`.
  - Each handler is tested alone, with an ordinary one-attempt client as the control: that client
    does send twice, signs the second request for the other region, and stays moved.
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
field, a wrong type, a duplicate key, or a state that cannot be is `ws7_ledger_invalid`, and the
run does not start. So is any other object under `ws7/ledger/`.

**The month's flag must match its entries, in both directions** (part 3). `assurance_waived` is
true exactly when the month holds an `estimate_accepted` entry. The loader refuses:

- an `estimate_accepted` entry in a month that is not flagged;
- a flagged month with no `estimate_accepted` entry. Nothing in WS7 writes one: only `resolve()`
  sets the flag, in the same write as the entry that causes it, and a resolved entry is never
  dropped.

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
- **A write whose reply failed** counts as a try. Its `write_id` is remembered. If a later read
  shows it, the write landed and is this job's.
  - **After the last try**, if a reply failed, the ledger is read once more and reconciled as
    an earlier retry would be, but **nothing more is written** (Codex review of PR #6; the
    author's rulings, 19 Nov 2026). First the `write_id`: if it is there, the result stands.
    Then the same decision as a retry: if it has nothing to write, because the entry is already
    in exactly the wanted state, that result stands. If it would write, the tries are spent
    and the job stops with `ws7_ledger_conflict`.
  - If another writer has written since, the `write_id` is no longer on the ledger. The job then
    treats an entry that is already in **exactly** the state it wanted as done, and does not
    write it twice.
  - That needs no operation ID, because no other writer can make the same change to this entry.
    Booking, settling, withdrawing and the crash mark are made only by the run's own process,
    which holds the run's exclusive claim. The one change another reader makes to someone
    else's entry is abandonment, which differs by kind. A resolution is made only by the
    author's command. So the end state is the one intended either way.
  - A **different** state is not credited. If another reader abandoned the entry while this
    run's settle reply failed, the settle is refused.
- A ledger that cannot be listed, read or written stops the run: `ws7_ledger_unavailable`, with
  the store's own code.

**One run at a time, across months.** One compare-and-swap covers one object, and an entry in
another month's object can be written after this run's scan. So after its entry is written, a
run reads every month once more and **meets admission rules 1 to 3 again, leaving itself out**
(Codex review of PR #6; the author's ruling, 19 Nov 2026):

- a scheduled run withdraws if any other entry, in any month, is live (`ws7_run_in_progress`),
  or is open past its `expires_at` or unresolved (`ws7_blocked_unresolved`);
- a manual run withdraws only for a live entry;
- either withdraws if the clock has reached its own `deadline_at`
  (`ws7_admitted_past_deadline`): admission took so long that no call may be sent.

It withdraws its own entry at zero (`settled`, `withdrawn`) and does not start. Each run writes
and then reads, so the later read always sees the earlier write: of two runs that race, at least
one withdraws, and both may. If that second read itself fails, the run does not start and its
entry is left open, to be abandoned and resolved.

**Settling** (`Ledger.settle`).

- It takes the guard's report (part 1) and replaces the reservation with `total_usd_micros`: the
  usage Bedrock returned wherever a reply was seen, and the worst case only where none was.
- A report that says `unmetered` is written as `unresolved`, `unmetered`, at that same total, in
  the same write.
- **`settle()` and `mark_crashed()` take the `Admission` that `admit()` returned, not a run ID**
  (Codex review of PR #6). The entry must be the one that `Admission` describes:
  `ws7_admission_invalid` otherwise.
- **The report must be the run's own.** Otherwise `ws7_report_invalid`, and the entry stays
  open.
  - **Its `run_id` must be the `Admission`'s** (Codex re-check of PR #6; the author's ruling,
    19 Nov 2026). The guard is given the run's ID when it is made and puts it in its report. A
    report with no run ID, or with another run's, is refused whatever else it holds.
  - Why: two runs of one target reserve the same amount. A fresh guard from another run has
    spent nothing and has no batch, so nothing else tells it apart, and it would settle a run
    that spent at zero.
  - Its target and its reservation must be the `Admission`'s, and its micro-dollar total must be
    its nano-dollar total rounded up.
  - **Every batch label in it must start with `<run_id>.`** as well. **Part 5 labels its batches
    `<run_id>.preflight` and `<run_id>.main`.**
  - A run's own report with no batch and no cost settles the run at zero.
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
- **The clock is read again inside the write.** If the entry expired while the call was reading
  S3, that same write records the abandonment, and the call is then refused (Codex review of
  PR #6).

**The block.** An unresolved entry in any month blocks every later scheduled run, with
`ws7_blocked_unresolved`. The job finds it by listing every ledger, so an entry two months back,
with no ledger for the month between, still blocks. An unresolved **manual** entry blocks
scheduled runs too: the rule is "an unresolved entry", read as written. A manual run is not
blocked.

**Resolving** (`Ledger.resolve(run_id, basis, settled_usd_micros)`; part 5b adds the command).

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
- **The 120 s margin does not bound a whole model reply plus settlement.** Settling between
  `deadline_at` and `expires_at` is intended, and a write decided before `expires_at` may finish
  after it; but a settle decided at or after `expires_at` is refused.
- A wall-clock limit of 120 s or less is refused (`ws7_wall_clock_limit_too_short`).
- If the clock has reached `deadline_at` by the end of `admit()`, the run withdraws at zero and
  is refused (`ws7_admitted_past_deadline`).
- **Withdrawal at zero happens only before `expires_at`.** If admission's own clean-up reaches
  `expires_at` (the write that would withdraw the entry is decided at or after it), the entry is
  not withdrawn: it becomes **abandoned at its full reservation**, although the run made no
  call, and it blocks later scheduled runs until the author resolves it. The same holds for a
  withdrawal after the re-read finds another run. (Stated here; not separately tested in this
  PR.)
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
| `safety_net_gate` (v1 only) | The same parser. Always on: a WS7 rule, not compared with SSM |

**The runner's argv is an allow-list** (Codex review of PR #6). Before a runner's parser sees an
argv, it must be exactly one of the forms `runner_argv()` builds, which are the forms part 5
passes:

| Target | The only forms |
|---|---|
| Pipeline | `<--all or the pre-flight scenario> --batch <id> --yes` |
| v1 | `<--all or the pre-flight scenario> --deployed-path --batch <id> --yes` |

- The pre-flight scenario is the config's (`S1`). A batch ID is 1 to 64 letters, digits, `.`,
  `_` or `-`, and does not start with a dash.
- **`runner_argv()` checks the pre-flight scenario again** (part 3), with the config loader's own
  pattern, read from `evals/ws7_reserve.py`. The loader already refuses a scenario that starts
  with a dash, but a `Config` built in code never went through the loader. Such a config builds
  no argv, in either form, and `own_values()` accepts none: `ws7_runner_argv_invalid`.
- Anything else is `ws7_runner_argv_invalid`, and nothing is printed, because the parser is
  never called: v1's `--no-gate`, the pipeline's `--verify`, `--dry-run`, `--runs`,
  `--no-prompt-caching`, another prompt or corpus, another scenario, another order.
- So a setting the match check does not compare cannot be changed by the argv either.
- What the parser made of the argv is checked as well: one run, no dry run, no step 7v, and for
  v1 the gate on.

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
| `keys` | The comparator's `keys` object (`docs/WS7-COMPARATOR.md`), checked by the comparator's own code (`ws7_compare.check_keys`). For v1 it holds the two prompt hashes, `patient_second_pass` and `prompt_caching`, and **both must be true** (part 3) |
| `reservation` | `generations`, `margin_percent`, `total_usd_micros` |
| `safety_net_gate` | `true` for v1 (a WS7 rule); `null` for the pipeline, whose runner has no such switch |
| `month_assurance_waived` | The month's flag when the run ran |
| `baseline_id` | The baseline it was compared with, or `null` |
| `part_c` | How PART C was produced: `pipeline_step_7` or `v1_second_pass` |

- The corpus hash, the gold hashes and the scorer versions come from the finished batch and its
  scores, so parts 4 and 5 pass them in. Part 2 checks their shape.
- A manifest holds IDs, hashes, numbers and flags only.
- **The whole manifest is validated twice** (`validate_manifest`): when it is built, and again on
  the exact payload immediately before the conditional PUT (Codex review of PR #6). The check
  covers every field and how they fit together: the run ID is the one its target, mode and
  scheduled time give, the month is that time's, the client settings are the plan's, and the
  PART C label and the gate are the target's. A malformed manifest sends no request.
- **A v1 manifest with `prompt_caching` or `patient_second_pass` off is malformed** (part 3). The
  match check already refuses an expected record with either off; the validator now refuses the
  manifest too, so one cannot be written whatever built it.

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
| Admission | `ws7_reservation_invalid`, `ws7_wall_clock_limit_too_short`, `ws7_run_in_progress`, `ws7_blocked_unresolved`, `ws7_over_cap`, `ws7_entry_exists`, `ws7_admitted_past_deadline` |
| Any ledger write | `ws7_ledger_conflict`, `ws7_ledger_unavailable`, `ws7_ledger_invalid`, `ws7_ledger_write_invalid` |
| Settling and resolving | `ws7_admission_invalid`, `ws7_entry_unknown`, `ws7_entry_not_open`, `ws7_entry_not_unresolved`, `ws7_report_invalid`, `ws7_basis_unknown`, `ws7_settled_figure_invalid`, `ws7_estimate_differs` |
| The match check and the manifest | `ws7_runner_argv_invalid`, `ws7_expected_unreadable`, `ws7_expected_invalid`, `ws7_match_differs`, `ws7_identity_missing`, `ws7_manifest_invalid`, `ws7_manifest_exists`, `ws7_manifest_unavailable` |
| The store | `ws7_store_endpoint_refused`, `ws7_store_bucket_refused`, `ws7_store_key_refused`, `ws7_store_list_failed`, `ws7_store_list_truncated`, `ws7_store_get_failed`, `ws7_store_object_too_large`, `ws7_store_put_refused`, `ws7_store_object_missing`, `ws7_store_create_unsure`, `ws7_store_wrong_region`, `ws7_store_resend_refused` |

### What the fakes cannot prove

The S3 fake answers a conditional write as AWS documents it, handles one request at a time, and
faults on anything it does not implement: an unconditional PUT, a PUT with both conditions, a
delete, any other method or query parameter. It ignores request headers it does not know, because
botocore adds some and a newer botocore may add more. Its ETags are deliberately not MD5s. It can
answer a GET for a missing key with 404 or with 403, and a test shows WS7 behaves the same either
way. It can answer any request as S3 does when a bucket is in another region, and it records the
region each request was signed for. The SSM fake implements `GetParameter` only.

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
- what real S3 answers for a bucket in another region, and that the job stops on it;
- that the SSM parameters are plain Strings the task can read and cannot write;
- the format of EventBridge Scheduler's scheduled time, and that a duplicate delivery carries the
  same value;
- that `WS7_IMAGE_DIGEST` in the task definition is the digest of the image it runs.

## Part 3: the follow-ups from PR #4 and PR #6

Small fixes from the two reviews. No new module, no new refusal code in the entrypoint's
modules, and one new error code in the comparator. No runner, no scorer, and neither
`evals/ws7_client.py` nor `evals/ws7_reserve.py` is changed.

| From | What changed | Where it is described |
|---|---|---|
| PR #4 | A readable input that is not valid JSON keeps its sha256 in the result | `docs/WS7-COMPARATOR.md`, "When an input's hash is null" |
| PR #4 | Tests: every v1 key that differs gives `no_baseline` naming that key | `tests/test_ws7_compare.py` |
| PR #4 | A ruled kind-2 metric must cover exactly the scenarios in `keys.gold_sha256` (`scenario_coverage_differs`) | `docs/WS7-COMPARATOR.md`, "The order of checks" and "The three kinds of rule" |
| PR #4 | The comparator's key check is public, as `check_keys`; the manifest calls that name | "The run manifest" above |
| PR #6 (a) | The ledger loader refuses a month whose flag does not match its entries, in both directions | "The ledger" above |
| PR #6 (b) | A v1 manifest's `prompt_caching` and `patient_second_pass` must be true | "The run manifest" above |
| PR #6 (c) | `runner_argv()` checks the pre-flight scenario again with the loader's pattern | "The match check" above |

**The notes this part closes:** the note under "Notes for later parts" that asked for a public
`check_keys` (part 2 called the comparator's private name, because the comparator was not to be
changed in that PR), and the three follow-ups (a) to (c) from the review of PR #6.

**The rule about coverage, in one place.** The baseline and the run each carry
`keys.gold_sha256`, the scenarios the gold covers. Before part 3 the comparator checked only
that the two files had verdicts for the same scenarios, so both could leave out the same one.
Now each file must cover the gold's scenarios exactly, for every verdict metric a rule names.
Part 4 writes the run summary, so part 4 must write a verdict (`pass`, `fail` or `no_output`)
for every gold scenario in every verdict metric that will be ruled.

## Part 4: the run summary and the candidate baseline

One new module, `evals/ws7_summary.py`, with three functions. Part 5 wires them into the modes.

- `write_haz03(batch_dir)` writes `scores/haz03.json` for a pipeline batch.
- `build_summary(batch_dir, own=..., report=..., batch_label=..., month_assurance_waived=...)`
  returns the run summary (`ws7-run/1`, `docs/WS7-COMPARATOR.md`).
- `build_candidate([s1, s2, s3], baseline_id=...)` returns a candidate baseline.

Nothing in part 4 makes an AWS call or a model call. No runner and no scorer is changed, and
neither is `evals/ws7_client.py` or `evals/ws7_reserve.py`. Two merged modules change:

- `evals/ws7_manifest.py` gains `build_keys()`, and `build_manifest()` calls it.
- `evals/ws7_compare.py`: two checks become public, and one carried fix
  (`docs/WS7-COMPARATOR.md`, "The order of checks").

The approved slice plan's metric list was not in the repository, so the list below was derived
from the merged documents and the scorers' code. The author ruled on it on 20 Nov 2026.

### What a summary is built from

One finished batch of 18, its score outputs, and the guard's report (part 1).

| File | Read for |
|---|---|
| `manifest.json`, `batch.json` | The batch's ID, target, model ID, caching setting, corpus hash, scenarios, and whether each generation has an error |
| `r1/<scenario>.json` (the records) | **Named fields only:** `scenario_id`, `run`, `batch_id`, `elapsed_s`, and the step names in `step_errors` |
| `scores/safety_net.json`, `scores/meds.json` | Both targets |
| `scores/step2_advice.json`, `scores/compose_clinical.json`, `scores/assemble/assemble.json`, `scores/haz03.json` | The pipeline only |

- **No `.md` report and no review file is opened.** They quote model output. A test records every
  file opened and shows it.
- **A string reaches the summary only through a fixed set of labels.** A record holds model
  output, so nothing is copied from one. A step name must be one of the runner's nine
  (`evals/run_pipeline_eval.py:80-82`); a failure label, an outcome or a gate status must be one
  the scorer can emit. A field holding anything else builds no summary. Error codes are compared
  with a literal or not read at all.
- **One whole batch, once.** A batch with `runs` other than 1, a batch that stopped early, or a
  batch whose generations or gold map do not name exactly the manifest's scenarios builds no
  summary (`ws7_summary_batch_incomplete`). The manifest's scenarios are whatever the runner was
  asked for, so a pre-flight batch of one scenario can be summarised. It can never become a
  baseline ("The candidate baseline", below).
  - Why: `score_batch` builds its gold map from the scenarios that were generated
    (`evals/score_batch.py:90`). A scenario that was never generated would be missing from
    `keys.gold_sha256`, not written `no_output`.
- **The batch label is one name in three places:** the runner's batch ID (`--batch`), the batch's
  label in the guard's report, and the summary's `run_id`. It starts with `<run_id>.`. A baseline
  is one run with three batches (ruling e8), so its three summaries have three different IDs.

### HAZ-03

ADR-011 D9 names `evals/haz03_check.py` as a source, but no scorer runs it on a batch.
`write_haz03()` calls `check_record()`, unchanged, on each record and writes the rows it returns.
The gold it reads must be the gold `score_batch` scored against: each file's sha256 must be the
one in `score_batch`'s gold map. The check does not cover v1 (`evals/haz03_check.py:33`).

**A row holds IDs, values from fixed sets and fixed labels only** (Codex review of PR #8).

- The checker promises that (`evals/haz03_check.py:35`), but it copies the facts' `age_group`
  into its row as it finds it (`:88`), and a record holds model output.
- So before a row is written, each field is held to its own allowlist. A value outside it is
  written as the fixed label `not_in_enum`, never as given.
- **The allowlist comes first, for every field and every type** (Codex re-check of PR #8). A
  value is written only if it is exactly one its field may hold, of the right type. A number, a
  boolean or nothing is not written as given: a first version let those through, so `42` or
  `true` in a record's `age_group` reached the file.
- **The verdict and the failures are written exactly as the checker gave them.** What the checker
  gives is always inside their sets. The checker is not changed.

| Field | What it may hold | From |
|---|---|---|
| `facts_age_group` | One of the facts schema's five age groups | `src/generate/pipeline/schemas.py:151` |
| `gold_age_group` | One of the gold's age groups | `evals/gold.py:33` |
| `expected_audience` | `paediatric` or `adult` | `evals/haz03_check.py:50` |
| `verdict` | `pass`, `fail` or `not_scored` | `evals/haz03_check.py:91`, `:104` |
| `failures` | A list in which every item is one of the checker's seven labels | `evals/haz03_check.py:69-82` |
| `check_version` | The integer `CHECK_VERSION`, and no other number | `evals/haz03_check.py:48` |
| `scenario_id` | The scenario the row is for | The batch manifest |
| `check`, `reason` | `HAZ-03`; `no_step_5b` | `evals/haz03_check.py:85`, `:104` |
| Any other field | Nothing: whatever it holds is written `not_in_enum` | |

### The keys

`ws7_manifest.build_keys(own, corpus_sha256=..., gold_sha256=..., scorer_versions=...)` builds
the `keys` object for the run manifest and for the run summary. `ws7_summary.key_parts(batch_dir)`
returns the last three, and part 5 passes the same ones to `build_manifest()`. A test builds both
from one batch and compares them.

| Key | From |
|---|---|
| `target`, `model_id`, `prompt_caching`, and `pipeline_version` or the two v1 prompt hashes and `patient_second_pass` | The job's own values, as `match_check` returned them (part 2). The batch manifest must agree where it holds the same value (`ws7_summary_own_invalid` otherwise) |
| `corpus_sha256` | The batch manifest (`evals/run_pipeline_eval.py:348`, `evals/run_cold_eval.py:675`) |
| `gold_sha256` | `score_batch`'s gold map (`evals/score_batch.py:101-102`) |
| `scorer_versions` | The scorers' own constants (below) |

| Scorer | Version | Target |
|---|---|---|
| `score_batch` | `SCORER_VERSION` (`evals/score_batch.py:34`) | both |
| `score_step2_advice` | `METRIC_VERSION` (`evals/score_step2_advice.py:45`) | pipeline |
| `score_compose` | `METRIC_VERSION` (`evals/score_compose.py:53`) | pipeline |
| `haz03_check` | `CHECK_VERSION` (`evals/haz03_check.py:48`) | pipeline |
| `score_assemble` | **The sha256 of the file's bytes**, as an ID string. The scorer has no version constant, and it is not changed here (the author's ruling, 20 Nov 2026) | pipeline |

- Each score file's own stamped version must equal the constant (`ws7_summary_scores_invalid`
  otherwise).
- `score_patient` is not in the keys: none of its metrics is published.
- Any edit to `evals/score_assemble.py`, a comment included, changes the key, and the next run
  is "no baseline" until a new baseline is frozen.

### The metrics

The set is fixed for each target, and every metric is always present, 0 when nothing was
counted. **Which kind each metric has, and every number, is set in the rules file by the author,
not here.** A v1 summary has the Run, Safety-netting and Medications families only.

**Run**

| Metric | From |
|---|---|
| `cost_usd_micros` | **The batch's own total** in the guard's report (`batches[label].total_usd_nanos`), rounded up to micro-dollars. Not the report's total, which includes the pre-flight and, for a baseline, the other two batches (the author's ruling, 20 Nov 2026). **It is an estimate, not the billed amount:** the usage Bedrock returned, priced at the runner's configured rates (`PRICE_PER_MTOK` times `REGIONAL_PREMIUM`, `evals/run_cold_eval.py:115-116`). **The batch's total is the priced usage of every reply plus the worst-case charge of every call that got no reply** (a throttle, for example), as the guard and the ledger count it (part 1, "After every call"). The v1 throttle test shows it: 34 replies at their usage and one throttled call at its worst case (`test_a_throttled_scenario_is_written_no_output_in_every_verdict_metric`) |
| `tokens_in_total`, `tokens_out_total`, `tokens_cache_read_total`, `tokens_cache_write_total` | The batch's calls in the report that got a reply with usage |
| `input_tokens_max_<step>` | The largest input side of one call (input, cache read and cache write) for each model step: four for the pipeline, two for v1. For the measured input allowance (ADR-011 D7, point 1; slice 4) |
| `elapsed_p90_ms` | The records' `elapsed_s`, every record in the batch |
| `generations_failed` | Generations whose `batch.json` row has an error code |
| `step_failures_<step>` (pipeline; the nine step names) | The step names in each record's `step_errors`. `step_failures_compose_patient` is here: it comes from the runner's record, not from PART C scoring |
| `calls_not_response`, `calls_refused`, `spend_guard_trips` | The batch's calls in the report, by outcome and refusal code |
| `gold_draft_records` | Gold records whose status is not `approved`. The candidate baseline refuses on it |
| `month_assurance_waived` | 0 or 1: the month's flag in the ledger when the run ran |

**Safety-netting** (`scores/safety_net.json`; rows from `evals/score_safety_net.py:261-278`)

| Metric | From |
|---|---|
| `safety_net_verdict` (a verdict) | `verdict` |
| `sn_invented_triggers` | The sum of `seek_help.n_invented` |
| `sn_pinned_line_missing` | Rows with that failure label |
| `sn_route_incorrect` | Rows where `route.correct` is false |
| `sn_review` | Rows whose verdict is `review` |
| `sn_gate_failures` (v1 only) | Rows where the current gate, re-run on the stored output, is not passed (`evals/score_batch.py:167-170`) |

**Medications** (`scores/meds.json`; rows from `evals/score_meds.py:538-549`)

| Metric | From |
|---|---|
| `meds_verdict` (a verdict) | `verdict` |
| `meds_invented_doses`, `meds_invented_frequencies` | The sums of those two fields |
| `meds_partial`, `meds_review` | Rows with that verdict |

**Step-2 advice** (pipeline; `scores/step2_advice.json`; `evals/score_step2_advice.py:90-98`)

| Metric | From |
|---|---|
| `step2_route_verdict` (a verdict) | `route_correct` |
| `step2_gold_items_recalled` | The sum of `verified.n_gold_matched` |
| `step2_not_extracted`, `step2_lost_at_verification`, `step2_non_advice_as_advice` | Rows with that `route_error` |
| `step2_right_route_wrong_item` | Rows where it is true |

**Composition** (pipeline; `scores/compose_clinical.json`, the `summary` of
`evals/score_compose.py:259-288`). Counts only: the scorer gives no verdict for a record.

| Metric | From |
|---|---|
| `compose_not_reached` | `n_records` less `n_scored` |
| `compose_year_added` | `year_added_step6.findings` |
| `compose_boxes_incorrect` | `n_scored` less `boxes_correct` |
| `compose_unknown_fact_ids`, `compose_excluded_fact_ids`, `compose_field_omitted`, `compose_gp_action_missing_in_b` | The fields of those names |
| `compose_contradictions_missing` | `contradictions.given` less `contradictions.in_a` |
| `compose_flagged_advice`, `compose_flagged_resus`, `compose_flagged_medication` | `review.<kind>.flagged`: sentences code flagged for a CSO ruling |
| `compose_checks_total` | Every check count in every row |

**HAZ-03** (pipeline; `scores/haz03.json`)

| Metric | From |
|---|---|
| `haz03_verdict` (a verdict) | `verdict` |
| `haz03_failures` | Every failure label in every row |
| `haz03_<kind>`, for the seven kinds (`evals/haz03_check.py:11-23`) | Rows with that label |

The two kinds that name PART C (`expected_line_missing_part_c`, `other_line_in_part_c`) are
published (the author's ruling, 20 Nov 2026). They read step 5b's `part_c` field, the line that
code inserts, not step 7's text, and they use no frozen file.

**Assembly** (pipeline; `scores/assemble/assemble.json`)

| Metric | From |
|---|---|
| `assembly_not_bundled` | Rows whose outcome is not `bundle` |
| `assembly_rejected_08a`, `assembly_rejected_08b` | Rejections, by step |
| `assembly_upstream_missing` | Rows with that outcome |
| `assembly_gate_failed` | Rows rejected at 8b with `safety_net_gate_failed` |
| `assembly_gate_clean`, `assembly_gate_documented_advice` | The gate status of each bundle. 8b rejects any other status (`src/generate/pipeline/assemble.py:606`) |
| `assembly_placeholders_left` | `summary.placeholders_left` |

- **Rejections and step failures are published by step, not by code** (the author's ruling,
  20 Nov 2026). A metric named after a code would be in some runs and not in others, and a
  baseline needs the same metrics in all three. The codes stay in the stored records.
- **PART C is not published** (ADR-011 D13). `score_batch`'s patient block and its
  `scores/patient_version.json` are never read. Nor is any warning in `assemble.json`:
  `warnings`, `warnings_c`, `warnings_located`, `c_meaning_vs_step7`, `a5` and `branches`. A test
  changes all of them and shows the summary's bytes do not change.
- Subgroup tags (D12) are not in the summary. Adding them is a new schema version (slice 7).

### Units

- **Cost** is integer micro-dollars. The guard counts in nano-dollars, and the conversion rounds
  up, so a cost is never understated ("Money units", part 1).
- **Elapsed time.** The runners record seconds to three decimal places. The value is read as a
  decimal from its JSON text, multiplied by 1,000 and rounded half-even to whole milliseconds.
  No binary float is involved, so the result is exact.
- **p90 is by nearest rank, with no interpolation.** Sort the values and take item
  `ceil(0.9 x n)`, counting from 1. For 18 records that is the 17th. The result is always one of
  the values. It is not `score_compose`'s quantile (`evals/score_compose.py:235`), which rounds
  differently.

### The verdict mapping

"pass" in a run summary is the scorer's own "pass", exactly.

| Scorer | The scorer says | Written |
|---|---|---|
| Safety-net (`evals/score_safety_net.py:257`) | `pass` | `pass` |
| | `review`, `fail` | `fail` |
| Medications (`evals/score_meds.py:527-535`) | `pass` | `pass` |
| | `partial`, `review`, `fail` | `fail` |
| Step-2 advice (`evals/score_step2_advice.py:95`, `:106`) | `route_correct` true / false | `pass` / `fail` |
| | `not_scored` | `no_output` |
| HAZ-03 (`evals/haz03_check.py:91`, `:104`) | `pass` / `fail` | `pass` / `fail` |
| | `not_scored` | `no_output` |
| Any of the four | No row for the scenario: a skipped record or a failed generation | `no_output` |

- `review` (waiting for a CSO ruling) and `partial` are not a pass. Writing either as "pass" would
  make WS7's verdict looser than the scorer's. How many there were is published
  (`sn_review`, `meds_partial`, `meds_review`), so the reason for the fail stays visible.
- Every verdict metric has a verdict for exactly the scenarios in `keys.gold_sha256` (part 3, "The
  rule about coverage"). Nothing is left out.
- A value that is not in the table, a row for a scenario the gold does not have, or two rows for
  one scenario builds no summary. Nothing is guessed.

### Refusals

`build_summary()` checks its result with the comparator's own `check_run()` before returning it.

| Code | When |
|---|---|
| `ws7_unmetered` | The report says a call's usage is unknown. That run's cost is an estimate. The code is the wrapper's own (part 1), and part 5 fails the run with it |
| `ws7_summary_own_invalid` | The job's own values are out of shape, or the batch manifest disagrees with them |
| `ws7_summary_batch_invalid` | The manifest, `batch.json` or a record is missing or malformed, or a read field holds something outside its fixed set |
| `ws7_summary_batch_incomplete` | Not exactly one run of the manifest's scenarios (above) |
| `ws7_summary_scores_invalid` | A score file is missing, malformed, another batch's or another scorer version's, or holds a value outside its fixed set |
| `ws7_summary_report_invalid` | The guard's report is not this batch's: no run ID, another target, no such batch, a label that does not start with the run's ID, or rows that do not add up to the batch's total |
| `ws7_summary_invalid` | The built summary fails `check_run()`, or the month's flag is not a boolean |

### The candidate baseline

`build_candidate()` takes the three run summaries of a manual baseline (ADR-011 D7: 18 x 3) and
a `baseline_id`. The ID is given by the author. It has no default and never comes from the clock.

```json
{"schema": "ws7-baseline-candidate/1",
 "run_ids": ["2026-12-14T0130-pipeline-manual.b1", "2026-12-14T0130-pipeline-manual.b2",
             "2026-12-14T0130-pipeline-manual.b3"],
 "baseline": {"schema": "ws7-baseline/1", "baseline_id": "b-2026-12-14", "keys": {"…": "…"},
              "metrics": {"…": "…"}}}
```

The IDs are invented. Each count becomes a list of three, in the order the summaries were given.
Each verdict becomes a list of three, and the `unstable` list is worked out from the verdicts.
The `baseline` member is checked with the comparator's own `check_baseline()` before anything is
returned.

| Code | When |
|---|---|
| `ws7_candidate_not_three_runs` | Not exactly three summaries |
| `ws7_candidate_id_invalid` | The `baseline_id` is not an ID |
| `ws7_candidate_summary_invalid` | A summary fails `check_run()`, a verdict metric does not cover the gold, or a summary has no `gold_draft_records` count |
| `ws7_candidate_not_whole_corpus` | A summary's `keys.gold_sha256` does not name exactly the scenarios in the corpus file, or the corpus file cannot be read |
| `ws7_candidate_runs_not_distinct` | Two summaries have the same `run_id`: the same batch given twice |
| `ws7_candidate_keys_differ` | The three `keys` are not equal. The refusal names the keys, never the values |
| `ws7_candidate_metrics_differ` | A metric is in some runs and not in others, or is a count in one and a verdict in another |
| `ws7_candidate_draft_gold` | `gold_draft_records` is above 0 in any run. Scores against draft gold are not the baseline (`evals/score_batch.py:225-226`) |
| `ws7_candidate_no_output` | Any verdict is `no_output`. `ws7-baseline/1` has no such verdict |

**A baseline is three whole runs of the corpus** (Codex review of PR #8).

- Three summaries of a pre-flight batch, with three labels, are three valid and distinct run
  summaries with equal keys. A baseline frozen from them would give "no baseline" for every
  real run, because a real run's gold names all 18 scenarios.
- So each summary must name every scenario in the corpus, and no other. The scenarios are read
  from the corpus file itself (`run_cold_eval.load_corpus` on `src/canary/scenarios.json`), not
  from a number written in the code.
- `build_summary()` is unchanged: it does not refuse a batch of fewer scenarios.

**A candidate is never a frozen baseline.**

- The comparator refuses the envelope: its schema is not `ws7-baseline/1`. A test runs the real
  comparator on one and gets exit code 2.
- **Code never freezes a baseline.** The author copies the `baseline` member into the repository
  by pull request, after comparing it with `evals/EVAL_RESULTS.md` (ADR-011 D9, "Freezing a
  baseline"; slice 4).
- `build_candidate()` writes no file.

### What part 4 found

- **The assembly replay stops on any failed pipeline generation.** `evals/score_assemble.py`
  raises a `KeyError` in `summary()` (line 202) when a record has an upstream step missing: such a
  record has no `branches` entry (`:148-150`). ADR-011 D2 said this only of a record in which
  step 1 failed. So today a pipeline batch with any failed generation has no `assemble.json`, and
  `build_summary()` refuses it (`ws7_summary_scores_invalid`). ADR-011 D2 already treats a replay
  that stops as a failed run. The scorer is not changed here; a test pins the behaviour.
- **So a pipeline summary cannot yet show `no_output` from a failed generation.** The mapping is
  tested on the scorers' own `not_scored` rows and on rows removed from a real score file. End to
  end it is tested on v1, with a throttled scenario at the stub.
- **The adjudication files are in no key** ("Notes for later parts", slice 4).

### What the tests cannot prove

- Real token counts, costs and elapsed times. The stub reports the same invented usage for every
  reply.
- A `review` or `partial` verdict from real model output. The stub's replies are the runners' own
  dry-run replies, so those verdicts are written into a copy of a real score file by the test.
- That real output keeps every label inside its fixed set. If a scorer gains a label, a summary
  is refused until this module names it.
- Anything in S3. Part 5 writes the summary and the candidate.

## Part 5a: the scheduled run, the Thursday check, the comparator call and publishing

Part 5 is two pull requests (the author's ruling, 20 Nov 2026). 5a is this section. 5b adds the
manual baseline, the manual confirm and the resolve command; in 5a no manual mode exists.

Two new modules, and changes to three merged ones:

- `evals/ws7_entry.py`: the entrypoint. Two modes, `scheduled` and `check`.
- `evals/ws7_publish.py`: the metric names and values, and the job's two SNS messages.
- `evals/ws7_store.py`: CloudWatch and SNS clients; the keys of success records, failed records
  and a run's stored files; the retained prefix; a content type for a stored report.
- `evals/ws7_manifest.py` and `evals/ws7_summary.py`: carried items C and F (below).
- `tests/ws7_fakes.py`: strict CloudWatch and SNS fakes and one sequence log.
  `tests/ws7_world.py`: the five local servers an entrypoint test runs against.

Nothing has been deployed or run against AWS. No runner, no scorer, no file in
`src/generate/pipeline/`, and none of `evals/ws7_client.py`, `evals/ws7_reserve.py`,
`evals/ws7_compare.py` or `evals/ws7_ledger.py` is changed.

### Running it

```zsh
python evals/ws7_entry.py scheduled --target v1 --scheduled-time 2026-12-16T04:00:00Z
python evals/ws7_entry.py check     --target v1 --scheduled-time 2026-12-17T10:00:00Z
```

| From | What |
|---|---|
| The schedule, as arguments | The mode (the subcommand), `--target`, `--scheduled-time` |
| The task definition, as environment variables | `WS7_BUCKET`, `WS7_ALERT_TOPIC_ARN`, `WS7_GIT_COMMIT`, `WS7_IMAGE_DIGEST` |

- There is no argument for an endpoint, a bucket, a baseline, rules, config or corpus path, a
  clock or a mode. Any other command line is a usage error: exit code 2, and nothing is built or
  sent.
- **A usage error prints one fixed line on stderr and nothing else** (Codex P2, PR #9):

  ```
  usage: ws7_entry.py {scheduled,check} --target {pipeline,v1} --scheduled-time <ISO 8601 with a zone>
  ```

  argparse's own messages quote the argument they refuse (`invalid choice: '…'`,
  `unrecognized arguments: …`), so none of them is ever printed, to stdout or to stderr. That
  holds for an unknown argument, a bad value and a missing one. There is no `--help`: it is a
  usage error like any other.
- `--scheduled-time` takes any string, so a bad one is refused by the job, not by argparse: a
  failed run with `ws7_scheduled_time_invalid`. The value is never printed.
- The alert topic must be an SNS topic ARN in `eu-west-2` (`ws7_topic_refused`).
- **Exit codes:** 0 for a successful run (a breach included), a duplicate, or a check that
  completed; 1 for a failed run or a failed check; 2 for a usage error.

### The order of a scheduled run

Everything before the claim costs nothing and writes nothing to S3.

1. The config, the clients, the commit and the image digest. One live Bedrock client is built
   here, so a refused endpoint stops the run before any claim or batch folder.
2. The distinct-scenarios precheck (below).
3. The match check (part 2).
4. The claim. A duplicate stops here.
5. Admission: the reservation is booked in the month's ledger.
6. The guard, made with `run_id=admission.run_id`.
7. The pre-flight batch, then the main batch.
8. **Settle**, as soon as the last runner returns and before anything is scored.
9. The scorers, the run summary, the comparator.
10. The run's records, then the manifest, to S3.
11. The metrics. On a breach, the job's SNS message.
12. The success record.
13. `RunOk` = 1, last of all.

### The distinct-scenarios precheck

`first_requests()` works out, for every scenario, the fingerprint of the first model request it
would send (`ws7_client.fingerprint`, the hash the wrapper itself uses), and stops the run with
`ws7_scenarios_not_distinct` if two are the same (Codex P3, PR #5).

- The request is built by the runner's own code: `run_pipeline` with no context for steps 5a, 6
  and 7, so only step 1 and step 2's request run; `run_generation` for v1.
- In place of a client there is a plain Python object that records the fingerprint and refuses
  the request. It builds no boto client and opens no socket. Its refusal is not a
  `ThrottlingException`, so neither runner retries or sleeps. Nothing is written.
- What "the same" means depends on the runner. The pipeline splits the notes into lines, so two
  scenarios that differ only in line endings send the same step-2 request. v1 sends the notes as
  they are apart from outer whitespace, so for v1 line endings alone do make two requests.
- A corpus the loader cannot read is `ws7_corpus_unreadable`. A scenario whose first request
  cannot be captured is `ws7_precheck_failed`. A corpus without the pre-flight scenario is
  `ws7_preflight_scenario_unknown`.

### What the runners are given

- **The argv** from `runner_argv()` (part 2), and the batch label as the batch ID:
  `<run_id>.preflight` and `<run_id>.main`.
- **`runs_dir`**: the task's scratch disk. A new temporary folder unless a test gives one.
- **The sleep** (part 1 left this to part 5): `no_wait`, which returns at once. Both runners
  call their sleep in one place only, before they retry a throttled call
  (`src/generate/pipeline/converse.py:43`, `evals/run_cold_eval.py:340`). WS7 refuses every
  retry, so waiting 20 to 30 seconds for one would only use up the run's wall clock.
- **The client**: the guard's client (part 1), behind one more wrapper, described next.
- The runners' own output is discarded. So is the scorers' and the comparator's.

### Once a run has failed, it spends nothing more

The author's change A (20 Nov 2026). Under ruling e2 any call whose outcome is not `response`,
and any refusal, fails the run. The runner does not know that and goes on to the next scenario,
and the wrapper itself refuses the rest only when a call is unmetered.

- Every client a runner gets is wrapped once more in `ws7_entry.py`. `evals/ws7_client.py` is
  not changed.
- Once the guard's report shows a call that is not a `response`, every further call in the run
  is refused with `ws7_run_failed`, in the pre-flight and the main batch. The refusal is a
  `Ws7Refused`, so it ends the generation exactly as the wrapper's own refusals do.
- **The run fails with the first failure's code**, and settles from the guard's report:

| The first call that is not a response | The run's code |
|---|---|
| A refusal by the wrapper | The refusal's own code, for example `ws7_spend_guard` or `ws7_deadline` |
| A throttle, no connection, or a request botocore refused | `ws7_call_failed`, naming the outcome |
| Anything that makes the run unmetered | `ws7_unmetered`, naming the outcome |

- The refusals made here are not in the guard's report. The failed record counts them
  (`calls_stopped`).
- A generation that fails with no call failing (a reply the pipeline cannot use, for example)
  does not trip this: the other scenarios are still generated.

### The deadline

- The guard's deadline is `Admission.deadline_at` less **`SETTLE_MARGIN_S`, 300 seconds**, a
  named constant in `evals/ws7_entry.py`. So the last runner returns with time in hand to settle:
  a settle is several S3 requests, and one decided at or after `expires_at` is refused.
- **Dispatch stops at that deadline.** No runner is started at or after it
  (`ws7_deadline`), and the wrapper refuses every call at or after it.
- A test stands the clock exactly on the deadline (no runner starts, no batch folder, no
  request) and one second before it (the run goes through).
- It is a margin, not a proof. If a settle is still decided at or after `expires_at`, it is
  refused, the entry is abandoned, and the failed record names both codes.
- It is a constant, not a config field, because the config loader is strict and
  `evals/ws7_reserve.py` was not to be changed in this pull request.

### The pre-flight

One scenario, alone, as its own batch (ADR-011 D7). The run goes on only if:

| Check | Otherwise |
|---|---|
| No call failed (above) | The first failure's code |
| Its one generation has no error | `ws7_preflight_failed` |
| The folder holds only files a runner writes, and the three it must | `ws7_preflight_files_unexpected` |
| It cost no more than one generation's worst case. Exactly that much is allowed | `ws7_preflight_over_reservation` |

### What a runner's return means

| The runner | Meaning |
|---|---|
| Exit code 0, or 1, with a `batch.json` | It ran. 1 is a failed generation or, for v1, a gate failure: both are counted, not refused |
| Any other exit code, or no `batch.json` | `ws7_runner_refused`: a canary window, an existing batch folder, a corpus it could not read |
| It raised | `ws7_runner_crashed` |
| `stopped_early`, or fewer generations than scenarios | `ws7_runner_stopped_early` |

### Settling

- `settle()` is called as soon as the main runner returns, before its return is judged and
  before anything is scored. A test reads the batch folder at the moment the settling write
  lands: the records are there and no score file is.
- A failure anywhere after admission settles first. It settles from the guard's report when
  there is a guard, and calls `mark_crashed()` only when there is none.
- A settle is tried once. If it is refused and the run had already failed, the first failure's
  code stays the run's code and the settle's code is recorded beside it.

### Scoring and the summary

`score_batch`, then for the pipeline `score_assemble` and `write_haz03()`, on the main batch;
then `key_parts()` and `build_summary()` (part 4).

- **A failed pipeline generation is a failed run: `ws7_generation_failed`** (carried item E).
  The assembly replay stops on any failed pipeline generation
  (`evals/score_assemble.py:202`), so that batch can have no summary. The run is stopped from
  `batch.json`, before any scorer is called. The scorer is not changed.
- A failed v1 generation with no call failing is not a failed run: it is `no_output` in the
  summary, and the run is compared.
- A scorer that raises is `ws7_scorer_stopped`, naming the scorer. A refusal from
  `build_summary()` is a failed run with the refusal's code.

### The comparator call

- The baseline is `evals/ws7_baseline_<target>.json` and the rules `evals/ws7_rules_<target>.json`.
  Neither exists until slice 4. **A missing file is `ws7_no_baseline`**, decided here; the
  comparator is then not called.
- The comparator is called through `ws7_compare.main`, with an output path that is new for the
  call. **If anything is already at that path the run fails (`ws7_compare_output_exists`) and
  the file is never read** (the author's change D).
- **The exit code is judged before the result is read.** Anything but 0, 1 or 2, or a comparator
  that raises, is `ws7_compare_error`, and the result file is not opened.
- **Exit 2 is never a pass.** The result is read only to tell `no_baseline`
  (`ws7_no_baseline`, naming the keys that differ) from `error` (`ws7_compare_error`).
- For exit 0 or 1 the result must agree with the exit code, name this run's batch and this
  summary's bytes, and have at least one row. Otherwise `ws7_compare_error`.
- **A row for the family `part_c` fails the run (`ws7_part_c_ruled`).** PART C is not published
  (ADR-011 D13), and there is no metric name for it.

### What is stored

Under the run's own prefix, `ws7/runs/scheduled/<run_id>/`:

| Object | Holds |
|---|---|
| `batches/<run_id>.preflight/…`, `batches/<run_id>.main/…` | Each batch folder as the runner and the scorers left it: records, scores, the assembly report |
| `guard_report.json` | The guard's report (part 1). Not in D8's list; added so a cost can be reconciled |
| `summary.json`, `comparison.json` | The run summary and the comparison result |
| `manifest.json` | The run manifest (part 2), with the baseline it was compared with |

Kept, so outside that prefix:

| Key | Written |
|---|---|
| `ws7/success/<run_id>.json` | By a successful run, as its last act but one |
| `ws7/failed/<run_id>.json` | By a run that failed while it held its claim |

- **The file list comes from what the real runners and scorers write** (`expected_files`), and a
  test pins it against a batch they really made, in both directions. **A file that is not on the
  list, is not `.json` or `.md`, or is not a plain file stops the run
  (`ws7_evidence_unstorable`). It is never skipped** (the author's ruling 7). Every file is
  decided before any is sent.
- A `.md` report is stored as `text/markdown; charset=utf-8`, everything else as
  `application/json`. The store refuses any other content type.
- The list includes `scores/patient_version.json`, `scores/PATIENT_VERSION.md` and
  `scores/review_patient.md`, because `score_batch` writes them. They are stored with the rest
  of the scores. No PART C metric is published.
- **The retained prefix is `retained/<run_id>/`, outside `ws7/`** (the author's ruling 9). The
  task role's whole reach is `ws7/`, so the job can never overwrite an incident copy and the
  lifecycle rule does not touch it. The author copies by hand (slice 3's runbook). Nothing in
  WS7 writes it, and the store refuses every key under it.
- A failed run stores what records exist, by the same rule. A run that fails before scoring has
  no manifest: its keys need the scores. Its failed record carries the commit and the digest.

### What is published

Namespace `DischargeAssistant/Eval`, one dimension, `Target`. Every value is a whole number.

| Metric | Value | When |
|---|---|---|
| `RunOk` | 1 | The last act of a successful scheduled run, after its success record |
| `RunOk` | 0 | A scheduled run that failed while it held its claim |
| `RunRefused` | 1 | A scheduled run that stopped, for any reason but a duplicate |
| `Duplicate` | 1 | A duplicate delivery |
| `NoBaseline` | 1, or 0 | No baseline; or compared |
| `Breach<Family>Kind<n>`, for example `BreachMedicationsKind1` | 0 or 1 | One for each family and kind in the comparison. 21 names; none for PART C |
| The Run family's counts, for example `CostUsdMicros`, `ElapsedP90Ms`, `StepFailuresExtractFacts` | The summary's count | A run that reached a summary. 25 for the pipeline, 14 for v1 |
| `SpendGuardTrips`, `CallsRefused`, `CallsNotResponse` | From the guard's report, over the whole run | A run that failed after admission and has no summary |

- A Run metric's name is its summary name in capitals with no underscores:
  `cost_usd_micros` is `CostUsdMicros`.
- **`RunOk` comes only from the process that holds the scheduled claim** (the author's ruling 3).
  Before the claim a process cannot know it is not a duplicate. So a stop before the claim
  publishes `RunRefused` alone. A duplicate and the check never publish `RunOk`.
- **A breach is a successful run** (the author's ruling 5): it generated, scored, compared and
  stored. It publishes `RunOk` = 1 and exits 0.
- The metrics are published before the success record. If they cannot be, the run fails
  (`ws7_publish_failed`): a breach value that was not published is never followed by `RunOk` = 1.
- If `RunOk` = 1 itself cannot be published, the success record stands, no failed record is
  written beside it, and the job exits 1. The backstop alarm may then fire.
- D7 says each spend-guard trip is published. Under e2 a tripped run has no summary, so the
  three guard counters are published from the report on the failure path.
- Until `evals/score_assemble.py` is fixed, `StepFailures…` and `GenerationsFailed` can only be 0
  in a published pipeline run. The failed record carries the count.
- A name outside the set above, or a value that is not a whole number, sends nothing
  (`ws7_publish_invalid`).

**The breach message.** Sent only on a breach, after the metrics and before the success record.
If it cannot be sent, the run fails (`ws7_alert_failed`). A failed scheduled run sends no message
of its own (the author's ruling 4): the Thursday check and the alarms report it. An invented
example:

```
WS7 breach - target v1 - run 2026-12-16T0400-v1
family medications, kind 1 (zero-tolerance counter): meds_invented_doses 3, baseline max 1
system release: v1 prompts 5d0c...9a / 77e1...42 / model anthropic.claude-sonnet-4-6 / caching on
baseline: b-2026-12-14  evidence: s3://<eval-bucket>/ws7/runs/scheduled/2026-12-16T0400-v1/
report time (the alarm): 2026-12-16 04:41 London
```

A message is built from the comparison result, run IDs, hashes and words written in
`evals/ws7_publish.py`. Every piece is held to a pattern before it is used, and the finished
text is checked once more against the characters a message may hold. A test puts a sentinel in
every stub reply: it is in the stored record, and in no CloudWatch request, no SNS request and
no line the job printed.

### A failed run

Every handled failure is a `Stop` with a fixed code. In this order: the ledger is settled; what
records exist are stored; the failed record is written; the metrics are published. Each step is
tried whatever became of the one before.

| Stage | Codes | Failed record | `RunOk` = 0 |
|---|---|---|---|
| A usage error | Exit code 2 | No | No |
| Before the claim | `ws7_config_invalid`, `ws7_topic_refused`, `ws7_store_bucket_refused`, `ws7_identity_missing`, the endpoint refusals, `ws7_corpus_unreadable`, `ws7_precheck_failed`, `ws7_preflight_scenario_unknown`, `ws7_scenarios_not_distinct`, the match check's codes, the claim's own refusals | No | No (`RunRefused` only) |
| A duplicate | `ws7_duplicate` | No | No |
| Admission | The ledger's codes | Yes | Yes |
| Dispatch | `ws7_deadline`, `ws7_runner_refused`, `ws7_runner_crashed`, `ws7_runner_stopped_early` | Yes | Yes |
| Calls | `ws7_call_failed`, `ws7_unmetered`, a refusal's own code | Yes | Yes |
| The pre-flight | `ws7_preflight_failed`, `ws7_preflight_files_unexpected`, `ws7_preflight_over_reservation` | Yes | Yes |
| Scoring and the summary | `ws7_generation_failed`, `ws7_scorer_stopped`, `build_summary()`'s codes | Yes | Yes |
| The comparator | `ws7_no_baseline`, `ws7_compare_error`, `ws7_compare_output_exists`, `ws7_part_c_ruled` | Yes | Yes |
| Storing and publishing | `ws7_evidence_unstorable`, `ws7_evidence_unavailable`, the manifest's codes, `ws7_publish_failed`, `ws7_alert_failed`, `ws7_success_record_unavailable` | Yes | Yes |
| Any other exception | `ws7_crashed` | If the claim is held | If the claim is held |
| `RunOk` = 1 could not be published | `ws7_runok_publish_failed`, in the log | No | No |

The failed record (`ws7-failed/1`) holds the run's ID, target and scheduled time, the code, the
IDs and counts the stop carried, any further codes (`also`), what became of the ledger entry
(`ledger`), whether the records were stored (`evidence`), the commit, the digest and the time.

### The Thursday check

`check` makes no Bedrock call, no claim, no SSM read and no CloudWatch call. It reads S3 and
publishes to the topic.

1. `Ledger.expire()`, **before** `Ledger.unresolved()` (the author's change E). So a run that
   died on Wednesday is marked abandoned and reported that Thursday.
2. The claim of the UTC day **`CHECK_LOOKBACK_DAYS`, 1,** before the check's own scheduled time.
   A named constant in `evals/ws7_entry.py`, tested at 0, 1 and 2 days.
3. **The run's ID is read from the claim object. The check never builds one** (the author's
   change B). In British Summer Time the 04:00 London run is 03:00 UTC, and its ID is
   `<date>T0300-<target>`. Tests cover a winter week and a summer week.
4. That run's failed record and success record.

| What it finds | The line it reports |
|---|---|
| No claim | `no claim: the run did not start, or stopped before its claim; see the task log` |
| A claim and a failed record | `failed run <run_id> code <code>`, with the IDs the stop carried. **If a success record is there too, it is still a failed run** |
| A run blocked by an unresolved entry | `failed run <run_id> code ws7_blocked_unresolved: blocked by unresolved <entry's run_id>` |
| A claim and neither record | `run <run_id> started and did not finish: no success record` |
| A claim and a success record only | Nothing |
| Any unresolved entry, in any month | `unresolved entry <run_id> (<kind>, month <month>): it blocks every scheduled run until it is resolved` |
| The occurrence's month is waived | `month <YYYY-MM>: assurance waived`, every week of that month |

- **The "no claim" line does not say the run never started** (the author's change C). A stop
  before the claim, at the match check for example, also leaves no claim.
- One message is sent when there is anything to report. With nothing to report the check is
  silent and exits 0.
- A check that fails says so on the topic if it can still reach it, and exits 1.

### The log

One fixed line for each stage: labels, IDs and counts. A stop is printed as `Stop.line()`. No
traceback is printed, and of an unexpected exception only its class name.

### Carried items closed

| Item | What changed |
|---|---|
| A | The PR #6 wiring: the guard's run ID, dispatch stopping at the deadline, settling before scoring with time in hand, `mark_crashed()` from the outermost handler when there is no guard |
| B | The distinct-scenarios precheck, on the first request's fingerprint |
| C | `build_keys()` and `build_candidate()` check what they were given before they copy it. The copy is a JSON round trip, which turns an integer scenario key `1` into `"1"`. (`build_summary()` copies nothing itself: its keys come from `build_keys()`) |
| D | A v1 acceptance check through the entrypoint (Tests, below) |
| E | A failed pipeline generation is `ws7_generation_failed` |
| F | A corpus file whose `notes` is not a string makes the runner's loader raise `AttributeError`. `build_candidate()` translates it to `ws7_candidate_not_whole_corpus` |

### What 5a does not do

- The manual baseline, the manual confirm and the resolve command (5b).
- It writes nothing under `retained/`.
- It does not pass `ExpectedBucketOwner` (slice 3).
- It does not end a process that hangs. The guard's deadline ends the model calls; scoring and
  storing after it are bounded only by the clients' timeouts.

### What the new fakes cannot prove

The CloudWatch fake implements `PutMetricData` only and the SNS fake `Publish` only. Each speaks
one wire protocol, the one botocore 1.43.110 uses for that service: `json` for CloudWatch and
`query` for SNS. **A request in any other protocol is a fault that names the protocol**, so a
botocore that prefers another format fails the tests by name. Slice 3's set-up checks must show,
with the task role:

- that real CloudWatch accepts these metric names, the `Target` dimension, the unit and the
  values, and that the role's `cloudwatch:PutMetricData` is limited to the namespace;
- that each alarm sees its datapoints, with the statistic, the period and the window as designed;
- that the KMS-encrypted topic accepts the role's `Publish`, and that the message is delivered
  and readable as an email;
- real error shapes, throttling and size limits for both services;
- that a `.md` object is stored and served as Markdown on the real bucket.

## Notes for later parts

### The next PR that may change `evals/ws7_reserve.py`

- Make the config loader's scenario pattern public. `runner_argv()` (part 3) reads the private
  name `reserve._SCENARIO_RE`, so that the rule lives in one place; `evals/ws7_reserve.py` was
  not to be changed in that PR.
- The author may then move `SETTLE_MARGIN_S` and `CHECK_LOOKBACK_DAYS` (part 5a) into
  `evals/ws7_config.json`. They are constants in `evals/ws7_entry.py` only because the loader
  refuses an unknown field.

### Part 4: the run summary

Both notes are closed by part 4:

- the month's "assurance waived" flag is the count metric `month_assurance_waived` (0 or 1), in
  the Run family;
- every verdict metric has a verdict for every scenario in `keys.gold_sha256`, and for no other.

### The next PR that may change `evals/score_assemble.py` (it needs the author's yes)

- `summary()` stops with a `KeyError` on a record with an upstream step missing (part 4, "What
  part 4 found"). Until it is fixed, a pipeline batch with a failed generation has no summary.
  Part 5a stops such a run before the scorers, with `ws7_generation_failed`. When the scorer is
  fixed, that stop is removed in the same pull request, and `StepFailures…` and
  `GenerationsFailed` can then be above 0 in a published pipeline run.
- The scorer has no version constant. Part 4 keys it by the sha256 of its bytes.

### Part 5: the modes

**Closed by part 5a:** wiring part 4 and part 2 (the order, the batch labels, the guard's run ID
and deadline, settling before scoring, `mark_crashed()`); a scorer that stops and a refused
summary as failed runs; where a frozen baseline is read from; the distinct-scenarios precheck;
`ws7_unmetered` as a failed run's code; the check-mode report; one live client before the runner
starts; the failed record; the prefixes of success records and retained copies.

### Part 5b: the manual modes and the resolve command

- **The yes** (the author's ruling, 20 Nov 2026; the details are for 5b's plan). A manual mode
  needs, in the invocation itself, the run ID the job derives and the reservation it computes.
  **A manual command without its yes stops with `ws7_manual_not_approved` and prints the run ID
  and the reservation it derived**, so the author copies them and does not work them out.
- **Manual runs publish no CloudWatch metric.**
- 5b uses 5a's run: the precheck, the match check, the guard's deadline, the wrapper that stops
  every call once the run has failed, and settling before scoring.
- **Where a candidate goes.** The manual baseline mode writes the envelope under the run's own
  prefix, `ws7/runs/manual/<run_id>/`. 5b names the object. It is never written to a path a
  baseline is read from.
- **Part 5b summarises three batches.** Their labels start `<run_id>.`; a summary's `run_id` is
  its batch label.
- **The resolve command.** A manual mode, run by the author only, that takes the run ID, the
  **basis** (`upper_bound` or `estimate_accepted`) and the **settled figure** in micro-dollars,
  and records them on an unresolved entry (unmetered, abandoned or crashed; part 2).
  - For `upper_bound` the settled figure is the Cost Explorer figure exactly as defined above. The
    command cannot check that figure, so the basis is recorded as the author's statement.
  - The command makes no Bedrock call and calls `Ledger.resolve()`.
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

### Part 6: the image

- The `Dockerfile` takes the commit as a build argument and sets `WS7_GIT_COMMIT` from it
  (part 2, "The run manifest").

### Slice 4: the baseline

- **No baseline is frozen until the author has decided how adjudications are keyed** (the
  author's ruling, 20 Nov 2026). It is decided before slice 4's baseline pull request.
  - The gap: `score_batch` reads the CSO's rulings from `evals/gold/adjudications/<ID>.json`
    (`evals/score_batch.py:97-100`), and they change verdicts. But `keys.gold_sha256` holds the
    hash of `evals/gold/<ID>.json` only (`:102`).
  - So a new ruling can move a verdict while every key stays the same, and the comparator would
    compare the run with a baseline scored under other rulings.
  - Part 4 does not fix it.
- The measured input allowance (ADR-011 D7, point 1) can be read from the candidate's
  `input_tokens_max_<step>` counts.
- The rules file names every metric it rules by the names in part 4, "The metrics".

### Slice 3: the stack

- The task role gains `s3:ListBucket` on the eval bucket, limited to the `ws7/` prefix (ADR-011
  D6; the author's ruling, 19 Nov 2026).
- The lifecycle rule that expires scheduled runs selects `ws7/runs/scheduled/` and nothing else.
- The task definition sets the bucket name and `WS7_IMAGE_DIGEST`, and pins the image by that
  digest.
- The set-up checks in "What the fakes cannot prove" (part 2) and "What the new fakes cannot
  prove" (part 5a).
- The task definition also sets `WS7_BUCKET` and `WS7_ALERT_TOPIC_ARN` (part 5a). The topic must
  be in `eu-west-2`.
- The task role may `cloudwatch:PutMetricData` in the namespace `DischargeAssistant/Eval` and
  `sns:Publish` to the eval topic.
- The check's schedule is one UTC day after the run's (`CHECK_LOOKBACK_DAYS`, part 5a). Each
  schedule passes its own scheduled time and its subcommand: `scheduled` or `check`.
- An alarm on a breach metric must not treat missing data as a breach: a failed run publishes
  no breach value. `RunOk` and `NoBaseline` are what report a run that did not compare.
- The runbook's triage step copies an incident's run to `retained/<run_id>/` by hand, with the
  author's own credentials.
- **Pass `ExpectedBucketOwner` on every S3 call.** The task definition supplies the account ID,
  so a mistyped bucket name can never write into another account's bucket.

## Tests

```zsh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q tests/test_ws7_compare.py tests/test_ws7_reserve.py tests/test_ws7_client.py tests/test_ws7_store.py tests/test_ws7_ledger.py tests/test_ws7_manifest.py tests/test_ws7_summary.py tests/test_ws7_publish.py tests/test_ws7_entry.py
```

- `tests/test_ws7_compare.py`: the comparator (`docs/WS7-COMPARATOR.md`, "Tests").

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
  with an ordinary client as the positive control. S3 pointing at another region: one request at
  the server and a fixed code, with an ordinary client that does send twice as the control. The
  key layout, and `Stop`.
- `tests/test_ws7_ledger.py`: the claim (a duplicate, a manual run on the scheduled date, two
  deliveries at once, a write that landed but whose reply failed); the compare-and-swap at
  `ledger_cas_attempts` and one below; admission at the cap and one micro-dollar over; the
  expiry at `expires_at` and one second before, with an injected clock; every test in the Part 2
  list for the unresolved state (each case blocks, each resolves on both bases, the month
  before, two months back with a gap, a manual run not blocked, the basis stored and an unknown
  one refused, the flag set, not set and kept); the monthly bound with invented costs; and
  the Codex review's cases (an entry that appears in another month between the scan and the
  re-read, the last try's reply failing, a report swapped between two runs, an entry that
  expires during the call, an admission that ends at its own deadline, another run's fresh
  empty report). Part 3: a month whose flag does not match its entries is refused in both
  directions; at the server, a flagged month with no accepted estimate stops the run with no
  ledger write, and its partner, with the accepted estimate, admits it.
- `tests/test_ws7_manifest.py`: the job's own values for both targets; the argv allow-list, with
  the parser shown never to be called for a refused argv; a malformed manifest sending no
  request; each expected value
  differing, with a matching partner; v1's second pass and caching; a missing parameter; the
  commit and the digest from the mapping given; the manifest for both targets, checked by the
  comparator's own key check; written once. Part 3: a v1 manifest with caching off is not
  built and sends no request, with a v1 manifest that is written as the control; and a `Config`
  built in code whose pre-flight scenario is not a scenario ID builds no argv, with the
  runner's parser shown never to be called.

- `tests/test_ws7_summary.py` (part 4). Every batch is invented: the real runners, given the
  argv `runner_argv()` builds, run against the stub with `runs_dir` in a temporary folder, and
  the real scorers, unchanged, score that fresh batch.
  - **The acceptance check.** Three stub batches give three summaries and a candidate. The test
    copies the `baseline` member to a file, as the author would. The real comparator
    (`ws7_compare.main`, a fresh output path, the exit code checked before the result is read)
    then compares a fourth summary with it, under an invented rules file:
    - exit 0 for an unchanged run;
    - exit 1 for a quality change: the stub's step-2 reply reports one line of the notes as
      documented advice, for one scenario that passed safety-netting three times. The scenario
      is taken from the candidate's stable list when the test runs. The scorer's own verdict
      becomes `fail`, and no call fails;
    - exit 2 for a run with another `pipeline_version`.
  - A throttled scenario at the stub (v1): `no_output` in every verdict metric, and no
    candidate from three such summaries. A 500 at the stub: `ws7_unmetered`, and no summary.
  - The rules, each on a copy of one scored stub batch: every verdict value in "The verdict
    mapping"; every count metric against its source; the cost as the batch's own part of a
    two-batch report; the percentile and the millisecond conversion at their boundaries; every
    refusal with a partner that is not refused; a read field holding anything outside its label
    set, with a label inside the set as the positive control.
  - PART C: every patient score and every assembly warning is changed, and the summary's bytes
    are not; the control changes a published field of the same file. A recorder on file opens
    shows that only the named JSON files are opened; the control opens a report.
  - The same bytes under four `PYTHONHASHSEED` values, each in a child process that scores its
    own copy of the batch. The control: `score_batch`'s own output order did differ. Each child
    has a time limit that fails the test.
  - The summary's keys equal the manifest's, for both targets.
  - The candidate: counts and verdicts as three-item lists, the unstable list, every refusal at
    each of the three positions, and the comparator refusing the envelope and accepting its
    `baseline` member.
  - HAZ-03: a value of every JSON type (a string, an integer, a float, a boolean, null, a list,
    an object) written into a record's `age_group`, with step 5b present, never reaches
    `scores/haz03.json`; the control is the checker's own row, which carries the raw value. For
    every field of a row, a value of each of those types outside its set is written
    `not_in_enum`. The partner: every value inside its set is written exactly as given, with the
    same verdict.
  - Three scored pre-flight batches, made at the stub for both targets, make no candidate; the
    partner is three full batches. The corpus is shown to be read from its file: with one
    scenario more or fewer in the file, the same three full summaries are refused.

- `tests/test_ws7_publish.py` (part 5a): the metric names (the Run list pinned against a real
  summary's metrics; 21 breach names and none for PART C); every list that is not all known
  names and whole numbers sending nothing; the two messages line by line; a message built from
  anything but an ID, a count or a fixed label refused; the final check of a message's
  characters; one try per call. The two new clients' endpoints with each override configured,
  with an ordinary client as the positive control. The fakes' own strictness: a request in
  another wire protocol is a fault that names it.
- `tests/test_ws7_entry.py` (part 5a). The real entrypoint, in-process, against five local
  servers (`tests/ws7_world.py`): the Bedrock stub and the S3, SSM, CloudWatch and SNS fakes.
  - **Order is proved from one locked sequence log** that all five servers append to as each
    request arrives, never from timestamps. Time is one injected clock that moves only when a
    test moves it.
  - **The acceptance check, for both targets, through the entrypoint.** Three scheduled runs
    against one frozen invented baseline: exit 0; a breach, which is a success with its message;
    and a baseline for another `pipeline_version` or another v1 prompt hash, which is "no
    baseline" and a failed run. The pipeline's breach is part 4's step-2 flip. v1's is a count:
    one stub reply lists a gold drug that has no documented dose, with a dose, and
    `meds_invented_doses` passes the baseline's highest count. The scenario is chosen when the
    test runs.
  - The order of a successful run; nothing sent before the match check, and no model call before
    the claim and the reservation; the settle landing before any score file exists; `RunOk` = 1
    as the last request any server received.
  - A throttle on the main batch's second scenario: no request for any later scenario reaches the
    stub; the partner sends all 18. A 500: unmetered and unresolved. A failed pre-flight: the
    main batch never starts.
  - The deadline at its boundary and one second before; a deadline that passes mid-run; a
    settle decided after `expires_at`.
  - The pre-flight's three checks, the cost one at exactly the reservation and one cache-read
    token over.
  - The comparator: a missing file; an exit code that is not 0, 1 or 2, with the result shown
    never to be read; ten results that disagree with their exit code; a file already at the
    output path; a rule for PART C.
  - Storing: the file list equal, in both directions, to what the real runners and scorers
    wrote; an extra file, or a link, stopping the run with nothing stored.
  - The precheck: its fingerprint equal to the wrapper's for every scenario; two scenarios with
    one first request stopping the run with no request at any server; the sleep shown never to
    be called, with a control in which a throttling client does make the runner sleep.
  - The check: a winter week and a summer week; each line of the table above; the look-back at
    0, 1 and 2 days; a run that died on Wednesday reported as abandoned that Thursday.
  - Every other command line, `--help` included: exit code 2, the one fixed usage line, nothing
    built and nothing sent. An invented sentinel in the rejected argument, for each kind of
    argument error, is in neither stream; the positive control gives the same command line to
    the same parser built on plain argparse, which prints it.

**Keeping the tests offline.** Every test in the three part 2 files, the part 4 file and the two part 5a files uses `offline_aws`, and every
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
