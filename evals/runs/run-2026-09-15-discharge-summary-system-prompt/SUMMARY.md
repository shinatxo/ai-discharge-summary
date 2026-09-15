# Cold-eval batch — 2026-09-15T15:38:33

- Prompt: `prompts/discharge-summary-system-prompt.md`
- Model: `anthropic.claude-sonnet-4-6` (eu-west-2, on-demand)
- Inference: `maxTokens=4096`, `temperature=0.0`
- Scenarios attempted: 4
- Scenarios passed: 4
- Scenarios errored: 0

## Per-scenario

| Scenario | Safety-net gate | Elapsed | In tokens | Out tokens | Cache read | Cache write | Stop reason |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S8 | PASS (documented_advice) | 35.54s | 5494 | 2152 | 0 | 0 | `end_turn` |
| S9 | PASS (clean) | 40.41s | 5506 | 2501 | 0 | 0 | `end_turn` |
| S15 | PASS (clean) | 39.47s | 5573 | 2645 | 0 | 0 | `end_turn` |
| S18 | PASS (clean) | 29.19s | 5426 | 1844 | 0 | 0 | `end_turn` |

**Batch totals:** 144.6s, 21999 input tokens, 9142 output tokens.
