"""evals/combine_judge.py — combined (checks + judge) verdicts for a judge run (W6).

Synthetic notes, outputs and verdicts written for these tests only — no corpus
text, and never note text in test names or messages."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import combine_judge as cj  # noqa: E402
import run_cold_eval as rce  # noqa: E402

D = cj.DIMENSIONS
NOTES = {"S1": "Day 1 (03/04) 60M admitted. NKDA.\nDay 2 (04/04) Discharged.",
         "S2": "Day 1 (03/04) NKDA on clerking.\nDay 2 nursing note: latex allergy, patient-reported."}


def doc(a="", b="", c=""):
    return f"Preamble.\n# PART A — SUMMARY\n{a}\n## PART B — GP LETTER\n{b}\nPART C - PATIENT VERSION\n{c}\n"


def judge_rec(sid, run, verdicts, output, rep=1, error=None):
    dims = {d: {"verdict": verdicts.get(d, "pass"), "auto_fail": False, "findings": []} for d in D}
    return {"scenario_id": sid, "run": run, "repeat": rep, "error": error,
            "output_sha256": rce.sha256_text(output),
            "verified": None if error else {"dimensions": dims, "defects": []}}


def make_run(tmp_path, items):
    """items: (sid, run, output, judge verdicts). Returns the judge folder."""
    batch = tmp_path / "batch"
    jd = batch / "judge" / "j1"
    for sid, run, output, verdicts in items:
        (batch / f"r{run}").mkdir(parents=True, exist_ok=True)
        (batch / f"r{run}" / f"{sid}.json").write_text(json.dumps({"output": output}))
        (jd / "rep1" / f"r{run}").mkdir(parents=True, exist_ok=True)
        (jd / "rep1" / f"r{run}" / f"{sid}.json").write_text(json.dumps(judge_rec(sid, run, verdicts, output)))
    (jd / "manifest.json").write_text(json.dumps({
        "batch_id": "batch", "judge_id": "j1", "model_id": "m",
        "prompt": {"instructions_sha256": "a" * 64, "rubric_sha256": "b" * 64}}))
    return jd


def test_check_fail_overrides_a_judge_pass_on_d2_only():
    out = doc("Admitted 03/04/2025.")                       # added year -> D2 check Fail
    rows = cj.combine_record(judge_rec("S1", 1, {}, out), cj.rc.run_checks(NOTES["S1"], out))
    by = {r["dim"]: r for r in rows}
    assert (by["D2"]["judge"], by["D2"]["check"], by["D2"]["combined"]) == ("pass", "fail", "fail")
    assert by["D2"]["check_labels"] == ["added_year"]
    assert all(by[d]["combined"] == by[d]["judge"] for d in ("D3", "D6", "D7", "D8"))


def test_allergy_inversion_carries_auto_fail_into_combined():
    out = doc(c="Allergies: NKDA")
    rows = cj.combine_record(judge_rec("S2", 1, {"D7": "partial"}, out), cj.rc.run_checks(NOTES["S2"], out))
    d7 = next(r for r in rows if r["dim"] == "D7")
    assert (d7["combined"], d7["combined_auto_fail"], d7["judge_auto_fail"]) == ("fail", True, False)


def test_no_check_decision_leaves_the_judge_verdict_including_na():
    out = doc("Seen and discharged.")
    rows = cj.combine_record(judge_rec("S1", 1, {"D2": "partial", "D8": "na"}, out),
                             cj.rc.run_checks(NOTES["S1"], out))
    assert [r["combined"] for r in rows] == [r["judge"] for r in rows]


def test_judge_error_keeps_a_check_fail_and_is_counted(tmp_path):
    out = doc("Admitted 03/04/2025.")
    rows = cj.combine_record(judge_rec("S1", 1, {}, out, error={"code": "x"}), cj.rc.run_checks(NOTES["S1"], out))
    by = {r["dim"]: r["combined"] for r in rows}
    assert by["D2"] == "fail" and by["D3"] is None
    assert cj.summarise(rows)["judge_errors"] == 1


def test_end_to_end_counts_and_changed_cells(tmp_path):
    jd = make_run(tmp_path, [
        ("S1", 1, doc("Admitted 03/04/2025."), {"D2": "partial"}),   # check flips D2
        ("S1", 2, doc("Discharged."), {"D2": "fail"}),               # judge already Fail
        ("S2", 1, doc("Discharged."), {"D8": "na"}),                 # no change
    ])
    jm, rows, _ = cj.load_rows(jd, notes=NOTES)
    s = cj.summarise(rows)
    assert s["records"] == 3
    assert s["judge_alone"]["D2"] == {"pass": 1, "partial": 1, "fail": 1, "na": 0}
    assert s["combined"]["D2"] == {"pass": 1, "partial": 0, "fail": 2, "na": 0}
    assert s["combined"]["D3"] == s["judge_alone"]["D3"]
    assert [(c["sid"], c["run"], c["dim"]) for c in s["changed_by_checks"]] == [("S1", 1, "D2")]
    assert s["by_scenario"]["S1"]["D2"] == {"fail": 2}
    cj.write_reports(jd, jm, s, 0)
    md = (jd / "COMBINED.md").read_text()
    assert "| S1 | r1 | rep1 | D2 | partial | fail (added_year) | fail |" in md
    assert "Discharged" not in md and "Admitted" not in md          # no output text in reports
    assert json.loads((jd / "combined.json").read_text())["combined"]["D2"]["fail"] == 2


def test_refuses_when_the_output_changed_since_judging(tmp_path):
    jd = make_run(tmp_path, [("S1", 1, doc("Discharged."), {})])
    (tmp_path / "batch" / "r1" / "S1.json").write_text(json.dumps({"output": doc("Edited.")}))
    with pytest.raises(cj.CombineError, match="different output"):
        cj.load_rows(jd, notes=NOTES)


def test_refuses_an_empty_judge_folder(tmp_path):
    jd = tmp_path / "b" / "judge" / "j"
    jd.mkdir(parents=True)
    (jd / "manifest.json").write_text("{}")
    with pytest.raises(cj.CombineError, match="no judge records"):
        cj.load_rows(jd, notes=NOTES)
