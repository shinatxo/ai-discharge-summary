# Step 6 sentences to rule on — batch `w6-pipeline-e08-m01-c02-s1-check`

Each sentence sits OUTSIDE a box and matched a pattern. Rule on each: **acceptable** (it is not advice / a resus status / a discharge medicine — e.g. "presented to A&E", "fluid resuscitation", an in-hospital drug) or **defect** (it is). Store rulings in `evals/gold/adjudications/<ID>.json` under `compose_rulings` as `{key: ruling}`; they are reused across runs.

- **S1** `0994c29036093642` medication `ticagrelor` · part_b / admission_summary (runs 1) · ruling: acceptable: Treatment included commencement of ticagrelor (dual antiplatelet therapy) and fondaparinux, uptitration of bisoprolol to 7.5mg once daily, and addition of isosorbide mononitrate 30mg once daily.
- **S1** `f75e727963079ca8` medication `bisoprolol` · part_b / admission_summary (runs 1) · ruling: acceptable: Treatment included commencement of ticagrelor (dual antiplatelet therapy) and fondaparinux, uptitration of bisoprolol to 7.5mg once daily, and addition of isosorbide mononitrate 30mg once daily.
