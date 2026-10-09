#!/usr/bin/env python3
"""Blind-scoring sheets for the rubric v2 calibration (W5).

    python evals/make_blind_sheets.py evals/runs/w4-baseline-v1-x5 --run 1
    python evals/make_blind_sheets.py --check evals/calibration/w4-baseline-v1-x5-r1

Writes evals/calibration/<batch>-r<run>/:
  sheets/<ID>.md    the notes, numbered as step 1 numbers them, and the output verbatim
  scores/<ID>.json  an empty template for D2, D3, D6, D7, D8 (the judge's output shape)
  MANIFEST.json     what is being scored: generation IDs, notes and output sha256, rubric sha256
  README.md         how to score

Blind by construction: a sheet is built from the corpus notes and the record's
`output` only. The gate result, cost, deterministic scores and any judge output
are never read into it. Refuses to write into an existing folder, so a re-run
can never overwrite scores. `--check` validates the filled scores as you go.
The console shows IDs and counts only, never note text.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gold as G  # noqa: E402
import run_cold_eval as rce  # noqa: E402

SHEETS_VERSION = 1
DIMENSIONS = ("D2", "D3", "D6", "D7", "D8")
VERDICTS = ("pass", "partial", "fail", "na")
NA_ALLOWED = ("D6", "D8")                  # rubric v2: D2, D3 and D7 always apply
RUBRIC_PATH = rce.REPO / "evals" / "EVAL_RESULTS.md"
DEFAULT_OUT_ROOT = rce.REPO / "evals" / "calibration"
EXCLUDED = ("gate result", "cost", "deterministic scores", "judge output")

_RUBRIC_START = "## 2. Scoring rubric"
_RUBRIC_END = re.compile(r"(?m)^## 3\. ")
_LINE_ID = re.compile(r"^L\d{3}$")


class SheetError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# The rubric being scored against: EVAL_RESULTS.md section 2, pinned by sha256
# ---------------------------------------------------------------------------
def rubric_section(path: Path = RUBRIC_PATH) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.find(_RUBRIC_START)
    if start == -1:
        raise SheetError("rubric section 2 not found")
    end = _RUBRIC_END.search(text, start)
    if end is None:
        raise SheetError("end of rubric section 2 not found")
    return text[start:end.start()].rstrip() + "\n"


def rubric_sha256(path: Path = RUBRIC_PATH) -> str:
    return rce.sha256_text(rubric_section(path))


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------
def _fence(text: str) -> str:
    """A code fence longer than any backtick run in the text (outputs contain ```)."""
    longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def score_template(rec: dict) -> dict:
    return {
        "sheets_version": SHEETS_VERSION,
        "generation_id": rec["generation_id"],
        "scenario_id": rec["scenario_id"],
        "scorer": "author_blind",
        "scored_at": None,
        "dimensions": {d: {"verdict": None, "auto_fail": False, "findings": []}
                       for d in DIMENSIONS},
    }


def render_sheet(rec: dict, lines: dict) -> str:
    sid, gid, output = rec["scenario_id"], rec["generation_id"], rec["output"]
    notes_block = "\n".join(f"{lid}  {text}" for lid, text in lines.items())
    nf, of = _fence(notes_block), _fence(output)
    return (
        f"# {sid} — blind sheet\n\n"
        f"- Generation: `{gid}`\n"
        f"- Score in `scores/{sid}.json` against `evals/EVAL_RESULTS.md` §2 (rubric v2).\n"
        f"- Dimensions: {', '.join(DIMENSIONS)}. D8 reads PART C only; "
        f"N/A only for {' and '.join(NA_ALLOWED)}.\n\n"
        f"## Notes (line IDs as step 1 numbers them)\n\n{nf}\n{notes_block}\n{nf}\n\n"
        f"## Output (verbatim)\n\n{of}\n{output}\n{of}\n"
    )


README = """# Blind scoring — {batch_id} r{run}

Rubric: `evals/EVAL_RESULTS.md` §2, v2 (sha256 `{rubric12}…` — in MANIFEST.json).

1. Open `sheets/<ID>.md`. Score it **before** you see any judge output for this item.
2. Fill `scores/<ID>.json` — for each of D2, D3, D6, D7, D8:
   - `verdict`: `pass` | `partial` | `fail` | `na` (`na` for D6 and D8 only)
   - `auto_fail`: `true` only with `fail` (D2 drugs/resus/diagnosis, D3, D7 allergy or side)
   - `findings` (optional; please give one for every partial/fail):
     `{{"output_quote": "...", "note_cites": ["L004"], "reason": "..."}}`
   - set `scored_at` (e.g. `"2026-10-10"`) when the file is done.
3. Check as you go: `python evals/make_blind_sheets.py --check {folder}`

Not on these sheets, deliberately: {excluded}.
"""


def build(batch_dir: Path, run: int, out_dir: Path | None = None,
          corpus_path: Path = rce.DEFAULT_CORPUS_PATH,
          rubric_path: Path = RUBRIC_PATH) -> Path:
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    index = json.loads((batch_dir / "batch.json").read_text(encoding="utf-8"))
    batch_id = manifest["batch_id"]
    out_dir = out_dir or DEFAULT_OUT_ROOT / f"{batch_id}-r{run}"
    if out_dir.exists():
        raise SheetError(f"{out_dir.name} already exists — refusing to overwrite scores")

    corpus, corpus_sha = rce.load_corpus(corpus_path)
    if corpus_sha != manifest["corpus"]["sha256"]:
        raise SheetError("the corpus has changed since this batch was generated")
    by_id = {s.id: s for s in corpus}

    # Read and check everything before writing anything.
    items = []
    for g in index["generations"]:
        if g["run"] != run:
            continue
        rec = json.loads((batch_dir / g["record"]).read_text(encoding="utf-8"))
        gid, sid = rec["generation_id"], rec["scenario_id"]
        if rec["generator"] != "v1":
            raise SheetError(f"{gid}: blind sheets are built for v1 records only")
        if rec["notes_sha256"] != by_id[sid].notes_sha256:
            raise SheetError(f"{gid}: notes_sha256 does not match the corpus")
        if rec.get("error") or not rec.get("output"):
            raise SheetError(f"{gid}: no output to score")
        items.append((rec, G.line_index(by_id[sid].notes)))
    if not items:
        raise SheetError(f"no generations for run {run} in {batch_id}")

    rubric_sha = rubric_sha256(rubric_path)
    (out_dir / "sheets").mkdir(parents=True)
    (out_dir / "scores").mkdir()
    entries = []
    for rec, lines in items:
        sid = rec["scenario_id"]
        (out_dir / "sheets" / f"{sid}.md").write_text(render_sheet(rec, lines), encoding="utf-8")
        (out_dir / "scores" / f"{sid}.json").write_text(
            json.dumps(score_template(rec), indent=2) + "\n", encoding="utf-8")
        entries.append({"scenario_id": sid, "generation_id": rec["generation_id"],
                        "notes_sha256": rec["notes_sha256"],
                        "output_sha256": rce.sha256_text(rec["output"]),
                        "sheet": f"sheets/{sid}.md", "scores": f"scores/{sid}.json"})
    (out_dir / "MANIFEST.json").write_text(json.dumps({
        "sheets_version": SHEETS_VERSION, "batch_id": batch_id, "run": run,
        "generator": manifest["generator"],
        "created_at": dt.datetime.now(rce.LONDON).isoformat(timespec="seconds"),
        "rubric": {"path": rce._rel(rubric_path), "section": _RUBRIC_START,
                   "sha256": rubric_sha},
        "corpus_sha256": corpus_sha, "dimensions": list(DIMENSIONS),
        "verdicts": list(VERDICTS), "na_allowed": list(NA_ALLOWED),
        "excluded": list(EXCLUDED), "items": entries}, indent=2) + "\n", encoding="utf-8")
    (out_dir / "README.md").write_text(README.format(
        batch_id=batch_id, run=run, rubric12=rubric_sha[:12], excluded=", ".join(EXCLUDED),
        folder=rce._rel(out_dir)), encoding="utf-8")
    return out_dir


# ---------------------------------------------------------------------------
# Checking the filled scores
# ---------------------------------------------------------------------------
def check(folder: Path, corpus_path: Path = rce.DEFAULT_CORPUS_PATH) -> tuple[int, list[str]]:
    """(number of complete score files, problems). Problems name IDs and fields only."""
    m = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
    corpus, _ = rce.load_corpus(corpus_path)
    by_id = {s.id: s for s in corpus}
    problems, complete = [], 0
    for item in m["items"]:
        sid = item["scenario_id"]
        s = json.loads((folder / item["scores"]).read_text(encoding="utf-8"))
        if s.get("generation_id") != item["generation_id"]:
            problems.append(f"{sid}: generation_id does not match the manifest")
            continue
        line_ids = set(G.line_index(by_id[sid].notes))
        mine = []
        for d in DIMENSIONS:
            v = (s.get("dimensions") or {}).get(d)
            if not isinstance(v, dict):
                mine.append(f"{sid} {d}: missing")
                continue
            verdict = v.get("verdict")
            if verdict is None:
                mine.append(f"{sid} {d}: no verdict yet")
            elif verdict not in VERDICTS:
                mine.append(f"{sid} {d}: verdict must be one of {', '.join(VERDICTS)}")
            elif verdict == "na" and d not in NA_ALLOWED:
                mine.append(f"{sid} {d}: na is not allowed for {d}")
            if v.get("auto_fail") not in (True, False):
                mine.append(f"{sid} {d}: auto_fail must be true or false")
            elif v.get("auto_fail") and verdict != "fail":
                mine.append(f"{sid} {d}: auto_fail needs verdict fail")
            for i, f in enumerate(v.get("findings") or []):
                bad = [c for c in (f.get("note_cites") or [])
                       if not (isinstance(c, str) and _LINE_ID.match(c) and c in line_ids)]
                if bad:
                    mine.append(f"{sid} {d} finding {i + 1}: unknown line ID(s) {', '.join(map(str, bad))}")
        if not s.get("scored_at"):
            mine.append(f"{sid}: scored_at not set")
        problems += mine
        complete += not mine
    return complete, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("batch_dir", nargs="?", type=Path)
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--check", type=Path, metavar="FOLDER")
    a = ap.parse_args(argv)
    try:
        if a.check:
            complete, problems = check(a.check)
            n = len(json.loads((a.check / "MANIFEST.json").read_text(encoding="utf-8"))["items"])
            for p in problems:
                print(f"  - {p}")
            print(f"{complete}/{n} score files complete; {len(problems)} problem(s)")
            return 0 if not problems else 1
        if a.batch_dir is None:
            ap.error("pass a batch folder, or --check FOLDER")
        out = build(a.batch_dir, a.run, a.out)
        n = len(json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))["items"])
        print(f"{n} blind sheets -> {rce._rel(out)}")
        return 0
    except SheetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
