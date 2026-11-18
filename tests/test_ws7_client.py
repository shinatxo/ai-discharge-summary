"""evals/ws7_client.py - the WS7 client wrapper (ADR-011 D7).

Two kinds of test:

- AT THE SERVER. The real runners, the real botocore client and transport, and a
  local stub that counts the requests it receives (tests/ws7_stub.py). This is
  ADR-011's acceptance check for slice 2: exactly one request per model step on
  a throttle, a 500 and a read timeout, for both runners; and a stub reporting
  inflated input usage shows the guard refusing calls once the reservation is
  reached.
- THE RULES. The guard against a strict in-process client, at each boundary.

No AWS and no spend: `offline_aws` lets a socket connect only to the stub's own
address. Every reply, usage figure and request text here is invented. Runners
write to a temporary folder, never to the repository.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import run_cold_eval as rce  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402
import ws7_client as wc  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
import ws7_stub as stubs  # noqa: E402
from pipeline import StepError, extract  # noqa: E402
from ws7_stub import ERROR_500, STALL, THROTTLE, offline_aws  # noqa: E402,F401  (a fixture)

MARGIN = 100
# What the runners are told the time is: a Wednesday at 04:00 London, the scheduled
# slot, clear of both canary windows. Injected; no test reads the real date.
QUIET = rce.dt.datetime(2026, 12, 16, 4, 0, tzinfo=rce.LONDON)
READ_TIMEOUT_S = 5.0          # every stub test: a reply that never comes fails in seconds
STALL_READ_TIMEOUT_S = 0.5    # the read-timeout tests
RUN_LIMIT_S = 60.0            # a run that takes longer than this has waited on something

PIPELINE = ("extract_facts", "reconcile_medications", "compose_clinical", "compose_patient")
V1 = ("combined", "patient_v2")
# The steps that still receive a request when one step fails (the runner's own rules:
# 5a and 6 each need only steps 1-3b; 7 needs 6; v1's second pass needs the combined call).
PIPELINE_REACHED = {"extract_facts": {"extract_facts"},
                    "reconcile_medications": set(PIPELINE),
                    "compose_clinical": {"extract_facts", "reconcile_medications", "compose_clinical"},
                    "compose_patient": set(PIPELINE)}
V1_REACHED = {"combined": {"combined"}, "patient_v2": set(V1)}
# The code each failure leaves in the record. A throttle is retried by the code under
# test; the wrapper refuses the repeat, and the refusal is what the record shows.
PIPELINE_CODE = {THROTTLE: wc.REPEAT_REFUSED, ERROR_500: "bedrock_error", STALL: "read_timeout"}
V1_CODE = {THROTTLE: "Ws7Refused", ERROR_500: "InternalServerException", STALL: "ReadTimeoutError"}
LABEL = {THROTTLE: "throttled", ERROR_500: "server_error", STALL: "read_timeout"}


def _never(_prompt):
    raise AssertionError("a WS7 test must never be asked to confirm a live run")


def worst(target: str, step: str) -> int:
    return reserve.worst_case_nanos({s.id: s for s in reserve.plan(target).steps}[step], MARGIN)


def honest_cost() -> int:
    return reserve.read_usage(stubs.HONEST_USAGE)["cost_usd_nanos"]


def make_guard(target: str, generations: int, **kw) -> wc.Guard:
    kw.setdefault("reservation_nanos",
                  reserve.reservation(target, generations, MARGIN)["total_usd_nanos"])
    kw.setdefault("deadline", time.monotonic() + RUN_LIMIT_S)
    return wc.Guard(reserve.plan(target), margin_percent=MARGIN, **kw)


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def timeouts(monkeypatch):
    """Short read timeouts for the stub tests, set where each runner reads its own."""
    def set_all(seconds: float) -> None:
        monkeypatch.setattr(rpe, "READ_TIMEOUT_S", seconds)
        monkeypatch.setattr(rpe, "READ_TIMEOUT_5A_S", seconds)
        monkeypatch.setattr(reserve, "V1_READ_TIMEOUT_S", seconds)
    set_all(READ_TIMEOUT_S)
    return set_all


@pytest.fixture
def pipeline_stub(offline_aws, timeouts):  # noqa: F811
    stub = stubs.started(stubs.pipeline_stub(), offline_aws)
    yield stub
    stub.stop()


@pytest.fixture
def v1_stub(offline_aws, timeouts):  # noqa: F811
    stub = stubs.started(stubs.v1_stub(), offline_aws)
    yield stub
    stub.stop()


class Run:
    """One runner call against a stub, in a temporary runs folder."""

    def __init__(self, target, stub, tmp_path, scenarios, guard=None):
        self.target, self.tmp_path = target, tmp_path
        self.guard = guard or make_guard(target, len(scenarios))
        self.sleeps: list = []
        runner, extra = (rpe, []) if target == "pipeline" else (rce, ["--deployed-path"])
        started = time.monotonic()
        self.guard.begin_batch("b", len(scenarios))
        try:
            self.exit_code = runner.main(
                [*scenarios, *extra, "--batch", "b", "--yes"],
                client_factory=wc.client_factory(self.guard, endpoint_url=stub.url),
                now=lambda: QUIET, runs_dir=tmp_path, sleep=self.sleeps.append, confirm=_never)
        finally:
            self.guard.end_batch()
        assert time.monotonic() - started < RUN_LIMIT_S, "the run waited on something"
        self.report = self.guard.report()

    def record(self, scenario: str) -> dict:
        return json.loads((self.tmp_path / "b" / "r1" / f"{scenario}.json").read_text("utf-8"))

    def outcomes(self) -> list:
        return [(c["step"], c["outcome"], c["code"]) for c in self.report["calls"]]


# =========================================================== AT THE SERVER ==

# ------------------------------------------------- one request per model step

def test_pipeline_a_clean_generation_is_one_request_per_step(pipeline_stub, tmp_path):
    run = Run("pipeline", pipeline_stub, tmp_path, ["S1"])
    assert pipeline_stub.counts() == {step: 1 for step in PIPELINE}
    assert run.record("S1")["step_errors"] == {} and run.exit_code == 0
    assert (run.report["calls_sent"], run.report["responses"], run.report["no_response"]) == (4, 4, 0)
    assert run.report["refused"] == {} and run.sleeps == []
    assert run.report["total_usd_nanos"] == 4 * honest_cost()
    assert run.report["worst_case_part_usd_nanos"] == 0


@pytest.mark.parametrize("kind", [THROTTLE, ERROR_500, STALL])
@pytest.mark.parametrize("step", PIPELINE)
def test_pipeline_a_failing_step_receives_exactly_one_request(pipeline_stub, timeouts, tmp_path,
                                                             step, kind):
    """ADR-011's acceptance check, pipeline runner. Every request for `step` fails,
    so a retry of any kind would be a second request at the server."""
    if kind == STALL:
        timeouts(STALL_READ_TIMEOUT_S)
    pipeline_stub.always(step, kind)
    run = Run("pipeline", pipeline_stub, tmp_path, ["S1"])

    assert pipeline_stub.count(step) == 1
    assert pipeline_stub.counts() == {s: 1 for s in PIPELINE if s in PIPELINE_REACHED[step]}
    assert run.record("S1")["step_errors"][step] == PIPELINE_CODE[kind]
    assert (step, LABEL[kind], None) in run.outcomes()
    assert run.report["no_response"] == 1
    # The failed call is counted at its worst case; the others at what they reported.
    assert run.report["worst_case_part_usd_nanos"] == worst("pipeline", step)
    assert run.report["total_usd_nanos"] == (worst("pipeline", step)
                                            + (len(PIPELINE_REACHED[step]) - 1) * honest_cost())
    if kind == THROTTLE:
        # The code under test asked to wait and then tried again; the repeat was refused.
        assert run.report["refused"] == {wc.REPEAT_REFUSED: 1}
        assert len(run.sleeps) == 1 and 20.0 <= run.sleeps[0] <= 30.0
    else:
        assert run.report["refused"] == {} and run.sleeps == []


def test_v1_a_clean_generation_is_one_request_per_step(v1_stub, tmp_path):
    run = Run("v1", v1_stub, tmp_path, ["S1"])
    assert v1_stub.counts() == {step: 1 for step in V1}
    rec = run.record("S1")
    assert rec["error"] is None and rec["patient_version"] == "v2"
    assert [c["name"] for c in rec["calls"]] == list(V1)
    assert (run.report["calls_sent"], run.report["responses"], run.report["no_response"]) == (2, 2, 0)
    assert run.report["refused"] == {} and run.sleeps == []


@pytest.mark.parametrize("kind", [THROTTLE, ERROR_500, STALL])
@pytest.mark.parametrize("step", V1)
def test_v1_a_failing_step_receives_exactly_one_request(v1_stub, timeouts, tmp_path, step, kind):
    """ADR-011's acceptance check, v1 runner."""
    if kind == STALL:
        timeouts(STALL_READ_TIMEOUT_S)
    v1_stub.always(step, kind)
    run = Run("v1", v1_stub, tmp_path, ["S1"])

    assert v1_stub.count(step) == 1
    assert v1_stub.counts() == {s: 1 for s in V1 if s in V1_REACHED[step]}
    error = run.record("S1")["error"]
    assert error["code"] == V1_CODE[kind]
    assert (step, LABEL[kind], None) in run.outcomes()
    assert run.report["no_response"] == 1
    assert run.report["worst_case_part_usd_nanos"] == worst("v1", step)
    if kind == THROTTLE:
        assert error["message"] == wc.REPEAT_REFUSED
        assert run.report["refused"] == {wc.REPEAT_REFUSED: 1}
        assert len(run.sleeps) == 1 and 20.0 <= run.sleeps[0] <= 30.0
    else:
        assert run.report["refused"] == {} and run.sleeps == []


# ------------------------------------- a refused repeat ends ONE generation

def test_pipeline_after_a_refused_repeat_the_next_generation_still_runs(pipeline_stub, tmp_path):
    pipeline_stub.first("extract_facts", THROTTLE)          # the first request only
    run = Run("pipeline", pipeline_stub, tmp_path, ["S1", "S2"])
    # One step-2 request per generation: S1's (throttled) and S2's. S1's repeat never left.
    assert pipeline_stub.counts() == {"extract_facts": 2, "reconcile_medications": 1,
                                      "compose_clinical": 1, "compose_patient": 1}
    assert run.record("S1")["step_errors"] == {"extract_facts": wc.REPEAT_REFUSED}
    assert run.record("S2")["step_errors"] == {}
    assert run.report["batches"]["b"]["started"] == 2
    assert run.report["refused"] == {wc.REPEAT_REFUSED: 1}


def test_v1_after_a_refused_repeat_the_next_generation_still_runs(v1_stub, tmp_path):
    v1_stub.first("combined", THROTTLE)
    run = Run("v1", v1_stub, tmp_path, ["S1", "S2"])
    assert v1_stub.counts() == {"combined": 2, "patient_v2": 1}
    assert run.record("S1")["error"] == {"code": "Ws7Refused", "message": wc.REPEAT_REFUSED}
    assert run.record("S2")["error"] is None
    assert run.report["batches"]["b"]["started"] == 2


# -------------------------------------------------------- the spend guard

def _inflated_input(target: str, generations: int) -> dict:
    """Invented usage: one call's input alone costs more than the whole reservation."""
    need = reserve.reservation(target, generations, MARGIN)["total_usd_nanos"]
    return {"inputTokens": need // reserve.rates()["input"] + 1, "outputTokens": 200}


def test_pipeline_inflated_input_usage_stops_every_later_call(pipeline_stub, tmp_path):
    """ADR-011's acceptance check: once the reservation is reached, no call leaves."""
    pipeline_stub.usage["extract_facts"] = _inflated_input("pipeline", 3)
    run = Run("pipeline", pipeline_stub, tmp_path, ["S1", "S2", "S3"])

    assert pipeline_stub.counts() == {"extract_facts": 1}          # nothing after the first reply
    assert run.report["tripped"] is True
    assert run.report["total_usd_nanos"] >= run.report["reservation_usd_nanos"]
    assert run.report["refused"] == {wc.SPEND_GUARD: 4}            # S1's 5a and 6; S2's and S3's step 2
    assert run.record("S1")["step_errors"] == {"reconcile_medications": wc.SPEND_GUARD,
                                               "compose_clinical": wc.SPEND_GUARD,
                                               "compose_patient": rpe.UPSTREAM_FAILED}
    for scenario in ("S2", "S3"):
        assert run.record(scenario)["step_errors"] == {"extract_facts": wc.SPEND_GUARD}


def test_pipeline_honest_usage_does_not_trip_the_guard(pipeline_stub, tmp_path):
    run = Run("pipeline", pipeline_stub, tmp_path, ["S1", "S2", "S3"])
    assert pipeline_stub.counts() == {step: 3 for step in PIPELINE}
    assert run.report["tripped"] is False and run.report["refused"] == {}
    assert run.report["total_usd_nanos"] == 12 * honest_cost() < run.report["reservation_usd_nanos"]


def test_v1_inflated_input_usage_stops_every_later_call(v1_stub, tmp_path):
    v1_stub.usage["combined"] = _inflated_input("v1", 3)
    run = Run("v1", v1_stub, tmp_path, ["S1", "S2", "S3"])
    assert v1_stub.counts() == {"combined": 1}
    assert run.report["tripped"] is True
    assert run.report["refused"] == {wc.SPEND_GUARD: 3}            # S1's second pass; S2's and S3's call
    for scenario in ("S1", "S2", "S3"):
        assert run.record(scenario)["error"] == {"code": "Ws7Refused", "message": wc.SPEND_GUARD}


def test_v1_honest_usage_does_not_trip_the_guard(v1_stub, tmp_path):
    run = Run("v1", v1_stub, tmp_path, ["S1", "S2", "S3"])
    assert v1_stub.counts() == {step: 3 for step in V1}
    assert run.report["tripped"] is False and run.report["refused"] == {}


# ----------------------------------------- a request that is not the plan's

def test_a_step_that_asks_for_more_output_than_reserved_never_reaches_the_server(
        pipeline_stub, tmp_path, monkeypatch):
    """The reservation is built from each step's maxTokens. A step that then sends a
    different figure is refused. (The clean test above is the partner.)"""
    guard = make_guard("pipeline", 1)                       # the plan is fixed here
    monkeypatch.setattr(extract, "MAX_TOKENS", extract.MAX_TOKENS + 1)   # ...and then the step changes
    run = Run("pipeline", pipeline_stub, tmp_path, ["S1"], guard=guard)
    assert pipeline_stub.counts() == {}
    assert run.record("S1")["step_errors"] == {"extract_facts": wc.MAX_TOKENS_DIFFERS}
    assert run.report["refused"] == {wc.MAX_TOKENS_DIFFERS: 1} and run.report["calls_sent"] == 0


# ------------------------------------------------------ nothing but numbers

def test_the_call_log_holds_no_model_output(v1_stub, tmp_path):
    run = Run("v1", v1_stub, tmp_path, ["S1"])
    assert stubs.SENTINEL in run.record("S1")["output"]     # the control: the reply did carry it
    assert stubs.SENTINEL not in json.dumps(run.report)


# ------------------------------------------------- the tests' own safety

def test_the_offline_fixture_refuses_any_address_but_the_stubs(pipeline_stub, offline_aws):  # noqa: F811
    # The control: the stub's own address connects.
    socket.create_connection(pipeline_stub.address, timeout=READ_TIMEOUT_S).close()
    # A documentation address (192.0.2.0/24 is never routed): refused before a packet leaves.
    with pytest.raises(AssertionError):
        socket.create_connection(("192.0.2.1", 443), timeout=READ_TIMEOUT_S)
    # Another loopback port is refused too: the rule is the stub's address, not "local".
    other = socket.socket()
    try:
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        with pytest.raises(AssertionError):
            socket.create_connection(other.getsockname(), timeout=READ_TIMEOUT_S)
        probe = socket.socket()
        with pytest.raises(AssertionError):
            probe.connect_ex(other.getsockname())
        probe.close()
    finally:
        other.close()
    assert len(offline_aws.refused) == 3
    offline_aws.refused.clear()          # expected here; anywhere else they fail the test


def test_the_offline_fixture_resolves_no_name_but_the_stubs(pipeline_stub, offline_aws):  # noqa: F811
    host, port = pipeline_stub.address
    assert socket.getaddrinfo(host, port)                    # the control: the stub's host resolves
    for name in ("bedrock-runtime.eu-west-2.amazonaws.com", "localhost"):
        with pytest.raises(AssertionError):
            socket.getaddrinfo(name, 443)
    assert [r[0] for r in offline_aws.refused] == ["bedrock-runtime.eu-west-2.amazonaws.com",
                                                   "localhost"]
    offline_aws.refused.clear()


def test_the_offline_fixture_gives_botocore_only_invented_credentials(offline_aws):  # noqa: F811
    import boto3
    creds = boto3.Session(region_name=rce.REGION).get_credentials()
    assert (creds.access_key, creds.method) == ("ws7-test-invented-id", "env")


# ================================================================ THE RULES ==

class Inner:
    """A strict stand-in for the client beneath the wrapper: it answers `converse`
    from a queue and has nothing else. An unexpected call fails the test."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.requests: list = []

    def converse(self, **request):
        assert self._replies, "the wrapper sent a call the test did not expect"
        self.requests.append(request)
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def reply(**usage) -> dict:
    return {"output": {"message": {"role": "assistant", "content": [{"text": "invented"}]}},
            "stopReason": "end_turn", "usage": usage or dict(stubs.HONEST_USAGE)}


def request(target: str, step: str, text: str = "invented notes", **change) -> dict:
    spec = {s.id: s for s in reserve.plan(target).steps}[step]
    req = {"modelId": rce.MODEL_ID, "system": [{"text": "invented prompt"}],
           "messages": [{"role": "user", "content": [{"text": text}]}],
           "inferenceConfig": {"maxTokens": spec.max_tokens, "temperature": 0.0}}
    if spec.tool_name:
        req["toolConfig"] = {"tools": [], "toolChoice": {"tool": {"name": spec.tool_name}}}
    req.update(change)
    return req


def wrapped(target: str, *replies, generations: int = 2, **kw):
    kw.setdefault("deadline", 1000.0)
    kw.setdefault("clock", lambda: 0.0)
    guard = make_guard(target, generations, **kw)
    guard.begin_batch("b", generations)
    inner = Inner(*replies)
    return wc.GuardedClient(inner, guard), inner, guard


def refused(client, req) -> str:
    with pytest.raises(wc.Ws7Refused) as exc:
        client.converse(**req)
    assert isinstance(exc.value, StepError) and exc.value.code in wc.CODES
    return exc.value.code


# ---------------------------------------------------------- the spend guard

def test_the_guard_refuses_once_the_total_equals_the_reservation():
    client, inner, guard = wrapped("pipeline", reply(), reservation_nanos=honest_cost())
    client.converse(**request("pipeline", "extract_facts"))
    assert guard.total_nanos == guard.reservation_nanos
    assert refused(client, request("pipeline", "reconcile_medications")) == wc.SPEND_GUARD
    assert len(inner.requests) == 1 and guard.tripped is True


def test_the_guard_allows_a_call_one_nano_dollar_below_the_reservation():
    client, inner, guard = wrapped("pipeline", reply(), reply(), reservation_nanos=honest_cost() + 1)
    client.converse(**request("pipeline", "extract_facts"))
    assert guard.total_nanos == guard.reservation_nanos - 1
    client.converse(**request("pipeline", "reconcile_medications"))
    assert len(inner.requests) == 2 and guard.tripped is False


def test_the_report_says_when_the_last_call_reached_the_reservation_with_nothing_to_refuse():
    client, _inner, guard = wrapped("pipeline", reply(), reservation_nanos=honest_cost())
    assert guard.report()["reservation_reached"] is False          # the partner: before the call
    client.converse(**request("pipeline", "extract_facts"))
    report = guard.report()
    assert (report["reservation_reached"], report["tripped"]) == (True, False)


def test_once_tripped_every_further_call_is_refused():
    client, inner, guard = wrapped("pipeline", reply(), generations=3,
                                   reservation_nanos=honest_cost())
    client.converse(**request("pipeline", "extract_facts"))
    for step in ("reconcile_medications", "compose_clinical", "compose_patient"):
        assert refused(client, request("pipeline", step)) == wc.SPEND_GUARD
    assert refused(client, request("pipeline", "extract_facts", "another scenario")) == wc.SPEND_GUARD
    assert len(inner.requests) == 1 and guard.report()["refused"] == {wc.SPEND_GUARD: 4}


@pytest.mark.parametrize("bad", [
    {"output": {}},                                                # no usage at all
    {"usage": None}, {"usage": {}}, {"usage": {"inputTokens": 5}},
    {"usage": {"inputTokens": True, "outputTokens": 1}},
    {"usage": {"inputTokens": -1, "outputTokens": 1}},
    {"usage": {"inputTokens": 1.5, "outputTokens": 1}},
    "not an object", None,
])
def test_a_reply_with_no_usable_usage_is_counted_at_its_worst_case(bad):
    client, _inner, guard = wrapped("pipeline", bad)
    assert client.converse(**request("pipeline", "extract_facts")) is bad    # passed on untouched
    assert guard.total_nanos == worst("pipeline", "extract_facts") > 0
    row = guard.report()["calls"][0]
    assert (row["outcome"], row["worst_case"]) == (wc.USAGE_UNKNOWN, True)
    assert guard.report()["usage_unknown"] == 1


def test_a_reply_with_usage_is_counted_at_what_it_reported():
    client, _inner, guard = wrapped("pipeline", reply(inputTokens=7, outputTokens=3,
                                                      cacheReadInputTokens=11,
                                                      cacheWriteInputTokens=5))
    client.converse(**request("pipeline", "extract_facts"))
    assert guard.total_nanos == 7 * 3300 + 3 * 16500 + 11 * 330 + 5 * 4125
    row = guard.report()["calls"][0]
    assert (row["outcome"], row["worst_case"]) == (wc.RESPONSE, False)
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"],
            row["cache_write_tokens"]) == (7, 3, 11, 5)
    assert guard.report()["usage_unknown"] == 0 and guard.report()["worst_case_part_usd_nanos"] == 0


def _client_error(code: str, status: int) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "invented"},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "Converse")


@pytest.mark.parametrize("exc,label", [
    (_client_error("ThrottlingException", 429), "throttled"),
    (_client_error("InternalServerException", 500), "server_error"),
    (_client_error("ServiceUnavailableException", 503), "server_error"),
    (_client_error("ValidationException", 400), "client_error"),
    (ReadTimeoutError(endpoint_url="http://127.0.0.1/"), "read_timeout"),
    (RuntimeError("invented"), "other_error"),
    (KeyboardInterrupt(), "other_error"),
])
def test_a_call_that_raised_is_counted_at_its_worst_case_and_raised_on(exc, label):
    client, inner, guard = wrapped("v1", exc)
    with pytest.raises(type(exc)) as got:
        client.converse(**request("v1", "combined"))
    assert got.value is exc                                  # the code under test sees the real error
    assert guard.total_nanos == worst("v1", "combined")
    report = guard.report()
    assert (report["calls"][0]["outcome"], report["no_response"], report["responses"]) == (label, 1, 0)
    assert len(inner.requests) == 1


def test_a_refused_call_costs_nothing():
    client, _inner, guard = wrapped("pipeline")
    assert refused(client, request("pipeline", "reconcile_medications")) == wc.OUT_OF_ORDER
    assert guard.total_nanos == 0 and guard.report()["calls_sent"] == 0


# --------------------------------------------- one call per step per generation

@pytest.mark.parametrize("target,steps", [("pipeline", PIPELINE), ("v1", V1)])
def test_each_step_may_be_called_once_in_a_generation(target, steps):
    for i, step in enumerate(steps):
        before = [request(target, s) for s in steps[:i + 1]]
        client, inner, _guard = wrapped(target, *[reply() for _ in before])
        for req in before:
            client.converse(**req)
        assert refused(client, request(target, step)) == wc.REPEAT_REFUSED
        assert len(inner.requests) == len(before)            # the repeat was never sent


def test_a_repeat_is_refused_whatever_the_first_call_did():
    for first in (reply(), _client_error("ThrottlingException", 429)):
        client, inner, _guard = wrapped("pipeline", first)
        try:
            client.converse(**request("pipeline", "extract_facts"))
        except ClientError:
            pass
        assert refused(client, request("pipeline", "extract_facts")) == wc.REPEAT_REFUSED
        assert len(inner.requests) == 1


def test_a_different_first_step_request_starts_the_next_generation():
    client, inner, guard = wrapped("pipeline", reply(), reply(), reply(), reply())
    client.converse(**request("pipeline", "extract_facts", "scenario one"))
    client.converse(**request("pipeline", "reconcile_medications", "scenario one"))
    client.converse(**request("pipeline", "extract_facts", "scenario two"))
    client.converse(**request("pipeline", "reconcile_medications", "scenario two"))   # allowed again
    assert len(inner.requests) == 4
    assert [c["generation"] for c in guard.report()["calls"]] == [1, 1, 2, 2]


def test_a_later_step_with_a_changed_body_is_still_a_repeat():
    client, inner, _guard = wrapped("pipeline", reply(), reply())
    client.converse(**request("pipeline", "extract_facts"))
    client.converse(**request("pipeline", "compose_clinical", "first wording"))
    assert refused(client, request("pipeline", "compose_clinical", "second wording")) == wc.REPEAT_REFUSED
    assert len(inner.requests) == 2


def test_no_more_generations_than_the_batch_planned():
    client, inner, guard = wrapped("v1", reply(), reply(), generations=2)
    client.converse(**request("v1", "combined", "scenario one"))
    client.converse(**request("v1", "combined", "scenario two"))        # the partner: the second starts
    assert refused(client, request("v1", "combined", "scenario three")) == wc.PLAN_EXCEEDED
    assert len(inner.requests) == 2 and guard.report()["batches"]["b"]["started"] == 2


def test_a_later_step_before_any_first_step_is_refused():
    client, inner, _guard = wrapped("v1")
    assert refused(client, request("v1", "patient_v2")) == wc.OUT_OF_ORDER
    assert inner.requests == []


def test_each_batch_starts_with_no_generation_open():
    client, inner, guard = wrapped("v1", reply(), reply(), generations=1)
    client.converse(**request("v1", "combined"))
    guard.end_batch()
    guard.begin_batch("b2", 1)
    client.converse(**request("v1", "combined"))        # the same scenario, in the next batch
    assert len(inner.requests) == 2
    assert guard.batch_total_nanos("b") == guard.batch_total_nanos("b2") == honest_cost()
    assert guard.total_nanos == 2 * honest_cost()


# ------------------------------------------------- requests the plan does not know

def test_the_plans_own_requests_are_sent():
    """The partner of every refusal below."""
    for target, steps in (("pipeline", PIPELINE), ("v1", V1)):
        client, inner, _guard = wrapped(target, *[reply() for _ in steps])
        for step in steps:
            client.converse(**request(target, step))
        assert len(inner.requests) == len(steps)


@pytest.mark.parametrize("delta", [1, -1])
def test_a_pipeline_request_whose_max_tokens_is_not_the_reserved_figure_is_refused(delta):
    client, inner, _guard = wrapped("pipeline")
    req = request("pipeline", "extract_facts")
    req["inferenceConfig"]["maxTokens"] += delta
    assert refused(client, req) == wc.MAX_TOKENS_DIFFERS
    assert inner.requests == []


@pytest.mark.parametrize("change", [
    {"toolConfig": {"tools": [], "toolChoice": {"tool": {"name": "record_verification"}}}},   # step 7v
    {"toolConfig": {"tools": []}},
    {"toolConfig": None},
    {"toolConfig": {"toolChoice": {"tool": {"name": 7}}}},
])
def test_a_pipeline_request_for_a_step_not_in_the_plan_is_refused(change):
    client, inner, _guard = wrapped("pipeline")
    assert refused(client, request("pipeline", "extract_facts", **change)) == wc.UNKNOWN_REQUEST
    assert inner.requests == []


@pytest.mark.parametrize("change", [
    {"inferenceConfig": {"maxTokens": 4097}},
    {"inferenceConfig": {"maxTokens": True}},
    {"inferenceConfig": {}},
    {"inferenceConfig": None},
    {"toolConfig": {"toolChoice": {"tool": {"name": "record_facts"}}}},
])
def test_a_v1_request_that_is_neither_of_its_two_calls_is_refused(change):
    client, inner, _guard = wrapped("v1")
    assert refused(client, request("v1", "combined", **change)) == wc.UNKNOWN_REQUEST
    assert inner.requests == []


@pytest.mark.parametrize("target,step", [("pipeline", "extract_facts"), ("v1", "combined")])
def test_another_model_id_is_refused(target, step):
    client, inner, _guard = wrapped(target)
    assert refused(client, request(target, step, modelId="anthropic.invented-model")) == wc.MODEL_DIFFERS
    assert inner.requests == []


def test_a_request_that_cannot_be_hashed_is_refused():
    client, inner, _guard = wrapped("v1")
    assert refused(client, request("v1", "combined", extra=object())) == wc.UNKNOWN_REQUEST
    assert inner.requests == []


def test_the_wrapped_client_offers_converse_and_nothing_else():
    client, _inner, _guard = wrapped("v1")
    assert callable(client.converse)
    for name in ("converse_stream", "invoke_model", "meta", "inner", "__dict__"):
        with pytest.raises(AttributeError):
            getattr(client, name)


# ------------------------------------------------------ deadline and batches

def test_the_deadline_is_checked_before_every_call():
    now = [99.999]
    client, inner, _guard = wrapped("v1", reply(), deadline=100.0, clock=lambda: now[0])
    client.converse(**request("v1", "combined"))            # just before: sent
    now[0] = 100.0
    assert refused(client, request("v1", "patient_v2")) == wc.DEADLINE
    assert len(inner.requests) == 1


def test_no_call_is_sent_outside_a_batch():
    guard = make_guard("v1", 1, deadline=1000.0, clock=lambda: 0.0)
    inner = Inner(reply())
    client = wc.GuardedClient(inner, guard)
    assert refused(client, request("v1", "combined")) == wc.NO_BATCH
    guard.begin_batch("b", 1)
    client.converse(**request("v1", "combined"))            # the partner
    guard.end_batch()
    assert refused(client, request("v1", "patient_v2")) == wc.NO_BATCH
    assert len(inner.requests) == 1


def test_batches_are_opened_one_at_a_time_with_an_unused_id():
    guard = make_guard("v1", 1)
    with pytest.raises(ValueError):
        guard.end_batch()
    for label, generations in (("has space", 1), ("", 1), ("b", 0), ("b", True), (7, 1)):
        with pytest.raises(ValueError):
            guard.begin_batch(label, generations)
    guard.begin_batch("b", 1)
    with pytest.raises(ValueError):
        guard.begin_batch("c", 1)                           # one is open
    guard.end_batch()
    with pytest.raises(ValueError):
        guard.begin_batch("b", 1)                           # used
    guard.begin_batch("c", 1)


@pytest.mark.parametrize("bad", [0, -5, 1.5, True, None])
def test_a_guard_needs_a_reservation_above_zero(bad):
    with pytest.raises(ValueError):
        wc.Guard(reserve.plan("v1"), reservation_nanos=bad, margin_percent=MARGIN, deadline=1.0)


# ------------------------------------------------------------- the call log

def test_the_report_holds_ids_fixed_labels_and_numbers_only():
    note = "INVENTED NOTE TEXT that must not be logged"
    client, inner, guard = wrapped("pipeline", reply(), _client_error("ThrottlingException", 429))
    client.converse(**request("pipeline", "extract_facts", note))
    with pytest.raises(ClientError):
        client.converse(**request("pipeline", "reconcile_medications", note))
    assert refused(client, request("pipeline", "reconcile_medications", note)) == wc.REPEAT_REFUSED
    assert note in json.dumps(inner.requests)               # the control: the requests did carry it

    report = guard.report()
    assert note not in json.dumps(report) and "invented" not in json.dumps(report)
    labels = {None, "b", *PIPELINE, "sent", wc.REFUSED, wc.RESPONSE, wc.USAGE_UNKNOWN,
              *wc.NO_RESPONSE, *wc.CODES}
    for row in report["calls"]:
        assert set(row) == {"seq", "batch", "generation", "step", "outcome", "code",
                            "input_tokens", "output_tokens", "cache_read_tokens",
                            "cache_write_tokens", "cost_usd_nanos", "worst_case", "latency_ms"}
        for value in row.values():
            assert type(value) in (int, bool) or value in labels
    assert [r["seq"] for r in report["calls"]] == [1, 2, 3]
    assert report["total_usd_micros"] == reserve.to_micros(report["total_usd_nanos"])


# ------------------------------------------------------------ the real client

def test_the_client_is_built_for_one_attempt_with_the_runners_timeout(offline_aws):  # noqa: F811
    client = wc.build_client(read_timeout_s=90, connect_timeout_s=10,
                             endpoint_url="http://127.0.0.1:9")
    assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": 1}
    assert (client.meta.config.read_timeout, client.meta.config.connect_timeout) == (90, 10)


def test_the_factory_gives_each_runner_its_own_timeouts(offline_aws):  # noqa: F811
    def config(client):
        return object.__getattribute__(client, "_inner").meta.config

    pipeline = wc.client_factory(make_guard("pipeline", 1), endpoint_url="http://127.0.0.1:9")
    assert (config(pipeline(90)).read_timeout, config(pipeline(60)).read_timeout) == (90, 60)
    assert config(pipeline(90)).connect_timeout == 10
    assert config(pipeline(90)).retries["total_max_attempts"] == 1
    for bad in (None, 0, -1, "90"):
        with pytest.raises(ValueError):
            pipeline(bad)                                    # the pipeline runner must name one

    v1 = wc.client_factory(make_guard("v1", 1), endpoint_url="http://127.0.0.1:9")
    assert (config(v1()).read_timeout, config(v1()).connect_timeout) == (60, 60)
    assert config(v1()).retries == {"mode": "standard", "total_max_attempts": 1}


def test_without_an_endpoint_the_client_is_the_regions_own_and_is_never_called(offline_aws):  # noqa: F811
    """Built, inspected and dropped: what the job will use. No request is made."""
    client = wc.build_client(read_timeout_s=60, connect_timeout_s=60)
    assert client.meta.endpoint_url == "https://bedrock-runtime.eu-west-2.amazonaws.com"
    assert client.meta.region_name == rce.REGION == "eu-west-2"
