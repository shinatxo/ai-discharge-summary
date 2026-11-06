"""Tests for the TraceTable writer (src/generate/trace_store.py; WS1a step build piece 6,
6 Nov 2026) and for the production writer behind the orchestrator. Fake DynamoDB (conftest,
strict about conditions), no AWS, synthetic notes only."""

from __future__ import annotations

import json
import logging
import math
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from conftest import FakeDynamoDB

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

import trace_store as TS  # noqa: E402
from pipeline import orchestrate as O  # noqa: E402
from pipeline import registry  # noqa: E402
from test_pipeline_orchestrate import NOTES, go  # noqa: E402

TABLE = "discharge-eph-test-traces"
PLANTED = "Synthetic-planted-text-XYZZY-7731"


def item(sk="STEP#06#compose_clinical#A1", **extra):
    return {"PK": "GEN#J1", "SK": sk, "ttl": 1_764_892_000, **extra}


class Spy:
    """Records the exact keyword arguments of every put_item; optionally raises."""
    def __init__(self, raises=None):
        self.calls, self.raises = [], raises

    def put_item(self, **kw):
        self.calls.append(kw)
        if self.raises is not None:
            raise self.raises
        return {}


def ccf():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "m"}}, "PutItem")


# --- types --------------------------------------------------------------------------------

def test_plain_json_becomes_typed_dynamodb_with_decimal_never_float():
    typed = TS.to_typed(item(n=3, f=0.25, neg=-1.5, ok=True, none=None, s="ü",
                             l=[1, "a", {"x": 2.5}], m={"k": []}))
    assert typed["n"] == {"N": "3"} and typed["f"] == {"N": "0.25"} and typed["neg"] == {"N": "-1.5"}
    assert typed["ok"] == {"BOOL": True} and typed["none"] == {"NULL": True} and typed["s"] == {"S": "ü"}
    assert typed["l"] == {"L": [{"N": "1"}, {"S": "a"}, {"M": {"x": {"N": "2.5"}}}]}
    assert typed["m"] == {"M": {"k": {"L": []}}}
    assert typed["PK"] == {"S": "GEN#J1"} and typed["ttl"] == {"N": "1764892000"}


@pytest.mark.parametrize("bad", [math.nan, math.inf, {1, 2}, b"bytes", object()])
def test_non_json_values_are_rejected_before_any_request(bad):
    spy = Spy()
    with pytest.raises((TypeError, ValueError)):
        TS.TraceStore(spy, TABLE).put(item(bad=bad))
    assert spy.calls == []


def test_numbers_beyond_dynamodb_precision_are_rejected_not_rounded():
    assert TS.to_typed(item(x=-int("9" * 38)))["x"] == {"N": "-" + "9" * 38}   # 38 digits: accepted
    assert TS.to_typed(item(x=0.1 + 0.2))["x"] == {"N": "0.30000000000000004"}  # a float's 17 digits, exact
    with pytest.raises(Exception) as e:
        TS.to_typed(item(x=int("9" * 39)))                     # 39 digits: rejected
    assert TS.failure_code(e.value) == "serialise_error"


# --- the request ---------------------------------------------------------------------------

def test_put_sends_exactly_table_item_and_attribute_not_exists_sk():
    spy = Spy()
    TS.TraceStore(spy, TABLE).put(item(out={"a": 1}))
    assert len(spy.calls) == 1
    kw = spy.calls[0]
    assert set(kw) == {"TableName", "Item", "ConditionExpression"}     # no ReturnValues*, nothing else
    assert kw["TableName"] == TABLE and kw["ConditionExpression"] == "attribute_not_exists(SK)"
    assert kw["Item"] == TS.to_typed(item(out={"a": 1}))


def test_client_config_is_one_request_with_short_timeouts():
    c = TS.CLIENT_CONFIG
    assert c.retries == {"mode": "standard", "total_max_attempts": 1}
    assert (c.connect_timeout, c.read_timeout) == (3, 5)
    src = (ROOT / "src" / "generate" / "app.py").read_text()
    assert 'boto3.client("dynamodb", region_name=REGION, config=trace_store.CLIENT_CONFIG)' in src


def test_never_overwrites_an_existing_sk():
    fake = FakeDynamoDB()
    w = TS.TraceStore(fake, TABLE)
    w.put(item(v="first"))
    with pytest.raises(ClientError) as e:
        w.put(item(v="second"))
    assert e.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    assert fake.items[(TABLE, "GEN#J1", "STEP#06#compose_clinical#A1")]["v"] == {"S": "first"}


def test_the_fake_rejects_a_condition_it_does_not_implement():
    """Positive control for the test above: the fake can no longer ignore a condition."""
    with pytest.raises(AssertionError):
        FakeDynamoDB().put_item(TableName=TABLE, Item=TS.to_typed(item()),
                                ConditionExpression="attribute_exists(SK)")


def test_a_new_attempt_is_a_new_item():
    fake = FakeDynamoDB()
    w = TS.TraceStore(fake, TABLE)
    w.put(item("STEP#05a#reconcile_medications#A1"))
    w.put(item("STEP#05a#reconcile_medications#A2"))
    assert len(fake.items) == 2


# --- failures: raise, log only fixed identifiers and codes ----------------------------------

@pytest.mark.parametrize("exc,code", [
    (ccf(), "ConditionalCheckFailedException"),
    (ClientError({"Error": {"Code": "ThrottlingException", "Message": PLANTED}}, "PutItem"), "ThrottlingException"),
    (ClientError({"Error": {"Code": "SomethingNew", "Message": PLANTED}}, "PutItem"), "other"),
    (ReadTimeoutError(endpoint_url="https://x"), "read_timeout"),
    (ConnectTimeoutError(endpoint_url="https://x"), "connect_timeout"),
    (RuntimeError(PLANTED), "other"),
])
def test_any_failure_raises_and_logs_only_job_sk_and_a_fixed_code(exc, code, caplog):
    caplog.set_level(logging.ERROR)
    w = TS.TraceStore(Spy(raises=exc), TABLE)
    with pytest.raises(type(exc)):
        w.put(item(output={"text": PLANTED}))
    recs = [json.loads(r.getMessage()) for r in caplog.records if "trace_write_error" in r.getMessage()]
    assert recs == [{"event": "trace_write_error", "job_id": "J1",
                     "sk": "STEP#06#compose_clinical#A1", "code": code}]
    assert code in TS.AWS_CODES or code in TS.LOCAL_CODES
    assert PLANTED not in caplog.text


def test_the_log_check_can_fire(caplog):
    """Positive control: a planted string that IS logged is seen by the check above."""
    caplog.set_level(logging.ERROR)
    TS.logger.error(PLANTED)
    assert PLANTED in caplog.text


def test_unrecognised_identifiers_are_not_logged(caplog):
    caplog.set_level(logging.ERROR)
    w = TS.TraceStore(Spy(raises=ccf()), TABLE)
    with pytest.raises(ClientError):
        w.put({"PK": "GEN#" + PLANTED + " x", "SK": PLANTED, "ttl": 1})
    assert PLANTED not in caplog.text and '"job_id": "invalid"' in caplog.text


# --- the size bound ------------------------------------------------------------------------

def _aws_approx_number(s: str) -> int:
    """AWS's documented approximation: 1 byte per two significant digits + 1 byte."""
    digits = s.lstrip("-").replace(".", "").lstrip("0").rstrip("0") or "0"
    return math.ceil(len(digits) / 2) + 1


@pytest.mark.parametrize("n", ["0", "7", "-7", "1764892000", "-0.000123", "9" * 38, "-" + "9" * 38,
                               "0." + "1" * 38, "123456789.987654321"])
def test_number_charge_is_at_least_aws_documented_approximation(n):
    assert TS._value_size({"N": n}) >= _aws_approx_number(n) + 1     # strictly above it


def test_bound_follows_aws_rules_for_names_strings_containers():
    typed = TS.to_typed({"PK": "GEN#J1", "s": "é€", "e": [], "m": {"k": None, "é": True}})
    expected = (2 + 6) + (1 + 5) + (1 + 3) + (1 + 3 + (1 + 1 + 1) + (2 + 1 + 1))
    assert TS.item_size_bound(typed) == expected       # é = 2 bytes, € = 3 bytes


def test_nested_containers_count_each_level():
    flat = TS.item_size_bound(TS.to_typed({"a": [1]}))
    nested = TS.item_size_bound(TS.to_typed({"a": [[[1]]]}))
    assert nested == flat + 2 * (3 + 1)


def _sized(target: int) -> dict:
    base = {"PK": "GEN#J1", "SK": "META", "pad": ""}
    over = target - TS.item_size_bound(TS.to_typed(base))
    return {**base, "pad": "€" * (over // 3) + "a" * (over % 3)}


def test_item_at_the_bound_is_written_and_one_byte_over_is_too_large(monkeypatch):
    monkeypatch.setattr(TS, "TRACE_ITEM_MAX_BYTES", 2_000)
    at = _sized(2_000)
    assert TS.item_size_bound(TS.to_typed(at)) == 2_000
    spy = Spy()
    TS.TraceStore(spy, TABLE).put(at)
    over = {**at, "pad": at["pad"] + "a"}
    with pytest.raises(O.TraceItemTooLarge):
        TS.TraceStore(spy, TABLE).put(over)
    assert len(spy.calls) == 1                                      # never sent, never truncated


def test_the_writer_uses_the_orchestrator_limit():
    assert TS.TRACE_ITEM_MAX_BYTES == O.TRACE_ITEM_MAX_BYTES == 400_000


def test_a_writer_size_failure_keeps_its_own_code_through_the_orchestrator(monkeypatch):
    monkeypatch.setattr(TS, "TRACE_ITEM_MAX_BYTES", 300)        # the orchestrator's JSON check passes
    out, fails, _, _ = go(writer=TS.TraceStore(FakeDynamoDB(), TABLE))
    assert not out.ok and out.bundle is None
    assert (fails[0].step, fails[0].code) == ("trace", "trace_item_too_large")


# --- the production writer behind the orchestrator: one Query returns the run in order -----

EXPECTED_ORDER = ["META", "STEP#01#guard_input#A1", "STEP#02#extract_facts#A1",
                  "STEP#03#validate_facts#A1", "STEP#03b#retrieve_evidence#A1",
                  "STEP#04#route_resus#A1", "STEP#05a#reconcile_medications#A1",
                  "STEP#05b#select_safety_net#A1", "STEP#06#compose_clinical#A1",
                  "STEP#07#compose_patient#A1", "STEP#08a#assemble_clinical#A1",
                  "STEP#08b#assemble_patient#A1"]


def _query(fake, job="J1"):
    resp = fake.query(TableName=TABLE, KeyConditionExpression="PK = :pk",
                      ExpressionAttributeValues={":pk": {"S": f"GEN#{job}"}})
    assert "LastEvaluatedKey" not in resp                         # one Query, one page
    return [i["SK"]["S"] for i in resp["Items"]], resp["Items"]


def test_the_sk_design_sorts_into_step_number_order():
    """The design property behind DoD 2: byte-sorted SKs = DELIVERY, META, then the steps
    in step-number order (the registry's order; 03 before 03b), whatever order they ran in."""
    by_number = [f"STEP#{s.seq}#{s.name}#A1" for s in registry._ALL_STEPS if s.name != "verify_patient"]
    assert by_number == EXPECTED_ORDER[1:]
    shuffled = list(reversed(["DELIVERY"] + EXPECTED_ORDER))
    assert sorted(shuffled, key=lambda s: s.encode("utf-8")) == ["DELIVERY"] + EXPECTED_ORDER


def test_one_query_returns_the_orchestrators_twelve_items_in_order():
    fake = FakeDynamoDB()
    out, fails, _, _ = go(writer=TS.TraceStore(fake, TABLE))
    assert out.ok and fails == []
    sks, items = _query(fake)
    assert sks == EXPECTED_ORDER                                    # 11 steps + META; 7v never runs
    ttl = {i["ttl"]["N"] for i in items}
    assert ttl == {str(O.trace_ttl(out.started_epoch))} and out.started_epoch == 1_762_300_000.0
    assert O.trace_ttl(out.started_epoch) - int(out.started_epoch) == 30 * 86400
    assert not any("user_sub" in json.dumps(i) for i in items)
    started = {i["SK"]["S"]: i["started_at"]["S"] for i in items if "STEP#" in i["SK"]["S"]}
    assert len(started) == 11                                       # execution order is in started_at


def test_a_failed_run_returns_fewer_items_still_in_order():
    from test_pipeline_orchestrate import FakeBedrock
    fake = FakeDynamoDB()
    b = FakeBedrock(script={"compose_clinical": [ReadTimeoutError(endpoint_url="x")]})
    out, fails, _, _ = go(b, writer=TS.TraceStore(fake, TABLE))
    assert not out.ok
    sks, items = _query(fake)
    assert sks == sorted(sks, key=lambda s: s.encode()) and "META" in sks
    meta = next(i for i in items if i["SK"]["S"] == "META")
    assert meta["status"]["S"] == "failed" and meta["error_code"]["S"] == "read_timeout"
    assert "STEP#07#compose_patient#A1" not in sks and "STEP#08b#assemble_patient#A1" not in sks


def _strings(o):
    """Every string in a typed item, unescaped (never search json.dumps output)."""
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def _notes_in(fake) -> bool:
    return any(NOTES in s for v in fake.items.values() for s in _strings(v))


def test_the_raw_notes_never_reach_the_table():
    fake = FakeDynamoDB()
    go(writer=TS.TraceStore(fake, TABLE))
    assert len(fake.items) == 12 and not _notes_in(fake)


def test_the_notes_check_can_fire():
    """Positive control: multi-line notes stored as a string ARE found by the walk."""
    fake = FakeDynamoDB()
    TS.TraceStore(fake, TABLE).put(item(x={"y": ["a", NOTES]}))
    assert "\n" in NOTES and _notes_in(fake)
