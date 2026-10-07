# Run r5 — batch `w4-baseline-v1-x5`

- Generator: v1 · path `deployed` · model `anthropic.claude-sonnet-4-6` (eu-west-2)
- Prompt file sha256: `a7ec8a5661007b0a69231dd68a1f286013cf23a2192ed7ec16770bc07f3c1e3a`
- Generations: 18

| Scenario | Safety-net gate | Patient | Elapsed | In | Out | Cache read | Cache write | Stop | Est. $ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | PASS (documented_advice) | v2 | 52.4s | 1686 | 3910 | 6230 | 0 | `end_turn,end_turn` | 0.0721 |
| S2 | PASS (documented_advice) | v2 | 43.9s | 1279 | 2969 | 6230 | 0 | `end_turn,end_turn` | 0.0553 |
| S3 | PASS (documented_advice) | v2 | 44.0s | 1327 | 3282 | 6230 | 0 | `end_turn,end_turn` | 0.0606 |
| S4 | PASS (documented_advice) | v2 | 29.6s | 1014 | 2093 | 6230 | 0 | `end_turn,end_turn` | 0.0399 |
| S8 | PASS (documented_advice) | v2 | 49.3s | 1527 | 3480 | 6230 | 0 | `end_turn,end_turn` | 0.0645 |
| S9 | PASS (clean) | v2 | 49.6s | 1396 | 3508 | 6230 | 0 | `end_turn,end_turn` | 0.0645 |
| S10 | PASS (documented_advice) | v2 | 57.7s | 1874 | 4252 | 6230 | 0 | `end_turn,end_turn` | 0.0784 |
| S11 | PASS (clean) | v2 | 63.7s | 1927 | 4657 | 6230 | 0 | `end_turn,end_turn` | 0.0853 |
| S12 | PASS (documented_advice) | v2 | 65.1s | 2684 | 4767 | 6230 | 0 | `max_tokens,end_turn` | 0.0896 |
| S13 | PASS (documented_advice) | v2 | 80.4s | 2194 | 4795 | 6230 | 0 | `end_turn,end_turn` | 0.0884 |
| S14 | PASS (clean) | v2 | 52.1s | 1338 | 4072 | 6230 | 0 | `end_turn,end_turn` | 0.0737 |
| S15 | PASS (clean) | v2 | 51.2s | 1438 | 3939 | 6230 | 0 | `end_turn,end_turn` | 0.0718 |
| S16 | PASS (clean) | v2 | 52.1s | 1449 | 4215 | 6230 | 0 | `end_turn,end_turn` | 0.0764 |
| S17 | PASS (clean) | v2 | 55.1s | 1679 | 4462 | 6230 | 0 | `end_turn,end_turn` | 0.0812 |
| S18 | PASS (clean) | v2 | 36.3s | 872 | 2722 | 6230 | 0 | `end_turn,end_turn` | 0.0498 |
| A5 | PASS (documented_advice) | v2 | 40.4s | 1275 | 3116 | 6230 | 0 | `end_turn,end_turn` | 0.0577 |
| B6 | PASS (documented_advice) | v2 | 59.1s | 1438 | 3930 | 6230 | 0 | `end_turn,end_turn` | 0.0716 |
| C7 | PASS (clean) | v2 | 44.4s | 785 | 2581 | 6230 | 0 | `end_turn,end_turn` | 0.0472 |

**Run total:** est. $1.2281, 926s.
