# Run r4 — batch `w4-baseline-v1-x5`

- Generator: v1 · path `deployed` · model `anthropic.claude-sonnet-4-6` (eu-west-2)
- Prompt file sha256: `a7ec8a5661007b0a69231dd68a1f286013cf23a2192ed7ec16770bc07f3c1e3a`
- Generations: 18

| Scenario | Safety-net gate | Patient | Elapsed | In | Out | Cache read | Cache write | Stop | Est. $ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | PASS (documented_advice) | v2 | 50.2s | 1769 | 4042 | 6230 | 0 | `end_turn,end_turn` | 0.0746 |
| S2 | PASS (documented_advice) | v2 | 33.9s | 917 | 2455 | 6230 | 0 | `end_turn,end_turn` | 0.0456 |
| S3 | PASS (documented_advice) | v2 | 42.0s | 1345 | 3289 | 6230 | 0 | `end_turn,end_turn` | 0.0608 |
| S4 | PASS (documented_advice) | v2 | 32.4s | 1215 | 2358 | 6230 | 0 | `end_turn,end_turn` | 0.0450 |
| S8 | PASS (documented_advice) | v2 | 57.9s | 1495 | 3441 | 6230 | 0 | `end_turn,end_turn` | 0.0638 |
| S9 | PASS (clean) | v2 | 42.8s | 1155 | 3200 | 6230 | 0 | `end_turn,end_turn` | 0.0587 |
| S10 | PASS (documented_advice) | v2 | 44.8s | 1339 | 3503 | 6230 | 0 | `end_turn,end_turn` | 0.0643 |
| S11 | PASS (clean) | v2 | 50.4s | 1596 | 3840 | 6230 | 0 | `end_turn,end_turn` | 0.0707 |
| S12 | PASS (documented_advice) | v2 | 68.3s | 2898 | 5117 | 6230 | 0 | `max_tokens,end_turn` | 0.0960 |
| S13 | PASS (documented_advice) | v2 | 65.7s | 2357 | 5096 | 6230 | 0 | `end_turn,end_turn` | 0.0939 |
| S14 | PASS (clean) | v2 | 46.4s | 1325 | 3523 | 6230 | 0 | `end_turn,end_turn` | 0.0646 |
| S15 | PASS (clean) | v2 | 48.9s | 1446 | 3818 | 6230 | 0 | `end_turn,end_turn` | 0.0698 |
| S16 | PASS (clean) | v2 | 56.3s | 1728 | 4335 | 6230 | 0 | `end_turn,end_turn` | 0.0793 |
| S17 | PASS (clean) | v2 | 55.4s | 1539 | 4397 | 6230 | 0 | `end_turn,end_turn` | 0.0797 |
| S18 | PASS (clean) | v2 | 51.1s | 796 | 3143 | 6230 | 0 | `end_turn,end_turn` | 0.0565 |
| A5 | PASS (documented_advice) | v2 | 38.1s | 1237 | 3014 | 6230 | 0 | `end_turn,end_turn` | 0.0559 |
| B6 | PASS (documented_advice) | v2 | 45.3s | 1374 | 3618 | 6230 | 0 | `end_turn,end_turn` | 0.0663 |
| C7 | PASS (clean) | v2 | 36.4s | 752 | 2663 | 6230 | 0 | `end_turn,end_turn` | 0.0485 |

**Run total:** est. $1.1938, 866s.
