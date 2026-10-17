"""evals/rubric_checks.py — rubric v2 deterministic checks (W5).

Synthetic notes and outputs written for these tests only — no corpus text, and
never note text in test names or messages."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import rubric_checks as rc  # noqa: E402

NOTES_NKDA = "Day 1 (03/04) 60M admitted. NKDA. Pain left knee.\nDay 2 (04/04) Discharged."
NOTES_ALLERGY = "Day 1 (03/04) NKDA on clerking.\nDay 2 nursing note: latex allergy, patient-reported."


def doc(a="", b="", c=""):
    return f"Preamble.\n# PART A — SUMMARY\n{a}\n## PART B — GP LETTER\n{b}\nPART C - PATIENT VERSION\n{c}\n"


def reasons(findings):
    return [f["reason"] for f in findings]


# --- parts -------------------------------------------------------------------
def test_split_parts_orders_ranges_and_marks_the_preamble():
    out = doc("x", "y", "z")
    parts = rc.split_parts(out)
    assert list(parts) == ["A", "B", "C"]
    assert parts["A"][1] == parts["B"][0] and parts["C"][1] == len(out)
    assert rc._part_of(0, parts) == "preamble"


# --- D2 added year ------------------------------------------------------------
def test_added_year_decides_fail_and_carries_only_the_token():
    r = rc.run_checks(NOTES_NKDA, doc("Admitted 03/04/2025."))
    assert r["D2"]["verdict"] == "fail"
    (f,) = r["D2"]["findings"]
    assert (f["check"], f["token"], f["part"]) == ("added_year", "2025", "A")


def test_year_in_the_notes_is_not_added():
    notes = NOTES_NKDA + "\nPMH: MI 2019."
    assert rc.run_checks(notes, doc("Previous MI (2019)."))["D2"]["verdict"] is None


@pytest.mark.parametrize("text", ["Call 0800 2000 123.", "Ref 1234-2010.", "Dose 12000 units.",
                                  "Seen at 20:15."])
def test_numbers_that_are_not_years(text):
    assert rc.run_checks(NOTES_NKDA, doc(text))["D2"]["findings"] == []


# --- D2 non-English -------------------------------------------------------------
def test_foreign_letters_in_part_c_decide_fail():
    r = rc.run_checks(NOTES_NKDA, doc(c="Jeśli masz pytania, zadzwoń."))
    assert reasons(r["D2"]["findings"]) == ["non_english_sentence"]
    assert r["D2"]["findings"][0]["part"] == "C"


def test_two_function_words_without_diacritics_decide_fail():
    r = rc.run_checks(NOTES_NKDA, doc(c="Prosze dzwonic jesli jest lub nie problem."))
    assert r["D2"]["verdict"] == "fail"


def test_one_function_word_is_only_a_flag():
    r = rc.run_checks(NOTES_NKDA, doc(c="Bring the leaflet dla your GP."))
    assert r["D2"]["verdict"] is None
    assert reasons(r["flags"]) == ["possible_non_english"]


def test_non_english_outside_part_c_is_not_this_check():
    assert rc.run_checks(NOTES_NKDA, doc(a="Jeśli masz pytania."))["D2"]["findings"] == []


def test_a_name_that_is_in_the_notes_is_not_a_translation():
    notes = NOTES_NKDA + "\nSeen by Dr Wąsik."
    assert rc.run_checks(notes, doc(c="Dr Wąsik saw you."))["D2"]["findings"] == []


def test_plain_english_part_c_has_no_finding():
    r = rc.run_checks(NOTES_NKDA, doc(c="You can go home. Call 999 if it is an emergency."))
    assert r["D2"]["findings"] == [] and r["flags"] == []


# --- D7 allergy -------------------------------------------------------------------
def test_allergy_status_from_notes():
    assert rc.allergy_status(NOTES_NKDA) == {"nkda": True, "allergens": set()}
    assert rc.allergy_status(NOTES_ALLERGY) == {"nkda": True, "allergens": {"latex"}}


@pytest.mark.parametrize("line", ["Allergies: NKDA", "No known drug allergies.",
                                  "You do not have any known allergies."])
def test_documented_allergy_shown_as_nkda_is_auto_fail(line):
    r = rc.run_checks(NOTES_ALLERGY, doc(c=line))
    assert r["D7"]["verdict"] == "fail" and r["D7"]["auto_fail"] is True
    assert reasons(r["D7"]["findings"]) == ["documented_allergy_shown_as_nkda"]


@pytest.mark.parametrize("line", ["Allergies: latex allergy (patient-reported, day 2).",
                                  "Allergies: NKDA on admission; latex allergy reported day 2.",
                                  "Allergies: conflicting records (NKDA vs reported) - confirm."])
def test_allergy_or_conflict_shown_is_not_an_inversion(line):
    assert rc.run_checks(NOTES_ALLERGY, doc(a=line))["D7"]["findings"] == []


def test_heading_on_its_own_line_is_joined_to_the_next_line():
    r = rc.run_checks(NOTES_ALLERGY, doc(a="ALLERGIES\n\nNKDA"))
    assert reasons(r["D7"]["findings"]) == ["documented_allergy_shown_as_nkda"]


def test_nkda_shown_as_an_allergy_is_auto_fail():
    r = rc.run_checks(NOTES_NKDA, doc(a="Allergies: penicillin allergy."))
    assert reasons(r["D7"]["findings"]) == ["nkda_shown_as_allergy"]
    assert r["D7"]["auto_fail"] is True


@pytest.mark.parametrize("line", ["Allergies: NKDA", "Allergies: Not documented",
                                  "Check allergy status before prescribing.", "Allergies: None known"])
def test_nkda_notes_with_no_inversion(line):
    assert rc.run_checks(NOTES_NKDA, doc(a=line))["D7"]["findings"] == []


def test_notes_with_no_allergy_statement_skip_the_check():
    assert rc.allergy("Day 1 seen.", "Allergies: latex allergy", {}) == []


# --- D7 laterality (flags only) ---------------------------------------------------
def test_a_side_the_notes_never_give_is_flagged_never_decided():
    r = rc.run_checks(NOTES_NKDA, doc(a="Pain right knee."))
    assert r["D7"]["verdict"] is None
    assert [(f["reason"], f["token"]) for f in r["flags"]] == [("side_not_in_notes", "right")]


def test_the_documented_side_is_not_flagged():
    assert rc.run_checks(NOTES_NKDA, doc(a="Pain left knee."))["flags"] == []


@pytest.mark.parametrize("text", ["Come back right away.", "That is all right.",
                                  "You left the ward on day 2.", "You have the right to ask."])
def test_words_that_are_not_sides(text):
    assert rc.run_checks("Day 1 seen. NKDA.", doc(c=text))["flags"] == []


def test_abbreviated_side_in_notes_counts_but_a_vertebral_level_does_not():
    assert rc._sides("L 7-9 rib fractures") == {"left"}
    assert rc._sides("L1 transverse process") == set()


def test_both_sides_in_notes_leaves_it_to_the_judge():
    notes = "Left wrist and right ankle injuries. NKDA."
    assert rc.run_checks(notes, doc(a="Right wrist."))["flags"] == []


# --- shape ---------------------------------------------------------------------
def test_results_carry_no_output_or_note_text():
    out = doc("Admitted 03/04/2025. Allergies: penicillin allergy.", c="Jeśli masz pytania.")
    r = rc.run_checks(NOTES_NKDA, out)
    text = json.dumps(r)
    for fragment in ("Admitted", "penicillin", "Jeśli", "knee"):
        assert fragment not in text
    assert set(r) == {"checks_version", "parts_found", "D2", "D7", "flags"}


# --- checks v2 (found by the W5 calibration) ------------------------------------
def test_checks_version_is_2():
    assert rc.run_checks(NOTES_NKDA, doc())["checks_version"] == 2


def test_two_digit_year_of_a_note_date_written_in_full_is_not_added():
    notes = "Day 1 (26/04/26) admitted.\nDay 3 (28.04.26) discharged."
    r = rc.run_checks(notes, doc(a="Admitted 26/04/2026. Discharged 28/04/2026."))
    assert r["D2"]["findings"] == []


def test_two_digit_date_year_does_not_license_another_year():
    r = rc.run_checks("Day 1 (26/04/26) admitted.", doc(a="Admitted 26/04/2025."))
    assert [f["token"] for f in r["D2"]["findings"]] == ["2025"]


def test_a_bare_two_digit_number_is_not_a_year():
    r = rc.run_checks("Day 1 (26/04) admitted, aged 26.", doc(a="Admitted 26/04/2026."))
    assert [f["token"] for f in r["D2"]["findings"]] == ["2026"]


CONFLICT_BLOCK = ("ALLERGIES\nConflicting allergy information - clinician to clarify:\n"
                  "  Day 1 notes record NKDA.\n  Day 2 records a latex allergy.\n"
                  "  Do not assume NKDA.")


@pytest.mark.parametrize("a", [CONFLICT_BLOCK,
                               CONFLICT_BLOCK.replace("clarify:\n", "clarify:\n\n"),
                               "Reconcile the allergy record (NKDA vs\n  patient-reported latex allergy)."])
def test_a_part_that_shows_the_allergy_or_a_conflict_is_not_an_inversion(a):
    assert rc.run_checks(NOTES_ALLERGY, doc(a=a))["D7"]["findings"] == []


def test_a_conflict_outside_an_allergy_statement_does_not_count():
    out = doc(a="Medication history: records conflict on dose.\n\nAllergies: NKDA")
    assert reasons(rc.run_checks(NOTES_ALLERGY, out)["D7"]["findings"]) == ["documented_allergy_shown_as_nkda"]


def test_conflict_in_part_a_does_not_cover_part_c():
    out = doc(a="Allergies: conflicting records - confirm.", c="You have no known allergies.")
    f = rc.run_checks(NOTES_ALLERGY, out)["D7"]["findings"]
    assert reasons(f) == ["documented_allergy_shown_as_nkda"] and f[0]["part"] == "C"


@pytest.mark.parametrize("a", ["Allergies: NKDA. No conflicts in the record.",
                               "Allergies: NKDA\nNo allergy discrepancy found."])
def test_no_conflict_is_not_a_stated_conflict(a):
    assert reasons(rc.run_checks(NOTES_ALLERGY, doc(a=a))["D7"]["findings"]) == ["documented_allergy_shown_as_nkda"]


def test_allergen_named_elsewhere_in_the_paragraph_does_not_excuse_nkda():
    notes = "Day 1 NKDA on clerking.\nDay 2 nursing note: penicillin allergy, rash."
    out = doc(a="Allergies: NKDA\nAntibiotics: penicillin V 500 mg QDS")
    assert reasons(rc.run_checks(notes, out)["D7"]["findings"]) == ["documented_allergy_shown_as_nkda"]
