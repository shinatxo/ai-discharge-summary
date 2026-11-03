# Batch `w6-pipeline-e09-m01-c02-p02-s1-check` — pipeline steps-1-5b-5a-6-7

- Prompt (extract_facts): `src/generate/pipeline/prompts/extract_facts.md` — prompt_sha256 `481a6c68d94acb1949ab3f0cd694d538b019446ed6cf8e6951560394cf02366c`
- Prompt (reconcile_medications): `src/generate/pipeline/prompts/reconcile_medications.md` — prompt_sha256 `b06f8c12a739b80c3d57f41ceada7dd7b1c2af38bc53159353b68425d09d5acf`
- Prompt (compose_clinical): `src/generate/pipeline/prompts/compose_clinical.md` — prompt_sha256 `48613f4d8669369039b77ffdf12828ce72445aaab0fd85f04d4d4ad0a82725fe`
- Prompt (compose_patient): `src/generate/pipeline/prompts/compose_patient.md` — prompt_sha256 `e292d44750c3906b1cde40f625273d88448c277d4a0106e17125f7469b238f40`
- Model: `anthropic.claude-sonnet-4-6` (eu-west-2); caching on
- Corpus sha256 `d510623686bff3fd851e69dc3943e014aad9b4aff87417fd80e030c71f604321`; runs 1; generations 1; step failures 0
- Est. cost $0.1462 (Cost Explorer is the source of truth)

Scores: `python evals/score_batch.py` on this folder.
