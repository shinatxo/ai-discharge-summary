"""A local stand-in for Bedrock's Converse API, and the fixture that keeps the WS7
tests offline (ADR-011, the acceptance check for slice 2).

The stub is a real HTTP server on 127.0.0.1. The WS7 client wrapper builds a
real botocore client and points it at the stub, so botocore's signing, its
transport and its retry handler all run. The stub counts every request it
RECEIVES, by step: that count is the evidence, not anything the client says.

Every response is invented. A stub can answer a step with a model reply, a
throttle, a 500, or silence until the client's read timeout.

`offline_aws` is the safety fixture. With it:
- credentials are invented, and no profile, config file or metadata service is read;
- no proxy is used;
- a socket may connect only to an address a test has registered - the stub's
  own host and port. Anything else raises, and the test fails at teardown.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "evals") not in sys.path:
    sys.path.insert(0, str(ROOT / "evals"))

import run_pipeline_eval as rpe  # noqa: E402
import ws7_reserve as reserve  # noqa: E402

OK, THROTTLE, ERROR_500, STALL = "ok", "throttle", "error_500", "stall"
FAILURES = (THROTTLE, ERROR_500, STALL)
STALL_LIMIT_S = 30.0          # a stalled handler waits no longer than this for the test to end
JOIN_LIMIT_S = 5.0
HONEST_USAGE = {"inputTokens": 1000, "outputTokens": 200, "totalTokens": 1200}   # invented
SENTINEL = "WS7-STUB-SENTINEL-TEXT"     # invented model output; must never reach a log or a message


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):      # nothing to stderr: a request line could carry an ID
        pass

    def do_POST(self):
        stub: BedrockStub = self.server.stub
        self.close_connection = True    # one request per connection: no idle handler threads
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            step = stub.step_of(body)
            path_ok = unquote(self.path) == f"/model/{stub.model_id}/converse"
            kind = stub.receive(step if path_ok else "bad_path")
            if not path_ok or step == "unknown":
                self._send(400, {"message": "stub: not a known request"}, "ValidationException")
            elif kind == THROTTLE:
                self._send(429, {"message": "stub: throttled"}, "ThrottlingException")
            elif kind == ERROR_500:
                self._send(500, {"message": "stub: server error"}, "InternalServerException")
            elif kind == STALL:
                # Say nothing. The client gives up at its read timeout; this thread
                # ends when the test releases it, or fails the test if it never does.
                if not stub.release.wait(STALL_LIMIT_S):
                    stub.faults.append("stall_never_released")
            else:
                reply = stub.ok_response(step, body)
                reply["usage"] = dict(stub.usage.get(step, HONEST_USAGE))
                self._send(200, reply, None)
        except Exception as exc:        # noqa: BLE001 - reported to the test, by type only
            stub.faults.append(type(exc).__name__)

    def _send(self, status: int, payload: dict, error_type: str | None) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        if error_type:
            self.send_header("x-amzn-ErrorType", error_type)
        self.end_headers()
        self.wfile.write(data)


class BedrockStub:
    """`step_of(body)` names a request's step; `ok_response(step, body)` is the
    model reply for it. Script failures with `always` and `first`."""

    def __init__(self, *, model_id: str, step_of, ok_response):
        self.model_id = model_id
        self.step_of = step_of
        self.ok_response = ok_response
        self.usage: dict = {}             # step -> the usage an OK reply reports
        self.received: list = []          # one step name per request received, in order
        self.faults: list = []
        self.release = threading.Event()
        self._script: dict = {}
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    # -- scripting
    def always(self, step: str, kind: str) -> None:
        self._script[step] = (kind, None)

    def first(self, step: str, kind: str, times: int = 1) -> None:
        self._script[step] = (kind, times)

    # -- the count at the server
    def receive(self, step: str) -> str:
        with self._lock:
            self.received.append(step)
            kind, times = self._script.get(step, (OK, None))
            return kind if times is None or self.received.count(step) <= times else OK

    def count(self, step: str) -> int:
        with self._lock:
            return self.received.count(step)

    def counts(self) -> dict:
        with self._lock:
            return {s: self.received.count(s) for s in dict.fromkeys(self.received)}

    # -- lifecycle
    @property
    def address(self) -> tuple:
        return self._server.server_address[:2]

    @property
    def url(self) -> str:
        return "http://{}:{}".format(*self.address)

    def start(self) -> "BedrockStub":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.stub = self
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(JOIN_LIMIT_S)
        assert not self._thread.is_alive(), "the stub did not stop within its limit"
        assert not self.faults, f"the stub hit a fault: {self.faults}"


# ------------------------------------------------------------ the two targets

def pipeline_stub() -> BedrockStub:
    """Answers each pipeline step with the pipeline runner's own dry-run reply."""
    plan = reserve.plan("pipeline")
    by_tool = {s.tool_name: s.id for s in plan.steps}
    dry = rpe.DryRunBedrock()

    def step_of(body: dict) -> str:
        try:
            return by_tool.get(body["toolConfig"]["toolChoice"]["tool"]["name"], "unknown")
        except (KeyError, TypeError):
            return "unknown"

    def ok_response(step: str, _body: dict) -> dict:
        return dry.converse(toolConfig=rpe.MODEL_STEPS[step].tool_config())

    return BedrockStub(model_id=plan.model_id, step_of=step_of, ok_response=ok_response)


V1_COMBINED_TEXT = (f"PART A - DISCHARGE SUMMARY\n\nInvented stub summary. {SENTINEL}\n\n"
                    "PART B - GP LETTER\n\nInvented stub letter.\n\n"
                    "PART C - PATIENT VERSION\n\nInvented stub leaflet.\n")
V1_PATIENT_TEXT = "PART C - PATIENT VERSION\n\nInvented stub second-pass leaflet.\n"


def v1_stub() -> BedrockStub:
    plan = reserve.plan("v1")
    by_max_tokens = {s.max_tokens: s.id for s in plan.steps}
    text = {"combined": V1_COMBINED_TEXT, "patient_v2": V1_PATIENT_TEXT}

    def step_of(body: dict) -> str:
        try:
            return by_max_tokens.get(body["inferenceConfig"]["maxTokens"], "unknown")
        except (KeyError, TypeError):
            return "unknown"

    def ok_response(step: str, _body: dict) -> dict:
        return {"output": {"message": {"role": "assistant", "content": [{"text": text[step]}]}},
                "stopReason": "end_turn", "metrics": {"latencyMs": 1}}

    return BedrockStub(model_id=plan.model_id, step_of=step_of, ok_response=ok_response)


# ------------------------------------------------------------------- offline

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_AWS_VARS = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
             "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "AWS_ROLE_ARN",
             "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
             "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CA_BUNDLE")


@pytest.fixture
def offline_aws(monkeypatch, tmp_path):
    """No test using this can reach a real AWS endpoint. Returns `allowed`
    (addresses a socket may connect to) and `refused` (attempts that were not)."""
    for name in _PROXY_VARS + _AWS_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ws7-test-invented-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ws7-test-invented-value")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-such-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-such-aws-profile"))

    net = SimpleNamespace(allowed=set(), refused=[])
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _check(address) -> None:
        if not (isinstance(address, tuple) and tuple(address[:2]) in net.allowed):
            net.refused.append(address)
            raise AssertionError("a WS7 test may connect only to its local stub")

    def connect(sock, address):
        _check(address)
        return real_connect(sock, address)

    def connect_ex(sock, address):
        _check(address)
        return real_connect_ex(sock, address)

    def getaddrinfo(host, port, *args, **kwargs):
        # No name lookup leaves the machine: only the stub's own host is resolved.
        if host not in {a[0] for a in net.allowed}:
            net.refused.append((host, port))
            raise AssertionError("a WS7 test may look up only its local stub")
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield net
    assert not net.refused, f"a connection outside the stub was attempted: {net.refused}"


def started(stub: BedrockStub, net) -> BedrockStub:
    """Start a stub and let sockets connect to it, and only to it."""
    stub.start()
    net.allowed.add(stub.address)
    return stub
