"""pipeline/render.py — PART C's medicines box, translation header and assembly
(CSO decisions D2 M1, D3 T1, R4, R5, R5a, 1 Nov 2026). Pure functions, no AWS."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "evals"))

from pipeline import StepError  # noqa: E402
from pipeline import render as R  # noqa: E402
from pipeline.safety_net import ADULT_LINE, PAEDIATRIC_LINE  # noqa: E402
import safety_net_gate as gate  # noqa: E402


def _e(drug="bisoprolol", tag="continued", dose="5mg", route=None, frequency="OD", previous=None,
       conflict=False, basis="documented", status="verified"):
    return {"entry_id": "reconciliation.0", "drug": drug, "tag": tag, "basis": basis, "dose": dose,
            "route": route, "frequency": frequency, "previous": previous, "conflict": conflict,
            "citation_status": status}


@pytest.mark.parametrize("entry,line", [
    (_e(tag="new"), "- Bisoprolol 5mg once a day — new medicine"),
    (_e(), "- Bisoprolol 5mg once a day — keep taking as before"),
    (_e(tag="increased", previous={"dose": "2.5mg", "frequency": "OD"}),
     "- Bisoprolol 5mg once a day — dose increased (was 2.5mg once a day)"),
    (_e(tag="decreased", previous={"dose": "10mg", "frequency": None}),
     "- Bisoprolol 5mg once a day — dose lowered (was 10mg)"),
    (_e(tag="changed", frequency="BD", previous={"dose": None, "frequency": "OD"}),
     "- Bisoprolol 5mg twice a day — changed (was once a day)"),
    (_e(tag="increased"), "- Bisoprolol 5mg once a day — dose increased"),
    (_e(tag="stopped"), "- Bisoprolol — stop taking this"),
    (_e(tag="withheld"), "- Bisoprolol — do not take this for now"),
    (_e(drug="enoxaparin", tag="new", dose="40mg", route="subcut", frequency="ON"),
     "- Enoxaparin 40mg injection under the skin at night — new medicine"),
    (_e(drug="salbutamol", tag="continued", dose=None, route="inhaled", frequency="PRN"),
     "- Salbutamol inhaled when needed — keep taking as before"),
    (_e(drug="amoxicillin", tag="new", dose="500mg", route="oral", frequency="7/7"),
     "- Amoxicillin 500mg for 7 days — new medicine"),
    (_e(drug="x", tag="new", dose=None, frequency="1/52"), "- X for 1 week — new medicine"),
    (_e(frequency="BD for 5 days"), "- Bisoprolol 5mg BD for 5 days — keep taking as before"),
    (_e(frequency="regular"), "- Bisoprolol 5mg regular — keep taking as before"),
])
def test_medicine_lines(entry, line):
    assert R.medicine_line(entry) == line


def test_staff_notes_are_appended_in_a_fixed_order():
    line = R.medicine_line(_e(conflict=True, basis="inferred_from_dh", status="unverified", dose=None))
    assert line == ("- Bisoprolol once a day — keep taking as before " + R.NOTE_CONFLICT + " "
                    + R.NOTE_INFERRED + " " + R.NOTE_UNVERIFIED)


def test_staff_notes_are_bracketed_and_carry_no_signpost():
    texts = [R.NOTE_CONFLICT, R.NOTE_INFERRED, R.NOTE_UNVERIFIED,
             R.STATUS_LINES["not_documented"], R.STATUS_LINES["referenced_not_listed"]]
    for t in texts:
        assert t.startswith("[") and t.endswith("]") and not gate._SIGNPOST.search(t)


@pytest.mark.parametrize("status,head", [
    ("listed", None), ("not_documented", R.STATUS_LINES["not_documented"]),
    ("referenced_not_listed", R.STATUS_LINES["referenced_not_listed"]),
    ("none_required", R.STATUS_LINES["none_required"])])
def test_box_status_lines(status, head):
    box = R.medicines_box({"reconciliation": [_e()], "discharge_status": status})
    lines = box.split("\n")
    assert lines[-1] == "- Bisoprolol 5mg once a day — keep taking as before"
    assert (lines[0] == head) if head else len(lines) == 1


def test_listed_with_no_entries_says_not_recorded():
    assert R.medicines_box({"reconciliation": [], "discharge_status": "listed"}) == R.STATUS_LINES["not_documented"]


def test_bad_box_input_fails_closed():
    with pytest.raises(StepError):
        R.medicines_box({"reconciliation": [_e(tag="paused")], "discharge_status": "listed"})
    with pytest.raises(StepError):
        R.medicines_box({})


def test_translation_header_wording_r4():
    assert R.translation_header([], "adult") is None
    h = R.translation_header([{"line": "L002"}], "adult")
    assert h.startswith("FOR TRANSLATION — do not hand to the patient untranslated.")
    assert R.translation_header([{"line": "L002"}], "parent_or_carer").startswith(
        "FOR TRANSLATION — do not hand to the parent or carer untranslated.")
    for t in R.TRANSLATION_HEADER.values():
        assert not gate._SIGNPOST.search(t) and "interpreter" in t
    with pytest.raises(StepError):
        R.translation_header([], "patient")


def _step7(audience="adult"):
    return {"audience": audience, "sections": [
        {"section_id": "c_sections.0", "heading": "why_in_hospital", "source_ids": ["sections.0"],
         "text": "You had a heart attack (NSTEMI)."},
        {"section_id": "c_sections.1", "heading": "medicines", "source_ids": [], "text": "{{MEDICATIONS}}"},
        {"section_id": "c_sections.2", "heading": "next_steps", "source_ids": ["sections.9"],
         "text": "Heart clinic in 6 weeks."},
        {"section_id": "c_sections.3", "heading": "safety_net", "source_ids": [], "text": "{{SAFETY_NET}}"},
    ]}


def _sn(line=ADULT_LINE):
    return {"part_c": "Rest for a week.\n\n" + line}


RECON = {"reconciliation": [_e()], "discharge_status": "listed"}


def test_assemble_order_headings_and_boxes():
    c = R.assemble_part_c(_step7(), RECON, _sn(), [])
    assert c.split("\n")[0] == R.TITLES["adult"]
    assert "FOR TRANSLATION" not in c and "{{" not in c
    i = [c.index(x) for x in ("Why you were in hospital", "Your medicines", "What happens next",
                              "Advice and getting help")]
    assert i == sorted(i)
    assert "- Bisoprolol 5mg once a day — keep taking as before" in c and ADULT_LINE in c


def test_assemble_paediatric_and_header():
    c = R.assemble_part_c(_step7("parent_or_carer"), RECON, _sn(PAEDIATRIC_LINE), [{"line": "L002"}])
    lines = c.split("\n")
    assert lines[0] == R.TITLES["parent_or_carer"] and lines[2] == R.TRANSLATION_HEADER["parent_or_carer"]
    assert "Why your child was in hospital" in c and "Your child's medicines" in c


def test_assembled_c_passes_the_gate_on_the_fallback_route():
    sn = {"part_c": ADULT_LINE}
    c = R.assemble_part_c(_step7(), RECON, sn, [{"line": "L002"}])
    g = gate.check("Day 1: chest pain. Home.", "SAFETY NET:\n" + ADULT_LINE + "\n", c)
    assert g.ok and g.status == "clean"


def test_a_stray_placeholder_stays_visible():
    s7 = _step7()
    s7["sections"][0]["text"] = "See {{RESUS}}."
    assert "{{RESUS}}" in R.assemble_part_c(s7, RECON, _sn(), [])


def test_bad_audience_fails_closed():
    with pytest.raises(StepError):
        R.assemble_part_c(_step7("patient"), RECON, _sn(), [])
