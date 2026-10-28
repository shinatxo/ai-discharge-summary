# D4 medication reconciliation — batch `w6-pipeline-e07-m01-c01-x5`

Second step-level metric (ADR-009 (e)), scorer v2, generator `pipeline`. Definition: `evals/score_meds.py`; gold: `evals/gold/`.

| Measure | Result |
| --- | --- |
| Generations scored | 90 |
| **Pass** | 80/90 (89%, 95% CI 81–94%) |
| Partial (tag missing or imprecise only) | 0/90 (0%, 95% CI 0–4%) |
| Fail | 10/90 (11%, 95% CI 6–19%) |
| Awaiting CSO review (otherwise passing) | 0 |
| Gold drugs fully correct | 265 of 275 |
| **Invented doses** (the notes give none) | 0 |
| **Invented frequencies** | 0 |

## Findings by kind

| Kind | Count |
| --- | --- |
| `missing` | 10 |

## Per scenario

| Scenario | Status | Pass | Partial | Fail | Review | Findings (all runs) |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | referenced_not_listed | 5 | 0 | 0 | 0 | — |
| S2 | listed | 5 | 0 | 0 | 0 | — |
| S3 | listed | 5 | 0 | 0 | 0 | — |
| S4 | none_required | 5 | 0 | 0 | 0 | — |
| S8 | not_documented | 5 | 0 | 0 | 0 | — |
| S9 | listed | 5 | 0 | 0 | 0 | — |
| S10 | listed | 5 | 0 | 0 | 0 | — |
| S11 | listed | 5 | 0 | 0 | 0 | — |
| S12 | not_documented | 5 | 0 | 0 | 0 | — |
| S13 | listed | 5 | 0 | 0 | 0 | — |
| S14 | listed | 5 | 0 | 0 | 0 | — |
| S15 | listed | 0 | 0 | 5 | 0 | rescue pack: missing ×5 |
| S16 | listed | 5 | 0 | 0 | 0 | — |
| S17 | listed | 0 | 0 | 5 | 0 | clarithromycin: missing ×5 |
| S18 | not_documented | 5 | 0 | 0 | 0 | — |
| A5 | referenced_not_listed | 5 | 0 | 0 | 0 | — |
| B6 | listed | 5 | 0 | 0 | 0 | — |
| C7 | not_documented | 5 | 0 | 0 | 0 | — |
