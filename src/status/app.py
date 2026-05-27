"""
AI Discharge Summary Assistant - status Lambda (Phase 2, slice 4b).

Backs `GET /generations/{job_id}` on the HTTP API. The client polls this after
receiving 202 from the dispatcher, until status flips to `complete` (returns
the outputs) or `failed` (returns an error_code; never PHI).

Anti-spoof is enforced by the schema, not by a separate check: every row's
PK embeds `user_sub`, so a GetItem against
    PK=USER#<jwt sub>, SK=GEN#<job_id>
naturally returns nothing if the job belongs to another user. We surface that
as a 404 - the same response another user would see if the job did not exist -
which is the correct posture for not leaking the existence of other users'
work. (cf. Cognito's PreventUserExistenceErrors.)

The execution role (see infra/template.yaml) is scoped to exactly:
  - dynamodb:GetItem on the audit table (status row) and the results table,
  - kms:Decrypt on the CMK via DynamoDB only (kms:ViaService),
  - write to this function's own log group.
There is deliberately NO PutItem / UpdateItem / DeleteItem on either table -
the status endpoint is strictly read-only.
"""

import json
import logging
import os
import re
import time

import boto3
from botocore.exceptions import ClientError

# --- configuration --------------------------------------------------------
REGION = os.environ.get("AWS_REGION", "eu-west-2")
AUDIT_TABLE_NAME = os.environ["AUDIT_TABLE_NAME"]
RESULTS_TABLE_NAME = os.environ["RESULTS_TABLE_NAME"]

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ddb = boto3.client("dynamodb", region_name=REGION)

# ULIDs are 26 chars of Crockford base32 (excludes I, L, O, U). A tight regex
# saves us from passing arbitrary path values into a GetItem - defensive
# (DynamoDB would reject them anyway, but failing fast is friendlier).
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _http_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store",
        },
        "body": json.dumps(body),
    }


def lambda_handler(event, context):
    """GET /generations/{id} - JWT-gated, scoped to the caller's own jobs."""
    started = time.time()

    rc = event.get("requestContext") or {}
    if not isinstance(rc.get("http"), dict):
        return _http_response(401, {"ok": False, "error": "unauthenticated",
                                      "message": "status endpoint requires HTTP API + JWT context"})

    claims = (rc.get("authorizer") or {}).get("jwt", {}).get("claims", {})
    user_sub = (claims.get("sub") or "").strip()
    if not user_sub:
        return _http_response(401, {"ok": False, "error": "unauthenticated",
                                      "message": "no verified sub claim on request"})

    path_params = event.get("pathParameters") or {}
    job_id = (path_params.get("id") or "").strip()

    # Defensive: reject anything that isn't a ULID before hitting DDB.
    if not _ULID_RE.match(job_id):
        return _not_found_response(job_id)

    # --- 1) Read the GEN# row -------------------------------------------------
    try:
        job_resp = _ddb.get_item(
            TableName=AUDIT_TABLE_NAME,
            Key={"PK": {"S": f"USER#{user_sub}"},
                 "SK": {"S": f"GEN#{job_id}"}},
            # Strong read - the client is polling for a state change, weak
            # consistency would extend tail latency in the "just flipped to
            # complete" window.
            ConsistentRead=True,
        )
    except ClientError as exc:
        logger.error("status_audit_get_failed: %s", exc.response.get("Error", {}))
        return _http_response(500, {"ok": False, "error": "lookup_failed",
                                      "message": exc.response.get("Error", {}).get("Code", "Unknown")})

    job_item = job_resp.get("Item")
    if not job_item:
        # Either the job doesn't exist OR it exists for a different user_sub.
        # Same response either way - we don't confirm/deny existence.
        return _not_found_response(job_id)

    status = (job_item.get("status") or {}).get("S", "pending")
    response = {
        "ok": True,
        "job_id": job_id,
        "status": status,
        "started_at": (job_item.get("started_at") or {}).get("S"),
        "draft": (job_item.get("draft") or {}).get("BOOL", True),
        "model_version": (job_item.get("model_version") or {}).get("S"),
        "input_sha256": (job_item.get("input_sha256") or {}).get("S"),
        "parse_ok": (job_item.get("parse_ok") or {}).get("BOOL"),
    }

    if status == "complete":
        # Pull outputs from the transient ResultsTable. The outputs are NOT in
        # the audit table by design (ADR-002 keeps audit hash-only); they live
        # in a TTL'd delivery buffer.
        outputs = _read_outputs(user_sub, job_id)
        if outputs is None:
            # The audit row says complete but the results row is gone (TTL
            # expired) or never written. Surface this as a distinct state so
            # the UI can render "this draft has expired - please regenerate".
            response["status"] = "expired"
            response["error"] = "outputs_unavailable"
        else:
            response["outputs"] = outputs
        response["output_sha256"] = _output_hashes(job_item)
        response["completed_at"] = (job_item.get("completed_at") or {}).get("S")

    elif status == "failed":
        response["error_code"] = (job_item.get("error_code") or {}).get("S")
        # error_message stays an AWS error code or our own short string -
        # never the model output, never the notes (PHI-free guarantee).
        response["error_message"] = (job_item.get("error_message") or {}).get("S")
        response["failed_at"] = (job_item.get("failed_at") or {}).get("S")

    logger.info(json.dumps({
        "event": "status_check",
        "job_id": job_id,
        "user_sub": user_sub,
        "status": status,
        "latency_ms": int((time.time() - started) * 1000),
    }))
    return _http_response(200, response)


def _read_outputs(user_sub: str, job_id: str):
    """Fetch the three outputs from the transient ResultsTable.

    Returns the {summary, gp_letter, patient} dict, or None if the row is
    missing (e.g. TTL has cleaned it up after 24h).
    """
    try:
        resp = _ddb.get_item(
            TableName=RESULTS_TABLE_NAME,
            Key={"PK": {"S": f"USER#{user_sub}"},
                 "SK": {"S": f"RES#{job_id}"}},
            ConsistentRead=True,
        )
    except ClientError as exc:
        logger.error("status_results_get_failed: %s", exc.response.get("Error", {}))
        return None
    item = resp.get("Item")
    if not item:
        return None
    return {
        "summary":   (item.get("summary")   or {}).get("S", ""),
        "gp_letter": (item.get("gp_letter") or {}).get("S", ""),
        "patient":   (item.get("patient")   or {}).get("S", ""),
    }


def _output_hashes(job_item: dict):
    """Pull the per-output hash map off the GEN# row. DynamoDB serialises map
    attributes as {'M': {key: {'S': hash}, ...}}."""
    raw = (job_item.get("output_sha256") or {}).get("M") or {}
    return {k: (v or {}).get("S", "") for k, v in raw.items()}


def _not_found_response(job_id: str):
    """Uniform 404 for the unknown-job case AND the cross-user case."""
    return _http_response(404, {
        "ok": False,
        "error": "not_found",
        "job_id": job_id,
        "message": "no such generation for this user",
    })
