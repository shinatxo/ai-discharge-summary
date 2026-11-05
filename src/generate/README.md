# `generate` Lambda — the worker

> **Which worker is this?** This README describes the **feature branch
> `feat/agentic-pipeline`** (WS1a step build, piece 5, 5 Nov 2026).
> **The deployed service is still v1**: `main` carries the v1 worker (one combined
> Bedrock call plus the Patient v2 second pass) and is the released configuration until
> the **W11 cut-over** merges this branch. There is **no switch** between the two
> (ADR-009 (d)): a feature-branch push deploys nothing, and nothing in `src/` reaches
> `main` before W11. For the v1 worker's behaviour, read this file on `main`.

## On this branch: the ADR-009 pipeline worker

- `app.py` — the handler. Dispatcher events only (the async 202 + poll path); an HTTP API
  event gets 410 Gone; anything else (including the old v1 direct-invoke shape) is refused
  with `unsupported_event`. For each job it:
  1. **claims** the job (sets `pipeline_version` only if the row is `pending` and unclaimed).
     A duplicate delivery exits without calling Bedrock or touching the row; an uncertain
     claim stops before any model call and is never cleared or taken over. A committed
     claim followed by a crash can leave the job pending (recorded limitation).
  2. **rejects a mismatched or missing `input_sha256`** (`input_hash_mismatch`).
  3. runs `pipeline/orchestrate.py` under a deadline — the earliest of 270 s from the
     dispatcher's `started_at`, 270 s from worker start, and Lambda's remaining time
     minus 30 s (`Timeout` is 300 s on this branch).
  4. on any failure: marks the row `failed` **immediately** with a stable `error_code`
     and one fixed message — *"No draft was released. Generation could not be completed.
     Try again or complete the summary manually."* — and releases nothing.
  5. on success (8b passed, every trace item written): one **conditional** PutItem to
     `ResultsTable` (three documents + structured units and templates for W10, 24 h TTL;
     never overwritten), then the audit flip to `complete` with 8b's hashes.
- `pipeline/` — the steps (`run_step`), `orchestrate.py` (the only place the step order
  is written: 1 → 2 → 3 → 3b → 4 → 5b, then 5a ∥ 6, pre-flight, 7 beside 8a, then 8b), and
  the step prompts under `pipeline/prompts/`. Step 7v (ADR-010) is **not** run.
- **PART C is a draft for clinician review** (banner + reviewer warnings); checklist
  item 3 is NOT MET, and pipeline PART C ships at W11 only if it is ticked.
- **Traces:** the worker needs a trace writer (checklist item 6, TraceTable). Until then
  `_make_trace_writer()` returns `None` and every job fails closed with `config_error` —
  a generation without a trace is never released.
- **Bedrock clients:** one per read timeout (90 s for steps 2 and 6, 60 s for 5a and 7),
  `total_max_attempts=1` — retries are the step's, for throttling only. (Not
  `max_attempts=1`: botocore counts that as one *retry*.)
- **What goes where.** Logs and the audit row never carry note text or model output text
  (ids, fixed codes, counts and hashes only). The **restricted trace** deliberately keeps
  intermediate step outputs and short citations (quotes ≤ 200 characters) for diagnosis,
  but never the raw notes or the line index (`orchestrate.scrub`); it is never served to
  the SPA (ADR-009 (b)).

## Still here, not used by this worker

- `system_prompt.md`, `patient_system_prompt.md` — the **v1** prompts. The branch worker
  does not load them; `evals/run_cold_eval.py` (the v1 baseline harness) still does. They
  are removed with `PatientV2SecondPass` at the W11 cut-over commit (ADR-009 F11).

## Known deferral (main, v1)

The deployed v1 worker builds its Bedrock client with `retries={"mode": "standard",
"max_attempts": 1}`, which botocore reads as **one retry**: a throttle or a read timeout
can be retried once, invisibly (150 s + 150 s > the 240 s timeout → a stuck `pending`
row, and extra requests). The comment claiming this control prevents silent retries is
wrong. **Deferred to the W11 cut-over, 14 Dec 2026 (CSO R4 (a), 5 Nov 2026); owner: the
author**, within the synthetic demonstration scope. A hotfix on `main` before then needs
its own exception, validation and release record. The branch correction does not
mitigate `main` meanwhile.

`requirements.txt` — empty by design; boto3/botocore ship with the runtime.
