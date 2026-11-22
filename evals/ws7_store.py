#!/usr/bin/env python3
"""WS7 store: the S3, SSM, CloudWatch and SNS clients, S3's conditional writes, and
the key layout (ADR-011 D6, D7, D8, D9).

Four things live here, and nothing else in WS7 builds an S3, SSM, CloudWatch or
SNS client or names an S3 key:

1. `build_client`: the one place WS7 creates an S3, SSM, CloudWatch or SNS
   client. One attempt per call (the callers do their own, counted, retries),
   eu-west-2 only, and endpoint URLs set in the environment or the AWS config
   file are ignored. A test endpoint must be http://127.0.0.1:<port>.
2. `Store`: list, get and two conditional puts. THERE IS NO UNCONDITIONAL
   WRITE and no delete. Every key must sit under `ws7/`.
3. The key layout: claims, ledgers, run prefixes, success records and failed
   records (docs/WS7-ENTRYPOINT.md, "The S3 layout"). The retained prefix for
   incident copies is named here and sits OUTSIDE `ws7/`, so this store can
   never write it.
4. `Stop`: how every WS7 refusal is raised. It carries a fixed code, IDs and
   counts, and nothing else: no note text, no model output, no AWS message.

How S3 answers a conditional write (AWS, read 19 Nov 2026):

- "Conditional write behavior", Amazon S3 User Guide,
  https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
    If-None-Match: "If there's an existing object, the write operation fails,
    resulting in a `412 Precondition Failed` response."
    If-Match: "If the ETag doesn't match, the write operation fails with a
    `412 Precondition Failed` response." "You can also receive a `409 Conflict`
    response in the case of concurrent requests." "If there's no current object
    version with the same name ... the operation fails with a `404 Not Found`
    error."
- PutObject, Amazon S3 API Reference,
  https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html
    If-None-Match: "If a conflicting operation occurs during the upload S3
    returns a `409 ConditionalRequestConflict` response. On a 409 failure you
    should retry the upload."
    If-Match: "On a 409 failure you should fetch the object's ETag and retry
    the upload."

Absence is decided from a listing, never from a GET error. S3 answers a GET
for a missing key with 404 only when the caller has `s3:ListBucket`, and with
403 otherwise (GetObject, API Reference,
https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html). AWS does
not say which a grant limited by `s3:prefix` gives. So WS7 lists a prefix
first, GETs only keys the listing returned, and treats any GET error on a
listed key as a failure.

A write whose reply failed (a 5xx, a timeout, a dropped connection) may have
landed. It is AMBIGUOUS, never assumed either way: the caller reads the object
back and looks for its own token.

A WS7 S3 client never follows a redirect and never sends one call twice.
botocore registers a region redirector on every S3 client
(`S3RegionRedirectorv2`, botocore 1.43.110 `client.py:404`, `utils.py:1758`). On
a 301, 302 or 307, a `PermanentRedirect`, an `AuthorizationHeaderMalformed` that
names a Region, or an `IllegalLocationConstraintException`, it re-signs the
request for the region S3 named, remembers that region for the bucket, and
returns 0 from `needs-retry`; `endpoint.py` (`_needs_retry`, `:352`) then sends
the request again, whatever `total_max_attempts` says. Two things stop that
here, each tested alone:

- `_never_redirect`: before every call, the request is marked as already
  redirected, which is the redirector's own reason to do nothing
  (`utils.py:1778`). It then neither re-sends nor remembers another region.
- `_SendOnce`: a second send within one call is refused before it leaves,
  whatever asked for it.

Such an answer is a failure with a fixed code (`ws7_store_wrong_region`), and
for a write it is not ambiguous: a redirect did not land.

An ETag is opaque here. The eval bucket is encrypted with a KMS key (D8, D9), so
an ETag is not the MD5 of the object; WS7 only hands back the one it was given.

No AWS call is made by importing this module.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

TARGETS = ("pipeline", "v1")
SCHEDULED, MANUAL = "scheduled", "manual"
MODES = (SCHEDULED, MANUAL)

LIVE_REGION = "eu-west-2"
# Written out rather than built from the region.
LIVE_ENDPOINTS = {"s3": "https://s3.eu-west-2.amazonaws.com",
                  "ssm": "https://ssm.eu-west-2.amazonaws.com",
                  "cloudwatch": "https://monitoring.eu-west-2.amazonaws.com",
                  "sns": "https://sns.eu-west-2.amazonaws.com"}
_STUB_ENDPOINT_RE = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}")
RETRIES = {"mode": "standard", "total_max_attempts": 1}   # the callers count their own tries
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 20

PREFIX = "ws7/"                       # the task role's whole reach in the bucket (D6)
LEDGER_PREFIX = PREFIX + "ledger/"
# Kept, so outside `ws7/runs/scheduled/`, which the lifecycle rule expires (D8).
SUCCESS_PREFIX = PREFIX + "success/"
FAILED_PREFIX = PREFIX + "failed/"
# Incident copies (D10). OUTSIDE `ws7/`: the task role cannot reach it and `Store`
# refuses every key under it, so the job can never overwrite a copy the author made.
RETAINED_PREFIX = "retained/"
MAX_OBJECT_BYTES = 1_048_576          # a claim, a ledger or a manifest is far smaller
# What an object may be stored as. A run's evidence is JSON and Markdown, and nothing else.
JSON_TYPE = "application/json"
MARKDOWN_TYPE = "text/markdown; charset=utf-8"
CONTENT_TYPES = {".json": JSON_TYPE, ".md": MARKDOWN_TYPE}

# A bucket WS7 will talk to: an ordinary general-purpose bucket name. No dots, and no
# "--", which rules out directory buckets (--x-s3) and the other suffixes that make
# botocore choose a different endpoint.
_BUCKET_RE = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?!-)){1,61}[a-z0-9]")
_KEY_RE = re.compile(r"ws7/[A-Za-z0-9._/-]{1,200}")
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")        # the comparator's ID pattern
_LABEL_RE = re.compile(r"[a-z0-9_]{1,64}")
_MONTH_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_PART_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")
_UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

# What a conditional put came to.
WRITTEN = "written"
EXISTS = "exists"            # If-None-Match: the key is taken (412)
CHANGED = "changed"          # If-Match: the object is not the one that was read (412)
CONFLICT = "conflict"        # a concurrent conditional write (409 ConditionalRequestConflict)
AMBIGUOUS = "ambiguous"      # the reply failed; the write may have landed
# What `create` came to.
CREATED, TAKEN = "created", "taken"

ENDPOINT_REFUSED = "ws7_store_endpoint_refused"
BUCKET_REFUSED = "ws7_store_bucket_refused"
KEY_REFUSED = "ws7_store_key_refused"
LIST_FAILED = "ws7_store_list_failed"
LIST_TRUNCATED = "ws7_store_list_truncated"
GET_FAILED = "ws7_store_get_failed"
OBJECT_TOO_LARGE = "ws7_store_object_too_large"
PUT_REFUSED = "ws7_store_put_refused"
OBJECT_MISSING = "ws7_store_object_missing"
CREATE_UNSURE = "ws7_store_create_unsure"
WRONG_REGION = "ws7_store_wrong_region"
RESEND_REFUSED = "ws7_store_resend_refused"
CODES = (ENDPOINT_REFUSED, BUCKET_REFUSED, KEY_REFUSED, LIST_FAILED, LIST_TRUNCATED, GET_FAILED,
         OBJECT_TOO_LARGE, PUT_REFUSED, OBJECT_MISSING, CREATE_UNSURE, WRONG_REGION,
         RESEND_REFUSED)

# What S3 answers when it wants the request sent somewhere else, or signed for
# another region: the answers botocore's redirector acts on.
_REDIRECT_STATUSES = (301, 302, 307)
_REDIRECT_CODES = ("PermanentRedirect", "TemporaryRedirect", "AuthorizationHeaderMalformed",
                   "IllegalLocationConstraintException")

REFUSED_METRIC = "RunRefused"


# ------------------------------------------------------------------- stops

class Stop(Exception):
    """A WS7 refusal: a fixed code, IDs and counts.

    `str()` is the code alone. `ids` are run IDs, months and fixed field names;
    `counts` are whole numbers under fixed names. A value that is not an ID or a
    count cannot be put in one, so no text can travel in a refusal."""

    metric = REFUSED_METRIC

    def __init__(self, code: str, *, ids: tuple = (), counts: dict | None = None):
        counts = dict(counts or {})
        if type(code) is not str or not _LABEL_RE.fullmatch(code):
            raise ValueError("a stop code is a fixed label")
        if not all(type(i) is str and ID_RE.fullmatch(i) for i in ids):
            raise ValueError("a stop carries IDs only")
        if not all(type(k) is str and _LABEL_RE.fullmatch(k) and type(v) is int
                   for k, v in counts.items()):
            raise ValueError("a stop carries whole-number counts only")
        super().__init__(code)
        self.code = code
        self.ids = tuple(ids)
        self.counts = counts

    def line(self) -> str:
        """One line for a log or an alert: the code, the IDs and the counts."""
        parts = [f"ws7 stop code={self.code}"]
        if self.ids:
            parts.append("ids=" + ",".join(self.ids))
        parts.extend(f"{name}={value}" for name, value in sorted(self.counts.items()))
        return " ".join(parts)


class StoreError(Stop):
    """S3 could not be read or written as asked. `code` is one of CODES."""


# -------------------------------------------------------------------- time

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_clock(clock) -> datetime:
    """The clock's time as whole UTC seconds. A clock without a zone is an error."""
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("the clock must give a time with a zone")
    return now.astimezone(timezone.utc).replace(microsecond=0)


def utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: object) -> datetime | None:
    """`YYYY-MM-DDTHH:MM:SSZ`, or None."""
    if type(text) is not str or not _UTC_RE.fullmatch(text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_month(text: object) -> bool:
    return type(text) is str and _MONTH_RE.fullmatch(text) is not None


# -------------------------------------------------------------- key layout

def run_id(target: str, mode: str, scheduled: datetime) -> str:
    """Scheduled: `<UTC date>T<HHMM>-<target>`. Manual: the same with `-manual`. A
    target is never called "manual", so the two cannot collide."""
    _check_target_mode(target, mode)
    stamp = scheduled.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M")
    return f"{stamp}-{target}" if mode == SCHEDULED else f"{stamp}-{target}-{MANUAL}"


def claim_key(target: str, mode: str, scheduled: datetime) -> str:
    """Scheduled: one key per target per UTC date of the scheduled time (ADR-011 D7).
    Manual: a key of its own, by run ID, so a manual run can never take a scheduled
    occurrence's claim."""
    _check_target_mode(target, mode)
    if mode == SCHEDULED:
        return f"{PREFIX}claims/{target}/{scheduled.astimezone(timezone.utc):%Y-%m-%d}"
    return f"{PREFIX}claims/{target}/{MANUAL}/{run_id(target, mode, scheduled)}"


def ledger_key(month: str) -> str:
    if not is_month(month):
        raise ValueError("a ledger month is YYYY-MM")
    return f"{LEDGER_PREFIX}{month}.json"


def ledger_month(key: object) -> str | None:
    """The month a key under the ledger prefix is for, or None if it is not a ledger."""
    if type(key) is str and key.startswith(LEDGER_PREFIX) and key.endswith(".json"):
        month = key[len(LEDGER_PREFIX):-len(".json")]
        if is_month(month):
            return month
    return None


def run_prefix(mode: str, run: str) -> str:
    """Where a run's records go. Scheduled runs expire after 90 days and manual ones
    are kept (D8), and a lifecycle rule selects by prefix, so the two never share one."""
    if mode not in MODES or type(run) is not str or not ID_RE.fullmatch(run):
        raise ValueError("a run prefix needs a mode and a run ID")
    return f"{PREFIX}runs/{mode}/{run}/"


def _run_id_text(run: object) -> str:
    if type(run) is not str or not ID_RE.fullmatch(run):
        raise ValueError("a run ID is needed")
    return run


def success_key(run: str) -> str:
    """A scheduled run's success record: written once, as the run's last act but one
    (D9). Kept, so it is not under the run's own prefix."""
    return f"{SUCCESS_PREFIX}{_run_id_text(run)}.json"


def failed_key(run: str) -> str:
    """The failed record of a scheduled run that stopped while it held its claim. The
    Thursday check reads it. Kept."""
    return f"{FAILED_PREFIX}{_run_id_text(run)}.json"


def retained_prefix(run: str) -> str:
    """Where the author copies a run that became an incident (D10). It is outside
    `ws7/`: nothing in WS7 writes it, and `Store` refuses every key under it."""
    return f"{RETAINED_PREFIX}{_run_id_text(run)}/"


def evidence_key(mode: str, run: str, *parts: str) -> str:
    """One stored file of a run, under the run's own prefix. Each part is a plain
    name: letters, digits, `.`, `_` and `-`, and never `.` or `..`."""
    if not parts or not all(type(p) is str and _PART_RE.fullmatch(p) and p.strip(".")
                            for p in parts):
        raise ValueError("an evidence key is made of plain names")
    return run_prefix(mode, run) + "/".join(parts)


def content_type(name: str) -> str | None:
    """What a file of this name is stored as, or None: it is not stored at all."""
    return next((kind for suffix, kind in CONTENT_TYPES.items() if name.endswith(suffix)), None)


def _check_target_mode(target: str, mode: str) -> None:
    if target not in TARGETS or mode not in MODES:
        raise ValueError("an unknown target or mode")


# ----------------------------------------------------------------- clients

class ResendRefused(Exception):
    """A second send within one S3 call was stopped before it left."""


def _never_redirect(context, **_kwargs) -> None:
    """`before-call.s3`: mark the request as already redirected, so botocore's
    region redirector does nothing with whatever S3 answers."""
    context.setdefault("s3_redirect", {})["redirected"] = True


class _SendOnce:
    """One send per S3 call. `new_call` runs at `before-call.s3`, `before_send` at
    `before-send.s3`; the count is per thread, as a call is."""

    def __init__(self):
        self._local = threading.local()

    def new_call(self, **_kwargs) -> None:
        self._local.sends = 0

    def before_send(self, **_kwargs) -> None:
        self._local.sends = getattr(self._local, "sends", 0) + 1
        if self._local.sends > 1:
            raise ResendRefused()


def build_client(service: str, *, endpoint_url: str | None = None):
    """The one place WS7 creates an S3, SSM, CloudWatch or SNS client.

    Endpoint URLs configured in the environment (AWS_ENDPOINT_URL,
    AWS_ENDPOINT_URL_S3, AWS_ENDPOINT_URL_SSM, AWS_ENDPOINT_URL_CLOUDWATCH,
    AWS_ENDPOINT_URL_SNS) or in the AWS config file are
    ignored: botocore's `ignore_configured_endpoint_urls=True`, as
    evals/ws7_client.py uses for Bedrock. The live path passes no `endpoint_url`
    and must resolve to the service's own endpoint in eu-west-2; a FIPS or
    dual-stack variant is refused here, before any request. `endpoint_url` is for
    the tests' fakes and must be http://127.0.0.1:<port>."""
    import boto3
    from botocore.config import Config
    if service not in LIVE_ENDPOINTS:
        raise StoreError(ENDPOINT_REFUSED)
    if endpoint_url is not None and not (type(endpoint_url) is str
                                         and _STUB_ENDPOINT_RE.fullmatch(endpoint_url)):
        raise StoreError(ENDPOINT_REFUSED)
    options: dict = {"connect_timeout": CONNECT_TIMEOUT_S, "read_timeout": READ_TIMEOUT_S,
                     "retries": dict(RETRIES), "ignore_configured_endpoint_urls": True}
    if service == "s3":
        # Set here so that an `s3 =` section in a config file cannot change them.
        options["s3"] = {"addressing_style": "virtual" if endpoint_url is None else "path",
                         "use_accelerate_endpoint": False}
    client = boto3.Session(region_name=LIVE_REGION).client(
        service, config=Config(**options), endpoint_url=endpoint_url)
    expected = LIVE_ENDPOINTS[service] if endpoint_url is None else endpoint_url
    if client.meta.endpoint_url != expected or client.meta.region_name != LIVE_REGION:
        raise StoreError(ENDPOINT_REFUSED)
    if service == "s3":
        # Never follow a redirect, and never send one call twice (module docstring).
        once = _SendOnce()
        client.meta.events.register("before-call.s3", _never_redirect)
        client.meta.events.register("before-call.s3", once.new_call)
        client.meta.events.register("before-send.s3", once.before_send)
    return client


# ------------------------------------------------------------------- store

def _status_and_code(exc: ClientError) -> tuple:
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return (status if type(status) is int else None), exc.response.get("Error", {}).get("Code")


def _never_followed(exc: BaseException) -> str | None:
    """The fixed code for an answer WS7 will not act on, or None: S3 pointing at
    another region or endpoint, or a second send that was stopped."""
    if isinstance(exc, ResendRefused):
        return RESEND_REFUSED
    if isinstance(exc, ClientError):
        status, code = _status_and_code(exc)
        if status in _REDIRECT_STATUSES or code in _REDIRECT_CODES:
            return WRONG_REGION
    return None


class Store:
    """The eval bucket's `ws7/` prefix, through one S3 client."""

    def __init__(self, client, bucket: str):
        if type(bucket) is not str or not _BUCKET_RE.fullmatch(bucket):
            raise StoreError(BUCKET_REFUSED)
        self._s3 = client
        self.bucket = bucket

    @staticmethod
    def _check_key(key: object) -> None:
        if type(key) is not str or not _KEY_RE.fullmatch(key) or "//" in key or "/../" in key:
            raise StoreError(KEY_REFUSED)

    def list_keys(self, prefix: str) -> list:
        """Every key under `prefix`, in one page. A truncated listing is refused: a
        key it left out would be read as absent."""
        self._check_key(prefix)
        try:
            page = self._s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        except (ClientError, BotoCoreError, ResendRefused) as exc:
            raise StoreError(_never_followed(exc) or LIST_FAILED) from None
        if page.get("IsTruncated") is not False:
            raise StoreError(LIST_TRUNCATED)
        keys = [item.get("Key") for item in page.get("Contents", [])]
        if not all(type(k) is str and k.startswith(prefix) for k in keys):
            raise StoreError(LIST_FAILED)
        return sorted(keys)

    def get(self, key: str) -> tuple:
        """The object's bytes and its ETag. Call it only for a key a listing
        returned: any error here, a 404 and a 403 alike, is a failure."""
        self._check_key(key)
        try:
            reply = self._s3.get_object(Bucket=self.bucket, Key=key)
            body = reply["Body"].read(MAX_OBJECT_BYTES + 1)
        except (ClientError, BotoCoreError, ResendRefused) as exc:
            raise StoreError(_never_followed(exc) or GET_FAILED) from None
        etag = reply.get("ETag")
        if type(etag) is not str or not etag or type(body) is not bytes:
            raise StoreError(GET_FAILED)
        if len(body) > MAX_OBJECT_BYTES:
            raise StoreError(OBJECT_TOO_LARGE)
        return body, etag

    def put_if_absent(self, key: str, body: bytes, *, kind: str = JSON_TYPE) -> tuple:
        """Write only if the key is free (`If-None-Match: *`). Returns the outcome
        and, when WRITTEN, the new ETag. `kind` is the content type: JSON, or
        Markdown for a stored report."""
        return self._put(key, body, EXISTS, kind, IfNoneMatch="*")

    def put_if_match(self, key: str, body: bytes, etag: str) -> tuple:
        """Write only if the object is still the one that was read (`If-Match`)."""
        if type(etag) is not str or not etag:
            raise StoreError(PUT_REFUSED)
        return self._put(key, body, CHANGED, JSON_TYPE, IfMatch=etag)

    def _put(self, key: str, body: bytes, failed_condition: str, kind: str,
             **condition) -> tuple:
        self._check_key(key)
        if type(body) is not bytes or len(body) > MAX_OBJECT_BYTES:
            raise StoreError(OBJECT_TOO_LARGE)
        if type(kind) is not str or kind not in CONTENT_TYPES.values():
            raise StoreError(PUT_REFUSED)               # never sent
        try:
            reply = self._s3.put_object(Bucket=self.bucket, Key=key, Body=body,
                                        ContentType=kind, **condition)
        except ResendRefused:
            raise StoreError(RESEND_REFUSED) from None
        except ClientError as exc:
            status, code = _status_and_code(exc)
            if _never_followed(exc):
                raise StoreError(WRONG_REGION) from None        # a redirect did not land
            if status == 412 and code == "PreconditionFailed":
                return failed_condition, None
            if status == 409 and code == "ConditionalRequestConflict":
                return CONFLICT, None
            if status == 404:
                raise StoreError(OBJECT_MISSING) from None      # If-Match on a key that is gone
            if status is not None and 400 <= status < 500 and status not in (409, 412):
                raise StoreError(PUT_REFUSED) from None         # refused outright: it did not land
            return AMBIGUOUS, None
        except ParamValidationError:
            raise StoreError(PUT_REFUSED) from None             # botocore never sent it
        except BotoCoreError:
            return AMBIGUOUS, None
        etag = reply.get("ETag")
        # A 200 with no ETag: the write landed, but the next If-Match has nothing to use.
        return (WRITTEN, etag) if type(etag) is str and etag else (AMBIGUOUS, None)

    def create(self, key: str, body: bytes, *, attempts: int, kind: str = JSON_TYPE) -> str:
        """Create `key` once. CREATED: the object is this call's bytes. TAKEN: it is
        someone else's.

        `body` must carry a token no other writer has, so that after a reply that
        failed the object can be read back and recognised. `attempts` counts tries
        in total. A try whose reply failed is checked from a listing: if the key
        is listed, its bytes decide; if not, the write has not landed and the next
        try may make it."""
        if type(attempts) is not int or attempts < 1:
            raise ValueError("attempts must be a whole number of one or more")
        unsure = False
        for _ in range(attempts):
            outcome, _etag = self.put_if_absent(key, body, kind=kind)
            if outcome == WRITTEN:
                return CREATED
            if outcome == EXISTS and not unsure:
                return TAKEN
            if outcome == AMBIGUOUS:
                unsure = True
            if outcome in (EXISTS, AMBIGUOUS) and key in self.list_keys(key):
                return CREATED if self.get(key)[0] == body else TAKEN
        raise StoreError(CREATE_UNSURE)
