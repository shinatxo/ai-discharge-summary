# Privacy notice for clinicians using the Discharge Summary Assistant

**Notice version 1.4 · 24 September 2026** · Published from Annex B of the Data Protection Impact
Assessment ([`docs/WS3-DPIA.md`](docs/WS3-DPIA.md)). This file is now the canonical version.

> **Read this first: what this system is today.** This is a **portfolio demonstration running
> on synthetic patient data only.** No real patient information may be entered, and none ever has
> been. What it *does* record is information about **you, the clinician using it** — and you are
> entitled to know what that is. There is no deploying NHS organisation behind this demonstration:
> the author of the system is the controller of the data described below. The section
> [Where this demonstration differs](#where-this-demonstration-differs-from-the-notice-above) lists
> the places where the system does not yet do what a production deployment would. Read it — it is
> part of the notice, not a footnote.

---

## How your use of the Discharge Summary Assistant is recorded

**In short: every draft you generate is recorded against your account, and kept. Your notes, as
you typed them, are not. From the release of the step-by-step version of the system (planned for
14 December 2026), a working record of how each draft was produced — which does contain clinical
content — is also kept, for 30 days.**

### What is recorded

Each time you generate a set of drafts, the system records:

- **who** — your account identifier,
- **when** — the date and time the request started and finished,
- **what** — a one-way *fingerprint* (a SHA-256 hash) of the notes you entered and of each
  document produced,
- **how much** — the size of your input and of each output, as AI token counts. *A token count
  roughly reflects how long your notes were.*
- **how** — the AI model version used, the AWS region, which documents you asked for, and the
  technical outcome of the run (completed or failed, and whether the output was read correctly).
  *From the release planned for 14 December 2026, also:* the version of the step-by-step pipeline,
  the number of AI calls, which stage failed if one did, and whether the automatic safety-netting
  check passed,
- **whether you signed it off** — a draft/reviewed marker and the time you reviewed it
  (*see [below](#where-this-demonstration-differs-from-the-notice-above) — not yet built*).

**The step record — from the release planned for 14 December 2026.** The system is being rebuilt
to produce each draft in separate stages: reading your notes, extracting the facts, reconciling
medicines, choosing the safety-netting text, and writing each document. From that release, each
generation also produces a **step record**: for every stage, what went in, what came out, which AI
model was used, how many tokens, how long it took, and whether it succeeded.

**The step record contains clinical content.** It holds the facts the system extracted from your
notes, short word-for-word quotes from your notes that each fact is checked against, and the drafts
themselves. Each quote is at most 200 characters, a limit the system enforces. It does **not** hold
your notes as you typed them — it refers to them by their one-way fingerprint — and it does **not**
hold your account identifier. It is linked to the record above by the generation's reference number
and by that same fingerprint, which also means the step records of identical notes can be matched to
each other. *Until that release, no step record is kept on the live
system — see [below](#where-this-demonstration-differs-from-the-notice-above).*

### What is *not* recorded

**The clinical notes you type are never stored by this system as you typed them.** They are sent
to Amazon Bedrock (the AWS service that runs the AI model, in AWS's London region) to produce your
drafts, held in memory only while that happens, and are then gone. They are not written to any
database, file or log in the form you entered them. **From the release of the step-by-step
version, what the system extracted from them — including short quotes — is kept in the step record
above for 30 days.** Until then, no clinical content derived from your notes is kept beyond the drafts
below — only the fingerprint and the token counts listed above. AWS states that Bedrock does not store model inputs or outputs by default, and the model this
system uses is not one of those AWS names as retaining traffic for abuse monitoring *(checked
20 September 2026; the supporting AWS statements and their limits are set out in Annex C.2 of the
DPIA)*.

The fingerprint is one-way: it can confirm that a particular set of notes produced a particular
draft, but **it cannot be turned back into the notes**, by us or by anyone else.

The drafts themselves are available to you for **24 hours**. After that the system stops
returning them, and AWS deletes them automatically *"within a few days of their expiration time"*
— not at the exact moment. *(Until this notice's release, an expired draft could still be
retrieved in that gap; the fix was released alongside it.)*
From the release of the step-by-step version, a copy of each draft also sits in the step record for
30 days. You cannot retrieve it from there; only the operator can, for the purposes below.

### Why this is recorded

The record about you exists for two reasons, and only two:

1. **Clinical safety.** If a problem is ever found with a discharge summary, we need to be able to
   establish which draft was produced, when, and with which version of the system. This is
   required of clinical IT systems under the NHS clinical risk management standard DCB0129.
2. **Non-repudiation.** The record shows that a document was generated and whether a clinician
   reviewed it — which protects you as much as it protects the patient.

The step record exists for the first of those reasons: if a problem is found with a draft, it shows
**which stage of the system caused it**. On real patient information that is the only thing it may
be used for. Testing and measuring how well each stage works is done **on synthetic notes only**,
never on step records from real patients.

### What it will *not* be used for

**Neither record will be used for performance management, appraisal, productivity monitoring, or
any comparison between clinicians.** It is not a measure of how much work you do or how fast you
do it, and it will not be presented as one. That includes the token counts above.

If anyone ever proposes using it for a different purpose, that requires a documented assessment
that the new purpose is compatible with this one — it cannot simply be repurposed.

### How long it is kept

| What | How long |
|---|---|
| Your clinical notes | Not kept at all |
| The drafts produced | 24 hours, then deleted automatically (AWS says *"within a few days"* of expiry; the platform does not guarantee the exact moment) |
| The step record *(from the release planned for 14 Dec 2026)* | **30 days**, then deleted automatically, again within a few days of that |
| A step record copied into a safety incident record | If a safety problem is reported, the step record for that draft is copied into the safety incident record, **with patient identifiers removed unless the investigation needs them**, and kept with the clinical safety file **for as long as the system runs** — the same rule that applies to the rest of that file |
| The record linked to **you** | In a deployment: a period set by your organisation — **8 years by default**. *In this demonstration: see below* |
| After that | Your identifier is removed, and only an anonymous record that a generation occurred is kept, for as long as the system runs. *In this demonstration: not yet built — see below* |

The second stage exists so the safety record survives without continuing to hold a record of who
did what. *(The organisation-set period and its default are explained at §7.2 of the DPIA,
including why the 8-year figure is an analogy rather than a legal requirement.)*

### Who can see it

Your own drafts are visible only to you — the system returns another user's generation as "not
found", not as "not allowed". The step record is not visible to you or to any other user in the
application at all. The person who operates the service can reach the stored records in
order to run and maintain it; that access is described at §6.6 of the DPIA.

### Your rights

You can ask for a copy of everything recorded about you, and it can be produced.

Because this record exists for clinical-safety and non-repudiation reasons, **it cannot generally
be deleted on request** — a safety audit trail that can be erased by the person it attributes
would not be an audit trail. In a deployment, what happens instead is the second stage above: your
identifier is removed at the end of the retention period.

If you have a question or a concern about any of this, or want to object to the processing:
**in this demonstration, contact the author** — [open an issue on this repository](https://github.com/shinatxo/ai-discharge-summary/issues)
for anything that can be said in public, or email **shinaoguntoye@hotmail.co.uk** for anything
that cannot; the contact details are also on [shinaoguntoye.dev](https://shinaoguntoye.dev). In a deployment, the
contact would be your organisation's Data Protection Officer.

### If you use this system as a locum, bank or agency clinician

This applies to you in exactly the same way. The Information Commissioner's Office is explicit
that monitoring rules apply *"regardless of the nature of the contract"*.

---

## Where this demonstration differs from the notice above

A notice that describes controls which do not exist yet would be worse than no notice. These are
the five places where the current system falls short of, or differs from, the text above:

1. **There is no sign-off step yet.** Every record is marked *draft* and the "time you reviewed it"
   field is empty, because the review-and-sign-off screen has not been built. It is scheduled for
   December 2026.
2. **Your identifier is not yet removed after a retention period.** The records linked to your
   account currently have **no expiry** and are kept until the demonstration is shut down or the
   removal step is built. No retention period has been set, because there is no deploying
   organisation to set one.
3. **There is no deploying organisation or Data Protection Officer.** The author of the system is
   the controller and the contact point (above).
4. **This notice is published in the repository, not yet shown in the application.** Showing it
   at first sign-in, and linking to it from the application, is scheduled for December 2026.
5. **The step record does not exist yet on the live system.** It is being built and tested on
   separate, temporary test deployments that process **synthetic notes only** and are deleted after
   use. It starts on the live system with the release planned for 14 December 2026, and this notice
   will be re-issued then to confirm it — or to correct it, if what is released differs from what
   is described here.

---

*Notice version 1.4 · 24 September 2026 · Changes from v1.3: the step record introduced by
`docs/ADR-phase1.md` ADR-009 described ahead of its release — what it holds (including clinical
content), what it does not, 30-day retention, who can see it, why it exists — and added as the
fifth difference above; "never stored" narrowed to "never stored as you typed them", which stays
true before and after that release; the drafts' deletion timing corrected from "usually within a
day" to AWS's documented "within a few days" (DynamoDB TTL documentation, read 24 Sep 2026), and the
status endpoint's new expiry check (released with this version) stated; the private email route
added to the contact; use of step records on real patient
information limited to investigating that draft; the incident-copy retention period stated; the
new pipeline fields added to "What is recorded". Checked by an independent verification pass,
24 Sep 2026. To be
reviewed alongside the DPIA (§11.3) and re-issued at the 14 December 2026 release.*

*Notice version 1.3 · 20 September 2026 · Changes from the DPIA's Annex B draft (v1.2, 17 Sep):
token counts and run status added to "What is recorded"; Amazon Bedrock named as where the notes
are processed; the demonstration context and the four gaps above stated; the demonstration
contact given.*
