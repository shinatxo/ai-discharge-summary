"""evals/ws7_publish.py - the CloudWatch metrics and the job's SNS messages, and the two
strict fakes they are tested against (WS7 slice 2 part 5a).

Offline. Every client points at a fake on 127.0.0.1, or is built, inspected and never
called. `offline_aws` (tests/ws7_stub.py) applies to every test in this file.

What these tests cannot prove is in docs/WS7-ENTRYPOINT.md ("What the fakes cannot
prove"): that real CloudWatch takes these names and values, that the task role may
publish them, that an alarm sees them, and that the encrypted topic delivers.
"""

from __future__ import annotations

import http.client
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "tests", ROOT / "src" / "generate", ROOT / "evals"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ws7_client as wc  # noqa: E402
import ws7_compare as compare  # noqa: E402
import ws7_fakes as fakes  # noqa: E402
import ws7_publish as publish  # noqa: E402
import ws7_store as store  # noqa: E402
import ws7_stub as stubs  # noqa: E402
import ws7_world as W  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from ws7_stub import offline_aws  # noqa: E402,F401  (a fixture)

TARGETS = ("pipeline", "v1")
TOPIC = W.TOPIC
SENTINEL = "WS7-PUBLISH-SENTINEL text that must never be sent"
NOW = datetime(2026, 12, 16, 4, 41, tzinfo=timezone.utc)
RUN = "2026-12-16T0400-pipeline"
PREFIX = f"ws7/runs/scheduled/{RUN}/"
HASH = "3f9c" + "0" * 58 + "e1"
KEYS = {"pipeline": {"target": "pipeline", "pipeline_version": HASH,
                     "model_id": "anthropic.claude-sonnet-4-6", "prompt_caching": True},
        "v1": {"target": "v1", "model_id": "anthropic.claude-sonnet-4-6", "prompt_caching": False,
               "v1_prompt_sha256": {"system_prompt": "5d0c" + "0" * 58 + "9a",
                                    "patient_system_prompt": "77e1" + "0" * 58 + "42"}}}
QUALITY = ("safety_net_", "sn_", "meds_", "step2_", "compose_", "haz03_", "assembly_")


@pytest.fixture(autouse=True)
def _offline(offline_aws):  # noqa: F811
    return offline_aws


@pytest.fixture
def cw(_offline):
    fake = stubs.started(fakes.CloudWatchFake(), _offline)
    yield fake
    fake.stop()


@pytest.fixture
def sns(_offline):
    fake = stubs.started(fakes.SnsFake(), _offline)
    yield fake
    fake.stop()


def publisher(target: str = "pipeline", cw=None, sns=None) -> publish.Publisher:
    return publish.Publisher(
        target,
        cloudwatch=store.build_client("cloudwatch", endpoint_url=cw.url) if cw else None,
        sns=store.build_client("sns", endpoint_url=sns.url) if sns else None,
        topic=TOPIC if sns else None)


def code_of(call) -> str:
    with pytest.raises(store.Stop) as exc:
        call()
    assert SENTINEL not in exc.value.line()
    return exc.value.code


# ==================================================================== THE NAMES ==

def test_a_summary_metric_is_published_under_a_fixed_renaming():
    assert publish.pascal("cost_usd_micros") == "CostUsdMicros"
    assert publish.pascal("elapsed_p90_ms") == "ElapsedP90Ms"
    assert publish.pascal("input_tokens_max_patient_v2") == "InputTokensMaxPatientV2"
    for bad in ("Cost", "cost usd", "", None, 7, "cost-usd", "x" * 65):
        assert code_of(lambda: publish.pascal(bad)) == publish.PUBLISH_INVALID


def test_there_is_a_breach_name_for_every_family_and_kind_but_part_c():
    names = {publish.breach_name(family, kind)
             for family in publish.BREACH_FAMILIES for kind in compare.KINDS}
    assert len(names) == 21 and "BreachSafetyNettingKind2" in names and "BreachHaz03Kind1" in names
    # every family the comparator knows has a name, except PART C, which has none (D13)
    assert set(publish.BREACH_FAMILIES) == set(compare.FAMILIES) - {"part_c"}
    assert not any("PartC" in name or "Part" in name for name in names)
    for family, kind in (("part_c", 1), ("run", 4), ("run", True), ("other", 1), ("run", "1")):
        assert code_of(lambda: publish.breach_name(family, kind)) == publish.PUBLISH_INVALID


@pytest.mark.parametrize("target", TARGETS)
def test_the_run_family_is_every_count_a_summary_has_outside_the_quality_families(
        target, _offline, tmp_path_factory):
    """The list is pinned against a real summary's metrics (the invented baseline is
    built from three): every metric that is not a quality family's is a Run metric and
    is published, and nothing else is."""
    metrics = W.invented_baseline(target, _offline, tmp_path_factory)["metrics"]
    run_family = {name for name in metrics if not name.startswith(QUALITY)}
    assert set(publish.run_metrics(target)) == run_family
    assert all("counts" in metrics[name] for name in run_family)        # no verdict among them
    assert len(publish.run_metrics(target)) == {"pipeline": 25, "v1": 14}[target]
    allowed = publish.allowed_names(target)
    assert {publish.pascal(name) for name in run_family} <= allowed
    assert {"RunOk", "RunRefused", "Duplicate", "NoBaseline", "SpendGuardTrips", "CallsRefused",
            "CallsNotResponse"} <= allowed
    assert not any(publish.pascal(name) in allowed for name in set(metrics) - run_family)
    assert len(allowed) == 4 + 21 + len(run_family)     # the guard's three are Run names too


# =================================================================== THE VALUES ==

def test_run_counts_come_from_the_summarys_count_metrics():
    metrics = {name: {"count": n} for n, name in enumerate(publish.run_metrics("v1"))}
    summary = {"metrics": dict(metrics, meds_verdict={"verdicts": {"S1": "pass"}},
                               sn_review={"count": 99})}
    assert publish.run_counts(summary, "v1") == [
        (publish.pascal(name), n) for n, name in enumerate(publish.run_metrics("v1"))]
    for change in ({"cost_usd_micros": {"count": True}}, {"cost_usd_micros": {"count": 1.0}},
                   {"cost_usd_micros": {"count": -1}}, {"cost_usd_micros": {"verdicts": {}}},
                   {"cost_usd_micros": SENTINEL}):
        bad = {"metrics": dict(summary["metrics"], **change)}
        assert code_of(lambda: publish.run_counts(bad, "v1")) == publish.PUBLISH_INVALID
    missing = {"metrics": {k: v for k, v in metrics.items() if k != "elapsed_p90_ms"}}
    assert code_of(lambda: publish.run_counts(missing, "v1")) == publish.PUBLISH_INVALID
    assert code_of(lambda: publish.run_counts(None, "v1")) == publish.PUBLISH_INVALID


def test_the_guards_three_counters_are_counted_over_the_whole_report():
    def row(outcome, code=None):
        return {"outcome": outcome, "code": code}
    report = {"no_response": 2, "calls": [
        row(wc.RESPONSE), row("throttled"), row("server_error"),
        row(wc.REFUSED, wc.SPEND_GUARD), row(wc.REFUSED, wc.SPEND_GUARD), row(wc.REFUSED, wc.UNMETERED)]}
    assert publish.guard_counts(report) == [("SpendGuardTrips", 2), ("CallsRefused", 3),
                                            ("CallsNotResponse", 2)]
    assert publish.guard_counts({"no_response": 0, "calls": []}) == [
        ("SpendGuardTrips", 0), ("CallsRefused", 0), ("CallsNotResponse", 0)]
    for bad in (None, {}, {"no_response": True, "calls": []}, {"no_response": 0, "calls": [7]}):
        assert code_of(lambda: publish.guard_counts(bad)) == publish.PUBLISH_INVALID


def result(status: str = "no_breach", rows: list | None = None, **change) -> dict:
    """An invented comparison result in the comparator's own shape."""
    rows = rows if rows is not None else [
        {"family": "medications", "kind": 1, "breach": 0, "metrics": [
            {"metric": "meds_invented_doses", "breach": 0, "run": 0, "baseline_max": 1}]}]
    out = {"schema": compare.RESULT_SCHEMA, "status": status,
           "exit_code": compare.EXIT_CODES[status], "baseline_id": "b-2026-12-14",
           "run_id": f"{RUN}.main", "inputs": {}, "key_mismatch": [], "errors": [],
           "comparisons": rows, "unruled_metrics": []}
    out.update(change)
    return out


BREACHING = [
    {"family": "assembly", "kind": 3, "breach": 1, "metrics": [
        {"metric": "assembly_placeholders_left", "breach": 1, "direction": "above", "run": 10,
         "low": 2, "high": 9}]},
    {"family": "medications", "kind": 1, "breach": 1, "metrics": [
        {"metric": "meds_invented_doses", "breach": 1, "run": 3, "baseline_max": 1},
        {"metric": "meds_invented_frequencies", "breach": 0, "run": 0, "baseline_max": 0}]},
    {"family": "run", "kind": 3, "breach": 0, "metrics": [
        {"metric": "tokens_out_total", "breach": 0, "direction": "inside", "run": 5, "low": 1,
         "high": 9}]},
    {"family": "safety_netting", "kind": 2, "breach": 1, "metrics": [
        {"metric": "safety_net_verdict", "breach": 1, "flips": ["S14", "S3"], "no_output": ["S3"],
         "min_flips": 1, "stable": 2, "unstable": []}]}]


def test_a_checked_result_gives_one_breach_value_for_each_family_and_kind():
    assert publish.breach_values(publish.check_result(result(), exit_code=0)) == [
        ("BreachMedicationsKind1", 0)]
    checked = publish.check_result(result("breach", BREACHING), exit_code=1)
    assert publish.breach_values(checked) == [
        ("BreachAssemblyKind3", 1), ("BreachMedicationsKind1", 1), ("BreachRunKind3", 0),
        ("BreachSafetyNettingKind2", 1)]


@pytest.mark.parametrize("exit_code, given", [
    (1, result()),                                              # exit 1 beside "no breach"
    (0, result("breach", BREACHING)),                           # exit 0 beside "breach"
    (0, result(exit_code=1)), (0, result(exit_code=False)),
    (2, result("no_baseline", [])), (0, result("no_baseline", [])), (0, result("error", [])),
    (0, result(rows=[])),                                       # a pass that compared nothing
    (0, result(schema="ws7-result/2")), (0, result(baseline_id=None)),
    (0, result(run_id=f"run {SENTINEL}")), (0, result(errors=[{"code": "x"}])),
    (0, result(key_mismatch=["model_id"])),
    (0, result(rows=[{"family": "medications", "kind": 1, "breach": 2, "metrics": [
        {"metric": "meds_invented_doses", "breach": 0}]}])),
    (0, result(rows=[{"family": "medications", "kind": 4, "breach": 0, "metrics": [
        {"metric": "meds_invented_doses", "breach": 0}]}])),
    (0, result(rows=[{"family": "something", "kind": 1, "breach": 0, "metrics": [
        {"metric": "meds_invented_doses", "breach": 0}]}])),
    (0, result(rows=[{"family": "medications", "kind": 1, "breach": 0, "metrics": []}])),
    (0, result(rows=[{"family": "medications", "kind": 1, "breach": 0, "metrics": [
        {"metric": f"meds {SENTINEL}", "breach": 0}]}])),
    (0, result(rows=[{"family": "medications", "kind": 1, "breach": 0, "metrics": [
        {"metric": "meds_invented_doses", "breach": 1}]}])),    # a row that hides its metric's breach
    (1, result("breach", [dict(BREACHING[2])])),                # "breach" with no breaching row
    (0, result(rows=result()["comparisons"] * 2)),              # one family and kind twice
    (0, None), (0, []), (True, result())], ids=lambda v: str(v)[:60])
def test_a_result_out_of_shape_or_at_odds_with_the_exit_code_is_refused(exit_code, given):
    assert publish.check_result(result(), exit_code=0)                  # the partner
    assert code_of(lambda: publish.check_result(given, exit_code=exit_code)) == publish.RESULT_INVALID


def test_a_row_for_part_c_is_refused_with_its_own_code_and_gives_no_value():
    """PART C is not published (D13). The partner: the same row under another family."""
    row = {"family": "part_c", "kind": 3, "breach": 0, "metrics": [
        {"metric": "tokens_in_total", "breach": 0}]}
    assert code_of(lambda: publish.check_result(result(rows=[row]), exit_code=0)) \
        == publish.PART_C_RULED
    assert publish.breach_values(publish.check_result(
        result(rows=[dict(row, family="run")]), exit_code=0)) == [("BreachRunKind3", 0)]


# ============================================================ PUTTING THE METRICS ==

@pytest.mark.parametrize("target", TARGETS)
def test_metrics_go_in_one_call_to_one_namespace_with_one_dimension(cw, target):
    publisher(target, cw).put([("RunOk", 1), ("NoBaseline", 0), ("CostUsdMicros", 1903211),
                               ("BreachRunKind3", 1)])
    assert len(cw.bodies) == 1
    assert cw.calls == [{"namespace": "DischargeAssistant/Eval", "data": [
        {"name": name, "value": value, "unit": "None", "dimensions": {"Target": target}}
        for name, value in (("RunOk", 1), ("NoBaseline", 0), ("CostUsdMicros", 1903211),
                            ("BreachRunKind3", 1))]}]


@pytest.mark.parametrize("values", [
    [], None, [("RunOk", 1), ("RunOk", 1)],                     # nothing; one name twice
    [("RunOk", 2)], [("RunOk", True)], [("RunOk", 1.0)], [("RunOk", -1)], [("RunOk", "1")],
    [("BreachRunKind3", 2)], [("NoBaseline", 7)],               # a 0-or-1 metric holding more
    [("CostUsdMicros", 1.5)], [("CostUsdMicros", None)],
    [("SomethingElse", 1)], [("runok", 1)], [("Run Ok", 1)], [(f"Run{SENTINEL}", 1)],
    [("BreachPartCKind3", 0)], [("MedsInventedDoses", 1)], [("SnReview", 1)],
    [("StepFailuresExtractFacts", 0)],                          # a pipeline name, on the v1 target
    [["RunOk", 1]], [("RunOk",)], ["RunOk"], [("RunOk", 1), ("Other", 1)]], ids=lambda v: str(v)[:50])
def test_a_list_that_is_not_all_known_names_and_whole_numbers_sends_nothing(cw, values):
    """Every value is checked before the one request. The partner sends one."""
    pub = publisher("v1", cw)
    assert code_of(lambda: pub.put(values)) == publish.PUBLISH_INVALID
    assert cw.bodies == []
    pub.put([("RunOk", 1), ("CostUsdMicros", 5)])
    assert len(cw.bodies) == 1


def test_a_metric_call_that_fails_is_tried_once_and_reported_by_a_fixed_code(cw):
    cw.script(500)
    pub = publisher("v1", cw)
    assert code_of(lambda: pub.put([("RunOk", 0)])) == publish.PUBLISH_FAILED
    assert len(cw.bodies) == 1 and cw.calls == []           # one request, never a second
    pub.put([("RunOk", 0)])                                 # the partner
    assert cw.values("RunOk") == [0]


def test_a_publisher_with_no_cloudwatch_client_cannot_put_a_metric(sns):
    """The Thursday check is given no CloudWatch client at all."""
    assert code_of(lambda: publisher("v1", sns=sns).put([("RunOk", 1)])) == publish.PUBLISH_INVALID
    assert code_of(lambda: publish.Publisher("other")) == publish.PUBLISH_INVALID


# ================================================================= THE MESSAGES ==

def test_the_breach_message_is_in_the_shape_of_adr_011_d10():
    subject, text = publish.breach_message(
        target="pipeline", run_id=RUN, result=publish.check_result(result("breach", BREACHING),
                                                                   exit_code=1),
        keys=KEYS["pipeline"], bucket="ws7-fake-eval-bucket", prefix=PREFIX, now=NOW)
    assert subject == f"WS7 breach - target pipeline - run {RUN}"
    assert text.split("\n") == [
        f"WS7 breach - target pipeline - run {RUN}",
        "family assembly, kind 3 (noisy count): assembly_placeholders_left 10, above 2 to 9",
        "family medications, kind 1 (zero-tolerance counter): meds_invented_doses 3, baseline max 1",
        "family safety-netting, kind 2 (per-scenario flip): safety_net_verdict: "
        "2 stable scenarios now fail (S14, S3)",
        "system release: pipeline_version 3f9c...e1 / model anthropic.claude-sonnet-4-6 / caching on",
        f"baseline: b-2026-12-14  evidence: s3://ws7-fake-eval-bucket/{PREFIX}",
        "report time (the alarm): 2026-12-16 04:41 London"]
    publish.check_text(subject, text)
    _subject, v1 = publish.breach_message(
        target="v1", run_id="2026-12-16T0400-v1", result=result("breach", BREACHING[1:2]),
        keys=KEYS["v1"], bucket="ws7-fake-eval-bucket", prefix=PREFIX, now=NOW)
    assert v1.split("\n")[2] == ("system release: v1 prompts 5d0c...9a / 77e1...42 / "
                                 "model anthropic.claude-sonnet-4-6 / caching off")


def test_the_report_time_is_on_the_london_clock_in_winter_and_in_summer():
    assert publish.london(datetime(2026, 12, 16, 4, 41, tzinfo=timezone.utc)) == "2026-12-16 04:41 London"
    assert publish.london(datetime(2027, 6, 16, 3, 41, tzinfo=timezone.utc)) == "2027-06-16 04:41 London"
    for bad in (datetime(2026, 12, 16, 4, 41), "2026-12-16", None):
        assert code_of(lambda: publish.london(bad)) == publish.ALERT_INVALID


def test_many_flipped_scenarios_are_named_up_to_a_limit_and_then_counted():
    flips = [f"S{n}" for n in range(1, 15)]
    row = {"family": "safety_netting", "kind": 2, "breach": 1, "metrics": [
        {"metric": "safety_net_verdict", "breach": 1, "flips": flips}]}
    _subject, text = publish.breach_message(
        target="pipeline", run_id=RUN, result=result("breach", [row]), keys=KEYS["pipeline"],
        bucket="ws7-fake-eval-bucket", prefix=PREFIX, now=NOW)
    assert text.split("\n")[1].endswith(
        "14 stable scenarios now fail (S1, S2, S3, S4, S5, S6, S7, S8, S9, S10 +4 more)")


def _message(**change):
    kwargs = dict(target="pipeline", run_id=RUN, result=result("breach", BREACHING),
                  keys=KEYS["pipeline"], bucket="ws7-fake-eval-bucket", prefix=PREFIX, now=NOW)
    kwargs.update(change)
    return publish.breach_message(**kwargs)


def _flips(value) -> dict:
    return result("breach", [{"family": "safety_netting", "kind": 2, "breach": 1, "metrics": [
        {"metric": "safety_net_verdict", "breach": 1, "flips": value}]}])


@pytest.mark.parametrize("change", [
    {"run_id": f"run {SENTINEL}"}, {"run_id": None}, {"target": "other"},
    {"bucket": f"bucket/{SENTINEL}"}, {"bucket": "Bucket_Name"},
    {"prefix": f"ws7/{SENTINEL} text"}, {"prefix": "retained/x/"}, {"prefix": None},
    {"now": datetime(2026, 12, 16, 4, 41)},
    {"keys": dict(KEYS["pipeline"], pipeline_version=SENTINEL)},
    {"keys": dict(KEYS["pipeline"], model_id=f"model {SENTINEL}")},
    {"keys": dict(KEYS["pipeline"], prompt_caching="on")}, {"keys": None},
    {"keys": dict(KEYS["v1"], v1_prompt_sha256=SENTINEL)},
    {"result": _flips([f"S1 {SENTINEL}"])}, {"result": _flips([7])}, {"result": _flips(SENTINEL)},
    {"result": result("breach", [{"family": "medications", "kind": 1, "breach": 1, "metrics": [
        {"metric": "meds_invented_doses", "breach": 1, "run": SENTINEL, "baseline_max": 1}]}])},
    {"result": result("breach", [{"family": "assembly", "kind": 3, "breach": 1, "metrics": [
        {"metric": "assembly_placeholders_left", "breach": 1, "direction": SENTINEL, "run": 1,
         "low": 0, "high": 0}]}])},
    {"result": result("breach", BREACHING, baseline_id=f"b {SENTINEL}")}], ids=lambda v: str(v)[:60])
def test_a_message_is_not_built_from_anything_that_is_not_an_id_a_count_or_a_fixed_label(change):
    assert _message()                                                   # the partner
    assert code_of(lambda: _message(**change)) == publish.ALERT_INVALID


def test_the_checks_message_has_its_findings_between_a_heading_and_two_footers():
    findings = [publish.NO_CLAIM_LINE, "month 2026-12: assurance waived"]
    subject, text = publish.check_message(target="v1", day="2026-12-16", findings=findings,
                                          bucket="ws7-fake-eval-bucket", now=NOW)
    assert subject == "WS7 check - target v1 - occurrence 2026-12-16"
    assert text.split("\n") == [subject, *findings, "records: s3://ws7-fake-eval-bucket/ws7/",
                                "report time (the alarm): 2026-12-16 04:41 London"]
    assert publish.NO_CLAIM_LINE == ("no claim: the run did not start, or stopped before its "
                                     "claim; see the task log")
    assert "never started" not in publish.NO_CLAIM_LINE
    publish.check_text(subject, text)
    for change in ({"target": "x"}, {"day": "16 Dec"}, {"day": None}, {"findings": "one line"},
                   {"bucket": SENTINEL}):
        kwargs = dict(target="v1", day="2026-12-16", findings=findings,
                      bucket="ws7-fake-eval-bucket", now=NOW)
        kwargs.update(change)
        assert code_of(lambda: publish.check_message(**kwargs)) == publish.ALERT_INVALID


GOOD_SUBJECT = "WS7 check - target v1 - occurrence 2026-12-16"


@pytest.mark.parametrize("subject, text", [
    (GOOD_SUBJECT, f"{GOOD_SUBJECT}\nPatient said: \"I feel worse\""),      # a quotation mark
    (GOOD_SUBJECT, f"{GOOD_SUBJECT}\nline with a tab\there"),
    (GOOD_SUBJECT, f"{GOOD_SUBJECT}\nnaïve"),                               # outside ASCII
    (GOOD_SUBJECT, f"{GOOD_SUBJECT}\n{{\"json\": 1}}"),
    (GOOD_SUBJECT, f"{GOOD_SUBJECT}\n"),                                    # an empty line
    (GOOD_SUBJECT, f"{GOOD_SUBJECT}\n" + "x" * 401),                        # one line too long
    (GOOD_SUBJECT, "\n".join([GOOD_SUBJECT] + ["a line"] * 60)),            # too many lines
    (GOOD_SUBJECT + "\nsecond line", GOOD_SUBJECT),                         # a subject of two lines
    ("s" * 100, GOOD_SUBJECT), ("", GOOD_SUBJECT), (None, GOOD_SUBJECT), (GOOD_SUBJECT, None),
    (GOOD_SUBJECT, "It's fine"), (GOOD_SUBJECT, "50% of patients"), (GOOD_SUBJECT, "a\\b"),
], ids=lambda v: repr(v)[:50])
def test_a_message_with_anything_outside_its_characters_is_never_sent(sns, subject, text):
    """The last check before a message leaves. The partner is sent."""
    pub = publisher("v1", sns=sns)
    assert code_of(lambda: pub.alert(subject, text)) == publish.ALERT_INVALID
    assert sns.bodies == []
    pub.alert(GOOD_SUBJECT, f"{GOOD_SUBJECT}\n{publish.NO_CLAIM_LINE}\n" + "x" * 400)
    assert sns.messages == [{"topic": TOPIC, "subject": GOOD_SUBJECT,
                             "message": f"{GOOD_SUBJECT}\n{publish.NO_CLAIM_LINE}\n" + "x" * 400}]
    assert len(GOOD_SUBJECT) < publish.SUBJECT_MAX == 100


def test_a_message_that_cannot_be_sent_is_tried_once_and_reported_by_a_fixed_code(sns):
    sns.script(500)
    pub = publisher("v1", sns=sns)
    assert code_of(lambda: pub.alert(GOOD_SUBJECT, GOOD_SUBJECT)) == publish.ALERT_FAILED
    assert len(sns.bodies) == 1 and sns.messages == []
    pub.alert(GOOD_SUBJECT, GOOD_SUBJECT)
    assert len(sns.messages) == 1


@pytest.mark.parametrize("arn", [
    None, "", 7, "ws7-fake-eval-alerts", "arn:aws:sns:eu-west-1:000000000000:alerts",
    "arn:aws:sns:us-east-1:000000000000:alerts", "arn:aws:sqs:eu-west-2:000000000000:alerts",
    "arn:aws:sns:eu-west-2:00000000000:alerts", "arn:aws:sns:eu-west-2:000000000000:al erts",
    "arn:aws:sns:eu-west-2:000000000000:alerts:subscription", "arn:aws-cn:sns:eu-west-2:000000000000:a"])
def test_the_alert_topic_is_an_sns_topic_in_eu_west_2_or_it_is_refused(sns, arn):
    assert publish.topic_arn(TOPIC) == TOPIC                             # the partner
    assert code_of(lambda: publish.topic_arn(arn)) == publish.TOPIC_REFUSED
    if arn is not None:             # None is "no topic": a publisher that cannot send (below)
        assert code_of(lambda: publish.Publisher("v1", topic=arn)) == publish.TOPIC_REFUSED
    assert code_of(lambda: publisher("v1").alert(GOOD_SUBJECT, GOOD_SUBJECT)) == publish.ALERT_INVALID
    assert sns.bodies == []


# =============================================================== THE TWO CLIENTS ==

SERVICES = ("cloudwatch", "sns")
LIVE = {"cloudwatch": "https://monitoring.eu-west-2.amazonaws.com",
        "sns": "https://sns.eu-west-2.amazonaws.com"}
OVERRIDES = {"AWS_ENDPOINT_URL": "https://global-override.invalid",
             "AWS_ENDPOINT_URL_CLOUDWATCH": "https://cloudwatch-override.invalid",
             "AWS_ENDPOINT_URL_SNS": "https://sns-override.invalid"}
CONFIG_OVERRIDE = "https://config-file-override.invalid"


class _Stopped(Exception):
    pass


def request_url(service: str, client) -> str:
    """Where a request would go, read just before it is sent. Nothing is sent."""
    seen = []

    def stop(request, **_kwargs):
        seen.append(request.url)
        raise _Stopped()
    client.meta.events.register("before-send", stop)
    with pytest.raises(_Stopped):
        if service == "cloudwatch":
            client.put_metric_data(Namespace=publish.NAMESPACE, MetricData=[
                {"MetricName": "RunOk", "Value": 1.0}])
        else:
            client.publish(TopicArn=TOPIC, Subject="s", Message="m")
    assert len(seen) == 1
    return seen[0]


@pytest.mark.parametrize("service", SERVICES)
def test_a_cloudwatch_or_sns_client_is_the_regions_own_makes_one_attempt_and_is_never_called(service):
    client = store.build_client(service)
    assert client.meta.endpoint_url == LIVE[service] == store.LIVE_ENDPOINTS[service]
    assert client.meta.region_name == "eu-west-2"
    assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": 1}
    assert request_url(service, client) == LIVE[service] + "/"


@pytest.mark.parametrize("source", ["global", "service", "config_file"])
@pytest.mark.parametrize("service", SERVICES)
def test_a_configured_endpoint_url_does_not_move_a_cloudwatch_or_sns_client(monkeypatch, tmp_path,
                                                                           service, source):
    """The positive control first: an ordinary client IS moved by the override."""
    import boto3
    monkeypatch.setenv("AWS_IGNORE_CONFIGURED_ENDPOINT_URLS", "false")
    if source == "config_file":
        path = tmp_path / "aws-config-with-endpoint"
        path.write_text(f"[default]\nendpoint_url = {CONFIG_OVERRIDE}\n", encoding="utf-8")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
        override = CONFIG_OVERRIDE
    else:
        name = "AWS_ENDPOINT_URL" if source == "global" else f"AWS_ENDPOINT_URL_{service.upper()}"
        monkeypatch.setenv(name, OVERRIDES[name])
        override = OVERRIDES[name]
    plain = boto3.Session(region_name="eu-west-2").client(service)
    assert plain.meta.endpoint_url == override
    live = store.build_client(service)
    assert live.meta.endpoint_url == LIVE[service]
    assert request_url(service, live) == LIVE[service] + "/"


@pytest.mark.parametrize("variable", ["AWS_USE_FIPS_ENDPOINT", "AWS_USE_DUALSTACK_ENDPOINT"])
@pytest.mark.parametrize("service", SERVICES)
def test_a_fips_or_dual_stack_variant_is_refused_at_construction(monkeypatch, service, variable):
    assert store.build_client(service)                                    # the partner: unset
    monkeypatch.setenv(variable, "true")
    assert code_of(lambda: store.build_client(service)) == store.ENDPOINT_REFUSED


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("endpoint", ["https://monitoring.eu-west-2.amazonaws.com", "http://localhost:9",
                                      "https://127.0.0.1:9", "http://10.0.0.1:9", "", 9])
def test_an_explicit_cloudwatch_or_sns_endpoint_must_be_the_loopback_fake(service, endpoint):
    assert store.build_client(service, endpoint_url="http://127.0.0.1:9")
    assert code_of(lambda: store.build_client(service, endpoint_url=endpoint)) == store.ENDPOINT_REFUSED


# ============================================================ THE FAKES ARE STRICT ==

def test_each_fake_speaks_the_protocol_this_botocore_uses_for_its_service():
    """If a newer botocore resolves another protocol, this says which, by name, and the
    fakes then refuse every request by that name too."""
    cloudwatch, topic = store.build_client("cloudwatch"), store.build_client("sns")
    assert cloudwatch.meta.service_model.resolved_protocol == fakes.CLOUDWATCH_PROTOCOL == "json"
    assert topic.meta.service_model.resolved_protocol == fakes.SNS_PROTOCOL == "query"


def _raw(fake, headers: dict, body: bytes, path: str = "/") -> int:
    """One request sent by hand, as a client speaking another protocol would send it."""
    host, port = fake.address
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request("POST", path, body=body, headers=headers)
        return connection.getresponse().status
    finally:
        connection.close()


OTHER_PROTOCOLS = [
    ("smithy-rpc-v2-cbor", {"smithy-protocol": "rpc-v2-cbor", "Content-Type": "application/cbor"},
     b"\xa0", "/service/GraniteServiceVersion20100801/operation/PutMetricData"),
    ("query", {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
     b"Action=PutMetricData&Version=2010-08-01&Namespace=x", "/"),
    ("json", {"Content-Type": "application/x-amz-json-1.0",
              "X-Amz-Target": "AmazonSimpleNotificationService.Publish"}, b"{}", "/"),
    ("unknown", {"Content-Type": "text/plain"}, b"hello", "/")]


@pytest.mark.parametrize("name, headers, body, path", OTHER_PROTOCOLS, ids=[p[0] for p in OTHER_PROTOCOLS])
def test_a_request_in_any_other_wire_protocol_is_a_fault_that_names_the_protocol(
        cw, sns, name, headers, body, path):
    """The author's change H. CI's botocore may send CloudWatch or SNS another way. The
    fake does not guess: it answers 501 and records which protocol arrived."""
    if name != fakes.CLOUDWATCH_PROTOCOL:
        assert _raw(cw, headers, body, path) == 501
        assert cw.faults == [f"cloudwatch:protocol:{name}"] and cw.calls == []
        cw.faults.clear()                                               # expected
    if name != fakes.SNS_PROTOCOL:
        assert _raw(sns, headers, body, path) == 501
        assert sns.faults == [f"sns:protocol:{name}"] and sns.messages == []
        sns.faults.clear()


def test_the_cloudwatch_fake_faults_on_anything_but_put_metric_data_as_the_job_sends_it(cw):
    """The control for every metric test: an operation or a field the fake does not
    implement does not pass quietly."""
    raw = store.build_client("cloudwatch", endpoint_url=cw.url)
    datum = {"MetricName": "RunOk", "Value": 1.0, "Unit": "None",
             "Dimensions": [{"Name": "Target", "Value": "v1"}]}
    calls = [lambda: raw.list_metrics(Namespace=publish.NAMESPACE),
             lambda: raw.describe_alarms(),
             lambda: raw.put_metric_data(Namespace=publish.NAMESPACE, MetricData=[
                 dict(datum, Timestamp=datetime(2026, 12, 16, tzinfo=timezone.utc))]),
             lambda: raw.put_metric_data(Namespace=publish.NAMESPACE, MetricData=[
                 dict(datum, StorageResolution=1)]),
             lambda: raw.put_metric_data(Namespace=publish.NAMESPACE, MetricData=[
                 {"MetricName": "RunOk", "Values": [1.0], "Counts": [1.0]}])]
    for call in calls:
        with pytest.raises(ClientError):
            call()
    assert cw.faults == ["cloudwatch:operation:POST:ListMetrics",
                         "cloudwatch:operation:POST:DescribeAlarms", "cloudwatch:datum",
                         "cloudwatch:datum", "cloudwatch:datum"]
    assert cw.calls == []
    cw.faults.clear()
    raw.put_metric_data(Namespace=publish.NAMESPACE, MetricData=[datum])   # the partner
    assert cw.values("RunOk") == [1.0]


def test_the_sns_fake_faults_on_anything_but_publish_as_the_job_sends_it(sns):
    raw = store.build_client("sns", endpoint_url=sns.url)
    calls = [lambda: raw.list_topics(),
             lambda: raw.create_topic(Name="another"),
             lambda: raw.publish(TopicArn=TOPIC, Subject="s", Message="m", MessageStructure="json"),
             lambda: raw.publish(TopicArn=TOPIC, Subject="s", Message="m", MessageAttributes={
                 "a": {"DataType": "String", "StringValue": "b"}}),
             lambda: raw.publish(TopicArn=TOPIC, Message="no subject"),
             lambda: raw.publish(PhoneNumber="+440000000000", Message="m")]
    for call in calls:
        with pytest.raises(ClientError):
            call()
    assert [fault.split(":")[1] for fault in sns.faults] == ["action", "action", "parameters",
                                                             "parameters", "parameters", "parameters"]
    assert sns.faults[:2] == ["sns:action:POST:ListTopics", "sns:action:POST:CreateTopic"]
    assert sns.messages == []
    sns.faults.clear()
    raw.publish(TopicArn=TOPIC, Subject="s", Message="m")               # the partner
    assert sns.messages == [{"topic": TOPIC, "subject": "s", "message": "m"}]


def test_one_sequence_log_holds_the_order_of_requests_across_every_server(_offline, cw, sns):
    """The author's change G. One list under one lock, appended to as each request
    arrives: an order across servers needs no timestamp."""
    s3 = stubs.started(fakes.S3Fake(), _offline)
    ssm = stubs.started(fakes.SsmFake(), _offline)
    bedrock = stubs.started(stubs.v1_stub(), _offline)
    try:
        log = fakes.SequenceLog()
        for name, server in (("s3", s3), ("ssm", ssm), ("cloudwatch", cw), ("sns", sns),
                             ("bedrock", bedrock)):
            log.watch(name, server)
        pub = publisher("v1", cw, sns)
        st = store.Store(store.build_client("s3", endpoint_url=s3.url), fakes.BUCKET)
        ssm.put("/invented", "x")
        pub.alert(GOOD_SUBJECT, GOOD_SUBJECT)
        st.put_if_absent("ws7/ledger/2026-12.json", b"{}")
        store.build_client("ssm", endpoint_url=ssm.url).get_parameter(Name="/invented")
        pub.put([("RunOk", 1), ("NoBaseline", 0)])
        bedrock.receive("combined")
        st.list_keys("ws7/ledger/")
        assert log.all() == [("sns", "Publish"), ("s3", "PUT ws7/ledger/2026-12.json"),
                             ("ssm", "GetParameter /invented"),
                             ("cloudwatch", "PutMetricData RunOk,NoBaseline"),
                             ("bedrock", "combined"), ("s3", "GET ?list")]
        assert log.index("s3") == 1 and log.index("s3", last=True) == 5
        assert log.index("s3", "GET") == 5 and log.servers().count("s3") == 2
        with pytest.raises(AssertionError):
            log.index("s3", "DELETE")               # an order is never proved from no event
    finally:
        for server in (s3, ssm, bedrock):
            server.stop()
