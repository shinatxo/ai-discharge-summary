# Combined verdicts — w4-baseline-v1-x5 / judge rescore-v2

Worse of deterministic checks (v2) and judge, per dimension (EVAL_RESULTS.md §2). Judge `anthropic.claude-sonnet-4-6`, instructions `7b251e319c0e…`, rubric `f8291ae16136…`. 90 judge records, 0 judge errors. combine_judge v1; offline, no model calls.

## Combined (the score that counts)

| | Pass | Partial | Fail | N/A |
|---|---|---|---|---|
| D2 | 3 | 9 | 78 | 0 |
| D3 | 79 | 7 | 4 | 0 |
| D6 | 68 | 16 | 6 | 0 |
| D7 | 72 | 8 | 10 | 0 |
| D8 | 0 | 9 | 4 | 77 |

## Judge alone

| | Pass | Partial | Fail | N/A |
|---|---|---|---|---|
| D2 | 3 | 9 | 78 | 0 |
| D3 | 79 | 7 | 4 | 0 |
| D6 | 68 | 16 | 6 | 0 |
| D7 | 72 | 8 | 10 | 0 |
| D8 | 0 | 9 | 4 | 77 |

Checks decided Fail: D2 54, D7 0. Auto-fails, combined: D2 6, D3 3, D6 0, D7 1, D8 0. Laterality / possible-non-English flags (judge rules, not counted): 130.

## Where the checks changed the judge's verdict (0)

None — combined equals judge alone on every cell.

## Combined, per scenario (all runs and repeats)

| Scenario | D2 | D3 | D6 | D7 | D8 |
|---|---|---|---|---|---|
| S1 | 5F | 5P | 4P 1Pa | 3P 2Pa | 5NA |
| S2 | 5F | 4P 1Pa | 5P | 5P | 5NA |
| S3 | 5F | 5P | 1P 3Pa 1F | 1Pa 4F | 1Pa 4F |
| S4 | 2Pa 3F | 5P | 4P 1Pa | 4P 1F | 5NA |
| S8 | 1P 2Pa 2F | 5P | 1P 4Pa | 5P | 5NA |
| S9 | 2Pa 3F | 5P | 5P | 5P | 5NA |
| S10 | 5F | 5P | 1P 4Pa | 5P | 5NA |
| S11 | 5F | 5P | 4P 1Pa | 5P | 5NA |
| S12 | 5F | 2Pa 3F | 5P | 4P 1Pa | 1Pa 4NA |
| S13 | 2P 3Pa | 5P | 5P | 5P | 2Pa 3NA |
| S14 | 5F | 4Pa 1F | 5P | 4P 1Pa | 5NA |
| S15 | 5F | 5P | 4P 1Pa | 1Pa 4F | 5NA |
| S16 | 5F | 5P | 5P | 3P 2Pa | 5NA |
| S17 | 5F | 5P | 4P 1Pa | 5P | 5NA |
| S18 | 5F | 5P | 5F | 5P | 5NA |
| A5 | 5F | 5P | 5P | 5P | 5Pa |
| B6 | 5F | 5P | 5P | 5P | 5NA |
| C7 | 5F | 5P | 5P | 4P 1F | 5NA |

P pass · Pa partial · F fail · NA not applicable · ? judge error.
