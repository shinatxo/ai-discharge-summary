# Step 2 documented advice — batch `w6-pipeline-e09-m01-c02-x5`

Step-level metric for extract_facts, metric v1. Definition: `evals/score_step2_advice.py` (matching by cited line ID; no text read or written).

| Measure | Result |
| --- | --- |
| Records scored | 90 of 90 |
| Route correct (verified items, = 5b's route) | 77/90 (86%, 95% CI 77–91%) |
| Route errors — not extracted | 6 |
| Route errors — lost at verification | 2 |
| Route errors — non-advice as advice | 5 |
| Right route, wrong item | 0 |
| As extracted (step 2) | item recall 57/85 (67%, 95% CI 57–76%); precision 52/63 |
| **After step 3 (verified)** | **item recall 57/85 (67%, 95% CI 57–76%); precision 52/61** |

## Per scenario

| Scenario | Gold route | Route correct | Not extracted | Lost at verification | Non-advice | Verified recall |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| S2 | documented | 5/5 | 0 | 0 | 0 | 10/10 |
| S3 | documented | 3/5 | 0 | 2 | 0 | 3/10 |
| S4 | documented | 5/5 | 0 | 0 | 0 | 5/5 |
| S8 | documented | 5/5 | 0 | 0 | 0 | 5/5 |
| S9 | documented | 3/5 | 2 | 0 | 0 | 3/10 |
| S10 | documented | 5/5 | 0 | 0 | 0 | 5/5 |
| S11 | documented | 5/5 | 0 | 0 | 0 | 5/10 |
| S12 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| S13 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| S14 | documented | 5/5 | 0 | 0 | 0 | 5/5 |
| S15 | documented | 1/5 | 4 | 0 | 0 | 1/10 |
| S16 | fallback | 0/5 | 0 | 0 | 5 | 0/0 |
| S17 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| S18 | documented | 5/5 | 0 | 0 | 0 | 15/15 |
| A5 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| B6 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| C7 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
