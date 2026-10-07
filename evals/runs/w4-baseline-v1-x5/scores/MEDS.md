# D4 medication reconciliation — batch `w4-baseline-v1-x5`

Second step-level metric (ADR-009 (e)), scorer v2, generator `v1`. Definition: `evals/score_meds.py`; gold: `evals/gold/`.

| Measure | Result |
| --- | --- |
| Generations scored | 90 |
| **Pass** | 62/90 (69%, 95% CI 59–78%) |
| Partial (tag missing or imprecise only) | 13/90 (14%, 95% CI 9–23%) |
| Fail | 15/90 (17%, 95% CI 10–26%) |
| Awaiting CSO review (otherwise passing) | 0 |
| Gold drugs fully correct | 263 of 275 |
| **Invented doses** (the notes give none) | 0 |
| **Invented frequencies** | 6 |

## Findings by kind

| Kind | Count |
| --- | --- |
| `tag_imprecise` | 18 |
| `frequency_invented` | 6 |
| `missing` | 5 |
| `must_not_appear` | 2 |
| `frequency_wrong_or_missing` | 1 |
| `invented_drug` | 1 |

## Per scenario

| Scenario | Status | Pass | Partial | Fail | Review | Findings (all runs) |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | referenced_not_listed | 3 | 0 | 2 | 0 | fondaparinux: must_not_appear ×2 |
| S2 | listed | 0 | 0 | 5 | 0 | ibuprofen: frequency_invented ×5 |
| S3 | listed | 5 | 0 | 0 | 0 | — |
| S4 | none_required | 5 | 0 | 0 | 0 | — |
| S8 | not_documented | 5 | 0 | 0 | 0 | — |
| S9 | listed | 2 | 3 | 0 | 0 | insulin: tag_imprecise ×3 |
| S10 | listed | 4 | 0 | 1 | 0 | enoxaparin: frequency_wrong_or_missing ×1 |
| S11 | listed | 0 | 5 | 0 | 0 | enoxaparin: tag_imprecise ×5; analgesia: tag_imprecise ×5 |
| S12 | not_documented | 5 | 0 | 0 | 0 | — |
| S13 | listed | 5 | 0 | 0 | 0 | — |
| S14 | listed | 5 | 0 | 0 | 0 | — |
| S15 | listed | 0 | 0 | 5 | 0 | rescue pack: missing ×5 |
| S16 | listed | 3 | 1 | 1 | 0 | warfarin: frequency_invented ×1; analgesia: tag_imprecise ×1 |
| S17 | listed | 1 | 4 | 0 | 0 | paracetamol: tag_imprecise ×4 |
| S18 | not_documented | 5 | 0 | 0 | 0 | — |
| A5 | referenced_not_listed | 4 | 0 | 1 | 0 | —: invented_drug ×1 |
| B6 | listed | 5 | 0 | 0 | 0 | — |
| C7 | not_documented | 5 | 0 | 0 | 0 | — |
