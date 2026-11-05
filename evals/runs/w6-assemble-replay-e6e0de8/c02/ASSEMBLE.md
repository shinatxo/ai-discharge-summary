# 8a/8b offline replay — w6-pipeline-e09-m01-c02-x5 (8a only)

Accounting (F2): every record is a validated bundle (or 8a candidates with --ab-only), or a fixed code and no document.

| Records | 90 |
|---|---|
| candidates_ab | 90 |

Rejections by step:code — none

Gate on assembled A + C — not run
Placeholders left in released bundles — 0

Warnings (part:kind) — A:dose_outside_box 18, A:unit_added 1, B:dose_outside_box 19, B:drug_not_in_cited_facts 1, B:unit_added 1

C meaning warnings vs step 7's own checks, located (heading, kind) per record — 0/0 records identical; only in 8b 0, only in step 7 0. Screening checks: no warning does not establish complete detection.

Branches exercised (records) — ab_unverified_marker 32, b_continued_count 24, b_resus_not_shown 40, b_resus_route 50, contradiction_quotes 3, contradiction_value_unverified 3, facts_held_back 6, header_inferred 45, header_not_extracted 18, med_status_listed 45, med_status_none_required 10, med_status_not_documented 20, med_status_referenced_not_listed 15, resus_route_absent 40, resus_route_documented 45, resus_route_documented_but_absent 5

## A5 preview — not in force for c0.2; effective from the first measured c0.3 batch.

(a) contradictions recorded by step 2 in A's code block exactly once: 6/6
(b) step-6 prose screen (contradiction ID cited, or "Notes conflict" wording): 13 flagged — part_a/allergies 5, part_a/medication_conflicts 3, part_b/medication_conflicts 5
D1 c0.2 exception (exact RESUS_CONFLICT_TEXT sentence in A treatment citing a resus contradiction) used: 0 — none

No composite verdict: (b) is a screen, not a ruling, and c0.2 tells step 6 to write contradictions in prose (L2, disclosed).
