# Safety-netting correctness — batch `w6-pipeline-e07-m01-c01-s1-check`

Step-level metric (WS1a DoD 5), scorer v2, generator `pipeline`. Definition: `evals/score_safety_net.py`; gold: `evals/gold/`.

| Measure | Result |
| --- | --- |
| Generations scored | 1 (skipped for errors: 0) |
| **Pass** (route, no invented trigger, pinned line when required, no unsupported advice) | 1/1 (100%, 95% CI 21–100%) |
| Awaiting CSO review (otherwise passing) | 0 |
| (i) Route correct — all | 1/1 (100%, 95% CI 21–100%) |
|   … advice written as instructions | — |
|   … advice only recorded (1 Oct ruling, post-dates v0.7) | — |
|   … no advice documented (fall-back) | 1/1 (100%, 95% CI 21–100%) |
| (ii) Advice sentences located in the notes | 0 of 0 (unlocated 0, adjudicated 0) |
| (ii) Gold advice items carried (recall) | 0 of 0 |
| (iii) **Invented seek-help triggers** (HAZ-01) | 0 sentences in 0 generations |
| (iii) Pinned line missing where required | 0 |
| Safety-net gate PASS — as run at generation | 0 of 0 |
| Safety-net gate PASS — current gate re-run | 0 of 0 |

## Per scenario

| Scenario | Gold route | Seek-help documented | Gold | Pass | Review | Fail | Route correct | Invented | Pinned missing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | fallback | no | approved | 1 | 0 | 0 | 1/1 | 0 | 0 |
