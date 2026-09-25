# WS4 — Safety Incident Management Log

**AI Discharge Summary Assistant**
Document version **1.0 — DRAFT for CSO approval** · 24 September 2026 · Author: Shina Oguntoye
Produced under **DCB0129 v4.2** §3.6.1 (*"The Manufacturer MUST maintain a Safety Incident
Management Log"*) and §7.2.5 (*"A record of safety incidents, including their resolution, MUST be
maintained by the Manufacturer in a Safety Incident Management Log"*), with the fields prescribed
by **DCB0129 Implementation Guidance v3.2 §3.6** (`docs/DCB0129-Implementation-Guidance-v3.2.pdf`,
p. 31). Closes safety-case non-conformance **NC-2** (`WS4-SAFETY-CASE.md` §10.3) once approved and
published. Part of the Clinical Risk Management File; referenced from `WS4-SAFETY-CASE.md` and
`WS4-HAZARD-LOG.md`.

> **Read this first.** This log is **public**, because the repository is. It therefore never holds
> patient information, clinical notes or anything that identifies a person — not even de-identified
> clinical content. Every entry refers to its evidence by **reference** (a generation ID, a hash, a
> ledger object key, an evidence-store path) and keeps the evidence itself in the private evidence
> store (§4). The system runs on **synthetic data only**; if real patient information is ever
> entered, that is itself an incident (§2, category E).

---

## 1. How an incident is reported — the published contact

| Route | Use it for | Where it is published |
|---|---|---|
| **GitHub issue**, template *Safety incident* (label `safety-incident`) | Anything that can be described without personal information | `README.md` → *Reporting a safety concern*; `.github/ISSUE_TEMPLATE/safety-incident.md` |
| **Email** — shinaoguntoye@hotmail.co.uk, subject "Safety incident" | Anything that might identify a person, anything that should not be public, and **any report that real patient data has been entered** | Same places |
| **Self-reported** | Anything the author, the canary, an alarm, an eval run or a verification pass finds that has — or could have — a patient-safety effect | This log directly |

**Commitments, stated honestly for a one-person project:** acknowledge within **2 working days**;
clinical risk assessment (§2) within **5 working days**; *made safe* the same day as the
assessment for anything scored 3 or above. These are targets, not a service level, and the key
performance indicators in §5 measure them.

IG v3.2 §7.2 asks that the process let users report, provide a central point of contact, and advise
the user community. Here the central point is the author; advice to users is a pinned GitHub issue
plus a line in `README.md` for the duration of an open incident rated 3 or above.

---

## 2. What happens to a report — the process (IG v3.2 §7.2)

1. **Log it.** Give it the next reference number, `INC-YYYY-NNN`, and record *Reported by*,
   *Reported date* and an *Incident summary* written without personal information. Put the
   evidence in the private store (§4) first, and record where it went.
2. **Assess clinical risk** with the scheme the hazard log already uses — severity × likelihood of
   re-occurrence → risk level 1–5 (`WS4-HAZARD-LOG.md` §2.3) — and answer IG v3.2's three questions
   in the *Clinical risk assessment* field:
   - **Is it a new hazard?** If yes, add it to the hazard log.
   - **Is it a realisation of a recorded hazard?** If yes, name the `HAZ-nn`.
   - **Have clinical risk controls failed?** If yes, name which ones, and re-examine that
     hazard's residual.

   **Categories**, for triage:
   - **A** — unsafe clinical content in an output: invention, omission, corruption or disclosure.
   - **B** — a control failed to operate: gate, flag, review state or residency.
   - **C** — integrity of the audit trail or ledger.
   - **D** — availability at the point of use.
   - **E** — real patient data entered into a synthetic-only system.
3. **Make it safe.** Apply short-term risk controls and record the *Made safe date*. In rising order
   of force:
   - pin a warning issue and add a README line;
   - disable the canary, if it is the source. **Do it in `.github/workflows/ci-cd.yml`**
     (`CanaryEnabled=off`) and push. CI pins the value, so a hand change to the stack is undone by
     the next push to `main` — which may well be the commit that logs this incident;
   - **stop generation** by setting the dispatcher's reserved concurrency to 0:
     `aws lambda put-function-concurrency --function-name discharge-audit-dispatcher --reserved-concurrent-executions 0`.
     New requests are throttled. The status endpoint and any in-flight worker are unaffected, the
     audit trail is untouched, and CloudFormation leaves the setting alone because the template does
     not manage it. **Expect** the canary to fail and its alarms to email you while this is in
     force. **Undo** with
     `aws lambda delete-function-concurrency --function-name discharge-audit-dispatcher`, and record
     both times in the *Journal*. Do not delete the API route by hand: that causes stack drift the
     next deploy would silently reverse.
   - **For category E:** also purge the affected `ResultsTable` item and, from W11, its trace
     items, record that the purge happened, and treat it as a **personal-data breach** for DPIA
     R-17 purposes. The data is deleted; the fact of the incident is not.
4. **Find the root cause.** Fill in the *Cause* field. Where the cause is a control, apply
   `WS4-HAZARD-LOG.md` HAZ-13: verify the verification instrument against its source of truth.
5. **Put a permanent control in place, then close.** Record the *Closed date*, and record the
   permanent control in the *Journal*. If the incident changed clinical risk, the hazard log is
   updated and — where required — the Clinical Safety Case Report is re-issued (DCB0129 §7.3.3),
   with CSO approval. The fix is released through the normal release route (the W7 release
   register, once it exists).
6. **Tell people.** Close the pinned issue with a resolution note, and remove the README line.

---

## 3. The log

The fields are IG v3.2 §3.6's ten, in its order. One column is added — **Hazard link** — so the
log and the hazard log cross-reference each other.

| Reference | Reported by | Reported date | Incident summary | Clinical risk assessment | System release | Journal | Made safe date | Closed date | Cause | Hazard link |
|---|---|---|---|---|---|---|---|---|---|---|
| — | — | — | *No incidents reported as at 24 Sep 2026.* | — | — | — | — | — | — | — |

**Field notes:**
- **System release** is the configuration as `WS4-SAFETY-CASE.md` states it — prompt version (from
  W11, `pipeline_version`), stack name and last deploy time, and the parameters that matter. It is
  never just a commit.
- **Journal** entries are dated, and each one says who, when and what (IG wording).
- **Reported by** holds only the reporter's chosen handle or "by email". The email address
  stays in the private store, because this file is public.

---

## 4. Evidence — capture, preservation and the trace export (ADR-009)

**The rule: capture first, while the evidence still exists.** Every store the system has expires
on a clock:

| Evidence | Where | Expires | How to capture it |
|---|---|---|---|
| Audit row (hashes, model, times, status) | `AuditTable`, `PK = USER#<sub>`, `SK = GEN#<id>` | Never (ADR-007) | Reference by generation ID. No copy is needed; it is hash-only |
| Ledger copy of every change to the row | S3 Object Lock bucket | `LedgerRetentionDays` — **183 days** from the W1 change set (1 day before it) | Record the object keys. Under Governance mode, copy them to the evidence store if the investigation may outlast retention |
| The three drafts | `ResultsTable` `RES#<id>` | **24 hours** (served for 24 h; physically deleted within a few days) | Export immediately — they contain clinical content, so into the **private** store only |
| **Step traces** *(from the W11 cut-over)* | `TraceTable`, `PK = GEN#<id>` | **30 days** | The export procedure below |
| Lambda logs | CloudWatch | 30 days | Filter on the generation ID; they contain no clinical content |

**The trace export procedure** — ADR-009 (b) and DPIA §7.2. This is ready for the W11 cut-over;
before then there are no traces.

1. **Export, don't extend.** No code path removes a `ttl`. From **AWS CloudShell** — inside the
   account, so nothing touches a laptop disk — pipe the query straight into the private evidence
   store:
   ```bash
   aws dynamodb query --table-name discharge-audit-traces \
     --key-condition-expression "PK = :p" \
     --expression-attribute-values '{":p":{"S":"GEN#GENERATION_ID"}}' --output json \
   | aws s3 cp - "s3://EVIDENCE_BUCKET/INC-YYYY-NNN/trace-raw.json" \
       --sse aws:kms --sse-kms-key-id alias/discharge-audit-audit
   ```
   Never save it to a laptop, and never put it in the repository.
2. **De-identify, in CloudShell, into a second object.** Read `trace-raw.json` from the store and
   write `trace-deid.json` next to it with the patient identifiers removed:
   - the facts object's patient-identifier fields;
   - the identifier lines in PART A's header;
   - any quote that contains an identifier.

   Keep the clinical content, because the investigation needs it. Then delete `trace-raw.json`,
   unless the investigation needs the identifiers — in which case say why in the *Journal*.
   The raw object stays under the store's access control either way.
3. **Record the export in the *Journal*:** who exported it, when, which item keys, and whether it
   was de-identified.
4. **Retention:** the de-identified export becomes part of the Clinical Risk Management File and is kept **for the
   life of the Health IT System** (DCB0129 §3.1.2). This is the one place patient-derived content
   outlives 30 days, and the privacy notice (v1.4) and DPIA (§7.2) say so.

**Category E is the exception: capture nothing.** When real patient data has been entered, do not
copy it anywhere — not even into the evidence store. Record only the generation ID, the
`input_sha256` and the item keys, then purge (§2, step 3).

**The private evidence store does not exist yet.** It is to be created with the first incident that
needs it, or at the W11 cut-over, whichever comes first. The plan is:
- a dedicated S3 bucket in the same account and region;
- SSE-KMS with the existing CMK;
- Block Public Access on and versioning on;
- access for the author/CSO principal only;
- no lifecycle expiry.

It is logged as owed in `docs/ADR-phase1.md` ADR-009. Until it exists, the only evidence the
system can produce by itself is hash-only — which, for a synthetic demonstration, is enough.

---

## 5. Key performance indicators (IG v3.2 §7.2)

Reported in the *Journal* of each incident, and summarised here at each safety-case re-issue:

- time to acknowledge (target: 2 working days);
- time to clinical risk assessment (target: 5 working days);
- time to made safe;
- time to close;
- open incidents by risk level.

*No incidents to summarise as at 24 Sep 2026.*

---

## 6. Change control

| Version | Date | CSO approval | Change |
|---|---|---|---|
| **1.0 — DRAFT** | 24 Sep 2026 | Pending, with `WS4-HAZARD-LOG.md` / `WS4-SAFETY-CASE.md` v1.4 | First issue. IG v3.2 §3.6's ten fields, plus a hazard link. Published contact: GitHub issue template + email. Process follows IG v3.2 §7.2. Evidence-capture table covers every store's expiry. ADR-009 trace-export procedure, de-identified on export by default. The private evidence store is declared as not yet existing. Checked by an independent verification pass the same day: made-safe steps now go through CI where CI pins the value; the trace export runs in CloudShell straight to the store; category E captures nothing |
