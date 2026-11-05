"""Offline replay of steps 8a / 8b on a saved pipeline batch (WS1a step build piece 4,
5 Nov 2026) — and the A5 PREVIEW of step 6 bar line 4 (v4).

    python evals/score_assemble.py <batch dir> [--ab-only] [--out DIR] [--save-docs DIR]

No model, no network. For every record: the notes come from the corpus (checked
against the record's notes_sha256 and step 1's flags), 8a and then 8b run through
run_step exactly as the orchestrator will call them, and each record is ACCOUNTED FOR
(F2, CSO 5 Nov 2026): a validated A/B/C bundle, or a fixed StepError code and no
document. `--ab-only` runs 8a alone (a batch made before step 7 existed).

Writes ASSEMBLE.md and assemble.json to --out (default <batch>/scores/assemble/):
counts, fixed codes, headings, warning kinds, fact_ids — never note or output text.
--save-docs writes the assembled documents for a human read; never commit that folder.

A5 PREVIEW — not in force for c0.2; effective from the first measured c0.3 batch.
It prints (a) block coverage — each contradiction step 2 recorded is in assembled A's
code block exactly once (identity map: several conflicts may render the same fixed
resus sentence) — and (b) the step-6 prose screen (a contradiction ID cited in A or B
prose, or the "Notes conflict" wording) as separate numbers, with NO composite
verdict. The screen cannot see a paraphrase or a side chosen: c0.3's first measured
batch adjudicates that. Under c0.2 (L2) step 6 is told to write contradictions in
prose, so (b) flags by design.

This module is new and imported by nothing else: score_compose.py and every frozen
scorer are untouched (their re-score must be byte-identical).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "generate"))
import run_cold_eval as rce  # noqa: E402
from pipeline import StepError, run_step  # noqa: E402
from pipeline import assemble as AS  # noqa: E402
from pipeline import meaning as MN  # noqa: E402
from pipeline.compose import BOXES as BOXES_6  # noqa: E402

A5_TITLE = "A5 preview — not in force for c0.2; effective from the first measured c0.3 batch."
_NOTES_CONFLICT = re.compile(r"\bnotes conflict\b", re.IGNORECASE)


def inputs_8a(rec: dict) -> dict:
    s = rec["steps"]
    return {"facts": s["validate_facts"]["facts"], "flags": s["guard_input"]["flags"],
            "language": s["retrieve_evidence"].get("language") or [], "resus": s["route_resus"],
            "reconciliation": s["reconcile_medications"], "safety_net": s["select_safety_net"],
            "composition": s["compose_clinical"], "generated_on": rec["started_at"][:10]}


def a5_preview(rec: dict, out8a: dict) -> dict:
    facts = rec["steps"]["validate_facts"]["facts"]
    rows = AS.contradictions(facts, rec["steps"]["guard_input"]["flags"])
    block = AS.conflict_block(rows)
    a = out8a["part_a"]
    covered = 0
    if block and a.count(block) == 1:
        lines = Counter(block.split("\n")[1:])
        covered = sum(1 for r in rows if lines[r["line"]] == sum(x["line"] == r["line"] for x in rows))
    ids = {c["fact_id"] for c in facts["contradictions"]}
    flags = []
    for s in rec["steps"]["compose_clinical"]["sections"]:
        if s["heading"] in BOXES_6:
            continue
        why = (["cites_contradiction"] if set(s["fact_ids"]) & ids else []) + \
              (["notes_conflict_wording"] if _NOTES_CONFLICT.search(s["text"]) else [])
        if why:
            flags.append({"part": s["part"], "heading": s["heading"], "why": why})
    return {"recorded": len(rows), "in_block_exactly_once": covered, "prose_flags": flags}


def c_meaning_vs_step7(rec: dict, warnings: list[dict]) -> dict:
    """Located comparison, not totals (CSO 5 Nov 14:40): 8b's C meaning warnings vs step
    7's own meaning checks in the same record, as multisets of (heading, kind). Equal
    counts could hide different flagged sections; this cannot. Sentence numbers are
    8b's own (step 7's checks carry none). No warning does not mean nothing to find."""
    kinds = set(MN.CHECK_OF.values())
    ours = Counter((w["heading"], w["kind"]) for w in warnings if w["part"] == "C" and w["kind"] in kinds)
    theirs = Counter((c["heading"], c["check"]) for c in rec["steps"]["compose_patient"]["checks"]
                     if c["check"] in kinds)
    return {"match": ours == theirs, "only_8b": sum((ours - theirs).values()),
            "only_step7": sum((theirs - ours).values()), "n": sum(ours.values())}


def branches(rec: dict, out8a: dict | None, out8b: dict | None) -> list[str]:
    """Which real branch this record exercised (F1: record which branches each replay covers)."""
    s, b = rec["steps"], []
    comp = s["compose_clinical"]
    if any(c["check"] == "section_held_back" for c in comp["checks"]):
        b.append("section_held_back")
    if any(h["field"] == "gp_actions" for h in comp.get("held_back") or []):
        b.append("gp_action_held_back")
    if comp.get("held_back"):
        b.append("facts_held_back")
    b.append("med_status_" + s["reconcile_medications"]["discharge_status"])
    b.append("resus_route_" + s["route_resus"]["route"])
    if s["retrieve_evidence"].get("language"):
        b.append("language_line")
    if out8a:
        for k, n in out8a["counts"]["contradictions_by_kind"].items():
            if n:
                b.append("contradiction_" + k)
        b += ["b_resus_" + r for r in out8a["resus_in_b"]["reasons"]] or ["b_resus_not_shown"]
        if out8a["counts"]["sections_unverified"]:
            b.append("ab_unverified_marker")
        if AS.NOT_EXTRACTED in out8a["part_a"]:
            b.append("header_not_extracted")
        if AS.INFERRED_MARK in out8a["part_a"].split("\n\n")[1]:
            b.append("header_inferred")
        if AS.B_NO_CHANGES in out8a["part_b"]:
            b.append("b_no_changes")
        if "Continued unchanged:" in out8a["part_b"]:
            b.append("b_continued_count")
    if out8b and out8b["counts"]["c_sections_unverified"]:
        b.append("c_unverified_note")
    if out8b:
        b.append("gate_" + out8b["gate"]["status"])
    return b


def replay(batch: Path, ab_only: bool, save_docs: Path | None) -> dict:
    manifest = json.loads((batch / "manifest.json").read_text())
    corpus, corpus_sha = rce.load_corpus(rce.DEFAULT_CORPUS_PATH)
    if corpus_sha != manifest["corpus"]["sha256"]:
        raise SystemExit("the corpus has changed since this batch was generated")
    by_id = {sc.id: sc for sc in corpus}
    results = []
    for path in sorted(batch.glob("r*/*.json")):
        rec = json.loads(path.read_text())
        sid, run = rec["scenario_id"], rec["run"]
        out = {"scenario": sid, "run": run, "outcome": None, "code": None, "step": None}
        sc = by_id[sid]
        if rec["notes_sha256"] != sc.notes_sha256:
            raise SystemExit(f"{rec['generation_id']}: notes_sha256 does not match the corpus")
        g = run_step("guard_input", {"notes": sc.notes})
        if g["flags"] != rec["steps"]["guard_input"]["flags"]:
            raise SystemExit(f"{rec['generation_id']}: step 1 flags differ from the saved record")
        need = ["compose_clinical", "reconcile_medications", "select_safety_net"] + ([] if ab_only else ["compose_patient"])
        missing = [k for k in need if k not in rec["steps"]]
        if missing:
            out.update(outcome="upstream_missing", code="upstream_failed", step=missing[0])
            results.append(out)
            continue
        inp = inputs_8a(rec)
        o8a = o8b = None
        try:
            o8a = run_step("assemble_clinical", inp)
        except StepError as e:
            out.update(outcome="rejected", code=e.code if hasattr(e, "code") else str(e), step="08a")
        if o8a is not None and not ab_only:
            try:
                o8b = run_step("assemble_patient", inp | {"lines": g["lines"], "patient": rec["steps"]["compose_patient"],
                                                          "clinical": o8a})
                out.update(outcome="bundle")
            except StepError as e:
                out.update(outcome="rejected", code=e.code if hasattr(e, "code") else str(e), step="08b")
        elif o8a is not None:
            out.update(outcome="candidates_ab")
        if o8a is not None:
            out["a5"] = a5_preview(rec, o8a)
            out["contradictions"] = o8a["contradictions"]
            out["resus_in_b"] = o8a["resus_in_b"]
        if o8a is not None:
            out["a5"]["d1_g3_allowance_used"] = o8a["counts"]["d1_g3_allowance_used"]
        if o8b is not None:
            out["gate"] = o8b["gate"]
            out["warnings_located"] = [[w["part"], w["heading"], w["kind"], w["sentence"]] for w in o8b["warnings"]]
            out["c_meaning_vs_step7"] = c_meaning_vs_step7(rec, o8b["warnings"])
            out["warnings"] = dict(Counter(f"{w['part']}:{w['kind']}" for w in o8b["warnings"]))
            out["warnings_c"] = o8b["counts"]["warnings_c"]
            out["placeholders_left"] = sum(bool(AS._PLACEHOLDER.search(o8b[k])) for k in ("part_a", "part_b", "part_c"))
        elif o8a is not None:
            out["warnings"] = dict(Counter(f"{w['part']}:{w['kind']}" for w in o8a["warnings"]))
            out["warnings_located"] = [[w["part"], w["heading"], w["kind"], w["sentence"]] for w in o8a["warnings"]]
        out["branches"] = branches(rec, o8a, o8b)
        if save_docs and (o8b or o8a):
            d = save_docs / f"r{run}"
            d.mkdir(parents=True, exist_ok=True)
            src = o8b or o8a
            (d / f"{sid}.txt").write_text("\n\n=====\n\n".join(src[k] for k in ("part_a", "part_b", "part_c") if k in src))
        results.append(out)
    return {"batch": batch.name, "ab_only": ab_only, "results": results}


def summary(rep: dict) -> dict:
    rs = rep["results"]
    a5 = [r["a5"] for r in rs if "a5" in r]
    return {
        "records": len(rs),
        "outcomes": dict(Counter(r["outcome"] for r in rs)),
        "rejections": dict(Counter(f"{r['step']}:{r['code']}" for r in rs if r["outcome"] in ("rejected", "upstream_missing"))),
        "gate": dict(Counter(r["gate"]["status"] for r in rs if "gate" in r)),
        "placeholders_left": sum(r.get("placeholders_left", 0) for r in rs),
        "warnings": dict(sum((Counter(r.get("warnings", {})) for r in rs), Counter())),
        "branches": dict(Counter(b for r in rs for b in r["branches"])),
        "c_meaning_vs_step7": {
            "records": sum(1 for r in rs if "c_meaning_vs_step7" in r),
            "match": sum(1 for r in rs if r.get("c_meaning_vs_step7", {}).get("match")),
            "only_8b": sum(r.get("c_meaning_vs_step7", {}).get("only_8b", 0) for r in rs),
            "only_step7": sum(r.get("c_meaning_vs_step7", {}).get("only_step7", 0) for r in rs)},
        "a5": {"recorded": sum(x["recorded"] for x in a5),
               "d1_g3_allowance_used": sum(x.get("d1_g3_allowance_used", 0) for x in a5),
               "d1_g3_records": sorted(f"{r['scenario']} r{r['run']}" for r in rs
                                       if r.get("a5", {}).get("d1_g3_allowance_used")),
               "in_block_exactly_once": sum(x["in_block_exactly_once"] for x in a5),
               "prose_flags": sum(len(x["prose_flags"]) for x in a5),
               "prose_flags_by_place": dict(Counter(f"{f['part']}/{f['heading']}" for x in a5 for f in x["prose_flags"]))},
    }


def write(out: Path, rep: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    sm = summary(rep)
    (out / "assemble.json").write_text(json.dumps({"summary": sm, **rep}, indent=2, sort_keys=True) + "\n")
    lines = [f"# 8a/8b offline replay — {rep['batch']}" + (" (8a only)" if rep["ab_only"] else ""), "",
             "Accounting (F2): every record is a validated bundle (or 8a candidates with --ab-only), "
             "or a fixed code and no document.", "",
             f"| Records | {sm['records']} |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(sm["outcomes"].items())]
    lines += ["", "Rejections by step:code — " + (", ".join(f"{k} {v}" for k, v in sorted(sm["rejections"].items())) or "none"),
              "", "Gate on assembled A + C — " + (", ".join(f"{k} {v}" for k, v in sorted(sm["gate"].items())) or "not run"),
              f"Placeholders left in released bundles — {sm['placeholders_left']}", "",
              "Warnings (part:kind) — " + ", ".join(f"{k} {v}" for k, v in sorted(sm["warnings"].items())), "",
              "C meaning warnings vs step 7's own checks, located (heading, kind) per record — "
              f"{sm['c_meaning_vs_step7']['match']}/{sm['c_meaning_vs_step7']['records']} records identical; "
              f"only in 8b {sm['c_meaning_vs_step7']['only_8b']}, only in step 7 {sm['c_meaning_vs_step7']['only_step7']}. "
              "Screening checks: no warning does not establish complete detection.", "",
              "Branches exercised (records) — " + ", ".join(f"{k} {v}" for k, v in sorted(sm["branches"].items())), "",
              f"## {A5_TITLE}", "",
              f"(a) contradictions recorded by step 2 in A's code block exactly once: "
              f"{sm['a5']['in_block_exactly_once']}/{sm['a5']['recorded']}",
              f"(b) step-6 prose screen (contradiction ID cited, or \"Notes conflict\" wording): "
              f"{sm['a5']['prose_flags']} flagged — " + (", ".join(f"{k} {v}" for k, v in sorted(sm['a5']['prose_flags_by_place'].items())) or "none"),
              f"D1 c0.2 exception (exact RESUS_CONFLICT_TEXT sentence in A treatment citing a resus "
              f"contradiction) used: {sm['a5']['d1_g3_allowance_used']} — "
              + (", ".join(sm['a5']['d1_g3_records']) or "none"),
              "", "No composite verdict: (b) is a screen, not a ruling, and c0.2 tells step 6 to write "
              "contradictions in prose (L2, disclosed).", ""]
    (out / "ASSEMBLE.md").write_text("\n".join(lines))


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("batch", type=Path)
    p.add_argument("--ab-only", action="store_true")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--save-docs", type=Path, default=None)
    a = p.parse_args(argv)
    rep = replay(a.batch, a.ab_only, a.save_docs)
    write(a.out or a.batch / "scores" / "assemble", rep)
    sm = summary(rep)
    print(json.dumps({k: sm[k] for k in ("records", "outcomes", "rejections", "gate", "placeholders_left")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
