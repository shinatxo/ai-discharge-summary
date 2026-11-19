"""Strict local stand-ins for S3 and SSM (ADR-011 D7, D11; slice 2, part 2).

Each fake is a real HTTP server on 127.0.0.1. The WS7 store builds a real
botocore client and points it at the fake, so botocore's signing, its transport
and its error parsing all run, and a conditional header is checked where it
arrives: at the server. Standard library only.

What the S3 fake implements, and nothing more:

- GET /<bucket>?list-type=2&prefix=...     ListObjectsV2, one page
- GET /<bucket>/<key>                      GetObject
- PUT /<bucket>/<key>                      PutObject with EXACTLY ONE of
                                           `If-None-Match: *` and `If-Match: <etag>`

Its answers to a conditional write are S3's documented ones (AWS, read
19 Nov 2026):

- "Conditional write behavior", Amazon S3 User Guide,
  https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
    If-None-Match on a key that exists     412 Precondition Failed
    If-Match with an ETag that differs     412 Precondition Failed
    If-Match on a key that is not there    404 Not Found
    a concurrent conditional write         409 Conflict
- PutObject, Amazon S3 API Reference,
  https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html
    names the 409's error code: ConditionalRequestConflict

A 409 cannot arise by itself here, because the fake handles one request at a
time; a test scripts it. A test can also script a write that lands and whose
reply then fails, and run a competing write between a caller's read and its
write (`before_put`), which gives a lost race with no threads and no sleeps.

STRICT: an unconditional PUT, a PUT with both conditions, any other method, an
unknown query parameter or another bucket is a FAULT. A fault is answered with
501 and fails the test when the fake stops. Extra request HEADERS are ignored:
botocore adds some (checksums, user agent) and a newer botocore may add more.

ETags are deliberately not the MD5 of the body. The real bucket is encrypted
with a KMS key, where an ETag is opaque, so code that computed one would pass
against MD5 ETags and fail in production.

`script_any` answers the next request, of any kind, with a given error instead
of handling it: how a test makes S3 point at another region. The region each
request was signed for is recorded (`signed_regions`).

A GET for a missing key answers 404 or 403 (`missing_status`): real S3 gives
either, depending on a permission (see evals/ws7_store.py). WS7 must behave
the same both ways.

The SSM fake implements GetParameter only.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
from xml.sax.saxutils import escape

JOIN_LIMIT_S = 5.0
BUCKET = "ws7-fake-eval-bucket"          # invented

# What a scripted PUT does instead of the ordinary thing.
CONFLICT_409 = "conflict_409"            # answer 409 ConditionalRequestConflict; write nothing
FAIL_500 = "fail_500"                    # answer 500; write nothing
DENY_403 = "deny_403"                    # answer 403 AccessDenied; write nothing
LAND_THEN_500 = "land_then_500"          # apply the write if its condition holds, then answer 500
LAND_THEN_DROP = "land_then_drop"        # apply the write, then close the connection unanswered
PUT_SCRIPTS = (CONFLICT_409, FAIL_500, DENY_403, LAND_THEN_500, LAND_THEN_DROP)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):       # nothing to stderr: a request line carries a key
        pass

    def _run(self, method: str) -> None:
        fake = self.server.fake
        self.close_connection = True     # one request per connection: no idle handler threads
        try:
            status, headers, payload = fake.handle(method, self.path, self.headers, self._body())
        except Exception as exc:         # noqa: BLE001 - reported to the test, by type only
            fake.faults.append(f"handler:{type(exc).__name__}")
            status, headers, payload = 500, {}, b""
        if status is None:               # a dropped connection: no reply at all
            return
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def _body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            raw = _read_http_chunks(self.rfile)
        else:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if "aws-chunked" in self.headers.get("Content-Encoding", "").lower():
                raw = _decode_aws_chunked(raw)
        return raw

    def do_GET(self):
        self._run("GET")

    def do_PUT(self):
        self._run("PUT")

    def do_POST(self):
        self._run("POST")

    def do_HEAD(self):
        self._run("HEAD")

    def do_DELETE(self):
        self._run("DELETE")


def _read_http_chunks(rfile) -> bytes:
    out = b""
    while True:
        size = int(rfile.readline().split(b";")[0].strip(), 16)
        if size == 0:
            while rfile.readline().strip():      # trailers, then the blank line
                pass
            return out
        out += rfile.read(size)
        rfile.readline()


def _decode_aws_chunked(raw: bytes) -> bytes:
    """botocore can frame a body as `<hex size>[;ext]\\r\\n<data>\\r\\n ... 0\\r\\n<trailers>`."""
    out, pos = b"", 0
    while True:
        end = raw.index(b"\r\n", pos)
        size = int(raw[pos:end].split(b";")[0], 16)
        if size == 0:
            return out
        out += raw[end + 2:end + 2 + size]
        pos = end + 2 + size + 2


class _Fake:
    """The server's lifecycle. `start`, `stop` and `address` match tests/ws7_stub.py's
    BedrockStub, so `ws7_stub.started(fake, net)` registers a fake with `offline_aws`."""

    def __init__(self):
        self.faults: list = []
        self._lock = threading.RLock()
        self._server = None
        self._thread = None

    @property
    def address(self) -> tuple:
        return self._server.server_address[:2]

    @property
    def url(self) -> str:
        return "http://{}:{}".format(*self.address)

    def start(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(JOIN_LIMIT_S)
        assert not self._thread.is_alive(), "the fake did not stop within its limit"
        assert not self.faults, f"the fake was asked for something it does not implement: {self.faults}"

    def _fault(self, what: str) -> tuple:
        self.faults.append(what)
        return 501, {"Content-Type": "application/xml"}, _s3_error("NotImplemented")


# ------------------------------------------------------------------------ S3

def _s3_error(code: str, extra: str = "") -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>{code}</Code>'
            f"<Message>fake</Message>{extra}<RequestId>fake</RequestId><HostId>fake</HostId></Error>"
            ).encode("utf-8")


# S3's two ways of saying "this bucket is in another region" (invented region).
PERMANENT_REDIRECT = (301, "PermanentRedirect", {"x-amz-bucket-region": "eu-west-1"},
                      "<Endpoint>s3.eu-west-1.amazonaws.com</Endpoint>")
WRONG_SIGNING_REGION = (400, "AuthorizationHeaderMalformed", {}, "<Region>eu-west-1</Region>")
SERVER_ERROR = (500, "InternalError", {}, "")


class S3Fake(_Fake):
    """One bucket, in memory. See the module docstring for what it implements."""

    def __init__(self, bucket: str = BUCKET):
        super().__init__()
        self.bucket = bucket
        self.missing_status = 404        # or 403: what a GET for a missing key answers
        self.truncate_listing = False    # answer a listing as truncated
        self.requests: list = []         # (method, key or "?list") per request received, in order
        self.puts: list = []             # (key, "if-none-match" | "if-match", status) per PUT
        self.missing_gets = 0            # GETs for a key that was not there
        self.signed_regions: list = []   # the region each request was signed for, in order
        self._any_script: list = []      # [(status, code, headers, extra xml), ...], one per request
        self._objects: dict = {}         # key -> (body, etag)
        self._seq = 0
        self._put_script: dict = {}      # key -> [script, ...], one consumed per PUT
        self._get_script: dict = {}      # key -> [status, ...], one consumed per GET
        self._list_script: list = []     # [status, ...], one consumed per listing
        self._before_put: dict = {}      # key -> [callable, ...], one run before the next PUT
        self._after_put: dict = {}       # key -> [callable, ...], one run after the next landed PUT

    # -- what a test does to the bucket directly (not through HTTP)
    def seed(self, key: str, body: bytes) -> str:
        """Put an object as another writer would have: unconditionally, with a new ETag."""
        with self._lock:
            self._seq += 1
            etag = '"' + hashlib.sha256(f"{self._seq}:{key}:opaque".encode()).hexdigest()[:32] + '"'
            self._objects[key] = (bytes(body), etag)
            return etag

    def remove(self, key: str) -> None:
        with self._lock:
            del self._objects[key]

    def body(self, key: str) -> bytes:
        with self._lock:
            return self._objects[key][0]

    def etag(self, key: str) -> str:
        with self._lock:
            return self._objects[key][1]

    def json(self, key: str):
        return json.loads(self.body(key).decode("utf-8"))

    def keys(self) -> list:
        with self._lock:
            return sorted(self._objects)

    def count(self, method: str, key: str | None = None) -> int:
        with self._lock:
            return sum(1 for m, k in self.requests if m == method and (key is None or k == key))

    # -- scripting
    def script_put(self, key: str, *scripts: str) -> None:
        assert all(s in PUT_SCRIPTS for s in scripts)
        self._put_script.setdefault(key, []).extend(scripts)

    def script_get(self, key: str, *statuses: int) -> None:
        self._get_script.setdefault(key, []).extend(statuses)

    def script_list(self, *statuses: int) -> None:
        self._list_script.extend(statuses)

    def script_any(self, *answers: tuple) -> None:
        """Answer the next requests, whatever they are, with these errors."""
        self._any_script.extend(answers)

    def before_put(self, key: str, action) -> None:
        """Run `action()` once, just before the next PUT of `key` is judged: a
        competing writer that got in between the caller's read and its write."""
        self._before_put.setdefault(key, []).append(action)

    def after_put(self, key: str, action) -> None:
        """Run `action()` once, just after the next PUT of `key` lands."""
        self._after_put.setdefault(key, []).append(action)

    # -- the server side
    def handle(self, method: str, raw_path: str, headers, body: bytes) -> tuple:
        with self._lock:
            parts = urlsplit(raw_path)
            path = unquote(parts.path)
            query = parse_qs(parts.query, keep_blank_values=True)
            bucket, _, key = path.lstrip("/").partition("/")
            if bucket != self.bucket:
                return self._fault("another_bucket")
            # "AWS4-HMAC-SHA256 Credential=<id>/<date>/<region>/s3/aws4_request, ..."
            scope = headers.get("Authorization", "").partition("Credential=")[2].split("/")
            self.signed_regions.append(scope[2] if len(scope) > 2 else None)
            if self._any_script and method in ("GET", "PUT"):
                status, code, extra_headers, extra = self._any_script.pop(0)
                self.requests.append((method, key or "?list"))
                return status, {"Content-Type": "application/xml", **extra_headers}, _s3_error(code, extra)
            if method == "GET" and not key:
                return self._list(query)
            if method == "GET" and key:
                return self._get(key, query)
            if method == "PUT" and key:
                return self._put(key, query, headers, body)
            self.requests.append((method, key))
            return self._fault(f"method:{method}")

    def _list(self, query: dict) -> tuple:
        self.requests.append(("GET", "?list"))
        unknown = set(query) - {"list-type", "prefix", "encoding-type"}
        if query.get("list-type") != ["2"] or unknown:
            return self._fault("listing:" + ",".join(sorted(unknown)) if unknown else "listing:v1")
        if self._list_script:
            return self._list_script.pop(0), {"Content-Type": "application/xml"}, _s3_error("InternalError")
        prefix = query.get("prefix", [""])[0]
        encode = query.get("encoding-type") == ["url"]
        keys = sorted(k for k in self._objects if k.startswith(prefix))

        def text(value: str) -> str:
            return escape(quote(value, safe="/") if encode else value)

        items = "".join(
            f"<Contents><Key>{text(k)}</Key><LastModified>2026-01-01T00:00:00.000Z</LastModified>"
            f"<ETag>{escape(self._objects[k][1])}</ETag><Size>{len(self._objects[k][0])}</Size>"
            f"<StorageClass>STANDARD</StorageClass></Contents>" for k in keys)
        truncated = "true<NextContinuationToken>fake</NextContinuationToken>" if self.truncate_listing \
            else "false"
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
               f"<Name>{self.bucket}</Name><Prefix>{text(prefix)}</Prefix>"
               f"<KeyCount>{len(keys)}</KeyCount><MaxKeys>1000</MaxKeys>"
               + ("<EncodingType>url</EncodingType>" if encode else "")
               + f"<IsTruncated>{truncated}</IsTruncated>{items}</ListBucketResult>")
        return 200, {"Content-Type": "application/xml"}, xml.encode("utf-8")

    def _get(self, key: str, query: dict) -> tuple:
        self.requests.append(("GET", key))
        if query:
            return self._fault("get_param:" + ",".join(sorted(query)))
        scripted = self._get_script.get(key)
        if scripted:
            status = scripted.pop(0)
            return status, {"Content-Type": "application/xml"}, _s3_error(
                "AccessDenied" if status == 403 else "InternalError")
        if key not in self._objects:
            self.missing_gets += 1
            code = {404: "NoSuchKey", 403: "AccessDenied"}[self.missing_status]
            return self.missing_status, {"Content-Type": "application/xml"}, _s3_error(code)
        data, etag = self._objects[key]
        return 200, {"Content-Type": "application/json", "ETag": etag}, data

    def _put(self, key: str, query: dict, headers, body: bytes) -> tuple:
        self.requests.append(("PUT", key))
        if query:
            return self._fault("put_param:" + ",".join(sorted(query)))
        if_none_match, if_match = headers.get("If-None-Match"), headers.get("If-Match")
        if if_none_match is None and if_match is None:
            return self._fault("unconditional_put")
        if if_none_match is not None and if_match is not None:
            return self._fault("both_conditions")
        if if_none_match is not None and if_none_match != "*":
            return self._fault("if_none_match_not_star")
        kind = "if-none-match" if if_match is None else "if-match"

        for action in [self._before_put[key].pop(0)] if self._before_put.get(key) else []:
            action()
        script = self._put_script[key].pop(0) if self._put_script.get(key) else None
        if script == CONFLICT_409:
            return self._answer(key, kind, 409, "ConditionalRequestConflict")
        if script == FAIL_500:
            return self._answer(key, kind, 500, "InternalError")
        if script == DENY_403:
            return self._answer(key, kind, 403, "AccessDenied")

        # The condition, as S3 judges it.
        if kind == "if-none-match" and key in self._objects:
            status, code = 412, "PreconditionFailed"
        elif kind == "if-match" and key not in self._objects:
            status, code = 404, "NoSuchKey"
        elif kind == "if-match" and self._objects[key][1] != if_match:
            status, code = 412, "PreconditionFailed"
        else:
            status, code = 200, None
            etag = self.seed(key, body)
            for action in [self._after_put[key].pop(0)] if self._after_put.get(key) else []:
                action()

        if script == LAND_THEN_500:
            return self._answer(key, kind, 500, "InternalError")
        if script == LAND_THEN_DROP:
            self.puts.append((key, kind, None))
            return None, {}, b""
        if status == 200:
            self.puts.append((key, kind, 200))
            return 200, {"ETag": etag}, b""
        return self._answer(key, kind, status, code)

    def _answer(self, key: str, kind: str, status: int, code: str) -> tuple:
        self.puts.append((key, kind, status))
        return status, {"Content-Type": "application/xml"}, _s3_error(code)


# ----------------------------------------------------------------------- SSM

class SsmFake(_Fake):
    """GetParameter, and nothing else."""

    def __init__(self):
        super().__init__()
        self.parameters: dict = {}       # name -> (value, type)
        self.requests: list = []         # parameter names asked for, in order
        self._script: list = []          # [status, ...], one consumed per request

    def put(self, name: str, value: str, type_: str = "String") -> None:
        self.parameters[name] = (value, type_)

    def script(self, *statuses: int) -> None:
        self._script.extend(statuses)

    def handle(self, method: str, raw_path: str, headers, body: bytes) -> tuple:
        with self._lock:
            json_type = {"Content-Type": "application/x-amz-json-1.1"}
            target = headers.get("X-Amz-Target")
            if method != "POST" or urlsplit(raw_path).path != "/" or target != "AmazonSSM.GetParameter":
                self.faults.append(f"ssm:{method}:{target}")
                return 501, json_type, b'{"__type":"NotImplemented","message":"fake"}'
            asked = json.loads(body.decode("utf-8"))
            if set(asked) - {"Name", "WithDecryption"} or type(asked.get("Name")) is not str:
                self.faults.append("ssm:parameters")
                return 501, json_type, b'{"__type":"NotImplemented","message":"fake"}'
            name = asked["Name"]
            self.requests.append(name)
            if self._script:
                return self._script.pop(0), json_type, b'{"__type":"InternalServerError","message":"fake"}'
            if name not in self.parameters:
                return 400, json_type, b'{"__type":"ParameterNotFound","message":"fake"}'
            value, type_ = self.parameters[name]
            reply = {"Parameter": {"Name": name, "Type": type_, "Value": value, "Version": 1,
                                   "DataType": "text",
                                   "ARN": f"arn:aws:ssm:eu-west-2:000000000000:parameter{name}"}}
            return 200, json_type, json.dumps(reply).encode("utf-8")
