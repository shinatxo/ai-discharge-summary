# Cold-eval batch — 2026-05-28T14:45:50

- Prompt: `prompts/discharge-summary-system-prompt.md`
- Model: `anthropic.claude-sonnet-4-6` (eu-west-2, on-demand)
- Inference: `maxTokens=4096`, `temperature=0.0`
- Scenarios attempted: 5
- Scenarios passed: 5
- Scenarios errored: 0

## Per-scenario

| Scenario | Elapsed | In tokens | Out tokens | Stop reason |
| --- | --- | --- | --- | --- |
| S14 | 111.60s | 5068 | 2774 | `end_turn` |
| S15 | 50.22s | 5074 | 2998 | `end_turn` |
| S16 | 58.72s | 5100 | 3231 | `end_turn` |
| S17 | 177.14s | 5094 | 3326 | `end_turn` |
| S18 | 34.31s | 4927 | 1880 | `end_turn` |

**Batch totals:** 432.0s, 25263 input tokens, 14209 output tokens.
