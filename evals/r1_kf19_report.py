#!/usr/bin/env python3
"""Item 3 R1 step 4b — the KF-19 code screen: REPORT ONLY, never a go condition (the part-B design
Rounds 1-2 and Codex 13 Nov 2026 12:06, adopted by Shina (author/CSO) 13 Nov 2026 ~12:08).

KF-19 is the four r1 audience findings: eight named terms left unexplained in A5, S10, S13 and S3.
It is DECIDED BY THE BLIND READ. This tool only records, for each of those terms in each run of
those scenarios, what the renderer did with it — so that the audience result has a reason on file:

    python -B evals/r1_kf19_report.py --batch <the rendered batch> --sheets <the sealed sheets>
                                      --out <a folder outside the batch>
                                      [--verdict <GO-STOP.json>] [--freeze <FREEZE.json>] [--inputs <inputs.json>]

Exit 0 = reported · 2 = refused (no report file is left). THERE IS NO EXIT 1: this tool cannot fail a run.

Four gates, in this order; the first that fails ends the run and nothing after it starts:
  1. the evaluator's own `integrity()` — the freeze record against the files on disk, the batch made by
     the frozen harness from the pinned inputs, every record's provenance and input hash;
  2. the evaluator's own `seal_state()` must be exactly "valid" — the blind read is sealed for THIS batch;
  3. the evaluator's saved report (GO-STOP.json) is readable, is this batch's and this freeze's, was
     written after the seal, and its verdict follows from its own eight conditions. GO is not required:
     a STOP and a post-seal INCOMPLETE are reported on just the same;
  4. only then: the ledger analysis.

What gate 1 does NOT prove: that every saved ledger is complete or well-formed (that is the
evaluator's G5, which may be NOT MET on a batch this tool still reports on). So `unaccounted` is an
ordinary, reachable result here — reported, never guessed around.
What gate 3 cannot prove: that the file is the evaluator's LATEST output. The sha256 of the verdict
file that was accepted is recorded in this tool's output.

Per (generation, term), decided in this order:
  no_input            the generation has no step-7 input (the upstream step failed);
  absent_from_input   the input has no occurrence of the term — decided from the input alone,
                      whether the generation was rendered or refused;
  not_rendered        the term is there, the generation was refused, and the record carries the
                      harness's own refusal code (recorded, with the number of occurrences);
  unaccounted         the term is there but the saved output cannot be laid against the input;
  then each occurrence gets one class from the ledger operations that touch its letters —
    glossed (ONE `sub` from the `glossary` table covers the whole occurrence) · substituted (one
    `sub` from another table — never counted as glossed) · copied · dropped (one `drop` covers it:
    a part of a line or the whole line) · partial (anything else) · unaccounted (no usable ledger
    entry for its unit) —
  and the row is that class when every occurrence has it, else `mixed` with the count of each class.

The terms are the constant TERMS. THE COMMAND LINE CANNOT CHANGE THEM. `report()` and `main()` take
a term table only as a Python argument (tests pass invented ones). Other spellings are not counted.

What it reads: the saved batch of the frozen run (each record's step-7 input and its saved ledger,
through the evaluator's `ledger_view()`), the freeze record, the sealed sheets (through
`seal_state()`), the pinned inputs file and the saved verdict. It renders nothing and calls no model.
What it writes: KF19-REPORT.json and KF19-REPORT.md in --out, which must be outside the batch
folder and outside the sheets folder (the two evaluator inputs it is given; it is not told where
run 2 and the rulings store are — keeping --out clear of those is the run script's job). It only
ever CREATES those two files: if either name is already there — a file, or a link to anything — it
refuses, and it never writes through a link (Codex 13 Nov 2026 13:38). The pair is published whole
or not at all. The evaluator never imports this file and never reads those two.
What it prints and writes: the eight terms, ids, codes and counts — no other wording. Nothing is
copied out of the part of a record that gate 1 does not pin (Codex 13 Nov 2026 13:38): an entry id
is emitted only if the approved tables hold it for that table or rule, a rule only if it is one of
the renderer's drop rules, a refusal code only if it is one of the renderer's refusal codes — a
saved value outside those is never printed; the occurrence or row is `unaccounted`. The run number
is emitted only as an integer and the generation id is rebuilt from it.
What it cannot show: whether an explanation is a good one, or whether a reader would understand a
copied term. That is the blind read's job.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import r1_freeze as FZ  # noqa: E402
import r1_go_stop as GS  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402
from pipeline.errors import StepError  # noqa: E402

TOOL_VERSION = 1
HEADER = "REPORT ONLY — not a go condition; KF-19 is decided by the blind read"
REPORT_JSON, REPORT_MD = "KF19-REPORT.json", "KF19-REPORT.md"
VERDICT_FILE = "GO-STOP.json"
EXIT_REPORTED, EXIT_REFUSED = 0, 2                                  # there is no exit 1

# The eight adopted terms, by exact pattern (part-B design Round 2). Not read from the case list,
# not chosen after the run. (scenario, ((the term as named, its pattern), ...))
TERMS = (
    ("A5", (("COPD", r"\bCOPD\b"), ("AF", r"\bAF\b"))),
    ("S10", (("bimanual compression", r"(?i)\bbimanual compression\b"), ("observations", r"(?i)\bobservations\b"),
             ("handover", r"(?i)\bhandover\b"))),
    ("S13", (("septic shock", r"(?i)\bseptic shock\b"), ("ICU", r"\bICU\b"))),
    ("S3", (("psychotic features", r"(?i)\bpsychotic features\b"),)),
)

CLASSES = ("glossed", "substituted", "copied", "dropped", "partial", "unaccounted")     # of one occurrence
STATUSES = ("no_input", "absent_from_input", "not_rendered", "unaccounted", "glossed", "substituted", "copied",
            "dropped", "partial", "mixed")                                               # of one row
GLOSS_TABLE = "glossary"                              # the only substitution that counts as an explanation
OPS = ("copy", "sub", "drop")
# The evaluator's eight conditions, by name (it has no constant for them; a test pins these to its output).
CONDITIONS = (
    "G1 every generation rendered",
    "G2 code-decided bar lines",
    "G3 every flag ruled in the fresh store",
    "G4 drop ledger reviewed, no required item dropped",
    "G5 audit() clean on every record",
    "G6 cases at their expected outcome, known failures absent",
    "G7 blind read 8/8",
    "G8 determinism (run 2 byte-identical except run.json)",
)
CONDITION_STATUSES = (GS.MET, GS.NOT_MET, GS.UNDECIDED)
CANNOT_SHOW = ("whether an explanation is a good one, or whether a reader would understand a copied term — "
               "that is the blind read's job")


# =================================================================================================
# gates 1-3 — nothing of the output is analysed before all three hold
# =================================================================================================

def gate_frozen_run(c: GS.Candidate, freeze: Path, inputs: Path) -> str:
    """Gate 1: the evaluator's own integrity check, unchanged. Returns the freeze sha256; raises GS.Refused."""
    freeze_sha, _frozen = GS.integrity(c, freeze, inputs)
    return freeze_sha


def gate_seal(sheets: Path, c: GS.Candidate, freeze_sha: str) -> None:
    """Gate 2: the blind read is sealed for this batch. Raises GS.Refused (the evaluator's codes, or ours)."""
    state, _doc = GS.seal_state(sheets, c, freeze_sha)
    if state != "valid":
        raise GS.Refused(f"seal:{state}")                            # seal:absent | seal:impossible


def verdict_problem(doc, c: GS.Candidate, freeze_sha: str) -> str | None:
    """The first reason a saved evaluator report cannot be relied on, or None."""
    if not isinstance(doc, dict):
        return "verdict:not_readable"
    if doc.get("batch_id") != c.batch_id or doc.get("freeze_sha256") != freeze_sha:
        return "verdict:not_this_batch"
    conditions = doc.get("conditions")
    if conditions == {} or conditions is None:                       # what the evaluator writes before the seal
        return "verdict:pre_seal"
    if doc.get("blind_read") != "sealed":
        return "verdict:not_consistent"
    if isinstance(doc.get("evaluator_version"), bool) or doc.get("evaluator_version") != GS.EVALUATOR_VERSION:
        return "verdict:not_consistent"
    if not isinstance(conditions, dict) or tuple(conditions) != CONDITIONS:
        return "verdict:not_consistent"
    for v in conditions.values():
        if not isinstance(v, dict) or not isinstance(v.get("status"), str) or v["status"] not in CONDITION_STATUSES:
            return "verdict:not_consistent"
    if doc.get("verdict") != GS.verdict_of(conditions):
        return "verdict:not_consistent"
    return None


def gate_verdict(path: Path, c: GS.Candidate, freeze_sha: str) -> tuple[dict, str]:
    """Gate 3: (the saved evaluator report, the sha256 of its file). Raises GS.Refused."""
    try:
        raw = path.read_bytes()
        doc = json.loads(raw)
    except (OSError, ValueError):
        raise GS.Refused("verdict:not_readable") from None
    problem = verdict_problem(doc, c, freeze_sha)
    if problem is not None:
        raise GS.Refused(problem)
    return doc, GS.sha_bytes(raw)


# =================================================================================================
# gate 4 — the ledger analysis
# =================================================================================================

def occurrences(pattern: str, text: str) -> list[tuple[int, int]]:
    """Each match of the pattern, left to right, not overlapping: (start, end)."""
    return [m.span() for m in re.finditer(pattern, text)]


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _known_drop(o: dict, tables: RP.Tables) -> bool:
    """A drop names one of the renderer's rules and, for a table rule, an entry that table holds."""
    rule, entry = o.get("rule"), o.get("entry")
    if not isinstance(rule, str) or rule not in RP.DROP_RULES:
        return False
    if rule == "resus_conflict":                                     # the one rule with no table entry
        return entry is None
    return isinstance(entry, str) and entry in {e.id for e in RP._table_entries(tables, rule)}


def ops_readable(text: str, ops, tables: RP.Tables) -> bool:
    """Can these saved operations be laid against this unit, and is everything they name a known id?
    (Shape and vocabulary only — not the evaluator's audit.) What fails here is never printed."""
    if not isinstance(ops, list):
        return False
    for o in ops:
        if not isinstance(o, dict) or not isinstance(o.get("op"), str) or o["op"] not in OPS:
            return False
        if not _int(o.get("a")) or not _int(o.get("b")) or not 0 <= o["a"] <= o["b"] <= len(text):
            return False
        if o["op"] == "sub" and not (isinstance(o.get("table"), str) and o["table"] in RP.SUB_TABLES
                                     and isinstance(o.get("entry"), str)
                                     and tables.entry(o["table"], o["entry"]) is not None):
            return False
        if o["op"] == "drop" and not _known_drop(o, tables):
            return False
    return True


def _op_ids(o: dict) -> dict:
    if o["op"] == "sub":
        return {"op": "sub", "table": o["table"], "entry": o["entry"]}
    if o["op"] == "drop":
        return {"op": "drop", "rule": o["rule"], "entry": o["entry"]}
    return {"op": "copy"}


def classify(text: str, a: int, b: int, x, tables: RP.Tables) -> dict:
    """One occurrence text[a:b] of a unit, against the unit's ledger entry `x` -> {"class": ..., ids}."""
    if not isinstance(x, dict) or not ops_readable(text, x.get("ops"), tables):
        return {"class": "unaccounted"}
    touching = [o for o in x["ops"] if o["a"] < o["b"] and o["a"] < b and o["b"] > a]
    if len(touching) == 1 and touching[0]["a"] <= a and touching[0]["b"] >= b:
        o = touching[0]
        if o["op"] == "copy":
            return {"class": "copied"}
        if o["op"] == "drop":
            return {"class": "dropped", "rule": o["rule"], "entry": o["entry"]}
        if o["table"] == GLOSS_TABLE:
            return {"class": "glossed", "entry": o["entry"]}
        return {"class": "substituted", "table": o["table"], "entry": o["entry"]}
    return {"class": "partial", "ops": [_op_ids(o) for o in touching]}


def combine(detail: list[dict]) -> dict:
    """The row for one (generation, term) from its occurrences' classes — the fixed rule."""
    classes = [d["class"] for d in detail]
    if len(set(classes)) != 1:
        return {"status": "mixed", "classes": {k: classes.count(k) for k in CLASSES}}
    row = {"status": classes[0]}
    if classes[0] == "glossed":
        row["entries"] = sorted({d["entry"] for d in detail})
    elif classes[0] == "substituted":
        row["entries"] = [list(p) for p in sorted({(d["table"], d["entry"]) for d in detail})]
    elif classes[0] == "dropped":
        row["entries"] = [[r, e or None] for r, e in sorted({(d["rule"], d["entry"] or "") for d in detail})]
    return row


def _input(rec: dict):
    try:
        return GS.step7_input(rec)
    except StepError:                                               # an upstream output step 7 cannot take
        return None


def _ledger_rows(rec: dict, tables: RP.Tables):
    try:
        return GS.ledger_view(rec, tables)
    except (KeyError, TypeError, AttributeError, IndexError, ValueError):
        return None                                                  # a saved output with no usable ledger


def term_row(rec: dict, pattern: str, tables: RP.Tables) -> dict:
    """What happened to one term in one generation. The input is looked at first, on its own."""
    inp = _input(rec)
    if inp is None:
        return {"status": "no_input"}
    n = sum(len(occurrences(pattern, sec["text"])) for sec in inp["sections"])
    if n == 0:
        return {"status": "absent_from_input"}
    if (rec.get("steps") or {}).get("compose_patient") is None:
        code = (rec.get("render_refusal") or {}).get("code") if isinstance(rec.get("render_refusal"), dict) else None
        if isinstance(code, str) and code in RP.REFUSALS:           # one of the renderer's own codes, or nothing
            return {"status": "not_rendered", "occurrences": n, "refusal": code}
        return {"status": "unaccounted", "occurrences": n, "why": "not_rendered_and_no_known_refusal_code"}
    rows = _ledger_rows(rec, tables)
    if rows is None:
        return {"status": "unaccounted", "occurrences": n, "why": "ledger_cannot_be_laid_against_the_input"}
    detail = []
    for r in rows:
        u = r["u"]
        for a, b in occurrences(pattern, u["text"]):
            detail.append({"heading": r["heading"], "source_id": u["source_id"], "line": u["line"],
                           "sentence": u["sentence"]} | classify(u["text"], a, b, r["x"], tables))
    if len(detail) != n:
        return {"status": "unaccounted", "occurrences": n, "why": "units_do_not_match_the_input"}
    return combine(detail) | {"occurrences": n, "detail": detail}


def analyse(c: GS.Candidate, terms, tables: RP.Tables) -> tuple[list[dict], list[dict]]:
    """(one row per generation of a named scenario and term, one summary line per term)."""
    rows, summary = [], []
    for scenario, named in terms:
        gens = [(rel, rec) for rel, rec in zip(c.rels, c.records) if rec.get("scenario_id") == scenario]
        for term, pattern in named:
            mine = []
            for rel, rec in gens:
                run = rec.get("run") if _int(rec.get("run")) else None           # never a saved string
                gid = f"{c.batch_id}/r{run}/{scenario}" if run is not None else None
                mine.append({"record": rel, "generation_id": gid, "scenario_id": scenario, "run": run,
                             "term": term} | term_row(rec, pattern, tables))
            rows += mine
            summary.append({"scenario_id": scenario, "term": term, "runs": len(mine),
                            "by_status": {s: sum(r["status"] == s for r in mine) for s in STATUSES}})
    return rows, summary


# =================================================================================================
# the report
# =================================================================================================

def _inside(path: Path, folder: Path) -> bool:
    p, f = path.resolve(), folder.resolve()
    return p == f or f in p.parents


def destinations(out: Path) -> tuple[Path, Path]:
    return out / REPORT_JSON, out / REPORT_MD


def out_problem(out: Path, batch: Path, sheets: Path) -> str | None:
    """Why this output folder cannot be used, or None. Looked at before any gate."""
    for folder, code in ((batch, "out:inside_the_batch"), (sheets, "out:inside_the_sheets")):
        if _inside(out, folder):                                     # neither folder is ever written to
            return code
    if any(os.path.lexists(p) for p in destinations(out)):           # a file, or a link to anything
        return "out:report_exists"
    return None


def publish(out: Path, body: str, text: str) -> str | None:
    """Create the two report files — never replace one, never write through a link. None, or why not.
    On any failure the files this call created are removed again: the pair is whole or absent."""
    made, made_dir = [], False
    try:
        if not out.exists():
            out.mkdir(parents=True)
            made_dir = True
        for path, content in zip(destinations(out), (body, text)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            made.append(path)                                        # O_EXCL: the name was not there, link or file
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        return None
    except OSError:
        for path in made:
            try:
                path.unlink()
            except OSError:
                pass
        if made_dir:
            try:
                out.rmdir()
            except OSError:
                pass
        return "out:partial_report_left" if any(os.path.lexists(p) for p in made) else "out:not_writable"


def report(batch: Path, sheets: Path, verdict: Path | None = None, *, freeze: Path = GS.DEFAULT_FREEZE,
           inputs: Path | None = None, terms=TERMS) -> dict:
    """The whole report. Raises GS.Refused; nothing of the output is analysed unless gates 1-3 all hold."""
    c = GS.Candidate(batch)
    inputs = inputs if inputs is not None else ROOT / FZ.MEASUREMENT_INPUTS
    freeze_sha = gate_frozen_run(c, freeze, inputs)                                      # gate 1
    gate_seal(sheets, c, freeze_sha)                                                     # gate 2
    saved, saved_sha = gate_verdict(verdict if verdict is not None else c.dir / GS.OUT_DIR / VERDICT_FILE,
                                    c, freeze_sha)                                       # gate 3
    rows, summary = analyse(c, terms, RP.default_tables())                               # gate 4
    return {"report": "KF-19 code screen", "header": HEADER, "deciding": False, "tool_version": TOOL_VERSION,
            "batch_id": c.batch_id, "source": c.source_label(), "freeze_sha256": freeze_sha, "blind_read": "sealed",
            "verdict_file": {"sha256": saved_sha, "verdict": saved["verdict"]},
            "terms": [{"scenario_id": s, "term": t, "pattern": p} for s, named in terms for t, p in named],
            "summary": summary, "rows": rows, "cannot_show": CANNOT_SHOW}


def _counts(by_status: dict) -> str:
    return ", ".join(f"{s} {n}" for s, n in by_status.items() if n) or "no generations"


def _row_note(r: dict) -> str:
    bits = []
    if "occurrences" in r:
        bits.append(f"occurrences {r['occurrences']}")
    if "refusal" in r:
        bits.append(f"refusal {r['refusal']}")
    if "why" in r:
        bits.append(r["why"])
    if "entries" in r:
        bits.append("entries " + ", ".join(e if isinstance(e, str) else "/".join(str(x) for x in e) for e in r["entries"]))
    if "classes" in r:
        bits.append(", ".join(f"{k} {n}" for k, n in r["classes"].items() if n))
    return "; ".join(bits)


def render_md(rep: dict) -> str:
    out = [f"# KF-19 code screen — {rep['batch_id']}", "", f"**{rep['header']}**", "",
           f"- batch: {rep['batch_id']} · {rep['source']}",
           f"- freeze sha256: {rep['freeze_sha256']}",
           f"- blind read: {rep['blind_read']} · the evaluator's saved verdict: {rep['verdict_file']['verdict']} "
           f"(file sha256 {rep['verdict_file']['sha256']})",
           f"- deciding: no · tool version {rep['tool_version']}",
           f"- cannot show: {rep['cannot_show']}", "",
           "## Per term, over the runs", "", "| scenario | term | runs | by status |", "|---|---|---|---|"]
    out += [f"| {s['scenario_id']} | {s['term']} | {s['runs']} | {_counts(s['by_status'])} |" for s in rep["summary"]]
    out += ["", "## Per generation and term", "", "| record | term | status | ids and counts |", "|---|---|---|---|"]
    out += [f"| {r['record']} | {r['term']} | {r['status']} | {_row_note(r)} |" for r in rep["rows"]]
    return "\n".join(out) + "\n"


def print_report(rep: dict, file=None) -> None:
    file = file if file is not None else sys.stdout
    print(rep["header"], file=file)
    print(f"batch {rep['batch_id']} · freeze {rep['freeze_sha256']} · blind read {rep['blind_read']}", file=file)
    print(f"the evaluator's saved verdict: {rep['verdict_file']['verdict']} "
          f"(file sha256 {rep['verdict_file']['sha256']})", file=file)
    for s in rep["summary"]:
        print(f"  {s['scenario_id']:<4} {s['term']:<22} runs {s['runs']}: {_counts(s['by_status'])}", file=file)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--batch", type=Path, required=True)
    ap.add_argument("--sheets", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--verdict", type=Path, default=None)
    ap.add_argument("--freeze", type=Path, default=GS.DEFAULT_FREEZE)
    ap.add_argument("--inputs", type=Path, default=None)
    return ap


def main(argv=None, *, _terms=TERMS) -> int:
    a = parser().parse_args(argv)
    try:
        problem = out_problem(a.out, a.batch, a.sheets)
        if problem is not None:
            raise GS.Refused(problem)
        rep = report(a.batch, a.sheets, a.verdict, freeze=a.freeze, inputs=a.inputs, terms=_terms)
        body, text = GS.dump(rep), render_md(rep)
    except GS.Refused as exc:
        print("REFUSED — nothing reported, nothing written: " + "; ".join(exc.problems))
        return EXIT_REFUSED
    except Exception as exc:                                         # never a traceback, never a half report
        print(f"REFUSED — nothing reported, nothing written: report:internal_error:{type(exc).__name__}")
        return EXIT_REFUSED
    problem = publish(a.out, body, text)
    if problem is not None:
        left = "a partial report is left" if problem == "out:partial_report_left" else "no report file is left"
        print(f"REFUSED — the report could not be written; {left}: {problem}")
        return EXIT_REFUSED
    print_report(rep)
    print(f"report -> {a.out / REPORT_JSON} (sha256 {GS.sha_bytes(body.encode('utf-8'))})")
    return EXIT_REPORTED


if __name__ == "__main__":
    sys.exit(main())
