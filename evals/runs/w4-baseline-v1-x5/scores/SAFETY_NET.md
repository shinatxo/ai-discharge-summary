# Safety-netting correctness — batch `w4-baseline-v1-x5`

Step-level metric (WS1a DoD 5), scorer v2, generator `v1`. Definition: `evals/score_safety_net.py`; gold: `evals/gold/`.

| Measure | Result |
| --- | --- |
| Generations scored | 90 (skipped for errors: 0) |
| **Pass** (route, no invented trigger, pinned line when required, no unsupported advice) | 78/90 (87%, 95% CI 78–92%) |
| Awaiting CSO review (otherwise passing) | 0 |
| (i) Route correct — all | 86/90 (96%, 95% CI 89–98%) |
|   … advice written as instructions | 21/25 (84%, 95% CI 65–94%) |
|   … advice only recorded (1 Oct ruling, post-dates v0.7) | 25/25 (100%, 95% CI 87–100%) |
|   … no advice documented (fall-back) | 40/40 (100%, 95% CI 91–100%) |
| (ii) Advice sentences located in the notes | 52 of 88 (unlocated 0, adjudicated 36) |
| (ii) Gold advice items carried (recall) | 55 of 85 |
| (iii) **Invented seek-help triggers** (HAZ-01) | 3 sentences in 2 generations |
| (iii) Pinned line missing where required | 0 |
| Safety-net gate PASS — as run at generation | 89 of 90 |
| Safety-net gate PASS — current gate re-run | 89 of 90 |

## Per scenario

| Scenario | Gold route | Seek-help documented | Gold | Pass | Review | Fail | Route correct | Invented | Pinned missing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S2 | documented (instruction) | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S3 | documented (record_only) | yes | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S4 | documented (record_only) | no | approved | 4 | 0 | 1 | 5/5 | 1 | 0 |
| S8 | documented (record_only) | yes | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S9 | documented (instruction) | yes | approved | 1 | 0 | 4 | 2/5 | 0 | 0 |
| S10 | documented (record_only) | yes | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S11 | documented (instruction) | no | approved | 4 | 0 | 1 | 4/5 | 0 | 0 |
| S12 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S13 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S14 | documented (instruction) | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S15 | documented (record_only) | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S16 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S17 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| S18 | documented (instruction) | no | approved | 0 | 0 | 5 | 5/5 | 0 | 0 |
| A5 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| B6 | fallback | no | approved | 5 | 0 | 0 | 5/5 | 0 | 0 |
| C7 | fallback | no | approved | 4 | 0 | 1 | 5/5 | 2 | 0 |
