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
| 6 | The image changes | **Built** |

Slice 3 (the stack, `docs/WS7-RUNBOOK.md`) changes one thing in the job: every S3 request names
the bucket's owner ("Slice 3: the bucket's owner", below).

Nothing here makes an AWS call or a model call, and nothing has been deployed or run live. No
image has been pushed to a registry.
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
- **Every request names the bucket's owner** (slice 3; the author's ruling, 23 Nov 2026). A
  `Store` is built with the bucket and the 12-digit account that owns it, and its three S3 calls
  pass that account as `ExpectedBucketOwner`. S3 answers 403 for a bucket another account owns, so
  a mistyped bucket name can never read from or write into someone else's bucket. A missing or
  malformed account is refused when the store is built (`ws7_store_owner_refused`).
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
- **The report must be complete and consistent** (part 5a; Codex P1, PR #9, second re-check).
  `report_fault()` checks it before any write, and `settle()` refuses a report that fails:

  | Rule | Otherwise |
  |---|---|
  | Complete: every call has a final outcome. No row is left `sent` | `ws7_report_incomplete` |
  | Every row's outcome is one of the guard's final outcomes: a reply, a reply with no usable usage, a refusal, or one of the seven labels of a call that failed | `ws7_report_invalid` |
  | The report's own `unmetered_calls` and `unmetered` flag equal what the rows give | `ws7_report_invalid` |
  | The run's total is the sum of its rows' costs, and each batch's total is the sum of that batch's rows | `ws7_report_inconsistent` |
  | A refused call cost nothing, and every other call is in a batch the report names | `ws7_report_inconsistent` |
  | The rows, the batches and those totals are there, in their types | `ws7_report_invalid` |

  - **Settlement works everything out from the rows** (Codex, PR #9, third re-check).
    `settlement()` computes the total, the number of unmetered calls and the unmetered status
    from the rows alone, and `settle()` writes the entry at those. Nothing the report says about
    itself is used: its own total, count and flag only have to equal what the rows give.
  - The outcomes and the rule for "unmetered" are the guard's own, **imported from
    `evals/ws7_client.py`, not copied** (`RESPONSE`, `USAGE_UNKNOWN`, `REFUSED`, `NO_RESPONSE`,
    and `_unmetered()`). That file is not changed.
  - The case this closes: a call whose cost is unknown (a 500, say) in a report whose own count
    said 0 and whose flag said metered. Taking the report's word, the run was settled as
    metered.

  - Why: the guard writes a call's row as `sent`, and only when the call ends does it give the
    row its outcome and cost, then add the cost to the run's total, then to the batch's, then
    mark the run unmetered if it is (`evals/ws7_client.py`, not changed). Anything that ends the
    work between two of those steps leaves a report whose total is not the run's cost. Since
    the failure path catches an interrupt and carries on, it must not trust that total.
  - A report that fails means the run's cost is unknown. **Unknown cost is unresolved, never
    metered**: the entry stays open, and the entrypoint then calls `mark_crashed()` (below).
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
| Crashed | The run itself: part 5 calls `mark_crashed()` when it has no guard report to settle from, **or when the report cannot be settled from**: it is incomplete, inconsistent or cannot be read (the author's ruling, 21 Nov 2026) | At once | Its reservation |
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
| `WS7_GIT_COMMIT` | The 40-character commit the image was built from | The image: a build argument in the `Dockerfile`, with no default (part 6). **Never the task definition** |
| `WS7_IMAGE_DIGEST` | `sha256:` and 64 hex characters | The ECS task definition, beside the image reference, which pins the image by the same digest (slice 3) |

- A run with either missing or malformed is refused: `ws7_identity_missing`.
- The digest is the deployer's statement. Slice 3's set-up check compares it with the task
  definition's image reference.
- The commit is the builder's statement: the image is built from a clean checkout of the commit
  it names (part 6, "The commit").

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
| The claim | `ws7_duplicate`, `ws7_target_unknown`, `ws7_mode_unknown`, `ws7_scheduled_time_invalid`, `ws7_scheduled_time_future`, `ws7_start_too_late`, `ws7_claim_unavailable`, `ws7_claim_invalid` (part 5a: the object failed the validator the Thursday check uses, and was not written) |
| Admission | `ws7_reservation_invalid`, `ws7_wall_clock_limit_too_short`, `ws7_run_in_progress`, `ws7_blocked_unresolved`, `ws7_over_cap`, `ws7_entry_exists`, `ws7_admitted_past_deadline` |
| Any ledger write | `ws7_ledger_conflict`, `ws7_ledger_unavailable`, `ws7_ledger_invalid`, `ws7_ledger_write_invalid` |
| Settling and resolving | `ws7_admission_invalid`, `ws7_entry_unknown`, `ws7_entry_not_open`, `ws7_entry_not_unresolved`, `ws7_report_invalid`, `ws7_report_incomplete`, `ws7_report_inconsistent`, `ws7_basis_unknown`, `ws7_settled_figure_invalid`, `ws7_estimate_differs` |
| The match check and the manifest | `ws7_runner_argv_invalid`, `ws7_expected_unreadable`, `ws7_expected_invalid`, `ws7_match_differs`, `ws7_identity_missing`, `ws7_manifest_invalid`, `ws7_manifest_exists`, `ws7_manifest_unavailable` |
| The store | `ws7_store_endpoint_refused`, `ws7_store_bucket_refused`, `ws7_store_owner_refused` (slice 3), `ws7_store_key_refused`, `ws7_store_list_failed`, `ws7_store_list_truncated`, `ws7_store_get_failed`, `ws7_store_object_too_large`, `ws7_store_put_refused`, `ws7_store_object_missing`, `ws7_store_create_unsure`, `ws7_store_wrong_region`, `ws7_store_resend_refused` |

### What the fakes cannot prove

The S3 fake answers a conditional write as AWS documents it, handles one request at a time, and
faults on anything it does not implement: an unconditional PUT, a PUT with both conditions, a
delete, any other method or query parameter. It ignores request headers it does not know, because
botocore adds some and a newer botocore may add more. One header it requires (slice 3): a request
with no `x-amz-expected-bucket-owner` is a fault, and one that names another account is answered
403, as S3 answers it. Its ETags are deliberately not MD5s. It can
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
| The task definition, as environment variables | `WS7_BUCKET`, `WS7_BUCKET_OWNER` (slice 3: the 12-digit account that owns the bucket), `WS7_ALERT_TOPIC_ARN`, `WS7_IMAGE_DIGEST` |
| The image, set when it is built | `WS7_GIT_COMMIT` (part 6). The task definition must not set it |

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
- **A call whose accounting did not finish stops the run too** (Codex P1, PR #9, second
  re-check). The wrapper looks at the guard's report only between calls: before one is sent,
  and after one has returned or raised. At those moments every call should have its outcome
  and the totals should be the sum of the rows. A row still `sent`, a report
  `ws7_ledger.report_fault()` refuses, or a report that cannot be read means the accounting of
  a call did not finish: the run's cost is unknown, and it sends no further call. The run
  fails with `ws7_report_incomplete` or `ws7_report_inconsistent`.
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

One scenario, alone, as its own batch (ADR-011 D7). **The go/no-go is D7's, and it is three
things:** no failed generation, only the expected files, and a cost within its reservation.

| Check | Otherwise |
|---|---|
| No call failed (above), and the runner's return is one the batch explains (below) | The first failure's code, or `ws7_runner_refused` |
| Its one generation has no error | `ws7_preflight_failed` |
| The folder holds only files a runner writes, and the three it must | `ws7_preflight_files_unexpected` |
| It cost no more than one generation's worst case. Exactly that much is allowed | `ws7_preflight_over_reservation` |

**A v1 safety-net gate failure on the pre-flight scenario is not a no-go** (the author's ruling,
21 Nov 2026). The v1 runner returns 1 for it and records it in `batch.json` (`gate_ok` false).

- It is quality evidence, not a fault in the run. The main batch runs, and the comparator judges
  the gate's result there, against the baseline, like any other quality metric.
- Stopping at the pre-flight would turn a safety-netting regression into "pre-flight failed":
  a failed run, with no comparison, no breach value and no breach alarm. The regression WS7
  exists to report would be reported as a broken harness.
- A test moves the pre-flight to a scenario the stub's v1 text fails the gate on: the pre-flight
  returns 1, the main batch runs, and the run is compared and succeeds.

### What a runner's return means

| The runner | Meaning |
|---|---|
| Exit code 0, with a `batch.json` that records no failed generation and no gate failure | It ran, clean |
| Exit code 1, with a `batch.json` that records a failed generation or, for v1, a gate failure | It ran. Both are counted, not refused |
| Exit code 0 or 1 that the `batch.json` does not explain | `ws7_runner_refused`, with the exit code and both counts |
| A `batch.json` whose entries are not in the shape that target's runner writes | `ws7_runner_refused`, with the exit code |
| Any other exit code from 0 to 255, or no `batch.json` | `ws7_runner_refused`, with the code: a canary window, an existing batch folder, a corpus it could not read |
| A return value that is not an integer from 0 to 255 | `ws7_runner_refused`, with the fixed label `exit_code_not_a_code`; the value is never printed ("The log", below) |
| It raised anything: an exception, an interrupt, or a `SystemExit` with any code | `ws7_runner_crashed` |
| `stopped_early`, or fewer generations than scenarios | `ws7_runner_stopped_early` |

**A runner ends only by returning** (Codex P2, PR #9, re-check). A `SystemExit` a runner
raises, with any code (0, 1, 2, None or a string), is a crash of that runner. It is never read
as an exit code: before this fix a `SystemExit(0)` raised after a clean index was read as a
clean return, and the run went on to `RunOk` = 1. The runner's argv is an allow-list, so a
parser exit already means something is wrong.

The same rule holds for every other piece of code the job calls and does not own, and for the
outermost handlers. Each handler is for `BaseException`:

| Where | What a raise becomes |
|---|---|
| A runner, in the pre-flight or the main batch (`_dispatch`) | `ws7_runner_crashed` |
| A scorer: `score_batch`, `score_assemble`, the HAZ-03 check | `ws7_scorer_stopped`, naming the scorer |
| The comparator (`ws7_compare.main`) | `ws7_compare_error`; its result is not read |
| The runners' loaders and request builders, in the precheck | `ws7_corpus_unreadable` or `ws7_precheck_failed`, before the claim |
| Anything else inside a run (the outermost handler) | `ws7_crashed`; the failure path runs |
| A duplicate's one publish call | Exit code 1; nothing else is published |
| Anything inside the check (its outermost handler) | Exit code 1; the check says on the topic that it failed |
| The command line's parser | The fixed usage line, exit code 2 |

The scorers and the comparator already treated a `SystemExit` as a failure; they and the two
outermost handlers did not catch a `KeyboardInterrupt`. The runner was the one place where a
raise was read as a return.

**That includes WS7's own exception types** (Codex P2, PR #9, second re-check). A `Stop` or a
`Duplicate` raised by code the job does not own is a crash of that code, like any other raise.
Only the job's own code may raise a `Stop` the run acts on.

| The call into code the job does not own | What any raise becomes, a `Stop` or `Duplicate` included |
|---|---|
| A runner (`_dispatch`) | `ws7_runner_crashed` |
| `score_batch` and `write_reports`; `score_assemble`'s `replay` and `write` | `ws7_scorer_stopped`, naming the scorer |
| The HAZ-03 check, which the job's own `write_haz03()` calls | `ScorerStopped` in `evals/ws7_summary.py`, which is not a `Stop`; the entrypoint makes it `ws7_scorer_stopped` |
| The comparator | `ws7_compare_error` |
| The precheck: the runner's parser, corpus loader, prompt loader and request builders | `ws7_corpus_unreadable` or `ws7_precheck_failed` |
| The match check (`ws7_manifest.own_values()`): the runner's parser, the pipeline's version, the runner's hash function | `ws7_runner_argv_invalid` |

- **A `Duplicate` is acted on only when the claim itself raises it.** `run()` catches it around
  the claim and nowhere else. Raised by anything else, it is `ws7_crashed`: a run that holds
  its claim can never end as a duplicate, with exit code 0.
- `write_haz03()` is the job's own code. A `Stop` it raises itself (`ws7_scores_invalid`, for
  example) is still the run's code.

**An exit code of 1 is accepted only when the batch explains it** (Codex P2, PR #9; the author's
ruling, 21 Nov 2026). Both runners return 1 for a failed generation, and also for other things
(a confirmation that was not given, `evals/run_cold_eval.py:663` and
`evals/run_pipeline_eval.py:331`). So a 1, from the pre-flight or the main batch, is accepted
only beside an index that records a failed generation or a gate failure. The rule is held in
both directions: a 0 beside an index that records either is refused too.

**An exit code is explained only by the fields that target's runner writes, each read by exact
type** (Codex P2, PR #9, re-check). `index_counts()` reads the index:

| Field | Written by | Read as |
|---|---|---|
| `error_code` | Both runners, for every generation (`evals/run_pipeline_eval.py:431`, `evals/run_cold_eval.py:568`) | null: no failure. A non-empty string: a failed generation. Anything else: not an index the runner wrote |
| `gate_ok` | The v1 runner only (`evals/run_cold_eval.py:567`) | For v1: `true` or null: no gate failure. Exactly `false`: a gate failure. Anything else: not an index the runner wrote. **For the pipeline it is not read at all** |
| `failed_step`, `gate_status`, any other field | | Never read: they explain nothing |

- **The pipeline has no safety-net gate.** So for the pipeline only a failed generation explains
  an exit code 1. A pipeline index with `gate_ok` false and an exit code 1 is refused.
- "By exact type" means `0`, `""`, `"false"`, `[]` and `{}` are not `false` or null. An index
  that holds one where a runner writes a boolean, a string or null is refused; it is never read
  as "failed" and never as "clean".
- The exit code itself is the integer 0 or 1. `True`, `"1"` and `1.0` are not.

### Settling

- `settle()` is called as soon as the main runner returns, before its return is judged and
  before anything is scored. A test reads the batch folder at the moment the settling write
  lands: the records are there and no score file is.
- A failure anywhere after admission settles first. It settles from the guard's report when
  there is a guard, and calls `mark_crashed()` when there is none.
- **It also calls `mark_crashed()` when the report cannot be settled from** (the author's
  ruling, 21 Nov 2026, widening change A). After catching anything, the job trusts only what
  it can check. If the ledger refuses the report (`ws7_report_incomplete`,
  `ws7_report_inconsistent`, `ws7_report_invalid`), or the report cannot be read:
  - the entry becomes **unresolved, as a crashed run, at its reservation**. It is never
    settled as metered from a total that may be short;
  - it **blocks every later scheduled run** (`ws7_blocked_unresolved`) until the author
    resolves it;
  - the ledger's refusal is still raised once the mark has landed, so the run fails by it; if
    the run had already failed, the code is recorded beside the first one (`also`).
- **A failed report read, or a caught interrupt, never un-happens** (Codex, PR #9, third
  re-check; the author's ruling, 21 Nov 2026). The run keeps one reason, a fixed code, the first
  time either happens, and never clears it:

  | Reason | Kept when |
  |---|---|
  | `ws7_report_unreadable` | A read of the guard's report failed, by anything but an interrupt. A later read that succeeds changes nothing |
  | `ws7_interrupted` | A Ctrl+C set the latch (below). Or a `KeyboardInterrupt` raised by other means was caught anywhere in the run: by a report read, by the wrapper during a call, by the log while a line was being written, or by any handler that goes on |

  - **A Ctrl+C never raises inside the job** (Codex, PR #9, fourth re-check; the author's
    ruling, 22 Nov 2026). A `KeyboardInterrupt` can arrive between any two lines, so a check
    for one can narrow the gap it falls into and cannot close it: there is always a line after
    the last check. At `9f9397f` a real Ctrl+C while the final `success` line was being logged
    was swallowed after the last check, and the run ended with `RunOk` = 1 and exit code 0. So
    the gap is closed at its source:
    - While `main()` runs, the process's SIGINT handler is the job's own, and **all it does is
      set a latch** (`_Latch`). It raises nothing. Nothing the job was writing is left
      half-written, and there is nothing for a handler to catch.
    - `main()` is the only place the handler is installed, and it **puts the previous handler
      back when it returns or raises**, so a caller in the same process (a test, pytest's own
      Ctrl+C) has its own handler again. It installs nothing off the main thread (Python
      handles signals in the main thread only), or when the handler in place was not set from
      Python.
    - The latch is folded into the reason at every read of it, so every check below sees it.
    - **SIGTERM is not handled.** It ends the process, and the open entry is abandoned later,
      as before.
    - A `KeyboardInterrupt` raised by other means is still caught and kept, as before.
  - **The reason is checked before every act the run cannot take back**, and it fails the run
    with its own code:

    | Check | The act it comes before |
    |---|---|
    | `_sound("claim")` | The claim |
    | `_sound("admission")` | Admission, which reserves |
    | `_sound(<batch label>)` | Each dispatch of a runner |
    | The stopping client's own look | Each call it sends |
    | `report()` in the settle | Settling from the guard (with a reason: `mark_crashed()`) |
    | `_sound("success")` | The run's metrics |
    | `_sound("alert")` | The breach message |
    | `_sound("success")`, again | The success record's PUT |
    | `_sound("run_ok")` | `RunOk` = 1, the last act |

    So there is no main batch after a pre-flight whose report read failed, and never `RunOk` = 1
    after a reason. **The check before `RunOk` = 1 is the last.** A reason found there comes
    after the success record was written, so the failed record goes beside it and the Thursday
    check reports a failed run. A Ctrl+C after that check finds a run that had finished: it sets
    the latch, nothing reads it again, and the run stays a success.
  - **A Ctrl+C is acted on at the next check; what has passed its check completes** (the
    author's ruling, 22 Nov 2026, on Codex's fifth re-check of PR #9). There is always a moment
    between a check and the act it guards, and more checks only move that moment. So the rule
    is about what has begun:
    - **Anything that has passed its check completes:** a dispatch, a call, a settlement (its
      reads and its PUT), a record, the breach message, `RunOk` = 1.
    - **A settlement that has begun is accurate.** The handler never raises, so a Ctrl+C
      cannot leave the guard's report half-written. Once the entry is settled as metered, later
      runs are admitted as normal.
    - **After a Ctrl+C, at most the one runner start or the one call already past its check
      goes out.** It is inside the reservation and it is counted.
    - **A latch found set at a check while the entry is still open** keeps the behaviour
      below: `mark_crashed()`.

    This is what a deferred signal does, and it is intended. Three cases Codex named, each a
    Ctrl+C that arrives just after a check: during the settlement's S3 read, the settlement
    completes as metered and the next run is admitted; during the dispatch's deadline check or
    the stopping client's look before a call, that runner or that one request still goes out;
    during the Thursday check's message build, its ordinary report goes out with exit code 0
    (a note for part 5b, below).
  - **A run that has caught a `KeyboardInterrupt` while its ledger entry was still open, or
    found the latch set at a check before its settlement began, never settles from the guard.
    It calls `mark_crashed()`**, so the entry is unresolved at its reservation (the author's
    ruling, 21 Nov 2026, narrowed on 22 Nov 2026). The same holds after a failed report read.
    The ruling's two cases:
    - **A `KeyboardInterrupt` raised by other means.** It arrives between any two
      instructions, so whatever was being written when it came may be half-written, and a
      report that looks complete is not proof that it is.
    - **A Ctrl+C found at a check before the settlement begins.** Nothing is half-written, but
      the run has failed before it settled, and it reports itself as crashed.

    It does not cover a Ctrl+C that arrives once the settlement has passed its check: that
    settlement completes, as above.
  - Every read the run DECIDES from goes through one method, which refuses once there is a
    reason: a runner's return, the settle, the summary. The failure path still reads the report
    afresh as a RECORD, to store it and to count from it: a failed run's report is what the
    author resolves the entry from. Nothing is decided from that read.
  - **A settle that did not return** (an interrupt in the middle of it, any other exception, a
    stop that is not the ledger's own) is followed by one `mark_crashed()`. Whether the settle's
    write landed is not known to the run; the ledger knows, and it refuses the mark if the entry
    is no longer open.
  - An interrupt caught, or a Ctrl+C that arrives, after the entry was settled leaves it
    settled. The run still fails if a check is still to come.
  - `ws7_interrupted` means a Ctrl+C found at a check or a `KeyboardInterrupt` that was caught.
    A `SystemExit` a runner raises is still `ws7_runner_crashed`, and that run still settles
    from the guard, as ruled before.
- Tests stop one call's accounting at each of five points (after the request is sent; after
  the reply and before usage is recorded; after usage and before the run's total; between the
  run's total and the batch's; after the totals and before the run is marked unmetered), by an
  interrupt and by an exception, in the pre-flight and in the main batch, for both targets.
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
- **The known score files are allowed as soon as scoring begins** (Codex P2, PR #9). A scorer
  that stops after `score_batch` wrote its reports does not make those reports unknown files:
  they are stored, and the run fails with the scorer's own code.
- **On the failure path one refused file does not stop the rest** (the author's ruling,
  21 Nov 2026). The run has already failed, and its records are evidence:
  - the guard's report is stored first, then the run's other documents, then every file on the
    list;
  - a file that is not on the list (or is not `.json` or `.md`, is not a plain file, cannot be
    read, or is larger than one object may be) is **never uploaded**;
  - the failed record counts them in `files_refused`.

  On the success path ruling 7 is unchanged: the run stops, nothing has been sent, and its
  failure path then stores what is allowed. Both paths send the guard's report first.
- Sending stops at the first object S3 will not take, on either path. If S3 is refusing writes,
  trying the rest would only delay the failed record and `RunOk` = 0.
- A `.md` report is stored as `text/markdown; charset=utf-8`, everything else as
  `application/json`. The store refuses any other content type.
- The list includes `scores/patient_version.json`, `scores/PATIENT_VERSION.md` and
  `scores/review_patient.md`, because `score_batch` writes them. They are stored with the rest
  of the scores. No PART C metric is published.
- **The retained prefix is `retained/<run_id>/`, outside `ws7/`** (the author's ruling 9). The
  task role's whole reach is `ws7/`, so the job can never overwrite an incident copy and the
  lifecycle rule does not touch it. The author copies by hand (slice 3's runbook). Nothing in
  WS7 writes it, and the store refuses every key under it.
- A run that fails before scoring has no manifest: its keys need the scores. Its failed record
  carries the commit and the digest.

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
- **If CloudWatch does not acknowledge `RunOk` = 1, the run fails like any other run that holds
  its claim**, with `ws7_runok_publish_failed` (Codex P2, PR #9, accepted by the author). The
  success record stands. The failed record is written beside it, so the Thursday check reports a
  failed run (the two-record rule below), and `RunOk` = 0 and `RunRefused` = 1 are tried. The
  job exits 1. Its counters are not sent again.
- D7 says each spend-guard trip is published. Under e2 a tripped run has no summary, so the
  three guard counters are published from the report on the failure path.
- Until `evals/score_assemble.py` is fixed, `StepFailures…` and `GenerationsFailed` can only be 0
  in a published pipeline run. The failed record carries the count.
- A name outside the set above, or a value that is not a whole number, sends nothing
  (`ws7_publish_invalid`).
- **Each metric is published at most once per run** (Codex P2, PR #9). The publisher keeps the
  names CloudWatch acknowledged, and refuses a list that holds one of them again
  (`ws7_publish_repeated`).
- **What was delivered before a later step failed stands.** A run publishes its counters and
  breach values, then sends its breach message, writes its success record and publishes
  `RunOk` = 1. If one of the later steps fails, the run fails, and:

  | Already delivered | What happens to it |
  |---|---|
  | The run's counters and `NoBaseline` = 0 | They stand. The failure path sends them again only if CloudWatch did not acknowledge them |
  | The breach values | They stand, so a breach alarm can fire for a run that then failed |
  | The breach message, if it was sent | It stands. No second message is sent |

  The failure path then sends `RunOk` = 0 and `RunRefused` = 1, and nothing else. So an alarm on
  a breach metric and the `RunOk` alarm can both report the same run; each is true.
- A call whose reply failed is not acknowledged. CloudWatch may or may not have taken it, and
  the job cannot tell which, so the failure path's second try can leave two samples of a counter
  for that one run. That case is a failed run, and the failed record names the code.

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

Every handled failure is a `Stop` with a fixed code. **The failure path never fails open**
(Codex P2, PR #9). It is five steps, in this order, and each is tried on its own: a step that
stops or raises is logged, and the next is still tried.

| Step | What it does |
|---|---|
| 1. The ledger | Settle from the guard's report; `mark_crashed()` when there is no guard, when the report cannot be settled from, or after a failed report read or a caught interrupt |
| 2. The records | Store what records exist: the guard's report first, then every allowed file. A file that may not be stored is left out and counted |
| 3. The failed record | Build it, the clock read included, and write it. Only for a run that holds its claim |
| 4. The markers | `RunRefused` = 1 and, from the claim's holder, `RunOk` = 0 (and `NoBaseline` = 1) |
| 5. The counters | The run's counts, from its summary or else from the guard's report |

- The markers and the counters are two calls. A counter that cannot be built or sent never
  holds back `RunOk` = 0.
- **Nothing a step raises ends the path early, whatever its type** (Codex P2, PR #9, re-check).
  `SystemExit` and `KeyboardInterrupt` derive from `BaseException`, not `Exception`, so a handler
  for `Exception` lets them through. Every handler on the failure path is for `BaseException`:
  the loop over the five steps, the settle, the store, and the log (`best_effort`). A log sink,
  the clock, a store call or a publish call that raises any of them costs that one step and no
  other.
- Anything raised inside a run that no other handler names is handled as a crash:
  `ws7_crashed`, and the failure path runs. That includes a `SystemExit` and a
  `KeyboardInterrupt` ("What a runner's return means", above).
- A test breaks one thing in each case: the log stream, the settling write, the first stored
  record, the clock, the building of the failed record, its write, and each publish. Every later
  step still reaches its server, with the ledger write first.

| Stage | Codes | Failed record | `RunOk` = 0 |
|---|---|---|---|
| A usage error | Exit code 2 | No | No |
| Before the claim | `ws7_config_invalid`, `ws7_topic_refused`, `ws7_store_bucket_refused`, `ws7_store_owner_refused`, `ws7_identity_missing`, the endpoint refusals, `ws7_corpus_unreadable`, `ws7_precheck_failed`, `ws7_preflight_scenario_unknown`, `ws7_scenarios_not_distinct`, the match check's codes, the claim's own refusals | No | No (`RunRefused` only) |
| A duplicate | `ws7_duplicate` | No | No |
| Admission | The ledger's codes | Yes | Yes |
| Dispatch | `ws7_deadline`, `ws7_runner_refused`, `ws7_runner_crashed`, `ws7_runner_stopped_early` | Yes | Yes |
| Calls | `ws7_call_failed`, `ws7_unmetered`, a refusal's own code | Yes | Yes |
| The pre-flight | `ws7_preflight_failed`, `ws7_preflight_files_unexpected`, `ws7_preflight_over_reservation` | Yes | Yes |
| Scoring and the summary | `ws7_generation_failed`, `ws7_scorer_stopped`, `build_summary()`'s codes | Yes | Yes |
| The comparator | `ws7_no_baseline`, `ws7_compare_error`, `ws7_compare_output_exists`, `ws7_part_c_ruled` | Yes | Yes |
| Storing and publishing | `ws7_evidence_unstorable`, `ws7_evidence_unavailable`, the manifest's codes, `ws7_publish_failed`, `ws7_alert_failed`, `ws7_success_record_unavailable`, `ws7_success_record_invalid` | Yes | Yes |
| Any other exception | `ws7_crashed` | If the claim is held | If the claim is held |
| A report read that failed, a Ctrl+C that set the latch, or an interrupt that was caught, at any time up to the last check | `ws7_report_unreadable`, `ws7_interrupted` | If the claim is held | If the claim is held |
| `RunOk` = 1 was not acknowledged | `ws7_runok_publish_failed` | Yes, beside the success record | Yes |

The failed record (`ws7-failed/1`) holds the run's ID, target, mode and scheduled time, the code,
the IDs and counts the stop carried, any further codes (`also`), what became of the ledger entry
(`ledger`), whether the records were stored (`evidence`), how many files were left out of the
store (`files_refused`), the commit, the digest and the time. Every field has a rule ("The
Thursday check", below), and the record is held to it before it is written.

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
5. **Nothing it reads is trusted as read** (Codex P2, PR #9). The claim, the failed record and
   the success record are each checked before they are used.

**A Ctrl+C is deferred to the latch in the check too** ("Settling", above). The check reads the
latch before `expire()`, which writes, and before its last act: its message when there is
something to report, and otherwise its exit code 0, which says the run succeeded. A set latch
fails the check as anything else does: `ws7_interrupted`, exit code 1, and it says on the topic
that the check failed. Once its message has been sent the check has finished, and a Ctrl+C
changes nothing. By the ruling of 22 Nov 2026, the same is true once the check has passed its
last read of the latch: a Ctrl+C while its message is being built or sent lets the ordinary
report go out, with exit code 0.

**One validator for each object, shared by its writer and the check** (Codex P2, PR #9,
re-check; the author's ruling, 21 Nov 2026), as the comparator's public checks are shared:

| Object | Validator | Run by the writer, before the PUT | Run by the check, after the GET |
|---|---|---|---|
| The claim | `ws7_ledger.claim_fault()` | `ws7_ledger.claim()` (`ws7_claim_invalid`) | `_occurrence()` |
| The success record | `ws7_entry.success_fault()` | `_succeed()` (`ws7_success_record_invalid`) | `_occurrence()` |
| The failed record | `ws7_entry.failed_fault()` | `_fail_record()` (`ws7_failed_record_invalid`) | `_failed_line()` |

So the job can never write a record its own check would reject. **Every field of all three is
checked**: its type, exactly (`true` is not an integer, `1` is not a string); its pattern or
range; and its agreement with the claim and the occurrence. **An unknown field, or a missing
one, is refused.**

| The claim (`ws7-claim/1`), 7 fields | Rule |
|---|---|
| `schema` | The string `ws7-claim/1` |
| `target`, `mode` | The strings of this occurrence: the check's target, and `scheduled` |
| `scheduled_time` | A UTC time, `YYYY-MM-DDTHH:MM:SSZ`, whose claim key is the key the object is at |
| `run_id` | The ID that target, mode and scheduled time give |
| `claim_id` | 32 hex characters |
| `claimed_at` | A UTC time, from the scheduled time to `max_start_delay_s` after it |

| Both records, the first 5 fields | Rule |
|---|---|
| `schema` | `ws7-success/1` or `ws7-failed/1` |
| `run_id`, `target`, `mode`, `scheduled_time` | Strings, equal to the claim's |

| The success record, 9 more fields | Rule |
|---|---|
| `status` | `no_breach` or `breach` |
| `breaches` | A whole number from 0 to 21 (one for each family and kind there is a metric for), above 0 exactly when the status is `breach` |
| `baseline_id` | An ID |
| `summary_sha256`, `comparison_sha256` | 64 hex characters |
| `evidence` | The run's own prefix, `ws7/runs/scheduled/<run_id>/` |
| `git_commit`, `image_digest` | 40 hex characters; `sha256:` and 64 hex characters |
| `finished_at` | A UTC time, not before the scheduled time |

| The failed record, 10 more fields | Rule |
|---|---|
| `code` | A `ws7_` code |
| `ids` | A list of IDs |
| `counts` | An object of fixed labels to integers |
| `also` | A list of `ws7_` codes |
| `ledger` | `not_admitted`, `failed`, a ledger state or kind, or a `ws7_` code |
| `evidence` | `none`, `not_finished`, `stored`, or a `ws7_` code |
| `files_refused` | A whole number |
| `git_commit`, `image_digest` | As above |
| `failed_at` | A UTC time, not before the scheduled time |

- An object that fails is reported as **invalid, with a fixed label**: the name of the first
  field that fails, or `not_json`, `not_an_object`, or `fields` (an unknown or a missing field).
- **Every JSON object the entrypoint reads is decoded strictly** (Codex P2, PR #9, second
  re-check), by one loader, `strict_json()`, as the comparator does: UTF-8 only, **no duplicate
  member** in any object, and no `NaN` or `Infinity`. Python's own loader keeps the last of two
  members with one name and says nothing, so a record with a wrong run ID and then the right one
  would pass its validator. The entrypoint has three reads, and all three use it: a batch's own
  index (`batch.json`; otherwise `ws7_runner_refused`), the comparator's result (otherwise
  `ws7_compare_error`), and each object the check reads (otherwise `not_json`).
- **Every number that loader returns is finite and within Python's integer conversion limit,
  however the JSON spells it** (Codex, PR #9, third re-check). `1e400` and `-1e400` are valid
  JSON, and Python reads them as plus and minus infinity, the values `Infinity` and `-Infinity`
  give by another spelling. An integer of 5,000 digits is valid JSON too, and Python will not
  print it. Each is refused like the others. The loader's bound on an integer is 4,300 digits,
  Python's own default, and it holds whatever the interpreter's limit is set to (with a lower
  limit Python's own conversion refuses first, and the loader treats that the same). Tests put each
  spelling in every field of each object the check reads, in a batch's index and in the
  comparator's result, with the interpreter's own limit turned off.
- **An invalid record never counts as a success**, and an invalid claim names no run, so no
  record is read on its word.
- **A record its validator refuses is not written.** For the success record the run then fails.
  For the failed record that step of the failure path stops, and the steps after it still run:
  `RunOk` = 0 is still published, and the check reports "started and did not finish".
- Every test that runs the entrypoint holds what the job wrote to these validators when it ends
  (`tests/ws7_world.py`, `check_written`).

| What it finds | The line it reports |
|---|---|
| No claim | `no claim: the run did not start, or stopped before its claim; see the task log` |
| A claim and a failed record | `failed run <run_id> code <code>`, with the IDs the stop carried. **If a success record is there too, it is still a failed run** |
| A run blocked by an unresolved entry | `failed run <run_id> code ws7_blocked_unresolved: blocked by unresolved <entry's run_id>` |
| A claim and neither record | `run <run_id> started and did not finish: no success record` |
| A claim and a success record only | Nothing |
| A claim that fails its check | `claim object invalid (<label>): it is not read as a scheduled claim` |
| A success record that fails its check, and no failed record | `run <run_id>: success record invalid (<label>); it is not counted as a success` |
| A failed record that fails its check | `failed run <run_id>: failed record invalid (<label>)`. It is still a failed run |
| Any unresolved entry, in any month | `unresolved entry <run_id> (<kind>, month <month>): it blocks every scheduled run until it is resolved` |
| The occurrence's month is waived | `month <YYYY-MM>: assurance waived`, every week of that month |

- **The "no claim" line does not say the run never started** (the author's change C). A stop
  before the claim, at the match check for example, also leaves no claim.
- One message is sent when there is anything to report. With nothing to report the check is
  silent and exits 0.
- A check that fails says so on the topic if it can still reach it, and exits 1.

### The log

One fixed line for each stage: labels, IDs and counts. A stop is printed as `Stop.line()`. No
traceback is printed.

**Building a diagnostic never raises and never carries text** (Codex P2, PR #9, second
re-check). Every value in a stop, a log line or a record is a fixed label, a bounded count, or
a string that matched an ID pattern. The job never formats an object it did not create.

- **An exception is logged by a fixed label for where it happened, never by its class name or
  its message.** A class from outside the job can be named anything, and looking up its name
  can itself raise. The line is `stage=crashed during=<where>`:

  | `during=` | Where |
  |---|---|
  | `run` | Anywhere in a scheduled run that no other handler names |
  | `settle`, `store`, `failed_record`, `markers`, `counters` | That step of the failure path |
  | `duplicate` | A duplicate's one publish call |
  | `check`, `alert` | The Thursday check, and its own failure report |

- **A runner's return code is used only once it is an integer from 0 to 255.** Anything else
  (None, a string, `True`, a negative number, a number of 5,001 digits, which Python will not
  print) is the fixed label `exit_code_not_a_code`, and is never copied into a stop, a line or
  a record. The comparator's code is used only once it is 0, 1 or 2. The scorers' return values
  are passed back to the scorers and never used in a diagnostic.
- **A count in the failed record is a whole number within ±(2^63 - 1).** A record with a count
  beyond that is refused by its validator.
- **Every line is built inside the best-effort boundary.** `best_effort` is given the function
  that builds the line, and calls it inside its `try`: a line that cannot be built costs that
  one line.
- **Only the job's own stops are rendered or acted on** (Codex, PR #9, third re-check). A `Stop`
  is an ordinary Python class: a subclass can give itself a `line()` that raises, or that returns
  any text. So the job never renders what it caught and never calls a method on it. Every stop
  it catches goes through `own_stop()`, which returns a new, exact `Stop` or nothing:

  | Rule | |
  |---|---|
  | Its class | Exactly one of the job's own three: `ws7_store.Stop`, `ws7_store.StoreError` (what the store raises) or `ws7_ledger.Duplicate` (what the claim raises). **A subclass of any of them is not one** |
  | Its code | One of the job's fixed codes: every module's own list (113 codes; a test holds the list to the modules) |
  | Its IDs | A tuple of at most 256 strings, each matching the ID pattern |
  | Its counts | Fixed labels to whole numbers within ±(2^63 - 1) |

  - The fields are read from the object's own dictionary, so no property of it is used either.
  - The line is built by `stop_line()` in `evals/ws7_entry.py`, from those checked fields.
  - **Anything else is a crash, named by the fixed label of where it was caught**: the run fails
    with `ws7_crashed` and the label `not_a_job_stop` (or that place's own fixed code, where it
    has one: the comparator's result, the store). Its code is never acted on, so a stop that
    calls itself "no baseline" publishes no `NoBaseline`, and one that calls itself a duplicate
    never ends a run with exit code 0.
  - The same rule is applied wherever a stop is caught: the run, the claim, the settle, the
    store, the success record, `RunOk` = 1, each step of the failure path, a duplicate, and the
    Thursday check, whose failure report puts the code in an SNS message.

**Logging is best effort everywhere and never raises** (Codex P2, PR #9). `best_effort` catches
`BaseException`, so that holds whatever the stream raises. A log stream that
fails (a closed pipe, a full disk) changes nothing a run, a duplicate or the check does: with a
stream that raises on every line, a good run still ends with `RunOk` = 1 and a failed run still
settles, writes its failed record and publishes `RunOk` = 0.

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
- It did not pass `ExpectedBucketOwner`. Slice 3 added it ("Slice 3: the bucket's owner").
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

## Part 6: the image

Two files change, `Dockerfile` and `.dockerignore`, and one test file is new,
`tests/test_image_files.py`. No file in `evals/` or `src/` is changed. **No image has been pushed
to a registry, and nothing has been deployed or run.** The image is built and checked by hand, on
the author's machine only (the checks below); the pull request records what they printed.

### The commit

- The `Dockerfile` declares `ARG WS7_GIT_COMMIT` and sets the environment variable
  `WS7_GIT_COMMIT` from it (ADR-011 D11).
- **There is no default.** An image built without `--build-arg WS7_GIT_COMMIT=…` has the variable
  set and empty, and the job refuses to run: `ws7_identity_missing`. It never carries a made-up
  commit. `tests/test_ws7_manifest.py::test_a_run_with_the_commit_or_the_digest_missing_or_malformed_is_refused`
  has that case, an empty commit beside a valid digest, with a valid pair as its control.
- **The argument and the variable come after every `RUN` and `COPY`.** A changed build argument is
  a cache miss for every `RUN` after it, and a changed `ENV` for every layer after it. Placed
  last, a new commit reinstalls no pin and recopies no folder.
- **The commit is the builder's statement**, as the digest is the deployer's. `COPY` takes
  whatever is in the folders it names, tracked by git or not, so the image is built from a clean
  checkout of the commit it names (checks 1 and 1b below; slice 3's runbook).
  - **The rule: no image is pushed unless check 5 has passed on that same image, identified by
    its image ID, after its last build** (the author's ruling, 23 Nov 2026, after Codex's
    re-check of PR #10). Check 5 looks inside the built image and does not depend on git. It is
    the control for ADR-011's condition: neither protected folder is in an image "before any
    image is pushed to a registry".
  - **Checks 0 to 1b and the build guards catch mistakes early. They are not the control.** They
    read git's view of the working folder, and local git configuration can change that view in
    more ways than a list can cover ("Known limits", below).
  - What they catch. The files in the working folder that decide what the builder reads are the
    `Dockerfile`, `.dockerignore`, `requirements-eval.txt`, the four copied folders, and
    `Dockerfile.dockerignore`. The last is not tracked and must not exist: when it does, the
    builder reads it **instead of** `.dockerignore` (Codex P1, PR #10).
    - Check 1 names all of them, and lists untracked files whatever `status.showUntrackedFiles`
      says (Codex P3, re-check of PR #10). An untracked `Dockerfile.dockerignore` prints `??`.
    - Check 1b lists every file marked `assume-unchanged` or `skip-worktree`. Git does not look
      at such a file, so check 1 cannot show an edit to it, `.dockerignore` included (the
      author's ruling, 23 Nov 2026). If it prints a line, the build must not go ahead.
    - The build guards (checks 2 and 7) build nothing while a `Dockerfile.dockerignore` exists,
      and nothing unless the raw bytes of the `Dockerfile` and of `.dockerignore` are the
      commit's. The bytes are hashed with no filter and no encoding applied, because a
      `working-tree-encoding` attribute or a filter can make a changed file compare clean
      (Codex P1, re-check of PR #10).
- **The task definition must not set `WS7_GIT_COMMIT`** (slice 3). An override there would let a
  task claim a commit its image was not built from.

### The ignore file

- `.dockerignore` now excludes `evals/calibration`, beside `evals/runs` (ADR-011, "What has to
  change in implementation pull requests").
- Nothing the job runs reads either folder. `tests/` and `evals/` were searched at `3d5214e` for
  the two path strings; the folders themselves were not opened or listed.
  - `evals/calibration` is named by the calibration and blind-sheet tools, as their default
    paths (`calibrate_verifier.py`, `make_blind_sheets.py`, `make_compose_sheets.py`,
    `make_patient_sheets.py`), and by one test, which skips when the folder is absent
    (`tests/test_make_patient_sheets.py:48`). No module the job loads imports any of those tools.
  - `evals/runs` is the runners' default output root (`evals/run_cold_eval.py:105`). The job
    never uses it: the entrypoint always hands the runners a temporary `runs_dir`.
- The file has no `!` line, and there is no `Dockerfile.dockerignore`, which the builder would
  read instead of `.dockerignore`. The test pins both.
- **A build does not run the test, so the build procedure has guards of its own, and one
  control** (Codex P1, PR #10, and its re-check).
  - The guards: check 1 names `Dockerfile.dockerignore`, so an untracked one prints `??`; check
    1b shows that no file, `.dockerignore` included, is hidden from check 1 by a git flag; and
    neither build runs while a `Dockerfile.dockerignore` exists, or unless the raw bytes of
    `.dockerignore` are the commit's.
  - The control: check 5 looks for both folders inside the built image, and no image is pushed
    unless it has passed on that image ("The commit", above).

### The default command

**The default command is the nine WS7 test files (the author's ruling, 23 Nov 2026).** The full
suite runs in CI and on the author's machine, not in the image.

- Why: `pytest tests/` cannot pass in the image. This was found by reading the tests at `3d5214e`,
  before part 6 changed anything; no image was built to show it.

  | What stops it | Where |
  |---|---|
  | A collection error, which ends pytest before any test runs: the module loads `infra/trace_query.py` at import, and `infra/` is not in the image | `tests/test_trace_query.py:25-27` |
  | Tests that read `infra/template.yaml` | `tests/test_template_trace.py`; `tests/test_worker.py:452` and `:462` |
  | Tests that read a saved run and have no skip | `tests/test_pipeline_orchestrate.py:614`; `tests/test_score_meds.py:157` (8 cases); `tests/test_score_safety_net.py:117` (4 cases) |

  The last two were already there when the image was made (`8c78790`, 26 Oct 2026), and nothing
  in CI builds the image. Excluding `evals/calibration` is not among the causes: it turns one
  test into a skip.
- The nine files are the job's own tests: the real runners, scorers and comparator against local
  fakes on `127.0.0.1`. They and their helpers read nothing from `docs/`, `infra/` or
  `evals/runs`, and none has a skip marker, so in the image every test is expected to pass and
  none to be skipped.
- The image that is tested is the image that is pushed, at the same digest. ADR-011's
  alternative, a build target of its own for the job, was not taken: it would keep a second
  stage whose full-suite command still fails, or push an image that cannot test itself.
- **It stays a `CMD`, not an `ENTRYPOINT`.** The ECS task's `command` replaces a `CMD` whole, and
  a bare `docker run` stays offline and harmless.
- `tests/test_image_files.py` fails if the command's list and `tests/test_ws7_*.py` on disk
  differ either way, so a tenth WS7 test file cannot be left out of the image's command.
- **A follow-up, not in this part:** skip guards in those six test files, so that the full suite
  could run in the image with no mounts.

### The header

The `Dockerfile` header said "No AWS calls". It now says:

- the default command runs the nine WS7 test files (the full suite runs in CI), offline;
- the ECS task (slice 3) overrides the command to run `evals/ws7_entry.py`, which calls Bedrock,
  S3, SSM, CloudWatch and SNS with the task role;
- no credentials are ever baked in.

### Unchanged

- `python:3.13.13-slim-trixie`, the non-root `app` user (uid 10001), the five named `COPY`s and
  their read-only modes.
- The architecture is set by the build command (`--platform linux/arm64`), not by the
  `Dockerfile`.
- `PYTHONHASHSEED=0` is not in the image. The task definition sets it (ADR-011; slice 3).

### Building and checking it

No command here pushes anything or needs AWS, and every `docker run` is `--network none`.

- Run it from the repository's root: every path in it is relative to that folder.
- **The rule: no image is pushed unless check 5 has passed on that same image, identified by its
  image ID, after its last build.** Check 5 prints the image ID it ran against. A rebuild makes
  another image, and check 5 is run again on it. Nothing in part 6 pushes. Slice 3's push is a
  guarded step that runs check 5 against the image ID it pushes, and goes by its status.
- **What check 5 must see** (Codex P1, second re-check of PR #10; the author's ruling, 25 Nov
  2026): **a protected folder in the image under any name the Mac's file system treats as that
  folder's name.**
  - The Mac's volume is case-insensitive, so `evals/runs` can be a folder stored as
    `evals/Runs`. `.dockerignore` matches by case, so `COPY evals/` would carry `Runs` into
    the image, and a probe of the lower-case path alone would print `absent`.
  - So check 5 lists `/app` and `/app/evals` inside the image and compares every name, whatever
    its case, with `docs` and `infra` (in `/app`) and `runs` and `calibration` (in
    `/app/evals`). It prints `PRESENT` and the name as found. A symlink counts.
  - **A name that is not plain ASCII is a `STOP`.** Case is not the whole of it: the volume
    also treats `runſ`, with a long s (U+017F), as `runs` (tried on the author's machine,
    25 Nov 2026). Check 5 does not try to fold such names; it refuses any. No tracked name at
    either level is anything but plain ASCII.
  - A folder that cannot be listed, or lists nothing, is a `STOP`. So is an image root that
    does not hold exactly the five names.
- **Check 5 has a status, and the status is the result** (Codex P3, the same re-check; the
  wording corrected in slice 3 after Codex's fourth re-check of PR #10). `check5` returns zero
  only when the in-image script found neither protected folder under any spelling, counted four
  folders as absent and counted exactly the five names. Anything else is not zero: a protected
  name, a `STOP`, or a `docker run` that fails. It is a function that returns: it never exits
  the shell. The script it runs in the image is POSIX `sh`.
  - **The status reflects what the script found in the image's file system. It does not show
    that any line was printed.** With stdout closed, a clean image still returns 0 and prints
    nothing; a protected name still returns 1.
  - **A run that fails can leave partial output.** `check5` prints the `image` line before
    `docker run` starts, and a script that stops part-way has already printed the lines before
    it stopped. So lines on the screen are for the reader; a caller goes by the status alone, as
    slice 3's push script does (`infra/eval/push-image.sh`).
- **No command in check 5 can fail and turn a match into a miss** (Codex P1, third re-check of
  PR #10; the author's ruling, 25 Nov 2026). The standing rule: a check never tests a command
  substitution's output without first checking that the command succeeded.
  - **Every decision in the in-image script is made by the shell itself.** A name is matched
    with a `case` pattern (`[Rr][Uu][Nn][Ss]` and so on), and a name that is not plain ASCII is
    found with a bracket pattern for any byte outside space to tilde, under `LC_ALL=C`. The
    script calls no `tr`, and has no command substitution at all.
  - **How `dash` reads that range.** It compares each byte's value with the two ends of the
    range, with no locale collation. On the Mac's `dash`, every byte value from 1 to 255 was
    tried: the 95 from space to tilde are plain, the other 160 are not. The long s is the bytes
    C5 BF in UTF-8, both outside the range. The image's `dash` has not been run by Claude, so
    **the script tests itself first**: a mixed-case name must match, a longer one must not, a
    plain name must be plain and `runſ` must not be. If any of the four fails, it prints `STOP`
    and stops.
  - **A pass is counted, not assumed.** The script ends with status 0 only if nothing bad was
    found **and** it counted four folders as absent **and** it counted exactly the five names.
    No count depends on its line having been written (the script does not stop when a line
    cannot be written), so the status does not show that any line was printed.
  - Every command, and what happens when it fails:

    | Command | Where | Used for | If it fails |
    |---|---|---|---|
    | `docker image inspect` | `check5` | The image ID | Its status is checked first, and an empty ID is a failure too: `STOP`, status 1 |
    | `docker run` | `check5` | Runs the script | Its status is `check5`'s status: not 0. The `image` line was already printed, and the script may have printed some of its lines before it failed: partial output is not a pass |
    | `sh` in the image | `docker run` | Runs the script | `docker run` fails, as above. A syntax error ends the script with a status that is not 0 |
    | `ls -A` (the one outside command in the script) | The script | Its status only: can the folder be listed? Its output is discarded | `STOP: cannot list …`, status 1. If it wrongly succeeds on a folder that cannot be read, the glob finds nothing: `STOP: evals/ lists no entries`, or for the root the five names are not counted |
    | The globs (`"$dir"/*` and the two dot-file forms) | The script | The names | A folder that gives no names is a `STOP`, as above |
    | `case` | The script | Every match, and the plain-ASCII test | Built in. The self-test stops the script if patterns do not work as needed |
    | `[`, `echo`, `printf`, `$(( ))`, `${p##*/}`, `export`, `exit`, `return`, `continue` | The script | Tests, lines, counting | Built in to `dash`. `bad=1` is set before the line is printed, so a line that could not be written does not change the status. A missed count leaves `absent` or `count` short, which is not a pass |
    | `echo`, `local`, `[` | `check5` | Lines | Built in to `zsh` |
- **Checks 0 to 1b and the build guards catch mistakes early. They are not the control**
  ("Known limits", below).
- **Check 1 counts only if check 1b prints nothing.** A file that 1b prints is one git does not
  look at, so check 1 cannot show an edit to it. Do not build: clear the flag, and start again
  from check 1.
- **The build guards.** `ok_to_build` is defined once, at the top of the block, and checks 2 and
  7 build only if it passes. Otherwise it prints one `STOP` line and nothing is built. It stops:
  - when `$C` is not a 40-character commit;
  - when a `Dockerfile.dockerignore` exists (a dangling symlink counts);
  - when git cannot find the `Dockerfile` or `.dockerignore` in that commit, or cannot hash the
    file. **Each git command is checked for success before its output is compared**: two failed
    commands print two empty strings, which are equal;
  - when the raw bytes of either file are not that commit's.
- **If a build prints `STOP`, stop there.** Put the cause right, and start again from check 1. The
  later checks would otherwise run against an older image, or none.

```zsh
C=$(git rev-parse HEAD); IMG=discharge-eval:ws7-p6

# The build guards, used by checks 2 and 7. Prints one STOP line and returns 1 unless:
# $C is a 40-character commit; there is no Dockerfile.dockerignore; and the raw bytes of
# the Dockerfile and of .dockerignore are that commit's (no filter and no encoding
# applied). Each git command is checked before its output is compared.
ok_to_build() {
  local f want have hex='^[0-9a-f]{40}$'
  if ! [[ "$C" =~ $hex ]]; then
    echo "STOP: \$C is not a 40-character commit. Nothing was built."; return 1
  fi
  if [ -e Dockerfile.dockerignore ] || [ -L Dockerfile.dockerignore ]; then
    echo "STOP: Dockerfile.dockerignore exists. The builder would read it instead of .dockerignore. Nothing was built."; return 1
  fi
  for f in Dockerfile .dockerignore; do
    if ! want=$(git rev-parse --verify --quiet "$C:$f" 2>/dev/null) || [ -z "$want" ]; then
      echo "STOP: git cannot find $f in commit $C. Nothing was built."; return 1
    fi
    if ! have=$(git hash-object --no-filters -- "$f" 2>/dev/null) || [ -z "$have" ]; then
      echo "STOP: git cannot hash the file $f. Nothing was built."; return 1
    fi
    if [ "$want" != "$have" ]; then
      echo "STOP: the bytes of $f are not the ones in commit $C. Nothing was built."; return 1
    fi
  done
  return 0
}

# 0. No tracked file is dropped by a credential pattern in .dockerignore. Prints nothing.
git ls-files -- src prompts evals tests ':(exclude)evals/runs' ':(exclude)evals/calibration' \
  | grep -E 'secret|credential|\.key$|\.pem$|\.p12$|/\.env|samconfig|/\.aws/'

# 1. A clean tree. Prints nothing, or only "!!" lines for files under __pycache__ or
#    for .DS_Store, which .dockerignore drops. A Dockerfile.dockerignore prints "??"
#    (or "!!"): remove it before going on. Untracked files are listed whatever
#    status.showUntrackedFiles says.
git status --porcelain --ignored --untracked-files=all -- Dockerfile .dockerignore \
  Dockerfile.dockerignore requirements-eval.txt src prompts evals tests \
  ':(exclude)evals/runs' ':(exclude)evals/calibration'

# 1b. No tracked file is hidden from check 1. Prints nothing.
#     A printed line is a file git has been told not to look at: a lower-case tag
#     ("h") is assume-unchanged, "S" is skip-worktree, "s" is both. Check 1 cannot
#     show an edit to such a file, so THE BUILD MUST NOT GO AHEAD. Clear the flag,
#     each kind with its own command (one command does not clear both):
#       git update-index --no-assume-unchanged <file>
#       git update-index --no-skip-worktree <file>
#     then start again from check 1.
git ls-files -v -- Dockerfile .dockerignore Dockerfile.dockerignore \
  requirements-eval.txt src prompts evals tests \
  ':(exclude)evals/runs' ':(exclude)evals/calibration' | grep -E '^[[:lower:]S] '

# 2. The build, only if the guards pass (otherwise one STOP line).
#    Ends "naming to docker.io/library/discharge-eval:ws7-p6". Nothing is pushed.
if ok_to_build; then
  docker build --platform linux/arm64 --build-arg WS7_GIT_COMMIT=$C -t $IMG .
fi

# 3. The default command, offline. Every test passes, none is skipped, "exit 0".
docker run --rm --network none $IMG; echo "exit $?"

# 4. The commit in the image is the commit built from. Prints "same".
#    The command is checked before its output is compared: a failed run, or an empty
#    value, is a STOP, never a match.
if ! GOT=$(docker run --rm --network none $IMG printenv WS7_GIT_COMMIT) || [ -z "$GOT" ]; then
  echo "STOP: the image's WS7_GIT_COMMIT could not be read."
elif [ "$GOT" = "$C" ]; then
  echo same
else
  echo "DIFFERENT: the image says $GOT"
fi

# 5. THE CONTROL: what the image holds. No image is pushed unless this has passed on
#    that same image, after its last build. THE STATUS IS THE RESULT: "check 5 status 0".
#    A pass also prints the "image" line with the image ID it ran against, four "absent"
#    lines and the five names, one a line (evals, prompts, requirements-eval.txt, src,
#    tests), but lines alone are not a pass: a run that fails can leave some behind.
#    A "PRESENT" line or a "STOP" line is a failure, and the status is then not 0.
#    Inside the image it lists /app and /app/evals and compares every name, whatever
#    its case, with docs and infra (in /app) and runs and calibration (in /app/evals).
#    A symlink counts. A name that is not plain ASCII is a STOP: this Mac's volume
#    treats some such names as the same folder (a long s for the "s" in "runs").
#    The script is POSIX sh: it runs under the image's /bin/sh. EVERY DECISION IN IT
#    IS MADE BY THE SHELL ITSELF (case patterns, [ ], globs), so no outside tool can
#    fail and turn a match into a miss. The one outside command is ls, asked only
#    whether a folder can be listed; its failure is a STOP.
CHECK5_SCRIPT='
LC_ALL=C; export LC_ALL
root=$1; bad=0; absent=0

plain() {   # is $1 made only of bytes from space to tilde?
  case "$1" in *[!\ -~]*) return 1 ;; esac
  return 0
}
selftest() {   # the controls: this shell matches names as the checks below need
  case "RuNs" in [Rr][Uu][Nn][Ss]) ;; *) return 1 ;; esac
  case "runsx" in [Rr][Uu][Nn][Ss]) return 1 ;; esac
  if ! plain "requirements-eval.txt ~"; then return 1; fi
  if plain "runſ"; then return 1; fi
  return 0
}
if ! selftest; then
  echo "STOP: this shell does not match names as check 5 needs"; exit 1
fi

dir=$root/evals
if ! ls -A "$dir" >/dev/null 2>&1; then
  bad=1; echo "STOP: cannot list evals/"
else
  seen=0; odd=0; runs=0; calibration=0
  for p in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
    if [ ! -e "$p" ] && [ ! -L "$p" ]; then continue; fi
    seen=1; name=${p##*/}
    if ! plain "$name"; then bad=1; odd=1; continue; fi
    case "$name" in
      [Rr][Uu][Nn][Ss])
        bad=1; runs=1; echo "PRESENT evals/$name" ;;
      [Cc][Aa][Ll][Ii][Bb][Rr][Aa][Tt][Ii][Oo][Nn])
        bad=1; calibration=1; echo "PRESENT evals/$name" ;;
    esac
  done
  if [ "$runs" = 0 ]; then absent=$((absent + 1)); echo "absent evals/runs"; fi
  if [ "$calibration" = 0 ]; then absent=$((absent + 1)); echo "absent evals/calibration"; fi
  if [ "$odd" != 0 ]; then bad=1; echo "STOP: a name in evals/ is not plain ASCII"; fi
  if [ "$seen" != 1 ]; then bad=1; echo "STOP: evals/ lists no entries"; fi
fi

dir=$root
if ! ls -A "$dir" >/dev/null 2>&1; then
  bad=1; echo "STOP: cannot list the image root"
else
  odd=0; docs=0; infra=0
  for p in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
    if [ ! -e "$p" ] && [ ! -L "$p" ]; then continue; fi
    name=${p##*/}
    if ! plain "$name"; then bad=1; odd=1; continue; fi
    case "$name" in
      [Dd][Oo][Cc][Ss])
        bad=1; docs=1; echo "PRESENT $name" ;;
      [Ii][Nn][Ff][Rr][Aa])
        bad=1; infra=1; echo "PRESENT $name" ;;
    esac
  done
  if [ "$docs" = 0 ]; then absent=$((absent + 1)); echo "absent docs"; fi
  if [ "$infra" = 0 ]; then absent=$((absent + 1)); echo "absent infra"; fi
  if [ "$odd" != 0 ]; then bad=1; echo "STOP: a name in the image root is not plain ASCII"; fi
fi

count=0; other=0
for p in "$root"/* "$root"/.[!.]* "$root"/..?*; do
  if [ ! -e "$p" ] && [ ! -L "$p" ]; then continue; fi
  name=${p##*/}; printf "%s\n" "$name"
  case "$name" in
    evals|prompts|requirements-eval.txt|src|tests) count=$((count + 1)) ;;
    *) other=1 ;;
  esac
done
if [ "$count" != 5 ] || [ "$other" != 0 ]; then
  bad=1; echo "STOP: the image root does not hold exactly the five names"
fi

# A pass is counted, not assumed: nothing bad, four absent lines, the five names.
if [ "$bad" = 0 ] && [ "$absent" = 4 ] && [ "$count" = 5 ] && [ "$other" = 0 ]; then
  exit 0
fi
exit 1
'
check5() {   # check5 <image>: returns 0 only on a pass. It returns; it never exits the shell.
  local id
  if ! id=$(docker image inspect --format '{{.Id}}' "$1" 2>/dev/null) || [ -z "$id" ]; then
    echo "STOP: there is no image $1 to check."; return 1
  fi
  echo "image $id"
  docker run --rm --network none "$id" sh -c "$CHECK5_SCRIPT" check5 /app
}
check5 $IMG; echo "check 5 status $?"

# 6. No arguments: the one fixed usage line ("Running it", part 5a), then "exit 2".
docker run --rm --network none $IMG python evals/ws7_entry.py; echo "exit $?"

# 7. Built without the argument, only if the guards pass (otherwise one STOP line).
#    Every RUN and COPY step shows CACHED. Prints ''
if ok_to_build; then
  docker build --platform linux/arm64 -t discharge-eval:ws7-noarg .
fi
docker run --rm --network none discharge-eval:ws7-noarg \
  python -c 'import os; print(repr(os.environ.get("WS7_GIT_COMMIT")))'

# 8. identity() inside each image, with an invented digest.
#    The first prints $C. The second prints: ws7 stop code=ws7_identity_missing
D=sha256:$(printf '0%.0s' {1..64})
for I in $IMG discharge-eval:ws7-noarg; do docker run --rm --network none -e WS7_IMAGE_DIGEST=$D $I python -c '
import os, sys; sys.path.insert(0, "evals"); import ws7_manifest as wm
try: print(wm.identity(os.environ).git_commit)
except wm.Stop as e: print(e.line())'; done

# 9. The architecture and the user. Prints: arm64 app
docker image inspect --format '{{.Architecture}} {{.Config.User}}' $IMG

# 10. The image's environment, names only. WS7_GIT_COMMIT is there; no AWS_* name,
#     no WS7_IMAGE_DIGEST and no PYTHONHASHSEED. The other names are the four this
#     Dockerfile sets (PYTHON..., PIP_...) and the base image's own.
docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' $IMG | cut -d= -f1
```

### Known limits

From Codex's review and re-check of PR #10, and the author's ruling of 23 Nov 2026.

- **The checks before the build rely on git's view of the working folder, and local git
  configuration can change that view**: an encoding attribute, a filter, a flag on a file, a
  setting that hides untracked files. Each case found so far has a guard above. A list of guards
  cannot be shown to be complete, so none of them is the control.
- **For the protected folders, check 5 at the push is the control.** It reads the image, not
  git: no image is pushed unless check 5 has passed on that same image, identified by its image
  ID, after its last build.
- **For the commit label on every copied file, part 6 has no control.** Checks 1 and 1b are what
  the author's machine has. Slice 3's runbook builds from a clean export of the commit, with the
  machine's git configuration isolated.
- **Where check 5's class ends.** Check 5 controls the protected folders reaching the image
  under any spelling of their own path: `evals/runs`, `evals/calibration`, `docs`, `infra`. A
  copy of their content under another path is a different class. That is the data rule
  (CLAUDE.md, section g), and check 1 shows an untracked file early.
- Check 5 has been written and read, and its in-image script tried on folders outside any
  image, under the Mac's `dash` and its own `ls`. What a real image prints, under the image's
  own shell, is seen only when the author runs it. The script's self-test is what stops it
  there if that shell reads a pattern differently.

### What part 6 does not do

- It pushes no image and creates no registry (slice 3).
- It does not change `CLAUDE.md` sections (b) and (c), `docs/COST.md` or any template (slice 3).
- It did not correct the docstring of `evals/ws7_entry.py` (lines 8 to 11), which listed the
  commit among the task definition's variables. No `evals/ws7_*.py` file was to change in this
  part. Slice 3 corrected it, in the sentence it had to change anyway (the author's ruling,
  26 Nov 2026).

### What the test cannot prove

`tests/test_image_files.py` reads two text files. It cannot show what a built image holds, that
the build is for arm64, or that ECS replaces the command. The checks above show the first two on
one machine. Slice 3's set-up checks show all three where the job runs.

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

- **A test for the `call` count of "a call still `sent`"** (found in slice 3, 26 Nov 2026, when
  the earlier mutations were run again; carried by the author's ruling of that day, with no code
  change in slice 3). One of PR #9's mutations, "a row still `sent` is not the run's first
  failure", survives when it is written as `first_failure()` skipping such a row
  (`evals/ws7_entry.py`): no test fails, not the named one
  (`test_accounting_that_did_not_finish_leaves_the_entry_unresolved_at_its_reservation`) and not
  the whole of `tests/test_ws7_entry.py`.
  - Why: `_judge()` calls `ws7_ledger.report_fault()` straight after `first_failure()`, and for a
    row still `sent` that gives the same code, `ws7_report_incomplete`. So the run still fails
    with the right code. What is lost is the stop's `call` count, which names the call, and no
    test holds it. If a later row has a failure of its own, that row's code would name the run
    instead.
  - Written the other way (the whole `sent` branch removed, so the row falls through), the
    mutation is caught. PR #9's body reports it as caught, so that was most likely the reading
    used then.
  - **The behaviour is right; a test is missing.** 5b adds one that holds the `call` count of
    that stop, and one with a later failing row, so that the first reading is caught too.
- **The yes** (the author's ruling, 20 Nov 2026; the details are for 5b's plan). A manual mode
  needs, in the invocation itself, the run ID the job derives and the reservation it computes.
  **A manual command without its yes stops with `ws7_manual_not_approved` and prints the run ID
  and the reservation it derived**, so the author copies them and does not work them out.
- **Manual runs publish no CloudWatch metric.**
- 5b uses 5a's run: the precheck, the match check, the guard's deadline, the wrapper that stops
  every call once the run has failed, and settling before scoring.
- **Reject an existing batch path before it is registered as evidence** (Codex P3, PR #9,
  carried by the author, 21 Nov 2026). `_dispatch()` records a batch folder as the run's own
  (`evals/ws7_entry.py`, `self.batches[label] = …`) before the runner has refused a folder that
  is already there. The runner does refuse it (`ws7_runner_refused`, exit code 2), but the
  failure path then stores that folder's allowed files as this run's records, though this run
  wrote none of them. The entrypoint must refuse a batch path that exists before it registers
  it, with a fixed code, in the scheduled and the manual modes.
- **A closed stdout pipe in a real subprocess can change the exit code** (Codex P3, PR #9
  re-check, carried by the author, 21 Nov 2026; `evals/ws7_entry.py`, `_say`). `best_effort`
  stops a log line that fails from raising inside the job. It does not cover what Python does
  as the process ends: with stdout a closed pipe, the interpreter's own flush at exit can fail,
  and the process then exits with a code that is not the job's. 5b decides how the real
  process ends (for example, flushing and closing stdout itself before `sys.exit`, and
  treating a failure there as best effort), and tests it in a child process whose stdout is a
  closed pipe: the exit code is the job's, for a successful run, a failed run and a usage error.
- **The guide's wording on the pipeline's gate** (Codex P3, PR #9 second re-check, carried by
  the author, 21 Nov 2026). This guide ("What a runner's return means"), ADR-011 D7 and the
  docstring of `evals/ws7_entry.py` say "the pipeline has no safety-net gate". What is meant is
  narrower, and the sentences should say it: **the pipeline's runner records no gate result in
  its index** (`batch.json`), so a gate cannot explain a pipeline runner's exit code. The
  pipeline's safety-netting is still checked: at assembly (`score_assemble`, the gate status in
  ADR-011 D9's Assembly family) and by `score_batch`, and the comparator judges both.
- **ADR-011 D9 and D10 still say a failed run publishes no breach metric and sends no message**,
  while also describing a run that delivered them and then failed (Codex P3, PR #9 re-check,
  carried by the author, 21 Nov 2026). Qualify both: a run that fails **before its comparison**
  publishes no breach value and sends no message; a run that fails after it leaves what
  CloudWatch **acknowledged** and what SNS accepted. Use "acknowledged" where the text now says
  "delivered" or "published".
- **The exact-class check should compare by identity** (Codex P3, PR #9 fourth re-check, carried
  by the author, 22 Nov 2026; `evals/ws7_entry.py`, `own_stop`). `type(caught) not in
  OWN_STOP_TYPES` is a tuple membership test, which compares with `==`, and a class can be made
  with a metaclass whose `__eq__` answers yes. Compare with `is` against each of the three
  classes, and add a test with such a class.
- **The settle marks itself tried before its protected region** (Codex P3, PR #9 fourth
  re-check, carried by the author, 22 Nov 2026; `evals/ws7_entry.py`, `_settle`).
  `self.settled = "failed"` is set on the line before the `try` that turns whatever the settle
  raises into a stop and a crash mark. Anything that ended the method between those two lines
  would leave the entry open with the settle recorded as tried, so the failure path would not
  try it again. Move the assignment inside the protected region, and test it.
- **An interrupted Thursday check can still send its ordinary report and exit 0** (Codex P3,
  PR #9 fifth re-check, carried by the author, 22 Nov 2026; `evals/ws7_entry.py`, `check`). A
  Ctrl+C that arrives after the check's last read of the latch, while its message is being
  built or sent, is not acted on: the report goes out and the exit code is 0. This follows the
  ruling of 22 Nov 2026 (what has passed its check completes), and the report it sends is a
  true one. 5b decides whether an interrupted check should also say that it was interrupted,
  and where (its log, its exit code), and tests what it decides.
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

### The next PR that changes `tests/test_image_files.py` or the `Dockerfile`

Two findings from Codex's review of PR #10, both P3, left out of that pull request because
fixing them changes the test file (the author's ruling, 23 Nov 2026):

- **The `Dockerfile` reader's continuation rules are not BuildKit's.** `instructions()` treats
  a line ending in a doubled backslash as a continuation, and puts a space between the
  fragments it joins. BuildKit does neither. So a standalone `ADD`, or a `WS7_GIT_COMMIT` key
  split across a continuation, can pass the test. Match BuildKit's continuation rules, or
  refuse those forms, with regression cases.
- **The ignore reader splits on every Unicode line break; Docker splits on LF only.**
  `ignore_patterns()` uses `str.splitlines()`. Read the bytes and split on LF, with a
  regression case.

### Slice 3's push script

**Closed by slice 3** (`infra/eval/push-image.sh`; `docs/WS7-RUNBOOK.md`, section 2). The script
runs every git command in an environment built from nothing (`env -i` and a short list), so
nothing inherited changes what git reads; it
requires `$C` to resolve to a commit, and to itself, and to be on `feat/agentic-pipeline`'s
first-parent chain in a fresh clone (a merge commit of the branch, never a commit from inside a
merged task branch; the author's ruling, 26 Nov 2026); it goes by `check5`'s status against the
image ID it pushes; it builds from a clone of GitHub, never from the local checkout; and it
checks its export against the commit, file by file, so the commit label is true of every copied
file. `tests/test_eval_scripts.py` holds each refusal.

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

**Built, not deployed** (`infra/eval-template.yaml`, `docs/WS7-RUNBOOK.md`,
`tests/test_eval_template.py`). Every note that stood here is in the template or the runbook:

| Note | Where it is now |
|---|---|
| `s3:ListBucket` limited to `ws7/`; `PutMetricData` in one namespace; `sns:Publish` to the eval topic | The task role, held to the job's constants by the template test |
| The lifecycle rule selects `ws7/runs/scheduled/` and nothing else | The bucket. Versioned, so the rule also deletes the old version 30 days later (the author's ruling, 26 Nov 2026) |
| The bucket name, `WS7_ALERT_TOPIC_ARN`, `WS7_IMAGE_DIGEST`, the image pinned by that digest, `PYTHONHASHSEED=0`, the command | The task definition |
| The task definition must not set `WS7_GIT_COMMIT` | It does not, and a test fails if it does |
| A writable temporary directory | The root file system is left writable. A read-only root needs a volume, which Fargate mounts owned by root unless the `Dockerfile` declares it |
| The check's schedule one UTC day after the run's; each passes its subcommand and its own scheduled time | The two schedules, created `DISABLED`. The scheduler's role may run the one pinned task definition revision |
| An alarm on a breach metric must not treat missing data as a breach | The 21 breach alarms |
| `ExpectedBucketOwner` on every S3 call | "Slice 3: the bucket's owner", below |
| The guarded push, from a clean export, with the machine's git configuration isolated | `infra/eval/push-image.sh` |
| The copy to `retained/<run_id>/` | The runbook, section 7 |
| The set-up checks in both "cannot prove" sections | The runbook, section 5. Those that need a task script are the next pull request |

### Slice 3, PR 2

Follow-ups from Codex's review of PR #11 (the stack's first pull request), ruled by the author
on 26 Nov 2026 as not holding that merge. Each is for the set-up checks pull request unless it
says otherwise.

- **Check the alarms by name, not by count** (Astra, P2). `infra/eval/schedules.sh on` counts 26
  alarms under the prefix `discharge-eval-`. An alarm deleted and another made by hand under that
  prefix would still count 26. Compare the alarm names with the template's own list.
- **A confirmed and unfiltered subscription for the configured email.** `schedules.sh on` asks
  only for some confirmed email subscription. It should be the stack's `AlertEmail`, confirmed,
  with no filter policy that could drop an alarm.
- **The bucket policy is not retained with the bucket.** The bucket has `DeletionPolicy: Retain`
  and its policy does not, so after a tear-down the retained bucket loses the two denies
  (insecure transport, the CI role).
- **A2's fallback does not verify the manifest's hash.** If the CLI reformats the manifest, the
  runbook falls back to `describe-images`, which shows the digest exists but not that the stored
  bytes hash to it.
- **The export's path spelling.** `push-image.sh` refuses a protected folder under another case,
  and a name that is not plain ASCII. It does not compare the case of every exported path with
  the commit's on a volume that ignores case.
- **`docker push` prints a digest before the script has compared it.** The script's own `PUSHED`
  line comes only after the comparison, but Docker's output above it shows a digest either way.
  Say so where it is printed, or quiet the push.
- **The set-up table's claims for C6, C7, and the CloudWatch and SNS error shapes and size
  limits.** State for each exactly what is observed on AWS and what stays covered offline only.
- **For the next docs pull request: the KMS rotation wording** in ADR-011's Costs and
  `docs/COST.md` (when a rotated key version starts to be charged, and the cap).

## Slice 3: the bucket's owner

The one change slice 3 makes to the job (the author's ruling 1, 23 Nov 2026). Two modules change,
`evals/ws7_store.py` and `evals/ws7_entry.py`, and for this alone.

- **The variable is `WS7_BUCKET_OWNER`**: the 12-digit account that owns the eval bucket. The task
  definition sets it from the stack's own account ID.
- **`Store(client, bucket, owner)`.** There is no default. A missing or malformed owner (anything
  but twelve ASCII digits in a string) is refused when the store is built, before any request:
  `ws7_store_owner_refused`.
- **Every S3 request the job sends carries it.** The store's three S3 calls (list, get, put) pass
  `ExpectedBucketOwner`, and nothing else in WS7 calls S3. The scheduled run and the Thursday
  check both build their store from the variable.
- **What S3 does with it.** A request for a bucket that another account owns is answered 403. For
  a read that is a failure (`ws7_store_list_failed`, `ws7_store_get_failed`). For a write it is a
  refusal that did not land (`ws7_store_put_refused`), never an ambiguous write.
- **The fake.** A request without the header is a fault, whatever it asks for, so every test that
  uses the fake is a control for a request that left it out. A request naming another account is
  answered 403 and nothing is read or written.
- **Because the job's code changed, the image is built from this change's merge commit**, not
  from part 6's.
- **What the fake cannot prove:** that real S3 answers 403 for another account's bucket under
  these exact calls. The set-up check B2 sends a request with a wrong owner to the real bucket.

## Tests

```zsh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q tests/test_ws7_compare.py tests/test_ws7_reserve.py tests/test_ws7_client.py tests/test_ws7_store.py tests/test_ws7_ledger.py tests/test_ws7_manifest.py tests/test_ws7_summary.py tests/test_ws7_publish.py tests/test_ws7_entry.py
```

- `tests/test_ws7_compare.py`: the comparator (`docs/WS7-COMPARATOR.md`, "Tests").

Slice 3 adds two test files that are not among the image's nine, because they read `infra/` and
`docs/`, which are not in the image:

```zsh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q tests/test_eval_template.py tests/test_eval_scripts.py
```

- `tests/test_eval_template.py`: `infra/eval-template.yaml` held to the job's own constants (the
  prefix, the namespace, the 21 breach names, the two parameter names, the variable names, the
  model), to ADR-011's rules, and to 51,200 bytes.
- `tests/test_eval_scripts.py`: the operator's scripts against stubs of `docker` and `aws` and a
  scratch repository. No test can reach a real `docker` or a real `aws`.

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
- `tests/test_ws7_store.py`, the bucket's owner (slice 3): at the server, every request of every
  store call carries the owner; an ordinary client that leaves it out is a fault for each kind of
  request (the positive control); a bucket another account owns is refused with nothing read or
  written, and a refused write is never ambiguous; a store is not built without twelve ASCII
  digits. `tests/test_ws7_entry.py`: the run and the check name the owner the variable gives, on
  every request; with the variable missing or malformed neither sends an S3 request.
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
  - Settlement from the rows (`tests/test_ws7_ledger.py`): each of the guard's ten final
    outcomes accepted with its metered or unmetered result; each with the report's own count
    or flag wrong in each direction; twelve words the guard does not write. Through the
    entrypoint, one call ended in each way the stub can end it, and the real guard's report
    settled as its rows say.
  - A report that cannot be settled from: one call's accounting stopped at each of five points,
    by an interrupt and by an exception, in each batch, for both targets. Each time no further
    call is sent, the entry is unresolved at its reservation, and next week's run is refused
    with `ws7_blocked_unresolved` and sends nothing. The partner: a clean run settles as
    metered and next week's run is admitted.
  - A report read that fails once, straight after the last pre-flight reply and after the last
    main reply, by an interrupt and by an exception, for both targets, with every later read
    succeeding: no main batch after the pre-flight's, never `RunOk` = 1, the failed record
    written, the entry a crashed run at its reservation, next week's run blocked. An interrupt
    caught at four places while the entry is open (two log lines, the outermost handler, inside
    the settle), with two partners after the settle; and at six log lines between the settle
    and the last act, the `success` line included, each with an ordinary exception as its
    partner.
  - **A real SIGINT** (`os.kill` on the test's own process, sent only while the job's handler is
    in place), at fourteen places from admission to the last check, for both targets: two log
    lines and three calls while the entry is open (the first pre-flight call, the first and the
    last main call); inside the settle after its write; five log lines after it; as the run's
    metrics are sent; as the success record is put; and the `success` line. Each time nothing
    raises, the run exits 1, `RunOk` is 0 and never 1, the failed record is written (beside the
    success record where that had been written), and no message is sent. An entry that was open
    is a crashed run at its reservation and blocks next week's run; one already settled stays
    settled. Each place has its partner: the same hook with no signal is a clean success. As
    `RunOk` = 1 is sent, and once it is acknowledged, the same signal changes nothing. One more
    is sent from the stub's thread while the run waits for a reply. On a breach run, a SIGINT
    before the alert means no message is sent.
  - The handler: while a run or the check is in progress it is the latch's, and the previous
    one (Python's own, a function, "ignored", the system default) is back after `main()` returns
    0, 1 or 2 and after it raises an exception, a `KeyboardInterrupt` or a `SystemExit`. Off the
    main thread, and with a handler that was not set from Python, nothing is installed.
  - The check: a real SIGINT before `expire()`, at the `checked` line with and without something
    to report, and once the message was sent, for both targets, each with its partner.
  - A throttle on the main batch's second scenario: no request for any later scenario reaches the
    stub; the partner sends all 18. A 500: unmetered and unresolved. A failed pre-flight: the
    main batch never starts.
  - The deadline at its boundary and one second before; a deadline that passes mid-run; a
    settle decided after `expires_at`.
  - The pre-flight's three checks, the cost one at exactly the reservation and one cache-read
    token over. A gate failure on the pre-flight scenario: the main batch runs and is compared.
  - A runner that raises after writing a clean index, in the pre-flight and in the main batch,
    for both targets: an exception, an interrupt, and `SystemExit` with 0, 1, 2, None and a
    string. Each is `ws7_runner_crashed`: `RunOk` = 1 is never sent, the ledger is settled and
    the failed record is written. The same seven endings for each scorer, the comparator, the
    precheck's runner code, the run's and the check's outermost handlers, a duplicate's publish
    and the parser.
  - The same call sites raising WS7's own `Stop` (a "no baseline" one) and `Duplicate`: the
    five calls into the scorers, the comparator, each runner in each batch, seven places in the
    precheck, and the match check. Each is that call's own code: a failed run, a failed record,
    the ledger settled, never exit code 0, no duplicate counted and no `NoBaseline` published.
    A `Duplicate` raised by the job's own code after the claim, at four places: `ws7_crashed`.
  - A runner's exit code 1 with nothing in the index to explain it, from each batch:
    `ws7_runner_refused`. Its partners: a 1 a recorded gate failure explains, and a 1 a failed
    generation explains. An exit code 0 beside a failed generation: refused too.
  - What explains an exit code, as a table: for each target, each of `error_code`, `gate_ok`,
    `gate_status` and `failed_step`, with thirteen values covering every JSON type. Once on
    `index_counts()` and `exit_code_explained()`, for exit codes 0 and 1, and once through the
    entrypoint with the pre-flight's index forged. Only a non-empty string `error_code` (either
    target) and `gate_ok` exactly false (v1 only) explain a 1.
  - The comparator: a missing file; an exit code that is not 0, 1 or 2, with the result shown
    never to be read; ten results that disagree with their exit code; a file already at the
    output path; a rule for PART C.
  - Storing: the file list equal, in both directions, to what the real runners and scorers
    wrote; an extra file, or a link, stopping a run that would have succeeded, and never
    uploaded. `score_batch` writing its reports and the assembly replay then raising: the
    reports and the guard's report stored, and the run failed by the scorer's code. Unknown
    files on the failure path: not uploaded, counted, and the rest stored, the guard's report
    first.
  - The precheck: its fingerprint equal to the wrapper's for every scenario; two scenarios with
    one first request stopping the run with no request at any server; the sleep shown never to
    be called, with a control in which a throttling client does make the runner sleep.
  - The check: a winter week and a summer week; each line of the table above; the look-back at
    0, 1 and 2 days; a run that died on Wednesday reported as abandoned that Thursday.
  - What the check reads, as a table: every field of the claim (7), the success record (14) and
    the failed record (15), each with a value of each JSON type (string, integer, float, boolean,
    null, list, object) outside its rule, each reported invalid by the check under the field's
    own name, each with the valid object as its partner. Then each field missing, unknown fields,
    objects that are not JSON objects, and values of the right type outside the pattern, the
    range or the claim, with the value at each boundary as the partner. A perfect success record
    beside an invalid claim does not count.
  - What the writers write: each writer is shown to give its object to the validator before the
    PUT, and to store that object; a record its validator refuses is never sent; a clock that
    steps back makes both records invalid and neither is written. Every entrypoint test also
    holds each claim and record the job created to its validator as the test ends.
  - The failure path: one thing broken in each case (the log stream, the settling write, the
    first stored record, the clock, the building of the failed record, its write, each publish),
    and every later step still tried, the ledger first. A log stream that raises on every line,
    under a good run, a duplicate and a check.
  - The same by exception type: the log sink, the settle, the first store call, the clock, the
    building of the failed record, its write and each publish call, each raising an
    `Exception`, `SystemExit(0)`, `SystemExit(1)` and `KeyboardInterrupt` inside the process.
    Every later step still reaches its server, the ledger is settled first, and the job
    returns 1. A stderr that raises while the usage line is printed: still exit code 2.
  - At most once: a breach run whose message, or whose success record, fails after the counters
    were accepted leaves exactly one sample of every counter at the fake, and the failure path
    makes one request. The control: counters that were not acknowledged are sent by the failure
    path.
  - `RunOk` = 1 rejected after the success record: a failed record beside it, `RunOk` = 0, and
    the check reporting a failed run. The partner: `RunOk` = 1 accepted, and no failed record.
  - Diagnostics: a runner returning a number of 5,001 digits (and eight other things that are
    not codes) from each batch, with every step of the failure path still running; an
    exception whose class-name lookup raises, and one whose class is named with a sentinel,
    raised at every place the seven other endings are. Every test holds that sentinel out of
    every log line, CloudWatch and SNS request, stored object, stdout and stderr.
  - Stops that are not the job's own: three `Stop` subclasses with a `line()` of their own (one
    raises, one returns a sentinel, one raises `SystemExit(0)`), a `StoreError` subclass, a
    `Duplicate` subclass, and an exact `Stop` with a code that is not the job's. Each from eight
    places in a run, three at its end, the failure path's eight steps and two places in the
    check. Each is a crash by a fixed label, every later step runs, and the sentinel is nowhere.
  - Every other command line, `--help` included: exit code 2, the one fixed usage line, nothing
    built and nothing sent. An invented sentinel in the rejected argument, for each kind of
    argument error, is in neither stream; the positive control gives the same command line to
    the same parser built on plain argparse, which prints it. The two layers are each tested
    alone: the parser raises and prints nothing with the streams open, and `main` shows nothing
    even of a parser that prints.

- `tests/test_image_files.py` (part 6) is **not one of the nine**, and the image's command never
  runs it. It checks the checkout, not the image, and it never skips: the `Dockerfile` is not in
  the image, and a skip-if-absent would pass silently in CI if a path were wrong. It reads
  `Dockerfile` and `.dockerignore` and pins five things, each as a whole class:
  - the commit argument has no default: one bare `ARG`, one `ENV` set from it and from nothing
    else, and no other instruction names the variable;
  - both come after the last `RUN` and `COPY`, the `ENV` after the `ARG`, in one stage;
  - `evals/runs` and `evals/calibration` are both ignored, the file has no `!` line, and there
    is no `Dockerfile.dockerignore`;
  - every `COPY` is one of the five named ones, exactly, so there is no `COPY .`; no `ADD` or
    other instruction the test does not read; no `RUN` that mounts the build context;
  - the `CMD` is pytest on exactly `tests/test_ws7_*.py` as they are on disk, both ways.

  ```zsh
  PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q tests/test_image_files.py
  ```

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
