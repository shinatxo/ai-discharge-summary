const fs = require('fs');
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  AlignmentType, LevelFormat, HeadingLevel, BorderStyle, WidthType, ShadingType,
  PageNumber, Header, Footer, PageBreak } = require('docx');

const NHS_BLUE = "005EB8";
const GREY = "F2F2F2";
const CW = 9360; // content width US Letter 1" margins

// ---- scenario content (Part A reference = gold-corrected) ----
const scenarios = [
  { id: "S8", title: "Neonatal early-onset sepsis",
    context: "Term newborn treated for early-onset sepsis after maternal GBS; blood cultures negative; discharged home well.",
    notes:
`Day 1 (16/05) Term male, born 39+2 by SVD. Birth wt 3.4 kg. Maternal GBS positive
this pregnancy, intrapartum abx <4h before delivery. At 6h of age: grunting,
temp 38.1, tachypnoea RR 70. Admitted NNU. Septic screen done (bloods, blood
culture, CRP). LP deferred (stable resp). Started IV benzylpenicillin +
gentamicin per neonatal sepsis protocol. CRP 28 -> 41.
Day 2 (17/05) Improving, off any resp support, feeding. Blood culture no growth
at 36h. Repeat CRP 18.
Day 3 (18/05) Well, afebrile, feeding fully. Blood culture negative at 48h.
Decision: complete 5-day course IV abx (clinical + initial CRP rise). Hearing
screen passed.
Day 5 (20/05) Completed abx. Thriving, feeding well, normal exam. Fit for
discharge home with parents. Routine newborn checks done. Safety-net advice to
parents re fever/feeding/breathing. No routine follow-up.`,
    summary:
`Patient: Leo Adeyemi | DOB: 16/05/2026 (5 days) | NHS No: 567 890 1234 | Hosp No: E5678901
Weight: 3.4 kg (birth weight)
Admitted: 16/05/2026 | Discharged: 20/05/2026
Specialty: Neonatology

PRESENTING COMPLAINT
Term newborn with grunting, fever and tachypnoea at 6 hours of age; maternal group
B streptococcus (GBS) carriage this pregnancy.

DIAGNOSIS
Primary: Early-onset neonatal sepsis (clinically treated; cultures negative)

KEY INVESTIGATIONS
Septic screen performed. CRP 28 -> 41 (initial rise), down to 18 by day 2. Blood
culture: no growth at 48 hours. Lumbar puncture deferred (respiratorily stable).
Newborn hearing screen passed.

TREATMENT DURING ADMISSION
IV benzylpenicillin and gentamicin per neonatal sepsis protocol; 5-day course
completed (treated on clinical grounds and the initial CRP rise despite negative
cultures). Supportive care; no respiratory support required.

RESUSCITATION STATUS
Not documented - no resuscitation discussion indicated for this admission.

MEDICATIONS ON DISCHARGE
None.

ALLERGIES: Not documented.

FOLLOW-UP
No routine follow-up required.

GP ACTIONS
None specific. Routine newborn care.

PARENT / CARER ADVICE
Safety-net advice given to parents regarding fever, poor feeding, breathing
difficulty, or any concern - to seek urgent review. Routine newborn checks completed.

VTE ASSESSMENT: Not applicable (neonatal).` },

  { id: "S9", title: "Paediatric DKA - new Type 1 diabetes",
    context: "9-year-old presenting in DKA, new diagnosis of Type 1 diabetes; started on subcutaneous insulin with diabetes-team education.",
    notes:
`Day 1 (17/05) 9yo girl, 2/52 polyuria/polydipsia, 3 kg weight loss, 2/7 vomiting +
abdo pain, drowsy. Wt 28 kg. BM "HI", ketones 5.2, pH 7.18, HCO3 11, glucose 34.
Dx DKA, new presentation T1DM. Started DKA protocol: IV fluids (cautious),
fixed-rate insulin infusion, K+ replacement, hourly monitoring. PICU informed,
managed on ward HDU.
Day 2 (18/05) Acidosis resolving pH 7.34, ketones 0.8. Transitioned to subcut
insulin (basal-bolus). Diabetes nurse + dietician input started. Family education begun.
Day 3 (19/05) Eating, on basal-bolus regimen, BMs improving. Ongoing structured
education (carb counting, hypo management, sick-day rules).
Day 4 (20/05) Confident with injections + monitoring (parents + child). Fit for
discharge. Diabetes team f/u 1/52, phone support meanwhile, dietician follow-up.
GP: aware, no acute action.`,
    summary:
`Patient: Amelia Frost | DOB: 04/03/2017 (9 years) | NHS No: 678 901 2345 | Hosp No: F6789012
Weight: 28 kg
Admitted: 17/05/2026 | Discharged: 20/05/2026
Specialty: Paediatrics

PRESENTING COMPLAINT
Two weeks of polyuria, polydipsia and 3 kg weight loss, with two days of vomiting,
abdominal pain and drowsiness.

DIAGNOSIS
Primary: Diabetic ketoacidosis (DKA)
Secondary: New diagnosis of Type 1 diabetes mellitus

KEY INVESTIGATIONS
Capillary glucose 34 mmol/L, blood ketones 5.2, venous pH 7.18, bicarbonate 11 on
admission. Acidosis resolved (pH 7.34, ketones 0.8) by day 2.

TREATMENT DURING ADMISSION
Managed per paediatric DKA protocol: cautious IV fluids, fixed-rate IV insulin
infusion, potassium replacement and hourly monitoring (ward HDU, PICU informed).
Transitioned to subcutaneous basal-bolus insulin once acidosis resolved. Structured
diabetes education with the diabetes nurse and dietician (carbohydrate counting,
hypoglycaemia management, sick-day rules) for the child and family.

RESUSCITATION STATUS
Not documented.

MEDICATIONS ON DISCHARGE
- Subcutaneous insulin, basal-bolus regimen (NEW) - doses as set by the paediatric
  diabetes team (refer to the team's insulin plan).

ALLERGIES: Not documented.

FOLLOW-UP
Paediatric diabetes team follow-up within 1 week, with telephone support in the
interim. Dietician follow-up arranged.

GP ACTIONS
None acute. Aware of new Type 1 diabetes diagnosis.

PARENT / CARER ADVICE
Family and child educated in insulin administration, blood glucose and ketone
monitoring, recognising and treating hypoglycaemia, and sick-day rules. Telephone
support contact provided.

VTE ASSESSMENT: Not applicable (paediatric).` },

  { id: "S10", title: "Emergency caesarean with postpartum haemorrhage",
    context: "Emergency LSCS for fetal distress, complicated by a primary postpartum haemorrhage; managed medically; discharged on VTE prophylaxis and iron.",
    notes:
`Day 1 (15/05) 31F P1, 39/40, emergency LSCS for pathological CTG / fetal distress
in labour. Live male infant, good condition, to postnatal ward with mum.
Intra-op: primary PPH ~1200ml (uterine atony), responded to oxytocin + ergometrine
+ bimanual; no return to theatre. EBL controlled. Pre-existing: nil. DH: nil. NKDA.
Day 1 post-op: Hb 84 (from 119). Asymptomatic, obs stable. Started oral iron.
VTE risk raised (LSCS + PPH) -> prophylactic enoxaparin daily.
Day 2 (16/05) Mobilising, feeding established, wound clean, lochia normal. Baby well.
Day 3 (17/05) Fit for discharge. TTO: enoxaparin prophylaxis to continue 10 days,
oral iron, regular paracetamol + ibuprofen PRN. Community midwife handover.
GP/HV informed. Postnatal check 6/52 with GP. Safety-net re PPH/sepsis/VTE.`,
    summary:
`Patient: Sofia Marchetti | DOB: 22/11/1994 (31y) | NHS No: 789 012 3456 | Hosp No: G7890123
Admitted: 15/05/2026 | Discharged: 17/05/2026
Specialty: Obstetrics

PRESENTING COMPLAINT
Emergency caesarean section at 39 weeks for fetal distress (pathological CTG) in labour.

DIAGNOSIS
Primary: Emergency lower-segment caesarean section
Secondary: Primary postpartum haemorrhage (~1200 mL, uterine atony) - managed
medically; resulting anaemia (Hb 84 from 119)

KEY INVESTIGATIONS
Estimated blood loss ~1200 mL intra-operatively. Postoperative haemoglobin 84 g/L
(pre-delivery 119 g/L).

TREATMENT DURING ADMISSION
Emergency LSCS; live male infant in good condition (to postnatal ward with mother).
Primary PPH from uterine atony managed with oxytocin, ergometrine and bimanual
compression - no return to theatre. Oral iron commenced for anaemia. VTE prophylaxis
with enoxaparin started (raised risk: caesarean + PPH).

RESUSCITATION STATUS
Not documented - no resuscitation discussion is recorded in these notes.

MEDICATIONS ON DISCHARGE
- Enoxaparin prophylactic dose, subcutaneous, once daily (NEW) - continue for 10 days
- Ferrous sulfate / oral iron (NEW) - for postpartum anaemia
- Paracetamol 1g QDS PRN (NEW)
- Ibuprofen 400mg TDS PRN (NEW)

ALLERGIES: None known.

FOLLOW-UP
Community midwife handover completed. GP postnatal check at 6 weeks. Health visitor informed.

GP ACTIONS
Postnatal check at 6 weeks. Recheck haemoglobin and review iron therapy.

PATIENT ADVICE
Safety-net advice on postpartum haemorrhage, signs of infection/sepsis, and venous
thromboembolism (calf pain/swelling, breathlessness, chest pain). Caesarean wound
care advice given.

VTE ASSESSMENT: Completed - prophylactic enoxaparin commenced (raised obstetric risk).` },

  { id: "S11", title: "Polytrauma (RTC) - multiple teams",
    context: "Restrained driver after a ~40 mph RTC with several injuries managed across orthopaedics, general surgery and thoracics.",
    notes:
`Day 1 (12/05) 47M, restrained driver, RTC ~40mph. Trauma call. Injuries: closed
left tibial shaft fracture, grade II splenic laceration (CT, stable), L 7-9 rib
fractures + small haemothorax (chest drain), undisplaced L1 transverse process
fractures. GCS 15 throughout. Pelvis/spine otherwise clear. For resus.
Ortho: tibia for IM nailing. General surgery: splenic injury non-operative,
serial obs/Hb. Chest drain by resp/thoracics.
Day 2 (13/05) IM nail left tibia done. Splenic obs stable, Hb stable. Chest drain
draining, lung re-expanding.
Day 4 (15/05) Chest drain removed, CXR satisfactory. Mobilising with physio,
partial weight-bear left leg per ortho. Hb stable, repeat CT spleen improving.
Day 6 (17/05) Stable across all injuries. Fit for discharge. VTE prophylaxis
(enoxaparin) given inpatient; continue per ortho. TTOs: analgesia, enoxaparin.
Follow-up: ortho fracture clinic 2/52, general surgery telephone review re spleen
6/52 + splenic vaccination advice, resp/thoracic no routine f/u. Pneumococcal/Hib/
MenACWY vaccination + prophylaxis advice given for splenic injury.`,
    summary:
`Patient: Marcus Bell | DOB: 09/06/1978 (47y) | NHS No: 890 123 4567 | Hosp No: H8901234
Admitted: 12/05/2026 | Discharged: 17/05/2026
Specialty: Trauma & Orthopaedics (with General Surgery and Thoracics)

PRESENTING COMPLAINT
Polytrauma following a road traffic collision (restrained driver, ~40 mph).

DIAGNOSIS
Primary: Closed left tibial shaft fracture
Secondary: Grade II splenic laceration (managed non-operatively); left 7th-9th rib
fractures with small haemothorax (chest drain); undisplaced L1 transverse process fractures

KEY INVESTIGATIONS
CT: grade II splenic laceration (haemodynamically stable, serial haemoglobin stable;
repeat CT improving). CXR: lung re-expansion after chest drain. GCS 15 throughout.

TREATMENT DURING ADMISSION
- Orthopaedics: intramedullary nailing of the left tibia; partial weight-bearing.
- General surgery: non-operative management of the splenic laceration with serial
  observations and haemoglobin (stable).
- Thoracics: chest drain for haemothorax, subsequently removed with satisfactory CXR.
- Mobilising with physiotherapy. VTE prophylaxis with enoxaparin during admission.

RESUSCITATION STATUS
For resuscitation. No change during admission.

MEDICATIONS ON DISCHARGE
- Enoxaparin prophylactic dose, subcutaneous OD (NEW) - continue per orthopaedic plan
- Analgesia as prescribed (see TTO)

ALLERGIES: Not documented.

FOLLOW-UP
- Orthopaedic fracture clinic in 2 weeks.
- General surgery telephone review at 6 weeks regarding the spleen.
- Thoracics: no routine follow-up.

GP ACTIONS
Support post-splenectomy-type prophylaxis advice (see below); review analgesia.

PATIENT ADVICE
Partial weight-bearing on the left leg per orthopaedics. Splenic injury advice:
vaccinations (pneumococcal, Hib, MenACWY) and antibiotic prophylaxis advice given;
seek urgent care with fever/signs of infection. Safety-net advice on delayed splenic
bleeding (abdominal pain, dizziness).

VTE ASSESSMENT: Completed - prophylactic enoxaparin commenced (trauma).` },

  { id: "S12", title: "Care of the Elderly - fall, delirium, evolving diagnosis",
    context: "Real-world messy notes: 88-year-old recently post-NSTEMI, unwitnessed fall with ?dementia, then an in-hospital deterioration with an evolving diagnosis and a ReSPECT form completed.",
    notes:
`(Day 1 - 26/04/26) 88F, recent discharge from CCU following NSTEMI on DAPT. Poor
historian, advised to refer to memory clinic as OP due to ?underlying dementia.
Presented to ED because found on floor by son at 10a, according to the patient she
went to commode and had a fall, does not recall whole event, dont know how long she
stays on floor ~saying 5 mins. No chest pain, SOB. Not complaining of pain. No
headache. Oriented with place but not time. Cant gather detailed history from the
patient. PMHx Hypertension, IHD, CKD, AKI, closed fracture of rib, fracture of neck
of femur, hyperparathyroidism. Ca cervix.
CK 186 / eGFR 44 (baseline) / WBC 11.1, CRP 2.5 / Trop 77
ECG - short run of A. flutter
CT Head nil acute / CT C-spine and CT thorax nil acute

(09/05/26) Mavis has deteriorated. NEWS 9. Spiking fevers. Dropped sats. Ongoing
vomiting despite antiemetics. Treat as sepsis. Possible aspiration pneumonia. I
explained the situation to the nursing staff and to her son, Steven. He understands
that she may not survive this admission.
Plan: Tazocin / Fluids / Oxygen / Bloods including cultures / Urine culture if
possible -> consider catheter to monitor urine output / CXR / Amber care / RESPECT form

(12/05/26) AXR dilated bowel loops concerning for obstruction, faecal loading.
CXR negative. Urine culture negative. Non contrast CT showed some consolidation RLZ
and sigmoid diverticulitis. AKI and raised inflammatory markers. Lactate normal.
Looks better today than she did over the weekend.

(18/05/26) Patient conscious and oriented. No c/o vomiting. Opening bowels type 5
on 16/05. No new concerns. P/A - soft, non tender, BS(+). PLAN: MFFD`,
    summary:
`Patient: Mavis [surname not documented] | DOB: Not documented (age 88, female) | NHS No: Not documented | Hosp No: Not documented
Admitted: 26/04/2026 | Discharged: Not documented (declared medically fit for discharge 18/05/2026)
Specialty: Care of the Elderly

PRESENTING COMPLAINT
Unwitnessed fall at home (found on the floor by her son; duration on the floor
unclear), on a background of recent NSTEMI and ?cognitive impairment.

DIAGNOSIS
Primary: Mechanical fall, unwitnessed (uncertain duration on the floor / possible long lie)
Secondary:
- Delirium on a background of ?underlying dementia (referral to memory clinic as an outpatient advised)
- Acute deterioration during admission initially treated as sepsis / possible aspiration pneumonia; CXR subsequently negative
- Sigmoid diverticulitis (CT); dilated bowel loops with faecal loading on AXR (concern for obstruction), subsequently resolved (bowels opened, type 5 on 16/05)
- Acute kidney injury (on background CKD)
- Short run of atrial flutter (ECG)
Background: recent NSTEMI (on dual antiplatelet therapy), hypertension, IHD, CKD,
hyperparathyroidism, carcinoma of the cervix, previous fractured neck of femur, previous rib fracture.

KEY INVESTIGATIONS
On admission: CK 186; eGFR 44 (stated baseline); WCC 11.1, CRP 2.5; troponin 77;
ECG short run of atrial flutter; CT head, CT C-spine and CT thorax all reported as no acute findings.
During deterioration (from 09/05): treated as sepsis; cultures sent. By 12/05: AXR
dilated bowel loops with faecal loading (concern for obstruction); CXR negative;
urine culture negative; non-contrast CT - right lower zone consolidation and sigmoid
diverticulitis; AKI with raised inflammatory markers; lactate normal.

TREATMENT DURING ADMISSION
Initial assessment after the fall (imaging excluded acute intracranial, cervical
spine and thoracic injury). Acute deterioration on 09/05 (NEWS 9, fevers, desaturation,
vomiting) managed as sepsis with piperacillin-tazobactam, IV fluids and oxygen;
cultures sent; "amber" treatment-escalation status documented and a ReSPECT form
completed. Clinical improvement thereafter, with bowels opening and resolution of
the obstruction concern; declared medically fit for discharge on 18/05.

RESUSCITATION STATUS
*** CHANGED DURING ADMISSION *** A ReSPECT form was completed on 09/05/2026 during
acute clinical deterioration, following discussion with the patient's son (who
understood she might not survive the admission). The explicit recommendation is not
transcribed in these notes; in this context (acute deterioration, "amber"
ceiling-of-care status, and the family informed of a poor prognosis) the
recommendation is most likely DNACPR with a ward-based ceiling of care. This should
be confirmed against the completed ReSPECT form before relying on it.

MEDICATIONS ON DISCHARGE
Not documented. NOTE: the patient was on dual antiplatelet therapy (DAPT) following
a recent NSTEMI before admission; no discharge medication list is recorded in these
notes. The discharge medications, and in particular continuation of DAPT, MUST be
reconciled before discharge (stopping dual antiplatelets early after a recent
coronary event/stent carries significant risk).

ALLERGIES: Not documented.

FOLLOW-UP
Referral to memory clinic as an outpatient (for ?underlying dementia). No further
follow-up is documented. NOTE: declared medically fit for discharge (18/05) but no
discharge destination, social/package-of-care plan or therapy (OT/PT) input is
documented - discharge-planning details are not recorded in these notes.

GP ACTIONS
Not explicitly documented. Suggested by the recorded course (for the clinical team
to confirm): monitor renal function following AKI; ensure post-NSTEMI medications
(including DAPT) are reconciled; await/expedite memory clinic assessment.

PATIENT / CARER ADVICE
Not documented in these notes. (Falls assessment, delirium and cognitive follow-up,
and a clear medication plan would ordinarily be communicated to the patient and her son - not recorded here.)

VTE ASSESSMENT: Not documented.` },

  { id: "S13", title: "Prolonged ITU admission (necrotising fasciitis)",
    context: "49-day admission for necrotising fasciitis and septic shock with a complex multi-system ITU course; DNACPR placed during deterioration then rescinded on improvement.",
    notes:
`Admitted 02/04. 58F, necrotising soft-tissue infection L thigh + septic shock.
ED -> theatre (debridement) -> ITU. Multi-organ support: intubated/ventilated,
noradrenaline, CVVH for AKI. Multiple theatre trips (debridements x4, days 0/2/5/9),
washout + delayed closure + VAC dressing. Organisms: Group A strep + anaerobes;
long course IV pip-taz then tailored to benzylpenicillin + clindamycin per micro.
Course: ARDS (proned x2), ICU-acquired weakness, line sepsis day 14 (lines changed),
AF (new, rate-controlled), nutrition via NG then improving oral.
Resus: For resus on admission. DNACPR placed day 8 during deterioration (poor
prognosis), then rescinded day 20 once improving + family discussion.
Renal recovered, off CVVH day 16. Extubated day 18. Stepped down to ward day 24.
Rehab/physio prolonged. Wound healing by secondary intention, district nurse +
tissue viability. Discharged 21/05 (49-day admission).
Pre-admission DH: amlodipine 5mg OD, atorvastatin 20mg ON. NKDA.
TTOs: bisoprolol 2.5mg OD (new, for AF), apixaban 5mg BD (new, AF), amlodipine 5mg
OD continued, atorvastatin 20mg ON continued, plus wound care. GP: monitor renal
function post-AKI, BP. Follow-up: plastics/wound clinic 2/52, cardiology (AF) 6/52,
ICU follow-up clinic, physio/OT community.`,
    summary:
`Patient: Diane Whitfield | DOB: 18/01/1968 (58y) | NHS No: 901 234 5678 | Hosp No: I9012345
Admitted: 02/04/2026 | Discharged: 21/05/2026 (49-day admission)
Specialty: Intensive Care / Plastic Surgery / General Medicine

PRESENTING COMPLAINT
Necrotising soft-tissue infection of the left thigh with septic shock.

DIAGNOSIS
Primary: Necrotising fasciitis (Group A streptococcus + anaerobes), left thigh
Secondary: Septic shock with multi-organ failure; acute kidney injury (required
haemofiltration, recovered); ARDS; new atrial fibrillation; ICU-acquired weakness;
line-associated sepsis (treated)

KEY INVESTIGATIONS
Microbiology: Group A streptococcus and anaerobes; antibiotics tailored per
microbiology. AKI requiring CVVH (renal function recovered). New AF on cardiac monitoring.

TREATMENT DURING ADMISSION
Prolonged intensive care admission with invasive ventilation (proned twice for
ARDS), vasopressor support and continuous haemofiltration for AKI. Four surgical
debridements followed by washout, delayed closure and VAC dressing. Antibiotics:
initial piperacillin-tazobactam, tailored to benzylpenicillin and clindamycin.
New AF rate-controlled and anticoagulated. Nutrition via NG then oral. Prolonged
rehabilitation with physiotherapy and occupational therapy. Wound healing by secondary intention.

RESUSCITATION STATUS
For resuscitation. *** CHANGED DURING ADMISSION *** Initially for resuscitation; a
DNACPR decision was made on day 8 during clinical deterioration (poor prognosis),
then rescinded on day 20 following clinical improvement and discussion with the
family. Current status at discharge: for resuscitation.

MEDICATIONS ON DISCHARGE
- Bisoprolol 2.5mg OD (NEW) - for atrial fibrillation
- Apixaban 5mg BD (NEW) - anticoagulation for atrial fibrillation
- Amlodipine 5mg OD (continued)
- Atorvastatin 20mg ON (continued)

ALLERGIES: None known.

FOLLOW-UP
Plastics / wound clinic in 2 weeks. Cardiology (atrial fibrillation) in 6 weeks.
ICU follow-up clinic. Community physiotherapy and occupational therapy. District
nurse and tissue viability for ongoing wound care.

GP ACTIONS
Monitor renal function following AKI. Monitor blood pressure. Support anticoagulation
and rate control for new AF.

PATIENT ADVICE
Ongoing wound care explained; district nurse involvement arranged. Information on
recovery from a prolonged critical-care stay, including expected fatigue and the role of rehabilitation.

VTE ASSESSMENT: Completed (now therapeutically anticoagulated for AF).` },

  { id: "S14", title: "Acute ischaemic stroke (thrombolysed)",
    context: "Thrombolysed ischaemic stroke with an antiplatelet-to-anticoagulant switch for AF, plus driving (DVLA) advice.",
    notes:
`Day 1 (14/05) 74M, sudden R facial droop, R arm weakness, dysphasia, onset 1h ago.
NIHSS 9. CT head no haemorrhage. Thrombolysed (alteplase) within window. Known AF -
NOT previously anticoagulated (was on aspirin only). PMH: AF, HTN, T2DM. DH: aspirin
75mg OD, amlodipine 10mg OD, metformin 1g BD. NKDA.
Day 2 (15/05) Post-thrombolysis CT no bleed. NIHSS improving to 4. Swallow unsafe
initially -> SALT, NG feeding, then swallow recovered day 3. Resus discussed with
patient (capacity intact) -> wishes to remain for resus.
Day 4 (17/05) Anticoagulation started for AF (apixaban) per stroke protocol timing;
aspirin stopped. Mobilising, mild residual arm weakness. Early supported discharge team accept.
Day 5 (18/05) Discharge. TTOs: apixaban 5mg BD (new), aspirin STOPPED, amlodipine
10mg OD continued, atorvastatin 80mg ON (new, secondary prevention), metformin 1g
BD continued. ESD/community stroke rehab. Stroke clinic 6/52. GP: BP, HbA1c.
Driving: advised not to drive, must inform DVLA (1 month off driving min per rules).`,
    summary:
`Patient: Raymond Clarke | DOB: 30/10/1951 (74y) | NHS No: 012 345 6789 | Hosp No: J0123456
Admitted: 14/05/2026 | Discharged: 18/05/2026
Specialty: Stroke Medicine

PRESENTING COMPLAINT
Sudden right facial droop, right arm weakness and dysphasia, onset one hour before arrival.

DIAGNOSIS
Primary: Acute ischaemic stroke (thrombolysed), NIHSS 9 at onset
Secondary: Atrial fibrillation; hypertension; Type 2 diabetes

KEY INVESTIGATIONS
CT head: no haemorrhage (pre- and post-thrombolysis). NIHSS 9 -> 4 with improvement.

TREATMENT DURING ADMISSION
Intravenous thrombolysis (alteplase) within the time window. Initial unsafe swallow
managed with SALT assessment and NG feeding; swallow recovered by day 3.
Anticoagulation for AF commenced per stroke-protocol timing and aspirin stopped.
Mobilising with mild residual right arm weakness. Accepted by the early supported
discharge / community stroke rehabilitation team.

RESUSCITATION STATUS
For resuscitation. Resuscitation was discussed with the patient (capacity intact)
during admission; he wished to remain for resuscitation. (Discussed; status unchanged.)

MEDICATIONS ON DISCHARGE
- Apixaban 5mg BD (NEW) - anticoagulation for AF (secondary stroke prevention)
- Aspirin 75mg OD - STOPPED (replaced by anticoagulation)
- Atorvastatin 80mg ON (NEW) - secondary prevention
- Amlodipine 10mg OD (continued)
- Metformin 1g BD (continued)

ALLERGIES: None known.

FOLLOW-UP
Early supported discharge / community stroke rehabilitation. Stroke clinic in 6 weeks.

GP ACTIONS
Monitor blood pressure and HbA1c. Support secondary prevention.

PATIENT ADVICE
Driving: advised not to drive and to inform the DVLA (minimum one month off driving
per current rules). Stroke recovery and secondary-prevention information given.

VTE ASSESSMENT: Completed (now anticoagulated for AF).` },

  { id: "S15", title: "COPD exacerbation with NIV (pre-existing DNACPR)",
    context: "Infective COPD exacerbation with type 2 respiratory failure needing NIV; a community DNACPR/ReSPECT was already in place before admission and unchanged.",
    notes:
`Day 1 (16/05) 79F, known severe COPD (FEV1 30% predicted, home O2, prev ITU avoided
by choice). PC: 4/7 increasing SOB, green sputum, wheeze. T 37.8, RR 28, sats 84% on
home O2, pH 7.30 PaCO2 8.1 -> type 2 resp failure. Started controlled O2, NIV,
nebs, IV hydrocortisone then oral pred, IV then oral amoxicillin (per local). DH:
Trelegy inhaler OD, salbutamol PRN, amlodipine 5mg OD. NKDA.
Resus: PRE-EXISTING community DNACPR (ReSPECT) in place from before admission
(advanced COPD, patient wishes). Documented, not changed. Ceiling of care = ward-
based NIV, not for intubation (per ReSPECT + patient).
Day 3 (18/05) Weaned off NIV, sats 88-90% on home O2 baseline. Improving.
Day 5 (20/05) Back to baseline. Fit for discharge. Pred 30mg OD for 5 days total
(complete course at home), complete oral amoxicillin course, inhaler technique
reviewed, rescue pack supplied, smoking cessation discussed (ex-smoker). Resp clinic
6/52, community resp team referral. GP: review after exacerbation.`,
    summary:
`Patient: Patricia Nolan | DOB: 07/02/1947 (79y) | NHS No: 123 456 7891 | Hosp No: K1234567
Admitted: 16/05/2026 | Discharged: 20/05/2026
Specialty: Respiratory Medicine

PRESENTING COMPLAINT
Four days of increasing breathlessness, green sputum and wheeze on a background of severe COPD.

DIAGNOSIS
Primary: Infective exacerbation of COPD with type 2 respiratory failure
Secondary: Severe COPD (FEV1 30% predicted, home oxygen)

KEY INVESTIGATIONS
On admission: SpO2 84% on home oxygen, pH 7.30, PaCO2 8.1 kPa (type 2 respiratory failure).

TREATMENT DURING ADMISSION
Controlled oxygen and non-invasive ventilation (weaned by day 3), nebulisers,
corticosteroids (IV hydrocortisone then oral prednisolone) and antibiotics (IV then
oral amoxicillin). Returned to respiratory baseline on home oxygen.

RESUSCITATION STATUS
DNACPR - pre-existing (community ReSPECT decision in place before this admission, in
keeping with advanced COPD and the patient's wishes). Not changed during admission.
Ceiling of care: ward-based NIV, not for intubation, per the ReSPECT plan and the patient.

MEDICATIONS ON DISCHARGE
- Prednisolone 30mg OD (NEW) - complete a 5-day course at home
- Amoxicillin oral (NEW) - complete the prescribed course
- Trelegy inhaler OD (continued)
- Salbutamol inhaler PRN (continued)
- Amlodipine 5mg OD (continued)

ALLERGIES: None known.

FOLLOW-UP
Respiratory clinic in 6 weeks. Community respiratory team referral made.

GP ACTIONS
Review after this exacerbation. Reinforce inhaler technique and rescue-pack use.

PATIENT ADVICE
Inhaler technique reviewed; rescue pack supplied with advice on when to use it.
Smoking cessation discussed (ex-smoker). Advice on when to seek help if symptoms worsen.

VTE ASSESSMENT: Completed on admission.` },

  { id: "S16", title: "Emergency laparotomy + stoma (small bowel obstruction)",
    context: "Adhesional small bowel obstruction failing conservative management, requiring emergency laparotomy, resection and a new defunctioning ileostomy; warfarin held and bridged.",
    notes:
`Day 1 (10/05) 72M, 3/7 colicky abdo pain, distension, vomiting, absolute
constipation. PMH: previous open appendicectomy, HTN, AF (on warfarin), T2DM.
DH: warfarin per INR, ramipril 5mg OD, metformin 1g BD, bisoprolol 5mg OD. NKDA.
CT: adhesional small bowel obstruction, transition point, no ischaemia initially.
Trial conservative (NG, drip & suck). Warfarin held, INR reversed for possible surgery.
Day 3 (12/05) Failed conservative, signs of compromise -> emergency laparotomy:
adhesiolysis + small bowel resection (non-viable segment) + primary anastomosis;
defunctioning loop ileostomy formed. Recovery in HDU.
Day 6 (15/05) Stoma functioning, tolerating diet, mobilising. Stoma nurse teaching.
Restarted: metformin, ramipril, bisoprolol. Warfarin restarted with bridging
(treatment-dose enoxaparin) until INR therapeutic.
Day 9 (18/05) Independent with stoma care. Fit for discharge. TTOs: warfarin
restarted (per INR), enoxaparin treatment dose bridging until INR in range,
ramipril 5mg OD, metformin 1g BD, bisoprolol 5mg OD, analgesia. Stoma nurse
community follow-up, surgical clinic 6/52 (?reversal later). GP: monitor INR.`,
    summary:
`Patient: Gerald Hopkins | DOB: 25/03/1954 (72y) | NHS No: 234 567 8902 | Hosp No: L2345678
Admitted: 10/05/2026 | Discharged: 18/05/2026
Specialty: General Surgery

PRESENTING COMPLAINT
Three days of colicky abdominal pain, distension, vomiting and absolute constipation.

DIAGNOSIS
Primary: Adhesional small bowel obstruction requiring emergency laparotomy, small
bowel resection and defunctioning loop ileostomy
Secondary: Hypertension; atrial fibrillation (anticoagulated); Type 2 diabetes

KEY INVESTIGATIONS
CT abdomen: adhesional small bowel obstruction with a transition point, no ischaemia initially.

TREATMENT DURING ADMISSION
Initial conservative management (NG decompression, IV fluids) failed; emergency
laparotomy performed with adhesiolysis, resection of a non-viable small bowel segment,
primary anastomosis and a defunctioning loop ileostomy. HDU recovery. Warfarin was
held and reversed peri-operatively, then restarted with treatment-dose enoxaparin
bridging. Stoma care teaching with the stoma nurse.

RESUSCITATION STATUS
Not documented - no resuscitation discussion is recorded in these notes. (The "INR
reversed for possible surgery" note relates to peri-operative anticoagulation, not a
resuscitation decision.)

MEDICATIONS ON DISCHARGE
- Warfarin (continued) - restarted, dose per INR
- Enoxaparin treatment dose, subcutaneous (NEW) - bridging until INR therapeutic
- Ramipril 5mg OD (continued)
- Metformin 1g BD (continued)
- Bisoprolol 5mg OD (continued)
- Analgesia as prescribed (see TTO)

ALLERGIES: None known.

FOLLOW-UP
Community stoma nurse follow-up. Surgical clinic in 6 weeks (to consider future stoma reversal).

GP ACTIONS
Monitor INR and manage warfarin/enoxaparin bridging until INR therapeutic.

PATIENT ADVICE
Independent with stoma care; community stoma nurse arranged. Advice on stoma output,
hydration, and when to seek help (high output, no output, signs of obstruction or wound infection).

VTE ASSESSMENT: Completed (therapeutic anticoagulation/bridging in place).` },

  { id: "S17", title: "Upper GI bleed (NSAID ulcer)",
    context: "Upper GI bleed from an NSAID-related duodenal ulcer treated endoscopically; rich medication changes - NSAID stopped permanently, anticoagulant withheld pending review.",
    notes:
`Day 1 (15/05) 70F, melaena 2/7, one episode coffee-ground vomit, dizzy. PMH: OA
(takes regular ibuprofen), AF (apixaban), HTN. DH: ibuprofen 400mg TDS, apixaban
5mg BD, amlodipine 5mg OD, paracetamol PRN. NKDA. Hb 78, HR 104, BP 104/64.
Resus status: for resus. Transfused 2 units. IV PPI infusion. Apixaban withheld.
OGD day 1: bleeding duodenal ulcer (Forrest IIa) -> adrenaline + clip, haemostasis
achieved. CLO test positive (H. pylori).
Day 2 (16/05) Stable, Hb 92 post-transfusion, no further bleeding. Eating.
Day 3 (17/05) Discharge. TTOs: oral PPI high dose (new), H. pylori eradication
(amoxicillin + clarithromycin 7/7, new), ibuprofen STOPPED permanently (cause),
paracetamol regular for OA instead, amlodipine continued. Apixaban: WITHHELD on
discharge, to be reviewed and restarted by GP/cardiology in ~2 weeks once healed.
GP: repeat Hb, confirm H. pylori eradication, decision on resuming apixaban.
Gastro clinic + repeat OGD 6-8/52 to confirm ulcer healing.`,
    summary:
`Patient: Brenda Shaw | DOB: 12/12/1955 (70y) | NHS No: 345 678 9013 | Hosp No: M3456789
Admitted: 15/05/2026 | Discharged: 17/05/2026
Specialty: Gastroenterology / General Medicine

PRESENTING COMPLAINT
Two days of melaena with one episode of coffee-ground vomiting and dizziness.

DIAGNOSIS
Primary: Upper gastrointestinal bleed from a bleeding duodenal ulcer (Forrest IIa),
NSAID-related; Helicobacter pylori positive
Secondary: Osteoarthritis; atrial fibrillation (anticoagulated); hypertension

KEY INVESTIGATIONS
Haemoglobin 78 g/L on admission, 92 g/L after transfusion. OGD: bleeding duodenal
ulcer (Forrest IIa) treated endoscopically (adrenaline injection + clip), haemostasis
achieved. CLO test positive for H. pylori.

TREATMENT DURING ADMISSION
Resuscitation and transfusion of 2 units of red cells. Intravenous proton-pump
inhibitor infusion. Apixaban withheld. Endoscopic haemostasis at OGD.

RESUSCITATION STATUS
For resuscitation. No change during admission.

MEDICATIONS ON DISCHARGE
- Proton-pump inhibitor, high dose oral (NEW)
- H. pylori eradication: amoxicillin + clarithromycin, 7-day course (NEW)
- Ibuprofen - STOPPED permanently (cause of the bleed)
- Paracetamol, regular (NEW/changed) - for osteoarthritis in place of the NSAID
- Apixaban - WITHHELD on discharge; to be reviewed and restarted by GP/cardiology in ~2 weeks once the ulcer has healed
- Amlodipine 5mg OD (continued)

ALLERGIES: None known.

FOLLOW-UP
Gastroenterology clinic with repeat OGD in 6-8 weeks to confirm ulcer healing.

GP ACTIONS
Repeat haemoglobin. Confirm H. pylori eradication. Decision on resuming apixaban in
~2 weeks (balance of bleeding vs stroke risk).

PATIENT ADVICE
Avoid NSAIDs (ibuprofen stopped permanently). Advice on recognising re-bleeding
(black stools, vomiting blood, dizziness) and to seek urgent help. Importance of
completing the eradication course.

VTE ASSESSMENT: Completed on admission.` },

  { id: "S18", title: "First unprovoked seizure",
    context: "A first unprovoked seizure with a normal workup; no medication started (deferred to neurology); the key output is correct DVLA/driving advice.",
    notes:
`Day 1 (19/05) 26M, witnessed first generalised tonic-clonic seizure at work, ~2 min,
post-ictal confusion, no incontinence reported, tongue bite lateral. No prev
seizures. No head injury. Bloods/glucose normal, tox screen negative, CT head normal.
No alcohol/drug trigger. Started on no antiepileptic (first seizure, low immediate
recurrence risk, neurology to decide post-investigation). DH: nil. NKDA.
Day 2 (20/05) Neurology review: first unprovoked seizure. Plan outpatient MRI brain
+ EEG, first-fit clinic. No driving. Discharged. Safety advice (no swimming alone,
heights, baths). DVLA: must not drive, must inform DVLA (6 months off driving from
seizure for a first unprovoked seizure, group 1). GP: aware, await neurology.`,
    summary:
`Patient: Tom Hargreaves | DOB: 02/07/1999 (26y) | NHS No: 456 789 0124 | Hosp No: N4567890
Admitted: 19/05/2026 | Discharged: 20/05/2026
Specialty: Neurology / Acute Medicine

PRESENTING COMPLAINT
Witnessed first generalised tonic-clonic seizure (~2 minutes) at work, with post-ictal
confusion and a lateral tongue bite.

DIAGNOSIS
Primary: First unprovoked seizure

KEY INVESTIGATIONS
Blood glucose and routine bloods normal. Toxicology screen negative. CT head normal.
MRI brain and EEG to be arranged as an outpatient.

TREATMENT DURING ADMISSION
Observation and neurology review. No antiepileptic medication started (first
unprovoked seizure; decision deferred to neurology after investigations).

RESUSCITATION STATUS
Not documented.

MEDICATIONS ON DISCHARGE
None.

ALLERGIES: None known.

FOLLOW-UP
First-fit clinic with outpatient MRI brain and EEG.

GP ACTIONS
None acute. Await neurology assessment and investigation results.

PATIENT ADVICE
Driving: must not drive and must inform the DVLA (6 months off driving from the date
of a first unprovoked seizure, Group 1 licence). Seizure safety advice given: avoid
swimming alone, working at heights, and taking baths (shower instead) until reviewed.
Advice on what to do if another seizure occurs.

VTE ASSESSMENT: Completed on admission.` },
];

// ---- helpers ----
function lines(text, font, size, opts={}) {
  return text.split('\n').map(l =>
    new Paragraph({
      spacing: { after: 0, line: 252 },
      children: [ new TextRun({ text: l.length ? l : " ", font, size, color: opts.color || "000000" }) ]
    })
  );
}
function h1(t){ return new Paragraph({ heading: HeadingLevel.HEADING_1, children:[new TextRun(t)] }); }
function h2(t){ return new Paragraph({ heading: HeadingLevel.HEADING_2, children:[new TextRun(t)] }); }
function p(runs, opts={}){ return new Paragraph({ spacing:{ after: opts.after!==undefined?opts.after:120 }, alignment: opts.align, children: Array.isArray(runs)?runs:[new TextRun(runs)] }); }
function label(t){ return new Paragraph({ spacing:{ before:160, after:60 },
  border:{ bottom:{ style:BorderStyle.SINGLE, size:6, color:NHS_BLUE, space:2 } },
  children:[new TextRun({ text:t, bold:true, color:NHS_BLUE, size:21 })] }); }

const RATE = "✓ Good   /   ⚠ Concern   /   ✗ Wrong";
function scoringGrid() {
  const head = (t,w) => new TableCell({ width:{size:w,type:WidthType.DXA}, columnSpan:undefined,
    shading:{ fill:NHS_BLUE, type:ShadingType.CLEAR }, margins:{top:60,bottom:60,left:120,right:120},
    children:[p([new TextRun({text:t,bold:true,color:"FFFFFF",size:19})],{after:0})] });
  const cell = (t,w,bold) => new TableCell({ width:{size:w,type:WidthType.DXA}, margins:{top:70,bottom:70,left:120,right:120},
    children:[p([new TextRun({text:t,size:19,bold:!!bold})],{after:0})] });
  const blank = (w) => new TableCell({ width:{size:w,type:WidthType.DXA}, margins:{top:70,bottom:70,left:120,right:120}, children:[p([new TextRun({text:" ",size:19})],{after:0})] });
  const W=[3260,2400,3700];
  const rows = [
    new TableRow({ tableHeader:true, children:[ head("Aspect to judge",W[0]), head("Your rating",W[1]), head("Comments",W[2]) ] }),
    ...[
      ["Completeness - is anything clinically important missing?"],
      ["Accuracy - is anything stated that the notes do NOT support?"],
      ["Resuscitation status - correct, and any change flagged?"],
      ["Medications - discharge meds & changes correct and safe?"],
      ["OVERALL - would you be willing to sign this as a draft?"],
    ].map((r,i)=> new TableRow({ children:[ cell(r[0],W[0], i===4), cell(RATE,W[1]), blank(W[2]) ] }))
  ];
  return new Table({ width:{size:CW,type:WidthType.DXA}, columnWidths:W,
    borders:{ top:{style:BorderStyle.SINGLE,size:1,color:"BBBBBB"}, bottom:{style:BorderStyle.SINGLE,size:1,color:"BBBBBB"},
      left:{style:BorderStyle.SINGLE,size:1,color:"BBBBBB"}, right:{style:BorderStyle.SINGLE,size:1,color:"BBBBBB"},
      insideHorizontal:{style:BorderStyle.SINGLE,size:1,color:"BBBBBB"}, insideVertical:{style:BorderStyle.SINGLE,size:1,color:"BBBBBB"} },
    rows });
}

// ---- build content ----
function frontMatter(specialtyLabel){
const children = [];

// Title
children.push(new Paragraph({ spacing:{after:60}, children:[new TextRun({ text:"AI Discharge Summary Assistant", bold:true, size:40, color:NHS_BLUE })] }));
children.push(new Paragraph({ spacing:{after:40}, children:[new TextRun({ text:"Clinician Reviewer Pack", bold:true, size:30 })] }));
children.push(p([new TextRun({ text:"A second pair of eyes on the tool's draft discharge summaries. All data is synthetic - no real patients.", italics:true, size:21 })],{after:60}));
children.push(p([new TextRun({ text:"Prepared for: "+specialtyLabel+" review (one scenario).", bold:true, size:21, color:NHS_BLUE })],{after:240}));

// How to review
children.push(h2("How to review (please read first - 2 minutes)"));
children.push(p([new TextRun({text:"What this tool does. ",bold:true}), new TextRun("It turns a doctor's messy, abbreviated ward-round notes into three drafts: a structured discharge summary, a GP letter, and a plain-English version for the patient. Every output is a DRAFT for a clinician to check and sign - the tool never replaces clinical judgement.")]));
children.push(p([new TextRun({text:"Your task. ",bold:true}), new TextRun("For each scenario you will see the original ward-round notes and the draft discharge summary the tool produced. Read the notes, then judge the draft: is it complete, accurate, and safe? Mark anything you would change. You are reviewing the clinical content (the structured summary); reading-age of the patient version is measured separately by formula, so you do not need to score that.")]));
children.push(p([new TextRun({text:"The core rule we are testing. ",bold:true}), new TextRun("The tool is built to report only what the notes support and to write “Not documented” rather than guess. So an honest “Not documented” is the correct answer when the notes are silent - it is NOT a miss. Inventing a resuscitation status, a drug, a dose, or a diagnosis that the notes do not support is the most serious failure.")]));
children.push(p([new TextRun({text:"How to mark. ",bold:true}), new TextRun("In each scoring grid, rate every row ✓ Good, ⚠ Concern, or ✗ Wrong, and add a comment where you have one. The five things we care about:")]));
children.push(new Paragraph({ numbering:{reference:"nums",level:0}, children:[ new TextRun({text:"Completeness ",bold:true}), new TextRun("- is any clinically important field missing?") ]}));
children.push(new Paragraph({ numbering:{reference:"nums",level:0}, children:[ new TextRun({text:"Accuracy / no fabrication ",bold:true}), new TextRun("- is anything asserted that the notes do not support?") ]}));
children.push(new Paragraph({ numbering:{reference:"nums",level:0}, children:[ new TextRun({text:"Resuscitation status ",bold:true}), new TextRun("- correct, and any change during admission clearly flagged?") ]}));
children.push(new Paragraph({ numbering:{reference:"nums",level:0}, children:[ new TextRun({text:"Medications ",bold:true}), new TextRun("- discharge list and changes (new / stopped / withheld / continued) correct and safe?") ]}));
children.push(new Paragraph({ numbering:{reference:"nums",level:0}, children:[ new TextRun({text:"Overall ",bold:true}), new TextRun("- bottom line: would you be willing to sign this draft?") ]}));
children.push(p([new TextRun({text:"You do not need the “right answer”. ",bold:true}), new TextRun("Please mark up the draft from your own clinical judgement. Where your view differs from the draft, that is exactly the signal we want. You can do as many or as few scenarios as you like - those in your own specialty are most valuable. There is a companion Excel sheet if you would rather type your scores.")]));
children.push(p([new TextRun({text:"Thank you. ",bold:true}), new TextRun("Your feedback hardens both the tool and our reference answers - a recent review like this already caught five errors in our own answer key.")]));
return children;
}

function scenarioParas(s){
  const children = [];
  children.push(new Paragraph({ pageBreakBefore:true, heading:HeadingLevel.HEADING_1, children:[new TextRun(`${s.id} - ${s.title}`)] }));
  children.push(p([new TextRun({ text:s.context, italics:true, size:21 })],{after:60}));
  children.push(label("WARD-ROUND NOTES (the input)"));
  lines(s.notes, "Courier New", 17).forEach(x=>children.push(x));
  children.push(label("DRAFT DISCHARGE SUMMARY (please review this)"));
  lines(s.summary, "Arial", 19).forEach(x=>children.push(x));
  children.push(label("YOUR REVIEW"));
  children.push(scoringGrid());
  children.push(p([new TextRun({text:"Specific errors or omissions you spotted (free text):",bold:true,size:19})],{before:120,after:240}));
  children.push(new Paragraph({ border:{ bottom:{style:BorderStyle.SINGLE,size:4,color:"BBBBBB",space:6} }, spacing:{after:200}, children:[new TextRun(" ")] }));
  children.push(new Paragraph({ border:{ bottom:{style:BorderStyle.SINGLE,size:4,color:"BBBBBB",space:6} }, spacing:{after:120}, children:[new TextRun(" ")] }));
  return children;
}

function buildDoc(children){
  return new Document({
    styles: {
      default:{ document:{ run:{ font:"Arial", size:22 } } },
      paragraphStyles:[
        { id:"Heading1", name:"Heading 1", basedOn:"Normal", next:"Normal", quickFormat:true,
          run:{ size:30, bold:true, font:"Arial", color:NHS_BLUE }, paragraph:{ spacing:{before:120,after:120}, outlineLevel:0 } },
        { id:"Heading2", name:"Heading 2", basedOn:"Normal", next:"Normal", quickFormat:true,
          run:{ size:25, bold:true, font:"Arial", color:"222222" }, paragraph:{ spacing:{before:160,after:100}, outlineLevel:1 } },
      ]
    },
    numbering:{ config:[ { reference:"nums", levels:[{ level:0, format:LevelFormat.DECIMAL, text:"%1.", alignment:AlignmentType.LEFT, style:{ paragraph:{ indent:{ left:600, hanging:340 } } } }] } ] },
    sections:[{
      properties:{ page:{ size:{ width:12240, height:15840 }, margin:{ top:1320, right:1440, bottom:1320, left:1440 } } },
      footers:{ default: new Footer({ children:[ new Paragraph({ alignment:AlignmentType.CENTER, children:[
        new TextRun({ text:"AI Discharge Summary Assistant - Clinician Reviewer Pack  |  synthetic data, draft tool output  |  page ", size:16, color:"888888" }),
        new TextRun({ children:[PageNumber.CURRENT], size:16, color:"888888" }) ] }) ] }) },
      children
    }]
  });
}

const specialties = {
  S8:"Neonatology / NICU", S9:"Paediatrics", S10:"Obstetrics", S11:"Trauma & Orthopaedics",
  S12:"Care of the Elderly / Geriatrics", S13:"Intensive Care", S14:"Stroke Medicine",
  S15:"Respiratory Medicine", S16:"General Surgery", S17:"Gastroenterology", S18:"Neurology"
};
const fileSpec = {
  S8:"Neonatology", S9:"Paediatrics", S10:"Obstetrics", S11:"Trauma-Orthopaedics",
  S12:"Care-of-the-Elderly", S13:"Intensive-Care", S14:"Stroke", S15:"Respiratory",
  S16:"General-Surgery", S17:"Gastroenterology", S18:"Neurology"
};

(async () => {
  for (const s of scenarios){
    const kids = [...frontMatter(specialties[s.id]), ...scenarioParas(s)];
    const buf = await Packer.toBuffer(buildDoc(kids));
    const fn = `Reviewer-Pack-${s.id}-${fileSpec[s.id]}.docx`;
    fs.writeFileSync(fn, buf);
    console.log("written", fn, buf.length);
  }
})();
