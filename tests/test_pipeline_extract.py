"""Step 2's tool definition: the schema Bedrock enforces IS schemas.py's.

If these fail, Bedrock would be compiling a grammar for a different facts
object than the one step 3 validates — or the model could answer without the
tool. No AWS: the definition is plain data.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline.extract import TOOL_DESCRIPTION, TOOL_NAME, tool_config  # noqa: E402
from pipeline.schemas import record_facts_schema  # noqa: E402


def _spec(cfg=None):
    cfg = cfg or tool_config()
    return cfg["tools"][0]["toolSpec"]


def test_input_schema_is_the_schemas_py_schema():
    assert _spec()["inputSchema"] == {"json": record_facts_schema()}


def test_exactly_one_tool_strict_and_forced():
    cfg = tool_config()
    assert len(cfg["tools"]) == 1
    assert _spec(cfg)["name"] == TOOL_NAME == "record_facts"
    assert _spec(cfg)["strict"] is True
    assert cfg["toolChoice"] == {"tool": {"name": TOOL_NAME}}   # forced, not "auto"/"any"


def test_toolspec_has_only_the_expected_keys():
    # Anything extra would be sent to Bedrock unreviewed.
    assert set(_spec()) == {"name", "description", "inputSchema", "strict"}
    assert set(tool_config()) == {"tools", "toolChoice"}


def test_mutating_one_config_does_not_leak_into_the_next():
    cfg = tool_config()
    _spec(cfg)["inputSchema"]["json"]["properties"]["age_group"]["enum"].append("teenager")
    _spec(cfg)["strict"] = False
    fresh = tool_config()
    assert "teenager" not in _spec(fresh)["inputSchema"]["json"]["properties"]["age_group"]["enum"]
    assert _spec(fresh)["strict"] is True


def test_config_is_json_and_deterministic():
    # pipeline_version will hash the tool definition with the schema.
    a = json.dumps(tool_config(), sort_keys=True)
    assert a == json.dumps(tool_config(), sort_keys=True)


def test_description_is_fixed_text_with_no_note_like_content():
    assert _spec()["description"] == TOOL_DESCRIPTION
    assert not re.search(r"\bL\d{3}\b|\d", TOOL_DESCRIPTION)   # no line IDs, no numbers
    assert len(TOOL_DESCRIPTION) <= 200


# --- extract_facts through run_step, with a fake Bedrock client ---------------

import copy  # noqa: E402

import pytest  # noqa: E402
from botocore.exceptions import ClientError, ParamValidationError, ReadTimeoutError  # noqa: E402

from pipeline import StepContext, StepError, run_step  # noqa: E402
from pipeline.extract import MAX_TOKENS, notes_as_user_text, system_prompt  # noqa: E402
from pipeline.schemas import FIELD_NAMES  # noqa: E402

LINES = {"L001": "Day 1 76M. PC: cough", "L002": "", "L003": "DH: apixaban 5mg BD"}


def _facts():
    return {
        "field_status": [{"field": n, "status": "not_documented"} for n in FIELD_NAMES],
        "facts": [],
        "discharge_status": "not_documented",
        "resus": {"form_or_discussion_documented": False, "status_documented": "not_documented",
                  "changed": "not_documented", "cites": []},
        "age_group": "adult", "suspicious_text": [],
    }


def _tool_response(facts=None, stop="tool_use", name="record_facts", extra_blocks=()):
    return {
        "output": {"message": {"role": "assistant", "content": [
            *extra_blocks,
            {"toolUse": {"toolUseId": "t1", "name": name, "input": facts if facts is not None else _facts()}},
        ]}},
        "stopReason": stop,
        "usage": {"inputTokens": 3000, "outputTokens": 900,
                  "cacheReadInputTokens": 0, "cacheWriteInputTokens": 2400},
        "metrics": {"latencyMs": 12345},
    }


class FakeBedrock:
    """Returns (or raises) the scripted items in order; records every request."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def converse(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _throttle():
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse")


def _ctx(fake, **kw):
    sleeps = kw.pop("sleeps", [])
    return StepContext(bedrock=fake, model_id="anthropic.claude-sonnet-4-6",
                       sleep=sleeps.append, jitter=lambda a, b: (a + b) / 2, **kw)


def test_happy_path_returns_facts_and_call_metadata():
    fake = FakeBedrock(_tool_response())
    out = run_step("extract_facts", {"lines": LINES}, ctx=_ctx(fake))
    assert out["facts"] == _facts()
    call = out["call"]
    assert call["stop_reason"] == "tool_use" and call["attempts"] == 1
    assert (call["input_tokens"], call["output_tokens"], call["cache_write_tokens"]) == (3000, 900, 2400)
    assert call["latency_ms"] == 12345
    assert call["prompt_sha256"] == system_prompt()[1]


def test_request_shape():
    fake = FakeBedrock(_tool_response())
    run_step("extract_facts", {"lines": LINES}, ctx=_ctx(fake))
    req = fake.requests[0]
    assert req["modelId"] == "anthropic.claude-sonnet-4-6"
    assert req["toolConfig"] == tool_config()
    assert req["inferenceConfig"] == {"maxTokens": MAX_TOKENS, "temperature": 0}
    assert req["system"] == [{"text": system_prompt()[0]}]          # caching off: no cachePoint
    assert req["messages"] == [{"role": "user", "content": [{"text": notes_as_user_text(LINES)}]}]


def test_user_text_is_line_indexed_in_order_blanks_included():
    shuffled = {"L003": "c", "L001": "a", "L002": ""}
    assert notes_as_user_text(shuffled) == "L001: a\nL002: \nL003: c"


def test_cache_point_follows_system_prompt_when_caching_on():
    fake = FakeBedrock(_tool_response())
    run_step("extract_facts", {"lines": LINES}, ctx=_ctx(fake, prompt_caching=True))
    assert fake.requests[0]["system"] == [{"text": system_prompt()[0]},
                                          {"cachePoint": {"type": "default"}}]


def test_model_step_without_ctx_fails_closed():
    with pytest.raises(StepError) as e:
        run_step("extract_facts", {"lines": LINES})
    assert e.value.code == "no_client"


@pytest.mark.parametrize("inp", [{}, {"lines": {}}, {"lines": ["L001"]},
                                 {"lines": {"1": "x"}}, {"lines": {"L001": 5}}])
def test_bad_input_fails_before_any_call(inp):
    fake = FakeBedrock()
    with pytest.raises(StepError) as e:
        run_step("extract_facts", inp, ctx=_ctx(fake))
    assert e.value.code == "bad_input" and fake.requests == []


@pytest.mark.parametrize("resp,code", [
    (_tool_response(stop="max_tokens"), "max_tokens"),
    ({"output": {"message": {"content": [{"text": "Here are the facts"}]}},
      "stopReason": "end_turn"}, "no_tool_call"),
    (_tool_response(name="something_else"), "no_tool_call"),
    (_tool_response(extra_blocks=({"toolUse": {"toolUseId": "t0", "name": "record_facts",
                                                "input": {}}},)), "no_tool_call"),
    (_tool_response(stop="end_turn"), "unexpected_stop_reason"),
    (_tool_response(facts=["not", "an", "object"]), "facts_not_object"),
])
def test_response_failures_fail_closed(resp, code):
    with pytest.raises(StepError) as e:
        run_step("extract_facts", {"lines": LINES}, ctx=_ctx(FakeBedrock(resp)))
    assert e.value.code == code


def test_throttle_is_retried_twice_with_20_to_30_s_waits():
    sleeps = []
    fake = FakeBedrock(_throttle(), _throttle(), _tool_response())
    out = run_step("extract_facts", {"lines": LINES}, ctx=_ctx(fake, sleeps=sleeps))
    assert out["call"]["attempts"] == 3 and len(fake.requests) == 3
    assert sleeps == [25.0, 25.0] and all(20 <= s <= 30 for s in sleeps)


def test_third_throttle_fails_closed():
    fake = FakeBedrock(_throttle(), _throttle(), _throttle())
    with pytest.raises(StepError) as e:
        run_step("extract_facts", {"lines": LINES}, ctx=_ctx(fake))
    assert e.value.code == "throttled" and len(fake.requests) == 3


@pytest.mark.parametrize("exc,code", [
    (ReadTimeoutError(endpoint_url="https://bedrock-runtime"), "read_timeout"),
    (ClientError({"Error": {"Code": "ValidationException", "Message": "m"}}, "Converse"),
     "request_rejected"),
    (ParamValidationError(report="Unknown parameter strict"), "request_invalid"),
    (ClientError({"Error": {"Code": "ModelTimeoutException", "Message": "m"}}, "Converse"),
     "bedrock_error"),
])
def test_other_errors_are_not_retried(exc, code):
    fake = FakeBedrock(exc, _tool_response())
    with pytest.raises(StepError) as e:
        run_step("extract_facts", {"lines": LINES}, ctx=_ctx(fake))
    assert e.value.code == code and len(fake.requests) == 1


def test_output_carries_no_note_text():
    note_line = "unique-note-text-7f3a"
    fake = FakeBedrock(_tool_response())
    out = run_step("extract_facts", {"lines": {"L001": note_line}}, ctx=_ctx(fake))
    assert note_line not in json.dumps(out)
    assert "lines" not in out


def test_code_steps_still_run_without_ctx():
    assert run_step("guard_input", {"notes": "a\nb"})["line_count"] == 2


# --- the prompt file ---------------------------------------------------------

def test_prompt_is_split_not_whole_v07():
    text, sha = system_prompt()
    assert len(text) > 4096                          # ~1k tokens: above the prompt-cache minimum
    assert "record_facts" in text and "L001" in text
    for gone in ("PART B", "PART C", "most likely", "GP LETTER"):
        assert gone not in text                      # composing and the §2a template left the step
    assert "## SYSTEM PROMPT" not in text


def test_every_schema_field_is_explained_in_the_prompt():
    text = system_prompt()[0]
    names = list(FIELD_NAMES) + ["pre_admission", "discharge_status", "none_required",
                                 "referenced_not_listed", "form_or_discussion_documented",
                                 "status_documented", "documented_advice", "age_group",
                                 "contradictions", "suspicious_text", "inferred_flagged",
                                 "field_status", "facts", "section", "discharge"]
    missing = [n for n in names if f"`{n}`" not in text]
    assert missing == []
