# ADR-011 — WS7: scheduled re-evaluation and drift alarms

## Status

**Proposed** — 16 Nov 2026. Decisions by the author, 16 Nov 2026.

This is a design. Nothing in it is built, deployed or run. Every deploy, and every run that costs
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
  records. Each record carries prompt, corpus and notes hashes, token counts and an estimated
  cost. Both runners refuse to start inside a canary window, because the Bedrock quota (25
  requests a minute) is shared across the account.
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
   periods, for periods of an hour or more). A weekly job sits exactly on that limit.

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
2. It runs the assembly steps directly. It is not the worker's orchestrator. That gap belongs to
   the later runner described below.
3. It also reports meaning warnings on PART C. Those numbers are held back with the PART C slice
   (D13). The first build publishes only the outcomes, the rejections by step and code, the gate
   status and the count of placeholders left.

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
  metrics in one namespace; read and write the eval bucket; read the expected-version parameters;
  publish to the eval topic. It has **no permission on any `discharge-audit` resource**.
- A NAT gateway or interface endpoints would add a fixed monthly cost many times the cost of the
  runs themselves (see Costs).

*AWS concept:* a public IP is not reachability. A security group with no inbound rules drops
everything unsolicited.

### D7. Schedule, pre-flight, cap and baseline

- **Schedule:** weekly, all 18 scenarios once, Wednesday 04:00 Europe/London. That is clear of
  both canary windows.
- **Pre-flight:** before the 18, one scenario runs alone as its own batch. The run continues only
  if it finished without error, wrote only the files expected, and cost no more than the bound.
  (The orchestrator has a function called `_preflight`. That is a different thing.)
- **No retries.** The schedule's retry policy is zero, the entrypoint has a wall-clock limit, and a
  runner that refuses to start (a canary window, an existing batch folder) is a failed run.
- **Cap, in three layers:**
  1. By construction: a fixed number of generations, each bounded by `maxTokens`.
  2. Before it starts, the job adds up the estimated cost in its own run manifests for the month.
     If that plus this run's upper bound is above **£15**, it does not start, and says so.
  3. An AWS Budgets email as a backstop. It lags by hours and warns; it does not stop anything.
- **Baseline:** one batch of 18 scenarios three times, run **by hand**, at set-up and at each
  version change. Each needs the author's approval and sits **outside** the scheduled cap. The
  baseline is not built from weekly runs with the alarms off.

*AWS concept:* EventBridge Scheduler is cron with time zones and a retry policy. Bedrock has no
spend cap of its own, so the cap has to be ours.

### D8. Run records go to an S3 bucket owned by the eval stack

- The bucket is created by the eval stack. It does not wait for the private evidence store.
- Encrypted with the eval stack's own key (D9). Block Public Access on.
- Scheduled runs expire after **90 days**. Baseline batches are kept.
- **A run that becomes an incident is copied to a retained prefix before the 90 days are up.** That
  copy is a step in triage (D10).
- What is stored: the run manifest, the batch records (model output on synthetic notes), the
  scores, the assembly report and the comparison result. No real data.

*AWS concept:* an S3 lifecycle rule deletes objects on a clock.

### D9. Metrics, the breach rule, the heartbeat and the alert topic

**Where drift is decided.** The job compares each run with a frozen baseline and publishes one
breach value (0 or 1) per family and kind. The CloudWatch alarms are then simple: breach above 0.
Per-scenario detail goes to the run summary in S3, not into metric dimensions.

**Metric families** (namespace `DischargeAssistant/Eval`, dimension `Target`):

| Family | Source | Examples |
|---|---|---|
| Run | the entrypoint | step failures by fixed code, p90 elapsed time, tokens, estimated cost |
| Safety-netting | `score_batch.py` | invented seek-help triggers, pinned line missing, gate failures, verdict per scenario |
| Medications (D4 metric) | `score_batch.py` | invented doses, invented frequencies, verdict per scenario |
| Step-2 advice | `score_batch.py` | route correct per scenario, gold items recalled |
| Composition (step 6) | `score_batch.py` | check counts |
| HAZ-03 | `haz03_check.py` | failures, verdict per scenario |
| Assembly | `score_assemble.py` | 8a and 8b rejections by code, gate status, placeholders left |
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

**The heartbeat covers three failures.**

| Failure | How it is seen |
|---|---|
| **Ran and failed** | An EventBridge rule on `ECS Task State Change` for the eval cluster: the task is `STOPPED` and a container's `exitCode` is not 0, or the `stopCode` is `TaskFailedToStart` |
| **Never started** | Alarms on `TargetErrorCount` and `InvocationDroppedCount` in `AWS/Scheduler` for the eval schedule group. AWS delivers these metrics on a best-effort basis, which is why the next row exists |
| **Silent absence** | An alarm on the job's own `RunOk` metric: seven one-day periods, missing data treated as breaching. **This sits exactly on CloudWatch's seven-day limit.** It fires about a day after a missed run, it cannot be widened, and it must be tested at set-up |

**The alert topic.** The eval stack has its own SNS topic, encrypted with a customer-managed key
owned by the eval stack. The key policy lets `cloudwatch.amazonaws.com` and `events.amazonaws.com`
use the key. It does not use `alias/aws/sns` (Context, finding 5). The same key encrypts the eval
bucket, so the stack has one key, not two.

**Worker error codes and `trace_write_error`.** These are a separate slice for W11. They are metric
filters on the deployed worker's log group in `infra/template.yaml`. The line is this: **WS7 may
count log lines the deployed worker writes, which carry fixed codes and IDs only. It never reads
`TraceTable`, `ResultsTable` or any generated text from the live stack.**

*AWS concept:* a metric filter turns matching log lines into a number. An alarm watches a number.

### D10. From an alarm to the Safety Incident Management Log

- CloudWatch sends the alarm by email. The job also sends its own short message built to map onto
  the log's fields. It carries no note text and no model output.
- **An alarm is a candidate, not an incident.** The author triages it within **N working days of
  the alarm** (the author sets N before the first live run).
- **Triage ends in one of three outcomes:**
  1. **False alarm.** Noise, or a fault in the harness or its infrastructure. Recorded against the
     run. No log entry.
  2. **Quality finding.** A real change with no patient-safety effect. Recorded with its evidence
     and followed up as ordinary work.
  3. **Incident.** It has, or could have, a patient-safety effect. It gets an `INC-YYYY-NNN`
     entry under the log's section 2, and the run is copied to retained storage (D8).
- The job never edits the log. It is a controlled document approved by the CSO.
- No GitHub issue is opened automatically. That would need a stored token and would publish
  alarms nobody has assessed.

An invented example of the job's message:

```
WS7 breach - target pipeline - run 2026-12-16T0400
family safety-netting, kind 2 (per-scenario flip): 1 stable scenario now fails (S14)
system release: pipeline_version 3f9c...e1 / model anthropic.claude-sonnet-4-6 / caching on
baseline: b-2026-12-14  evidence: s3://<eval-bucket>/ws7/runs/2026-12-16T0400/
triage due: <alarm date + N working days>
```

*AWS concept:* an SNS topic fans one message out to its subscribers.

### D11. A version stamp on every output, and a match check before any spend

**Deployed outputs** are already stamped by ADR-009: `model_version` on the audit row today;
`pipeline_version` on the audit row and, per step, `model_id` and `prompt_sha256` in the trace from
W11. This ADR adds nothing there.

**Eval outputs** get a run manifest written by the entrypoint. No runner and no file in
`src/generate/pipeline/` is changed. The manifest holds:

- the git commit and the image digest;
- `pipeline_version` (pipeline target) or the two v1 prompt file hashes (v1 target);
- the model ID and the caching setting;
- the corpus hash, the gold hashes and the scorer versions;
- the baseline it was compared with, and how PART C was produced.

**The match check** runs first and costs nothing. The job compares its own values with the expected
ones and does not generate if they differ.

| Target | What must match |
|---|---|
| Pipeline | `pipeline_version`, model ID, caching setting |
| v1 | Model ID, the sha256 of `src/generate/system_prompt.md` and `patient_system_prompt.md` at the commit deployed on `main`, second pass on, caching on. v1 has no `pipeline_version` |

**Who writes the expected values, and when.** The author writes them to SSM Parameter Store by
hand, as a step of each release to `main`, starting with W11. The values come from the release
commit: `pipeline_version` computed from that commit, and `ModelId` and `PromptCaching` as
`.github/workflows/ci-cd.yml` pins them at that commit. CI does not write them. The eval task can
read them and cannot write them.

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
$3 and $15 per million input and output tokens, plus 10%.

| Item | Per run | Per month | Price checked |
|---|---|---|---|
| Bedrock, pipeline, 18 + 1 pre-flight | £0.76–2.28 (bound £3.03) | £3.30–9.90 (bound £13.15) | Repository constants; not confirmed on AWS's page |
| Bedrock, v1 until W11, 18 + 1 | up to £1.32 | up to £5.74 | As above |
| Baseline batch, 18 x 3, by hand | £2.16–6.48 (bound £8.62) | Not monthly; outside the cap | As above |
| Customer-managed KMS key | n/a | £0.80 | $1 per key, AWS price list for London, 8 Nov 2026 |
| Fargate, ARM, 0.5 vCPU / 1 GB | under £0.01 | under £0.05 | AWS price list for London, 11 Nov 2026 |
| Public IPv4 address | under £0.01 | about £0.01 | AWS price list for London, 5 Nov 2026 |
| CloudWatch alarms, about 12 | n/a | up to £1.00 | $0.10 each beyond the first ten; London, 16 Nov 2026 |
| Metrics, logs, S3, ECR | pennies | pennies | Metric, log and ECR rates checked; S3 not checked |
| **Total as designed** | | **about £5–12 (bound about £15)** | |
| *Not chosen:* NAT gateway | | about £26 fixed | US rate; London not checked |
| *Not chosen:* five interface endpoints | | about £32 fixed | AWS price list for London, 5 Nov 2026 |

- The per-generation range ($0.05–0.15) is from earlier measured runs. The bound uses the runner's
  own planning figure of about $0.20.
- **In a month with five Wednesdays, at the bound, the fifth run would pass the £15 cap and would
  not start.** That is the cap working, and the heartbeat reports it.
- If key rotation is on, each rotated key version adds its own monthly charge.

## Consequences

**What this gives.** A weekly, costed, version-checked answer to one question: on our 18 synthetic
scenarios, has the model's behaviour moved since the baseline? Each answer is stored, stamped and
traceable to a release.

**What it does not give.** It is not a test of the deployed service: that is the canary. It does
not see settings drift, the assembled bundle, value errors with no scorer, or real-input drift
(D1). It does not see intermittent or sub-weekly change (D9). These limits are stated so that a
quiet week is not read as proof of safety.

**What has to change in implementation pull requests, not this one.**

- CLAUDE.md section (b) says only two buckets exist. The eval bucket makes three. Section (c)
  gains ECS, ECR and Fargate.
- `.dockerignore` must exclude `evals/calibration` and anything else the job does not run,
  **before any image is pushed to a registry**.
- The `Dockerfile` header says "No AWS calls". That stops being true.
- `PYTHONHASHSEED=0` is set in the task definition. `evals/score_batch.py` is left as it is.
- `docs/COST.md` gains the eval stack.
- `docs/WS4-SAFETY-INCIDENT-LOG.md` gains the alarm route, the triage time and the three outcomes.
  That is a controlled document and needs CSO approval.
- Linting the new template in CI means changing the workflow, which needs the author's approval.

**The order of work.**

| # | Slice | Needs |
|---|---|---|
| 1 | Offline comparator: baseline against run, a breach per family | Nothing. No AWS, no spend, no frozen file |
| 2 | Entrypoint and image changes, tested with the runners' dry-run mode | The author's say: it runs the frozen scorer, unchanged, on fresh batches |
| 3 | `infra/eval-template.yaml` and a hand-deploy runbook | The author's yes to deploy |
| 4 | Baseline batch, the rule numbers, the baseline pull request | The author's yes to spend |
| 5 | Schedule switched on | The author's yes |
| 6 | Incident-log wording | CSO approval |
| 7 | Subgroup tags | Nothing |
| 8 | Metric filters on worker logs | W11 |
| 9 | PART C metrics | Blocked until the author says |
| 10 | The orchestrator-driven runner | After R1 |

**Open checks for the author.**

- Whether the live `AlertsTopic` delivers alarm emails (Context, finding 5).
- Whether Bedrock's price for this model in London matches the repository's constants.

**Revisit this decision if:**

- real patient data is ever in scope for evaluation (the network and storage choices assume
  synthetic data);
- alarms prove noisy at 18 x 1 (move to 18 x 3 and re-cost);
- a rule requires that nothing holds a public IP (move to CodeBuild or to endpoints);
- image builds move into CI (a separate ADR to widen the deploy role).
