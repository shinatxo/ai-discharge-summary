"""Step 6 — compose_clinical (ADR-009 row 6): the record_composition schema and
tool, the c0.1 prompt, and the step through run_step with a fake Bedrock. No AWS.

The notes below are invented for these tests (an NSTEMI, an allergy conflict,
one drug, one piece of advice, a resus status) — not a canary scenario. Steps 1
and 3 run for real on them, so step 6 gets exactly the input shape it gets in
the pipeline.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "evals"))

from pipeline import STEPS, StepContext, StepError, run_step  # noqa: E402
from pipeline import compose as C  # noqa: E402
from pipeline.schemas import (COMPOSE_HEADINGS, COMPOSE_PARTS, FIELD_NAMES,  # noqa: E402
                              record_composition_schema, shape_errors)
from test_pipeline_schema import limit_violations  # noqa: E402
import rubric_checks as RC  # noqa: E402

NOTES = "\n".join([
    "Day 1 (16/05) 64M admitted with central chest pain.",
    "Trop 287 -> 412. Dx NSTEMI.",
    "Angiogram: severe 3VD, not for revasc. Medical management.",
    "Allergies: NKDA",
    "Day 3 nursing: reports penicillin allergy (rash as child).",
    "DH: aspirin 75mg OD",
    "Plan: GP to check U&E in 1 week. Cardiology clinic 6 weeks.",
    "Advised to call 999 if chest pain recurs.",
    "VTE assessment done on admission.",
    "For CPR.",
    "",
])

DOCUMENTED = {"age_sex", "admission_date", "presenting_complaint", "diagnosis_primary",
              "key_investigations", "treatment", "allergies", "follow_up", "gp_actions", "vte_assessment"}


def _cite(lines, quote):
    return {"lines": lines, "quote": quote}


def _f(section, value, lines, quote):
    return {"section": section, "value": value, "cites": [_cite(lines, quote)]}


def _wire_facts():
    status = {n: ("documented" if n in DOCUMENTED else "not_documented") for n in FIELD_NAMES}
    status["specialty"] = "inferred_flagged"
    facts = [
        _f("age_sex", "64M", ["L001"], "64M"),
        _f("specialty", "Cardiology", ["L007"], "Cardiology clinic 6 weeks"),
        _f("admission_date", "16/05", ["L001"], "Day 1 (16/05)"),
        _f("presenting_complaint", "central chest pain", ["L001"], "central chest pain"),
        _f("diagnosis_primary", "NSTEMI", ["L002"], "Dx NSTEMI"),
        _f("key_investigations", "Troponin 287 -> 412", ["L002"], "Trop 287 -> 412"),
        # quote not on L003: citation_unverified, still given to step 6 (decision 3A)
        _f("treatment", "Severe 3VD, not for revascularisation; medical management", ["L003"],
           "triple vessel disease"),
        _f("allergies", "NKDA", ["L004"], "NKDA"),
        _f("follow_up", "Cardiology clinic in 6 weeks", ["L007"], "Cardiology clinic 6 weeks"),
        _f("gp_actions", "Check U&E in 1 week", ["L007"], "GP to check U&E in 1 week"),
        _f("vte_assessment", "Done on admission", ["L009"], "VTE assessment done on admission"),
        _f("pre_admission", "aspirin 75mg OD", ["L006"], "aspirin 75mg OD"),
        _f("documented_advice", "call 999 if chest pain recurs", ["L008"], "call 999 if chest pain recurs"),
        {"section": "contradictions", "value": "Allergy: NKDA on Day 1; penicillin allergy reported Day 3",
         "cites": [_cite(["L004"], "NKDA"), _cite(["L005"], "reports penicillin allergy")]},
    ]
    return {
        "field_status": [{"field": n, "status": status[n]} for n in FIELD_NAMES],
        "facts": facts, "discharge_status": "not_documented",
        "resus": {"form_or_discussion_documented": False, "status_documented": "for_resuscitation",
                  "changed": "not_documented", "cites": [_cite(["L010"], "For CPR")]},
        "age_group": "adult", "suspicious_text": [],
    }


def _facts():
    g = run_step("guard_input", {"notes": NOTES})
    return run_step("validate_facts", {"facts": _wire_facts(), "lines": g["lines"]})["facts"]


def _s(part, heading, ids, text):
    return {"part": part, "heading": heading, "fact_ids": ids, "text": text}


def _good():
    return {"sections": [
        _s("part_a", "presenting_complaint", ["presenting_complaint.0"], "Central chest pain."),
        _s("part_a", "diagnosis", ["diagnosis_primary.0"], "Primary: NSTEMI."),
        _s("part_a", "key_investigations", ["key_investigations.0"], "Troponin 287 -> 412."),
        _s("part_a", "treatment", ["treatment.0"], "Severe 3VD, not for revascularisation; medical management."),
        _s("part_a", "resuscitation_status", [], "{{RESUS}}"),
        _s("part_a", "medications", [], "{{MEDICATIONS}}"),
        _s("part_a", "allergies", ["allergies.0", "contradictions.0"],
           "NKDA. Notes conflict: NKDA on Day 1; penicillin allergy reported Day 3 — requires clarification."),
        _s("part_a", "follow_up", ["follow_up.0"], "Cardiology clinic in 6 weeks."),
        _s("part_a", "gp_actions", ["gp_actions.0"], "- Check U&E in 1 week."),
        _s("part_a", "safety_net", [], "{{SAFETY_NET}}"),
        _s("part_a", "vte_assessment", ["vte_assessment.0"], "Done on admission."),
        _s("part_b", "opening", ["age_sex.0", "presenting_complaint.0", "diagnosis_primary.0"],
           "This 64-year-old man was admitted with central chest pain due to an NSTEMI."),
        _s("part_b", "admission_summary", ["key_investigations.0", "treatment.0"],
           "Troponin rose 287 -> 412. Angiography showed severe 3VD, not for revascularisation."),
        _s("part_b", "resuscitation_status", [], "{{RESUS}}"),
        _s("part_b", "medications", [], "{{MEDICATIONS}}"),
        _s("part_b", "gp_actions", ["gp_actions.0"], "- Please check U&E in 1 week."),
        _s("part_b", "follow_up", ["follow_up.0"], "Cardiology clinic in 6 weeks."),
        _s("part_b", "safety_net", [], "{{SAFETY_NET}}"),
    ]}


def _response(payload=None, stop="tool_use", name="record_composition"):
    return {"output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "t1", "name": name,
                             "input": payload if payload is not None else _good()}}]}},
            "stopReason": stop,
            "usage": {"inputTokens": 4100, "outputTokens": 1900,
                      "cacheReadInputTokens": 2600, "cacheWriteInputTokens": 0},
            "metrics": {"latencyMs": 27000}}


class FakeBedrock:
    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def converse(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _ctx(fake, **kw):
    sleeps = kw.pop("sleeps", [])
    return StepContext(bedrock=fake, model_id="anthropic.claude-sonnet-4-6",
                       sleep=sleeps.append, jitter=lambda a, b: (a + b) / 2, **kw)


def _run(payload=None, facts=None, **kw):
    return run_step("compose_clinical", {"facts": facts or _facts()},
                    ctx=_ctx(FakeBedrock(_response(payload)), **kw))


def _checks(out):
    return [(c["check"], c["part"], c["heading"], c["fact_id"], c["token"]) for c in out["checks"]]


def _names(out):
    return {c["check"] for c in out["checks"]}


def _sec(payload, part, heading):
    return next(s for s in payload["sections"] if s["part"] == part and s["heading"] == heading)


# --- schema ----------------------------------------------------------------------

def test_schema_is_inside_bedrocks_limits_and_annex_c2():
    assert limit_violations(record_composition_schema()) == []


def test_schema_is_small_and_has_the_section_shape_once():
    # W2 part 2: the grammar grows with the number of places a shape occurs.
    dumped = json.dumps(record_composition_schema())
    assert len(dumped) <= 1000
    assert dumped.count('"heading": {') == 1 and dumped.count('"fact_ids": {') == 1


def test_evidence_comes_before_the_text():
    props = list(record_composition_schema()["properties"]["sections"]["items"]["properties"])
    assert props == ["part", "heading", "fact_ids", "text"]


def test_headings_partition_and_fields_cover_the_facts_object():
    assert set(C.A_HEADINGS) | set(C.B_HEADINGS) == set(COMPOSE_HEADINGS)
    assert set(C.A_REQUIRED) <= set(C.A_HEADINGS) and set(C.B_REQUIRED) <= set(C.B_HEADINGS)
    assert set(C.BOXES) <= set(C.A_REQUIRED) and set(C.BOXES) <= set(C.B_REQUIRED)
    assert set(C.A_FIELDS) <= set(C.A_HEADINGS)
    covered = list(C.CONTENT_FIELDS) + list(C.HEADER_FIELDS)
    assert sorted(covered) == sorted(FIELD_NAMES) and len(covered) == len(set(covered))


def test_tool_is_strict_forced_and_uses_the_schema():
    cfg = C.tool_config()
    spec = cfg["tools"][0]["toolSpec"]
    assert len(cfg["tools"]) == 1 and spec["name"] == C.TOOL_NAME == "record_composition"
    assert spec["strict"] is True and cfg["toolChoice"] == {"tool": {"name": C.TOOL_NAME}}
    assert spec["inputSchema"] == {"json": record_composition_schema()}
    assert set(spec) == {"name", "description", "inputSchema", "strict"}
    assert not re.search(r"\bL\d{3}\b|\d", C.TOOL_DESCRIPTION)


# --- prompt ----------------------------------------------------------------------

def test_prompt_is_cacheable_and_carries_no_header_template_or_part_c():
    text, _ = C.system_prompt()
    assert len(text) > 4096 and "## SYSTEM PROMPT" not in text
    assert "YYYY" not in text and "PART C" not in text and "NHS No:" not in text


def test_every_schema_name_and_value_is_explained_in_the_prompt():
    text = C.system_prompt()[0]
    names = ["record_composition", "sections", "part", "heading", "fact_ids", "text",
             *COMPOSE_PARTS, *COMPOSE_HEADINGS, *C.BOXES.values()]
    assert [n for n in names if f"`{n}`" not in text] == []


def test_prompt_carries_q4_rule_and_the_year_rule():
    text = " ".join(C.system_prompt()[0].split())
    assert ("Every clinical statement in PARTS A and B must come from the facts you are given "
            "or from a code-rendered block") in text
    assert "Never add a year" in text and C.PAEDIATRIC_VTE in text and C.NOT_DOCUMENTED in text


# --- the step ---------------------------------------------------------------------

def test_registered_as_06_model_step():
    s = STEPS["compose_clinical"]
    assert (s.seq, s.kind) == ("06", "model")
    seqs = [st.seq for st in STEPS.values()]
    assert seqs == sorted(seqs)


def test_without_ctx_fails_closed():
    with pytest.raises(StepError) as e:
        run_step("compose_clinical", {"facts": _facts()})
    assert e.value.code == "no_client"


def test_happy_path_has_no_checks_and_lists_unverified_ids():
    out = _run()
    assert out["checks"] == []
    assert len(out["sections"]) == 18
    assert [s["section_id"] for s in out["sections"]] == [f"sections.{i}" for i in range(18)]
    treat = next(s for s in out["sections"] if s["part"] == "part_a" and s["heading"] == "treatment")
    assert treat["unverified_fact_ids"] == ["treatment.0"]           # 8a adds the warning
    assert out["counts"] == {
        "sections": 18, "sections_a": 11, "sections_b": 7, "facts_given": 8, "facts_used_in_a": 8,
        "header_facts_given": 3, "contradictions_given": 1, "contradictions_in_a": 1,
        "gp_actions_given": 1, "gp_actions_in_b": 1, "unverified_given": 1, "unverified_used": 1,
        "checks": 0}
    assert out["age_group"] == "adult"
    c = out["call"]
    assert (c["input_tokens"], c["output_tokens"], c["cache_read_tokens"], c["latency_ms"]) == (4100, 1900, 2600, 27000)
    assert c["prompt_sha256"] == C.system_prompt()[1] and c["attempts"] == 1


def test_user_text_excludes_medications_resus_advice_quotes_and_line_ids():
    facts = _facts()
    text = C.user_text(facts)
    assert "aspirin" not in text and "999" not in text and "CPR" not in text
    assert not re.search(r"\bL\d{3}\b", text) and "triple vessel disease" not in text   # no quotes
    assert "- [contradictions.0] Allergy: NKDA on Day 1; penicillin allergy reported Day 3" in text
    assert "- [treatment.0] Severe 3VD" in text and "unverified" not in text            # included, unmarked
    assert "specialty (inferred_flagged)" in text and "legal_status (not_documented)" in text
    assert text.startswith("age_group: adult")
    order = [text.index(f"\n{n} (") for n in FIELD_NAMES]
    assert order == sorted(order)


def test_no_contradictions_says_none():
    facts = _facts()
    facts["contradictions"] = []
    assert C.user_text(facts).endswith("CONTRADICTIONS\nnone")


@pytest.mark.parametrize("inp", [{}, {"facts": {}}, {"facts": "x"}, None])
def test_bad_input_fails_before_any_call(inp):
    if inp is None:
        inp = {"facts": _facts(), "lines": {"L001": "x"}}   # step 6 never receives the notes
    fake = FakeBedrock()
    with pytest.raises(StepError) as e:
        run_step("compose_clinical", inp, ctx=_ctx(fake))
    assert e.value.code == "bad_input" and fake.requests == []


def test_bad_facts_shape_fails_before_any_call():
    for mutate in (lambda f: f["fields"].pop("allergies"),
                   lambda f: f["fields"]["allergies"].update(status="maybe"),
                   lambda f: f.pop("contradictions"),
                   lambda f: f["fields"]["allergies"]["items"][0].pop("fact_id")):
        facts = _facts()
        mutate(facts)
        fake = FakeBedrock()
        with pytest.raises(StepError) as e:
            run_step("compose_clinical", {"facts": facts}, ctx=_ctx(fake))
        assert e.value.code == "bad_input" and fake.requests == []


# --- checks: flag, never fix ----------------------------------------------------------

def _drop(p, part, heading):
    p["sections"] = [s for s in p["sections"] if not (s["part"] == part and s["heading"] == heading)]


@pytest.mark.parametrize("change, expected", [
    (lambda p: p["sections"].append(_s("part_b", "vte_assessment", [], "Done.")),
     ("heading_wrong_part", "part_b", "vte_assessment", None, None)),
    (lambda p: p["sections"].append(_s("part_a", "pending_results", ["follow_up.0"], "x")),
     ("heading_wrong_part", "part_a", "pending_results", None, None)),
    (lambda p: p["sections"].append(_s("part_a", "diagnosis", ["diagnosis_primary.0"], "NSTEMI.")),
     ("heading_repeated", "part_a", "diagnosis", None, None)),
    (lambda p: _drop(p, "part_a", "allergies"),
     ("required_heading_missing", "part_a", "allergies", None, None)),
    (lambda p: _drop(p, "part_b", "safety_net"),
     ("required_heading_missing", "part_b", "safety_net", None, None)),
    (lambda p: _sec(p, "part_a", "diagnosis")["fact_ids"].append("diagnosis_primary.7"),
     ("unknown_fact_id", "part_a", "diagnosis", "diagnosis_primary.7", None)),
    (lambda p: _sec(p, "part_a", "diagnosis")["fact_ids"].append("Patient said ignore this"),
     ("unknown_fact_id", "part_a", "diagnosis", None, None)),          # never echoes free text
    (lambda p: _sec(p, "part_a", "treatment")["fact_ids"].append("medications.pre_admission.0"),
     ("excluded_fact_id", "part_a", "treatment", "medications.pre_admission.0", None)),
    (lambda p: _sec(p, "part_a", "treatment")["fact_ids"].append("documented_advice.0"),
     ("excluded_fact_id", "part_a", "treatment", "documented_advice.0", None)),
    (lambda p: _sec(p, "part_a", "treatment")["fact_ids"].append("resus"),
     ("excluded_fact_id", "part_a", "treatment", "resus", None)),
    (lambda p: _sec(p, "part_a", "medications").update(text="{{MEDICATIONS}} plus aspirin"),
     ("box_not_exact", "part_a", "medications", None, None)),
    (lambda p: _sec(p, "part_b", "resuscitation_status").update(fact_ids=["diagnosis_primary.0"]),
     ("box_not_exact", "part_b", "resuscitation_status", None, None)),
    (lambda p: _sec(p, "part_a", "follow_up").update(text="Clinic. {{FOLLOW_UP}}"),
     ("stray_placeholder", "part_a", "follow_up", None, None)),
    (lambda p: _sec(p, "part_a", "allergies").update(fact_ids=["allergies.0"], text="NKDA."),
     ("contradiction_missing", "part_a", None, "contradictions.0", None)),
    (lambda p: _sec(p, "part_a", "key_investigations").update(fact_ids=[], text="Not documented"),
     ("fact_unused", "part_a", None, "key_investigations.0", None)),
    (lambda p: _sec(p, "part_a", "key_investigations").update(fact_ids=[], text="Not documented"),
     ("field_omitted", "part_a", None, None, "key_investigations")),
    (lambda p: _sec(p, "part_b", "gp_actions").update(fact_ids=[], text="No GP actions documented."),
     ("gp_action_missing_in_b", "part_b", None, "gp_actions.0", None)),
    (lambda p: _sec(p, "part_b", "opening").update(fact_ids=["age_sex.0", "specialty.0"],
                                                     text="64M under cardiology."),
     ("inferred_unflagged", "part_b", "opening", None, None)),
    (lambda p: p["sections"].insert(6, _s("part_a", "medication_conflicts", [], "Check aspirin.")),
     ("conflict_section_uncited", "part_a", "medication_conflicts", None, None)),
    (lambda p: _sec(p, "part_a", "presenting_complaint").update(text="Chest pain on 16/05/2025."),
     ("year_added", "part_a", "presenting_complaint", None, "2025")),
    (lambda p: _sec(p, "part_a", "treatment").update(text="Medical management. For CPR."),
     ("resus_text_outside", "part_a", "treatment", None, None)),
    (lambda p: _sec(p, "part_b", "admission_summary").update(text="DNACPR discussed."),
     ("resus_text_outside", "part_b", "admission_summary", None, None)),
])
def test_checks_flag_and_never_fix(change, expected):
    p = _good()
    change(p)
    out = _run(p)
    assert expected in _checks(out)
    assert len(out["sections"]) == len(p["sections"])                    # nothing dropped
    assert out["counts"]["checks"] == len(out["checks"])


def test_inferred_fact_with_the_flag_passes():
    p = _good()
    _sec(p, "part_b", "opening").update(fact_ids=["age_sex.0", "specialty.0"],
                                        text="64M under Cardiology (inferred — not documented, confirm).")
    assert _run(p)["checks"] == []


def test_years_the_facts_give_are_not_added():
    facts = _facts()
    facts["fields"]["treatment"]["items"][0]["value"] = "PCI 2019; review 26/04/26"
    p = _good()
    _sec(p, "part_a", "treatment").update(text="PCI in 2019; review 26/04/2026.")
    assert "year_added" not in _names(_run(p, facts=facts))


def test_phone_numbers_are_not_years():
    p = _good()
    _sec(p, "part_a", "follow_up").update(text="Clinic: 0800 2000 123.")
    assert "year_added" not in _names(_run(p))


@pytest.mark.parametrize("text", ["Day 1 (16/05)", "Dx 2019, review 26/04/26", "1/1/99 and 2001", "no years"])
def test_documented_years_matches_the_rubric_check(text):
    assert C.documented_years(text) == RC.documented_years(text)


def _nd(facts, name):
    facts["fields"][name] = {"status": "not_documented", "items": [], "field_check": "ok"}
    return facts


@pytest.mark.parametrize("text, age, flagged", [
    ("Not documented", "adult", False),
    ("Not documented.", "adult", False),
    ("Completed on admission", "adult", True),
    ("Not applicable (paediatric)", "child", False),
    ("Not applicable (paediatric)", "adult", True),
])
def test_not_documented_field(text, age, flagged):
    facts = _nd(_facts(), "vte_assessment")
    facts["age_group"] = age
    p = _good()
    _sec(p, "part_a", "vte_assessment").update(fact_ids=[], text=text)
    assert ("not_documented_with_content" in _names(_run(p, facts=facts))) is flagged


def test_risk_assessment_when_not_documented_is_left_out():
    p = _good()
    p["sections"].insert(5, _s("part_a", "risk_assessment", [], "Not documented"))
    out = _run(p)
    assert ("not_documented_with_content", "part_a", "risk_assessment", None, None) in _checks(out)


def test_a_contradiction_may_sit_in_a_not_documented_section():
    facts = _nd(_facts(), "allergies")
    p = _good()
    _sec(p, "part_a", "allergies").update(
        fact_ids=["contradictions.0"], text="Not documented. Notes conflict: … — requires clarification.")
    assert "not_documented_with_content" not in _names(_run(p, facts=facts))


def test_checks_carry_no_text():
    p = _good()
    _sec(p, "part_a", "presenting_complaint").update(text="Chest pain on 16/05/2025. For CPR. {{X}}")
    _sec(p, "part_a", "diagnosis")["fact_ids"].append("some free text here")
    out = _run(p)
    for c in out["checks"]:
        assert set(c) == {"check", "part", "heading", "fact_id", "token"}
        assert c["check"] in C.CHECKS
        assert c["token"] is None or re.fullmatch(r"\d{4}|[a-z_]+", c["token"])
        assert c["fact_id"] is None or re.fullmatch(r"[a-z_]+\.\d+|resus", c["fact_id"])


# --- the request --------------------------------------------------------------------

def test_request_shape_and_cache_point():
    fake = FakeBedrock(_response())
    facts = _facts()
    run_step("compose_clinical", {"facts": facts}, ctx=_ctx(fake, prompt_caching=True))
    req = fake.requests[0]
    assert req["toolConfig"] == C.tool_config()
    assert req["inferenceConfig"] == {"maxTokens": C.MAX_TOKENS, "temperature": 0}
    assert req["system"] == [{"text": C.system_prompt()[0]}, {"cachePoint": {"type": "default"}}]
    assert req["messages"] == [{"role": "user", "content": [{"text": C.user_text(facts)}]}]


# --- failures -----------------------------------------------------------------------

def _missing_key():
    p = _good()
    del p["sections"][0]["fact_ids"]
    return p


@pytest.mark.parametrize("resp, code", [
    (_response(stop="max_tokens"), "max_tokens"),
    ({"output": {"message": {"content": [{"text": "PART A"}]}}, "stopReason": "end_turn"}, "no_tool_call"),
    (_response(name="record_facts"), "no_tool_call"),
    (_response(stop="end_turn"), "unexpected_stop_reason"),
    (_response(payload=["not", "an", "object"]), "composition_not_object"),
    (_response(payload=_missing_key()), "composition_shape_invalid"),
    (_response(payload={"sections": [dict(_good()["sections"][0], heading="header")]}), "composition_shape_invalid"),
])
def test_response_failures_fail_closed(resp, code):
    with pytest.raises(StepError) as e:
        run_step("compose_clinical", {"facts": _facts()}, ctx=_ctx(FakeBedrock(resp)))
    assert e.value.code == code


def test_throttle_retried_twice_then_succeeds():
    sleeps = []
    t = ClientError({"Error": {"Code": "ThrottlingException", "Message": "m"}}, "Converse")
    fake = FakeBedrock(t, t, _response())
    out = run_step("compose_clinical", {"facts": _facts()}, ctx=_ctx(fake, sleeps=sleeps))
    assert out["call"]["attempts"] == 3 and sleeps == [25.0, 25.0]


def test_read_timeout_is_not_retried():
    fake = FakeBedrock(ReadTimeoutError(endpoint_url="https://bedrock-runtime"), _response())
    with pytest.raises(StepError) as e:
        run_step("compose_clinical", {"facts": _facts()}, ctx=_ctx(fake))
    assert e.value.code == "read_timeout" and len(fake.requests) == 1


def test_grammar_rejection_is_request_rejected():
    # A Bedrock schema/grammar rejection is the structural trigger for Option D
    # (after one redesign attempt) — it must surface as its own fixed code.
    v = ClientError({"Error": {"Code": "ValidationException", "Message": "grammar too large"}}, "Converse")
    with pytest.raises(StepError) as e:
        run_step("compose_clinical", {"facts": _facts()}, ctx=_ctx(FakeBedrock(v)))
    assert e.value.code == "request_rejected"


def test_output_never_carries_the_notes():
    dumped = json.dumps(_run())
    assert "Day 3 nursing" not in dumped and "triple vessel disease" not in dumped
    assert shape_errors({"sections": []}, record_composition_schema()) == []


def test_items_under_a_not_documented_field_are_not_a_field_omission():
    # CSO R4, 28 Oct 2026: field-level omission is defined on DOCUMENTED fields.
    facts = _facts()
    facts["fields"]["diagnosis_background"] = {
        "status": "not_documented", "field_check": "items_on_not_documented",
        "items": [{"fact_id": "diagnosis_background.0", "value": "Not documented — note: x",
                   "citation_status": "verified"}]}
    names = _names(_run(facts=facts))
    assert "field_omitted" not in names and "fact_unused" in names
