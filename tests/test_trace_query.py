"""infra/trace_query.py — the read-only operator reader (WS1a step build piece 6, decision 5).
Against the fake table: one Query returns the run in SK order; pagination is followed and
reported; the summary on stdout carries no content; full items only to a new 0600 file."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from conftest import FakeDynamoDB

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

import trace_store as TS  # noqa: E402
from test_pipeline_orchestrate import go  # noqa: E402
from test_trace_store import EXPECTED_ORDER, PLANTED, TABLE  # noqa: E402

_spec = importlib.util.spec_from_file_location("trace_query", ROOT / "infra" / "trace_query.py")
Q = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(Q)


def _table_with_a_run():
    fake = FakeDynamoDB()
    out, _, _, _ = go(writer=TS.TraceStore(fake, TABLE))
    assert out.ok
    return fake


def test_one_query_returns_the_run_in_step_order():
    fake = _table_with_a_run()
    items, pages = Q.query_run(fake, TABLE, "J1")
    assert pages == 1 and len(fake.query_calls) == 1
    assert [i["SK"] for i in items] == EXPECTED_ORDER and Q.in_step_order(items)
    assert isinstance(items[0]["ttl"], int)                       # Decimal -> int for whole numbers


def test_other_runs_are_not_returned():
    fake = _table_with_a_run()
    TS.TraceStore(fake, TABLE).put({"PK": "GEN#J2", "SK": "META", "ttl": 1})
    items, _ = Q.query_run(fake, TABLE, "J1")
    assert {i["PK"] for i in items} == {"GEN#J1"}


def test_pagination_is_followed_and_reported(capsys):
    fake = _table_with_a_run()
    fake.page_size = 5
    items, pages = Q.query_run(fake, TABLE, "J1")
    assert pages == 3 and [i["SK"] for i in items] == EXPECTED_ORDER
    Q.main(["--table", TABLE, "--job-id", "J1"], client=fake)
    cap = capsys.readouterr()
    assert json.loads(cap.out.strip().splitlines()[-1]) == {"items": 12, "pages": 3, "in_sk_order": True}
    assert "more than one Query page" in cap.err


def test_out_of_order_items_are_detected():
    assert not Q.in_step_order([{"SK": "STEP#02#x#A1"}, {"SK": "STEP#01#y#A1"}])


def test_summary_carries_no_content_and_the_file_does(tmp_path, capsys):
    fake = _table_with_a_run()
    TS.TraceStore(fake, TABLE).put({"PK": "GEN#J1", "SK": "STEP#99#planted#A1", "ttl": 1,
                                    "output": {"text": PLANTED}})
    out = tmp_path / "run.json"
    assert Q.main(["--table", TABLE, "--job-id", "J1", "--out", str(out)], client=fake) == 0
    printed = capsys.readouterr().out
    assert PLANTED not in printed
    assert PLANTED in out.read_text()                              # positive control: content is there
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_out_never_overwrites(tmp_path):
    fake = _table_with_a_run()
    out = tmp_path / "run.json"
    out.write_text("keep")
    with pytest.raises(FileExistsError):
        Q.main(["--table", TABLE, "--job-id", "J1", "--out", str(out)], client=fake)
    assert out.read_text() == "keep"


def test_the_reader_only_queries():
    src = (ROOT / "infra" / "trace_query.py").read_text()
    calls = set(__import__("re").findall(r"client\.(\w+)\(", src))
    assert calls == {"query"}
