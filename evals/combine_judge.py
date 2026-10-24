#!/usr/bin/env python3
"""Combined rubric-v2 verdicts for a whole judge run — the score that counts (W6).

    python evals/combine_judge.py evals/runs/w4-baseline-v1-x5/judge/rescore-v2

EVAL_RESULTS.md section 2: "The final verdict per dimension is the worse of the
check and the judge." compare_calibration.py applies that rule only to the
calibration items (it needs the author's blind scores); this script applies it
to every record a judge run scored, with no author scores involved.

For every judge record (rep x run x scenario) it re-runs the deterministic
checks (evals/rubric_checks.py, current version) on the output the judge saw —
the pipeline through pipeline_view, as the judge saw it — and takes the worse
of check and judge per dimension (compare_calibration.worse). Checks only ever
decide a Fail, and only on D2 and D7, so combined can differ from judge-alone
in one direction: a D2/D7 judge pass/partial/N/A becomes a Fail.

Offline: no Bedrock, no client. Guards: the judge run's corpus sha must match
the corpus; every judge record's output_sha256 must match the batch record it
scored. Writes <judge_dir>/combined.json and <judge_dir>/COMBINED.md (counts per
dimension and per scenario; every cell where the checks changed the judge's
verdict). Both carry IDs, labels and verdicts only — never note text, output
text or judge findings. The console prints counts only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_view as pv  # noqa: E402
import rubric_checks as rc  # noqa: E402
import run_cold_eval as rce  # noqa: E402
from compare_calibration import DIMENSIONS, worse  # noqa: E402

COMBINE_VERSION = 1
VERDICTS = ("pass", "partial", "fail", "na")
CHECKED = ("D2", "D7")          # the only dimensions a check can decide


class CombineError(RuntimeError):
    pass


def combine_record(judge_rec: dict, checks: dict) -> list[dict]:
    """One row per dimension: judge, check, combined (+ auto-fail on each side)."""
    rows = []
    jdims = None if judge_rec["error"] else judge_rec["verified"]["dimensions"]
    for d in DIMENSIONS:
        jv = None if jdims is None else jdims[d]["verdict"]
        jaf = False if jdims is None else bool(jdims[d].get("auto_fail"))
        c = checks.get(d, {}) if d in CHECKED else {}
        cv, caf = c.get("verdict"), bool(c.get("auto_fail"))
        rows.append({
            "sid": judge_rec["scenario_id"], "run": judge_rec["run"], "rep": judge_rec["repeat"],
            "dim": d, "judge": jv, "judge_auto_fail": jaf,
            "check": cv, "check_auto_fail": caf,
            "check_labels": sorted({f["check"] for f in c.get("findings", [])}),
            "combined": worse(cv, jv), "combined_auto_fail": jaf or caf,
            "judge_error": jdims is None,
        })
    return rows


def load_rows(judge_dir: Path, *, notes: dict[str, str] | None = None,
              batch_dir: Path | None = None, corpus_path: Path = rce.DEFAULT_CORPUS_PATH):
    jm = json.loads((judge_dir / "manifest.json").read_text(encoding="utf-8"))
    batch_dir = batch_dir or judge_dir.parent.parent
    if notes is None:
        corpus, corpus_sha = rce.load_corpus(corpus_path)
        if jm.get("corpus_sha256") and jm["corpus_sha256"] != corpus_sha:
            raise CombineError("the corpus has changed since this judge run")
        notes = {s.id: s.notes for s in corpus}
    paths = sorted(judge_dir.glob("rep*/r*/*.json"))
    if not paths:
        raise CombineError("no judge records found")
    rows, flags = [], 0
    for p in paths:
        j = json.loads(p.read_text(encoding="utf-8"))
        sid, run = j["scenario_id"], j["run"]
        rec = json.loads((batch_dir / f"r{run}" / f"{sid}.json").read_text(encoding="utf-8"))
        if rec.get("generator") == "pipeline":
            rec = pv.judgeable(rec)          # the view the judge scored
        if rce.sha256_text(rec["output"]) != j["output_sha256"]:
            raise CombineError(f"{sid} r{run} rep{j['repeat']}: judge saw a different output")
        checks = rc.run_checks(notes[sid], rec["output"])
        flags += len(checks["flags"])
        rows += combine_record(j, checks)
    return jm, rows, flags


def summarise(rows: list[dict]) -> dict:
    def counts(key):
        return {d: {v: sum(1 for r in rows if r["dim"] == d and r[key] == v) for v in VERDICTS}
                for d in DIMENSIONS}
    changed = [r for r in rows if r["combined"] != r["judge"]]
    by_scenario: dict[str, dict] = {}
    for r in rows:
        by_scenario.setdefault(r["sid"], {d: Counter() for d in DIMENSIONS})[r["dim"]][r["combined"]] += 1
    return {
        "records": len({(r["sid"], r["run"], r["rep"]) for r in rows}),
        "judge_errors": len({(r["sid"], r["run"], r["rep"]) for r in rows if r["judge_error"]}),
        "judge_alone": counts("judge"),
        "combined": counts("combined"),
        "auto_fail": {"judge": {d: sum(1 for r in rows if r["dim"] == d and r["judge_auto_fail"])
                                for d in DIMENSIONS},
                      "combined": {d: sum(1 for r in rows if r["dim"] == d and r["combined_auto_fail"])
                                   for d in DIMENSIONS}},
        "check_fails": {d: sum(1 for r in rows if r["dim"] == d and r["check"] == "fail") for d in CHECKED},
        "changed_by_checks": [{k: r[k] for k in ("sid", "run", "rep", "dim", "judge", "check",
                                                 "check_labels", "combined")} for r in changed],
        "by_scenario": {s: {d: dict(c) for d, c in v.items()} for s, v in sorted(by_scenario.items(), key=_sid_key)},
    }


def _sid_key(item):
    s = item[0]
    return (0, int(s[1:])) if s[0] == "S" and s[1:].isdigit() else (1, s)


_ABBR = {"pass": "P", "partial": "Pa", "fail": "F", "na": "NA", None: "?"}


def _cell(c: dict) -> str:
    return " ".join(f"{c[v]}{_ABBR[v]}" for v in (*VERDICTS, None) if c.get(v)) or "—"


def write_reports(judge_dir: Path, jm: dict, s: dict, flags: int) -> None:
    head = "| | Pass | Partial | Fail | N/A |\n|---|---|---|---|---|"

    def table(key):
        return [head] + [f"| {d} | " + " | ".join(str(s[key][d][v]) for v in VERDICTS) + " |"
                         for d in DIMENSIONS]

    lines = [f"# Combined verdicts — {jm['batch_id']} / judge {jm['judge_id']}", "",
             f"Worse of deterministic checks (v{rc.CHECKS_VERSION}) and judge, per dimension "
             f"(EVAL_RESULTS.md §2). Judge `{jm['model_id']}`, instructions "
             f"`{jm['prompt']['instructions_sha256'][:12]}…`, rubric `{jm['prompt']['rubric_sha256'][:12]}…`. "
             f"{s['records']} judge records, {s['judge_errors']} judge errors. "
             f"combine_judge v{COMBINE_VERSION}; offline, no model calls.", "",
             "## Combined (the score that counts)", "", *table("combined"), "",
             "## Judge alone", "", *table("judge_alone"), "",
             f"Checks decided Fail: D2 {s['check_fails']['D2']}, D7 {s['check_fails']['D7']}. "
             f"Auto-fails, combined: " + ", ".join(f"{d} {s['auto_fail']['combined'][d]}" for d in DIMENSIONS)
             + f". Laterality / possible-non-English flags (judge rules, not counted): {flags}.", "",
             f"## Where the checks changed the judge's verdict ({len(s['changed_by_checks'])})", ""]
    if s["changed_by_checks"]:
        lines += ["| Scenario | Run | Rep | Dim | Judge | Check | Combined |", "|---|---|---|---|---|---|---|"]
        lines += [f"| {c['sid']} | r{c['run']} | rep{c['rep']} | {c['dim']} | {c['judge']} | "
                  f"{c['check']} ({', '.join(c['check_labels'])}) | {c['combined']} |"
                  for c in s["changed_by_checks"]]
    else:
        lines.append("None — combined equals judge alone on every cell.")
    lines += ["", "## Combined, per scenario (all runs and repeats)", "",
              "| Scenario | " + " | ".join(DIMENSIONS) + " |", "|---|" + "---|" * len(DIMENSIONS)]
    lines += [f"| {sid} | " + " | ".join(_cell(v[d]) for d in DIMENSIONS) + " |"
              for sid, v in s["by_scenario"].items()]
    lines += ["", "P pass · Pa partial · F fail · NA not applicable · ? judge error."]
    (judge_dir / "COMBINED.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (judge_dir / "combined.json").write_text(json.dumps({
        "combine_version": COMBINE_VERSION, "checks_version": rc.CHECKS_VERSION,
        "batch_id": jm["batch_id"], "judge_id": jm["judge_id"], "model_id": jm["model_id"],
        "judge_prompt": jm["prompt"], "flags": flags, **s}, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Combined (checks + judge) verdicts for a judge run.")
    ap.add_argument("judge_dir", type=Path)
    a = ap.parse_args(argv)
    try:
        jm, rows, flags = load_rows(a.judge_dir)
    except CombineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    s = summarise(rows)
    write_reports(a.judge_dir, jm, s, flags)
    for d in DIMENSIONS:
        j, c = s["judge_alone"][d], s["combined"][d]
        print(f"{d}: fail judge {j['fail']} -> combined {c['fail']}  "
              f"(pass {j['pass']}->{c['pass']}, partial {j['partial']}->{c['partial']}, na {j['na']}->{c['na']})")
    print(f"{s['records']} records, {s['judge_errors']} judge errors, "
          f"{len(s['changed_by_checks'])} cells changed by checks -> {rce._rel(a.judge_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
