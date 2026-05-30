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


# ---------------------------------------------------------------------------
# Patient v2 — optional second-pass patient-version generation
# (docs/PATIENT_V2_DESIGN.md). Off by default; flag-gated on.
# ---------------------------------------------------------------------------
def _converse_resp(text, *, in_tok=100, out_tok=200):
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": in_tok, "outputTokens": out_tok},
    }


# A combined v1 output whose PART C contains standard-of-care stoma red flags
# that are NOT in PART A — the exact Run 4 / Ibrahim failure mode.
_COMBINED_WITH_ADDED_ADVICE = (
    "PART A - DISCHARGE SUMMARY\n\n"
    "Emergency laparotomy, end colostomy formed. No safety-net advice documented.\n\n"
    "PART B - GP LETTER\n\n"
    "Dear GP, post-op as above.\n\n"
    "PART C - PATIENT VERSION\n\n"
    "You had an operation. Call 999 if you have high output, no output, or a blockage.\n"
)

# What the second pass returns when anchored to PART A alone: no invented red
# flags, only the generic fall-back line.
_CLEAN_LEAFLET = (
    "Your discharge information\n\n"
    "You had an operation on your tummy (laparotomy) and now have a stoma.\n"
    "If you become unwell or are worried about anything, contact your GP or "
    "call NHS 111. Call 999 if it is an emergency.\n"
)


def test_patient_v1_is_default_single_call(load_worker, fake_ddb):
    """With the flag unset, the worker makes ONE Bedrock call and tags the row v1."""
    _seed_pending(fake_ddb)
    app = load_worker()  # PATIENT_V2_SECOND_PASS unset -> off
    result = app.lambda_handler(_dispatcher_event(), None)

    assert result["status"] == "complete"
    assert result["patient_version"] == "v1"
    assert app._bedrock.converse.call_count == 1  # no second pass

    gen = fake_ddb.items[("discharge-audit-audit",
                          f"USER#{USER_SUB}", f"GEN#{JOB_ID}")]
    assert gen["patient_version"]["S"] == "v1"


def test_patient_v2_regenerates_part_c_from_summary_only(load_worker, fake_ddb):
    """Flag on: the patient version is produced by a SECOND call whose only input
    is PART A. The added stoma red flags from the combined pass are gone, and the
    second call is fed the summary (not the raw notes / PART C)."""
    _seed_pending(fake_ddb)
    app = load_worker(extra_env={"PATIENT_V2_SECOND_PASS": "on"})
    app._bedrock.converse.side_effect = [
        _converse_resp(_COMBINED_WITH_ADDED_ADVICE),
        _converse_resp(_CLEAN_LEAFLET),
    ]
    result = app.lambda_handler(_dispatcher_event(), None)

    assert result["status"] == "complete"
    assert result["patient_version"] == "v2"
    assert app._bedrock.converse.call_count == 2

    # The stored patient version is the clean second-pass leaflet.
    res = fake_ddb.items[("discharge-audit-results",
                          f"USER#{USER_SUB}", f"RES#{JOB_ID}")]
    assert "high output" not in res["patient"]["S"]
    assert "no output" not in res["patient"]["S"]
    assert "laparotomy" in res["patient"]["S"]

    # Second call used the patient prompt and was fed PART A only (no PART C).
    second = app._bedrock.converse.call_args_list[1].kwargs
    assert second["system"][0]["text"] == app.PATIENT_SYSTEM_PROMPT
    fed = second["messages"][0]["content"][0]["text"]
    assert "Emergency laparotomy" in fed       # PART A content
    assert "PART C" not in fed                  # the raw leaflet was NOT passed back in

    # Audit row records the v2 provenance.
    gen = fake_ddb.items[("discharge-audit-audit",
                          f"USER#{USER_SUB}", f"GEN#{JOB_ID}")]
    assert gen["patient_version"]["S"] == "v2"
    assert gen["patient_parse_ok"]["BOOL"] is True


def test_patient_v2_falls_back_to_v1_on_second_pass_error(load_worker, fake_ddb):
    """If the second pass errors, the job still completes with the v1 leaflet and
    the row is tagged v1_fallback — the optional pass never fails the generation."""
    _seed_pending(fake_ddb)
    app = load_worker(extra_env={"PATIENT_V2_SECOND_PASS": "on"})
    app._bedrock.converse.side_effect = [
        _converse_resp(_COMBINED_WITH_ADDED_ADVICE),
        ClientError(
            error_response={"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            operation_name="Converse",
        ),
    ]
    result = app.lambda_handler(_dispatcher_event(), None)

    assert result["status"] == "complete"
    assert result["patient_version"] == "v1_fallback"
    # v1 combined-pass leaflet is preserved (job not failed).
    res = fake_ddb.items[("discharge-audit-results",
                          f"USER#{USER_SUB}", f"RES#{JOB_ID}")]
    assert res["patient"]["S"].startswith("PART C")


def test_maybe_second_pass_skips_when_no_summary(load_worker, fake_ddb):
    """Belt-and-braces unit: flag on but an empty PART A -> no second call, v1."""
    app = load_worker(extra_env={"PATIENT_V2_SECOND_PASS": "on"})
    app._bedrock.converse.reset_mock(side_effect=True)
    version, _mv, parse_ok, _usage = app._maybe_second_pass({"summary": "", "patient": "x"})
    assert version == "v1"
    assert parse_ok is True
    assert app._bedrock.converse.call_count == 0
