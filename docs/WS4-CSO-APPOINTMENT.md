# Clinical Safety Officer — Appointment and Competency Records

**AI Discharge Summary Assistant**
Document version **1.3** · 18 September 2026
Made under **DCB0129 v4.2** §2.2.1 (nomination), §2.2.2 (authorisation levels), §2.3 (CSO
requirements) and §2.4.1–§2.4.2 (competency and experience records), by the **Manufacturer**.

> **Plural, from v1.1.** §2.4.2 requires competency and experience records for **all personnel
> involved in performing the clinical risk tasks**, not only the Clinical Safety Officer. §3.5
> records the second person. The v1.0 title said "Record", singular, and that was the shape of the
> omission as well as its name.

---

## 1. Top Management nomination instrument (DCB0129 §2.2.1)

§2.2.1 places the nomination on **Top Management**, not on the nominee. On a one-person project the
two are the same person, which is exactly why the act needs to be written down separately rather
than assumed from the CSO's own signature. §6 carries both signatures, in both capacities.

| Field | Value |
|---|---|
| **Health IT System** | AI Discharge Summary Assistant |
| **Nominating body** | Top Management of the manufacturer (DCB0129 §2.2.1) — on this project, Shina Oguntoye in that capacity. **Conflict declared at §5** |
| **Clinical Safety Officer nominated** | **Shina Oguntoye, MBBS** |
| **Profession** | Medical practitioner |
| **Registration body** | **General Medical Council (GMC)** |
| **Registration number** | **GMC 7646070** |
| **Registration status** | Current, with licence to practise |
| **Date of appointment** | **17 September 2026** |
| **Scope** | The Health IT System as defined at `WS4-SAFETY-CASE.md` §4, in its **demonstration** lifecycle phase |
| **Appointment type** | Internal. **Not** an outsourced arrangement — though DTAC 2.0 C1.2.5 permits one, and §5 argues one should be obtained before any real-data deployment |
| **Authority conferred** | Approval of the Clinical Risk Management Plan (§3.2.2), each version of the Hazard Log (§3.3.2) and each Clinical Safety Case Report (§3.5.2); and the authority to withhold approval of a release. **§4.8 records that the release pipeline does not currently give effect to the last of these** |
| **Authorisation levels (§2.2.2)** | **Not defined.** §2.2.2 requires them to be set in the Clinical Risk Management Plan, and no Plan exists — `WS4-SAFETY-CASE.md` §10.3 non-conformance 1. To be set when the Plan is written |

---

## 2. The four requirements of DCB0129 §2.3, addressed individually

Clause 2.3 is quoted in full, because this record's function is to evidence it and a paraphrase
would not be evidence.

> **2.3.1** A Clinical Safety Officer MUST be a suitably qualified and experienced clinician.
> **2.3.2** A Clinical Safety Officer MUST hold a current registration with an appropriate
> professional body relevant to their training and experience.
> **2.3.3** A Clinical Safety Officer MUST be knowledgeable in risk management and its application
> to clinical domains.
> **2.3.4** A Clinical Safety Officer MUST make sure that the processes defined by the clinical risk
> management process are followed.

*(Clause 2.3 is **word-for-word identical in DCB0160 v3.2**. A deploying organisation nominating its
own CSO meets the same four requirements — it does not meet them by reference to this one.
`WS4-SAFETY-CASE.md` §11.)*

| Clause | Met by | Status |
|---|---|:---:|
| **2.3.1** Suitably qualified and experienced clinician | MBBS; Foundation Programme FY1–FY2 completed 2019–2021; practising as a locum Senior House Officer since August 2021. **Directly relevant: authoring discharge summaries, GP letters and patient-facing discharge information is a core, daily part of the role this system drafts for** — the CSO has personally performed the task the system automates, across multiple specialties and multiple trusts | **Met** |
| **2.3.2** Current registration with an appropriate professional body | **GMC registration, current, with licence to practise.** The GMC is the appropriate body for a medical practitioner | **Met** |
| **2.3.3** Knowledgeable in risk management and its application to clinical domains | §3 | **Met** |
| **2.3.4** Ensures the defined processes are followed | Duties accepted at §4. **§4.8 records that no mechanism currently enables this**, so `WS4-SAFETY-CASE.md` §10.1 scores 2.3.4 **Partial**, not Met | **Partial** |

### 2.1 DTAC form 2.0 C1.2.5 — the four stated attributes

C1.2.5 requires *"the name of your Clinical Safety Officer (CSO), their profession and registration
details"*, and states four attributes. Addressed individually, because v1.0 of this record answered
three of them and skipped the fourth:

| C1.2.5 attribute | Status |
|---|---|
| A suitably qualified and experienced clinician | **Met** — §2.3.1 above |
| Holds current registration with an appropriate professional body | **Met** — §2.3.2 above. **GMC 7646070**, §1 |
| Knowledgeable in risk management and its application to clinical domains | **Met** — §3 |
| **"Have sufficient responsibility to be able to ensure processes defined in DCB0129 are followed"** | **NOT MET.** §4.8. The CSO holds the responsibility and has no mechanism to discharge it: CI deploys on any push to `main` with no clinical-safety approval step, and **prompt v0.7 was deployed on 16 September 2026 — before this appointment existed**. *This is the one C1.2.5 attribute v1.0 neither quoted nor answered, found by the omissions verification pass* |

> **Three distinctions worth stating, because they are commonly conflated.**
>
> **(a) NHS England's guidance is stricter than the standard.** The NHS England *Digital clinical
> safety assurance* page describes a CSO as *"a senior clinician"* with registration *"with a
> professional body such as the General Medical Council (GMC) or Nursing and Midwifery Council
> (NMC)"*, plus clinical safety training. **The standard's own §2.3 says none of that** — not
> "senior", not GMC or NMC by name, and no training course. This record evidences the clause; the
> NHS England gloss is noted, not adopted as the test, and is addressed on its merits at §5.3.
>
> **(b) No specific training course is mandated.** DTAC form 2.0, C1.2.5, verbatim: *"To note: the
> requirement for the Clinical Safety Officer to have undergone NHS training specific to DTAC v1.0 is
> no longer applicable. The requirements of the role are summarised above; with full details of
> expected responsibilities and competencies found in the DCB0129 documentation and implementation
> guidance. Training by a suitable provider remains a strongly recommended way of meeting these
> requirements."* No course is a gate. **But the middle sentence matters:** DTAC points assessors to
> the implementation guidance for the expected competencies — which is where (c) sits. *(v1.3: the
> v1.0–v1.2 quote elided that sentence and paraphrased the opening.)* Formal CSO
> training is a credibility and competence decision, not a gate. §5.3.
>
> **(c) But the Implementation Guidance *expects* training — and v1.0–v1.2 omitted the training
> actually done.** *Added v1.3.* DCB0129 IG v3.2, guidance to §2.3: *"A Clinical Safety Officer
> needs to have completed appropriate training. Whilst suitable training is provided by NHS Digital
> in partnership with other bodies it is recognised that there are other methods to acquire relevant
> skills, e.g. Masters modules in Patient Safety"*, preceded by: *"The Clinical Safety Officer needs
> to be suitably trained and qualified in risk management or have an understanding in principles of
> risk and safety as applied to Health IT Systems."* The IG is non-normative and names no course —
> but this record cannot dismiss it on that ground: the Report relies on the IG where it helps
> (Tables 7–10; the clause 3.4.1 upgrade), and DTAC 2.0 itself sends assessors to the implementation
> guidance for the CSO's *"expected responsibilities and competencies"* ((b) above). The v1.2 pass
> applied the IG everywhere else without testing this sentence. **Position: partly met.** Completed: NHS England *Digital Clinical Safety —
> Essentials* (2 Sep 2026) and *— Intermediate* (9 Sep 2026), the first two rungs of NHS England's own
> CSO route. **Not yet completed: the Practitioner workshop — the CSO-level course — booked for
> 16 March 2027.** The §6 approval rests on the §2.3 MUSTs, which are met, with this limitation
> declared, and is re-given as a new issue after 16 March 2027 (§5.3). The IG's Table 1
> competencies that depend on working with others — facilitating consensus, for one — cannot be
> evidenced on a one-person project at all; §3.4 item 4 and §5 already carry that limitation.

---

## 3. Competency and experience record — Clinical Safety Officer (DCB0129 §2.4.2)

§2.4.2 requires that *"competency and experience records for the personnel involved in performing
the clinical risk tasks MUST be maintained."* §3.1–§3.4 are that record for the CSO; §3.5 is the
record for the second person; §3.6 sets the maintenance provisions §2.4.2's word *maintained*
requires.

### 3.1 Clinical experience relevant to this system

| | |
|---|---|
| Qualification | **MBBS**, St George's, University of London, 2014–2019 |
| Foundation training | **FY1–FY2 completed, 2019–2021** |
| Current practice | **Locum Senior House Officer**, UK secondary care, since August 2021 |
| Domain relevance | The intended use environment (`WS2a` §1.5) is a UK secondary-care ward, and the intended user (§1.4) is *"a registered clinician who already holds responsibility for authoring the discharge summary — in practice a foundation doctor, SHO or registrar"* who *"must be capable of recognising a clinical error in a discharge summary"*. **The CSO is that user.** The clinical judgement this record relies on — what a discharge summary must contain, which omissions are dangerous, which fields must never be inferred, what a safety-netting instruction is actually for, and **which content should not reach a patient in a leaflet** (HAZ-16) — is first-hand and current, not researched |

### 3.2 Risk-management knowledge and its application to clinical domains (§2.3.3)

**No CSO-level clinical-safety qualification is held yet** — the NHS England Essentials and Intermediate modules are complete (2 and 9 Sep 2026) and the Practitioner workshop is booked for 16 Mar 2027 (§3.4 item 1). Stated first, because a competency record that
buries its gap is worthless. What is evidenced instead is applied risk-management work on this
system, all inspectable in the repository:

| Evidence | Artefact | What it demonstrates |
|---|---|---|
| **Hazard identification and analysis** | `docs/WS4-HAZARD-LOG.md` v1.3 — 24 hazards with causes, effects, initial and residual scoring, status, control owners and evidence links | §2.3.3 directly |
| **Risk-scoring scheme derivation** | `WS4-SAFETY-CASE.md` §6 — establishing from the Specification that DCB0129 contains **no matrix** and requires the manufacturer to declare one, then declaring it with its provenance and its unverified parts named | Working from the primary standard rather than a secondary account of it |
| **Risk control across a lifecycle** | `WS4-SAFETY-CASE.md` §8.2 — six prompt versions, each traced to the hazard that caused it, each with its re-verification, and **one (v0.6) recorded as having been insufficient rather than quietly superseded** | Applied risk control, including recording one's own inadequate fix |
| **Assessing a proposed control for new hazards** | `WS4-SAFETY-CASE.md` §7.4 — the §6.1.2 analysis of the clinician review gate, finding four hazards the gate would introduce, **applied to the control this Report makes a condition of release** | The discipline of not treating one's own proposed fix as costless |
| **Structured threat analysis** | `docs/THREAT_MODEL.md` — STRIDE plus eight AI-specific threats | Structured analysis predating any regulatory requirement to do it |
| **Safety-oriented evaluation design** | `evals/EVAL_RESULTS.md` §2 — five dimensions with **three auto-fail gates** on the failure modes that cause real harm; 18 synthetic scenarios including adversarial cases; cold independent generation at temperature 0 | Designing a measurement instrument around harm rather than performance |
| **Regression control on a safety-critical rule** | `evals/safety_net_gate.py` — **anchored to the source notes, not to model output** — 24 unit tests, and a module docstring that states the gate's own blind spots | The competence that matters most: knowing what a control cannot see, and writing it down beside the control |
| **Regulatory determination** | `docs/WS2a-DEVICE-DETERMINATION.md` v1.5 — intended purpose to the MHRA four-element structure; tested against Examples 5, 6, 8, 9; 12 boundary conditions; the argument-by-analogy caveat stated rather than buried | Regulatory reasoning under uncertainty |
| **Data-protection risk assessment** | `docs/WS3-DPIA.md` v2.3 — on the real NHS England template, 22-row register, two adversarial verification passes | Risk assessment on a second, independent scale (`WS4-SAFETY-CASE.md` §6.5) |
| **Standards-conformance self-assessment** | `docs/WS2b-DTAC-EVIDENCE-MAP.md` **v1.3** — 44 criteria, **18 declared gaps**, conclusion *"would not pass"* | **Willingness to record a failing self-assessment**, the single most relevant disposition for this role |

### 3.3 The disposition this record is actually evidencing

§2.3.3 asks for knowledge of risk management *and its application to clinical domains*. The
repository evidences something narrower and more useful than a certificate:

- A DTAC self-assessment that concludes **"would not pass"**, with 18 gaps in the form's own
  language.
- A device determination that **names the feature closest to the line and retains it on stated
  conditions** rather than arguing it away — and a safety case that then **reverses that trade**
  against its own earlier reasoning (HAZ-02).
- A safety case that concludes the system is **not releasable for clinical use** rather than
  manufacturing an acceptance.
- A defect log recording **seven occasions on which a check was itself unsound** — including the
  author's own first fix, the author's own first gate, and **three defects in v1.0 of the safety
  case itself**.
- **Four** non-conformances against DCB0129 identified against the author's own work, in the same
  document that claims conformance elsewhere.

### 3.4 Declared limitations of this competency record

1. **CSO-level training not yet complete.** *Corrected v1.3 — v1.0–v1.2 said "not held" and omitted
   what was.* Completed: NHS England Digital Clinical Safety **Essentials** (2 Sep 2026) and
   **Intermediate** (9 Sep 2026). Outstanding: the **Practitioner** workshop, booked **16 Mar 2027**.
   Not required by DCB0129 §2.3; strongly recommended by DTAC 2.0; *expected* by IG v3.2 (§2.1(c)). §5.3.
2. **No prior CSO appointment**, on this or any other system. A first appointment.
3. **No experience of a real deployment, a real safety incident, or a DCB0160 assurance
   conversation** with a deploying organisation. Every artefact at §3.2 concerns a system with no
   users.
4. **No independent verification of this competency assessment.** It is written by its subject.
5. **No named hazard-identification technique was used, and no hazard workshop was held.** DCB0129
   IG v3.2 §4.3 states that *"it is strongly recommended that a hazard workshop is run to support
   complete hazard identification"*, with its *"date, attendees and minutes"* recorded in the
   Clinical Risk Management File and documented in the Clinical Safety Case Report — and Appendix B
   sets out four techniques (FFA, HAZID, SWIFT, Fishbone), none of which was used by name. A
   competency record that claims applied risk-management skill should say that the applied method
   was ad-hoc: six identification sources (`WS4-SAFETY-CASE.md` §5.1) reasoned through by one
   person, plus adversarial review. **SWIFT would suit a one-person project and is the obvious next
   step**, which is a limitation with a remedy rather than an excuse.
6. **The appointment postdates the work it cites.** Prompt v0.7 was deployed on 16 September 2026;
   this appointment is dated 17 September 2026. The risk-management work was done before there was
   a CSO to do it, which is the honest sequence and is why §4.8 exists.

### 3.5 Competency and experience record — independent clinical reviewer, Run 4

§2.4.2 is plural. A **second person performed a clinical risk task on this project** and had no
record here until v1.1.

| Field | Value |
|---|---|
| **Role in the clinical risk management process** | Independent clinical review of generated outputs — **hazard identification** under §4.3.1 |
| **Task performed** | Review of a specialty-matched output pack (scenario S16, general surgery), Run 4, closed 28 May 2026 |
| **Profession** | Medical practitioner, general surgery |
| **Registration details** | **NOT RECORDED.** No registration body, number or status was captured at the time |
| **Competency and experience** | **NOT RECORDED.** Grade, years of experience and scope of practice were not captured |
| **What the task produced** | **The single most load-bearing hazard identification in the project.** The reviewer found that the patient version had added standard-of-care stoma red flags not present in the source notes — clinically correct advice the responsible clinician had not given. That finding drove prompt **v0.6**, and the verification of that fix drove **v0.7**, the safety-net gate and its 24 unit tests. It is the origin of **HAZ-01**, which carries the highest initial score of any hazard the project has actually reduced |
| **Status** | **INCOMPLETE — a §2.4.2 non-conformance**, recorded rather than omitted |

**Why this matters and is not a clerical point.** `WS4-SAFETY-CASE.md` §5.1 names observed failures
as *"the strongest hazard-identification evidence in the project"*, and this is the strongest of
those. The one piece of genuinely independent clinical judgement the project has rests on a person
whose competence is nowhere evidenced — **while §3.4 item 4 complains that the CSO's own competency
assessment is unverified.** A reviewer assessing this pack would be entitled to ask how much weight
HAZ-01's identification carries, and the honest answer is: unknown, because nobody wrote it down.

**Closure, and it also closes a second gap.** Capture the reviewer's profession, grade, registration
and scope of practice retrospectively, with consent. Then, for the further clinician review at
§12.3 Tier 3, **capture it at the point of recruitment as a matter of process** — and note that
Run 4 returned 1 substantive response of 11, so the recruitment method needs revisiting anyway.
Target: **Jan 2027** *(was Oct 2026; re-planned 18 Sep 2026 — no review round falls before then)*, with the reviewer-recruitment process written down before any further review
round.

### 3.6 Maintenance of these records (§2.4.2)

§2.4.2 requires records to be **maintained**, which v1.0 did not provide for.

| Provision | Value |
|---|---|
| **Custody** | `docs/WS4-CSO-APPOINTMENT.md`, in the Clinical Risk Management File (`WS4-SAFETY-CASE.md` §8.4), under version control |
| **Review cadence** | With each review of the clinical risk management process — **six-monthly or at any lifecycle-phase transition, whichever is sooner** (§4.5) |
| **Event triggers for immediate update** | Any change in registration status or licence to practise; any change of role or of the personnel performing clinical risk tasks; any person newly performing a clinical risk task (**their record is created before the task, not after**); completion of formal clinical safety training; appointment of an independent or outsourced CSO |
| **On a change of CSO** | The outgoing record is retained, not replaced — §3.1.2 requires the File to be maintained for the life of the system, and who approved what, when, is part of it |

---

## 4. Responsibilities accepted (DCB0129 §2.3.4)

The process is defined at `WS4-SAFETY-CASE.md` §8. The following are accepted as standing duties.

1. **Approve each version of the Hazard Log** (§3.3.2) and **each Clinical Safety Case Report**
   (§3.5.2), and the Clinical Risk Management Plan once it exists (§3.2.2).
2. **Maintain the Hazard Log** (§3.3.1) against its update triggers at `WS4-HAZARD-LOG.md` §8 —
   including, from v1.1, **re-issuing the Clinical Safety Case Report under §7.3.3** where a change
   alters clinical risk, which six prompt versions did and none of which produced one.
3. **Maintain the Clinical Risk Management File** (§3.1.2) for the life of the system, recording all
   formal documents and compliance evidence (§3.1.3) and **all decisions influencing clinical risk
   management activities** (§3.1.4) — **and the release and patch audit trail required by §7.3.4**,
   which does not exist.
4. **Establish and maintain a Safety Incident Management Log** (§3.6.1) **and the process to collect
   and review reported safety concerns** (§7.2.1), with **assessment of their impact on the ongoing
   validity of the Clinical Safety Case** (§7.2.2), **timely reporting and resolution** (§7.2.4) and
   **a record of incidents including their resolution** (§7.2.5). **Currently not met — five
   clauses, not one.** `WS4-SAFETY-CASE.md` §10.3 non-conformance 2. **This appointment's first
   outstanding action**, because there is currently no route by which anyone could report a safety
   incident to this project at all.
5. **Review the clinical risk management process at planned, regular intervals** (§2.6.1).
   **Interval set here at six months, or at any lifecycle-phase transition, whichever is sooner.
   First review due 17 March 2027.** *(This is the authoritative statement of the interval;
   `WS4-SAFETY-CASE.md` §10.1 clause 2.6.1 cites it. v1.0 of the three documents stated it three
   contradictory ways, which the verification pass caught.)*
6. **Maintain competency and experience records for all personnel performing clinical risk tasks**
   (§2.4.1–§2.4.2), per §3.6 — **creating a record before the task, not after**.
7. **Enforce the release position at `WS4-SAFETY-CASE.md` §12.** The system is not released for
   clinical use. The §12.3 Tier 1 conditions are **conditions of approval, not recommendations**,
   and no deployment on real patient data or with real clinical users may proceed until all seven
   are met and re-approved.
8. **Obtain the authority to discharge duty 7, which does not currently exist.** §2.3.4 and DTAC
   C1.2.5's fourth attribute both require *sufficient responsibility to ensure the processes are
   followed*. **CI deploys the `discharge-audit` stack on any push to `main` with no
   clinical-safety approval step**, and prompt v0.7 — a change to the safety-critical component —
   was deployed on 16 September 2026, before this appointment existed. A duty without a gate is a
   statement of intent. **Adding a CSO approval step to the release process is the act that makes
   this appointment real**, and it is the companion to §12.3 Tier 1 condition 7. Target **W7,
   16 Nov 2026** *(was Oct 2026; re-planned 18 Sep 2026)*.
9. **Refuse approval where the evidence does not support it**, including where refusal is
   inconvenient to the project's other objectives. Written down because the CSO and the developer
   are the same person, and the circumstance in which it matters is precisely the one in which
   nobody else will notice.

---

## 5. Conflict of interest, and what should change before any real deployment

### 5.1 The conflict, stated plainly

**The Clinical Safety Officer is also the sole developer, the author of every artefact this record
cites, and Top Management for the purposes of DCB0129 §2.2.** All four roles the standard
distinguishes are held by one person.

§2.2.1 requires Top Management to *"assign competent personnel **from each of the specialist areas**
that are involved in developing and assuring the Health IT System"*. **Not met and not meetable by
this project as constituted.** Recorded as a non-conformance at `WS4-SAFETY-CASE.md` §8.6 and
§10.1, not argued away here. §4.1.2's SHOULD — multi-disciplinary clinical risk analysis — is a
declared departure for the same reason.

### 5.2 The compensating measure, and its limits

**Adversarial verification by independent readers working from primary sources**, applied to every
artefact in the block. It has found a material defect on every pass:

- **WS2a** — four factual errors, two unrecorded safety-netting gaps and one code defect; then a
  second pass found the **first fix had been aimed at the wrong layer**.
- **WS2b** — a stale test figure, two mis-cited headings, one overstated count, and the ADR-007
  draft banner.
- **WS3** — 22 findings, then 26 more on the template transposition, including **a template question
  dropped entirely** and a headline correction that was itself a misdiagnosis.
- **WS4** — an omissions pass that built the standard's required-artefact list and its own hazard
  list *before* reading the documents, finding **four missed non-conformances and ten missing
  hazards** including wrong-patient association; and a fact-check pass finding **a mis-banded
  hazard, an unsupported headline figure, a correction asserted but never made, and an eval run
  absent from the project's own record**.

This is a real mitigation and it has repeatedly worked. **It is not multi-disciplinary review and
it is not independent clinical judgement.** An adversarial reader checking a document against
primary sources cannot supply what a second clinician supplies: a different view of what would
actually harm a patient on a ward. §3.5 is the evidence for that — the one genuinely independent
clinical judgement this project has obtained found a hazard no document review had, and it came
from a person whose competence was never recorded.

### 5.3 What should change, and when

| Trigger | Required change |
|---|---|
| **Before any deployment on real patient data or with real clinical users** | **Appoint an independent Clinical Safety Officer**, or obtain external clinical-safety review of the Safety Case Report and the Hazard Log by a clinician who is not the author. DTAC 2.0 C1.2.5 expressly permits an outsourced arrangement, which makes this **procurable rather than structural**. The single most valuable change available to this project's clinical-safety posture, and it does not require a legal entity |
| ~~Before this record is supplied to a receiving organisation~~ | ~~Insert the GMC registration number~~ — **done 18 Sep 2026 (GMC 7646070).** The pack at `WS4-SAFETY-CASE.md` §11.4 is now complete on registration details |
| **Before any further clinician review round** | Complete §3.5 and write down the reviewer-recruitment and record-capture process. Run 4 returned 1 of 11; the method needs revisiting regardless |
| **W7, 16 Nov 2026** *(was Oct 2026; re-planned 18 Sep 2026)* | **Add a CSO approval gate to the release process** (§4.8). Until then the fourth C1.2.5 attribute is unmet and §2.3.4 is Partial |
| **16 March 2027 (booked)** | **Complete the NHS England Digital Clinical Safety Practitioner workshop** (Essentials and Intermediate already complete). Then update §3.4 item 1 and **re-approve the Safety Case Report and Hazard Log as a new issue**. Not required by §2.3; expected by IG v3.2 and strongly recommended by DTAC 2.0. The honest reason to do it is §3.4 item 1, not the recommendation |
| **On any §12.3 condition being met** | Re-approve the Safety Case Report and the Hazard Log |
| **On publication of a revised DCB0129** | Re-assess this appointment against the revised clause 2.3. The national review closed 11 Sep 2026; **no outcome, no revised standard and no publication date announced as at 17 Sep 2026** |

---

## 6. Declarations

**As Top Management (DCB0129 §2.2.1):** I nominate Shina Oguntoye as Clinical Safety Officer for the
AI Discharge Summary Assistant, with the authority set out at §1. I record that §2.2.1's requirement
to assign competent personnel from each specialist area is **not met**, and that §2.2.2's
authorisation levels are **not defined** pending the Clinical Risk Management Plan.

**As Clinical Safety Officer (DCB0129 §2.3):** I confirm that I meet requirements §2.3.1–§2.3.3 as
evidenced at §2, that **§2.3.4 is met in duty and not in mechanism** (§4.8), that the competency and
experience records at §3 are complete and accurate **including their declared limitations and the
incomplete record at §3.5**, and that I accept the responsibilities at §4.

I have approved `docs/WS4-SAFETY-CASE.md` v1.3 and `docs/WS4-HAZARD-LOG.md` v1.3 for the system in
its **demonstration** configuration, and I have **withheld approval for clinical use**, on the
grounds and subject to the conditions at `WS4-SAFETY-CASE.md` §12.

I declare the conflict of interest at §5 and record that it materially limits the weight this
approval should be given.

| | |
|---|---|
| **Name** | Shina Oguntoye, MBBS |
| **Capacities** | Top Management (§2.2.1) and Clinical Safety Officer (§2.3) — **the conflict this creates is declared at §5** |
| **GMC number** | **7646070** |
| **Date** | 17 September 2026 (appointment) · 18 September 2026 (v1.2 and v1.3 re-approval) |

---

## 7. Change control

| Version | Date | Change |
|---|---|---|
| **1.3** | **18 Sep 2026** | **Training record corrected.** v1.0–v1.2 recorded CSO training as "not held", omitting the NHS England Essentials (2 Sep) and Intermediate (9 Sep 2026) modules already completed and the Practitioner workshop already booked for 16 Mar 2027. **New §2.1(c):** DCB0129 IG v3.2's guidance that a CSO *"needs to have completed appropriate training"* — the one IG sentence on this clause the v1.2 pass did not test. **Decision (Notion stocktake, 18 Sep 2026): the approval stands** — its scope is the synthetic demonstration only, the §2.3 MUSTs are met, and an unsigned Report would fail DTAC C1.2.4 outright; the limitation is declared and approval is re-given after the Practitioner workshop. CSO-gate target → W7; reviewer-process target → Jan 2027; cross-references → Hazard Log v1.3, Safety Case v1.3, DPIA v2.3, WS2b v1.3. **Revised the same day after an independent verification pass:** §2.1(b) DTAC quote made verbatim, restoring the elided sentence that points assessors to the implementation guidance; §2.1(c) now quotes the IG's preceding sentence and records that the IG cannot be treated as non-normative selectively; "Two distinctions" → three; §4 item 8 target → W7 (still read Oct). |
| **1.2** | **18 Sep 2026** | **GMC registration number inserted (7646070)**, closing the one item that made the §11.4 pack unfit to issue and satisfying DTAC C1.2.5's *"name, profession and registration details"*. *(Its fourth attribute — sufficient responsibility to ensure the processes are followed — remains **not met**; §2.1 and §4.8.)* **DCB0129 Implementation Guidance v3.2 applied** (`docs/DCB0129-Implementation-Guidance-v3.2.pdf`): §3.2's competency evidence re-stated against the Guidance's own apparatus, and a fourth limitation added at §3.4 — **no named hazard-identification technique was used**, where IG v3.2 Appendix B sets out four (FFA, HAZID, SWIFT, Fishbone) and §4.3 *"strongly recommends"* a hazard workshop whose *"date, attendees and minutes"* this record would have been the place to hold. Re-approval of the Safety Case Report and Hazard Log at v1.2 recorded at §6 |
| **1.1** | 17 Sep 2026 | **Omissions verification pass applied.** Title made plural: §2.4.2 requires records for **all** personnel performing clinical risk tasks, and v1.0 had one. **New §3.5** — the independent Run 4 clinical reviewer, whose finding is the origin of HAZ-01 and whose profession, registration and competence were never recorded; logged as a §2.4.2 non-conformance rather than omitted. **New §1** — a separate Top Management nomination instrument, because on a one-person project the nomination must be an act rather than an inference from the nominee's own signature; §2.2.2 authorisation levels recorded as not defined. **New §2.1** — DTAC C1.2.5's four attributes addressed individually, including **the fourth, which v1.0 neither quoted nor answered and which is not met**. **New §3.6** — maintenance provisions, since §2.4.2 says *maintained*. **New §4.8** — the CSO has the duty and no mechanism: CI deploys on any push to `main` with no clinical-safety approval step, and prompt v0.7 was deployed the day before this appointment existed. §2.3.4 re-scored **Partial**. §4.4 expanded from one clause to five. §4.5 confirmed as the authoritative statement of the §2.6.1 review interval, which v1.0 stated three contradictory ways across three documents. WS2b cited at its actual version, **v1.1**; the intended-user description corrected to the verbatim `WS2a` §1.4 wording |
| **1.0** | 17 Sep 2026 | First issue. Appointment under §2.2.1; the four §2.3 requirements evidenced individually; competency record under §2.4.2 with four declared limitations; responsibilities under §2.3.4 accepted; conflict of interest declared with a procurable remedy. Records that DCB0129 §2.3 mandates **no** specific training course and does **not** name the GMC or require seniority — NHS England's guidance does, and the two are kept separate |

---

*Referenced from `docs/WS4-SAFETY-CASE.md` §13 and `docs/WS4-HAZARD-LOG.md`.
Closes DTAC form 2.0 criterion **C1.2.5** on three of its four attributes — the fourth is
outstanding (§2.1, §4.8). `docs/WS2b-DTAC-EVIDENCE-MAP.md`.*
