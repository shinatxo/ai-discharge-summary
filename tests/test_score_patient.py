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
                                                                      "11", "12", "13", "14")}
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
    assert SP.TRANSLATION_SCENARIOS == ("C7",) and SP.METRIC_VERSION == 4      # v4, 9 Nov 2026 (was 3)


# --- metric v2 (2 Nov 2026, F1-F5): line 13, promise (report only), refusals ------

def test_line_13_sourceless_prose_decides():
    b, sm = bar_of([score(record(_with("why_in_hospital", ids=[])))])
    assert b["13"] == ("1", "NOT MET") and sm["prose_without_source"] == 1
    b, _ = bar_of([score(record(_with("why_in_hospital", ids=["sections.77"])))])
    assert b["13"][1] == "NOT MET"                    # an unknown id is not a given source


def test_line_13_is_computed_from_the_sections_not_the_steps_check():
    # an older record (p0.1, before the check existed) is measured too
    rec = record(_with("why_in_hospital", ids=[]))
    rec["steps"]["compose_patient"]["checks"] = []
    assert bar_of([score(rec)])[0]["13"][1] == "NOT MET"


def test_promise_is_flagged_but_moves_no_bar_line():
    rec = record(_with("next_steps", "A nurse will visit you at home. You will see the heart team in 6 weeks."))
    r = score(rec)
    keys = [x["key"] for x in r["review"] if x["kind"] == "promise"]
    assert len(keys) == 1
    b, sm = bar_of([r])
    assert all(v == "MET" for _r, v in b.values()) and sm["review"]["promise"]["pending"] == 1
    r = score(rec, rulings={keys[0]: "defect"})
    b, sm = bar_of([r])
    assert all(v == "MET" for _r, v in b.values()) and sm["review"]["promise"]["defect"] == 1


def test_refusals_are_counted_and_line_1_counts_them_as_not_reached():
    sm = SP.summarise([score(record())], [{"generation_id": "b/r1/S12", "error_code": SP.REFUSED}], 2)
    assert sm["refused_no_sections"] == 1
    assert dict((ln.split(" ", 1)[0], v) for ln, _r, v in SP.bar(sm))["1"] == "NOT MET"


def test_review_file_marks_promise_as_no_bar_and_states_the_sensitive_scope(tmp_path):
    rec = record(_with("next_steps", "A nurse will visit you at home. You will see the heart team in 6 weeks."))
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "S1.json").write_text(json.dumps(rec))
    res = [score(rec)]
    scored = {"batch_id": "b", "patient": {"metric": "compose_patient", "metric_version": SP.METRIC_VERSION,
                                           "results": res, "summary": SP.summarise(res, [], 1)}}
    SP.write_reports(tmp_path, scored, tmp_path)
    rv = (tmp_path / "review_patient.md").read_text()
    assert "promise (no bar)" in rv and "suicidal thoughts included, is not withheld-category" in rv
    pv = (tmp_path / "PATIENT_VERSION.md").read_text()
    assert "| 13 Step-7 prose sections" in pv and "no_sections_given" in pv


# --- v3 (3 Nov 2026, CSO G2 A): bar line 14, meaning flags ------------------------

def test_line_14_meaning_flags_are_pending_then_ruled():
    rec = record(_with("what_we_did", "Blood tests showed your heart has fully recovered."))
    r = score(rec)
    rows = [x for x in r["review"] if x["kind"] in SP.MEANING_KINDS]
    assert {(x["kind"], x["token"]) for x in rows} >= {("certainty", "fully"), ("certainty", "recovered")}
    b, _ = bar_of([r])
    assert b["14"][1] == "PENDING REVIEW"
    assert all(v == "MET" for k, (_r, v) in b.items() if k != "14")         # no other line moves
    ruled = {x["key"]: "acceptable" for x in rows}
    assert bar_of([score(rec, rulings=ruled)])[0]["14"][1] == "MET"
    ruled[rows[0]["key"]] = "defect"
    assert bar_of([score(rec, rulings=ruled)])[0]["14"][1] == "NOT MET"


def test_line_14_is_computed_from_the_sections_not_the_steps_check():
    # a p0.2-era record carries none of the v3 checks; it is measured all the same
    rec = record(_with("what_we_did", "A swab showed strain on your heart."))
    rec["steps"]["compose_patient"]["checks"] = []
    r = score(rec)
    assert ("test_term", "swab") in {(x["kind"], x["token"]) for x in r["review"]}
    assert bar_of([r])[0]["14"][1] == "PENDING REVIEW"


def test_line_14_counts_an_unreached_diagnosis():
    r = score(record(_with("why_in_hospital", "You came in with chest pain.")))
    rows = [x for x in r["review"] if x["kind"] == "diagnosis"]
    assert len(rows) == 1 and rows[0]["heading"] == "why_in_hospital" and rows[0]["token"] is None


def test_meaning_kinds_map_to_line_14_and_stay_out_of_other_checks():
    assert SP.MEANING_KINDS == ("cause", "certainty", "qualifier", "test_term", "diagnosis")
    assert all(SP.BAR_OF[k] == 14 for k in SP.MEANING_KINDS)
    sm = SP.summarise([score(record())], [], 1)
    assert not set(sm["other_checks"]) & set(SP.MN.CHECK_OF.values())


def test_review_file_states_the_line_14_standard(tmp_path):
    rec = record(_with("what_we_did", "Blood tests showed your heart is fine."))
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "S1.json").write_text(json.dumps(rec))
    res = [score(rec)]
    scored = {"batch_id": "b", "patient": {"metric": "compose_patient", "metric_version": SP.METRIC_VERSION,
                                           "summary": SP.summarise(res, [], 1), "results": res}}
    SP.write_reports(tmp_path, scored, tmp_path)
    review = (tmp_path / "review_patient.md").read_text()
    assert "(bar 14, written 3 Nov 2026 before the p0.3 batch)" in review and "certainty `fine` (bar 14)" in review
    pv = (tmp_path / "PATIENT_VERSION.md").read_text()
    assert "| 14 Meaning flags" in pv and "## Per scenario — meaning flags (bar line 14, v4)" in pv   # was v3


def test_line_14_diagnosis_key_is_per_leaflet():
    # K-A (4 Nov 2026): the same unreached line in two different leaflets gets two keys
    a = score(record(_with("why_in_hospital", "You came in with chest pain.")))
    b = score(record(_with("why_in_hospital", "You came in with pain in your chest.")))
    c = score(record(_with("why_in_hospital", "You came in with chest pain.")))
    key = lambda r: [x["key"] for x in r["review"] if x["kind"] == "diagnosis"]  # noqa: E731
    assert key(a) != key(b) and key(a) == key(c) and len(key(a)) == 1


# --- v4 (9 Nov 2026): flags change, the verdict rule does not (Codex 20:57:39) ----------

def test_v4_a_retained_defect_with_a_new_unruled_flag_is_not_met():
    r = score(record(_with("what_we_did", "Blood tests showed your heart is fine. There was no sign of damage.")))
    rows = {(x["kind"], x["token"]): x["key"] for x in r["review"]}
    assert ("certainty", "fine") in rows and ("certainty", "damage") in rows        # damage: v4
    ruled = score(record(_with("what_we_did", "Blood tests showed your heart is fine. There was no sign of damage.")),
                  rulings={rows[("certainty", "fine")]: "defect"})
    b, sm = bar_of([ruled])
    assert sm["review"]["certainty"]["defect"] == 1 and sm["review"]["certainty"]["pending"] >= 1
    assert b["14"][1] == "NOT MET"                                  # a defect wins over pending
    all_acc = {x["key"]: "acceptable" for x in r["review"] if x["kind"] in SP.MEANING_KINDS
               and x["key"] != rows[("certainty", "damage")]}
    b, _ = bar_of([score(record(_with("what_we_did", "Blood tests showed your heart is fine. "
                                      "There was no sign of damage.")), rulings=all_acc)])
    assert b["14"][1] == "PENDING REVIEW"                           # nothing a defect, one pending


def test_v4_a_new_risk_flag_is_line_9_and_leaves_it_pending():
    r = score(record(_with("next_steps", "You will see the heart team in 6 weeks. The risk of this is low.")))
    sens = [x for x in r["review"] if x["kind"] == "sensitive"]
    assert len(sens) == 1 and SP.BAR_OF["sensitive"] == 9
    assert bar_of([r])[0]["9"][1] == "PENDING REVIEW"


def test_v4_will_be_told_is_promise_and_report_only():
    r = score(record(_with("next_steps", "You will see the heart team in 6 weeks. You will be told the plan.")))
    assert [x["kind"] for x in r["review"] if x["kind"] == "promise"] == ["promise"] and SP.BAR_OF["promise"] is None
