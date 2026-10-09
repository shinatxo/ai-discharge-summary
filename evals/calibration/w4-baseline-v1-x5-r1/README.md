# Blind scoring — w4-baseline-v1-x5 r1

Rubric: `evals/EVAL_RESULTS.md` §2, v2 (sha256 `d2f6709522f0…` — in MANIFEST.json).

1. Open `sheets/<ID>.md`. Score it **before** you see any judge output for this item.
2. Fill `scores/<ID>.json` — for each of D2, D3, D6, D7, D8:
   - `verdict`: `pass` | `partial` | `fail` | `na` (`na` for D6 and D8 only)
   - `auto_fail`: `true` only with `fail` (D2 drugs/resus/diagnosis, D3, D7 allergy or side)
   - `findings` (optional; please give one for every partial/fail):
     `{"output_quote": "...", "note_cites": ["L004"], "reason": "..."}`
   - set `scored_at` (e.g. `"2026-10-10"`) when the file is done.
3. Check as you go: `python evals/make_blind_sheets.py --check evals/calibration/w4-baseline-v1-x5-r1`

Not on these sheets, deliberately: gate result, cost, deterministic scores, judge output.
