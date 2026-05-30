# Cold-eval batch — 2026-05-30T12:50:08

- Prompt: `prompts/discharge-summary-system-prompt.md`
- Model: `anthropic.claude-sonnet-4-6` (eu-west-2, on-demand)
- Inference: `maxTokens=4096`, `temperature=0.0`
- Scenarios attempted: 5
- Scenarios passed: 5
- Scenarios errored: 0

## Per-scenario

| Scenario | Elapsed | In tokens | Out tokens | Stop reason |
| --- | --- | --- | --- | --- |
| S14 | 75.00s | 5068 | 3321 | `end_turn` |
| S15 | 67.16s | 5074 | 2963 | `end_turn` |
| S16 | 80.25s | 5100 | 3231 | `end_turn` |
| S17 | 83.06s | 5094 | 3465 | `end_turn` |
| S18 | 52.16s | 4927 | 2158 | `end_turn` |

**Batch totals:** 357.6s, 25263 input tokens, 15138 output tokens.
