#!/usr/bin/env python3
"""Blind-read sheets for step 6 compose_clinical (CSO decision 4C, 27 Oct 2026).

    python evals/make_compose_sheets.py evals/runs/<batch> --run 1
    python evals/make_compose_sheets.py --check evals/calibration/<batch>-compose-r1

A step-level read: is step 6's prose faithful to the facts it was GIVEN? So a
sheet shows exactly step 6's input (the user message it was sent, rebuilt from
the record's step-3 facts by the same function) and its sections — and NO
notes, so a step-2 error is not blamed on step 6. Boxes show as
"[filled by code: …]".

Items, fixed by rule before anyone looks (the W5 pipeline spot-check rule,
f5c9adc): S1, S4, S10, S13, S16, A5 from one run.

Writes evals/calibration/<batch>-compose-r<run>/:
  sheets/<ID>.md    step 6's input facts and its output
  scores/<ID>.json  an empty template: D2, D6, D7, omission (pass / partial / fail)
  MANIFEST.json     generation IDs and output sha256
  README.md         how to score

Blind by construction: built from steps.validate_facts.facts and
steps.compose_clinical.sections ONLY — never its checks, counts, the scorer's
reports or anything under scores/. Score the sheets BEFORE opening
COMPOSE_CLINICAL.md. Refuses to write into an existing folder. Evidence, not a
bar (n = 6). The console shows IDs and counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "generate"))
import run_cold_eval as rce  # noqa: E402
from pipeline import compose as CMP  # noqa: E402

SHEETS_VERSION = 1
ITEMS = ("S1", "S4", "S10", "S13", "S16", "A5")
DIMENSIONS = ("D2", "D6", "D7", "omission")
VERDICTS = ("pass", "partial", "fail")
DEFAULT_OUT_ROOT = rce.REPO / "evals" / "calibration"
LABELS = {"part_a": "PART A — clinical summary", "part_b": "PART B — GP letter"}

README = """# Step 6 blind read — how to score

You are checking ONE thing: is step 6's prose faithful to the facts it was given?
Each sheet shows those facts (exactly the message step 6 received) and what it wrote.
There are no notes on purpose: judge against the facts, not the notes.

For each sheet, fill `scores/<ID>.json`: each dimension `pass`, `partial` or `fail`.

- **D2 — added:** anything stated that no fact gives (a diagnosis, result, detail,
  year, number). Partial: non-clinical framing only. Fail: a clinical addition.
- **D6 — meaning kept:** a fact's meaning narrowed, widened or changed (e.g. "?" made
  certain; "alone" dropped). Partial: minor; Fail: changes what a clinician would do.
- **D7 — polarity / side / timing:** a negation flipped, a side or a time changed.
- **omission:** a fact given that PART A does not state (or a GP action missing from
  PART B). Partial: non-critical; Fail: critical (a diagnosis, a GP action, an allergy
  or a contradiction).

For every partial or fail, add a finding: `{"dimension", "part", "heading",
"fact_ids": [...], "note"}` — the note in your words, a few words.

Do NOT open the batch's `scores/COMPOSE_CLINICAL.md` or `review_compose.md` until all
six are scored. Check as you go: `python evals/make_compose_sheets.py --check <this folder>`.
"""


class SheetError(RuntimeError):
    pass


def render_sheet(rec: dict) -> str:
    facts = rec["steps"]["validate_facts"]["facts"]
    sections = rec["steps"]["compose_clinical"]["sections"]
    out = [f"# {rec['scenario_id']} — step 6 blind read", "",
           "## Facts given to step 6 (its whole input)", "", "```", CMP.user_text(facts), "```", ""]
    for part in ("part_a", "part_b"):
        out += [f"## {LABELS[part]}", ""]
        for s in (x for x in sections if x["part"] == part):
            out.append(f"### {s['heading']}" + (f"  (facts: {', '.join(s['fact_ids'])})" if s["fact_ids"] else ""))
            out += ["", f"[filled by code: {s['heading']}]" if s["heading"] in CMP.BOXES else s["text"], ""]
    return "\n".join(out) + "\n"


def template(rec: dict) -> dict:
    return {"generation_id": rec["generation_id"], "verdicts": {d: None for d in DIMENSIONS}, "findings": []}


def build(batch_dir: Path, run: int, out_dir: Path | None = None) -> Path:
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    out = out_dir or DEFAULT_OUT_ROOT / f"{manifest['batch_id']}-compose-r{run}"
    if out.exists():
        raise SheetError(f"{out} exists — never overwritten")
    recs = []
    for sid in ITEMS:
        p = batch_dir / f"r{run}" / f"{sid}.json"
        rec = json.loads(p.read_text(encoding="utf-8"))
        if "compose_clinical" not in (rec.get("steps") or {}):
            raise SheetError(f"{sid}: no step 6 output in r{run} — the fixed rule has no substitute; record it")
        recs.append(rec)
    (out / "sheets").mkdir(parents=True)
    (out / "scores").mkdir()
    items = []
    for rec in recs:
        sid = rec["scenario_id"]
        (out / "sheets" / f"{sid}.md").write_text(render_sheet(rec), encoding="utf-8")
        (out / "scores" / f"{sid}.json").write_text(json.dumps(template(rec), indent=2) + "\n", encoding="utf-8")
        body = json.dumps(rec["steps"]["compose_clinical"]["sections"], sort_keys=True, ensure_ascii=False)
        items.append({"scenario_id": sid, "generation_id": rec["generation_id"],
                      "sections_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()})
    (out / "MANIFEST.json").write_text(json.dumps({
        "sheets_version": SHEETS_VERSION, "batch_id": manifest["batch_id"], "run": run,
        "rule": "W5 pipeline spot-check items (f5c9adc): " + ", ".join(ITEMS),
        "dimensions": list(DIMENSIONS), "items": items,
        "excluded": ["step 6 checks and counts", "scores/", "notes"]}, indent=2) + "\n", encoding="utf-8")
    (out / "README.md").write_text(README, encoding="utf-8")
    return out


def check(folder: Path) -> tuple[int, list[str]]:
    """(number fully scored, problems) — problems name files and fields only."""
    done, problems = 0, []
    for p in sorted((folder / "scores").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        v = d.get("verdicts") or {}
        if set(v) != set(DIMENSIONS):
            problems.append(f"{p.name}: verdicts must be exactly {DIMENSIONS}")
            continue
        bad = [k for k, x in v.items() if x is not None and x not in VERDICTS]
        problems += [f"{p.name}: {k} is not one of {VERDICTS}" for k in bad]
        for k, x in v.items():
            if x in ("partial", "fail") and not any(f.get("dimension") == k for f in d.get("findings") or []):
                problems.append(f"{p.name}: {k} is {x} but has no finding")
        if all(x in VERDICTS for x in v.values()) and not bad:
            done += 1
    return done, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", type=Path)
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        done, problems = check(a.path)
        for x in problems:
            print(x)
        print(f"{done}/{len(ITEMS)} scored; {len(problems)} problem(s).")
        return 0 if not problems else 1
    try:
        out = build(a.path, a.run)
    except (SheetError, FileNotFoundError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"{len(ITEMS)} sheets -> {rce._rel(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
