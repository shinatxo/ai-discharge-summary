# WS7 entrypoint

The WS7 entrypoint is the job the weekly Fargate task will run ([ADR-011](adr/ADR-011-ws7-scheduled-eval.md),
slice 2). It is being built as six pull requests. This document grows with each one.

| Part | What | Status |
|---|---|---|
| 1 | The client wrapper and the reservation | **Built** (this document) |
| 2 | The claim, the ledger, the match check and the run manifest | Not built |
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
| `ledger_cas_attempts` | 4 | Tries at the ledger's compare-and-swap (part 2) |
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
- if the run is unmetered, its true cost is not known. Its ledger entry keeps the worst-case
  figure and is **unresolved**: it blocks every later scheduled (inside-cap) run until the author
  resolves it by recording a settled figure and its basis, for example Cost Explorer's day view,
  or "worst case accepted" (ADR-011 D7, point 4). An outside-cap manual run is not blocked by it,
  and still needs the author's yes;
- **AWS Budgets is an alert only, never the reconciliation.** It cannot identify one run's charge:
  the canary also calls Bedrock on this account, and Budgets is a delayed aggregate.

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
- It does not settle the ledger; part 5 settles from `total_usd_nanos`.
- It does not check that the scenarios' first-step requests are distinct, set the deadline or
  choose the sleep the runners are given (part 5).

## Notes for later parts

### Part 2: the ledger

Part 2 implements the **unresolved** state, the block and their tests (ADR-011 D7, point 4;
Codex review of PR #5):

- A run whose report says `unmetered` is settled at its counted total, which includes the worst
  case of the call that made it unmetered. Its ledger entry is marked **unresolved**.
- An unresolved entry **blocks every later inside-cap (scheduled) run**, in that month or any
  later one. A blocked run reserves nothing, spends nothing and fails with its own fixed code.
  Part 2 must therefore find an unresolved entry from an earlier month, not only in the current
  month's ledger.
- Resolving records a settled figure and its basis (for example "Cost Explorer day view", or
  "worst case accepted"), and the entry then counts at the settled figure.
- An outside-cap manual run is not blocked by it, and still needs the author's yes.
- Tests: an unresolved entry blocks the next scheduled run, with a partner where a resolved
  entry does not; an entry from the month before still blocks; a manual run is not blocked.

### Part 5: the modes

- **The distinct-scenarios precheck must use a canonical first-request fingerprint, not the raw
  notes hash** (Codex P3, PR #5). Two scenarios whose notes differ only in ways the pipeline
  normalises away, such as line endings, would have different notes hashes but could send the same
  first-step request. The wrapper would then refuse the second as a repeat. So the precheck builds
  the first-step request each scenario would send, after the runner's and the pipeline's own
  normalisation, and compares `ws7_client.fingerprint()` of each: the hash the wrapper itself uses.
  This is not fixed in part 1.
- A run whose report says `unmetered` is a failed run with the fixed code `ws7_unmetered`.
- **The resolve command.** A manual mode, run by the author only, that records a settled figure
  and its basis on an unresolved entry. It makes no Bedrock call. It is the only way an entry
  leaves the unresolved state; AWS Budgets never resolves one.
- **The check-mode report.** The Thursday check reports a run blocked by an unresolved entry by
  that cause, naming the entry's run ID, so the alert says what the author must resolve.
- Build one live client before the runner starts, so that a refused endpoint fails the run before
  any batch folder is written.

## Tests

```zsh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q tests/test_ws7_reserve.py tests/test_ws7_client.py
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

**Keeping the tests offline.** The `offline_aws` fixture:

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
