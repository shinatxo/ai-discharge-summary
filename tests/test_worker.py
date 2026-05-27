"""Unit tests for the slice-4b worker (src/generate/app.py).

The worker runs in three modes:

  (1) dispatcher event (production hot path, slice 4b) — writes outputs to
      ResultsTable and flips the pending GEN# row to complete (or failed).
  (2) direct invoke (legacy slice-2/3 smoke tests) — synthesises its own row
      in the OLD SK format `GEN#<timestamp>#<ulid>` for backwards compat.
  (3) accidentally-routed HTTP API event — returns 410 Gone (anti-regression).
"""

from __future__ import annotations

import json
import re

from botocore.exceptions import ClientError

from conftest import http_api_event


USER_SUB = "1682f284-bf41-7032-aa6b-cognito00sub"
JOB_ID = "01HZZZAAAAAAAAAAAAAAAAAAAA"


def _dispatcher_event(notes="72M NSTEMI ..."):
    return {
        "job_id": JOB_ID,
        "user_sub": USER_SUB,
        "notes": notes,
        "input_sha256": "irrelevant_for_dispatch",
        "started_at": "2026-05-26T10:00:00+00:00",
    }


def _seed_pending(fake_ddb):
    fake_ddb.items[("discharge-audit-audit", f"USER#{USER_SUB}", f"GEN#{JOB_ID}")] = {
        "PK": {"S": f"USER#{USER_SUB}"}, "SK": {"S": f"GEN#{JOB_ID}"},
        "status": {"S": "pending"},
        "user_sub": {"S": USER_SUB},
        "started_at": {"S": "2026-05-26T10:00:00+00:00"},
    }


# ---------------------------------------------------------------------------
# (1) dispatcher event - the slice 4b hot path
# ---------------------------------------------------------------------------
def test_worker_writes_outputs_and_flips_row_to_complete(load_worker, fake_ddb):
    _seed_pending(fake_ddb)
    app = load_worker()
    result = app.lambda_handler(_dispatcher_event(), None)

    assert result["ok"] is True
    assert result["status"] == "complete"

    # GEN# row flipped to complete with hashes, model version, tokens, parse_ok.
    gen = fake_ddb.items[("discharge-audit-audit",
                          f"USER#{USER_SUB}", f"GEN#{JOB_ID}")]
    assert gen["status"]["S"] == "complete"
    assert gen["parse_ok"]["BOOL"] is True
    assert "anthropic.claude-sonnet-4-6" in gen["model_version"]["S"]
    assert set(gen["output_sha256"]["M"].keys()) == {"summary", "gp_letter", "patient"}
    assert gen["input_tokens"]["N"] == "123"
    assert gen["output_tokens"]["N"] == "456"

    # Results row written under the right composite key with the actual text.
    res = fake_ddb.items[("discharge-audit-results",
                          f"USER#{USER_SUB}", f"RES#{JOB_ID}")]
    assert res["summary"]["S"].startswith("PART A")
    assert res["gp_letter"]["S"].startswith("PART B")
    assert res["patient"]["S"].startswith("PART C")
    # TTL attribute present (epoch seconds in the future).
    assert int(res["ttl"]["N"]) > 0


def test_worker_bedrock_failure_marks_row_failed(load_worker, fake_ddb):
    _seed_pending(fake_ddb)
    bedrock_error = ClientError(
        error_response={"Error": {"Code": "ResourceNotFoundException",
                                    "Message": "model gate"}},
        operation_name="Converse",
    )
    app = load_worker(bedrock_error=bedrock_error)
    result = app.lambda_handler(_dispatcher_event(), None)

    assert result["ok"] is False
    assert result["error_code"] == "bedrock_error"
    gen = fake_ddb.items[("discharge-audit-audit",
                          f"USER#{USER_SUB}", f"GEN#{JOB_ID}")]
    assert gen["status"]["S"] == "failed"
    assert gen["error_code"]["S"] == "bedrock_error"
    # error_message is an AWS code, NOT PHI / model text.
    assert gen["error_message"]["S"] == "ResourceNotFoundException"
    # No results row was written.
    assert ("discharge-audit-results", f"USER#{USER_SUB}", f"RES#{JOB_ID}") not in fake_ddb.items


def test_worker_idempotent_on_retry_via_conditional_check(load_worker, fake_ddb):
    """If a Lambda async retry fires the worker a second time on the SAME
    job_id, the UpdateItem ConditionExpression on status=pending should
    refuse to clobber the already-complete row. The second invocation must
    NOT raise — it returns a benign "already_terminal" outcome."""
    _seed_pending(fake_ddb)
    app = load_worker()
    first = app.lambda_handler(_dispatcher_event(), None)
    assert first["status"] == "complete"
    # Row is now status=complete. Re-fire the worker.
    second = app.lambda_handler(_dispatcher_event(), None)
    assert second["ok"] is True
    assert second["status"] == "already_terminal"


# ---------------------------------------------------------------------------
# (2) Legacy direct-invoke - slice 2/3 smoke test path must still work
# ---------------------------------------------------------------------------
def test_direct_invoke_writes_legacy_format_row(load_worker, fake_ddb):
    app = load_worker()
    result = app.lambda_handler({"user_sub": USER_SUB, "notes": "x"}, None)
    assert result["ok"] is True
    assert result["parse_ok"] is True
    assert set(result["outputs"]) == {"summary", "gp_letter", "patient"}
    # Legacy SK format: GEN#<iso8601 timestamp>#<ulid> - we don't know the exact
    # timestamp, so check the shape.
    rows = [k for k in fake_ddb.items
            if k[0] == "discharge-audit-audit"
            and k[1] == f"USER#{USER_SUB}"
            and re.match(r"^GEN#20\d\d-\d\d-\d\dT.*#[0-9A-HJKMNP-TV-Z]{26}$", k[2])]
    assert rows, f"no legacy-shape row written; keys: {list(fake_ddb.items)}"


def test_direct_invoke_missing_notes_returns_bad_request(load_worker, fake_ddb):
    app = load_worker()
    result = app.lambda_handler({"user_sub": USER_SUB}, None)
    assert result["error"] == "bad_request"


# ---------------------------------------------------------------------------
# (3) Anti-regression: accidentally-routed HTTP API event
# ---------------------------------------------------------------------------
def test_http_api_event_returns_410(load_worker, fake_ddb):
    """If someone re-attaches the HTTP API to this function (slice 4a shape),
    the response should be a loud 410 Gone, not a silent 30s-timeout regression."""
    app = load_worker()
    evt = http_api_event(USER_SUB, body={"notes": "x"})
    result = app.lambda_handler(evt, None)
    assert result["statusCode"] == 410
    body = json.loads(result["body"])
    assert body["error"] == "deprecated_path"


# ---------------------------------------------------------------------------
# Splitter — preserved from slice 2/3 (regression coverage)
# ---------------------------------------------------------------------------
def test_split_strict_path(load_worker, fake_ddb):
    app = load_worker()
    text = "PART A\nA-body\nPART B\nB-body\nPART C\nC-body\n"
    out, ok = app._split_outputs(text)
    assert ok is True
    assert "A-body" in out["summary"]
    assert "B-body" in out["gp_letter"]
    assert "C-body" in out["patient"]


def test_split_forgiving_path_missing_part_a_label(load_worker, fake_ddb):
    app = load_worker()
    text = "Discharge summary content here.\nPART B\nB-body\nPART C\nC-body\n"
    out, ok = app._split_outputs(text)
    assert ok is True
    assert "Discharge summary" in out["summary"]
    assert "B-body" in out["gp_letter"]
    assert "C-body" in out["patient"]


def test_split_fail_safe_when_unparseable(load_worker, fake_ddb):
    app = load_worker()
    text = "I have no idea what to write."
    out, ok = app._split_outputs(text)
    assert ok is False
    assert out["summary"] == text
    assert out["gp_letter"] == ""
    assert out["patient"] == ""
