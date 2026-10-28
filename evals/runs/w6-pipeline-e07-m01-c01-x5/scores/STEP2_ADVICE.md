# Step 2 documented advice — batch `w6-pipeline-e07-m01-c01-x5`

Step-level metric for extract_facts, metric v1. Definition: `evals/score_step2_advice.py` (matching by cited line ID; no text read or written).

| Measure | Result |
| --- | --- |
| Records scored | 90 of 90 |
| Route correct (verified items, = 5b's route) | 77/90 (86%, 95% CI 77–91%) |
| Route errors — not extracted | 7 |
| Route errors — lost at verification | 1 |
| Route errors — non-advice as advice | 5 |
| Right route, wrong item | 2 |
| As extracted (step 2) | item recall 56/85 (66%, 95% CI 55–75%); precision 50/59 |
| **After step 3 (verified)** | **item recall 56/85 (66%, 95% CI 55–75%); precision 48/56** |

## Per scenario

| Scenario | Gold route | Route correct | Not extracted | Lost at verification | Non-advice | Verified recall |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| S2 | documented | 5/5 | 0 | 0 | 0 | 10/10 |
| S3 | documented | 3/5 | 1 | 1 | 0 | 3/10 |
| S4 | documented | 5/5 | 0 | 0 | 0 | 5/5 |
| S8 | documented | 5/5 | 0 | 0 | 0 | 5/5 |
| S9 | documented | 2/5 | 3 | 0 | 0 | 0/10 |
| S10 | documented | 5/5 | 0 | 0 | 0 | 5/5 |
| S11 | documented | 5/5 | 0 | 0 | 0 | 5/10 |
| S12 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| S13 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| S14 | documented | 5/5 | 0 | 0 | 0 | 5/5 |
| S15 | documented | 2/5 | 3 | 0 | 0 | 3/10 |
| S16 | fallback | 0/5 | 0 | 0 | 5 | 0/0 |
| S17 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| S18 | documented | 5/5 | 0 | 0 | 0 | 15/15 |
| A5 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| B6 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
| C7 | fallback | 5/5 | 0 | 0 | 0 | 0/0 |
