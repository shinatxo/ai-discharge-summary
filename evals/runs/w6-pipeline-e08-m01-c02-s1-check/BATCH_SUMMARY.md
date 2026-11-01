# Batch `w6-pipeline-e08-m01-c02-s1-check` — pipeline steps-1-5b-5a-6

- Prompt (extract_facts): `src/generate/pipeline/prompts/extract_facts.md` — prompt_sha256 `e778f02a56c7e2da1cc338af7bf332db988290357a23e90fb1a9b9b8539671ed`
- Prompt (reconcile_medications): `src/generate/pipeline/prompts/reconcile_medications.md` — prompt_sha256 `b06f8c12a739b80c3d57f41ceada7dd7b1c2af38bc53159353b68425d09d5acf`
- Prompt (compose_clinical): `src/generate/pipeline/prompts/compose_clinical.md` — prompt_sha256 `48613f4d8669369039b77ffdf12828ce72445aaab0fd85f04d4d4ad0a82725fe`
- Model: `anthropic.claude-sonnet-4-6` (eu-west-2); caching on
- Corpus sha256 `d510623686bff3fd851e69dc3943e014aad9b4aff87417fd80e030c71f604321`; runs 1; generations 1; step failures 0
- Est. cost $0.1275 (Cost Explorer is the source of truth)

Scores: `python evals/score_batch.py` on this folder.
