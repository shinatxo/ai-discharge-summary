# Model Card — AI Discharge Summary Assistant

_Last updated: 2026-05-22 · Phase 1 (Discovery & Validation) · System prompt v0.5_

This is a portfolio / demonstration project. **It is not a medical device, it is not deployed in clinical care, and it must not be used to make clinical decisions.** Every output is a draft for a qualified clinician to review, edit, and sign.

---

## 1. Overview

The AI Discharge Summary Assistant turns a doctor's free-text, abbreviated ward-round notes into three drafts:

1. a **structured clinical discharge summary**,
2. a **GP letter** (clinician-to-clinician handover), and
3. a **patient-friendly version** written at a low reading age.

It is built around a single, heavily-constrained system prompt (v0.5) running on a Claude Sonnet-class model via Amazon Bedrock. The design goal is not fluency — it is **safe restraint**: the assistant is engineered to report only what the notes support, to flag gaps and contradictions rather than resolve them, and to refuse to invent the fields that cause real harm (resuscitation status, medications, diagnoses).

## 2. Intended use

- **Intended:** draft assistance for clinician-authored discharge summaries, GP letters, and patient-facing explanations, always reviewed and signed off by a qualified clinician before any clinical use.
- **Intended users:** hospital clinicians (e.g. junior doctors) who already hold responsibility for the discharge summary.
- **Intended context:** a human-in-the-loop workflow where the model accelerates a first draft and the clinician remains the author of record.

## 3. Out of scope

- Autonomous clinical decision-making, diagnosis, triage, or medication dosing.
- Any output emitted to a patient, GP, or record **without** human review and sign-off.
- Use as a system of record. The tool drafts; the clinical record system remains authoritative.
- Real patient data during this phase — **all evaluation data is synthetic** (fictional names, fabricated NHS numbers, invented clinical detail).

**Not a medical device.** No CE / UKCA marking; no regulatory clearance. This repository is a portfolio demonstration of cloud architecture, applied AI safety, and healthcare-flavoured governance.

## 4. Model and platform

| Item | Detail |
|------|--------|
| Foundation model | Anthropic Claude (Sonnet-class), accessed via Amazon Bedrock |
| Access pattern | Bedrock inference profile; **on-demand in eu-west-2 (London)** where the model is available, otherwise the **EU geographic inference profile** (processing stays within the EU). Never US / global profiles. |
| Fine-tuning | None. Behaviour is shaped entirely by the system prompt (prompt engineering), partly because fine-tuning is unavailable in eu-west-2 and partly because an auditable prompt is preferable to opaque weights for a safety-critical draft. |
| Versioning | The exact model ID + version is pinned in configuration and **recorded on every generation in the audit log**, alongside the request region and inference profile used. |
| Prompt version | v0.5 (see change history below). |

## 5. Safety behaviours encoded in the prompt (v0.5)

The prompt is the safety surface. Its load-bearing rules:

- **Report, don't invent.** The model may state only what the notes support; otherwise it writes **"Not documented"**. An honest "Not documented" is always preferred to a plausible fabrication.
- **Resuscitation status.** Never inferred from the clinical picture. A change during admission is flagged on its own marker line (`*** CHANGED DURING ADMISSION ***`) capturing direction, date, clinician, reason, and patient/family agreement. DNACPR-rescinded and pre-existing DNACPR are handled distinctly.
- **Resuscitation carve-out (v0.5, narrow).** Where a resus form or discussion is *documented to have occurred* but its recommendation is not transcribed, the model may state the most likely recommendation, **explicitly flagged as inferred** with a "confirm against the form" instruction. Where no form or discussion is documented at all, the value stays "Not documented" and inventing one is a critical failure.
- **Drug reconciliation.** Every discharge medication is tagged against the pre-admission drug history: `NEW` / `INCREASED` / `DECREASED` / `continued` / `STOPPED` / `WITHHELD`. Inpatient-only drugs must not be carried onto the discharge list. "None" and "Not documented" are kept distinct.
- **Contradictions are surfaced, not resolved.** Conflicting notes (e.g. two warfarin doses) are flagged for clarification rather than silently picked.
- **Prompt-injection resistance.** The notes field is treated as data, never as instructions; embedded "ignore previous instructions" text is ignored and flagged.
- **Permitted, flagged inference (low-stakes only).** Administrative fields such as specialty may be inferred when strongly implied, but must be tagged `(inferred — not documented, confirm)`. This never extends to resus, drugs, diagnoses, allergies, or investigations.
- **Patient version.** Plain English at Flesch–Kincaid grade ≤ 8, audience-shifted to parents/carers for paediatrics, with safety-net advice. **Non-English-speaking patients:** the patient version must prominently flag that translation / an interpreter is required and not be handed over untranslated.
- **Every output is a draft for clinician sign-off.** This is stated to the model and enforced by the UI (see §8).

## 6. Evaluation

Outputs are scored on five dimensions, three of which carry **auto-fail gates**:

| Dimension | Auto-fail? |
|-----------|------------|
| Omission of a clinically important field | — |
| Hallucination / fabrication | **Yes** — invented resus status, drug/dose, or diagnosis; or an inpatient-only drug on the discharge list |
| Resuscitation-status accuracy | **Yes** — wrong or invented status |
| Drug reconciliation | **Yes** — wrong/invented drug, dose, or a silently dropped pre-admission drug |
| Patient-version reading age (Flesch–Kincaid) | — (target ≤ 8) |

**Evaluation set:** ~19 fully synthetic scenarios — 4 signed-off seeds, 3 adversarial (prompt injection; internally contradictory notes; missing data + non-English), and 11 expansion cases (S8–S18) spanning neonatal, paediatric, obstetric, polytrauma, prolonged ITU, stroke, COPD, surgical/stoma, GI-bleed, first-seizure, and a real-world Care-of-the-Elderly case.

**Results to date** (full detail in [`EVAL_RESULTS.md`](EVAL_RESULTS.md)):

- **Run 1** (4 seeds, prompt v0.2): 4/4 PASS — but self-generated and self-scored, so treated as a smoke-test of internal consistency, not unbiased measurement.
- **Run 2** (adversarial, *independent* generation by fresh agents, v0.2): prompt injection resisted; contradictions surfaced not resolved; **one genuine failure** found (C7 — English-only patient leaflet for a Polish-speaking patient).
- **C7 fix loop:** failure → prompt v0.3 (non-English rule) → re-verified cold (PASS). A later inconsistency in specialty handling drove v0.4 (flagged-inference rule), also re-verified.
- **Run 3** (expansion S8–S18, *cold* independent generation, v0.5): **11/11 PASS, no auto-fails**, reading ages FK 3.6–6.2. The cold run additionally **caught five errors in the hand-drafted gold reference itself** (two invented resus statuses, three unfounded "None known" allergy lines), which were corrected — evidence the harness and prompt discipline are working.
- **Run 4** (independent **clinician** scoring): in progress. Eleven specialty-matched reviewer packs are out with practising clinicians; results will be folded into `EVAL_RESULTS.md`.

**Reading age:** every patient-version measured to date sits well below the grade-8 target (FK 2.3–6.2), computed with the standard Flesch–Kincaid formula.

## 7. Known limitations

- **Scoring independence is partial.** Generation has been made independent (fresh, separate model contexts with no access to the gold), but automated scoring is still performed within the same model family. The clinician review (Run 4) is the step that closes this gap; until it returns, treat the pass rates as provisional.
- **Cross-model validation not yet done.** All generation has used the same model family; behaviour on other models is untested.
- **Synthetic data only.** The scenarios are realistic but invented; they do not capture the full messiness, handwriting-OCR errors, or volume of real notes.
- **English-centric.** Non-English handling is a *flag-for-translation* behaviour, not validated multilingual clinical output.
- **The prompt is the safety boundary.** A model or provider change could shift behaviour; the eval set must be re-run on any model/prompt change before trust transfers.
- **No real-world outcome data.** No evidence yet on downstream effects (GP follow-up, readmission, clinician time saved) — those are claims to test, not claims proven.

## 8. Human-in-the-loop controls

- The UI requires the clinician to tick **"I have reviewed and edited this output"** before download is unlocked; review is per-output-tab.
- Every generation is marked `draft = true` in the audit log until that confirmation, which records a `reviewed_at` timestamp.
- A draft watermark and the model version / timestamp are shown on every output.
- Regenerating an output resets its review state.

## 9. Data handling and privacy

- **Audit log (DynamoDB):** hash-only. It stores `input_sha256`, a per-output `output_sha256`, the Cognito subject, timestamp, model version, output type, request region, and inference profile — **no patient-identifiable content**. The only mutable field is the `draft → reviewed_at` transition; there is no `DeleteItem`. KMS customer-managed-key encryption; point-in-time recovery and an append-only stream support tamper-evidence.
- **No PHI in logs.** Inputs are sanitised before any CloudWatch logging; the system prompt is never echoed back.
- **Generated documents (S3):** KMS-encrypted, signed-URL access only, lifecycle to Glacier after 90 days.
- **Residency:** all stateful resources single-region in eu-west-2; model inference constrained to the EU. Region and inference profile are recorded per generation as residency evidence.
- Controls are aligned to the NHS Data Security and Protection Toolkit (DSPT) in spirit; this is a demonstration, not a formal DSPT submission.

## 10. Prompt change history

| Version | Change | Trigger |
|---------|--------|---------|
| v0.2 | Added the GP-letter output (three outputs total) | Match the UI tabs |
| v0.3 | Non-English / interpreter rule in the patient version | C7 failure (English-only leaflet for a non-English speaker) |
| v0.4 | "Permitted, flagged inference" rule for low-stakes fields (e.g. specialty) | Inconsistent specialty handling between scenarios |
| v0.5 | Narrow resuscitation carve-out (form documented but recommendation not transcribed → flagged most-likely recommendation) | Care-of-the-Elderly scenario (ReSPECT form completed, recommendation not written down) |

## 11. Maintainer

Shina Oguntoye — portfolio project. Feedback and limitations are tracked openly in this repository; see [`EVAL_RESULTS.md`](EVAL_RESULTS.md) for the running evaluation log and the documented failure cases.
