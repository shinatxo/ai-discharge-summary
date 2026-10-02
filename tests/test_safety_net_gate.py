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

Since 2 Oct 2026 (ADR-009 W3) the gate takes the accepted pinned lines as a
parameter. The real-output tests score v1 with V1_LINES; the section at the end
tests the pipeline's two lines (CSO, 1 Oct 2026), the default.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from safety_net_gate import (  # noqa: E402
    CANONICAL_ADULT,
    CANONICAL_PAEDIATRIC,
    PIPELINE_LINES,
    V1_CANONICAL,
    V1_LINES,
    advice_block,
    advice_documented,
    check,
    check_combined,
    notes_document_seek_help,
    signpost_sentences,
    split_parts,
)


def check_v1(notes, part_a, part_c):
    """The tests down to the pipeline section below score v1 output against the
    line the live v1 prompts pin — what evals/run_cold_eval.py does for W4."""
    return check(notes, part_a, part_c, V1_LINES)


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
    "- " + V1_CANONICAL,
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
    res = check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(V1_CANONICAL))
    assert res.ok and res.status == "clean", res.findings


def test_soft_wrapped_canonical_passes():
    """The model wraps at ~75 chars, so the line straddles two lines."""
    wrapped = ("If you become unwell or are worried about anything, contact your GP or\n"
               "call NHS 111. Call 999 if it is an emergency.")
    assert check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(wrapped)).ok


def test_markdown_emphasis_passes():
    bolded = ("If you become unwell or are worried about anything, contact your GP "
              "or call **NHS 111**. Call **999** if it is an emergency.")
    assert check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(bolded)).ok


@pytest.mark.parametrize("heading", [
    "WHEN TO GET HELP",
    "When to get help:",
    "**When to get help:**",
    "─────────────────────────",
])
def test_section_headings_and_rules_do_not_pollute_the_sentence(heading):
    body = f"{heading}\n{V1_CANONICAL}"
    res = check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(body))
    assert res.ok, f"{heading!r} -> {res.findings}"


def test_quoted_canonical_passes():
    """The patient-version prompt shows the line in quotes; models copy them."""
    assert check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE,
                 _part_c(f'"{V1_CANONICAL}"')).ok


def test_canonical_repeated_twice_passes():
    body = f"{V1_CANONICAL}\n\nAnd again:\n\n{V1_CANONICAL}"
    assert check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(body)).ok


def test_routine_follow_up_is_not_urgent_signposting():
    body = ("- **See your GP in about 2 weeks.** They will check your blood count.\n\n"
            + V1_CANONICAL)
    res = check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(body))
    assert res.ok, res.findings
    assert all("2 weeks" not in s for s in signpost_sentences(_part_c(body)))


def test_canonical_line_in_part_a_is_exempt():
    """Putting the patient-independent fall-back in PART A invents nothing."""
    res = check_v1(NOTES_NO_TRIGGER, PART_A_CANONICAL_ONLY, _part_c(V1_CANONICAL))
    assert res.ok and res.status == "clean", res.findings


def test_documented_trigger_makes_the_gate_advisory():
    body = ("If your stoma stops working for 12 hours, or produces more than 1.5 "
            "litres in a day, call the ward or NHS 111.")
    res = check_v1(NOTES_WITH_TRIGGER, PART_A_NO_ADVICE, _part_c(body))
    assert res.ok and res.status == "documented_advice"


# --- failures: PART A, where the invention actually happens ------------------

@pytest.mark.parametrize("label,part_a", [
    ("S15 phlegm/breathlessness", PART_A_S15_INVENTED),
    ("S18 another seizure", PART_A_S18_INVENTED),
])
def test_invented_trigger_in_part_a_fails(label, part_a):
    notes = (NOTES_NO_TRIGGER if "S15" in label else NOTES_ACTIVITY_ADVICE_ONLY)
    res = check_v1(notes, part_a, _part_c(V1_CANONICAL))
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
    res = check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(body))
    assert not res.ok, f"{label} should fail"
    assert res.status == "added_advice"
    assert any("expected:" in f for f in res.findings)


@pytest.mark.parametrize("extra", ["Call 999 if worse", "Go to A&E if worse"])
def test_short_unpunctuated_escalation_line_still_fails(extra):
    """A heading-shaped line must not be discarded if it carries a signpost."""
    res = check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE,
                _part_c(f"{V1_CANONICAL}\n{extra}"))
    assert not res.ok, f"{extra!r} slipped through as a heading"


def test_absent_fallback_fails():
    res = check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE,
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
        f"PART C - PATIENT VERSION\n\n{V1_CANONICAL}\n"
    )
    part_a, part_c = split_parts(combined)
    assert "PATIENT ADVICE" in part_a and "Dear GP" not in part_a
    assert V1_CANONICAL in part_c
    assert check_combined(NOTES_NO_TRIGGER, combined, V1_LINES).ok


# --- the pipeline's pinned lines (CSO, 1 Oct 2026) — the default ------------

def test_gate_lines_equal_the_pipeline_step_5b_lines():
    # Two copies until the gate moves into the pipeline package at 8b ([A3]);
    # this test is what keeps them one string.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "generate"))
    from pipeline.safety_net import ADULT_LINE, PAEDIATRIC_LINE
    assert (CANONICAL_ADULT, CANONICAL_PAEDIATRIC) == (ADULT_LINE, PAEDIATRIC_LINE)
    assert PIPELINE_LINES == (ADULT_LINE, PAEDIATRIC_LINE)


@pytest.mark.parametrize("line", [CANONICAL_ADULT, CANONICAL_PAEDIATRIC])
def test_pipeline_line_passes_by_default(line):
    # Regression: the 1 Oct lines open with a sentence that carries no 111/999
    # token, so comparing PART C's signposting with the WHOLE line failed
    # correct output. The gate compares the signposting sentences only.
    res = check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(line))
    assert res.ok and res.status == "clean", res.findings


def test_pipeline_line_soft_wrapped_passes():
    wrapped = ("If you have been given a number to call, use that first. Otherwise, if\n"
               "you are worried, contact your GP or call NHS 111. Call 999 if it is an\n"
               "emergency.")
    assert check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(wrapped)).ok


def test_documented_advice_above_the_pinned_line_passes():
    # 5b's PART C on the documented route: advice quote(s), then the pinned line.
    body = f"Wound care advice given.\n\n{CANONICAL_ADULT}"
    res = check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(body))
    assert res.ok and res.status == "clean", res.findings


def test_pipeline_line_in_part_a_is_exempt():
    part_a = PART_A_NO_ADVICE.replace("PATIENT ADVICE\nNot documented",
                                      "PATIENT ADVICE\n" + CANONICAL_PAEDIATRIC)
    res = check(NOTES_NO_TRIGGER, part_a, _part_c(CANONICAL_PAEDIATRIC))
    assert res.ok and res.status == "clean", res.findings


def test_v1_line_fails_against_the_pipeline_default():
    res = check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(V1_CANONICAL))
    assert not res.ok and res.status == "added_advice"
    assert sum(f.startswith("expected:") for f in res.findings) == 2


def test_pipeline_line_fails_when_scoring_v1():
    assert not check_v1(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(CANONICAL_ADULT)).ok


def test_both_pinned_lines_in_one_part_c_fails():
    # One audience per document: adult AND paediatric signposting is drift.
    body = f"{CANONICAL_ADULT}\n\n{CANONICAL_PAEDIATRIC}"
    assert not check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(body)).ok


@pytest.mark.parametrize("label,part_a", [
    ("S15 phlegm/breathlessness", PART_A_S15_INVENTED),
    ("S18 another seizure", PART_A_S18_INVENTED),
])
def test_invented_trigger_in_part_a_still_fails_under_the_pipeline_lines(label, part_a):
    notes = NOTES_NO_TRIGGER if "S15" in label else NOTES_ACTIVITY_ADVICE_ONLY
    res = check(notes, part_a, _part_c(CANONICAL_ADULT))
    assert not res.ok and res.status == "added_advice_part_a"


def test_extra_route_after_the_pipeline_line_fails():
    res = check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(f"{CANONICAL_ADULT}\nGo to A&E if worse"))
    assert not res.ok


def test_empty_canonical_is_refused():
    with pytest.raises(ValueError):
        check(NOTES_NO_TRIGGER, PART_A_NO_ADVICE, _part_c(CANONICAL_ADULT), ())
