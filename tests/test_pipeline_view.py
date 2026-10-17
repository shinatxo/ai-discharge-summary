"""evals/pipeline_view.py — the pipeline's judgeable view at steps 1–5b (W5).

Synthetic records written for these tests only — no corpus text, and never
note text in test names or messages."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import pipeline_view as pv  # noqa: E402
import rubric_checks as rc  # noqa: E402


def item(value, lines=("L001",), status="verified"):
    return {"value": value, "cites": [{"lines": list(lines), "quote": "q", "verified": True, "reason": None}],
            "citation_status": status, "reason": None, "fact_id": "f"}


def field(status="not_documented", items=()):
    return {"status": status, "items": list(items), "field_check": "ok"}


def record(**over):
    fields = {name: field() for name in pv.FIELD_LABELS}
    fields["allergies"] = field("documented", [item("Penicillin allergy (rash)", ("L004",))])
    fields["treatment"] = field("documented", [item("Oral antibiotics", ("L005",)),
                                               item("IV fluids", ("L006", "L007"))])
    rec = {"generator": "pipeline", "error": None, "failed_step": None, "output": None,
           "steps": {
               "validate_facts": {"facts": {
                   "fields": fields, "age_group": "adult",
                   "medications": {"discharge_status": "listed", "medications_check": "ok",
                                   "discharge": [item("Amoxicillin 500 mg TDS", ("L008",))],
                                   "pre_admission": []},
                   "resus": {}, "documented_advice": [], "contradictions": [], "suspicious_text": []}},
               "route_resus": {"rendered": "RESUSCITATION STATUS\nNot documented.\n"},
               "select_safety_net": {"part_ab": "PINNED AB LINE", "part_c": "PINNED C LINE"}}}
    rec.update(over)
    return rec


def test_parts_are_in_order_and_the_checks_find_them():
    text = pv.render(record())
    parts = rc.split_parts(text)
    assert list(parts) == ["A", "B", "C"]
    assert "PINNED AB LINE" in text[slice(*parts["B"])] and "PINNED C LINE" in text[slice(*parts["C"])]


def test_values_are_verbatim_with_their_line_ids():
    text = pv.render(record())
    assert "Allergies: Penicillin allergy (rash) [L004]" in text
    assert "Treatment:\n- Oral antibiotics [L005]\n- IV fluids [L006, L007]" in text
    assert "Discharge medications:\n- Amoxicillin 500 mg TDS [L008]" in text


def test_statuses_without_items_are_shown_not_dropped():
    text = pv.render(record())
    assert "Weight: Not documented" in text
    assert "Pre-admission medications: Not documented" in text
    assert "Documented advice: None documented" in text


def test_inferred_and_unverified_items_are_marked():
    r = record()
    r["steps"]["validate_facts"]["facts"]["fields"]["weight"] = field(
        "inferred_flagged", [item("About 70 kg", status="citation_unverified")])
    text = pv.render(r)
    assert "Weight: (inferred, flagged for clinician check)\n- About 70 kg [L001] [citation unverified]" in text


def test_contradictions_and_resus_render_from_their_steps():
    r = record()
    r["steps"]["validate_facts"]["facts"]["contradictions"] = [item("Two doses recorded", ("L009", "L010"))]
    text = pv.render(r)
    assert "Resuscitation status:\nRESUSCITATION STATUS\nNot documented." in text
    assert "Contradictions flagged for clinician review:\n- Two doses recorded [L009, L010]" in text


@pytest.mark.parametrize("change, message", [
    (lambda r: r.update(generator="v1"), "not a pipeline record"),
    (lambda r: r.update(failed_step="extract_facts"), "stopped before step 5b"),
    (lambda r: r["steps"].pop("select_safety_net"), "missing a step"),
    (lambda r: r["steps"]["validate_facts"]["facts"]["fields"].update(extra=field()), "unknown fact fields"),
    (lambda r: r["steps"]["validate_facts"]["facts"]["fields"].update(weight=field("odd")), "unknown field status"),
])
def test_records_it_cannot_render_are_refused(change, message):
    r = record()
    change(r)
    with pytest.raises(pv.ViewError, match=message):
        pv.render(r)


def test_judgeable_sets_output_and_view_without_touching_the_record():
    r = record()
    before = copy.deepcopy(r)
    j = pv.judgeable(r)
    assert r == before
    assert j["output"] == pv.render(r) == pv.render(r)            # deterministic
    assert j["view"] == {"name": pv.VIEW_NAME, "version": pv.VIEW_VERSION}


def test_checks_read_the_view_like_a_v1_output():
    r = record()
    r["steps"]["validate_facts"]["facts"]["fields"]["admission_date"] = field(
        "documented", [item("03/04/2025")])
    findings = rc.run_checks("Day 1 (03/04) admitted.", pv.render(r))["D2"]["findings"]
    assert [(f["part"], f["token"]) for f in findings] == [("A", "2025")]
