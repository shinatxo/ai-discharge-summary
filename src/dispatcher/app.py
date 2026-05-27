"""
AI Discharge Summary Assistant - dispatcher Lambda (Phase 2, slice 4b).

This is the front-of-the-line of the ASYNC PATTERN that replaces slice 4a's
synchronous `POST /generate -> Bedrock -> 200` path. The slice 4a smoke test on
2026-05-26 surfaced two flaws in the sync shape (both recorded in
docs/ADR-phase1.md sec ADR-004 "Slice 4a findings"):

  1. API Gateway HTTP API has a HARD 30s integration timeout that cannot be
     raised. Bedrock Sonnet at MAX_TOKENS=4096 runs ~20-25s warm and longer on
     cold start, brushing or breaching the cap on a fresh container with a
     longer ward-round note. The client gets 503 while Lambda is still mid-call.
  2. Lambda completes server-side even after API Gateway has cut the client
     off, leaving a "ghost" audit row (and ledger object) behind. A naive
     client retry doubles the Bedrock invocation.

Slice 4b's answer is the textbook async pattern:

   client  ---POST /generate (with Idempotency-Key header)--->  THIS Lambda
                                                                    |
                                                                    | (1) write pending job row
                                                                    | (2) lambda.invoke worker (Event)
                                                                    | (3) return 202 + {job_id}
                                                                    v
                                                            -------- 202 returned --------
                                                                    |
                                                                    | (worker runs async,
                                                                    |  flips status to
                                                                    |  complete or failed
                                                                    |  + writes outputs to
                                                                    |  ResultsTable)
                                                                    |
   client  ---GET /generations/{id}--->  StatusFunction  reads job row + (if complete) outputs


Anti-spoof: `user_sub` is taken from the JWT claims that API Gateway's authoriser
verified BEFORE invoking this function, never from the request body.

Idempotency (Stripe-style): an optional client-supplied `Idempotency-Key` header
holds a UUID; a TransactWriteItems pairs (IDEM# row, GEN# pending row) into one
atomic write, so a retried POST with the same key returns the SAME job_id - the
ghost-record retry hazard from slice 4a is closed at this layer.

The execution role (see infra/template.yaml) is scoped to exactly:
  - dynamodb:PutItem / GetItem on the audit table (TransactWriteItems +
    idempotency-hit lookup) - no Delete, no Scan,
  - lambda:InvokeFunction on the ONE worker (the generate Lambda) - no other,
  - kms:Decrypt/GenerateDataKey on the CMK via DynamoDB only (kms:ViaService),
  - write to this function's own log group.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# --- configuration (all injected by the template; no hard-coded resource ids) -
REGION = os.environ.get("AWS_REGION", "eu-west-2")
AUDIT_TABLE_NAME = os.environ["AUDIT_TABLE_NAME"]
WORKER_FUNCTION_NAME = os.environ["WORKER_FUNCTION_NAME"]
# How long the idempotency receipt lives. The receipt is just a pointer
# (idempotency_key -> job_id); the GEN# job row itself never expires.
IDEM_TTL_HOURS = int(os.environ.get("IDEM_TTL_HOURS", "24"))
SCHEMA_VERSION = int(os.environ.get("SCHEMA_VERSION", "1"))

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Clients reused across warm invocations (created at import time).
_ddb = boto3.client("dynamodb", region_name=REGION)
_lambda = boto3.client("lambda", region_name=REGION)


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # excludes I, L, O, U


def _ulid() -> str:
    """A dependency-free ULID: 48-bit ms timestamp + 80 bits randomness, in
    Crockford base32. Lexicographically sortable, so SK=GEN#<ulid> gives the
    audit table natural time-ordering for per-clinician history queries (no
    need to keep a separate timestamp in the SK)."""
    ms = int(time.time() * 1000)
    rand = secrets.randbits(80)
    value = (ms << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# UUIDs (any version) only - rejects anything funny in the Idempotency-Key.
# We accept lowercase canonical with hyphens (Amplify/Stripe/etc. default).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _is_valid_idempotency_key(key: str) -> bool:
    if not isinstance(key, str):
        return False
    return bool(_UUID_RE.match(key.strip().lower()))


def _bad_request(message: str):
    return {"ok": False, "error": "bad_request", "message": message}


def _http_response(status: int, body: dict) -> dict:
    """Wrap a JSON body in the {statusCode, headers, body} shape API Gateway
    HTTP API v2 needs when PayloadFormatVersion is 2.0."""
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store",
        },
        "body": json.dumps(body),
    }


def _get_header(headers: dict, name: str) -> str:
    """HTTP API v2 lower-cases header keys, but be tolerant just in case."""
    if not isinstance(headers, dict):
        return ""
    name_l = name.lower()
    for k, v in headers.items():
        if isinstance(k, str) and k.lower() == name_l:
            return v or ""
    return ""


# -----------------------------------------------------------------------------
# Handler
# -----------------------------------------------------------------------------
def lambda_handler(event, context):
    """Receives the authenticated POST /generate, writes a pending job row,
    fires the worker, and returns 202.

    Contract with the worker (lambda.invoke InvocationType=Event):
        {
          "job_id":       "<ulid>",
          "user_sub":     "<verified cognito sub>",
          "notes":        "<free-text ward-round notes>",
          "input_sha256": "<hex>",       # already computed here; saves the worker recomputing
          "started_at":   "<iso8601 utc>"
        }
    """
    started = time.time()

    # API Gateway HTTP API v2 always sets event['requestContext']['http'].
    # If this handler is invoked any other way (a future EventBridge rule, a
    # console "Test" button) we want to fail closed rather than process a
    # body whose identity we cannot verify.
    rc = event.get("requestContext") or {}
    if not isinstance(rc.get("http"), dict):
        # No HTTP context -> no JWT context -> no trusted user_sub. Refuse.
        return _http_response(401, {"ok": False, "error": "unauthenticated",
                                     "message": "dispatcher requires HTTP API + JWT context"})

    claims = (rc.get("authorizer") or {}).get("jwt", {}).get("claims", {})
    user_sub = (claims.get("sub") or "").strip()
    if not user_sub:
        return _http_response(401, {"ok": False, "error": "unauthenticated",
                                     "message": "no verified sub claim on request"})

    # Parse body (may arrive as a string or a pre-parsed dict from test harnesses).
    body_raw = event.get("body")
    if isinstance(body_raw, str):
        try:
            body_obj = json.loads(body_raw) if body_raw else {}
        except json.JSONDecodeError:
            return _http_response(400, _bad_request("body was a string but not valid JSON"))
    elif isinstance(body_raw, dict):
        body_obj = body_raw
    else:
        body_obj = {}

    notes = (body_obj.get("notes") or "").strip()
    output_type = (body_obj.get("output_type") or "summary,gp_letter,patient").strip()

    if not notes:
        return _http_response(400, _bad_request("'notes' is required and must be non-empty"))

    # Defensive ceiling on the notes payload - keeps a typo'd 50MB paste from
    # turning into a runaway Bedrock bill. (Slice 4b: documented size limit.)
    if len(notes) > 50_000:
        return _http_response(413, {"ok": False, "error": "payload_too_large",
                                      "message": "'notes' exceeds 50000-char ceiling"})

    # Idempotency-Key is optional. If present it must be a UUID; if absent we
    # generate a per-request UUID purely to give the pending row a stable
    # client-visible key even though no idempotency check happens.
    headers = event.get("headers") or {}
    raw_key = _get_header(headers, "idempotency-key").strip()
    if raw_key:
        if not _is_valid_idempotency_key(raw_key):
            return _http_response(400, _bad_request(
                "Idempotency-Key must be a canonical UUID (8-4-4-4-12 hex)"))
        idempotency_key = raw_key.lower()
        enforce_idempotency = True
    else:
        idempotency_key = str(uuid.uuid4())
        enforce_idempotency = False

    input_hash = _sha256(notes)

    # Fast path for idempotency replay: if a record already exists, return it
    # without trying the transaction (saves a write). When the client retries
    # quickly this is the common case.
    if enforce_idempotency:
        existing = _lookup_idem(user_sub, idempotency_key)
        if existing is not None:
            return _idempotency_replay_response(user_sub, existing, started)

    job_id = _ulid()
    now = datetime.now(timezone.utc).isoformat()
    ttl_epoch = int(time.time()) + IDEM_TTL_HOURS * 3600

    # Atomic write: PENDING job row + idempotency receipt, in one transaction.
    # If a concurrent dispatch already wrote the IDEM# row (same key), the
    # ConditionCheck fails for THAT item and the transaction is rolled back -
    # we then look up the existing record and return it.
    try:
        _ddb.transact_write_items(TransactItems=[
            {
                "Put": {
                    "TableName": AUDIT_TABLE_NAME,
                    "Item": _pending_job_item(
                        user_sub=user_sub, job_id=job_id, now=now,
                        input_hash=input_hash, output_type=output_type,
                        idempotency_key=idempotency_key,
                    ),
                    # GEN# rows are write-once (slice 2 contract). A ulid collision
                    # is astronomically unlikely but we still guard against it.
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                },
            },
            {
                "Put": {
                    "TableName": AUDIT_TABLE_NAME,
                    "Item": _idem_receipt_item(
                        user_sub=user_sub, idempotency_key=idempotency_key,
                        job_id=job_id, now=now, ttl_epoch=ttl_epoch,
                        input_hash=input_hash,
                    ),
                    # If an IDEM# row with this PK+SK exists, this fails -> whole
                    # transaction rolls back -> we go to the replay path below.
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                },
            },
        ])
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        # TransactionCanceledException is the expected outcome on idempotency
        # collision. Anything else is a real failure we surface to the client.
        if code == "TransactionCanceledException" and enforce_idempotency:
            existing = _lookup_idem(user_sub, idempotency_key)
            if existing is not None:
                logger.info(json.dumps({
                    "event": "idempotency_replay",
                    "user_sub": user_sub,
                    "job_id": existing["job_id"],
                    "latency_ms": int((time.time() - started) * 1000),
                }))
                return _idempotency_replay_response(user_sub, existing, started)
            # If we can't find the existing record but a collision happened,
            # something is wrong - fall through to error.
        logger.error("dispatch_transact_failed: %s reasons=%s",
                     code, exc.response.get("CancellationReasons"))
        return _http_response(500, {"ok": False, "error": "dispatch_failed",
                                      "message": code or "Unknown"})

    # --- Fire the worker asynchronously --------------------------------------
    # InvocationType='Event' returns 202 from Lambda immediately and the worker
    # runs in its own invocation. We do NOT wait. If this invoke itself fails
    # (resource limits, throttling) we leave the GEN# row in PENDING - the
    # client will poll, see PENDING for longer than expected, and we can mark
    # it FAILED out of band. (We could add a retry-on-invoke-failure here but
    # it would mask real Lambda exhaustion; better to surface it.)
    try:
        _lambda.invoke(
            FunctionName=WORKER_FUNCTION_NAME,
            InvocationType="Event",        # async; returns 202 immediately
            Payload=json.dumps({
                "job_id": job_id,
                "user_sub": user_sub,
                "notes": notes,
                "input_sha256": input_hash,
                "started_at": now,
            }).encode("utf-8"),
        )
    except ClientError as exc:
        # Mark the row as failed so polling returns honest status. The IDEM#
        # row stays - a retry with the same key will replay this failure
        # rather than start a duplicate Bedrock run.
        logger.error("worker_invoke_failed: %s", exc.response.get("Error", {}))
        _mark_failed(user_sub, job_id, "worker_invoke_failed",
                     exc.response.get("Error", {}).get("Code", "Unknown"))
        return _http_response(503, {"ok": False, "error": "worker_invoke_failed",
                                      "job_id": job_id,
                                      "message": "could not start the generation worker"})

    logger.info(json.dumps({
        "event": "dispatch_accepted",
        "job_id": job_id,
        "user_sub": user_sub,
        "idempotency_key_present": enforce_idempotency,
        "input_sha256": input_hash,
        "latency_ms": int((time.time() - started) * 1000),
    }))

    return _http_response(202, {
        "ok": True,
        "job_id": job_id,
        "status": "pending",
        "poll_url": f"/generations/{job_id}",
        "input_sha256": input_hash,
        "started_at": now,
    })


# -----------------------------------------------------------------------------
# Item builders + small DB helpers
# -----------------------------------------------------------------------------
def _pending_job_item(*, user_sub, job_id, now, input_hash, output_type,
                       idempotency_key):
    """The GEN# row that holds the job's lifecycle. Worker UpdateItem-s this row
    to status=complete (with output_sha256, parse_ok, model_version, tokens) or
    status=failed (with error_code).

    Schema notes:
      - SK=GEN#<ulid> (NEW for slice 4b). ULIDs are time-sortable already, so we
        no longer prefix the SK with the timestamp - that lets the status Lambda
        do a direct GetItem PK=USER#<sub>, SK=GEN#<job_id>.
      - draft=True from creation; the review-flag transition flips it later
        (ADR-002's single allowed mutation).
      - schema_version bumped to 2 to mark the new SK shape.
    """
    return {
        "PK":              {"S": f"USER#{user_sub}"},
        "SK":              {"S": f"GEN#{job_id}"},
        "generation_id":   {"S": job_id},
        "user_sub":        {"S": user_sub},
        "idempotency_key": {"S": idempotency_key},
        "status":          {"S": "pending"},
        "draft":           {"BOOL": True},
        "reviewed_at":     {"NULL": True},
        "started_at":      {"S": now},
        "input_sha256":    {"S": input_hash},
        "output_type":     {"S": output_type},
        "request_region":  {"S": REGION},
        "inference_profile": {"S": "n/a (on-demand)"},
        "schema_version":  {"N": "2"},
    }


def _idem_receipt_item(*, user_sub, idempotency_key, job_id, now, ttl_epoch,
                        input_hash):
    """The IDEM# row that maps (user_sub, idempotency_key) -> job_id.

    Carries a TTL so the table doesn't accumulate idempotency receipts forever -
    24h is a comfortable replay window for browser/UI retries. The GEN# row it
    points to has NO TTL attribute, so it survives the receipt's expiry.

    We also record `input_sha256` here. We do NOT enforce that a retried POST
    with the same idempotency key carries the same body (Stripe doesn't either -
    it just returns the first response); we record it so operators can spot
    "different body, same key" misuse in the ledger if it ever matters.
    """
    return {
        "PK":              {"S": f"USER#{user_sub}"},
        "SK":              {"S": f"IDEM#{idempotency_key}"},
        "kind":            {"S": "idempotency_receipt"},
        "user_sub":        {"S": user_sub},
        "idempotency_key": {"S": idempotency_key},
        "generation_id":   {"S": job_id},
        "input_sha256":    {"S": input_hash},
        "created_at":      {"S": now},
        # DynamoDB TTL attribute (epoch seconds). Items past this epoch are
        # deleted by DynamoDB (best-effort, within ~48h of expiry per AWS docs).
        "ttl":             {"N": str(ttl_epoch)},
    }


def _lookup_idem(user_sub: str, idempotency_key: str):
    """Return {'job_id', 'status_at_replay'} or None.

    Reads the IDEM# row (cheap GetItem, single RCU), then the GEN# row to
    surface the current status. We always echo the CURRENT status on a replay,
    not the status at first dispatch - the client cares about state right now.
    """
    try:
        idem_resp = _ddb.get_item(
            TableName=AUDIT_TABLE_NAME,
            Key={"PK": {"S": f"USER#{user_sub}"},
                 "SK": {"S": f"IDEM#{idempotency_key}"}},
            ConsistentRead=True,
        )
    except ClientError as exc:
        logger.error("idem_lookup_failed: %s", exc.response.get("Error", {}))
        return None
    item = idem_resp.get("Item")
    if not item:
        return None
    job_id = item.get("generation_id", {}).get("S")
    if not job_id:
        return None
    # Surface the GEN# row's status so the client gets fresh state on replay.
    try:
        job_resp = _ddb.get_item(
            TableName=AUDIT_TABLE_NAME,
            Key={"PK": {"S": f"USER#{user_sub}"},
                 "SK": {"S": f"GEN#{job_id}"}},
            ConsistentRead=True,
        )
    except ClientError:
        return {"job_id": job_id, "status_at_replay": "pending"}
    job_item = job_resp.get("Item") or {}
    status = job_item.get("status", {}).get("S", "pending")
    return {
        "job_id": job_id,
        "status_at_replay": status,
        "started_at": (job_item.get("started_at") or {}).get("S"),
        "input_sha256": (job_item.get("input_sha256") or {}).get("S"),
    }


def _idempotency_replay_response(user_sub, existing, started):
    """200 (not 202) on replay - we are NOT starting a new job."""
    body = {
        "ok": True,
        "job_id": existing["job_id"],
        "status": existing.get("status_at_replay", "pending"),
        "idempotent_replay": True,
        "poll_url": f"/generations/{existing['job_id']}",
        "started_at": existing.get("started_at"),
        "input_sha256": existing.get("input_sha256"),
    }
    return _http_response(200, body)


def _mark_failed(user_sub: str, job_id: str, error_code: str, error_message: str):
    """Best-effort flip of the GEN# row to status=failed. We never store PHI -
    error_message is the AWS error code, not the model output."""
    try:
        _ddb.update_item(
            TableName=AUDIT_TABLE_NAME,
            Key={"PK": {"S": f"USER#{user_sub}"},
                 "SK": {"S": f"GEN#{job_id}"}},
            UpdateExpression=("SET #st = :failed, error_code = :ec, "
                              "error_message = :em, failed_at = :now"),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":failed": {"S": "failed"},
                ":ec":     {"S": error_code},
                ":em":     {"S": error_message},
                ":now":    {"S": datetime.now(timezone.utc).isoformat()},
                ":pending": {"S": "pending"},
            },
            # Only flip from pending -> failed; never overwrite a complete row.
            ConditionExpression="#st = :pending",
        )
    except ClientError as exc:
        # Already terminal, or row missing - log and move on.
        logger.error("mark_failed_skipped: %s", exc.response.get("Error", {}))
