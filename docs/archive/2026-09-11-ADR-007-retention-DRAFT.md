# ADR-007 — Retention and expiry of generation records

> **DRAFT — 11 Sep 2026.** Awaiting author approval; not yet folded into `ADR-phase1.md`. Regulatory positions below were verified against primary sources on 11 Sep 2026 and are dated accordingly — re-verify before quoting them in the DPIA.

## Status

Proposed. Supersedes nothing. It **surfaces** a decision already taken in `infra/template.yaml`, and **extends** it, because the original decision answered "does this row expire?" without answering "for how long, and on whose authority?"

## Context

### The decision already existed, in a CloudFormation comment

`infra/template.yaml` L301–306:

> TTL added in slice 4b for the IDEM# rows (the dispatcher's idempotency receipts). DynamoDB only deletes items that ACTUALLY carry the `ttl` attribute — the GEN# audit rows are written without it, so they never expire and the hash-only audit log retains its immutability.

Real reasoning, correctly implemented. But it lives where no DPIA reader, Clinical Safety Officer or procurement reviewer will find it, and "never expires" is indefinite retention — a position that must be argued, not inherited from the absence of an attribute.

### The table holds two different kinds of data

By ADR-002 the audit rows are hash-only: no clinical content. What they do hold is `user_sub` (the clinician's Cognito subject), a timestamp, `input_sha256`, `output_sha256`, model version, output type, request region and inference profile.

So there is no patient personal data here at all. There is **clinician** personal data — a record of who generated what, and when. That distinction drives everything below, because the two halves are governed by different regimes and have no business sharing a retention period.

## What the law and the standards actually require

*Verified 11 Sep 2026. Where no requirement exists, that is stated as a finding rather than filled with an assumption.*

**The NHS Records Management Code of Practice 2023 (v5) sets no retention period for audit trails, system logs or access logs.** Every Appendix II sub-schedule was checked. The nearest entries are "Clinical audit — 5 years" (audit *projects*, not system trails) and "Telephony systems record — 1 year". The Code's only signal is indirect: on EPR decommissioning it says *"The system, along with the audit trails, should be retained"* — implying an audit trail follows the record it documents. The Code also binds **NHS and adult social care organisations as controllers**; Appendix III characterises a system supplier as a **processor**, reached through contract rather than by the Code directly. *Any claim that "the NHS Code requires N years for audit logs" is extrapolation.* — [Code](https://digital.nhs.uk/data-and-information/information-governance/guidance/records-management-code-of-practice), [Appendix II](https://digital.nhs.uk/data-and-information/information-governance/guidance/records-management-code-of-practice/appendix-ii), [Scope](https://digital.nhs.uk/data-and-information/information-governance/guidance/records-management-code-of-practice/scope-of-the-code)

**The DSPT gives a floor of six months, for a different purpose.** CAF-aligned DSPT Principle C1 relays NCSC advice that logs answering incident-investigation questions be retained *"for a minimum of 6 months"*, with the normative requirement being to *"define and implement an appropriate retention period"* per log type. This is **security monitoring**, not clinical non-repudiation, and IT suppliers complete assertions-based Category 2/3 rather than the CAF view — so it reaches a supplier contractually. Treat six months as a floor to justify departing from, not a target. — [DSPT C1](https://digital.nhs.uk/cyber-and-data-security/guidance-and-resources/caf-aligned-dspt-guidance/objective-c/security-monitoring), [NCSC logging](https://www.ncsc.gov.uk/guidance/introduction-logging-security-purposes)

**DCB0129 v4.2 sets no period, but imposes an open-ended duty.** §3.1.2: *"The Clinical Risk Management File MUST be maintained for the life of the Health IT System."* No destruction date anywhere in the standard; DCB0160 v3.2 is materially identical. Note also that DUAA 2025 s121 extended the s250 information-standards compliance duty to **IT providers** from 5 Feb 2026, and s251ZA carries a power to require production of records — you cannot produce what you destroyed. — [DCB0129 spec v4.2](https://digital.nhs.uk/data-and-information/information-standards/governance/latest-activity/standards-and-collections/dcb0129-clinical-risk-management-its-application-in-the-manufacture-of-health-it-systems/), [DUAA s121](https://www.legislation.gov.uk/ukpga/2025/18/section/121)

**The ICO prescribes no period, and mandates a schedule.** *"The UK GDPR does not dictate how long you should keep personal data. It is up to you to justify this, based on your purposes."* Pseudonymisation does not help: *"Pseudonymised data is personal data in the hands of someone who holds the additional information"* — and `user_sub` resolves to a named clinician by design, which is the whole point of a non-repudiation log. — [Storage limitation](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/data-protection-principles/a-guide-to-the-data-protection-principles/storage-limitation/), [Pseudonymisation](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/data-sharing/anonymisation/pseudonymisation/)

**This log is worker monitoring.** The ICO's monitoring-workers guidance has a *"Time and access control"* category expressly covering *"controlling access to IT and other systems"*, and applies *"regardless of the nature of the contract"* — so locums and bank staff are in scope. Its retention requirement is mandatory in form: *"You **must** ensure you have a retention schedule and delete any information you collect from monitoring workers in line with your schedule."* It also requires transparency — *"you **must** make sure workers are aware of how and what personal information you are collecting during any monitoring"* — and constrains repurposing: a purpose may only change where the new purpose is compatible, consented to, or legally required. Note this guidance is itself marked as under review following the DUAA. — [Monitoring workers](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/employment/monitoring-workers/), [Methods](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/employment/monitoring-workers/specific-data-protection-considerations-for-different-ways-or-methods-of-monitoring-workers/)

**Net position.** Nothing names a number. The binding constraints are a six-month contractual floor for security logs, an open-ended clinical-safety duty pulling long, storage limitation pulling short, and — decisively — a *mandatory* obligation to have a documented schedule at all. **Indefinite retention with no schedule fails that obligation before the period is even argued.**

## Current state

*Verified against `infra/template.yaml` and `src/`, 11 Sep 2026.*

| Class | Where | Expiry today | Mechanism |
|---|---|---|---|
| Generated outputs | DynamoDB `ResultsTable` | **24 hours** | `ttl` per item, `RESULTS_TTL_HOURS` default 24 (`src/generate/app.py:451`). KMS-CMK; no stream, no PITR |
| Idempotency receipts (`IDEM#`) | DynamoDB `AuditTable` | TTL'd | `ttl` written by dispatcher (`src/dispatcher/app.py:397`) |
| Audit rows (`GEN#`) | DynamoDB `AuditTable` | **Never** | No `ttl` attribute — deliberate. KMS-CMK, PITR 35 days, stream on, no `DeleteItem` in the IAM policy |
| WORM ledger copy | S3 `LedgerBucket` | `LedgerRetentionDays` — **1 day in demo**, unset for prod | Object Lock (Governance in demo / Compliance in prod), versioning, KMS-CMK with Bucket Key, `DeletionPolicy: Retain` |
| Lambda logs | CloudWatch | 30 days | `RetentionInDays: 30` |

Note the asymmetry: the audit row is permanent, while the WORM copy that makes its immutability *provable* expires after a day in the demo configuration.

## Decision

**Split the audit row's lifetime in two, because it holds two kinds of data.**

1. **Generated outputs stay at 24 hours.** A delivery buffer between worker and poll. The clinician's copy of record is whatever they put in the EPR; nothing downstream may treat `ResultsTable` as storage.

2. **`GEN#` rows are never deleted or mutated by the write path.** Retention is not implemented by adding a `ttl` attribute — that would hand the expiry mechanism to the same code that creates the record, defeating the non-repudiation property ADR-002 exists to provide.

3. **Attribution phase — the full row, including `user_sub`, is retained for a period set by the deploying organisation.** We are the processor; the controller sets retention. Where a deployment specifies nothing, the default is **8 years**, matching the adult health-record period in the Records Management Code — on the argument that a record of what was generated for an episode should not outlive the episode's own record. *This is reasoning by analogy, not a citation; the Code has no audit-trail entry. State it that way in the DPIA.*

4. **Integrity phase — at the end of the attribution period, `user_sub` is removed or replaced with a salted forward-hash, and the de-identified row is retained for the life of the Health IT System** per DCB0129 §3.1.2. What survives is the fact that a generation occurred, its input and output hashes, model version, region and timestamp — enough to prove the integrity chain and to investigate a safety incident, without continuing to hold a record of which clinician did it.

5. **The transition is a separate, authorised lifecycle operation**, not a write-path capability. It requires its own IAM principal, and each transition is itself logged.

6. **`LedgerRetentionDays` must be set for any non-demo deployment**, in Compliance mode, to at least the attribution period. Left at the 1-day demo default, the immutability claim in `THREAT_MODEL.md` is true for twenty-four hours and false thereafter.

### Why not the alternatives

- **Indefinite, unqualified.** Not available: it fails the ICO's mandatory retention-schedule requirement for worker-monitoring data regardless of how good the purpose is.
- **A single bounded period for the whole row.** Either too short for the DCB0129 "life of the system" duty, or too long for `user_sub`. The split exists precisely because one number cannot satisfy both.
- **Six months (the DSPT/NCSC floor).** Wrong purpose. That floor is calibrated to attacker dwell time, not to clinical incident investigation, which runs for years.

## Consequences

**Declared now (what WS3 needs):** the DPIA can state a period and a basis per data class. `MODEL_CARD.md` §9 gains a retention line it currently lacks. The clinician-not-patient distinction goes into the DPIA explicitly — it is the reasoning that separates a real assessment from a template.

**Built later (v2.1+, logged not silently deferred):** the de-identification lifecycle job and its authorisation path. Declaring the period is what September requires; implementing it is not.

**New scope for WS3 — a control that does not exist.** Nothing in the product or its documentation tells clinicians that their generations are attributed and retained. Under the monitoring guidance that transparency is mandatory, and it cannot be satisfied by a line in a threat model they will never read. A worker-facing privacy notice belongs in the IG pack.

**A constraint to record:** this log is collected for clinical safety and non-repudiation. It may not be repurposed for performance management or appraisal without a compatibility assessment. Writing that limitation down now is cheap; discovering it after someone asks for "a report on who is using the tool" is not.

## Unverified / to re-check before the DPIA quotes this

- The 8-year default is an analogy to a health-record period, **not** an audit-trail citation. Do not present it as one.
- ICO monitoring-workers guidance is expressly under review following the DUAA; the pages carry 2026 dates but may move.
- Whether the DSPT supplier view (Category 2/3) states a log-retention figure of its own — not found, not excluded.
- The DCB0129/0160 national review closed 11 Sep 2026. A revised standard may change the "life of the system" duty.
