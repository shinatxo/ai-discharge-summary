"""evals/score_patient.py — step 7's pass bar, one test per bar line against its
WRITTEN definition (1 Nov 2026, rounds 1-2 of step build piece 3), on synthetic
records built by the real steps where it matters. No AWS."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "tests"))

from pipeline import compose_patient as P  # noqa: E402
from pipeline import render as R  # noqa: E402
from pipeline.safety_net import ADULT_LINE, PAEDIATRIC_LINE  # noqa: E402
import score_patient as SP  # noqa: E402
from test_pipeline_compose_patient import FakeBedrock, _ctx, _response, _step6  # noqa: E402
from pipeline import run_step  # noqa: E402

NOTES = "Day 1 (16/05) 64M chest pain. Dx NSTEMI.\nCardiology clinic 6 weeks.\nHome."


def _good_c():
    return {"sections": [
        {"heading": "why_in_hospital", "source_ids": ["sections.0", "sections.1"],
         "text": "You came in with chest pain. You had a heart attack (NSTEMI)."},
        {"heading": "what_we_did", "source_ids": ["sections.2", "sections.3"],
         "text": "Blood tests showed strain on your heart. The team will treat this with medicines."},
        {"heading": "medicines", "source_ids": [], "text": "{{MEDICATIONS}}"},
        {"heading": "next_steps", "source_ids": ["sections.9"], "text": "You will see the heart team in 6 weeks."},
        {"heading": "safety_net", "source_ids": [], "text": "{{SAFETY_NET}}"},
    ]}


def record(payload=None, sid="S1", age="adult", language=(), latency=(15.0, 8.0, 7.0, 6.0), five_a=True):
    s6 = _step6(age_group=age)
    step7 = run_step("compose_patient", P.patient_input(s6),
                     ctx=_ctx(FakeBedrock(_response(payload or _good_c()))))
    line = PAEDIATRIC_LINE if age in ("child", "infant", "neonate") else ADULT_LINE
    steps = {
        "validate_facts": {"facts": {"medications": {
            "pre_admission": [{"value": "bisoprolol 2.5mg OD"}], "discharge": [{"value": "bisoprolol 5mg OD"}]}}},
        "retrieve_evidence": {"language": [{"line": "L002"}] if language else []},
        "select_safety_net": {"part_c": line, "part_ab": line},
        "compose_clinical": s6,
        "compose_patient": step7,
    }
    if five_a:
        steps["reconcile_medications"] = {"discharge_status": "listed", "reconciliation": [{
            "drug": "bisoprolol", "tag": "increased", "basis": "documented", "dose": "5mg", "route": None,
            "frequency": "OD", "previous": {"dose": "2.5mg", "frequency": "OD"}, "conflict": False,
            "citation_status": "verified"}]}
    names = ("extract_facts", "reconcile_medications", "compose_clinical", "compose_patient")
    return {"generation_id": f"b/r1/{sid}", "scenario_id": sid, "run": 1, "steps": steps,
            "calls": [{"name": n, "latency_s": x} for n, x in zip(names, latency)]}


def gold(age="adult", sid="S1"):
    return {"scenario_id": sid, "age_group": {"value": age}}


def score(rec, age="adult", rulings=None, notes=NOTES):
    r = SP.score_record(rec, notes, gold(age, rec["scenario_id"]), rulings or {})
    r.update({"scenario_id": rec["scenario_id"], "run": rec["run"], "generation_id": rec["generation_id"]})
    return r


def bar_of(results, n=None):
    sm = SP.summarise(results, [], n if n is not None else len(results))
    return {line.split(" ", 1)[0]: (res, verdict) for line, res, verdict in SP.bar(sm)}, sm


def _with(heading, text=None, ids=None):
    p = _good_c()
    for s in p["sections"]:
        if s["heading"] == heading:
            if text is not None:
                s["text"] = text
            if ids is not None:
                s["source_ids"] = ids
    return p


def test_a_good_record_meets_every_line():
    b, sm = bar_of([score(record())])
    assert {k: v for k, (_r, v) in b.items()} == {k: "MET" for k in ("1", "2", "3", "4", "5", "6", "7", "8", "9",
                                                                      "11", "12")}
    assert sm["gate"]["pass"] == 1 and sm["haz03_c"] == 1 and sm["title_ok"] == 1


def test_line_1_counts_records_not_reached():
    b, _ = bar_of([score(record())], n=2)
    assert b["1"] == ("1/2", "NOT MET")


def test_line_2_boxes_and_unknown_sources():
    assert bar_of([score(record(_with("medicines", "{{MEDICATIONS}} x")))])[0]["2"][1] == "NOT MET"
    assert bar_of([score(record(_with("why_in_hospital", ids=["sections.77"])))])[0]["2"][1] == "NOT MET"


def test_line_3_signpost_outside_box_and_the_gate():
    b, sm = bar_of([score(record(_with("next_steps", "Call 999 if it hurts.")))])
    assert b["3"][1] == "NOT MET" and sm["signpost_outside_box"] == 1 and sm["gate"]["pass"] == 0


def test_line_3_needs_an_assembled_leaflet():
    assert bar_of([score(record(five_a=False))])[0]["3"][1] == "NOT MET"


def test_line_4_advice_is_pending_then_ruled():
    rec = record(_with("next_steps", "The team asked you not to drive. You will see the heart team in 6 weeks."))
    r = score(rec)
    keys = [x["key"] for x in r["review"] if x["kind"] == "advice"]
    assert len(keys) == 1
    assert bar_of([r])[0]["4"][1] == "PENDING REVIEW"
    assert bar_of([score(rec, rulings={keys[0]: "acceptable"})])[0]["4"][1] == "MET"
    assert bar_of([score(rec, rulings={keys[0]: "defect"})])[0]["4"][1] == "NOT MET"


def test_a_bad_ruling_value_is_refused():
    rec = record(_with("next_steps", "Please rest."))
    key = score(rec)["review"][0]["key"]
    with pytest.raises(ValueError):
        score(rec, rulings={key: "fine"})


def test_line_5_resus_decides_and_doses_and_drugs_are_ruled():
    assert bar_of([score(record(_with("why_in_hospital", "You are for CPR.")))])[0]["5"][1] == "NOT MET"
    r = score(record(_with("what_we_did", "Blood tests showed strain. Take bisoprolol 5mg once a day.")))
    kinds = sorted(x["kind"] for x in r["review"])
    assert "medication" in kinds and "drug" in kinds and bar_of([r])[0]["5"][1] == "PENDING REVIEW"


def test_line_6_years_decide_numbers_and_units_are_ruled():
    assert bar_of([score(record(_with("next_steps", "Clinic on 16/05/2026.")))])[0]["6"][1] == "NOT MET"
    r = score(record(_with("next_steps", "You will see the heart team in 8 weeks.")))
    assert [x["token"] for x in r["review"] if x["kind"] == "number"] == ["8"]
    assert bar_of([r])[0]["6"][1] == "PENDING REVIEW"


def test_line_7_fk_on_prose_only():
    long = ("Electrophysiological investigations demonstrated considerable myocardial dysfunction "
            "necessitating comprehensive pharmacological optimisation and multidisciplinary consideration.")
    b, sm = bar_of([score(record(_with("what_we_did", long, ids=["sections.2", "sections.3"])))])
    assert b["7"][1] == "NOT MET" and sm["fk"]["prose_max"] > 8
    rec = record()
    assert score(rec)["fk_prose"] == SP.RA.fk_grade(SP.prose(rec["steps"]["compose_patient"]))
    assert "{{" not in SP.prose(rec["steps"]["compose_patient"])            # boxes excluded


def test_line_8_header_where_expected_and_nowhere_else():
    assert bar_of([score(record(sid="C7", language=True))])[0]["8"][1] == "MET"
    assert bar_of([score(record(sid="C7"))])[0]["8"][1] == "NOT MET"          # expected, missing
    assert bar_of([score(record(sid="S1", language=True))])[0]["8"][1] == "NOT MET"   # unexpected


def test_line_8_non_english_sentence_decides():
    b, sm = bar_of([score(record(_with("next_steps", "Wizyta w poradni za 6 tygodni, proszę przyjść.")))])
    assert sm["non_english"]["decided"] >= 1 and b["8"][1] == "NOT MET"


def test_line_9_title_audience_follows_gold():
    b, _ = bar_of([score(record(age="adult"), age="child")])        # facts said adult, gold says child
    assert b["9"][1] == "NOT MET"
    assert bar_of([score(record(age="child"), age="child")])[0]["9"][1] == "MET"


def test_line_9_not_documented_and_follow_up_missing():
    assert bar_of([score(record(_with("next_steps", "Not documented.")))])[0]["9"][1] == "NOT MET"
    assert bar_of([score(record(_with("next_steps", "You can go home.", ids=[])))])[0]["9"][1] == "NOT MET"


def test_line_9_sensitive_is_ruled():
    r = score(record(_with("why_in_hospital", "You had a heart attack (NSTEMI). The police were called.")))
    assert any(x["kind"] == "sensitive" for x in r["review"]) and bar_of([r])[0]["9"][1] == "PENDING REVIEW"


def test_line_11_haz03_on_assembled_c():
    rec = record()
    rec["steps"]["select_safety_net"]["part_c"] = PAEDIATRIC_LINE
    assert bar_of([score(rec)])[0]["11"][1] == "NOT MET"


def test_line_12_watch_line_on_step_7_start():
    b, sm = bar_of([score(record(latency=(150.0, 30.0, 55.0, 20.0)))])
    assert b["12"][1] == "NOT MET" and sm["latency"]["start7_max"] == 205.0
    assert sm["latency"]["critical_max"] == 225.0


def test_risk_section_is_counted_but_never_given():
    r = score(record())
    assert r["risk_sections_in_a"] == 1
    assert "self-harm" not in P.user_text(SP.given_input(record()))


def test_results_carry_no_sentences():
    r = score(record(_with("next_steps", "Please rest. Call 999 if worse in 8 weeks.")))
    assert all("sentence" not in x for x in r["review"])
    assert "Please rest" not in json.dumps(r)


def test_review_file_quotes_and_report_files(tmp_path):
    rec = record(_with("next_steps", "Please rest. You will see the heart team in 6 weeks."))
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "S1.json").write_text(json.dumps(rec))
    res = [score(rec)]
    scored = {"batch_id": "b", "patient": {"metric": "compose_patient", "metric_version": SP.METRIC_VERSION,
                                           "results": res, "summary": SP.summarise(res, [], 1)}}
    SP.write_reports(tmp_path, scored, tmp_path)
    assert "| 1 Reached step 7" in (tmp_path / "PATIENT_VERSION.md").read_text()
    rv = (tmp_path / "review_patient.md").read_text()
    assert "Please rest." in rv and "PENDING" in rv and "(bar 4)" in rv
    assert json.loads((tmp_path / "patient_version.json").read_text())["metric"] == "compose_patient"


def test_translation_scenarios_are_pre_declared():
    assert SP.TRANSLATION_SCENARIOS == ("C7",) and SP.METRIC_VERSION == 1
