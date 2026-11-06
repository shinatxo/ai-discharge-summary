# Trace-store replay — `w6-pipeline-e09-m01-c02-p04-x5`

pipeline_version `d3df2826d8bbbdac21f62bec560889fb28bc4ce05829ba4b5bfb6d6ed940cad1`. Piece 5's offline orchestrator replay (saved model outputs; step-2 input reconstructed — see evals/replay_orchestrate.py) with the PRODUCTION writer (`trace_store.TraceStore`) into the tests' strict fake table; one Query per run. No Bedrock request (model_calls 0), no AWS, no timing claims (item 7). The fake table is not DynamoDB: request validation, real item sizes and IAM are item 7.

- **Records accounted for: 90/90** — 90 released bundles, rejections 0; a document on a failure: 0.
- **One Query per run: 90/90 runs return 12 items** (11 steps + META; DELIVERY is the worker's — worker tests), one page 90/90, SKs in step-number order 90/90 (UTF-8 byte-sorted 90/90); META complete 90/90.
- Round trip (stored item deserialised == item written): 90/90; one ttl per run = start + 30 days: 90/90.
- In the table: user_sub 0; raw line index 0; raw notes strings 0; notes text present 0 (positive control: multi-line notes stored in a probe item → detected True); quotes > 200 0.
- Size (the writer's conservative bound): largest item 35,157 bytes (limit 400,000); largest run 143,124 bytes (one Query page is at most 1 MB). JSON measure as in piece 5: largest item 40,566, run 174,950.
- Same as the committed piece-5 replay: outcome 90/90, bundle hashes 90/90. Piece-5 checks unchanged: 8a/8b direct 90/90; step 3 vs saved {'identical': 86, 'r3_only': 4}.
- Table total: 1081 items (90 runs × 12 + 1 probe).
