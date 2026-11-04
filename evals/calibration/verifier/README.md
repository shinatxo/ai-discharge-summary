# Step 7v calibration (ADR-010 V5)

- `blind_read_map.json` — the CSO's blind-read findings mapped to sentences / must-keep items (O4).
  Approved 4 Nov ~15:20; **amended by the CSO's audit 4 Nov ~15:36** (see its `status` and each row's `note`).
- `<prompt_sha12>/<batch_id>/rep<k>/r<run>/<ID>.json` — verifier outputs on saved leaflets (`calibrate_verifier.py run`).
  Never overwritten. `scores/` — `calibrate_verifier.py score`.

## Runs

| Prompt | Folder | Role | Note |
|---|---|---|---|
| v0.1 `996b88ca22a6` | `996b88ca22a6/w6-pipeline-e09-m01-c02-p04-x5/` | **PILOT (dev)** — not a measured run | 270 calls (90 × 3), **0 failures**, est. **$9.06** (printed estimate was "≤ $7.13" — exceeded; estimate corrected). Started 4 Nov ~15:25, before the audit. Its verdicts were **not opened** before the audit's corrections (map amendments, counting rule v2, prompt v0.2) were made. Preserved with its manifest. v0.1 records one verdict per sentence and so cannot catch two defects in one sentence (S4 r2); it is never used for the bar. |
| v0.2 `714377f4d7df` | (to run) | dev, measured | Glaucoma example removed; one item per fault; evidence must be a listed section. |

## Counting rule

**v3** = v2 + eligibility (both frozen 4 Nov, before any measured run); label counts in `LABEL_COUNTS.json` — in `calibrate_verifier.py`'s docstring. v1 (C1 as first approved)
is superseded: it could report a pass without the required defects being caught (CSO audit).
