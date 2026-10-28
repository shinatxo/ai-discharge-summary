# Batch `w6-pipeline-e07-m01-c01-s1-check` — pipeline steps-1-5b-5a-6

- Prompt (extract_facts): `src/generate/pipeline/prompts/extract_facts.md` — prompt_sha256 `208ac00d3d6096f05c4ba91137e923aec4054651c042f55d9e7c089bcd7675a2`
- Prompt (reconcile_medications): `src/generate/pipeline/prompts/reconcile_medications.md` — prompt_sha256 `b06f8c12a739b80c3d57f41ceada7dd7b1c2af38bc53159353b68425d09d5acf`
- Prompt (compose_clinical): `src/generate/pipeline/prompts/compose_clinical.md` — prompt_sha256 `cb55505a0692ff0aa7548836ab5dc777ac116dd604081d0864a1654284b0282d`
- Model: `anthropic.claude-sonnet-4-6` (eu-west-2); caching on
- Corpus sha256 `d510623686bff3fd851e69dc3943e014aad9b4aff87417fd80e030c71f604321`; runs 1; generations 1; step failures 0
- Est. cost $0.1266 (Cost Explorer is the source of truth)

Scores: `python evals/score_batch.py` on this folder.
