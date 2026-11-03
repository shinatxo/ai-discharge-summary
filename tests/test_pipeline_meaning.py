"""pipeline/meaning.py — step 7's meaning flags (piece 3 follow-up 2, CSO G2 A,
3 Nov 2026; HAZ-17). Pure functions, no AWS. Every PART A / PART C text below is
invented for these tests — not a canary scenario."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline import meaning as MN  # noqa: E402


def _flags(c_text, *a_texts, heading="treatment"):
    return {(k, t) for k, t, _s in MN.section_flags(c_text, [{"heading": heading, "text": x} for x in a_texts])}


# --- cause --------------------------------------------------------------------

def test_an_added_cause_or_purpose_is_flagged():
    assert ("cause", "caused by") in _flags("The stones were caused by your gout.", "Kidney stones. Gout.")
    assert ("cause", "to help") in _flags("You had a drip to help your kidneys.", "IV fluids given.")
    assert ("cause", "which means") in _flags("You had a fever, which means an infection.", "Pyrexia.")


def test_a_cause_the_section_states_is_not_flagged():
    assert _flags("The stones were caused by your gout.", "Kidney stones caused by gout.") == set()
    assert _flags("You had fluids because of dehydration.", "IV fluids because of dehydration.") == set()


def test_because_after_came_in_is_pre_ruled():
    assert _flags("You came in because you had a fever.", "Pyrexia for 2 days.") == set()
    assert ("cause", "because") in _flags("You had fluids because you were dry.", "IV fluids given.")


# --- certainty ------------------------------------------------------------------

@pytest.mark.parametrize("word", ["fully", "normal", "healed", "damaged", "carefully", "fine", "resolved"])
def test_an_added_certainty_or_grading_word_is_flagged(word):
    assert ("certainty", word) in _flags(f"Your knee was {word}.", "Knee effusion, reducing.")


def test_a_certainty_word_the_section_uses_is_not_flagged():
    assert _flags("Your kidney function has recovered.", "Renal function recovered.") == set()


# --- qualifier --------------------------------------------------------------------

def test_improving_must_be_kept():
    assert ("qualifier", "improving") in _flags("You are walking normally again.", "Mobility improving.")
    assert _flags("Your walking is getting better.", "Mobility improving.") == set()


def test_treated_as_needs_a_form_of_treat():
    assert ("qualifier", "treated_as") in _flags("You had cellulitis.", "Treated as cellulitis.")
    assert _flags("The team treated you for cellulitis.", "Treated as cellulitis.") == set()


def test_uncertainty_must_be_kept():
    assert ("qualifier", "possible") in _flags("You had gout.", "?Gout.")
    assert _flags("The team thinks you may have gout.", "?Gout.") == set()


def test_a_diagnosis_sections_background_is_not_read_for_qualifiers():
    a = "Primary: Gout.\n\nBackground:\n- ?Asthma"
    assert _flags("You had gout.", a, heading="diagnosis") == set()
    assert ("qualifier", "possible") in _flags("You had gout.", a, heading="treatment")


# --- test terms ---------------------------------------------------------------------

def test_a_different_test_word_is_flagged():
    assert ("test_term", "swab") in _flags("A swab was taken from your knee.", "Joint aspirate sent.")
    assert ("test_term", "scan") in _flags("A scan of your chest was clear.", "CXR clear.")


@pytest.mark.parametrize("c,a", [
    ("A scan of your belly.", "CT abdomen."),
    ("A chest X-ray.", "CXR."),
    ("A camera test of the bladder (cystoscopy).", "Cystoscopy."),
    ("A sample of fluid from your knee (joint aspirate).", "Joint aspirate sent."),
    ("Blood tests were taken.", "FBC, CRP."),
    # CSO additions, 3 Nov 2026 (before the p0.3 batch)
    ("A scan of your neck.", "USS neck."),
    ("Blood tests of your thyroid.", "TFTs normal range."),
    ("A heart trace over a day.", "24hr tape arranged."),
    ("A heart trace over a day.", "24 hour tape arranged."),
    ("A heart trace at home.", "Holter monitor fitted."),
    ("A heart trace at home.", "Holtor monitor fitted."),
])
def test_the_pre_ruled_plain_words_are_justified_by_their_terms(c, a):
    assert not {f for f in _flags(c, a) if f[0] == "test_term"}


# --- diagnosis ------------------------------------------------------------------------

DX = [{"section_id": "sections.1", "heading": "diagnosis",
       "text": "Primary: Gout.\n\nSecondary:\n- Kidney stones\n- Hospital-acquired weakness\n- "
               "Community-acquired pneumonia (CAP), score 3\n\nBackground:\n- Asthma"}]


def test_diagnosis_lines_stop_at_background():
    assert MN.diagnosis_lines(DX[0]["text"]) == [
        "Gout.", "Kidney stones", "Hospital-acquired weakness", "Community-acquired pneumonia (CAP), score 3"]


def test_an_unreached_diagnosis_is_flagged_and_its_head_noun_reaches_it():
    prose = "You had gout and kidney stones. You also had a chest infection (pneumonia)."
    assert MN.diagnoses_unreached(DX, prose) == [("sections.1", 3, "Hospital-acquired weakness")]
    assert MN.diagnoses_unreached(DX, prose + " You have weakness after your stay.") == []


def test_a_shared_hospital_word_does_not_reach_a_diagnosis():
    # "hospital" alone is not the head noun and is under half the head phrase
    prose = "You had gout, kidney stones and pneumonia. You were in hospital for a week."
    assert ("sections.1", 3, "Hospital-acquired weakness") in MN.diagnoses_unreached(DX, prose)


def test_only_given_diagnosis_sections_are_read():
    other = [{"section_id": "sections.3", "heading": "treatment", "text": "Primary: something"}]
    assert MN.diagnoses_unreached(other, "") == []


def test_kinds_and_check_names_are_fixed():
    assert MN.KINDS == ("cause", "certainty", "qualifier", "test_term", "diagnosis")
    assert set(MN.CHECK_OF) == set(MN.KINDS)
