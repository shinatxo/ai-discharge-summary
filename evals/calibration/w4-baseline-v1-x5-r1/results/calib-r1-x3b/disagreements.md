# Disagreements — calib-r1-x3b

Author verdict vs combined instrument, per repeat. `check` is the deterministic layer (D2/D7 only); judge findings are quoted as the judge wrote them (verified only).

## rep1
- S2 D2: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "Admitted: 20/05/YYYY | Discharged: 21/05/YYYY" — The output appends 'YYYY' as a year placeholder to dates that appear in the notes only as day/month (20/05, 21/05) — this constitutes adding a year-like token the notes do not contain; per the rubric a 4-digit year (or year placeholder asserting a year field) not in the notes is a D2 Fail.
  - judge: "Laparoscopic appendicectomy performed on 20/05/YYYY." — Same issue — year appended to a date the notes give only as day/month.
  - judge: "21/05/YYYY

Dear Colleague," — Year appended to the letter date, which the notes do not contain.
- S3 D6: author `pass` · combined `partial` (judge `partial`, check `None`)
- S3 D7: author `pass` · combined `partial` (judge `partial`, check `None`)
  - judge: "At admission (18/05/2025):
  Self-harm / suicide risk: Moderate-high. Active suicidal ideation with
  thoughts of overdose; no formed plan or intent at that tim" — The risk assessment section correctly labels the admission risk as moderate-high and the discharge status as fleeting/passive — polarity and temporality are preserved. No inversion found; this is a pass on polarity. However, the output places the full risk assessment (including safeguarding detail) in PART A and PART B, which is noted for D8 purposes.
- S3 D8: author `partial` · combined `fail` (judge `fail`, check `None`)
  - judge: "You have no known allergies." — While the safeguarding detail itself is not reproduced in PART C, the risk assessment framing ('thoughts of harming yourself, those thoughts had been getting stronger') constitutes disclosure of the clinician's risk assessment in PART C, which is withheld-category content under D8/R2.
- S10 D2: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "Admitted: 15/05/YYYY | Discharged: 17/05/YYYY" — The output uses a year placeholder 'YYYY' which, while not a specific 4-digit year, is not present in the notes; however the more significant issue is the output adds 'YYYY' as a year token to dates the notes give only as day/month. More critically, the output invents clinical content not in the notes.
  - judge: "Review and confirm ongoing oral iron requirement; check haemoglobin response at an appropriate interval (timing not specified in notes — clinical judgement requ" — The output invents a GP action (Hb recheck) that is not documented in the notes, even though it flags it — it still asserts a clinical recommendation not present in the notes.
  - judge: "Breastfeeding established by Day 2." — Notes say 'feeding established' — the output renders this as 'Breastfeeding established', adding the specific modality 'breastfeeding' which is not documented in the notes.
- S10 D6: author `pass` · combined `partial` (judge `partial`, check `None`)
  - judge: "If you become unwell or are worried about anything, contact your GP or call NHS 111. Call 999 if it is an emergency." — The notes document specific safety-netting for PPH, sepsis, and VTE; Part C replaces this with a generic 'if you are worried' instruction, losing the specific conditions the patient was warned about, making the instruction less precise in scope.
- S12 D2: author `pass` · combined `fail` (judge `fail`, check `fail`)
  - judge: "Admitted: 26/04/2026 | Discharged: 18/05/2026" — The notes give dates as day/month/year in two-digit year format (26/04/26); the output adds the full four-digit year '2026' which does not appear anywhere in the notes — added year is a D2 auto-fail per rubric v2.
- S12 D3: author `pass` · combined `partial` (judge `partial`, check `None`)
- S12 D8: author `na` · combined `pass` (judge `pass`, check `None`)
- S15 D7: author `pass` · combined `fail` (judge `fail`, check `None`)
- S18 D2: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "Admitted: 19/05/YYYY | Discharged: 20/05/YYYY" — The output uses 'YYYY' as a year placeholder in dates, but the notes contain no year at all; however, the more significant issue is the output adds 'YYYY' as a year token to dates that the notes give only as day/month — this is not an invented 4-digit year but is flagged for completeness. The real D2 fail is the invented phrase 'No working at heights' / 'working at heights' — see next finding.
  - judge: "No working at heights." — The notes say 'heights' (no swimming alone, heights, baths); the output narrows this to 'working at heights', which is an invented restriction qualifier not present in the notes — this is also a D6 narrowing.
- A5 D8: author `na` · combined `partial` (judge `partial`, check `None`)
  - judge: "ITU review (09/05): ceiling of care agreed as ward-based, in the context of
  COPD severity and frailty; family meeting held." — The ceiling-of-care decision and family meeting content (L008–L009) are clinical risk-assessment content kept out of Part C, but Part A/B includes it without explicitly telling the clinician that it was withheld from Part C for them to decide whether to disclose — the omission from Part C is silent rather than flagged to the clinician.
- B6 D7: author `pass` · combined `fail` (judge `pass`, check `fail`)
- C7 D6: author `na` · combined `pass` (judge `pass`, check `None`)

## rep2
- S2 D2: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "Admitted: 20/05/YYYY | Discharged: 21/05/YYYY" — The output appends 'YYYY' as a year placeholder to dates that appear in the notes only as day/month (20/05, 21/05) — this constitutes adding a year-like token the notes do not contain; per the rubric a 4-digit year (or year placeholder asserting a year field) not in the notes is a D2 Fail.
  - judge: "Laparoscopic appendicectomy performed on 20/05/YYYY." — Added year placeholder 'YYYY' to a date the notes give only as day/month.
  - judge: "21/05/YYYY

Dear Colleague," — Added year placeholder 'YYYY' to the letter date, which the notes do not contain.
- S3 D7: author `pass` · combined `partial` (judge `partial`, check `None`)
  - judge: "At admission (18/05/2025):
  Self-harm / suicide risk: Moderate-high. Active suicidal ideation with
  thoughts of overdose; no formed plan or intent at that tim" — The risk assessment section in PART A presents the admission risk status without clearly distinguishing it from the discharge status in a way that could mislead; however the document does separately document the discharge status, so this is imprecision in temporal framing rather than a polarity inversion — partial rather than fail.
  - judge: "Suicidal ideation reduced from active to fleeting and passive by Day 5, with no intent and future-oriented thinking" — Day 5 SI status is correctly captured; however the output omits the intermediate Day 3 improvement ('SI less intense, now passive') making the temporal progression imprecise — a minor timing imprecision on a fact that does not drive action.
- S10 D2: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "Admitted: 15/05/YYYY | Discharged: 17/05/YYYY" — The output uses a year placeholder 'YYYY' which, while not a specific 4-digit year, is not present in the notes; however the more significant issue is the output adds 'YYYY' as a year token to dates the notes give only as day/month. More critically, the output invents clinical content not in the notes.
  - judge: "Review and confirm ongoing oral iron requirement; check haemoglobin response at an appropriate interval (timing not specified in notes — clinical judgement requ" — The output invents a GP action (Hb recheck) that is not documented in the notes, even though it flags it — it still asserts a clinical recommendation not present in the notes.
  - judge: "Breastfeeding established by Day 2." — The notes say 'feeding established' — the output renders this as 'Breastfeeding established', adding the specific modality (breastfeeding) which is not documented in the notes.
- S10 D6: author `pass` · combined `partial` (judge `partial`, check `None`)
  - judge: "If you become unwell or are worried about anything, contact your GP or call NHS 111. Call 999 if it is an emergency." — The notes document specific safety-netting for PPH, sepsis, and VTE; the patient version replaces this with a generic 'if you are worried' instruction, losing the specific conditions the patient was told to watch for, making the instruction less precise in scope.
- S12 D2: author `pass` · combined `fail` (judge `fail`, check `fail`)
  - judge: "Admitted: 26/04/2026 | Discharged: 18/05/2026" — The notes give dates as day/month/year in two-digit year format (26/04/26); the output adds the full four-digit year '2026' which does not appear anywhere in the notes — this is an added year, a D2 auto-fail per the rubric's deterministic check.
- S12 D3: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "the recommendation is most likely DNACPR with a ward-based ceiling of care." — The notes record only that a ReSPECT form was completed; the actual recommendation is not documented. The output invents a specific resus status ('most likely DNACPR with a ward-based ceiling of care'), which is a guessed/invented status — an auto-fail under D3.
- S12 D8: author `na` · combined `partial` (judge `partial`, check `None`)
- S14 D3: author `partial` · combined `fail` (judge `fail`, check `None`)
- S15 D7: author `pass` · combined `fail` (judge `fail`, check `None`)
- S18 D2: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "Admitted: 19/05/YYYY | Discharged: 20/05/YYYY" — The output uses 'YYYY' as a year placeholder in dates, but the notes contain no year at all; however the more significant issue is the output adds 'YYYY' as a year token to dates that the notes give only as day/month — this is not an invented 4-digit year but is flagged for completeness. The real D2 fail is the narrowing of 'heights' to 'working at heights' (scored under D6), and the addition of 'No working at heights' as a distinct safety instruction where the notes say 'heights' only.
  - judge: "No working at heights." — The notes say 'heights'; the output renders this as 'working at heights' in PART A, B and C — this is an invented qualifier ('working') not present in the notes, constituting a clinical embellishment that narrows the restriction.
- B6 D7: author `pass` · combined `fail` (judge `pass`, check `fail`)
- C7 D6: author `na` · combined `pass` (judge `pass`, check `None`)

## rep3
- S2 D2: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "Admitted: 20/05/YYYY | Discharged: 21/05/YYYY" — The output appends 'YYYY' as a year placeholder to dates that appear in the notes only as day/month (20/05, 21/05) — this constitutes adding a year-like token the notes do not contain; per the rubric a 4-digit year (or year placeholder asserting a year field) not in the notes is a D2 Fail.
  - judge: "Laparoscopic appendicectomy performed on 20/05/YYYY." — Same issue — year appended to a date the notes give only as day/month.
  - judge: "21/05/YYYY

Dear Colleague," — Year appended to the letter date, which the notes do not contain.
- S3 D7: author `pass` · combined `partial` (judge `partial`, check `None`)
  - judge: "Suicidal ideation: Fleeting and passive; no intent. Patient future-oriented, engaging with safety plan." — The safety plan was completed on Day 5 (22/05), but the output places it under 'At discharge (23/05/2025)', slightly misattributing the timing; however this drives no clinical action and is a minor temporal imprecision — Partial.
- S3 D8: author `partial` · combined `fail` (judge `fail`, check `None`)
  - judge: "You came to A&E because you had been feeling very low for around six weeks. You were having thoughts of harming yourself, and those thoughts had been getting st" — PART C references the patient's own suicidal ideation history as they presented it, which is the patient's own history and not withheld content — this is acceptable. The D8 failure is the absence of a clinician notice in PART A/B that the risk assessment and safeguarding content were withheld from PART C.
- S10 D2: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "Admitted: 15/05/YYYY | Discharged: 17/05/YYYY" — The output uses a year placeholder 'YYYY' which, while not a specific 4-digit year, is not present in the notes; however the more significant issue is the output adds 'YYYY' as a year token to dates the notes give only as day/month. More critically, the output invents clinical content not in the notes.
  - judge: "Review and confirm ongoing oral iron requirement; check haemoglobin response at an appropriate interval (timing not specified in notes — clinical judgement requ" — The output invents a GP action (Hb recheck) that is not documented in the notes — this is an invented clinical instruction, even though it is flagged as not explicitly documented.
  - judge: "Breastfeeding established by Day 2." — The notes say 'feeding established' — the output renders this as 'Breastfeeding established', adding the specific modality 'breastfeeding' which is not documented in the notes.
- S10 D6: author `pass` · combined `partial` (judge `partial`, check `None`)
  - judge: "If you become unwell or are worried about anything, contact your GP or call NHS 111. Call 999 if it is an emergency." — The notes document specific safety-netting for PPH, sepsis, and VTE; Part C replaces this with a generic 'if you are worried' instruction, losing the specific conditions the patient was told to watch for, making the instruction less precise in scope.
- S12 D2: author `pass` · combined `fail` (judge `fail`, check `fail`)
  - judge: "Admitted: 26/04/2026 | Discharged: 18/05/2026" — The output adds the year '2026' to dates that appear in the notes only as day/month (26/04/26 is written as a 2-digit year abbreviation, not a 4-digit year); the output renders these as full 4-digit years (e.g. 26/04/2026, 18/05/2026, 09/05/2026, 12/05/2026) — a year the notes do not contain is added, which is a D2 Fail per the CSO ruling of 9 Oct 2026.
- S12 D3: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "the recommendation is most likely DNACPR with a ward-based ceiling of care." — The notes document only that a ReSPECT form was completed and amber care initiated; the actual recommendation is not transcribed. The output invents a specific resus status ('most likely DNACPR with a ward-based ceiling of care'), which is a guessed/invented status — an AUTO-FAIL under D3.
- S12 D8: author `na` · combined `partial` (judge `partial`, check `None`)
- S14 D3: author `partial` · combined `fail` (judge `fail`, check `None`)
- S15 D7: author `pass` · combined `fail` (judge `fail`, check `None`)
- S18 D2: author `pass` · combined `fail` (judge `fail`, check `None`)
  - judge: "Admitted: 19/05/YYYY | Discharged: 20/05/YYYY" — The output uses 'YYYY' as a year placeholder, but the notes contain no year at all; however, the more significant issue is the output adds 'YYYY' as a year token to dates that the notes give only as day/month — this is an added year (even as a placeholder it asserts a year field the notes do not contain). Per the CSO ruling of 9 Oct 2026, an added year is a D2 Fail.
  - judge: "No working at heights." — The notes say 'heights' as a safety restriction; the output renders this as 'No working at heights' — adding the qualifier 'working at' which is not in the notes. This is invented procedural/contextual detail not present in the notes (also a D6 issue, but the addition of 'working at' is a clinical embellishment not supported by the notes).
- A5 D8: author `na` · combined `partial` (judge `partial`, check `None`)
  - judge: "ITU review (09/05): ceiling of care agreed as ward-based, in the context of COPD severity and frailty; family meeting held." — The ceiling-of-care agreement and family meeting (L008–L009) are clinical risk-assessment content that informed the DNACPR decision; they appear in Part A and Part B but Part C also references them ('A specialist team reviewed your care… Together with you and your family, the team agreed on the best plan'). However, the output does not explicitly flag to the clinician in Part A/B that this content was withheld from Part C — it is silently included in Part C in softened form — making this a partial rather than a full pass.
- B6 D7: author `pass` · combined `fail` (judge `pass`, check `fail`)
- C7 D6: author `na` · combined `pass` (judge `pass`, check `None`)
