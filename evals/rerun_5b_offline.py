"""Offline re-run of step 5b over a saved pipeline batch (W5 carry-in 1).

Recomputes select_safety_net from each saved record's step-1 flags and step-3
facts with TODAY's 5b (CSO decision of 23 Oct 2026: part_ab always carries the
pinned line) and writes a DERIVED batch to a new directory:

  <out>/r<n>/<ID>.json  the source record, copied, with steps.select_safety_net
                        replaced and a `rerun` block saying where it came from
  <out>/batch.json      the source index, batch_id changed
  <out>/manifest.json   the source manifest, batch_id changed, `derived_from` added
  <out>/rerun.json      counts only: what changed against the source
  <out>/RERUN.md        the same, readable

No model call, no network. The source batch is only read — never written. Steps
1–4 (and the record's calls / cost) are the source's, unchanged; only 5b is new.
The derived batch keeps the source's layout, so evals/score_batch.py scores it
as it scored the source. Output carries counts, IDs and fixed labels — never
note or output text.

Usage: python evals/rerun_5b_offline.py evals/runs/<src> --out evals/runs/<new>
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "evals"))

import gold as G  # noqa: E402
import haz03_check as H  # noqa: E402
from pipeline.safety_net import select_safety_net  # noqa: E402

RERUN_VERSION = 1
SAFETY_NET_PY = ROOT / "src" / "generate" / "pipeline" / "safety_net.py"
DECISION = "CSO 23 Oct 2026 — 5b part_ab always carries the pinned line (amends 1 Oct)"
LONDON = ZoneInfo("Europe/London")


class RerunError(ValueError):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def rerun(src: Path, out: Path, now=None, gold_dir: Path = G.GOLD_DIR) -> dict:
    src, out = src.resolve(), out.resolve()
    if out.exists():
        raise RerunError("output directory already exists — never written over")
    if out == src or src in out.parents:
        raise RerunError("output must not be the source batch or inside it")
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    index = json.loads((src / "batch.json").read_text(encoding="utf-8"))
    if manifest.get("generator") != "pipeline":
        raise RerunError("not a pipeline batch")

    created = (now or (lambda: dt.datetime.now(LONDON)))().isoformat(timespec="seconds")
    sn_sha = _sha(SAFETY_NET_PY)
    provenance = {"kind": "offline_5b", "rerun_version": RERUN_VERSION, "decision": DECISION,
                  "safety_net_py_sha256": sn_sha, "created_at": created,
                  "from_batch": manifest["batch_id"], "model_calls": 0}

    rows, skipped, records = [], [], {}
    for g in index["generations"]:
        path = src / g["record"]
        rec = json.loads(path.read_text(encoding="utf-8"))
        steps = rec.get("steps") or {}
        old = steps.get("select_safety_net")
        facts = (steps.get("validate_facts") or {}).get("facts")
        flags = (steps.get("guard_input") or {}).get("flags")
        if rec.get("error") or rec.get("failed_step") or not isinstance(old, dict) \
                or facts is None or flags is None:
            skipped.append({"record": g["record"], "reason": "incomplete_source"})
            continue
        new = select_safety_net({"facts": facts, "flags": flags})
        derived = copy.deepcopy(rec)
        derived["steps"]["select_safety_net"] = new
        derived["rerun"] = {**provenance, "from_record": g["record"], "from_record_sha256": _sha(path)}
        records[g["record"]] = derived

        gold = G.load(rec["scenario_id"], gold_dir)
        h_old = H.check(gold, facts.get("age_group"), old)
        h_new = H.check(gold, facts.get("age_group"), new)
        same = {k: old.get(k) == new.get(k) for k in
                ("route", "audience", "pinned_line", "advice", "advice_excluded", "cited_lines", "part_c")}
        rows.append({"record": g["record"], "scenario_id": rec["scenario_id"], "run": rec.get("run"),
                     "route": new["route"], "unchanged": same,
                     "part_ab_changed": old.get("part_ab") != new["part_ab"],
                     "pinned_in_part_ab_before": new["pinned_line"] in (old.get("part_ab") or ""),
                     "pinned_in_part_ab_after": new["pinned_line"] in new["part_ab"],
                     "haz03_v2_before": h_old["verdict"], "haz03_v2_after": h_new["verdict"],
                     "haz03_v2_after_failures": h_new["failures"]})

    out_id = out.name
    for rel, derived in records.items():
        _write(out / rel, derived)
    _write(out / "batch.json", {**index, "batch_id": out_id,
                                "generations": [g for g in index["generations"] if g["record"] in records]})
    _write(out / "manifest.json", {**manifest, "batch_id": out_id, "created_at": created,
                                   "derived_from": {**provenance,
                                                    "manifest_sha256": _sha(src / "manifest.json"),
                                                    "batch_sha256": _sha(src / "batch.json")}})
    summary = _summarise(rows, skipped, provenance, out_id)
    _write(out / "rerun.json", summary)
    (out / "RERUN.md").write_text(_markdown(summary), encoding="utf-8")
    return summary


def _summarise(rows, skipped, provenance, out_id) -> dict:
    n = len(rows)
    by = lambda pred: sum(1 for r in rows if pred(r))  # noqa: E731
    fields = ("route", "audience", "pinned_line", "advice", "advice_excluded", "cited_lines", "part_c")
    return {
        "batch_id": out_id, **{k: v for k, v in provenance.items() if k != "kind"},
        "n": n, "skipped": skipped,
        "unchanged": {f: by(lambda r, f=f: r["unchanged"][f]) for f in fields},
        "part_ab_changed": {"documented": by(lambda r: r["part_ab_changed"] and r["route"] == "documented"),
                            "fallback": by(lambda r: r["part_ab_changed"] and r["route"] == "fallback")},
        "routes": {"documented": by(lambda r: r["route"] == "documented"),
                   "fallback": by(lambda r: r["route"] == "fallback")},
        "pinned_in_part_ab": {"before": by(lambda r: r["pinned_in_part_ab_before"]),
                              "after": by(lambda r: r["pinned_in_part_ab_after"])},
        "haz03_v2_pass": {"before": by(lambda r: r["haz03_v2_before"] == "pass"),
                          "after": by(lambda r: r["haz03_v2_after"] == "pass")},
        "rows": rows,
    }


def _markdown(s: dict) -> str:
    n = s["n"]
    u = s["unchanged"]
    lines = [
        f"# Offline 5b re-run — {s['batch_id']}",
        "",
        f"From `{s['from_batch']}` at {s['created_at']}. {s['decision']}. "
        f"safety_net.py sha256 `{s['safety_net_py_sha256'][:12]}`. Model calls: 0. "
        f"Steps 1–4 copied from the source; only 5b recomputed.",
        "",
        f"- Records: {n} (skipped {len(s['skipped'])})",
        f"- Routes: documented {s['routes']['documented']}, fall-back {s['routes']['fallback']}",
        "- Unchanged against the source: " + ", ".join(f"{k} {v}/{n}" for k, v in u.items()),
        f"- part_ab changed: documented route {s['part_ab_changed']['documented']}, "
        f"fall-back route {s['part_ab_changed']['fallback']}",
        f"- Pinned line in part_ab: before {s['pinned_in_part_ab']['before']}/{n}, "
        f"after {s['pinned_in_part_ab']['after']}/{n}",
        f"- HAZ-03 check v2 pass: before {s['haz03_v2_pass']['before']}/{n}, "
        f"after {s['haz03_v2_pass']['after']}/{n}",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("src", type=Path)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)
    try:
        s = rerun(a.src, a.out)
    except (RerunError, FileNotFoundError, KeyError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(_markdown(s), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
