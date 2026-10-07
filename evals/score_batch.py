#!/usr/bin/env python3
"""Score a run_cold_eval.py batch against the gold — step-level metrics (W4).

    python evals/score_batch.py evals/runs/<batch_id>

Writes <batch>/scores/safety_net.json (every result), SAFETY_NET.md (the
summary) and review_safety_net.md (advice sentences the scorer could not place,
for CSO adjudication, one entry per distinct sentence). Re-runnable: scoring
never touches the generations, so a gold correction or a new adjudication is a
re-score, not a re-run.

Refuses to score when anything does not match: the batch's corpus hash, a
record's notes_sha256, or the gold's. The console shows counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gold as G  # noqa: E402
import run_cold_eval as rce  # noqa: E402
import score_meds as MD  # noqa: E402
import score_safety_net as SN  # noqa: E402

SCORER_VERSION = 2
ADJUDICATIONS_DIR = G.GOLD_DIR / "adjudications"


class ScoreError(RuntimeError):
    pass


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _pct(k: int, n: int) -> str:
    if not n:
        return "—"
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({100 * k / n:.0f}%, 95% CI {100 * lo:.0f}–{100 * hi:.0f}%)"


def load_adjudications(sid: str, adir: Path, section: str = "rulings") -> dict:
    """CSO rulings for one scenario. `rulings`: advice sentences (safety-net);
    `med_rulings`: medication entries naming no gold drug (D4)."""
    p = adir / f"{sid}.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("scenario_id") != sid:
        raise ScoreError(f"{p.name}: scenario_id does not match the file name")
    return data.get(section, {})


def view_for(record: dict) -> SN.View:
    if record["generator"] == "v1":
        return SN.view_from_v1(record["output"])
    if record["generator"] == "pipeline":
        return SN.view_from_5b(record["steps"]["select_safety_net"])
    raise ScoreError(f"{record['generation_id']}: unknown generator")


def score_batch(batch_dir: Path, gold_dir: Path = G.GOLD_DIR,
                adjudications_dir: Path = ADJUDICATIONS_DIR,
                corpus_path: Path = rce.DEFAULT_CORPUS_PATH) -> dict:
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    index = json.loads((batch_dir / "batch.json").read_text(encoding="utf-8"))
    corpus, corpus_sha = rce.load_corpus(corpus_path)
    if corpus_sha != manifest["corpus"]["sha256"]:
        raise ScoreError("the corpus has changed since this batch was generated")
    by_id = {s.id: s for s in corpus}

    golds, lines, adj, gold_meta = {}, {}, {}, {}
    for sid in {g["scenario_id"] for g in index["generations"]}:
        s = by_id[sid]
        try:
            golds[sid] = G.load_valid(sid, s.notes, s.notes_sha256, gold_dir)
        except G.GoldError as exc:
            raise ScoreError(str(exc)) from None
        lines[sid] = G.line_index(s.notes)
        adj[sid] = (load_adjudications(sid, adjudications_dir),
                    load_adjudications(sid, adjudications_dir, "med_rulings"))
        gold_meta[sid] = {"status": golds[sid]["status"],
                          "sha256": hashlib.sha256((gold_dir / f"{sid}.json").read_bytes()).hexdigest()}

    results, med_results, skipped = [], [], []
    for g in index["generations"]:
        rec = json.loads((batch_dir / g["record"]).read_text(encoding="utf-8"))
        sid = rec["scenario_id"]
        if rec["notes_sha256"] != by_id[sid].notes_sha256:
            raise ScoreError(f"{rec['generation_id']}: notes_sha256 does not match the corpus")
        if rec["error"] or rec.get("output") is None and rec["generator"] == "v1":
            skipped.append({"generation_id": rec["generation_id"],
                            "error_code": (rec["error"] or {}).get("code")})
            continue
        ids = {"generation_id": rec["generation_id"], "scenario_id": sid, "run": rec["run"]}
        r = SN.score(view_for(rec), golds[sid], lines[sid], adj[sid][0])
        r.update(ids)
        if rec["generator"] == "v1":
            # The gate as it ran at generation, and the CURRENT gate re-run on the same
            # output — so a gate fix is visible as a change, never a silent re-grade.
            now = SN.gate.check_combined(by_id[sid].notes, rec["output"], SN.gate.V1_LINES)
            r["gate"] = {"at_generation": (rec.get("gate") or {}).get("status"),
                         "at_generation_ok": (rec.get("gate") or {}).get("ok"),
                         "now": now.status, "now_ok": now.ok}
        results.append(r)
        mv = med_view_for(rec)
        if mv is not None:                   # None: the pipeline has no step 5a yet
            m = MD.score(mv, golds[sid], adj[sid][1])
            m.update(ids)
            med_results.append(m)
    return {"scorer": "safety_net", "scorer_version": SCORER_VERSION,
            "batch_id": manifest["batch_id"], "generator": manifest["generator"],
            "gold": gold_meta, "results": results, "skipped": skipped,
            "meds": {"scorer": "d4_medications", "scorer_version": SCORER_VERSION,
                     "results": med_results}}


def med_view_for(record: dict) -> MD.MedView | None:
    if record["generator"] == "v1":
        return MD.view_from_v1(record["output"])
    if record["generator"] == "pipeline":
        st = record["steps"].get("reconcile_medications")
        if st is None:
            return None                      # step 5a not built yet: D4 is not scored
        return MD.view_from_5a(st["reconciliation"], st.get("discharge_status"))
    raise ScoreError(f"{record['generation_id']}: unknown generator")


def write_reports(batch_dir: Path, scored: dict, corpus_path: Path = rce.DEFAULT_CORPUS_PATH) -> Path:
    out = batch_dir / "scores"
    out.mkdir(exist_ok=True)
    (out / "safety_net.json").write_text(json.dumps(scored, indent=2) + "\n", encoding="utf-8")
    res = scored["results"]
    n = len(res)
    drafts = sorted(sid for sid, m in scored["gold"].items() if m["status"] != "approved")

    def count(pred, rows=res):
        return sum(1 for r in rows if pred(r))

    lines = [f"# Safety-netting correctness — batch `{scored['batch_id']}`", "",
             f"Step-level metric (WS1a DoD 5), scorer v{scored['scorer_version']}, generator "
             f"`{scored['generator']}`. Definition: `evals/score_safety_net.py`; gold: `evals/gold/`.", ""]
    if drafts:
        lines += [f"> **Provisional:** {len(drafts)} of {len(scored['gold'])} gold records are drafts "
                  f"({', '.join(drafts)}). Scores against draft gold are not the baseline.", ""]
    record_rows = [r for r in res if r["route"]["gold_basis"] == "record_only"]
    instr_rows = [r for r in res if r["route"]["gold_basis"] == "instruction"]
    fb_rows = [r for r in res if r["route"]["gold"] == "fallback"]
    lines += [
        "| Measure | Result |", "| --- | --- |",
        f"| Generations scored | {n} (skipped for errors: {len(scored['skipped'])}) |",
        f"| **Pass** (route, no invented trigger, pinned line when required, no unsupported advice) | {_pct(count(lambda r: r['verdict'] == 'pass'), n)} |",
        f"| Awaiting CSO review (otherwise passing) | {count(lambda r: r['verdict'] == 'review')} |",
        f"| (i) Route correct — all | {_pct(count(lambda r: r['route']['correct']), n)} |",
        f"|   … advice written as instructions | {_pct(count(lambda r: r['route']['correct'], instr_rows), len(instr_rows))} |",
        f"|   … advice only recorded (1 Oct ruling, post-dates v0.7) | {_pct(count(lambda r: r['route']['correct'], record_rows), len(record_rows))} |",
        f"|   … no advice documented (fall-back) | {_pct(count(lambda r: r['route']['correct'], fb_rows), len(fb_rows))} |",
        f"| (ii) Advice sentences located in the notes | {sum(r['advice']['n_gold'] + r['advice']['n_other_line'] for r in res)} of {sum(r['advice']['n_units'] for r in res)} (unlocated {sum(r['advice']['n_unlocated'] for r in res)}, adjudicated {sum(r['advice']['n_adjudicated'] for r in res)}) |",
        f"| (ii) Gold advice items carried (recall) | {sum(r['advice']['gold_items_carried'] for r in res)} of {sum(r['advice']['gold_items'] for r in res)} |",
        f"| (iii) **Invented seek-help triggers** (HAZ-01) | {sum(r['seek_help']['n_invented'] for r in res)} sentences in {count(lambda r: r['seek_help']['n_invented'] > 0)} generations |",
        f"| (iii) Pinned line missing where required | {count(lambda r: 'pinned_line_missing' in r['failures'])} |",
        f"| Safety-net gate PASS — as run at generation | {count(lambda r: r.get('gate', {}).get('at_generation_ok') is True)} of {count(lambda r: 'gate' in r)} |",
        f"| Safety-net gate PASS — current gate re-run | {count(lambda r: r.get('gate', {}).get('now_ok') is True)} of {count(lambda r: 'gate' in r)} |",
        "", "## Per scenario", "",
        "| Scenario | Gold route | Seek-help documented | Gold | Pass | Review | Fail | Route correct | Invented | Pinned missing |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for sid in sorted(scored["gold"], key=lambda s: [r["scenario_id"] for r in res].index(s)
                      if s in [r["scenario_id"] for r in res] else 99):
        rows = [r for r in res if r["scenario_id"] == sid]
        if not rows:
            continue
        r0 = rows[0]
        route = r0["route"]["gold"] + (f" ({r0['route']['gold_basis']})" if r0["route"]["gold_basis"] else "")
        lines.append(
            f"| {sid} | {route} | {'yes' if r0['seek_help']['gold_documented'] else 'no'} | "
            f"{scored['gold'][sid]['status']} | {count(lambda r: r['verdict'] == 'pass', rows)} | "
            f"{count(lambda r: r['verdict'] == 'review', rows)} | {count(lambda r: r['verdict'] == 'fail', rows)} | "
            f"{count(lambda r: r['route']['correct'], rows)}/{len(rows)} | "
            f"{sum(r['seek_help']['n_invented'] for r in rows)} | "
            f"{count(lambda r: 'pinned_line_missing' in r['failures'], rows)} |")
    (out / "SAFETY_NET.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # The review sheet: one entry per distinct unlocated sentence. A FILE, so it
    # may quote output and notes; nothing here reaches the console.
    corpus = {s.id: s for s in rce.load_corpus(corpus_path)[0]}
    pending: dict[tuple[str, str], dict] = {}
    for r in res:
        rec = json.loads((batch_dir / f"r{r['run']}" / f"{r['scenario_id']}.json").read_text(encoding="utf-8"))
        view = view_for(rec)
        for u in r["advice"]["units"]:
            if u["kind"] == "unlocated":
                e = pending.setdefault((r["scenario_id"], u["key"]),
                                       {"text": view.advice_units[u["index"]], "runs": []})
                e["runs"].append(r["run"])
    rv = [f"# Advice sentences to adjudicate — batch `{scored['batch_id']}`", "",
          "Each sentence appeared in v1's advice field and the scorer could not place it in a note "
          "line. Rule on each: **supported** (it says what a note line says) or **unsupported** (it "
          "adds something the notes do not say). Rulings are stored per sentence and reused across runs.", ""]
    for sid in sorted({k[0] for k in pending}):
        idx = G.line_index(corpus[sid].notes)
        rv += [f"## {sid}", "", "<details><summary>Notes</summary>", "", "```"]
        rv += [f"{k} {idx[k]}" for k in sorted(idx)] + ["```", "</details>", ""]
        for (s2, key), e in pending.items():
            if s2 == sid:
                rv.append(f"- `{key[:12]}` (runs {', '.join(map(str, e['runs']))}): {e['text']}")
        rv.append("")
    if not pending:
        rv.append("Nothing to adjudicate.")
    (out / "review_safety_net.md").write_text("\n".join(rv) + "\n", encoding="utf-8")
    _write_med_reports(batch_dir, out, scored, corpus)
    return out


def _write_med_reports(batch_dir: Path, out: Path, scored: dict, corpus: dict) -> None:
    meds = scored["meds"]
    (out / "meds.json").write_text(json.dumps(
        {"batch_id": scored["batch_id"], "generator": scored["generator"], "gold": scored["gold"], **meds},
        indent=2) + "\n", encoding="utf-8")
    res = meds["results"]
    n = len(res)
    if n == 0:
        (out / "MEDS.md").write_text(
            f"# D4 medication reconciliation — batch `{scored['batch_id']}`\n\n"
            "**Not scored:** no generation in this batch has a step 5a (reconcile_medications) "
            "output. D4 on the pipeline side waits for 5a.\n", encoding="utf-8")
        (out / "review_meds.md").write_text("Nothing to adjudicate.\n", encoding="utf-8")
        return
    v = lambda x: sum(1 for r in res if r["verdict"] == x)  # noqa: E731
    kinds: dict[str, int] = {}
    for r in res:
        for f in r["findings"]:
            kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    lines = [f"# D4 medication reconciliation — batch `{scored['batch_id']}`", "",
             f"Second step-level metric (ADR-009 (e)), scorer v{meds['scorer_version']}, generator "
             f"`{scored['generator']}`. Definition: `evals/score_meds.py`; gold: `evals/gold/`.", "",
             "| Measure | Result |", "| --- | --- |",
             f"| Generations scored | {n} |",
             f"| **Pass** | {_pct(v('pass'), n)} |",
             f"| Partial (tag missing or imprecise only) | {_pct(v('partial'), n)} |",
             f"| Fail | {_pct(v('fail'), n)} |",
             f"| Awaiting CSO review (otherwise passing) | {v('review')} |",
             f"| Gold drugs fully correct | {sum(r['drugs']['fully_correct'] for r in res)} of {sum(r['drugs']['gold'] for r in res)} |",
             f"| **Invented doses** (the notes give none) | {sum(r['invented_doses'] for r in res)} |",
             f"| **Invented frequencies** | {sum(r['invented_frequencies'] for r in res)} |",
             "", "## Findings by kind", "", "| Kind | Count |", "| --- | --- |"]
    lines += [f"| `{k}` | {c} |" for k, c in sorted(kinds.items(), key=lambda kv: -kv[1])] or ["| — | 0 |"]
    lines += ["", "## Per scenario", "", "| Scenario | Status | Pass | Partial | Fail | Review | Findings (all runs) |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    order = list(dict.fromkeys(r["scenario_id"] for r in res))
    for sid in order:
        rows = [r for r in res if r["scenario_id"] == sid]
        fk: dict[str, int] = {}
        for r in rows:
            for f in r["findings"]:
                label = f"{f['drug'] or '—'}: {f['kind']}"
                fk[label] = fk.get(label, 0) + 1
        lines.append(f"| {sid} | {rows[0]['discharge_status']} | "
                     + " | ".join(str(sum(1 for r in rows if r["verdict"] == x)) for x in ("pass", "partial", "fail", "review"))
                     + " | " + ("; ".join(f"{k} ×{c}" for k, c in fk.items()) or "—") + " |")
    (out / "MEDS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    pending: dict[tuple[str, str], dict] = {}
    for r in res:
        rec = json.loads((batch_dir / f"r{r['run']}" / f"{r['scenario_id']}.json").read_text(encoding="utf-8"))
        view = med_view_for(rec)
        for x in r["extras"]:
            if x["kind"] == "unadjudicated":
                e = pending.setdefault((r["scenario_id"], x["key"]), {"text": view.entries[x["index"]].text, "runs": []})
                e["runs"].append(r["run"])
    rv = [f"# Medication entries to adjudicate — batch `{scored['batch_id']}`", "",
          "Each entry names no drug in the gold. Rule on each: **acceptable** (a synonym or a "
          "documented item the gold should name) or **invented** (a drug the notes do not support).", ""]
    for (sid, key), e in sorted(pending.items()):
        rv.append(f"- **{sid}** `{key[:12]}` (runs {', '.join(map(str, e['runs']))}): {e['text']}")
    if not pending:
        rv.append("Nothing to adjudicate.")
    (out / "review_meds.md").write_text("\n".join(rv) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("batch_dir", type=Path)
    args = p.parse_args(argv)
    try:
        scored = score_batch(args.batch_dir)
    except (ScoreError, FileNotFoundError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    out = write_reports(args.batch_dir, scored)
    res = scored["results"]
    print(f"Scored {len(res)} generations (skipped {len(scored['skipped'])}); "
          f"pass {sum(r['verdict'] == 'pass' for r in res)}, review {sum(r['verdict'] == 'review' for r in res)}, "
          f"fail {sum(r['verdict'] == 'fail' for r in res)}; invented triggers "
          f"{sum(r['seek_help']['n_invented'] for r in res)}.")
    mres = scored["meds"]["results"]
    print(f"D4: pass {sum(r['verdict'] == 'pass' for r in mres)}, partial "
          f"{sum(r['verdict'] == 'partial' for r in mres)}, fail {sum(r['verdict'] == 'fail' for r in mres)}, "
          f"review {sum(r['verdict'] == 'review' for r in mres)}; invented doses "
          f"{sum(r['invented_doses'] for r in mres)}.")
    drafts = sum(1 for m in scored["gold"].values() if m["status"] != "approved")
    if drafts:
        print(f"Provisional: {drafts} gold record(s) are drafts.")
    print(f"Reports -> {rce._rel(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
