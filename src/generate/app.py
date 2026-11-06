"""
AI Discharge Summary Assistant - generate Lambda: the ADR-009 PIPELINE WORKER
(feature branch feat/agentic-pipeline; WS1a step build piece 5, 5 Nov 2026).

main still carries the v1 worker (one combined Bedrock call + the Patient v2 second
pass) until the W11 cut-over. This branch has NO switch between the two (ADR-009 (d):
"no runtime pipeline parameter"; CSO 5 Nov 2026): on the branch the dispatcher path
runs the pipeline, and main is the released configuration until the merge. The v1
direct-invoke path is not kept (ADR-009: "the agentic worker does not keep" it); its
audit PutItem permission is removed from the role at the cut-over.

Invocation contracts:

(1) DISPATCHER EVENT - the only working path. Invoked asynchronously by
    src/dispatcher/ (InvocationType='Event'); the dispatcher has ALREADY written the
    pending GEN# audit row. Event:
        {"job_id": "<ulid>", "user_sub": "<cognito sub>", "notes": "<notes>",
         "input_sha256": "<hex>", "started_at": "<iso8601 utc>"}
    The worker:
      a. CLAIMS the job (R2, CSO 5 Nov 2026): UpdateItem SET pipeline_version, only if
         status = pending AND pipeline_version is not yet set. A duplicate delivery fails
         the condition and exits WITHOUT calling Bedrock, writing a trace or marking the
         job failed. An uncertain claim response (any other error: DynamoDB documents that
         some failed writes leave the outcome unknown) also stops before any model call and
         is NOT cleared or taken over. Limitation (recorded): one confirmed claimant does
         not guarantee completion or recovery - a committed claim followed by a lost
         response or a crash leaves the job pending; the SPA gives up at 5 minutes.
      a2. REJECTS a mismatched or missing input_sha256 (input_hash_mismatch, before any
         model call): the audit row's input hash must describe the notes generated from.
      b. runs pipeline/orchestrate.py under a deadline: the earliest of 270 s from the
         dispatcher's started_at (the SPA's clock starts at the 202), 270 s from worker
         start, and Lambda's remaining time - 30 s. Any failure calls _mark_failed
         IMMEDIATELY (the orchestrator's failure callback), before in-flight requests are
         drained. The row gets a stable error_code and the fixed FAILURE_MESSAGE.
      c. releases only after 8b succeeded and the trace (every step + META) is written:
         one CONDITIONAL PutItem to ResultsTable (never overwrites; an existing item ->
         results_exists, job failed), then the audit flip to complete with 8b's hashes.
      d. writes a best-effort DELIVERY trace item (results / audit outcome). META is the
         pipeline outcome; DELIVERY is the delivery outcome.

(2) HTTP API event - refused with 410 Gone (anti-regression, slice 4b).

Anything else is refused: {"ok": false, "error": "unsupported_event"}.

Logs and the audit row never carry note text or model output text: logs carry job ids,
fixed codes and counts; the audit row carries hashes and fixed codes. The restricted
trace (item 6) deliberately keeps intermediate step outputs and short citations (quotes
<= 200 characters) for diagnosis, but never the raw notes or the line index
(orchestrate.scrub), and is never served to the SPA (ADR-009 (b)).

The execution role (infra/template.yaml): UpdateItem on the audit table with an attribute
whitelist; PutItem on ResultsTable; KMS via DynamoDB only; bedrock:InvokeModel on the one
pinned model; its own log group; PutItem on TraceTable only (checklist item 6, 6 Nov 2026;
ReturnValues NONE), through `trace_store.TraceStore` on its own client (one request per
write). Botocore's DEFAULT retries still apply to `_ddb` below (claim, marking, results,
flip): ten attempts on a throttle or a timeout — a recorded residual (piece 6, decision 8;
item-7 check), not changed here.
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

import trace_store
from pipeline import orchestrate

# --- configuration (injected by the template; no hard-coded resource ids) --------------
REGION = os.environ.get("AWS_REGION", "eu-west-2")
TABLE_NAME = os.environ["AUDIT_TABLE_NAME"]
RESULTS_TABLE_NAME = os.environ.get("RESULTS_TABLE_NAME", "")
TRACE_TABLE_NAME = os.environ.get("TRACE_TABLE_NAME", "")      # checklist item 6
MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
RESULTS_TTL_HOURS = int(os.environ.get("RESULTS_TTL_HOURS", "24"))
PROMPT_CACHING = os.environ.get("PROMPT_CACHING", "").strip().lower() in ("1", "true", "yes", "on")

# R1 (CSO, 5 Nov 2026): one fixed message for every failure. The specific diagnosis is
# the stable error_code; this text never describes the cause.
FAILURE_MESSAGE = ("No draft was released. Generation could not be completed. "
                   "Try again or complete the summary manually.")
RESULTS_ITEM_MAX_BYTES = 400_000
LONDON = ZoneInfo("Europe/London")   # generated_on; confirm on the Lambda runtime in item 7

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ddb = boto3.client("dynamodb", region_name=REGION)


def _bedrock_client(read_timeout: int):
    """One client per read timeout (ADR-009 Q3 (2)). total_max_attempts=1: botocore's
    max_attempts counts RETRIES, so max_attempts=1 would allow one hidden retry of a
    throttle or a read timeout (verified 5 Nov 2026). Retries are the step's job, for
    throttling only."""
    return boto3.client("bedrock-runtime", region_name=REGION, config=Config(
        connect_timeout=10, read_timeout=read_timeout,
        retries={"mode": "standard", "total_max_attempts": 1}))


_BEDROCK = {rt: _bedrock_client(rt) for rt in sorted(set(orchestrate.READ_TIMEOUT_S.values()))}


_TRACE_DDB = boto3.client("dynamodb", region_name=REGION, config=trace_store.CLIENT_CONFIG)


def _make_trace_writer():
    """The TraceTable writer (checklist item 6). Without TRACE_TABLE_NAME there is no trace
    store, and a generation without a trace may not be released (decision 6, 5 Nov 2026 —
    Shina, author/CSO), so the worker fails closed with config_error."""
    if not TRACE_TABLE_NAME:
        return None
    return trace_store.TraceStore(_TRACE_DDB, TRACE_TABLE_NAME)


# --- small helpers ---------------------------------------------------------------------
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _opt_number(n):
    return {"NULL": True} if n is None else {"N": str(int(n))}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_epoch(started_at):
    try:
        dt = datetime.fromisoformat(started_at)
        if dt.tzinfo is None:
            return None
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _generated_on(started_at, wall0: float) -> str:
    """The London date of the job's start (the dispatcher's started_at), as the harness
    dates its records. 8a and 8b receive the same value; a different one is clinical_stale."""
    t = _parse_epoch(started_at)
    return datetime.fromtimestamp(t if t is not None else wall0, LONDON).date().isoformat()


def _deadline(mono0: float, wall0: float, started_at, context) -> float:
    budget = float(orchestrate.DEADLINE_S)
    t = _parse_epoch(started_at)
    if t is not None:
        budget = min(budget, t + orchestrate.DEADLINE_S - wall0)
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if callable(remaining):
        budget = min(budget, remaining() / 1000.0 - orchestrate.LAMBDA_RESERVE_S)
    return mono0 + budget


def _key(user_sub: str, job_id: str) -> dict:
    return {"PK": {"S": f"USER#{user_sub}"}, "SK": {"S": f"GEN#{job_id}"}}


def _error_code(exc) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "Unknown")
    return type(exc).__name__


# --- event shapes ----------------------------------------------------------------------
def _is_http_api_event(event) -> bool:
    if not isinstance(event, dict):
        return False
    rc = event.get("requestContext") or {}
    return isinstance(rc, dict) and isinstance(rc.get("http"), dict)


def _is_dispatcher_event(event) -> bool:
    return (isinstance(event, dict)
            and isinstance(event.get("job_id"), str)
            and isinstance(event.get("user_sub"), str)
            and isinstance(event.get("notes"), str)
            and not _is_http_api_event(event))


def _http_response(status: int, body: dict) -> dict:
    return {"statusCode": status,
            "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(body)}


def lambda_handler(event, context):
    if _is_http_api_event(event):
        return _http_response(410, {
            "ok": False, "error": "deprecated_path",
            "message": ("POST /generate now routes to the dispatcher (slice 4b). "
                        "If you are seeing this from API Gateway, the route is "
                        "misconfigured: it should target DispatcherFunction."),
        })
    if _is_dispatcher_event(event):
        return _run_async_worker(event, context)
    logger.warning(json.dumps({"event": "worker_unsupported_event"}))
    return {"ok": False, "error": "unsupported_event"}


# --- the audit row -----------------------------------------------------------------------
def _claim(user_sub: str, job_id: str, pv: str) -> str:
    """'claimed' | 'not_claimed' (a duplicate, or the row is no longer pending) |
    'uncertain' (the outcome of the write is unknown: stop, do not clear, do not take over)."""
    try:
        _ddb.update_item(
            TableName=TABLE_NAME, Key=_key(user_sub, job_id),
            UpdateExpression="SET pipeline_version = :pv",
            ConditionExpression="#st = :pending AND attribute_not_exists(pipeline_version)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":pv": {"S": pv}, ":pending": {"S": "pending"}},
        )
        return "claimed"
    except ClientError as exc:
        if _error_code(exc) == "ConditionalCheckFailedException":
            return "not_claimed"
        return "uncertain"
    except BotoCoreError:
        return "uncertain"


def _mark_failed(user_sub: str, job_id: str, f: "orchestrate.Failure") -> str:
    """Flip pending -> failed with a stable code and the fixed message. Called only by the
    confirmed claimant. 'failed' | 'already_terminal' | 'audit_update_error'."""
    expr = ("SET #st = :failed, error_code = :ec, error_message = :em, failed_at = :now, "
            "failed_step = :fs, model_calls = :mc, input_tokens = :it, output_tokens = :ot")
    values = {
        ":failed": {"S": "failed"}, ":ec": {"S": f.code}, ":em": {"S": FAILURE_MESSAGE},
        ":now": {"S": _now_iso()}, ":fs": {"S": f.step}, ":mc": {"N": str(f.model_calls)},
        ":it": _opt_number(f.input_tokens), ":ot": _opt_number(f.output_tokens),
        ":pending": {"S": "pending"},
    }
    if f.safety_net_gate == "fail":            # only for safety_net_gate_failed
        expr += ", safety_net_gate = :sng"
        values[":sng"] = {"S": "fail"}
    try:
        _ddb.update_item(TableName=TABLE_NAME, Key=_key(user_sub, job_id), UpdateExpression=expr,
                         ExpressionAttributeNames={"#st": "status"},
                         ExpressionAttributeValues=values, ConditionExpression="#st = :pending")
        return "failed"
    except ClientError as exc:
        if _error_code(exc) == "ConditionalCheckFailedException":
            return "already_terminal"
        logger.error(json.dumps({"event": "mark_failed_error", "job_id": job_id,
                                 "aws_code": _error_code(exc)}))
        return "audit_update_error"
    except BotoCoreError as exc:
        logger.error(json.dumps({"event": "mark_failed_error", "job_id": job_id,
                                 "aws_code": _error_code(exc)}))
        return "audit_update_error"


def _flip_complete(user_sub: str, job_id: str, outcome: "orchestrate.Outcome") -> str:
    """'complete' | 'already_terminal' | 'audit_update_error'. output_sha256 = 8b's hashes
    under the keys the status endpoint and SPA already use."""
    h = outcome.bundle["hashes"]
    hashes = {"summary": h["part_a"], "gp_letter": h["part_b"], "patient": h["part_c"]}
    try:
        _ddb.update_item(
            TableName=TABLE_NAME, Key=_key(user_sub, job_id),
            UpdateExpression=("SET #st = :complete, completed_at = :now, model_version = :mv, "
                              "output_sha256 = :hashes, parse_ok = :pok, input_tokens = :it, "
                              "output_tokens = :ot, model_calls = :mc, safety_net_gate = :sng"),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":complete": {"S": "complete"}, ":now": {"S": _now_iso()},
                ":mv": {"S": f"{MODEL_ID} ({REGION}, on-demand)"},
                ":hashes": {"M": {k: {"S": v} for k, v in hashes.items()}},
                ":pok": {"BOOL": True},   # ADR-009: never false on a delivered pipeline generation
                ":it": _opt_number(outcome.input_tokens), ":ot": _opt_number(outcome.output_tokens),
                ":mc": {"N": str(outcome.model_calls)}, ":sng": {"S": "pass"},
                ":pending": {"S": "pending"},
            },
            ConditionExpression="#st = :pending",
        )
        return "complete"
    except ClientError as exc:
        if _error_code(exc) == "ConditionalCheckFailedException":
            return "already_terminal"
        logger.error(json.dumps({"event": "audit_flip_error", "job_id": job_id,
                                 "aws_code": _error_code(exc)}))
        return "audit_update_error"
    except BotoCoreError as exc:
        logger.error(json.dumps({"event": "audit_flip_error", "job_id": job_id,
                                 "aws_code": _error_code(exc)}))
        return "audit_update_error"


# --- ResultsTable ------------------------------------------------------------------------
def _results_item(user_sub: str, job_id: str, outcome: "orchestrate.Outcome") -> dict:
    b, s = outcome.bundle, outcome.stored
    return {
        "PK": {"S": f"USER#{user_sub}"}, "SK": {"S": f"RES#{job_id}"},
        "user_sub": {"S": user_sub}, "job_id": {"S": job_id},
        # The three documents under the names the status endpoint already serves.
        "summary": {"S": b["part_a"]}, "gp_letter": {"S": b["part_b"]}, "patient": {"S": b["part_c"]},
        # ADR-009 (c), for W10 - never served by the status endpoint (it reads the three above).
        "units_json": {"S": json.dumps(s["units"], ensure_ascii=False)},
        "templates_json": {"S": json.dumps(s["templates"], ensure_ascii=False)},
        "warnings_json": {"S": json.dumps(s["warnings"], ensure_ascii=False)},
        "output_sha256_json": {"S": json.dumps(s["hashes"])},   # W10: a changed document resets review
        "pipeline_version": {"S": outcome.pipeline_version},
        "generated_on": {"S": outcome.generated_on},
        "completed_at": {"S": _now_iso()},
        "ttl": {"N": str(int(time.time()) + RESULTS_TTL_HOURS * 3600)},
    }


def _write_results(user_sub: str, job_id: str, outcome: "orchestrate.Outcome") -> str:
    """'written' | 'results_too_large' | 'results_exists' | 'results_write_error'.
    Conditional: a result item is created once and never overwritten."""
    item = _results_item(user_sub, job_id, outcome)
    size = sum(len(k.encode()) + len(json.dumps(v).encode()) for k, v in item.items())
    if size > RESULTS_ITEM_MAX_BYTES:
        return "results_too_large"
    try:
        _ddb.put_item(TableName=RESULTS_TABLE_NAME, Item=item,
                      ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)")
        return "written"
    except ClientError as exc:
        if _error_code(exc) == "ConditionalCheckFailedException":
            return "results_exists"
        logger.error(json.dumps({"event": "results_write_error", "job_id": job_id,
                                 "aws_code": _error_code(exc)}))
        return "results_write_error"
    except BotoCoreError as exc:
        logger.error(json.dumps({"event": "results_write_error", "job_id": job_id,
                                 "aws_code": _error_code(exc)}))
        return "results_write_error"


def _deliver(writer, job_id: str, results: str, audit: str, started_epoch: float) -> None:
    """Best effort: the DELIVERY trace item (META is the pipeline outcome). Its failure does
    not undo a release. `ttl` from the run's own start epoch, like every other item."""
    if writer is None:
        return
    try:
        writer.put({"PK": f"GEN#{job_id}", "SK": "DELIVERY", "outcome_of": "delivery",
                    "results": results, "audit": audit, "at": _now_iso(),
                    "ttl": orchestrate.trace_ttl(started_epoch)})
    except Exception:  # noqa: BLE001
        logger.error(json.dumps({"event": "delivery_trace_error", "job_id": job_id}))


def _worker_failure(code: str) -> "orchestrate.Failure":
    return orchestrate.Failure(step="worker", code=code, model_calls=0, input_tokens=0,
                               output_tokens=0, safety_net_gate=None)


# --- (1) the dispatcher path -------------------------------------------------------------
def _run_async_worker(event, context):
    mono0, wall0 = time.monotonic(), time.time()
    job_id, user_sub, notes = event["job_id"], event["user_sub"], event["notes"]

    # a. claim - only the confirmed claimant may call Bedrock, mark failure or write results
    pv = orchestrate.pipeline_version()
    claim = _claim(user_sub, job_id, pv)
    if claim != "claimed":
        logger.info(json.dumps({"event": f"worker_{claim}", "job_id": job_id}))
        return {"ok": claim == "not_claimed", "job_id": job_id, "status": claim}

    # Input provenance (Codex read-only code inspection, 5 Nov 2026 19:36): the notes must be the notes the
    # dispatcher hashed into the audit row's input_sha256, or the record would describe a
    # different input. A missing hash cannot be verified and is treated as a mismatch.
    if event.get("input_sha256") != _sha256(notes):
        audit = _mark_failed(user_sub, job_id, _worker_failure("input_hash_mismatch"))
        logger.error(json.dumps({"event": "input_hash_mismatch", "job_id": job_id}))
        return {"ok": False, "job_id": job_id, "status": "failed",
                "error_code": "input_hash_mismatch", "audit": audit}

    writer = _make_trace_writer()
    if writer is None or not RESULTS_TABLE_NAME:
        audit = _mark_failed(user_sub, job_id, _worker_failure("config_error"))
        logger.error(json.dumps({"event": "worker_config_error", "job_id": job_id}))
        return {"ok": False, "job_id": job_id, "status": "failed", "error_code": "config_error",
                "audit": audit}

    # b. the pipeline
    generated_on = _generated_on(event.get("started_at"), wall0)
    deadline = _deadline(mono0, wall0, event.get("started_at"), context)
    marked: dict = {}

    def on_failure(f):
        marked["audit"] = _mark_failed(user_sub, job_id, f)

    outcome = orchestrate.run(notes, generation_id=job_id, clients=_BEDROCK, model_id=MODEL_ID,
                              prompt_caching=PROMPT_CACHING, trace=writer, deadline=deadline,
                              generated_on=generated_on, on_failure=on_failure)
    if not outcome.ok:
        _deliver(writer, job_id, "not_written", marked.get("audit", "not_marked"), outcome.started_epoch)
        logger.info(json.dumps({"event": "worker_failed", "job_id": job_id,
                                "failed_step": outcome.failure.step, "error_code": outcome.failure.code,
                                "model_calls": outcome.model_calls}))
        return {"ok": False, "job_id": job_id, "status": "failed",
                "error_code": outcome.failure.code}

    # c. release: results (conditional), then the audit flip
    res = _write_results(user_sub, job_id, outcome)
    if res != "written":
        f = orchestrate.Failure(step="results", code=res, model_calls=outcome.model_calls,
                                input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
                                safety_net_gate=None)
        audit = _mark_failed(user_sub, job_id, f)
        _deliver(writer, job_id, res, audit, outcome.started_epoch)
        return {"ok": False, "job_id": job_id, "status": "failed", "error_code": res}

    audit = _flip_complete(user_sub, job_id, outcome)
    _deliver(writer, job_id, "written", audit, outcome.started_epoch)
    logger.info(json.dumps({"event": "worker_complete" if audit == "complete" else "worker_flip_" + audit,
                            "job_id": job_id, "model_calls": outcome.model_calls,
                            "latency_ms": int((time.monotonic() - mono0) * 1000)}))
    return {"ok": audit == "complete", "job_id": job_id, "status": audit}
