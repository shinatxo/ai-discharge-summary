# Run r1 — batch `w4-baseline-v1-x5`

- Generator: v1 · path `deployed` · model `anthropic.claude-sonnet-4-6` (eu-west-2)
- Prompt file sha256: `a7ec8a5661007b0a69231dd68a1f286013cf23a2192ed7ec16770bc07f3c1e3a`
- Generations: 18

| Scenario | Safety-net gate | Patient | Elapsed | In | Out | Cache read | Cache write | Stop | Est. $ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | PASS (documented_advice) | v2 | 61.2s | 1686 | 3903 | 0 | 6230 | `end_turn,end_turn` | 0.0957 |
| S2 | PASS (documented_advice) | v2 | 36.1s | 1025 | 2603 | 6230 | 0 | `end_turn,end_turn` | 0.0484 |
| S3 | PASS (documented_advice) | v2 | 45.5s | 1742 | 3364 | 6230 | 0 | `end_turn,end_turn` | 0.0633 |
| S4 | PASS (documented_advice) | v2 | 28.4s | 976 | 2127 | 6230 | 0 | `end_turn,end_turn` | 0.0404 |
| S8 | PASS (documented_advice) | v2 | 42.3s | 1161 | 2939 | 6230 | 0 | `end_turn,end_turn` | 0.0544 |
| S9 | PASS (clean) | v2 | 42.3s | 1155 | 3130 | 6230 | 0 | `end_turn,end_turn` | 0.0575 |
| S10 | PASS (documented_advice) | v2 | 54.2s | 1874 | 4163 | 6230 | 0 | `end_turn,end_turn` | 0.0769 |
| S11 | PASS (clean) | v2 | 55.6s | 1694 | 4162 | 6230 | 0 | `end_turn,end_turn` | 0.0763 |
| S12 | PASS (documented_advice) | v2 | 68.4s | 2597 | 5035 | 6230 | 0 | `max_tokens,end_turn` | 0.0937 |
| S13 | PASS (documented_advice) | v2 | 67.4s | 2388 | 5269 | 6230 | 0 | `end_turn,end_turn` | 0.0969 |
| S14 | PASS (clean) | v2 | 46.8s | 1246 | 3550 | 6230 | 0 | `end_turn,end_turn` | 0.0647 |
| S15 | PASS (clean) | v2 | 47.3s | 1638 | 4068 | 6230 | 0 | `end_turn,end_turn` | 0.0746 |
| S16 | PASS (clean) | v2 | 55.3s | 1605 | 4059 | 6230 | 0 | `end_turn,end_turn` | 0.0743 |
| S17 | PASS (clean) | v2 | 52.7s | 1562 | 4057 | 6230 | 0 | `end_turn,end_turn` | 0.0742 |
| S18 | PASS (clean) | v2 | 38.2s | 1099 | 2801 | 6230 | 0 | `end_turn,end_turn` | 0.0519 |
| A5 | PASS (documented_advice) | v2 | 40.5s | 1237 | 3094 | 6230 | 0 | `end_turn,end_turn` | 0.0572 |
| B6 | PASS (documented_advice) | v2 | 48.6s | 1449 | 3821 | 6230 | 0 | `end_turn,end_turn` | 0.0699 |
| C7 | PASS (clean) | v2 | 33.6s | 830 | 2462 | 6230 | 0 | `end_turn,end_turn` | 0.0454 |

**Run total:** est. $1.2156, 865s.
