#!/usr/bin/env python3
"""WS7 client wrapper: no retries, one call per step, and a spend guard (ADR-011 D7).

The entrypoint hands each runner `client_factory(guard)` through the runner's
existing `client_factory` argument. No runner and no pipeline file is changed.

What the wrapper does, in the order it checks:

1. It builds the Bedrock client itself, with
   retries={"mode": "standard", "total_max_attempts": 1} and the read timeout
   the runner asks for. botocore then sends each `converse` call once.
   Endpoint URLs set in the environment or the AWS config file are ignored
   (botocore's `ignore_configured_endpoint_urls`); the live client must be
   Bedrock runtime in eu-west-2, or it is refused at construction.
2. Before every call: a batch must be open, the wall-clock deadline must not
   have passed, the run must not be unmetered (point 6), and the running total
   must be below the run's reservation.
3. It names the request's step (the pipeline by the tool the step forces, v1
   by its maxTokens) and refuses a request it does not know, a maxTokens that
   is not the reserved figure, or another model ID.
4. It allows one call per step per generation, and no more generations than
   the batch planned. A generation starts at a first-step request whose body
   differs from the current generation's first-step request; an identical one
   is a repeat. Once any call is refused, every further call is refused until
   such a request starts the next generation.
5. After every call it adds the call's cost from the usage Bedrock returned.
   A call with no usable usage - an error, a timeout, a malformed reply - is
   counted at its worst case.
6. A call whose cost is unknown and whose model may have run - a reply with no
   usable usage, a 5xx, a read timeout, any other error except the three in
   NOT_RUN - makes the run UNMETERED: every further call in the run is refused.
   Its worst case is an estimate, not a bound, because a step's input is not
   capped, so nothing more is spent on top of it.

A refusal raises Ws7Refused, a StepError with a fixed code. The pipeline's
converse.py passes a StepError through unchanged (src/generate/pipeline/
converse.py:34), so the code becomes the step's error code. The v1 runner
records the exception's class name and its message, which is the same code
(evals/run_cold_eval.py:395).

The call log holds step names, fixed labels, token counts and costs. It never
holds a request or a response. A request's body is hashed to tell a repeat
from a new generation; only the hash is kept, in memory, for one generation.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path

from botocore.exceptions import (ClientError, ConnectTimeoutError, EndpointConnectionError,
                                 ParamValidationError, ReadTimeoutError)

_EVALS = Path(__file__).resolve().parent
for _p in (str(_EVALS.parent / "src" / "generate"), str(_EVALS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ws7_reserve as reserve  # noqa: E402
from pipeline import StepError  # noqa: E402

RETRIES = {"mode": "standard", "total_max_attempts": 1}   # one attempt: no retry inside a call

NO_BATCH = "ws7_no_batch"
DEADLINE = "ws7_deadline"
UNMETERED = "ws7_unmetered"
SPEND_GUARD = "ws7_spend_guard"
UNKNOWN_REQUEST = "ws7_unknown_request"
MAX_TOKENS_DIFFERS = "ws7_max_tokens_differs"
MODEL_DIFFERS = "ws7_model_differs"
OUT_OF_ORDER = "ws7_out_of_order"
PLAN_EXCEEDED = "ws7_plan_exceeded"
GENERATION_BLOCKED = "ws7_generation_blocked"
REPEAT_REFUSED = "ws7_repeat_refused"
CODES = (NO_BATCH, DEADLINE, UNMETERED, SPEND_GUARD, UNKNOWN_REQUEST, MAX_TOKENS_DIFFERS,
         MODEL_DIFFERS, OUT_OF_ORDER, PLAN_EXCEEDED, GENERATION_BLOCKED, REPEAT_REFUSED)

# What became of a call that was sent.
RESPONSE, USAGE_UNKNOWN = "response", "response_usage_unknown"
NO_RESPONSE = ("throttled", "server_error", "client_error", "read_timeout", "connect_error",
               "request_invalid", "other_error")
REFUSED = "refused"
# Failures in which the model did not run: a throttle (Bedrock rejects the request
# before the model runs - an assumption, not checked against AWS's documentation), a
# connection never made, and a request botocore refused before sending. Every other
# failure, and a reply with no usable usage, makes the run unmetered.
NOT_RUN = ("throttled", "connect_error", "request_invalid")

# The live client's endpoint, written out rather than built from the region, and the
# only other endpoint allowed: the tests' stub on loopback.
LIVE_REGION = "eu-west-2"
LIVE_ENDPOINT = "https://bedrock-runtime.eu-west-2.amazonaws.com"
_STUB_ENDPOINT_RE = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}")
ENDPOINT_REFUSED = "ws7_endpoint_refused"

_BATCH_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class Ws7Refused(StepError):
    """A call the wrapper did not send. `code` is one of CODES."""


class EndpointRefused(Exception):
    """The client would not talk to Bedrock runtime in eu-west-2 (or, in tests, to the
    loopback stub). Raised at construction, before any request. `code` is fixed."""

    def __init__(self):
        super().__init__(ENDPOINT_REFUSED)
        self.code = ENDPOINT_REFUSED


def _unmetered(outcome: str) -> bool:
    """A call was sent, its cost is not known from usage, and its model may have run."""
    return outcome not in (RESPONSE, REFUSED, "sent") and outcome not in NOT_RUN


def fingerprint(request: object) -> str:
    """The sha256 the wrapper uses to tell a repeat from a new generation: the request
    as canonical JSON. Part 5's distinct-scenarios precheck must use this, on the
    first-step request the runner would send, not a hash of the raw notes."""
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def _failure_label(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}).get("Code")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if error == "ThrottlingException":
            return "throttled"
        return "server_error" if type(status) is int and status >= 500 else "client_error"
    if isinstance(exc, ReadTimeoutError):
        return "read_timeout"
    if isinstance(exc, (ConnectTimeoutError, EndpointConnectionError)):
        return "connect_error"
    if isinstance(exc, ParamValidationError):
        return "request_invalid"
    return "other_error"


class Guard:
    """The state of one run: its reservation, its running total and its call
    log. Every client the factory hands out shares one Guard."""

    def __init__(self, plan: reserve.Plan, *, reservation_nanos: int, margin_percent: int,
                 deadline: float, clock=time.monotonic):
        if type(reservation_nanos) is not int or reservation_nanos <= 0:
            raise ValueError("reservation_nanos must be a whole number above zero")
        self.plan = plan
        self.reservation_nanos = reservation_nanos
        self._worst = {s.id: reserve.worst_case_nanos(s, margin_percent) for s in plan.steps}
        self._by_tool = {s.tool_name: s for s in plan.steps if s.tool_name is not None}
        self._by_max_tokens = {s.max_tokens: s for s in plan.steps if s.tool_name is None}
        self._deadline = deadline
        self._clock = clock
        self._lock = threading.Lock()
        self._batch: dict | None = None
        self._batches: dict = {}
        self.total_nanos = 0
        self.tripped = False
        self.unmetered = False
        self.calls: list = []

    # ---------------------------------------------------------------- batches

    def begin_batch(self, label: str, generations: int) -> None:
        """Open a batch: one runner call. `generations` is how many it may start."""
        with self._lock:
            if self._batch is not None:
                raise ValueError("a batch is already open")
            if type(label) is not str or not _BATCH_RE.fullmatch(label) or label in self._batches:
                raise ValueError("a batch label is an unused ID")
            if type(generations) is not int or generations < 1:
                raise ValueError("generations must be a whole number of one or more")
            self._batch = {"label": label, "planned": generations, "started": 0,
                           "total_nanos": 0, "generation": None, "blocked": False}
            self._batches[label] = self._batch

    def end_batch(self) -> None:
        with self._lock:
            if self._batch is None:
                raise ValueError("no batch is open")
            self._batch = None

    # ------------------------------------------------------------ before a call

    def admit(self, request: object) -> dict:
        """Check one request. Returns its row in the call log, or raises
        Ws7Refused; a refused request is never sent."""
        with self._lock:
            batch = self._batch
            if batch is None:
                self._refuse(NO_BATCH)
            if self._clock() >= self._deadline:
                self._refuse(DEADLINE)
            if self.unmetered:
                self._refuse(UNMETERED)
            if self.total_nanos >= self.reservation_nanos:
                self.tripped = True
                self._refuse(SPEND_GUARD)

            step, body = self._recognise(request)
            generation = batch["generation"]
            if step is self.plan.steps[0] and (generation is None or body != generation["first"]):
                if batch["started"] >= batch["planned"]:
                    self._refuse(PLAN_EXCEEDED, step.id)
                batch["started"] += 1
                generation = batch["generation"] = {"number": batch["started"], "first": body,
                                                    "sent": set()}
                batch["blocked"] = False          # only a new generation lifts a refusal
            elif generation is None:
                self._refuse(OUT_OF_ORDER, step.id)
            if batch["blocked"]:
                self._refuse(GENERATION_BLOCKED, step.id)
            if step.id in generation["sent"]:
                self._refuse(REPEAT_REFUSED, step.id)
            generation["sent"].add(step.id)
            return self._row(step.id, "sent", generation=generation["number"],
                             cost_usd_nanos=self._worst[step.id], worst_case=True)

    def _recognise(self, request: object) -> tuple:
        """The request's step and the hash of its body."""
        if type(request) is not dict:
            self._refuse(UNKNOWN_REQUEST)
        inference = request.get("inferenceConfig")
        max_tokens = inference.get("maxTokens") if type(inference) is dict else None
        if self.plan.target == "pipeline":
            try:
                tool = request["toolConfig"]["toolChoice"]["tool"]["name"]
            except (KeyError, TypeError):
                self._refuse(UNKNOWN_REQUEST)
            step = self._by_tool.get(tool) if type(tool) is str else None
            if step is None:
                self._refuse(UNKNOWN_REQUEST)
            if type(max_tokens) is not int or max_tokens != step.max_tokens:
                self._refuse(MAX_TOKENS_DIFFERS, step.id)
        else:
            step = self._by_max_tokens.get(max_tokens) if type(max_tokens) is int else None
            if step is None or "toolConfig" in request:
                self._refuse(UNKNOWN_REQUEST)
        if request.get("modelId") != self.plan.model_id:
            self._refuse(MODEL_DIFFERS, step.id)
        try:
            body = fingerprint(request)
        except (TypeError, ValueError):
            self._refuse(UNKNOWN_REQUEST, step.id)
        return step, body

    def _refuse(self, code: str, step: str | None = None):
        self._row(step, REFUSED, code=code)
        if self._batch is not None:
            # Every later call is refused until a distinct first-step request
            # starts the next generation.
            self._batch["blocked"] = True
        raise Ws7Refused(code)

    def _row(self, step: str | None, outcome: str, **fields) -> dict:
        batch = self._batch
        row = {"seq": len(self.calls) + 1, "batch": batch["label"] if batch else None,
               "generation": None, "step": step, "outcome": outcome, "code": None,
               "input_tokens": None, "output_tokens": None, "cache_read_tokens": None,
               "cache_write_tokens": None, "cost_usd_nanos": 0, "worst_case": False,
               "latency_ms": None}
        row.update(fields)
        self.calls.append(row)
        return row

    # ------------------------------------------------------------- after a call

    def settle_response(self, row: dict, response: object, latency_ms: int) -> None:
        usage = reserve.read_usage(response.get("usage") if type(response) is dict else None)
        self._settle(row, USAGE_UNKNOWN if usage is None else RESPONSE, usage, latency_ms)

    def settle_failure(self, row: dict, exc: BaseException, latency_ms: int) -> None:
        self._settle(row, _failure_label(exc), None, latency_ms)

    def _settle(self, row: dict, outcome: str, usage: dict | None, latency_ms: int) -> None:
        with self._lock:
            # No usable usage: the call stays at its worst case (ADR-011 D7, point 3).
            cost = row["cost_usd_nanos"] if usage is None else usage["cost_usd_nanos"]
            row.update(usage or {}, outcome=outcome, cost_usd_nanos=cost,
                       worst_case=usage is None, latency_ms=latency_ms)
            self.total_nanos += cost
            self._batches[row["batch"]]["total_nanos"] += cost
            if _unmetered(outcome):
                # Its true cost is unknown and its input is not capped: spend no more.
                self.unmetered = True

    # ------------------------------------------------------------------ report

    def batch_total_nanos(self, label: str) -> int:
        with self._lock:
            return self._batches[label]["total_nanos"]

    def report(self) -> dict:
        """Counts, costs and the call log: IDs, fixed labels and numbers only."""
        with self._lock:
            sent = [c for c in self.calls if c["outcome"] != REFUSED]
            refused: dict = {}
            for c in self.calls:
                if c["outcome"] == REFUSED:
                    refused[c["code"]] = refused.get(c["code"], 0) + 1
            return {
                "target": self.plan.target,
                "reservation_usd_nanos": self.reservation_nanos,
                "total_usd_nanos": self.total_nanos,
                "total_usd_micros": reserve.to_micros(self.total_nanos),
                "worst_case_part_usd_nanos": sum(c["cost_usd_nanos"] for c in sent
                                                 if c["worst_case"]),
                # reached: the total is at or past the reservation. tripped: a call
                # was refused because of it. The last call of a run can reach it
                # with nothing left to refuse.
                "reservation_reached": self.total_nanos >= self.reservation_nanos,
                "tripped": self.tripped,
                # unmetered: a call's cost is unknown and its model may have run. The
                # total is then an estimate, and every later call was refused.
                "unmetered": self.unmetered,
                "unmetered_calls": sum(_unmetered(c["outcome"]) for c in sent),
                "calls_sent": len(sent),
                "responses": sum(c["outcome"] in (RESPONSE, USAGE_UNKNOWN) for c in sent),
                "no_response": sum(c["outcome"] in NO_RESPONSE for c in sent),
                "usage_unknown": sum(c["outcome"] == USAGE_UNKNOWN for c in sent),
                "refused": dict(sorted(refused.items())),
                "batches": {label: {"planned": b["planned"], "started": b["started"],
                                    "total_usd_nanos": b["total_nanos"]}
                            for label, b in self._batches.items()},
                "calls": [dict(c) for c in self.calls],
            }

    def elapsed_ms(self, since: float) -> int:
        return int((self._clock() - since) * 1000)

    def now(self) -> float:
        return self._clock()


class GuardedClient:
    """What the code under test is given: `converse`, through the guard, and
    nothing else public. The client beneath is a private slot; neither runner
    reaches for it (Python cannot make it unreachable, only private)."""

    __slots__ = ("_inner", "_guard")

    def __init__(self, inner, guard: Guard):
        self._inner = inner
        self._guard = guard

    def converse(self, **request):
        row = self._guard.admit(request)
        started = self._guard.now()
        try:
            response = self._inner.converse(**request)
        except BaseException as exc:
            self._guard.settle_failure(row, exc, self._guard.elapsed_ms(started))
            raise
        self._guard.settle_response(row, response, self._guard.elapsed_ms(started))
        return response


def build_client(*, read_timeout_s: float, connect_timeout_s: float, endpoint_url: str | None = None):
    """The one place WS7 creates a Bedrock client.

    Endpoint URLs configured in the environment (AWS_ENDPOINT_URL,
    AWS_ENDPOINT_URL_BEDROCK_RUNTIME) or in the AWS config file are ignored:
    botocore's `ignore_configured_endpoint_urls=True` (botocore/config.py:208-213,
    applied at botocore/args.py:387-406, where an explicit `endpoint_url` argument
    still wins and the Config value outranks AWS_IGNORE_CONFIGURED_ENDPOINT_URLS).

    The live path passes no `endpoint_url` and must resolve to Bedrock runtime in
    eu-west-2; anything else (a FIPS or dual-stack variant, another region) is
    refused here, before any request. `endpoint_url` is for the tests' stub and
    must be http://127.0.0.1:<port>."""
    import boto3
    from botocore.config import Config
    if reserve.rce.REGION != LIVE_REGION:
        raise EndpointRefused()
    if endpoint_url is not None and not (type(endpoint_url) is str
                                         and _STUB_ENDPOINT_RE.fullmatch(endpoint_url)):
        raise EndpointRefused()
    cfg = Config(connect_timeout=connect_timeout_s, read_timeout=read_timeout_s,
                 retries=dict(RETRIES), ignore_configured_endpoint_urls=True)
    client = boto3.Session(region_name=LIVE_REGION).client(
        "bedrock-runtime", config=cfg, endpoint_url=endpoint_url)
    expected = LIVE_ENDPOINT if endpoint_url is None else endpoint_url
    if client.meta.endpoint_url != expected or client.meta.region_name != LIVE_REGION:
        raise EndpointRefused()
    return client


def client_factory(guard: Guard, *, endpoint_url: str | None = None):
    """What a runner receives as `client_factory`. The pipeline runner calls it
    with a read timeout for each client it wants; the v1 runner calls it with
    nothing and gets the fixed v1 timeout."""
    plan = guard.plan

    def factory(read_timeout=None):
        timeout = plan.read_timeout_s if read_timeout is None else read_timeout
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("the runner must name a read timeout above zero")
        return GuardedClient(build_client(read_timeout_s=timeout,
                                          connect_timeout_s=plan.connect_timeout_s,
                                          endpoint_url=endpoint_url), guard)
    return factory
