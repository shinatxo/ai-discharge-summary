"""Tests for step 4, route_resus (ADR-009 [A2], (e) §2a; HAZ-02, HAZ-06).

Every scenario's notes go through the real steps 1 -> 3 -> 3b -> 4. Facts come
from two places, both synthetic:
- A5 and S12: real step-3 output from the 1 Oct e0.4 live run
  (tests/fixtures/step3_probe_20261001.json);
- the other resus scenarios: a hand-written GOLD resus object below — what a
  complete, correct extraction records — validated by the real step 3, so
  every quote is proven verbatim against the notes before step 4 sees it.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline import STEPS, StepError, run_step  # noqa: E402
from pipeline.route import (  # noqa: E402
    CHANGED_MARKER, FLAGGED_NOT_QUOTED, FORM_NOT_TRANSCRIBED, HEADING, NOT_CONFIRMED,
)
from pipeline.schemas import FIELD_NAMES, wire_facts  # noqa: E402

_SCENARIOS = {
    s["id"]: s["notes"]
    for s in json.loads((ROOT / "src" / "canary" / "scenarios.json").read_text())["scenarios"]
}
_PROBE = json.loads((ROOT / "tests" / "fixtures" / "step3_probe_20261001.json").read_text())["facts"]


def c(lines, quote):
    return {"lines": lines, "quote": quote}


def resus(status, changed, form, *cites):
    return {"form_or_discussion_documented": form, "status_documented": status,
            "changed": changed, "cites": list(cites)}


NONE = resus("not_documented", "not_documented", False)

# What a complete extraction records. Quotes are verbatim from the synthetic notes.
GOLD = {
    "S1": resus("dnacpr", "yes", True, c(["L008"], "For resus."), c(["L012"], "For resus."),
                c(["L015", "L016"], "DNACPR discussed + agreed w/ pt + family. DNACPR form completed by Dr Patel 14/05.")),
    "S2": resus("for_resuscitation", "not_documented", False, c(["L006"], "For resus.")),
    "S11": resus("for_resuscitation", "not_documented", False, c(["L004"], "For resus.")),
    "S13": resus("for_resuscitation", "yes", True,
                 c(["L008", "L009"], "For resus on admission. DNACPR placed day 8 during deterioration (poor "
                                     "prognosis), then rescinded day 20 once improving + family discussion.")),
    "S14": resus("for_resuscitation", "no", True,
                 c(["L006", "L007"], "Resus discussed with patient (capacity intact) -> wishes to remain for resus.")),
    "S15": resus("dnacpr", "no", True,
                 c(["L006"], "Resus: PRE-EXISTING community DNACPR (ReSPECT) in place from before admission"),
                 c(["L007"], "Documented, not changed."),
                 c(["L008"], "per ReSPECT + patient")),
    "S17": resus("for_resuscitation", "not_documented", False, c(["L004"], "Resus status: for resus.")),
    "B6": resus("for_resuscitation", "yes", True, c(["L006"], "For resus."),
                c(["L011", "L012"], "DNACPR form completed by Dr Lowe after an overnight deterioration."),
                c(["L015"], "DNACPR revoked, for resuscitation.")),
}
NO_RESUS = ["S3", "S4", "S8", "S9", "S10", "S16", "S18", "C7"]


def facts_with(r):
    return {
        "fields": {n: {"status": "not_documented", "items": []} for n in FIELD_NAMES},
        "medications": {"pre_admission": [], "discharge": [], "discharge_status": "not_documented"},
        "resus": copy.deepcopy(r), "documented_advice": [], "age_group": "adult",
        "contradictions": [], "suspicious_text": [],
    }


def pipeline(sid, r=None, step3=None):
    """Steps 1 -> 3 -> 3b -> 4 on a scenario. `r`: a grouped resus object to run
    through the real step 3; `step3`: step-3 facts to use as they are."""
    g = run_step("guard_input", {"notes": _SCENARIOS[sid]})
    if step3 is None:
        step3 = run_step("validate_facts", {"facts": wire_facts(facts_with(r)), "lines": g["lines"]})["facts"]
    ev = run_step("retrieve_evidence", {"facts": step3, "lines": g["lines"], "flags": g["flags"]})
    return run_step("route_resus", {"facts": step3, "evidence": ev, "lines": g["lines"]})


def test_gold_quotes_all_verify():
    # Guards the fixture itself: a typo in a GOLD quote would silently test the forced route.
    for sid, r in GOLD.items():
        g = run_step("guard_input", {"notes": _SCENARIOS[sid]})
        out = run_step("validate_facts", {"facts": wire_facts(facts_with(r)), "lines": g["lines"]})
        assert out["facts"]["resus"]["citation_status"] == "verified", sid


# --- every resus scenario, complete extraction --------------------------------

EXPECTED = {   # sid: (route, rendered status line)
    "S1": ("documented", f"DNACPR. {CHANGED_MARKER}"),
    "S2": ("documented", "For resuscitation."),
    "S11": ("documented", "For resuscitation."),
    "S13": ("documented", f"For resuscitation. {CHANGED_MARKER}"),
    "S14": ("documented", "For resuscitation. No change during admission."),
    "S15": ("documented", "DNACPR. No change during admission."),
    "S17": ("documented", "For resuscitation."),
    "B6": ("documented", f"For resuscitation. {CHANGED_MARKER}"),
}


@pytest.mark.parametrize("sid", sorted(EXPECTED))
def test_route_and_rendered_status_on_gold(sid):
    out = pipeline(sid, GOLD[sid])
    route, status_line = EXPECTED[sid]
    assert (out["route"], out["forced"]) == (route, "no")
    heading, first, source = out["rendered"].split("\n")
    assert (heading, first) == (HEADING, status_line)
    # Every gold citation is shown, by its line span.
    assert source.startswith("Source: ")
    for cite in GOLD[sid]["cites"]:
        span = cite["lines"][0] if len(cite["lines"]) == 1 else f"{cite['lines'][0]}–{cite['lines'][-1]}"
        assert f"[{span}]" in source


def test_rendered_block_exact_for_one_scenario():
    assert pipeline("S14", GOLD["S14"])["rendered"] == (
        "RESUSCITATION STATUS\n"
        "For resuscitation. No change during admission.\n"
        'Source: [L006–L007]: "Resus discussed with patient (capacity intact) -> wishes to remain for resus."'
    )


@pytest.mark.parametrize("sid", NO_RESUS)
def test_absent_on_scenarios_with_no_resus_content(sid):
    out = pipeline(sid, NONE)
    assert (out["route"], out["forced"], out["cited_lines"]) == ("absent", "no", [])
    assert out["rendered"] == "RESUSCITATION STATUS\nNot documented."


# --- the real step-3 outputs -----------------------------------------------

def test_s12_form_documented_recommendation_not_transcribed():
    out = pipeline("S12", step3=_PROBE["S12"])
    assert (out["route"], out["forced"], out["status"]) == ("documented_but_absent", "no", "not_documented")
    assert out["rendered"] == HEADING + "\n" + FORM_NOT_TRANSCRIBED.format(sources='[L029]: "RESPECT form"')


def test_a5_injection_does_not_suppress_the_documented_status():
    # A5's injected text names DNACPR (L015, guard-flagged). It is retrieved by
    # 3b, but must neither force the route nor be quoted (decision 2 Oct 2026).
    out = pipeline("A5", step3=_PROBE["A5"])
    assert (out["route"], out["forced"], out["status"], out["changed"]) == ("documented", "no", "dnacpr", "yes")
    assert out["rendered"].split("\n")[1] == f"DNACPR. {CHANGED_MARKER}"
    assert "L015" not in out["rendered"] and "L015" not in out["cited_lines"]
    assert out["cited_lines"] == ["L006", "L011", "L012"]


def test_flagged_line_inside_a_verified_citation_is_not_quoted():
    step3 = copy.deepcopy(_PROBE["A5"])
    g = run_step("guard_input", {"notes": _SCENARIOS["A5"]})
    step3["resus"]["cites"].append({"lines": ["L015"], "quote": g["lines"]["L015"].strip(),
                                    "verified": True, "reason": None})
    out = pipeline("A5", step3=step3)
    assert out["route"] == "documented"
    assert f"[L015] {FLAGGED_NOT_QUOTED}" in out["rendered"]
    assert g["lines"]["L015"].strip() not in out["rendered"]


# --- forced to documented_but_absent [A2] -------------------------------------

def test_s1_uncited_resus_line_forces_the_route_and_hides_the_status():
    partial = resus("dnacpr", "yes", True, *GOLD["S1"]["cites"][2:])   # cites L015-16 only
    out = pipeline("S1", partial)
    assert (out["route"], out["forced"]) == ("documented_but_absent", "resus_lines_uncited")
    assert out["status"] == "not_documented"
    assert out["cited_lines"] == ["L008", "L012", "L015", "L016"]
    assert out["rendered"].split("\n")[1].startswith("Resuscitation is recorded in these notes (")
    assert CHANGED_MARKER not in out["rendered"]
    # Sources in note order: L008, L012, then L015-L016.
    assert out["rendered"].index("[L008]") < out["rendered"].index("[L012]") < out["rendered"].index("[L015–L016]")
    assert '[L008]: "For resus."' in out["rendered"] and '[L012]: "For resus."' in out["rendered"]


def test_unverified_resus_citation_forces_the_route():
    bad = resus("dnacpr", "yes", True, c(["L015", "L016"], "DNACPR form completed by Dr Smith"))
    out = pipeline("S1", bad)
    assert (out["route"], out["forced"]) == ("documented_but_absent", "resus_citation_unverified")
    assert "Dr Smith" not in out["rendered"]          # a failed quote is never printed


def test_resus_claim_with_no_cites_and_nothing_found_still_forces():
    out = pipeline("S3", resus("dnacpr", "not_documented", False))
    assert (out["route"], out["forced"], out["cited_lines"]) == (
        "documented_but_absent", "resus_citation_unverified", [])
    assert out["rendered"] == HEADING + "\n" + NOT_CONFIRMED.format(sources="")


def test_extraction_missing_resus_entirely_is_forced_not_absent():
    out = pipeline("S2", NONE)                       # notes say "For resus."; extraction recorded nothing
    assert (out["route"], out["forced"], out["cited_lines"]) == (
        "documented_but_absent", "resus_lines_uncited", ["L006"])


@pytest.mark.parametrize("r", [
    resus("not_documented", "not_documented", False, c(["L006"], "For resus.")),   # cites, no claim
    resus("not_documented", "yes", False, c(["L006"], "For resus.")),              # "changed" alone
])
def test_inconsistent_claim_forces(r):
    assert pipeline("S2", r)["forced"] == "resus_claim_inconsistent"


def test_over_long_line_is_cited_by_id_without_quote():
    lines = {"L001": "For resus. " + "x" * 200}
    step3 = facts_with(NONE)
    step3["resus"].update({"fact_id": "resus", "citation_status": "verified", "reason": None})
    ev = run_step("retrieve_evidence", {"facts": step3, "lines": lines, "flags": []})
    out = run_step("route_resus", {"facts": step3, "evidence": ev, "lines": lines})
    assert "(L001)" not in out["rendered"] and "([L001])" in out["rendered"]


# --- contract ---------------------------------------------------------------

def test_registered_as_code_step_04():
    assert (STEPS["route_resus"].seq, STEPS["route_resus"].kind) == ("04", "code")


@pytest.mark.parametrize("bad", [
    None, {}, {"facts": {}, "evidence": {"resus": []}, "lines": {}},
    {"facts": facts_with(NONE), "evidence": {}, "lines": {}},
    {"facts": facts_with(resus("unknown", "no", False)), "evidence": {"resus": []}, "lines": {}},
    {"facts": facts_with(resus("dnacpr", "maybe", False)), "evidence": {"resus": []}, "lines": {}},
])
def test_bad_input_fails_closed(bad):
    with pytest.raises(StepError) as e:
        run_step("route_resus", bad)
    assert e.value.code == "bad_input"
