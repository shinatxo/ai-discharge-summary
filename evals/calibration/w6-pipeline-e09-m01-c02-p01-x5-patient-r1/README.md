# Step 7 blind read — how to score

You are checking ONE thing: does step 7's leaflet say the same things as the PART A
sections it was given? Each sheet shows those sections (exactly the message step 7
received), what step 7 wrote, and the leaflet as code assembles it. There are no notes
and no facts on purpose: judge step 7 against its input.

The two boxes (medicines, advice) and the translation header are written by code, not
step 7. Judge step 7's own sections; use the assembled leaflet only for audience and tone.

For each sheet, fill `scores/<ID>.json`: each dimension `pass`, `partial` or `fail`.

- **added:** anything step 7 states that its input does not (a fact, cause, result,
  timescale, warning, reassurance, piece of advice, number). Partial: non-clinical
  framing only. Fail: a clinical addition.
- **meaning:** something said with different force or scope (HAZ-17): a condition such
  as "only if" / "until" dropped; a "?" diagnosis made certain; an instruction made
  stronger, weaker, broader or narrower. Partial: minor; Fail: changes what the patient
  would do.
- **withheld:** content a patient leaflet should not carry (HAZ-16): resuscitation,
  risk levels or scores, safeguarding or police detail, a disagreement in the notes.
  Partial: borderline; Fail: disclosed.
- **audience:** addressed to the right reader (the patient, or the parent / carer for a
  child), plain and calm, nothing alarming or stigmatising. Partial: minor; Fail: wrong
  reader or harmful tone.

An item passes when all four are `pass` (bar line 10: 8/8).

For every partial or fail, add a finding: `{"dimension", "heading", "source_ids": [...],
"note"}` — the note in your words, a few words.

Do NOT run score_batch or open the batch's `scores/PATIENT_VERSION.md` or
`review_patient.md` until all eight are scored. Check as you go:
`python evals/make_patient_sheets.py --check <this folder>`.
