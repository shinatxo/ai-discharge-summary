"""The required META write under the deadline (WS1a step build piece 6, decision 1 —
Shina (author/CSO), 6 Nov 2026 07:07, informed by Codex recommendations of 06:55 / 07:03).

META is the last write before release. It runs on the pool under the same deadline watch as
the steps, and the clock is checked again after it. These tests hold the META write with
bounded events and the production writer on the strict fake table, and check: failure
handling starts at the deadline while the write is still held; no bundle is released; and
what the table holds afterwards. **META alone does not prove release** — a write in flight
at the deadline can still land as "complete" (ordering 1, disclosed).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from botocore.exceptions import ReadTimeoutError

from conftest import FakeDynamoDB

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

import trace_store as TS  # noqa: E402
from pipeline import orchestrate as O  # noqa: E402
from test_pipeline_orchestrate import Clock, go  # noqa: E402

TABLE = "discharge-eph-test-traces"
DEADLINE_IN = 270.0


class HeldMeta:
    """Wraps the fake table. When 8b's item is written, the clock moves to 0.5 s before the
    deadline (so the META wait is bounded at ~0.5 s real time). The complete META write is
    then handled by `mode`:
      stored_then_held — stored, then held until released (lands before the deadline fires)
      held_then_lost   — held, then fails with a read timeout without storing; the request
                         is kept to be "delivered late" by the test
      in_time          — stored and returned at once, in time; the TEST moves the clock past
                         the deadline after the watched wait succeeds and before the final
                         clock check (so only that check can stop the release)."""

    def __init__(self, fake, clock, deadline, mode):
        self.fake, self.clock, self.deadline, self.mode = fake, clock, deadline, mode
        self.entered, self.release = threading.Event(), threading.Event()
        self.lost = None

    def put_item(self, **kw):
        sk = kw["Item"]["SK"]["S"]
        if sk.startswith("STEP#08b#"):
            r = self.fake.put_item(**kw)
            self.clock.t = self.deadline - 0.5
            return r
        if sk == "META" and kw["Item"]["status"]["S"] == "complete":
            if self.mode == "in_time":
                return self.fake.put_item(**kw)
            if self.mode == "stored_then_held":
                self.fake.put_item(**kw)
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("schedule not established: META never released")
            self.clock.t = self.deadline + 1.0
            if self.mode == "held_then_lost":
                self.lost = kw
                raise ReadTimeoutError(endpoint_url="https://dynamodb.eu-west-2.amazonaws.com")
            return {}
        return self.fake.put_item(**kw)


def _run(mode, monkeypatch):
    clock, fake = Clock(), FakeDynamoDB()
    held = HeldMeta(fake, clock, clock.now() + DEADLINE_IN, mode)
    seen, real_next_done = [], O._Run.next_done

    def next_done(self, running):
        """Explicit schedules around the META wait (test-only wrapper):
        held modes — the META write must have ENTERED (bounded handshake) before the watched
        wait starts, so the deadline is reached while it is held;
        in_time — the watched wait succeeds in time, THEN the clock passes the deadline,
        before the orchestrator's final clock check."""
        is_meta = O.META_RUNNING_KEY in running
        if is_meta and mode != "in_time" and not held.entered.wait(5):
            raise AssertionError("schedule not established: META write never entered")
        done = real_next_done(self, running)
        if is_meta and mode == "in_time":
            assert O.META_RUNNING_KEY in done, "schedule not established: META wait did not succeed"
            clock.t = held.deadline + 1.0
        return done
    monkeypatch.setattr(O._Run, "next_done", next_done)

    def on_failure(f):
        seen.append({"step": f.step, "code": f.code, "entered": held.entered.is_set(),
                     "released": held.release.is_set()})
        held.release.set()

    out, _, _, _ = go(writer=TS.TraceStore(held, TABLE), clock=clock, deadline_in=DEADLINE_IN,
                      on_failure=on_failure)
    return out, seen, fake, held


def _meta(fake):
    return fake.items.get((TABLE, "GEN#J1", "META"))


def test_meta_held_at_the_deadline_fails_the_job_while_held_and_releases_nothing__stored_first(monkeypatch):
    out, seen, fake, _ = _run("stored_then_held", monkeypatch)
    assert seen == [{"step": "trace", "code": "deadline_reached", "entered": True, "released": False}]
    assert not out.ok and out.bundle is None and out.stored is None
    # ordering 1 (disclosed): the complete META landed; the failure-side META was refused by
    # attribute_not_exists(SK). META alone does not prove release.
    assert _meta(fake)["status"]["S"] == "complete"


def test_meta_held_at_the_deadline__lost_request_then_failed_meta_then_late_arrival_rejected(monkeypatch):
    out, seen, fake, held = _run("held_then_lost", monkeypatch)
    assert seen == [{"step": "trace", "code": "deadline_reached", "entered": True, "released": False}]
    assert not out.ok and out.bundle is None
    # ordering 2: the failure-side META landed first ...
    meta = _meta(fake)
    assert meta["status"]["S"] == "failed" and meta["error_code"]["S"] == "deadline_reached"
    assert meta["failed_step"]["S"] == "trace"
    # ... and the complete META arriving late is rejected by the condition, not overwritten
    try:
        fake.put_item(**held.lost)
        raise AssertionError("late META overwrote the failed META")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    assert _meta(fake)["status"]["S"] == "failed"


def test_meta_written_in_time_but_past_the_deadline_at_the_final_check_releases_nothing(monkeypatch):
    out, seen, fake, _ = _run("in_time", monkeypatch)
    assert [(s["step"], s["code"]) for s in seen] == [("trace", "deadline_reached")]
    assert not out.ok and out.bundle is None
    assert _meta(fake)["status"]["S"] == "complete"        # disclosed: META alone does not prove release


def test_meta_in_time_releases_and_is_the_last_item():
    clock, fake = Clock(), FakeDynamoDB()
    out, fails, _, _ = go(writer=TS.TraceStore(fake, TABLE), clock=clock)
    assert out.ok and fails == [] and _meta(fake)["status"]["S"] == "complete"
