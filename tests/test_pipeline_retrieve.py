"""Tests for step 3b, retrieve_evidence (ADR-009 [A7]; HAZ-02, -06, -07).

Two kinds of fixture, both synthetic:
- the 18 canary scenarios (src/canary/scenarios.json), with NO facts, to pin
  exactly which lines code matches on its own — the false-positive check, the
  guard's approach;
- real step-3 output from the 1 Oct e0.4 live run (tests/fixtures/
  step3_probe_20261001.json: A5, S12, S8), for recall with real citations.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline import STEPS, StepError, run_step  # noqa: E402

_SCENARIOS = {
    s["id"]: s["notes"]
    for s in json.loads((ROOT / "src" / "canary" / "scenarios.json").read_text())["scenarios"]
}
_PROBE = json.loads((ROOT / "tests" / "fixtures" / "step3_probe_20261001.json").read_text())["facts"]


def guard(sid):
    return run_step("guard_input", {"notes": _SCENARIOS[sid]})


def no_facts():
    """Step-3 facts with nothing cited — so only code's own matches show."""
    return {
        "medications": {"pre_admission": [], "discharge": [], "discharge_status": "not_documented"},
        "resus": {"form_or_discussion_documented": False, "status_documented": "not_documented",
                  "changed": "not_documented", "cites": [], "fact_id": "resus",
                  "citation_status": "verified", "reason": None},
    }


def retrieve(facts, g):
    return run_step("retrieve_evidence", {"facts": facts, "lines": g["lines"], "flags": g["flags"]})


def rows(out, block):
    return {r["line"]: r for r in out[block]}


# --- code's own matches on all 18 scenarios, pinned -------------------------
# H = med_header, C = med_continuation. A change to any pattern must change this
# table on purpose. Known, accepted continuation over-reach (a wrapped line that
# is follow-up, not drugs): S13 L016, S14 L013, A5 L020.

PINNED = {
    "S1": ({"L003": "H", "L004": "C", "L021": "H"}, ["L008", "L012", "L015", "L016"]),
    "S2": ({"L002": "H", "L011": "H"}, ["L006"]),
    "S3": ({"L007": "H"}, []),
    "S4": ({"L015": "H"}, []),
    "S8": ({}, []),
    "S9": ({}, []),
    "S10": ({"L004": "H", "L008": "H", "L009": "C"}, []),
    "S11": ({"L012": "H"}, ["L004"]),
    "S12": ({}, ["L029"]),
    "S13": ({"L013": "H", "L014": "H", "L015": "C", "L016": "C"}, ["L008"]),
    "S14": ({"L003": "H", "L004": "C", "L011": "H", "L012": "C", "L013": "C"}, ["L006", "L007"]),
    "S15": ({"L004": "H", "L005": "C"}, ["L006", "L008"]),
    "S16": ({"L003": "H", "L013": "H", "L014": "C", "L015": "C"}, []),
    "S17": ({"L002": "H", "L003": "C", "L008": "H", "L009": "C", "L010": "C"}, ["L004"]),
    "S18": ({"L005": "H"}, []),
    "A5": ({"L002": "H", "L003": "C", "L018": "H", "L019": "H", "L020": "C"}, ["L006", "L011", "L015"]),
    "B6": ({"L003": "H", "L004": "C", "L017": "H", "L018": "C", "L019": "H"}, ["L006", "L011", "L015"]),
    "C7": ({}, []),
}
_CODE = {"med_header": "H", "med_continuation": "C"}


def test_pinned_table_covers_every_scenario():
    assert set(PINNED) == set(_SCENARIOS)


@pytest.mark.parametrize("sid", sorted(PINNED))
def test_code_matches_on_scenario_are_pinned(sid):
    out = retrieve(no_facts(), guard(sid))
    meds = {r["line"]: _CODE[r["sources"][0]] for r in out["medication"]}
    resus = [r["line"] for r in out["resus"]]
    assert (meds, resus) == PINNED[sid]
    assert all(r["sources"] == ["resus_term"] and r["fact_ids"] == [] for r in out["resus"])


# --- recall on real step-3 output --------------------------------------------

def test_a5_every_expected_line_is_retrieved():
    out = retrieve(_PROBE["A5"], guard("A5"))
    med, resus = rows(out, "medication"), rows(out, "resus")
    # Drug history wraps L002->L003; the TTO runs L018->L019.
    assert {"L002", "L003", "L018", "L019"} <= set(med)
    # Status lines (L006, L011-L012) and the injection line that names DNACPR (L015).
    assert {"L006", "L011", "L012", "L015"} <= set(resus)


def test_a5_cited_lines_carry_their_fact_ids_and_both_sources():
    out = retrieve(_PROBE["A5"], guard("A5"))
    l011 = rows(out, "resus")["L011"]
    assert l011["sources"] == ["cited", "resus_term"]
    assert l011["fact_ids"] == ["resus"]
    # L012 is the wrapped half of the DNACPR sentence: no term on it, found only via the citation.
    assert rows(out, "resus")["L012"]["sources"] == ["cited"]


def test_a5_injection_line_is_retrieved_flagged_and_uncited():
    out = retrieve(_PROBE["A5"], guard("A5"))
    l015 = rows(out, "resus")["L015"]
    assert l015 == {"line": "L015", "sources": ["resus_term"], "fact_ids": [], "flagged": True}
    assert not any(r["flagged"] for lid, r in rows(out, "resus").items() if lid != "L015")
    assert out["counts"]["resus_matched_not_cited"] == 1


def test_s12_respect_form_line_is_retrieved():
    out = retrieve(_PROBE["S12"], guard("S12"))
    assert rows(out, "resus")["L029"]["sources"] == ["cited", "resus_term"]


def test_s12_line_found_only_by_citation_is_retrieved():
    # S12 has no medication header code can match; extraction's cited
    # discharge statement is still in the block.
    out = retrieve(_PROBE["S12"], guard("S12"))
    assert rows(out, "medication")["L045"]["sources"] == ["cited"]


def test_s8_nothing_to_retrieve():
    out = retrieve(_PROBE["S8"], guard("S8"))
    assert out["medication"] == [] and out["resus"] == []


# --- the citation rule ------------------------------------------------------

def test_unverified_citation_does_not_retrieve_its_lines():
    facts = no_facts()
    facts["medications"]["pre_admission"] = [{
        "value": "x", "fact_id": "medications.pre_admission.0",
        "cites": [{"lines": ["L009"], "quote": "x", "verified": False, "reason": "quote_not_found"}],
    }]
    out = retrieve(facts, guard("A5"))
    assert "L009" not in rows(out, "medication")


def test_verified_citation_on_any_line_is_retrieved_with_fact_id():
    facts = no_facts()
    facts["medications"]["discharge"] = [{
        "value": "x", "fact_id": "medications.discharge.0",
        "cites": [{"lines": ["L009", "L010"], "quote": "x", "verified": True, "reason": None}],
    }]
    out = retrieve(facts, guard("A5"))
    assert rows(out, "medication")["L009"] == {
        "line": "L009", "sources": ["cited"], "fact_ids": ["medications.discharge.0"], "flagged": False}
    assert "L010" in rows(out, "medication")


# --- pattern edges (synthetic one-liners, not scenario text) ------------------

def _lines(*texts):
    return {f"L{i:03d}": t for i, t in enumerate(texts, start=1)}


def _resus_hits(*texts):
    out = run_step("retrieve_evidence", {"facts": no_facts(), "lines": _lines(*texts), "flags": []})
    return [r["line"] for r in out["resus"]]


@pytest.mark.parametrize("text", [
    "DNACPR in place.", "DNA CPR discussed.", "DNA-CPR form.", "DNAR.", "Not for CPR.",
    "For resus.", "Resuscitation discussed.", "ReSPECT form completed.", "RESPECT form",
])
def test_resus_terms_match(text):
    assert _resus_hits(text) == ["L001"]


@pytest.mark.parametrize("text", [
    "Moved to resus bay.", "Seen in resus room.", "Resus area busy.",
    "Plan made with respect to her wishes.", "Respect patient wishes.",
    "Ceiling of care: ward-based.",
])
def test_resus_non_matches(text):
    assert _resus_hits(text) == []


def _med_rows(*texts):
    out = run_step("retrieve_evidence", {"facts": no_facts(), "lines": _lines(*texts), "flags": []})
    return {r["line"]: _CODE[r["sources"][0]] for r in out["medication"]}


def test_continuation_stops_at_a_full_stop():
    assert _med_rows("DH: drug a 5mg OD.", "O/E: well.") == {"L001": "H"}


def test_continuation_takes_at_most_two_lines():
    assert _med_rows("TTO: a,", "b,", "c,", "d.") == {"L001": "H", "L002": "C", "L003": "C"}


@pytest.mark.parametrize("stopper", ["", "Day 3 (10/05) seen.", "TTO: c."])
def test_continuation_stops_at_blank_day_entry_or_new_header(stopper):
    got = _med_rows("DH: a,", stopper, "b.")
    assert "L002" not in got or got["L002"] == "H"
    assert "L003" not in got


def test_dh_and_tto_are_case_sensitive():
    assert _med_rows("dh was fine", "tto") == {}


# --- contract ---------------------------------------------------------------

def test_registered_as_code_step_03b():
    step = STEPS["retrieve_evidence"]
    assert (step.seq, step.kind) == ("03b", "code")


_SAFE_STRING = re.compile(r"^L\d{3}$|^(?:cited|resus_term|med_header|med_continuation)$"
                          r"|^[a-z_]+(?:\.[a-z_]+)*(?:\.\d+)?$")   # line IDs, fixed words, fact_ids


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


@pytest.mark.parametrize("sid", ["A5", "S12", "S8"])
def test_output_carries_no_note_text(sid):
    # Every string anywhere in the output is a line ID, a fixed rule word or a
    # fact_id — so nothing from the notes can be in it.
    out = retrieve(_PROBE[sid], guard(sid))
    assert all(_SAFE_STRING.match(s) for s in _strings(out))


def test_input_is_not_mutated():
    facts = copy.deepcopy(_PROBE["A5"])
    before = copy.deepcopy(facts)
    retrieve(facts, guard("A5"))
    assert facts == before


@pytest.mark.parametrize("bad", [
    None, {}, {"facts": {}, "lines": {}, "flags": []},
    {"facts": no_facts(), "lines": {}},                      # flags missing: required
    {"facts": no_facts(), "lines": [], "flags": []},
    {"facts": no_facts(), "lines": {}, "flags": [{"rule": "x"}]},
])
def test_bad_input_fails_closed(bad):
    with pytest.raises(StepError) as e:
        run_step("retrieve_evidence", bad)
    assert e.value.code == "bad_input"
