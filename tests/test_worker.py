"""Unit tests for the pipeline worker (src/generate/app.py; WS1a step build piece 5,
5 Nov 2026). Replaces the v1 worker tests on the feature branch: the v1 dispatcher
path, direct-invoke, _split_outputs and Patient v2 tests stay on main with the v1
worker until the W11 cut-over (ADR-009 (d): no switch on the branch).

Fake DynamoDB (conftest), the harness's dry-run Bedrock (scriptable), a fake trace
writer. The release contract (CSO, 5 Nov 2026) and HAZ-19: an 8b failure exposes no
draft through polling, downloads or error payloads — these tests are its evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from conftest import ROOT, http_api_event

sys.path.insert(0, str(ROOT / "evals"))
import run_pipeline_eval as R  # noqa: E402

USER_SUB = "1682f284-bf41-7032-aa6b-cognito00sub"
JOB_ID = "01HZZZAAAAAAAAAAAAAAAAAAAA"
AUDIT = "discharge-audit-audit"
RESULTS = "discharge-audit-results"
NOTES = "72M admitted with chest pain\nTrop raised\nDH: aspirin 75mg OD\nPlan: home, GP review 2/52"
FIXED_MESSAGE = ("No draft was released. Generation could not be completed. "
                 "Try again or complete the summary manually.")
EIGHT_B_CODES = ["bad_input", "required_heading_missing", "box_structure", "resus_text_outside",
                 "clinical_stale", "contradictions_block_mismatch", "placeholder_unfilled",
                 "gate_cannot_read_a", "translation_marker_mismatch", "safety_net_gate_failed",
                 "assembly_check_error"]


class CountingBedrock(R.DryRunBedrock):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self._lock = threading.Lock()

    def converse(self, **kw):
        with self._lock:
            self.calls += 1
        return super().converse(**kw)


class Writer:
    def __init__(self):
        self.items = []

    def put(self, item):
        json.dumps(item)
        assert item["SK"] not in {i["SK"] for i in self.items}
        self.items.append(item)

    def sks(self):
        return [i["SK"] for i in self.items]


class Ctx:
    def __init__(self, ms=300_000):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _event(notes=NOTES, started_at=None):
    started_at = started_at or _now_iso()
    return {"job_id": JOB_ID, "user_sub": USER_SUB, "notes": notes,
            "input_sha256": hashlib.sha256(notes.encode()).hexdigest(), "started_at": started_at}


def _seed_pending(fake_ddb, **extra):
    fake_ddb.items[(AUDIT, f"USER#{USER_SUB}", f"GEN#{JOB_ID}")] = {
        "PK": {"S": f"USER#{USER_SUB}"}, "SK": {"S": f"GEN#{JOB_ID}"},
        "status": {"S": "pending"}, "user_sub": {"S": USER_SUB},
        "started_at": {"S": "2026-11-05T10:00:00+00:00"}, "draft": {"BOOL": True},
        "input_sha256": {"S": hashlib.sha256(NOTES.encode()).hexdigest()}, **extra}


def _row(fake_ddb):
    return fake_ddb.items[(AUDIT, f"USER#{USER_SUB}", f"GEN#{JOB_ID}")]


def _res(fake_ddb):
    return fake_ddb.items.get((RESULTS, f"USER#{USER_SUB}", f"RES#{JOB_ID}"))


@pytest.fixture
def worker(load_worker, monkeypatch, fake_ddb):
    def _load(writer=None, bedrock=None, extra_env=None):
        app = load_worker(extra_env=extra_env)
        b = bedrock or CountingBedrock()
        monkeypatch.setattr(app, "_BEDROCK", {90: b, 60: b})
        w = writer if writer is not None else Writer()
        monkeypatch.setattr(app, "_make_trace_writer", lambda: w)
        return app, b, w
    return _load


def _fail_8b(monkeypatch, code):
    from pipeline import registry
    s = registry.STEPS["assemble_patient"]

    def raiser(inp):
        from pipeline import StepError
        raise StepError(code)
    monkeypatch.setitem(registry.STEPS, "assemble_patient", registry.Step(s.name, s.seq, s.kind, raiser))


def _status(load_status):
    status_app = load_status()
    evt = http_api_event(USER_SUB, method="GET", path=f"/generations/{JOB_ID}",
                         path_params={"id": JOB_ID})
    return json.loads(status_app.lambda_handler(evt, None)["body"])


# --- the complete path ----------------------------------------------------------------

def test_pipeline_run_releases_documents_and_flips_the_row(worker, fake_ddb):
    _seed_pending(fake_ddb)
    app, b, w = worker()
    result = app.lambda_handler(_event(), Ctx())
    assert result == {"ok": True, "job_id": JOB_ID, "status": "complete"}

    row, res = _row(fake_ddb), _res(fake_ddb)
    assert row["status"]["S"] == "complete" and row["parse_ok"]["BOOL"] is True
    assert row["safety_net_gate"]["S"] == "pass" and row["model_calls"]["N"] == "4"
    assert row["pipeline_version"]["S"] == app.orchestrate.pipeline_version()
    assert "anthropic.claude-sonnet-4-6" in row["model_version"]["S"]
    assert not any(k.startswith("patient_") for k in row)           # stop being written (ADR-009)
    assert "error_code" not in row and "failed_step" not in row
    # 8b's hashes, under the keys the status endpoint already serves, match the stored text
    hashes = {k: v["S"] for k, v in row["output_sha256"]["M"].items()}
    assert set(hashes) == {"summary", "gp_letter", "patient"}
    for key in hashes:
        assert hashes[key] == hashlib.sha256(res[key]["S"].encode("utf-8")).hexdigest()
    assert res["summary"]["S"].startswith("PART A") and res["gp_letter"]["S"].startswith("PART B")
    assert "DRAFT FOR CLINICIAN REVIEW" in res["patient"]["S"]      # PART C stays a flagged draft
    assert set(json.loads(res["units_json"]["S"])) == {"facts", "evidence", "flags", "resus",
                                                        "safety_net", "reconciliation"}
    assert set(json.loads(res["templates_json"]["S"])) == {"composition", "patient"}
    assert res["generated_on"]["S"] == "2026-11-05" and int(res["ttl"]["N"]) > 0
    assert w.sks()[-2:] == ["META", "DELIVERY"]
    assert w.items[-1]["results"] == "written" and w.items[-1]["audit"] == "complete"


def test_tokens_are_written_null_when_any_usage_is_unknown(worker, fake_ddb, monkeypatch):
    """R5: the dry fake reports 0/0 usage; a throttled request has none -> NULL totals."""
    _seed_pending(fake_ddb)

    class Throttles(CountingBedrock):
        first = True

        def converse(self, **kw):
            if Throttles.first:
                Throttles.first = False
                raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "Converse")
            return super().converse(**kw)

    app, b, _ = worker(bedrock=Throttles())
    orig = app.orchestrate.run

    def fast(*a, **k):        # no real 20-30 s throttle wait in a unit test
        return orig(*a, **{**k, "sleep": lambda s: None, "jitter": lambda x, y: 0.0})
    monkeypatch.setattr(app.orchestrate, "run", fast)
    assert app.lambda_handler(_event(), Ctx())["status"] == "complete"
    row = _row(fake_ddb)
    assert row["model_calls"]["N"] == "5"
    assert row["input_tokens"] == {"NULL": True} and row["output_tokens"] == {"NULL": True}


def test_status_endpoint_serves_the_released_documents(worker, fake_ddb, load_status):
    _seed_pending(fake_ddb)
    app, _, _ = worker()
    app.lambda_handler(_event(), Ctx())
    body = _status(load_status)
    assert body["status"] == "complete"
    assert set(body["outputs"]) == {"summary", "gp_letter", "patient"}
    assert "units_json" not in json.dumps(body)                    # W10 units are not served


def test_generated_on_is_the_london_date_of_the_dispatch(worker, fake_ddb):
    app, _, _ = worker()
    assert app._generated_on("2026-06-30T23:30:00+00:00", 0) == "2026-07-01"    # BST
    assert app._generated_on("2026-01-15T23:30:00+00:00", 0) == "2026-01-15"    # GMT
    assert app._generated_on("not-a-date", 1_762_300_000) == "2025-11-04"       # worker start
    _seed_pending(fake_ddb)
    started = _now_iso()
    app.lambda_handler(_event(started_at=started), Ctx())
    assert _res(fake_ddb)["generated_on"]["S"] == app._generated_on(started, 0)


# --- the claim (R2) ----------------------------------------------------------------------

def test_a_duplicate_delivery_exits_before_any_model_call(worker, fake_ddb):
    _seed_pending(fake_ddb, pipeline_version={"S": "another-worker"})
    app, b, w = worker()
    result = app.lambda_handler(_event(), Ctx())
    assert result["status"] == "not_claimed" and b.calls == 0 and w.items == []
    assert _row(fake_ddb)["status"]["S"] == "pending" and "error_code" not in _row(fake_ddb)
    assert _res(fake_ddb) is None


def test_a_second_delivery_after_completion_changes_nothing(worker, fake_ddb):
    _seed_pending(fake_ddb)
    app, b, _ = worker()
    assert app.lambda_handler(_event(), Ctx())["status"] == "complete"
    before_row, before_res, calls = dict(_row(fake_ddb)), dict(_res(fake_ddb)), b.calls
    second = app.lambda_handler(_event(), Ctx())
    assert second["status"] == "not_claimed" and b.calls == calls
    assert _row(fake_ddb) == before_row and _res(fake_ddb) == before_res


@pytest.mark.parametrize("err", [
    ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "UpdateItem"),
    ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "UpdateItem"),
    EndpointConnectionError(endpoint_url="https://dynamodb"),
])
def test_an_uncertain_claim_stops_model_calls_and_takes_nothing_over(worker, fake_ddb, monkeypatch, err):
    _seed_pending(fake_ddb)
    app, b, w = worker()
    real = fake_ddb.update_item

    def flaky(**kw):
        if "attribute_not_exists(pipeline_version)" in (kw.get("ConditionExpression") or ""):
            raise err
        return real(**kw)
    monkeypatch.setattr(fake_ddb, "update_item", flaky)
    result = app.lambda_handler(_event(), Ctx())
    assert result["status"] == "uncertain" and result["ok"] is False
    assert b.calls == 0 and w.items == []
    assert _row(fake_ddb)["status"]["S"] == "pending"            # not cleared, not marked failed


# --- fail-closed and the release contract (HAZ-19) -------------------------------------

@pytest.mark.parametrize("code", EIGHT_B_CODES)
def test_an_8b_failure_exposes_no_draft_anywhere(worker, fake_ddb, load_status, monkeypatch, caplog, code):
    _seed_pending(fake_ddb)
    app, _, w = worker()
    _fail_8b(monkeypatch, code)
    caplog.set_level(logging.INFO)
    result = app.lambda_handler(_event(), Ctx())

    # the worker's return value: a fixed code only
    assert result == {"ok": False, "job_id": JOB_ID, "status": "failed", "error_code": code}
    # downloads: nothing in ResultsTable
    assert _res(fake_ddb) is None
    # the audit row: failed, stable code, fixed message, no output hashes
    row = _row(fake_ddb)
    assert row["status"]["S"] == "failed" and row["error_code"]["S"] == code
    assert row["error_message"]["S"] == FIXED_MESSAGE and row["failed_step"]["S"] == "assemble_patient"
    assert "output_sha256" not in row and "completed_at" not in row
    assert ("safety_net_gate" in row) == (code == "safety_net_gate_failed")
    if code == "safety_net_gate_failed":
        assert row["safety_net_gate"]["S"] == "fail"
    # polling: no outputs, no output hashes, only the fixed words
    body = _status(load_status)
    assert body["status"] == "failed" and "outputs" not in body and "output_sha256" not in body
    assert body["error_code"] == code and body["error_message"] == FIXED_MESSAGE
    # no document or note text in the status payload, the audit row, the return value or the logs
    eight_a = next(i for i in w.items if i["SK"] == "STEP#08a#assemble_clinical#A1")["output"]
    leaks = [eight_a["part_a"][:40], eight_a["part_b"][:40], "PART A", "PART B", "PART C",
             "DRAFT FOR CLINICIAN REVIEW", "chest pain", "aspirin"]
    for blob in (json.dumps(body), json.dumps(row), json.dumps(result), caplog.text):
        for leak in leaks:
            assert leak not in blob, leak
    # the drafts remain only in the restricted trace
    assert w.items[-1] == {**w.items[-1], "SK": "DELIVERY", "results": "not_written", "audit": "failed"}


@pytest.mark.parametrize("step", ["extract_facts", "reconcile_medications", "compose_clinical", "compose_patient"])
def test_a_model_step_failure_marks_failed_with_its_code(worker, fake_ddb, step):
    from botocore.exceptions import ReadTimeoutError
    from pipeline import compose, compose_patient, extract, reconcile
    tool = {"extract_facts": extract, "reconcile_medications": reconcile,
            "compose_clinical": compose, "compose_patient": compose_patient}[step].tool_config()

    class FailsOne(CountingBedrock):
        def converse(self, **kw):
            if kw["toolConfig"] == tool:
                self.calls += 1
                raise ReadTimeoutError(endpoint_url="https://bedrock")
            return super().converse(**kw)

    _seed_pending(fake_ddb)
    app, _, _ = worker(bedrock=FailsOne())
    result = app.lambda_handler(_event(), Ctx())
    row = _row(fake_ddb)
    assert result["error_code"] == "read_timeout" and row["failed_step"]["S"] == step
    assert row["error_message"]["S"] == FIXED_MESSAGE and _res(fake_ddb) is None
    assert "safety_net_gate" not in row                         # never reached / not the gate


def test_failure_is_marked_before_inflight_requests_are_drained(worker, fake_ddb, monkeypatch):
    """CSO correction 3: the screen sees `failed` while a sibling request is still running."""
    from botocore.exceptions import ReadTimeoutError
    from pipeline import compose, reconcile
    release, five_a_in = threading.Event(), threading.Event()
    seen = {}

    class Sibling(CountingBedrock):
        def converse(self, **kw):
            if kw["toolConfig"] == compose.tool_config():
                # Schedule (d313 review, 19:46): fail step 6 only once 5a is in its request.
                if not five_a_in.wait(5):
                    raise AssertionError("schedule not established: 5a in its request")
                raise ReadTimeoutError(endpoint_url="x")
            if kw["toolConfig"] == reconcile.tool_config():
                five_a_in.set()
                if not release.wait(5):
                    raise AssertionError("never released")
                seen["status_when_5a_returned"] = _row(fake_ddb)["status"]["S"]
            return super().converse(**kw)

    _seed_pending(fake_ddb)
    app, _, _ = worker(bedrock=Sibling())
    real = app._mark_failed

    def mark_then_release(*a, **k):
        r = real(*a, **k)
        release.set()
        return r
    monkeypatch.setattr(app, "_mark_failed", mark_then_release)
    app.lambda_handler(_event(), Ctx())
    assert seen["status_when_5a_returned"] == "failed"
    assert _row(fake_ddb)["input_tokens"] == {"NULL": True}        # 5a was in flight at marking


def test_no_trace_store_fails_closed_before_any_model_call(load_worker, monkeypatch, fake_ddb):
    _seed_pending(fake_ddb)
    app = load_worker()
    b = CountingBedrock()
    monkeypatch.setattr(app, "_BEDROCK", {90: b, 60: b})
    result = app.lambda_handler(_event(), Ctx())             # item 6 not built: no writer
    assert result["error_code"] == "config_error" and b.calls == 0
    assert _row(fake_ddb)["error_code"]["S"] == "config_error"
    assert _row(fake_ddb)["error_message"]["S"] == FIXED_MESSAGE


def test_a_dispatch_already_past_its_deadline_sends_no_request(worker, fake_ddb):
    _seed_pending(fake_ddb)
    app, b, _ = worker()
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()   # 70 s left < 90 + 10
    result = app.lambda_handler(_event(started_at=old), Ctx())
    assert result["error_code"] == "deadline_reached" and b.calls == 0
    assert _row(fake_ddb)["failed_step"]["S"] == "extract_facts"


def test_lambda_remaining_time_bounds_the_deadline(worker, fake_ddb):
    _seed_pending(fake_ddb)
    app, b, _ = worker()
    result = app.lambda_handler(_event(started_at="not-a-date"), Ctx(ms=(30 + 90 + 5) * 1000))
    assert result["error_code"] == "deadline_reached" and b.calls == 0


# --- ResultsTable: conditional, immutable --------------------------------------------

def test_an_existing_result_is_never_overwritten(worker, fake_ddb, load_status):
    _seed_pending(fake_ddb)
    original = {"PK": {"S": f"USER#{USER_SUB}"}, "SK": {"S": f"RES#{JOB_ID}"}, "summary": {"S": "earlier"},
                "gp_letter": {"S": "earlier"}, "patient": {"S": "earlier"}}
    fake_ddb.items[(RESULTS, f"USER#{USER_SUB}", f"RES#{JOB_ID}")] = dict(original)
    app, _, w = worker()
    result = app.lambda_handler(_event(), Ctx())
    assert result["error_code"] == "results_exists"
    assert _res(fake_ddb) == original
    assert _row(fake_ddb)["status"]["S"] == "failed" and _row(fake_ddb)["failed_step"]["S"] == "results"
    assert "outputs" not in _status(load_status)
    assert w.items[-1]["results"] == "results_exists"


def test_a_results_write_error_fails_the_job(worker, fake_ddb, monkeypatch, load_status):
    _seed_pending(fake_ddb)
    app, _, _ = worker()

    def boom(**kw):
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "PutItem")
    monkeypatch.setattr(fake_ddb, "put_item", boom)
    result = app.lambda_handler(_event(), Ctx())
    assert result["error_code"] == "results_write_error"
    assert "outputs" not in _status(load_status)


def test_results_written_but_audit_flip_failed_serves_nothing(worker, fake_ddb, monkeypatch, load_status):
    _seed_pending(fake_ddb)
    app, _, w = worker()
    real = fake_ddb.update_item

    def flip_fails(**kw):
        if ":complete" in (kw.get("ExpressionAttributeValues") or {}):
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "UpdateItem")
        return real(**kw)
    monkeypatch.setattr(fake_ddb, "update_item", flip_fails)
    result = app.lambda_handler(_event(), Ctx())
    assert result["status"] == "audit_update_error" and _res(fake_ddb) is not None
    body = _status(load_status)
    assert body["status"] == "pending" and "outputs" not in body     # documented residual
    assert w.items[-1]["audit"] == "audit_update_error"


# --- other events, configuration --------------------------------------------------------

def test_http_api_event_returns_410(worker):
    app, _, _ = worker()
    result = app.lambda_handler(http_api_event(USER_SUB, body={"notes": "x"}), None)
    assert result["statusCode"] == 410 and json.loads(result["body"])["error"] == "deprecated_path"


def test_direct_invoke_is_no_longer_served(worker, fake_ddb):
    app, b, _ = worker()
    assert app.lambda_handler({"user_sub": USER_SUB, "notes": "x"}, None) == {
        "ok": False, "error": "unsupported_event"}
    assert b.calls == 0 and fake_ddb.put_calls == []


def test_bedrock_clients_send_exactly_one_request(load_worker, monkeypatch):
    app = load_worker()
    import boto3
    seen = []
    monkeypatch.setattr(boto3, "client", lambda name, region_name=None, config=None: seen.append(config))
    app._bedrock_client(90)
    assert seen[0].retries == {"mode": "standard", "total_max_attempts": 1}
    assert seen[0].read_timeout == 90 and seen[0].connect_timeout == 10
    assert sorted(app._BEDROCK) == [60, 90]


def test_template_timeout_covers_the_deadline_and_reserve(load_worker):
    app = load_worker()
    text = (ROOT / "infra" / "template.yaml").read_text()
    block = text.split("  GenerateFunction:\n", 1)[1].split("\n  GenerateFunctionAsyncConfig:", 1)[0]
    timeout = int(re.search(r"\n      Timeout: (\d+)", block).group(1))
    assert timeout == 300
    assert timeout >= app.orchestrate.DEADLINE_S + app.orchestrate.LAMBDA_RESERVE_S


def test_audit_attributes_written_are_on_the_role_whitelist(worker, fake_ddb, monkeypatch):
    """Every attribute the worker writes to the audit row must be in the role's
    dynamodb:Attributes whitelist, or the W11 worker is denied at the flip."""
    text = (ROOT / "infra" / "template.yaml").read_text()
    block = text.split("Sid: StatusTransitionUpdateWhitelisted", 1)[1].split("StringEqualsIfExists", 1)[0]
    allowed = set(re.findall(r"\n\s+- (\w+)", block))
    _seed_pending(fake_ddb)
    app, _, _ = worker()
    app.lambda_handler(_event(), Ctx())
    _fail_8b(monkeypatch, "safety_net_gate_failed")
    fake_ddb.items.pop((AUDIT, f"USER#{USER_SUB}", f"GEN#{JOB_ID}"))
    fake_ddb.items.pop((RESULTS, f"USER#{USER_SUB}", f"RES#{JOB_ID}"))
    _seed_pending(fake_ddb)
    app.lambda_handler(_event(), Ctx())
    written = set()
    for call in fake_ddb.update_calls:
        expr = call["UpdateExpression"]
        written |= {a.strip() for a in re.findall(r"(?:SET |, )\s*([#\w]+) =", expr)}
        written |= set(re.findall(r"attribute_not_exists\((\w+)\)", call["ConditionExpression"] or ""))
    written = {"status" if a == "#st" else a for a in written}
    assert written and written <= allowed, written - allowed


# --- CSO review 5 Nov 2026 19:36: input provenance ----------------------------------------

@pytest.mark.parametrize("bad", ["0" * 64, None])
def test_a_mismatched_or_missing_input_hash_is_rejected_before_any_model_call(worker, fake_ddb, bad):
    _seed_pending(fake_ddb)
    app, b, w = worker()
    evt = _event()
    if bad is None:
        evt.pop("input_sha256")
    else:
        evt["input_sha256"] = bad
    result = app.lambda_handler(evt, Ctx())
    assert result["error_code"] == "input_hash_mismatch" and b.calls == 0 and w.items == []
    row = _row(fake_ddb)
    assert row["status"]["S"] == "failed" and row["error_code"]["S"] == "input_hash_mismatch"
    assert row["error_message"]["S"] == FIXED_MESSAGE and _res(fake_ddb) is None


def test_a_duplicate_with_a_bad_hash_still_exits_without_marking(worker, fake_ddb):
    _seed_pending(fake_ddb, pipeline_version={"S": "another-worker"})
    app, b, _ = worker()
    evt = _event()
    evt["input_sha256"] = "0" * 64
    assert app.lambda_handler(evt, Ctx())["status"] == "not_claimed"
    assert _row(fake_ddb)["status"]["S"] == "pending" and b.calls == 0
