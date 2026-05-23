"""
AI Discharge Summary Assistant - generate Lambda (Phase 2, slice 2).

Hot path: take messy ward-round notes, call Amazon Bedrock (the pinned Claude
Sonnet model, on-demand in eu-west-2 per ADR-001/003), and return the three
drafts (clinician discharge summary, GP letter, patient-friendly version).

Every generation also writes a HASH-ONLY audit item to the DynamoDB audit table
(ADR-002): who/when/which-model plus SHA-256 hashes of the input and each
output. No patient-identifiable content (PHI) is ever stored in the table or
written to CloudWatch logs - only hashes, ids, and operational metadata.

The execution role (see infra/template.yaml) can do exactly four things:
  - dynamodb:PutItem/UpdateItem on the audit table (no delete),
  - kms:Decrypt/GenerateDataKey on the CMK *via DynamoDB only*,
  - bedrock:InvokeModel on the one pinned model,
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
MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
SCHEMA_VERSION = int(os.environ.get("SCHEMA_VERSION", "1"))

# Deterministic, safety-first generation: temperature 0 so the model doesn't
# get "creative" with clinical facts. maxTokens sized for three full outputs.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4096"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0"))

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Reuse clients across warm invocations (created at import time, outside the
# handler) - a standard Lambda performance pattern.
_bedrock = boto3.client("bedrock-runtime", region_name=REGION)
_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)

# Load the system prompt once per container. It is packaged alongside this file
# (synced copy of prompts/discharge-summary-system-prompt.md, the canonical
# source of truth). We send the model-role portion only - everything from the
# "## SYSTEM PROMPT" marker onward.
_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "system_prompt.md")


def _load_system_prompt() -> str:
    with open(_PROMPT_PATH, "r", encoding="utf-8") as fh:
        raw = fh.read()
    marker = "## SYSTEM PROMPT"
    idx = raw.find(marker)
    if idx == -1:
        # Fail closed: if the packaged prompt is malformed, do not silently send
        # a half-prompt that could weaken the safety rules.
        raise RuntimeError("system_prompt.md is missing the '## SYSTEM PROMPT' marker")
    body = raw[idx:]
    # Drop the marker heading line itself, keep the rule text after it.
    body = body.split("\n", 1)[1] if "\n" in body else body
    return body.strip()


SYSTEM_PROMPT = _load_system_prompt()


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # excludes I, L, O, U


def _ulid() -> str:
    """A dependency-free ULID: 48-bit ms timestamp + 80 bits randomness, in
    Crockford base32. Lexicographically sortable, so it gives the SK natural
    time-ordering for cheap per-clinician history queries (ADR-002)."""
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


# Match a "PART A/B/C ..." line, tolerating leading markdown (#, *, >) and any
# surrounding whitespace, case-insensitive, anchored at line start.
def _part_marker(label: str):
    return re.compile(rf"(?im)^[#*>\s]*PART\s+{label}\b.*$")


def _split_outputs(text: str):
    """Split the combined model output into {summary, gp_letter, patient}.

    Returns (outputs_dict, parse_ok). The combined prompt (ADR open-Q #3,
    Option A) emits PART A, PART B, PART C in order; we slice between the
    markers. If the markers are missing/out-of-order we fail safe: hash the
    whole text under 'summary', flag parse_ok=False so the audit record tells
    the truth about what happened.
    """
    pos = {}
    for label in ("A", "B", "C"):
        m = _part_marker(label).search(text)
        if m:
            pos[label] = m.start()

    if {"A", "B", "C"} <= pos.keys() and pos["A"] < pos["B"] < pos["C"]:
        summary = text[pos["A"]:pos["B"]].strip()
        gp_letter = text[pos["B"]:pos["C"]].strip()
        patient = text[pos["C"]:].strip()
        return {"summary": summary, "gp_letter": gp_letter, "patient": patient}, True

    # Fail-safe: keep the full text, mark unparsed.
    return {"summary": text.strip(), "gp_letter": "", "patient": ""}, False


def _bad_request(message: str):
    return {"ok": False, "error": "bad_request", "message": message}


# -----------------------------------------------------------------------------
# Handler
# -----------------------------------------------------------------------------
def lambda_handler(event, context):
    """Direct-invoke contract (an API Gateway / Cognito front door comes in a
    later slice). Expected event:
        {
          "notes": "<free-text ward-round notes>",   # required
          "user_sub": "<cognito subject>",            # required (pseudonymous)
          "output_type": "summary,gp_letter,patient"  # optional, default all
        }
    """
    started = time.time()

    # Accept either a raw dict (console/CLI test) or an API-GW-style body string,
    # so this keeps working when the HTTP front door is added.
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except json.JSONDecodeError:
            return _bad_request("event was a string but not valid JSON")
    if isinstance(event.get("body"), str):
        try:
            event = {**event, **json.loads(event["body"])}
        except json.JSONDecodeError:
            return _bad_request("body was a string but not valid JSON")

    notes = (event.get("notes") or "").strip()
    user_sub = (event.get("user_sub") or "").strip()
    output_type = (event.get("output_type") or "summary,gp_letter,patient").strip()

    if not notes:
        return _bad_request("'notes' is required and must be non-empty")
    if not user_sub:
        return _bad_request("'user_sub' is required (the Cognito subject)")

    # --- 1) Bedrock Converse call on the pinned model -------------------------
    try:
        response = _bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": notes}]}],
            inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
        )
    except ClientError as exc:
        # Log the error class/code, never the notes.
        logger.error("bedrock_converse_failed: %s", exc.response.get("Error", {}))
        return {"ok": False, "error": "bedrock_error",
                "message": exc.response.get("Error", {}).get("Code", "Unknown")}

    model_text = response["output"]["message"]["content"][0]["text"]
    usage = response.get("usage", {})

    outputs, parse_ok = _split_outputs(model_text)

    # --- 2) Hashes (the only representation of content we persist) ------------
    input_hash = _sha256(notes)
    output_hashes = {k: _sha256(v) for k, v in outputs.items()}

    # --- 3) Hash-only audit item (ADR-002) -----------------------------------
    now = datetime.now(timezone.utc).isoformat()
    generation_id = _ulid()
    model_version = f"{MODEL_ID} ({REGION}, on-demand)"

    item = {
        "PK": f"USER#{user_sub}",
        "SK": f"GEN#{now}#{generation_id}",
        "generation_id": generation_id,
        "user_sub": user_sub,
        "timestamp": now,
        "input_sha256": input_hash,
        "output_sha256": output_hashes,           # DynamoDB map (M)
        "model_version": model_version,
        "output_type": output_type,
        "draft": True,                            # until the clinician reviews
        "reviewed_at": None,
        "request_region": REGION,
        "inference_profile": "n/a (on-demand)",   # residency evidence (ADR-003)
        "parse_ok": parse_ok,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "schema_version": SCHEMA_VERSION,
    }

    try:
        # Write-once: refuse to overwrite an existing generation. Combined with
        # the role having no DeleteItem, this is the immutability contract at
        # the data layer (the S3 Object Lock WORM ledger - a later slice - is
        # the tamper-evidence control on top).
        _table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )
    except ClientError as exc:
        logger.error("audit_put_failed: %s", exc.response.get("Error", {}))
        return {"ok": False, "error": "audit_write_error",
                "message": exc.response.get("Error", {}).get("Code", "Unknown")}

    # --- 4) PHI-free operational log + response -------------------------------
    logger.info(json.dumps({
        "event": "generation_complete",
        "generation_id": generation_id,
        "user_sub": user_sub,
        "model_version": model_version,
        "parse_ok": parse_ok,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "latency_ms": int((time.time() - started) * 1000),
    }))

    return {
        "ok": True,
        "generation_id": generation_id,
        "draft": True,
        "model_version": model_version,
        "parse_ok": parse_ok,
        "outputs": outputs,        # returned to the caller/UI, NOT logged
        "input_sha256": input_hash,
        "output_sha256": output_hashes,
    }
