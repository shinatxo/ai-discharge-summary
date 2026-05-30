"""Unit tests for the Wave 3 synthetic-traffic canary (src/canary/app.py).

The canary talks HTTP to the deployed API and AWS via cognito-idp / ssm /
cloudwatch clients. We stub the three clients and monkeypatch the module's
`_http` to simulate POST /generate + GET /generations/{id}, so the scoring and
metric-emission logic is exercised with no network and no sleeps.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CANARY_DIR = ROOT / "src" / "canary"

BASE_ENV = {
    "API_BASE": "https://api.test",
    "USER_POOL_CLIENT_ID": "clientid",
    "CANARY_USERNAME": "canary@synthetic.invalid",
    "CANARY_PASSWORD_PARAM": "/discharge/canary/password",
    "POLL_INTERVAL_S": "0",
    "POLL_TIMEOUT_S": "30",
}


class FakeSSM:
    def get_parameter(self, *, Name, WithDecryption=False):
        return {"Parameter": {"Value": "synthetic-pw"}}


class FakeIdp:
    def __init__(self, id_token="id-token", challenge=None):
        self.id_token = id_token
        self.challenge = challenge

    def initiate_auth(self, *, ClientId, AuthFlow, AuthParameters):
        if self.id_token:
            return {"AuthenticationResult": {"IdToken": self.id_token}}
        return {"ChallengeName": self.challenge or "NEW_PASSWORD_REQUIRED"}


class FakeCW:
    def __init__(self):
        self.metric_data = []

    def put_metric_data(self, *, Namespace, MetricData):
        self.namespace = Namespace
        self.metric_data += MetricData


def _load_canary(monkeypatch, env, fakes):
    for k, v in {**BASE_ENV, **env}.items():
        monkeypatch.setenv(k, v)
    import boto3

    def fake_client(name, region_name=None, **kwargs):
        if name in fakes:
            return fakes[name]
        raise AssertionError(f"unexpected boto3.client({name!r})")

    monkeypatch.setattr(boto3, "client", fake_client)
    sys.path = [p for p in sys.path
                if not p.endswith(("dispatcher", "status", "generate", "canary"))]
    sys.path.insert(0, str(CANARY_DIR))
    sys.modules.pop("app", None)
    return importlib.import_module("app")


def _metric_map(cw):
    """(MetricName, ScenarioDim) -> Value."""
    return {(m["MetricName"], m["Dimensions"][0]["Value"]): m["Value"] for m in cw.metric_data}


# ---------------------------------------------------------------------------
def test_canary_happy_path_emits_success_metrics(monkeypatch):
    cw = FakeCW()
    app = _load_canary(monkeypatch, {"CANARY_SCENARIOS": "S1,S2"},
                       {"cognito-idp": FakeIdp(), "ssm": FakeSSM(), "cloudwatch": cw})

    counter = {"n": 0}

    def fake_http(method, url, id_token, body=None, idempotency_key=None):
        assert id_token == "id-token"
        if method == "POST":
            counter["n"] += 1
            assert idempotency_key  # fresh key per POST
            return 202, {"job_id": f"job{counter['n']}", "status": "pending"}
        return 200, {"status": "complete", "parse_ok": True,
                     "outputs": {"summary": "a", "gp_letter": "b", "patient": "c"}}

    monkeypatch.setattr(app, "_http", fake_http)
    out = app.lambda_handler({}, None)

    assert out["ok"] is True
    assert out["scenarios"] == 2 and out["success"] == 2 and out["parse_ok"] == 2
    m = _metric_map(cw)
    assert m[("CanaryRunOk", "ALL")] == 1.0
    assert m[("EndToEndSuccess", "ALL")] == 2.0
    assert m[("SuccessRatePct", "ALL")] == 100.0
    # per-scenario latency + success present
    assert ("EndToEndLatencyMs", "S1") in m
    assert m[("EndToEndSuccess", "S2")] == 1.0


def test_canary_auth_failure_emits_runok_zero(monkeypatch):
    cw = FakeCW()
    app = _load_canary(monkeypatch, {"CANARY_SCENARIOS": "S1"},
                       {"cognito-idp": FakeIdp(id_token=None), "ssm": FakeSSM(),
                        "cloudwatch": cw})
    out = app.lambda_handler({}, None)
    assert out["ok"] is False and out["error"] == "auth_failed"
    assert _metric_map(cw)[("CanaryRunOk", "ALL")] == 0.0


def test_canary_failed_job_flags_throttle(monkeypatch):
    cw = FakeCW()
    app = _load_canary(monkeypatch, {"CANARY_SCENARIOS": "S1"},
                       {"cognito-idp": FakeIdp(), "ssm": FakeSSM(), "cloudwatch": cw})

    def fake_http(method, url, id_token, body=None, idempotency_key=None):
        if method == "POST":
            return 202, {"job_id": "j1"}
        return 200, {"status": "failed", "error_code": "bedrock_error",
                     "error_message": "ThrottlingException: rate exceeded"}

    monkeypatch.setattr(app, "_http", fake_http)
    out = app.lambda_handler({}, None)

    assert out["success"] == 0
    m = _metric_map(cw)
    assert m[("BedrockThrottle", "S1")] == 1.0
    assert m[("JobFailed", "S1")] == 1.0
    assert m[("EndToEndSuccess", "S1")] == 0.0
    assert m[("CanaryRunOk", "ALL")] == 1.0   # the canary itself still ran


def test_canary_poll_timeout_marks_failure(monkeypatch):
    cw = FakeCW()
    # POLL_TIMEOUT_S=0 -> the poll loop body is skipped; the job is still in
    # flight and must be scored as a timeout failure.
    app = _load_canary(monkeypatch, {"CANARY_SCENARIOS": "S1", "POLL_TIMEOUT_S": "0"},
                       {"cognito-idp": FakeIdp(), "ssm": FakeSSM(), "cloudwatch": cw})

    def fake_http(method, url, id_token, body=None, idempotency_key=None):
        if method == "POST":
            return 202, {"job_id": "j1"}
        return 200, {"status": "pending"}

    monkeypatch.setattr(app, "_http", fake_http)
    out = app.lambda_handler({}, None)

    assert out["success"] == 0
    assert _metric_map(cw)[("JobFailed", "S1")] == 1.0


def test_canary_dispatch_failure_scored_without_polling(monkeypatch):
    cw = FakeCW()
    app = _load_canary(monkeypatch, {"CANARY_SCENARIOS": "S1"},
                       {"cognito-idp": FakeIdp(), "ssm": FakeSSM(), "cloudwatch": cw})

    def fake_http(method, url, id_token, body=None, idempotency_key=None):
        if method == "POST":
            return 401, {"error": "unauthorized"}   # never returns a job_id
        raise AssertionError("should not poll a job that never dispatched")

    monkeypatch.setattr(app, "_http", fake_http)
    out = app.lambda_handler({}, None)
    assert out["success"] == 0
    assert _metric_map(cw)[("JobFailed", "S1")] == 1.0
