"""evals/haz03_check.py — HAZ-03: paediatric gold scenarios get PAEDIATRIC_LINE
verbatim and never the adult line, with age_group matching gold; adults the reverse.

Offline. Synthetic notes only. Scenario IDs only in test names and messages;
5b outputs are built from synthetic facts, never from note text."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import gold as G  # noqa: E402
import haz03_check as H  # noqa: E402
from pipeline.safety_net import (AB_PINNED_LABEL, ADULT_LINE, PAEDIATRIC_LINE,  # noqa: E402
                                 select_safety_net)

GOLD_IDS = sorted(p.stem for p in G.GOLD_DIR.glob("*.json"))
PAEDIATRIC_IDS = ["S4", "S8", "S9"]
ADULT_IDS = [s for s in GOLD_IDS if s not in PAEDIATRIC_IDS]
W4_PIPELINE = ROOT / "evals" / "runs" / "w4-pipeline-e06-x5"


def facts(age_group, advice=False):
    items = []
    if advice:
        items = [{"fact_id": "A1", "citation_status": "verified",
                  "cites": [{"lines": ["L001"], "quote": "Synthetic advice sentence.",
                             "verified": True}]}]
    return {"documented_advice": items, "age_group": age_group}


def run_5b(age_group, advice=False):
    return select_safety_net({"facts": facts(age_group, advice), "flags": []})


def check(sid, age_group=None, sn=None):
    gold = G.load(sid)
    ag = gold["age_group"]["value"] if age_group is None else age_group
    return H.check(gold, ag, sn if sn is not None else run_5b(ag))


# --- the corpus split the assertion depends on ----------------------------------

def test_gold_pins_exactly_s4_s8_s9_as_paediatric():
    paed = [s for s in GOLD_IDS if H.expected_audience(G.load(s)["age_group"]["value"]) == "paediatric"]
    assert paed == PAEDIATRIC_IDS
    assert len(GOLD_IDS) == 18


def test_the_check_uses_the_same_lines_as_step_5b():
    assert H.LINES == {"paediatric": PAEDIATRIC_LINE, "adult": ADULT_LINE}


# --- correct 5b output passes, both routes ----------------------------------------

@pytest.mark.parametrize("advice", [False, True], ids=["fallback", "documented"])
@pytest.mark.parametrize("sid", GOLD_IDS)
def test_correct_5b_output_passes(sid, advice):
    gold = G.load(sid)
    ag = gold["age_group"]["value"]
    r = H.check(gold, ag, run_5b(ag, advice))
    assert r["verdict"] == "pass", (sid, r["failures"])


# --- misclassified age group: both directions --------------------------------------

@pytest.mark.parametrize("sid", PAEDIATRIC_IDS)
def test_paediatric_scenario_read_as_adult_fails(sid):
    r = check(sid, age_group="adult")
    assert set(r["failures"]) == {"age_group_mismatch", "wrong_audience_label", "wrong_pinned_line",
                                  "expected_line_missing_part_c", "expected_line_missing_part_ab",
                                  "other_line_in_part_ab", "other_line_in_part_c"}


@pytest.mark.parametrize("sid", PAEDIATRIC_IDS)
def test_paediatric_scenario_read_as_not_documented_fails(sid):
    # not_documented falls back to the ADULT line in 5b — the HAZ-03 failure mode.
    r = check(sid, age_group="not_documented")
    assert "age_group_mismatch" in r["failures"] and "other_line_in_part_c" in r["failures"]


@pytest.mark.parametrize("sid", ["S1", "A5"])
def test_adult_scenario_read_as_child_fails(sid):
    r = check(sid, age_group="child")
    assert {"age_group_mismatch", "wrong_pinned_line", "other_line_in_part_c"} <= set(r["failures"])


def test_age_group_must_match_exactly_even_when_the_line_is_right():
    # S8 gold is neonate; read as infant, 5b still picks the paediatric line.
    r = check("S8", age_group="infant")
    assert r["failures"] == ["age_group_mismatch"]


def test_adult_read_as_not_documented_fails_on_age_group_only():
    r = check("S1", age_group="not_documented")
    assert r["failures"] == ["age_group_mismatch"]


# --- the text that reaches the patient, not just the field -------------------------

def test_right_field_but_adult_text_in_part_c_fails():
    # The gap in score_safety_net's audience_ok: it reads only pinned_line.
    sn = run_5b("child")
    sn["part_c"] = sn["part_c"].replace(PAEDIATRIC_LINE, ADULT_LINE)
    r = check("S9", sn=sn)
    assert set(r["failures"]) == {"expected_line_missing_part_c", "other_line_in_part_c"}


def test_both_lines_in_part_c_fails_even_with_the_right_one_present():
    sn = run_5b("infant")
    sn["part_c"] += "\n" + ADULT_LINE
    assert check("S4", sn=sn)["failures"] == ["other_line_in_part_c"]


def test_reformatted_wrong_line_is_still_caught():
    sn = run_5b("neonate", advice=True)
    sn["part_ab"] += "\n- **" + ADULT_LINE + "**"
    assert check("S8", sn=sn)["failures"] == ["other_line_in_part_ab"]


@pytest.mark.parametrize("advice", [False, True], ids=["fallback", "documented"])
def test_expected_line_missing_from_part_ab_fails(advice):
    # v2 (CSO 23 Oct 2026): part_ab must carry the audience's line on both routes.
    sn = run_5b("child", advice)
    sn["part_ab"] = sn["part_ab"].replace(PAEDIATRIC_LINE, "")
    assert check("S9", sn=sn)["failures"] == ["expected_line_missing_part_ab"]


def test_expected_line_in_part_ab_must_be_verbatim():
    sn = run_5b("adult", advice=True)
    sn["part_ab"] = sn["part_ab"].replace("NHS 111", "NHS  111")
    assert check("S1", sn=sn)["failures"] == ["expected_line_missing_part_ab"]


def test_label_alone_does_not_satisfy_part_ab():
    sn = run_5b("adult", advice=True)
    sn["part_ab"] = sn["part_ab"].split(AB_PINNED_LABEL)[0] + AB_PINNED_LABEL
    assert check("S1", sn=sn)["failures"] == ["expected_line_missing_part_ab"]


def test_result_names_check_version_2():
    assert check("S1")["check_version"] == 2


def test_paediatric_line_in_an_adult_part_c_fails():
    sn = run_5b("adult")
    sn["part_c"] += "\n" + PAEDIATRIC_LINE
    assert check("S1", sn=sn)["failures"] == ["other_line_in_part_c"]


def test_expected_line_must_be_verbatim():
    sn = run_5b("child")
    sn["part_c"] = sn["part_c"].replace("NHS 111", "NHS  111")
    assert check("S9", sn=sn)["failures"] == ["expected_line_missing_part_c"]


# --- records, and what the result may carry -----------------------------------------

def test_record_without_step_5b_is_not_scored():
    rec = {"scenario_id": "S4", "steps": {"validate_facts": {"facts": {"age_group": "infant"}}}}
    assert H.check_record(rec, G.load("S4"))["verdict"] == "not_scored"


def test_result_carries_no_output_text():
    sn = run_5b("child", advice=True)
    blob = json.dumps(check("S9", sn=sn))
    assert "Synthetic advice" not in blob and "NHS 111" not in blob


def _w4_records():
    return [json.loads(p.read_text()) for p in sorted(W4_PIPELINE.glob("r*/*.json"))]


@pytest.mark.skipif(not W4_PIPELINE.is_dir(), reason="saved W4 pipeline runs not present")
def test_saved_w4_records_fail_v2_only_on_part_ab_and_only_on_the_documented_route():
    # Made before the 23 Oct decision: the 48 documented-route records lack the line
    # in part_ab, and that is their ONLY failure; the 42 fall-back records pass.
    recs = _w4_records()
    results = [H.check_record(rec, G.load(rec["scenario_id"])) for rec in recs]
    assert len(results) == 90
    assert sum(r["expected_audience"] == "paediatric" for r in results) == 15
    routes = [rec["steps"]["select_safety_net"]["route"] for rec in recs]
    for route, r in zip(routes, results):
        if route == "documented":
            assert r["failures"] == ["expected_line_missing_part_ab"], r["scenario_id"]
        else:
            assert r["verdict"] == "pass", (r["scenario_id"], r["failures"])
    assert routes.count("documented") == 48


@pytest.mark.skipif(not W4_PIPELINE.is_dir(), reason="saved W4 pipeline runs not present")
def test_saved_w4_facts_through_current_5b_all_pass_v2():
    # The offline re-run in miniature: step-3 facts + step-1 flags from each saved
    # record, through today's 5b. No Bedrock, nothing written.
    for rec in _w4_records():
        steps = rec["steps"]
        sn = select_safety_net({"facts": steps["validate_facts"]["facts"],
                                "flags": steps["guard_input"]["flags"]})
        assert sn["route"] == steps["select_safety_net"]["route"], rec["scenario_id"]
        r = H.check(G.load(rec["scenario_id"]), steps["validate_facts"]["facts"]["age_group"], sn)
        assert r["verdict"] == "pass", (rec["scenario_id"], r["failures"])
