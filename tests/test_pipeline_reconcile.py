"""Step 5a — reconcile_medications (ADR-009 row 5a): the record_reconciliation
schema and tool, the m0.1 prompt, and the step through run_step with a fake
Bedrock. No AWS.

The notes below are invented for these tests (four drugs, one conflict) — not
a canary scenario. Steps 1, 3 and 3b run for real on them, so 5a gets exactly
the input shape it gets in the pipeline.
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

from pipeline import STEPS, StepContext, StepError, run_step  # noqa: E402
from pipeline import reconcile as R  # noqa: E402
from pipeline.schemas import (FIELD_NAMES, RECON_TAGS, record_reconciliation_schema,  # noqa: E402
                              shape_errors)
from test_pipeline_schema import limit_violations  # noqa: E402

NOTES = "\n".join([
    "Day 1 70F admitted with fast AF.",
    "DH: bisoprolol 2.5mg OD, ramipril 5mg OD",
    "Warfarin 3mg OD",
    "Plan: bisoprolol up to 5mg OD. Start digoxin 125mcg OD.",
    "TTO: warfarin 5mg OD",
    "Pharmacy reconciliation: warfarin 4mg OD",
    "",
])


def _cite(lines, quote):
    return {"lines": lines, "quote": quote}


def _wire_facts(discharge_status="listed", extra_pre=()):
    facts = [
        {"section": "pre_admission", "value": "bisoprolol 2.5mg OD", "cites": [_cite(["L002"], "bisoprolol 2.5mg OD")]},
        {"section": "pre_admission", "value": "ramipril 5mg OD", "cites": [_cite(["L002"], "ramipril 5mg OD")]},
        {"section": "pre_admission", "value": "warfarin 3mg OD", "cites": [_cite(["L003"], "Warfarin 3mg OD")]},
        *extra_pre,
        {"section": "discharge", "value": "bisoprolol 5mg OD", "cites": [_cite(["L004"], "bisoprolol up to 5mg OD")]},
        {"section": "discharge", "value": "digoxin 125mcg OD", "cites": [_cite(["L004"], "digoxin 125mcg OD")]},
        {"section": "contradictions", "value": "warfarin dose differs between TTO and pharmacy",
         "cites": [_cite(["L005"], "warfarin 5mg OD"), _cite(["L006"], "warfarin 4mg OD")]},
    ]
    return {
        "field_status": [{"field": n, "status": "not_documented"} for n in FIELD_NAMES],
        "facts": facts, "discharge_status": discharge_status,
        "resus": {"form_or_discussion_documented": False, "status_documented": "not_documented",
                  "changed": "not_documented", "cites": []},
        "age_group": "adult", "suspicious_text": [],
    }


def _inputs(**kw):
    g = run_step("guard_input", {"notes": NOTES})
    v = run_step("validate_facts", {"facts": _wire_facts(**kw), "lines": g["lines"]})
    ev = run_step("retrieve_evidence", {"facts": v["facts"], "lines": g["lines"], "flags": g["flags"]})
    return {"lines": g["lines"], "facts": v["facts"], "evidence": ev}


def _drug(drug, tag, cites, basis="documented", dose="", route="", frequency="OD",
          previous_dose="", previous_frequency="", conflict=False):
    return {"drug": drug, "cites": cites, "tag": tag, "basis": basis, "dose": dose, "route": route,
            "frequency": frequency, "previous_dose": previous_dose,
            "previous_frequency": previous_frequency, "conflict": conflict}


def _c(role, lines, quote):
    return {"role": role, "lines": lines, "quote": quote}


def _good():
    return {"drugs": [
        _drug("bisoprolol", "increased", [_c("dh", ["L002"], "bisoprolol 2.5mg OD"),
                                          _c("discharge", ["L004"], "bisoprolol up to 5mg OD")],
              dose="5mg", previous_dose="2.5mg", previous_frequency="OD"),
        _drug("ramipril", "continued", [_c("dh", ["L002"], "ramipril 5mg OD")],
              basis="inferred_from_dh", dose="5mg"),
        _drug("warfarin", "increased", [_c("dh", ["L003"], "Warfarin 3mg OD"),
                                        _c("discharge", ["L005"], "warfarin 5mg OD"),
                                        _c("discharge", ["L006"], "warfarin 4mg OD")],
              previous_dose="3mg", conflict=True),
        _drug("digoxin", "new", [_c("discharge", ["L004"], "digoxin 125mcg OD")], dose="125mcg"),
    ]}


def _response(payload=None, stop="tool_use", name="record_reconciliation"):
    return {"output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "t1", "name": name,
                             "input": payload if payload is not None else _good()}}]}},
            "stopReason": stop,
            "usage": {"inputTokens": 5200, "outputTokens": 700,
                      "cacheReadInputTokens": 1700, "cacheWriteInputTokens": 0},
            "metrics": {"latencyMs": 8100}}


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


def _run(payload=None, inp=None, **kw):
    return run_step("reconcile_medications", inp or _inputs(), ctx=_ctx(FakeBedrock(_response(payload)), **kw))


def _by_drug(out):
    return {e["drug"]: e for e in out["reconciliation"]}


# --- schema ----------------------------------------------------------------------

def test_schema_is_inside_bedrocks_limits_and_annex_c2():
    assert limit_violations(record_reconciliation_schema()) == []


def test_schema_is_small_and_has_the_cite_shape_once():
    # W2 part 2: the grammar grows with the number of places a shape occurs.
    dumped = json.dumps(record_reconciliation_schema())
    assert len(dumped) <= 1500
    assert dumped.count('"role": {') == 1 and dumped.count('"quote": {') == 1


def test_evidence_comes_before_the_values():
    # The model fills the schema in order: cites first, then what is read from them.
    props = list(record_reconciliation_schema()["properties"]["drugs"]["items"]["properties"])
    assert props == ["drug", "cites", "tag", "basis", "dose", "route", "frequency",
                     "previous_dose", "previous_frequency", "conflict"]


def test_tags_cover_every_gold_tag():
    gold_tags = {d["tag"] for p in (ROOT / "evals" / "gold").glob("*.json")
                 for d in json.loads(p.read_text())["medications"]["drugs"]}
    assert gold_tags <= set(RECON_TAGS)


def test_tool_is_strict_forced_and_uses_the_schema():
    cfg = R.tool_config()
    spec = cfg["tools"][0]["toolSpec"]
    assert len(cfg["tools"]) == 1 and spec["name"] == R.TOOL_NAME == "record_reconciliation"
    assert spec["strict"] is True and cfg["toolChoice"] == {"tool": {"name": R.TOOL_NAME}}
    assert spec["inputSchema"] == {"json": record_reconciliation_schema()}
    assert set(spec) == {"name", "description", "inputSchema", "strict"}
    assert not re.search(r"\bL\d{3}\b|\d", R.TOOL_DESCRIPTION)


# --- prompt ----------------------------------------------------------------------

def test_prompt_is_cacheable_and_carries_no_composing():
    text, _ = R.system_prompt()
    assert len(text) > 4096 and "## SYSTEM PROMPT" not in text
    for gone in ("PART A", "PART B", "PART C", "GP LETTER"):
        assert gone not in text


def test_every_schema_name_and_value_is_explained_in_the_prompt():
    text = R.system_prompt()[0]
    names = ["record_reconciliation", "drug", "cites", "tag", "basis", "dose", "route", "frequency",
             "previous_dose", "previous_frequency", "conflict", "role", "dh", "discharge", "lines",
             "quote", "documented", "inferred_from_dh", *RECON_TAGS]
    assert [n for n in names if f"`{n}`" not in text] == []


# --- the step ---------------------------------------------------------------------

def test_registered_as_05a_model_step():
    s = STEPS["reconcile_medications"]
    assert (s.seq, s.kind) == ("05a", "model")
    seqs = [st.seq for st in STEPS.values()]
    assert seqs == sorted(seqs)


def test_without_ctx_fails_closed():
    with pytest.raises(StepError) as e:
        run_step("reconcile_medications", _inputs())
    assert e.value.code == "no_client"


def test_happy_path_groups_verifies_and_copies_status():
    out = _run()
    d = _by_drug(out)
    assert list(d) == ["bisoprolol", "ramipril", "warfarin", "digoxin"]
    b = d["bisoprolol"]
    assert (b["tag"], b["dose"], b["route"], b["frequency"]) == ("increased", "5mg", None, "OD")
    assert b["previous"] == {"dose": "2.5mg", "frequency": "OD"}
    assert [c["lines"] for c in b["dh_cites"]] == [["L002"]] and [c["lines"] for c in b["discharge_cites"]] == [["L004"]]
    assert d["ramipril"]["basis"] == "inferred_from_dh" and d["ramipril"]["discharge_cites"] == []
    w = d["warfarin"]
    assert w["conflict"] is True and w["dose"] is None and len(w["discharge_cites"]) == 2
    assert all(e["citation_status"] == "verified" and e["checks"] == [] for e in out["reconciliation"])
    assert out["discharge_status"] == "listed" and out["reconciliation_check"] == "ok"
    assert out["dh_lines_unreconciled"] == []
    assert out["counts"] == {"entries": 4, "cites_total": 7, "cites_verified": 7, "entries_unverified": 0,
                             "entries_flagged": 0, "dh_lines_unreconciled": 0}
    c = out["call"]
    assert (c["input_tokens"], c["output_tokens"], c["cache_read_tokens"], c["latency_ms"]) == (5200, 700, 1700, 8100)
    assert c["prompt_sha256"] == R.system_prompt()[1] and c["attempts"] == 1
    assert [e["entry_id"] for e in out["reconciliation"]] == [f"reconciliation.{i}" for i in range(4)]


def test_unverified_citation_is_flagged_kept_and_never_repaired():
    p = _good()
    p["drugs"][3]["cites"][0]["quote"] = "digoxin 250mcg OD"          # not what L004 says
    d = _by_drug(_run(p))["digoxin"]
    assert d["citation_status"] == "citation_unverified" and d["reason"] == "quote_not_found"
    assert d["discharge_cites"][0]["quote"] == "digoxin 250mcg OD"     # kept as written
    assert d["dose"] == "125mcg"                                       # the entry stays


def test_wrong_neighbouring_line_is_caught():
    p = _good()
    p["drugs"][3]["cites"][0]["lines"] = ["L005"]                       # the quote is on L004
    assert _by_drug(_run(p))["digoxin"]["reason"] == "quote_not_found"


def test_over_long_quote_is_dropped_not_truncated():
    p = _good()
    p["drugs"][3]["cites"][0]["quote"] = "x" * 201
    c = _by_drug(_run(p))["digoxin"]["discharge_cites"][0]
    assert (c["quote"], c["reason"]) == ("", "quote_too_long")


@pytest.mark.parametrize("change, drug, check", [
    (lambda p: p["drugs"][3].update(cites=[]), "digoxin", "missing_discharge_cite"),
    (lambda p: p["drugs"][0].update(cites=[p["drugs"][0]["cites"][1]]), "bisoprolol", "missing_dh_cite"),
    (lambda p: p["drugs"][1].update(tag="stopped"), "ramipril", "inferred_not_continued"),
    (lambda p: p["drugs"][2].update(cites=p["drugs"][2]["cites"][:2]), "warfarin", "conflict_one_side"),
    (lambda p: p["drugs"][0].update(previous_dose=""), "bisoprolol", "previous_missing"),
    (lambda p: p["drugs"][3].update(previous_dose="62.5mcg"), "digoxin", "previous_on_unchanged_tag"),
])
def test_checks_flag_and_never_fix(change, drug, check):
    p = _good()
    change(p)
    out = _run(p)
    e = _by_drug(out)[drug]
    assert check in e["checks"] and out["counts"]["entries_flagged"] >= 1
    assert len(out["reconciliation"]) == 4                              # nothing dropped


def test_duplicate_drug_is_flagged():
    p = _good()
    p["drugs"].append(copy.deepcopy(p["drugs"][3]))
    assert _run(p)["reconciliation"][4]["checks"] == ["duplicate_drug"]


def test_a_dropped_drug_history_line_is_reported_by_id():
    p = _good()
    del p["drugs"][2]                                                   # warfarin (L003) left out
    out = _run(p)
    assert out["dh_lines_unreconciled"] == ["L003"] and out["counts"]["dh_lines_unreconciled"] == 1


def test_a_drug_dropped_from_a_shared_line_is_not_detected_line_level_limit():
    # Honest limit, pinned: coverage is by LINE. Ramipril shares L002 with
    # bisoprolol, so leaving ramipril out is invisible here — D4 catches it.
    p = _good()
    del p["drugs"][1]
    assert _run(p)["dh_lines_unreconciled"] == []


@pytest.mark.parametrize("status", ["not_documented", "none_required"])
def test_active_drugs_where_step_3_recorded_none_are_flagged(status):
    out = _run(inp=_inputs(discharge_status=status))
    assert out["discharge_status"] == status and out["reconciliation_check"] == "drugs_where_none_documented"


def test_only_stopped_or_withheld_where_none_documented_is_ok():
    p = {"drugs": [_drug("warfarin", "withheld", [_c("dh", ["L003"], "Warfarin 3mg OD"),
                                                  _c("discharge", ["L005"], "warfarin 5mg OD")], dose="3mg")]}
    assert _run(p, inp=_inputs(discharge_status="not_documented"))["reconciliation_check"] == "ok"


# --- the request --------------------------------------------------------------------

def test_request_shape_and_cache_point():
    fake = FakeBedrock(_response())
    inp = _inputs()
    run_step("reconcile_medications", inp, ctx=_ctx(fake, prompt_caching=True))
    req = fake.requests[0]
    assert req["toolConfig"] == R.tool_config()
    assert req["inferenceConfig"] == {"maxTokens": R.MAX_TOKENS, "temperature": 0}
    assert req["system"] == [{"text": R.system_prompt()[0]}, {"cachePoint": {"type": "default"}}]
    assert req["messages"] == [{"role": "user", "content": [
        {"text": R.user_text(inp["lines"], inp["facts"], inp["evidence"]["medication"])}]}]


def test_user_text_has_three_parts_and_only_verified_facts():
    bad = {"section": "pre_admission", "value": "aspirin 75mg OD", "cites": [_cite(["L003"], "aspirin 75mg OD")]}
    inp = _inputs(extra_pre=(bad,))
    text = R.user_text(inp["lines"], inp["facts"], inp["evidence"]["medication"])
    assert text.index("NOTES") < text.index("MEDICATION LINES") < text.index("EXTRACTED FACTS")
    assert all(f"{lid}: {inp['lines'][lid]}" in text for lid in inp["lines"])   # every line, in the notes part
    assert "aspirin" not in text                                         # its citation failed step 3
    assert "- warfarin dose differs between TTO and pharmacy [L005, L006]" in text
    assert "discharge_status: listed" in text


def test_contradiction_on_no_medication_line_is_left_out():
    inp = _inputs()
    inp["facts"]["contradictions"][0]["cites"] = [
        {"lines": ["L001"], "quote": "Day 1", "verified": True, "reason": None},
        {"lines": ["L007"], "quote": "", "verified": True, "reason": None}]
    text = R.user_text(inp["lines"], inp["facts"], inp["evidence"]["medication"])
    assert "differs" not in text


# --- failures -----------------------------------------------------------------------

def _missing_key():
    p = _good()
    del p["drugs"][0]["conflict"]
    return p


@pytest.mark.parametrize("resp, code", [
    (_response(stop="max_tokens"), "max_tokens"),
    ({"output": {"message": {"content": [{"text": "Here is the list"}]}}, "stopReason": "end_turn"}, "no_tool_call"),
    (_response(name="record_facts"), "no_tool_call"),
    (_response(stop="end_turn"), "unexpected_stop_reason"),
    (_response(payload=["not", "an", "object"]), "reconciliation_not_object"),
    (_response(payload=_missing_key()), "reconciliation_shape_invalid"),
    (_response(payload={"drugs": [dict(_good()["drugs"][0], tag="paused")]}), "reconciliation_shape_invalid"),
])
def test_response_failures_fail_closed(resp, code):
    with pytest.raises(StepError) as e:
        run_step("reconcile_medications", _inputs(), ctx=_ctx(FakeBedrock(resp)))
    assert e.value.code == code


def _bad_inputs():
    good = _inputs()
    out = [{}, dict(good, lines={}), dict(good, facts={}), dict(good, evidence={}),
           dict(good, evidence={"medication": [{"line": "L099"}]})]
    f = copy.deepcopy(good)
    del f["facts"]["contradictions"]
    return out + [f]


@pytest.mark.parametrize("inp", _bad_inputs())
def test_bad_input_fails_before_any_call(inp):
    fake = FakeBedrock()
    with pytest.raises(StepError) as e:
        run_step("reconcile_medications", inp, ctx=_ctx(fake))
    assert e.value.code == "bad_input" and fake.requests == []


def test_throttle_retried_twice_then_succeeds():
    sleeps = []
    t = ClientError({"Error": {"Code": "ThrottlingException", "Message": "m"}}, "Converse")
    fake = FakeBedrock(t, t, _response())
    out = run_step("reconcile_medications", _inputs(), ctx=_ctx(fake, sleeps=sleeps))
    assert out["call"]["attempts"] == 3 and sleeps == [25.0, 25.0]


def test_read_timeout_is_not_retried():
    fake = FakeBedrock(ReadTimeoutError(endpoint_url="https://bedrock-runtime"), _response())
    with pytest.raises(StepError) as e:
        run_step("reconcile_medications", _inputs(), ctx=_ctx(fake))
    assert e.value.code == "read_timeout" and len(fake.requests) == 1


def test_output_never_carries_the_line_index_or_uncited_note_text():
    out = _run()
    dumped = json.dumps(out)
    assert "lines" not in out
    assert "fast AF" not in dumped and "Pharmacy reconciliation" not in dumped
    assert shape_errors({"drugs": []}, record_reconciliation_schema()) == []
