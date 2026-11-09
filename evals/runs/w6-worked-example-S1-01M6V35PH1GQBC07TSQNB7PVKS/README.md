# Worked example — one synthetic generation, end to end (scenario S1)

WS1a step build piece 8 · DoD 7 (*"one worked example followed end to end"*) · exported 9 Nov 2026.

## What this is

This is a synthetic example. Scenario `S1` from the committed synthetic corpus `src/canary/scenarios.json` was run once through the deployed pipeline on the ephemeral stack `discharge-eph-20261107` (deployed from `b57f4bc`, 7 Nov 2026; torn down 7–8 Nov) as job `01M6V35PH1GQBC07TSQNB7PVKS`, and exported here from its saved trace (piece 7 evidence, `snap-1`). Traces expire after 30 days and only an operator can read them (ADR-009 (b)); this folder keeps one synthetic run readable.

## What this is not

- It is not clinical validation, evidence of safety, or a quality verdict. Scored batches and their bars are in `evals/EVAL_RESULTS.md` and the step-build records.
- It is not real patient data, and not an incident export. On a real deployment a trace may be used only to investigate its own generation (ADR-009 (b), DPIA).
- All three documents are drafts for clinician review. PART C is a draft flagged for clinician review: WS1a checklist item 3 (compose_patient) is NOT MET.
- Structural consistency does not establish faithful clinical meaning.

## Identity

| | |
|---|---|
| scenario | `S1` (notes by reference: sha256 `deacfb8fe45187fb890a33b6e0c8ad2f5e6902c825879bb20931a69f80969fa3`, 22 lines) |
| job / generation | `01M6V35PH1GQBC07TSQNB7PVKS` |
| deployed commit | `b57f4bce75abe0075d2dc4daf51a2bc72c3c9446` |
| pipeline_version | `d3df2826d8bbbdac21f62bec560889fb28bc4ce05829ba4b5bfb6d6ed940cad1` |
| model | `anthropic.claude-sonnet-4-6` · 4 requests · 4026 input / 4800 output tokens |
| run | 2026-11-07T13:29:09.105+00:00 → 2026-11-07T13:29:45.437+00:00 (UTC) |

## How to verify

```
python evals/check_worked_example.py
```

Prints `MET` or the failed codes. The checker verifies offline consistency, binding to this run and the privacy rules; it does not prove origin. Origin rests on the export record: the five saved source files' byte hashes (pinned in the checker's `BINDING` from a read-only probe of the saved evidence, before export) and `trace.json` being a byte-identical copy of the saved trace. `provenance.json` records each file's relation to its source.

## The run, step by step

SK order (one DynamoDB Query returns the run in this order). Start = ms after META.started_at; execution order comes from these timestamps, not from the step numbers.

| SK | kind | status | start ms | latency ms | model requests |
|---|---|---|---|---|---|
| `STEP#01#guard_input#A1` | code | ok | 0 | 0 | 0 |
| `STEP#02#extract_facts#A1` | model | ok | 38 | 19698 | 1 |
| `STEP#03#validate_facts#A1` | code | ok | 19751 | 1 | 0 |
| `STEP#03b#retrieve_evidence#A1` | code | ok | 19771 | 0 | 0 |
| `STEP#04#route_resus#A1` | code | ok | 19791 | 0 | 0 |
| `STEP#05a#reconcile_medications#A1` | model | ok | 19852 | 8767 | 1 |
| `STEP#05b#select_safety_net#A1` | code | ok | 19831 | 0 | 0 |
| `STEP#06#compose_clinical#A1` | model | ok | 19853 | 9021 | 1 |
| `STEP#07#compose_patient#A1` | model | ok | 28892 | 7396 | 1 |
| `STEP#08a#assemble_clinical#A1` | code | ok | 28892 | 1 | 0 |
| `STEP#08b#assemble_patient#A1` | code | ok | 36295 | 3 | 0 |

Plus `META` (the pipeline outcome: `complete`) and `DELIVERY` (the worker's record: results `written`, audit `complete`). Start-time ties: 1 (tied steps have no order between them). 5a and 6 overlap; 8a is scheduled beside step 7.

## Following one line end to end

Released PART A (`release.json` → `summary`) is rendered by 8a from step 6's sections; each A/B section lists the step-3 `fact_ids` it uses; each fact carries citations (line IDs + a quote ≤ 200 characters) with step 3's recorded verification outcome (verified, or rejected with a reason); PART C's sections list the PART A `section_id`s they re-express. The notes themselves are not here — they are in the corpus, joined by `notes_sha256`.

- Example (IDs only): PART A section `sections.0` (`presenting_complaint`) → fact `presenting_complaint.0` (step 3 recorded its citation as verified) → note line(s) `L001`.
- PART C section `why_in_hospital` → PART A sections `sections.0`, `sections.1`.

`trail.json` is the full report, recomputed by the checker from `trace.json` on every run:

- **Facts → note lines (step 3):** step 3 recorded 38 of 38 citations as verified and 0 item(s) as unverified. Rejected citations are kept as saved, with the producer's reasons (none) — diagnostics, not repaired.
- **A/B sections → facts (step 6):** 49 references resolved to facts step 6 was given; 0 fact(s) held back by the resus guard; reference-resolution flags (unknown or excluded IDs): 0; boxes with no references by design: 6.
- **C sections → A sections (step 7):** 6 references resolved; reference-resolution flags (unknown source IDs): 0; boxes: 2. These counts are reference diagnostics only — step 7's own meaning checks and 8b's PART C review warnings are separate and are kept (the PART C banner and `release.json` → `warnings_json`).

Structural resolution says each reference points at something that exists, as the producer recorded it. Whether each sentence says what its sources say is a clinician question.

## The release

Release truth is the audit row (`status` complete) plus the ResultsTable item — META alone does not prove release. Each document's sha256 is recorded three times, independently — the audit row (`audit.json` → `output_sha256`), the ResultsTable item (`release.json` → `output_sha256_json`) and 8b in the trace (`STEP#08b` → `output.hashes`) — and the checker recomputes all three from the documents.

- PART A (`summary`): `75051c785e1cf0d0fe68a99d9cae0578fb25f1ba20001fff37992381c95ca1ba`
- PART B (`gp_letter`): `2867d5a271bc20b3eabf714c47f3d2874e6a4c1ba887d26b6b674a1f5dc66219`
- PART C (`patient`): `1af46c00ef96f313cbab7692d10ba6efb0770034798aa88b17420194d08a639b`

## Left out, and why

- `user_sub` and the clinician linkage (`PK`): a trace never holds it (ADR-009 (b)); the audit row that links a run to a clinician stays in AWS.
- The idempotency key, tokens and passwords; log events and worker logs (CloudWatch content, request IDs, function names); account IDs, ARNs, the StackId, key IDs and the endpoint.
- The ResultsTable `ttl` (its own 24-hour bound, not the trace's 30 days).

## CSO read

- Recorded S1 step-2 temporality defect (status doc, carried open): **present in this run.** Note line L001 gives a 2-day history ("x2/7"); step 2 recorded "for 2 weeks"; step 3 recorded the citation as verified because the quoted words are in L001 (the check compares the quote, not the fact's value); PART A and PART B carry "2 weeks"; PART C gives no duration. Read by Shina (author/CSO), 9 Nov 2026. Recorded, not fixed.
