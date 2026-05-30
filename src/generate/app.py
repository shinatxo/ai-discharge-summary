"""
AI Discharge Summary Assistant - generate Lambda (Phase 2, slice 4b).

This function is the BEDROCK WORKER. In slice 4a it sat directly behind the
HTTP API; slice 4b moves it behind the dispatcher (src/dispatcher/) so the
async `202 + poll` pattern can absorb Bedrock's ~20-25 s warm latency without
hitting API Gateway's hard 30 s integration cap. There are three invocation
contracts this handler supports, in priority order:

(1) DISPATCHER EVENT (the production hot path - slice 4b onwards).
    Invoked asynchronously by src/dispatcher/ via lambda.invoke
    (InvocationType='Event'). The dispatcher has ALREADY written the pending
    GEN# audit row, so this function:
       - runs Bedrock,
       - splits the combined output into PART A/B/C,
       - writes the three outputs to the transient ResultsTable
         (NOT the audit table - ADR-002 keeps the audit log hash-only),
       - UpdateItem-s the existing GEN# row to status=complete (with hashes +
         model_version + parse_ok + tokens) or status=failed (with error_code).
    Returns a small status dict, but the caller (Lambda async invoke pipeline)
    discards it; the client sees the result via GET /generations/{id}.

    Event shape:
        {
          "job_id":       "<ulid>",
          "user_sub":     "<verified cognito sub>",
          "notes":        "<free-text ward-round notes>",
          "input_sha256": "<hex>",       # already computed by dispatcher
          "started_at":   "<iso8601>"
        }

(2) DIRECT INVOKE (slice 2/3 smoke tests, console "Test" button - preserved).
    No job_id, no HTTP context. Worker writes its own audit row in the old
    schema (SK=GEN#<timestamp>#<ulid>) for backwards compatibility and
    returns outputs synchronously. ADR-002 invariants hold.

(3) HTTP API DIRECT (slice 4a's old hot path - kept fail-closed). If anyone
    ever wires API Gateway to this function again (which slice 4b unwired),
    we refuse with 410 Gone and a pointer to the dispatcher route. The reason
    is honesty: if a future operator re-attaches it, they should see the
    failure immediately, not a silent regression to the 30-s-timeout shape.

The execution role (see infra/template.yaml) is scoped to exactly:
  - dynamodb:PutItem / UpdateItem on the audit table (write row OR flip pending
    -> complete/failed; never Delete, never Scan),
  - dynamodb:PutItem on the results table (transient outputs),
  - kms:Decrypt / GenerateDataKey on the CMK via DynamoDB only (kms:ViaService),
  - bedrock:InvokeModel on the ONE pinned model,
  - write to this function's own log group.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# --- configuration (all injected by the template; no hard-coded resource ids) -
REGION = os.environ.get("AWS_REGION", "eu-west-2")
TABLE_NAME = os.environ["AUDIT_TABLE_NAME"]
# RESULTS_TABLE_NAME is new for slice 4b. Defaulting to empty keeps the unit
# tests on the legacy code path (direct-invoke) able to run without setting it.
RESULTS_TABLE_NAME = os.environ.get("RESULTS_TABLE_NAME", "")
MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
SCHEMA_VERSION = int(os.environ.get("SCHEMA_VERSION", "1"))
# Results TTL - matches the dispatcher's IDEM TTL by default. The transient
# outputs are auto-cleaned by DynamoDB TTL after this window; the GEN# audit
# row (hash-only) persists indefinitely.
RESULTS_TTL_HOURS = int(os.environ.get("RESULTS_TTL_HOURS", "24"))

# Deterministic, safety-first generation: temperature 0 so the model doesn't
# get "creative" with clinical facts. maxTokens sized for three full outputs.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4096"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0"))

# --- Patient v2: optional second-pass patient-version generation -------------
# When enabled, the patient version (PART C) is regenerated in a SEPARATE Bedrock
# call whose only input is the curated PART A summary - never the raw notes. This
# makes it structurally unable to reintroduce undocumented standard-of-care
# advice (the Run 4 / Ibrahim "helpful hallucination" failure mode), independent
# of the v0.6 prompt rule. See docs/PATIENT_V2_DESIGN.md. Off by default: flag
# off => today's single combined call, bit-identical. The second pass stays on
# the SAME pinned Sonnet model, on-demand in eu-west-2 (ADR-003 rule 1: UK-only
# inference; deliberately NOT a Haiku/EU-profile pass).
PATIENT_V2_SECOND_PASS = os.environ.get("PATIENT_V2_SECOND_PASS", "").strip().lower() in (
    "1", "true", "yes", "on",
)
# The leaflet alone is short, so the second pass is capped tighter than the
# combined call to keep the marginal token cost small.
PATIENT_MAX_TOKENS = int(os.environ.get("PATIENT_MAX_TOKENS", "1500"))

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Reuse clients across warm invocations.
_bedrock = boto3.client("bedrock-runtime", region_name=REGION)
_ddb = boto3.client("dynamodb", region_name=REGION)
_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)

# Load the system prompt once per container.
_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "system_prompt.md")


def _load_system_prompt() -> str:
    with open(_PROMPT_PATH, "r", encoding="utf-8") as fh:
        raw = fh.read()
    marker = "## SYSTEM PROMPT"
    idx = raw.find(marker)
    if idx == -1:
        raise RuntimeError("system_prompt.md is missing the '## SYSTEM PROMPT' marker")
    body = raw[idx:]
    body = body.split("\n", 1)[1] if "\n" in body else body
    return body.strip()


SYSTEM_PROMPT = _load_system_prompt()


_PATIENT_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "patient_system_prompt.md")


def _load_patient_prompt() -> str:
    """Load the dedicated patient-version (PART C) prompt for the v2 second pass.

    Returns "" if the file is absent so the worker can fall back to v1 cleanly
    rather than crash at import time. The body after the '## SYSTEM PROMPT'
    marker is used (same convention as the combined prompt)."""
    try:
        with open(_PATIENT_PROMPT_PATH, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return ""
    marker = "## SYSTEM PROMPT"
    idx = raw.find(marker)
    body = raw[idx:] if idx != -1 else raw
    body = body.split("\n", 1)[1] if "\n" in body else body
    return body.strip()


PATIENT_SYSTEM_PROMPT = _load_patient_prompt()


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
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


def _part_marker(label: str):
    return re.compile(rf"(?im)^[#*>\s]*PART\s+{label}\b.*$")


def _split_outputs(text: str):
    """Split the combined model output into {summary, gp_letter, patient}.

    Two parse paths both count as parse_ok=True:
      1. STRICT: all three markers present and ordered (A then B then C).
      2. FORGIVING: B and C present and ordered, A absent (model dove straight
         into the summary). Everything before B becomes the summary. Observed
         2026-05-26 on a short UTI case.
    Anything else: fail safe, return whole text under 'summary', parse_ok=False.
    """
    pos = {}
    for label in ("A", "B", "C"):
        m = _part_marker(label).search(text)
        if m:
            pos[label] = m.start()

    if {"A", "B", "C"} <= pos.keys() and pos["A"] < pos["B"] < pos["C"]:
        return ({
            "summary":   text[pos["A"]:pos["B"]].strip(),
            "gp_letter": text[pos["B"]:pos["C"]].strip(),
            "patient":   text[pos["C"]:].strip(),
        }, True)

    if "B" in pos and "C" in pos and pos["B"] < pos["C"] and pos["B"] > 0:
        return ({
            "summary":   text[:pos["B"]].strip(),
            "gp_letter": text[pos["B"]:pos["C"]].strip(),
            "patient":   text[pos["C"]:].strip(),
        }, True)

    return ({"summary": text.strip(), "gp_letter": "", "patient": ""}, False)


def _converse(system_prompt: str, user_text: str, max_tokens: int):
    """Single Bedrock Converse call on the pinned model. Centralised so the
    combined pass and the patient second pass share one code path / config."""
    return _bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": TEMPERATURE},
    )


_PART_C_HEADER = re.compile(r"(?im)^[#*>\s]*PART\s+C\b.*$")


def _strip_part_c_marker(text: str) -> str:
    """The second-pass prompt asks for the leaflet body only, but if the model
    still emits a 'PART C ...' header line, drop that one leading line so the
    stored patient version is clean."""
    m = _PART_C_HEADER.match(text.lstrip())
    if not m:
        return text.strip()
    after = text.lstrip()[m.end():]
    return after.strip()


def _maybe_second_pass(outputs: dict):
    """Patient v2: optionally regenerate the patient version from PART A alone.

    Returns (patient_version, patient_model_version, patient_parse_ok,
    patient_usage). Mutates ``outputs['patient']`` in place when the v2 pass
    runs successfully. Degrades gracefully — any failure (flag off, no prompt,
    empty summary, Bedrock error, empty result) leaves the v1 combined-pass
    patient text untouched and is reflected in the returned status:

      - "v1"          : second pass not attempted (flag off / no summary / no prompt)
      - "v2"          : second pass produced the leaflet (outputs mutated)
      - "v1_fallback" : second pass was attempted but failed; v1 text kept
    """
    base_mv = f"{MODEL_ID} ({REGION}, on-demand)"
    if not PATIENT_V2_SECOND_PASS:
        return ("v1", base_mv, True, {})
    if not PATIENT_SYSTEM_PROMPT:
        logger.warning(json.dumps({"event": "patient_v2_no_prompt"}))
        return ("v1", base_mv, True, {})

    summary = (outputs.get("summary") or "").strip()
    if not summary:
        # Parse failed upstream; there is no trustworthy PART A to anchor to.
        logger.info(json.dumps({"event": "patient_v2_skipped_no_summary"}))
        return ("v1", base_mv, True, {})

    try:
        resp = _converse(PATIENT_SYSTEM_PROMPT, summary, PATIENT_MAX_TOKENS)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error("patient_v2_bedrock_failed: %s", code)
        return ("v1_fallback", base_mv, False, {})

    text = _strip_part_c_marker(resp["output"]["message"]["content"][0]["text"])
    if not text:
        logger.warning(json.dumps({"event": "patient_v2_empty_result"}))
        return ("v1_fallback", base_mv, False, {})

    outputs["patient"] = text
    logger.info(json.dumps({"event": "patient_v2_applied"}))
    return ("v2", base_mv, True, resp.get("usage", {}))


def _bad_request(message: str):
    return {"ok": False, "error": "bad_request", "message": message}


def _is_http_api_event(event) -> bool:
    if not isinstance(event, dict):
        return False
    rc = event.get("requestContext") or {}
    return isinstance(rc, dict) and isinstance(rc.get("http"), dict)


def _is_dispatcher_event(event) -> bool:
    """The new (slice 4b) async worker event shape: created by the dispatcher,
    delivered via lambda.invoke(InvocationType='Event'). Distinguished from
    the direct-invoke shape by the presence of `job_id`."""
    return (
        isinstance(event, dict)
        and isinstance(event.get("job_id"), str)
        and isinstance(event.get("user_sub"), str)
        and isinstance(event.get("notes"), str)
        and not _is_http_api_event(event)
    )


def _http_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body),
    }


# -----------------------------------------------------------------------------
# Handler
# -----------------------------------------------------------------------------
def lambda_handler(event, context):
    """Multiplex on event shape - dispatcher (slice 4b) vs direct invoke
    (slice 2/3 smoke tests) vs accidentally re-attached HTTP API (refuse)."""
    if _is_http_api_event(event):
        # Slice 4b moved the HTTP API to the dispatcher. Fail loudly rather
        # than silently regressing to the 30 s timeout shape.
        return _http_response(410, {
            "ok": False, "error": "deprecated_path",
            "message": ("POST /generate now routes to the dispatcher (slice 4b). "
                        "If you are seeing this from API Gateway, the route is "
                        "misconfigured: it should target DispatcherFunction."),
        })

    if _is_dispatcher_event(event):
        return _run_async_worker(event)

    # Otherwise: legacy direct-invoke. Preserve the slice 2/3 contract.
    return _run_direct_invoke(event)


# -----------------------------------------------------------------------------
# (1) Slice 4b async worker - the production hot path
# -----------------------------------------------------------------------------
def _run_async_worker(event):
    """Update the pending GEN# row to complete / failed, and write outputs to
    the transient ResultsTable."""
    started = time.time()
    job_id = event["job_id"]
    user_sub = event["user_sub"]
    notes = event["notes"]
    input_hash_dispatched = event.get("input_sha256")

    # Recompute the input hash and compare with the dispatcher's value.
    # Mismatch implies the dispatcher and worker disagree about the payload -
    # the audit log should record the value WE actually generated against.
    input_hash = _sha256(notes)
    if input_hash_dispatched and input_hash_dispatched != input_hash:
        logger.warning(json.dumps({
            "event": "input_hash_mismatch",
            "job_id": job_id, "user_sub": user_sub,
            "dispatched": input_hash_dispatched, "worker": input_hash,
        }))

    # --- 1) Bedrock Converse on the pinned model -----------------------------
    try:
        response = _converse(SYSTEM_PROMPT, notes, MAX_TOKENS)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        # PHI-free log: error class only, never the notes.
        logger.error("worker_bedrock_failed: %s job=%s", code, job_id)
        _mark_failed(user_sub, job_id, "bedrock_error", code)
        return {"ok": False, "job_id": job_id, "status": "failed",
                "error_code": "bedrock_error", "error_message": code}

    model_text = response["output"]["message"]["content"][0]["text"]
    usage = response.get("usage", {})
    outputs, parse_ok = _split_outputs(model_text)

    # --- 1b) Patient v2 (optional): regenerate PART C from PART A alone -------
    # No-op unless PATIENT_V2_SECOND_PASS is enabled. Mutates outputs['patient']
    # in place on success; never fails the job (graceful fallback to v1 text).
    patient_version, patient_mv, patient_parse_ok, patient_usage = _maybe_second_pass(outputs)

    # --- 2) Hashes (the only representation we keep in the audit log) --------
    output_hashes = {k: _sha256(v) for k, v in outputs.items()}

    # --- 3) Write outputs to the transient ResultsTable (NOT audit) ----------
    # If RESULTS_TABLE_NAME is empty (misconfig) we still flip status=failed
    # rather than leave the row pending forever.
    if not RESULTS_TABLE_NAME:
        _mark_failed(user_sub, job_id, "config_error", "RESULTS_TABLE_NAME unset")
        return {"ok": False, "job_id": job_id, "status": "failed",
                "error_code": "config_error", "error_message": "RESULTS_TABLE_NAME unset"}

    try:
        _ddb.put_item(
            TableName=RESULTS_TABLE_NAME,
            Item={
                "PK":        {"S": f"USER#{user_sub}"},
                "SK":        {"S": f"RES#{job_id}"},
                "user_sub":  {"S": user_sub},
                "job_id":    {"S": job_id},
                "summary":   {"S": outputs["summary"]},
                "gp_letter": {"S": outputs["gp_letter"]},
                "patient":   {"S": outputs["patient"]},
                "completed_at": {"S": datetime.now(timezone.utc).isoformat()},
                # TTL for auto-cleanup of transient outputs. The audit row
                # (hash-only) outlives this and remains the system of record.
                "ttl":       {"N": str(int(time.time()) + RESULTS_TTL_HOURS * 3600)},
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error("worker_results_put_failed: %s job=%s", code, job_id)
        _mark_failed(user_sub, job_id, "results_write_error", code)
        return {"ok": False, "job_id": job_id, "status": "failed",
                "error_code": "results_write_error", "error_message": code}

    # --- 4) Flip the GEN# row from pending -> complete -----------------------
    now = datetime.now(timezone.utc).isoformat()
    model_version = f"{MODEL_ID} ({REGION}, on-demand)"
    try:
        _ddb.update_item(
            TableName=TABLE_NAME,
            Key={"PK": {"S": f"USER#{user_sub}"},
                 "SK": {"S": f"GEN#{job_id}"}},
            UpdateExpression=(
                "SET #st = :complete, "
                "    completed_at = :now, "
                "    model_version = :mv, "
                "    output_sha256 = :hashes, "
                "    parse_ok = :parse_ok, "
                "    input_tokens = :in_tok, "
                "    output_tokens = :out_tok, "
                "    patient_version = :pv, "
                "    patient_model_version = :pmv, "
                "    patient_parse_ok = :ppok, "
                "    patient_output_tokens = :ptok"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":complete": {"S": "complete"},
                ":now":      {"S": now},
                ":mv":       {"S": model_version},
                ":hashes":   {"M": {k: {"S": v} for k, v in output_hashes.items()}},
                ":parse_ok": {"BOOL": parse_ok},
                ":in_tok":   _opt_number(usage.get("inputTokens")),
                ":out_tok":  _opt_number(usage.get("outputTokens")),
                # Patient v2 provenance: which path produced the leaflet, so the
                # audit log distinguishes a combined-pass (v1) leaflet from a
                # second-pass (v2) one. See docs/PATIENT_V2_DESIGN.md.
                ":pv":       {"S": patient_version},
                ":pmv":      {"S": patient_mv},
                ":ppok":     {"BOOL": patient_parse_ok},
                ":ptok":     _opt_number(patient_usage.get("outputTokens")),
                ":pending":  {"S": "pending"},
            },
            # Only flip from pending; never overwrite a complete OR failed row.
            # This makes a double-invocation (Lambda async retry) idempotent at
            # the data layer: the second worker can't clobber the first.
            ConditionExpression="#st = :pending",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        # ConditionalCheckFailed means another invocation already finished this
        # job. That's fine - log and move on, outputs are already in place.
        if code == "ConditionalCheckFailedException":
            logger.info(json.dumps({
                "event": "worker_already_terminal",
                "job_id": job_id, "user_sub": user_sub,
            }))
            return {"ok": True, "job_id": job_id, "status": "already_terminal"}
        logger.error("worker_audit_update_failed: %s job=%s", code, job_id)
        return {"ok": False, "job_id": job_id, "status": "failed_to_mark_complete",
                "error_message": code}

    logger.info(json.dumps({
        "event": "worker_complete",
        "job_id": job_id, "user_sub": user_sub,
        "model_version": model_version,
        "parse_ok": parse_ok,
        "patient_version": patient_version,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "patient_output_tokens": patient_usage.get("outputTokens"),
        "latency_ms": int((time.time() - started) * 1000),
    }))
    return {"ok": True, "job_id": job_id, "status": "complete",
            "parse_ok": parse_ok, "patient_version": patient_version}


def _mark_failed(user_sub: str, job_id: str, error_code: str, error_message: str):
    """Flip the GEN# row from pending -> failed. Best-effort: if it's already
    terminal (a parallel invoke beat us, or this is a retry of a known-failed
    row) we just log and move on."""
    try:
        _ddb.update_item(
            TableName=TABLE_NAME,
            Key={"PK": {"S": f"USER#{user_sub}"},
                 "SK": {"S": f"GEN#{job_id}"}},
            UpdateExpression=("SET #st = :failed, error_code = :ec, "
                              "error_message = :em, failed_at = :now"),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":failed":  {"S": "failed"},
                ":ec":      {"S": error_code},
                ":em":      {"S": error_message},
                ":now":     {"S": datetime.now(timezone.utc).isoformat()},
                ":pending": {"S": "pending"},
            },
            ConditionExpression="#st = :pending",
        )
    except ClientError as exc:
        logger.error("mark_failed_skipped: %s", exc.response.get("Error", {}))


def _opt_number(n):
    """DynamoDB N attribute or NULL if None. Bedrock omits token counts in
    some error paths; we want to write a clean NULL, not crash on .str()."""
    if n is None:
        return {"NULL": True}
    return {"N": str(int(n))}


# -----------------------------------------------------------------------------
# (2) Legacy direct-invoke path - kept for slice 2/3 smoke tests
# -----------------------------------------------------------------------------
def _run_direct_invoke(event):
    """Old slice-2 sync handler: synthesize a generation row in one go, return
    the outputs directly. Used by `aws lambda invoke` smoke tests."""
    started = time.time()

    if isinstance(event, str):
        try:
            event = json.loads(event)
        except json.JSONDecodeError:
            return _bad_request("event was a string but not valid JSON")

    body_obj = {}
    if isinstance(event.get("body"), str):
        try:
            body_obj = json.loads(event["body"])
        except json.JSONDecodeError:
            return _bad_request("body was a string but not valid JSON")
    elif isinstance(event.get("body"), dict):
        body_obj = event["body"]

    merged = {**event, **body_obj}
    user_sub = (merged.get("user_sub") or "").strip()
    notes = (merged.get("notes") or "").strip()
    output_type = (merged.get("output_type") or "summary,gp_letter,patient").strip()

    if not user_sub:
        return _bad_request("'user_sub' is required (the Cognito subject)")
    if not notes:
        return _bad_request("'notes' is required and must be non-empty")

    try:
        response = _converse(SYSTEM_PROMPT, notes, MAX_TOKENS)
    except ClientError as exc:
        logger.error("direct_bedrock_failed: %s", exc.response.get("Error", {}))
        return {"ok": False, "error": "bedrock_error",
                "message": exc.response.get("Error", {}).get("Code", "Unknown")}

    model_text = response["output"]["message"]["content"][0]["text"]
    usage = response.get("usage", {})
    outputs, parse_ok = _split_outputs(model_text)

    # Patient v2 (optional): regenerate PART C from PART A alone. No-op unless
    # PATIENT_V2_SECOND_PASS is enabled; mutates outputs['patient'] on success.
    patient_version, patient_mv, patient_parse_ok, _patient_usage = _maybe_second_pass(outputs)

    input_hash = _sha256(notes)
    output_hashes = {k: _sha256(v) for k, v in outputs.items()}

    now = datetime.now(timezone.utc).isoformat()
    generation_id = _ulid()
    model_version = f"{MODEL_ID} ({REGION}, on-demand)"

    # Legacy SK format keeps slice 2/3 smoke tests bit-identical to before
    # slice 4b: SK=GEN#<timestamp>#<ulid>. New async path uses SK=GEN#<ulid>.
    item = {
        "PK": f"USER#{user_sub}",
        "SK": f"GEN#{now}#{generation_id}",
        "generation_id": generation_id,
        "user_sub": user_sub,
        "timestamp": now,
        "input_sha256": input_hash,
        "output_sha256": output_hashes,
        "model_version": model_version,
        "output_type": output_type,
        "draft": True,
        "reviewed_at": None,
        "request_region": REGION,
        "inference_profile": "n/a (on-demand)",
        "parse_ok": parse_ok,
        "patient_version": patient_version,
        "patient_model_version": patient_mv,
        "patient_parse_ok": patient_parse_ok,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "schema_version": SCHEMA_VERSION,
        # status field added so direct-invoke rows match the new schema shape.
        "status": "complete",
        "started_at": now,
        "completed_at": now,
    }

    try:
        _table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )
    except ClientError as exc:
        logger.error("direct_audit_put_failed: %s", exc.response.get("Error", {}))
        return {"ok": False, "error": "audit_write_error",
                "message": exc.response.get("Error", {}).get("Code", "Unknown")}

    logger.info(json.dumps({
        "event": "direct_invoke_complete",
        "generation_id": generation_id, "user_sub": user_sub,
        "model_version": model_version, "parse_ok": parse_ok,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "latency_ms": int((time.time() - started) * 1000),
    }))

    return {
        "ok": True, "generation_id": generation_id, "draft": True,
        "model_version": model_version, "parse_ok": parse_ok,
        "outputs": outputs,
        "input_sha256": input_hash, "output_sha256": output_hashes,
    }
