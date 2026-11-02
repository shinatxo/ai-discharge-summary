# Step 7 compose_patient — batch `w6-pipeline-e09-m01-c02-p01-s1-check`

Step-level checks, metric v1. Definition: `evals/score_patient.py` (pass bar written 1 Nov 2026 before any step-7 code; frozen in code before the first live call). PART C is assembled by `pipeline/render.py`, as 8b will.

> **Not measured here:** whether C says the same things as A (HAZ-17), withholding judgement, tone — see the CSO blind read (bar line 10, `make_patient_sheets.py`). Bar line 11's other measures are in SAFETY_NET.md, MEDS.md, STEP2_ADVICE.md and COMPOSE_CLINICAL.md.

| Bar line | Result | Verdict |
| --- | --- | --- |
| 1 Reached step 7 (schema accepted; a failure counts as a fail everywhere) | 1/1 | **MET** |
| 2 C boxes exactly once and exact; no stray {{...}}; unknown source_ids | 1/1; unknown 0 | **MET** |
| 3 Signpost sentences outside {{SAFETY_NET}}; gate on assembled A + C | 0; gate pass 1/1 | **MET** |
| 4 Advice-shaped sentences outside the box (CSO-ruled defects) | flagged 0, defects 0, pending 0 | **MET** |
| 5 Resus text in step 7 (code) / dose, frequency, unsourced drug (CSO-ruled defects) | resus 0; flagged 0, defects 0, pending 0 | **MET** |
| 6 Years added vs step 7's input (code) / numbers, units added (CSO-ruled defects) | years 0; flagged 0, defects 0, pending 0 | **MET** |
| 7 FK grade <= 8 on step 7's prose (boxes excluded) | 1/1 (p50 6.82, max 6.82) | **MET** |
| 8 Translation header where expected / elsewhere; non-English sentences in C | 0/0; elsewhere 0; non-English 0 | **MET** |
| 9 Withholding / coverage: unknown sources, 'Not documented', follow-up missing, title audience; sensitive / conflict (CSO-ruled defects) | 0 / 0 / 0 / title 1/1; flagged 0, defects 0, pending 0 | **MET** |
| 11 HAZ-03 on assembled C (expected pinned line verbatim, other line absent) | 1/1 | **MET** |
| 12 Generations where step 7 would start after the 200 s watch line | 0 (start max 20.662 s; full path max 26.727 s vs 270 s) | **MET** |

## Reported, no bar

| Measure | Result |
| --- | --- |
| Assembled C (5a and 5b present) | 1/1 |
| Gate statuses | documented_advice 1 |
| FK on the whole assembled C (incl. boxes and verbatim advice) p50 / max | 5.97 / 5.97 |
| Possible non-English sentences (one weak signal) | 0 |
| Years vs the notes in step-7 prose (rubric D2) | 0 |
| PART A risk_assessment sections not given to step 7 (by construction) | 0 |
| C sections drawing on an A section with unverified facts (8b's warning) | 0 |
| Words of step-7 prose, p50 | 184 |
| Check `heading_repeated` | 0 |
| Check `heading_out_of_order` | 0 |
| Check `required_heading_missing` | 0 |
| Check `heading_without_source` | 0 |
| Check `box_not_exact` | 0 |
| Check `stray_placeholder` | 0 |
| Check `source_unused` | 0 |
| Check `advice_shaped` | 0 |
| Check `dose_or_frequency` | 0 |
| Check `number_added` | 0 |
| Check `unit_added` | 0 |
| Check `conflict_text` | 0 |
| Check `sensitive_text` | 0 |

## Latency (270 s deadline watch)

| Measure | p50 | max |
| --- | --- | --- |
| Step 7 | 6.1 s | 6.1 s |
| Step 2 + max(5a, 6) + 7 | 26.7 s | 26.7 s |

Step 7 start after 200 s: **0**; full path over 270 s: **0**. Bedrock `latencyMs` per call — a floor on wall time.

## Per scenario

| Scenario | Scored | Box problems | FK prose (max) | Gate pass | Header | Review flags (advice / meds+drug / number+unit / conflict+sensitive) |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | 1 | 0 | 6.82 | 1 | 0 | 0 / 0 / 0 / 0 |
