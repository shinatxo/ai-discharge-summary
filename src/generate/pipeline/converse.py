"""One Bedrock Converse call with the pipeline's retry rule — shared by the model
steps (2 extract_facts, 5a reconcile_medications; later 6 and 7).

ADR-009 Q3 (3): retry ThrottlingException only, at most twice, 20–30 s jittered.
Never a read timeout (the 6 Jun 2026 lesson: silent retries of timeouts). Every
other failure becomes a StepError with a fixed code — never the provider's
message, which could echo request content. The deadline check before a retry
sleep belongs to the orchestrator (checklist item 5).

Moved out of extract.py on 27 Oct 2026 (step build piece 1) so step 5a uses the
same function, not a copy; behaviour unchanged.
"""

from __future__ import annotations

from botocore.exceptions import ClientError, ParamValidationError, ReadTimeoutError

from .context import StepContext
from .errors import StepError

THROTTLE_RETRIES = 2
THROTTLE_WAIT_S = (20.0, 30.0)


def converse_with_throttle_retry(ctx: StepContext, request: dict) -> tuple[dict, int]:
    """(response, attempts). Raises StepError: throttled, request_rejected,
    request_invalid, read_timeout, bedrock_error."""
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


def call_block(ctx: StepContext, prompt_sha256: str, resp: dict, attempts: int) -> dict:
    """The `call` block a model step returns — what its trace row needs (ADR-009 (b))."""
    usage = resp.get("usage", {})
    return {
        "model_id": ctx.model_id,
        "prompt_sha256": prompt_sha256,
        "stop_reason": resp.get("stopReason"),
        "attempts": attempts,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "cache_read_tokens": usage.get("cacheReadInputTokens", 0),
        "cache_write_tokens": usage.get("cacheWriteInputTokens", 0),
        "latency_ms": resp.get("metrics", {}).get("latencyMs"),
    }
