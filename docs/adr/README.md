# Architecture decision records

## Where each ADR lives

| ADR | Decision | Where |
|-----|----------|-------|
| ADR-001 | Foundation model: Claude on Amazon Bedrock | [`docs/ADR-phase1.md`](../ADR-phase1.md) |
| ADR-002 | DynamoDB audit-log schema (hash-only, immutable) | [`docs/ADR-phase1.md`](../ADR-phase1.md) |
| ADR-003 | Region strategy: single-region eu-west-2 | [`docs/ADR-phase1.md`](../ADR-phase1.md) |
| ADR-004 | Front door: HTTP API (v2) + Cognito JWT authoriser | [`docs/ADR-phase1.md`](../ADR-phase1.md) |
| ADR-005 | Async `202 + poll` pattern with client-supplied idempotency | [`docs/ADR-phase1.md`](../ADR-phase1.md) |
| ADR-006 | Static SPA delivery: one CloudFront distribution, two origins | [`docs/ADR-phase1.md`](../ADR-phase1.md) |
| ADR-007 | Retention and expiry of generation records | [`docs/ADR-phase1.md`](../ADR-phase1.md) |
| ADR-008 | The serverless substitution | [`docs/ADR-phase1.md`](../ADR-phase1.md) |
| ADR-009 | The agentic pipeline: named steps, step traces, a review-gate seam, a branch-isolated build | [`docs/ADR-phase1.md`](../ADR-phase1.md) |
| ADR-010 | The step 7v verifier | Not committed (see below) |
| ADR-011 | Reserved for the WS7 design | Not written yet |

ADR-001 to ADR-009 stay where they are in `docs/ADR-phase1.md`. They are not moved or copied
into this folder.

**ADR-010 is taken.** It is the step 7v verifier, built 4–5 Nov 2026, stopped at its dev gate
and not adopted. Its text is a local draft at `docs/_local/ADR-010-DRAFT.md`. It is not
committed, and `docs/_local/` cannot be read from a locked Claude or Codex session.

**ADR-011 is reserved for the WS7 design** and for nothing else.

## Adding a new ADR

- Any other new decision takes **ADR-012 onward**.
- One decision per file, in this folder: `docs/adr/ADR-012-<short-name>.md`.
- Each file has four sections: **Status**, **Context**, **Decision**, **Consequences**.
- Add a row to the table above in the same PR.
- The ADR goes in the same PR as the code it describes.
