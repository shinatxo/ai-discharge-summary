"""Item 3 R1 step 4 — the case locator (step-4 design Round 1 §1.1-1.3; decision D1 (a), Shina 11 Nov 2026).

Finds, in a saved batch's step-7 INPUTS, the lines the D3' control and boundary cases sit on:

    python evals/r1_locate_cases.py <batch> --inputs <inputs.json> --out <cases-located.json>
                                    [--reading-out <CASE-READING.md>]

What it reads: exactly `patient_input(record["steps"]["compose_clinical"])["sections"]` of every
record — step 7's A-only input, the same reader as step 3's coverage scan — after the harness's own
frozen-inputs check. Nothing else in a record is read (no step 7, no 8b, no notes, no facts).

What it does NOT do: it never calls `render_patient.split_lines` or anything after it. That function
is the renderer's first stage and refuses forms, so calling it would reveal form refusals on the 90
before the frozen run. The locator numbers lines itself, the way the ledger does — physical line n
of `text.split("\\n")` (blank lines counted), sentence k by the renderer's sentence split on prose
lines, None for "- " items and diagnosis lines — and never judges whether a line fits the grammar.

Screens are word lists and the approved withheld / seek-help patterns, applied to the input. The
output holds locations, categories, screen ids, tokens and the sha256 of each physical line — never
a sentence. The console prints counts only. `--reading-out` writes the reading sheet (the located A
lines, A wording only) and is refused inside the repository unless under docs/_local/ (gitignored):
the sheet is read by Claude and Shina to set each case's expected outcome and is never committed.

Categories: L1 mixed content (history / diagnosis + withheld wording) · L2 negated · L3 planned ·
L4 conditional instruction · L5 parent / carer · L6i inferred · L6u documented-but-uncertain ·
L7 F3 reason for admission · U1 the declared upstream defect (S1 "2 weeks"). L8 (every diagnosis
line) is a class rule checked by the evaluator on every generation and is not listed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import render_offline as H  # noqa: E402
from pipeline import compose_patient as CP7  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402

LOCATOR_VERSION = 1
HISTORY = ("presenting_complaint", "diagnosis")
INSTRUCTION_HEADINGS = ("follow_up", "gp_actions", "treatment")
_RISK = re.compile(r"\brisk\w*", re.IGNORECASE)
_RULED_OUT = re.compile(r"\brule[sd]?\s+out\b", re.IGNORECASE)
_PLANNED = re.compile(r"\b(?:planned|plan|booked|awaiting|awaited|pending|scheduled|listed\s+for|due|will|"
                      r"to\s+have|to\s+undergo)\b", re.IGNORECASE)
_CONDITIONAL = re.compile(r"\b(?:if|unless|until|when|in\s+case|as\s+required|PRN)\b", re.IGNORECASE)
_PERSON = re.compile(r"\b(?:patient|he|she|his|her|hers|him|they|their|mum|mother|dad|father|parents?|carers?|"
                     r"child|baby|infant)\b", re.IGNORECASE)
_UNCERTAIN = re.compile(r"\?(?=[A-Za-z])|\b(?:query|possibl\w*|probabl\w*|likely|suspect\w*|presumed|impression)\b|"
                        r"\bconcerning\s+for\b", re.IGNORECASE)
_TWO_WEEKS = re.compile(r"\b2\s*(?:weeks?\b|/52)", re.IGNORECASE)
U1_SCENARIO = "S1"


class LocateError(ValueError):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def units(section: dict):
    """(line, sentence, physical line, unit text) for every non-blank line — the ledger's numbering,
    with no grammar judgement."""
    heading = section["heading"]
    for n, raw in enumerate(section["text"].split("\n"), 1):
        s = raw.rstrip()
        if not s.strip():
            continue
        if s.startswith("-") or heading == "diagnosis":
            yield n, None, s, s
        else:
            for k, sent in enumerate((x for x in RP._SENT.split(s) if x.strip()), 1):
                yield n, k, s, sent.strip()


def _entry_hits(text: str, entries) -> list[str]:
    return sorted({e.id for e in entries if RP._entry_matches(e, text)})


def screens(heading: str, text: str, audience: str, scenario_id: str, tables: RP.Tables) -> list[tuple[str, list[str]]]:
    """[(category, screen ids / tokens)] for one unit of step-7 input."""
    out = []
    withheld = _entry_hits(text, tables.withheld)
    boxed = withheld + _entry_hits(text, tables.seek_help) + \
        [f"resus{i}" for i, p in enumerate(RP._RESUS_PATTERNS) if p.search(text)]
    if heading in HISTORY and (withheld or _RISK.search(text)):
        out.append(("L1", withheld + (["risk"] if _RISK.search(text) else [])))
    neg = sorted({m.group(0).lower() for m in RP.WORD.finditer(text) if m.group(0).lower() in RP.NEGATION}
                 | ({"ruled out"} if _RULED_OUT.search(text) else set()))
    if neg:
        out.append(("L2", neg + [f"meets:{b}" for b in boxed]))
    planned = sorted({re.sub(r"\s+", " ", m.group(0).lower()) for m in _PLANNED.finditer(text)})
    if planned:
        out.append(("L3", planned))
    cond = sorted({re.sub(r"\s+", " ", m.group(0).lower()) for m in _CONDITIONAL.finditer(text)})
    if heading in INSTRUCTION_HEADINGS and cond:
        out.append(("L4", cond + [f"meets:{b}" for b in boxed]))
    person = sorted({m.group(0).lower() for m in _PERSON.finditer(text)})
    if audience == "parent_or_carer" and person:
        out.append(("L5", person))
    if RP.INFERRED_BRACKET.search(text):
        out.append(("L6i", ["inferred"]))
    unc = sorted({re.sub(r"\s+", " ", m.group(0).lower()) for m in _UNCERTAIN.finditer(text)})
    if unc:
        out.append(("L6u", unc))
    if heading in HISTORY and RP.F3_NEVER_DROP.search(text):
        out.append(("L7", ["f3"]))
    if scenario_id == U1_SCENARIO and _TWO_WEEKS.search(text):
        out.append(("U1", ["2 weeks"]))
    return out


def locate(batch_dir: Path, inputs_path: Path, tables: RP.Tables | None = None) -> tuple[dict, list[dict]]:
    """(the located list — no text, the reading rows — with text, never committed)."""
    src = batch_dir.resolve()
    try:
        manifest, _index, mb, bb, rels, rbs = H._read_source(src)
        ib = inputs_path.read_bytes()
    except (H.HarnessError, OSError) as exc:
        raise LocateError(str(exc)) from None
    if json.loads(ib) != H._inputs_from(manifest, mb, bb, rels, rbs):
        raise LocateError("the inputs file does not match this batch (records or hashes changed)")
    tables = tables if tables is not None else RP.load_tables()
    cases, reading, n_units = [], [], 0
    for rel, rb in zip(rels, rbs):
        rec = json.loads(rb)
        cc = (rec.get("steps") or {}).get("compose_clinical")
        if cc is None:
            continue
        inp = CP7.patient_input(cc)                    # the ONLY part of a record that is read
        for sec in inp["sections"]:
            for n, k, line, text in units(sec):
                n_units += 1
                for category, ids in screens(sec["heading"], text, inp["audience"], rec["scenario_id"], tables):
                    cid = f"{category}-{rec['scenario_id']}-r{rec['run']}-{sec['section_id']}-{n}-{k or 0}"
                    cases.append({"case_id": cid, "category": category, "scenario_id": rec["scenario_id"],
                                  "run": rec["run"], "record": rel, "source_id": sec["section_id"],
                                  "heading": sec["heading"], "line": n, "sentence": k,
                                  "line_sha256": _sha(line), "screen": ids})
                    reading.append({"case_id": cid, "category": category, "scenario_id": rec["scenario_id"],
                                    "run": rec["run"], "heading": sec["heading"], "audience": inp["audience"],
                                    "line_sha256": _sha(line), "text": text, "screen": ids})
    counts = dict(sorted(Counter(c["category"] for c in cases).items()))
    located = {"locator_version": LOCATOR_VERSION, "source_batch": manifest["batch_id"],
               "inputs_sha256": hashlib.sha256(ib).hexdigest(),
               "tables": dict(sorted(tables.sha256.items())),
               "records": len(rels), "units_scanned": n_units, "counts": counts, "cases": cases}
    return located, reading


def reading_sheet(located: dict, reading: list[dict]) -> str:
    """The located A lines, each distinct (scenario, heading, category, wording) once, with its case ids."""
    groups: dict[tuple, list[dict]] = {}
    for r in reading:
        groups.setdefault((r["scenario_id"], r["category"], r["heading"], r["text"]), []).append(r)
    out = ["# R1 step 4 — case reading sheet (NEVER COMMITTED — holds step-7 input wording)", "",
           f"Source batch `{located['source_batch']}`; inputs sha256 `{located['inputs_sha256']}`. "
           "PART A wording only — the same text the blind-read sheets show as step 7's input. No renderer, "
           "step-7 or 8b output.", "",
           f"{len(reading)} located cases in {len(groups)} distinct lines.", ""]
    last = None
    for (sid, cat, heading, text), rows in sorted(groups.items()):
        if (sid, cat) != last:
            out += [f"## {sid} · {cat}", ""]
            last = (sid, cat)
        runs = ", ".join(f"r{r['run']}" for r in rows)
        out += [f"- **{heading}** ({rows[0]['audience']}; {runs}; screen: {', '.join(rows[0]['screen'])})",
                f"  - A: `{text}`",
                f"  - cases: {', '.join(r['case_id'] for r in rows)}", ""]
    return "\n".join(out) + "\n"


def _reading_path_ok(path: Path) -> bool:
    path = path.resolve()
    return ROOT not in path.parents or (ROOT / "docs" / "_local") in path.parents


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("batch", type=Path)
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--reading-out", type=Path, default=None)
    a = ap.parse_args(argv)
    try:
        if a.out.exists() or (a.reading_out is not None and a.reading_out.exists()):
            raise LocateError("an output file already exists — never written over")
        if a.reading_out is not None and not _reading_path_ok(a.reading_out):
            raise LocateError("the reading sheet may be written only outside the repository or under docs/_local/")
        located, reading = locate(a.batch, a.inputs)
    except (LocateError, RP.RenderRefusal) as exc:
        print(f"refused: {getattr(exc, 'code', None) or exc}", file=sys.stderr)
        return 2
    a.out.write_text(json.dumps(located, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if a.reading_out is not None:
        a.reading_out.write_text(reading_sheet(located, reading), encoding="utf-8")
    print(f"located: {located['records']} records, {located['units_scanned']} lines scanned, "
          f"{len(located['cases'])} cases " + ", ".join(f"{k} {v}" for k, v in located["counts"].items())
          + f" -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
