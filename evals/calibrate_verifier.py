"""Offline calibration of step 7v `verify_patient` (ADR-010 V5, 4 Nov 2026).

"Offline" = no new patient-facing generation: the verifier runs, live, on step-7
leaflets already saved in a batch, and its verdicts are compared with labels the
CSO has ALREADY made — the step-7 rulings (`patient_rulings` in
evals/gold/adjudications/*.json, joined through score_patient.review_items) and
the blind-read findings, mapped to sentences in a file the CSO approves before
any held-out run (evals/calibration/verifier/blind_read_map.json, O4).

  python evals/calibrate_verifier.py run   <batch_dir> [--repeats N] [--yes] [--dry-run] [--held-out --frozen <sha12>]
  python evals/calibrate_verifier.py score --dev <batch_id> --held-out <batch_id> [<batch_id> ...]

`run` writes evals/calibration/verifier/<prompt_sha12>/<batch_id>/rep<k>/r<run>/<ID>.json
(never overwrites). A held-out batch runs only with --held-out and --frozen = the
verifier prompt's body sha (first 12) — the frozen, committed prompt; `score`
refuses a held-out set produced by a different prompt than the dev set.

The bar (ADR-010 V5, CSO 4 Nov 2026 14:34), written before calibration ran:
  1. missed defects 0 (held-out): every CSO-ruled defect row and every mapped
     blind-read finding gets a non-supported verdict (or, for an omission, an
     unreached item);
  2. false flags <= 25% of CSO-ruled acceptable rows (held-out);
  3. self-agreement >= 90% of sentence verdicts (supported vs not) across the
     repeats (on the dev set — O2, recommended; --agreement-on held-out to change);
  4. precision on NEW flags (sentences no label covers) reported — grouped in
     NEW_FLAGS.md for a CSO ruling; no threshold; never changes items 1-3.

How a label is matched (C1 — to be approved by the CSO before the held-out run):
  - sentence-level rows (cause, certainty, test_term, advice, medication, drug,
    number, unit, conflict, sensitive, promise): the numbered sentence in the same
    C section whose text equals the row's sentence; caught / false flag = that
    sentence is not `supported`;
  - qualifier rows (score_patient records them per C SECTION): caught / false
    flag = any sentence of that C section is not `supported`;
  - diagnosis rows ("not reached: <line>"): caught / false flag = an `unreached`
    item on that diagnosis section whose span shares a head word with the line;
  - blind-read findings: as the map says (a sentence, or an unreached A item).
Rows are counted per generation (the same sentence in two runs is two rows), and
also reported per unique key; held-out rows whose key also occurs in dev are
reported separately (identical leaflets at temperature 0 would leak).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "generate"))
sys.path.insert(0, str(REPO / "evals"))

import run_cold_eval as rce  # noqa: E402
import score_batch as SB  # noqa: E402
import score_patient as SP  # noqa: E402
from pipeline import StepContext, StepError, run_step  # noqa: E402
from pipeline import compose_patient as CP7  # noqa: E402
from pipeline import meaning as MN  # noqa: E402
from pipeline import verify_patient as V  # noqa: E402

CAL_DIR = REPO / "evals" / "calibration" / "verifier"
MAP_PATH = CAL_DIR / "blind_read_map.json"
NEW_FLAG_RULINGS = CAL_DIR / "new_flag_rulings.json"
ADJ_DIR = REPO / "evals" / "gold" / "adjudications"
RUNS_DIR = rce.DEFAULT_RUNS_DIR

BAR = {"missed_max": 0, "false_flag_max": 0.25, "self_agreement_min": 0.90}
SECTION_KINDS = ("qualifier",)
UNREACHED_KINDS = ("diagnosis",)
EST_TOKENS = {"input": 3500, "output": 900}       # per verifier call (ADR-010 Consequences)
EST_SECONDS = 12


def _norm(t: str) -> str:
    return " ".join(t.split()).casefold()


def prompt_sha12() -> str:
    return V.system_prompt()[1][:12]


# --- run ----------------------------------------------------------------------

def records(batch_dir: Path) -> list[dict]:
    out = []
    for p in sorted(batch_dir.glob("r*/*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        if (rec.get("steps") or {}).get("compose_patient"):
            out.append(rec)
    return out


def verify_record(rec: dict, ctx: StepContext) -> dict:
    inp7 = CP7.patient_input(rec["steps"]["compose_clinical"])
    vin = V.verify_input(inp7, rec["steps"]["compose_patient"])
    try:
        return {"verify_patient": run_step("verify_patient", vin, ctx=ctx), "error": None}
    except StepError as exc:
        return {"verify_patient": None, "error": exc.code}


class DryVerifier:
    """Every sentence supported, nothing unreached — for --dry-run and tests."""
    def __init__(self, read_timeout=None):
        self.read_timeout = read_timeout

    def converse(self, **kw):
        text = kw["messages"][0]["content"][0]["text"]
        items, cid, first_a = [], None, ""
        for line in text.splitlines():
            if line.startswith("=== "):
                cid, first_a = line.split()[1], ""
            elif line.startswith("[sections.") and not first_a:
                first_a = line[1:].split("]", 1)[0]
            elif cid and line.startswith(f"{cid}.s") and ": " in line:
                items.append({"c_section_id": cid, "sentence_id": line.split(": ", 1)[0],
                              "a_section_id": first_a, "span": "", "reason": "none", "verdict": "supported"})
        return {"output": {"message": {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": "dry", "name": V.TOOL_NAME, "input": {"items": items}}}]}},
                "stopReason": "tool_use", "usage": {"inputTokens": 0, "outputTokens": 0}, "metrics": {"latencyMs": 0}}


def cmd_run(a, *, client_factory=None, now=None, confirm=input, sleep=time.sleep, cal_dir: Path = CAL_DIR) -> int:
    batch_dir = Path(a.batch_dir)
    recs = records(batch_dir)
    if not recs:
        print(f"error: no step-7 records in {batch_dir}", file=sys.stderr)
        return 2
    sha = prompt_sha12()
    if a.held_out and a.frozen != sha:
        print(f"refused: a held-out run needs --frozen {sha} (the verifier prompt's body sha) — "
              f"freeze and commit the prompt first.", file=sys.stderr)
        return 2
    batch_id = recs[0]["batch_id"]
    out_dir = cal_dir / sha / batch_id
    n = len(recs) * a.repeats
    est = rce.cost_usd([{"input_tokens": EST_TOKENS["input"] * n, "output_tokens": EST_TOKENS["output"] * n}])
    start = (now or (lambda: dt.datetime.now(rce.LONDON)))()
    end = start + dt.timedelta(seconds=n * EST_SECONDS)
    print(f"Verifier:  prompt_sha256 {sha}… · model {rce.MODEL_ID} ({rce.REGION})")
    print(f"Batch:     {batch_id} ({len(recs)} leaflets x {a.repeats} repeats = {n} calls) · "
          f"{'HELD-OUT' if a.held_out else 'dev'}")
    print(f"Estimate:  <= ${est:.2f}; ~{n * EST_SECONDS / 60:.0f} min, finishing ~{end:%H:%M} London")
    clash = rce.canary_window_clash(start, end)
    if clash:
        print(f"refused: overlaps a canary window ({clash[0]:%a %H:%M}–{clash[1]:%H:%M} London).", file=sys.stderr)
        return 3
    if out_dir.exists() and not a.dry_run:
        print(f"refused: {rce._rel(out_dir)} already exists — never overwritten.", file=sys.stderr)
        return 2
    if not a.dry_run and not a.yes:
        if confirm(f"Run {n} live verifier calls (est. <= ${est:.2f})? Type yes: ").strip().lower() != "yes":
            print("Not run.")
            return 1
    factory = DryVerifier if a.dry_run else (client_factory or (lambda t: __import__("run_pipeline_eval").live_client(t)))
    ctx = StepContext(bedrock=factory(V.READ_TIMEOUT_S), model_id=rce.MODEL_ID, prompt_caching=True, sleep=sleep)
    total, errors = 0.0, 0
    if not a.dry_run:
        out_dir.mkdir(parents=True)
        (out_dir / "manifest.json").write_text(json.dumps({
            "batch_id": batch_id, "prompt_sha256": V.system_prompt()[1], "prompt_path": rce._rel(V.PROMPT_PATH),
            "model_id": rce.MODEL_ID, "region": rce.REGION, "repeats": a.repeats, "held_out": a.held_out,
            "created_at": start.isoformat(timespec="seconds")}, indent=2) + "\n", encoding="utf-8")
    for k in range(1, a.repeats + 1):
        for rec in recs:
            t = (now or (lambda: dt.datetime.now(rce.LONDON)))()
            if not a.dry_run and rce.canary_window_clash(t, t + dt.timedelta(seconds=EST_SECONDS)):
                print("  stopping: a canary window is about to open.")
                return 1
            res = verify_record(rec, ctx)
            vp = res["verify_patient"]
            if vp:
                total += rce.cost_usd([{"input_tokens": (vp["call"]["input_tokens"] or 0)
                                        + (vp["call"]["cache_read_tokens"] or 0) + (vp["call"]["cache_write_tokens"] or 0),
                                        "output_tokens": vp["call"]["output_tokens"] or 0}])
            else:
                errors += 1
            print(f"  rep{k} r{rec['run']} {rec['scenario_id']:<4} "
                  + (f"flagged={vp['flagged']} sentences={vp['counts']['sentences']} checks={vp['counts']['checks']}"
                     if vp else f"FAILED: {res['error']}"))
            if not a.dry_run:
                d = out_dir / f"rep{k}" / f"r{rec['run']}"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{rec['scenario_id']}.json").write_text(json.dumps(
                    {"generation_id": rec["generation_id"], **res}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nEst. cost ${total:.4f} · failures {errors}" + (" · dry run, nothing written" if a.dry_run else ""))
    return 0 if not errors else 1


# --- labels -------------------------------------------------------------------

def rulings_for(sid: str) -> dict:
    return SB.load_adjudications(sid, ADJ_DIR, "patient_rulings")


def labels_for(rec: dict) -> list[dict]:
    """The CSO-ruled step-7 rows of one record: {key, kind, heading, sentence, ruling}."""
    rul = rulings_for(rec["scenario_id"])
    return [{"key": it["key"], "kind": it["kind"], "heading": it["heading"], "sentence": it["sentence"],
             "ruling": rul[it["key"]]} for it in SP.review_items(rec) if it["key"] in rul]


def blind_read_map() -> list[dict]:
    if not MAP_PATH.exists():
        return []
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))["findings"]


def _not_supported(vp: dict) -> set[str]:
    return {v["sentence_id"] for v in vp["verdicts"] if v["verdict"] != V.SUPPORTED}


def match(label: dict, rec: dict, vp: dict) -> tuple[bool, list[str]]:
    """(flagged by the verifier?, the sentence_ids the label covers). C1 rules."""
    vin = V.verify_input(CP7.patient_input(rec["steps"]["compose_clinical"]), rec["steps"]["compose_patient"])
    bad = _not_supported(vp)
    secs = [s for s in vin["c_sections"] if s["heading"] == label["heading"]]
    if label.get("unit") == "unreached" or label["kind"] in UNREACHED_KINDS:
        line = label.get("a_text") or label["sentence"].removeprefix("not reached: ")
        words = set(MN.head_words(line)) or MN.stems(line)
        hit = any(words & MN.stems(u["span"]) for u in vp["unreached"])
        return hit, []
    if label["kind"] in SECTION_KINDS and label.get("unit") != "sentence":
        ids = [t["sentence_id"] for s in secs for t in s["sentences"]]
        return bool(set(ids) & bad), ids
    target = _norm(label.get("text") or label["sentence"])
    ids = [t["sentence_id"] for s in secs for t in s["sentences"]
           if (_norm(t["text"]) == target if label.get("unit") != "sentence" or "text" not in label
               else target in _norm(t["text"]))]
    return bool(set(ids) & bad), ids


# --- score --------------------------------------------------------------------

def load_outputs(sha_dir: Path, batch_id: str) -> dict[int, dict[str, dict]]:
    """{repeat: {generation_id: verify_patient output or None}}"""
    reps = {}
    for rd in sorted((sha_dir / batch_id).glob("rep*")):
        k = int(rd.name[3:])
        reps[k] = {}
        for p in rd.glob("r*/*.json"):
            d = json.loads(p.read_text(encoding="utf-8"))
            reps[k][d["generation_id"]] = d["verify_patient"]
    return reps


def evaluate(batch_ids: list[str], sha_dir: Path, repeat: int = 1, exclude_keys: set[str] = frozenset()) -> dict:
    rows, covered, unlabelled = [], {}, []
    bmap = blind_read_map()
    for bid in batch_ids:
        outs = load_outputs(sha_dir, bid).get(repeat, {})
        for rec in records(RUNS_DIR / bid):
            vp = outs.get(rec["generation_id"])
            if vp is None:
                rows.append({"generation_id": rec["generation_id"], "source": "error", "ruling": None,
                             "caught": False, "key": None, "kind": None})
                continue
            cov, ruled_defect_ids = set(), set()
            labs = labels_for(rec) + [dict(f, ruling="defect", key=f"map:{i}", kind=f.get("dimension", "map"),
                                           sentence=f.get("text", ""))
                                      for i, f in enumerate(bmap) if f["generation_id"] == rec["generation_id"]]
            for lab in labs:
                caught, ids = match(lab, rec, vp)
                cov |= set(ids)
                if lab["key"].startswith("map:") and ids and set(ids) <= ruled_defect_ids:
                    continue        # the blind read found what a CSO-ruled defect row already covers
                if lab["ruling"] == "defect" and not lab["key"].startswith("map:"):
                    ruled_defect_ids |= set(ids)
                rows.append({"generation_id": rec["generation_id"], "source": "map" if lab["key"].startswith("map:") else "ruling",
                             "key": lab["key"], "kind": lab["kind"], "heading": lab["heading"],
                             "ruling": lab["ruling"], "caught": caught, "leaked": lab["key"] in exclude_keys})
            for v in vp["verdicts"]:
                if v["verdict"] != V.SUPPORTED and v["sentence_id"] not in cov:
                    text = next(t["text"] for s in V.verify_input(CP7.patient_input(rec["steps"]["compose_clinical"]),
                                                                   rec["steps"]["compose_patient"])["c_sections"]
                                for t in s["sentences"] if t["sentence_id"] == v["sentence_id"])
                    unlabelled.append({"generation_id": rec["generation_id"], "heading": v["heading"],
                                       "sentence": text, "verdict": v["verdict"], "reason": v["reason"], "span": v["span"],
                                       "key": hashlib.sha256(f"verifier|{v['heading']}|{_norm(text)}".encode()).hexdigest()[:16]})
    defects = [r for r in rows if r["ruling"] == "defect"]
    accept = [r for r in rows if r["ruling"] == "acceptable"]
    missed = [r for r in defects if not r["caught"]]
    false = [r for r in accept if r["caught"]]
    return {"rows": rows, "defects": len(defects), "missed": missed, "acceptable": len(accept),
            "false_flags": len(false), "false_rate": (len(false) / len(accept)) if accept else None,
            "errors": sum(1 for r in rows if r["source"] == "error"), "unlabelled": unlabelled,
            "missed_unique": sorted({r["key"] for r in missed}),
            "missed_not_leaked": [r for r in missed if not r.get("leaked")]}


def self_agreement(batch_id: str, sha_dir: Path) -> float | None:
    reps = load_outputs(sha_dir, batch_id)
    if len(reps) < 2:
        return None
    same = total = 0
    for gid in set.intersection(*(set(r) for r in reps.values())):
        outs = [reps[k][gid] for k in sorted(reps)]
        if any(o is None for o in outs):
            continue
        per = [{v["sentence_id"]: v["verdict"] == V.SUPPORTED for v in o["verdicts"]} for o in outs]
        for sid in per[0]:
            total += 1
            same += all(p.get(sid) == per[0][sid] for p in per)
    return same / total if total else None


def cmd_score(a, *, sha_dir: Path | None = None) -> int:
    sha_dir = sha_dir or (CAL_DIR / (a.sha or prompt_sha12()))
    shas = {json.loads((sha_dir / b / "manifest.json").read_text())["prompt_sha256"][:12]
            for b in [a.dev, *a.held_out] if (sha_dir / b / "manifest.json").exists()}
    if len(shas) > 1:
        print("refused: dev and held-out were produced by different verifier prompts.", file=sys.stderr)
        return 2
    dev = evaluate([a.dev], sha_dir)
    dev_keys = {r["key"] for r in dev["rows"] if r.get("key")}
    held = evaluate(a.held_out, sha_dir, exclude_keys=dev_keys) if a.held_out else None
    agree_on = a.dev if a.agreement_on == "dev" else (a.held_out[0] if a.held_out else a.dev)
    agree = self_agreement(agree_on, sha_dir)
    judged = held or dev
    verdict = {
        "1 missed defects": (len(judged["missed"]), len(judged["missed"]) <= BAR["missed_max"]),
        "2 false-flag rate": (judged["false_rate"], judged["false_rate"] is not None
                              and judged["false_rate"] <= BAR["false_flag_max"]),
        "3 self-agreement": (agree, agree is not None and agree >= BAR["self_agreement_min"]),
    }
    rulings = json.loads(NEW_FLAG_RULINGS.read_text()) if NEW_FLAG_RULINGS.exists() else {}
    new = judged["unlabelled"]
    ruled = [u for u in new if u["key"] in rulings]
    prec = (sum(1 for u in ruled if rulings[u["key"]] == "defect") / len(ruled)) if ruled else None
    lines = [f"# Step 7v calibration — prompt `{sha_dir.name}`", "",
             f"Dev: `{a.dev}` · held-out: {', '.join(f'`{b}`' for b in a.held_out) or '(none yet)'} · "
             f"judged on: {'held-out' if held else '**dev only — not the bar**'}", "",
             "| Bar item | Value | Bar | Verdict |", "|---|---|---|---|"]
    fmt = {"1 missed defects": lambda v: f"{v} of {judged['defects']}",
           "2 false-flag rate": lambda v: "—" if v is None else f"{v:.1%} ({judged['false_flags']} of {judged['acceptable']})",
           "3 self-agreement": lambda v: "—" if v is None else f"{v:.1%} (on {agree_on})"}
    bars = {"1 missed defects": "0", "2 false-flag rate": "≤ 25%", "3 self-agreement": "≥ 90%"}
    for k, (val, ok) in verdict.items():
        lines.append(f"| {k} | {fmt[k](val)} | {bars[k]} | {'MET' if ok else '**NOT MET**'} |")
    lines.append(f"| 4 new-flag precision | {'—' if prec is None else f'{prec:.0%}'} ({len(ruled)} of {len(new)} ruled) | report | — |")
    lines += ["", f"Verifier failures: {judged['errors']}. Missed (unique keys): {len(judged['missed_unique'])}; "
              f"missed excluding keys also in dev: {len(judged['missed_not_leaked'])}.", "",
              "## Missed defects", ""]
    lines += [f"- {r['generation_id']} · {r['kind']} · {r['heading']} · {r['source']}" for r in judged["missed"]] or ["- none"]
    out = sha_dir / "scores"
    out.mkdir(exist_ok=True)
    (out / "CALIBRATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    groups: dict = {}
    for u in new:
        groups.setdefault(u["key"], []).append(u)
    nf = ["# New verifier flags — for a CSO ruling (bar item 4; never changes items 1–3)", "",
          "Rule each key `acceptable` (the flag was wrong) or `defect` in "
          f"`{rce._rel(NEW_FLAG_RULINGS)}`.", ""]
    for key, us in sorted(groups.items(), key=lambda kv: (kv[1][0]["generation_id"], kv[1][0]["heading"])):
        u = us[0]
        nf.append(f"- `{key}` ×{len(us)} · {u['heading']} · {u['verdict']}/{u['reason']} · "
                  f"\"{u['sentence']}\" — span \"{u['span']}\"" + (f" — ruled {rulings[key]}" if key in rulings else ""))
    (out / "NEW_FLAGS.md").write_text("\n".join(nf) + "\n", encoding="utf-8")
    (out / "calibration.json").write_text(json.dumps({
        "prompt_sha12": sha_dir.name, "dev": a.dev, "held_out": a.held_out,
        "bar": BAR, "verdict": {k: {"value": v, "met": ok} for k, (v, ok) in verdict.items()},
        "new_flag_precision": prec, "missed": judged["missed"]}, indent=2, default=str) + "\n", encoding="utf-8")
    print("\n".join(lines[:12]))
    return 0 if all(ok for _v, ok in verdict.values()) else 1


def parse_args(argv):
    p = argparse.ArgumentParser(description="Offline calibration of step 7v (ADR-010 V5).")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("batch_dir")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--held-out", action="store_true")
    r.add_argument("--frozen", default=None)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--yes", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("--dev", required=True)
    s.add_argument("--held-out", nargs="*", default=[])
    s.add_argument("--agreement-on", choices=("dev", "held-out"), default="dev")
    s.add_argument("--sha", default=None)
    a = p.parse_args(argv)
    if a.cmd == "run" and a.repeats < 1:
        p.error("--repeats must be at least 1")
    return a


def main(argv=None, **kw) -> int:
    a = parse_args(argv)
    if a.cmd == "run":
        return cmd_run(a, **kw)
    return cmd_score(a, **{k: v for k, v in kw.items() if k == "sha_dir"})


if __name__ == "__main__":
    sys.exit(main())
