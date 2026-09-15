"""Tests for the safety-net gate (evals/safety_net_gate.py).

The FAIL cases are not invented: each is text the deployed prompt actually
produced, quoted from the saved cold-eval runs, paired with the real source
notes. They are regression tests for the drift the WS2a determination review
found (docs/WS2a-DEVICE-DETERMINATION.md §5.2), so a prompt change that
reintroduces it fails here rather than in review.

The PASS cases matter just as much. An over-eager gate that fires on correct
output gets switched off within a week, so routine follow-up, markdown emphasis,
soft line wrapping, colon-terminated headings, quoted text and a repeated
fall-back line are all pinned as non-failures.

The helper tests exist because an earlier version of this gate treated PART A as
ground truth and mis-parsed its advice block, which made it report the wrong
answer on the whole real corpus while its unit tests stayed green. The fixtures
below therefore use the real PART A shape — including the `FIELD: value` line
that follows the advice block and broke the parser.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from safety_net_gate import (  # noqa: E402
    CANONICAL_FALLBACK,
    advice_block,
    advice_documented,
    check,
    check_combined,
    notes_document_seek_help,
    signpost_sentences,
    split_parts,
)

# --- real source notes ------------------------------------------------------
# S15, verbatim tail. No safety-netting anywhere in the notes.
NOTES_NO_TRIGGER = """
Day 5 (20/05) Back to baseline. Fit for discharge. Pred 30mg OD for 5 days total
(complete course at home), complete oral amoxicillin course, inhaler technique
reviewed, rescue pack supplied, smoking cessation discussed (ex-smoker). Resp clinic
6/52, community resp team referral. GP: review after exacerbation.
"""

# S18, verbatim tail. Documents ACTIVITY restrictions, not a seek-help trigger.
NOTES_ACTIVITY_ADVICE_ONLY = """
Day 2 (20/05) Neurology review: first unprovoked seizure. Plan outpatient MRI brain
+ EEG, first-fit clinic. No driving. Discharged. Safety advice (no swimming alone,
heights, baths). DVLA: must not drive, must inform DVLA. GP: aware, await neurology.
"""

NOTES_WITH_TRIGGER = """
Day 3 (18/05) For discharge. Stoma nurse reviewed. Advised to come back if the
stoma stops working for 12 hours or output exceeds 1.5L/24h. GP: review in 1/52.
"""

# --- PART A fixtures, in the real field shape -------------------------------
PART_A_NO_ADVICE = """
DIAGNOSIS
Infective exacerbation of COPD

PATIENT ADVICE
Not documented

VTE ASSESSMENT: Not documented
ALLERGIES: NKDA
"""

PART_A_CANONICAL_ONLY = PART_A_NO_ADVICE.replace(
    "PATIENT ADVICE\nNot documented",
    "PATIENT ADVICE\n- Complete the full course of prednisolone as directed.\n"
    "- " + CANONICAL_FALLBACK,
)

# run-2026-05-30-patient-v2/S15.md, verbatim — invented into PART A.
PART_A_S15_INVENTED = PART_A_NO_ADVICE.replace(
    "PATIENT ADVICE\nNot documented",
    "PATIENT ADVICE\n- Complete the full course of prednisolone and antibiotics.\n"
    "- If breathlessness worsens, sputum changes, or you feel unwell, contact your\n"
    "  GP or call NHS 111. Call 999 in an emergency.",
)

# run-2026-05-30-patient-v2/S18.md, verbatim — invented into PART A.
PART_A_S18_INVENTED = PART_A_NO_ADVICE.replace(
    "PATIENT ADVICE\nNot documented",
    "PATIENT ADVICE\n- No swimming alone, no baths, no heights.\n"
    "- If you become unwell or have another seizure, contact your GP or call NHS\n"
    "  111. Call 999 if it is an emergency.",
)


def _part_c(body: str) -> str:
    return f"\nWhat happened\n\nYou were in hospital.\n\n{body}\n"


# --- passes -----------------------------------------------------------------

def test_canonical_line_passes():
    res = check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(CANONICAL_FALLBACK))
    assert res.ok and res.status == "clean", res.findings


def test_soft_wrapped_canonical_passes():
    """The model wraps at ~75 chars, so the line straddles two lines."""
    wrapped = ("If you become unwell or are worried about anything, contact your GP or\n"
               "call NHS 111. Call 999 if it is an emergency.")
    assert check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(wrapped)).ok


def test_markdown_emphasis_passes():
    bolded = ("If you become unwell or are worried about anything, contact your GP "
              "or call **NHS 111**. Call **999** if it is an emergency.")
    assert check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(bolded)).ok


@pytest.mark.parametrize("heading", [
    "WHEN TO GET HELP",
    "When to get help:",
    "**When to get help:**",
    "─────────────────────────",
])
def test_section_headings_and_rules_do_not_pollute_the_sentence(heading):
    body = f"{heading}\n{CANONICAL_FALLBACK}"
    res = check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(body))
    assert res.ok, f"{heading!r} -> {res.findings}"


def test_quoted_canonical_passes():
    """The patient-version prompt shows the line in quotes; models copy them."""
    assert check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE,
                 _part_c(f'"{CANONICAL_FALLBACK}"')).ok


def test_canonical_repeated_twice_passes():
    body = f"{CANONICAL_FALLBACK}\n\nAnd again:\n\n{CANONICAL_FALLBACK}"
    assert check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(body)).ok


def test_routine_follow_up_is_not_urgent_signposting():
    body = ("- **See your GP in about 2 weeks.** They will check your blood count.\n\n"
            + CANONICAL_FALLBACK)
    res = check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(body))
    assert res.ok, res.findings
    assert all("2 weeks" not in s for s in signpost_sentences(_part_c(body)))


def test_canonical_line_in_part_a_is_exempt():
    """Putting the patient-independent fall-back in PART A invents nothing."""
    res = check(NOTES_NO_TRIGGER, PART_A_CANONICAL_ONLY, _part_c(CANONICAL_FALLBACK))
    assert res.ok and res.status == "clean", res.findings


def test_documented_trigger_makes_the_gate_advisory():
    body = ("If your stoma stops working for 12 hours, or produces more than 1.5 "
            "litres in a day, call the ward or NHS 111.")
    res = check(NOTES_WITH_TRIGGER, PART_A_NO_ADVICE, _part_c(body))
    assert res.ok and res.status == "documented_advice"


# --- failures: PART A, where the invention actually happens ------------------

@pytest.mark.parametrize("label,part_a", [
    ("S15 phlegm/breathlessness", PART_A_S15_INVENTED),
    ("S18 another seizure", PART_A_S18_INVENTED),
])
def test_invented_trigger_in_part_a_fails(label, part_a):
    notes = (NOTES_NO_TRIGGER if "S15" in label else NOTES_ACTIVITY_ADVICE_ONLY)
    res = check(notes, part_a, _part_c(CANONICAL_FALLBACK))
    assert not res.ok, f"{label} should fail"
    assert res.status == "added_advice_part_a"
    assert "PART A:" in res.findings[-1]


# --- failures: PART C wording drift -----------------------------------------

@pytest.mark.parametrize("label,body", [
    ("S16 extra A&E route",
     "If you become unwell or are worried about anything, contact your GP or call "
     "**NHS 111**. Call **999** or go to your nearest **A&E** if it is an emergency."),
    ("S18 line reworded",
     "If you become unwell or are worried about anything, contact your GP or call "
     "NHS 111. Call 999 in an emergency."),
    ("S17 'anything else'",
     "If you become unwell or are worried about anything else, contact your GP or "
     "call **NHS 111**. Call **999** if it is an emergency."),
])
def test_part_c_drift_fails(label, body):
    res = check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(body))
    assert not res.ok, f"{label} should fail"
    assert res.status == "added_advice"
    assert any("expected:" in f for f in res.findings)


@pytest.mark.parametrize("extra", ["Call 999 if worse", "Go to A&E if worse"])
def test_short_unpunctuated_escalation_line_still_fails(extra):
    """A heading-shaped line must not be discarded if it carries a signpost."""
    res = check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE,
                _part_c(f"{CANONICAL_FALLBACK}\n{extra}"))
    assert not res.ok, f"{extra!r} slipped through as a heading"


def test_absent_fallback_fails():
    res = check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE,
                _part_c("Take your tablets as written."))
    assert not res.ok and res.status == "missing_fallback"


# --- helpers: the parsing that an earlier version got wrong ------------------

def test_advice_block_stops_at_the_next_field_label():
    """'VTE ASSESSMENT: Not documented' must terminate the block, or the gate
    reads someone else's 'Not documented' as the advice field's content."""
    block = advice_block(PART_A_CANONICAL_ONLY)
    assert "prednisolone" in block
    assert "VTE ASSESSMENT" not in block and "NKDA" not in block


def test_advice_documented_variants():
    assert not advice_documented(PART_A_NO_ADVICE)
    assert advice_documented(PART_A_S15_INVENTED)
    assert not advice_documented("DIAGNOSIS\nCOPD\n")            # heading absent
    # content on the heading line
    assert advice_documented("PATIENT ADVICE: Return if the wound leaks.\n"
                             "VTE ASSESSMENT: Not documented\n")
    # 'none' deep in the block must not mark the whole field undocumented
    assert advice_documented("PATIENT ADVICE\nKeep the dressing dry. None of your "
                             "other medicines changed.\nALLERGIES: NKDA\n")
    # the paediatric heading form
    assert advice_documented("PARENT / CARER ADVICE\nKeep him well hydrated.\n"
                             "VTE ASSESSMENT: N/A\n")


def test_notes_trigger_detection():
    assert not notes_document_seek_help(NOTES_NO_TRIGGER)
    # activity restrictions are not a seek-help trigger
    assert not notes_document_seek_help(NOTES_ACTIVITY_ADVICE_ONLY)
    assert notes_document_seek_help(NOTES_WITH_TRIGGER)
    assert notes_document_seek_help("Safety-netting given re: worsening pain.")
    assert notes_document_seek_help("Advised to attend A&E if febrile.")


def test_split_parts_of_a_combined_response():
    combined = (
        "PART A - DISCHARGE SUMMARY\n\nPATIENT ADVICE\nNot documented\n\n"
        "PART B - GP LETTER\n\nDear GP,\n\n"
        f"PART C - PATIENT VERSION\n\n{CANONICAL_FALLBACK}\n"
    )
    part_a, part_c = split_parts(combined)
    assert "PATIENT ADVICE" in part_a and "Dear GP" not in part_a
    assert CANONICAL_FALLBACK in part_c
    assert check_combined(NOTES_NO_TRIGGER, combined).ok
