# Gold records — step-level metrics (W4)

One file per scenario, `<ID>.json`, scored identically against v1's output and
the agentic pipeline's step outputs (ADR-009 (e), "Build record — W4").
Validated by `evals/gold.py`; `tests/test_gold.py` runs the validator on every file.

**Rule:** gold states what the **notes** document — never what any generator
wrote. It is drafted from `src/canary/scenarios.json` alone, then approved by
the CSO (`status: approved`). Older checkpoints (`EVAL_RESULTS.md` §4, the
scenario markdown files) are a cross-check only: they contain values the
notes do not support (S1's ticagrelor dose; Run 3's five gold errors).

Every claim cites `{lines, quote}`: line IDs from the pipeline's own step-1
index, quote verbatim as step 3 defines it. The validator checks each with the
pipeline's `verify_cite`. `notes_sha256` pins the record to the exact notes.

| Field | Meaning |
|---|---|
| `age_group` | Selects the audience of the pinned line (5b): neonate / infant / child → paediatric |
| `safety_net.route` | `documented` if the notes record advice given to the patient or carer (5b's definition, incl. the 1 Oct ruling on a record of advice with its scope); else `fallback` |
| `safety_net.advice[]` | The note lines that record that advice, each with `kind`: `instruction` (the advice itself is written) or `record` (the notes say advice was given and what it covered — documented advice since the CSO ruling of 1 Oct 2026, which post-dates v0.7) |
| `safety_net.seek_help` | `{documented, cites}` — whether the notes document a seek-help trigger (when and where to get help). **Distinct from advice** (CSO, 6 Oct 2026): without one, nothing beyond the pinned line may tell the patient when to seek help, even where other advice is documented |
| `medications.discharge_status` | As step 2's enum: `listed` / `referenced_not_listed` / `none_required` / `not_documented` |
| `drugs[]` | Per drug: `drug`, `aliases`, `dose`, `route` (null = oral/not stated), `frequency` (null = not documented), `tag`, `previous` (for a change), `dh_cites`, `discharge_cites` |
| `dose` / `frequency` on a `stopped` drug | The pre-admission values |
| `tag` | `new` · `continued` · `increased` · `decreased` · `stopped` · `withheld` · `changed` (the v0.7 prompt's tags, plus `withheld` and `changed` for frequency/route changes) |
| `basis` | `documented` — the discharge state is in the notes; `inferred_from_dh` — a DH drug with no documented change, where the notes do not list the TTOs. Only `continued` can be inferred |
| `conflict` | Optional, per drug: the notes give two or more values for `dose` or `frequency` (B6's warfarin). That field is null; the correct output flags the conflict and never picks a value |
| `must_not_appear[]` | Drugs that must not be on a discharge list (inpatient-only courses, stopped drugs, drugs not prescribed) |
| `review_notes` | What the CSO must decide before approving |
