# Orchestrator replay — `w6-pipeline-e09-m01-c02-p04-x5`

pipeline_version `3e4eae346a985e0c466e52404458c1649a871de4637a3df376b414ee46f56bac`. Offline: saved model outputs at the run_step boundary, step-2 input RECONSTRUCTED from step 3's saved output (see the module docstring); every code step re-run; no Bedrock request (model_calls 0); no timing claims (item 7).

- **Records accounted for: 90/90** — 90 released bundles, rejections 0; a document on a failure: 0; a release without a bundle: 0; failure callbacks on a success: 0.
- Gate on assembled A + C: {'documented_advice': 50, 'clean': 40}.
- Same bundle as 8a/8b run directly on the saved inputs (piece 4's path): 90/90.
- Committed piece-4 replay: outcome equal 90/90, gate equal 90/90.
- Step 3 re-run on the reconstructed input vs saved: {'identical': 86, 'r3_only': 4} (quote_too_long placeholders: 6; R3-only records: S11 r2, S11 r3, S11 r4, S11 r5). Other re-run code steps differing from saved: 0.
- Trace: complete (11 step items + META) 90/90; raw line index 0; raw notes strings 0; notes text present 0; quotes > 200 0; largest item 40,566 bytes; largest run 174,950 bytes.
- Stored units: quotes > 200 0; largest ResultsTable item ~38,504 bytes (limit 400,000).
