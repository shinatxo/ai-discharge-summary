# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
`AGENTS.md` is a symlink to this file, so Codex reads the same rules.

## a) What this is

The AI Discharge Summary Assistant turns ward-round notes into three drafts for a clinician to
review and sign: a discharge summary, a GP letter and a patient version. v1 is live on `main`.
The v2.1 pipeline rebuild (ADR-009) is on `feat/agentic-pipeline` and is not live; there is no
switch between the two.

## b) Architecture

```
Browser (React SPA, Cognito sign-in)
  -> CloudFront (stack discharge-web)
       default path             -> private S3 SPA bucket (OAC)
       /generate, /generations/* -> the HTTP API (same origin, no CORS)
  -> API Gateway HTTP API, native Cognito JWT authoriser
       POST /generate           -> dispatcher Lambda (src/dispatcher)
       GET  /generations/{id}   -> status Lambda (src/status)
  -> dispatcher: writes a pending audit row and an idempotency receipt,
     async-invokes the worker, returns 202 + job_id
  -> worker Lambda (src/generate) -> Bedrock
```

Where data lives:

- **Generated outputs:** the DynamoDB results table (`<stack>-results`), 24-hour TTL. Never S3.
- **Audit:** the DynamoDB audit table (`<stack>-audit`) is hash-only: SHA-256 hashes and
  metadata, no clinical text. Its stream feeds the ledger Lambda (`src/ledger`).
- **Ledger:** an S3 bucket with Object Lock (WORM) holds a copy of every audit change.
- **Step traces:** the DynamoDB traces table (`<stack>-traces`) holds the pipeline's step
  traces. It has a TTL and is never served to the SPA.
- **S3:** only two buckets exist, the WORM ledger and the SPA bucket.
- All three tables and the ledger bucket are encrypted with one KMS customer-managed key.

Infrastructure:

- Plain CloudFormation. No SAM transform, no Terraform. `infra/samconfig.toml` is not used by CI.
- `infra/template.yaml` is the `discharge-audit` stack. `infra/web-template.yaml` is the
  `discharge-web` stack. CI deploys only `discharge-audit`; `discharge-web` is deployed by hand.
- Region `eu-west-2`. The one exception is the CloudFront viewer certificate (ACM, `us-east-1`).
- Model: `anthropic.claude-sonnet-4-6`, on-demand in `eu-west-2`. No `eu.` or `global.` prefix.
  The worker's IAM policy allows that one foundation-model ARN only.

On this branch the worker runs the ADR-009 pipeline. The step order is written in one place,
`src/generate/pipeline/orchestrate.py`, and the run is fail-closed: the first failure fails the
job and no draft is released. Step 7v (`verify_patient`, ADR-010) is in the registry but is not
run. `src/generate/README.md` describes the worker. `src/canary` replays synthetic scenarios
through the deployed path on a schedule. `evals/` holds the eval and scoring scripts.

## c) Stack

- Python 3.13 Lambdas (arm64), `boto3`; tests with `pytest` and hand-rolled AWS mocks
  (`tests/conftest.py`)
- React 18 + Vite + TypeScript SPA with Amplify Auth (`ui-spa/`)
- AWS: Bedrock, Lambda, API Gateway (HTTP API), Cognito, DynamoDB (+ Streams), S3, KMS,
  CloudFront, Route 53, ACM, EventBridge Scheduler, CloudWatch, SNS, CloudFormation
- GitHub Actions with OIDC (no stored AWS keys)

## d) Test commands

Use Python 3.13 (the `discharge-313` environment).

- Full suite: `PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q`
- One file or one test: add `tests/test_dispatcher.py` or `tests/test_dispatcher.py::test_name`
- CI also runs: `python src/canary/build_scenarios.py --check`
- CI also runs: `cfn-lint --non-zero-exit-code error infra/template.yaml infra/web-template.yaml`

The full suite cannot pass inside a locked Claude session, because some tests read
`evals/runs/` and that folder is blocked. That is expected. Claude runs focused tests only: the
tests for what changed. Say exactly which tests you ran and the result. The full suite runs in
CI and on Shina's machine. Never report a test as passed unless you ran it.

## e) Branch and PR rules

- The base branch is `feat/agentic-pipeline`.
- Never commit to, push to, or open a PR against `main`. Every push to `main` deploys the live
  stack.
- One task = one branch named `task/<short-name>` = one PR into `feat/agentic-pipeline`.
- Plan first and wait for Shina's "approved" before you write code.
- Small commits. Never force-push. Never merge; Shina does every merge.
- The PR description says: what changed and why; how it was tested (commands and results);
  which docs were updated; risks; and "What to learn from this" in two or three plain
  sentences. Link the Notion task. `.github/pull_request_template.md` has the headings.

## f) Docs rule

- In-repo docs are updated in the same PR as the code they describe.
- New architecture decisions get one file each in `docs/adr/` with Status, Context, Decision
  and Consequences. ADR-011 is reserved for the WS7 design; any other new decision takes
  ADR-012 onward.
- ADR-001 to ADR-009 stay in `docs/ADR-phase1.md`. See `docs/adr/README.md`.

## g) Data rule

- Synthetic or fully de-identified data only. This repository is public.
- Never real patient data: not in code, tests, fixtures, prompts, logs, commits, PRs or chat.
- Stop and tell Shina if you find real patient data, information that identifies a real
  person, or clinical data whose source you cannot show to be synthetic or fully de-identified.
- Invented fixtures are allowed and expected.
- R1 is stricter: its fixtures and controls must be invented, never copied or adapted from
  saved batch records.

## h) Off limits

- Do not read `evals/runs/`, `evals/calibration/` or `docs/_local/`. A plain `git status` or
  `git ls-files` can print file names from them; exclude the three folders in git commands.
- Do not change these without Shina's explicit approval in that session. They are the
  instrument of a running experiment (R1):
  - the scorer, frozen at commit `76276c9`. Six files: `evals/score_patient.py`,
    `src/generate/pipeline/meaning.py`, `src/generate/pipeline/compose_patient.py`,
    `tests/test_score_patient.py`, `tests/test_pipeline_meaning.py`,
    `tests/test_pipeline_compose_patient.py`
  - the render tables, approved at commit `3cf3847` (`src/generate/pipeline/render_tables/`)
  - the patient renderer (`src/generate/pipeline/render_patient.py`) and its offline harness
    (`evals/render_offline.py`)
  - the R1 tooling: `evals/r1_*.py`, `evals/r1/`, `tests/test_r1_*.py`, `tests/r1_fixture.py`
  - everything else in `src/generate/pipeline/`: `pipeline_version` is a hash of every `.py`
    file in that package, and the R1 freeze records it. Until Shina says the R1 run is
    finished, ask before changing any file there.
- Do no R1 work until Shina gives the R1 handoff.
- Never run the renderer or the scorer on saved batch records.

## i) No spend, no deploys

No AWS deploy, no Bedrock call and no command that costs money without Shina's explicit yes in
that session.

## j) End of every session

Finish with a three-line state note that Shina will pass to Cowork:

```
State: planning / building / in review / blocked (on what) / waiting for merge
Next: the single next step and who holds it
PR or branch: the link or name
```
