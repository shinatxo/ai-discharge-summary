# ADR-011 — WS7: scheduled re-evaluation and drift alarms

## Status

**Proposed** — 16 Nov 2026. Decisions by the author, 16 Nov 2026. Revised 17 Nov 2026 after
review: the cost ceiling, duplicate deliveries, the heartbeat and the incident procedure. Updated
18 Nov 2026 with the author's rulings on slice 2, in the part of slice 2 that builds each one.
Updated 19 Nov 2026 with the rulings on part 2: a manual run's own claim key, the listing
permission, the run prefixes, and what an unmetered entry counts at. Updated 19 Nov 2026 with the
ruling on part 3: a verdict metric that a rule names covers exactly the gold's scenarios, in the
baseline and in the run. Updated 20 Nov 2026 with the rulings on part 4: step failures and
assembly rejections are published by step, with the codes kept in the stored records; the cost
metric is the batch's own total; and code builds only a candidate baseline, in an envelope the
comparator refuses. Updated 20 Nov 2026 with the rulings on part 5a: `RunOk` is published only by
the process that holds the scheduled claim; once a call fails, the run sends no further call; a
breach is a successful run; a failed scheduled run sends no message of its own; the retained
prefix is outside `ws7/`; and a stored file that is not one the runners and scorers write stops
the run. Updated 21 Nov 2026 with the rulings after the review of PR #9: the pre-flight's
go/no-go is no failed generation, only the expected files and a cost within its reservation, and
a v1 gate failure is not in it.

This is a design. Slice 1, the offline comparator, is built (`docs/WS7-COMPARATOR.md`). Of slice 2,
parts 1 to 4 and part 5a are built (`docs/WS7-ENTRYPOINT.md`): the client wrapper and the
reservation; the claim, the ledger, the match check and the run manifest; the follow-ups from the
reviews of PR #4 and PR #6; the run summary and the candidate baseline; and the entrypoint's
scheduled run and Thursday check, with the comparator call and publishing. Part 5b (the manual
baseline, the manual confirm and the resolve command) and part 6 (the image) are not built.
Nothing is deployed or run. Every deploy, and every run that costs
money, needs the author's explicit yes at the time. No number in a breach rule is set here: the
author sets them before the first live run, and they change only by pull request.

## Context

### What WS7 is

A scheduled job that re-runs the synthetic evals and raises an alarm when the results move. Its
scope is: the scheduled re-evaluation, the drift alarms, the route from an alarm to the Safety
Incident Management Log, a version stamp on every output, and subgroup cuts (designed here, kept
separable, and possibly moved to January).

WS7 evaluates **synthetic runs only**. It never reads production inputs, results or traces.

### What already exists

- **The canary** (`src/canary`, ADR-009). A Lambda that signs in as a synthetic user and drives the
  deployed path: three scenarios nightly at 02:00, all 18 on Monday at 03:00 (Europe/London). It
  checks that a job completes, that `parse_ok` is true and that three outputs are non-empty. It
  holds no gold and scores nothing. Four alarms watch its `Scenario=ALL` metrics; three more watch
  Lambda errors and API 5xx.
- **Step traces** (`TraceTable`, ADR-009 (b)). They carry `pipeline_version` and, per step,
  `model_id` and `prompt_sha256`. They go live at the W11 cut-over. WS7 does not read them.
- **The eval harness** (`evals/`). Two runners call Bedrock directly: `run_cold_eval.py` (v1) and
  `run_pipeline_eval.py` (the pipeline's steps 1 to 7). Scorers compare the output with 18 gold
  records. Each record carries prompt and notes hashes, token counts and an estimated cost. The
  corpus hash is in the batch manifest, and each record links to its batch by `batch_id`. Both
  runners refuse to start inside a canary window, because the Bedrock quota (25 requests a
  minute) is shared across the account.
- **A container image** (`Dockerfile`, commit `8c78790`). It has run the scorers and the test
  suite offline. It has never run a live eval.
- **The Safety Incident Management Log** (`docs/WS4-SAFETY-INCIDENT-LOG.md`). Section 1 already
  lists "an alarm, an eval run" as a self-reported route.

### What does not exist

No schedule for the harness. No ECS, ECR or VPC. No metric made from a score. No baseline and no
definition of drift. No commit, image digest or `pipeline_version` on an eval record. No subgroup
tags.

### Findings in the repository that shaped the design

1. **The pipeline runner is not the worker.** `run_pipeline_eval.py` sequences the steps itself.
   It never calls `pipeline/orchestrate.py`, so it has no 270-second deadline, is not fail-closed,
   and does not run steps 8a and 8b. It matches the worker's prompts, step code and model. It does
   not match the worker's control flow.
2. **The image is offline-only today.** Its files are owned by root and it runs as a non-root
   user, so a runner cannot create `evals/runs/` inside it. Both runners' `main()` already accept
   a `runs_dir` argument, so an entrypoint can send output elsewhere without changing a runner.
3. **`.dockerignore` excludes `evals/runs` but not `evals/calibration`**, and the image copies
   all of `evals/` and `tests/`. An image built today would carry whatever is in those folders.
4. **`evals/score_batch.py` iterates a set of scenario IDs** (line 90), so the order of the `gold`
   block in its JSON output changes from process to process. The data is identical.
5. **The live alarm topic may not deliver.** `AlertsTopic` in `infra/template.yaml` is encrypted
   with `alias/aws/sns`. AWS's guidance is that CloudWatch alarms cannot publish to a topic
   encrypted with that default key. This was read from the template and from AWS's documentation;
   it was **not checked against the live stack**. It is recorded here as context and is **not
   fixed by this ADR**. The check is read-only:
   `aws cloudwatch describe-alarm-history --alarm-name discharge-audit-canary-success-low --history-item-type Action`.
6. **The CI deploy role cannot build this.** It has no ECS, ECR or EC2 permissions and may manage
   only roles named `discharge-audit-*` (`infra/cicd-oidc-role.yaml`).
7. **A CloudWatch alarm can look back seven days at most** (period multiplied by evaluation
   periods, for periods of an hour or more). A weekly job sits exactly on that limit. With the
   default sliding window, one datapoint a week leaves no slack at all (D9).
8. **The code under test may retry.** The pipeline retries a throttled call up to twice
   (`src/generate/pipeline/converse.py:22`), and so does the v1 runner
   (`evals/run_cold_eval.py:124`). The v1 runner's default client (`evals/run_cold_eval.py:684`)
   does not pin botocore's own retries either. This matters for a spend ceiling (D7).

### Constraints carried in

- Nothing reaches the live stack before the W11 cut-over. A push to `main` redeploys it.
- The R1 experiment is parked and unrun. Its instrument is frozen (CLAUDE.md section (h)). This
  ADR designs no renderer signal and relies on no R1 material.
- A verified citation does not mean a faithful value. A dose duration written "x2/7" has been seen
  to come out as "2 weeks" with its citation intact.
- One person operates this system and is also its Clinical Safety Officer.

## Decision

### D1. "Against what is deployed" means a version-matched artifact, and the canary stays

WS7 runs a container image built from the released commit, and that image calls Bedrock directly.
The canary remains the only test of the deployed service. Neither covers the other.

| | Can detect | Cannot detect |
|---|---|---|
| **Testing the deployed service** (the canary) | Broken sign-in, API, IAM, KMS or DynamoDB. A deploy that shipped different code. The orchestrator as wired: the deadline, steps 5a and 6 side by side, fail-closed, trace writes. Quota contention | Content quality. It sees three documents and holds no gold. The step outputs a scorer needs are in traces, which WS7 must not read |
| **A version-matched artifact calling Bedrock** (WS7) | Model behaviour changing behind the same model ID (HAZ-10). Step-level quality against gold. Token, latency and cost drift | Anything about the live stack. Whether the artifact really is what is deployed, unless that is checked (D11) |

**One deliberate difference from the deployed service: WS7 never retries a model call.** The
worker does. The pipeline retries a throttled call up to twice
(`src/generate/pipeline/converse.py:22`), and the v1 worker's client allows one hidden botocore
retry (`src/generate/README.md`, "Known deferral"). A throttle in WS7 is therefore a failed run,
not a quality signal (D7).

**What neither approach covers:**

- **Settings drift.** The harness sets the model ID and the caching setting itself. The stack takes
  them as parameters (`ModelId`, `PromptCaching`, pinned in `.github/workflows/ci-cd.yml`). The
  canary runs whatever the stack is set to but cannot say what that is. WS7 runs what it set
  itself. A live parameter that differs from the release record is seen by neither. D11's check
  compares the harness with the release record, not with the running stack.
- **The assembled bundle.** Neither scores the three documents a clinician would receive. The
  canary checks that they are non-empty. WS7 scores step outputs and, from D2, counts 8a and 8b
  rejections on an offline replay, which is the assembly code run outside the worker.
- **Value errors with no scorer.** The medication scorer counts invented doses and frequencies.
  Nothing checks a duration or any other value, so the "x2/7" case above would pass.
- **Real-input drift.** The 18 scenarios are fixed. A change in what real notes look like is
  invisible, by design: WS7 never reads production inputs.

*AWS concept:* a Bedrock model ID is a stable name. The response carries no build number, so
behaviour is the only thing that can be watched.

### D2. What is monitored: the existing runners, unchanged, plus an offline assembly replay

- **Until W11:** v1, with `run_cold_eval.py --all --deployed-path`.
- **After W11:** the pipeline, with `run_pipeline_eval.py --all`.
- The target is chosen by the container's command, set per schedule.
- The entrypoint calls each runner's `main()` with `runs_dir` pointing at the task's scratch disk.
  No runner is changed.
- After each pipeline run the entrypoint runs `evals/score_batch.py` and then
  `evals/score_assemble.py` on the fresh batch. Both are offline and cost nothing.

**Can `score_assemble.py` run on a `run_pipeline_eval.py` batch as it is? Yes.** It was written for
those batches. This was established by reading the code, not by running it. Three limits:

1. A record in which step 1 failed has no `guard_input` entry. The replay reads that entry
   without a guard, so it would stop with an error and not account for the record. Step 1 is code
   on fixed notes, so this should show at baseline, not later. The entrypoint treats a replay
   that stops as a failed run.
   - **Found in slice 2 part 4, by running it on an invented batch:** the replay also stops on a
     record with any upstream step missing (step 5a, 6 or 7, or a generation that failed before
     them). Its report step raises a `KeyError` (`evals/score_assemble.py:202`), because such a
     record has no `branches` entry (`:148-150`). So today any failed pipeline generation is a
     replay that stops, and so a failed run. The scorer is not changed by that part
     (`docs/WS7-ENTRYPOINT.md`, "What part 4 found").
   - **Built in slice 2 part 5a:** the entrypoint stops such a run from the batch's own index,
     before any scorer is called, with the fixed code `ws7_generation_failed`. A v1 run has no
     replay, so a failed v1 generation is `no_output` in the summary and the run is compared.
2. It runs the assembly steps directly. It is not the worker's orchestrator. That gap belongs to
   the later runner described below.
3. It also reports meaning warnings on PART C. Those numbers are held back with the PART C slice
   (D13). The first build publishes only the outcomes, the rejections by step, the gate status
   and the count of placeholders left.
   - **Rejections are published by step, and the codes are kept in the stored records** (slice 2
     part 4; the author's ruling, 20 Nov 2026). A gate failure at 8b has a count of its own. A
     metric named after a code would be in some runs and not in others, and a baseline needs the
     same metrics in all three runs.

The replay does not apply to the v1 target, which has no steps.

**Later, not now:** a second runner that drives `orchestrate.run` with an in-memory trace writer,
so the artifact follows the worker's control flow. It waits until R1 is finished.

*AWS concept:* an ECS container override lets one task definition run a different command for
each schedule.

### D3. WS7 sits beside the canary

The canary is not replaced and not extended. It answers a different question, and it holds no
Bedrock or DynamoDB permissions by design. Extending it would put gold and scorers under Lambda's
15-minute ceiling. Replacing it would remove the only test of the deployed path.

*AWS concept:* a Lambda invocation ends at 900 seconds; each function has its own least-privilege
role.

### D4. Its own stack, deployed by hand

- A new stack, `discharge-eval`, from a new template, `infra/eval-template.yaml`. It is deployed by
  hand with the author's own credentials, as `discharge-web` is.
- It is **not the live stack**. It can be deployed from `feat/agentic-pipeline` before W11 without
  touching `discharge-audit`. Its deploy and each spend still need the author's yes.
- Its roles are named `discharge-eval-*`, so the CI deploy role cannot change them.
- **If the stack is not up by about 30 Nov 2026, the v1 target is dropped** and WS7 starts with the
  pipeline after W11.

*AWS concept:* a CloudFormation stack is a unit of lifecycle. Separate stacks are created, changed
and deleted separately.

### D5. Compute: one Fargate task

Fargate, ARM64, 0.5 vCPU and 1 GB, on demand, one task at a time. ADR-008 already places
containerisation here. Lambda's ceiling leaves no room for retries. CodeBuild would work and
needs no network, but it is a build service used as a job runner. Scheduled GitHub Actions would
run only from `main`, publish their logs, and need a GitHub-to-AWS role that can invoke Bedrock.

*AWS concept:* Fargate runs a container without servers and bills per second of vCPU and memory.

### D6. Networking: a public subnet with nothing coming in

- A small VPC owned by the eval stack, one public subnet, a public IP on the task.
- A security group with **no inbound rules** and outbound 443 only.
- The control is the task role, not the network. The role may: invoke the one pinned model; put
  metrics in one namespace; get and put objects under the eval bucket's `ws7/` prefix (it cannot
  delete), and list that prefix (`s3:ListBucket` on the bucket, limited to `ws7/`; the author's
  ruling, 19 Nov 2026; slice 3 grants it; D7, "Permissions", says why); read the
  expected-version parameters; publish to the eval topic. It has **no
  permission on any `discharge-audit` resource**.
- A NAT gateway or interface endpoints would add a fixed monthly cost many times the cost of the
  runs themselves (see Costs).

*AWS concept:* a public IP is not reachability. A security group with no inbound rules drops
everything unsolicited.

### D7. Schedule, pre-flight, cost ceiling, duplicates and baseline

**Schedule.** Weekly, all 18 scenarios once, Wednesday 04:00 Europe/London. That is clear of both
canary windows.

**Pre-flight.** Before the 18, one scenario runs alone as its own batch. The run continues only if
it finished without error, wrote only the files expected, and cost no more than its reservation.
(The orchestrator has a function called `_preflight`. That is a different thing.)

- **"Without error" means no failed generation** (the author's ruling, 21 Nov 2026). A v1
  safety-net gate failure on the pre-flight scenario is quality evidence, not a no-go: the main
  batch runs and the comparator judges it. Stopping there would turn a safety-netting regression
  into "pre-flight failed", with no comparison and no breach alarm.
- A runner's exit code 1, from either batch, is accepted only when the batch's own index explains
  it: a failed generation, or a recorded gate failure. Otherwise the run fails
  (`docs/WS7-ENTRYPOINT.md`, "What a runner's return means").

**No retries.**

- The schedule's retry policy is zero.
- The entrypoint hands each runner a wrapped Bedrock client through the runner's existing
  `client_factory` argument. Two layers of retry have to be stopped, and the wrapper stops both:
  - **Retries inside one call.** botocore can retry within a single `converse` call, where the
    wrapper cannot see it. So the wrapper builds the underlying client itself, with
    `retries={"mode": "standard", "total_max_attempts": 1}` and the read timeout the runner asks
    for. `evals/run_pipeline_eval.py`'s `live_client` already does this (`:175`).
    `evals/run_cold_eval.py`'s default client (`:684`) does not, and it asks for no read timeout
    either, so the value used for v1 is fixed in slice 2 and written to the manifest. **It is 60
    seconds to read and 60 to connect** (`evals/ws7_reserve.py`): botocore's defaults, which the
    v1 runner runs on today.
  - **Retries by the code under test.** The wrapper allows **at most one call per model step per
    generation**. A repeat is refused, and the refusal ends that generation with a fixed code.
    - The wrapper sees requests, not generations. A generation starts at a first-step request
      (step 2, or v1's combined call) whose body differs from the current generation's; an
      identical one is a repeat. Each batch allows only the generations it planned, and the
      entrypoint first checks that no two scenarios would send the same first-step request,
      comparing a canonical fingerprint of the request, not the raw notes hash. Every doubt is a
      refusal (ruling e1, 18 Nov 2026).
    - **Once any call is refused, every further call is refused** until a distinct first-step
      request starts the next generation. So after a throttled step 5a whose retry was refused,
      the step 6 the runner still asks for is never sent (Codex review of PR #5).
    - The wrapper also refuses a request that is not one of the plan's steps, a step whose
      `maxTokens` is not the figure reserved for it, and any other model ID. The reservation is a
      bound only if the steps send what it reserved.
  - **Where the calls go.** The wrapper builds the client with botocore's
    `ignore_configured_endpoint_urls=True`, so an endpoint URL set in the environment or the AWS
    config file cannot move it; only an explicit argument can, and the live path passes none. At
    construction the live client must be Bedrock runtime in eu-west-2, or it is refused before any
    request (`docs/WS7-ENTRYPOINT.md`).
- So a throttle fails that generation, and the run is reported as failed.
- **Once a run has failed, it sends no further call** (slice 2 part 5a; the author's change,
  20 Nov 2026). Any call whose outcome is not a response, and any refusal, fails the run. The
  runner does not know that and would go on to the next scenario, and the wrapper itself refuses
  the rest only when a call is unmetered. So the entrypoint wraps each client once more: once
  the wrapper's report shows such a call, every further call in the run is refused, in the
  pre-flight and the main batch, and each generation ends as it does at the wrapper's own
  refusals. The run fails with the first failure's code and settles from the wrapper's report.
  `evals/ws7_client.py` is not changed.
- This is tighter than the code under test, which may retry (Context, finding 8). No runner and
  no pipeline file is changed.
- The reason is the ceiling below. With the two retries the code allows, the output part of the
  reservation alone would be £16.02, above the cap, and no run could ever start.
- The entrypoint has a wall-clock limit that ends the run on Wednesday. A runner that refuses to
  start (a canary window, an existing batch folder) is a failed run.

**The cost ceiling: £15 a month of model spend.** Output is enforced by `maxTokens`. Input is not
capped by anything in a request: the running-spend guard stops a run within one call of its
reservation, and that one call's input may exceed its allowance (about £0.13 by the planning
figures, an estimate, not an enforced limit). The ledger records actual cost. Where a run's cost is
unknown, its entry holds an estimate and is **unresolved**: it blocks every later scheduled run
until the author resolves it (point 4). AWS Budgets is an alert only, never the reconciliation
(point 5). **The month's spend passes £15 by at most one call only while every settled entry in
the month is either its actual metered cost or a defensible upper bound** (point 3). A month in
which an entry was resolved as an accepted estimate has no such assurance, and is marked so
(point 4).

1. **A worst-case reservation for each run.** For every model step: its enforced `maxTokens` as
   output, for every attempt the client may make (one), plus an input allowance.

   | Step | `maxTokens` | Set at | Sent at |
   |---|---|---|---|
   | 2 `extract_facts` | 8,192 | `src/generate/pipeline/extract.py:75` | `:121` |
   | 5a `reconcile_medications` | 4,096 | `src/generate/pipeline/reconcile.py:58` | `:251` |
   | 6 `compose_clinical` | 6,000 | `src/generate/pipeline/compose.py:85` | `:411` |
   | 7 `compose_patient` | 3,000 | `src/generate/pipeline/compose_patient.py:103` | `:431` |
   | v1 combined call | 4,096 | `evals/run_cold_eval.py:96` | `:333` |
   | v1 patient second pass | 1,500 | `evals/run_cold_eval.py:98` | `:333` |

   - **The output part is enforced by `maxTokens`.** Bedrock stops a response there.
   - **The input allowance is measured.** It is the largest input per step seen in the baseline,
     multiplied by a margin the author sets, and priced at the cache-write rate, the highest
     input rate. Until a baseline exists the figure is provisional and comes from the runners'
     planning estimates, split by step: 6,000, 7,000, 5,000 and 2,500 tokens for steps 2, 5a, 6
     and 7 (`evals/run_pipeline_eval.py:75-78`, which add up to `:79`), and 5,600 and 3,300 for
     v1's two calls (`evals/run_cold_eval.py:121-122`).
   - **The margin is a multiplier written as a percentage:** 100 means the figure itself, not
     widened. It is set in `evals/ws7_config.json` (100, ruled 18 Nov 2026).
   - Nothing in the request can cap input the way `maxTokens` caps output. The guard in point 3
     is what holds a run to its reservation.
2. **Reserve first.** The reservation is written to the monthly ledger before the first Bedrock
   call. The run starts only if the month's total plus the reservation is **£15 or less**. The
   pre-flight generation is inside the reservation. In code the cap is 18,750,000 micro-dollars:
   1,500 pence at 80 pence a dollar (`evals/ws7_config.json`), rounded down.
3. **Guard the spend while the run is going.** The client wrapper keeps a running total for the
   run.
   - **After every call** it adds that call's cost, from the usage Bedrock returned. A call that
     returned no usage is counted at its worst case.
   - **A call whose cost is unknown stops the run.** A worst case is an estimate, not a bound: it
     counts the input at the step's allowance, and nothing caps a real call's input. So a call
     that returned no usable usage, and whose model may have run (a reply with no usage, a 5xx, a
     read timeout, any other error except a throttle or a request that never reached Bedrock),
     makes the run **unmetered**: the wrapper refuses every further call in the run, and the run
     ends as failed with the fixed code `ws7_unmetered`. Its ledger entry holds the wrapper's
     counted total, which counts that call at its worst case, and is unresolved (point 4) (Codex
     review of PR #5).
   - **A throttle is exactly an HTTP 429 with the error code `ThrottlingException`.** AWS: "A 429
     response means that the request was not admitted. Inspect the API-specific error type
     rather than relying on the HTTP status alone." (*Scaling and throughput best practices*,
     <https://docs.aws.amazon.com/bedrock/latest/userguide/scaling-throughput-best-practices.html>,
     read 18 Nov 2026.) The page says nothing about billing, and that some operations also use
     429 for `ModelNotReadyException`. So a 5xx carrying that code, or a 429 with any other code,
     is unmetered.
   - **Before every call** it checks the total. Once the total has reached the run's reservation,
     the wrapper refuses every further call, and the run ends as failed with a fixed code.
   - **A run can therefore pass its reservation by at most one call.** That call's output is
     capped by its `maxTokens`; its input is not, and may exceed its allowance. This holds because
     both runners send one request at a time, and because every earlier call was either metered
     or did not run the model. A runner that sent requests in parallel could pass it by one call
     per call in flight; the later orchestrator-driven runner (D2) must revisit this.
   - **The month's spend can pass £15 by at most one call, but only while every settled entry in
     the month is either its actual metered cost or a defensible upper bound** (Codex review of
     PR #5).
     - Why: a run starts only if the month's *recorded* total plus its reservation is £15 or less
       (point 2), so the recorded total passes £15 by at most the last run's one call.
     - The *true* spend is no more than the recorded total only if no entry is below its true
       cost.
     - An entry resolved as `estimate_accepted` (point 4) may be below it, so it waives this
       assurance for its month.
     - "Actual metered cost" is priced at the repository's rates, which are not confirmed
       against AWS's price page (Costs).
   - **The same bound, restated from what was booked** (Codex P3, PR #5; built in slice 2
     part 2). Entries change after later runs are admitted (a reservation is settled, an entry
     is resolved), so the proof does not rest on the recorded total as it stands later. Take
     the month's last admitted scheduled run, *r*, with reservation R.
     - **What was booked at r's admission.** The rule in point 2 held inside the write that
       booked *r*: B + R is £15 or less, where B is what every earlier scheduled entry counted
       at, at that moment. No scheduled run is admitted while any entry is open or unresolved,
       so each of those entries was settled or resolved, and a settled or resolved entry never
       changes. B is fixed from then on.
     - **Each term of B is at least that run's true cost**, provided the entry is its metered
       cost or an `upper_bound`.
     - **The last run's reservation and its final call.** *r*'s true cost is below R plus one
       call.
     - So the month's true spend is at most B + R plus that call: £15 plus one call. The one
       way the second step fails is an `estimate_accepted` entry, and that is what sets the
       month's "assurance waived" flag.
     - The full text is in `docs/WS7-ENTRYPOINT.md` ("The monthly bound, restated") and in
       `evals/ws7_ledger.py`, beside the code and the tests that pin each step.
   - **Why that one call is limited in practice, though by nothing enforced.** A step's input is
     its fixed prompt and the fixed scenario notes, plus the outputs of earlier steps. The prompt
     and the notes are pinned by hash in the baseline key and the match check (D9, D11). Each
     earlier output is capped by its own `maxTokens`, and so is the step's own output. But the
     code adds fields to the facts it passes on, so this is not a hard cap on input.
   - **How much: an estimate.** By the runners' planning figures (`evals/run_pipeline_eval.py:75`),
     the dearest single request is step 2: about 6,000 input tokens, all of them fixed, and 8,192
     output tokens. That is about **£0.13**. The other steps come to about £0.07 to £0.11 with
     their upstream input taken at its cap. A v1 request is about £0.07. These figures are
     estimates from the planning figures, **not an enforced limit**. They are provisional until
     the baseline measures the inputs, and they do not count the fields the code adds to the
     facts it passes on.
   - **The guard tripping is itself a drift signal.** The inputs are pinned, so a run reaches its
     reservation only if the model is writing far more than it did at baseline. Each trip is
     published as a metric (D9).
4. **Settle after.** When the run ends, the reservation is replaced by the actual cost, worked out
   from the usage Bedrock returned. The wrapper's report carries the run's ID, and a run is
   settled only from its own report (Codex review of PR #6). Any request that was sent and has no recorded usage stays at
   its worst case: a step that raised after a response, a crash before the record was written,
   an unfinished run.
   - **Three kinds of entry are unresolved**, because each one's cost is only an estimate:
     - an unmetered run's entry;
     - an abandoned reservation ("One run at a time", below);
     - the entry of a run that crashed before its record was written.

     The last two were ruled by the author on 18 Nov 2026. Each is recorded at an estimate,
     because its true cost is not known (the author's ruling, 19 Nov 2026):
     - an unmetered run at the wrapper's **counted total**: every reply at the usage Bedrock
       returned, and the call that made it unmetered at its worst case;
     - an abandoned reservation, and a crashed run, at the **reservation**.
   - **How each is recognised** (`docs/WS7-ENTRYPOINT.md`): an unmetered run and a crashed run
     report themselves. A run that dies outright reports nothing, so its entry stays open and
     is found at its wall-clock limit: that is the abandoned case. An entry takes one kind, the
     first one written.
   - An unresolved entry **blocks every later inside-cap (scheduled) run**, in that month or any
     later one, until the author resolves it.
   - The author resolves an unresolved entry, of any of the three kinds, by recording a settled
     figure **and its basis**, which is one of two:
     - **`upper_bound`:** a figure from AWS Cost Explorer, defined exactly (Codex review of
       PR #5; the Service and Region filters removed by the author, 18 Nov 2026):
       - **unblended** cost;
       - **Charge type** = Usage **only**, so credits, refunds and other negative adjustments are
         excluded;
       - for the **whole account**, with **no Service filter and no Region filter**;
       - for the run's **UTC date or dates** (a run that spans two UTC dates takes both), read
         once that day's figure is complete. Cost Explorer's days are UTC: "All times are in
         Universal Coordinated Time (UTC)" (AWS, *Modifying your chart*,
         <https://docs.aws.amazon.com/cost-management/latest/userguide/ce-modify.html>, read
         18 Nov 2026).
       - **Why no Service or Region filter:** Cost Explorer shows Anthropic models on Bedrock
         under a service name of their own for each model, not under "Amazon Bedrock", so a
         Service = Amazon Bedrock filter returns none of the model's cost.
         - AWS does not document these names, so the source is secondary: Classmethod
           DevelopersIO, *Tried aggregating by model using Amazon Bedrock product attributes in
           Cost Explorer* (10 Oct 2026),
           <https://dev.classmethod.jp/en/articles/bedrock-product-attributes-cost-explorer/>,
           read 18 Nov 2026. It says: "Anthropic and OpenAI models are displayed in Cost Explorer
           under different service names for each model". Its example is "Claude Sonnet 5.5
           (Amazon Bedrock Edition)".
         - The service name for our model, `anthropic.claude-sonnet-4-6`, is inferred from that
           pattern; it has not been seen.
         - With no Service or Region filter, the figure cannot miss the model's rows, whatever
           service name or region they carry.
       - **It is looser:** it includes every other service's usage on the account that day, the
         canary's calls among them. That only makes the figure larger, so it errs only towards
         blocking. The entry counts at that figure, and **the month keeps its assurance**.
       - **Why Usage only:** a net figure, reduced by credits or refunds, can fall below the run's
         usage, and would then not be an upper bound. Cost Explorer lists **Credit** ("Any AWS
         credits that are applied to your account") and **Refund** ("Any refunds that you
         received") as charge types apart from **Usage** (AWS, *Filtering the data that you want
         to view*, <https://docs.aws.amazon.com/cost-management/latest/userguide/ce-filtering.html>,
         read 18 Nov 2026).
       - It assumes no reservation or Savings Plan applies to the model's usage on this account.
         The same page defines Usage as "Usage that AWS didn't apply reservation discounts to",
         and puts Savings Plan covered usage under a charge type of its own, so Usage alone would
         then leave part of a run out. If either is ever bought, this definition is revisited.
       - Before its first use, the author checks against real billing rows that the model's rows
         carry Charge type = Usage (slice 2 part 5, `docs/WS7-ENTRYPOINT.md`).
     - **`estimate_accepted`:** the entry's own estimate is accepted as the figure, and no other
       figure can be recorded under this basis. It may be below
       the true cost, because the input was not capped. This **waives the monthly assurance for
       that month**: the ledger marks the month "assurance waived", and every run summary and
       check report for that month shows it.
   - An outside-cap manual run is not blocked by an unresolved entry, and still needs the author's
     yes, as every manual run does.
   - A scheduled run that an unresolved entry blocks spends nothing and fails with its own fixed
     code; the Thursday check reports the blocked run (D9).
5. **A backstop alert.** An AWS Budgets email. It lags by hours and warns; it does not stop
   anything. **It is an alert only, never the reconciliation:** it cannot identify one run's
   charge, because the canary also calls Bedrock on this account and Budgets is a delayed
   aggregate (Codex review of PR #5).
   - **Its filter must not be Service = Amazon Bedrock alone**, for the reason in point 4:
     Anthropic models are billed under a service name of their own for each model, so that
     filter would miss our model's cost.
   - Use no service filter, or Bedrock's **model provider** product attribute (Anthropic).
   - AWS added Amazon Bedrock product attributes to Cost Explorer, Budgets and Dashboards on
     8 Oct 2026: model, model provider, inference type and feature. "In Budgets, you can filter
     budgets using the same product attributes." Source: AWS What's New,
     <https://aws.amazon.com/jp/about-aws/whats-new/2026/10/aws-bedrock-attributes-in-cost-explorer/>,
     read 18 Nov 2026. The page is in Japanese; the quote and the attribute names are
     translations.

**Duplicates and concurrency.** EventBridge Scheduler delivers at least once, so the same
Wednesday can arrive twice.

- **Claim the occurrence first.** The job writes `ws7/claims/<target>/<scheduled date>` with
  `If-None-Match: *`. The date comes from the schedule's own scheduled time, not from the clock:
  it is the **UTC date** of that time, which the job is given as an argument.
  If the object already exists, the job exits as a duplicate: it spends nothing and publishes a
  duplicate count, which has no alarm.
  - A scheduled time in the future, or older than `max_start_delay_s`, is refused before any
    write.
- **One ledger object a month,** updated by compare-and-swap: read it, then write it back with
  `If-Match` and the ETag that was read. On a conflict the job re-reads and tries again, a fixed
  number of times. If it still cannot write, it does not start.
  - The month is the UTC month of the run's scheduled time.
  - The number is `ledger_cas_attempts`, and it counts **tries in total**, not retries.
  - The month's first ledger is created with `If-None-Match: *`.
  - **A write whose reply failed may have landed.** It is never assumed either way: the job
    reads the object back and looks for its own `write_id` (a claim's `claim_id`), after the
    last try as well.
  - **The S3 client never follows a redirect**, and never sends one call twice. botocore would
    otherwise re-send a request to the region S3 names, whatever the attempt limit says
    (`docs/WS7-ENTRYPOINT.md`, "The store").
- **One run at a time.** A reservation that is still open blocks any other run. One left open past
  the wall-clock limit is treated as abandoned.
  - The open reservation may be in another month's ledger, so the job reads every month. After
    booking its own it reads them once more and meets the same rules again, leaving itself
    out: a scheduled run withdraws at zero if any other entry is open or unresolved, and a
    manual run if another run is still live (Codex review of PR #6).
  - **The run's own deadline ends before that limit**, by a margin that covers one call in
    flight (120 seconds: v1's 60 to connect and 60 to read), so that no call should still be
    running when another task may mark the entry abandoned.
  - It stops blocking as an open reservation, so it no longer holds back an outside-cap manual
    run.
  - It stays counted at its worst case, and becomes **unresolved** (point 4; the author's ruling,
    18 Nov 2026): it blocks every later inside-cap (scheduled) run until the author resolves it
    on a basis.
- **Manual runs use the same claim and the same ledger.** A baseline batch or a confirming run is
  written to the ledger labelled outside-cap. It does not count towards the £15, and it cannot
  overlap a scheduled run.
  - **The same claim mechanism, not the same key** (the author's ruling, 19 Nov 2026). A manual
    run claims `ws7/claims/<target>/manual/<run_id>`, also with `If-None-Match: *`, and its
    run ID cannot be a scheduled run's.
  - Why: with one shared key, a manual run started on a Wednesday before 04:00 would take that
    date's claim. The scheduled delivery would then exit as a duplicate, which has no alarm,
    and the Thursday check would find a claim that was not the scheduled run's.
  - What stops the two overlapping is the open reservation, as above.
- **Permissions.** `If-None-Match` needs `s3:PutObject`. `If-Match` needs `s3:GetObject` and
  `s3:PutObject`. The task role has both on the `ws7/` prefix and has no delete permission, so
  it cannot remove a claim. Source: AWS, *How to prevent object overwrites with conditional
  writes*, <https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html>, read
  17 Nov 2026.
  - **The role may also list the `ws7/` prefix** (`s3:ListBucket`, limited by `s3:prefix`; the
    author's ruling, 19 Nov 2026). An unresolved entry in an earlier month has to be found, and
    so does a claim that is not there (the Thursday check, D9).
  - **Absence is decided from a listing, never from a GET error.** S3 answers a GET for a
    missing key with 404 only if the caller has `s3:ListBucket`, and with 403 otherwise (AWS,
    *GetObject*, <https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html>, read
    19 Nov 2026). AWS does not say which a grant limited by prefix gives. So the job lists
    first, reads only the keys the listing returned, and treats any error reading a listed key
    as a failure.

**Baseline.** One batch of 18 scenarios three times, run **by hand**, at set-up and at each version
change. Each needs the author's approval and sits **outside** the scheduled cap. The baseline is
not built from weekly runs with the alarms off.

*AWS concept:* EventBridge Scheduler is cron with time zones and a retry policy. Bedrock has no
spend cap of its own, so the ceiling has to be ours. An S3 conditional write succeeds only if the
object is absent, or unchanged since it was read.

### D8. Run records go to an S3 bucket owned by the eval stack

- The bucket is created by the eval stack. It does not wait for the private evidence store.
- Encrypted with the eval stack's own key (D9). Block Public Access on.
- Scheduled runs expire after **90 days**. Baseline batches are kept. So are the claims, the
  monthly ledgers and the success records (D7, D9): they are small and hold no generated text.
- **The prefixes follow that rule** (the author's ruling, 19 Nov 2026). A lifecycle rule selects
  by prefix, so a scheduled run's records go under `ws7/runs/scheduled/<run_id>/` and a manual
  run's under `ws7/runs/manual/<run_id>/`. The rule expires `ws7/runs/scheduled/` only, and
  nothing that is kept sits under it. The layout is in `docs/WS7-ENTRYPOINT.md`.
- **A run that becomes an incident is copied to a retained prefix before the 90 days are up.** That
  copy is a step in triage (D10).
  - **The retained prefix is `retained/<run_id>/`, outside `ws7/`** (the author's ruling,
    20 Nov 2026). The task role's whole reach is `ws7/` (D6), so the job can never overwrite an
    incident copy, and the lifecycle rule does not touch it. The author makes the copy by hand,
    with their own credentials (slice 3's runbook). Nothing in the job writes it.
- What is stored: the run manifest, the batch records (model output on synthetic notes), the
  scores, the assembly report and the comparison result. No real data.
  - As built (slice 2 part 5a): also the pre-flight batch, the run summary and the client
    wrapper's report. Success records are at `ws7/success/<run_id>.json` and failed records at
    `ws7/failed/<run_id>.json`, both kept.
  - **Only `.json` and `.md` files, and only the files the runners and scorers are known to
    write.** Any other file in a batch folder stops the run; it is never skipped (the author's
    ruling, 20 Nov 2026).

*AWS concept:* an S3 lifecycle rule deletes objects on a clock.

### D9. Metrics, the breach rule, the heartbeat and the alert topic

**Where drift is decided.** The job compares each run with a frozen baseline and publishes one
breach value (0 or 1) per family and kind. The CloudWatch alarms are then simple: breach above 0.
Per-scenario detail goes to the run summary in S3, not into metric dimensions.

- As built (slice 2 part 5a): the breach metrics are named `Breach<Family>Kind<n>`, for example
  `BreachSafetyNettingKind2`, with the one dimension `Target`. There is no name for PART C (D13):
  a comparison row for that family fails the run. The other metrics are `RunOk`, `RunRefused`,
  `Duplicate`, `NoBaseline` and the Run family's counts (`docs/WS7-ENTRYPOINT.md`, "What is
  published").
- **A breach is a successful run** (the author's ruling, 20 Nov 2026): it generated, scored,
  compared and stored. It publishes its breach values and `RunOk` = 1.
- A run that fails publishes no breach value, because it made no comparison. So an alarm on a
  breach metric must not treat missing data as a breach; `RunOk` and "no baseline" report a run
  that did not compare.

**Metric families** (namespace `DischargeAssistant/Eval`, dimension `Target`):

| Family | Source | Examples |
|---|---|---|
| Run | the entrypoint | step failures by step (the fixed codes stay in the stored records: slice 2 part 4, ruling 20 Nov 2026), spend-guard trips, p90 elapsed time, tokens, estimated cost (the batch's own total in the client wrapper's report) |
| Safety-netting | `score_batch.py` | invented seek-help triggers, pinned line missing, gate failures, verdict per scenario |
| Medications (D4 metric) | `score_batch.py` | invented doses, invented frequencies, verdict per scenario |
| Step-2 advice | `score_batch.py` | route correct per scenario, gold items recalled |
| Composition (step 6) | `score_batch.py` | check counts |
| HAZ-03 | `haz03_check.py` | failures, verdict per scenario |
| Assembly | `score_assemble.py` | 8a and 8b rejections by step (the codes stay in the stored records), gate status, placeholders left |
| PART C | blocked, see D13 | meaning-flag counts by kind |

**The breach rule has three kinds.** The ADR fixes their form. The author fixes their numbers.

1. **Zero-tolerance counters.** A breach is any count above the highest value in the three
   baseline runs. Baseline and weekly runs are both counted per 18-scenario run, so no scaling is
   needed.
2. **Per-scenario flips.** A scenario is *stable at baseline* when it has the same passing verdict
   in all three baseline runs. A breach is a stable scenario that now fails. Scenarios that are not
   stable are named in the baseline file and left out of this kind.
3. **Noisy counts** (meaning flags, for example). A breach is a count outside the baseline's range,
   widened by a margin, **in either direction**. A fall can mean a check has stopped firing.

**The numbers.** Which metric belongs to which kind, how many flips make a breach, and each margin
are set by the author before the first live run. They live in a rules file in the repository and
change only by pull request.

**The baseline file.** It holds counts and verdicts, never text. It is keyed by `pipeline_version`
(or the v1 prompt hashes), model ID, caching setting, corpus hash, gold hashes and scorer versions.
If any key differs from the run's, there is no comparison: the job publishes "no baseline" and
that has its own alarm.

**Freezing a baseline.** A baseline is frozen only after the author has compared it with the
figures in `evals/EVAL_RESULTS.md` for that version. **A first batch that is worse than those
figures is a finding, not a baseline.**

**What weekly 18 x 1 cannot detect.**

- **An intermittent failure.** A fault that appears in one generation in five of one scenario is
  seen in a given week one time in five. After four weeks there is still about a 41% chance it has
  not been seen once.
- **Whether one flip is drift or noise.** Three passes out of three at baseline are consistent with
  a true failure rate as high as about 60%. One flip is a reason for a confirming run (18 x 3, by
  hand, with approval), not proof of change.
- **A small shift** in a noisy count that stays inside the baseline's range.
- **Anything faster than a week,** including a change that appears and reverts between Wednesdays.
- **Anything outside the 18 scenarios,** and any rate for a subgroup (D12).
- Everything in D1's "neither" list.

**The heartbeat has two independent paths.** Both answer one question: did Wednesday's run
succeed? A run succeeds when it has generated, scored, compared and stored its records. Its last
two acts are to write a success record to S3 and then to publish `RunOk` = 1.

**Primary: a check run on Thursday.**

- A second schedule, **Thursday 10:00 Europe/London**, starts the same task in a check mode. Check
  mode makes no Bedrock call and no claim.
- It looks for Wednesday's claim and success record, and sends an alert when there is no claim,
  when the run started and did not finish (a claim and no success record), or when it finished
  as failed.
  - **"No claim" does not mean the run never started** (the author's change, 20 Nov 2026). A run
    that stopped before its claim, at the match check for example, also leaves no claim. The
    report says: "no claim: the run did not start, or stopped before its claim; see the task
    log".
  - **The check takes the run's ID from the claim object and never builds one.** The claim is
    keyed by the UTC date; the ID also holds the UTC hour, which is 04 in winter and 03 in
    British Summer Time.
  - If a success record and a failed record both exist, it is reported as a failed run.
  - **The check trusts none of the three objects as read** (as built, 21 Nov 2026). The claim,
    the success record and the failed record are each checked (schema, run ID, target, mode,
    scheduled time) against the occurrence and the claim. One that fails is reported as invalid,
    with a fixed label, and never counts as a success.
  - The check first marks any reservation left open past its limit as abandoned, so a run that
    died on Wednesday is reported as abandoned that Thursday.
- A run **blocked by an unresolved ledger entry** (D7, point 4) has a claim and a failed record
  with its own fixed code. The check reports that cause by name, so the alert says what the
  author must resolve.
- If the month's ledger is marked "assurance waived" (an entry resolved as
  `estimate_accepted`, D7 point 4), the check's report says so, every week of that month.
- **Warning time: 30 hours** after the scheduled start (Wednesday 04:00 to Thursday 10:00), plus
  the minute or two the check takes.
- It reads S3 and publishes to the topic. It does not depend on CloudWatch.

**Backstop: an alarm on `RunOk`.** It covers the case where the check itself does not run.

| Setting | Value |
|---|---|
| Metric | `RunOk`, namespace `DischargeAssistant/Eval`, dimension `Target` |
| Published | Once, value 1, by the scheduled run as its last act. A scheduled run that fails **while it holds its claim** publishes 0. A run that stops before its claim publishes no `RunOk`: that process cannot know it is not a duplicate (the author's ruling, 20 Nov 2026). Manual runs, duplicates and the check never publish it |
| Statistic | Maximum |
| Period | 86,400 seconds (one day) |
| Evaluation periods | 7 |
| Datapoints to alarm | 7 |
| Threshold | Less than 1 |
| Missing data | Treated as breaching |
| Window type | Wall clock (`EvaluationWindow` = `WallClockWindow`, time zone `Europe/London`) |

- **What it means.** The alarm fires when none of the seven most recently completed London
  calendar days holds a success.
- **Why a wall clock window.** AWS supports it for a one-day period on a metric alarm. It looks at
  exactly the aligned window and no further back, and it follows the local clock through the
  clock changes. The default sliding window is not used: with one datapoint a week it has no
  slack, so a late run, or the October week that is an hour longer, would raise a false alarm;
  and a sliding window also reads further back than its evaluation periods, by an amount AWS
  does not state.
- **Real worst-case warning time.** After a missed Wednesday the window that matters is Thursday
  to Wednesday. It completes at Thursday 00:00 London, and an alarm that spans more than a day is
  evaluated once an hour. By AWS's documented rules the alarm changes state by about **Thursday
  01:00, 21 hours after the scheduled start**. It can never fire sooner than Thursday 00:00,
  however early on Wednesday the run failed. AWS gives no bound for metric or notification
  delay. **None of this has been tested.**
- **Still on the seven-day limit.** Seven one-day periods is the most CloudWatch allows. If the
  API refuses that with a wall clock window, the fallback is a single one-week period, which
  evaluates Monday to Monday. Its worst case is the following Monday at about 01:00: **4 days and
  21 hours** after the scheduled start.
- **Not checked:** whether CloudFormation's alarm resource exposes `EvaluationWindow`. If it does
  not, the runbook creates this one alarm with the CLI.
- Sources, read 17 Nov 2026: AWS, *Configuring how CloudWatch alarms treat missing data*,
  <https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarms-and-missing-data.html>;
  *Alarm evaluation*,
  <https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarm-evaluation.html>;
  *Alarm evaluation window*,
  <https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarm-evaluation-window.html>.

**Early signals, not relied on.** Two faster signals stay, because they cost almost nothing:

- an EventBridge rule on `ECS Task State Change` for the eval cluster, for a task that is
  `STOPPED` with a container `exitCode` other than 0 or a `stopCode` of `TaskFailedToStart`;
- alarms on `TargetErrorCount` and `InvocationDroppedCount` in `AWS/Scheduler` for the eval
  schedule group. AWS delivers these metrics on a best-effort basis.

**The alert topic.** The eval stack has its own SNS topic, encrypted with a customer-managed key
owned by the eval stack. It does not use `alias/aws/sns` (Context, finding 5). The same key
encrypts the eval bucket, so the stack has one key, not two. CloudWatch's and EventBridge's
grants are separate statements in the key policy: CloudWatch's can carry a source condition
(`aws:SourceAccount`, `aws:SourceArn`), and EventBridge's cannot, because AWS does not support
those conditions for EventBridge publishing to an encrypted topic (AWS, *Managing Amazon SNS
encryption keys and costs*, <https://docs.aws.amazon.com/sns/latest/dg/sns-key-management.html>,
read 17 Nov 2026).

**Worker error codes and `trace_write_error`.** These are a separate slice for W11. They are metric
filters on the deployed worker's log group in `infra/template.yaml`. The line is this: **WS7 may
count log lines the deployed worker writes, which carry fixed codes and IDs only. It never reads
`TraceTable`, `ResultsTable` or any generated text from the live stack.**

*AWS concept:* a metric filter turns matching log lines into a number. An alarm watches a number.

### D10. From an alarm to the Safety Incident Management Log

- CloudWatch sends the alarm by email. The job also sends its own short message built to map onto
  the log's fields. It carries no note text and no model output.
  - The job sends that message **on a breach only**. A failed scheduled run sends no message of
    its own: the Thursday check and the alarms report it (the author's ruling, 20 Nov 2026).
- **An alarm is a candidate, not an incident.** The author triages it.
- **The clocks are the log's own, and they start at the alarm.** The alarm time is the report
  time for the log's existing targets: acknowledge within 2 working days, clinical risk
  assessment within 5 working days (`docs/WS4-SAFETY-INCIDENT-LOG.md:30`). Triage runs inside
  those clocks and never restarts them.
- **Triage ends in one of three outcomes:**
  1. **False alarm.** Noise, or a fault in the harness or its infrastructure. Recorded against the
     run. No log entry.
  2. **Quality finding.** A real change with no patient-safety effect. Recorded with its evidence
     and followed up as ordinary work.
  3. **Incident.** It has, or could have, a patient-safety effect. It gets an `INC-YYYY-NNN`
     entry under the log's section 2, and the author copies the run by hand to
     `retained/<run_id>/` (D8).
- The job never edits the log. It is a controlled document approved by the CSO.
- **The log's wording comes first.** The log gains this route, with CSO approval, before the
  schedule is switched on. Switching it on requires that approval (see the order of work).
- No GitHub issue is opened automatically. That would need a stored token and would publish
  alarms nobody has assessed.

An invented example of the job's message:

```
WS7 breach - target pipeline - run 2026-12-16T0400-pipeline
family safety-netting, kind 2 (per-scenario flip): 1 stable scenario now fails (S14)
system release: pipeline_version 3f9c...e1 / model anthropic.claude-sonnet-4-6 / caching on
baseline: b-2026-12-14  evidence: s3://<eval-bucket>/ws7/runs/scheduled/2026-12-16T0400-pipeline/
report time (the alarm): 2026-12-16 04:41 London
```

*AWS concept:* an SNS topic fans one message out to its subscribers.

### D11. A version stamp on every output, and a match check before any spend

**Deployed outputs** are already stamped by ADR-009: `model_version` on the audit row today;
`pipeline_version` on the audit row and, per step, `model_id` and `prompt_sha256` in the trace from
W11. This ADR adds nothing there.

**Eval outputs** get a run manifest written by the entrypoint. No runner and no file in
`src/generate/pipeline/` is changed. The manifest holds:

- the git commit and the image digest. The image has no `.git` and a container cannot hash its
  own image, so both are handed in: the commit by a build argument (`WS7_GIT_COMMIT`), the
  digest by the task definition (`WS7_IMAGE_DIGEST`), which pins the image by the same digest.
  A run with either missing is refused;
- `pipeline_version` (pipeline target) or the two v1 prompt file hashes (v1 target);
- the model ID and the caching setting;
- the corpus hash (taken from the batch manifest, which each record links to by `batch_id`), the
  gold hashes and the scorer versions;
- the baseline it was compared with, and how PART C was produced.

**The match check** runs first and costs nothing. The job compares its own values with the expected
ones and does not generate if they differ.

| Target | What must match |
|---|---|
| Pipeline | `pipeline_version`, model ID, caching setting |
| v1 | Model ID, the sha256 of `src/generate/system_prompt.md` and `patient_system_prompt.md` at the commit deployed on `main`, second pass on, caching on. v1 has no `pipeline_version`. **Safety-net gate on**: a WS7 rule, not an SSM value (below) |

**The runner's arguments are an allow-list** (Codex review of PR #6; the author's ruling,
19 Nov 2026). The check compares a few values, so the rest must not be movable. The job hands a
runner only the forms it builds itself: all 18 scenarios or the one pre-flight scenario, a batch
ID, `--yes`, and for v1 `--deployed-path`. Any other argument refuses the run before the
runner's parser sees it. So the v1 safety-net gate cannot be switched off (`--no-gate`), and the
pipeline's step 7v cannot be switched on (`--verify`). The gate is recorded in the job's own
values and in the manifest.

**Who writes the expected values, and when.** The author writes them to SSM Parameter Store by
hand. CI does not write them. The eval task can read them and cannot write them. There is one
String parameter for each target, `/discharge-eval/ws7/expected/pipeline` and
`/discharge-eval/ws7/expected/v1`, each holding strict JSON (`docs/WS7-ENTRYPOINT.md`).

- **v1 values: once, at eval-stack set-up.** v1 is already deployed, so they do not wait for a
  release. They come from the commit deployed on `main` at that time.
- **Pipeline values: at each release to `main`, starting with W11,** as a step of the release.
  They come from the release commit: `pipeline_version` computed from that commit, and `ModelId`
  and `PromptCaching` as `.github/workflows/ci-cd.yml` pins them at that commit.

**The limit.** The check proves the image agrees with the release record. It does not prove the
running Lambda does. The status endpoint does not return `pipeline_version`, so the canary cannot
close that gap today.

*AWS concept:* an ECR image digest is a hash of the image's content. SSM Parameter Store holds
small named values.

### D12. Subgroup cuts: tags now, a stratified corpus later, and separable

- The corpus has 15 adult scenarios, one child, one infant and one neonate; three adversarial
  scenarios; and about one scenario per specialty. A cut can show **where** a breach is. It cannot
  support a rate.
- **Now:** a tags file (age band, specialty, adversarial, safety-net route) applied when results
  are added up. It costs nothing, appears in the run summary and has **no alarm**.
- **January:** the stratified corpus of 40 to 60 scenarios that HAZ-05 needs.
- It is its own slice. It reads the same records, and removing it changes nothing else.

*AWS concept:* every distinct dimension value is a separate CloudWatch metric, so subgroup detail
stays out of dimensions.

### D13. PART C under both outcomes

The two outcomes are about **release** (decision S4, 9 Nov 2026):

1. PART C is issued as final only through the W10 clinician review-and-correction workflow.
2. PART C stays a labelled draft and is withheld.

**The monitoring is the same in both:** meaning-flag counts by kind against the baseline, with an
alarm on change in either direction (kind 3 in D9).

**What a breach means is different:**

| Outcome | A PART C breach is |
|---|---|
| (2) Draft, withheld | a quality finding |
| (1) Issued through the W10 workflow | a Safety Incident Management Log candidate (HAZ-17) |

The manifest records how PART C was produced and the baseline is keyed by `pipeline_version`, so a
future change in how PART C is made is a new baseline, not a redesign.

**This slice is blocked.** It uses the frozen scorer files, and it does not start until the author
says so.

## Costs

Converted at £0.80 to $1, the rate `docs/COST.md` uses. Bedrock is priced as the runners price it:
$3 and $15 per million input and output tokens, plus 10%. A worst-case reservation prices input
at the cache-write rate ($3.75 per million, plus 10%).

| Item | Expected per run | Worst-case reservation per run | Per month | Price checked |
|---|---|---|---|---|
| Bedrock, pipeline, 18 + 1 pre-flight | £0.76–2.28 | £6.62 (£5.34 enforced output + £1.29 provisional input allowance, rounded) | expected £3.30–9.90 | Repository constants; not confirmed on AWS's page |
| Bedrock, v1 until W11, 18 + 1 | up to £1.32 | £1.96 (£1.40 + £0.56) | up to £5.74 | As above |
| Baseline, 18 x 3, by hand: 54 generations, **excluding** the pre-flight | £2.16–6.48 | £18.83 | Not monthly; outside the cap | As above |
| Baseline with its pre-flight: 55 generations (ruling e8, 18 Nov 2026; **the figure used**) | £2.20–6.60 | £19.18 | Not monthly; outside the cap | As above |
| Customer-managed KMS key | n/a | n/a | £0.80 | $1 per key, AWS price list for London, 8 Nov 2026 |
| Fargate, ARM, 0.5 vCPU / 1 GB (the run and the Thursday check) | under £0.01 | n/a | under £0.05 | AWS price list for London, 11 Nov 2026 |
| Public IPv4 address | under £0.01 | n/a | about £0.01 | AWS price list for London, 5 Nov 2026 |
| CloudWatch alarms, about 12 | n/a | n/a | up to £1.00 | $0.10 each beyond the first ten; London, 16 Nov 2026 |
| Metrics, logs, S3, ECR | pennies | n/a | pennies | Metric, log and ECR rates checked; S3 not checked |
| **Total as designed** | | | **expected about £5–12; ceiling £15 a month of model spend, as worded below** | |
| *Not chosen:* NAT gateway | | | about £26 fixed | US rate; London not checked |
| *Not chosen:* five interface endpoints | | | about £32 fixed | AWS price list for London, 5 Nov 2026 |

- **Expected** figures use the range from earlier measured runs, $0.05–0.15 a generation.
- **A worst-case reservation** assumes every step of every generation runs to its `maxTokens`,
  with one attempt (D7). It is far above the expected cost because the limits are set generously:
  a truncated step fails.
- **The reservation figures are provisional.** The input allowance becomes a measured figure at
  the baseline, with the author's margin.
- **The ceiling is £15 a month of model spend.** Output is enforced by `maxTokens`. Input is not
  capped: the running-spend guard stops a run within one call of its reservation, and that
  call's input may exceed its allowance (about £0.13 by the planning figures, an estimate, not an
  enforced limit). The ledger records actual cost. Where a cost is unknown, it holds an estimate
  in an unresolved entry, which blocks later scheduled runs until the author resolves it. AWS
  Budgets is an alert only. The month's spend passes £15 by at most one call **only while every
  settled entry in it is its actual metered cost or a defensible upper bound**; an entry resolved
  as `estimate_accepted` waives that for its month (D7, points 3 and 4). The fixed items, about £2
  a month, are not in the ledger.
- **What the ceiling allows.** A run starts only while the month's ledger total is £8.38 or less.
  Three runs at the top of the expected range total £6.84, so four Wednesdays start. **In a month
  with five Wednesdays the fifth run starts only if the first four averaged £2.09 or less. At the
  top of the expected range (£9.12) it does not start.** That is the ceiling working, and the
  heartbeat reports it.
- **A run whose cost is unknown is unresolved** (an unmetered run, an abandoned reservation, or a
  run that crashed before its record was written): no later scheduled run starts until the
  author resolves it. Until then it counts at an estimate (D7 point 4; the author's ruling,
  19 Nov 2026):
  - an abandoned reservation, or a crashed run, stays at its reservation, **£6.62**;
  - an unmetered run stays at the wrapper's **counted total**: what was metered before the
    stop, plus the worst case of the call that made it unmetered. That is below £6.62 unless
    the run had all but reached its reservation.
  - Resolved as `upper_bound`, it counts at the Cost Explorer figure as D7 point 4 defines it
    (unblended; Charge type Usage only; the whole account, with no Service or Region filter; the
    run's UTC dates), and the month keeps its assurance.
  - Resolved as `estimate_accepted`, it counts at that same estimate, and **the month's assurance
    is waived**: its spend may pass £15 by more than one call.
    - An abandoned or crashed run, accepted at £6.62: one more run can start that month, and a
      third only if the second cost £1.76 or less.
    - An unmetered run, accepted at a counted total of £2.00, say: one more run can start, and
      a third only if the second cost £6.38 or less. A lower estimate leaves more room, and the
      true cost may be further above it.
- If key rotation is on, each rotated key version adds its own monthly charge.

## Consequences

**What this gives.** A weekly, costed, version-checked answer to one question: on our 18 synthetic
scenarios, has the model's behaviour moved since the baseline? Each answer is stored, stamped and
traceable to a release.

**What it does not give.** It is not a test of the deployed service: that is the canary. It does
not see settings drift, the assembled bundle, value errors with no scorer, or real-input drift
(D1). It does not see intermittent or sub-weekly change (D9). These limits are stated so that a
quiet week is not read as proof of safety.

**What the spend ceiling is.** £15 a month of model spend. Output is enforced by `maxTokens`.
Input is not capped: the running-spend guard stops a run within one call of its reservation, and
that call's input may exceed its allowance (about £0.13 by the planning figures, an estimate, not
an enforced limit). The ledger records actual cost. Where a cost is unknown, it holds an estimate
in an unresolved entry, which blocks later scheduled runs until the author resolves it with a
settled figure and its basis. AWS Budgets is an alert only, never the reconciliation. The month's
spend passes £15 by at most one call **only while every settled entry in the month is its actual
metered cost or a defensible upper bound**. A month with an entry resolved as `estimate_accepted`
has no such assurance, and its ledger, run summaries and check reports say so. It is not an exact
limit, and the figures are provisional until the baseline.

**What has to change in implementation pull requests, not this one.**

- CLAUDE.md section (b) says only two buckets exist. The eval bucket makes three. Section (c)
  gains ECS, ECR and Fargate.
- `.dockerignore` must exclude `evals/calibration`, unless a script the job runs reads it,
  **before any image is pushed to a registry**. A search of the source on 17 Nov 2026 found that
  none of the scripts the job runs, nor anything they import, names that folder; the scripts that
  do are the calibration and blind-sheet tools. The same pull request must confirm that the
  image's test command still passes, or give the job its own build target.
- The `Dockerfile` header says "No AWS calls". That stops being true.
- `PYTHONHASHSEED=0` is set in the task definition. `evals/score_batch.py` is left as it is.
- `docs/COST.md` gains the eval stack.
- `docs/WS4-SAFETY-INCIDENT-LOG.md` gains the alarm route, the rule that the alarm time is the
  report time, and the three outcomes. That is a controlled document and needs CSO approval, and
  the schedule is not switched on without it.
- Linting the new template in CI means changing the workflow, which needs the author's approval.

**The order of work.**

| # | Slice | Needs |
|---|---|---|
| 1 | Offline comparator: baseline against run, a breach per family | Nothing. No AWS, no spend, no frozen file |
| 2 | Entrypoint (client wrapper, claim, ledger, check mode) and image changes, tested offline with the runners' dry-run mode | The author's say: it runs the frozen scorer, unchanged, on fresh batches |
| 3 | `infra/eval-template.yaml`, a hand-deploy runbook, and the set-up acceptance checks below | The author's yes to deploy |
| 4 | Baseline batch, the rule numbers, the input allowance and margin, the baseline pull request | The author's yes to spend |
| 5 | Incident-log wording | CSO approval |
| 6 | Schedule switched on | Slice 5's CSO approval, and the author's yes |
| 7 | Subgroup tags | Nothing |
| 8 | Metric filters on worker logs | W11 |
| 9 | PART C metrics | Blocked until the author says |
| 10 | The orchestrator-driven runner | After R1 |

**Acceptance check for slice 2: one request per step, counted at the server.** A local HTTP stub
stands in for Bedrock and counts the requests it receives. For **both runners**, it shows exactly
one request per model step when the stub answers with a throttle, with a 500, and with a read
timeout. A stub that reports inflated input usage shows the wrapper refusing calls once the
reservation is reached. No AWS and no spend.

**Set-up acceptance checks for slice 3.** None of them needs a Bedrock call.

- **First run after deploy.** The state of the `RunOk` alarm before any success exists is known
  and expected. After the first success it returns to OK at the next London midnight.
- **A skipped Wednesday.** With the schedule off for one Wednesday, the backstop alarm and the
  Thursday check both alert. The actual times are recorded against the 21 and 30 hours above.
- **A late-finishing run.** A run that ends at the wall-clock limit still counts for Wednesday. A
  run that would pass midnight is stopped by the limit and reported as failed.
- **A duplicate delivery.** A second start for the same Wednesday exits as a duplicate and spends
  nothing.
- **The bucket, with the task role** (slice 2 part 2 was tested against local fakes only; the
  full list is in `docs/WS7-ENTRYPOINT.md`, "What the fakes cannot prove"). Real S3 answers a
  conditional write as documented on the KMS-encrypted bucket; the role can list, get and put
  under `ws7/` and can do nothing else; whether a missing key gives 404 or 403 is recorded; a
  real ETag is accepted by `If-Match`; two tasks racing for one claim leave one holder; the
  expected-version parameters can be read and not written.
- **The metrics and the topic, with the task role** (slice 2 part 5a was tested against local
  fakes only; `docs/WS7-ENTRYPOINT.md`, "What the new fakes cannot prove"). Real CloudWatch
  accepts the metric names, the dimension and the values, and the role may publish only in the
  namespace; each alarm sees its datapoints; the encrypted topic accepts the role's message and
  delivers it.
- **The clock changes.** Both schedules and the alarm window name `Europe/London`, not a fixed
  offset. In the weeks of 28 March 2027 and 31 October 2027, the Wednesday run and the Thursday
  check fire at 04:00 and 10:00 local time, and the alarm history shows no false alarm.

**Open checks for the author.**

- Whether the live `AlertsTopic` delivers alarm emails (Context, finding 5).
- Whether Bedrock's price for this model in London matches the repository's constants.
- Whether a wall clock alarm of seven one-day periods behaves as AWS documents it (D9). It is
  untested.

**Revisit this decision if:**

- real patient data is ever in scope for evaluation (the network and storage choices assume
  synthetic data);
- alarms prove noisy at 18 x 1 (move to 18 x 3 and re-cost);
- throttled runs become common under the no-retry rule (decide retries and the ceiling together:
  one retry needs a reservation of about £13.25 a run);
- a rule requires that nothing holds a public IP (move to CodeBuild or to endpoints);
- image builds move into CI (a separate ADR to widen the deploy role).
