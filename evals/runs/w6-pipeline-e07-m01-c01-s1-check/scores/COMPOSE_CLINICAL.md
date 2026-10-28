# Step 6 compose_clinical — batch `w6-pipeline-e07-m01-c01-s1-check`

Step-level checks, metric v2. Definition: `evals/score_compose.py` (pass bar written 27 Oct 2026, before any step-6 code; frozen in code before the first live call).

> **Not measured here:** faithfulness of the prose to its facts (D2 beyond years, D6, D7) and narrative omission — see the CSO blind read. Bar line 8 (no regression) is read from SAFETY_NET.md, MEDS.md and `haz03_check.py`.

| Bar line | Result | Verdict |
| --- | --- | --- |
| 1 Reached step 6 (schema accepted; a failure counts as a fail everywhere) | 1/1 | **MET** |
| 2 Years added by step 6 (its own check, vs its input) | 0 in 0 generations | **MET** |
| 3a Boxes exactly once and exact in A and B; no stray {{...}} | 1/1 | **MET** |
| 3b Unknown / excluded fact_ids | 0 / 0 | **MET** |
| 4 Contradictions given to step 6 that appear in PART A | 0/0 | **MET** |
| 5 Resus / advice text outside its box (CSO-ruled defects) | flagged 0, defects 0, pending 0 | **MET** |
| 6 Field-level omission in A (documented fields) / GP actions missing from B | 0 / 0 | **MET** |
| 7 Medication text outside the box (CSO-ruled defects) | flagged 2, defects 0, pending 0 | **MET** |

## Reported, no bar

| Measure | Result |
| --- | --- |
| Fact coverage in PART A (k/n) | 26/26 |
| Unverified facts given / used (8a adds the warning) | 0 / 0 |
| Field omitted — the step's own check (v1 counted not_documented fields too) | 0 |
| Years vs the notes (rubric D2) — added by step 6 / by step 2 | 0 / 0 (in 0 generations) |
| Check `heading_wrong_part` | 0 |
| Check `heading_repeated` | 0 |
| Check `required_heading_missing` | 0 |
| Check `box_not_exact` | 0 |
| Check `stray_placeholder` | 0 |
| Check `fact_unused` | 0 |
| Check `inferred_unflagged` | 0 |
| Check `not_documented_with_content` | 0 |
| Check `conflict_section_uncited` | 0 |
| Check `resus_text_outside` | 0 |

## Latency (270 s deadline watch)

| Measure | p50 | p95 | max |
| --- | --- | --- | --- |
| Step 6 | 9.5 s | 9.5 s | 9.5 s |
| Step 2 + max(5a, 6) | 25.4 s | 25.4 s | 25.4 s |

Generations over the 200 s watch line (270 − step 7's 60 s read timeout − 10): **0**. Latency is Bedrock's reported `latencyMs` per call: it excludes network time and any throttle wait, so it is a floor on wall time, not a ceiling.

## Per scenario

| Scenario | Scored | Box problems | Years (step 6) | Contradictions in A | Field omitted | Review flags (advice / resus / medication) |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | 1 | 0 | 0 | 0/0 | 0 | 0 / 0 / 2 |
