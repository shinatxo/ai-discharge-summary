# Step 6 sentences to rule on — batch `w6-pipeline-e07-m01-c01-s1-check`

Each sentence sits OUTSIDE a box and matched a pattern. Rule on each: **acceptable** (it is not advice / a resus status / a discharge medicine — e.g. "presented to A&E", "fluid resuscitation", an in-hospital drug) or **defect** (it is). Store rulings in `evals/gold/adjudications/<ID>.json` under `compose_rulings` as `{key: ruling}`; they are reused across runs.

- **S1** `047632a85534deb2` medication `bisoprolol` · part_b / admission_summary (runs 1) · ruling: acceptable: Bisoprolol was uptitrated to 7.5mg once daily and isosorbide mononitrate 30mg once daily was added.
- **S1** `d86f03b53bf46bff` medication `ticagrelor` · part_b / admission_summary (runs 1) · ruling: acceptable: Ticagrelor was added for dual antiplatelet therapy and fondaparinux was commenced.
