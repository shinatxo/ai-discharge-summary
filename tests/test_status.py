"""Unit tests for the slice-4b status Lambda (src/status/app.py)."""

from __future__ import annotations

import json

from conftest import http_api_event


JWT_SUB = "1682f284-bf41-7032-aa6b-cognito00sub"
OTHER_SUB = "ffffffff-bbbb-cccc-dddd-someoneelse00"
JOB_ID = "01HZZZAAAAAAAAAAAAAAAAAAAA"   # 26 char Crockford-safe ULID


def _get(job_id=JOB_ID, headers=None, sub=JWT_SUB):
    return http_api_event(sub, headers=headers,
                           path_params={"id": job_id},
                           method="GET", path=f"/generations/{job_id}")


def _seed_pending(fake_ddb, *, sub=JWT_SUB, job_id=JOB_ID):
    fake_ddb.items[("discharge-audit-audit", f"USER#{sub}", f"GEN#{job_id}")] = {
        "PK": {"S": f"USER#{sub}"}, "SK": {"S": f"GEN#{job_id}"},
        "status": {"S": "pending"},
        "started_at": {"S": "2026-05-26T10:00:00+00:00"},
        "draft": {"BOOL": True},
        "input_sha256": {"S": "abc"},
    }


def _seed_complete(fake_ddb, *, sub=JWT_SUB, job_id=JOB_ID, with_outputs=True):
    fake_ddb.items[("discharge-audit-audit", f"USER#{sub}", f"GEN#{job_id}")] = {
        "PK": {"S": f"USER#{sub}"}, "SK": {"S": f"GEN#{job_id}"},
        "status": {"S": "complete"},
        "started_at": {"S": "2026-05-26T10:00:00+00:00"},
        "completed_at": {"S": "2026-05-26T10:00:21+00:00"},
        "draft": {"BOOL": True},
        "input_sha256": {"S": "abc"},
        "output_sha256": {"M": {
            "summary":   {"S": "h1"},
            "gp_letter": {"S": "h2"},
            "patient":   {"S": "h3"},
        }},
        "parse_ok": {"BOOL": True},
        "model_version": {"S": "anthropic.claude-sonnet-4-6 (eu-west-2, on-demand)"},
    }
    if with_outputs:
        fake_ddb.items[("discharge-audit-results", f"USER#{sub}", f"RES#{job_id}")] = {
            "PK": {"S": f"USER#{sub}"}, "SK": {"S": f"RES#{job_id}"},
            "summary":   {"S": "DISCHARGE SUMMARY ..."},
            "gp_letter": {"S": "Dear GP, ..."},
            "patient":   {"S": "You came in with ..."},
        }


def _seed_failed(fake_ddb, *, sub=JWT_SUB, job_id=JOB_ID, code="bedrock_error",
                  msg="ResourceNotFoundException"):
    fake_ddb.items[("discharge-audit-audit", f"USER#{sub}", f"GEN#{job_id}")] = {
        "PK": {"S": f"USER#{sub}"}, "SK": {"S": f"GEN#{job_id}"},
        "status": {"S": "failed"},
        "error_code":    {"S": code},
        "error_message": {"S": msg},
        "failed_at":     {"S": "2026-05-26T10:00:05+00:00"},
        "started_at":    {"S": "2026-05-26T10:00:00+00:00"},
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
def test_pending_returns_200_with_status_pending_and_no_outputs(load_status, fake_ddb):
    _seed_pending(fake_ddb)
    app = load_status()
    resp = app.lambda_handler(_get(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "pending"
    assert body["job_id"] == JOB_ID
    assert "outputs" not in body


def test_complete_returns_outputs_from_results_table(load_status, fake_ddb):
    _seed_complete(fake_ddb, with_outputs=True)
    app = load_status()
    resp = app.lambda_handler(_get(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "complete"
    assert body["outputs"]["summary"].startswith("DISCHARGE SUMMARY")
    assert body["outputs"]["gp_letter"].startswith("Dear GP")
    assert body["outputs"]["patient"].startswith("You came in")
    assert body["output_sha256"] == {"summary": "h1", "gp_letter": "h2", "patient": "h3"}


def test_complete_but_results_ttl_expired_returns_status_expired(load_status, fake_ddb):
    _seed_complete(fake_ddb, with_outputs=False)
    app = load_status()
    resp = app.lambda_handler(_get(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    # We surface a NEW state, not status=complete-without-outputs, so the UI
    # can render "this draft has expired" rather than show empty fields.
    assert body["status"] == "expired"
    assert body["error"] == "outputs_unavailable"


def test_failed_returns_error_code_and_message_no_outputs(load_status, fake_ddb):
    _seed_failed(fake_ddb)
    app = load_status()
    resp = app.lambda_handler(_get(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "failed"
    assert body["error_code"] == "bedrock_error"
    # error_message should be an AWS code, NOT PHI / NOT model output.
    assert body["error_message"] == "ResourceNotFoundException"
    assert "outputs" not in body


# ---------------------------------------------------------------------------
# Fail-closed paths (anti-spoof + safety)
# ---------------------------------------------------------------------------
def test_cross_user_get_returns_404_does_not_leak_existence(load_status, fake_ddb):
    # Seed a job under OTHER_SUB; call as JWT_SUB.
    _seed_complete(fake_ddb, sub=OTHER_SUB)
    app = load_status()
    resp = app.lambda_handler(_get(sub=JWT_SUB), None)
    # Uniform 404 (same as "no such job"). Critically NOT 403, which would
    # confirm the job exists for some other user.
    assert resp["statusCode"] == 404
    body = json.loads(resp["body"])
    assert body["error"] == "not_found"


def test_unknown_job_returns_404(load_status, fake_ddb):
    app = load_status()
    resp = app.lambda_handler(_get(job_id="01HZZZBBBBBBBBBBBBBBBBBBBB"), None)
    assert resp["statusCode"] == 404


def test_invalid_ulid_path_param_returns_404(load_status, fake_ddb):
    app = load_status()
    resp = app.lambda_handler(_get(job_id="../../etc/passwd"), None)
    assert resp["statusCode"] == 404


def test_no_http_context_returns_401(load_status, fake_ddb):
    app = load_status()
    resp = app.lambda_handler({"pathParameters": {"id": JOB_ID}}, None)
    assert resp["statusCode"] == 401


def test_no_jwt_sub_returns_401(load_status, fake_ddb):
    app = load_status()
    evt = _get()
    evt["requestContext"]["authorizer"]["jwt"]["claims"] = {}
    resp = app.lambda_handler(evt, None)
    assert resp["statusCode"] == 401
