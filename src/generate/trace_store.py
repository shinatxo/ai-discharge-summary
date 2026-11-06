"""TraceTable writer (WS1a step build piece 6, 6 Nov 2026; ADR-009 (b)).

Implements `pipeline.orchestrate.TraceWriter` on DynamoDB. The orchestrator builds every item
(scrubbed, with PK / SK / ttl); this module only stores it.

    put(item): plain JSON -> typed DynamoDB (floats become Decimal; DynamoDB takes no floats)
               -> size bound -> PutItem with ConditionExpression attribute_not_exists(SK).

- Never overwrites: a second item with the same SK is rejected by DynamoDB
  (ConditionalCheckFailedException) and raised as a failure. "Never overwrite" is enforced
  by this condition in code — no IAM condition key can require it, and the table has no
  stream, so an overwrite would not be detected.
- Raises on ANY failure; the orchestrator maps it to trace_write_failed, or to
  trace_item_too_large for TraceItemTooLarge. Before release a failure fails the job.
- No other request parameters are sent: no ReturnValues and no
  ReturnValuesOnConditionCheckFailure (the role also allows only ReturnValues NONE).
- One request per write: the client is built with `CLIENT_CONFIG` (standard mode,
  total_max_attempts=1; connect 3 s, read 5 s). Verified 6 Nov 2026 with a local HTTP stub
  through botocore's transport: one request for a throttle, a 500 and a read timeout
  (docs/_local/p6-retry-probe). One request does not bound wall-clock time — the
  orchestrator's deadline watch does (piece 6, decision 1).
- Size: a CONSERVATIVE bound against AWS's documented approximation (read 6 Nov 2026,
  developerguide/CapacityUnitCalculations): names and strings in UTF-8 bytes; each number
  charged its decimal string length + 2 bytes (AWS: ~1 byte per two significant digits + 1);
  NULL / BOOL 1 byte; List / Map 3 bytes + 1 byte per element. Not checked against the
  service (item 7). Over the bound -> TraceItemTooLarge; never truncated.
- Logs: a failed write logs `job_id`, `SK` and one code from a fixed list — never the
  exception message, a response or item content (CloudWatch never receives trace content).
"""

from __future__ import annotations

import decimal
import json
import logging
import re
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import (ClientError, ConnectTimeoutError, EndpointConnectionError,
                                 ReadTimeoutError)

from pipeline.orchestrate import TRACE_ITEM_MAX_BYTES, TraceItemTooLarge

CLIENT_CONFIG = Config(connect_timeout=3, read_timeout=5,
                       retries={"mode": "standard", "total_max_attempts": 1})
CONDITION = "attribute_not_exists(SK)"

# Codes a failed write may log (fixed list; anything else -> "other").
AWS_CODES = frozenset({
    "ConditionalCheckFailedException", "ThrottlingException",
    "ProvisionedThroughputExceededException", "RequestLimitExceeded", "ValidationException",
    "AccessDeniedException", "ResourceNotFoundException", "InternalServerError",
    "ServiceUnavailable", "ItemCollectionSizeLimitExceededException",
})
LOCAL_CODES = ("item_too_large", "serialise_error", "read_timeout", "connect_timeout",
               "connect_error", "other")

_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
_SK = re.compile(r"META|DELIVERY|STEP#\d{2}[a-z]?#[a-z_]{1,40}#A\d{1,3}")
_SERIALISER = TypeSerializer()

logger = logging.getLogger(__name__)


def to_typed(item: dict) -> dict:
    """Plain JSON item -> DynamoDB typed item. Floats become Decimal; NaN / Infinity, sets
    and non-JSON types are rejected; numbers beyond DynamoDB's 38-digit precision raise."""
    plain = json.loads(json.dumps(item, allow_nan=False, ensure_ascii=False), parse_float=Decimal)
    return {k: _SERIALISER.serialize(v) for k, v in plain.items()}


def _value_size(v: dict) -> int:
    (t, x), = v.items()
    if t == "S":
        return len(x.encode("utf-8"))
    if t == "N":
        return len(x) + 2                       # conservative: AWS ~ digits/2 + 1
    if t == "B":
        return len(x)
    if t in ("NULL", "BOOL"):
        return 1
    if t == "L":
        return 3 + sum(1 + _value_size(e) for e in x)
    if t == "M":
        return 3 + sum(len(k.encode("utf-8")) + 1 + _value_size(e) for k, e in x.items())
    raise ValueError("unsupported type")        # sets never come from JSON


def item_size_bound(typed: dict) -> int:
    """Conservative upper estimate of a typed item's DynamoDB size, in bytes."""
    return sum(len(k.encode("utf-8")) + _value_size(v) for k, v in typed.items())


def _ids(item: Any) -> tuple[str, str]:
    pk = item.get("PK") if isinstance(item, dict) else None
    sk = item.get("SK") if isinstance(item, dict) else None
    job = pk[4:] if isinstance(pk, str) and pk.startswith("GEN#") else ""
    return (job if _JOB_ID.fullmatch(job) else "invalid",
            sk if isinstance(sk, str) and _SK.fullmatch(sk) else "invalid")


def failure_code(exc: BaseException) -> str:
    if isinstance(exc, TraceItemTooLarge):
        return "item_too_large"
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code if code in AWS_CODES else "other"
    if isinstance(exc, ReadTimeoutError):
        return "read_timeout"
    if isinstance(exc, ConnectTimeoutError):
        return "connect_timeout"
    if isinstance(exc, EndpointConnectionError):
        return "connect_error"
    if isinstance(exc, (TypeError, ValueError, decimal.DecimalException)):
        return "serialise_error"
    return "other"


class TraceStore:
    """orchestrate.TraceWriter on TraceTable. `client` is a boto3 DynamoDB client built with
    CLIENT_CONFIG (the worker builds it; tests inject a fake)."""

    def __init__(self, client: Any, table_name: str):
        self._client, self._table = client, table_name

    def put(self, item: dict) -> None:
        try:
            typed = to_typed(item)
            if item_size_bound(typed) > TRACE_ITEM_MAX_BYTES:
                raise TraceItemTooLarge()
            self._client.put_item(TableName=self._table, Item=typed, ConditionExpression=CONDITION)
        except BaseException as exc:
            job, sk = _ids(item)
            logger.error(json.dumps({"event": "trace_write_error", "job_id": job, "sk": sk,
                                     "code": failure_code(exc)}))
            raise
