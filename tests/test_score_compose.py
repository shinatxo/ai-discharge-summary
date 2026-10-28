"""evals/score_compose.py — step 6's step-level scoring (WS1a step build piece 2).
Records are built from test_pipeline_compose's invented notes; no AWS."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "tests"))

import score_compose as CP  # noqa: E402
import test_pipeline_compose as T  # noqa: E402


def _record(payload=None, latency=(14.0, 9.0, 27.0)):
    facts = T._facts()
    out = T._run(payload, facts=facts)
    rec = {"steps": {"validate_facts": {"facts": facts},
                     "reconcile_medications": {"reconciliation": [{"drug": "aspirin"}]},
                     "compose_clinical": out},
           "calls": [{"name": n, "latency_s": s} for n, s in
                     zip(("extract_facts", "reconcile_medications", "compose_clinical"), latency)]}
    return rec


def _score(payload=None, rulings=None, **kw):
    return CP.score_record(_record(payload, **kw), T.NOTES, rulings or {})


def _kinds(r):
    return sorted((x["kind"], x["heading"], x["token"]) for x in r["review"])


def test_clean_composition_has_nothing_to_rule_on():
    r = _score()
    assert r["review"] == [] and r["years_vs_notes"] == [] and r["box_problems"] == 0
    assert r["latency"] == {"step2_s": 14.0, "step5a_s": 9.0, "step6_s": 27.0, "critical_s": 41.0}


@pytest.mark.parametrize("heading, part, text, expected", [
    ("follow_up", "part_a", "Clinic in 6 weeks. Return to ED if pain recurs.", [("advice", "follow_up", None)]),
    ("gp_actions", "part_b", "Please call 999 if needed.", [("advice", "gp_actions", None)]),
    ("opening", "part_b", "He presented to A&E with chest pain.", [("advice", "opening", None)]),   # CSO rules
    ("admission_summary", "part_b", "He remained for CPR.", [("resus", "admission_summary", None)]),
    ("follow_up", "part_a", "Continue aspirin; clinic in 6 weeks.", [("medication", "follow_up", "aspirin")]),
    ("follow_up", "part_a", "Re-present if pain recurs.", [("advice", "follow_up", None)]),
    ("follow_up", "part_a", "Seek review if the wound worsens.", [("advice", "follow_up", None)]),
    ("admission_summary", "part_b", "This represents worsening renal function.", []),   # not advice
])
def test_review_items_flag_sentences_outside_the_boxes(heading, part, text, expected):
    p = T._good()
    T._sec(p, part, heading).update(text=text)
    assert _kinds(_score(p)) == sorted(expected)


def test_treatment_and_contradiction_sections_may_name_a_drug():
    p = T._good()
    T._sec(p, "part_a", "treatment").update(text="Loaded with aspirin. Medical management.")
    T._sec(p, "part_a", "allergies").update(text="NKDA. Notes conflict: aspirin? — requires clarification.")
    assert _score(p)["review"] == []


def test_boxes_are_never_read():
    assert CP.review_items(_record()) == []


def test_rulings_decide_and_unruled_is_pending():
    p = T._good()
    T._sec(p, "part_b", "opening").update(text="He presented to A&E with chest pain.")
    key = _score(p)["review"][0]["key"]
    assert _score(p)["review"][0]["ruling"] is None
    assert _score(p, {key: "acceptable"})["review"][0]["ruling"] == "acceptable"
    with pytest.raises(ValueError):
        _score(p, {key: "fine"})


def test_the_review_key_ignores_whitespace_and_is_stable():
    a, b = T._good(), T._good()
    T._sec(a, "part_b", "opening").update(text="He presented to A&E.")
    T._sec(b, "part_b", "opening").update(text="He  presented   to A&E.")
    ka, kb = _score(a)["review"][0]["key"], _score(b)["review"][0]["key"]
    assert ka == kb and ka == _score(a)["review"][0]["key"] and len(ka) == 16


def test_years_vs_notes_are_attributed():
    p = T._good()
    T._sec(p, "part_a", "presenting_complaint").update(text="Chest pain on 16/05/2025.")
    r = _score(p)
    assert r["years_vs_notes"] == [{"part": "part_a", "heading": "presenting_complaint",
                                    "token": "2025", "source": "step_6"}]
    rec = _record(p)
    rec["steps"]["validate_facts"]["facts"]["fields"]["admission_date"]["items"][0]["value"] = "16/05/2025"
    r2 = CP.score_record(rec, T.NOTES, {})
    assert r2["years_vs_notes"][0]["source"] == "step_2"            # step 2 put it in the value


def _sm(results, n=None):
    return CP.summarise(results, [], n if n is not None else len(results))


def test_bar_all_met_on_clean_records():
    verdicts = [v for _l, _r, v in CP.bar(_sm([_score(), _score()]))]
    assert verdicts == ["MET"] * 8


def test_bar_pending_then_met_or_not_met_by_ruling():
    p = T._good()
    T._sec(p, "part_b", "opening").update(text="He presented to A&E with chest pain.")
    key = _score(p)["review"][0]["key"]
    line5 = lambda rs: CP.bar(_sm([_score(p, rs)]))[5][2]  # noqa: E731
    assert line5({}) == "PENDING REVIEW"
    assert line5({key: "acceptable"}) == "MET"
    assert line5({key: "defect"}) == "NOT MET"


def test_bar_not_met_on_a_year_a_box_a_missing_contradiction_or_a_skip():
    p = T._good()
    T._sec(p, "part_a", "presenting_complaint").update(text="Chest pain on 16/05/2025.")
    T._sec(p, "part_a", "medications").update(text="aspirin")
    T._sec(p, "part_a", "allergies").update(fact_ids=["allergies.0"], text="NKDA.")
    v = {line.split(" ", 1)[0]: verdict for line, _r, verdict in CP.bar(_sm([_score(p)]))}
    assert (v["2"], v["3a"], v["4"]) == ("NOT MET", "NOT MET", "NOT MET")
    assert CP.bar(_sm([_score()], n=2))[0][2] == "NOT MET"           # one record never reached step 6


def test_latency_watch_counts_over_200_s():
    sm = _sm([_score(latency=(120.0, 50.0, 90.0)), _score()])
    assert sm["latency"]["critical_over_watch"] == 1 and sm["latency"]["critical_max"] == 210.0


def test_results_hold_no_sentence():
    p = T._good()
    T._sec(p, "part_a", "follow_up").update(text="Clinic. Return to ED if pain recurs.")
    r = _score(p)
    assert all("sentence" not in x for x in r["review"])
    assert "Return to ED" not in str(r)
