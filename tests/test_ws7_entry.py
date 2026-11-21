"""evals/ws7_entry.py - the scheduled run and the Thursday check (WS7 slice 2 part 5a).

Offline. Every test runs the real entrypoint in-process against five local servers on
127.0.0.1 (tests/ws7_world.py): the Bedrock stub and strict S3, SSM, CloudWatch and SNS
fakes. Every batch is INVENTED: the real runners against the stub, with `runs_dir` in a
temporary folder, and the real scorers, unchanged, on that fresh batch. Nothing is read
from evals/runs/, and no test can reach a real AWS endpoint (`offline_aws`).

ORDER is proved from one locked sequence log that all five servers append to as each
request arrives, never from timestamps. TIME is one injected clock that moves only when
a test moves it; no test sleeps, and every entrypoint call has a bounded wait.

Every refusal has a partner that is not refused, and every "nothing was sent" has a
positive control in which something was.
"""

from __future__ import annotations

import argparse
import builtins
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "tests", ROOT / "src" / "generate", ROOT / "evals"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import run_cold_eval as rce  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402
import score_assemble as SA  # noqa: E402
import score_batch as SB  # noqa: E402
import ws7_client as wc  # noqa: E402
import ws7_compare as compare  # noqa: E402
import ws7_entry as entry  # noqa: E402
import ws7_fakes as fakes  # noqa: E402
import ws7_ledger as wl  # noqa: E402
import ws7_manifest as wm  # noqa: E402
import ws7_publish as publish  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
import ws7_store as store  # noqa: E402
import ws7_stub as stubs  # noqa: E402
import ws7_summary as ws  # noqa: E402
import ws7_world as W  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from pipeline import StepContext  # noqa: E402
from ws7_stub import ERROR_500, THROTTLE, offline_aws  # noqa: E402,F401  (a fixture)
from ws7_world import (BUCKET, CFG, CHECK_WHEN, CLAIM_KEY, FIRST_STEP, LEDGER_KEY, MARGIN,  # noqa: E402
                       RUN_ID, SCENARIOS, SENTINEL, STEPS, TARGETS, WHEN)

PRE = {t: f"{RUN_ID[t]}.preflight" for t in TARGETS}
MAIN = {t: f"{RUN_ID[t]}.main" for t in TARGETS}
PREFIX = {t: f"ws7/runs/scheduled/{RUN_ID[t]}/" for t in TARGETS}
CANARY = rce.dt.datetime(2026, 12, 14, 3, 0, tzinfo=rce.LONDON)     # inside Monday's canary window
RESERVED = {t: reserve.reservation(t, len(SCENARIOS) + 1, MARGIN) for t in TARGETS}
ONE = {t: reserve.reservation(t, 1, MARGIN)["total_usd_nanos"] for t in TARGETS}
N_STEPS = {t: len(STEPS[t]) for t in TARGETS}
LIMIT_S = CFG.wall_clock_limit_scheduled_s
CHILD_LIMIT_S = 60.0


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def world(offline_aws, tmp_path, monkeypatch):  # noqa: F811
    w = W.World(offline_aws, tmp_path, monkeypatch)
    yield w
    w.stop()


@pytest.fixture
def ready(world, tmp_path_factory):
    """`ready(target)`: the stub, the expected values and a frozen invented baseline."""
    def make(target: str, stub=None, baseline: dict | None = None):
        return world.ready(target, tmp_path_factory, stub=stub, baseline=baseline)
    return make


@pytest.fixture
def baseline_of(world, tmp_path_factory):
    return lambda target: W.invented_baseline(target, world.net, tmp_path_factory)


def code_in(world, target: str) -> str:
    """The failed record's code. A run that wrote none fails the test here."""
    return world.failed(RUN_ID[target])["code"]


def stopped_with(world, code: str) -> bool:
    return any(line.startswith(f"ws7 stop code={code}") for line in world.lines)


def events(world, server: str) -> list:
    return [what for srv, what in world.log.all() if srv == server]


def run_keys(world, target: str) -> list:
    return [k for k in world.s3.keys() if k.startswith(PREFIX[target])]


def no_success(world, target: str) -> bool:
    return store.success_key(RUN_ID[target]) not in world.s3.keys()


# Every way code the job does not own can end without returning: an exception, an
# interrupt, and a SystemExit with each kind of code. Each is made new when it is raised.
ENDINGS = {"Exception": lambda: RuntimeError(SENTINEL), "KeyboardInterrupt": KeyboardInterrupt,
           "SystemExit(0)": lambda: SystemExit(0), "SystemExit(1)": lambda: SystemExit(1),
           "SystemExit(2)": lambda: SystemExit(2), "SystemExit(None)": lambda: SystemExit(None),
           "SystemExit(text)": lambda: SystemExit(SENTINEL)}


def _ends(ending: str):
    """A function that ends as `ending` says, whatever it is called with."""
    def ended(*_args, **_kwargs):
        raise ENDINGS[ending]()
    return ended


# ============================================================ A SUCCESSFUL RUN ==

@pytest.mark.parametrize("target", TARGETS)
def test_a_successful_run_follows_the_order_and_ends_with_runok_1(ready, world, target):
    """The order of "The order of a scheduled run", read from the one sequence log."""
    ready(target)
    assert world.run(target) == 0
    log = world.log
    match = log.index("ssm", "GetParameter")
    claim = log.index("s3", f"PUT {CLAIM_KEY[target]}")
    booked = log.index("s3", f"PUT {LEDGER_KEY}")
    first_call, last_call = log.index("bedrock"), log.index("bedrock", last=True)
    settled = log.index("s3", f"PUT {LEDGER_KEY}", last=True)
    first_stored = log.index("s3", f"PUT {PREFIX[target]}")
    manifest = log.index("s3", f"PUT {PREFIX[target]}manifest.json")
    metrics = log.index("cloudwatch", "PutMetricData NoBaseline")
    success = log.index("s3", f"PUT {store.success_key(RUN_ID[target])}")
    run_ok = log.index("cloudwatch", "PutMetricData RunOk")
    # the match check, then the claim, then the reservation, and only then a model call
    assert log.index("s3") > match and log.index("bedrock") > match
    assert match < claim < booked < first_call
    # settled as soon as the last runner returned, before anything was stored
    assert last_call < settled < first_stored <= manifest < metrics < success < run_ok
    assert booked != settled
    # RunOk = 1 is the last request any server received, and it was sent once
    assert log.all()[-1] == ("cloudwatch", "PutMetricData RunOk")
    assert world.metric("RunOk") == [1]
    assert world.stub.counts() == {step: len(SCENARIOS) + 1 for step in STEPS[target]}
    assert world.sns.messages == []                 # no breach: the job sends no message


@pytest.mark.parametrize("target", TARGETS)
def test_a_successful_run_settles_at_what_the_guard_counted(ready, world, target):
    ready(target)
    assert world.run(target) == 0
    report = world.s3.json(PREFIX[target] + "guard_report.json")
    booked = world.entry_of(RUN_ID[target])
    assert report["run_id"] == RUN_ID[target]       # the guard was made with the admission's ID
    assert sorted(report["batches"]) == [MAIN[target], PRE[target]]
    assert (booked["state"], booked["basis"], booked["mode"]) == ("settled", "metered", "scheduled")
    assert booked["reserved_usd_micros"] == RESERVED[target]["total_usd_micros"]
    assert booked["counted_usd_micros"] == report["total_usd_micros"] > 0
    assert report["reservation_usd_nanos"] == RESERVED[target]["total_usd_nanos"]


@pytest.mark.parametrize("target", TARGETS)
def test_what_a_successful_run_stores_and_where(ready, world, target):
    """D8: the manifest, the batch records, the scores, the assembly report, the
    summary and the comparison result, under the run's own prefix. The success record
    is kept, so it is outside the prefix the lifecycle rule expires."""
    ready(target)
    assert world.run(target) == 0
    run = RUN_ID[target]
    files = {f"batches/{PRE[target]}/{name}"
             for name in entry.expected_files(target, [CFG.preflight_scenario], scored=False)}
    files |= {f"batches/{MAIN[target]}/{name}"
              for name in entry.expected_files(target, SCENARIOS, scored=True)}
    files |= {"guard_report.json", "summary.json", "comparison.json", "manifest.json"}
    assert set(world.s3.keys()) == {PREFIX[target] + name for name in files} | {
        CLAIM_KEY[target], LEDGER_KEY, store.success_key(run)}
    # the reports are stored as Markdown and everything else as JSON
    kinds = {key: world.s3.content_types[key] for key in run_keys(world, target)}
    assert {kind for key, kind in kinds.items() if key.endswith(".md")} == {store.MARKDOWN_TYPE}
    assert {kind for key, kind in kinds.items() if key.endswith(".json")} == {store.JSON_TYPE}
    assert any(key.endswith(".md") for key in kinds)

    summary = world.s3.json(PREFIX[target] + "summary.json")
    result = world.s3.json(PREFIX[target] + "comparison.json")
    manifest = world.s3.json(PREFIX[target] + "manifest.json")
    assert (summary["schema"], summary["run_id"]) == (compare.RUN_SCHEMA, MAIN[target])
    assert (result["status"], result["run_id"], result["baseline_id"]) == (
        "no_breach", MAIN[target], W.BASELINE_ID)
    assert manifest["keys"] == summary["keys"]
    assert (manifest["run_id"], manifest["baseline_id"], manifest["git_commit"],
            manifest["image_digest"]) == (run, W.BASELINE_ID, W.COMMIT, W.DIGEST)

    record = world.s3.json(store.success_key(run))
    assert record == {
        "schema": entry.SUCCESS_SCHEMA, "run_id": run, "target": target, "mode": "scheduled",
        "scheduled_time": WHEN, "status": "no_breach", "breaches": 0, "baseline_id": W.BASELINE_ID,
        "summary_sha256": entry._sha256(world.s3.body(PREFIX[target] + "summary.json")),
        "comparison_sha256": entry._sha256(world.s3.body(PREFIX[target] + "comparison.json")),
        "evidence": PREFIX[target], "git_commit": W.COMMIT, "image_digest": W.DIGEST,
        "finished_at": store.utc_text(world.clock.wall())}
    assert not store.success_key(run).startswith("ws7/runs/scheduled/")
    assert not store.failed_key(run).startswith("ws7/runs/scheduled/")


@pytest.mark.parametrize("target", TARGETS)
def test_the_metrics_of_a_successful_run(ready, world, target):
    """D9: one breach value per family and kind the rules name, the Run family's
    counts, "no baseline" at 0, and RunOk. One namespace, one dimension."""
    ready(target)
    assert world.run(target) == 0
    assert [call["namespace"] for call in world.cw.calls] == [publish.NAMESPACE] * 2
    assert all(dims == {"Target": target} for _n, _v, dims in world.cw.datums())
    assert all(d["unit"] == "None" for call in world.cw.calls for d in call["data"])
    out = world.published()
    summary = world.s3.json(PREFIX[target] + "summary.json")
    run_names = {publish.pascal(name): name for name in publish.run_metrics(target)}
    breaches = {"BreachSafetyNettingKind1", "BreachSafetyNettingKind2", "BreachMedicationsKind1",
                "BreachRunKind3"}
    assert set(out) == {"RunOk", "NoBaseline"} | breaches | set(run_names)
    assert set(out) <= publish.allowed_names(target)
    assert all(len(values) == 1 for values in out.values())
    assert out["RunOk"] == [1] and out["NoBaseline"] == [0]
    assert all(out[name] == [0] for name in breaches)
    for published, name in run_names.items():
        assert out[published] == [summary["metrics"][name]["count"]], name
    assert out["CostUsdMicros"][0] > 0 and len(run_names) == {"pipeline": 25, "v1": 14}[target]
    # no quality count, no verdict and nothing from PART C is among them
    assert not any(n.startswith(("Sn", "Meds", "Step2", "Compose", "Haz03", "Assembly", "Patient"))
                   or "PartC" in n for n in out)


@pytest.mark.parametrize("target", TARGETS)
def test_the_file_list_is_what_the_real_runners_and_scorers_write(ready, world, target):
    """`expected_files` is pinned against batches the real runners and scorers made:
    equal in both directions, for the unscored pre-flight and the scored main batch."""
    ready(target)
    assert world.run(target) == 0
    pre, other = entry.files_in(world.runs_dir / PRE[target])
    assert other == 0
    assert set(pre) == entry.expected_files(target, [CFG.preflight_scenario], scored=False)
    main, other = entry.files_in(world.runs_dir / MAIN[target])
    assert other == 0
    assert set(main) == entry.expected_files(target, SCENARIOS, scored=True)
    assert set(main) - entry.expected_files(target, SCENARIOS, scored=False)     # scoring added some
    assert all(store.content_type(name) for name in main)


# ============================================ NOTHING IS SPENT BEFORE THE CLAIM ==

@pytest.mark.parametrize("target", TARGETS)
def test_a_match_that_differs_stops_before_the_claim_and_spends_nothing(ready, world, target):
    """The match check comes first (D11). Its partner is the successful run above, in
    which the same check passes and a claim is written."""
    ready(target)
    world.expect(target, model_id="another.model-id")
    assert world.run(target) == 1
    assert stopped_with(world, wm.MATCH_DIFFERS)
    # the whole run, as the servers saw it: the expected values were read, and one count
    assert world.log.all() == [("ssm", f"GetParameter {wm.EXPECTED_PARAMETER[target]}"),
                               ("cloudwatch", "PutMetricData RunRefused")]
    assert world.s3.requests == [] and world.bedrock_requests() == 0
    # RunOk comes only from a run that holds its claim: this one publishes neither value
    assert world.published() == {"RunRefused": [1]}


def _settled(run: str, counted: int, target: str = "pipeline") -> dict:
    return {"run_id": run, "target": target, "mode": "scheduled", "state": "settled", "kind": None,
            "basis": "metered", "reserved_usd_micros": counted or 1, "counted_usd_micros": counted,
            "estimate_usd_micros": None, "opened_at": "2026-12-02T04:00:30Z",
            "expires_at": "2026-12-02T06:00:30Z", "closed_at": "2026-12-02T04:38:10Z",
            "resolved_at": None}


def seed_ledger(world, entries: list, month: str = "2026-12", waived: bool = False) -> None:
    world.s3.seed(store.ledger_key(month), wl._dump({
        "schema": wl.LEDGER_SCHEMA, "month": month, "rev": 1, "write_id": "a" * 32,
        "cap_usd_micros": CFG.cap_usd_micros, "assurance_waived": waived, "entries": entries}))


@pytest.mark.parametrize("over", [1, 0])
def test_a_run_the_cap_refuses_makes_no_model_call(ready, world, over):
    """Reserve first (D7 point 2). One micro-dollar over the cap: refused, nothing
    sent to the stub. Exactly at the cap: admitted, and the run goes through."""
    target = "pipeline"
    ready(target)
    spent = CFG.cap_usd_micros - RESERVED[target]["total_usd_micros"] + over
    seed_ledger(world, [_settled("2026-12-02T0400-pipeline", spent)])
    code = world.run(target)
    if over:
        assert code == 1 and code_in(world, target) == wl.OVER_CAP
        assert world.bedrock_requests() == 0
        assert world.published() == {"RunRefused": [1], "RunOk": [0]}       # it held its claim
        assert len(world.s3.json(LEDGER_KEY)["entries"]) == 1               # nothing was booked
    else:
        assert code == 0 and world.metric("RunOk") == [1]
        assert world.bedrock_requests() == N_STEPS[target] * (len(SCENARIOS) + 1)


@pytest.mark.parametrize("target", TARGETS)
def test_a_second_delivery_is_a_duplicate_that_spends_nothing_and_never_publishes_runok(
        ready, world, target):
    ready(target)
    assert world.run(target) == 0
    before = len(world.log.all())
    calls, ledger = world.bedrock_requests(), world.s3.body(LEDGER_KEY)
    assert world.run(target) == 0                   # a duplicate is not a failure
    later = world.log.all()[before:]
    assert stopped_with(world, wl.DUPLICATE)
    # it read the expected values, found the claim taken, and counted one duplicate
    assert later == [("ssm", f"GetParameter {wm.EXPECTED_PARAMETER[target]}"),
                     ("s3", f"PUT {CLAIM_KEY[target]}"),
                     ("cloudwatch", "PutMetricData Duplicate")]
    assert world.bedrock_requests() == calls and world.s3.body(LEDGER_KEY) == ledger
    out = world.published()
    assert out["Duplicate"] == [1] and out["RunOk"] == [1]      # the one RunOk is the first run's
    assert "RunRefused" not in out
    assert store.failed_key(RUN_ID[target]) not in world.s3.keys()


def _lose_identity(world, _target):
    del world.environ[wm.DIGEST_VARIABLE]


def _lose_topic(world, _target):
    world.environ[entry.TOPIC_VARIABLE] = "arn:aws:sns:us-east-1:000000000000:elsewhere"


def _lose_bucket(world, _target):
    del world.environ[entry.BUCKET_VARIABLE]


def _lose_expected(world, target):
    del world.ssm.parameters[wm.EXPECTED_PARAMETER[target]]


@pytest.mark.parametrize("break_it, code", [
    (_lose_identity, wm.IDENTITY_MISSING), (_lose_topic, publish.TOPIC_REFUSED),
    (_lose_bucket, store.BUCKET_REFUSED), (_lose_expected, wm.EXPECTED_UNREADABLE)])
def test_a_stop_before_the_claim_writes_nothing_and_publishes_runrefused_alone(
        ready, world, break_it, code):
    """Before the claim a process cannot know it is not a duplicate, so it publishes
    no RunOk. It writes no S3 object and makes no model call."""
    target = "v1"
    ready(target)
    break_it(world, target)
    assert world.run(target) == 1
    assert stopped_with(world, code)
    assert world.s3.requests == [] and world.bedrock_requests() == 0 and world.sns.messages == []
    assert world.published() == {"RunRefused": [1]}


@pytest.mark.parametrize("when, start, code", [
    ("2026-12-16T04:00:00", W.START, wl.SCHEDULED_TIME_INVALID),               # no zone
    ("2026-12-16T05:00:00Z", W.START, wl.SCHEDULED_TIME_FUTURE),
    (WHEN, W.START + timedelta(seconds=CFG.max_start_delay_s), wl.START_TOO_LATE)])
def test_a_scheduled_time_the_claim_refuses_stops_the_run_before_any_write(
        ready, world, when, start, code):
    target = "pipeline"
    ready(target)
    world.clock.start = start
    assert world.run(target, when) == 1
    assert stopped_with(world, code)
    assert not any(method == "PUT" for method, _key in world.s3.requests)
    assert world.bedrock_requests() == 0 and world.published() == {"RunRefused": [1]}


def test_the_oldest_scheduled_time_a_run_may_still_claim_is_claimed(ready, world):
    """The partner of `ws7_start_too_late`: exactly at `max_start_delay_s`."""
    target = "pipeline"
    ready(target)
    world.clock.start = datetime(2026, 12, 16, 4, 0, tzinfo=timezone.utc) + timedelta(
        seconds=CFG.max_start_delay_s)
    assert world.run(target) == 0 and world.metric("RunOk") == [1]


# ================================================== SETTLE BEFORE ANY SCORING ==

@pytest.mark.parametrize("target", TARGETS)
def test_the_ledger_is_settled_before_anything_is_scored(ready, world, target):
    """`settle()` runs as soon as the last runner returns. At the moment the settling
    write lands, the main batch has its records and no score file. The control: the
    scores are there when the run ends."""
    ready(target)
    main = world.runs_dir / MAIN[target]
    seen: dict = {}

    def at_booking():
        seen["booked"] = world.s3.json(LEDGER_KEY)["entries"][-1]["state"]

    def at_settling():
        seen["settled"] = world.s3.json(LEDGER_KEY)["entries"][-1]["state"]
        seen["records"] = (main / "batch.json").is_file()
        seen["scores"] = (main / "scores").exists()
        seen["work"] = (world.runs_dir / f"{RUN_ID[target]}.work").exists()

    world.s3.after_put(LEDGER_KEY, at_booking)      # the first PUT books the run
    world.s3.after_put(LEDGER_KEY, at_settling)     # the second settles it
    assert world.run(target) == 0
    assert seen == {"booked": "open", "settled": "settled", "records": True, "scores": False,
                    "work": False}
    assert (main / "scores" / "safety_net.json").is_file()


# ============================== ONCE A RUN HAS FAILED IT SPENDS NOTHING MORE ==

@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("throttle", [True, False])
def test_a_throttle_on_the_main_batchs_second_scenario_stops_every_later_call(
        ready, world, target, throttle):
    """The author's change A. A throttled call fails the run (ruling e2), and no
    request for any later scenario reaches the stub. The partner, with no throttle,
    sends all 18."""
    stub = W.default_stub(target)
    if throttle:
        W.fail_nth(stub, FIRST_STEP[target], 3, THROTTLE)   # 1: the pre-flight; 2, 3: the main batch
    ready(target, stub=stub)
    code = world.run(target)
    if not throttle:
        assert code == 0
        assert world.stub.counts() == {step: len(SCENARIOS) + 1 for step in STEPS[target]}
        return
    assert code == 1
    # the pre-flight's steps, the first main scenario's steps, and the one throttled call
    assert world.stub.count(FIRST_STEP[target]) == 3
    assert world.bedrock_requests() == 2 * N_STEPS[target] + 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (entry.CALL_FAILED, ["throttled", MAIN[target]])
    # every later first-step call of the 16 scenarios left, and the throttled call's own
    # retry, were refused here, before the guard or the stub saw them
    assert record["counts"]["calls_stopped"] == len(SCENARIOS) - 2 + 1
    assert record["counts"]["call"] == 2 * N_STEPS[target] + 1
    # every record of the batch was still written, each later one with the refusal's code
    index = json.loads((world.runs_dir / MAIN[target] / "batch.json").read_text("utf-8"))
    assert len(index["generations"]) == len(SCENARIOS)
    assert sum(g["error_code"] is not None for g in index["generations"]) == len(SCENARIOS) - 1
    # it settled from the guard's report: a throttle did not run the model, so it is metered
    report = world.s3.json(PREFIX[target] + "guard_report.json")
    booked = world.entry_of(RUN_ID[target])
    assert (booked["state"], booked["basis"]) == ("settled", "metered")
    assert booked["counted_usd_micros"] == report["total_usd_micros"]
    assert (record["ledger"], record["evidence"], record["also"]) == ("settled", "stored", [])
    assert world.published() == {"RunRefused": [1], "RunOk": [0], "SpendGuardTrips": [0],
                                 "CallsRefused": [0], "CallsNotResponse": [1]}
    assert no_success(world, target) and world.sns.messages == []


def test_a_throttle_at_a_later_step_stops_the_rest_of_that_generation_and_the_run(ready, world):
    target = "pipeline"
    stub = W.default_stub(target)
    W.fail_nth(stub, "reconcile_medications", 3, THROTTLE)
    ready(target, stub=stub)
    assert world.run(target) == 1
    assert world.stub.counts() == {"extract_facts": 3, "reconcile_medications": 3,
                                   "compose_clinical": 2, "compose_patient": 2}
    assert code_in(world, target) == entry.CALL_FAILED


@pytest.mark.parametrize("target", TARGETS)
def test_a_500_makes_the_run_unmetered_and_its_entry_unresolved(ready, world, target):
    stub = W.default_stub(target)
    W.fail_nth(stub, FIRST_STEP[target], 3, ERROR_500)
    ready(target, stub=stub)
    assert world.run(target) == 1
    assert world.bedrock_requests() == 2 * N_STEPS[target] + 1      # nothing after it
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (wc.UNMETERED, ["server_error", MAIN[target]])
    booked = world.entry_of(RUN_ID[target])
    assert (booked["state"], booked["kind"]) == ("unresolved", "unmetered")
    assert record["ledger"] == "unmetered"
    assert world.published()["RunOk"] == [0]


@pytest.mark.parametrize("target", TARGETS)
def test_a_failed_pre_flight_never_starts_the_main_batch(ready, world, target):
    stub = W.default_stub(target)
    W.fail_nth(stub, FIRST_STEP[target], 1, THROTTLE)
    ready(target, stub=stub)
    assert world.run(target) == 1
    assert world.bedrock_requests() == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (entry.CALL_FAILED, ["throttled", PRE[target]])
    assert (world.runs_dir / PRE[target]).is_dir() and not (world.runs_dir / MAIN[target]).exists()
    assert world.entry_of(RUN_ID[target])["state"] == "settled"


def test_the_first_failure_names_the_run_whatever_follows_it():
    """`first_failure`: the first call that is not a response, and nothing after it."""
    def row(seq, outcome, code=None):
        return {"seq": seq, "outcome": outcome, "code": code}
    ok = row(1, wc.RESPONSE)
    assert entry.first_failure({"calls": [ok, ok]}) is None
    cases = [(row(2, "throttled"), entry.CALL_FAILED, ("throttled",)),
             (row(2, "connect_error"), entry.CALL_FAILED, ("connect_error",)),
             (row(2, "request_invalid"), entry.CALL_FAILED, ("request_invalid",)),
             (row(2, "server_error"), wc.UNMETERED, ("server_error",)),
             (row(2, "read_timeout"), wc.UNMETERED, ("read_timeout",)),
             (row(2, wc.USAGE_UNKNOWN), wc.UNMETERED, (wc.USAGE_UNKNOWN,)),
             (row(2, "something else"), wc.UNMETERED, ("unknown",)),
             (row(2, wc.REFUSED, wc.SPEND_GUARD), wc.SPEND_GUARD, ()),
             (row(2, wc.REFUSED, wc.DEADLINE), wc.DEADLINE, ()),
             (row(2, wc.REFUSED, "not a code"), entry.CALL_FAILED, ())]
    for failing, code, ids in cases:
        stop = entry.first_failure({"calls": [ok, failing, row(3, "server_error")]})
        assert (stop.code, stop.ids, stop.counts) == (code, ids, {"call": 2}), failing


# =================================================================== THE DEADLINE ==

def _advance_once_booked(world, seconds: float) -> None:
    """Move the clock as the run's reservation lands in the ledger: between admission
    and the first runner."""
    world.s3.after_put(LEDGER_KEY, lambda: world.clock.advance(seconds))


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("before_deadline", [0, 1])
def test_no_runner_is_started_at_the_guards_deadline_and_one_second_before_it_one_is(
        ready, world, target, before_deadline):
    """Dispatch stops at the deadline, which is SETTLE_MARGIN_S before `deadline_at`.
    At the boundary no runner starts: no batch folder, no request. One second earlier
    the run goes through (the injected clock does not move again)."""
    ready(target)
    to_deadline = LIMIT_S - wl.DEADLINE_MARGIN_S - entry.SETTLE_MARGIN_S
    assert to_deadline > 0
    _advance_once_booked(world, to_deadline - before_deadline)
    code = world.run(target)
    if before_deadline:
        assert code == 0 and world.metric("RunOk") == [1]
        assert world.bedrock_requests() == N_STEPS[target] * (len(SCENARIOS) + 1)
        return
    assert code == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (wc.DEADLINE, [PRE[target]])
    assert world.bedrock_requests() == 0
    assert not (world.runs_dir / PRE[target]).exists()          # the runner was never started
    report = world.s3.json(PREFIX[target] + "guard_report.json")
    assert (report["calls"], report["batches"]) == ([], {})     # and the guard was never asked
    booked = world.entry_of(RUN_ID[target])
    assert (booked["state"], booked["basis"], booked["counted_usd_micros"]) == ("settled", "metered", 0)


def test_the_settle_margin_is_what_separates_the_guards_deadline_from_deadline_at(ready, world):
    """With the clock SETTLE_MARGIN_S short of `deadline_at`, the admission's own
    deadline has not come (the run is admitted), and the guard's has (nothing runs)."""
    target = "v1"
    ready(target)
    _advance_once_booked(world, LIMIT_S - wl.DEADLINE_MARGIN_S - entry.SETTLE_MARGIN_S)
    assert world.run(target) == 1
    assert code_in(world, target) == wc.DEADLINE                 # not ws7_admitted_past_deadline
    assert entry.SETTLE_MARGIN_S == 300


@pytest.mark.parametrize("target", TARGETS)
def test_a_deadline_that_passes_mid_run_stops_every_later_call_and_the_run_still_settles(
        ready, world, target):
    """No generation is begun at or after the deadline. The clock passes it while the
    main batch's second scenario is being answered; the wrapper refuses the next call,
    and nothing after it is sent. The settle is still inside `expires_at`."""
    stub = W.default_stub(target)
    past = LIMIT_S - wl.DEADLINE_MARGIN_S - entry.SETTLE_MARGIN_S
    W.on_nth(stub, FIRST_STEP[target], 3, lambda: world.clock.advance(past))
    ready(target, stub=stub)
    assert world.run(target) == 1
    assert world.bedrock_requests() == 2 * N_STEPS[target] + 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ledger"]) == (wc.DEADLINE, "settled")
    assert world.entry_of(RUN_ID[target])["basis"] == "metered"


def test_a_settle_decided_after_expires_at_is_refused_and_the_failed_record_says_so(ready, world):
    target = "v1"
    stub = W.default_stub(target)
    W.on_nth(stub, FIRST_STEP[target], 3, lambda: world.clock.advance(LIMIT_S))
    ready(target, stub=stub)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["also"], record["ledger"]) == (
        wc.DEADLINE, [wl.ENTRY_NOT_OPEN], wl.ENTRY_NOT_OPEN)
    booked = world.entry_of(RUN_ID[target])
    assert (booked["state"], booked["kind"]) == ("unresolved", "abandoned")


# ====================================================================== A CRASH ==

def test_a_crash_before_there_is_a_guard_marks_the_run_crashed(ready, world, monkeypatch):
    """`mark_crashed()` from the outermost handler, only when there is no guard
    report to settle from. Nothing of the exception reaches the log but its class."""
    target = "pipeline"
    ready(target)

    def no_guard(*_args, **_kwargs):
        raise RuntimeError(SENTINEL)
    monkeypatch.setattr(wc, "Guard", no_guard)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ledger"], record["evidence"]) == (entry.CRASHED, "crashed", "stored")
    booked = world.entry_of(RUN_ID[target])
    assert (booked["state"], booked["kind"]) == ("unresolved", "crashed")
    assert booked["counted_usd_micros"] == RESERVED[target]["total_usd_micros"]
    assert world.published() == {"RunRefused": [1], "RunOk": [0]}
    assert "ws7 entry mode=scheduled target=pipeline stage=crashed error=RuntimeError" in world.lines
    assert not any(SENTINEL in line or "Traceback" in line for line in world.lines)
    assert world.bedrock_requests() == 0


def test_a_crash_once_there_is_a_guard_settles_from_its_report(ready, world, monkeypatch):
    target = "v1"
    ready(target)

    def broken(*_args, **_kwargs):
        raise RuntimeError(SENTINEL)
    monkeypatch.setattr(ws, "build_summary", broken)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ledger"]) == (entry.CRASHED, "settled")
    booked = world.entry_of(RUN_ID[target])
    assert (booked["state"], booked["basis"]) == ("settled", "metered")
    assert booked["counted_usd_micros"] > 0
    assert not any(SENTINEL in line for line in world.lines)


@pytest.mark.parametrize("target", TARGETS)
def test_a_runner_that_raises_is_a_failed_run_settled_at_what_was_counted(ready, world, target,
                                                                          monkeypatch):
    ready(target)

    def raising(*_args, **_kwargs):
        raise KeyError(SENTINEL)
    monkeypatch.setattr(rpe if target == "pipeline" else rce, "main", raising)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"], record["ledger"]) == (
        entry.RUNNER_CRASHED, [PRE[target]], "settled")
    assert world.entry_of(RUN_ID[target])["counted_usd_micros"] == 0
    assert not any(SENTINEL in line for line in world.lines)


# ================================================== A RUNNER THAT WILL NOT RUN ==

@pytest.mark.parametrize("target", TARGETS)
def test_a_runner_that_refuses_to_start_in_a_canary_window_is_a_failed_run(ready, world, target):
    ready(target)
    world.runner_now = lambda: CANARY
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"], record["counts"]) == (
        entry.RUNNER_REFUSED, [PRE[target]], {"exit_code": 3})
    assert world.bedrock_requests() == 0
    assert world.entry_of(RUN_ID[target])["counted_usd_micros"] == 0


def test_a_runner_that_finds_its_batch_folder_already_there_is_a_failed_run(ready, world):
    target = "pipeline"
    ready(target)
    (world.runs_dir / PRE[target]).mkdir()
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["counts"]) == (entry.RUNNER_REFUSED, {"exit_code": 2})
    assert world.bedrock_requests() == 0


@pytest.mark.parametrize("target", TARGETS)
def test_a_main_batch_that_stops_early_is_a_failed_run(ready, world, target):
    """A canary window opens part-way through the main batch: the runner stops, and a
    batch that is not the whole corpus is never scored."""
    ready(target)
    asked = []

    def now():
        asked.append(1)
        return CANARY if len(asked) > 6 else W.QUIET
    world.runner_now = now
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (entry.RUNNER_STOPPED_EARLY, [MAIN[target]])
    assert 0 < world.stub.count(FIRST_STEP[target]) < len(SCENARIOS) + 1
    assert not (world.runs_dir / MAIN[target] / "scores").exists()
    assert world.entry_of(RUN_ID[target])["state"] == "settled"


# ============================================================== THE PRE-FLIGHT ==

def _failing_generation_stub(target: str, scenario: str):
    """A stub whose reply for `scenario` is one the runner cannot use. The call itself
    succeeds, so no call fails; the generation does."""
    if target == "pipeline":
        return W.pipeline_stub_with_changes({scenario: W.broken_facts})
    stub = stubs.v1_stub()
    usual = stub.ok_response

    def ok_response(step: str, body: dict) -> dict:
        reply = usual(step, body)
        if step == "combined" and body["messages"][0]["content"][0]["text"] == W.CORPUS[scenario].notes:
            reply["output"]["message"]["content"] = []
        return reply
    stub.ok_response = ok_response
    return stub


@pytest.mark.parametrize("target", TARGETS)
def test_a_pre_flight_generation_that_fails_stops_the_run_before_the_main_batch(ready, world,
                                                                               target):
    ready(target, stub=_failing_generation_stub(target, CFG.preflight_scenario))
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (entry.PREFLIGHT_FAILED, [PRE[target]])
    assert world.bedrock_requests() == 1 and not (world.runs_dir / MAIN[target]).exists()
    # the call was answered: nothing here is a call failure
    assert world.published()["CallsNotResponse"] == [0]


@pytest.mark.parametrize("name", ["extra.json", "notes.txt", "r1/EXTRA.md"])
def test_a_pre_flight_that_wrote_a_file_that_is_not_expected_stops_the_run(ready, world, name):
    target = "v1"
    stub = W.default_stub(target)
    W.on_nth(stub, FIRST_STEP[target], 1,
             lambda: (world.runs_dir / PRE[target] / name).write_text("invented", "utf-8"))
    ready(target, stub=stub)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["counts"]) == (
        entry.PREFLIGHT_FILES_UNEXPECTED, {"unexpected": 1, "missing": 0})
    assert not (world.runs_dir / MAIN[target]).exists()
    # the failure path stores what the runner does write, and never that file
    assert (record["evidence"], record["files_refused"]) == ("stored", 1)
    allowed = entry.expected_files(target, [CFG.preflight_scenario], scored=False)
    assert set(run_keys(world, target)) == {PREFIX[target] + "guard_report.json"} | {
        PREFIX[target] + f"batches/{PRE[target]}/{known}" for known in allowed}
    assert (world.runs_dir / PRE[target] / name).is_file()      # it was there, and was left out


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("over", [0, 1])
def test_a_pre_flight_may_cost_its_reservation_and_not_one_token_more(ready, world, target, over):
    """D7: the pre-flight continues only if it cost no more than its reservation, one
    generation's worst case. Exactly that: the run goes on. One cache-read token more
    (330 nano-dollars): it stops."""
    rates = reserve.rates()
    honest = 1000 * rates["input"] + 200 * rates["output"]
    room = ONE[target] - honest * (N_STEPS[target] - 1)
    assert room % rates["cache_read"] == 0
    stub = W.default_stub(target)
    last = STEPS[target][-1]
    answered = []

    def usage_for_the_pre_flight_only():
        # The last step of the first generation reports this usage; every later one is honest.
        answered.append(1)
        stub.usage[last] = ({"inputTokens": 0, "outputTokens": 0,
                             "cacheReadInputTokens": room // rates["cache_read"] + over}
                            if len(answered) == 1 else dict(stubs.HONEST_USAGE))
    receive = stub.receive

    def scripted(name: str) -> str:
        if name == last:
            usage_for_the_pre_flight_only()
        return receive(name)
    stub.receive = scripted
    ready(target, stub=stub)
    code = world.run(target)
    report = world.s3.json(PREFIX[target] + "guard_report.json")
    assert report["batches"][PRE[target]]["total_usd_nanos"] == ONE[target] + over * rates["cache_read"]
    if over:
        assert code == 1
        record = world.failed(RUN_ID[target])
        assert record["code"] == entry.PREFLIGHT_OVER_RESERVATION
        assert record["counts"]["reserved_usd_micros"] == reserve.to_micros(ONE[target])
        assert not (world.runs_dir / MAIN[target]).exists()
    else:
        assert code == 0 and world.metric("RunOk") == [1]


# ====================================================================== SCORING ==

def _spy(monkeypatch, module, name: str) -> list:
    calls, real = [], getattr(module, name)

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)
    monkeypatch.setattr(module, name, spy)
    return calls


@pytest.mark.parametrize("broken", [True, False])
def test_a_failed_pipeline_generation_is_a_failed_run_with_its_own_code_and_no_scorer_runs(
        ready, world, monkeypatch, broken):
    """Carried item E. The assembly replay stops on any failed pipeline generation
    (evals/score_assemble.py:202), so the run is stopped from batch.json, before any
    scorer is called. No call fails, so the other 17 scenarios are still generated."""
    target = "pipeline"
    ready(target, stub=_failing_generation_stub(target, "S2") if broken else None)
    scored, replayed = _spy(monkeypatch, SB, "score_batch"), _spy(monkeypatch, SA, "replay")
    code = world.run(target)
    if not broken:
        assert code == 0 and len(scored) == 1 and len(replayed) == 1
        return
    assert code == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"], record["counts"]) == (
        entry.GENERATION_FAILED, [MAIN[target]], {"generations_failed": 1})
    assert scored == [] and replayed == []
    assert world.stub.count("extract_facts") == len(SCENARIOS) + 1      # nothing was stopped
    assert world.stub.count("compose_patient") == len(SCENARIOS)        # all but the failed one
    assert world.entry_of(RUN_ID[target])["state"] == "settled"
    assert PREFIX[target] + f"batches/{MAIN[target]}/r1/S2.json" in world.s3.keys()
    assert world.published()["RunOk"] == [0] and no_success(world, target)


def test_a_failed_v1_generation_is_counted_and_the_run_is_still_compared(ready, world):
    """The partner of item E: v1 has no assembly replay, so a generation that failed
    with no call failing is `no_output` in the summary, and the run goes on."""
    target = "v1"
    ready(target, stub=_failing_generation_stub(target, "S2"))
    assert world.run(target) == 0
    summary = world.s3.json(PREFIX[target] + "summary.json")
    assert summary["metrics"]["generations_failed"] == {"count": 1}
    assert summary["metrics"]["meds_verdict"]["verdicts"]["S2"] == "no_output"
    out = world.published()
    assert out["GenerationsFailed"] == [1] and out["RunOk"] == [1]


@pytest.mark.parametrize("ending", list(ENDINGS))
@pytest.mark.parametrize("module, name, scorer", [
    (SB, "score_batch", "score_batch"), (SA, "replay", "score_assemble"),
    (ws.H3, "check_record", "haz03_check")])
def test_a_scorer_that_stops_is_a_failed_run(ready, world, monkeypatch, module, name, scorer,
                                             ending):
    """ADR-011 D2. A scorer ends only by returning. Whatever it raised (an exception,
    an interrupt, a SystemExit with any code, 0 included), the run fails with one
    fixed code, RunOk = 1 is never sent, and nothing of the exception is logged. The
    partner is every successful run, in which each scorer returns."""
    target = "pipeline"
    ready(target)
    monkeypatch.setattr(module, name, _ends(ending))
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"], record["ledger"]) == (
        entry.SCORER_STOPPED, [scorer], "settled")
    assert not any(SENTINEL in line for line in world.lines)
    assert world.published()["RunOk"] == [0] and no_success(world, target)


def test_a_summary_that_is_refused_is_a_failed_run_with_the_refusals_code(ready, world,
                                                                         monkeypatch):
    target = "v1"
    ready(target)

    def refused(*_args, **_kwargs):
        raise store.Stop(ws.SCORES_INVALID)
    monkeypatch.setattr(ws, "build_summary", refused)
    assert world.run(target) == 1
    assert code_in(world, target) == ws.SCORES_INVALID


# ============================================= THE COMPARATOR: THE ACCEPTANCE CHECK ==

def test_acceptance_pipeline_exit_0_exit_1_and_another_pipeline_version(
        world, baseline_of, tmp_path_factory):
    """Three scheduled runs through the entrypoint, each on its own Wednesday, against
    one frozen invented baseline. Exit 0 is a success; exit 1, a breach, is a success
    too, with its message; another `pipeline_version` in the baseline's keys is "no
    baseline", a failed run."""
    target = "pipeline"
    baseline = baseline_of(target)
    changed: dict = {}
    world.ready(target, tmp_path_factory, stub=W.pipeline_stub_with_changes(changed), baseline=baseline)

    # ---- exit 0
    assert world.run(target) == 0
    first = world.s3.json(PREFIX[target] + "comparison.json")
    assert (first["status"], first["exit_code"]) == ("no_breach", 0)

    # ---- exit 1: a quality change on one scenario that was stable at baseline
    safety_net = baseline["metrics"]["safety_net_verdict"]
    stable = sorted(set(safety_net["verdicts"]) - set(safety_net["unstable"]))
    assert stable, "the stub baseline has no scenario that passed safety-netting three times"
    chosen = stable[0]
    changed[chosen] = W.advice_the_notes_do_not_document(chosen)
    world.clock.advance(7 * 86400)
    assert world.run(target, "2026-12-23T04:00:00Z") == 0       # a breach is a successful run
    run = "2026-12-23T0400-pipeline"
    prefix = f"ws7/runs/scheduled/{run}/"
    result = world.s3.json(prefix + "comparison.json")
    assert (result["status"], result["exit_code"]) == ("breach", 1)
    row = next(r for r in result["comparisons"] if (r["family"], r["kind"]) == ("safety_netting", 2))
    assert (row["breach"], row["metrics"][0]["flips"]) == (1, [chosen])
    success = world.s3.json(store.success_key(run))
    assert (success["status"], success["breaches"]) == ("breach", 1)
    out = world.published()
    assert out["BreachSafetyNettingKind2"] == [0, 1] and out["RunOk"] == [1, 1]
    assert out["BreachMedicationsKind1"] == [0, 0] and out["NoBaseline"] == [0, 0]
    keys = world.s3.json(prefix + "summary.json")["keys"]
    version = keys["pipeline_version"]
    assert world.sns.messages == [{
        "topic": W.TOPIC, "subject": f"WS7 breach - target pipeline - run {run}",
        "message": "\n".join([
            f"WS7 breach - target pipeline - run {run}",
            f"family safety-netting, kind 2 (per-scenario flip): safety_net_verdict: "
            f"1 stable scenario now fails ({chosen})",
            f"system release: pipeline_version {version[:4]}...{version[-2:]} / "
            f"model {keys['model_id']} / caching on",
            f"baseline: {W.BASELINE_ID}  evidence: s3://{BUCKET}/{prefix}",
            f"report time (the alarm): {publish.london(world.clock.wall())}"])}]
    # the message is sent after the metrics and before the success record
    assert world.log.index("cloudwatch", "PutMetricData NoBaseline", last=True) \
        < world.log.index("sns", "Publish") < world.log.index("s3", f"PUT {store.success_key(run)}")
    changed.clear()

    # ---- exit 2: the frozen baseline is for another pipeline_version
    other = dict(baseline, keys=dict(baseline["keys"], pipeline_version="9" * 64))
    world.freeze(target, other)
    world.clock.advance(7 * 86400)
    assert world.run(target, "2026-12-30T04:00:00Z") == 1
    run = "2026-12-30T0400-pipeline"
    record = world.failed(run)
    assert (record["code"], record["ids"], record["counts"]) == (
        entry.NO_BASELINE, ["pipeline_version"], {"keys": 1})
    result = world.s3.json(f"ws7/runs/scheduled/{run}/comparison.json")
    assert (result["status"], result["exit_code"], result["comparisons"]) == ("no_baseline", 2, [])
    out = world.published()
    assert out["NoBaseline"] == [0, 0, 1] and out["RunOk"] == [1, 1, 0] and out["RunRefused"] == [1]
    assert out["BreachSafetyNettingKind2"] == [0, 1]            # no breach value without a comparison
    assert store.success_key(run) not in world.s3.keys()
    assert len(world.sns.messages) == 1                         # a failed run sends none of its own
    # the run still stored its records, and its manifest says it was compared with nothing
    assert world.s3.json(f"ws7/runs/scheduled/{run}/manifest.json")["baseline_id"] is None
    assert out["CostUsdMicros"][2] > 0                          # the Run counts of a run that has a summary


def test_acceptance_v1_exit_0_exit_1_and_another_prompt_hash(world, baseline_of, tmp_path_factory):
    """Carried item D: v1 is the first target to go live. With the stub's v1 text no
    scenario passes safety-netting, so the breach is a count: a stub reply with an
    invented dose takes `meds_invented_doses` past the baseline's highest count."""
    target = "v1"
    baseline = baseline_of(target)
    assert baseline["metrics"]["meds_invented_doses"] == {"counts": [0, 0, 0]}
    changed: dict = {}
    world.ready(target, tmp_path_factory, stub=W.v1_stub_with_text(changed), baseline=baseline)

    # ---- exit 0
    assert world.run(target) == 0
    assert world.s3.json(PREFIX[target] + "comparison.json")["status"] == "no_breach"

    # ---- exit 1: the scenario is the first whose gold lets the scorer count an invented dose
    texts = {sid: W.v1_text_with_an_invented_dose(sid) for sid in SCENARIOS}
    chosen = next((sid for sid in SCENARIOS if texts[sid]), None)
    assert chosen, "no scenario's gold has a drug with no documented dose"
    changed[chosen] = texts[chosen]
    world.clock.advance(7 * 86400)
    assert world.run(target, "2026-12-23T04:00:00Z") == 0
    run = "2026-12-23T0400-v1"
    prefix = f"ws7/runs/scheduled/{run}/"
    result = world.s3.json(prefix + "comparison.json")
    assert (result["status"], result["exit_code"]) == ("breach", 1)
    row = next(r for r in result["comparisons"] if (r["family"], r["kind"]) == ("medications", 1))
    doses = row["metrics"][0]["run"]
    assert row["breach"] == 1 and doses > 0 and row["metrics"][0]["baseline_max"] == 0
    summary = world.s3.json(prefix + "summary.json")
    assert summary["metrics"]["calls_not_response"] == {"count": 0}     # no call failed
    out = world.published()
    assert out["BreachMedicationsKind1"] == [0, 1] and out["RunOk"] == [1, 1]
    prompts = summary["keys"]["v1_prompt_sha256"]
    system, patient = prompts["system_prompt"], prompts["patient_system_prompt"]
    assert [m["message"] for m in world.sns.messages] == ["\n".join([
        f"WS7 breach - target v1 - run {run}",
        f"family medications, kind 1 (zero-tolerance counter): meds_invented_doses {doses}, "
        f"baseline max 0",
        f"system release: v1 prompts {system[:4]}...{system[-2:]} / {patient[:4]}...{patient[-2:]} / "
        f"model {summary['keys']['model_id']} / caching on",
        f"baseline: {W.BASELINE_ID}  evidence: s3://{BUCKET}/{prefix}",
        f"report time (the alarm): {publish.london(world.clock.wall())}"])]
    changed.clear()

    # ---- exit 2: the frozen baseline is for another system prompt
    other = dict(baseline, keys=dict(baseline["keys"], v1_prompt_sha256=dict(
        baseline["keys"]["v1_prompt_sha256"], system_prompt="9" * 64)))
    world.freeze(target, other)
    world.clock.advance(7 * 86400)
    assert world.run(target, "2026-12-30T04:00:00Z") == 1
    record = world.failed("2026-12-30T0400-v1")
    assert (record["code"], record["ids"]) == (entry.NO_BASELINE, ["v1_prompt_sha256"])
    out = world.published()
    assert out["NoBaseline"] == [0, 0, 1] and out["RunOk"] == [1, 1, 0]
    assert len(world.sns.messages) == 1


# ================================================ THE COMPARATOR: THE EXIT CODE ==

def _result_path(world, target: str) -> Path:
    return world.runs_dir / f"{RUN_ID[target]}.work" / f"comparison.{MAIN[target]}.json"


def _reads(monkeypatch) -> list:
    """Every path whose bytes are read through `Path.read_bytes`."""
    read, real = [], Path.read_bytes

    def read_bytes(self):
        read.append(Path(self))
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    return read


@pytest.mark.parametrize("missing", ["baseline", "rules", "both"])
def test_a_missing_baseline_or_rules_file_is_no_baseline_and_the_comparator_is_not_called(
        ready, world, monkeypatch, missing):
    """Neither file exists until slice 4. A missing file is "no baseline", never a pass."""
    target = "v1"
    ready(target)
    for name in {"baseline": [entry.BASELINE_NAME], "rules": [entry.RULES_NAME],
                 "both": [entry.BASELINE_NAME, entry.RULES_NAME]}[missing]:
        (world.frozen / name.format(target=target)).unlink()
    called = _spy(monkeypatch, compare, "main")
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (entry.NO_BASELINE, ["file_missing"])
    assert called == [] and not _result_path(world, target).exists()
    out = world.published()
    assert (out["NoBaseline"], out["RunOk"], out["RunRefused"]) == ([1], [0], [1])
    assert no_success(world, target) and not any(n.startswith("Breach") for n in out)


def _comparator(monkeypatch, act) -> None:
    """Replace `ws7_compare.main` with the real comparator followed by `act(out)`,
    which may rewrite the result file and decides what `main` returns."""
    real = compare.main

    def main(argv):
        code = real(argv)
        return act(Path(argv[argv.index("--out") + 1]), code)
    monkeypatch.setattr(compare, "main", main)


@pytest.mark.parametrize("returned", [7, -1, None, True, "0", 0.0], ids=repr)
def test_an_exit_code_that_is_not_0_1_or_2_fails_the_run_and_the_result_is_never_read(
        ready, world, monkeypatch, returned):
    """The exit code is judged BEFORE the result is read. Here the comparator leaves a
    perfect "no breach" result and returns something that is not an exit code: the
    result file is never opened. The control: on a good run it is."""
    target = "v1"
    ready(target)
    _comparator(monkeypatch, lambda _out, _code: returned)
    read = _reads(monkeypatch)
    assert world.run(target) == 1
    result = _result_path(world, target)
    assert result.is_file() and result not in read
    assert json.loads(result.read_text("utf-8"))["status"] == "no_breach"
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (entry.COMPARE_ERROR, ["exit_code_unknown"])
    assert no_success(world, target) and world.published()["RunOk"] == [0]


def test_on_a_good_run_the_result_is_read_after_the_exit_code(ready, world, monkeypatch):
    target = "v1"
    ready(target)
    read = _reads(monkeypatch)
    assert world.run(target) == 0
    assert _result_path(world, target) in read


@pytest.mark.parametrize("ending", list(ENDINGS))
def test_a_comparator_that_raises_is_a_failed_run(ready, world, monkeypatch, ending):
    """The comparator ends only by returning. It has written a perfect "no breach"
    result and then raises: whatever it raised, a SystemExit(0) included, the run
    fails and the result is never read."""
    target = "v1"
    ready(target)
    _comparator(monkeypatch, lambda _out, _code: _ends(ending)())
    read = _reads(monkeypatch)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (entry.COMPARE_ERROR, ["comparator_raised"])
    assert _result_path(world, target).is_file() and _result_path(world, target) not in read
    assert not any(SENTINEL in line for line in world.lines)
    assert world.published()["RunOk"] == [0] and no_success(world, target)


def _rewrite(out: Path, **changes) -> None:
    result = json.loads(out.read_text("utf-8"))
    result.update(changes)
    out.write_bytes(compare.render(result))


@pytest.mark.parametrize("returned, written", [
    (2, {}),                                            # exit 2 beside a result that says no breach
    (1, {}),                                            # exit 1 beside a result that says no breach
    (0, {"status": "breach", "exit_code": 1}),          # exit 0 beside a result that says breach
    (0, {"status": "breach"}),                          # a result that disagrees with itself
    (0, {"run_id": "2026-12-09T0400-v1.main"}),         # another run's result
    (0, {"inputs": {"baseline_sha256": "0" * 64, "rules_sha256": "0" * 64,
                    "run_sha256": "0" * 64}}),          # a result of another summary's bytes
    (0, {"schema": "ws7-result/2"}),
    (0, {"comparisons": []}),                           # a pass that compared nothing
    (2, {"status": "no_baseline", "exit_code": 2}),     # "no baseline" that names no key
    (0, None),                                          # no result file at all
], ids=str)
def test_a_result_that_disagrees_with_the_exit_code_is_never_a_pass(ready, world, monkeypatch,
                                                                   returned, written):
    """The exit code is the authority. Each case starts from the real comparator's
    "no breach" result for this run, so the only thing wrong is what is listed."""
    target = "v1"
    ready(target)

    def act(out: Path, code: int):
        assert code == 0
        if written is None:
            out.unlink()
        elif written:
            _rewrite(out, **written)
        return returned
    _comparator(monkeypatch, act)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert record["code"] == entry.COMPARE_ERROR
    out = world.published()
    assert out["RunOk"] == [0] and "NoBaseline" not in out
    assert no_success(world, target) and not any(n.startswith("Breach") for n in out)


def test_a_comparator_error_is_a_failed_run_with_its_own_code(ready, world):
    """Exit 2 with status `error`: a rules file the comparator refuses."""
    target = "v1"
    ready(target)
    world.freeze(target, W.invented_baseline(target, world.net, None),
                 dict(W.invented_rules(target), surprise=1))
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"], record["counts"]) == (
        entry.COMPARE_ERROR, ["comparator_error"], {"errors": 1})
    assert "NoBaseline" not in world.published()
    assert world.s3.json(PREFIX[target] + "comparison.json")["status"] == "error"


@pytest.mark.parametrize("target", TARGETS)
def test_a_file_already_at_the_comparators_output_path_fails_the_run_and_is_never_read(
        ready, world, monkeypatch, target):
    """The author's change D. The output path is new for each call and must not exist
    before it. Here a perfect "no breach" result for this very run is already there."""
    ready(target)
    assert world.run(target) == 0                               # a good run, to copy its result
    good = _result_path(world, target).read_bytes()
    assert json.loads(good)["status"] == "no_breach"

    world.clock.advance(7 * 86400)
    run = f"2026-12-23T0400-{target}"
    stale = world.runs_dir / f"{run}.work" / f"comparison.{run}.main.json"
    stale.parent.mkdir()
    stale.write_bytes(good.replace(RUN_ID[target].encode(), run.encode()))
    called = _spy(monkeypatch, compare, "main")
    read = _reads(monkeypatch)
    assert world.run(target, "2026-12-23T04:00:00Z") == 1
    assert world.failed(run)["code"] == entry.COMPARE_OUTPUT_EXISTS
    assert called == [] and stale not in read
    assert store.success_key(run) not in world.s3.keys()
    assert world.published()["RunOk"] == [1, 0]


def test_a_rule_for_part_c_fails_the_run_and_publishes_no_breach_value(ready, world):
    """PART C is not published (D13). A rules file that puts a metric in that family
    gives a comparison row the job will not publish: the run fails."""
    target = "v1"
    ready(target)
    rules = W.invented_rules(target)
    rules["metrics"].append({"metric": "tokens_in_total", "family": "part_c", "kind": 1})
    world.freeze(target, W.invented_baseline(target, world.net, None), rules)
    assert world.run(target) == 1
    assert code_in(world, target) == publish.PART_C_RULED
    out = world.published()
    assert out["RunOk"] == [0] and not any(n.startswith("Breach") for n in out)
    assert no_success(world, target)


# ====================================================================== STORING ==

@pytest.mark.parametrize("name", ["extra.json", "notes.txt", "scores/EXTRA.md", "link.json"])
def test_a_file_that_is_not_on_the_list_stops_the_run_and_is_never_uploaded(ready, world, name):
    """The author's ruling 7, unchanged on the success path: a file the runners and
    scorers do not write stops the run. The extra file appears in the main batch
    folder as the ledger is settled. The run fails; its failure path then stores the
    guard's report and every file that IS on the list, never the extra one, and
    counts it (Codex P2, PR #9)."""
    target = "pipeline"
    ready(target)
    main = world.runs_dir / MAIN[target]

    def add():
        path = main / name
        path.parent.mkdir(exist_ok=True)
        if name.startswith("link"):
            path.symlink_to(main / "batch.json")
        else:
            path.write_text("invented", "utf-8")
    world.s3.after_put(LEDGER_KEY, lambda: None)
    world.s3.after_put(LEDGER_KEY, add)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"], record["counts"]) == (
        entry.EVIDENCE_UNSTORABLE, [MAIN[target]], {"unexpected": 1})
    assert (record["evidence"], record["files_refused"]) == ("stored", 1)
    stored = set(run_keys(world, target))
    assert PREFIX[target] + f"batches/{MAIN[target]}/{name}" not in stored
    assert stored == {PREFIX[target] + known for known in (
        "guard_report.json", "summary.json", "comparison.json", "manifest.json")} | {
        PREFIX[target] + f"batches/{PRE[target]}/{known}"
        for known in entry.expected_files(target, [CFG.preflight_scenario], scored=False)} | {
        PREFIX[target] + f"batches/{MAIN[target]}/{known}"
        for known in entry.expected_files(target, SCENARIOS, scored=True)}
    assert no_success(world, target) and world.published()["RunOk"] == [0]


def test_a_record_s3_will_not_take_is_a_failed_run(ready, world):
    target = "v1"
    ready(target)
    world.s3.script_put(PREFIX[target] + "summary.json", fakes.DENY_403)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (entry.EVIDENCE_UNAVAILABLE, [store.PUT_REFUSED])
    assert no_success(world, target) and world.published()["RunOk"] == [0]


def test_a_success_record_that_cannot_be_written_is_a_failed_run_and_runok_is_never_1(ready, world):
    target = "v1"
    ready(target)
    world.s3.script_put(store.success_key(RUN_ID[target]), fakes.DENY_403)
    assert world.run(target) == 1
    assert code_in(world, target) == entry.SUCCESS_RECORD_UNAVAILABLE
    assert no_success(world, target) and world.metric("RunOk") == [0]


# =================================================================== PUBLISHING ==

def test_metrics_that_cannot_be_published_fail_the_run_before_its_success_record(ready, world):
    """A breach value that was not published must not be followed by RunOk = 1."""
    target = "v1"
    ready(target)
    world.cw.script(500)
    assert world.run(target) == 1
    assert code_in(world, target) == publish.PUBLISH_FAILED
    assert no_success(world, target)
    assert world.metric("RunOk") == [0]             # published by the failure path's own call
    # each tried once: the run's metrics (refused), the failure's markers, and the run's
    # counters, which CloudWatch had not acknowledged
    assert len(events(world, "cloudwatch")) == 3
    out = world.published()
    assert out["CostUsdMicros"][0] > 0 and all(len(values) == 1 for values in out.values())
    assert "NoBaseline" not in out and not any(n.startswith("Breach") for n in out)


@pytest.mark.parametrize("acknowledged", [False, True])
def test_if_runok_1_is_not_acknowledged_the_run_fails_with_its_own_code_and_a_failed_record(
        ready, world, acknowledged):
    """Codex P2, PR #9, accepted by the author. CloudWatch rejects `RunOk` = 1 after
    the success record was written. The run goes through the claimed failure handler
    with `ws7_runok_publish_failed`: the failed record is written beside the success
    record, so the Thursday check reports a failed run (change B), and `RunOk` = 0
    is tried. The partner: `RunOk` = 1 acknowledged, and no failed record."""
    target = "v1"
    ready(target)
    if not acknowledged:
        world.cw.script(None, 500)                  # the metrics are taken; RunOk = 1 is not
    code = world.run(target)
    run = RUN_ID[target]
    assert not no_success(world, target)            # the success record stands either way
    if acknowledged:
        assert code == 0 and world.metric("RunOk") == [1]
        assert store.failed_key(run) not in world.s3.keys()
        assert "RunRefused" not in world.published()
        return
    assert code == 1
    record = world.failed(run)
    assert (record["code"], record["ids"], record["ledger"], record["evidence"]) == (
        entry.RUNOK_PUBLISH_FAILED, [publish.PUBLISH_FAILED], "settled", "stored")
    out = world.published()
    assert (out["RunOk"], out["RunRefused"]) == ([0], [1])
    # the run's metrics, RunOk = 1 (rejected), then the failure's markers: nothing else,
    # and no counter a second time
    assert [what.split(" ")[1].split(",")[0] for what in events(world, "cloudwatch")] == [
        "NoBaseline", "RunOk", "RunRefused"]
    assert all(len(values) == 1 for values in out.values())
    # the failed record was written after the success record, and before RunOk = 0
    log = world.log
    assert log.index("s3", f"PUT {store.success_key(run)}") \
        < log.index("s3", f"PUT {store.failed_key(run)}") \
        < log.index("cloudwatch", "PutMetricData RunRefused")
    world.clock.start = THURSDAY
    assert world.check(target) == 0
    assert check_lines(world)[1] == (f"failed run {run} code {entry.RUNOK_PUBLISH_FAILED} "
                                     f"ids {publish.PUBLISH_FAILED}")


def test_a_breach_message_that_cannot_be_sent_fails_the_run(ready, world, baseline_of):
    target = "v1"
    text = next(t for t in (W.v1_text_with_an_invented_dose(s) for s in SCENARIOS) if t)
    scenario = next(s for s in SCENARIOS if W.v1_text_with_an_invented_dose(s) == text)
    ready(target, stub=W.v1_stub_with_text({scenario: text}))
    world.sns.script(500)
    assert world.run(target) == 1
    assert code_in(world, target) == publish.ALERT_FAILED
    assert no_success(world, target) and world.metric("RunOk") == [0]
    assert world.metric("BreachMedicationsKind1") == [1]        # the alarm's own path still fired


@pytest.mark.parametrize("target", TARGETS)
def test_no_text_reaches_a_metric_a_message_or_the_log(ready, world, baseline_of, target, capsys):
    """A breach run whose stub replies all carry a sentinel. It is in the stored
    record (the control), and in no CloudWatch request, no SNS request and no line the
    job printed."""
    if target == "v1":
        scenario = next(s for s in SCENARIOS if W.v1_text_with_an_invented_dose(s))
        stub = W.v1_stub_with_text({scenario: W.v1_text_with_an_invented_dose(scenario)})
    else:
        safety_net = baseline_of(target)["metrics"]["safety_net_verdict"]
        scenario = sorted(set(safety_net["verdicts"]) - set(safety_net["unstable"]))[0]
        stub = W.pipeline_stub_with_changes({scenario: W.advice_the_notes_do_not_document(
            scenario, f"Invented advice. {SENTINEL}")})
    ready(target, stub=stub)
    assert world.run(target) == 0
    assert len(world.sns.messages) == 1 and world.cw.calls
    mark = SENTINEL.encode()
    assert mark in world.s3.body(PREFIX[target] + f"batches/{MAIN[target]}/r1/{scenario}.json")
    assert not any(mark in body for body in world.cw.bodies)
    assert not any(mark in body for body in world.sns.bodies)
    assert not any(SENTINEL in line for line in world.lines)
    printed = capsys.readouterr()
    assert SENTINEL not in printed.out and SENTINEL not in printed.err
    # and nothing the runners or the scorers print reached the terminal at all
    assert "Batch:" not in printed.out and "== run r1 ==" not in printed.out
    for name in ("summary.json", "comparison.json", "manifest.json", "guard_report.json"):
        assert mark not in world.s3.body(PREFIX[target] + name), name
    assert mark not in world.s3.body(store.success_key(RUN_ID[target]))


# ======================================== THE FAILURE PATH NEVER FAILS OPEN ==

def _raising(*_args, **_kwargs):
    raise RuntimeError(SENTINEL)


def _fail_a_main_batch_call(target: str):
    """A stub that throttles the main batch's second scenario: a run that fails with
    a guard and no summary, so every step of the failure path has something to do."""
    stub = W.default_stub(target)
    W.fail_nth(stub, FIRST_STEP[target], 3, THROTTLE)
    return stub


def _failure_steps(world, target: str) -> list:
    """The steps of the failure path that reached a server, in the order they did:
    the settling write, the first stored record, the failed record, the failure's
    markers and the run's counters."""
    run = RUN_ID[target]
    marks = (("settle", "s3", f"PUT {LEDGER_KEY}", True),       # the last ledger write
             ("store", "s3", f"PUT {PREFIX[target]}", False),
             ("failed_record", "s3", f"PUT {store.failed_key(run)}", False),
             ("markers", "cloudwatch", "PutMetricData RunRefused", False),
             ("counters", "cloudwatch", "PutMetricData SpendGuardTrips", False))
    seen = []
    for step, server, what, last in marks:
        hits = [i for i, (srv, text) in enumerate(world.log.all())
                if srv == server and text.startswith(what)]
        if hits:
            seen.append((hits[-1] if last else hits[0], step))
    return [step for _at, step in sorted(seen)]


ALL_STEPS = ["settle", "store", "failed_record", "markers", "counters"]


@pytest.mark.parametrize("broken", ["nothing", "log", "settle", "store", "clock", "build",
                                    "s3_write", "markers", "counters"])
def test_every_step_of_the_failure_path_is_tried_whatever_became_of_the_one_before(
        ready, world, monkeypatch, broken):
    """Codex P2, PR #9: the failure path must not fail open. One thing is broken in
    each case: the log stream, the settling write, the first stored record, the clock
    (once the ledger is settled), the building of the failed record, the failed
    record's write, and each of the two publishes. In every case each LATER step is
    still tried, and the ledger write comes first. The partner breaks nothing."""
    target = "v1"
    run = RUN_ID[target]
    ready(target, stub=_fail_a_main_batch_call(target))
    if broken == "log":
        world.out = _raising
    elif broken == "settle":
        world.s3.after_put(LEDGER_KEY, lambda: world.s3.script_put(LEDGER_KEY, fakes.DENY_403))
    elif broken == "store":
        world.s3.script_put(PREFIX[target] + "guard_report.json", fakes.DENY_403)
    elif broken == "clock":
        def stop_the_clock():
            world.clock.fail = RuntimeError(SENTINEL)
        world.s3.after_put(LEDGER_KEY, lambda: None)            # the booking
        world.s3.after_put(LEDGER_KEY, stop_the_clock)          # the settle has landed
    elif broken == "build":
        monkeypatch.setattr(entry._Scheduled, "_failed_record", _raising)
    elif broken == "s3_write":
        world.s3.script_put(store.failed_key(run), fakes.DENY_403)
    elif broken == "markers":
        world.cw.script(500)
    elif broken == "counters":
        world.cw.script(None, 500)

    assert world.run(target) == 1
    world.clock.fail = None
    # ---- every step reached its server, in order, the ledger first
    never_sent = {"clock": "failed_record", "build": "failed_record"}.get(broken)
    assert _failure_steps(world, target) == [s for s in ALL_STEPS if s != never_sent]
    # ---- and what landed is everything but the one thing that was broken
    booked = world.entry_of(run)
    if broken == "settle":
        assert booked["state"] == "open"                        # it expires, and is then abandoned
    else:
        assert (booked["state"], booked["basis"]) == ("settled", "metered")
    assert (PREFIX[target] + "guard_report.json" in world.s3.keys()) == (broken != "store")
    has_record = store.failed_key(run) in world.s3.keys()
    assert has_record == (broken not in ("clock", "build", "s3_write"))
    out = world.published()
    assert ("RunOk" in out, "RunRefused" in out) == ((broken != "markers"),) * 2
    assert ("CallsNotResponse" in out) == (broken != "counters")
    assert all(len(values) == 1 for values in out.values())
    if has_record:
        record = world.failed(run)
        assert record["code"] == entry.CALL_FAILED
        assert record["evidence"] == ("stored" if broken != "store" else entry.EVIDENCE_UNAVAILABLE)
        assert (record["ledger"] == "settled") == (broken != "settle")
        assert record["also"] == ([] if broken != "settle" else [record["ledger"]])
    # ---- the log says which step was lost, and nothing of the exception but its class
    assert not any(SENTINEL in line or "Traceback" in line for line in world.lines)
    if broken in ("clock", "build"):
        assert ("ws7 entry mode=scheduled target=v1 stage=crashed error=RuntimeError "
                "during=failed_record") in world.lines
    if broken == "log":
        assert world.lines == []
    assert no_success(world, target) and world.sns.messages == []


RAISED = {"Exception": lambda: RuntimeError(SENTINEL), "SystemExit(0)": lambda: SystemExit(0),
          "SystemExit(1)": lambda: SystemExit(1), "KeyboardInterrupt": KeyboardInterrupt}
RAISING_STEPS = ["log", "settle", "store", "clock", "build", "write", "markers", "counters"]


def _raise_inside(world, monkeypatch, target: str, step: str, make) -> None:
    """Make one thing the failure path uses raise `make()`, in this process: the log
    sink, the settle, the first store call, the clock, the building of the failed
    record, its write, or one of the two publish calls. Everything else is real."""
    run = RUN_ID[target]
    if step == "log":
        def sink(_line):
            raise make()
        world.out = sink
    elif step == "settle":
        def settle(*_args, **_kwargs):
            raise make()
        monkeypatch.setattr(wl.Ledger, "settle", settle)
    elif step in ("store", "write"):
        real = store.Store.create
        broken = PREFIX[target] if step == "store" else store.failed_key(run)

        def create(self, key, *args, **kwargs):
            if key.startswith(broken):
                raise make()
            return real(self, key, *args, **kwargs)
        monkeypatch.setattr(store.Store, "create", create)
    elif step == "clock":
        def stop_the_clock():
            world.clock.fail = make()
        world.s3.after_put(LEDGER_KEY, lambda: None)            # the booking
        world.s3.after_put(LEDGER_KEY, stop_the_clock)          # the settle has landed
    elif step == "build":
        def build(*_args, **_kwargs):
            raise make()
        monkeypatch.setattr(entry._Scheduled, "_failed_record", build)
    else:
        real_put = publish.Publisher.put
        name = "RunRefused" if step == "markers" else "SpendGuardTrips"

        def put(self, values):
            if any(item[0] == name for item in values):
                raise make()
            return real_put(self, values)
        monkeypatch.setattr(publish.Publisher, "put", put)


@pytest.mark.parametrize("raised", list(RAISED))
@pytest.mark.parametrize("step", RAISING_STEPS)
def test_nothing_raised_inside_the_failure_path_ends_it_early_whatever_its_type(
        ready, world, monkeypatch, step, raised):
    """THE CLASS (Codex P2, PR #9, re-check): `SystemExit` and `KeyboardInterrupt`
    derive from `BaseException`, not `Exception`. Nothing raised by a log sink, the
    clock, a store call or a publish call may end the failure path early, whatever
    its type.

    The run fails at the pre-flight (a throttled call), so all five steps are still
    to do. Each step in turn raises an `Exception`, `SystemExit(0)`, `SystemExit(1)`
    and `KeyboardInterrupt`. Every LATER step still reaches its server, the ledger
    is settled first, and the job returns 1: nothing gets out of it."""
    target = "v1"
    run = RUN_ID[target]
    stub = W.default_stub(target)
    W.fail_nth(stub, FIRST_STEP[target], 1, THROTTLE)
    ready(target, stub=stub)
    _raise_inside(world, monkeypatch, target, step, RAISED[raised])

    assert world.run(target) == 1                   # `World.call` fails the test if anything escapes
    world.clock.fail = None
    # ---- the steps that reached a server: all but the one that raised in this process.
    # (When the settle raises, the last ledger write the servers saw is the booking,
    # which still comes before every other step; the entry's state is checked below.)
    not_sent = {"log": None, "settle": None, "clock": "failed_record", "build": "failed_record",
                "write": "failed_record"}.get(step, step)
    assert _failure_steps(world, target) == [s for s in ALL_STEPS if s != not_sent]
    # ---- the ledger first: settled before anything else of the failure path
    booked = world.entry_of(run)
    if step == "settle":
        assert booked["state"] == "open"            # the settle itself raised; the rest still ran
    else:
        assert (booked["state"], booked["basis"]) == ("settled", "metered")
    # ---- what landed is everything but the one thing that raised
    assert (PREFIX[target] + "guard_report.json" in world.s3.keys()) == (step != "store")
    has_record = store.failed_key(run) in world.s3.keys()
    assert has_record == (step not in ("clock", "build", "write"))
    out = world.published()
    assert ("RunOk" in out, "RunRefused" in out) == ((step != "markers"),) * 2
    assert ("CallsNotResponse" in out) == (step != "counters")
    assert out.get("RunOk", [0]) == [0] and all(len(values) == 1 for values in out.values())
    if has_record:
        record = world.failed(run)
        assert record["code"] == entry.CALL_FAILED
        assert record["evidence"] == ("stored" if step != "store" else entry.CRASHED)
        assert record["also"] == ([] if step != "settle" else [entry.CRASHED])
    assert not any(SENTINEL in line or "Traceback" in line for line in world.lines)
    assert no_success(world, target) and world.sns.messages == []


@pytest.mark.parametrize("raised", list(RAISED))
def test_a_log_sink_cannot_end_anything_whatever_it_raises(ready, world, raised):
    """`best_effort` catches `BaseException`. With a sink that raises on every line: a
    good run ends with RunOk = 1, a second delivery is a duplicate, and the check
    still sends its report. The positive control: the sink itself does raise."""
    target = "v1"
    make = RAISED[raised]

    def sink(_line):
        raise make()
    with pytest.raises(BaseException) as control:
        sink("a line")
    assert type(control.value) is type(make())
    assert entry.best_effort(sink)("a line") is None

    ready(target)
    world.out = sink
    assert world.run(target) == 0
    assert world.metric("RunOk") == [1] and not no_success(world, target)
    assert world.run(target) == 0
    assert world.metric("Duplicate") == [1]
    world.s3.remove(store.success_key(RUN_ID[target]))
    world.clock.start = THURSDAY
    assert world.check(target) == 0
    assert check_lines(world)[1] == f"run {RUN_ID[target]} started and did not finish: no success record"


@pytest.mark.parametrize("raised", list(RAISED))
def test_a_check_that_failed_still_exits_1_whatever_its_own_failure_report_raises(
        thursday, monkeypatch, raised):
    """The check's last act, when it has itself failed, is to say so on the topic.
    Whatever that call raises, the check returns 1."""
    world, target = thursday, "v1"
    world.s3.script_list(500)

    def alert(*_args, **_kwargs):
        raise RAISED[raised]()
    monkeypatch.setattr(publish.Publisher, "alert", alert)
    assert world.check(target) == 1
    assert f"ws7 entry mode=check target={target} stage=crashed error=" \
           f"{type(RAISED[raised]()).__name__} during=alert" in world.lines
    assert not any(SENTINEL in line for line in world.lines)


def test_a_log_stream_that_fails_changes_nothing_a_run_a_duplicate_or_a_check_does(
        ready, world, capsys):
    """Logging is best effort everywhere. With a log stream that raises on every
    line: a good run still ends with RunOk = 1, a second delivery is still counted as
    a duplicate, and the check still sends its report."""
    target = "v1"
    ready(target)
    world.out = _raising
    assert world.run(target) == 0
    assert world.metric("RunOk") == [1] and not no_success(world, target)
    assert world.run(target) == 0
    assert world.metric("Duplicate") == [1] and world.metric("RunOk") == [1]
    world.s3.remove(store.success_key(RUN_ID[target]))
    world.clock.start = THURSDAY
    assert world.check(target) == 0
    assert check_lines(world)[1] == f"run {RUN_ID[target]} started and did not finish: no success record"
    assert world.lines == []
    printed = capsys.readouterr()
    assert (printed.out, printed.err) == ("", "")


def test_best_effort_makes_a_stream_unable_to_raise_and_the_stream_itself_does_raise():
    """The positive control: the stream the tests above break really raises."""
    with pytest.raises(RuntimeError):
        _raising("a line")
    assert entry.best_effort(_raising)("a line") is None
    kept: list = []
    entry.best_effort(kept.append)("a line")
    assert kept == ["a line"]


@pytest.mark.parametrize("ending", list(ENDINGS))
def test_anything_raised_inside_a_run_is_a_crashed_run_that_still_settles(ready, world,
                                                                         monkeypatch, ending):
    """The outermost handler of a run is for `BaseException`. Whatever is raised inside
    the run, the failure path runs: nothing leaves the process without a settle and a
    failed record, and RunOk = 1 is never sent."""
    target = "v1"
    ready(target)
    monkeypatch.setattr(ws, "build_summary", _ends(ending))
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ledger"]) == (entry.CRASHED, "settled")
    assert world.published()["RunOk"] == [0] and no_success(world, target)
    assert f"ws7 entry mode=scheduled target=v1 stage=crashed error=" \
           f"{type(ENDINGS[ending]()).__name__}" in world.lines
    assert not any(SENTINEL in line for line in world.lines)


# ============================== CODE THE JOB DOES NOT OWN ENDS ONLY BY RETURNING ==

def _end_runner_after(monkeypatch, target: str, nth: int, ending: str) -> list:
    """Let the real runner run, write its index and return; then, for the `nth` call
    (1: the pre-flight, 2: the main batch), end as `ending` says instead of returning.
    Returns the exit codes the real runner gave."""
    runner = rpe if target == "pipeline" else rce
    real, codes = runner.main, []

    def main(*args, **kwargs):
        codes.append(real(*args, **kwargs))
        if len(codes) == nth:
            raise ENDINGS[ending]()
        return codes[-1]
    monkeypatch.setattr(runner, "main", main)
    return codes


@pytest.mark.parametrize("ending", list(ENDINGS))
@pytest.mark.parametrize("nth, kind", [(1, PRE), (2, MAIN)])
@pytest.mark.parametrize("target", TARGETS)
def test_a_runner_that_raises_after_writing_a_clean_index_is_a_crashed_runner_never_a_return(
        ready, world, monkeypatch, target, nth, kind, ending):
    """THE CLASS (Codex P2, PR #9, re-check): code the job does not own ends only by
    returning. A `SystemExit` raised by a runner, with any code (0, 1, 2, None or a
    string), is a crash of that runner, never a normal return.

    Here the real runner has run and written an index that explains its exit code, in
    the pre-flight or in the main batch, and then raises. Before the fix a
    `SystemExit(0)` was read as exit code 0 and the run went on to RunOk = 1. Now:
    `ws7_runner_crashed`, RunOk = 1 is never sent, the ledger is settled and the
    failed record is written. The partner is every successful run: the same runner,
    returning."""
    ready(target)
    codes = _end_runner_after(monkeypatch, target, nth, ending)
    assert world.run(target) == 1
    assert len(codes) == nth                                    # the real runner did run
    index = json.loads((world.runs_dir / kind[target] / "batch.json").read_text("utf-8"))
    assert entry.index_counts(target, index)[0] == 0            # and its index is clean
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"], record["ledger"]) == (
        entry.RUNNER_CRASHED, [kind[target]], "settled")
    booked = world.entry_of(RUN_ID[target])
    assert (booked["state"], booked["basis"]) == ("settled", "metered")
    out = world.published()
    assert out["RunOk"] == [0] and out["RunRefused"] == [1]     # never RunOk = 1
    assert no_success(world, target) and world.sns.messages == []
    if nth == 1:
        assert not (world.runs_dir / MAIN[target]).exists()     # the main batch never started
    assert not any(SENTINEL in line for line in world.lines)


@pytest.mark.parametrize("ending", list(ENDINGS))
def test_a_duplicate_whose_own_count_raises_exits_1_and_publishes_nothing(ready, world,
                                                                         monkeypatch, ending):
    """A duplicate is handled outside the run's own handler. Whatever its one publish
    call raises, the job returns 1, and it publishes no RunOk and no RunRefused. The
    partner: `test_a_second_delivery_is_a_duplicate_...`, in which the count lands."""
    target = "v1"
    ready(target)
    assert world.run(target) == 0
    before = len(events(world, "cloudwatch"))
    real = publish.Publisher.put

    def put(self, values):
        if values == [(publish.DUPLICATE, 1)]:
            raise ENDINGS[ending]()
        return real(self, values)
    monkeypatch.setattr(publish.Publisher, "put", put)
    assert world.run(target) == 1
    assert len(events(world, "cloudwatch")) == before           # nothing more was published
    assert world.published()["RunOk"] == [1]                    # the first run's own
    assert f"ws7 entry mode=scheduled target=v1 stage=crashed error=" \
           f"{type(ENDINGS[ending]()).__name__} during=duplicate" in world.lines


@pytest.mark.parametrize("ending", list(ENDINGS))
def test_anything_raised_inside_the_check_is_a_failed_check_that_says_so(thursday, monkeypatch,
                                                                         ending):
    """The outermost handler of the check is for `BaseException` too: the check returns
    1 and says on the topic that it failed. The partner is every check that completes."""
    world, target = thursday, "v1"
    monkeypatch.setattr(wl.Ledger, "expire", _ends(ending))
    assert world.check(target) == 1
    assert check_lines(world) == [f"WS7 check - target {target} - the check failed",
                                  f"code {entry.CRASHED}; see the task log"]
    assert not any(SENTINEL in line for line in world.lines)


@pytest.mark.parametrize("ending", list(ENDINGS))
@pytest.mark.parametrize("target, module, name, code", [
    ("v1", rce, "load_corpus", entry.CORPUS_UNREADABLE),
    ("pipeline", rce, "select_scenarios", entry.CORPUS_UNREADABLE),
    ("v1", rce, "load_system_prompt", entry.PRECHECK_FAILED),
    ("v1", rce, "run_generation", entry.PRECHECK_FAILED),
    ("pipeline", rpe, "run_pipeline", entry.PRECHECK_FAILED)])
def test_runner_code_that_raises_in_the_precheck_stops_the_run_before_the_claim(
        ready, world, monkeypatch, target, module, name, code, ending):
    """The precheck calls the runners' own loaders and request builders. Whatever one
    of them raises, the run stops with a fixed code before the claim: nothing is
    written, nothing is spent, and RunRefused alone is published."""
    ready(target)
    monkeypatch.setattr(module, name, _ends(ending))
    assert world.run(target) == 1
    assert stopped_with(world, code)
    assert world.s3.requests == [] and world.bedrock_requests() == 0
    assert world.published() == {"RunRefused": [1]}
    assert not any(SENTINEL in line for line in world.lines)


# ================================== EACH METRIC IS PUBLISHED AT MOST ONCE PER RUN ==

def _breaching_v1_stub():
    scenario = next(s for s in SCENARIOS if W.v1_text_with_an_invented_dose(s))
    return W.v1_stub_with_text({scenario: W.v1_text_with_an_invented_dose(scenario)})


@pytest.mark.parametrize("failing", ["nothing", "sns", "success_record"])
def test_a_step_that_fails_after_the_counters_were_accepted_never_publishes_them_again(
        ready, world, failing):
    """Codex P2, PR #9. A breach run. CloudWatch accepts the run's counters and breach
    values; then the breach message, or the success record, fails. The failure path
    sends RunOk = 0 and RunRefused = 1 and nothing else: exactly one sample of every
    counter is at the fake. What was delivered before the failure stands. The
    partner, in which nothing fails, has the same one sample of each."""
    target = "v1"
    ready(target, stub=_breaching_v1_stub())
    if failing == "sns":
        world.sns.script(500)
    elif failing == "success_record":
        world.s3.script_put(store.success_key(RUN_ID[target]), fakes.DENY_403)
    code = world.run(target)
    out = world.published()
    counters = {publish.pascal(name) for name in publish.run_metrics(target)}
    assert counters <= set(out) and out["CostUsdMicros"][0] > 0
    assert all(len(values) == 1 for values in out.values()), out
    # delivered before the failure, and it stands
    assert out["BreachMedicationsKind1"] == [1] and out["NoBaseline"] == [0]
    assert len(world.sns.messages) == (0 if failing == "sns" else 1)
    requests = [what.split(" ")[1].split(",")[0] for what in events(world, "cloudwatch")]
    if failing == "nothing":
        assert code == 0 and requests == ["NoBaseline", "RunOk"] and out["RunOk"] == [1]
        return
    assert code == 1
    assert requests == ["NoBaseline", "RunRefused"]             # no third request: no counter again
    assert (out["RunOk"], out["RunRefused"]) == ([0], [1])
    assert code_in(world, target) == {"sns": publish.ALERT_FAILED,
                                      "success_record": entry.SUCCESS_RECORD_UNAVAILABLE}[failing]
    assert not stopped_with(world, publish.PUBLISH_REPEATED)    # it was never even tried
    assert no_success(world, target)


def test_counters_cloudwatch_did_not_acknowledge_are_sent_by_the_failure_path(ready, world):
    """The positive control for the test above: when the run's own metrics call was
    refused, the failure path does send the counters, once."""
    target = "v1"
    ready(target)
    world.cw.script(500)
    assert world.run(target) == 1
    out = world.published()
    assert out["CostUsdMicros"][0] > 0 and out["RunOk"] == [0]
    assert [what.split(" ")[1].split(",")[0] for what in events(world, "cloudwatch")] == [
        "NoBaseline", "RunRefused", "CostUsdMicros"]


# ======================================= AN EXIT CODE THE BATCH MUST EXPLAIN ==

def _forge_exit(monkeypatch, target: str, nth: int, forged: int) -> list:
    """Make the `nth` runner call (1: the pre-flight, 2: the main batch) return
    `forged` after the real runner has run. Returns the real exit codes, as they come."""
    runner = rpe if target == "pipeline" else rce
    real, codes = runner.main, []

    def main(*args, **kwargs):
        codes.append(real(*args, **kwargs))
        return forged if len(codes) == nth else codes[-1]
    monkeypatch.setattr(runner, "main", main)
    return codes


@pytest.mark.parametrize("nth, kind", [(1, PRE), (2, MAIN), (0, None)])
def test_an_exit_code_1_the_batch_does_not_explain_is_a_runner_that_refused(
        ready, world, monkeypatch, nth, kind):
    """Codex P2, PR #9. Both runners return 1 for a failed generation, and also for
    other things. From either batch, a 1 beside an index with no failed generation
    and no gate failure is `ws7_runner_refused`. The partner (nth 0) forges nothing."""
    target = "pipeline"
    ready(target)
    codes = _forge_exit(monkeypatch, target, nth, 1)
    code = world.run(target)
    if kind is None:
        assert code == 0 and codes == [0, 0]
        return
    assert code == 1 and codes == [0] * nth                     # the real runner said 0
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"], record["counts"]) == (
        entry.RUNNER_REFUSED, [kind[target]],
        {"exit_code": 1, "generations_failed": 0, "gate_failures": 0})
    assert world.entry_of(RUN_ID[target])["state"] == "settled"
    assert no_success(world, target)
    if nth == 1:
        assert not (world.runs_dir / MAIN[target]).exists()     # the main batch never started


def test_an_exit_code_0_beside_a_failed_generation_is_refused_too(ready, world, monkeypatch):
    """The other direction of the same rule. The partner is
    `test_a_failed_v1_generation_is_counted_and_the_run_is_still_compared`, in which
    the runner returns 1 for the same batch and the run goes on."""
    target = "v1"
    ready(target, stub=_failing_generation_stub(target, "S2"))
    codes = _forge_exit(monkeypatch, target, 2, 0)
    assert world.run(target) == 1
    assert codes == [0, 1]
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (entry.RUNNER_REFUSED, [MAIN[target]])
    assert (record["counts"]["exit_code"], record["counts"]["generations_failed"]) == (0, 1)


# A value of each JSON type, and of each shape within a type that could be mistaken
# for an explanation ("false", 0, an empty string).
JSON_VALUES = {"a string": "x", "the string false": "false", "an empty string": "",
               "an integer": 1, "zero": 0, "a float": 1.5, "true": True, "false": False,
               "null": None, "an empty list": [], "a list": ["x"], "an empty object": {},
               "an object": {"error_code": "x"}}
# Every field of an index entry that could pass for an explanation of an exit code.
INDEX_FIELDS = ("error_code", "gate_ok", "gate_status", "failed_step")


def _clean_index(target: str, scenarios: list) -> dict:
    """A clean index in the shape that target's runner writes (invented)."""
    rows = []
    for sid in scenarios:
        row = {"run": 1, "scenario_id": sid, "record": f"r1/{sid}.json", "error_code": None,
               "cost_usd_est": 0.01}
        row.update({"failed_step": None} if target == "pipeline"
                   else {"gate_status": "documented_advice", "gate_ok": True})
        rows.append(row)
    return {"batch_id": "b", "stopped_early": False, "generations": rows}


def _explains(target: str, field: str, value: object) -> tuple | None:
    """What the rule says of one entry whose `field` is `value`: (failed generations,
    gate failures), or None for an index that target's runner did not write."""
    if field == "error_code":
        if value is None:
            return 0, 0
        return (1, 0) if type(value) is str and value else None
    if field == "gate_ok" and target == "v1":
        if value is None or value is True:
            return 0, 0
        return (0, 1) if value is False else None
    return 0, 0                 # not a field that explains anything for this target


@pytest.mark.parametrize("value", list(JSON_VALUES))
@pytest.mark.parametrize("field", INDEX_FIELDS)
@pytest.mark.parametrize("target", TARGETS)
def test_only_the_fields_a_targets_runner_writes_explain_an_exit_code_each_by_exact_type(
        target, field, value):
    """THE CLASS (Codex P2, PR #9, re-check): an exit code is explained only by the
    fields that target's runner writes, checked by exact type. The pipeline has no
    safety-net gate, so for the pipeline only a failed generation explains a 1; for
    v1, a failed generation or `gate_ok` that is exactly the boolean false.

    Every field that could pass for an explanation, with a value of each JSON type.
    The partners are in the same table: the one value of each field that does explain
    a 1, and the values that leave the index clean."""
    index = _clean_index(target, ["S1", "S2"])
    assert entry.index_counts(target, index) == (0, 0)
    assert entry.exit_code_explained(target, index, 0)
    assert not entry.exit_code_explained(target, index, 1)

    index["generations"][1][field] = JSON_VALUES[value]
    wanted = _explains(target, field, JSON_VALUES[value])
    assert entry.index_counts(target, index) == wanted
    explains_a_1 = wanted is not None and wanted != (0, 0)
    assert entry.exit_code_explained(target, index, 1) is explains_a_1
    assert entry.exit_code_explained(target, index, 0) is (wanted == (0, 0))
    # an explanation is exactly these, and no other
    assert explains_a_1 == (
        (field == "error_code" and value in ("a string", "the string false"))
        or (target, field, value) == ("v1", "gate_ok", "false"))


@pytest.mark.parametrize("exit_code", [2, 3, -1, True, False, None, "1", 1.0, [1]], ids=repr)
def test_nothing_but_the_integers_0_and_1_is_an_exit_code_an_index_can_explain(exit_code):
    for target in TARGETS:
        clean = _clean_index(target, ["S1"])
        failed = _clean_index(target, ["S1"])
        failed["generations"][0]["error_code"] = "invented_code"
        assert entry.exit_code_explained(target, clean, 0)       # the partners
        assert entry.exit_code_explained(target, failed, 1)
        assert not entry.exit_code_explained(target, clean, exit_code)
        assert not entry.exit_code_explained(target, failed, exit_code)


@pytest.mark.parametrize("index", [
    None, [], "x", {}, {"generations": None}, {"generations": {}}, {"generations": [None]},
    {"generations": ["S1"]}, {"generations": [[]]}, {"generations": [{}]},
    {"generations": [{"gate_ok": False}]}], ids=repr)
def test_an_index_that_is_not_a_list_of_entries_with_an_error_code_explains_nothing(index):
    for target in TARGETS:
        assert entry.index_counts(target, index) is None
        assert not entry.exit_code_explained(target, index, 0)
        assert not entry.exit_code_explained(target, index, 1)
    # the partner; and v1's runner always writes `gate_ok`, the pipeline's never does
    assert entry.index_counts("pipeline", {"generations": [{"error_code": None}]}) == (0, 0)
    assert entry.index_counts("v1", {"generations": [{"error_code": None}]}) is None
    assert entry.index_counts("v1", {"generations": [{"error_code": None, "gate_ok": None}]}) == (0, 0)


def _forge_pre_flight(monkeypatch, world, target: str, field: str, value: object) -> None:
    """Let the real pre-flight run; then put `value` in `field` of its index entry and
    have the runner return 1."""
    runner = rpe if target == "pipeline" else rce
    real, calls = runner.main, []

    def main(*args, **kwargs):
        code = real(*args, **kwargs)
        calls.append(code)
        if len(calls) > 1:
            return code
        path = world.runs_dir / PRE[target] / "batch.json"
        index = json.loads(path.read_text("utf-8"))
        index["generations"][0][field] = value
        path.write_text(json.dumps(index), "utf-8")
        return 1
    monkeypatch.setattr(runner, "main", main)


@pytest.mark.parametrize("value", list(JSON_VALUES))
@pytest.mark.parametrize("field", INDEX_FIELDS)
@pytest.mark.parametrize("target", TARGETS)
def test_through_the_entrypoint_an_exit_1_is_accepted_only_for_that_targets_own_explanation(
        ready, world, monkeypatch, target, field, value):
    """The same table, through the entrypoint. The real pre-flight runs; its index
    entry then gets one forged field, and the runner returns 1.

    - A failed generation (`error_code` a non-empty string) explains the 1, for either
      target: the run reaches the pre-flight's own go/no-go, which stops it
      (`ws7_preflight_failed`).
    - For v1 only, `gate_ok` exactly false explains it: the run goes on and succeeds.
    - Everything else is `ws7_runner_refused`: for the pipeline that includes
      `gate_ok` false, which its runner never writes."""
    ready(target)
    _forge_pre_flight(monkeypatch, world, target, field, JSON_VALUES[value])
    wanted = _explains(target, field, JSON_VALUES[value])
    code = world.run(target)
    if wanted == (0, 1):
        assert (target, field, value) == ("v1", "gate_ok", "false")
        assert code == 0 and world.metric("RunOk") == [1]
        return
    assert code == 1
    record = world.failed(RUN_ID[target])
    if wanted == (1, 0):
        assert (record["code"], record["ids"]) == (entry.PREFLIGHT_FAILED, [PRE[target]])
    else:
        assert (record["code"], record["ids"]) == (entry.RUNNER_REFUSED, [PRE[target]])
        assert record["counts"]["exit_code"] == 1
    assert not (world.runs_dir / MAIN[target]).exists()         # the main batch never started
    assert world.published()["RunOk"] == [0] and no_success(world, target)


def _gate_failing_scenario() -> str:
    """A scenario whose notes record no seek-help trigger: the stub's v1 text, which
    has no fall-back line, fails the safety-net gate on it."""
    gate = rce.safety_net_gate
    return next(sid for sid in SCENARIOS if not gate.check_combined(
        W.CORPUS[sid].notes, stubs.V1_COMBINED_TEXT, gate.V1_LINES).ok)


def test_an_exit_code_1_that_a_recorded_gate_failure_explains_is_accepted(ready, world,
                                                                          monkeypatch):
    """The partner of the refusal: v1's main batch really returns 1, because some
    scenarios fail the safety-net gate, and its index says so. The run goes on."""
    target = "v1"
    ready(target)
    codes = _forge_exit(monkeypatch, target, 0, 1)              # forges nothing: it only listens
    assert world.run(target) == 0
    assert codes == [0, 1]
    index = json.loads((world.runs_dir / MAIN[target] / "batch.json").read_text("utf-8"))
    failed, gates = entry.index_counts(target, index)
    assert (failed, gates > 0) == (0, True)
    assert _gate_failing_scenario() in {g["scenario_id"] for g in index["generations"]
                                        if g["gate_ok"] is False}


def test_a_gate_failure_on_the_pre_flight_scenario_is_not_a_no_go(ready, world, monkeypatch,
                                                                  tmp_path):
    """The author's ruling (21 Nov 2026). The pre-flight's go/no-go is D7's: no failed
    generation, only the expected files, within its reservation. Here the pre-flight
    scenario fails the v1 safety-net gate, so the pre-flight batch returns 1 with a
    recorded gate failure. That is quality evidence: the main batch runs, and the
    comparator judges it. The partner, a pre-flight whose generation FAILS, stops:
    `test_a_pre_flight_generation_that_fails_stops_the_run_before_the_main_batch`."""
    target = "v1"
    scenario = _gate_failing_scenario()
    assert scenario != CFG.preflight_scenario
    config = json.loads(reserve.CONFIG_PATH.read_text("utf-8"))
    config["preflight_scenario"] = scenario
    world.config_path = tmp_path / "ws7_config.json"
    world.config_path.write_text(json.dumps(config), "utf-8")
    ready(target)
    codes = _forge_exit(monkeypatch, target, 0, 1)
    assert world.run(target) == 0
    assert codes == [1, 1]                                      # the pre-flight returned 1
    index = json.loads((world.runs_dir / PRE[target] / "batch.json").read_text("utf-8"))
    assert [(g["scenario_id"], g["gate_ok"], g["error_code"]) for g in index["generations"]] == [
        (scenario, False, None)]
    # the main batch ran, the comparator judged it, and the run is a success
    assert world.stub.count(FIRST_STEP[target]) == len(SCENARIOS) + 1
    assert world.s3.json(PREFIX[target] + "comparison.json")["status"] == "no_breach"
    assert world.metric("RunOk") == [1]
    assert not stopped_with(world, entry.PREFLIGHT_FAILED)


# ============================================== STORING ON THE FAILURE PATH ==

def test_a_scorer_that_stops_after_the_reports_were_written_does_not_make_them_unknown(
        ready, world, monkeypatch):
    """Codex P2, PR #9. `score_batch` writes its reports, and the assembly replay then
    stops. The known score files are allowed from the moment scoring begins, so the
    reports and the guard's report are stored, and the run fails with the scorer's
    own code, not with a storing code."""
    target = "pipeline"
    ready(target)
    monkeypatch.setattr(SA, "replay", _raising)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (entry.SCORER_STOPPED, ["score_assemble"])
    assert (record["evidence"], record["files_refused"]) == ("stored", 0)
    stored = set(run_keys(world, target))
    written = {name for name in entry.files_in(world.runs_dir / MAIN[target])[0]
               if name.startswith("scores/")}
    assert written and "scores/safety_net.json" in written
    assert "scores/assemble/assemble.json" not in written       # the replay never wrote
    assert {PREFIX[target] + f"batches/{MAIN[target]}/{name}" for name in written} <= stored
    assert PREFIX[target] + "guard_report.json" in stored
    assert not any(SENTINEL in line for line in world.lines)


@pytest.mark.parametrize("extra", [(), ("extra.json",), ("extra.json", "notes.txt", "r1/EXTRA.md")])
def test_an_unknown_file_on_the_failure_path_is_left_out_and_counted_and_the_rest_is_stored(
        ready, world, extra):
    """The author's ruling (21 Nov 2026). The run fails for another reason (a throttled
    call), and files the runners do not write are in the main batch folder. One
    refused file does not stop the rest: the guard's report goes first, then every
    allowed file; the unknown files are never uploaded, and the failed record counts
    them. The partner has no unknown file."""
    target = "v1"
    stub = _fail_a_main_batch_call(target)
    main = world.runs_dir / MAIN[target]

    def add():
        for name in extra:
            (main / name).write_text("invented", "utf-8")
    W.on_nth(stub, FIRST_STEP[target], 3, add)
    ready(target, stub=stub)
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["evidence"], record["files_refused"]) == (
        entry.CALL_FAILED, "stored", len(extra))
    assert all((main / name).is_file() for name in extra)
    stored = run_keys(world, target)
    assert set(stored) == {PREFIX[target] + "guard_report.json"} | {
        PREFIX[target] + f"batches/{PRE[target]}/{name}"
        for name in entry.expected_files(target, [CFG.preflight_scenario], scored=False)} | {
        PREFIX[target] + f"batches/{MAIN[target]}/{name}"
        for name in entry.expected_files(target, SCENARIOS, scored=False)}
    # the guard's report is the first record of the run to be stored
    first = next(what for server, what in world.log.all()
                 if server == "s3" and what.startswith(f"PUT {PREFIX[target]}"))
    assert first == f"PUT {PREFIX[target]}guard_report.json"


def test_the_guards_report_is_the_first_record_a_successful_run_stores_too(ready, world):
    target = "v1"
    ready(target)
    assert world.run(target) == 0
    first = next(what for server, what in world.log.all()
                 if server == "s3" and what.startswith(f"PUT {PREFIX[target]}"))
    assert first == f"PUT {PREFIX[target]}guard_report.json"
    assert world.lines.count("ws7 entry mode=scheduled target=v1 stage=stored "
                             f"objects={len(run_keys(world, target))} files_refused=0") == 1


# ============================================ THE DISTINCT-SCENARIOS PRECHECK ==

def _argv(target: str) -> list:
    return wm.runner_argv(target, CFG, preflight=False, batch="b")


@pytest.mark.parametrize("target", TARGETS)
def test_the_prechecks_fingerprint_is_the_one_the_wrapper_records_for_each_scenario(
        ready, world, target, monkeypatch):
    """Carried item B. For every scenario, the fingerprint the precheck works out with
    no client is the hash the wrapper records for that scenario's first model request
    when the real runner sends it to the stub."""
    ready(target)
    predicted = entry.first_requests(target, _argv(target))
    assert list(predicted) == SCENARIOS and len(set(predicted.values())) == len(SCENARIOS)
    assert world.log.all() == []                    # the precheck asked no server anything

    recorded, real = [], wc.fingerprint

    def fingerprint(request):
        digest = real(request)
        step = request["inferenceConfig"]["maxTokens"] == reserve.plan(target).steps[0].max_tokens
        recorded.append((step, digest))
        return digest
    monkeypatch.setattr(wc, "fingerprint", fingerprint)
    assert world.run(target) == 0
    # what the wrapper saw at the stub: the pre-flight's first request, then the 18
    at_wrapper = [digest for first, digest in recorded[len(SCENARIOS):] if first]
    assert at_wrapper == [predicted[CFG.preflight_scenario]] + [predicted[s] for s in SCENARIOS]
    assert world.stub.count(FIRST_STEP[target]) == len(SCENARIOS) + 1


def _corpus(tmp_path: Path, notes: dict) -> Path:
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps({"scenarios": [{"id": sid, "title": "invented", "notes": text}
                                               for sid, text in notes.items()]}), "utf-8")
    return path


INVENTED_NOTES = "Invented ward note.\nNo real patient.\nDischarged home."
SAME_REQUEST = {      # two scenarios whose notes differ only in a way that runner normalises away
    "pipeline": INVENTED_NOTES.replace("\n", "\r\n"),          # line endings: step 1 splits lines
    "v1": INVENTED_NOTES + "\n\n",                             # outer whitespace: the loader strips
}


@pytest.mark.parametrize("target", TARGETS)
def test_two_scenarios_that_would_send_the_same_first_request_stop_the_run_before_the_claim(
        ready, world, target, tmp_path, monkeypatch):
    """The notes differ, so their raw hashes differ; the first model request does not.
    The precheck itself sends nothing to any of the five servers. The run then stops
    with nothing claimed, read or spent, and counts one refusal."""
    ready(target)
    notes = {"S1": INVENTED_NOTES, "S2": SAME_REQUEST[target]}
    assert notes["S1"] != notes["S2"]
    monkeypatch.setattr(rce, "DEFAULT_CORPUS_PATH", _corpus(tmp_path, notes))
    with pytest.raises(store.Stop) as stop:
        entry.first_requests(target, _argv(target))
    assert (stop.value.code, stop.value.ids, stop.value.counts) == (
        entry.SCENARIOS_NOT_DISTINCT, ("S1", "S2"), {"scenarios": 2})
    assert world.log.all() == []                    # zero requests at every server

    assert world.run(target) == 1
    assert stopped_with(world, entry.SCENARIOS_NOT_DISTINCT)
    assert world.log.all() == [("cloudwatch", "PutMetricData RunRefused")]
    assert world.s3.requests == [] and world.ssm.requests == [] and world.sns.messages == []
    assert world.bedrock_requests() == 0 and world.published() == {"RunRefused": [1]}

    # the partner: the same two scenarios with notes that do differ are distinct
    monkeypatch.setattr(rce, "DEFAULT_CORPUS_PATH", _corpus(
        tmp_path, {"S1": INVENTED_NOTES, "S2": INVENTED_NOTES + " Seen by the registrar."}))
    assert list(entry.first_requests(target, _argv(target))) == ["S1", "S2"]


def test_line_endings_alone_do_not_make_two_v1_requests_the_same(tmp_path, monkeypatch):
    """v1 sends the notes as they are, apart from outer whitespace, so for v1 two
    scenarios that differ in line endings are two different first requests."""
    monkeypatch.setattr(rce, "DEFAULT_CORPUS_PATH", _corpus(
        tmp_path, {"S1": INVENTED_NOTES, "S2": SAME_REQUEST["pipeline"]}))
    assert len(set(entry.first_requests("v1", _argv("v1")).values())) == 2


@pytest.mark.parametrize("target", TARGETS)
def test_the_precheck_never_sleeps_never_writes_and_runs_only_the_first_request(
        offline_aws, target, monkeypatch, tmp_path):  # noqa: F811
    """The author's conditions on precheck B. No server is running and no address is
    allowed, so any socket would fail the test."""
    slept, written, steps = [], [], []
    monkeypatch.setattr(entry, "_precheck_sleep", slept.append)
    for name in ("write_text", "write_bytes", "mkdir", "touch"):
        monkeypatch.setattr(Path, name, lambda self, *a, _n=name, **k: written.append((_n, self)))
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        if set(mode) & set("wax+"):
            written.append(("open", file))
        return real_open(file, mode, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", spy_open)
    real_step = rpe.run_step
    monkeypatch.setattr(rpe, "run_step", lambda name, *a, **k: (steps.append(name),
                                                               real_step(name, *a, **k))[1])
    recorders = []
    real_recorder = entry._Recorder
    monkeypatch.setattr(entry, "_Recorder", lambda: (recorders.append(real_recorder()),
                                                     recorders[-1])[1])
    found = entry.first_requests(target, _argv(target))
    assert len(found) == len(SCENARIOS) == len(recorders)
    assert slept == [] and written == []
    assert all(len(r.fingerprints) == 1 for r in recorders)     # one request each, and no more
    # a plain Python object: nothing of boto, and nothing but the fingerprints it kept
    assert all(type(r).__module__ == "ws7_entry" and vars(r) == {"fingerprints": r.fingerprints}
               for r in recorders)
    if target == "pipeline":
        assert steps == ["guard_input", "extract_facts"] * len(SCENARIOS)   # step 1 and the first request
    assert offline_aws.allowed == set()


@pytest.mark.parametrize("target", TARGETS)
def test_the_sleep_spy_would_have_seen_a_sleep(target, monkeypatch):
    """The positive control for "never sleeps": the same runner code, handed a client
    that answers with a throttle, does call the sleep it was given."""
    slept = []

    class Throttling:
        def converse(self, **_request):
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "invented"},
                               "ResponseMetadata": {"HTTPStatusCode": 429}}, "Converse")
    scenario = W.CORPUS["S1"]
    if target == "pipeline":
        ctx = StepContext(bedrock=Throttling(), model_id=rpe.MODEL_ID, sleep=slept.append)
        _steps, failed, code, _errors = rpe.run_pipeline(ctx, scenario)
        assert (failed, code) == ("extract_facts", "throttled")
    else:
        v1 = rce.Config(rce.load_system_prompt(rce.DEFAULT_PROMPT_PATH), {}, None, None, True, True)
        record = rce.run_generation(Throttling(), scenario, v1, batch_id="b", run=1, started_at="",
                                    sleep=slept.append)
        assert record["error"]["code"] == "ThrottlingException"
    assert len(slept) == 2                          # two retries, each after a sleep


def test_a_precheck_whose_sleep_is_called_stops():
    with pytest.raises(entry.StepError) as stop:
        entry._precheck_sleep(1.0)
    assert stop.value.code == "ws7_precheck_slept"


@pytest.mark.parametrize("notes", [7, 1.5, True, ["a"], {"a": 1}, None, ""], ids=repr)
def test_a_corpus_the_loader_cannot_read_stops_the_run_with_a_fixed_code(ready, world, tmp_path,
                                                                        monkeypatch, notes):
    target = "v1"
    ready(target)
    monkeypatch.setattr(rce, "DEFAULT_CORPUS_PATH", _corpus(tmp_path, {"S1": notes}))
    assert world.run(target) == 1
    assert stopped_with(world, entry.CORPUS_UNREADABLE)
    assert world.log.all() == [("cloudwatch", "PutMetricData RunRefused")]


def test_a_corpus_without_the_pre_flight_scenario_stops_the_run_before_the_claim(
        ready, world, tmp_path, monkeypatch):
    target = "v1"
    ready(target)
    monkeypatch.setattr(rce, "DEFAULT_CORPUS_PATH", _corpus(tmp_path, {"S2": INVENTED_NOTES}))
    assert world.run(target) == 1
    assert stopped_with(world, entry.PREFLIGHT_SCENARIO_UNKNOWN)
    assert world.s3.requests == []


def test_the_runners_get_a_sleep_that_returns_at_once(ready, world, monkeypatch):
    """The author's change F. Both runners call their sleep only before a retry of a
    throttled call, and WS7 refuses every retry, so the sleep they get does not wait.
    Here it is asked for 20 to 30 seconds once, by the pipeline's retry rule."""
    target = "pipeline"
    asked = []
    real = entry.no_wait
    monkeypatch.setattr(entry, "no_wait", lambda seconds: (asked.append(seconds), real(seconds))[1])
    stub = W.default_stub(target)
    W.fail_nth(stub, "extract_facts", 3, THROTTLE)
    ready(target, stub=stub)
    assert world.run(target) == 1                   # inside the bounded wait of `world.call`
    assert len(asked) == 1 and 20.0 <= asked[0] <= 30.0
    assert entry.no_wait(30.0) is None


# ============================================================ THE THURSDAY CHECK ==

def check_lines(world) -> list:
    """The lines of the one message the check sent."""
    assert len(world.sns.messages) == 1, world.sns.messages
    return world.sns.messages[0]["message"].split("\n")


def seed_claim(world, target: str, when: datetime, run: str | None = None) -> str:
    """A scheduled claim, as `claim()` writes one. Returns its run ID."""
    run = run or store.run_id(target, "scheduled", when)
    world.s3.seed(store.claim_key(target, "scheduled", when), wl._dump({
        "schema": wl.CLAIM_SCHEMA, "target": target, "mode": "scheduled",
        "scheduled_time": store.utc_text(when), "run_id": run, "claim_id": "c" * 32,
        "claimed_at": store.utc_text(when + timedelta(seconds=37))}))
    return run


WEDNESDAY = datetime(2026, 12, 16, 4, 0, tzinfo=timezone.utc)
THURSDAY = datetime(2026, 12, 17, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def thursday(world):
    world.clock.start = THURSDAY
    return world


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("when, start, check_when, check_start, run", [
    (WHEN, W.START, CHECK_WHEN, THURSDAY, RUN_ID),                              # a GMT week
    (W.BST_WHEN, W.BST_START, W.BST_CHECK_WHEN,
     datetime(2027, 6, 17, 9, 1, tzinfo=timezone.utc), W.BST_RUN_ID)])           # a BST week
def test_the_check_after_a_successful_run_is_silent_and_takes_the_run_id_from_the_claim(
        ready, world, target, when, start, check_when, check_start, run):
    """The author's change B. In British Summer Time the 04:00 London run is 03:00 UTC,
    and its ID says so. The check reads that ID in the claim object: it never builds
    one. With the success record gone, it names the run by the claim's own ID."""
    ready(target)
    world.clock.start = start
    assert world.run(target, when) == 0
    run_id = run[target]
    assert json.loads(world.s3.body(f"ws7/claims/{target}/{when[:10]}"))["run_id"] == run_id
    before = len(world.log.all())
    world.clock.start = check_start
    assert world.check(target, check_when) == 0
    assert world.sns.messages == []                 # nothing to report
    assert world.lines[-1] == f"ws7 entry mode=check target={target} stage=checked findings=0"
    # the check makes no model call, no claim, no SSM read and no CloudWatch call
    after = world.log.all()[before:]
    assert {server for server, _what in after} == {"s3"}
    assert not any(what.startswith("PUT") for _server, what in after)

    world.s3.remove(store.success_key(run_id))
    assert world.check(target, check_when) == 0
    assert check_lines(world)[:2] == [
        f"WS7 check - target {target} - occurrence {when[:10]}",
        f"run {run_id} started and did not finish: no success record"]
    assert world.metric("RunOk") == [1]             # the run's own; the check published nothing


@pytest.mark.parametrize("target", TARGETS)
def test_the_check_says_no_claim_without_saying_the_run_never_started(thursday, target):
    """The author's change C: a stop before the claim also leaves no claim."""
    world = thursday
    assert world.check(target) == 0
    assert check_lines(world) == [
        f"WS7 check - target {target} - occurrence 2026-12-16",
        "no claim: the run did not start, or stopped before its claim; see the task log",
        f"records: s3://{BUCKET}/ws7/",
        f"report time (the alarm): 2026-12-17 10:01 London"]
    assert world.sns.messages[0]["topic"] == W.TOPIC
    assert world.sns.messages[0]["subject"] == check_lines(world)[0]
    assert world.cw.bodies == [] and world.ssm.requests == []
    assert not any(method == "PUT" for method, _key in world.s3.requests)       # no claim made


def test_a_stop_before_the_claim_is_what_the_no_claim_line_covers(ready, world):
    """A run that stopped at the match check left no claim, and the check says so in
    words that cover it."""
    target = "v1"
    ready(target)
    world.expect(target, model_id="another.model-id")
    assert world.run(target) == 1
    world.clock.start = THURSDAY
    assert world.check(target) == 0
    assert check_lines(world)[1] == publish.NO_CLAIM_LINE
    assert "did not start, or stopped before its claim" in publish.NO_CLAIM_LINE


@pytest.mark.parametrize("days, found", [(0, False), (1, True), (2, False)])
def test_the_check_looks_exactly_one_utc_day_back(thursday, days, found):
    """CHECK_LOOKBACK_DAYS at its boundary: a claim dated the day before the check's
    scheduled time is the one it reads; one dated the same day, or two days before,
    is not."""
    world, target = thursday, "v1"
    assert entry.CHECK_LOOKBACK_DAYS == 1
    when = datetime(2026, 12, 17, 4, 0, tzinfo=timezone.utc) - timedelta(days=days)
    run = seed_claim(world, target, when)
    assert world.check(target) == 0
    line = check_lines(world)[1]
    assert line == (f"run {run} started and did not finish: no success record" if found
                    else publish.NO_CLAIM_LINE)


def _failed_record(run: str, target: str, code: str, ids: list, when: str = WHEN,
                   change: dict | None = None) -> bytes:
    """A failed record, as the entrypoint writes one (invented), with `change` applied."""
    return compare.render({**{
        "schema": entry.FAILED_SCHEMA, "run_id": run, "target": target, "mode": "scheduled",
        "scheduled_time": when, "code": code, "ids": ids, "counts": {}, "also": [],
        "ledger": "not_admitted", "evidence": "none", "files_refused": 0,
        "git_commit": W.COMMIT, "image_digest": W.DIGEST,
        "failed_at": "2026-12-16T04:00:40Z"}, **(change or {})})


def _success_record(run: str, target: str, when: str = WHEN,
                    change: dict | None = None) -> bytes:
    """A success record, as the entrypoint writes one (invented), with `change` applied."""
    return compare.render({**{
        "schema": entry.SUCCESS_SCHEMA, "run_id": run, "target": target, "mode": "scheduled",
        "scheduled_time": when, "status": "no_breach", "breaches": 0,
        "baseline_id": W.BASELINE_ID, "summary_sha256": "1" * 64, "comparison_sha256": "2" * 64,
        "evidence": f"ws7/runs/scheduled/{run}/", "git_commit": W.COMMIT,
        "image_digest": W.DIGEST, "finished_at": "2026-12-16T04:40:00Z"}, **(change or {})})


def test_a_success_record_and_a_failed_record_together_are_a_failed_run(thursday):
    """The author's change B. The partner: the success record alone is silence."""
    world, target = thursday, "pipeline"
    run = seed_claim(world, target, WEDNESDAY)
    world.s3.seed(store.success_key(run), _success_record(run, target))
    assert world.check(target) == 0
    assert world.sns.messages == []
    world.s3.seed(store.failed_key(run), _failed_record(run, target, publish.PUBLISH_FAILED, []))
    assert world.check(target) == 0
    assert check_lines(world)[1] == f"failed run {run} code {publish.PUBLISH_FAILED}"


def test_the_check_reports_a_run_that_really_failed_by_its_code(ready, world):
    target = "v1"
    stub = W.default_stub(target)
    W.fail_nth(stub, FIRST_STEP[target], 3, ERROR_500)
    ready(target, stub=stub)
    assert world.run(target) == 1
    world.clock.start = THURSDAY
    assert world.check(target) == 0
    lines = check_lines(world)
    assert lines[1] == f"failed run {RUN_ID[target]} code ws7_unmetered ids server_error, {MAIN[target]}"
    # and the entry that run left unresolved, which blocks the next scheduled run
    assert lines[2] == (f"unresolved entry {RUN_ID[target]} (unmetered, month 2026-12): "
                        f"it blocks every scheduled run until it is resolved")


def _unresolved(run: str, kind: str = "abandoned", mode: str = "scheduled",
                target: str = "pipeline") -> dict:
    return {"run_id": run, "target": target, "mode": mode, "state": "unresolved", "kind": kind,
            "basis": None, "reserved_usd_micros": 8280476, "counted_usd_micros": 8280476,
            "estimate_usd_micros": 8280476, "opened_at": "2026-12-09T04:00:30Z",
            "expires_at": "2026-12-09T06:00:30Z", "closed_at": "2026-12-09T06:00:30Z",
            "resolved_at": None}


def test_a_run_blocked_by_an_unresolved_entry_is_reported_by_that_cause_naming_the_entry(
        ready, world):
    """D9. The scheduled run really is blocked, by an entry an earlier run left; the
    check's report names the entry the author must resolve."""
    target = "pipeline"
    ready(target)
    blocker = "2026-12-09T0400-pipeline"
    seed_ledger(world, [_unresolved(blocker)])
    assert world.run(target) == 1
    record = world.failed(RUN_ID[target])
    assert (record["code"], record["ids"]) == (wl.BLOCKED_UNRESOLVED, [blocker])
    assert world.bedrock_requests() == 0
    world.clock.start = THURSDAY
    assert world.check(target) == 0
    assert check_lines(world)[1:3] == [
        f"failed run {RUN_ID[target]} code ws7_blocked_unresolved: blocked by unresolved {blocker}",
        f"unresolved entry {blocker} (abandoned, month 2026-12): "
        f"it blocks every scheduled run until it is resolved"]


def _open(run: str, opened: str, expires: str, target: str = "v1") -> dict:
    return {"run_id": run, "target": target, "mode": "scheduled", "state": "open", "kind": None,
            "basis": None, "reserved_usd_micros": 2451884, "counted_usd_micros": 2451884,
            "estimate_usd_micros": None, "opened_at": opened, "expires_at": expires,
            "closed_at": None, "resolved_at": None}


def test_a_run_that_died_on_wednesday_is_reported_as_abandoned_that_thursday(thursday):
    """The author's change E: `expire()` before `unresolved()`. The run died outright:
    its entry is still open, and nothing has marked it. The check marks it abandoned
    and reports it in the same call."""
    world, target = thursday, "v1"
    run = seed_claim(world, target, WEDNESDAY)
    seed_ledger(world, [_open(run, "2026-12-16T04:00:40Z", "2026-12-16T06:00:40Z")])
    assert world.check(target) == 0
    assert check_lines(world)[1:3] == [
        f"run {run} started and did not finish: no success record",
        f"unresolved entry {run} (abandoned, month 2026-12): "
        f"it blocks every scheduled run until it is resolved"]
    booked = world.entry_of(run)
    assert (booked["state"], booked["kind"]) == ("unresolved", "abandoned")
    # the abandonment was written before the report was sent
    assert world.log.index("s3", f"PUT {LEDGER_KEY}") < world.log.index("sns", "Publish")


def test_an_entry_that_has_not_expired_is_not_abandoned_by_the_check(thursday):
    """The partner: an open entry still inside its limit is left alone."""
    world, target = thursday, "v1"
    seed_claim(world, target, WEDNESDAY)
    seed_ledger(world, [_open("2026-12-17T0930-v1-manual", "2026-12-17T09:30:00Z",
                              "2026-12-17T14:30:00Z")])
    assert world.check(target) == 0
    assert len(check_lines(world)) == 4             # the heading, one finding and the two footers
    assert not any(method == "PUT" for method, _key in world.s3.requests)


def test_a_waived_month_is_reported_every_week_of_that_month_and_not_the_next(thursday):
    world, target = thursday, "pipeline"
    accepted = dict(_unresolved("2026-12-02T0400-pipeline"), state="resolved",
                    basis="estimate_accepted", resolved_at="2026-12-03T09:00:00Z")
    seed_ledger(world, [accepted], waived=True)
    for wednesday in (9, 16, 23, 30):               # every Wednesday left in December
        when = datetime(2026, 12, wednesday, 4, 0, tzinfo=timezone.utc)
        run = seed_claim(world, target, when)
        world.s3.seed(store.success_key(run), _success_record(run, target, store.utc_text(when)))
        world.sns.messages.clear()
        world.clock.start = when + timedelta(days=1, hours=6, minutes=1)
        assert world.check(target, store.utc_text(when + timedelta(days=1, hours=6))) == 0
        assert check_lines(world)[1] == "month 2026-12: assurance waived"
    # the first Wednesday of January: the run succeeded and its month is not waived
    when = datetime(2027, 1, 6, 4, 0, tzinfo=timezone.utc)
    run = seed_claim(world, target, when)
    world.s3.seed(store.success_key(run), _success_record(run, target, store.utc_text(when)))
    world.sns.messages.clear()
    world.clock.start = when + timedelta(days=1, hours=6, minutes=1)
    assert world.check(target, "2027-01-07T10:00:00Z") == 0
    assert world.sns.messages == []


# ---------------------------------------- the check trusts nothing as read (Codex P2, PR #9)

def _claim(target: str, when: datetime = WEDNESDAY, change: dict | None = None) -> bytes:
    """A scheduled claim, as `claim()` writes one (invented), with `change` applied."""
    return wl._dump({**{
        "schema": wl.CLAIM_SCHEMA, "target": target, "mode": "scheduled",
        "scheduled_time": store.utc_text(when), "run_id": store.run_id(target, "scheduled", when),
        "claim_id": "c" * 32, "claimed_at": store.utc_text(when + timedelta(seconds=37))},
        **(change or {})})


OTHER = {"v1": "pipeline", "pipeline": "v1"}
# (what is wrong with the object, the label the check reports it by). None: nothing is wrong.
WRONG_CLAIMS = [
    (None, None),
    (b"not json", "not_json"), (b"\xff\xfe", "not_json"),
    (b"[]", "not_an_object"), (b"null", "not_an_object"), (b'"ws7-claim/1"', "not_an_object"),
    ({"schema": "ws7-claim/2"}, "schema"), ({"schema": None}, "schema"),
    ({"target": "pipeline"}, "target"), ({"target": 7}, "target"),
    ({"mode": "manual"}, "mode"), ({"mode": None}, "mode"),
    ({"scheduled_time": "2026-12-16T04:00:00"}, "scheduled_time"),      # no zone
    ({"scheduled_time": "2026-12-15T04:00:00Z"}, "scheduled_time"),     # another day's occurrence
    ({"scheduled_time": 7}, "scheduled_time"),
    ({"run_id": "2026-12-16T0300-v1"}, "run_id"),                       # not its own time's ID
    ({"run_id": "2026-12-16T0400-pipeline"}, "run_id"),                 # another target's run
    ({"run_id": "2026-12-16T0400-v1-manual"}, "run_id"),
    ({"run_id": None}, "run_id")]


@pytest.mark.parametrize("wrong, label", WRONG_CLAIMS, ids=lambda v: str(v)[:40])
def test_a_claim_object_that_fails_its_check_is_reported_invalid_and_never_trusted(
        thursday, wrong, label):
    """Each field wrong in turn. The run the claim would name has a perfect success
    record, and it still does not count: an invalid claim names no run. The partner
    (nothing wrong) is silent, because that success record is then read and trusted."""
    world, target = thursday, "v1"
    body = wrong if isinstance(wrong, bytes) else _claim(target, change=wrong)
    world.s3.seed(CLAIM_KEY[target], body)
    for run in {RUN_ID[target], "2026-12-16T0300-v1", "2026-12-16T0400-pipeline"}:
        world.s3.seed(store.success_key(run), _success_record(run, target))
    assert world.check(target) == 0
    if label is None:
        assert world.sns.messages == []
        return
    assert check_lines(world)[1] == (f"claim object invalid ({label}): "
                                     f"it is not read as a scheduled claim")
    # nothing was read on the word of an invalid claim
    assert not any(key.startswith(("ws7/success/", "ws7/failed/"))
                   for method, key in world.s3.requests if method == "GET")


def test_the_claims_run_id_is_reported_as_the_claim_wrote_it_at_whatever_hour(thursday):
    """The check never builds a run ID from its own scheduled time: a claim made at
    03:00 UTC names its 03:00 run."""
    world, target = thursday, "v1"
    seed_claim(world, target, datetime(2026, 12, 16, 3, 0, tzinfo=timezone.utc))
    assert world.check(target) == 0
    assert check_lines(world)[1].startswith("run 2026-12-16T0300-v1 started")


WRONG_RECORDS = [
    (None, None),
    (b"not json", "not_json"), (b"[]", "not_an_object"), (b"{}", "schema"),
    ({"schema": "ws7-other/1"}, "schema"), ({"schema": None}, "schema"),
    ({"run_id": "2026-12-09T0400-v1"}, "run_id"), ({"run_id": 7}, "run_id"),
    ({"target": "pipeline"}, "target"), ({"target": None}, "target"),
    ({"mode": "manual"}, "mode"), ({"mode": None}, "mode"),
    ({"scheduled_time": "2026-12-16T03:00:00Z"}, "scheduled_time"),     # not the claim's time
    ({"scheduled_time": None}, "scheduled_time")]


@pytest.mark.parametrize("wrong, label", WRONG_RECORDS, ids=lambda v: str(v)[:40])
def test_a_success_record_that_fails_its_check_never_counts_as_a_success(thursday, wrong, label):
    """Each field wrong in turn, against the claim. The partner (nothing wrong) is
    the silence of a run that succeeded."""
    world, target = thursday, "v1"
    run = seed_claim(world, target, WEDNESDAY)
    body = wrong if isinstance(wrong, bytes) else _success_record(run, target, change=wrong)
    world.s3.seed(store.success_key(run), body)
    assert world.check(target) == 0
    if label is None:
        assert world.sns.messages == []
        return
    assert check_lines(world)[1] == (f"run {run}: success record invalid ({label}); "
                                     f"it is not counted as a success")


def test_another_schemas_record_under_the_success_key_is_not_a_success(thursday):
    """A failed record's body under the success key: every field but the schema fits."""
    world, target = thursday, "v1"
    run = seed_claim(world, target, WEDNESDAY)
    world.s3.seed(store.success_key(run), _failed_record(run, target, entry.CRASHED, []))
    assert world.check(target) == 0
    assert "success record invalid (schema)" in check_lines(world)[1]


@pytest.mark.parametrize("wrong, label", WRONG_RECORDS + [
    ({"code": "Not A Code"}, "code"), ({"code": None}, "code"),
    ({"ids": "S1"}, "ids"), ({"ids": ["not an id"]}, "ids"), ({"ids": [7]}, "ids")],
    ids=lambda v: str(v)[:40])
def test_a_failed_record_that_fails_its_check_is_reported_invalid_and_is_still_a_failed_run(
        thursday, wrong, label):
    """Each field wrong in turn. A perfect success record is there too: the failed
    record still wins (change B), valid or not. The partner reports the code."""
    world, target = thursday, "v1"
    run = seed_claim(world, target, WEDNESDAY)
    world.s3.seed(store.success_key(run), _success_record(run, target))
    body = wrong if isinstance(wrong, bytes) else _failed_record(
        run, target, entry.CALL_FAILED, ["throttled"], change=wrong)
    world.s3.seed(store.failed_key(run), body)
    assert world.check(target) == 0
    if label is None:
        assert check_lines(world)[1] == f"failed run {run} code {entry.CALL_FAILED} ids throttled"
        return
    assert check_lines(world)[1] == f"failed run {run}: failed record invalid ({label})"


def test_the_records_a_real_run_writes_pass_the_checks_own_checks(ready, world):
    """The check's rules are held against what the entrypoint really writes: the claim
    and the success record of a good run, and the failed record of a failed one."""
    target = "v1"
    ready(target)
    assert world.run(target) == 0
    claim = world.s3.json(CLAIM_KEY[target])
    assert entry.claim_fault(claim, key=CLAIM_KEY[target], target=target) is None
    success = world.s3.json(store.success_key(RUN_ID[target]))
    assert entry.record_fault(success, schema=entry.SUCCESS_SCHEMA, claim=claim) is None
    assert entry.record_fault(success, schema=entry.FAILED_SCHEMA, claim=claim) == "schema"
    assert entry.claim_fault(claim, key=CLAIM_KEY["pipeline"], target="pipeline") == "target"

    world.clock.advance(7 * 86400)
    world.freeze(target, dict(W.invented_baseline(target, world.net, None), keys={}))
    assert world.run(target, "2026-12-23T04:00:00Z") == 1
    run = "2026-12-23T0400-v1"
    later = world.s3.json("ws7/claims/v1/2026-12-23")
    assert entry.record_fault(world.failed(run), schema=entry.FAILED_SCHEMA, claim=later) is None
    assert entry.record_fault(world.failed(run), schema=entry.FAILED_SCHEMA, claim=claim) == "run_id"


def test_a_check_that_cannot_read_s3_says_so_on_the_topic_and_exits_1(thursday):
    world, target = thursday, "v1"
    world.s3.script_list(500)
    assert world.check(target) == 1
    assert check_lines(world) == [f"WS7 check - target {target} - the check failed",
                                  f"code {wl.LEDGER_UNAVAILABLE}; see the task log"]
    assert world.cw.bodies == []


def test_a_check_that_cannot_reach_the_topic_exits_1(thursday):
    world, target = thursday, "v1"
    world.sns.script(500)
    assert world.check(target) == 1
    assert world.sns.messages == [] and len(world.sns.bodies) == 1      # tried once
    assert stopped_with(world, publish.ALERT_FAILED)


# ========================================================================== THE CLI ==

SCHEDULE_ARGS = ["--target", "v1", "--scheduled-time", WHEN]
CLI_SENTINEL = "WS7-CLI-SENTINEL-invented"


@pytest.mark.parametrize("argv", [
    [], ["scheduled"], ["scheduled", "--target", "v1"], ["scheduled", "--scheduled-time", WHEN],
    ["scheduled", "--target", "v2", "--scheduled-time", WHEN],
    ["scheduled", "--target", "manual", "--scheduled-time", WHEN],
    ["baseline", *SCHEDULE_ARGS], ["confirm", *SCHEDULE_ARGS], ["manual", *SCHEDULE_ARGS],
    ["resolve", "--run-id", "2026-12-09T0400-v1", "--basis", "upper_bound",
     "--settled-usd-micros", "1"],
    ["scheduled", *SCHEDULE_ARGS, "--mode", "manual"], ["scheduled", *SCHEDULE_ARGS, "--manual"],
    ["scheduled", *SCHEDULE_ARGS, "--yes"], ["scheduled", *SCHEDULE_ARGS, "--baseline-id", "b-1"],
    ["scheduled", *SCHEDULE_ARGS, "--endpoint-url", "http://127.0.0.1:1"],
    ["scheduled", *SCHEDULE_ARGS, "--bucket", "another-bucket"],
    ["scheduled", *SCHEDULE_ARGS, "--baseline", "/tmp/b.json"],
    ["check", *SCHEDULE_ARGS, "--lookback-days", "0"],
    ["scheduled", *SCHEDULE_ARGS, "--help"], ["--help"], ["scheduled", "-h"],
    ["--mode", "manual", "scheduled", *SCHEDULE_ARGS]], ids=lambda a: " ".join(a) or "none")
def test_any_other_command_line_is_a_usage_error_that_builds_and_sends_nothing(
        ready, world, argv, capsys):
    """There are two subcommands and two arguments. In 5a no manual mode exists, and
    nothing on the command line names a mode, an endpoint, a bucket, a path or a
    clock. The answer is one fixed line. The partner is every other test here, which
    uses the one valid form."""
    ready("v1")
    assert world.call(argv) == 2
    assert world.log.all() == [] and world.lines == []
    printed = capsys.readouterr()
    assert (printed.out, printed.err) == ("", entry.USAGE + "\n")


REFUSED_ARGV = {       # each kind of argument error, with a sentinel in what is refused
    "an unknown option": ["scheduled", *SCHEDULE_ARGS, f"--{CLI_SENTINEL}", "1"],
    "an unknown value": ["scheduled", *SCHEDULE_ARGS, CLI_SENTINEL],
    "a bad value": ["scheduled", "--target", CLI_SENTINEL, "--scheduled-time", WHEN],
    "a bad subcommand": [CLI_SENTINEL, *SCHEDULE_ARGS],
    "a missing argument": ["scheduled", "--scheduled-time", CLI_SENTINEL],
    "a missing value": ["check", "--target", "v1", f"--{CLI_SENTINEL}", "--scheduled-time"],
}
# What plain argparse prints for each. For something missing it prints its own
# sentence, which names no value; for the rest it quotes what it refused.
PLAIN_ARGPARSE_PRINTS = dict.fromkeys(REFUSED_ARGV, CLI_SENTINEL) | {
    "a missing argument": "the following arguments are required: --target",
    "a missing value": "argument --scheduled-time: expected one argument"}


@pytest.mark.parametrize("kind", list(REFUSED_ARGV))
def test_no_argparse_text_reaches_stdout_or_stderr(ready, world, capsys, kind):
    """Codex P2, PR #9. argparse's messages quote the argument they refuse. Here an
    invented sentinel is in the rejected argument, for each kind of error: it is in
    neither stream, and what is printed is the one fixed usage line.

    THE POSITIVE CONTROL: the same command line, given to the same parser built on
    plain `argparse.ArgumentParser`, does print it."""
    argv = REFUSED_ARGV[kind]
    ready("v1")
    assert world.call(argv) == 2
    printed = capsys.readouterr()
    assert (printed.out, printed.err) == ("", entry.USAGE + "\n")
    assert CLI_SENTINEL not in printed.out + printed.err and CLI_SENTINEL not in entry.USAGE
    assert world.log.all() == [] and world.lines == []

    with pytest.raises(SystemExit) as plain:
        entry.build_parser(argparse.ArgumentParser).parse_args(argv)
    assert plain.value.code == 2
    control = capsys.readouterr()
    assert PLAIN_ARGPARSE_PRINTS[kind] in control.err
    assert control.err != entry.USAGE + "\n"


def test_the_parser_alone_raises_on_a_usage_error_and_prints_nothing(capsys):
    """The first of two layers. The parser's own `error` raises, so nothing is printed
    even while both streams are open, and the exception carries nothing of what was
    refused."""
    for kind, argv in REFUSED_ARGV.items():
        with pytest.raises(entry._UsageError) as refused:
            entry.build_parser().parse_args(argv)
        assert refused.value.args == (), kind
        printed = capsys.readouterr()
        assert (printed.out, printed.err) == ("", ""), kind


def test_both_streams_are_discarded_while_the_command_line_is_parsed(world, capsys, monkeypatch):
    """The second layer. Even with a parser that prints as plain argparse does, `main`
    shows nothing of it: both streams are discarded while it parses."""
    build = entry.build_parser
    monkeypatch.setattr(entry, "build_parser", lambda: build(argparse.ArgumentParser))
    for kind, argv in REFUSED_ARGV.items():
        assert world.call(argv) == 2
        printed = capsys.readouterr()
        assert (printed.out, printed.err) == ("", entry.USAGE + "\n"), kind
    assert world.log.all() == [] and world.lines == []


@pytest.mark.parametrize("ending", list(ENDINGS))
def test_a_parse_that_ends_any_other_way_is_a_usage_error_too(world, capsys, monkeypatch, ending):
    monkeypatch.setattr(entry, "build_parser", _ends(ending))
    assert world.call(["scheduled", *SCHEDULE_ARGS]) == 2
    printed = capsys.readouterr()
    assert (printed.out, printed.err) == ("", entry.USAGE + "\n")
    assert world.log.all() == [] and world.lines == []


def test_a_scheduled_time_argparse_accepts_and_the_job_refuses_reaches_no_stream_either(
        ready, world, capsys):
    """`--scheduled-time` takes any string, so a bad one is refused by the job, not by
    argparse: a failed run, by a fixed code. The sentinel is in no stream, no log line
    and no request. The control: it really was the argument the job was given."""
    target = "v1"
    ready(target)
    given = []
    real = wl.parse_scheduled_time

    def parse(text):
        given.append(text)
        return real(text)
    wl.parse_scheduled_time, kept = parse, wl.parse_scheduled_time
    try:
        assert world.run(target, CLI_SENTINEL) == 1
    finally:
        wl.parse_scheduled_time = kept
    assert given == [CLI_SENTINEL]
    assert stopped_with(world, wl.SCHEDULED_TIME_INVALID)
    printed = capsys.readouterr()
    assert CLI_SENTINEL not in printed.out + printed.err + "".join(world.lines)
    assert not any(CLI_SENTINEL.encode() in body for body in world.cw.bodies)
    assert world.published() == {"RunRefused": [1]} and world.s3.requests == []


def test_the_real_command_line_refuses_a_manual_mode_without_touching_anything(tmp_path):
    """The script itself, in a child process with invented credentials and no route
    to AWS configured: a usage error exits 2 before any client is built, and prints
    the one fixed line and nothing of what it was given."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
           "AWS_ACCESS_KEY_ID": "ws7-test-invented-id", "AWS_SECRET_ACCESS_KEY": "ws7-test-invented-value",
           "AWS_EC2_METADATA_DISABLED": "true", "AWS_CONFIG_FILE": str(tmp_path / "none"),
           "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "none"),
           "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}
    for argv in (["baseline", *SCHEDULE_ARGS], [], ["scheduled", "--target", CLI_SENTINEL],
                 ["scheduled", "--help"]):
        done = subprocess.run([sys.executable, str(ROOT / "evals" / "ws7_entry.py"), *argv],
                              env=env, cwd=tmp_path, capture_output=True, text=True,
                              timeout=CHILD_LIMIT_S)
        assert done.returncode == 2, done.stderr
        assert (done.stdout, done.stderr) == ("", entry.USAGE + "\n")
    assert list(tmp_path.iterdir()) == []


def test_the_deps_a_run_takes_cannot_be_named_on_the_command_line():
    """What `Deps` holds (endpoints, clocks, folders) has no argument: each subcommand
    takes `--target` and `--scheduled-time` and nothing else, not even `--help`."""
    parser = entry.build_parser()
    modes = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert [a for a in parser._actions if a is not modes] == []
    assert sorted(modes.choices) == ["check", "scheduled"]
    for sub in modes.choices.values():
        assert sorted(o for a in sub._actions for o in a.option_strings) == [
            "--scheduled-time", "--target"]
        assert all(a.option_strings for a in sub._actions)      # no positional argument
    assert set(entry.USAGE.split()) >= {"{scheduled,check}", "--target", "--scheduled-time"}
