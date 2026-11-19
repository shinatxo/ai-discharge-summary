"""evals/ws7_store.py - the S3 and SSM clients, conditional writes and the key layout
(ADR-011 D6, D7, D8; slice 2 part 2).

Every client here is a real botocore client pointed at a strict fake on
127.0.0.1 (tests/ws7_fakes.py), or is built and inspected without being called.
`offline_aws` (tests/ws7_stub.py) applies to every test in this file: invented
credentials, no metadata service, and a socket may connect only to an address
the test registered. No test here can reach a real AWS endpoint.

Every key, body and ETag is invented.
"""

from __future__ import annotations

import hashlib
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "evals"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ws7_fakes as fakes  # noqa: E402
import ws7_store as store  # noqa: E402
import ws7_stub as stubs  # noqa: E402
from ws7_stub import offline_aws  # noqa: E402,F401  (a fixture)

KEY = "ws7/ledger/2026-12.json"          # invented
OTHER = b'{"writer":"another"}'
MINE = b'{"writer":"this one"}'
CALL_LIMIT_S = 10.0                      # one call to a local fake never takes this long


@pytest.fixture(autouse=True)
def _offline(offline_aws):  # noqa: F811
    """Every test in this file runs offline."""
    return offline_aws


@pytest.fixture
def s3(_offline):
    fake = stubs.started(fakes.S3Fake(), _offline)
    yield fake
    fake.stop()


@pytest.fixture
def st(s3):
    return store.Store(store.build_client("s3", endpoint_url=s3.url), fakes.BUCKET)


def code_of(call) -> str:
    with pytest.raises(store.StoreError) as exc:
        call()
    assert exc.value.code in store.CODES
    return exc.value.code


# ====================================================== conditional writes ==

def test_if_none_match_writes_a_free_key_and_is_refused_once_it_is_taken(s3, st):
    outcome, etag = st.put_if_absent(KEY, MINE)
    assert (outcome, etag) == (store.WRITTEN, s3.etag(KEY))
    assert st.put_if_absent(KEY, OTHER) == (store.EXISTS, None)
    assert s3.body(KEY) == MINE                                   # the first writer's, untouched
    # At the server: both arrived carrying If-None-Match, and S3's answers were 200 and 412.
    assert s3.puts == [(KEY, "if-none-match", 200), (KEY, "if-none-match", 412)]


def test_if_match_writes_only_with_the_etag_that_was_read(s3, st):
    s3.seed(KEY, OTHER)
    _body, read = st.get(KEY)
    outcome, new = st.put_if_match(KEY, MINE, read)
    assert outcome == store.WRITTEN and new == s3.etag(KEY) != read
    assert st.put_if_match(KEY, OTHER, read) == (store.CHANGED, None)     # the stale ETag
    assert s3.body(KEY) == MINE
    assert s3.puts == [(KEY, "if-match", 200), (KEY, "if-match", 412)]


def test_if_match_on_a_key_that_is_gone_is_a_failure_not_a_conflict(s3, st):
    etag = s3.seed(KEY, OTHER)
    assert st.put_if_match(KEY, MINE, etag)[0] == store.WRITTEN           # the partner
    s3.remove(KEY)
    assert code_of(lambda: st.put_if_match(KEY, MINE, etag)) == store.OBJECT_MISSING
    assert s3.puts[-1] == (KEY, "if-match", 404)


@pytest.mark.parametrize("kind", ["if-none-match", "if-match"])
def test_a_409_is_a_conflict_and_writes_nothing(s3, st, kind):
    etag = s3.seed(KEY, OTHER) if kind == "if-match" else None
    s3.script_put(KEY, fakes.CONFLICT_409)
    put = (lambda: st.put_if_match(KEY, MINE, etag)) if etag else (lambda: st.put_if_absent(KEY, MINE))
    assert put() == (store.CONFLICT, None)
    assert s3.puts == [(KEY, kind, 409)]
    assert put()[0] == store.WRITTEN                                      # the partner: no script


@pytest.mark.parametrize("script,landed", [(fakes.FAIL_500, False), (fakes.LAND_THEN_500, True),
                                           (fakes.LAND_THEN_DROP, True)])
def test_a_write_whose_reply_failed_is_ambiguous_whether_or_not_it_landed(s3, st, script, landed):
    s3.script_put(KEY, script)
    started = time.monotonic()
    assert st.put_if_absent(KEY, MINE) == (store.AMBIGUOUS, None)
    assert time.monotonic() - started < CALL_LIMIT_S, "the call waited on something"
    assert (KEY in s3.keys()) is landed
    assert s3.count("PUT", KEY) == 1                                      # one attempt: no retry


def test_a_write_refused_outright_did_not_land_and_is_not_ambiguous(s3, st):
    s3.script_put(KEY, fakes.DENY_403)
    assert code_of(lambda: st.put_if_absent(KEY, MINE)) == store.PUT_REFUSED
    assert KEY not in s3.keys()
    assert st.put_if_absent(KEY, MINE)[0] == store.WRITTEN                # the partner


def test_an_etag_is_opaque_and_is_not_the_md5_of_the_body(s3, st):
    """The real bucket is KMS-encrypted, where an ETag is not an MD5. The fake's are
    not either, so code that computed one would fail here."""
    _outcome, etag = st.put_if_absent(KEY, MINE)
    assert etag.strip('"') != hashlib.md5(MINE).hexdigest()
    assert st.get(KEY) == (MINE, etag)                                    # handed back as given


# ---------------------------------------------------- the fake is strict

def test_the_fake_faults_on_an_unconditional_put_and_stores_nothing(s3):
    """The control for every conditional-write test: a write with no condition does
    not pass quietly."""
    raw = store.build_client("s3", endpoint_url=s3.url)
    with pytest.raises(ClientError):
        raw.put_object(Bucket=fakes.BUCKET, Key=KEY, Body=MINE)
    assert s3.faults == ["unconditional_put"] and KEY not in s3.keys()
    s3.faults.clear()                                                     # expected: do not fail teardown


def test_the_fake_faults_on_what_it_does_not_implement(s3):
    raw = store.build_client("s3", endpoint_url=s3.url)
    calls = [lambda: raw.put_object(Bucket=fakes.BUCKET, Key=KEY, Body=MINE, IfNoneMatch="*",
                                    IfMatch='"x"'),
             lambda: raw.delete_object(Bucket=fakes.BUCKET, Key=KEY),
             lambda: raw.head_object(Bucket=fakes.BUCKET, Key=KEY),
             lambda: raw.get_object(Bucket=fakes.BUCKET, Key=KEY, VersionId="v1"),
             lambda: raw.list_objects_v2(Bucket=fakes.BUCKET, Prefix="ws7/", Delimiter="/"),
             lambda: raw.get_object(Bucket="another-invented-bucket", Key=KEY)]
    for call in calls:
        with pytest.raises(ClientError):
            call()
    assert s3.faults == ["both_conditions", "method:DELETE", "method:HEAD", "get_param:versionId",
                         "listing:delimiter", "another_bucket"]
    s3.faults.clear()


def test_the_fake_ignores_extra_request_headers(s3):
    """A newer botocore may add headers. Conditions, keys and methods are strict;
    headers the fake does not know are not a fault."""
    raw = store.build_client("s3", endpoint_url=s3.url)
    raw.put_object(Bucket=fakes.BUCKET, Key=KEY, Body=MINE, IfNoneMatch="*",
                   Metadata={"invented": "header"}, CacheControl="no-store",
                   ChecksumAlgorithm="SHA256")
    assert s3.body(KEY) == MINE and not s3.faults


# ================================================= absence and the listing ==

@pytest.mark.parametrize("missing_status", [404, 403])
def test_absence_is_read_from_the_listing_and_a_get_error_is_a_failure(s3, st, missing_status):
    """Real S3 answers a GET for a missing key with 404 or 403, depending on a
    permission. Either way the store reports a failure, never "absent"."""
    s3.missing_status = missing_status
    assert st.list_keys(store.LEDGER_PREFIX) == []                        # how absence is known
    assert code_of(lambda: st.get(KEY)) == store.GET_FAILED
    assert s3.missing_gets == 1                                           # the counter's control
    s3.seed(KEY, MINE)
    assert st.list_keys(store.LEDGER_PREFIX) == [KEY] and st.get(KEY)[0] == MINE


@pytest.mark.parametrize("status", [500, 403])
def test_a_get_error_on_a_listed_key_is_a_failure(s3, st, status):
    s3.seed(KEY, MINE)
    s3.script_get(KEY, status)
    assert code_of(lambda: st.get(KEY)) == store.GET_FAILED
    assert st.get(KEY)[0] == MINE                                         # the partner
    assert s3.count("GET", KEY) == 2                                      # one attempt each: no retry


def test_a_truncated_listing_is_refused(s3, st):
    s3.seed(KEY, MINE)
    assert st.list_keys(store.LEDGER_PREFIX) == [KEY]                     # the partner
    s3.truncate_listing = True
    assert code_of(lambda: st.list_keys(store.LEDGER_PREFIX)) == store.LIST_TRUNCATED


def test_a_listing_error_is_a_failure_and_is_not_retried(s3, st):
    s3.script_list(500)
    assert code_of(lambda: st.list_keys(store.LEDGER_PREFIX)) == store.LIST_FAILED
    assert s3.count("GET", "?list") == 1
    assert st.list_keys(store.LEDGER_PREFIX) == []                        # the partner


def test_a_listing_returns_only_the_keys_under_its_prefix(s3, st):
    s3.seed("ws7/claims/pipeline/2026-12-16", OTHER)
    s3.seed("ws7/ledger/2026-11.json", OTHER)
    s3.seed(KEY, MINE)
    assert st.list_keys(store.LEDGER_PREFIX) == ["ws7/ledger/2026-11.json", KEY]


@pytest.mark.parametrize("key", ["ledger/2026-12.json", "ws8/x", "ws7/", "ws7//x", "ws7/a/../b",
                                 "ws7/has space", "", None, 7])
def test_a_key_outside_the_ws7_prefix_is_refused_before_any_request(s3, st, key):
    assert st.put_if_absent("ws7/ok", MINE)[0] == store.WRITTEN           # the partner
    before = len(s3.requests)
    for call in (lambda: st.get(key), lambda: st.put_if_absent(key, MINE),
                 lambda: st.put_if_match(key, MINE, '"e"'), lambda: st.list_keys(key)):
        assert code_of(call) == store.KEY_REFUSED
    assert len(s3.requests) == before


@pytest.mark.parametrize("bucket", ["Has-Capitals", "has.dots.in-it", "ab", "a--x-s3", "name--x-s3",
                                    "arn:aws:s3:::bucket", "", None])
def test_only_an_ordinary_bucket_name_is_accepted(bucket):
    assert store.Store(object(), "ws7-fake-eval-bucket")                  # the partner
    assert code_of(lambda: store.Store(object(), bucket)) == store.BUCKET_REFUSED


def test_a_body_must_be_bytes_within_the_size_limit(s3, st):
    assert code_of(lambda: st.put_if_absent(KEY, "text")) == store.OBJECT_TOO_LARGE
    assert code_of(lambda: st.put_if_absent(KEY, b"x" * (store.MAX_OBJECT_BYTES + 1))) \
        == store.OBJECT_TOO_LARGE
    assert not s3.requests
    s3.seed(KEY, b"x" * (store.MAX_OBJECT_BYTES + 1))
    assert code_of(lambda: st.get(KEY)) == store.OBJECT_TOO_LARGE
    s3.seed(KEY, b"x" * store.MAX_OBJECT_BYTES)
    assert len(st.get(KEY)[0]) == store.MAX_OBJECT_BYTES                  # the partner: at the limit


# ================================================================= create ==

def test_create_writes_once_and_reports_a_key_that_is_taken(s3, st):
    assert st.create(KEY, MINE, attempts=4) == store.CREATED
    assert st.create(KEY, OTHER, attempts=4) == store.TAKEN
    assert s3.body(KEY) == MINE
    # A plain "taken" needs no read: one PUT each, no listing and no GET.
    assert [r for r in s3.requests if r != ("PUT", KEY)] == []


def test_create_counts_tries_in_total(s3, st):
    s3.script_put(KEY, fakes.CONFLICT_409, fakes.CONFLICT_409, fakes.CONFLICT_409)
    assert st.create(KEY, MINE, attempts=4) == store.CREATED              # three lost, the fourth wins
    assert s3.count("PUT", KEY) == 4

    other = "ws7/ledger/2026-11.json"
    s3.script_put(other, *[fakes.CONFLICT_409] * 4)
    assert code_of(lambda: st.create(other, MINE, attempts=4)) == store.CREATE_UNSURE
    assert s3.count("PUT", other) == 4 and other not in s3.keys()


@pytest.mark.parametrize("script", [fakes.LAND_THEN_500, fakes.LAND_THEN_DROP])
def test_create_reads_back_a_write_that_landed_but_whose_reply_failed(s3, st, script):
    s3.script_put(KEY, script)
    assert st.create(KEY, MINE, attempts=4) == store.CREATED
    # One PUT, then the listing, then a GET of the key the listing returned.
    assert s3.requests == [("PUT", KEY), ("GET", "?list"), ("GET", KEY)]


def test_create_after_a_failed_reply_finds_another_writers_object(s3, st):
    s3.before_put(KEY, lambda: s3.seed(KEY, OTHER))         # another writer lands first
    s3.script_put(KEY, fakes.FAIL_500)
    assert st.create(KEY, MINE, attempts=4) == store.TAKEN
    assert s3.body(KEY) == OTHER


def test_create_tries_again_when_the_failed_write_did_not_land(s3, st):
    s3.script_put(KEY, fakes.FAIL_500)
    assert st.create(KEY, MINE, attempts=4) == store.CREATED
    assert s3.requests == [("PUT", KEY), ("GET", "?list"), ("PUT", KEY)]  # not listed: never read


def test_create_checks_a_412_that_follows_a_failed_reply_rather_than_assuming(s3, st):
    """The first write fails and is not listed; it then lands late. The next try
    meets a 412. The object is this caller's own, and is recognised by its bytes."""
    s3.script_put(KEY, fakes.FAIL_500)
    st_create_started = len(s3.requests)

    def land_late():
        if s3.count("PUT", KEY) == 2:
            s3.seed(KEY, MINE)

    s3.before_put(KEY, lambda: None)                        # the first PUT: nothing in between
    s3.before_put(KEY, land_late)                           # before the second PUT is judged
    assert st.create(KEY, MINE, attempts=4) == store.CREATED
    assert s3.puts == [(KEY, "if-none-match", 500), (KEY, "if-none-match", 412)]
    assert s3.requests[st_create_started:][-2:] == [("GET", "?list"), ("GET", KEY)]


# ================================================================ clients ==

@pytest.mark.parametrize("service", ["s3", "ssm"])
def test_a_client_makes_one_attempt_per_call(service):
    client = store.build_client(service, endpoint_url="http://127.0.0.1:9")
    assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": 1}
    assert (client.meta.config.connect_timeout, client.meta.config.read_timeout) \
        == (store.CONNECT_TIMEOUT_S, store.READ_TIMEOUT_S)


@pytest.mark.parametrize("service", ["s3", "ssm"])
def test_without_an_endpoint_the_client_is_the_regions_own_and_is_never_called(service):
    """Built, inspected and dropped: what the job will use. No request is made."""
    client = store.build_client(service)
    assert client.meta.endpoint_url == f"https://{service}.eu-west-2.amazonaws.com"
    assert client.meta.region_name == "eu-west-2"


LIVE_URL = {"s3": f"https://{fakes.BUCKET}.s3.eu-west-2.amazonaws.com/{KEY}",
            "ssm": "https://ssm.eu-west-2.amazonaws.com/"}
# Reserved names (RFC 2606): none of them can resolve, and the fixture refuses to look them up.
OVERRIDES = {"AWS_ENDPOINT_URL": "https://global-override.invalid",
             "AWS_ENDPOINT_URL_S3": "https://s3-override.invalid",
             "AWS_ENDPOINT_URL_SSM": "https://ssm-override.invalid"}
CONFIG_OVERRIDE = "https://config-file-override.invalid"


class _Stopped(Exception):
    pass


def request_url(service: str, client) -> str:
    """The URL botocore would send a request to, found without connecting: a
    `before-send` handler records it and raises, so the request never leaves."""
    seen: list = []

    def stop(request, **_kwargs):
        seen.append(request.url)
        raise _Stopped()

    client.meta.events.register("before-send", stop)
    with pytest.raises(_Stopped):
        if service == "s3":
            client.get_object(Bucket=fakes.BUCKET, Key=KEY)
        else:
            client.get_parameter(Name="/discharge-eval/ws7/expected/v1")
    assert len(seen) == 1
    return seen[0]


def _configure_override(monkeypatch, tmp_path, service: str, source: str) -> str:
    """Set one kind of configured endpoint URL; return what an unprotected client would use."""
    monkeypatch.setenv("AWS_IGNORE_CONFIGURED_ENDPOINT_URLS", "false")    # the Config value must win
    if source == "config_file":
        path = tmp_path / "aws-config-with-endpoint"
        path.write_text(f"[default]\nendpoint_url = {CONFIG_OVERRIDE}\n", encoding="utf-8")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
        return CONFIG_OVERRIDE
    name = "AWS_ENDPOINT_URL" if source == "global" else f"AWS_ENDPOINT_URL_{service.upper()}"
    monkeypatch.setenv(name, OVERRIDES[name])
    return OVERRIDES[name]


@pytest.mark.parametrize("source", ["global", "service", "config_file"])
@pytest.mark.parametrize("service", ["s3", "ssm"])
def test_a_configured_endpoint_url_does_not_move_the_live_client(monkeypatch, tmp_path, service,
                                                                 source):
    """The positive control first: an ordinary client IS moved by the override, so the
    environment in this test really would redirect a request."""
    import boto3
    override = _configure_override(monkeypatch, tmp_path, service, source)
    plain = boto3.Session(region_name="eu-west-2").client(service)
    assert plain.meta.endpoint_url == override

    live = store.build_client(service)
    assert live.meta.endpoint_url == store.LIVE_ENDPOINTS[service]
    assert request_url(service, live) == LIVE_URL[service]


def test_an_s3_section_in_the_config_file_does_not_move_the_live_client(monkeypatch, tmp_path):
    import boto3
    path = tmp_path / "aws-config-with-s3-section"
    path.write_text("[default]\ns3 =\n    use_accelerate_endpoint = true\n", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
    plain = boto3.Session(region_name="eu-west-2").client("s3")
    assert "s3-accelerate" in request_url("s3", plain)                    # the control: it is moved
    assert request_url("s3", store.build_client("s3")) == LIVE_URL["s3"]


@pytest.mark.parametrize("variable", ["AWS_USE_FIPS_ENDPOINT", "AWS_USE_DUALSTACK_ENDPOINT"])
@pytest.mark.parametrize("service", ["s3", "ssm"])
def test_the_live_client_is_refused_if_it_would_not_be_the_regions_own_endpoint(monkeypatch,
                                                                                service, variable):
    assert store.build_client(service)                                    # the partner: unset, built
    monkeypatch.setenv(variable, "true")
    assert code_of(lambda: store.build_client(service)) == store.ENDPOINT_REFUSED


@pytest.mark.parametrize("endpoint", [
    "https://s3.eu-west-2.amazonaws.com",                   # the live path passes none
    "http://localhost:9", "https://127.0.0.1:9", "http://127.0.0.1:9/path",
    "http://127.0.0.1", "http://10.0.0.1:9", "", 9,
])
def test_an_explicit_endpoint_must_be_the_loopback_fake(endpoint):
    assert store.build_client("s3", endpoint_url="http://127.0.0.1:9")    # the partner
    assert code_of(lambda: store.build_client("s3", endpoint_url=endpoint)) == store.ENDPOINT_REFUSED


def test_only_s3_and_ssm_clients_are_built():
    for service in ("dynamodb", "bedrock-runtime", "sts", ""):
        assert code_of(lambda: store.build_client(service)) == store.ENDPOINT_REFUSED


def test_the_offline_fixture_lets_the_store_reach_only_its_fake(s3, _offline):
    """The control for "no test can reach AWS": a client aimed at another loopback
    port is stopped by the fixture, and one aimed at the fake is not."""
    elsewhere = store.Store(store.build_client("s3", endpoint_url="http://127.0.0.1:9"), fakes.BUCKET)
    assert code_of(lambda: elsewhere.list_keys(store.LEDGER_PREFIX)) == store.LIST_FAILED
    assert _offline.refused == [("127.0.0.1", 9)]
    _offline.refused.clear()                                              # expected
    here = store.Store(store.build_client("s3", endpoint_url=s3.url), fakes.BUCKET)
    assert here.list_keys(store.LEDGER_PREFIX) == []


# ================================================================== stops ==

def test_a_stop_carries_a_code_ids_and_counts_and_nothing_else():
    stop = store.Stop("ws7_blocked_unresolved", ids=("2026-12-09T0400-pipeline", "2026-11"),
                      counts={"unresolved": 1})
    assert str(stop) == "ws7_blocked_unresolved" == stop.code
    assert stop.line() == ("ws7 stop code=ws7_blocked_unresolved "
                           "ids=2026-12-09T0400-pipeline,2026-11 unresolved=1")
    assert stop.metric == store.REFUSED_METRIC
    assert store.Stop("ws7_duplicate").line() == "ws7 stop code=ws7_duplicate"


@pytest.mark.parametrize("kwargs", [
    {"code": "a sentence, not a label"},
    {"code": "ws7_x", "ids": ("patient text with spaces",)},
    {"code": "ws7_x", "ids": ("x" * 65,)},
    {"code": "ws7_x", "ids": (7,)},
    {"code": "ws7_x", "counts": {"n": "seven"}},
    {"code": "ws7_x", "counts": {"n": 1.5}},
    {"code": "ws7_x", "counts": {"n": True}},
    {"code": "ws7_x", "counts": {"a name with spaces": 1}},
])
def test_a_stop_cannot_be_given_text(kwargs):
    assert store.Stop("ws7_x", ids=("2026-12",), counts={"n": 1})         # the partner
    code = kwargs.pop("code")
    with pytest.raises(ValueError):
        store.Stop(code, **kwargs)


# ============================================================== the layout ==

WEDNESDAY = datetime(2026, 12, 16, 4, 0, tzinfo=timezone.utc)             # invented occurrence


def test_a_scheduled_claim_is_keyed_by_the_utc_date_of_the_scheduled_time():
    assert store.claim_key("pipeline", "scheduled", WEDNESDAY) == "ws7/claims/pipeline/2026-12-16"
    assert store.run_id("pipeline", "scheduled", WEDNESDAY) == "2026-12-16T0400-pipeline"
    # The date is UTC's, whatever offset the time was written with.
    london_summer = datetime(2027, 6, 30, 0, 30, tzinfo=timezone(timedelta(hours=1)))
    assert store.claim_key("v1", "scheduled", london_summer) == "ws7/claims/v1/2027-06-29"
    # One key per target per date: a later time that day is the same occurrence.
    assert store.claim_key("pipeline", "scheduled", WEDNESDAY + timedelta(hours=3)) \
        == store.claim_key("pipeline", "scheduled", WEDNESDAY)
    assert store.claim_key("v1", "scheduled", WEDNESDAY) != store.claim_key("pipeline", "scheduled",
                                                                            WEDNESDAY)


def test_a_manual_claim_has_a_key_and_a_run_id_of_its_own():
    run = store.run_id("pipeline", "manual", WEDNESDAY)
    assert run == "2026-12-16T0400-pipeline-manual"
    assert store.claim_key("pipeline", "manual", WEDNESDAY) == f"ws7/claims/pipeline/manual/{run}"
    # Never a scheduled run's ID or key, for the same target and the same minute.
    assert run != store.run_id("pipeline", "scheduled", WEDNESDAY)
    assert store.claim_key("pipeline", "manual", WEDNESDAY) \
        != store.claim_key("pipeline", "scheduled", WEDNESDAY)
    assert "manual" not in store.TARGETS                                  # why they cannot collide
    assert store.ID_RE.fullmatch(run)


def test_nothing_that_is_kept_sits_under_the_prefix_that_expires():
    """D8: scheduled runs expire after 90 days; claims, ledgers and manual (baseline)
    runs are kept. A lifecycle rule selects by prefix."""
    expiring = "ws7/runs/scheduled/"
    assert store.run_prefix("scheduled", "2026-12-16T0400-pipeline") \
        == expiring + "2026-12-16T0400-pipeline/"                         # the control: it is there
    kept = [store.claim_key("pipeline", "scheduled", WEDNESDAY),
            store.claim_key("pipeline", "manual", WEDNESDAY),
            store.ledger_key("2026-12"),
            store.run_prefix("manual", "2026-12-16T0400-pipeline-manual")]
    assert kept[3] == "ws7/runs/manual/2026-12-16T0400-pipeline-manual/"
    assert not any(key.startswith(expiring) or expiring.startswith(key) for key in kept)
    assert all(key.startswith(store.PREFIX) for key in kept)


def test_a_ledger_key_names_its_month_and_nothing_else_is_a_ledger():
    assert store.ledger_key("2026-12") == "ws7/ledger/2026-12.json"
    assert store.ledger_month("ws7/ledger/2026-12.json") == "2026-12"
    for key in ("ws7/ledger/2026-13.json", "ws7/ledger/2026-12.json.bak", "ws7/ledger/notes.json",
                "ws7/claims/2026-12.json", "ws7/ledger/2026-12", None):
        assert store.ledger_month(key) is None
    for bad in ("2026-13", "2026-1", "26-12", "2026-12-01", None):
        with pytest.raises(ValueError):
            store.ledger_key(bad)
    for bad in (("pipeline", "weekly"), ("other", "scheduled")):
        with pytest.raises(ValueError):
            store.claim_key(*bad, WEDNESDAY)
    with pytest.raises(ValueError):
        store.run_prefix("scheduled", "has/slash")


# =================================================================== time ==

def test_times_are_whole_utc_seconds_and_a_clock_without_a_zone_is_refused():
    aware = datetime(2026, 12, 16, 5, 0, 30, 999999, tzinfo=timezone(timedelta(hours=1)))
    assert store.read_clock(lambda: aware) == datetime(2026, 12, 16, 4, 0, 30, tzinfo=timezone.utc)
    assert store.utc_text(aware) == "2026-12-16T04:00:30Z"
    assert store.parse_utc("2026-12-16T04:00:30Z") == store.read_clock(lambda: aware)
    for bad in ("2026-12-16T04:00:30", "2026-12-16T04:00:30+00:00", "2026-13-40T04:00:30Z", "", None):
        assert store.parse_utc(bad) is None
    with pytest.raises(ValueError):
        store.read_clock(lambda: datetime(2026, 12, 16, 4, 0, 30))


# ============================================ never redirected, sent once ==

def _plain_s3(url: str):
    """An ordinary S3 client with one attempt: what WS7's would be without its two
    extra handlers. Used only as the control."""
    import boto3
    from botocore.config import Config
    return boto3.Session(region_name="eu-west-2").client(
        "s3", endpoint_url=url, config=Config(retries=dict(store.RETRIES),
                                              s3={"addressing_style": "path"}))


OPERATIONS = {
    "list": lambda st, etag: st.list_keys(store.LEDGER_PREFIX),
    "get": lambda st, etag: st.get(KEY),
    "put_if_absent": lambda st, etag: st.put_if_absent("ws7/ledger/2026-11.json", MINE),
    "put_if_match": lambda st, etag: st.put_if_match(KEY, MINE, etag),
}
ELSEWHERE = pytest.mark.parametrize("answer", [fakes.PERMANENT_REDIRECT, fakes.WRONG_SIGNING_REGION],
                                    ids=["PermanentRedirect", "AuthorizationHeaderMalformed"])


@ELSEWHERE
def test_the_control_an_ordinary_s3_client_does_send_the_request_again(s3, answer):
    """Why the next test matters: with one attempt configured, botocore still follows
    S3's pointer to another region, signs for it, and remembers it."""
    s3.seed(KEY, OTHER)
    plain = _plain_s3(s3.url)
    s3.script_any(answer)
    assert plain.get_object(Bucket=fakes.BUCKET, Key=KEY)["Body"].read() == OTHER
    assert s3.requests == [("GET", KEY), ("GET", KEY)]                    # sent twice
    assert s3.signed_regions == ["eu-west-2", "eu-west-1"]                # the second for another region
    plain.get_object(Bucket=fakes.BUCKET, Key=KEY)
    assert s3.signed_regions[-1] == "eu-west-1"                           # and it stays moved


@ELSEWHERE
@pytest.mark.parametrize("operation", list(OPERATIONS))
def test_a_ws7_s3_client_never_follows_a_redirect(s3, st, answer, operation):
    """Ruling 7. S3 points at another region: the fake sees exactly one request, the
    store raises a fixed code, and nothing is remembered for the next call."""
    etag = s3.seed(KEY, OTHER)
    assert OPERATIONS["get"](st, etag)[0] == OTHER                        # the partner: a normal reply
    seen = len(s3.requests)

    s3.script_any(answer)
    assert code_of(lambda: OPERATIONS[operation](st, etag)) == store.WRONG_REGION
    assert len(s3.requests) == seen + 1                                   # exactly one request
    assert s3.body(KEY) == OTHER and s3.keys() == [KEY]                   # a write did not land

    assert st.get(KEY)[0] == OTHER                                        # the next call is ordinary
    assert set(s3.signed_regions) == {"eu-west-2"}                        # never signed for another region


def _ask_again_after_a_500(client) -> None:
    """What botocore's redirector does, done by a test: tell the endpoint to send again."""
    def again(response, **_kwargs):
        return 0 if response is not None and response[0].status_code == 500 else None
    client.meta.events.register("needs-retry.s3", again)


def test_a_ws7_s3_client_never_sends_one_call_twice_whatever_asks_for_it(s3):
    """The second safeguard, alone. Something other than the redirector asks for the
    request to be sent again; the second send is stopped before it leaves."""
    s3.seed(KEY, OTHER)
    plain = _plain_s3(s3.url)
    _ask_again_after_a_500(plain)
    s3.script_any(fakes.SERVER_ERROR)
    assert plain.get_object(Bucket=fakes.BUCKET, Key=KEY)["Body"].read() == OTHER
    assert s3.requests == [("GET", KEY), ("GET", KEY)]                    # the control: sent twice

    client = store.build_client("s3", endpoint_url=s3.url)
    _ask_again_after_a_500(client)
    ours = store.Store(client, fakes.BUCKET)
    for call in (lambda: ours.get(KEY), lambda: ours.list_keys(store.LEDGER_PREFIX),
                 lambda: ours.put_if_absent("ws7/ledger/2026-11.json", MINE)):
        seen = len(s3.requests)
        s3.script_any(fakes.SERVER_ERROR)
        assert code_of(call) == store.RESEND_REFUSED
        assert len(s3.requests) == seen + 1                               # exactly one request
    assert ours.get(KEY)[0] == OTHER                                      # the partner: a normal reply
