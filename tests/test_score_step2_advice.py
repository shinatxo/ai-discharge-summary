"""evals/score_step2_advice.py — step 2's documented advice vs gold: item recall,
precision and route, at the extracted and verified stages (W5).

Offline. Synthetic only: items are built from line IDs, with placeholder values,
never note text. Scenario IDs only in test names and messages."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import gold as G  # noqa: E402
import score_step2_advice as M  # noqa: E402

GOLD_IDS = sorted(p.stem for p in G.GOLD_DIR.glob("*.json"))
DOCUMENTED = [s for s in GOLD_IDS if G.load(s)["safety_net"]["route"] == "documented"]
FALLBACK = [s for s in GOLD_IDS if s not in DOCUMENTED]
W4_PIPELINE = ROOT / "evals" / "runs" / "w4-pipeline-e06-x5"


def item(lines, verified=True, n=0):
    return {"fact_id": f"documented_advice.{n}", "value": "placeholder",
            "citation_status": "verified" if verified else "citation_unverified",
            "cites": [{"lines": list(lines), "quote": "placeholder", "verified": verified}]}


def gold_line_sets(sid):
    return [sorted({l for c in a["cites"] for l in c["lines"]}) for a in G.load(sid)["safety_net"]["advice"]]


def score(sid, items):
    return M.score(G.load(sid), {"documented_advice": items})


# --- the corpus split -------------------------------------------------------------

def test_gold_has_ten_documented_and_eight_fallback_scenarios():
    assert len(DOCUMENTED) == 10 and len(FALLBACK) == 8


# --- exact extraction ---------------------------------------------------------------

@pytest.mark.parametrize("sid", DOCUMENTED)
def test_one_item_per_gold_item_scores_full_recall_and_precision(sid):
    r = score(sid, [item(ls, n=i) for i, ls in enumerate(gold_line_sets(sid))])
    for stage in ("extracted", "verified"):
        assert r[stage]["recall"] == 1.0 and r[stage]["precision"] == 1.0, (sid, stage)
    assert r["route_correct"] and r["route_error"] is None and not r["right_route_wrong_item"]


@pytest.mark.parametrize("sid", FALLBACK)
def test_fallback_scenario_with_nothing_extracted_is_correct_with_no_ratios(sid):
    r = score(sid, [])
    assert r["route_correct"] and r["verified"]["route"] == "fallback"
    assert r["verified"]["recall"] is None and r["verified"]["precision"] is None


# --- the three route errors, and the right route for the wrong reason ---------------

@pytest.mark.parametrize("sid", ["S9", "S15"])
def test_documented_scenario_with_nothing_extracted_is_not_extracted(sid):
    r = score(sid, [])
    assert r["route_error"] == "not_extracted" and r["verified"]["recall"] == 0.0


def test_items_that_all_fail_verification_are_lost_at_verification():
    r = score("S3", [item(["L099"], verified=False)])
    assert r["route_error"] == "lost_at_verification"
    assert r["extracted"]["route"] == "documented" and r["verified"]["route"] == "fallback"


@pytest.mark.parametrize("sid", ["S16", "C7"])
def test_verified_item_in_a_fallback_scenario_is_non_advice_as_advice(sid):
    r = score(sid, [item(["L005"])])
    assert r["route_error"] == "non_advice_as_advice" and r["verified"]["precision"] == 0.0


def test_unverified_item_in_a_fallback_scenario_is_caught_by_step_3():
    r = score("S1", [item(["L005"], verified=False)])
    assert r["route_correct"]
    assert r["extracted"]["precision"] == 0.0 and r["verified"]["precision"] is None


def test_verified_item_on_a_non_gold_line_is_the_right_route_for_the_wrong_reason():
    r = score("S10", [item(["L002"])])
    assert r["route_correct"] and r["right_route_wrong_item"]
    assert r["verified"]["recall"] == 0.0 and r["verified"]["precision"] == 0.0


# --- the matching rule ----------------------------------------------------------------

def test_partial_extraction_gives_partial_recall():
    r = score("S3", [item(["L018"])])                    # gold: L018, L022
    assert r["verified"]["recall"] == 0.5 and r["verified"]["precision"] == 1.0


def test_a_span_overlapping_the_gold_line_matches():
    r = score("S3", [item(["L021", "L022"]), item(["L018"], n=1)])
    assert r["verified"]["recall"] == 1.0


def test_one_item_can_match_two_gold_items_on_the_same_line():
    r = score("S2", [item(["L012"])])                    # gold: two items, both L012
    assert r["verified"]["n_gold_matched"] == 2 and r["verified"]["recall"] == 1.0


def test_an_extra_non_gold_item_lowers_precision_not_recall():
    r = score("S10", [item(["L010"]), item(["L003"], n=1)])
    assert r["verified"]["recall"] == 1.0 and r["verified"]["precision"] == 0.5


def test_item_marked_verified_with_an_unverified_cite_is_not_verified():
    it = item(["L010"])
    it["cites"].append({"lines": ["L011"], "quote": "placeholder", "verified": False})
    r = score("S10", [it])
    assert r["extracted"]["n_items"] == 1 and r["verified"]["n_items"] == 0
    assert r["route_error"] == "lost_at_verification"


# --- records, summaries, and what the result may carry ---------------------------------

def test_record_without_step_3_facts_is_not_scored_and_left_out_of_the_summary():
    rec = {"scenario_id": "S10", "steps": {"guard_input": {"flags": []}}}
    r = M.score_record(rec, G.load("S10"))
    assert r["not_scored"] == "no_step_3_facts"
    s = M.summarise([r, score("S10", [item(["L010"])])])
    assert s["n_records"] == 2 and s["n_scored"] == 1 and s["route_correct"] == 1


def test_summary_pools_items_not_ratios():
    s = M.summarise([score("S3", [item(["L018"])]),                       # 1/2 gold
                     score("S10", [item(["L010"]), item(["L003"], n=1)])])  # 1/1 gold, 1/2 items
    assert s["verified"]["recall"] == 2 / 3 and s["verified"]["precision"] == 2 / 3


def test_result_carries_no_text():
    blob = json.dumps(score("S3", [item(["L018"])]))
    assert "placeholder" not in blob


# --- the saved W4 pipeline batch ---------------------------------------------------------

def _w4():
    out = []
    for p in sorted(W4_PIPELINE.glob("r*/*.json")):
        rec = json.loads(p.read_text())
        out.append((rec, M.score_record(rec, G.load(rec["scenario_id"]))))
    return out


@pytest.mark.skipif(not W4_PIPELINE.is_dir(), reason="saved W4 pipeline runs not present")
def test_w4_route_errors_reproduce_the_recorded_74_of_90():
    # EVAL_RESULTS.md run log, 2026-10-07: 74/90; non-advice as advice S16 4/5, C7 3/5;
    # wrong-line citation excluded S3 3/5; not extracted S9 3/5, S15 2/5, S3 1/5.
    results = [r for _, r in _w4()]
    s = M.summarise(results)
    assert s["n_scored"] == 90 and s["route_correct"] == 74
    assert s["route_errors"] == {"not_extracted": 6, "lost_at_verification": 3,
                                 "non_advice_as_advice": 7}
    by = {}
    for r in results:
        if r["route_error"]:
            by[(r["scenario_id"], r["route_error"])] = by.get((r["scenario_id"], r["route_error"]), 0) + 1
    assert by == {("S16", "non_advice_as_advice"): 4, ("C7", "non_advice_as_advice"): 3,
                  ("S3", "lost_at_verification"): 3, ("S3", "not_extracted"): 1,
                  ("S9", "not_extracted"): 3, ("S15", "not_extracted"): 2}


@pytest.mark.skipif(not W4_PIPELINE.is_dir(), reason="saved W4 pipeline runs not present")
def test_w4_item_counts_are_pinned():
    s = M.summarise([r for _, r in _w4()])
    assert (s["extracted"]["n_items"], s["verified"]["n_items"], s["verified"]["n_gold"]) == (61, 56, 85)
    assert s["verified"]["recall"] == 56 / 85 and s["verified"]["precision"] == 48 / 56
    assert s["right_route_wrong_item"] == 0


@pytest.mark.skipif(not W4_PIPELINE.is_dir(), reason="saved W4 pipeline runs not present")
def test_verified_route_is_the_route_5b_took_on_every_w4_record():
    for rec, r in _w4():
        assert r["verified"]["route"] == rec["steps"]["select_safety_net"]["route"], rec["scenario_id"]
