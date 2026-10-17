#!/usr/bin/env python3
"""Rubric v2 calibration — the author's blind scores against the instrument (W5).

    python evals/compare_calibration.py evals/calibration/w4-baseline-v1-x5-r1 \\
        evals/runs/w4-baseline-v1-x5/judge/calib-r1-x3b

For every item x dimension (D2, D3, D6, D7, D8) it compares the author's blind
verdict with (a) the judge alone and (b) the COMBINED instrument — the worse of
the deterministic checks (evals/rubric_checks.py) and the judge, which is the
score that counts (EVAL_RESULTS.md section 2). Each judge repeat is scored as
its own run of the instrument; a threshold is met only if it holds on EVERY
repeat.

Thresholds (set before any judge output existed; section 2, Calibration):
  (1) the combined instrument misses NONE of the author's Fails or auto-fails;
  (2) pooled quadratic-weighted kappa >= 0.6 across the five dimensions
      (pairs where either side is N/A are left out of kappa and counted apart;
      raw agreement, N/A included, is reported with a Wilson 95% interval);
  (3) the judge disagrees with itself on <= 10% of item x dimension verdicts
      across the repeats.

Writes <calibration folder>/results/<judge_id>/ CALIBRATION.md, calibration.json
and disagreements.md (the review list; it quotes judge findings). The console
prints counts and the three numbers only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_blind_sheets as mbs  # noqa: E402
import rubric_checks as rc  # noqa: E402
import run_cold_eval as rce  # noqa: E402
from score_batch import wilson  # noqa: E402

DIMENSIONS = mbs.DIMENSIONS
ORDINAL = {"pass": 0, "partial": 1, "fail": 2}
KAPPA_MIN = 0.6
SELF_DISAGREE_MAX = 0.10


class CalibrationError(RuntimeError):
    pass


def worse(a: str | None, b: str | None) -> str | None:
    """Worse of two verdicts; a deterministic Fail overrides anything, N/A included."""
    if a is None:
        return b
    if b is None:
        return a
    if "fail" in (a, b):
        return "fail"
    if "na" in (a, b):
        return b if a == "na" else a
    return a if ORDINAL[a] >= ORDINAL[b] else b


def weighted_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Quadratic-weighted Cohen's kappa on pass/partial/fail pairs."""
    if not pairs:
        return None
    k = 3
    obs = [[0] * k for _ in range(k)]
    for a, b in pairs:
        obs[ORDINAL[a]][ORDINAL[b]] += 1
    n = len(pairs)
    row = [sum(obs[i]) for i in range(k)]
    col = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    w = [[(i - j) ** 2 / (k - 1) ** 2 for j in range(k)] for i in range(k)]
    num = sum(w[i][j] * obs[i][j] for i in range(k) for j in range(k))
    den = sum(w[i][j] * row[i] * col[j] / n for i in range(k) for j in range(k))
    if den == 0:
        return 1.0 if num == 0 else None
    return 1 - num / den


def load(cal_dir: Path, judge_dir: Path, batch_dir: Path | None = None,
         corpus_path: Path = rce.DEFAULT_CORPUS_PATH):
    m = json.loads((cal_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    complete, problems = mbs.check(cal_dir, corpus_path)
    if problems:
        raise CalibrationError(f"author scores incomplete: {complete}/{len(m['items'])}")
    jm = json.loads((judge_dir / "manifest.json").read_text(encoding="utf-8"))
    if jm["batch_id"] != m["batch_id"]:
        raise CalibrationError("judge run and calibration folder are for different batches")
    batch_dir = batch_dir or judge_dir.parent.parent
    corpus, _ = rce.load_corpus(corpus_path)
    notes = {s.id: s.notes for s in corpus}
    reps = sorted(p.name for p in judge_dir.glob("rep*") if p.is_dir())
    items = []
    for it in m["items"]:
        sid, run = it["scenario_id"], m["run"]
        author = json.loads((cal_dir / it["scores"]).read_text(encoding="utf-8"))
        rec = json.loads((batch_dir / f"r{run}" / f"{sid}.json").read_text(encoding="utf-8"))
        if rce.sha256_text(rec["output"]) != it["output_sha256"]:
            raise CalibrationError(f"{sid}: output changed since the blind sheets were made")
        judged = []
        for rep in reps:
            j = json.loads((judge_dir / rep / f"r{run}" / f"{sid}.json").read_text(encoding="utf-8"))
            if j["output_sha256"] != it["output_sha256"]:
                raise CalibrationError(f"{sid} {rep}: judge saw a different output")
            judged.append(j)
        checks = rc.run_checks(notes[sid], rec["output"])
        items.append({"sid": sid, "author": author, "judged": judged, "checks": checks})
    return m, jm, reps, items


def analyse(items, reps) -> dict:
    per_rep = []
    for k in range(len(reps)):
        rows = []
        for it in items:
            for d in DIMENSIONS:
                a = it["author"]["dimensions"][d]
                j = it["judged"][k]
                jv = None if j["error"] else j["verified"]["dimensions"][d]["verdict"]
                chk = it["checks"].get(d, {}).get("verdict") if d in ("D2", "D7") else None
                rows.append({"sid": it["sid"], "dim": d, "author": a["verdict"],
                             "author_af": bool(a.get("auto_fail")), "judge": jv,
                             "check": chk, "combined": worse(chk, jv)})
        per_rep.append(rows)

    def summarise(rows, key):
        missed = [r for r in rows if r["author"] == "fail" and r[key] != "fail"]
        pairs = [(r["author"], r[key]) for r in rows
                 if r["author"] in ORDINAL and r[key] in ORDINAL]
        na_mismatch = sum(1 for r in rows if (r["author"] == "na") != (r[key] == "na"))
        agree = sum(1 for r in rows if r["author"] == r[key])
        lo, hi = wilson(agree, len(rows))
        return {"missed_fails": [(r["sid"], r["dim"]) for r in missed],
                "kappa_w": weighted_kappa(pairs), "kappa_n": len(pairs),
                "na_mismatches": na_mismatch, "raw_agreement": agree, "n": len(rows),
                "raw_ci95": [lo, hi],
                "by_dim": {d: sum(1 for r in rows if r["dim"] == d and r["author"] == r[key])
                           for d in DIMENSIONS}}

    combined = [summarise(rows, "combined") for rows in per_rep]
    judge_alone = [summarise(rows, "judge") for rows in per_rep]

    cells = len(items) * len(DIMENSIONS)
    unstable = []
    for i, it in enumerate(items):
        for d in DIMENSIONS:
            vs = {per_rep[k][i * len(DIMENSIONS) + DIMENSIONS.index(d)]["judge"] for k in range(len(reps))}
            if len(vs) > 1:
                unstable.append((it["sid"], d))
    self_rate = len(unstable) / cells if cells else 0.0

    t1 = all(not c["missed_fails"] for c in combined)
    t2 = all(c["kappa_w"] is not None and c["kappa_w"] >= KAPPA_MIN for c in combined)
    t3 = self_rate <= SELF_DISAGREE_MAX if len(reps) > 1 else None
    return {"per_rep_rows": per_rep, "combined": combined, "judge_alone": judge_alone,
            "self_disagreement": {"cells": cells, "unstable": unstable, "rate": self_rate},
            "thresholds": {"t1_no_missed_fails": t1, "t2_kappa": t2, "t3_self": t3,
                           "met": bool(t1 and t2 and (t3 is not False))}}


def _fmt(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def write_reports(out_dir: Path, m, jm, reps, items, res) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    t = res["thresholds"]
    lines = [f"# Calibration — {m['batch_id']} r{m['run']} vs judge {jm['judge_id']}", "",
             f"- Judge: `{jm['model_id']}`, temperature {jm['temperature']}, tool_strict "
             f"{jm.get('tool_strict')}; instructions `{jm['prompt']['instructions_sha256'][:12]}…`, "
             f"rubric `{jm['prompt']['rubric_sha256'][:12]}…`; repeats {len(reps)}.",
             f"- Deterministic checks: version {rc.CHECKS_VERSION}.",
             f"- Items: {len(items)} × {len(DIMENSIONS)} dimensions; author blind scores "
             f"(rubric stamp `{m['rubric']['sha256'][:12]}…`).", "",
             "## Thresholds (each must hold on every repeat)", "",
             "| | Threshold | Result |", "|---|---|---|",
             f"| 1 | Combined instrument misses no author Fail | **{'MET' if t['t1_no_missed_fails'] else 'NOT MET'}** |",
             f"| 2 | Pooled quadratic-weighted κ ≥ {KAPPA_MIN} | **{'MET' if t['t2_kappa'] else 'NOT MET'}** |",
             f"| 3 | Judge self-disagreement ≤ {int(SELF_DISAGREE_MAX*100)}% | **"
             f"{'—' if t['t3_self'] is None else ('MET' if t['t3_self'] else 'NOT MET')}** |",
             "", f"**Overall: {'MET — judge scores may count' if t['met'] else 'NOT MET — judge scores do not count yet'}**",
             "", "## Per repeat", "",
             "| Repeat | Missed Fails (combined) | κw combined (n) | κw judge alone | Raw agreement combined (95% CI) | N/A mismatches |",
             "|---|---|---|---|---|---|"]
    for k, rep in enumerate(reps):
        c, j = res["combined"][k], res["judge_alone"][k]
        lines.append(f"| {rep} | {len(c['missed_fails'])} | {_fmt(c['kappa_w'])} ({c['kappa_n']}) | "
                     f"{_fmt(j['kappa_w'])} | {c['raw_agreement']}/{c['n']} "
                     f"({c['raw_ci95'][0]*100:.0f}–{c['raw_ci95'][1]*100:.0f}%) | {c['na_mismatches']} |")
    sd = res["self_disagreement"]
    lines += ["", f"Self-disagreement: {len(sd['unstable'])}/{sd['cells']} item × dimension cells "
              f"({sd['rate']*100:.1f}%) had different judge verdicts across repeats.", "",
              "## Agreement by dimension (combined, rep1)", "",
              "| " + " | ".join(DIMENSIONS) + " |", "|" + "---|" * len(DIMENSIONS),
              "| " + " | ".join(f"{res['combined'][0]['by_dim'][d]}/{len(items)}" for d in DIMENSIONS) + " |",
              "", "Missed Fails and every disagreement are listed in `disagreements.md`."]
    (out_dir / "CALIBRATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    rev = [f"# Disagreements — {jm['judge_id']}", "",
           "Author verdict vs combined instrument, per repeat. `check` is the deterministic "
           "layer (D2/D7 only); judge findings are quoted as the judge wrote them (verified only).", ""]
    for k, rep in enumerate(reps):
        rev.append(f"## {rep}")
        for r in res["per_rep_rows"][k]:
            if r["author"] != r["combined"]:
                it = next(i for i in items if i["sid"] == r["sid"])
                j = it["judged"][k]
                fs = [] if j["error"] else j["verified"]["dimensions"][r["dim"]]["findings"]
                tag = " **MISSED FAIL**" if r["author"] == "fail" and r["combined"] != "fail" else ""
                rev.append(f"- {r['sid']} {r['dim']}: author `{r['author']}` · combined "
                           f"`{r['combined']}` (judge `{r['judge']}`, check `{r['check']}`){tag}")
                for f in fs:
                    rev.append(f"  - judge: \"{f['output_quote'][:160]}\" — {f['reason']}")
        rev.append("")
    (out_dir / "disagreements.md").write_text("\n".join(rev), encoding="utf-8")

    slim = {k: v for k, v in res.items() if k != "per_rep_rows"}
    slim["rows"] = res["per_rep_rows"]
    (out_dir / "calibration.json").write_text(json.dumps({
        "batch_id": m["batch_id"], "run": m["run"], "judge_id": jm["judge_id"],
        "judge_prompt": jm["prompt"], "author_rubric_sha256": m["rubric"]["sha256"],
        "checks_version": rc.CHECKS_VERSION,
        "repeats": reps, **slim}, indent=2, default=list) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rubric v2 calibration comparison (W5).")
    ap.add_argument("calibration_dir", type=Path)
    ap.add_argument("judge_dir", type=Path)
    a = ap.parse_args(argv)
    try:
        m, jm, reps, items = load(a.calibration_dir, a.judge_dir)
    except CalibrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    res = analyse(items, reps)
    out = a.calibration_dir / "results" / jm["judge_id"]
    write_reports(out, m, jm, reps, items, res)
    t = res["thresholds"]
    for k, rep in enumerate(reps):
        c = res["combined"][k]
        print(f"{rep}: missed fails {len(c['missed_fails'])}, kappa_w {_fmt(c['kappa_w'])}, "
              f"raw {c['raw_agreement']}/{c['n']}")
    print(f"self-disagreement {res['self_disagreement']['rate']*100:.1f}%")
    print(f"T1 {t['t1_no_missed_fails']} · T2 {t['t2_kappa']} · T3 {t['t3_self']} -> "
          f"{'MET' if t['met'] else 'NOT MET'}  ({rce._rel(out)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
