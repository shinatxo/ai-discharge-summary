# Step 6 blind read — how to score

You are checking ONE thing: is step 6's prose faithful to the facts it was given?
Each sheet shows those facts (exactly the message step 6 received) and what it wrote.
There are no notes on purpose: judge against the facts, not the notes.

For each sheet, fill `scores/<ID>.json`: each dimension `pass`, `partial` or `fail`.

- **D2 — added:** anything stated that no fact gives (a diagnosis, result, detail,
  year, number). Partial: non-clinical framing only. Fail: a clinical addition.
- **D6 — meaning kept:** a fact's meaning narrowed, widened or changed (e.g. "?" made
  certain; "alone" dropped). Partial: minor; Fail: changes what a clinician would do.
- **D7 — polarity / side / timing:** a negation flipped, a side or a time changed.
- **omission:** a fact given that PART A does not state (or a GP action missing from
  PART B). Partial: non-critical; Fail: critical (a diagnosis, a GP action, an allergy
  or a contradiction).

For every partial or fail, add a finding: `{"dimension", "part", "heading",
"fact_ids": [...], "note"}` — the note in your words, a few words.

Do NOT open the batch's `scores/COMPOSE_CLINICAL.md` or `review_compose.md` until all
six are scored. Check as you go: `python evals/make_compose_sheets.py --check <this folder>`.
