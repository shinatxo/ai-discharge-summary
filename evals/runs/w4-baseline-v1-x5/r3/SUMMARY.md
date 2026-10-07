# Run r3 — batch `w4-baseline-v1-x5`

- Generator: v1 · path `deployed` · model `anthropic.claude-sonnet-4-6` (eu-west-2)
- Prompt file sha256: `a7ec8a5661007b0a69231dd68a1f286013cf23a2192ed7ec16770bc07f3c1e3a`
- Generations: 18

| Scenario | Safety-net gate | Patient | Elapsed | In | Out | Cache read | Cache write | Stop | Est. $ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | PASS (documented_advice) | v2 | 51.7s | 1893 | 4036 | 6230 | 0 | `end_turn,end_turn` | 0.0749 |
| S2 | PASS (documented_advice) | v2 | 34.3s | 917 | 2497 | 6230 | 0 | `end_turn,end_turn` | 0.0463 |
| S3 | PASS (documented_advice) | v2 | 41.6s | 1339 | 3212 | 6230 | 0 | `end_turn,end_turn` | 0.0595 |
| S4 | PASS (documented_advice) | v2 | 34.8s | 1249 | 2502 | 6230 | 0 | `end_turn,end_turn` | 0.0475 |
| S8 | PASS (documented_advice) | v2 | 40.9s | 1421 | 2983 | 6230 | 0 | `end_turn,end_turn` | 0.0560 |
| S9 | PASS (clean) | v2 | 44.6s | 1216 | 3279 | 6230 | 0 | `end_turn,end_turn` | 0.0602 |
| S10 | PASS (documented_advice) | v2 | 48.9s | 1421 | 3671 | 6230 | 0 | `end_turn,end_turn` | 0.0673 |
| S11 | PASS (clean) | v2 | 60.2s | 1911 | 4686 | 6230 | 0 | `end_turn,end_turn` | 0.0857 |
| S12 | PASS (documented_advice) | v2 | 68.9s | 2898 | 5133 | 6230 | 0 | `max_tokens,end_turn` | 0.0963 |
| S13 | PASS (documented_advice) | v2 | 58.9s | 2194 | 4795 | 6230 | 0 | `end_turn,end_turn` | 0.0884 |
| S14 | PASS (clean) | v2 | 49.8s | 1280 | 3973 | 6230 | 0 | `end_turn,end_turn` | 0.0718 |
| S15 | PASS (clean) | v2 | 43.3s | 1370 | 3505 | 6230 | 0 | `end_turn,end_turn` | 0.0644 |
| S16 | PASS (clean) | v2 | 57.5s | 1920 | 4471 | 6230 | 0 | `end_turn,end_turn` | 0.0822 |
| S17 | PASS (clean) | v2 | 51.7s | 1562 | 4057 | 6230 | 0 | `end_turn,end_turn` | 0.0742 |
| S18 | PASS (clean) | v2 | 31.3s | 834 | 2472 | 6230 | 0 | `end_turn,end_turn` | 0.0456 |
| A5 | PASS (documented_advice) | v2 | 42.2s | 1274 | 3212 | 6230 | 0 | `end_turn,end_turn` | 0.0593 |
| B6 | PASS (documented_advice) | v2 | 50.6s | 1633 | 3996 | 6230 | 0 | `end_turn,end_turn` | 0.0734 |
| C7 | PASS (clean) | v2 | 34.5s | 858 | 2666 | 6230 | 0 | `end_turn,end_turn` | 0.0489 |

**Run total:** est. $1.2016, 846s.
