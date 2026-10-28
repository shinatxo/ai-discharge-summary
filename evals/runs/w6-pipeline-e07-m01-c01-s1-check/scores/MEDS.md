# D4 medication reconciliation — batch `w6-pipeline-e07-m01-c01-s1-check`

Second step-level metric (ADR-009 (e)), scorer v2, generator `pipeline`. Definition: `evals/score_meds.py`; gold: `evals/gold/`.

| Measure | Result |
| --- | --- |
| Generations scored | 1 |
| **Pass** | 1/1 (100%, 95% CI 21–100%) |
| Partial (tag missing or imprecise only) | 0/1 (0%, 95% CI 0–79%) |
| Fail | 0/1 (0%, 95% CI 0–79%) |
| Awaiting CSO review (otherwise passing) | 0 |
| Gold drugs fully correct | 8 of 8 |
| **Invented doses** (the notes give none) | 0 |
| **Invented frequencies** | 0 |

## Findings by kind

| Kind | Count |
| --- | --- |
| — | 0 |

## Per scenario

| Scenario | Status | Pass | Partial | Fail | Review | Findings (all runs) |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | referenced_not_listed | 1 | 0 | 0 | 0 | — |
