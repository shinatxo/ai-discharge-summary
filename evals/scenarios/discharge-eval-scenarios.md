# Discharge Summary Assistant — Eval Scenarios (seed set)

> **All data here is fully synthetic.** Fictional names, fabricated NHS numbers, invented clinical details. No real patient information.
>
> Purpose: these scenarios (a) give us realistic free-text input to prompt-engineer against, and (b) seed the ~20-case evaluation set required by Phase 4. Each scenario has an **input** (messy ward-round notes, as a junior doctor would actually write them) and an **expected output** (the structured discharge summary the AI should produce). The gap between AI output and expected output is what we score.

Scoring dimensions (applied to every scenario):
- **Omission** — did it drop a clinically important field?
- **Hallucination** — did it invent anything not in the notes? (Automatic fail for resus status, drugs, diagnoses.)
- **Resus status accuracy** — correct current status + change correctly flagged.
- **Drug reconciliation** — discharge meds correct, changes from pre-admission flagged.
- **Patient-version reading age** — Flesch-Kincaid grade ≤ 8 (patient output only).

---

## Scenario 1 — Elderly NSTEMI with resuscitation-status change

**Tests:** polypharmacy, dose changes, DNACPR placed mid-admission, cardiac follow-up, "not for revascularisation" nuance.

### INPUT — free-text ward-round notes

```
Day 1 (12/05) 82F via ED. PC: central chest pain + SOB on exertion x2/7.
PMH: HFrEF (EF 35%), CKD 3b, T2DM, HTN, prev MI 2019.
DH: ramipril 5mg OD, bisoprolol 5mg OD, furosemide 40mg OD, metformin 1g BD,
atorvastatin 80mg ON, aspirin 75mg OD. NKDA.
O/E: HS I+II+0, bibasal creps, BP 148/86, HR 92, sats 94% RA.
ECG: TWI V4-6. Trop 287 -> 412.
Imp: NSTEMI. Plan: add ticagrelor (DAPT), fondaparinux, cardiology r/v, monitor U&E.
For resus.

Day 2 (13/05) Cardiology r/v. NSTEMI confirmed, GRACE high. For inpatient angiogram.
Continue DAPT. Watch renal fn w/ contrast. Creat 168.
For resus.

Day 3 (14/05) Angio: severe 3VD, not for PCI. MDT -> medical mgmt.
Family meeting. Given frailty + comorbidities + inoperable disease, DNACPR discussed +
agreed w/ pt + family. DNACPR form completed by Dr Patel 14/05.
Bisoprolol up to 7.5mg OD. Added ISMN 30mg OD.

Day 4 (15/05) Stable. Mobilising w/ physio. Creat improving 142. Cardiac rehab referral done.

Day 5 (16/05) Fit for discharge. TTOs done. GP: monitor renal fn, uptitrate ACEi as tolerated.
Cardiology OPA 6/52.
```

### EXPECTED OUTPUT — structured discharge summary

```
DISCHARGE SUMMARY

Patient: Margaret Owusu | DOB: 03/08/1943 (82y) | NHS No: 123 456 7890 | Hosp No: A1234567
Admitted: 12/05/2026 | Discharged: 16/05/2026
Specialty: Cardiology / General Medicine

PRESENTING COMPLAINT
Central chest pain and exertional breathlessness for 2 days.

DIAGNOSIS
Primary: Non-ST-elevation myocardial infarction (NSTEMI)
Secondary: Severe three-vessel coronary disease — not suitable for revascularisation;
Heart failure with reduced ejection fraction (EF 35%); CKD stage 3b; Type 2 diabetes;
Hypertension; Previous MI (2019)

KEY INVESTIGATIONS
Troponin rise 287 → 412 ng/L. ECG: T-wave inversion V4–V6.
Coronary angiogram: severe 3-vessel disease, not for PCI (managed medically per MDT).
Renal function: creatinine peaked 168, improved to 142 µmol/L by discharge.

TREATMENT DURING ADMISSION
Dual antiplatelet therapy (aspirin + ticagrelor), fondaparinux. Inpatient coronary
angiogram. Heart failure therapy optimised.

RESUSCITATION STATUS
DNACPR. *** CHANGED DURING ADMISSION *** Patient was initially for resuscitation;
DNACPR decision made and documented 14/05/2026 by Dr Patel, following angiogram findings
of inoperable severe 3-vessel disease, MDT discussion, and agreement with patient and family.

MEDICATIONS ON DISCHARGE
- Aspirin 75mg OD (continued)
- Ticagrelor 90mg BD (NEW)
- Bisoprolol 7.5mg OD (INCREASED from 5mg)
- Isosorbide mononitrate 30mg OD (NEW)
- Ramipril 5mg OD (continued)
- Furosemide 40mg OD (continued)
- Metformin 1g BD (continued)
- Atorvastatin 80mg ON (continued)

ALLERGIES: None known.

FOLLOW-UP
Cardiology outpatient appointment in 6 weeks. Cardiac rehabilitation referral made.

GP ACTIONS
Monitor renal function. Uptitrate ACE inhibitor as tolerated.

VTE ASSESSMENT: Completed on admission.
Author: [name, grade, date]
```

---

## Scenario 2 — Uncomplicated laparoscopic appendicectomy

**Tests:** simple surgical case, short stay, "For resus" with no change, post-op meds, pending histology, safety-net advice.

### INPUT — free-text ward-round notes

```
Day 1 (20/05) 28M ED referral. PC: RIF pain 18h, began periumbilical then migrated. N+V, anorexia.
PMH: nil. DH: nil. NKDA.
O/E: T 37.9, tender RIF c guarding, Rovsing +ve.
Bloods: WCC 14.2, CRP 68. Urine dip -ve. USS: appendix not well seen, free fluid RIF.
Imp: acute appendicitis. Plan: NBM, IV fluids, IV co-amoxiclav, consent lap appendicectomy.
For resus.

Day 1 (later) Lap appendicectomy. Findings: acutely inflamed appendix, no perf. Uncomplicated. EBL minimal.

Day 2 (21/05) Post-op well, tolerating diet, pain controlled, apyrexial, mobilising.
Wounds clean. Fit for discharge. TTOs: co-codamol PRN + ibuprofen.
Histology pending. Sutures absorbable - no removal. Wound care + safety-net advice given.
```

### EXPECTED OUTPUT — structured discharge summary

```
DISCHARGE SUMMARY

Patient: Daniel Reeves | DOB: 11/02/1998 (28y) | NHS No: 234 567 8901 | Hosp No: B2345678
Admitted: 20/05/2026 | Discharged: 21/05/2026
Specialty: General Surgery

PRESENTING COMPLAINT
Right iliac fossa pain for 18 hours, originally periumbilical then migrated. Nausea, vomiting, anorexia.

DIAGNOSIS
Primary: Acute appendicitis

KEY INVESTIGATIONS
WCC 14.2, CRP 68. Urine dipstick negative. USS abdomen: appendix not well visualised, free fluid in RIF.

TREATMENT DURING ADMISSION
Laparoscopic appendicectomy (20/05/2026) — acutely inflamed appendix, no perforation,
uncomplicated. IV co-amoxiclav, IV fluids perioperatively.

RESUSCITATION STATUS
For resuscitation. No change during admission.

MEDICATIONS ON DISCHARGE
- Co-codamol 30/500, 2 tablets QDS PRN (NEW)
- Ibuprofen 400mg TDS PRN (NEW)

ALLERGIES: None known.

FOLLOW-UP
Histology pending — results to be communicated to GP. No routine surgical follow-up required.

GP ACTIONS
None specific. Review if histology flags concern.

PATIENT ADVICE
Wound care advice given. Sutures absorbable — no removal needed. Safety-net advice on signs
of infection / complications and when to seek help.

VTE ASSESSMENT: Completed on admission.
Author: [name, grade, date]
```

---

## Scenario 3 — Mental health admission

**Tests:** risk assessment + change over admission, crisis/safety plan, CMHT/community follow-up, SSRI initiation, safeguarding (children at home), MHA legal status, *resus status genuinely not documented* (deliberate test of the "output Not documented if unclear" rule), patient version handled sensitively.

### INPUT — free-text ward-round notes

```
Day 1 (18/05) 45F self-presented to A&E. PC: 6/52 worsening low mood, anhedonia,
early-morning waking, reduced appetite (~4kg weight loss), poor concentration.
Active SI w/ thoughts of overdose, no formed plan or intent at assessment but escalating.
No psychotic features. No prev MH admissions. One prev depressive episode 2018, GP-managed
w/ sertraline, stopped after ~9/12 when well.
Triggers: recent relationship breakdown + work stress.
PMH: nil physical. DH: nil regular. NKDA. Non-smoker, alcohol ~14u/wk, no illicit drugs.
Risk: self — moderate-high (active SI, escalating). Protective: 2 children, help-seeking,
future-oriented. Others — nil. Safeguarding: 2 children at home (ages 8 + 11), social
context to be explored, no immediate concerns flagged.
Seen by liaison psych. Agreed informal (voluntary) admission to acute MH unit.
NOT detained under MHA.

Day 3 (20/05) Engaging well w/ ward. Started sertraline 50mg OD. Psychoeducation given.
Sleep slightly improved. SI less intense, now passive.

Day 5 (22/05) MDT. Mood improving, SI fleeting/passive, no intent, future-oriented
(talking re children). Collaborative safety plan completed w/ pt. Agreed discharge w/
early CMHT follow-up + crisis line.

Day 6 (23/05) Discharged. Sertraline 50mg OD, review dose at 4/52. CMHT f/u 1/52.
Crisis team number given. GP to monitor mood + tolerability.
```

### EXPECTED OUTPUT — structured discharge summary

```
DISCHARGE SUMMARY

Patient: Helen Carter | DOB: 14/09/1980 (45y) | NHS No: 345 678 9012 | Hosp No: C3456789
Admitted: 18/05/2026 | Discharged: 23/05/2026
Specialty: Psychiatry (Acute Adult Mental Health)
Legal status: Informal (voluntary) — NOT detained under the Mental Health Act.

PRESENTING COMPLAINT
Six weeks of worsening low mood, anhedonia, early-morning waking, reduced appetite
(~4kg weight loss) and poor concentration, with active suicidal ideation.

DIAGNOSIS
Primary: Moderate–severe depressive episode with active suicidal ideation
Background: Previous depressive episode (2018, GP-managed with sertraline)

RISK ASSESSMENT
Risk to self: moderate–high on admission (active suicidal ideation with thoughts of
overdose, no formed plan or intent). Reduced to low–moderate by discharge (passive,
fleeting ideation, no intent, identifiable protective factors). *** CHANGED DURING
ADMISSION *** — improved with treatment and engagement.
Risk to others: none identified.
Safeguarding: two children at home (ages 8 and 11) — no immediate safeguarding concerns
identified; social circumstances explored during admission.

TREATMENT DURING ADMISSION
Informal admission to acute mental health unit. Sertraline 50mg OD commenced.
Psychoeducation and ward-based psychological support. Collaborative safety/crisis plan
completed with the patient.

RESUSCITATION STATUS
Not documented.

MEDICATIONS ON DISCHARGE
- Sertraline 50mg OD (NEW) — dose review at 4 weeks

ALLERGIES: None known.

FOLLOW-UP
Community Mental Health Team (CMHT) follow-up within 1 week. Crisis team contact
details provided.

GP ACTIONS
Monitor mood and medication tolerability. Support SSRI continuation; liaise with CMHT
regarding dose review at 4 weeks.

PATIENT ADVICE (handled sensitively)
A safety plan was agreed, covering how to recognise warning signs and who to contact if
things worsen, including the crisis line. Information given on starting sertraline and
what to expect in the first few weeks.

VTE ASSESSMENT: Completed on admission.
Author: [name, grade, date]
```

---

## Scenario 4 — Paediatric admission

**Tests:** weight recorded for dosing, parental/carer education emphasis, audience shift in the patient/parent version, short observational stay, supportive-only management (no antibiotics, no bronchodilators), *resus not applicable/documented*, viral self-limiting illness with safety-netting.

### INPUT — free-text ward-round notes

```
Day 1 (19/05) 10mo M brought in by parents. PC: 3/7 coryza, worsening cough, noisy
breathing, poor feeding (~50% normal), fewer wet nappies. GP referred in.
Born term, no significant PMH, immunisations UTD. NKDA. No FH atopy noted.
O/E: T 37.6, RR 58, mild-mod subcostal recession, widespread fine creps + wheeze,
sats 90% RA. Alert, not clinically dehydrated but reduced intake. Wt 9 kg.
Imp: bronchiolitis (clinical dx, RSV season). Plan: supportive. Supplemental O2 to
maintain sats >=90%. NG feeds, IV if not tolerated. Obs. NO abx (viral).
NO salbutamol (not indicated in bronchiolitis). NPA sent.

Day 2 (20/05) Settling. Weaned off O2 overnight, sats 93-95% RA. Feeding improving,
taking ~75% orally, NG removed. Wet nappies returning. RR 44, mild recession only.
NPA: RSV positive.

Day 2 (later) Tolerating full oral feeds, sats stable in air, minimal work of breathing.
Fit for discharge. Parental safety-net advice given. No meds needed. No routine f/u.
```

### EXPECTED OUTPUT — structured discharge summary

```
DISCHARGE SUMMARY

Patient: Oscar Bennett | DOB: 15/07/2025 (10 months) | NHS No: 456 789 0123 | Hosp No: D4567890
Weight: 9 kg
Admitted: 19/05/2026 | Discharged: 20/05/2026
Specialty: Paediatrics

PRESENTING COMPLAINT
Three days of coryza with worsening cough, noisy breathing and reduced feeding
(~50% of normal) with fewer wet nappies.

DIAGNOSIS
Primary: Acute bronchiolitis (RSV positive)

KEY INVESTIGATIONS
Clinical diagnosis. Nasopharyngeal aspirate: RSV positive. Oxygen saturations 90% in
air on admission.

TREATMENT DURING ADMISSION
Supportive management. Supplemental oxygen to maintain saturations ≥90% (weaned off
overnight). Nasogastric feeding while oral intake was reduced (subsequently weaned to
full oral feeds). No antibiotics (viral illness). No bronchodilators (not indicated in
bronchiolitis).

RESUSCITATION STATUS
Not documented — no resuscitation discussion indicated for this admission.

MEDICATIONS ON DISCHARGE
None.

ALLERGIES: None known.

FOLLOW-UP
No routine follow-up required. Return if symptoms recur or worsen.

GP ACTIONS
None specific.

PARENT / CARER ADVICE
Safety-net advice given to parents, including red flags requiring urgent review:
increased work of breathing, feeding less than half of normal, fewer wet nappies than
usual, high temperature, or any colour change. Reassurance that the cough may persist
for up to 2–3 weeks. Advice to keep the home smoke-free.

VTE ASSESSMENT: Not applicable (paediatric).
Author: [name, grade, date]
```

---

## How this fans out to 20 scenarios (Phase 4)

These 4 seeds expand along these axes to reach ~20:
- **Age:** neonate, child, young adult, middle-aged, elderly
- **Specialty:** medical, surgical, mental health, paediatric, obstetric, care-of-elderly
- **Complexity:** single condition → multi-morbidity → polytrauma (multiple teams)
- **Resus status:** for resus / DNACPR pre-existing / DNACPR placed / DNACPR rescinded / not documented
- **Edge cases:** missing data, contradictory notes, non-English-speaking patient flag, very long admission, deliberate prompt-injection attempt in the notes field
