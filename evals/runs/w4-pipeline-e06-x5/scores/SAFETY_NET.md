# Safety-netting correctness — batch `w4-pipeline-e06-x5`

Step-level metric (WS1a DoD 5), scorer v2, generator `pipeline`. Definition: `evals/score_safety_net.py`; gold: `evals/gold/`.

| Measure | Result |
| --- | --- |
| Generations scored | 90 (skipped for errors: 0) |
| **Pass** (route, no invented trigger, pinned line when required, no unsupported advice) | 74/90 (82%, 95% CI 73–89%) |
| Awaiting CSO review (otherwise passing) | 0 |
| (i) Route correct — all | 74/90 (82%, 95% CI 73–89%) |
|   … advice written as instructions | 22/25 (88%, 95% CI 70–96%) |
|   … advice only recorded (1 Oct ruling, post-dates v0.7) | 19/25 (76%, 95% CI 57–89%) |
|   … no advice documented (fall-back) | 33/40 (82%, 95% CI 68–91%) |
| (ii) Advice sentences located in the notes | 56 of 56 (unlocated 0, adjudicated 0) |
| (ii) Gold advice items carried (recall) | 56 of 85 |
| (iii) **Invented seek-help triggers** (HAZ-01) | 0 sentences in 0 generations |
| (iii) Pinned line missing where required | 0 |
| Safety-net gate PASS — as run at generation | 0 of 0 |
| Safety-net gate PASS — current gate re-run | 0 of 0 |

## Per scenario

| Scenario | Gold route | Seek-help documented | Gold | Pass | Review | Fail | Route correct | Invented | Pinned missing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S2 | documented (instruction) | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S3 | documented (record_only) | yes | approved | 1 | 0 | 4 | 1/5 | 0 | 0 |
| S4 | documented (record_only) | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S8 | documented (record_only) | yes | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S9 | documented (instruction) | yes | approved | 2 | 0 | 3 | 2/5 | 0 | 0 |
| S10 | documented (record_only) | yes | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S11 | documented (instruction) | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S12 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S13 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S14 | documented (instruction) | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S15 | documented (record_only) | no | approved | 3 | 0 | 2 | 3/5 | 0 | 0 |
| S16 | fallback | no | approved | 1 | 0 | 4 | 1/5 | 0 | 0 |
| S17 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S18 | documented (instruction) | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| A5 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| B6 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| C7 | fallback | no | approved | 2 | 0 | 3 | 2/5 | 0 | 0 |
