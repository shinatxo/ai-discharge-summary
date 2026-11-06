"""The orchestrator — runs the ADR-009 pipeline end to end, fail-closed (WS1a step
build piece 5, 5 Nov 2026; Shina (author/CSO) decisions of 5 Nov 16:21, informed by Codex
recommendations of 16:01; piece 6, 6 Nov 2026: META under the deadline watch).

Only this module knows the step order. It has no AWS code: the worker (app.py) passes
in the Bedrock clients, the trace writer, the deadline, the clocks and a failure
callback, so every path here runs under fakes in the tests.

ORDER
    1 guard -> 2 extract -> 3 validate -> 3b retrieve -> 4 route_resus -> 5b safety_net
    then 5a reconcile  ||  6 compose_clinical                                  ([A5])
    6 done  -> PRE-FLIGHT (8a's own step-6 structure rule) -> 7 compose_patient
    5a + 6 done -> 8a assemble_clinical  (beside step 7: 7 reads only step 6's prose)
    7 + 8a done -> 8b assemble_patient   (the release check)
Step 7v (ADR-010) is not run: the verifier stopped at its dev gate (5 Nov 2026).
PART C stays a DRAFT FOR CLINICIAN REVIEW; checklist item 3 is NOT MET.

FAIL-CLOSED (ADR-009 Q5). The first failure fails the job: `on_failure` is called once,
immediately — by the thread that found the failure, BEFORE that step's trace item is
written and before requests already underway are drained — so the status row can say
"failed" without waiting (Codex read-only code inspection, 5 Nov 2026 19:36). After it, no step and no request starts; requests already sent
are waited for, because a thread left running when the handler returns is frozen by
Lambda and would wake inside the next job. Only a successful 8b, with every step item and
META written, returns a bundle. Release contract (CSO, 5 Nov 2026): no document bundle is
released on failure; intermediate drafts may remain in the restricted trace.

DEADLINE (ADR-009 Q3). Checked immediately before EVERY Bedrock request by a guarded
client — a request starts only if the time left exceeds the step's read timeout + 10 s —
and in the throttle sleep (which also wakes early on cancellation). Admission (the
cancellation check, the deadline check and counting the request) is one atomic act under
the lock that records a failure: no request is admitted after a failure, and none admitted
before it is missing from the failure's count. Read and connect timeouts are headroom,
not a wall-clock bound on a request, so the coordinating thread waits only until the
deadline: if work is still running then, the job fails `deadline_reached` at that moment
and the request is drained afterwards. Every step, 8a and 8b included, runs on the pool
under that watch, so a code step that hangs is also failed at the deadline (its thread
cannot be stopped: the row is already failed, and Lambda's own timeout ends the run).

REQUESTS (R5, CSO 5 Nov 2026). Counted when SENT; usage is captured from each response
before the step parses it. model_calls = requests sent. Token totals are None whenever
any request's usage is unknown (still in flight, throttled, timed out, errored, or a
response without usage): a partial sum is never presented as a total. At the failure
callback a request still in flight makes the totals None; the final per-request record
is in META, written after draining.

TRACES (ADR-009 (b), [A6]). One item per step, written as the step ends (SK
STEP#<seq>#<name>#A<attempt>); META — the PIPELINE outcome, not delivery — once at the
end. Everything traced goes through `scrub`. Before release every write is required (a
failure fails the job: trace_write_failed / trace_item_too_large); after a failure,
writes are best effort. The required META write runs on the pool under the same deadline
watch as the steps, and the clock is checked again after it (piece 6, decision 1): a META
write still in flight at the deadline fails the job `deadline_reached` at that moment. Such a
write may still land afterwards with status "complete" although nothing was released — META
alone does not prove release; the ResultsTable item and the audit row do. Every item's `ttl`
is `trace_ttl(started)` from the run's one start epoch, returned in Outcome.started_epoch so
the worker's DELIVERY item matches.

Fixed orchestrator codes: deadline_reached; cancelled (a step stopped because another
failed — never the job's code); trace_write_failed; trace_item_too_large;
unexpected_error (an exception other than StepError inside a step); orchestrator_error
(an exception in this module).
"""

from __future__ import annotations

import functools
import hashlib
import json
import random
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from botocore.exceptions import ClientError, ReadTimeoutError

from . import compose, compose_patient, extract, reconcile
from .assemble import check_step6_structure
from .context import StepContext
from .errors import StepError
from .registry import STEPS, run_step
from .schemas import record_facts_schema
from .validate import QUOTE_MAX_CHARS

DEADLINE_S = 270          # ADR-009 Q3 (2): internal deadline; the worker Timeout is 300 s
START_MARGIN_S = 10       # a request starts only if read timeout + 10 s remain
LAMBDA_RESERVE_S = 30     # the worker keeps 30 s of Lambda time for marking and writing
READ_TIMEOUT_S = {        # ADR-009 Q3 (2): one client per timeout
    "extract_facts": 90,
    "compose_clinical": compose.READ_TIMEOUT_S,           # 90
    "reconcile_medications": reconcile.READ_TIMEOUT_S,    # 60
    "compose_patient": compose_patient.READ_TIMEOUT_S,    # 60
}
PROMPT_MODULES = {"extract_facts": extract, "reconcile_medications": reconcile,
                  "compose_clinical": compose, "compose_patient": compose_patient}
TRACE_SCHEMA_VERSION = 1
TRACE_TTL_S = 30 * 86400
TRACE_ITEM_MAX_BYTES = 400_000     # DynamoDB's item limit; the writer fails, never truncates
META_RUNNING_KEY = "trace"         # failed_step when the deadline passes during the META write
ATTEMPT = 1                        # regeneration (W10) writes A2, A3 ...
WITHHELD = "[withheld]"
ORCHESTRATOR_CODES = ("deadline_reached", "cancelled", "trace_write_failed",
                      "trace_item_too_large", "unexpected_error", "orchestrator_error")
REQUEST_OUTCOMES = ("in_flight", "response", "throttled", "read_timeout", "rejected", "error")

_LINE_ID = re.compile(r"L\d{3,}")


# --- scrub: what may be written to a trace (and to ResultsTable's units) ------------------

def _is_raw_index(d: dict) -> bool:
    """A raw line index {"L001": "<note text>", ...} — never traced, under any key."""
    return bool(d) and all(isinstance(k, str) and _LINE_ID.fullmatch(k) and isinstance(v, str)
                           for k, v in d.items())


def scrub(obj: Any, notes_sha256: str) -> Any:
    """Note text out, structure in (R3, CSO 5 Nov 2026).

    - a raw line index (under ANY key)  -> {"notes_sha256", "line_ids"} (by reference, [A6])
    - a raw "notes" string              -> {"notes_sha256", "withheld": "raw_notes"}
    - a "lines" LIST (a citation)       -> kept, but only well-formed line IDs survive; any
                                           other entry (malformed model output) -> "[withheld]"
    - any "quote" over 200 characters   -> "" (cleared, never truncated)
    Everything else is kept: fact IDs, reasons, verification flags, counts, documents."""
    if isinstance(obj, dict):
        if _is_raw_index(obj):
            return {"notes_sha256": notes_sha256, "line_ids": sorted(obj)}
        out = {}
        for k, v in obj.items():
            if k == "notes" and isinstance(v, str):
                out[k] = {"notes_sha256": notes_sha256, "withheld": "raw_notes"}
            elif k == "lines" and isinstance(v, list):
                out[k] = [x if isinstance(x, str) and _LINE_ID.fullmatch(x) else WITHHELD for x in v]
            elif k == "quote" and isinstance(v, str) and len(v) > QUOTE_MAX_CHARS:
                out[k] = ""
            else:
                out[k] = scrub(v, notes_sha256)
        return out
    if isinstance(obj, list):
        return [scrub(x, notes_sha256) for x in obj]
    return obj


# --- provenance -------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _prompt_shas() -> dict:
    return {name: m.system_prompt()[1] for name, m in PROMPT_MODULES.items()}


@functools.lru_cache(maxsize=1)
def pipeline_version() -> str:
    """ADR-009 (b): a hash of the four step prompts + the facts schema + the code. There is
    no git inside a Lambda, so the code version is the bytes of this package's .py files."""
    h = hashlib.sha256()
    for name, sha in sorted(_prompt_shas().items()):
        h.update(f"{name}:{sha}\n".encode())
    h.update(json.dumps(record_facts_schema(), sort_keys=True).encode())
    for p in sorted(Path(__file__).resolve().parent.glob("*.py")):
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


# --- what the orchestrator hands back ----------------------------------------------------

class TraceWriter(Protocol):
    """Implemented on TraceTable by `trace_store.TraceStore` (item 6: PutItem with
    attribute_not_exists(SK)). `put` raises on any failure — TraceItemTooLarge for an item
    over the size bound — and never overwrites an existing SK."""
    def put(self, item: dict) -> None: ...


class TraceItemTooLarge(Exception):
    """Raised by a trace writer whose own size bound rejects an item (never truncated)."""


def trace_ttl(started_epoch: float) -> int:
    """ADR-009 (b): `ttl` = started_at + 30 days on every item of a run."""
    return int(started_epoch + TRACE_TTL_S)


@dataclass(frozen=True)
class Failure:
    step: str                    # a step name, "trace" or "orchestrator" (fixed words)
    code: str                    # a fixed code (a step's StepError code or ORCHESTRATOR_CODES)
    model_calls: int             # requests sent so far
    input_tokens: int | None     # None unless every request's usage is known
    output_tokens: int | None
    safety_net_gate: str | None  # "fail" ONLY for safety_net_gate_failed; else not reached / not failed


@dataclass(frozen=True)
class Outcome:
    ok: bool
    failure: Failure | None
    bundle: dict | None          # 8b's output — only when ok
    stored: dict | None          # units + templates for ResultsTable — only when ok
    model_calls: int
    input_tokens: int | None
    output_tokens: int | None
    pipeline_version: str
    generated_on: str
    started_epoch: float = 0.0   # the run's one start epoch (wall clock): every item's ttl


class _Failed(Exception):
    def __init__(self, step: str, code: str):
        super().__init__(code)
        self.step, self.code = step, code


# --- the run ----------------------------------------------------------------------------

def _outcome_of(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return {"ThrottlingException": "throttled", "ValidationException": "rejected"}.get(code, "error")
    if isinstance(exc, ReadTimeoutError):
        return "read_timeout"
    return "error"


def _usage_of(resp: Any) -> dict | None:
    u = resp.get("usage") if isinstance(resp, dict) else None
    if not isinstance(u, dict):
        return None
    it, ot = u.get("inputTokens"), u.get("outputTokens")
    if not isinstance(it, int) or not isinstance(ot, int):
        return None
    cr, cw = u.get("cacheReadInputTokens", 0), u.get("cacheWriteInputTokens", 0)
    return {"input_tokens": it, "output_tokens": ot,
            "cache_read_tokens": cr if isinstance(cr, int) else None,
            "cache_write_tokens": cw if isinstance(cw, int) else None}


class _GuardedClient:
    """Wraps a Bedrock client for one step: deadline + cancellation checked immediately
    before EVERY request; each request counted when sent; usage captured from the raw
    response before the step parses it."""

    def __init__(self, run: "_Run", step: str, inner: Any, read_timeout: int):
        self._run, self._step, self._inner, self._rt = run, step, inner, read_timeout

    def converse(self, **request):
        idx = self._run.admit(self._step, self._rt)
        try:
            resp = self._inner.converse(**request)
        except BaseException as exc:
            self._run.request_ended(idx, _outcome_of(exc), None)
            raise
        self._run.request_ended(idx, "response", _usage_of(resp))
        return resp


class _Run:
    def __init__(self, *, generation_id, notes_sha256, clients, model_id, prompt_caching, trace,
                 deadline, now, wall, sleep, jitter, on_failure, step_runner):
        self.generation_id = generation_id
        self.notes_sha256 = notes_sha256
        self.clients, self.model_id, self.prompt_caching = clients, model_id, prompt_caching
        self.trace, self.deadline, self.now, self.wall = trace, deadline, now, wall
        self.jitter, self.on_failure, self.step_runner = jitter, on_failure, step_runner
        self.cancel = threading.Event()
        self.sleep = sleep or (lambda s: self.cancel.wait(s))   # wakes early on cancellation
        self.lock = threading.Lock()
        self.failure: Failure | None = None
        self.requests: list[dict] = []
        self.running: dict[str, str] = {}     # step name -> seq, for deadline attribution
        self.started_wall = wall()
        self.line_count: int | None = None

    # requests -------------------------------------------------------------------------
    def admit(self, step: str, read_timeout: int) -> int:
        """Atomic with fail(): both hold self.lock, and fail() sets the cancel flag inside it.
        A request is either admitted (and counted) before a failure is recorded, or refused."""
        with self.lock:
            if self.cancel.is_set():
                raise StepError("cancelled")
            if self.now() + read_timeout + START_MARGIN_S > self.deadline:
                raise StepError("deadline_reached")
            self.requests.append({"step": step, "n": len(self.requests) + 1,
                                  "outcome": "in_flight", "usage": None})
            return len(self.requests) - 1

    def request_ended(self, idx: int, outcome: str, usage: dict | None) -> None:
        with self.lock:
            self.requests[idx]["outcome"] = outcome
            self.requests[idx]["usage"] = usage

    def _accounting_locked(self) -> tuple[int, int | None, int | None]:
        n = len(self.requests)
        if all(r["outcome"] == "response" and r["usage"] is not None for r in self.requests):
            return (n, sum(r["usage"]["input_tokens"] for r in self.requests),
                    sum(r["usage"]["output_tokens"] for r in self.requests))
        return n, None, None

    def accounting(self) -> tuple[int, int | None, int | None]:
        with self.lock:
            return self._accounting_locked()

    def _sleep_for(self, read_timeout: int) -> Callable[[float], None]:
        def guarded_sleep(seconds: float) -> None:
            if self.cancel.is_set():
                raise StepError("cancelled")
            if self.now() + seconds + read_timeout + START_MARGIN_S > self.deadline:
                raise StepError("deadline_reached")
            self.sleep(seconds)
        return guarded_sleep

    # failure --------------------------------------------------------------------------
    def fail(self, step: str, code: str) -> None:
        with self.lock:
            if self.failure is not None:
                return
            n, it, ot = self._accounting_locked()
            self.failure = Failure(step, code, n, it, ot,
                                   "fail" if code == "safety_net_gate_failed" else None)
            self.cancel.set()
            failure = self.failure
        try:
            self.on_failure(failure)
        except Exception:  # noqa: BLE001 — the worker's callback logs its own errors
            pass

    # traces ---------------------------------------------------------------------------
    def write(self, item: dict, *, required: bool) -> None:
        err = None
        try:
            blob = json.dumps(item, allow_nan=False, ensure_ascii=False).encode("utf-8")
            if len(blob) > TRACE_ITEM_MAX_BYTES:
                err = "trace_item_too_large"
            else:
                self.trace.put(item)
        except TraceItemTooLarge:
            err = "trace_item_too_large"
        except Exception:  # noqa: BLE001
            err = err or "trace_write_failed"
        if err and required and self.failure is None:
            self.fail("trace", err)                 # signal first, then unwind
            raise _Failed("trace", err)

    def _step_item(self, name, status, code, inp, out, started, latency_ms) -> dict:
        spec = STEPS[name]
        item = {
            "PK": f"GEN#{self.generation_id}", "SK": f"STEP#{spec.seq}#{name}#A{ATTEMPT}",
            "step_name": name, "seq": spec.seq, "kind": spec.kind, "status": status,
            "attempt": ATTEMPT, "started_at": _iso(started), "latency_ms": latency_ms,
            "input": scrub(inp, self.notes_sha256),
            "output": scrub(out, self.notes_sha256) if out is not None else None,
            "error_code": code, "ttl": trace_ttl(self.started_wall),
        }
        if spec.kind == "model":
            with self.lock:
                reqs = [dict(r) for r in self.requests if r["step"] == name]
            item.update(model_id=self.model_id, prompt_sha256=_prompt_shas()[name], requests=reqs)
        return item

    def meta_item(self, ok: bool, generated_on: str) -> dict:
        n, it, ot = self.accounting()
        with self.lock:
            reqs = [dict(r) for r in self.requests]
        f = self.failure
        return {
            "PK": f"GEN#{self.generation_id}", "SK": "META", "outcome_of": "pipeline",
            "trace_schema_version": TRACE_SCHEMA_VERSION, "pipeline_version": pipeline_version(),
            "status": "complete" if ok else "failed",
            "failed_step": f.step if f else None, "error_code": f.code if f else None,
            "started_at": _iso(self.started_wall), "completed_at": _iso(self.wall()),
            "notes_sha256": self.notes_sha256, "line_count": self.line_count,
            "model_id": self.model_id, "generated_on": generated_on,
            "model_calls": n, "input_tokens": it, "output_tokens": ot, "requests": reqs,
            "ttl": trace_ttl(self.started_wall),
        }

    # steps ----------------------------------------------------------------------------
    def step(self, name: str, inp: dict) -> dict:
        if self.cancel.is_set():
            raise _Failed(name, "cancelled")          # never started: no item
        spec = STEPS[name]
        ctx = None
        if spec.kind == "model":
            rt = READ_TIMEOUT_S[name]
            ctx = StepContext(bedrock=_GuardedClient(self, name, self.clients[rt], rt),
                              model_id=self.model_id, prompt_caching=self.prompt_caching,
                              sleep=self._sleep_for(rt), jitter=self.jitter)
        with self.lock:
            self.running[name] = spec.seq
        started, t0 = self.wall(), self.now()
        try:
            out, status, code = self.step_runner(name, inp, ctx=ctx), "ok", None
        except StepError as exc:
            out, code = None, exc.code
            status = "cancelled" if code == "cancelled" else "failed"
        except Exception:  # noqa: BLE001 — fail closed with a fixed code, never the message
            out, status, code = None, "failed", "unexpected_error"
        finally:
            with self.lock:
                self.running.pop(name, None)
        latency_ms = int((self.now() - t0) * 1000)
        if code and code != "cancelled":
            self.fail(name, code)                   # signal BEFORE the trace write (Codex, 19:36)
        self.write(self._step_item(name, status, code, inp, out, started, latency_ms),
                   required=(status == "ok"))
        if code:
            raise _Failed(name, code)
        return out

    # waiting --------------------------------------------------------------------------
    def next_done(self, running: dict) -> list[str]:
        """Names of finished futures; waits at most until the deadline."""
        timeout = self.deadline - self.now()
        done = set()
        if timeout > 0:
            done, _ = wait(list(running.values()), timeout=timeout, return_when=FIRST_COMPLETED)
        if not done:
            with self.lock:
                inflight = sorted(self.running.items(), key=lambda kv: kv[1])
            raise _Failed(inflight[0][0] if inflight else sorted(running)[0], "deadline_reached")
        return [n for n, f in running.items() if f in done]


# --- the order (the only place it is written) --------------------------------------------

def _phase_a(r: _Run, notes: str) -> dict:
    g = r.step("guard_input", {"notes": notes})
    r.line_count = g["line_count"]
    lines = g["lines"]
    x = r.step("extract_facts", {"lines": lines})
    v = r.step("validate_facts", {"facts": x["facts"], "lines": lines})
    facts = v["facts"]
    ev = r.step("retrieve_evidence", {"facts": facts, "lines": lines, "flags": g["flags"]})
    rr = r.step("route_resus", {"facts": facts, "evidence": ev, "lines": lines})
    sn = r.step("select_safety_net", {"facts": facts, "flags": g["flags"]})
    return {"guard": g, "facts": facts, "evidence": ev, "resus": rr, "safety_net": sn}


def _preflight(comp: dict) -> dict:
    """Status-doc carry-in F1: fail on step 6's structure BEFORE step 7 starts (8a's own
    check is defence in depth, not a timing guarantee — 8a may run beside step 7)."""
    try:
        check_step6_structure(comp)
    except StepError as exc:
        raise _Failed("compose_clinical", exc.code) from None
    try:
        return compose_patient.patient_input(comp)
    except StepError as exc:
        raise _Failed("compose_patient", exc.code) from None


def _coordinate(r: _Run, ex: ThreadPoolExecutor, notes: str, generated_on: str) -> dict:
    # keyed by its first step: a deadline before any step has started is attributed to
    # guard_input (failed_step is always a step name or a fixed word)
    running = {"guard_input": ex.submit(_phase_a, r, notes)}
    while running:
        for name in r.next_done(running):
            a = running.pop(name).result()
    g, facts, ev = a["guard"], a["facts"], a["evidence"]
    lines = g["lines"]

    running = {
        "reconcile_medications": ex.submit(r.step, "reconcile_medications",
                                           {"lines": lines, "facts": facts, "evidence": ev}),
        "compose_clinical": ex.submit(r.step, "compose_clinical", {"facts": facts}),  # never the notes
    }
    outs: dict = {}
    inp8a = None
    while running:
        for name in r.next_done(running):
            outs[name] = running.pop(name).result()
            if name == "compose_clinical":
                inp7 = _preflight(outs[name])
                if r.cancel.is_set():
                    raise _Failed("compose_patient", "cancelled")
                running["compose_patient"] = ex.submit(r.step, "compose_patient", inp7)
        if inp8a is None and "reconcile_medications" in outs and "compose_clinical" in outs:
            inp8a = {"facts": facts, "flags": g["flags"], "language": ev.get("language") or [],
                     "resus": a["resus"], "reconciliation": outs["reconcile_medications"],
                     "safety_net": a["safety_net"], "composition": outs["compose_clinical"],
                     "generated_on": generated_on}
            if r.cancel.is_set():
                raise _Failed("assemble_clinical", "cancelled")
            # beside step 7, on the pool: under the deadline watch like every other step
            running["assemble_clinical"] = ex.submit(r.step, "assemble_clinical", inp8a)
    o8a = outs["assemble_clinical"]

    if r.cancel.is_set():
        raise _Failed("assemble_patient", "cancelled")
    running = {"assemble_patient": ex.submit(
        r.step, "assemble_patient",
        inp8a | {"lines": lines, "patient": outs["compose_patient"], "clinical": o8a})}
    while running:
        for name in r.next_done(running):
            outs[name] = running.pop(name).result()
    o8b = outs["assemble_patient"]
    stored = {
        # What a W10 regeneration (5a -> 8a -> 8b, reusing step 7) and hand-edit
        # propagation need — never from TraceTable (ADR-009 (c)).
        "units": scrub({"facts": facts, "evidence": ev, "flags": g["flags"], "resus": a["resus"],
                        "safety_net": a["safety_net"], "reconciliation": outs["reconcile_medications"]},
                       r.notes_sha256),
        "templates": scrub({"composition": outs["compose_clinical"], "patient": outs["compose_patient"]},
                           r.notes_sha256),
        "warnings": o8b["warnings"],
        "hashes": o8b["hashes"],
        "generated_on": generated_on,
    }
    return {"bundle": o8b, "stored": stored}


def run(notes: str, *, generation_id: str, clients: dict, model_id: str, trace: TraceWriter,
        deadline: float, generated_on: str, on_failure: Callable[[Failure], None],
        prompt_caching: bool = False, now: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time, sleep: Callable[[float], Any] | None = None,
        jitter: Callable[[float, float], float] = random.uniform,
        step_runner: Callable[..., dict] = run_step) -> Outcome:
    """Run the pipeline once. `clients` maps read timeout (90, 60) -> a Bedrock client built
    with that read timeout and total_max_attempts=1. `deadline` is on the `now` clock.
    `on_failure` is called at most once, at the first failure, before draining.
    `step_runner` is run_step; the offline evidence run substitutes saved outputs."""
    notes_sha256 = hashlib.sha256(notes.encode("utf-8")).hexdigest() if isinstance(notes, str) else ""
    r = _Run(generation_id=generation_id, notes_sha256=notes_sha256, clients=clients,
             model_id=model_id, prompt_caching=prompt_caching, trace=trace, deadline=deadline,
             now=now, wall=wall, sleep=sleep, jitter=jitter, on_failure=on_failure,
             step_runner=step_runner)
    result, meta_written = None, False
    ex = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline")
    try:
        result = _coordinate(r, ex, notes, generated_on)
        # The required META write (piece 6, decision 1): on the pool, under the deadline
        # watch like every step, then the clock checked again — a write that returns after
        # the deadline releases nothing.
        running = {META_RUNNING_KEY: ex.submit(r.write, r.meta_item(True, generated_on), required=True)}
        while running:
            for name in r.next_done(running):
                running.pop(name).result()
        meta_written = True
        if r.now() > r.deadline:
            raise _Failed(META_RUNNING_KEY, "deadline_reached")
    except _Failed as f:
        r.fail(f.step, f.code)
    except Exception:  # noqa: BLE001 — a crash in the order must not let a bundle out
        r.fail("orchestrator", "orchestrator_error")
    finally:
        ex.shutdown(wait=True, cancel_futures=True)      # drain: nothing new, in-flight finishes

    ok = r.failure is None and result is not None and meta_written
    if not ok and not meta_written:
        r.write(r.meta_item(False, generated_on), required=False)   # best effort after a failure
    n, it, ot = r.accounting()
    if not ok:
        return Outcome(False, r.failure, None, None, n, it, ot, pipeline_version(), generated_on,
                       r.started_wall)
    return Outcome(True, None, result["bundle"], result["stored"], n, it, ot,
                   pipeline_version(), generated_on, r.started_wall)
