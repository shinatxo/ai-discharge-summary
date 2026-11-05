"""Tests for the orchestrator (pipeline/orchestrate.py; WS1a step build piece 5, 5 Nov 2026).

A scripted fake Bedrock (the harness's dry-run payloads, overridable per step), a fake
clock and a fake trace writer. No AWS, no live calls, synthetic notes only; no note text
is asserted into logs. CSO rulings of 5 Nov 2026 16:01 / 16:21 are cited by number.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "evals"))

import run_pipeline_eval as R  # noqa: E402
from pipeline import StepContext, StepError, run_step  # noqa: E402
from pipeline import assemble as AS  # noqa: E402
from pipeline import compose, compose_patient, extract, reconcile  # noqa: E402
from pipeline import orchestrate as O  # noqa: E402
from pipeline import registry  # noqa: E402
from pipeline.converse import converse_with_throttle_retry  # noqa: E402
from pipeline.schemas import FIELD_NAMES, wire_facts  # noqa: E402
from pipeline.validate import QUOTE_MAX_CHARS, verify_cite  # noqa: E402

NOTES = "72M admitted with chest pain\nTrop raised\nDH: aspirin 75mg OD\nPlan: home, GP review 2/52"
C02_PROMPT_SHA_PREFIX = "48613f4d8669"      # step 6 c0.2 — the live step-6 prompt (status doc)

_TOOL_STEP = {json.dumps(m.tool_config(), sort_keys=True): name
              for name, m in (("extract_facts", extract), ("reconcile_medications", reconcile),
                              ("compose_clinical", compose), ("compose_patient", compose_patient))}


def step_of(kw) -> str:
    return _TOOL_STEP[json.dumps(kw["toolConfig"], sort_keys=True)]


def throttle():
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse")


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t, self._lock = t, threading.Lock()

    def now(self) -> float:
        return self.t

    def advance(self, d: float) -> None:
        with self._lock:
            self.t += d


class FakeBedrock:
    """Answers like the harness's DryRunBedrock unless a per-step script says otherwise. A
    script action is an exception (raised), a callable (called with the request; it may
    block, raise or return a response) or None (dry answer)."""

    def __init__(self, script=None, clock=None, latency=1.0):
        self.dry = R.DryRunBedrock()
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[str] = []
        self.clock, self.latency = clock, latency
        self._lock = threading.Lock()

    def converse(self, **kw):
        step = step_of(kw)
        with self._lock:
            self.calls.append(step)
            queue = self.script.get(step) or []
            action = queue.pop(0) if queue else None
        if self.clock is not None:
            self.clock.advance(self.latency)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            resp = action(kw)
            if resp is not None:
                return resp
        return self.dry.converse(**kw)


class Writer:
    def __init__(self, fail_when=None):
        self.items: list[dict] = []
        self.fail_when = fail_when or (lambda item: False)
        self._lock = threading.Lock()

    def put(self, item: dict) -> None:
        json.dumps(item)                                    # must be plain JSON
        with self._lock:
            if self.fail_when(item):
                raise RuntimeError("trace store unavailable")
            assert item["SK"] not in {i["SK"] for i in self.items}, "attribute_not_exists(SK)"
            self.items.append(item)

    def sk(self) -> list[str]:
        return [i["SK"] for i in self.items]

    def get(self, sk: str) -> dict:
        return next(i for i in self.items if i["SK"] == sk)


def go(bedrock=None, *, writer=None, clock=None, deadline_in=270.0, notes=NOTES, sleep=None,
       step_runner=run_step, on_failure=None, jitter=lambda a, b: 25.0):
    clock = clock or Clock()
    bedrock = bedrock or FakeBedrock(clock=clock)
    writer = writer or Writer()
    fails: list = []
    out = O.run(notes, generation_id="J1", clients={90: bedrock, 60: bedrock}, model_id="m",
                trace=writer, deadline=clock.now() + deadline_in, generated_on="2026-11-05",
                on_failure=on_failure or fails.append, now=clock.now, wall=lambda: 1_762_300_000.0,
                sleep=(None if sleep == "default" else sleep if sleep is not None
                       else (lambda s: clock.advance(s))),
                jitter=jitter, step_runner=step_runner)
    return out, fails, writer, bedrock


def _waits_for(event: threading.Event, then, what: str, timeout: float = 5.0):
    """A script action that establishes a schedule: wait (bounded) for `event`, then act.
    A missed prerequisite fails the test loudly instead of passing for the wrong reason."""
    def action(kw):
        if not event.wait(timeout):
            raise AssertionError(f"schedule not established: {what}")
        if isinstance(then, BaseException):
            raise then
        return then(kw) if callable(then) else None
    return action


def composition_without(heading: str):
    secs = [s for s in R.DryRunBedrock.composition() if not (s["part"] == "part_a" and s["heading"] == heading)]
    return lambda kw: R.DryRunBedrock._tool_use("record_composition", {"sections": secs})


def walk(obj):
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


# --- the happy path and the order ------------------------------------------------------

def test_happy_path_releases_a_bundle_with_every_step_traced_and_meta_last():
    out, fails, w, b = go()
    assert out.ok and out.failure is None and fails == []
    assert set(out.bundle["hashes"]) == {"part_a", "part_b", "part_c"}
    assert out.model_calls == 4 and sorted(b.calls) == sorted(
        ["extract_facts", "reconcile_medications", "compose_clinical", "compose_patient"])
    steps = [s for s in w.sk() if s.startswith("STEP#")]
    assert sorted(steps) == sorted(f"STEP#{registry.STEPS[n].seq}#{n}#A1" for n in (
        "guard_input", "extract_facts", "validate_facts", "retrieve_evidence", "route_resus",
        "select_safety_net", "reconcile_medications", "compose_clinical", "compose_patient",
        "assemble_clinical", "assemble_patient"))
    assert "STEP#07v#verify_patient#A1" not in w.sk()            # 7v not in the orchestrator
    assert w.sk()[-1] == "META"
    meta = w.get("META")
    assert meta["status"] == "complete" and meta["outcome_of"] == "pipeline"
    assert meta["model_calls"] == 4 and meta["pipeline_version"] == O.pipeline_version()
    assert all(i["status"] == "ok" for i in w.items if i["SK"].startswith("STEP#"))


def test_dependencies_hold_in_the_written_order():
    out, _, w, _ = go()
    pos = {i["step_name"]: n for n, i in enumerate(w.items) if "step_name" in i}
    seq_a = ["guard_input", "extract_facts", "validate_facts", "retrieve_evidence",
             "route_resus", "select_safety_net"]
    assert [pos[s] for s in seq_a] == sorted(pos[s] for s in seq_a)
    assert pos["select_safety_net"] < min(pos["reconcile_medications"], pos["compose_clinical"])
    assert pos["compose_clinical"] < pos["compose_patient"]
    assert max(pos["reconcile_medications"], pos["compose_clinical"]) < pos["assemble_clinical"]
    assert max(pos["compose_patient"], pos["assemble_clinical"]) < pos["assemble_patient"]


def test_5a_and_6_run_at_the_same_time():
    both = threading.Barrier(2, timeout=3)    # sequential execution would break the barrier
    b = FakeBedrock(script={"reconcile_medications": [lambda kw: (both.wait(), None)[1]],
                            "compose_clinical": [lambda kw: (both.wait(), None)[1]]})
    out, fails, _, _ = go(b)
    assert out.ok, fails


def test_step_7_starts_while_5a_is_still_running():
    seven = threading.Event()

    def five_a(kw):
        if not seven.wait(3):
            raise RuntimeError("step 7 never started while 5a ran")

    b = FakeBedrock(script={"reconcile_medications": [five_a],
                            "compose_patient": [lambda kw: seven.set()]})
    out, fails, _, _ = go(b)
    assert out.ok, fails


def test_8a_input_matches_the_offline_replay_shape():
    out, _, w, _ = go()
    inp = w.get("STEP#08a#assemble_clinical#A1")["input"]
    assert set(inp) == AS.INPUT_KEYS_8A and inp["generated_on"] == "2026-11-05"
    inp8b = w.get("STEP#08b#assemble_patient#A1")["input"]
    assert set(inp8b) == AS.INPUT_KEYS_8B
    assert inp8b["lines"]["line_ids"] == ["L001", "L002", "L003", "L004"]   # by reference


# --- pre-flight (carry-in F1) ---------------------------------------------------------

def test_preflight_fails_required_heading_missing_before_step_7():
    b = FakeBedrock(script={"compose_clinical": [composition_without("presenting_complaint")]})
    out, fails, w, _ = go(b)
    assert not out.ok and out.bundle is None
    assert (fails[0].step, fails[0].code) == ("compose_clinical", "required_heading_missing")
    assert "compose_patient" not in b.calls
    assert not any(s.startswith("STEP#07#") for s in w.sk())


def test_preflight_keeps_box_structure_distinct():
    def bad_box(kw):
        secs = R.DryRunBedrock.composition()
        for s in secs:
            if s["part"] == "part_a" and s["heading"] == "medications":
                s["fact_ids"] = ["presenting_complaint.0"]        # a box must cite nothing
        return R.DryRunBedrock._tool_use("record_composition", {"sections": secs})

    b = FakeBedrock(script={"compose_clinical": [bad_box]})
    out, fails, w, _ = go(b)
    assert w.get("STEP#06#compose_clinical#A1")["status"] == "ok"       # step 6 itself accepted it
    assert (fails[0].step, fails[0].code) == ("compose_clinical", "box_structure")
    assert "compose_patient" not in b.calls


def test_preflight_rule_is_8a_rule():
    comp = {"checks": [{"check": "required_heading_missing"}], "sections": []}
    with pytest.raises(StepError) as e:
        AS.check_step6_structure(comp)
    assert e.value.code == "required_heading_missing"
    with pytest.raises(StepError) as e:
        AS.check_step6_structure({"sections": []})
    assert e.value.code == "bad_input"


# --- fail-closed: every model step, one fixed code, one callback ------------------------

@pytest.mark.parametrize("step", ["extract_facts", "reconcile_medications", "compose_clinical", "compose_patient"])
def test_any_model_step_failure_fails_the_job_once_with_its_code(step):
    b = FakeBedrock(script={step: [ReadTimeoutError(endpoint_url="https://bedrock")]})
    out, fails, w, _ = go(b)
    assert not out.ok and out.bundle is None and out.stored is None
    assert len(fails) == 1 and (fails[0].step, fails[0].code) == (step, "read_timeout")
    assert w.get("META")["status"] == "failed" and w.get("META")["error_code"] == "read_timeout"
    assert not any(s.startswith("STEP#08b#") for s in w.sk())


@pytest.mark.parametrize("code", ["safety_net_gate_failed", "clinical_stale", "resus_text_outside",
                                  "placeholder_unfilled", "gate_cannot_read_a", "assembly_check_error"])
def test_8b_failure_releases_nothing(monkeypatch, code):
    def raiser(inp):
        raise StepError(code)
    s = registry.STEPS["assemble_patient"]
    monkeypatch.setitem(registry.STEPS, "assemble_patient", registry.Step(s.name, s.seq, s.kind, raiser))
    out, fails, w, _ = go()
    assert not out.ok and out.bundle is None and out.stored is None
    assert (fails[0].step, fails[0].code) == ("assemble_patient", code)
    assert fails[0].safety_net_gate == ("fail" if code == "safety_net_gate_failed" else None)
    # the drafts stay only in the restricted trace (release contract)
    assert w.get("STEP#08a#assemble_clinical#A1")["status"] == "ok"
    assert w.get("STEP#08b#assemble_patient#A1")["output"] is None


def test_a_non_steperror_inside_a_step_is_unexpected_error():
    def runner(name, inp, *, ctx=None):
        if name == "route_resus":
            raise RuntimeError("boom with text that must not surface")
        return run_step(name, inp, ctx=ctx)
    out, fails, w, _ = go(step_runner=runner)
    assert (fails[0].step, fails[0].code) == ("route_resus", "unexpected_error")
    assert "boom" not in json.dumps(w.items)


def test_a_crash_in_the_order_is_orchestrator_error(monkeypatch):
    def broken(comp):
        raise RuntimeError("bug")
    monkeypatch.setattr(O, "_preflight", broken)
    out, fails, _, _ = go()
    assert not out.ok and (fails[0].step, fails[0].code) == ("orchestrator", "orchestrator_error")


# --- throttle-only retry, the deadline, cancellation -----------------------------------

def test_throttle_is_retried_with_the_jittered_sleep_and_tokens_become_unknown():
    clock = Clock()
    slept = []
    b = FakeBedrock(script={"extract_facts": [throttle()]}, clock=clock)
    out, fails, w, _ = go(b, clock=clock, sleep=lambda s: (slept.append(s), clock.advance(s)))
    assert out.ok, fails
    assert slept == [25.0]
    assert b.calls.count("extract_facts") == 2 and out.model_calls == 5
    reqs = w.get("STEP#02#extract_facts#A1")["requests"]
    assert [r["outcome"] for r in reqs] == ["throttled", "response"]
    assert out.input_tokens is None and out.output_tokens is None      # R5: incomplete -> NULL


def test_read_timeout_is_never_retried():
    b = FakeBedrock(script={"compose_clinical": [ReadTimeoutError(endpoint_url="x")]})
    out, fails, _, _ = go(b)
    assert b.calls.count("compose_clinical") == 1 and fails[0].code == "read_timeout"


def test_no_request_starts_without_read_timeout_plus_10s():
    clock = Clock()
    b = FakeBedrock(clock=clock)
    out, fails, w, _ = go(b, clock=clock, deadline_in=90 + 10 - 0.001)
    assert b.calls == [] and out.model_calls == 0
    assert (fails[0].step, fails[0].code) == ("extract_facts", "deadline_reached")
    assert w.get("STEP#02#extract_facts#A1")["error_code"] == "deadline_reached"


def test_throttle_sleep_that_would_pass_the_deadline_is_not_taken():
    clock = Clock()
    slept = []
    b = FakeBedrock(script={"extract_facts": [throttle()]}, clock=clock, latency=0.0)
    out, fails, _, _ = go(b, clock=clock, deadline_in=105, sleep=lambda s: slept.append(s))
    assert slept == [] and b.calls == ["extract_facts"]
    assert (fails[0].step, fails[0].code) == ("extract_facts", "deadline_reached")


def test_deadline_is_rechecked_before_the_retry_request_even_after_a_long_sleep():
    clock = Clock()
    b = FakeBedrock(script={"extract_facts": [throttle()]}, clock=clock, latency=0.0)
    # the sleep overruns (e.g. a slow wake): the pre-request check must catch it
    out, fails, _, _ = go(b, clock=clock, deadline_in=140, sleep=lambda s: clock.advance(s + 20))
    assert b.calls == ["extract_facts"] and fails[0].code == "deadline_reached"


def test_a_sibling_failure_cancels_a_sleeping_retry_and_no_new_request_starts(monkeypatch):
    # Schedule (d313 review 19:46, refined 19:56): step 6 fails only once 5a is INSIDE the
    # run's own cancellable wait — signalled by a wrapper around this run's actual
    # `self.sleep`, which guarded_sleep calls only after its cancellation and deadline
    # checks. The wrapper delegates to the original wait (cancel.wait), so the test proves
    # cancellation wakes the sleep, not only that it blocks the next request.
    entered, waits = threading.Event(), []
    real_init = O._Run.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        original = self.sleep                   # the default: lambda s: self.cancel.wait(s)

        def wrapped(seconds):
            entered.set()
            t = time.monotonic()
            woke = original(seconds)
            waits.append((seconds, woke, time.monotonic() - t))
            return woke
        self.sleep = wrapped

    monkeypatch.setattr(O._Run, "__init__", init)
    b = FakeBedrock(script={"reconcile_medications": [throttle()],
                            "compose_clinical": [_waits_for(entered, ReadTimeoutError(endpoint_url="x"),
                                                            "5a inside its cancellable retry wait")]})
    out, fails, w, _ = go(b, sleep="default")
    assert len(waits) == 1                                  # the original wait was called
    seconds, woke, slept = waits[0]
    assert seconds == 25.0 and woke is True and slept < 10  # woken by cancellation, not after 25 s
    assert (fails[0].step, fails[0].code) == ("compose_clinical", "read_timeout")
    assert b.calls.count("reconcile_medications") == 1
    item = w.get("STEP#05a#reconcile_medications#A1")
    assert item["status"] == "cancelled" and item["error_code"] == "cancelled"
    assert w.get("META")["error_code"] == "read_timeout"     # cancelled is never the job's code


def test_guarded_client_steperror_passes_through_converse():
    class Refuses:
        def converse(self, **kw):
            raise StepError("deadline_reached")
    ctx = StepContext(bedrock=Refuses(), model_id="m", sleep=lambda s: None)
    with pytest.raises(StepError) as e:
        converse_with_throttle_retry(ctx, {})
    assert e.value.code == "deadline_reached"                 # not bedrock_error


def test_watchdog_fails_at_the_deadline_before_the_inflight_call_returns(monkeypatch):
    monkeypatch.setattr(O, "START_MARGIN_S", 0)
    for k in list(O.READ_TIMEOUT_S):
        monkeypatch.setitem(O.READ_TIMEOUT_S, k, 0)
    release, events = threading.Event(), []

    def stalls(kw):
        events.append("6_sent")
        release.wait(5)
        events.append("6_returned")

    def on_failure(f):
        events.append(("failed", f.step, f.code, f.input_tokens))
        threading.Timer(0.1, release.set).start()

    b = FakeBedrock(script={"compose_clinical": [stalls]})
    out = O.run(NOTES, generation_id="J1", clients={0: b, 90: b, 60: b}, model_id="m", trace=Writer(),
                deadline=time.monotonic() + 0.5, generated_on="2026-11-05", on_failure=on_failure,
                jitter=lambda a, c: 0.0)
    assert not out.ok
    failed_at = next(i for i, e in enumerate(events) if isinstance(e, tuple))
    assert events[failed_at][1:3] == ("compose_clinical", "deadline_reached")
    assert events.index("6_returned") > failed_at             # marked before the call ended
    assert events[failed_at][3] is None                        # a request in flight -> tokens unknown
    assert "6_returned" in events                              # drained before run() returned


def test_failure_callback_counts_requests_sent_and_meta_has_the_final_account():
    release, five_a_in = threading.Event(), threading.Event()
    marked = []

    def five_a(kw):
        five_a_in.set()                          # 5a has entered its request
        if not release.wait(5):
            raise AssertionError("never released")

    def on_failure(f):
        marked.append(f)
        release.set()

    # Schedule: step 6 fails only after 5a has entered its request (bounded wait).
    b = FakeBedrock(script={"reconcile_medications": [five_a],
                            "compose_clinical": [_waits_for(five_a_in, ReadTimeoutError(endpoint_url="x"),
                                                            "5a in its request")]})
    out, _, w, _ = go(b, on_failure=on_failure)
    f = marked[0]
    assert f.model_calls == 3 and f.input_tokens is None       # step 2, 5a (in flight), 6
    meta = w.get("META")
    assert [r["outcome"] for r in meta["requests"] if r["step"] == "reconcile_medications"] == ["response"]
    assert w.get("STEP#05a#reconcile_medications#A1")["status"] == "ok"   # drained, job still failed


def test_usage_is_captured_at_the_request_boundary_even_when_parsing_fails():
    def no_tool(kw):
        return {"output": {"message": {"role": "assistant", "content": [{"text": "x"}]}},
                "stopReason": "end_turn", "usage": {"inputTokens": 11, "outputTokens": 7},
                "metrics": {"latencyMs": 1}}
    b = FakeBedrock(script={"extract_facts": [no_tool]})
    out, fails, w, _ = go(b)
    assert fails[0].code == "no_tool_call" and out.model_calls == 1
    assert w.get("META")["requests"][0]["usage"]["input_tokens"] == 11
    assert out.input_tokens == 11 and out.output_tokens == 7


# --- traces: required before release, best effort after a failure ----------------------

def test_a_step_trace_write_failure_prevents_release():
    w = Writer(fail_when=lambda i: i["SK"].startswith("STEP#06#"))
    out, fails, _, _ = go(writer=w)
    assert not out.ok and (fails[0].step, fails[0].code) == ("trace", "trace_write_failed")


def test_a_meta_write_failure_prevents_release_and_marks_failed():
    w = Writer(fail_when=lambda i: i["SK"] == "META")
    out, fails, _, _ = go(writer=w)
    assert not out.ok and out.bundle is None
    assert (fails[0].step, fails[0].code) == ("trace", "trace_write_failed")


def test_trace_item_too_large_is_its_own_code(monkeypatch):
    monkeypatch.setattr(O, "TRACE_ITEM_MAX_BYTES", 200)
    out, fails, _, _ = go()
    assert (fails[0].step, fails[0].code) == ("trace", "trace_item_too_large")


def test_after_a_failure_trace_writes_are_best_effort():
    w = Writer(fail_when=lambda i: i["SK"] == "META")
    b = FakeBedrock(script={"extract_facts": [ReadTimeoutError(endpoint_url="x")]})
    out, fails, _, _ = go(b, writer=w)
    assert len(fails) == 1 and fails[0].code == "read_timeout"


# --- scrub: no raw notes, quotes <= 200 (R3) ------------------------------------------

def test_scrub_rules():
    sha = "ab" * 32
    raw = {"L001": "72M", "L002": "CP"}
    got = O.scrub({"lines": raw, "x": {"anything": raw}, "notes": "72M\nCP",
                   "cites": [{"lines": ["L001", "chest pain since Tue"], "quote": "q" * 201, "reason": "malformed_line_id"},
                             {"lines": ["L002"], "quote": "CP"}]}, sha)
    assert got["lines"] == {"notes_sha256": sha, "line_ids": ["L001", "L002"]}
    assert got["x"]["anything"] == {"notes_sha256": sha, "line_ids": ["L001", "L002"]}
    assert got["notes"] == {"notes_sha256": sha, "withheld": "raw_notes"}
    assert got["cites"][0] == {"lines": ["L001", O.WITHHELD], "quote": "", "reason": "malformed_line_id"}
    assert got["cites"][1] == {"lines": ["L002"], "quote": "CP"}      # safe IDs and short quotes kept


def _strings(o):
    """String values and keys, unescaped — never search json.dumps output for note text:
    escaping turns newlines into \\n, so multi-line notes could never match (review 21:28, 5 Nov 2026)."""
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def _no_raw_notes(items, notes):
    assert "\n" in notes                         # the case the old dump check could never see
    for item in items:
        for node in walk(item):
            if isinstance(node, dict):
                assert not O._is_raw_index(node), item["SK"]
                assert not isinstance(node.get("notes"), str), item["SK"]
        assert not any(notes in s for s in _strings(item)), item["SK"]


def test_the_notes_text_check_can_fire():
    """Positive control: the same check finds multi-line notes planted in a trace item."""
    notes = "72M\nCP\nDH: nil\nhome"
    planted = [{"SK": "STEP#99#x#A1", "output": {"text": "before " + notes + " after"}}]
    with pytest.raises(AssertionError):
        _no_raw_notes(planted, notes)
    assert notes not in json.dumps(planted[0])     # why the old check was vacuous


def test_short_raw_notes_never_reach_the_trace():
    notes = "72M\nCP\nDH: nil\nhome"           # short lines: the 200-char rule could not catch these
    out, fails, w, _ = go(notes=notes)
    assert out.ok, fails
    _no_raw_notes(w.items, notes)
    assert w.get("STEP#01#guard_input#A1")["input"] == {
        "notes": {"notes_sha256": out_sha(notes), "withheld": "raw_notes"}}
    assert w.get("STEP#01#guard_input#A1")["output"]["lines"]["line_ids"] == ["L001", "L002", "L003", "L004"]


def out_sha(notes):
    import hashlib
    return hashlib.sha256(notes.encode()).hexdigest()


def _facts_with_long_quotes():
    f = {"fields": {n: {"status": "not_documented", "items": []} for n in FIELD_NAMES},
         "medications": {"pre_admission": [], "discharge": [], "discharge_status": "not_documented"},
         "resus": {"form_or_discussion_documented": False, "status_documented": "not_documented",
                   "changed": "not_documented", "cites": []},
         "documented_advice": [], "age_group": "not_documented", "contradictions": [], "suspicious_text": []}
    f["fields"]["specialty"] = {"status": "documented", "items": [{"value": "cardiology", "cites": [
        {"lines": ["L001", "L002", "L003", "L004"], "quote": "x" * 229},          # cite_too_many_lines
        {"lines": ["chest pain since Tuesday"], "quote": "y" * 250},              # malformed_line_id
        {"lines": ["L999"], "quote": "z" * 240}]}]}                               # line_out_of_range
    return wire_facts(copy.deepcopy(f))


def test_oversized_quotes_with_invalid_line_ids_are_cleared_everywhere():
    facts = _facts_with_long_quotes()
    b = FakeBedrock(script={"extract_facts": [lambda kw: R.DryRunBedrock._tool_use("record_facts", facts)]})
    out, fails, w, _ = go(b, notes="L1\nL2\nL3\nL4\nL5")
    traced = [n for i in w.items for n in walk(i) if isinstance(n, dict) and "quote" in n]
    assert traced and all(len(n["quote"]) <= QUOTE_MAX_CHARS for n in traced)
    v = w.get("STEP#03#validate_facts#A1")["output"]["facts"]["fields"]["specialty"]["items"][0]["cites"]
    assert [c["reason"] for c in v] == ["cite_too_many_lines", "malformed_line_id", "line_out_of_range"]
    assert [c["quote"] for c in v] == ["", "", ""]
    assert v[1]["lines"] == [O.WITHHELD]                       # model text in an ID slot withheld
    assert out.ok, fails
    stored = [n for n in walk(out.stored) if isinstance(n, dict) and "quote" in n]
    assert stored and all(len(n["quote"]) <= QUOTE_MAX_CHARS for n in stored)


@pytest.mark.parametrize("ids,reason", [(["L001", "L002", "L003", "L004"], "cite_too_many_lines"),
                                        (["bad id"], "malformed_line_id"),
                                        (["L099"], "line_out_of_range"),
                                        (["L001", "L003"], "non_consecutive_lines"),
                                        ([], "missing_lines")])
def test_verify_cite_clears_an_oversized_quote_and_keeps_the_structural_reason(ids, reason):
    lines = {f"L{n:03d}": "text" for n in range(1, 6)}
    got = verify_cite({"lines": ids, "quote": "q" * (QUOTE_MAX_CHARS + 1)}, lines)
    assert got["quote"] == "" and got["reason"] == reason and got["verified"] is False


def test_verify_cite_quote_too_long_path_unchanged():
    lines = {"L001": "text"}
    got = verify_cite({"lines": ["L001"], "quote": "q" * (QUOTE_MAX_CHARS + 1)}, lines)
    assert got == {"lines": ["L001"], "quote": "", "verified": False, "reason": "quote_too_long"}
    ok = verify_cite({"lines": ["L001"], "quote": "text"}, lines)
    assert ok["verified"] is True and ok["quote"] == "text"


# --- stored units, provenance, guards --------------------------------------------------

def test_stored_units_and_templates_are_what_a_w10_regeneration_needs():
    out, _, _, _ = go()
    assert set(out.stored["units"]) == {"facts", "evidence", "flags", "resus", "safety_net", "reconciliation"}
    assert set(out.stored["templates"]) == {"composition", "patient"}
    assert out.stored["hashes"] == out.bundle["hashes"] and out.stored["generated_on"] == "2026-11-05"


def test_regeneration_without_rerunning_8a_is_clinical_stale():
    """Ruling 4 (5 Nov): the W10 path is 5a -> 8a -> 8b reusing step 7. 8b refuses a stale 8a."""
    out, _, w, _ = go()
    # rebuild 8a/8b inputs from the run, then change 5a's output without re-running 8a
    g = run_step("guard_input", {"notes": NOTES})
    inp8a = w.get("STEP#08a#assemble_clinical#A1")["input"]
    inp8a = copy.deepcopy(inp8a)
    o8a = run_step("assemble_clinical", inp8a)
    changed = copy.deepcopy(inp8a)
    # a regenerated 5a with one more medicine (an entry from a saved synthetic record), 8a not re-run
    saved = json.loads((ROOT / "evals" / "runs" / "w6-pipeline-e09-m01-c02-p04-x5" / "r1" / "S1.json").read_text())
    changed["reconciliation"]["reconciliation"] = [saved["steps"]["reconcile_medications"]["reconciliation"][0]]
    assert run_step("assemble_clinical", changed)["part_a"] != o8a["part_a"]   # the change renders
    patient = w.get("STEP#07#compose_patient#A1")["output"]
    with pytest.raises(StepError) as e:
        run_step("assemble_patient", changed | {"lines": g["lines"], "patient": patient, "clinical": o8a})
    assert e.value.code == "clinical_stale"


def test_pipeline_version_is_a_stable_sha256():
    v = O.pipeline_version()
    assert len(v) == 64 and int(v, 16) >= 0 and v == O.pipeline_version()


def test_g3_allowance_tracks_the_live_step6_prompt():
    """Carry-in (status doc): set assemble.G3_ALLOWANCE = False when c0.3 goes live. This
    fails if the live step-6 prompt is no longer c0.2 while the allowance is still on (or
    the allowance is switched off while c0.2 is live)."""
    live = compose.system_prompt()[1]
    assert AS.G3_ALLOWANCE == live.startswith(C02_PROMPT_SHA_PREFIX)


def test_harness_client_sends_exactly_one_request(monkeypatch):
    import boto3
    seen = []

    class Session:
        def __init__(self, region_name=None):
            pass

        def client(self, name, config=None):
            seen.append(config)
            return object()

    monkeypatch.setattr(boto3, "Session", Session)
    R.live_client(60)
    assert seen[0].retries == {"mode": "standard", "total_max_attempts": 1}
    assert seen[0].read_timeout == 60


# --- CSO review 5 Nov 2026 19:36: three concurrency gaps --------------------------------

def test_a_step_failure_is_signalled_before_its_trace_item_is_written():
    events = []

    class Recording(Writer):
        def put(self, item):
            if item["SK"].startswith("STEP#06#"):
                events.append(("trace_06", item["status"]))
            super().put(item)

    b = FakeBedrock(script={"compose_clinical": [ReadTimeoutError(endpoint_url="x")]})
    out, _, _, _ = go(b, writer=Recording(), on_failure=lambda f: events.append(("failed", f.code)))
    assert events[0] == ("failed", "read_timeout") and events[1] == ("trace_06", "failed")


def test_a_trace_write_failure_is_signalled_before_unwinding():
    """Signalled where it is found: on the thread whose write failed, before control
    leaves that step (not later, when the coordinator collects the future)."""
    events = []

    class Fails(Writer):
        def put(self, item):
            if item["SK"].startswith("STEP#05a#"):
                events.append(("put_05a", threading.current_thread().name))
                raise RuntimeError("down")
            super().put(item)

    out, _, _, _ = go(writer=Fails(), on_failure=lambda f: events.append(
        (f.step, f.code, threading.current_thread().name)))
    assert not out.ok
    (_, writer_thread), (step, code, callback_thread) = events
    assert (step, code) == ("trace", "trace_write_failed")
    assert callback_thread == writer_thread and writer_thread.startswith("pipeline")


@pytest.mark.parametrize("step", ["assemble_clinical", "assemble_patient"])
def test_a_hanging_assembly_step_is_failed_at_the_deadline(monkeypatch, step):
    monkeypatch.setattr(O, "START_MARGIN_S", 0)
    for k in list(O.READ_TIMEOUT_S):
        monkeypatch.setitem(O.READ_TIMEOUT_S, k, 0)
    release, events = threading.Event(), []
    s = registry.STEPS[step]

    def hangs(inp):
        events.append("started")
        release.wait(5)
        events.append("returned")
        return s.fn(inp)

    monkeypatch.setitem(registry.STEPS, step, registry.Step(s.name, s.seq, s.kind, hangs))

    def on_failure(f):
        events.append((f.step, f.code))
        threading.Timer(0.1, release.set).start()

    b = FakeBedrock()
    out = O.run(NOTES, generation_id="J1", clients={0: b, 90: b, 60: b}, model_id="m", trace=Writer(),
                deadline=time.monotonic() + 0.5, generated_on="2026-11-05", on_failure=on_failure,
                jitter=lambda a, c: 0.0)
    assert not out.ok and out.bundle is None
    assert (step, "deadline_reached") in events
    assert events.index((step, "deadline_reached")) < events.index("returned")


def test_no_request_is_admitted_after_a_failure_and_none_before_it_is_uncounted():
    r = O._Run(generation_id="J", notes_sha256="x", clients={}, model_id="m", prompt_caching=False,
               trace=Writer(), deadline=10_000.0, now=lambda: 0.0, wall=lambda: 0.0, sleep=None,
               jitter=lambda a, b: 0.0, on_failure=lambda f: None, step_runner=run_step)
    r.admit("extract_facts", 90)                       # admitted before the failure: in flight
    r.fail("compose_clinical", "read_timeout")
    assert r.failure.model_calls == 1 and r.failure.input_tokens is None
    with pytest.raises(StepError) as e:
        r.admit("reconcile_medications", 60)
    assert e.value.code == "cancelled" and len(r.requests) == 1


def test_a_failure_recorded_during_admission_cannot_miss_the_admitted_request():
    """Deterministic race: a sibling records a failure WHILE a request is being admitted
    (from inside admission's deadline check). Atomic admission makes the failure wait,
    so its count includes the request; a check-then-append admission would let the
    failure count 0 and then send a request anyway."""
    seen, fired = [], []
    holder = {}

    def now():
        if not fired:
            fired.append(1)
            t = threading.Thread(target=lambda: holder["r"].fail("compose_clinical", "read_timeout"))
            t.start()
            t.join(0.3)                     # atomic: blocked on the lock; racy: completes here
        return 0.0

    r = O._Run(generation_id="J", notes_sha256="x", clients={}, model_id="m", prompt_caching=False,
               trace=Writer(), deadline=10_000.0, now=now, wall=lambda: 0.0, sleep=None,
               jitter=lambda a, b: 0.0, on_failure=seen.append, step_runner=run_step)
    holder["r"] = r
    try:
        r.admit("reconcile_medications", 60)
    except StepError:
        pass
    for _ in range(100):
        if seen:
            break
        time.sleep(0.01)
    assert seen and len(r.requests) == seen[0].model_calls
