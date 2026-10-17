# Calibration — w4-baseline-v1-x5 r1 vs judge calib-r1-x3b

- Judge: `anthropic.claude-sonnet-4-6`, temperature 0.0, tool_strict False; instructions `7b251e319c0e…`, rubric `f8291ae16136…`; repeats 3.
- Items: 18 × 5 dimensions; author blind scores (rubric stamp `f8291ae16136…`).

## Thresholds (each must hold on every repeat)

| | Threshold | Result |
|---|---|---|
| 1 | Combined instrument misses no author Fail | **MET** |
| 2 | Pooled quadratic-weighted κ ≥ 0.6 | **MET** |
| 3 | Judge self-disagreement ≤ 10% | **MET** |

**Overall: MET — judge scores may count**

## Per repeat

| Repeat | Missed Fails (combined) | κw combined (n) | κw judge alone | Raw agreement combined (95% CI) | N/A mismatches |
|---|---|---|---|---|---|
| rep1 | 0 | 0.73 (72) | 0.76 | 76/90 (76–91%) | 3 |
| rep2 | 0 | 0.72 (72) | 0.75 | 78/90 (78–92%) | 2 |
| rep3 | 0 | 0.71 (72) | 0.74 | 76/90 (76–91%) | 3 |

Self-disagreement: 6/90 item × dimension cells (6.7%) had different judge verdicts across repeats.

## Agreement by dimension (combined, rep1)

| D2 | D3 | D6 | D7 | D8 |
|---|---|---|---|---|
| 14/18 | 17/18 | 15/18 | 15/18 | 15/18 |

Missed Fails and every disagreement are listed in `disagreements.md`.
