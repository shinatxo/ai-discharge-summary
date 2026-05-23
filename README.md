# 🩺 AI Discharge Summary Assistant

Turn a doctor's messy ward-round notes into a structured discharge summary, a GP letter, and a plain-English version for the patient — safely, with every output left as a **draft for a clinician to review and sign**.

> **Status:** Phase 1 (Discovery & Validation). Portfolio / demonstration project. **Not a medical device; not deployed in clinical care.** All evaluation data is synthetic.

---

## Why this exists

Junior doctors spend a large share of their day on discharge paperwork, and poor summaries delay GP follow-up and contribute to readmissions. This tool drafts a structured summary, a GP letter, and a patient explanation from clinical notes in seconds — but its real point is to do so **without making things up**. The hard problem in clinical AI is not fluency; it is restraint. The assistant is engineered to report only what the notes support, to surface gaps and contradictions rather than paper over them, and to refuse outright to invent the fields that cause harm.

It is built with NHS-flavoured, DSPT-aligned controls: Cognito MFA, KMS customer-managed-key encryption, and an immutable, **hash-only** audit log that never stores patient-identifiable data.

## What it produces

From one block of free-text notes, three outputs (matching the UI tabs):

1. **Structured discharge summary** — diagnosis, investigations, treatment, resuscitation status, reconciled medications, follow-up, GP actions.
2. **GP letter** — a brief clinician-to-clinician handover that mirrors the medication changes and GP actions (the highest-harm interface).
3. **Patient-friendly version** — plain English at Flesch–Kincaid grade ≤ 8, with safety-net advice; audience-shifted for parents/carers in paediatrics, and flagged for translation when the patient does not speak English.

## How a junior doctor would actually use it on a shift

Paste the ward-round notes into the box. Read the three drafts. Fix anything that needs fixing — the tool deliberately writes **"Not documented"** wherever the notes are silent, so the gaps are visible rather than invented. Tick **"I have reviewed and edited this output"**, which unlocks download. The draft is marked `draft = true` in the audit log until that tick is recorded as a sign-off. The clinician remains the author of record throughout.

## Architecture

```
Clinician browser
   │  (Cognito auth, TLS)
CloudFront ── S3 (React frontend)
   │
API Gateway ── Lambda (Python)
   │                 │
   ▼                 ▼
Bedrock          DynamoDB (audit log, KMS-CMK, hash-only — no PHI)
(Claude Sonnet-class,
 eu-west-2 / EU profile)
   │
   ▼
S3 (generated documents, KMS-encrypted, signed-URL only, lifecycle → Glacier @ 90d)
```

- **Single-region, eu-west-2 (London)** for all stateful resources; model inference pinned to the EU (on-demand in eu-west-2 where available, otherwise the EU geographic inference profile — never US/global). Region and inference profile are logged on every generation as residency evidence.
- **Audit log:** one DynamoDB table, on-demand, KMS-CMK encrypted, `PK = USER#<cognito_sub>`, `SK = GEN#<timestamp>#<ulid>`. Stores hashes (`input_sha256`, per-output `output_sha256`), model version, output type, region, and inference profile — **never the clinical content**. The only mutable field is `draft → reviewed_at`; no `DeleteItem`; PITR + append-only stream.
- **No fine-tuning.** Behaviour is shaped by an auditable system prompt (currently v0.5), which is preferable to opaque weights for a safety-critical draft.

SAA-C03 services exercised (~70% of the syllabus): Lambda, API Gateway, DynamoDB, Cognito, S3 (encryption + lifecycle), KMS CMK, CloudFront, Route 53, IAM, CloudWatch, plus Bedrock.

## The interesting part: how it's evaluated

Outputs are scored on five dimensions, three with **auto-fail gates**: omission, hallucination (auto-fail: invented resus/drug/diagnosis), resuscitation-status accuracy (auto-fail), drug reconciliation (auto-fail), and patient-version reading age (Flesch–Kincaid ≤ 8). The evaluation set is ~19 fully synthetic scenarios spanning neonatal to elderly, medical and surgical, plus deliberately adversarial cases (prompt injection, internally contradictory notes, missing data + a non-English-speaking patient).

This repo deliberately documents what **didn't** work, because a portfolio that only shows green ticks isn't credible:

- **A real failure, fixed and verified.** Run on an adversarial case (C7) by an *independent* model with no access to the answer key, the tool produced an English-only patient leaflet for a Polish-speaking patient — useless to that patient. Root cause: the prompt had no rule for non-English speakers. Fix: prompt v0.3 added a translation/interpreter rule. Re-run cold: the leaflet now leads with "FOR TRANSLATION — do not hand to the patient untranslated." A complete **failure → fix → verify** loop.
- **The evaluation caught errors in its own answer key.** When the 11 expansion scenarios were generated cold (system prompt + notes only, no gold in context), the disciplined model disagreed with the hand-written reference answers in five places — and the *model* was right: the gold had invented a resuscitation status in two cases and asserted "None known" allergies with no documented basis in three. The gold was corrected. The eval auditing its own answer key is, itself, evidence the safety rules are doing their job.
- **Current scoreboard:** seeds 4/4 (smoke-test, self-scored); adversarial injection/contradiction handling PASS; expansion S8–S18 **11/11 PASS cold under v0.5, no auto-fails**, reading ages FK 3.6–6.2. **Independent clinician scoring (Run 4) is in progress** — specialty-matched review packs are out with practising clinicians.

Full detail, per-scenario checkpoints, and the run log are in [`EVAL_RESULTS.md`](EVAL_RESULTS.md).

## Governance

This project treats governance as a first-class deliverable, not an afterthought — the cheapest credibility upgrade for a healthcare AI portfolio.

- [`MODEL_CARD.md`](MODEL_CARD.md) — intended use, out-of-scope, model details, safety behaviours, evaluation, known limitations.
- [`THREAT_MODEL.md`](THREAT_MODEL.md) — STRIDE plus AI-specific threats (prompt injection, hallucination, automation bias, gold-error, model drift).
- [`EVAL_RESULTS.md`](EVAL_RESULTS.md) — the scoring rubric, the synthetic scenarios, every run, and the documented failure cases.

## Repository contents

| File | What it is |
|------|------------|
| `discharge-summary-system-prompt.md` | The system prompt (v0.5) — the safety surface of the whole tool |
| `EVAL_RESULTS.md` | Scoring harness, rubric, run log, documented failures |
| `eval-scenarios-expansion.md` | The synthetic scenario set (notes + gold reference outputs) |
| `adversarial-scenarios.md` | The injection / contradiction / missing-data edge cases |
| `ADR-phase1.md` | Architecture Decision Records (model choice, audit-log schema, region/residency) |
| `mvp-ui-mockup.html` | Clickable wireframe of the two-pane UI with the review gate |
| `MODEL_CARD.md`, `THREAT_MODEL.md` | Governance artifacts |
| `Reviewer-Pack-*.docx`, `Reviewer-Scoring-Sheet.xlsx` | Clinician review pack (one scenario per specialty) + collation sheet |

## Roadmap

- **Phase 1 (now):** problem definition, system prompt, evaluation harness, architecture decisions, governance — largely complete; clinician review in progress.
- **Phase 2:** build the backend skeleton (API Gateway + Lambda + the DynamoDB audit table per ADR-002), wire the Bedrock call, add Cognito and the React frontend, deploy behind CloudFront with KMS.
- **Open decisions:** pin the exact Claude model + version available on-demand in eu-west-2 at build time; choose the audit-log tamper-evidence mechanism (Streams → S3 Object Lock/WORM vs PITR-only).

## Disclaimer

This is a portfolio demonstration. It is **not a medical device**, carries no CE/UKCA marking, has no regulatory clearance, and must not be used in clinical care or with real patient data. Every output is a draft requiring review and sign-off by a qualified clinician who remains responsible for the discharge summary.
