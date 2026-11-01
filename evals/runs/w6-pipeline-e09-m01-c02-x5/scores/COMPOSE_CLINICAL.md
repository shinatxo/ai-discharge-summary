# Step 6 compose_clinical — batch `w6-pipeline-e09-m01-c02-x5`

Step-level checks, metric v3. Definition: `evals/score_compose.py` (pass bar written 27 Oct 2026, before any step-6 code; frozen in code before the first live call). v3 (1 Nov 2026, before the follow-up batch): a resus fact held back by step 6's guard counts as present in PART A only if its verified lines are in the RESUS block.

> **Not measured here:** faithfulness of the prose to its facts (D2 beyond years, D6, D7) and narrative omission — see the CSO blind read. Bar line 8 (no regression) is read from SAFETY_NET.md, MEDS.md and `haz03_check.py`.

| Bar line | Result | Verdict |
| --- | --- | --- |
| 1 Reached step 6 (schema accepted; a failure counts as a fail everywhere) | 90/90 | **MET** |
| 2 Years added by step 6 (its own check, vs its input) | 0 in 0 generations | **MET** |
| 3a Boxes exactly once and exact in A and B; no stray {{...}} | 90/90 | **MET** |
| 3b Unknown / excluded fact_ids | 0 / 0 | **MET** |
| 4 Contradictions given to step 6 that appear in PART A | 6/6 | **MET** |
| 5 Resus / advice text outside its box (CSO-ruled defects) | flagged 0, defects 0, pending 0 | **MET** |
| 6 Field-level omission in A (documented fields) / GP actions missing from B | 0 / 0 | **MET** |
| 7 Medication text outside the box (CSO-ruled defects) | flagged 252, defects 0, pending 0 | **MET** |

## Reported, no bar

| Measure | Result |
| --- | --- |
| Fact coverage in PART A (k/n) — facts step 6 was given | 1587/1587 |
| Fact coverage incl. held-back facts present in the RESUS block (v3) | 1593/1593 |
| Resus facts held back by step 6's guard / present in the RESUS block / not in it | 6 / 6 / 0 (in 6 generations) |
| Unverified facts given / used (8a adds the warning) | 41 / 36 |
| Field omitted — the step's own check (v1 counted not_documented fields too) | 0 |
| Years vs the notes (rubric D2) — added by step 6 / by step 2 | 0 / 0 (in 0 generations) |
| Check `heading_wrong_part` | 0 |
| Check `heading_repeated` | 0 |
| Check `required_heading_missing` | 0 |
| Check `box_not_exact` | 0 |
| Check `stray_placeholder` | 0 |
| Check `fact_unused` | 0 |
| Check `inferred_unflagged` | 1 |
| Check `not_documented_with_content` | 0 |
| Check `conflict_section_uncited` | 0 |
| Check `resus_text_outside` | 0 |
| Check `section_held_back` | 0 |

## Latency (270 s deadline watch)

| Measure | p50 | p95 | max |
| --- | --- | --- | --- |
| Step 6 | 7.4 s | 9.0 s | 9.4 s |
| Step 2 + max(5a, 6) | 19.1 s | 22.7 s | 23.6 s |

Generations over the 200 s watch line (270 − step 7's 60 s read timeout − 10): **0**. Latency is Bedrock's reported `latencyMs` per call: it excludes network time and any throttle wait, so it is a floor on wall time, not a ceiling.

## Per scenario

| Scenario | Scored | Box problems | Years (step 6) | Contradictions in A | Field omitted | Review flags (advice / resus / medication) |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 11 |
| S2 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 0 |
| S3 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 25 |
| S4 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 0 |
| S8 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 0 |
| S9 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 10 |
| S10 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 15 |
| S11 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 6 |
| S12 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 0 |
| S13 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 26 |
| S14 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 10 |
| S15 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 15 |
| S16 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 40 |
| S17 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 60 |
| S18 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 0 |
| A5 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 14 |
| B6 | 5 | 0 | 0 | 6/6 | 0 | 0 / 0 / 20 |
| C7 | 5 | 0 | 0 | 0/0 | 0 | 0 / 0 / 0 |
