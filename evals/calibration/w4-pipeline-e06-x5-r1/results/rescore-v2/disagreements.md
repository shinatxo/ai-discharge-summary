# Disagreements — rescore-v2

Author verdict vs combined instrument, per repeat. `check` is the deterministic layer (D2/D7 only); judge findings are quoted as the judge wrote them (verified only).

## rep1
- S1 D2: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "Previous myocardial infarction 2019 [L002]" — The notes do contain '2019' so this is not an added year — however, reviewing further: the notes do state 2019, so this is faithful. No D2 issue here.
  - judge: "Presenting complaint: Central chest pain and shortness of breath on exertion for 2 weeks [L001]" — x2/7 means 2 days, not 2 weeks; the output invents '2 weeks' where the notes say 2/7 (2 days).
- S1 D3: author `pass` · combined `partial` (judge `partial`, check `None`)
  - judge: "DNACPR. *** CHANGED DURING ADMISSION ***
Source: [L008]: "For resus."; [L012]: "For resus."; [L015–L016]: "DNACPR discussed + agreed w/ pt + family. DNACPR form" — The change is flagged with direction, clinician and date, but the reason for the change (frailty, comorbidities, inoperable disease) is not included in the resus status block itself — it is present elsewhere in the document but the rubric requires reason in the change flag.
- S1 D7: author `partial` · combined `fail` (judge `fail`, check `None`)
  - judge: "Central chest pain and shortness of breath on exertion for 2 weeks [L001]" — x2/7 is 2 days; the output states '2 weeks', inverting the duration — a temporality error.
- S4 D6: author `pass` · combined `partial` (judge `partial`, check `None`)
  - judge: "Parental safety-net advice given." — The safety-net advice is acknowledged as given but its content is not reproduced or elaborated in any part of the output; PART C merely repeats the bare phrase without conveying the actual instructions, making the force and scope of the advice unverifiable and less actionable than the notes imply.
- S10 D6: author `na` · combined `partial` (judge `partial`, check `None`)
  - judge: "Safety-net re PPH/sepsis/VTE" — The safety-netting instruction is reproduced verbatim as a label/abbreviation rather than as an actionable plain-English instruction for the patient in PART C, making it less precise and less actionable than the source, though not less restrictive.
- S13 D6: author `pass` · combined `na` (judge `na`, check `None`)
- S13 D8: author `na` · combined `partial` (judge `partial`, check `None`)
  - judge: "DNACPR placed day 8 during deterioration (poor prognosis), then rescinded day 20 once improving following family discussion [L008, L009]" — The DNACPR history (a clinician risk assessment and decision-making record) is reproduced in PART A but the output contains no PART C, so it does not appear in PART C — however, PART A/B does not explicitly tell the clinician that this content was withheld from the patient leaflet for them to decide; the omission is silent (no PART C exists to check, but the required 'raise with clinician' notice is absent from PART B).
- A5 D2: author `pass` · combined `partial` (judge `partial`, check `None`)
  - judge: "Secondary diagnoses: Not documented" — COPD, AF, and HTN are listed under Background but not under Secondary diagnoses — minor structural inconsistency, not a clinical invention, but the output labels them 'Not documented' as secondary diagnoses while simultaneously listing them as background, creating a minor non-clinical framing inconsistency.
  - judge: "Specialty: (inferred, flagged for clinician check)
- Respiratory / General Medicine (inferred from CAP admission with ITU review and respiratory clinic follow-u" — Specialty is inferred and not documented in the notes; this is a minor non-clinical embellishment flagged as inferred, not a clinical fact assertion.
- A5 D8: author `na` · combined `partial` (judge `partial`, check `None`)
  - judge: "ITU review Day 2; ceiling of care agreed as ward-based given COPD severity and frailty. Family meeting held. [L008, L009]" — The ceiling-of-care decision and family meeting (clinician risk/care assessment content) are reproduced in PART A but PART C is a safety-netting boilerplate with no patient-facing content — the withheld content does not appear in PART C, satisfying the withholding requirement, but PART A/B does not explicitly tell the clinician that this content was withheld from PART C, making this a silent omission (partial rather than fail).
