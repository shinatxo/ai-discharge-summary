# Step 7 compose_patient — batch `w6-pipeline-e09-m01-c02-p04-x5`

Step-level checks, metric v3. Definition: `evals/score_patient.py` (pass bar written 1 Nov 2026 before any step-7 code; frozen in code before the first live call). PART C is assembled by `pipeline/render.py`, as 8b will.

> **Not measured here:** whether C says the same things as A (HAZ-17) beyond line 14's fixed word lists, withholding judgement, tone — see the CSO blind read (bar line 10, `make_patient_sheets.py`). Bar line 11's other measures are in SAFETY_NET.md, MEDS.md, STEP2_ADVICE.md and COMPOSE_CLINICAL.md.

| Bar line | Result | Verdict |
| --- | --- | --- |
| 1 Reached step 7 (schema accepted; a failure counts as a fail everywhere) | 90/90 | **MET** |
| 2 C boxes exactly once and exact; no stray {{...}}; unknown source_ids | 90/90; unknown 0 | **MET** |
| 3 Signpost sentences outside {{SAFETY_NET}}; gate on assembled A + C | 0; gate pass 90/90 | **MET** |
| 4 Advice-shaped sentences outside the box (CSO-ruled defects) | flagged 25, defects 0, pending 0 | **MET** |
| 5 Resus text in step 7 (code) / dose, frequency, unsourced drug (CSO-ruled defects) | resus 0; flagged 29, defects 0, pending 0 | **MET** |
| 6 Years added vs step 7's input (code) / numbers, units added (CSO-ruled defects) | years 0; flagged 0, defects 0, pending 0 | **MET** |
| 7 FK grade <= 8 on step 7's prose (boxes excluded) | 90/90 (p50 5.63, max 7.65) | **MET** |
| 8 Translation header where expected / elsewhere; non-English sentences in C | 5/5; elsewhere 0; non-English 0 | **MET** |
| 9 Withholding / coverage: unknown sources, 'Not documented', follow-up missing, title audience; sensitive / conflict (CSO-ruled defects) | 0 / 0 / 0 / title 90/90; flagged 22, defects 0, pending 0 | **MET** |
| 11 HAZ-03 on assembled C (expected pinned line verbatim, other line absent) | 90/90 | **MET** |
| 12 Generations where step 7 would start after the 200 s watch line | 0 (start max 23.629 s; full path max 30.232 s vs 270 s) | **MET** |
| 13 Step-7 prose sections citing no section step 7 was given (code) | 0 | **MET** |
| 14 Meaning flags: cause, certainty, qualifier, test term, diagnosis (CSO-ruled defects) | flagged 184, defects 37, pending 0 | **NOT MET** |

## Reported, no bar

| Measure | Result |
| --- | --- |
| Assembled C (5a and 5b present) | 90/90 |
| Gate statuses | clean 40, documented_advice 50 |
| FK on the whole assembled C (incl. boxes and verbatim advice) p50 / max | 5.3 / 6.93 |
| Possible non-English sentences (one weak signal) | 0 |
| Years vs the notes in step-7 prose (rubric D2) | 0 |
| PART A risk_assessment sections not given to step 7 (by construction) | 5 |
| C sections drawing on an A section with unverified facts (8b's warning) | 24 |
| Words of step-7 prose, p50 | 181 |
| Step 7 refused: `no_sections_given` (F1; counted as not reached in line 1) | 0 |
| Promise wording (F5; report only, CSO-ruled for the record) | flagged 10, defects 4, pending 0 |
| Check `heading_repeated` | 0 |
| Check `heading_out_of_order` | 0 |
| Check `required_heading_missing` | 0 |
| Check `heading_without_source` | 2 |
| Check `box_not_exact` | 0 |
| Check `stray_placeholder` | 0 |
| Check `source_unused` | 0 |
| Check `advice_shaped` | 25 |
| Check `dose_or_frequency` | 24 |
| Check `number_added` | 0 |
| Check `unit_added` | 0 |
| Check `conflict_text` | 1 |
| Check `sensitive_text` | 21 |

## Latency (270 s deadline watch)

| Measure | p50 | max |
| --- | --- | --- |
| Step 7 | 4.4 s | 7.0 s |
| Step 2 + max(5a, 6) + 7 | 24.0 s | 30.2 s |

Step 7 start after 200 s: **0**; full path over 270 s: **0**. Bedrock `latencyMs` per call — a floor on wall time.

## Per scenario

| Scenario | Scored | Box problems | FK prose (max) | Gate pass | Header | Review flags (advice / meds+drug / number+unit / conflict+sensitive) | Promise (no bar) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | 5 | 0 | 7.1 | 5 | 0 | 0 / 10 / 0 / 0 | 0 |
| S2 | 5 | 0 | 6.06 | 5 | 0 | 2 / 0 / 0 / 0 | 0 |
| S3 | 5 | 0 | 6.1 | 5 | 0 | 0 / 0 / 0 / 21 | 0 |
| S4 | 5 | 0 | 4.94 | 5 | 0 | 0 / 0 / 0 / 0 | 0 |
| S8 | 5 | 0 | 5.93 | 5 | 0 | 0 / 0 / 0 / 0 | 0 |
| S9 | 5 | 0 | 5.6 | 5 | 0 | 6 / 0 / 0 / 0 | 5 |
| S10 | 5 | 0 | 7.22 | 5 | 0 | 4 / 4 / 0 / 0 | 0 |
| S11 | 5 | 0 | 7.65 | 5 | 0 | 5 / 0 / 0 / 0 | 0 |
| S12 | 5 | 0 | 5.1 | 5 | 0 | 5 / 0 / 0 / 0 | 0 |
| S13 | 5 | 0 | 7.23 | 5 | 0 | 0 / 0 / 0 / 0 | 0 |
| S14 | 5 | 0 | 6.65 | 5 | 0 | 0 / 0 / 0 / 0 | 1 |
| S15 | 5 | 0 | 6.36 | 5 | 0 | 0 / 0 / 0 / 0 | 0 |
| S16 | 5 | 0 | 6.4 | 5 | 0 | 0 / 5 / 0 / 1 | 4 |
| S17 | 5 | 0 | 5.65 | 5 | 0 | 0 / 5 / 0 / 0 | 0 |
| S18 | 5 | 0 | 5.54 | 5 | 0 | 3 / 0 / 0 / 0 | 0 |
| A5 | 5 | 0 | 6.69 | 5 | 0 | 0 / 0 / 0 / 0 | 0 |
| B6 | 5 | 0 | 4.84 | 5 | 0 | 0 / 5 / 0 / 0 | 0 |
| C7 | 5 | 0 | 4.7 | 5 | 5 | 0 / 0 / 0 / 0 | 0 |

## Per scenario — meaning flags (bar line 14, v3)

| Scenario | cause | certainty | qualifier | test_term | diagnosis |
| --- | --- | --- | --- | --- | --- |
| S1 | 4 | 0 | 5 | 5 | 4 |
| S2 | 3 | 0 | 1 | 0 | 0 |
| S3 | 0 | 2 | 1 | 0 | 5 |
| S4 | 13 | 3 | 1 | 7 | 0 |
| S8 | 1 | 1 | 4 | 0 | 0 |
| S9 | 0 | 0 | 0 | 0 | 0 |
| S10 | 23 | 0 | 0 | 0 | 2 |
| S11 | 1 | 1 | 0 | 3 | 0 |
| S12 | 5 | 3 | 4 | 1 | 3 |
| S13 | 17 | 4 | 2 | 0 | 1 |
| S14 | 5 | 8 | 4 | 0 | 0 |
| S15 | 4 | 1 | 0 | 0 | 0 |
| S16 | 8 | 7 | 5 | 0 | 0 |
| S17 | 0 | 5 | 0 | 0 | 0 |
| S18 | 5 | 0 | 0 | 0 | 1 |
| A5 | 0 | 0 | 0 | 0 | 0 |
| B6 | 1 | 0 | 0 | 0 | 0 |
| C7 | 0 | 0 | 0 | 0 | 0 |
