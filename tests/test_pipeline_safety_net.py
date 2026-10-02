"""Tests for step 5b, select_safety_net (ADR-009 [A1], (e); 1 Oct 2026 CSO decision; HAZ-01, HAZ-03).

Synthetic notes only: S8 and A5 are real step-3 output from the 1 Oct e0.4 run
(tests/fixtures/step3_probe_20261001.json); S10 uses a hand-written advice item
validated by the real step 3.
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
from pipeline.safety_net import ADULT_LINE, PAEDIATRIC_LINE  # noqa: E402
from pipeline.schemas import FIELD_NAMES, wire_facts  # noqa: E402

_SCENARIOS = {
    s["id"]: s["notes"]
    for s in json.loads((ROOT / "src" / "canary" / "scenarios.json").read_text())["scenarios"]
}
_PROBE = json.loads((ROOT / "tests" / "fixtures" / "step3_probe_20261001.json").read_text())["facts"]


def guard(sid):
    return run_step("guard_input", {"notes": _SCENARIOS[sid]})


def select(facts, flags=()):
    return run_step("select_safety_net", {"facts": facts, "flags": list(flags)})


def facts_with(advice=(), age_group="adult"):
    return {
        "fields": {n: {"status": "not_documented", "items": []} for n in FIELD_NAMES},
        "medications": {"pre_admission": [], "discharge": [], "discharge_status": "not_documented"},
        "resus": {"form_or_discussion_documented": False, "status_documented": "not_documented",
                  "changed": "not_documented", "cites": []},
        "documented_advice": list(advice), "age_group": age_group,
        "contradictions": [], "suspicious_text": [],
    }


def step3(sid, f):
    return run_step("validate_facts", {"facts": wire_facts(f), "lines": guard(sid)["lines"]})["facts"]


# --- the pinned wording: the CSO's text of 1 Oct 2026, character for character --

def test_adult_line_is_the_cso_wording():
    assert ADULT_LINE == ("If you have been given a number to call, use that first. Otherwise, "
                          "if you are worried, contact your GP or call NHS 111. Call 999 if it is "
                          "an emergency.")


def test_paediatric_line_is_the_cso_wording():
    assert PAEDIATRIC_LINE == ("If you have been given a number to call, use that first. Otherwise, "
                               "if you are worried about your child, contact your GP or call NHS 111. "
                               "Call 999 if it is an emergency.")


# --- the two routes on real step-3 output -----------------------------------

def test_s8_documented_advice_is_carried_and_parent_still_gets_the_pinned_line():
    out = select(_PROBE["S8"], guard("S8")["flags"])
    assert (out["route"], out["audience"], out["advice_excluded"]) == ("documented", "paediatric", 0)
    quote = "Safety-net advice to parents re fever/feeding/breathing."
    assert out["part_ab"] == f'- "{quote}" [L012–L013]'
    assert out["part_c"] == f"{quote}\n\n{PAEDIATRIC_LINE}"
    assert out["cited_lines"] == ["L012", "L013"]


def test_a5_no_advice_gives_the_adult_line_in_every_part():
    out = select(_PROBE["A5"], guard("A5")["flags"])
    assert (out["route"], out["audience"]) == ("fallback", "adult")
    assert out["part_ab"] == out["part_c"] == ADULT_LINE
    assert out["advice"] == [] and out["cited_lines"] == []


def test_s10_adult_documented_advice():
    advice = [{"value": "safety-net advice", "cites": [{"lines": ["L010"], "quote": "Safety-net re PPH/sepsis/VTE."}]}]
    out = select(step3("S10", facts_with(advice)), guard("S10")["flags"])
    assert out["route"] == "documented"
    assert out["part_ab"] == '- "Safety-net re PPH/sepsis/VTE." [L010]'
    assert out["part_c"] == f"Safety-net re PPH/sepsis/VTE.\n\n{ADULT_LINE}"


# --- what counts as documented ----------------------------------------------

def _advice_item(verified=True, status="verified", lines=("L005",), value="model wording"):
    return {"value": value, "fact_id": "documented_advice.0", "citation_status": status, "reason": None,
            "cites": [{"lines": list(lines), "quote": "quoted text", "verified": verified, "reason": None}]}


@pytest.mark.parametrize("item", [
    _advice_item(verified=False, status="citation_unverified"),
    _advice_item(verified=True, status="citation_unverified"),     # another cite on the item failed
    {**_advice_item(), "cites": []},
])
def test_unverified_advice_is_excluded_counted_and_never_printed(item):
    out = select(facts_with([item]))
    assert (out["route"], out["advice_excluded"]) == ("fallback", 1)
    assert "quoted text" not in out["part_ab"] + out["part_c"]


def test_advice_on_a_guard_flagged_line_is_excluded():
    out = select(facts_with([_advice_item(lines=("L014", "L015"))]), [{"line": "L015", "rule": "output_control"}])
    assert (out["route"], out["advice_excluded"]) == ("fallback", 1)


def test_model_value_is_never_rendered_only_the_quote():
    out = select(facts_with([_advice_item(value="return if fever above 38")]))
    assert out["route"] == "documented"
    assert "fever" not in json.dumps(out)
    assert out["part_ab"] == '- "quoted text" [L005]'


def test_several_advice_items_keep_their_order():
    a = _advice_item()
    b = {**_advice_item(lines=("L007",)), "fact_id": "documented_advice.1"}
    b["cites"][0]["quote"] = "second"
    out = select(facts_with([a, b]))
    assert out["part_ab"] == '- "quoted text" [L005]\n- "second" [L007]'
    assert out["part_c"] == f"quoted text\nsecond\n\n{ADULT_LINE}"


# --- audience ---------------------------------------------------------------

@pytest.mark.parametrize("age_group, line", [
    ("neonate", PAEDIATRIC_LINE), ("infant", PAEDIATRIC_LINE), ("child", PAEDIATRIC_LINE),
    ("adult", ADULT_LINE), ("not_documented", ADULT_LINE),
])
def test_audience_selects_the_pinned_line(age_group, line):
    out = select(facts_with(age_group=age_group))
    assert out["pinned_line"] == out["part_c"] == out["part_ab"] == line


def test_part_c_always_ends_with_the_pinned_line():
    for f in (_PROBE["S8"], _PROBE["A5"], _PROBE["S12"], facts_with([_advice_item()], "child")):
        out = select(f)
        assert out["part_c"].endswith(out["pinned_line"])


# --- contract ---------------------------------------------------------------

def test_registered_as_code_step_05b():
    assert (STEPS["select_safety_net"].seq, STEPS["select_safety_net"].kind) == ("05b", "code")


def test_input_is_not_mutated():
    f = copy.deepcopy(_PROBE["S8"])
    before = copy.deepcopy(f)
    select(f)
    assert f == before


@pytest.mark.parametrize("bad", [
    None, {}, {"facts": facts_with()},                                   # flags missing: required
    {"facts": facts_with(age_group="teen"), "flags": []},
    {"facts": {**facts_with(), "documented_advice": None}, "flags": []},
    {"facts": facts_with([{"value": "x"}]), "flags": []},
])
def test_bad_input_fails_closed(bad):
    with pytest.raises(StepError) as e:
        run_step("select_safety_net", bad)
    assert e.value.code == "bad_input"
