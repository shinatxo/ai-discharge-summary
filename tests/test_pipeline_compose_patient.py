"""Step 7 — compose_patient (ADR-009 row 7): the record_patient_version schema and
tool, the p0.1 prompt, the input boundary (patient_input / D1 I3) and the step
through run_step with a fake Bedrock. No AWS.

The PART A sections below are invented for these tests (an NSTEMI) — not a canary
scenario.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "evals"))

from pipeline import STEPS, StepContext, StepError, run_step  # noqa: E402
from pipeline import compose as C6  # noqa: E402
from pipeline import compose_patient as P  # noqa: E402
from pipeline import meaning as MN  # noqa: E402
from pipeline.retrieve import _RESUS_PATTERNS  # noqa: E402
from pipeline.schemas import PATIENT_HEADINGS, record_patient_version_schema, shape_errors  # noqa: E402
from test_pipeline_schema import limit_violations  # noqa: E402
import safety_net_gate as gate  # noqa: E402


def _a(idx, heading, text, part="part_a", fact_ids=("x.0",), unverified=()):
    return {"section_id": f"sections.{idx}", "part": part, "heading": heading, "fact_ids": list(fact_ids),
            "text": text, "unverified_fact_ids": list(unverified)}


def _step6(age_group="adult"):
    """A step-6 output: every PART A heading (boxes, risk, allergies, VTE, a
    conflict, a Not documented section) plus PART B."""
    return {"age_group": age_group, "sections": [
        _a(0, "presenting_complaint", "Central chest pain."),
        _a(1, "diagnosis", "Primary: NSTEMI."),
        _a(2, "key_investigations", "Troponin 287 -> 412 ng/L."),
        _a(3, "treatment", "Severe 3VD, not for revascularisation; medical management. IV furosemide 40mg given."),
        _a(4, "resuscitation_status", "{{RESUS}}", fact_ids=()),
        _a(5, "risk_assessment", "Low risk of self-harm."),
        _a(6, "medications", "{{MEDICATIONS}}", fact_ids=()),
        _a(7, "medication_conflicts", "Warfarin dose conflict."),
        _a(8, "allergies", "NKDA."),
        _a(9, "follow_up", "Cardiology clinic in 6 weeks."),
        _a(10, "gp_actions", "Not documented", fact_ids=()),
        _a(11, "safety_net", "{{SAFETY_NET}}", fact_ids=()),
        _a(12, "vte_assessment", "Done on admission."),
        _a(13, "opening", "This 64-year-old man ...", part="part_b"),
        _a(14, "gp_actions", "Please check U&E in 1 week.", part="part_b"),
    ]}


def _input(**kw):
    return P.patient_input(_step6(**kw))


def _s(heading, ids, text):
    return {"heading": heading, "source_ids": ids, "text": text}


def _good():
    return {"sections": [
        _s("why_in_hospital", ["sections.0", "sections.1"],
           "You came in with chest pain. You had a heart attack (NSTEMI)."),
        _s("what_we_did", ["sections.2", "sections.3"],
           "Blood tests showed strain on your heart. The heart arteries are narrowed. "
           "The team will treat this with medicines."),
        _s("medicines", [], "{{MEDICATIONS}}"),
        _s("next_steps", ["sections.9"], "You will see the heart team in clinic in 6 weeks."),
        _s("safety_net", [], "{{SAFETY_NET}}"),
    ]}


def _response(payload=None, stop="tool_use", name="record_patient_version"):
    return {"output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "t1", "name": name,
                             "input": payload if payload is not None else _good()}}]}},
            "stopReason": stop,
            "usage": {"inputTokens": 1500, "outputTokens": 600,
                      "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0},
            "metrics": {"latencyMs": 9000}}


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


def _run(payload=None, inp=None, fake=None, **kw):
    fake = fake or FakeBedrock(_response(payload))
    return run_step("compose_patient", inp if inp is not None else _input(), ctx=_ctx(fake, **kw))


def _checks(out):
    return [(c["check"], c["heading"], c["source_id"], c["token"]) for c in out["checks"]]


def _names(out):
    return {c["check"] for c in out["checks"]}


def _with(heading, text=None, ids=None):
    p = _good()
    for s in p["sections"]:
        if s["heading"] == heading:
            if text is not None:
                s["text"] = text
            if ids is not None:
                s["source_ids"] = ids
    return p


# --- schema and tool -------------------------------------------------------------

def test_schema_is_inside_bedrocks_limits_and_annex_c2():
    assert limit_violations(record_patient_version_schema()) == []


def test_schema_is_flat_small_and_has_the_section_shape_once():
    dumped = json.dumps(record_patient_version_schema())
    assert len(dumped) <= len(json.dumps(C6.record_composition_schema()))     # smaller than step 6's 715 bytes
    assert dumped.count('"heading": {') == 1 and dumped.count('"source_ids": {') == 1


def test_evidence_comes_before_the_text():
    props = list(record_patient_version_schema()["properties"]["sections"]["items"]["properties"])
    assert props == ["heading", "source_ids", "text"]


def test_headings_and_boxes():
    assert PATIENT_HEADINGS == ("why_in_hospital", "what_we_did", "medicines", "next_steps", "safety_net")
    assert P.BOXES == {"medicines": "{{MEDICATIONS}}", "safety_net": "{{SAFETY_NET}}"}
    assert set(P.ALWAYS) | set(P.SOURCES) == set(PATIENT_HEADINGS)
    assert set(P.GIVEN_A_HEADINGS) | set(P.NOT_GIVEN_A_HEADINGS) == set(C6.A_HEADINGS)
    assert not set(P.GIVEN_A_HEADINGS) & set(P.NOT_GIVEN_A_HEADINGS)


def test_tool_is_strict_forced_and_uses_the_schema():
    cfg = P.tool_config()
    spec = cfg["tools"][0]["toolSpec"]
    assert len(cfg["tools"]) == 1 and spec["name"] == P.TOOL_NAME == "record_patient_version"
    assert spec["strict"] is True and cfg["toolChoice"] == {"tool": {"name": P.TOOL_NAME}}
    assert spec["inputSchema"]["json"] == record_patient_version_schema()


def test_registered_as_seq_07_model_step():
    s = STEPS["compose_patient"]
    assert (s.seq, s.kind) == ("07", "model")


def test_read_timeout_is_60_s():
    assert P.READ_TIMEOUT_S == 60


def test_signpost_pattern_is_the_gates():
    # a copy until the gate moves into the package at 8b (item 4)
    assert P._SIGNPOST.pattern == gate._SIGNPOST.pattern and P._SIGNPOST.flags == gate._SIGNPOST.flags


def test_resus_patterns_are_the_shared_object():
    assert P._RESUS_PATTERNS is _RESUS_PATTERNS


# --- prompt ----------------------------------------------------------------------

def test_prompt_body_only_and_its_sha():
    text, sha = P.system_prompt()
    assert text.startswith("You write the patient version") and "p0.1 (2026-11-01)" not in text
    import hashlib
    assert sha == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_prompt_names_both_boxes_every_heading_and_no_resus_box():
    text, _ = P.system_prompt()
    for token in P.BOXES.values():
        assert token in text
    assert "{{RESUS}}" not in text
    for h in PATIENT_HEADINGS:
        assert f"`{h}`" in text
    assert "`record_patient_version`" in text


def test_p02_and_p03_examples_are_clear_of_the_scenarios():
    # p0.1's examples mirrored scenario content (furosemide - B6 r1 copied it; NSTEMI - S1;
    # driving - S14/S18). p0.2's examples must appear in none of the scenario files.
    text, _ = P.system_prompt()
    for old in ("furosemide", "NSTEMI", "drive"):
        assert old not in text
    scenarios = "\n".join(p.read_text(encoding="utf-8").lower()
                          for p in (ROOT / "evals" / "scenarios").glob("*.md"))
    for example in ("flucloxacillin", "cholelithiasis", "the swelling is smaller", "rest your foot",
                    "an antibiotic",
                    # p0.3 (3 Nov, G1): grep-checked against the scenario files before review
                    "cystoscopy", "a camera test of the bladder", "mobility improving",
                    "your walking is getting better", "walking normally", "gout", "kidney stones",
                    "no clot", "the scan showed no clot", "your legs are healthy", "carefully", "closely",
                    "gently", "the team treated you for",
                    "a test of fluid from the knee"):             # p0.4 (3 Nov, H1 A)
        assert example in " ".join(text.lower().split()) and example not in scenarios   # may wrap
    assert "treat as" not in text.lower()          # S12's own wording, kept out (G1.2)


def test_p02_rules_are_in_the_prompt():
    text = " ".join(P.system_prompt()[0].split())          # rules may wrap across lines
    for rule in ("KEEP THE MEANING EXACTLY", "WHY YOU WERE IN HOSPITAL", "Never leave the reason out",
                 "if you have any concerns", "please make sure", "never supply a medicine's name",
                 "Most sentences 10 words or fewer; none over 15", "Every sentence you write must come",
                 "Do not mention risk scores or ceilings of care"):
        assert rule in text
    assert "[W" not in text and "Do not alarm" not in text


def test_p03_and_p04_rules_are_in_the_prompt():
    text = " ".join(P.system_prompt()[0].split())
    for rule in ("say what it is in everyday words that keep it the same test",
                 "never call it a different test, sample or procedure",
                 "Add the section's own term in brackets only for an operation or procedure the patient had",
                 "Words in brackets count towards the 15",
                 "put each on its own short line starting",
                 "Keep words about change and time at the same strength",
                 "A diagnosis the team treated but did not confirm stays unconfirmed",
                 "a link between two things the sections list separately",
                 "Never widen \"no …\"", "every primary and secondary diagnosis the diagnosis section lists",
                 "do not mention a course or supply to finish or keep taking at home",
                 "a word that grades how something was done",
                 "Never leave out the reason for admission, a primary or secondary diagnosis"):
        assert rule in text, rule
    assert "[G1" not in text and "about 250 words" not in text


@pytest.mark.parametrize("payload,expected", [
    (_with("what_we_did", "Blood tests showed your heart has fully healed."),
     ("certainty_added", "what_we_did", None, "fully")),
    (_with("what_we_did", "Blood tests showed strain caused by your blood pressure."),
     ("cause_added", "what_we_did", None, "caused by")),
    (_with("what_we_did", "A swab showed strain on your heart."),
     ("test_term_added", "what_we_did", None, "swab")),
    (_with("why_in_hospital", "You came in with chest pain."),
     ("diagnosis_unreached", "why_in_hospital", "sections.1", "line 1")),
])
def test_meaning_checks_fire(payload, expected):
    assert expected in _checks(_run(payload))


def test_meaning_qualifier_check_fires_and_names_the_group():
    s6 = _step6()
    s6["sections"][2]["text"] = "Troponin 287 -> 412 ng/L, initially rising."
    out = _run(inp=P.patient_input(s6))
    assert ("qualifier_dropped", "what_we_did", None, "initially") in _checks(out)


def test_meaning_checks_are_in_checks_and_the_good_output_raises_none():
    assert set(MN.CHECK_OF.values()) <= set(P.CHECKS)
    assert _run()["checks"] == []


def test_prompt_says_never_translate_is_codes_job_and_writes_english():
    # D3 T1: the step is not told about translation at all
    text, _ = P.system_prompt()
    assert "translat" not in text.lower() and "interpreter" not in text.lower()


# --- input boundary (D1 I3, D4, R1, R2) --------------------------------------------

def test_patient_input_keeps_only_given_prose_sections_in_order():
    inp = _input()
    assert inp["audience"] == "adult"
    assert [s["heading"] for s in inp["sections"]] == [
        "presenting_complaint", "diagnosis", "key_investigations", "treatment", "follow_up"]
    assert [s["section_id"] for s in inp["sections"]] == [
        "sections.0", "sections.1", "sections.2", "sections.3", "sections.9"]
    assert set(inp["sections"][0]) == {"section_id", "heading", "text"}


def test_patient_input_drops_boxes_risk_allergies_vte_conflicts_not_documented_and_part_b():
    text = P.user_text(_input())
    for absent in ("{{", "self-harm", "NKDA", "Warfarin", "Done on admission", "Not documented",
                   "Please check U&E", "64-year-old"):
        assert absent not in text


def test_gp_actions_are_given_when_documented():
    s6 = _step6()
    s6["sections"][10]["text"] = "Check U&E in 1 week."
    assert "gp_actions" in [s["heading"] for s in P.patient_input(s6)["sections"]]


@pytest.mark.parametrize("age_group,audience", [
    ("adult", "adult"), ("not_documented", "adult"),
    ("neonate", "parent_or_carer"), ("infant", "parent_or_carer"), ("child", "parent_or_carer")])
def test_audience_follows_age_group(age_group, audience):
    assert _input(age_group=age_group)["audience"] == audience


@pytest.mark.parametrize("label,mutate", [
    ("lines key", lambda i: i.update(lines={"L001": "x"})),
    ("facts key", lambda i: i.update(facts={})),
    ("bad audience", lambda i: i.update(audience="patient")),
    ("box heading", lambda i: i["sections"].append({"section_id": "sections.40", "heading": "safety_net",
                                                    "text": "{{SAFETY_NET}}"})),
    ("risk heading", lambda i: i["sections"].append({"section_id": "sections.41", "heading": "risk_assessment",
                                                     "text": "Low risk."})),
    ("allergies heading", lambda i: i["sections"].append({"section_id": "sections.42", "heading": "allergies",
                                                          "text": "NKDA."})),
    ("not documented", lambda i: i["sections"].append({"section_id": "sections.43", "heading": "follow_up",
                                                       "text": "Not documented."})),
    ("duplicate id", lambda i: i["sections"].append(dict(i["sections"][0]))),
    ("bad id", lambda i: i["sections"][0].update(section_id="L001")),
    ("extra key", lambda i: i["sections"][0].update(fact_ids=[])),
    ("not a list", lambda i: i.update(sections={})),
])
def test_input_is_refused(label, mutate):
    inp = _input()
    mutate(inp)
    fake = FakeBedrock(_response())
    with pytest.raises(StepError) as exc:
        run_step("compose_patient", inp, ctx=_ctx(fake))
    assert exc.value.code == "bad_input" and fake.requests == []


def test_patient_input_fails_closed_on_a_malformed_step6_output():
    with pytest.raises(StepError) as exc:
        P.patient_input({"sections": []})
    assert exc.value.code == "bad_input"


# --- the call --------------------------------------------------------------------

def test_request_shape_and_message():
    fake = FakeBedrock(_response())
    _run(fake=fake, prompt_caching=True)
    req = fake.requests[0]
    assert req["system"][0]["text"] == P.system_prompt()[0]
    assert req["system"][1] == {"cachePoint": {"type": "default"}}
    assert req["inferenceConfig"] == {"maxTokens": P.MAX_TOKENS, "temperature": 0}
    msg = req["messages"][0]["content"][0]["text"]
    assert msg.startswith("audience: adult\n\nPART A SECTIONS")
    assert "[sections.1] diagnosis\nPrimary: NSTEMI." in msg


def test_good_output_has_no_checks_and_the_output_shape():
    out = _run()
    assert out["checks"] == []
    assert set(out) == {"sections", "checks", "counts", "audience", "revised", "call"}
    assert out["revised"] is False                 # ADR-010 V3: True only on the one regeneration
    assert out["sections"][0] == {"section_id": "c_sections.0", "heading": "why_in_hospital",
                                  "source_ids": ["sections.0", "sections.1"],
                                  "text": "You came in with chest pain. You had a heart attack (NSTEMI)."}
    assert out["counts"]["sections"] == 5 and out["counts"]["sections_given"] == 5
    assert out["counts"]["sources_cited"] == 5 and out["counts"]["checks"] == 0
    c = out["call"]
    assert c["prompt_sha256"] == P.system_prompt()[1] and c["attempts"] == 1 and c["latency_ms"] == 9000


# --- F1 (2 Nov 2026): refuse when nothing says why the patient was in -------------

@pytest.mark.parametrize("sections", [
    [],                                                                  # S12 r5 (1 Nov): given nothing
    [{"section_id": "sections.9", "heading": "follow_up", "text": "Clinic in 6 weeks."}],
    [{"section_id": "sections.2", "heading": "key_investigations", "text": "Troponin raised."},
     {"section_id": "sections.14", "heading": "gp_actions", "text": "Check U&E."}],
])
def test_no_presenting_complaint_or_diagnosis_is_refused_before_any_call(sections):
    fake = FakeBedrock()
    with pytest.raises(StepError) as exc:
        _run(inp={"audience": "adult", "sections": sections}, fake=fake)
    assert exc.value.code == "no_sections_given" and fake.requests == []


@pytest.mark.parametrize("heading", P.REASON_HEADINGS)
def test_either_reason_heading_alone_is_enough(heading):
    out = _run(payload={"sections": [_s("why_in_hospital", ["sections.0"], "You came in with chest pain."),
                                     _s("medicines", [], "{{MEDICATIONS}}"),
                                     _s("safety_net", [], "{{SAFETY_NET}}")]},
               inp={"audience": "adult", "sections": [
                   {"section_id": "sections.0", "heading": heading, "text": "Central chest pain."}]})
    assert out["checks"] == []


def test_refusal_code_is_documented():
    assert "no_sections_given" in P.__doc__ and P.REASON_HEADINGS == ("presenting_complaint", "diagnosis")


# --- F2 / F5 (2 Nov 2026) ---------------------------------------------------------

def test_prose_without_a_given_source_is_flagged():
    out = _run(_with("why_in_hospital", ids=[]))
    assert ("prose_without_source", "why_in_hospital", None, None) in _checks(out)
    out = _run(_with("why_in_hospital", ids=["sections.99"]))
    assert {"prose_without_source", "unknown_source_id"} <= _names(out)
    assert "prose_without_source" not in _names(_run())                 # boxes never count


@pytest.mark.parametrize("text", [
    "A nurse will visit you at home.", "Your GP will be there if you need them.",
    "The team will be able to tell you more.", "Phone support is available.",
])
def test_promise_wording_is_flagged(text):
    assert ("promise_shaped", "next_steps", None, None) in _checks(_run(_with("next_steps", text)))


# --- checks (flag, never repair) ---------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    (_with("medicines", "{{MEDICATIONS}} (see below)"), ("box_not_exact", "medicines", None, None)),
    (_with("safety_net", ids=["sections.0"]), ("box_not_exact", "safety_net", None, None)),
    (_with("next_steps", "See {{RESUS}}."), ("stray_placeholder", "next_steps", None, None)),
    (_with("why_in_hospital", ids=["sections.0", "sections.99"]),
     ("unknown_source_id", "why_in_hospital", "sections.99", None)),
    (_with("why_in_hospital", ids=["sections.0", "allergies"]),
     ("unknown_source_id", "why_in_hospital", None, None)),
    (_with("what_we_did", "Your troponin was 287 -> 412 ng/L in 2025."),
     ("year_added", "what_we_did", None, "2025")),
    (_with("next_steps", "Your clinic is not documented yet."),
     ("not_documented_text", "next_steps", None, None)),
    (_with("next_steps", "Call 999 if the pain comes back."),
     ("signpost_outside_box", "next_steps", None, None)),
    (_with("why_in_hospital", "Your DNACPR form was discussed."),
     ("resus_text", "why_in_hospital", None, None)),
    (_with("next_steps", "If you feel worse, contact your GP."),
     ("advice_shaped", "next_steps", None, None)),
    (_with("what_we_did", "You were given furosemide 40mg twice a day."),
     ("dose_or_frequency", "what_we_did", None, None)),
    (_with("next_steps", "You will see the heart team in 8 weeks."),
     ("number_added", "next_steps", None, "8")),
    (_with("what_we_did", "Your troponin was 287 -> 412 g/L."),
     ("unit_added", "what_we_did", None, "g/l")),
    (_with("why_in_hospital", "The notes disagree about your diagnosis."),
     ("conflict_text", "why_in_hospital", None, None)),
    (_with("why_in_hospital", "A safeguarding referral was made."),
     ("sensitive_text", "why_in_hospital", None, None)),
])
def test_each_check_fires(payload, expected):
    assert expected in _checks(_run(payload))


def test_order_repeat_and_required():
    p = _good()
    p["sections"] = [p["sections"][1], p["sections"][0]] + p["sections"][2:]
    assert ("heading_out_of_order", "why_in_hospital", None, None) in _checks(_run(p))
    p = _good()
    p["sections"].append(_s("next_steps", ["sections.9"], "Clinic in 6 weeks."))
    assert ("heading_repeated", "next_steps", None, None) in _checks(_run(p))
    p = _good()
    p["sections"] = [s for s in p["sections"] if s["heading"] not in ("safety_net", "what_we_did")]
    names = _checks(_run(p))
    assert ("required_heading_missing", "safety_net", None, None) in names
    assert ("required_heading_missing", "what_we_did", None, None) in names


def test_next_steps_without_a_source_is_flagged_and_not_required():
    inp = _input()
    inp["sections"] = [s for s in inp["sections"] if s["heading"] != "follow_up"]
    out = _run(_with("next_steps", ids=[]), inp=inp)
    assert ("heading_without_source", "next_steps", None, None) in _checks(out)
    p = _good()
    p["sections"] = [s for s in p["sections"] if s["heading"] != "next_steps"]
    assert _run(p, inp=inp)["checks"] == []


def test_follow_up_and_gp_actions_must_reach_c_other_sections_reported():
    s6 = _step6()
    s6["sections"][10]["text"] = "Check U&E in 1 week."
    inp = P.patient_input(s6)
    out = _run(inp=inp)
    assert ("follow_up_missing", None, "sections.10", "gp_actions") in _checks(out)
    p = _with("what_we_did", ids=["sections.3"])
    assert ("source_unused", None, "sections.2", "key_investigations") in _checks(_run(p))


def test_numbers_in_the_cited_section_and_in_a_dose_given_in_hospital_are_not_added():
    out = _run(_with("what_we_did", "Your troponin went from 287 to 412. You were given IV furosemide 40mg."))
    names = _names(out)
    assert "number_added" not in names and "unit_added" not in names
    assert "dose_or_frequency" in names          # flagged for a CSO ruling (bar line 5, standard of round 2)


def test_dose_abbreviations_are_case_sensitive():
    # found by this file's own good-output test: "strain on your heart" matched ON
    assert "dose_or_frequency" not in _names(_run(_with("what_we_did", "Blood tests showed strain on your heart.")))
    assert "dose_or_frequency" in _names(_run(_with("what_we_did", "You had your tablet ON.")))


def test_a_bare_emergency_operation_is_not_a_signpost():
    assert "signpost_outside_box" not in _names(_run(_with("what_we_did", "You had an emergency operation.")))


def test_checks_never_carry_text():
    p = _with("next_steps", "Call 999 if the pain comes back in 8 weeks or 2025.")
    for c in _run(p)["checks"]:
        assert set(c) == {"check", "heading", "source_id", "token"}
        assert c["token"] in (None, "8", "999", "2025")
        assert c["check"] in P.CHECKS


# --- fail closed ------------------------------------------------------------------

@pytest.mark.parametrize("resp,code", [
    (_response(stop="max_tokens"), "max_tokens"),
    (_response(name="record_composition"), "no_tool_call"),
    (_response(stop="end_turn"), "unexpected_stop_reason"),
    ({"output": {"message": {"content": [{"toolUse": {"name": "record_patient_version", "input": "x"}}]}},
      "stopReason": "tool_use"}, "patient_version_not_object"),
    (_response({"sections": [{"heading": "why_in_hospital", "text": "x"}]}), "patient_version_shape_invalid"),
    (_response({"sections": [{"heading": "summary", "source_ids": [], "text": "x"}]}),
     "patient_version_shape_invalid"),
])
def test_bad_responses_fail_closed(resp, code):
    with pytest.raises(StepError) as exc:
        _run(fake=FakeBedrock(resp))
    assert exc.value.code == code


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "secret request content"}}, "Converse")


def test_throttle_is_retried_then_succeeds():
    sleeps = []
    fake = FakeBedrock(_client_error("ThrottlingException"), _response())
    out = _run(fake=fake, sleeps=sleeps)
    assert out["call"]["attempts"] == 2 and sleeps == [25.0]


def test_read_timeout_is_never_retried():
    fake = FakeBedrock(ReadTimeoutError(endpoint_url="x"), _response())
    with pytest.raises(StepError) as exc:
        _run(fake=fake)
    assert exc.value.code == "read_timeout" and len(fake.requests) == 1


def test_no_client_fails_closed():
    with pytest.raises(StepError) as exc:
        run_step("compose_patient", _input())
    assert exc.value.code == "no_client"


def test_shape_errors_on_good_payload_is_empty():
    assert shape_errors(_good(), record_patient_version_schema()) == []
