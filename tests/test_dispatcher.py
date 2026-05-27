"""Unit tests for the slice-4b dispatcher Lambda (src/dispatcher/app.py).

The contracts the dispatcher MUST satisfy, restated as tests:

  1. Anti-spoof: user_sub comes from the verified JWT claim, never from the
     request body. Body-supplied user_sub is silently ignored.
  2. POST without Idempotency-Key -> 202 + new job_id, GEN# row written
     pending, worker invoked async.
  3. POST with Idempotency-Key (first time) -> 202, IDEM# receipt written
     alongside the GEN# pending row.
  4. POST with the SAME Idempotency-Key (replay) -> 200 (not 202), same
     job_id, idempotent_replay=true, worker NOT invoked a second time.
  5. POST without an HTTP/JWT context -> 401 (defensive fail-closed).
  6. POST with bad input (no notes, oversized notes, malformed key) -> 4xx.
  7. Worker invoke failure -> GEN# row flipped to status=failed via the
     dispatcher's mark_failed path; client sees 503 + job_id.
"""

from __future__ import annotations

import json

from botocore.exceptions import ClientError

from conftest import http_api_event


JWT_SUB = "1682f284-bf41-7032-aa6b-cognito00sub"


def _post(body=None, headers=None):
    return http_api_event(JWT_SUB, body=body, headers=headers,
                           method="POST", path="/generate")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
def test_post_without_idempotency_key_returns_202_and_invokes_worker(
        load_dispatcher, fake_ddb, fake_lambda):
    app = load_dispatcher()
    resp = app.lambda_handler(_post(body={"notes": "72M NSTEMI, started ticagrelor."}), None)

    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["ok"] is True
    assert body["status"] == "pending"
    assert body["job_id"]
    assert body["poll_url"].startswith("/generations/")
    # GEN# pending row + IDEM# receipt (auto-generated UUID) written via transaction.
    assert len(fake_ddb.transact_calls) == 1
    items = fake_ddb.transact_calls[0]
    assert len(items) == 2
    sks = sorted(op["Put"]["Item"]["SK"]["S"] for op in items)
    assert any(sk.startswith("GEN#") for sk in sks)
    assert any(sk.startswith("IDEM#") for sk in sks)
    # Worker fired exactly once, async.
    assert len(fake_lambda.invocations) == 1
    inv = fake_lambda.invocations[0]
    assert inv["InvocationType"] == "Event"
    assert inv["FunctionName"] == "discharge-audit-generate"
    assert inv["Payload"]["user_sub"] == JWT_SUB
    assert inv["Payload"]["job_id"] == body["job_id"]


def test_post_with_idempotency_key_writes_receipt_with_that_key(
        load_dispatcher, fake_ddb, fake_lambda):
    app = load_dispatcher()
    key = "11111111-2222-3333-4444-555555555555"
    resp = app.lambda_handler(_post(
        body={"notes": "UTI, ciprofloxacin started."},
        headers={"idempotency-key": key},
    ), None)
    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    # The IDEM# row must use the SUPPLIED key, not a generated one.
    idem_items = [op["Put"]["Item"] for op in fake_ddb.transact_calls[0]
                   if op["Put"]["Item"]["SK"]["S"].startswith("IDEM#")]
    assert len(idem_items) == 1
    assert idem_items[0]["SK"]["S"] == f"IDEM#{key}"
    assert idem_items[0]["generation_id"]["S"] == body["job_id"]
    # IDEM row carries a TTL attribute (epoch seconds).
    assert "ttl" in idem_items[0]
    assert int(idem_items[0]["ttl"]["N"]) > 0


def test_idempotent_replay_returns_200_not_202_and_does_not_invoke_worker_again(
        load_dispatcher, fake_ddb, fake_lambda):
    app = load_dispatcher()
    key = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    notes = "Same notes both times."

    # First call: 202 + worker invoked.
    first = app.lambda_handler(_post(body={"notes": notes},
                                      headers={"idempotency-key": key}), None)
    first_body = json.loads(first["body"])
    assert first["statusCode"] == 202
    assert len(fake_lambda.invocations) == 1

    # Second call with the SAME key: 200 + same job_id, no new invoke.
    second = app.lambda_handler(_post(body={"notes": notes},
                                       headers={"idempotency-key": key}), None)
    second_body = json.loads(second["body"])
    assert second["statusCode"] == 200
    assert second_body["job_id"] == first_body["job_id"]
    assert second_body.get("idempotent_replay") is True
    assert len(fake_lambda.invocations) == 1  # NOT invoked again


# ---------------------------------------------------------------------------
# Anti-spoof: JWT wins over body
# ---------------------------------------------------------------------------
def test_anti_spoof_ignores_body_user_sub(load_dispatcher, fake_ddb, fake_lambda):
    app = load_dispatcher()
    resp = app.lambda_handler(_post(body={
        "notes": "x",
        "user_sub": "ATTACKER-SUB-NOT-MINE",   # MUST be ignored
    }), None)
    assert resp["statusCode"] == 202
    # GEN# row PK must use the JWT sub, not the body sub.
    gen_items = [op["Put"]["Item"] for op in fake_ddb.transact_calls[0]
                  if op["Put"]["Item"]["SK"]["S"].startswith("GEN#")]
    assert gen_items[0]["PK"]["S"] == f"USER#{JWT_SUB}"
    assert gen_items[0]["user_sub"]["S"] == JWT_SUB
    # Worker payload likewise carries the JWT sub, not the body value.
    assert fake_lambda.invocations[0]["Payload"]["user_sub"] == JWT_SUB


# ---------------------------------------------------------------------------
# Fail-closed paths
# ---------------------------------------------------------------------------
def test_no_http_context_returns_401(load_dispatcher, fake_ddb, fake_lambda):
    app = load_dispatcher()
    resp = app.lambda_handler({"notes": "console test"}, None)
    assert resp["statusCode"] == 401
    assert fake_lambda.invocations == []


def test_no_jwt_sub_returns_401(load_dispatcher, fake_ddb):
    app = load_dispatcher()
    evt = _post(body={"notes": "x"})
    # Wipe the sub claim
    evt["requestContext"]["authorizer"]["jwt"]["claims"] = {}
    resp = app.lambda_handler(evt, None)
    assert resp["statusCode"] == 401


def test_missing_notes_returns_400(load_dispatcher):
    app = load_dispatcher()
    resp = app.lambda_handler(_post(body={"output_type": "summary"}), None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "bad_request"


def test_oversized_notes_returns_413(load_dispatcher):
    app = load_dispatcher()
    huge = "x" * 50_001
    resp = app.lambda_handler(_post(body={"notes": huge}), None)
    assert resp["statusCode"] == 413


def test_malformed_idempotency_key_returns_400(load_dispatcher):
    app = load_dispatcher()
    resp = app.lambda_handler(_post(
        body={"notes": "x"},
        headers={"idempotency-key": "not-a-uuid"},
    ), None)
    assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# Worker invoke failure -> failed row + 503
# ---------------------------------------------------------------------------
def test_worker_invoke_failure_marks_row_failed_and_returns_503(
        load_dispatcher, fake_ddb, fake_lambda):
    fake_lambda.next_error = ClientError(
        error_response={"Error": {"Code": "TooManyRequestsException",
                                    "Message": "throttled"}},
        operation_name="Invoke",
    )
    app = load_dispatcher()
    resp = app.lambda_handler(_post(body={"notes": "x"}), None)
    assert resp["statusCode"] == 503
    body = json.loads(resp["body"])
    assert body["job_id"]
    # The GEN# row should now be status=failed.
    gen_rows = [v for k, v in fake_ddb.items.items()
                if k[1] == f"USER#{JWT_SUB}" and k[2].startswith("GEN#")]
    assert gen_rows
    assert gen_rows[0]["status"]["S"] == "failed"
    assert gen_rows[0]["error_code"]["S"] == "worker_invoke_failed"
