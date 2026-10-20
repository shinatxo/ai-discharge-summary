# Calibration — w4-pipeline-e06-x5 r1 vs judge rescore-v2

- Judge: `anthropic.claude-sonnet-4-6`, temperature 0.0, tool_strict False; instructions `7b251e319c0e…`, rubric `f8291ae16136…`; repeats 1.
- Deterministic checks: version 2.
- Items: 6 × 5 dimensions; author blind scores (rubric stamp `f8291ae16136…`).

## Thresholds (each must hold on every repeat)

| | Threshold | Result |
|---|---|---|
| 1 | Combined instrument misses no author Fail | **MET** |
| 2 | Pooled quadratic-weighted κ ≥ 0.6 | **NOT MET** |
| 3 | Judge self-disagreement ≤ 10% | **—** |

**Overall: NOT MET — judge scores do not count yet**

## Per repeat

| Repeat | Missed Fails (combined) | κw combined (n) | κw judge alone | Raw agreement combined (95% CI) | N/A mismatches |
|---|---|---|---|---|---|
| rep1 | 0 | 0.29 (21) | 0.29 | 21/30 (52–83%) | 4 |

Self-disagreement: 0/30 item × dimension cells (0.0%) had different judge verdicts across repeats.

## Agreement by dimension (combined, rep1)

| D2 | D3 | D6 | D7 | D8 |
|---|---|---|---|---|
| 4/6 | 5/6 | 3/6 | 5/6 | 4/6 |

Missed Fails and every disagreement are listed in `disagreements.md`.
