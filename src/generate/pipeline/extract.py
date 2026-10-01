"""Step 2 — extract_facts (ADR-009 [A4]): the record_facts tool definition.

Step 2 is one Bedrock Converse call with a single tool, `record_facts`, marked
`strict: true` and forced with toolChoice. Strict means Bedrock compiles the
tool's inputSchema into a grammar, so the model can only emit a facts object
that matches schemas.py — short of truncation (stopReason max_tokens), which
fails the step. Forced means the model cannot answer in prose instead.

The inputSchema is record_facts_schema() itself, never a copy kept here: one
schema, so what step 3 checks is exactly what Bedrock enforced.

The tool description is sent to the model but is not part of the compiled
grammar. It is still a fixed string: nothing from the notes goes in a tool
definition. What each field means is in prompts/extract_facts.md.

extract_facts — run_step("extract_facts", {"lines": ...}, ctx=StepContext(...)):
  in:  {"lines": {"L001": text, ...}}            (step 1's line index)
  out: {"facts": <record_facts object>, "call": {model_id, prompt_sha256,
        stop_reason, attempts, input_tokens, output_tokens, cache_read_tokens,
        cache_write_tokens, latency_ms}}
Fails closed with a fixed code: bad_input, no_client (registry), max_tokens,
unexpected_stop_reason, no_tool_call, facts_not_object, throttled,
read_timeout, request_rejected, request_invalid, bedrock_error.
The output carries no copy of the notes — only the facts object (whose
quotes are the ≤200-character evidence the trace allows) and numbers/ids
about the call. The deadline check before a retry sleep belongs
to the W3 orchestrator.

Nothing here creates an AWS client (package rule, __init__.py).
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from botocore.exceptions import ClientError, ParamValidationError, ReadTimeoutError

from .context import StepContext
from .errors import StepError
from .schemas import record_facts_schema

TOOL_NAME = "record_facts"
TOOL_DESCRIPTION = (
    "Record the facts extracted from the line-indexed notes, each with its "
    "citations. Call exactly once."
)


def tool_config() -> dict:
    """The Converse `toolConfig` for step 2: one strict tool, forced.

    Built fresh on each call (record_facts_schema() returns a deep copy), so a
    caller that mutates it cannot change what the next call sends.
    """
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": TOOL_NAME,
                    "description": TOOL_DESCRIPTION,
                    "inputSchema": {"json": record_facts_schema()},
                    "strict": True,
                }
            }
        ],
        "toolChoice": {"tool": {"name": TOOL_NAME}},
    }


# --- the step ------------------------------------------------------------------

MAX_TOKENS = 8192          # 30 Sep 2026: truncation fails the job, so generous; at
                           # ~63 tok/s the 90 s read timeout binds first (~5.7k out)
TEMPERATURE = 0
THROTTLE_RETRIES = 2       # ADR-009 Q3 (3): ThrottlingException only, at most twice
THROTTLE_WAIT_S = (20.0, 30.0)

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract_facts.md"
PROMPT_MARKER = "## SYSTEM PROMPT"
_LINE_ID = re.compile(r"^L\d{3}$")


@lru_cache(maxsize=1)
def system_prompt() -> tuple[str, str]:
    """(text sent to Bedrock, its SHA-256). Read on first use, not at import.
    Only the text after the marker is sent and hashed, so editing the file's
    change-log header does not change prompt_sha256."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    if PROMPT_MARKER not in raw:
        raise RuntimeError("extract_facts.md is missing the SYSTEM PROMPT marker")
    text = raw.split(PROMPT_MARKER, 1)[1].strip()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def notes_as_user_text(lines: dict) -> str:
    """The user turn: one "L001: <text>" per line, in ID order, blanks included."""
    return "\n".join(f"{lid}: {lines[lid]}" for lid in sorted(lines))


def _check_lines(inp) -> dict:
    lines = inp.get("lines") if isinstance(inp, dict) else None
    if (not isinstance(lines, dict) or not lines
            or any(not isinstance(k, str) or not _LINE_ID.match(k) or not isinstance(v, str)
                   for k, v in lines.items())):
        raise StepError("bad_input")
    return lines


def _converse_with_throttle_retry(ctx: StepContext, request: dict) -> tuple[dict, int]:
    attempts = 0
    while True:
        attempts += 1
        try:
            return ctx.bedrock.converse(**request), attempts
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ThrottlingException":
                if attempts > THROTTLE_RETRIES:
                    raise StepError("throttled") from None
                ctx.sleep(ctx.jitter(*THROTTLE_WAIT_S))
                continue
            if code == "ValidationException":
                raise StepError("request_rejected") from None   # Bedrock refused the request
            raise StepError("bedrock_error") from None
        except ParamValidationError:
            # botocore refused before sending — e.g. an SDK too old to know toolSpec.strict
            raise StepError("request_invalid") from None
        except ReadTimeoutError:
            raise StepError("read_timeout") from None     # never retried: the 6 Jun lesson
        except Exception:
            raise StepError("bedrock_error") from None


def extract_facts(inp: dict, ctx: StepContext) -> dict:
    lines = _check_lines(inp)
    prompt, prompt_sha256 = system_prompt()

    system = [{"text": prompt}]
    if ctx.prompt_caching:
        system.append({"cachePoint": {"type": "default"}})   # notes stay below it, never cached
    request = {
        "modelId": ctx.model_id,
        "system": system,
        "messages": [{"role": "user", "content": [{"text": notes_as_user_text(lines)}]}],
        "inferenceConfig": {"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
        "toolConfig": tool_config(),
    }
    resp, attempts = _converse_with_throttle_retry(ctx, request)

    stop = resp.get("stopReason")
    if stop == "max_tokens":
        raise StepError("max_tokens")          # truncated: the facts object is incomplete
    uses = [b["toolUse"] for b in resp.get("output", {}).get("message", {}).get("content", [])
            if isinstance(b, dict) and "toolUse" in b]
    if len(uses) != 1 or uses[0].get("name") != TOOL_NAME:
        raise StepError("no_tool_call")
    if stop != "tool_use":
        raise StepError("unexpected_stop_reason")
    facts = uses[0].get("input")
    if not isinstance(facts, dict):
        raise StepError("facts_not_object")

    usage = resp.get("usage", {})
    return {
        "facts": facts,
        "call": {
            "model_id": ctx.model_id,
            "prompt_sha256": prompt_sha256,
            "stop_reason": stop,
            "attempts": attempts,
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
            "cache_read_tokens": usage.get("cacheReadInputTokens", 0),
            "cache_write_tokens": usage.get("cacheWriteInputTokens", 0),
            "latency_ms": resp.get("metrics", {}).get("latencyMs"),
        },
    }
