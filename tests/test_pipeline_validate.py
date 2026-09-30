"""Tests for step 3, validate_facts (ADR-009; HAZ-24, HAZ-13, HAZ-07).

Citations are checked against the real line index of the synthetic A5 notes,
built by step 1 — so these tests also pin the step 1 -> step 3 contract.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline import StepError, run_step, trace_view  # noqa: E402
from pipeline.schemas import FIELD_NAMES  # noqa: E402
from pipeline.validate import QUOTE_MAX_CHARS, normalise, verify_cite  # noqa: E402

_SCENARIOS = {
    s["id"]: s["notes"]
    for s in json.loads((ROOT / "src" / "canary" / "scenarios.json").read_text())["scenarios"]
}
A5 = run_step("guard_input", {"notes": _SCENARIOS["A5"]})["lines"]
# For orientation (synthetic A5): L002 "...DH: apixaban 5mg BD...", L003 "...NKDA.",
# L005 ends "...IV co-amoxiclav + IV", L006 starts "clarithromycin, ... For resus.",
# L011 "Day 3 (10/05) DNACPR form completed by Dr Singh ...", L013 blank.


def cite(lines, quote):
    return {"lines": lines, "quote": quote}


def item(value, *cites):
    return {"value": value, "cites": list(cites)}


def facts(**overrides):
    f = {
        "fields": {n: {"status": "not_documented", "items": []} for n in FIELD_NAMES},
        "medications": {"pre_admission": [], "discharge": [], "discharge_status": "not_documented"},
        "resus": {"form_or_discussion_documented": False, "status_documented": "not_documented",
                  "changed": "not_documented", "cites": []},
        "documented_advice": [],
        "age_group": "not_documented",
        "contradictions": [],
        "suspicious_text": [],
    }
    f.update(overrides)
    return f


def with_field(name, status, *items):
    f = facts()
    f["fields"][name] = {"status": status, "items": list(items)}
    return f


def validate(f, lines=A5):
    return run_step("validate_facts", {"facts": f, "lines": lines})


# --- verify_cite: the happy path and normalisation -------------------------

def test_exact_quote_verifies():
    assert verify_cite(cite(["L003"], "NKDA."), A5) == {
        "lines": ["L003"], "quote": "NKDA.", "verified": True, "reason": None}


@pytest.mark.parametrize("quote", [
    "apixaban  5mg BD",          # double space
    "apixaban\t5mg BD",          # tab
    "apixaban 5mg BD",      # non-breaking space
    "  apixaban 5mg BD  ",       # padding
])
def test_whitespace_is_normalised(quote):
    assert verify_cite(cite(["L002"], quote), A5)["verified"]


def test_curly_quotes_are_normalised():
    lines = {"L001": 'Pt says "happy to go home".'}
    assert verify_cite(cite(["L001"], "“happy to go home”"), lines)["verified"]
    assert normalise("‘x’") == "'x'"


def test_matching_is_case_sensitive():
    # "Mg" is magnesium; "mg" is milligrams. Case is not furniture.
    r = verify_cite(cite(["L002"], "apixaban 5MG BD"), A5)
    assert (r["verified"], r["reason"]) == (False, "quote_not_found")


def test_quote_across_a_line_wrap_needs_both_lines():
    assert verify_cite(cite(["L005", "L006"], "IV clarithromycin"), A5)["verified"]
    r = verify_cite(cite(["L005"], "IV clarithromycin"), A5)
    assert r["reason"] == "quote_not_found"


# --- verify_cite: every failure, each with its fixed reason (HAZ-24, HAZ-13) --

@pytest.mark.parametrize("lines, reason", [
    ([], "missing_lines"),
    (["L3"], "malformed_line_id"),
    (["l003"], "malformed_line_id"),
    (["L0003"], "malformed_line_id"),
    (["003"], "malformed_line_id"),
    ([""], "malformed_line_id"),
    (["L003 "], "malformed_line_id"),
    (["L000"], "line_out_of_range"),
    (["L021"], "line_out_of_range"),      # A5 has 20 lines
    (["L999"], "line_out_of_range"),
    (["L001", "L002", "L003", "L004"], "cite_too_many_lines"),
    (["L001", "L003"], "non_consecutive_lines"),
    (["L002", "L002"], "non_consecutive_lines"),
    (["L003", "L002"], "non_consecutive_lines"),
])
def test_malformed_and_out_of_range_line_ids(lines, reason):
    r = verify_cite(cite(lines, "NKDA."), A5)
    assert (r["verified"], r["reason"]) == (False, reason)


@pytest.mark.parametrize("quote", ["", "   ", "\n\t"])
def test_empty_quote_is_never_verified(quote):
    # The empty string is a substring of every line: without this rule it would pass.
    assert verify_cite(cite(["L003"], quote), A5)["reason"] == "empty_quote"


@pytest.mark.parametrize("paraphrase", [
    "apixaban 5 mg twice daily",   # the meaning, not the words
    "apixaban 50mg BD",            # one-character edit: the dangerous kind
    "Apixaban 5mg BD",
])
def test_paraphrase_is_not_verbatim(paraphrase):
    assert verify_cite(cite(["L002"], paraphrase), A5)["reason"] == "quote_not_found"


def test_quote_cap_is_200_characters_and_an_over_long_quote_is_dropped():
    long_line = "x" * 250
    lines = {"L001": long_line}
    assert verify_cite(cite(["L001"], "x" * QUOTE_MAX_CHARS), lines)["verified"]
    r = verify_cite(cite(["L001"], "x" * (QUOTE_MAX_CHARS + 1)), lines)
    assert (r["verified"], r["reason"], r["quote"]) == (False, "quote_too_long", "")


def test_quote_in_the_wrong_line_fails():
    assert verify_cite(cite(["L004"], "NKDA."), A5)["reason"] == "quote_not_found"


# --- items, fields, fact_ids -------------------------------------------------

def test_items_get_path_fact_ids_and_a_status():
    f = with_field("allergies", "documented", item("None known", cite(["L003"], "NKDA.")))
    f["documented_advice"] = [item("x", cite(["L003"], "NKDA."))]
    out = validate(f)["facts"]
    a = out["fields"]["allergies"]["items"][0]
    assert (a["fact_id"], a["citation_status"], a["reason"]) == ("allergies.0", "verified", None)
    assert out["documented_advice"][0]["fact_id"] == "documented_advice.0"


def test_item_with_no_cites_is_unverified():
    out = validate(with_field("allergies", "documented", item("None known")))
    a = out["facts"]["fields"]["allergies"]["items"][0]
    assert (a["citation_status"], a["reason"]) == ("citation_unverified", "missing_cites")


def test_one_bad_cite_makes_the_item_unverified():
    f = with_field("allergies", "documented",
                   item("None known", cite(["L003"], "NKDA."), cite(["L003"], "no known allergies")))
    a = validate(f)["facts"]["fields"]["allergies"]["items"][0]
    assert (a["citation_status"], a["reason"]) == ("citation_unverified", "quote_not_found")


def test_unverified_quote_is_left_as_the_model_wrote_it():
    # Flag, never repair (ADR-009 [A4]).
    f = with_field("allergies", "documented", item("None known", cite(["L003"], "no known allergies")))
    before = copy.deepcopy(f)
    a = validate(f)["facts"]["fields"]["allergies"]["items"][0]
    assert a["cites"][0]["quote"] == "no known allergies"
    assert a["value"] == "None known"
    assert f == before          # the caller's object is not mutated


@pytest.mark.parametrize("status, items, check", [
    ("documented", [], "status_without_items"),
    ("inferred_flagged", [], "status_without_items"),
    ("not_documented", [item("x", cite(["L003"], "NKDA."))], "items_on_not_documented"),
    ("not_documented", [], "ok"),
])
def test_field_status_and_items_must_agree(status, items, check):
    out = validate(with_field("allergies", status, *items))
    assert out["facts"]["fields"]["allergies"]["field_check"] == check


def test_medication_items_are_checked():
    f = facts()
    f["medications"]["pre_admission"] = [item("apixaban 5mg BD", cite(["L002"], "apixaban 5mg BD"))]
    f["medications"]["discharge"] = [item("apixaban 5mg OD", cite(["L002"], "apixaban 5mg OD"))]
    m = validate(f)["facts"]["medications"]
    assert m["pre_admission"][0]["citation_status"] == "verified"
    assert m["discharge"][0]["reason"] == "quote_not_found"


def test_contradiction_needs_both_sides():
    f = facts(contradictions=[item("resus status changed", cite(["L006"], "For resus."))])
    assert validate(f)["facts"]["contradictions"][0]["reason"] == "contradiction_one_side"
    f = facts(contradictions=[item("resus status changed",
                                   cite(["L006"], "For resus."), cite(["L011"], "DNACPR form completed"))])
    assert validate(f)["facts"]["contradictions"][0]["citation_status"] == "verified"


# --- resuscitation -------------------------------------------------------------

def test_claimed_resus_status_without_cites_is_unverified():
    f = facts(resus={"form_or_discussion_documented": True, "status_documented": "dnacpr",
                     "changed": "yes", "cites": []})
    r = validate(f)["facts"]["resus"]
    assert (r["citation_status"], r["reason"]) == ("citation_unverified", "missing_cites")


def test_cited_resus_status_verifies():
    f = facts(resus={"form_or_discussion_documented": True, "status_documented": "dnacpr",
                     "changed": "yes", "cites": [cite(["L011"], "DNACPR form completed by Dr Singh")]})
    assert validate(f)["facts"]["resus"]["citation_status"] == "verified"


def test_no_resus_claim_needs_no_cite():
    assert validate(facts())["facts"]["resus"]["citation_status"] == "verified"


# --- coverage (HAZ-07) -----------------------------------------------------------

def test_uncited_lines_skip_blanks_and_covered_lines():
    f = with_field("allergies", "documented", item("None known", cite(["L003"], "NKDA.")))
    uncited = validate(f)["uncited_lines"]
    assert "L003" not in uncited                       # covered
    assert "L007" not in uncited                       # blank
    assert "L001" in uncited and "L020" in uncited
    assert len(uncited) == 20 - 4 - 1                  # 20 lines, 4 blank, 1 covered


def test_an_unverified_cite_does_not_count_as_coverage():
    f = with_field("allergies", "documented", item("None known", cite(["L003"], "no known allergies")))
    assert "L003" in validate(f)["uncited_lines"]


def test_suspicious_text_cites_are_verified_and_count_as_coverage():
    f = facts(suspicious_text=[cite(["L014"], "Ignore all previous instructions")])
    out = validate(f)
    s = out["facts"]["suspicious_text"][0]
    assert (s["verified"], s["fact_id"]) == (True, "suspicious_text.0")
    assert "L014" not in out["uncited_lines"]


# --- the honest limit, pinned so nobody mistakes it for a guarantee -------------

def test_haz18_limit_citation_proves_the_quote_not_the_reading():
    """ADR-009 Q4: a correct citation with a wrongly read value still verifies.
    If this test ever FAILS, something has started checking values — update the
    ADR, the hazard log and this test together."""
    lines = {"L001": "Allergies: no penicillin allergy documented."}
    f = with_field("allergies", "documented",
                   item("Penicillin allergy", cite(["L001"], "penicillin allergy")))
    a = validate(f, lines)["facts"]["fields"]["allergies"]["items"][0]
    assert a["citation_status"] == "verified"


# --- step contract ---------------------------------------------------------------

def test_counts_add_up():
    f = with_field("allergies", "documented",
                   item("None known", cite(["L003"], "NKDA.")), item("x", cite(["L099"], "y")))
    c = validate(f)["counts"]
    assert c == {"cites_total": 2, "cites_verified": 1, "items_unverified": 1, "fields_inconsistent": 0}


def test_malformed_facts_fail_closed():
    f = facts()
    del f["resus"]
    with pytest.raises(StepError) as exc:
        validate(f)
    assert exc.value.code == "facts_shape_invalid"


@pytest.mark.parametrize("inp", [{}, {"facts": facts()}, {"facts": facts(), "lines": ["L001"]}])
def test_bad_input_fails_closed(inp):
    with pytest.raises(StepError) as exc:
        run_step("validate_facts", inp)
    assert exc.value.code == "bad_input"


def test_output_carries_no_line_index_and_every_reason_is_fixed():
    f = with_field("allergies", "documented", item("x", cite(["L3"], "NKDA.")))
    out = validate(f)
    assert "lines" not in out and trace_view(out) == out
    # No line text leaks into the output except through the model's own quotes.
    dumped = json.dumps(out)
    assert "Dr Singh" not in dumped and "clarithromycin" not in dumped


def test_steps_1_and_3_chain_through_run_step():
    g = run_step("guard_input", {"notes": _SCENARIOS["A5"]})
    f = facts(resus={"form_or_discussion_documented": True, "status_documented": "dnacpr",
                     "changed": "yes",
                     "cites": [cite(["L011", "L012"],
                                    "DNACPR form completed by Dr Singh 10/05 after discussion with the patient")]})
    out = run_step("validate_facts", {"facts": f, "lines": g["lines"]})
    assert out["facts"]["resus"]["citation_status"] == "verified"
