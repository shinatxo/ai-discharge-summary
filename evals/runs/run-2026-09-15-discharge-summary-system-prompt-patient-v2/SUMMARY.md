# Cold-eval batch — 2026-09-15T16:10:46

- Prompt: `prompts/discharge-summary-system-prompt.md`
- Model: `anthropic.claude-sonnet-4-6` (eu-west-2, on-demand)
- Inference: `maxTokens=4096`, `temperature=0.0`
- Scenarios attempted: 4
- Scenarios passed: 4
- Scenarios errored: 0

## Per-scenario

| Scenario | Safety-net gate | Elapsed | In tokens | Out tokens | Cache read | Cache write | Stop reason |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S8 | PASS (documented_advice) | 55.01s | 5494 | 2570 | 0 | 0 | `end_turn` |
| S9 | PASS (clean) | 54.44s | 5506 | 2316 | 0 | 0 | `end_turn` |
| S15 | PASS (clean) | 57.26s | 5573 | 2589 | 0 | 0 | `end_turn` |
| S18 | PASS (clean) | 43.59s | 5426 | 1828 | 0 | 0 | `end_turn` |

**Batch totals:** 210.3s, 21999 input tokens, 9303 output tokens.
