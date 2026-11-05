"""Step 7v — verify_patient (ADR-010): the record_verification schema and tool, the
v0.1 prompt, the input boundary (verify_input), the coverage rule, the V3 sequence
(verify -> one regeneration -> verify -> hold back) and step 7's revision block.
Fake Bedrock; no AWS.

The PART A and PART C text below is invented for these tests (a hip fracture) —
not a canary scenario.
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
from pipeline import compose_patient as P  # noqa: E402
from pipeline import meaning as MN  # noqa: E402
from pipeline import verify_patient as V  # noqa: E402
from pipeline.schemas import (VERIFY_REASONS, VERIFY_VERDICTS, record_patient_version_schema,  # noqa: E402
                              record_verification_schema, shape_errors)
from test_pipeline_schema import limit_violations  # noqa: E402

MODEL = "anthropic.claude-sonnet-4-6"

STEP7_INPUT = {"audience": "adult", "sections": [
    {"section_id": "sections.0", "heading": "presenting_complaint", "text": "Fall at home; unable to weight bear."},
    {"section_id": "sections.1", "heading": "diagnosis", "text": "Primary: left neck of femur fracture."},
    {"section_id": "sections.2", "heading": "treatment", "text": "Hemiarthroplasty day 2. Mobility improving."},
    {"section_id": "sections.4", "heading": "follow_up", "text": "Orthopaedic clinic in 6 weeks."},
]}


def _c(idx, heading, ids, text):
    return {"section_id": f"c_sections.{idx}", "heading": heading, "source_ids": ids, "text": text}


def _step7_out(why="You fell at home. You broke your left hip.", did="You had an operation to replace part of the hip.\nYour walking is getting better."):
    return {"sections": [
        _c(0, "why_in_hospital", ["sections.0", "sections.1"], why),
        _c(1, "what_we_did", ["sections.2"], did),
        _c(2, "medicines", [], "{{MEDICATIONS}}"),
        _c(3, "next_steps", ["sections.4"], "You will see the bone team in clinic in 6 weeks."),
        _c(4, "safety_net", [], "{{SAFETY_NET}}"),
    ], "checks": [], "counts": {}, "audience": "adult", "revised": False, "call": {}}


def _vin(**kw):
    return V.verify_input(STEP7_INPUT, _step7_out(**kw))


def _item(sid, cid, aid, verdict="supported", reason="none", span=""):
    return {"c_section_id": cid, "sentence_id": sid, "a_section_id": aid, "span": span,
            "reason": reason, "verdict": verdict}


def _all_supported(inp):
    a_of = {s["section_id"]: (s["source_ids"] or ["sections.0"])[0] for s in inp["c_sections"]}
    return {"items": [_item(t["sentence_id"], s["section_id"], a_of[s["section_id"]])
                      for s in inp["c_sections"] for t in s["sentences"]]}


def _response(payload, stop="tool_use", name="record_verification"):
    return {"output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "t1", "name": name, "input": payload}}]}},
            "stopReason": stop,
            "usage": {"inputTokens": 3500, "outputTokens": 700,
                      "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0},
            "metrics": {"latencyMs": 8000}}


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
    return StepContext(bedrock=fake, model_id=MODEL, sleep=sleeps.append,
                       jitter=lambda a, b: (a + b) / 2, **kw)


def _run(payload=None, inp=None, fake=None, **kw):
    inp = inp if inp is not None else _vin()
    fake = fake or FakeBedrock(_response(payload if payload is not None else _all_supported(inp)))
    return run_step("verify_patient", inp, ctx=_ctx(fake, **kw))


def _names(out):
    return {c["check"] for c in out["checks"]}


# --- schema and tool -------------------------------------------------------------

def test_schema_is_inside_bedrocks_limits_and_annex_c2():
    assert limit_violations(record_verification_schema()) == []


def test_schema_is_flat_with_one_item_shape_and_small():
    dumped = json.dumps(record_verification_schema())
    assert len(dumped) <= 800                       # step 6's 715 bytes was accepted first call
    assert dumped.count('"verdict": {') == 1 and dumped.count('"span": {') == 1


def test_evidence_comes_before_the_verdict():
    props = list(record_verification_schema()["properties"]["items"]["items"]["properties"])
    assert props == ["c_section_id", "sentence_id", "a_section_id", "span", "reason", "verdict"]


def test_verdicts_and_reasons():
    assert VERIFY_VERDICTS == ("supported", "added", "changed", "qualifier_dropped", "unreached")
    assert VERIFY_REASONS[0] == "none" and "never_drop_item" in VERIFY_REASONS


def test_tool_is_strict_forced_and_uses_the_schema():
    cfg = V.tool_config()
    spec = cfg["tools"][0]["toolSpec"]
    assert len(cfg["tools"]) == 1 and spec["name"] == V.TOOL_NAME == "record_verification"
    assert spec["strict"] is True and cfg["toolChoice"] == {"tool": {"name": V.TOOL_NAME}}
    assert spec["inputSchema"]["json"] == record_verification_schema()


def test_registered_as_seq_07v_model_step_and_60_s():
    s = STEPS["verify_patient"]
    assert (s.seq, s.kind) == ("07v", "model") and V.READ_TIMEOUT_S == 60


# --- prompt ----------------------------------------------------------------------

# Every example the v0.1 prompt uses. They must be clear of the scenario files (the
# 2 Nov rule) and of every leaflet and PART A text in the three step-7 batches that
# calibration reads (ADR-010 V5: an example copied from a labelled defect would
# inflate recall on the held-out set too).
PROMPT_EXAMPLES = ("skin scraping", "biopsy", "hearing test", "slow heartbeat", "bradycardia",
                   "24-hour recording", "camera on a tube", "heart monitoring",
                   # v0.3 (STATED, NOT TRUE)
                   "tonsillitis", "infection of the tonsils", "a sling so that the bone heals",
                   "at your gp surgery", "x-ray of your ankle was normal")


def test_v02_dropped_the_glaucoma_example():
    # CSO audit 4 Nov: "high pressure in the eye" is not an equivalent definition of glaucoma
    text = " ".join(V.system_prompt()[0].split()).casefold()
    assert "glaucoma" not in text and "pressure in the eye" not in text
CAL_BATCHES = ("w6-pipeline-e09-m01-c02-p01-x5", "w6-pipeline-e09-m01-c02-p02-x5",
               "w6-pipeline-e09-m01-c02-p04-x5")


def test_prompt_body_only_and_its_sha():
    import hashlib
    text, sha = V.system_prompt()
    assert text.startswith("You check a patient leaflet") and "v0.3 (2026-11-04)" not in text
    assert sha == hashlib.sha256(text.encode("utf-8")).hexdigest()


def _flat(text):
    return " ".join(text.split())


def test_prompt_uses_every_example_it_is_pinned_with():
    text = _flat(V.system_prompt()[0]).casefold()
    for e in PROMPT_EXAMPLES:
        assert e in text, e


def test_prompt_examples_are_clear_of_the_scenarios():
    files = list((ROOT / "evals" / "scenarios").glob("*.md")) + [ROOT / "src" / "canary" / "scenarios.json"]
    corpus = "\n".join(f.read_text(encoding="utf-8") for f in files).casefold()
    assert [e for e in PROMPT_EXAMPLES if e in corpus] == []


@pytest.mark.skipif(not all((ROOT / "evals" / "runs" / b).is_dir() for b in CAL_BATCHES),
                    reason="calibration batches not mounted")
def test_prompt_examples_are_clear_of_the_calibration_batches():
    hits = []
    for b in CAL_BATCHES:
        for f in (ROOT / "evals" / "runs" / b).glob("r*/*.json"):
            steps = json.loads(f.read_text(encoding="utf-8"))["steps"]
            text = " ".join(s["text"] for k in ("compose_patient", "compose_clinical")
                            for s in (steps.get(k) or {}).get("sections", [])).casefold()
            hits += [e for e in PROMPT_EXAMPLES if e in text]
    assert hits == []


def test_prompt_states_the_standard():
    text = _flat(V.system_prompt()[0])
    for phrase in ("Judge each leaflet sentence ONLY against", "Never skip a sentence",
                   "When you are unsure whether a sentence is supported, it is not `supported`",
                   "primary, secondary AND background", "is never `unreached`"):
        assert phrase in text, phrase
    for v in VERIFY_VERDICTS:
        assert f"`{v}`" in text
    for r in VERIFY_REASONS:
        if r not in ("none", "never_drop_item"):
            assert f"`{r}`" in text, r


# --- input -----------------------------------------------------------------------

def test_verify_input_numbers_sentences_with_the_shared_splitter_and_skips_boxes():
    inp = _vin()
    assert [s["heading"] for s in inp["c_sections"]] == ["why_in_hospital", "what_we_did", "next_steps"]
    did = inp["c_sections"][1]
    assert [t["text"] for t in did["sentences"]] == MN.sentences(_step7_out()["sections"][1]["text"])
    assert [t["sentence_id"] for t in did["sentences"]] == ["c_sections.1.s1", "c_sections.1.s2"]
    assert inp["a_sections"] == STEP7_INPUT["sections"]


def test_verify_input_ignores_a_revision_block():
    inp7 = dict(STEP7_INPUT, revision={"previous": [], "findings": []})
    assert V.verify_input(inp7, _step7_out())["a_sections"] == STEP7_INPUT["sections"]


def _bad(mutate):
    inp = copy.deepcopy(_vin())
    mutate(inp)
    return inp


@pytest.mark.parametrize("label,mutate", [
    ("lines key", lambda i: i.update(lines=["L001 x"])),
    ("facts key", lambda i: i.update(facts={})),
    ("box heading in C", lambda i: i["c_sections"][0].update(heading="medicines")),
    ("not-given A heading", lambda i: i["a_sections"][0].update(heading="risk_assessment")),
    ("duplicate A id", lambda i: i["a_sections"].append(dict(i["a_sections"][0]))),
    ("sentence id of another section", lambda i: i["c_sections"][0]["sentences"][0].update(sentence_id="c_sections.9.s1")),
    ("extra sentence key", lambda i: i["c_sections"][0]["sentences"][0].update(source="x")),
])
def test_input_is_refused(label, mutate):
    with pytest.raises(StepError) as exc:
        _run(inp=_bad(mutate), fake=FakeBedrock())
    assert exc.value.code == "bad_input", label


def test_nothing_to_verify_is_refused_before_any_call():
    fake = FakeBedrock()
    inp = {"a_sections": STEP7_INPUT["sections"], "c_sections": []}
    with pytest.raises(StepError) as exc:
        run_step("verify_patient", inp, ctx=_ctx(fake))
    assert exc.value.code == "nothing_to_verify" and fake.requests == []


def test_request_carries_only_each_sections_cited_a_text_and_the_numbered_sentences():
    fake = FakeBedrock(_response(_all_supported(_vin())))
    _run(fake=fake)
    req = fake.requests[0]
    assert req["modelId"] == MODEL and req["inferenceConfig"] == {"maxTokens": V.MAX_TOKENS, "temperature": 0}
    text = req["messages"][0]["content"][0]["text"]
    block = text.split("=== c_sections.3 next_steps", 1)[1]
    assert "[sections.4] follow_up" in block and "[sections.1]" not in block
    assert "c_sections.3.s1: You will see the bone team in clinic in 6 weeks." in block
    assert "{{MEDICATIONS}}" not in text and "{{SAFETY_NET}}" not in text


def test_prompt_caching_adds_a_cache_point_after_the_system_prompt():
    fake = FakeBedrock(_response(_all_supported(_vin())))
    _run(fake=fake, prompt_caching=True)
    assert fake.requests[0]["system"][1] == {"cachePoint": {"type": "default"}}


# --- output ----------------------------------------------------------------------

def test_all_supported_is_not_flagged_and_the_output_shape():
    out = _run()
    assert set(out) == {"verdicts", "unreached", "flagged", "checks", "counts", "call"}
    assert out["flagged"] == 0 and out["unreached"] == [] and out["checks"] == []
    assert out["counts"]["sentences"] == out["counts"]["supported"] == 5
    assert [v["sentence_id"] for v in out["verdicts"]] == [
        "c_sections.0.s1", "c_sections.0.s2", "c_sections.1.s1", "c_sections.1.s2", "c_sections.3.s1"]


def _payload_with(*changes, extra=()):
    p = _all_supported(_vin())
    for sid, upd in changes:
        next(i for i in p["items"] if i["sentence_id"] == sid).update(upd)
    p["items"] += list(extra)
    return p


def test_a_flagged_sentence_and_an_unreached_item_are_counted():
    p = _payload_with(("c_sections.1.s2", {"verdict": "changed", "reason": "certainty_or_completion",
                                           "span": "getting better", "a_section_id": "sections.2"}),
                      extra=[_item("", "c_sections.0", "sections.0", "unreached", "never_drop_item",
                                   "unable to weight bear")])
    out = _run(p)
    assert out["flagged"] == 2 and out["checks"] == []
    assert out["counts"]["by_reason"]["certainty_or_completion"] == 1 and out["counts"]["unreached"] == 1
    assert out["unreached"][0] == {"c_section_id": "c_sections.0", "heading": "why_in_hospital",
                                   "a_section_id": "sections.0", "reason": "never_drop_item",
                                   "span": "unable to weight bear", "evidence_valid": True}


@pytest.mark.parametrize("mutate,code", [
    (lambda p: p["items"].pop(), "verdict_missing"),
    (lambda p: p["items"].append(dict(p["items"][0], verdict="added", reason="new_fact", span="You fell")),
     "verdict_conflict"),                                               # supported AND at fault
    (lambda p: p["items"][0].update(a_section_id="sections.8"), "unknown_a_section"),   # evidence not given
    (lambda p: p["items"][0].update(sentence_id="c_sections.0.s9"), "unknown_sentence_id"),
    (lambda p: p["items"][0].update(sentence_id="c_sections.1.s1"), "unknown_sentence_id"),   # wrong section
    (lambda p: p["items"][0].update(c_section_id="c_sections.7"), "unknown_section_id"),
])
def test_coverage_fails_closed(mutate, code):
    p = _all_supported(_vin())
    mutate(p)
    with pytest.raises(StepError) as exc:
        _run(p)
    assert exc.value.code == code


@pytest.mark.parametrize("change,expected", [
    (("c_sections.3.s1", {"a_section_id": "sections.1"}), "uncited_source"),
    (("c_sections.3.s1", {"verdict": "added", "reason": "new_fact", "span": "not in the sentence"}), "span_not_found"),
    (("c_sections.3.s1", {"verdict": "added", "reason": "new_fact", "span": ""}), "span_not_found"),
    (("c_sections.3.s1", {"span": "clinic"}), "span_on_supported"),
    (("c_sections.3.s1", {"reason": "new_fact"}), "reason_mismatch"),
    (("c_sections.3.s1", {"verdict": "added", "reason": "none", "span": "clinic"}), "reason_mismatch"),
])
def test_verifier_output_faults_are_flagged_not_repaired(change, expected):
    out = _run(_payload_with(change))
    assert expected in _names(out)
    assert len(out["verdicts"]) == 5                  # nothing dropped


def test_unreached_item_faults_are_flagged():
    p = _payload_with(extra=[_item("c_sections.0.s1", "c_sections.0", "sections.0", "unreached", "new_fact", "nowhere")])
    out = _run(p)
    assert {"unreached_with_sentence", "reason_mismatch", "span_not_found"} <= _names(out)
    assert len(out["unreached"]) == 1 and out["flagged"] == 1


def test_checks_never_carry_text():
    p = _payload_with(("c_sections.3.s1", {"a_section_id": "sections.1", "span": "bone team"}))
    for c in _run(p)["checks"]:
        assert set(c) == {"check", "sentence_id", "c_section_id", "token"}
        assert c["check"] in V.CHECKS
        assert c["token"] is None or re.fullmatch(r"sections\.\d+|[a-z_]+", c["token"])


@pytest.mark.parametrize("resp,code", [
    (_response({"items": []}, stop="max_tokens"), "max_tokens"),
    (_response({"items": []}, name="record_patient_version"), "no_tool_call"),
    (_response({"items": []}, stop="end_turn"), "unexpected_stop_reason"),
    ({"output": {"message": {"content": [{"toolUse": {"name": "record_verification", "input": "x"}}]}},
      "stopReason": "tool_use"}, "verification_not_object"),
    (_response({"items": [{"verdict": "supported"}]}), "verification_shape_invalid"),
    (_response({"items": [_item("c_sections.0.s1", "c_sections.0", "sections.0", verdict="fine")]}),
     "verification_shape_invalid"),
])
def test_bad_responses_fail_closed(resp, code):
    with pytest.raises(StepError) as exc:
        _run(fake=FakeBedrock(resp))
    assert exc.value.code == code


def test_throttle_is_retried_then_succeeds_and_timeouts_never_are():
    err = ClientError({"Error": {"Code": "ThrottlingException", "Message": "secret"}}, "Converse")
    sleeps = []
    out = _run(fake=FakeBedrock(err, _response(_all_supported(_vin()))), sleeps=sleeps)
    assert out["call"]["attempts"] == 2 and sleeps == [25.0]
    fake = FakeBedrock(ReadTimeoutError(endpoint_url="x"))
    with pytest.raises(StepError) as exc:
        _run(fake=fake)
    assert exc.value.code == "read_timeout" and len(fake.requests) == 1


def test_no_client_fails_closed():
    with pytest.raises(StepError) as exc:
        run_step("verify_patient", _vin())
    assert exc.value.code == "no_client"


def test_shape_errors_on_a_good_payload_is_empty():
    assert shape_errors(_all_supported(_vin()), record_verification_schema()) == []


# --- step 7's revision block (ADR-010 V3) -----------------------------------------

def test_revision_issue_words_cover_every_reason():
    assert set(P.REVISION_ISSUES) == set(VERIFY_REASONS) - {"none"}


def test_held_back_text_raises_none_of_step_7s_pattern_checks():
    t = V.HELD_BACK_TEXT
    for name in ("_SIGNPOST", "_ADVICE", "_DOSE", "_CONFLICT", "_SENSITIVE", "_PROMISE", "_NOT_DOCUMENTED", "_PLACEHOLDER"):
        assert not getattr(P, name).search(t), name


def test_held_back_text_adds_nothing_but_drops_what_it_replaces():
    # Finding (4 Nov): the fixed line never ADDS a cause, certainty or test word, but it
    # does drop the section's qualifiers (and, in why_in_hospital, its diagnoses) — so a
    # held-back section raises line-14 "dropped" flags. The scorer must treat a held-back
    # section as pre-ruled once the CSO approves the wording (ADR-010 O1), not leave
    # those flags to a ruling after the batch.
    kinds = {k for k, _tok, _shown in MN.section_flags(V.HELD_BACK_TEXT, STEP7_INPUT["sections"])}
    assert kinds <= {"qualifier"} and not kinds & {"cause", "certainty", "test_term"}


def _good7():
    return {"sections": [{k: s[k] for k in ("heading", "source_ids", "text")} for s in _step7_out()["sections"]]}


def _resp7(payload):
    return {"output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "t", "name": "record_patient_version", "input": payload}}]}},
            "stopReason": "tool_use", "usage": {"inputTokens": 1, "outputTokens": 1}, "metrics": {"latencyMs": 1}}


def test_step7_accepts_a_revision_and_puts_the_draft_and_findings_in_the_user_message():
    first = _step7_out()
    rev = {"previous": [{k: s[k] for k in ("heading", "source_ids", "text")} for s in first["sections"]],
           "findings": [{"heading": "what_we_did", "span": "getting better", "issue": "certainty_or_completion"},
                        {"heading": "why_in_hospital", "span": "unable to weight bear", "issue": "never_drop_item"}]}
    fake = FakeBedrock(_resp7(_good7()))
    out = run_step("compose_patient", dict(STEP7_INPUT, revision=rev), ctx=_ctx(fake))
    assert out["revised"] is True
    text = fake.requests[0]["messages"][0]["content"][0]["text"]
    assert "YOUR FIRST DRAFT" in text and "[what_we_did] (source_ids: sections.2)" in text
    assert f'- what_we_did: "getting better" — {P.REVISION_ISSUES["certainty_or_completion"]}.' in text
    assert text.rstrip().endswith(P.REVISION_INSTRUCTION)
    assert fake.requests[0]["system"][0]["text"] == P.system_prompt()[0]     # p0.4 body unchanged


@pytest.mark.parametrize("rev", [
    {"previous": [], "findings": []},                                                   # nothing to fix
    {"previous": [], "findings": [{"heading": "what_we_did", "span": "x", "issue": "none"}]},
    {"previous": [], "findings": [{"heading": "medicines_box", "span": "x", "issue": "other"}]},
    {"previous": [{"heading": "what_we_did", "text": "x"}], "findings": [{"heading": "what_we_did", "span": "x", "issue": "other"}]},
    {"previous": [], "findings": [], "notes": "x"},
])
def test_step7_refuses_a_malformed_revision(rev):
    with pytest.raises(StepError) as exc:
        run_step("compose_patient", dict(STEP7_INPUT, revision=rev), ctx=_ctx(FakeBedrock()))
    assert exc.value.code == "bad_input"


def test_revision_for_lists_flagged_sentences_and_unreached_items():
    v = {"verdicts": [
            {"sentence_id": "c_sections.1.s2", "c_section_id": "c_sections.1", "heading": "what_we_did",
             "verdict": "changed", "reason": "certainty_or_completion", "a_section_id": "sections.2", "span": "better"},
            {"sentence_id": "c_sections.0.s1", "c_section_id": "c_sections.0", "heading": "why_in_hospital",
             "verdict": "supported", "reason": "none", "a_section_id": "sections.0", "span": ""}],
         "unreached": [{"c_section_id": "c_sections.0", "heading": "why_in_hospital", "a_section_id": "sections.0",
                        "reason": "never_drop_item", "span": "unable to weight bear"}]}
    rev = V.revision_for(_step7_out(), v)
    assert rev["findings"] == [{"heading": "what_we_did", "span": "better", "issue": "certainty_or_completion"},
                               {"heading": "why_in_hospital", "span": "unable to weight bear", "issue": "never_drop_item"}]
    assert [s["heading"] for s in rev["previous"]] == [s["heading"] for s in _step7_out()["sections"]]


# --- the V3 sequence ----------------------------------------------------------------

def _seq(v1_payload, v2_payload=None, regen=None):
    """Run verified_patient_version through run_step with one fake for 7 and one for 7v."""
    f7 = FakeBedrock(_resp7(_good7()), *([_resp7(regen)] if regen else []))
    f7v = FakeBedrock(_response(v1_payload), *([_response(v2_payload)] if v2_payload else []))
    out = V.verified_patient_version(
        STEP7_INPUT,
        lambda i: run_step("compose_patient", i, ctx=_ctx(f7)),
        lambda i: run_step("verify_patient", i, ctx=_ctx(f7v)))
    return out, f7, f7v


def test_sequence_clean_first_time_is_one_call_each():
    out, f7, f7v = _seq(_all_supported(_vin()))
    assert set(out) == {"compose_patient", "verify_patient"}
    assert len(f7.requests) == 1 and len(f7v.requests) == 1


_FLAG = ("c_sections.1.s2", {"verdict": "changed", "reason": "certainty_or_completion", "span": "getting better",
                             "a_section_id": "sections.2"})


def test_sequence_regenerates_once_and_keeps_both_drafts_when_the_second_check_is_clean():
    regen = _good7()
    regen["sections"][1]["text"] = "You had an operation to replace part of the hip.\nYour walking is improving."
    v2 = _all_supported(V.verify_input(STEP7_INPUT, {"sections": [
        dict(s, section_id=f"c_sections.{i}") for i, s in enumerate(regen["sections"])]}))
    out, f7, f7v = _seq(_payload_with(_FLAG), v2, regen)
    assert set(out) == {"compose_patient_first", "verify_patient", "compose_patient", "verify_patient_2",
                        "patient_held_back"}
    assert out["patient_held_back"] == [] and out["compose_patient"]["revised"] is True
    assert out["compose_patient"]["sections"][1]["text"].endswith("improving.")
    assert out["compose_patient_first"]["sections"][1]["text"].endswith("getting better.")
    assert len(f7.requests) == 2 and len(f7v.requests) == 2
    assert "A CHECK OF YOUR FIRST DRAFT" in f7.requests[1]["messages"][0]["content"][0]["text"]


def test_sequence_holds_back_a_section_still_flagged_and_rechecks_the_kept_text():
    out, _, _ = _seq(_payload_with(_FLAG), _payload_with(_FLAG), _good7())
    final = out["compose_patient"]
    assert out["patient_held_back"] == [{"section_id": "c_sections.1", "heading": "what_we_did"}]
    assert final["sections"][1]["text"] == V.HELD_BACK_TEXT
    assert final["sections"][0]["text"] == _step7_out()["sections"][0]["text"]     # others kept
    assert final["sections"][2]["text"] == "{{MEDICATIONS}}"                         # boxes untouched
    _, recheck, _ = P.check_patient_version({"sections": [{k: s[k] for k in ("heading", "source_ids", "text")}
                                                          for s in final["sections"]]}, STEP7_INPUT)
    assert final["checks"] == recheck


def test_sequence_unreached_only_is_regenerated_but_never_held_back():
    unreached = _item("", "c_sections.0", "sections.0", "unreached", "never_drop_item", "unable to weight bear")
    out, _, _ = _seq(_payload_with(extra=[unreached]), _payload_with(extra=[unreached]), _good7())
    assert out["patient_held_back"] == [] and out["verify_patient_2"]["flagged"] == 1


def test_sequence_failure_propagates():
    f7 = FakeBedrock(_resp7(_good7()))
    f7v = FakeBedrock(_response({"items": []}, stop="max_tokens"))
    with pytest.raises(StepError) as exc:
        V.verified_patient_version(STEP7_INPUT, lambda i: run_step("compose_patient", i, ctx=_ctx(f7)),
                                   lambda i: run_step("verify_patient", i, ctx=_ctx(f7v)))
    assert exc.value.code == "max_tokens"


# --- v0.2 (CSO audit, 4 Nov 2026) ---------------------------------------------------

def test_a_sentence_may_carry_one_item_per_fault():
    p = _payload_with(("c_sections.1.s1", {"verdict": "changed", "reason": "term_changed", "span": "replace part of the hip",
                                           "a_section_id": "sections.2"}),
                      extra=[_item("c_sections.1.s1", "c_sections.1", "sections.2", "added", "cause_or_purpose",
                                   "an operation")])
    out = _run(p)
    on = [v for v in out["verdicts"] if v["sentence_id"] == "c_sections.1.s1"]
    assert [v["reason"] for v in on] == ["term_changed", "cause_or_purpose"]
    assert out["flagged"] == 2 and out["counts"]["faults"] == 2 and out["counts"]["sentences"] == 5


def test_an_exact_repeat_is_flagged_and_kept_once():
    item = _item("c_sections.3.s1", "c_sections.3", "sections.4", "added", "new_fact", "bone team")
    out = _run(_payload_with(("c_sections.3.s1", dict(item)), extra=[dict(item)]))
    assert "item_repeated" in _names(out) and out["flagged"] == 1


def test_a_supported_verdict_on_an_uncited_source_counts_as_flagged():
    out = _run(_payload_with(("c_sections.3.s1", {"a_section_id": "sections.1"})))     # next_steps cites only sections.4
    v = next(v for v in out["verdicts"] if v["sentence_id"] == "c_sections.3.s1")
    assert v["verdict"] == "supported" and v["evidence_valid"] is False and not V.acceptable(v)
    assert out["flagged"] == 1 and out["counts"]["supported_uncited"] == 1 and out["counts"]["supported"] == 4


def test_an_uncited_supported_sentence_is_sent_back_and_then_held_back():
    p = _payload_with(("c_sections.3.s1", {"a_section_id": "sections.1"}))
    out, f7, _ = _seq(p, p, _good7())
    assert out["patient_held_back"] == [{"section_id": "c_sections.3", "heading": "next_steps"}]
    assert "says something the sections do not say" in f7.requests[1]["messages"][0]["content"][0]["text"]


# --- CSO audit 2 (4 Nov 2026): evidence failures survive de-duplication --------------

@pytest.mark.parametrize("order", ["valid_first", "uncited_first"])
def test_an_uncited_supported_item_is_never_lost_as_a_repeat_of_a_valid_one(order):
    valid = _item("c_sections.3.s1", "c_sections.3", "sections.4")          # next_steps cites sections.4
    uncited = _item("c_sections.3.s1", "c_sections.3", "sections.1")        # same item, uncited evidence
    p = _all_supported(_vin())
    p["items"] = [i for i in p["items"] if i["sentence_id"] != "c_sections.3.s1"]
    p["items"] += [valid, uncited] if order == "valid_first" else [uncited, valid]
    out = _run(p)
    on = [v for v in out["verdicts"] if v["sentence_id"] == "c_sections.3.s1"]
    assert len(on) == 2 and {v["evidence_valid"] for v in on} == {True, False}
    assert out["flagged"] == 1 and out["counts"]["supported"] == 4 and "item_repeated" not in _names(out)


def test_an_exact_repeat_with_the_same_evidence_is_still_kept_once():
    item = _item("c_sections.3.s1", "c_sections.3", "sections.4")
    p = _all_supported(_vin())
    p["items"].append(dict(item))
    out = _run(p)
    assert "item_repeated" in _names(out) and out["flagged"] == 0
    assert len([v for v in out["verdicts"] if v["sentence_id"] == "c_sections.3.s1"]) == 1


@pytest.mark.parametrize("order", ["valid_first", "uncited_first"])
def test_the_v3_sequence_holds_back_a_sentence_whose_only_problem_is_a_second_uncited_item(order):
    valid = _item("c_sections.3.s1", "c_sections.3", "sections.4")
    uncited = _item("c_sections.3.s1", "c_sections.3", "sections.1")
    p = _all_supported(_vin())
    p["items"] = [i for i in p["items"] if i["sentence_id"] != "c_sections.3.s1"]
    p["items"] += [valid, uncited] if order == "valid_first" else [uncited, valid]
    out, _, _ = _seq(p, p, _good7())
    assert out["patient_held_back"] == [{"section_id": "c_sections.3", "heading": "next_steps"}]



@pytest.mark.skipif(not all((ROOT / "evals" / "runs" / b).is_dir() for b in CAL_BATCHES),
                    reason="calibration batches not mounted")
def test_the_prompt_shares_no_three_word_sequence_with_any_labelled_defect():
    """v0.3 rule: the prompt must not teach the verifier the labelled defects (dev OR held-out)."""
    import calibrate_verifier as CV

    def grams(text):
        w = re.findall(r"[a-z0-9']+", text.casefold())
        return {" ".join(w[i:i + 3]) for i in range(len(w) - 2)}
    prompt = grams(V.system_prompt()[0])
    hits = set()
    for b in CAL_BATCHES:
        for rec in CV.records(ROOT / "evals" / "runs" / b):
            for lab in CV.labels_for(rec) + CV.map_labels(rec):
                if lab["ruling"] == "defect":
                    hits |= prompt & grams(lab.get("text") or lab.get("a_text") or lab["sentence"])
    assert hits == set()


def test_v03_keeps_faithful_definitions_and_rejects_unstated_patient_facts():
    text = " ".join(V.system_prompt()[0].split())
    assert "STATED, NOT TRUE" in text and "What a term means" in text and "What happened to this patient" in text
    for word in ("purpose", "cause", "place", "result", "outcome"):
        assert word in text



# Class vocabulary the verifier needs to recognise a category (kept on purpose); any OTHER
# ruled-defect token or short blind-read phrase found in the prompt is an instance word = a leak.
CLASS_VOCABULARY = {"because", "cause", "caused by", "to help", "improving", "scan", "camera test"}


@pytest.mark.skipif(not all((ROOT / "evals" / "runs" / b).is_dir() for b in CAL_BATCHES),
                    reason="calibration batches not mounted")
def test_no_instance_word_from_a_labelled_defect_is_in_the_prompt():
    import calibrate_verifier as CV

    def norm(s):
        return " " + " ".join(re.findall(r"[a-z0-9']+", s.casefold())) + " "
    prompt = norm(V.system_prompt()[0])
    found = set()
    for b in CAL_BATCHES:
        for rec in CV.records(ROOT / "evals" / "runs" / b):
            for lab in CV.labels_for(rec) + CV.map_labels(rec):
                if lab["ruling"] != "defect":
                    continue
                cues = [str(lab["token"]).replace("_", " ")] if lab.get("token") else []
                if lab["source"] == "map" and lab.get("text") and len(lab["text"].split()) <= 4:
                    cues.append(lab["text"])
                found |= {c.casefold() for c in cues if norm(c).strip() and norm(c) in prompt}
    assert found <= CLASS_VOCABULARY, found - CLASS_VOCABULARY
