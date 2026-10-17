"""evals/compare_calibration.py — calibration maths (W5). Synthetic verdicts only."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import compare_calibration as cc  # noqa: E402

D = cc.DIMENSIONS


@pytest.mark.parametrize("a, b, out", [
    ("fail", "pass", "fail"), ("pass", "fail", "fail"), ("partial", "pass", "partial"),
    (None, "pass", "pass"), ("fail", None, "fail"), ("fail", "na", "fail"),
    (None, "na", "na"), ("pass", "na", "pass"), ("partial", "partial", "partial"),
])
def test_worse(a, b, out):
    assert cc.worse(a, b) == out


def test_kappa_perfect_and_chance():
    assert cc.weighted_kappa([("pass", "pass"), ("fail", "fail"), ("partial", "partial")]) == 1.0
    assert cc.weighted_kappa([]) is None
    k = cc.weighted_kappa([("pass", "fail"), ("fail", "pass"), ("pass", "pass"), ("fail", "fail")])
    assert k == pytest.approx(0.0)


def test_kappa_quadratic_penalises_distance():
    near = cc.weighted_kappa([("pass", "partial"), ("partial", "partial"), ("fail", "fail"), ("pass", "pass")])
    far = cc.weighted_kappa([("pass", "fail"), ("partial", "partial"), ("fail", "fail"), ("pass", "pass")])
    assert near > far


def item(sid, author, judge_reps, checks=None):
    """author/judge: dict dim -> verdict; missing dims default to pass."""
    a = {"dimensions": {d: {"verdict": author.get(d, "pass"), "auto_fail": False} for d in D}}
    judged = [{"error": None, "verified": {"dimensions": {d: {"verdict": j.get(d, "pass"),
               "findings": []} for d in D}}} for j in judge_reps]
    return {"sid": sid, "author": a, "judged": judged,
            "checks": checks or {"D2": {"verdict": None}, "D7": {"verdict": None}}}


def test_deterministic_check_rescues_a_judge_miss():
    items = [item("S1", {"D2": "fail"}, [{"D2": "pass"}], checks={"D2": {"verdict": "fail"}, "D7": {"verdict": None}})]
    res = cc.analyse(items, ["rep1"])
    assert res["combined"][0]["missed_fails"] == []
    assert res["judge_alone"][0]["missed_fails"] == [("S1", "D2")]
    assert res["thresholds"]["t1_no_missed_fails"] is True


def test_a_miss_on_any_repeat_fails_threshold_1():
    items = [item("S1", {"D6": "fail"}, [{"D6": "fail"}, {"D6": "pass"}, {"D6": "fail"}])]
    res = cc.analyse(items, ["rep1", "rep2", "rep3"])
    assert res["thresholds"]["t1_no_missed_fails"] is False
    assert res["self_disagreement"]["unstable"] == [("S1", "D6")]


def test_self_disagreement_rate_and_threshold_3():
    items = [item(f"S{i}", {}, [{}, {}, {}]) for i in range(10)]
    items[0]["judged"][1]["verified"]["dimensions"]["D3"]["verdict"] = "partial"
    res = cc.analyse(items, ["rep1", "rep2", "rep3"])
    assert res["self_disagreement"]["rate"] == pytest.approx(1 / 50)
    assert res["thresholds"]["t3_self"] is True


def test_na_pairs_are_left_out_of_kappa_and_counted():
    items = [item("S1", {"D8": "na"}, [{"D8": "pass"}]), item("S2", {"D2": "fail"}, [{"D2": "fail"}])]
    c = cc.analyse(items, ["rep1"])["combined"][0]
    assert c["kappa_n"] == 9 and c["na_mismatches"] == 1


def test_judge_error_counts_as_no_verdict():
    it = item("S1", {"D2": "fail"}, [{}])
    it["judged"][0] = {"error": {"code": "X"}, "verified": None}
    res = cc.analyse([it], ["rep1"])
    assert res["combined"][0]["missed_fails"] == [("S1", "D2")]


def test_all_thresholds_met_on_perfect_agreement():
    items = [item(f"S{i}", {"D2": "fail"}, [{"D2": "fail"}] * 3) for i in range(5)]
    items += [item(f"T{i}", {"D3": "partial"}, [{"D3": "partial"}] * 3) for i in range(3)]
    res = cc.analyse(items, ["rep1", "rep2", "rep3"])
    assert res["thresholds"]["met"] is True
