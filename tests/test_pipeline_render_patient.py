"""Item 3 R1 step 2 — the constrained PART C renderer (pipeline/render_patient.py).

Fixtures are INVENTED from step 6's c0.2 rules (compose_clinical.md) and general vocabulary —
never the 90 saved p0.4 records (D3'); their content terms are checked clear of the scenario
files below. The fixture tables (tests/fixtures/render_tables_fixture.json) are test data, not
the step-3 tables and not a CSO approval.
"""

from __future__ import annotations

import copy
import json
import re
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline import compose_patient as CP7  # noqa: E402
from pipeline import meaning as MN  # noqa: E402
from pipeline import render as RN  # noqa: E402
from pipeline import render_patient as R  # noqa: E402
from pipeline.compose import RESUS_CONFLICT_TEXT  # noqa: E402
from pipeline.schemas import record_patient_version_schema, shape_errors  # noqa: E402

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "render_tables_fixture.json").read_text(encoding="utf-8"))
INFERRED_A = "(inferred — not documented, confirm)"


def raw_tables():
    return copy.deepcopy(FIXTURE["tables"]), FIXTURE["general_words"]


@pytest.fixture(scope="module")
def T():
    raw, words = raw_tables()
    return R.build_tables(raw, words)


def sec(i, heading, text):
    return {"section_id": f"sections.{i}", "heading": heading, "text": text}


def full_input(audience="adult"):
    return {"audience": audience, "sections": [
        sec(0, "presenting_complaint", "Three days of sore throat and fever. Unable to swallow."),
        sec(1, "diagnosis", "Primary: Right peritonsillar abscess (high risk)\nSecondary: ?Dehydration\nBackground: Asthma"),
        sec(2, "key_investigations", "- White cell count 18 → 11\n- No collection on repeat examination"),
        sec(3, "treatment", "- Needle aspiration on Day 1\n- IV phenoxymethylpenicillin\n"
                            f"- Oral fluids encouraged {INFERRED_A}"),
        sec(4, "follow_up", "- ENT clinic review in 6 weeks\n- Advised to avoid contact sports until reviewed"),
        sec(5, "gp_actions", "- GP to check fluid intake at 1 week"),
    ]}


def two(pc_text, other_heading=None, other_text=None, audience="adult"):
    s = [sec(0, "presenting_complaint", pc_text)]
    if other_heading:
        s.append(sec(1, other_heading, other_text))
    return {"audience": audience, "sections": s}


def c_text(out, heading):
    return next((s["text"] for s in out["sections"] if s["heading"] == heading), None)


def refusal(inp, tables):
    with pytest.raises(R.RenderRefusal) as e:
        R.render_patient(inp, tables)
    return e.value


# --- fixtures are clear of the scenario files -------------------------------------------------------

def test_fixture_terms_are_clear_of_the_scenario_files():
    scen = "\n".join(p.read_text(encoding="utf-8").lower() for p in (ROOT / "evals" / "scenarios").glob("*.md"))
    terms = ("peritonsillar", "quinsy", "tonsil", "needle aspiration", "phenoxymethylpenicillin", "sore throat",
             "contact sports", "fluid intake", "abscess", "asthma", "wells score", "police informed",
             "emergency department", "antibiotic supply", "oral fluids", "white cell count 18")
    for t in terms:
        assert t not in scen, t


# --- the package tables: approved (step 3, 11 Nov 2026), pinned -------------------------------------------
# (the content of the shipped tables is tested in test_render_tables_approved.py)

def test_package_tables_are_the_approved_set_and_match_their_pins():
    t = R.load_tables()
    assert t.status == "approved"
    assert set(t.sha256) == set(R.TABLE_FILES) and t.sha256 == R.TABLE_SHA256
    out = R.render_patient(two("Two days of dyspnoea."), t)
    assert "breathlessness (dyspnoea)" in c_text(out, "why_in_hospital")
    assert out["renderer"]["tables_status"] == "approved" and out["renderer"]["version"] == 2


def test_a_changed_table_byte_is_tables_changed(tmp_path):
    d = tmp_path / "t"
    shutil.copytree(R.TABLES_DIR, d)
    p = d / "glossary.json"
    p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(R.RenderRefusal) as e:
        R.load_tables(d)
    assert e.value.code == "tables_changed"
    assert R.load_tables(d, pins=None).status == "approved"     # the content itself is still valid


def test_the_shipped_inferred_entry_is_the_adopted_wording():
    t = R.load_tables()
    inf = [e for e in t.phrases if e.raw["kind"] == "inferred"]
    assert len(inf) == 1 and set(inf[0].raw["plain"].values()) == {R.ADOPTED_INFERRED}
    assert R.ADOPTED_INFERRED == "(the notes point to this but do not say it; the team must check it)"


# --- table validation, each rule firing ----------------------------------------------------------

def _mutated(fn):
    raw, words = raw_tables()
    words = fn(raw) or words
    return R.validate_tables(raw, words)


@pytest.mark.parametrize("fn, problem", [
    (lambda r: r["glossary"]["entries"][0].update(plain="a collection of pus that is not on the left"),
     "glossary:g_quinsy:adds_protected_token"),
    (lambda r: r["glossary"]["entries"][0].update(plain="a collection that is fully drained"),
     "glossary:g_quinsy:check_word_undeclared"),
    (lambda r: r["glossary"]["entries"][0].update(flag_words=["must"]), "glossary:g_quinsy:flag_word_not_in_plain"),
    (lambda r: r["drug_classes"]["entries"][0].update(forms=["Quinsy"]), "drug_classes:d_pen:form_in_two_entries"),
    (lambda r: r["phrases"]["entries"][-1]["plain"].update(adult="(not yet confirmed)"),
     "phrases:inferred:not_the_adopted_wording"),
    (lambda r: r["withheld"]["entries"].append(
        {"id": "w_bad", "category": "x", "pattern": r"self-harm|overdose", "number_slot": False, "examples": ["overdose"]}),
     "withheld:w_bad:matches_f3_word"),
    (lambda r: r["withheld"]["entries"].append(
        {"id": "w_neg", "category": "x", "pattern": r"no police", "number_slot": False, "examples": ["no police"]}),
     "withheld:w_neg:protected_token"),
    (lambda r: r["home_medicine"]["entries"].append(
        {"id": "h_od", "category": "x", "pattern": r"overdose kit", "number_slot": False, "examples": ["overdose kit"]}),
     "home_medicine:h_od:example_holds_f3_word"),
    (lambda r: r["withheld"]["entries"][1].update(examples=["high risk 3"]), "withheld:w_level:digit_without_number_slot"),
    (lambda r: r["withheld"]["entries"][1].update(examples=["risk"]), "withheld:w_level:example_not_matched"),
    (lambda r: r["seek_help"]["entries"][0].update(pattern=r"call 999", examples=["call 999"], number_slot=True),
     "seek_help:s_ed:instruction_marker"),
    (lambda r: r["withheld"]["entries"][2].update(pattern=r"police \w+", examples=["police clinic"]),
     "withheld:w_police:required_content"),
    (lambda r: r["lead_ins"]["entries"][0].update(text="Why you came in today:"), "lead_ins:why_in_hospital/adult:protected_token"),
    (lambda r: r["lead_ins"]["entries"][0].update(text="Please read:"), "lead_ins:why_in_hospital/adult:check_word"),
    (lambda r: (r["lead_ins"]["entries"].pop(0), None)[1], "lead_ins:why_in_hospital/adult:missing"),
    (lambda r: "general words\n", "general_words:header"),
    (lambda r: r["glossary"].update(approved_by=None), "glossary:approval_fields"),
    (lambda r: r["phrases"]["entries"][3].update(keeps={}), "phrases:p_query:adds_protected_token"),
    (lambda r: r["phrases"]["entries"][0].update(forms=["tertiary"]), "phrases:p_primary:label_form"),
])
def test_each_validation_rule_fires(fn, problem):
    assert problem in _mutated(fn)


def test_the_fixture_tables_are_valid(T):
    raw, words = raw_tables()
    assert R.validate_tables(raw, words) == []
    assert T.status == "approved"


def test_invalid_tables_refuse_with_tables_invalid():
    raw, words = raw_tables()
    raw["glossary"]["entries"][0]["plain"] = "only a collection"
    with pytest.raises(R.RenderRefusal) as e:
        R.build_tables(raw, words)
    assert e.value.code == "tables_invalid"


# --- the grammar (D7.4; decision 1 (b)) --------------------------------------------------------------

@pytest.mark.parametrize("label", ["Primary:", "PRIMARY:", "primary diagnosis:", "Primary diagnoses:"])
def test_label_forms_accepted(T, label):
    out = R.render_patient(two("Fever.", "diagnosis", f"{label} Right quinsy"), T)
    assert "Main problem: Right a collection of pus beside the tonsil (quinsy)" in c_text(out, "why_in_hospital")


def test_label_alone_then_items(T):
    out = R.render_patient(two("Fever.", "diagnosis", "Primary:\n- Right quinsy\nBackground:\n- Asthma"), T)
    lines = c_text(out, "why_in_hospital").split("\n")
    assert lines[-4:] == ["Main problem:", "- Right a collection of pus beside the tonsil (quinsy)",
                          "Health conditions you have:", "- Asthma"]
    forms = [x["form"] for x in out["ledger"]["lines"]]
    assert forms == ["prose", "label", "label_item", "label", "label_item"]


@pytest.mark.parametrize("heading, text", [
    ("diagnosis", "Right quinsy"),                       # free prose in diagnosis
    ("diagnosis", "- Right quinsy"),                     # an item before any label
    ("diagnosis", "Primary Right quinsy"),               # no colon
    ("diagnosis", "Primary  diagnosis: quinsy"),         # two spaces
    ("treatment", "# Treatment"),
    ("treatment", "* aspiration"),
    ("treatment", "• aspiration"),
    ("treatment", "1. aspiration"),
    ("treatment", "| a | b |"),
    ("treatment", "  - aspiration"),
    ("treatment", "-aspiration"),
    ("treatment", "- "),
    ("treatment", "- aspiration {{RESUS}}"),
    ("treatment", "Fluids encouraged, inferred — not documented, confirm"),
])
def test_unsupported_forms_refuse_with_where(T, heading, text):
    e = refusal(two("Fever.", heading, text), T)
    assert e.code == "unsupported_form"
    assert e.where == {"source_id": "sections.1", "line": 1, "sentence": None}


def test_prose_is_taken_sentence_by_sentence(T):
    out = R.render_patient(two("Fever for a week. Unable to swallow! Seen once?"), T)
    assert c_text(out, "why_in_hospital").split("\n")[1:] == ["Fever for a week.", "Unable to swallow!", "Seen once?"]
    assert [x["sentence"] for x in out["ledger"]["lines"]] == [1, 2, 3]


# --- the four transformations ------------------------------------------------------------------------

def test_full_fixture_renders_as_expected(T):
    out = R.render_patient(full_input(), T)
    assert c_text(out, "why_in_hospital") == (
        "Why you came in:\nThree days of sore throat and fever.\nUnable to swallow.\n"
        "Main problem: Right a collection of pus beside the tonsil (peritonsillar abscess)\n"
        "Also found: possible Dehydration\nHealth conditions you have: Asthma")
    assert c_text(out, "what_we_did") == (
        "What the team did:\n- White cell count 18 → 11\n- No collection on repeat examination\n"
        "- Removing fluid through a needle (Needle aspiration) on Day 1\n"
        "- Into a vein (IV) an antibiotic (phenoxymethylpenicillin)\n"
        f"- Drinks encouraged {R.ADOPTED_INFERRED}")
    assert c_text(out, "next_steps") == (
        "What happens next:\n- Ear, nose and throat (ENT) clinic review in 6 weeks\n"
        "- Advised to avoid contact sports until reviewed\n- GP to check fluid intake at 1 week")
    assert [s["source_ids"] for s in out["sections"]] == [
        ["sections.0", "sections.1"], ["sections.2", "sections.3"], [], ["sections.4", "sections.5"], []]
    assert out["ledger"]["drops"] == [{"rule": "withheld", "entry": "w_level", "source_id": "sections.1", "line": 1,
                                       "sentence": None, "text": " (high risk)"}]


def test_longest_match_and_the_term_in_brackets_verbatim(T):
    out = R.render_patient(two("Fever.", "treatment", "- needle aspiration and aspiration again"), T)
    assert "- removing fluid through a needle (needle aspiration) and removing fluid (aspiration) again" \
        in c_text(out, "what_we_did")


def test_case_rule_capitalises_only_at_line_start(T):
    out = R.render_patient(two("Fever.", "treatment", "- given IV fluids\n- IV fluids\n- ?Quinsy drained"), T)
    assert c_text(out, "what_we_did").split("\n")[1:] == [
        "- given into a vein (IV) fluids", "- Into a vein (IV) fluids",
        "- Possible a collection of pus beside the tonsil (Quinsy) drained"]


def test_case_sensitive_entry_does_not_match_other_case(T):
    out = R.render_patient(two("Fever.", "treatment", "- ent review"), T)
    assert c_text(out, "what_we_did").endswith("- ent review")


def test_query_forms(T):
    out = R.render_patient(two("Fever.", "diagnosis", "Primary: ?quinsy\nSecondary: query dehydration\nBackground: ? asthma"), T)
    lines = c_text(out, "why_in_hospital").split("\n")
    assert lines[2:] == ["Main problem: possible a collection of pus beside the tonsil (quinsy)",
                         "Also found: possible dehydration", "Health conditions you have: ? asthma"]


def test_person_and_label_by_audience(T):
    out = R.render_patient(two("The patient had a sore throat.", "diagnosis", "Background: Asthma",
                               audience="parent_or_carer"), T)
    assert c_text(out, "why_in_hospital") == ("Why your child came in:\nYour child had a sore throat.\n"
                                              "Health conditions your child has: Asthma")


def test_inferred_and_uncertain_lines_are_distinguishable(T):
    out = R.render_patient(two("Fever.", "treatment", f"- Oral fluids encouraged {INFERRED_A}\n- ?quinsy drained"), T)
    lines = c_text(out, "what_we_did").split("\n")
    assert lines[1] == f"- Drinks encouraged {R.ADOPTED_INFERRED}"
    assert lines[2] == "- possible a collection of pus beside the tonsil (quinsy) drained"
    assert not any(c["check"] == "not_documented_text" for c in out["checks"])


def test_empty_lead_in_writes_no_line(T):
    raw, words = raw_tables()
    for e in raw["lead_ins"]["entries"]:
        e["text"] = ""
    t = R.build_tables(raw, words)
    out = R.render_patient(two("Fever."), t)
    assert c_text(out, "why_in_hospital") == "Fever."


def test_negation_planned_procedure_and_conditional_instruction_are_copied(T):
    inp = {"audience": "adult", "sections": [
        sec(0, "presenting_complaint", "Fever."),
        sec(1, "key_investigations", "- No collection on repeat examination"),
        sec(2, "follow_up", "- For tonsillectomy in 6 weeks\n- Advised to avoid contact sports until reviewed")]}
    out = R.render_patient(inp, T)
    assert "- No collection on repeat examination" in c_text(out, "what_we_did")
    assert c_text(out, "next_steps").split("\n")[1:] == [
        "- For an operation to remove the tonsils (tonsillectomy) in 6 weeks",
        "- Advised to avoid contact sports until reviewed"]


def test_unknown_words_are_kept_and_listed(T):
    out = R.render_patient(two("Fever with trismus."), T)
    assert "Fever with trismus." in c_text(out, "why_in_hospital")
    assert {"source_id": "sections.0", "line": 1, "sentence": 1, "word": "trismus"} in out["ledger"]["unknown_terms"]


# --- drops: authorised for the whole span (Round 2 fix 1) ---------------------------------------------

@pytest.mark.parametrize("text, expected, unit", [
    ("Primary: Right quinsy (high risk)", "Main problem: Right a collection of pus beside the tonsil (quinsy)", "aside"),
    ("Primary: Right quinsy — high risk", "Main problem: Right a collection of pus beside the tonsil (quinsy)", "dash"),
    ("Primary: Right quinsy; high risk", "Main problem: Right a collection of pus beside the tonsil (quinsy)", "semicolon"),
    ("Primary: high risk, Right quinsy", "Main problem: Right a collection of pus beside the tonsil (quinsy)", "comma"),
    ("Primary: Right quinsy (Wells score 4)", "Main problem: Right a collection of pus beside the tonsil (quinsy)", "aside"),
])
def test_withheld_units_dropped_in_a_diagnosis_line(T, text, expected, unit):
    out = R.render_patient(two("Fever.", "diagnosis", text), T)
    assert c_text(out, "why_in_hospital").split("\n")[2] == expected
    ops = out["ledger"]["lines"][1]["ops"]
    assert [o["unit"] for o in ops if o["op"] == "drop"] == [unit]


def test_a_whole_item_may_drop_outside_pc_and_diagnosis(T):
    out = R.render_patient(two("Fever.", "treatment", "- Police informed\n- Needle aspiration"), T)
    assert c_text(out, "what_we_did").split("\n")[1:] == ["- Removing fluid through a needle (Needle aspiration)"]
    assert out["ledger"]["lines"][1]["c_line"] is None
    assert out["ledger"]["counts"]["dropped_whole"] == 1


@pytest.mark.parametrize("inp", [
    two("Police informed."),                                          # a whole PC sentence
    two("Fever.", "diagnosis", "Primary: high risk"),                 # the rest would hold no content word
    two("Fever.", "treatment", "- Aspiration (police informed at clinic)"),   # required content in the unit
    two("Fever.", "treatment", "- Aspiration (high risk 4)"),                 # a value outside a number slot
    two("Fever.", "treatment", "- Aspiration (high risk of spread)"),         # not fully covered
    two("Fever.", "diagnosis", "Primary: high risk, the"),                    # (v): the rest keeps no content word
])
def test_withheld_without_an_authorised_unit_refuses(T, inp):
    assert refusal(inp, T).code == "withheld_not_isolable"


def test_resus_pattern_with_an_authorised_drop_and_without(T):
    out = R.render_patient(two("Collapse at home (DNACPR)."), T)
    assert c_text(out, "why_in_hospital").split("\n")[1] == "Collapse at home."
    assert out["ledger"]["drops"][0]["entry"] == R.RESUS_ENTRY
    assert refusal(two("Collapse at home (DNACPR in place)."), T).code == "resus_not_isolable"


def test_a_signpost_match_is_screening_only(T):
    # Codex 07:38:27: a past treatment AT an emergency department must never be dropped.
    e = refusal(two("Fever.", "treatment", "- Wound repaired in the emergency department."), T)
    assert e.code == "signpost_not_isolable"


def test_an_isolable_seek_help_clause_is_dropped(T):
    out = R.render_patient(two("Fever.", "follow_up", "- Leaflet on throat infections given, attend the emergency department"), T)
    assert c_text(out, "next_steps").split("\n")[1:] == ["- Leaflet on throat infections given"]
    assert out["ledger"]["drops"][0]["rule"] == "seek_help"


def test_an_instruction_overlap_refuses(T):
    for text in ("- Advised to attend the emergency department",
                 "- Advised to rest, attend the emergency department"):
        assert refusal(two("Fever.", "follow_up", text), T).code == "instruction_overlap"


def test_home_medicine_covered_drops_uncovered_copies(T):
    out = R.render_patient(two("Fever.", "treatment", "- Aspiration done, antibiotic supply given"), T)
    assert c_text(out, "what_we_did").split("\n")[1:] == ["- Removing fluid (Aspiration) done"]
    out = R.render_patient(two("Fever.", "treatment", "- Aspiration done, antibiotic supply given for 5 days"), T)
    assert c_text(out, "what_we_did").split("\n")[1:] == [
        "- Removing fluid (Aspiration) done, antibiotic supply given for 5 days"]


def test_resus_conflict_sentence_is_dropped_only_in_treatment(T):
    out = R.render_patient(two("Fever.", "treatment", f"- Needle aspiration\n- {RESUS_CONFLICT_TEXT}"), T)
    assert c_text(out, "what_we_did").split("\n")[1:] == ["- Removing fluid through a needle (Needle aspiration)"]
    d = out["ledger"]["drops"][0]
    assert d["rule"] == "resus_conflict" and d["entry"] is None
    assert refusal(two("Fever.", "follow_up", f"- {RESUS_CONFLICT_TEXT}"), T).code == "conflict_in_input"


def test_notes_conflict_refuses_even_with_required_content(T):
    e = refusal(two("Fever.", "treatment", "- Needle aspiration on Day 1. Notes conflict: side of abscess — requires "
                                         "clarification."), T)
    assert e.code == "conflict_in_input"


def test_a_wholly_dropped_section_is_visible_in_the_unchanged_checks(T):
    out = R.render_patient(two("Fever.", "treatment", "- Police informed"), T)
    assert c_text(out, "what_we_did") is None
    got = {(c["check"], c["heading"] or c["source_id"]) for c in out["checks"]}
    assert ("required_heading_missing", "what_we_did") in got and ("source_unused", "sections.1") in got


def test_suicidal_thoughts_in_the_reason_for_admission_are_kept(T):
    out = R.render_patient(two("Overdose with suicidal thoughts."), T)
    assert c_text(out, "why_in_hospital").split("\n")[1] == "Overdose with suicidal thoughts."
    assert any(c["check"] == "sensitive_text" for c in out["checks"])        # flagged for the CSO, not dropped


# --- the self-checks ------------------------------------------------------------------------------

def _unit(text, heading="treatment", form="item"):
    return {"source_id": "sections.1", "line": 1, "sentence": None, "form": form, "label": None,
            "marker": "- " if form == "item" else "", "text": text}


def test_verify_line_partition_reproduction_and_protected(T):
    u = _unit("No collection on Day 1")
    ok = [{"op": "copy", "a": 0, "b": len(u["text"])}]
    assert R.verify_line(u, "- No collection on Day 1", ok, T) == []
    assert R.verify_line(u, "- No collection", ok, T) == ["reproduction", "protected_lost"]   # a clause lost
    assert R.verify_line(u, "- No collection on", ok, T) == ["reproduction", "protected_lost"]
    assert R.verify_line(u, "- No collection on Day 1", [{"op": "copy", "a": 0, "b": 5}], T) == ["partition"]
    split = [{"op": "copy", "a": 0, "b": 4}, {"op": "copy", "a": 4, "b": len(u["text"])}]
    assert R.verify_line(u, "- No collection on Day 1", split, T) == ["partition_inside_word"]
    lost = [{"op": "sub", "a": 0, "b": 2, "table": "phrases", "entry": "p_patient", "out": "you"},
            {"op": "copy", "a": 2, "b": len(u["text"])}]
    assert R.verify_line(u, "- you collection on Day 1", lost, T) == ["protected_lost"]


def test_authorise_ops_catches_a_fabricated_substitution_and_a_false_drop(T):
    u = _unit("Needle aspiration in 6 weeks")
    good, c = R.render_line(u, "treatment", "adult", T)
    assert R.authorise_ops(u, "treatment", "adult", good, T) == []
    fab = copy.deepcopy(good)
    fab[0]["out"] = "a minor procedure (Needle aspiration)"
    assert R.authorise_ops(u, "treatment", "adult", fab, T) == ["op0:sub_out_differs"]
    wrong_entry = [{"op": "sub", "a": 0, "b": 6, "table": "glossary", "entry": "g_needle", "out": "x"},
                   {"op": "copy", "a": 6, "b": len(u["text"])}]
    assert "op0:sub_not_an_entry_form" in R.authorise_ops(u, "treatment", "adult", wrong_entry, T)
    false_drop = [good[0], {"op": "drop", "a": 17, "b": len(u["text"]), "rule": "withheld", "entry": "w_level",
                            "unit": "line"}]
    probs = R.authorise_ops(u, "treatment", "adult", false_drop, T)
    assert probs and all(p.startswith("op1:drop_") for p in probs)
    assert R.authorise_ops(u, "treatment", "adult", [{"op": "copy", "a": 0, "b": len(u["text"])}], T) == []
    assert R.authorise_ops(_unit("seen in the emergency department"), "treatment", "adult",
                           [{"op": "copy", "a": 0, "b": 32}], T) == ["op0:copy_holds_resus_or_signpost"]


def test_a_builder_fault_fails_closed(T, monkeypatch):
    def lossy(u, heading, audience, tables):
        ops = [{"op": "sub", "a": 0, "b": len(u["text"]), "table": "phrases", "entry": "p_patient", "out": "you"}]
        return ops, u["marker"] + "you"
    monkeypatch.setattr(R, "render_line", lossy)
    assert refusal(two("No fever."), T).code == "meaning_token_lost"

    def unaccounted(u, heading, audience, tables):
        return [{"op": "copy", "a": 0, "b": len(u["text"])}], u["marker"] + "something else"
    monkeypatch.setattr(R, "render_line", unaccounted)
    assert refusal(two("Fever."), T).code == "accounting_failed"

    def unauthorised(u, heading, audience, tables):
        return ([{"op": "drop", "a": 0, "b": len(u["text"]), "rule": "withheld", "entry": "w_level", "unit": "line"}],
                None)
    monkeypatch.setattr(R, "render_line", unauthorised)
    assert refusal(two("Fever."), T).code == "op_not_authorised"


def test_reason_dropped_fails_closed(T, monkeypatch):
    real = R.split_lines
    monkeypatch.setattr(R, "split_lines", lambda s: [] if s["heading"] == "presenting_complaint" else real(s))
    assert refusal(two("Fever."), T).code == "reason_dropped"


# --- audit: the ordinary evaluator's re-check of a saved output ------------------------------------

def _tamper_line(out, heading, idx, new):
    out = copy.deepcopy(out)
    s = next(s for s in out["sections"] if s["heading"] == heading)
    lines = s["text"].split("\n")
    lines[idx] = new
    s["text"] = "\n".join(lines)
    return out


def test_audit_is_clean_on_a_real_output(T):
    inp = full_input()
    assert R.audit(inp, R.render_patient(inp, T), T) == []


def test_audit_catches_a_clause_lost_in_a_rendered_cited_line(T):
    inp = full_input()
    out = _tamper_line(R.render_patient(inp, T), "next_steps", 2, "- Advised to avoid contact sports")
    probs = R.audit(inp, out, T)
    assert any(p.endswith(":reproduction") for p in probs) and "rerender_differs" in probs
    # the section is still cited and the unchanged checks raise nothing about it (line 9 stays MET)
    _s, checks, _c = CP7.check_patient_version({"sections": [{k: s[k] for k in ("heading", "source_ids", "text")}
                                                             for s in out["sections"]]}, inp)
    assert not any(c["check"] in ("follow_up_missing", "source_unused") for c in checks)


def test_audit_catches_a_fabricated_substitution_and_a_falsely_labelled_drop(T):
    inp = full_input()
    out = R.render_patient(inp, T)
    # fabricated substitution: the C line and its op's out changed together (reproduction still holds)
    fab = _tamper_line(out, "what_we_did", 3, "- A minor procedure (Needle aspiration) on Day 1")
    line = next(x for x in fab["ledger"]["lines"] if x["source_id"] == "sections.3" and x["line"] == 1)
    line["ops"][0]["out"] = "A minor procedure (Needle aspiration)"
    probs = R.audit(inp, fab, T)
    assert any(p.endswith("op0:sub_out_differs") for p in probs)
    assert not any(p.endswith(":reproduction") for p in probs)
    # falsely labelled drop: "clinic review in 6 weeks" dropped under a withheld label, C line consistent
    fd = _tamper_line(out, "next_steps", 1, "- Ear, nose and throat (ENT)")
    line = next(x for x in fd["ledger"]["lines"] if x["source_id"] == "sections.4" and x["line"] == 1)
    t = "ENT clinic review in 6 weeks"
    line["ops"] = [line["ops"][0], {"op": "drop", "a": 3, "b": len(t), "rule": "withheld", "entry": "w_level",
                                    "unit": "line"}]
    probs = R.audit(inp, fd, T)
    assert any(":op1:drop_" in p for p in probs)
    assert not any(p.endswith(":reproduction") for p in probs)
    _s, checks, _c = CP7.check_patient_version({"sections": [{k: s[k] for k in ("heading", "source_ids", "text")}
                                                             for s in fd["sections"]]}, inp)
    assert not any(c["check"] == "follow_up_missing" for c in checks)        # still cited: line 9 MET


def test_audit_catches_a_ledger_that_does_not_match_the_input(T):
    inp = full_input()
    out = R.render_patient(inp, T)
    other = copy.deepcopy(inp)
    other["sections"][2]["text"] = "- White cell count 18 → 11\n- Collection on repeat examination"
    assert R.audit(other, out, T)


# --- output contract ------------------------------------------------------------------------------

def test_output_contract(T):
    inp = full_input()
    out = R.render_patient(inp, T)
    assert set(out) == {"sections", "checks", "counts", "audience", "revised", "renderer", "ledger"}
    assert "call" not in out and out["revised"] is False and out["audience"] == "adult"
    wire = {"sections": [{k: s[k] for k in ("heading", "source_ids", "text")} for s in out["sections"]]}
    assert shape_errors(wire, record_patient_version_schema()) == []
    sections, checks, counts = CP7.check_patient_version(wire, inp)
    assert (sections, checks, counts) == (out["sections"], out["checks"], out["counts"])
    assert [s["text"] for s in out["sections"] if s["heading"] in CP7.BOXES] == ["{{MEDICATIONS}}", "{{SAFETY_NET}}"]
    assert set(out["renderer"]) == {"name", "version", "module_sha256", "tables", "tables_status"}
    assert out["renderer"]["module_sha256"] == R.module_sha256()
    step7 = {"audience": "adult", "sections": out["sections"]}
    c = RN.assemble_part_c(step7, {"reconciliation": [], "discharge_status": "none_required"},
                           {"part_c": "BOX"}, [])
    assert c.startswith(RN.TITLES["adult"]) and "BOX" in c and "{{" not in c


def test_input_contract(T):
    inp = full_input()
    inp["revision"] = {"previous": [], "findings": []}
    assert refusal(inp, T).code == "bad_input"
    assert refusal({"audience": "adult", "sections": [sec(0, "treatment", "- x")]}, T).code == "no_sections_given"
    assert refusal({"audience": "adult", "sections": [sec(0, "risk_assessment", "x")]}, T).code == "bad_input"


def test_refusal_codes_are_the_adopted_list():
    assert R.REFUSALS == ("bad_input", "no_sections_given", "unsupported_form", "conflict_in_input",
                          "resus_not_isolable", "signpost_not_isolable", "withheld_not_isolable",
                          "instruction_overlap", "reason_dropped", "accounting_failed", "op_not_authorised",
                          "meaning_token_lost", "patient_version_shape_invalid", "tables_invalid", "tables_changed")
    src = (ROOT / "src" / "generate" / "pipeline" / "render_patient.py").read_text(encoding="utf-8")
    for code in R.REFUSALS:                       # each code is raised somewhere (beyond its listing)
        if code != "no_sections_given":           # raised by compose_patient._check_input, passed through
            assert src.count(f'"{code}"') >= 2, code


def test_not_registered_as_a_step():
    from pipeline import registry
    assert "render_patient" not in registry.STEPS
    assert all(s.fn is not R.render_patient for s in registry.STEPS.values())


# --- protected tokens: aligned with meaning.py's qualifier groups --------------------------------------

def test_every_meaning_qualifier_word_is_protected():
    probes = ("improving improved initially initial possible possibly probably probable likely suspected query ? "
              "partially partly mild mildly until unless ongoing awaited pending may might").split()
    probes += ["at first", "treated as", "concerning for", "only if", "for now", "for the time being", "not yet"]
    for name, in_src, _kept in MN._QUALIFIERS:
        hit = [p for p in probes if in_src.search(p)]
        assert hit, name
        for p in hit:
            assert R.protected(p), (name, p)


def test_protected_classes():
    c = R.protected("No clot on Day 3 of 2/7; troponin 287 → 412 ng/L, only left side, not yet, didn't")
    for k in ("no", "#3", "#2/7", "#287", "#412", "unit:ng/l", "day", "only", "left", "not", "phrase:not yet", "n't"):
        assert c[k] >= 1, k


# --- determinism and purity -------------------------------------------------------------------------

def test_deterministic_in_process(T):
    a = json.dumps(R.render_patient(full_input(), T), sort_keys=True)
    b = json.dumps(R.render_patient(full_input(), T), sort_keys=True)
    assert a == b


def test_deterministic_across_hash_seeds():
    code = ("import json,sys; sys.path.insert(0,'src/generate'); sys.path.insert(0,'tests');"
            "import test_pipeline_render_patient as t; from pipeline import render_patient as R;"
            "raw,w=t.raw_tables(); T=R.build_tables(raw,w);"
            "print(json.dumps(R.render_patient(t.full_input(),T)))")
    outs = []
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"}
        r = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                           check=True)
        outs.append(r.stdout)
    assert outs[0] == outs[1] == outs[2] and outs[0]


def test_pure_no_network_no_writes(T, monkeypatch):
    import builtins
    import socket
    real_open = builtins.open

    def no_write(file, mode="r", *a, **k):
        if any(m in mode for m in "wax+"):
            raise AssertionError("write attempted")
        return real_open(file, mode, *a, **k)

    def no_socket(*a, **k):
        raise AssertionError("network attempted")
    monkeypatch.setattr(builtins, "open", no_write)
    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("write")))
    monkeypatch.setattr(Path, "write_bytes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("write")))
    R.render_patient(full_input(), T)
    R.render_patient(full_input(), R.load_tables())


def test_authorise_ops_unit_and_entry_claims_are_checked(T):
    u = _unit("Right quinsy high risk now")
    a = u["text"].index(" high risk")
    not_a_unit = [{"op": "copy", "a": 0, "b": a},
                  {"op": "drop", "a": a, "b": a + 10, "rule": "withheld", "entry": "w_level", "unit": "comma"},
                  {"op": "copy", "a": a + 10, "b": len(u["text"])}]
    assert R.authorise_ops(u, "treatment", "adult", not_a_unit, T) == ["op1:drop_not_a_unit"]
    u2 = _unit("Aspiration (police informed)")
    good, _c = R.render_line(u2, "treatment", "adult", T)
    drop = next(o for o in good if o["op"] == "drop")
    assert drop["entry"] == "w_police"
    wrong = copy.deepcopy(good)
    next(o for o in wrong if o["op"] == "drop")["entry"] = "w_level"
    assert any(p.endswith("drop_entry_not_in_span") for p in R.authorise_ops(u2, "treatment", "adult", wrong, T))


def test_audit_catches_a_line_with_no_ledger_entry(T):
    inp = full_input()
    out = copy.deepcopy(R.render_patient(inp, T))
    s = next(s for s in out["sections"] if s["heading"] == "next_steps")
    s["text"] += "\n- Call your GP if worse"
    probs = R.audit(inp, out, T)
    assert "next_steps:section_text_differs" in probs and "rerender_differs" in probs


def test_required_content_inside_a_covered_unit_refuses():
    # An entry whose pattern is broader than its (clean) examples: the unit is fully covered, but holds a
    # required-content word, so it is never dropped (Round 2 (iii)).
    raw, words = raw_tables()
    raw["withheld"]["entries"][2].update(pattern=r"police \w+", examples=["police informed"])
    t = R.build_tables(raw, words)
    assert R.render_patient(two("Fever.", "treatment", "- Aspiration (police informed)"), t)
    assert refusal(two("Fever.", "treatment", "- Aspiration (police clinic)"), t).code == "withheld_not_isolable"


# --- Round 4 (Codex 09:04:06) ------------------------------------------------------------------------

def _tables_with(**changes):
    """The fixture tables with entries changed or added (still valid tables — asserted)."""
    raw, words = raw_tables()
    for fn in changes.values():
        fn(raw)
    assert R.validate_tables(raw, words) == []
    return R.build_tables(raw, words)


BROAD_POLICE = {"police": lambda r: r["withheld"]["entries"][2].update(
    pattern=r"police(?: informed)?(?: of self-harm)?", examples=["police informed", "police"])}


def test_f3_is_checked_on_the_actual_dropped_span():
    # P1: a withheld pattern broader than its clean examples passes validation (its F3 probe test looks
    # at standalone words) but must never remove F3 words from a real line — builder or audit.
    t = _tables_with(**BROAD_POLICE)
    out = R.render_patient(two("Admitted with collapse (police informed)."), t)        # police only: dropped
    assert c_text(out, "why_in_hospital").split("\n")[1] == "Admitted with collapse."
    assert refusal(two("Admitted with collapse (police informed of self-harm)."), t).code == "withheld_not_isolable"
    assert refusal(two("Fever.", "treatment", "- Police informed of self-harm"), t).code == "withheld_not_isolable"
    text = "Admitted with collapse (police informed of self-harm)."
    u = _unit(text, "presenting_complaint", "prose")
    a = text.index(" (")
    forged = [{"op": "copy", "a": 0, "b": a},
              {"op": "drop", "a": a, "b": len(text) - 1, "rule": "withheld", "entry": "w_police", "unit": "aside"},
              {"op": "copy", "a": len(text) - 1, "b": len(text)}]
    assert R.authorise_ops(u, "presenting_complaint", "adult", forged, t) == ["op1:drop_f3_content"]


def test_audit_rejects_an_output_that_removed_f3_words(monkeypatch):
    # An output built WITHOUT the F3 span check (as a faulty builder would) is rejected by the evaluator.
    t = _tables_with(**BROAD_POLICE)
    inp = two("Admitted with collapse (police informed of self-harm).")
    monkeypatch.setattr(R, "F3_NEVER_DROP", re.compile(r"(?!x)x"))
    bad = R.render_patient(inp, t)
    assert c_text(bad, "why_in_hospital").split("\n")[1] == "Admitted with collapse."
    monkeypatch.undo()
    probs = R.audit(inp, bad, t)
    assert "sections.0:1:1:op1:drop_f3_content" in probs and "rerender_refused:withheld_not_isolable" in probs


SPLITTERS = {"out": lambda r: r["phrases"]["entries"].append(
                 {"id": "p_out", "kind": "literal", "forms": ["out"], "plain": {"adult": "outside", "parent_or_carer": "outside"},
                  "keeps": {}, "flag_words": []}),
             "treated": lambda r: r["phrases"]["entries"].append(
                 {"id": "p_treated", "kind": "literal", "forms": ["treated"],
                  "plain": {"adult": "cared for", "parent_or_carer": "cared for"}, "keeps": {}, "flag_words": []})}


@pytest.mark.parametrize("text", ["Asthma ruled out.", "Treated as a throat infection."])
def test_a_protected_phrase_split_across_ops_is_caught(text):
    # P2: a schema-valid literal substitution of one word of "ruled out" / "treated as" splits the phrase
    # over copy / sub / copy ops; no single op holds it, but its loss must still fail closed.
    t = _tables_with(**SPLITTERS)
    assert refusal(two(text), t).code == "meaning_token_lost"
    u = _unit(text, "presenting_complaint", "prose")
    ops, c_line = R.render_line(u, "presenting_complaint", "adult", t)
    assert [o["op"] for o in ops][:2] in (["copy", "sub"], ["sub", "copy"])
    assert R.verify_line(u, c_line, ops, t) == ["protected_lost"]


def test_unchanged_copies_and_the_adopted_transformations_still_pass(T):
    t = _tables_with(**SPLITTERS)
    for tables in (T, t):
        u = _unit("Asthma ruled out", "presenting_complaint", "prose")
        whole = [{"op": "copy", "a": 0, "b": len(u["text"])}]
        assert R.verify_line(u, "Asthma ruled out", whole, tables) == []
    out = R.render_patient(two("?Quinsy ruled out at first.", "treatment", f"- Oral fluids encouraged {INFERRED_A}"), T)
    assert c_text(out, "why_in_hospital").split("\n")[1] == \
        "Possible a collection of pus beside the tonsil (Quinsy) ruled out at first."
    assert c_text(out, "what_we_did").split("\n")[1] == f"- Drinks encouraged {R.ADOPTED_INFERRED}"
    assert R.audit(two("?Quinsy ruled out at first.", "treatment", f"- Oral fluids encouraged {INFERRED_A}"), out, T) == []


def test_protected_spans_are_the_protected_multiset_with_positions():
    for text in ("No collection on Day 1", "Asthma ruled out.", "WCC 18 → 11 (142) 5 mg x2/7 ?quinsy",
                 "Treated as cellulitis for now; not yet reviewed, only if worse."):
        spans = R.protected_spans(text)
        assert Counter(k for _s, _e, k in spans) == R.protected(text)
        for s, e, k in spans:
            seg = text[s:e]
            if k.startswith("phrase:"):
                assert " ".join(seg.lower().split()) == k[len("phrase:"):]
            elif k.startswith("#"):
                assert seg == k[1:]


# --- D5 (step 3, adopted 11 Nov 2026): no protected token leaves with a drop --------------------------------
# A drop pattern broader than its clean examples passes table validation; the runtime rule stops it removing a
# negation, an uncertainty marker or a timing word. Only a digit token inside a number-slot match may leave.

def _police(pattern):
    return {"police": lambda r: r["withheld"]["entries"][2].update(pattern=pattern,
                                                                     examples=["police informed", "police"])}


@pytest.mark.parametrize("pattern, line, token", [
    (r"police(?: not)?(?: informed| involved)?", "- Police not informed.", "negation"),
    (r"(?:possible )?police(?: informed)?", "- Possible police informed.", "uncertainty word"),
    (r"\??police(?: informed)?", "- ?Police informed.", "uncertainty mark"),
    (r"police(?: informed)?(?: today)?", "- Police informed today.", "timing"),
])
def test_d5_a_drop_may_not_remove_a_protected_token(pattern, line, token):
    t = _tables_with(**_police(pattern))
    assert refusal(two("Fever.", "treatment", line), t).code == "withheld_not_isolable", token


def test_d5_the_same_entry_still_drops_a_clean_span():
    t = _tables_with(**_police(r"police(?: not)?(?: informed| involved)?"))
    out = R.render_patient(two("Fever.", "treatment", "- Police informed\n- Needle aspiration"), t)
    assert [d["entry"] for d in out["ledger"]["drops"]] == ["w_police"]


def test_d5_a_number_in_a_number_slot_still_leaves_with_its_drop(T):
    out = R.render_patient(two("Fever.", "diagnosis", "Primary: Right quinsy (Wells score 4)"), T)
    assert [d["entry"] for d in out["ledger"]["drops"]] == ["w_wells"]
    assert "4" not in c_text(out, "why_in_hospital").split("\n")[2]


def test_d5_only_the_digit_is_exempt_inside_a_number_slot():
    t = _tables_with(wells=lambda r: r["withheld"]["entries"][0].update(pattern=r"Wells score(?: of)? \d+(?: mg)?"))
    assert refusal(two("Fever.", "diagnosis", "Primary: Right quinsy (Wells score 4 mg)"), t).code == \
        "withheld_not_isolable"


def test_d5_authorisation_rejects_such_a_drop_independently():
    t = _tables_with(**_police(r"police(?: not)?(?: informed| involved)?"))
    text = "Police not informed."
    u = _unit(text)
    ops = [{"op": "drop", "a": 0, "b": len(text), "rule": "withheld", "entry": "w_police", "unit": "line"}]
    assert R.authorise_ops(u, "treatment", "adult", ops, t) == ["op0:drop_protected_token"]


# --- D11 (step 3, adopted 11 Nov 2026): no signpost in the line AS WRITTEN -----------------------------------
@pytest.mark.parametrize("gap", ["  ", "\t", " \t "])
def test_d11_a_spaced_out_signpost_refuses_rather_than_leaking(T, gap):
    # _normalise collapses white space, so the copied text would carry the gate's literal "emergency department".
    for heading, text in (("treatment", f"- Wound repaired in the emergency{gap}department."),
                          ("presenting_complaint", f"Seen in the accident{gap}and emergency unit.")):
        inp = two("Fever.", heading, text) if heading != "presenting_complaint" else two(text)
        assert refusal(inp, T).code == "signpost_not_isolable", (heading, gap)


def test_d11_a_signpost_formed_by_joining_the_pieces_round_a_drop_refuses(T):
    # Codex 11 Nov 07:07 P2: the score in brackets is an authorised drop; neither kept piece holds a signpost, the
    # joined line does.
    assert refusal(two("Seen in the emergency (Wells score 4) department."), T).code == "signpost_not_isolable"


def test_d11_a_diagnosis_with_a_score_dropped_is_still_permitted(T):
    out = R.render_patient(two("Fever.", "diagnosis", "Primary: Right quinsy (Wells score 4)"), T)
    assert c_text(out, "why_in_hospital").split("\n")[2] == "Main problem: Right a collection of pus beside the tonsil (quinsy)"
    assert [d["entry"] for d in out["ledger"]["drops"]] == ["w_wells"]


def test_d11_authorisation_checks_the_joined_line_independently(T):
    text = "Seen in the emergency (Wells score 4) department."
    u = _unit(text, heading="presenting_complaint", form="prose")
    s, e, kind = next(c for c in R.candidate_units(text, R.content_start(text, "prose")) if c[2] == "aside")
    assert text[s:e].strip() == "(Wells score 4)"
    assert R.drop_problem(text, s, e, kind, "withheld", "w_wells", T, "presenting_complaint",
                          "prose") is None                      # the drop on its own is authorised
    ops = [{"op": "copy", "a": 0, "b": s},
           {"op": "drop", "a": s, "b": e, "rule": "withheld", "entry": "w_wells", "unit": kind},
           {"op": "copy", "a": e, "b": len(text)}]
    assert R.authorise_ops(u, "presenting_complaint", "adult", ops, T) == ["line:written_signpost_not_isolable"]


def test_d11_audit_rejects_a_copy_whose_written_form_is_a_signpost(T):
    text = "seen in the emergency  department"
    assert R.authorise_ops(_unit(text), "treatment", "adult",
                           [{"op": "copy", "a": 0, "b": len(text)}], T) == ["line:written_signpost_not_isolable"]


def test_d11_spaced_out_text_without_a_signpost_still_copies(T):
    out = R.render_patient(two("Fever.", "treatment", "- Wound  dressing\tchanged."), T)
    assert "Wound dressing changed." in json.dumps(out)
