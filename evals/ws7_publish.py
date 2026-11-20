#!/usr/bin/env python3
"""WS7 publishing: the CloudWatch metrics and the job's own SNS messages (ADR-011 D9, D10).

Nothing here calls a model. The entrypoint (evals/ws7_entry.py) decides WHEN each
thing is published; this module decides WHAT may be published, and sends it.

THE METRICS. Namespace `DischargeAssistant/Eval`, one dimension, `Target`. Every
name is one this file can build, and every value is a whole number of zero or more:

    RunOk                     1: the last act of a successful scheduled run.
                              0: a scheduled run that failed while it held its claim.
    RunRefused                1: a scheduled run that stopped, for any reason but a duplicate.
    Duplicate                 1: a duplicate delivery.
    NoBaseline                1: there was no baseline to compare with. 0: compared.
    Breach<Family>Kind<n>     0 or 1: one for each family and kind in the comparison.
                              There is no PART C name (D13): a row for that family
                              publishes nothing and fails the run.
    the Run family's counts   the run summary's own Run metrics, by a fixed renaming
                              (`cost_usd_micros` is `CostUsdMicros`).
    SpendGuardTrips, CallsRefused, CallsNotResponse
                              from the guard's report, for a run that failed after it
                              was admitted and so has no summary to take them from.

A name outside that set, a value that is not a whole number, or a target that is
not one of the two is refused before anything is sent (`ws7_publish_invalid`).

THE MESSAGES. Two shapes, each in the form of ADR-011 D10's example: the breach
message of a scheduled run, and the Thursday check's report. A message is built
from the comparison result (which holds IDs, counts and fixed labels only), run
IDs, hashes, and words written in this file. Every piece is held to a pattern
before it is used, and the finished text is checked once more against the
characters a message may hold. No note text and no model output can be in one.

One attempt per call: the clients are built by `ws7_store.build_client`.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_EVALS = Path(__file__).resolve().parent
for _p in (str(_EVALS.parent / "src" / "generate"), str(_EVALS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ws7_client as wc  # noqa: E402
import ws7_compare as compare  # noqa: E402
import ws7_ledger as ledger  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
import ws7_store as store  # noqa: E402
from ws7_store import Stop  # noqa: E402

NAMESPACE = "DischargeAssistant/Eval"
DIMENSION = "Target"
LONDON = ZoneInfo("Europe/London")

RUN_OK = "RunOk"
RUN_REFUSED = store.REFUSED_METRIC          # "RunRefused"
DUPLICATE = ledger.DUPLICATE_METRIC         # "Duplicate"
NO_BASELINE = "NoBaseline"
STATE_METRICS = (RUN_OK, RUN_REFUSED, DUPLICATE, NO_BASELINE)
# From the guard's report, on the failure path.
GUARD_METRICS = ("SpendGuardTrips", "CallsRefused", "CallsNotResponse")

# The families a breach value is published for. PART C is not one of them (D13).
BREACH_FAMILIES = {"run": "Run", "safety_netting": "SafetyNetting", "medications": "Medications",
                   "step2_advice": "Step2Advice", "composition": "Composition", "haz03": "Haz03",
                   "assembly": "Assembly"}
NOT_PUBLISHED_FAMILY = "part_c"
KIND_LABELS = {1: "zero-tolerance counter", 2: "per-scenario flip", 3: "noisy count"}

PUBLISH_INVALID = "ws7_publish_invalid"
PUBLISH_FAILED = "ws7_publish_failed"
ALERT_INVALID = "ws7_alert_invalid"
ALERT_FAILED = "ws7_alert_failed"
TOPIC_REFUSED = "ws7_topic_refused"
PART_C_RULED = "ws7_part_c_ruled"
RESULT_INVALID = "ws7_compare_result_invalid"
CODES = (PUBLISH_INVALID, PUBLISH_FAILED, ALERT_INVALID, ALERT_FAILED, TOPIC_REFUSED, PART_C_RULED,
         RESULT_INVALID)

_NAME_RE = re.compile(r"[A-Za-z0-9]{1,64}")
_TOPIC_RE = re.compile(r"arn:aws:sns:eu-west-2:[0-9]{12}:[A-Za-z0-9_-]{1,256}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_METRIC_RE = re.compile(r"[a-z0-9_]{1,64}")
_SCENARIO_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_MODEL_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_BUCKET_RE = re.compile(r"[a-z0-9-]{3,63}")
# Every character a message may hold. Letters and digits, a space, and the marks the
# two shapes below use. No quote, no backslash, no control character.
_LINE_RE = re.compile(r"[A-Za-z0-9 ._:;,()/+=<>-]{1,400}")
SUBJECT_MAX = 100             # SNS: a subject is under 100 characters, ASCII, one line
MESSAGE_MAX_LINES = 60
SCENARIOS_SHOWN = 10          # scenario IDs named on one line; the rest are counted


# ------------------------------------------------------------------- names

def pascal(name: str) -> str:
    """`cost_usd_micros` to `CostUsdMicros`: a summary metric's CloudWatch name."""
    if type(name) is not str or not _METRIC_RE.fullmatch(name):
        raise Stop(PUBLISH_INVALID)
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))


def breach_name(family: str, kind: int) -> str:
    if family not in BREACH_FAMILIES or type(kind) is not int or kind not in compare.KINDS:
        raise Stop(PUBLISH_INVALID)
    return f"Breach{BREACH_FAMILIES[family]}Kind{kind}"


def run_metrics(target: str) -> tuple:
    """The run summary's Run-family metrics for `target` (docs/WS7-ENTRYPOINT.md, part
    4, "The metrics"): every one is a count. No verdict metric, nothing from a
    quality family, nothing from PART C."""
    steps = tuple(step.id for step in reserve.plan(target).steps)
    names = ["cost_usd_micros", "tokens_in_total", "tokens_out_total", "tokens_cache_read_total",
             "tokens_cache_write_total", "calls_not_response", "calls_refused",
             "spend_guard_trips", "elapsed_p90_ms", "generations_failed", "gold_draft_records",
             "month_assurance_waived"]
    names += [f"input_tokens_max_{step}" for step in steps]
    if target == "pipeline":
        names += [f"step_failures_{step}" for step in reserve.rpe.STEP_ORDER]
    return tuple(names)


def allowed_names(target: str) -> frozenset:
    """Every metric name that can be published for `target`."""
    if target not in store.TARGETS:
        raise Stop(PUBLISH_INVALID)
    names = set(STATE_METRICS) | set(GUARD_METRICS)
    names |= {breach_name(family, kind) for family in BREACH_FAMILIES for kind in compare.KINDS}
    names |= {pascal(name) for name in run_metrics(target)}
    return frozenset(names)


# ------------------------------------------------------------------ values

def _whole(value: object) -> bool:
    return type(value) is int and value >= 0          # a bool is not a count


def run_counts(summary: object, target: str) -> list:
    """(name, value) for each Run-family count in a run summary."""
    metrics = summary.get("metrics") if type(summary) is dict else None
    out = []
    for name in run_metrics(target):
        value = metrics.get(name) if type(metrics) is dict else None
        count = value.get("count") if type(value) is dict and set(value) == {"count"} else None
        if not _whole(count):
            raise Stop(PUBLISH_INVALID)
        out.append((pascal(name), count))
    return out


def guard_counts(report: object) -> list:
    """The three counters a failed run publishes from the guard's report: over the
    whole run, the pre-flight included."""
    calls = report.get("calls") if type(report) is dict else None
    if type(calls) is not list or not all(type(c) is dict for c in calls) \
            or not _whole(report.get("no_response")):
        raise Stop(PUBLISH_INVALID)
    refused = [c for c in calls if c.get("outcome") == wc.REFUSED]
    return [("SpendGuardTrips", sum(c.get("code") == wc.SPEND_GUARD for c in refused)),
            ("CallsRefused", len(refused)),
            ("CallsNotResponse", report["no_response"])]


def check_result(result: object, *, exit_code: int) -> dict:
    """A comparison result the entrypoint may act on, for exit code 0 or 1: the rows
    it publishes, each in shape. Raises `ws7_compare_result_invalid`, or
    `ws7_part_c_ruled` when a row is for PART C."""
    bad = Stop(RESULT_INVALID)
    if type(result) is not dict or exit_code not in (0, 1):
        raise bad
    status = {0: compare.NO_BREACH, 1: compare.BREACH}[exit_code]
    rows = result.get("comparisons")
    if result.get("schema") != compare.RESULT_SCHEMA or result.get("status") != status \
            or type(result.get("exit_code")) is not int or result["exit_code"] != exit_code \
            or type(rows) is not list or not rows or result.get("errors") != [] \
            or result.get("key_mismatch") != []:
        raise bad
    for name in ("baseline_id", "run_id"):
        if type(result.get(name)) is not str or not store.ID_RE.fullmatch(result[name]):
            raise bad
    seen = set()
    for row in rows:
        if type(row) is not dict or set(row) != {"family", "kind", "breach", "metrics"}:
            raise bad
        family, kind, breach = row["family"], row["kind"], row["breach"]
        if family == NOT_PUBLISHED_FAMILY:
            raise Stop(PART_C_RULED)                # PART C is not published (D13)
        if family not in BREACH_FAMILIES or type(kind) is not int or kind not in compare.KINDS \
                or type(breach) is not int or breach not in (0, 1) or (family, kind) in seen \
                or type(row["metrics"]) is not list or not row["metrics"]:
            raise bad
        seen.add((family, kind))
        for detail in row["metrics"]:
            if type(detail) is not dict or type(detail.get("metric")) is not str \
                    or not _METRIC_RE.fullmatch(detail["metric"]) \
                    or type(detail.get("breach")) is not int or detail["breach"] not in (0, 1):
                raise bad
        if breach != int(any(d["breach"] for d in row["metrics"])):
            raise bad
    if (status == compare.BREACH) != any(row["breach"] for row in rows):
        raise bad
    return result


def breach_values(result: dict) -> list:
    """(name, 0 or 1) for each family and kind in a checked result."""
    return [(breach_name(row["family"], row["kind"]), row["breach"])
            for row in result["comparisons"]]


# ---------------------------------------------------------------- messages

def _need(value: object, pattern) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise Stop(ALERT_INVALID)
    return value


def _number(value: object) -> str:
    if type(value) is not int:
        raise Stop(ALERT_INVALID)
    return str(value)


def _short(digest: object) -> str:
    """A hash as D10's example shows one: `3f9c...e1`."""
    digest = _need(digest, _HEX64_RE)
    return f"{digest[:4]}...{digest[-2:]}"


def _scenarios(ids: object) -> str:
    if type(ids) is not list:
        raise Stop(ALERT_INVALID)
    shown = ", ".join(_need(sid, _SCENARIO_RE) for sid in ids[:SCENARIOS_SHOWN])
    more = len(ids) - SCENARIOS_SHOWN
    return shown + (f" +{more} more" if more > 0 else "")


def london(moment: datetime) -> str:
    """`2026-12-16 04:41 London`: the report time, on the London clock."""
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise Stop(ALERT_INVALID)
    return f"{moment.astimezone(LONDON):%Y-%m-%d %H:%M} London"


def _detail_line(family: str, kind: int, detail: dict) -> str:
    head = f"family {family.replace('_', '-')}, kind {kind} ({KIND_LABELS[kind]}): "
    metric = _need(detail.get("metric"), _METRIC_RE)
    if kind == 1:
        return head + (f"{metric} {_number(detail.get('run'))}, "
                       f"baseline max {_number(detail.get('baseline_max'))}")
    if kind == 2:
        flips = detail.get("flips")
        n = len(flips) if type(flips) is list else None
        return head + (f"{metric}: {_number(n)} stable scenario{'' if n == 1 else 's'} "
                       f"now fail{'s' if n == 1 else ''} ({_scenarios(flips)})")
    direction = detail.get("direction")
    if direction not in ("below", "above"):
        raise Stop(ALERT_INVALID)
    return head + (f"{metric} {_number(detail.get('run'))}, {direction} "
                   f"{_number(detail.get('low'))} to {_number(detail.get('high'))}")


def release_line(keys: object) -> str:
    """`system release: ...`: what was run, from the run's keys."""
    if type(keys) is not dict or keys.get("target") not in store.TARGETS \
            or type(keys.get("prompt_caching")) is not bool:
        raise Stop(ALERT_INVALID)
    if keys["target"] == "pipeline":
        what = f"pipeline_version {_short(keys.get('pipeline_version'))}"
    else:
        prompts = keys.get("v1_prompt_sha256")
        if type(prompts) is not dict:
            raise Stop(ALERT_INVALID)
        what = (f"v1 prompts {_short(prompts.get('system_prompt'))} / "
                f"{_short(prompts.get('patient_system_prompt'))}")
    return (f"system release: {what} / model {_need(keys.get('model_id'), _MODEL_RE)} / "
            f"caching {'on' if keys['prompt_caching'] else 'off'}")


def breach_message(*, target: str, run_id: str, result: dict, keys: dict, bucket: str,
                   prefix: str, now: datetime) -> tuple:
    """(subject, text) of a scheduled run's breach message, in the shape of D10's
    example. `result` has passed `check_result` with exit code 1."""
    if target not in store.TARGETS:
        raise Stop(ALERT_INVALID)
    subject = f"WS7 breach - target {target} - run {_need(run_id, store.ID_RE)}"
    lines = [subject]
    for row in result["comparisons"]:
        if row["breach"]:
            lines += [_detail_line(row["family"], row["kind"], detail)
                      for detail in row["metrics"] if detail["breach"]]
    lines.append(release_line(keys))
    lines.append(f"baseline: {_need(result.get('baseline_id'), store.ID_RE)}  "
                 f"evidence: s3://{_need(bucket, _BUCKET_RE)}/{_evidence(prefix)}")
    lines.append(f"report time (the alarm): {london(now)}")
    return subject, "\n".join(lines)


def _evidence(prefix: object) -> str:
    if type(prefix) is not str or not prefix.startswith(store.PREFIX) \
            or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", prefix):
        raise Stop(ALERT_INVALID)
    return prefix


NO_CLAIM_LINE = ("no claim: the run did not start, or stopped before its claim; "
                 "see the task log")


def check_message(*, target: str, day: str, findings: list, bucket: str,
                  now: datetime) -> tuple:
    """(subject, text) of the Thursday check's report. `findings` are lines the
    entrypoint built from IDs, fixed codes and the words above."""
    if target not in store.TARGETS or type(day) is not str \
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day) or type(findings) is not list:
        raise Stop(ALERT_INVALID)
    subject = f"WS7 check - target {target} - occurrence {day}"
    lines = [subject, *findings,
             f"records: s3://{_need(bucket, _BUCKET_RE)}/{store.PREFIX}",
             f"report time (the alarm): {london(now)}"]
    return subject, "\n".join(lines)


def check_text(subject: object, text: object) -> None:
    """The last check before a message leaves: one line of subject, a bounded number
    of lines, and no character outside the set a message may hold."""
    if type(subject) is not str or type(text) is not str or len(subject) >= SUBJECT_MAX \
            or not _LINE_RE.fullmatch(subject):
        raise Stop(ALERT_INVALID)
    lines = text.split("\n")
    if not 1 <= len(lines) <= MESSAGE_MAX_LINES or not all(_LINE_RE.fullmatch(l) for l in lines):
        raise Stop(ALERT_INVALID)


# ---------------------------------------------------------------- sending

def topic_arn(value: object) -> str:
    """The alert topic, from the task definition: an SNS topic ARN in eu-west-2."""
    if type(value) is not str or not _TOPIC_RE.fullmatch(value):
        raise Stop(TOPIC_REFUSED)
    return value


class Publisher:
    """One target's metrics and messages. `cloudwatch` and `sns` are clients from
    `ws7_store.build_client`; either may be None for a mode that never uses it, and
    a call that needs it is then refused."""

    def __init__(self, target: str, *, cloudwatch=None, sns=None, topic: str | None = None):
        if target not in store.TARGETS:
            raise Stop(PUBLISH_INVALID)
        self.target = target
        self._cloudwatch = cloudwatch
        self._sns = sns
        self._topic = None if topic is None else topic_arn(topic)
        self._names = allowed_names(target)

    def put(self, values: list) -> None:
        """Publish (name, value) pairs in one PutMetricData call. Everything is
        checked first; a list that fails sends nothing."""
        if self._cloudwatch is None or type(values) not in (list, tuple) or not values:
            raise Stop(PUBLISH_INVALID)
        data, seen = [], set()
        for item in values:
            if type(item) is not tuple or len(item) != 2:
                raise Stop(PUBLISH_INVALID)
            name, value = item
            if type(name) is not str or not _NAME_RE.fullmatch(name) or name not in self._names \
                    or name in seen or not _whole(value):
                raise Stop(PUBLISH_INVALID)
            if name in STATE_METRICS or name.startswith("Breach"):
                if value not in (0, 1):
                    raise Stop(PUBLISH_INVALID)
            seen.add(name)
            data.append({"MetricName": name, "Value": float(value), "Unit": "None",
                         "Dimensions": [{"Name": DIMENSION, "Value": self.target}]})
        try:
            self._cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=data)
        except Exception:               # noqa: BLE001 - whatever stopped it, it was not published
            raise Stop(PUBLISH_FAILED) from None

    def alert(self, subject: str, text: str) -> None:
        """Send one message to the alert topic. The text is checked first."""
        if self._sns is None or self._topic is None:
            raise Stop(ALERT_INVALID)
        check_text(subject, text)
        try:
            self._sns.publish(TopicArn=self._topic, Subject=subject, Message=text)
        except Exception:               # noqa: BLE001 - whatever stopped it, it was not sent
            raise Stop(ALERT_FAILED) from None
