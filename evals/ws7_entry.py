#!/usr/bin/env python3
"""WS7 entrypoint: the scheduled run and the Thursday check (ADR-011 D7 to D11; slice 2
part 5a). The manual modes and the resolve command are part 5b and are not here.

    python evals/ws7_entry.py scheduled --target v1 --scheduled-time 2026-12-16T04:00:00Z
    python evals/ws7_entry.py check     --target v1 --scheduled-time 2026-12-17T10:00:00Z

The target and the scheduled time come from the schedule, as arguments. The bucket,
the alert topic, the commit and the image digest come from the task definition, as
environment variables (WS7_BUCKET, WS7_ALERT_TOPIC_ARN, WS7_GIT_COMMIT,
WS7_IMAGE_DIGEST). Nothing comes from the clock or from a default. There is no
argument for an endpoint, a path, a clock or a mode: the mode is the subcommand, and
`scheduled` fixes it in code.

THE ORDER OF A SCHEDULED RUN. Everything before the claim costs nothing and writes
nothing to S3.

 1. The config, the clients (one live Bedrock client among them, so a refused
    endpoint stops the run before any batch folder is written), the commit and the
    image digest.
 2. The distinct-scenarios precheck: no two scenarios may send the same first
    model request (`first_requests`).
 3. The match check (evals/ws7_manifest.py).
 4. The claim. A duplicate stops here.
 5. Admission: the reservation is booked in the month's ledger.
 6. The guard, made with `run_id=admission.run_id`, and with a deadline
    SETTLE_MARGIN_S before `Admission.deadline_at`.
 7. The pre-flight batch (one scenario), then the main batch (the corpus). No runner
    is started at or after the guard's deadline.
 8. SETTLE, as soon as the last runner returns and before anything is scored.
 9. The scorers, the run summary, the comparator.
10. The run's records, then the manifest, to S3.
11. The metrics; on a breach, the job's SNS message.
12. The success record.
13. `RunOk` = 1, last of all.

ONCE A RUN HAS FAILED IT SPENDS NOTHING MORE (the author's change A, 20 Nov 2026).
Under ruling e2 any call whose outcome is not `response`, and any refusal, fails the
run. The runner does not know that and goes on to the next scenario, and the
wrapper itself refuses the rest only when a call is unmetered. So every client the
runners get is wrapped once more here (`_StoppingClient`): once the guard's report
shows such a call, every further call in the run is refused with
`ws7_run_failed`, raised as the wrapper raises its own refusals. The run fails with
the FIRST failure's code and settles from the guard's report.

A FAILURE. Every handled failure is a `Stop` with a fixed code. In this order:
the ledger is settled (from the guard's report; `mark_crashed()` only when there is
no guard); what records exist are stored; the failed record the Thursday check reads
is written; `RunOk` = 0 and `RunRefused` = 1 are published. `RunOk` is published
only by the process that holds the scheduled claim: before the claim a process
cannot know it is not a duplicate, and a duplicate never publishes `RunOk`. A stop
before the claim publishes `RunRefused` alone and writes no S3 object.

A BREACH IS A SUCCESSFUL RUN (D9): it generated, scored, compared and stored. It
publishes its breach values, sends the job's message, writes its success record,
publishes `RunOk` = 1 and exits 0. A failed scheduled run sends no message of its
own; the Thursday check and the alarms report it.

THE PRE-FLIGHT'S GO/NO-GO (D7) is three things: no failed generation, only the
expected files, and a cost within its reservation. A v1 safety-net gate failure
on the pre-flight scenario is NOT one of them: it is quality evidence, and the
comparator judges it on the main batch. A runner's exit code 1 is accepted, from
either batch, only when the batch's own index explains it: a failed generation
or, for v1, a recorded gate failure. An exit code that disagrees with the index
is `ws7_runner_refused`.

THE COMPARATOR is called through `ws7_compare.main`, with an output path that is
new for the call and must not exist before it. Its exit code is checked BEFORE its
result is read. Exit 2 is a failed run: `ws7_no_baseline` (which publishes
`NoBaseline` = 1) or `ws7_compare_error`. A missing baseline or rules file is "no
baseline", decided here; the comparator is then not called.

THE THURSDAY CHECK makes no Bedrock call, no claim and no CloudWatch call. It
calls `Ledger.expire()` first, then reads the scheduled claim of the day
CHECK_LOOKBACK_DAYS before its own scheduled time, takes the run's ID FROM THE
CLAIM OBJECT (it never builds one), and reads that run's success and failed
records. It sends one message when there is anything to report.

THE LOG. One fixed line per stage: labels, IDs and counts. The runners' and the
scorers' own output is discarded, and no traceback is printed: either could quote a
record.

THE COMMAND LINE. An argument the parser refuses is answered with one fixed usage
line on stderr and exit code 2. argparse's own messages quote the argument they
refuse, so none of them is ever printed, and there is no `--help`.

Exit codes: 0 a successful run (a breach included), a duplicate, or a check that
completed; 1 a failed run or a failed check; 2 a usage error.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

_EVALS = Path(__file__).resolve().parent
for _p in (str(_EVALS.parent / "src" / "generate"), str(_EVALS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import score_assemble as SA  # noqa: E402
import score_batch as SB  # noqa: E402
import ws7_client as wc  # noqa: E402
import ws7_compare as compare  # noqa: E402
import ws7_ledger as wl  # noqa: E402
import ws7_manifest as wm  # noqa: E402
import ws7_publish as publish  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
import ws7_store as store  # noqa: E402
import ws7_summary as ws  # noqa: E402
from pipeline import StepContext, StepError  # noqa: E402
from ws7_store import SCHEDULED, Stop  # noqa: E402

rce, rpe = reserve.rce, reserve.rpe

BUCKET_VARIABLE = "WS7_BUCKET"
TOPIC_VARIABLE = "WS7_ALERT_TOPIC_ARN"

# The guard's deadline is this long before `Admission.deadline_at`, so that the last
# runner returns with time in hand to settle: a settle is several S3 requests, and
# one decided at or after `expires_at` is refused (docs/WS7-ENTRYPOINT.md, "The run's
# own deadline"). A margin, not a proof. It is here, not in ws7_config.json, because
# the config loader is strict and evals/ws7_reserve.py was not to be changed in the
# pull request that added it.
SETTLE_MARGIN_S = 300
# The Thursday check looks for the claim of the UTC day this many days before its
# own scheduled time: Wednesday's run, checked on Thursday.
CHECK_LOOKBACK_DAYS = 1

PREFLIGHT, MAIN = "preflight", "main"
SUCCESS_SCHEMA = "ws7-success/1"
FAILED_SCHEMA = "ws7-failed/1"
BASELINE_NAME = "ws7_baseline_{target}.json"       # frozen by pull request (slice 4)
RULES_NAME = "ws7_rules_{target}.json"

CONFIG_INVALID = "ws7_config_invalid"
CORPUS_UNREADABLE = "ws7_corpus_unreadable"
PRECHECK_FAILED = "ws7_precheck_failed"
SCENARIOS_NOT_DISTINCT = "ws7_scenarios_not_distinct"
PREFLIGHT_SCENARIO_UNKNOWN = "ws7_preflight_scenario_unknown"
RUN_FAILED = "ws7_run_failed"                      # a call refused because the run has failed
CALL_FAILED = "ws7_call_failed"
RUNNER_REFUSED = "ws7_runner_refused"
RUNNER_CRASHED = "ws7_runner_crashed"
RUNNER_STOPPED_EARLY = "ws7_runner_stopped_early"
PREFLIGHT_FAILED = "ws7_preflight_failed"
PREFLIGHT_FILES_UNEXPECTED = "ws7_preflight_files_unexpected"
PREFLIGHT_OVER_RESERVATION = "ws7_preflight_over_reservation"
GENERATION_FAILED = "ws7_generation_failed"
SCORER_STOPPED = "ws7_scorer_stopped"
NO_BASELINE = "ws7_no_baseline"
COMPARE_ERROR = "ws7_compare_error"
COMPARE_OUTPUT_EXISTS = "ws7_compare_output_exists"
EVIDENCE_UNSTORABLE = "ws7_evidence_unstorable"
EVIDENCE_UNAVAILABLE = "ws7_evidence_unavailable"
SUCCESS_RECORD_UNAVAILABLE = "ws7_success_record_unavailable"
CRASHED = "ws7_crashed"
RUNOK_PUBLISH_FAILED = "ws7_runok_publish_failed"
CHECK_FAILED = "ws7_check_failed"
CODES = (CONFIG_INVALID, CORPUS_UNREADABLE, PRECHECK_FAILED, SCENARIOS_NOT_DISTINCT,
         PREFLIGHT_SCENARIO_UNKNOWN, RUN_FAILED, CALL_FAILED, RUNNER_REFUSED, RUNNER_CRASHED,
         RUNNER_STOPPED_EARLY, PREFLIGHT_FAILED, PREFLIGHT_FILES_UNEXPECTED,
         PREFLIGHT_OVER_RESERVATION, GENERATION_FAILED, SCORER_STOPPED, NO_BASELINE,
         COMPARE_ERROR, COMPARE_OUTPUT_EXISTS, EVIDENCE_UNSTORABLE, EVIDENCE_UNAVAILABLE,
         SUCCESS_RECORD_UNAVAILABLE, CRASHED, RUNOK_PUBLISH_FAILED, CHECK_FAILED)

_CAPTURED = "ws7_precheck_captured"
IDS_SHOWN = 8                 # IDs named in one stop; the rest are counted


def _say(line: str) -> None:
    print(line, flush=True)


@dataclass
class Deps:
    """What a run takes from outside itself. Production uses the defaults. A test
    passes its own: the fakes' loopback endpoints (every client builder still refuses
    anything but http://127.0.0.1:<port>), clocks, a scratch folder and the folder
    the baseline is read from. NONE OF THESE CAN BE SET FROM THE COMMAND LINE."""
    environ: object = None                      # a mapping; None is os.environ
    endpoints: dict = field(default_factory=dict)   # service -> a loopback URL (tests only)
    wall: object = store.utc_now                # UTC wall clock: compared with other tasks'
    monotonic: object = time.monotonic          # the guard's clock
    runner_now: object = None                   # None: each runner reads its own London clock
    runs_dir: Path | None = None                # the task's scratch disk; None: a new temp folder
    baseline_dir: Path = _EVALS                 # where the frozen baseline and the rules are
    config_path: Path = reserve.CONFIG_PATH
    out: object = _say


# ------------------------------------------------------- what the runners are given

def no_wait(_seconds: float) -> None:
    """THE SLEEP THE RUNNERS GET (part 1 left this to part 5). Both runners call
    their sleep in one place only: before they retry a throttled call
    (src/generate/pipeline/converse.py:43, evals/run_cold_eval.py:340). WS7 never
    lets a retry through: a throttle fails the run, and every later call is refused.
    Waiting 20 to 30 seconds for a retry that will be refused would only use up the
    run's wall clock, so this sleep returns at once."""


def _never_confirm(_prompt: str) -> str:
    raise RuntimeError("a WS7 run is never asked to confirm")     # the argv carries --yes


@contextlib.contextmanager
def _quiet():
    """Discard what the runners, the scorers and the comparator print. Their lines
    are for a person at a terminal; some could quote a record."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def first_failure(report: dict) -> Stop | None:
    """Ruling e2 (18 Nov 2026): the first call in the guard's report whose outcome is
    not `response`, as the stop that fails the run. A refusal keeps its own code. A
    call that makes the run unmetered is `ws7_unmetered`. A call that did not run the
    model (a throttle, no connection, a request botocore refused) is
    `ws7_call_failed`, naming the outcome."""
    for row in report["calls"]:
        outcome = row["outcome"]
        if outcome == wc.RESPONSE:
            continue
        if outcome == wc.REFUSED:
            code = row["code"] if row["code"] in wc.CODES else CALL_FAILED
            return Stop(code, counts={"call": row["seq"]})
        if outcome in wc.NOT_RUN:
            return Stop(CALL_FAILED, ids=(outcome,), counts={"call": row["seq"]})
        label = outcome if outcome in wc.NO_RESPONSE or outcome == wc.USAGE_UNKNOWN else "unknown"
        return Stop(wc.UNMETERED, ids=(label,), counts={"call": row["seq"]})
    return None


class _Tripwire:
    """Whether the run has failed, read from the guard's own report."""

    def __init__(self, guard: wc.Guard):
        self._guard = guard
        self.tripped = False
        self.refused = 0          # calls refused here, after the first failure

    def look(self) -> bool:
        if not self.tripped:
            # "sent" is a call in flight; both runners send one call at a time.
            self.tripped = any(row["outcome"] not in (wc.RESPONSE, "sent")
                               for row in self._guard.report()["calls"])
        return self.tripped


class _StoppingClient:
    """What a runner is given: the guard's client, behind the tripwire. Once the run
    has failed, a call is refused before the guard sees it, with a `Ws7Refused`, which
    ends the generation exactly as the wrapper's own refusals do."""

    __slots__ = ("_inner", "_wire")

    def __init__(self, inner, wire: _Tripwire):
        self._inner = inner
        self._wire = wire

    def converse(self, **request):
        if self._wire.look():
            self._wire.refused += 1
            raise wc.Ws7Refused(RUN_FAILED)
        try:
            return self._inner.converse(**request)
        finally:
            self._wire.look()


def stopping_factory(guard: wc.Guard, wire: _Tripwire, *, endpoint_url: str | None = None):
    inner = wc.client_factory(guard, endpoint_url=endpoint_url)

    def factory(read_timeout=None):
        return _StoppingClient(inner(read_timeout), wire)
    return factory


# -------------------------------------------------- the distinct-scenarios precheck

class _Captured(StepError):
    """How the recorder refuses a request. It is not a ThrottlingException, so neither
    runner retries or sleeps."""


class _Recorder:
    """A plain Python object standing where a Bedrock client goes: no boto client and
    no socket. It keeps the fingerprint of each request it is given, the hash the
    wrapper itself uses (`ws7_client.fingerprint`), and refuses the request."""

    def __init__(self):
        self.fingerprints: list = []

    def converse(self, **request):
        self.fingerprints.append(wc.fingerprint(request))
        raise _Captured(_CAPTURED)


def _precheck_sleep(_seconds: float) -> None:
    """The sleep the precheck hands the runners' code. It must never be called: the
    recorder's refusal is not a throttle. If it is, the precheck stops."""
    raise _Captured("ws7_precheck_slept")


def first_requests(target: str, argv: list) -> dict:
    """Scenario ID -> the fingerprint of the first model request that scenario would
    send, for the scenarios `argv` selects, in the runner's order (Codex P3, PR #5).

    The request is built by the runner's own code, after its own and the pipeline's
    normalisation, so two scenarios whose notes differ only in ways that code
    normalises away have the same fingerprint here, as they would at the wrapper.

    - Pipeline: `run_pipeline` with no context for steps 5a, 6 and 7, so only step 1
      and the first model request (step 2) run.
    - v1: `run_generation`; the combined call is its first.

    Nothing is sent, nothing is written, and nothing sleeps."""
    runner = rpe if target == "pipeline" else rce
    try:
        with _quiet():
            args = runner.parse_args(list(argv))
        corpus, _sha = rce.load_corpus(Path(args.corpus))
        scenarios = rce.select_scenarios(corpus, args.scenarios, args.all)
        if not scenarios or not all(type(s.id) is str and compare._SCENARIO_RE.fullmatch(s.id)
                                    for s in scenarios):
            raise ValueError("a scenario ID is not an ID")
    except (Exception, SystemExit):               # whatever the loader made of the file
        raise Stop(CORPUS_UNREADABLE) from None
    v1 = None
    if target == "v1":
        try:
            system = rce.load_system_prompt(args.prompt)
            patient = rce.load_system_prompt(args.patient_prompt) \
                if args.patient_second_pass else None
        except Exception:
            raise Stop(PRECHECK_FAILED) from None
        v1 = rce.Config(system, {}, patient, {} if patient else None, args.prompt_caching,
                        args.gate)
    found: dict = {}
    for scenario in scenarios:
        recorder = _Recorder()
        try:
            if target == "pipeline":
                ctx = StepContext(bedrock=recorder, model_id=rpe.MODEL_ID,
                                  prompt_caching=args.prompt_caching, sleep=_precheck_sleep)
                _steps, failed, code, _errors = rpe.run_pipeline(ctx, scenario)
                ok = failed == "extract_facts" and code == _CAPTURED
            else:
                record = rce.run_generation(recorder, scenario, v1, batch_id="ws7-precheck", run=1,
                                            started_at="", sleep=_precheck_sleep)
                ok = record.get("error") == {"code": _Captured.__name__, "message": _CAPTURED}
        except Exception:
            ok = False
        if not ok or len(recorder.fingerprints) != 1:
            raise Stop(PRECHECK_FAILED, ids=(scenario.id,))
        found[scenario.id] = recorder.fingerprints[0]
    by_request: dict = {}
    for sid, digest in found.items():
        by_request.setdefault(digest, []).append(sid)
    same = sorted(sid for sids in by_request.values() if len(sids) > 1 for sid in sids)
    if same:
        raise Stop(SCENARIOS_NOT_DISTINCT, ids=tuple(same[:IDS_SHOWN]),
                   counts={"scenarios": len(same)})
    return found


# ------------------------------------------------------------- the files of a batch

_SCORE_FILES = {
    "v1": ("MEDS.md", "SAFETY_NET.md", "meds.json", "review_meds.md", "review_safety_net.md",
           "safety_net.json"),
    "pipeline": ("COMPOSE_CLINICAL.md", "MEDS.md", "PATIENT_VERSION.md", "SAFETY_NET.md",
                 "STEP2_ADVICE.md", "compose_clinical.json", "haz03.json", "meds.json",
                 "patient_version.json", "review_compose.md", "review_meds.md",
                 "review_patient.md", "review_safety_net.md", "safety_net.json",
                 "step2_advice.json", "assemble/ASSEMBLE.md", "assemble/assemble.json"),
}


def expected_files(target: str, scenarios: list, *, scored: bool) -> frozenset:
    """Every file a batch folder may hold, as paths relative to it: what the real
    runners write and, when `scored`, what the real scorers add. tests/test_ws7_entry.py
    pins this list against a batch the runners and scorers really made. A file that is
    not on it stops the run; it is never skipped."""
    files = {"manifest.json", "batch.json", "BATCH_SUMMARY.md"}
    files |= {f"r1/{sid}.json" for sid in scenarios}
    if target == "v1":
        files |= {f"r1/{sid}.md" for sid in scenarios} | {"r1/SUMMARY.md"}
    if scored:
        files |= {f"scores/{name}" for name in _SCORE_FILES[target]}
    return frozenset(files)


REQUIRED_FILES = ("manifest.json", "batch.json")


def files_in(batch_dir: Path) -> tuple:
    """(the regular files under a folder, as relative POSIX paths; how many other
    things are there: a link, a socket, anything that is not a plain file or folder)."""
    files, other = [], 0
    for root, dirs, names in os.walk(batch_dir, followlinks=False):
        for name in dirs + names:
            path = Path(root) / name
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                other += 1
            elif path.is_file():
                files.append(path.relative_to(batch_dir).as_posix())
    return sorted(files), other


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ a scheduled run

class _Scheduled:
    """One scheduled run. The fields fill in as it goes; `fail()` reads them to know
    how far it got."""

    def __init__(self, target: str, scheduled_time: str, deps: Deps):
        self.target, self.scheduled_time, self.deps = target, scheduled_time, deps
        self.environ = os.environ if deps.environ is None else deps.environ
        self.publisher = None
        self.st = None
        self.ledger = None
        self.who = None
        self.held = None                # the claim: only its holder publishes RunOk
        self.admission = None
        self.guard = None
        self.wire = None
        self.settled = None             # None: not tried. Otherwise the entry's state, or a code
        self.settle_stop = None         # the stop a settle that was tried ended in, if any
        self.batches: dict = {}         # label -> {"dir", "scenarios", "scored"}
        self.summary = None
        self.documents: dict = {}       # stored name -> bytes
        self.key_parts = None
        self.own = None
        self.reservation = None
        self.baseline_id = None
        self.waived = False
        self.stored = "none"

    def say(self, stage: str, **fields) -> None:
        parts = [f"ws7 entry mode=scheduled target={self.target} stage={stage}"]
        parts += [f"{name}={value}" for name, value in fields.items()]
        self.deps.out(" ".join(parts))

    # ------------------------------------------------------------- the run

    def run(self) -> int:
        try:
            self._before_the_claim()
            self._claim_and_admit()
            self._generate()
            result, exit_code = self._score_and_compare()
            self._store(strict=True)
            return self._succeed(result, exit_code)
        except wl.Duplicate as stop:
            return self._duplicate(stop)
        except Stop as stop:
            return self._fail(stop)
        except Exception as exc:        # noqa: BLE001 - the outermost handler
            # No traceback: one could quote a record. The class name is a name in code.
            self.say("crashed", error=type(exc).__name__)
            return self._fail(Stop(CRASHED))

    def _before_the_claim(self) -> None:
        deps, target = self.deps, self.target
        ends = deps.endpoints
        # CloudWatch first, so that every later stop can be counted.
        cloudwatch = store.build_client("cloudwatch", endpoint_url=ends.get("cloudwatch"))
        self.publisher = publish.Publisher(target, cloudwatch=cloudwatch)
        try:
            self.cfg = reserve.load_config(deps.config_path)
        except reserve.ReserveError:
            raise Stop(CONFIG_INVALID) from None
        self.publisher = publish.Publisher(
            target, cloudwatch=cloudwatch,
            sns=store.build_client("sns", endpoint_url=ends.get("sns")),
            topic=publish.topic_arn(self.environ.get(TOPIC_VARIABLE)))
        self.st = store.Store(store.build_client("s3", endpoint_url=ends.get("s3")),
                              self.environ.get(BUCKET_VARIABLE))
        ssm = store.build_client("ssm", endpoint_url=ends.get("ssm"))
        self.who = wm.identity(self.environ)
        self.plan = reserve.plan(target)
        try:
            # One live client now: a refused endpoint stops the run before any claim.
            wc.build_client(read_timeout_s=self.plan.read_timeout_s or rpe.READ_TIMEOUT_S,
                            connect_timeout_s=self.plan.connect_timeout_s,
                            endpoint_url=ends.get("bedrock"))
        except wc.EndpointRefused:
            raise Stop(wc.ENDPOINT_REFUSED) from None

        self.run_id = store.run_id(target, SCHEDULED, wl.parse_scheduled_time(self.scheduled_time))
        self.labels = {PREFLIGHT: f"{self.run_id}.{PREFLIGHT}", MAIN: f"{self.run_id}.{MAIN}"}
        cfg = self.cfg
        self.argv = {kind: wm.runner_argv(target, cfg, preflight=kind == PREFLIGHT,
                                          batch=self.labels[kind]) for kind in (PREFLIGHT, MAIN)}
        self.scenarios = list(first_requests(target, self.argv[MAIN]))
        if cfg.preflight_scenario not in self.scenarios:
            raise Stop(PREFLIGHT_SCENARIO_UNKNOWN)
        self.say("precheck", scenarios=len(self.scenarios))

        self.own = wm.match_check(ssm, target, self.argv[MAIN], cfg)
        if wm.own_values(target, self.argv[PREFLIGHT], cfg) != self.own:
            raise Stop(wm.RUNNER_ARGV_INVALID)      # the two batches would not run alike
        self.say("match")

    def _claim_and_admit(self) -> None:
        cfg, deps = self.cfg, self.deps
        held = wl.claim(self.st, cfg, target=self.target, mode=SCHEDULED,
                        scheduled_time=self.scheduled_time, clock=deps.wall)
        if held.run_id != self.run_id:
            raise Stop(wl.SCHEDULED_TIME_INVALID)
        self.held = held
        self.say("claim", run=held.run_id)
        self.margin = cfg.input_allowance_margin_percent
        self.reservation = reserve.reservation(self.target, len(self.scenarios) + 1, self.margin)
        self.ledger = wl.Ledger(self.st, cfg, clock=deps.wall)
        self.admission = self.ledger.admit(held, self.reservation["total_usd_micros"])
        # The guard's deadline is never later than deadline_at, and is SETTLE_MARGIN_S
        # before it. deadline_at is wall-clock; the guard's clock is monotonic.
        left = self.admission.seconds_to_deadline(store.read_clock(deps.wall)) - SETTLE_MARGIN_S
        self.deadline = deps.monotonic() + left
        self.guard = wc.Guard(self.plan, reservation_nanos=self.reservation["total_usd_nanos"],
                              margin_percent=self.margin, deadline=self.deadline,
                              clock=deps.monotonic, run_id=self.admission.run_id)
        self.wire = _Tripwire(self.guard)
        self.factory = stopping_factory(self.guard, self.wire,
                                        endpoint_url=deps.endpoints.get("bedrock"))
        self.runs_dir = Path(deps.runs_dir) if deps.runs_dir is not None \
            else Path(tempfile.mkdtemp(prefix="ws7-"))
        self.say("admitted", reserved_usd_micros=self.admission.reservation_usd_micros)

    def _dispatch(self, kind: str) -> tuple:
        """Start one runner. Returns (its exit code, whether it raised)."""
        label = self.labels[kind]
        # Dispatch stops at the deadline: no runner is started at or after it.
        if self.deps.monotonic() >= self.deadline:
            raise Stop(wc.DEADLINE, ids=(label,))
        scenarios = [self.cfg.preflight_scenario] if kind == PREFLIGHT else self.scenarios
        runner = rpe if self.target == "pipeline" else rce
        self.batches[label] = {"dir": self.runs_dir / label, "scenarios": scenarios,
                               "scored": False}
        self.guard.begin_batch(label, len(scenarios))
        exit_code, raised = None, False
        try:
            with _quiet():
                exit_code = runner.main(list(self.argv[kind]), client_factory=self.factory,
                                        now=self.deps.runner_now, sleep=no_wait,
                                        confirm=_never_confirm, runs_dir=self.runs_dir)
        except SystemExit as exc:                   # argparse's way of refusing an argv
            exit_code = exc.code
        except Exception:                           # noqa: BLE001
            raised = True
        finally:
            self.guard.end_batch()
        return exit_code, raised

    def _judge(self, kind: str, exit_code: object, raised: bool) -> dict:
        """What a runner's return means. Returns the batch's index (`batch.json`)."""
        label = self.labels[kind]
        failure = first_failure(self.guard.report())
        if failure is not None:                     # ruling e2: the first failure's code
            raise Stop(failure.code, ids=failure.ids + (label,),
                       counts=dict(failure.counts, calls_stopped=self.wire.refused))
        if raised:
            raise Stop(RUNNER_CRASHED, ids=(label,))
        index = _read_json(self.batches[label]["dir"] / "batch.json")
        # Anything but 0 or 1 (a canary window, an existing batch folder, a corpus it
        # could not read) is a runner that refused to start.
        if type(exit_code) is not int or exit_code not in (0, 1) or type(index) is not dict \
                or type(index.get("generations")) is not list:
            raise Stop(RUNNER_REFUSED, ids=(label,),
                       counts={"exit_code": exit_code if type(exit_code) is int else -1})
        if index.get("stopped_early") is not False:
            raise Stop(RUNNER_STOPPED_EARLY, ids=(label,))
        # 0: clean. 1: a failed generation or, for v1, a safety-net gate failure. Both
        # runners return 1 for other reasons too (run_cold_eval.py:663, a confirmation
        # that was not given), so 1 is accepted ONLY when the batch's own index explains
        # it, and 0 only when the index has nothing to explain (Codex P2, PR #9).
        failed, gates = self._failed_generations(index), self._gate_failures(index)
        if (exit_code == 1) != bool(failed or gates):
            raise Stop(RUNNER_REFUSED, ids=(label,),
                       counts={"exit_code": exit_code, "generations_failed": failed,
                               "gate_failures": gates})
        return index

    @staticmethod
    def _failed_generations(index: dict) -> int:
        return sum(1 for g in index["generations"]
                   if type(g) is not dict or g.get("error_code") is not None)

    @staticmethod
    def _gate_failures(index: dict) -> int:
        """Generations whose v1 safety-net gate did not pass, as the runner recorded
        them (`gate_ok` in batch.json). The pipeline runner records no gate."""
        return sum(1 for g in index["generations"]
                   if type(g) is dict and g.get("gate_ok") is False)

    def _generate(self) -> None:
        # ---- the pre-flight: one scenario, alone, as its own batch (D7). Its go/no-go
        # is three things: no failed generation, only the expected files, within its
        # reservation. A v1 gate failure is not one of them: it is quality evidence,
        # which the comparator judges on the main batch. Stopping here for it would
        # turn a safety-netting regression into "pre-flight failed", with no
        # comparison and no breach alarm.
        label = self.labels[PREFLIGHT]
        index = self._judge(PREFLIGHT, *self._dispatch(PREFLIGHT))
        if len(index["generations"]) != 1 or self._failed_generations(index):
            raise Stop(PREFLIGHT_FAILED, ids=(label,))
        batch_dir, scenarios = self.batches[label]["dir"], self.batches[label]["scenarios"]
        files, other = files_in(batch_dir)
        allowed = expected_files(self.target, scenarios, scored=False)
        unexpected = [name for name in files if name not in allowed]
        missing = [name for name in REQUIRED_FILES + (f"r1/{scenarios[0]}.json",)
                   if name not in files]
        if unexpected or missing or other:
            raise Stop(PREFLIGHT_FILES_UNEXPECTED, ids=(label,),
                       counts={"unexpected": len(unexpected) + other, "missing": len(missing)})
        spent = self.guard.batch_total_nanos(label)
        limit = reserve.reservation(self.target, 1, self.margin)["total_usd_nanos"]
        if spent > limit:
            raise Stop(PREFLIGHT_OVER_RESERVATION, ids=(label,),
                       counts={"spent_usd_micros": reserve.to_micros(spent),
                               "reserved_usd_micros": reserve.to_micros(limit)})
        self.say("preflight", cost_usd_micros=reserve.to_micros(spent))

        # ---- the main batch, and then the ledger, before anything else
        returned = self._dispatch(MAIN)
        try:
            self._settle()
        except Stop:
            pass                        # kept in settle_stop: the run's first failure comes first
        self.main_index = self._judge(MAIN, *returned)
        if self.settle_stop is not None:
            raise self.settle_stop
        if len(self.main_index["generations"]) != len(self.scenarios):
            raise Stop(RUNNER_STOPPED_EARLY, ids=(self.labels[MAIN],))
        self.say("generated", generations=len(self.scenarios))

    def _settle(self) -> None:
        """Settle from the guard's report, or mark the run crashed when there is no
        guard. Tried once: whatever comes of it, it is not tried again."""
        if self.admission is None or self.settled is not None:
            return
        self.settled = "failed"
        try:
            if self.guard is None:
                entry = self.ledger.mark_crashed(self.admission)
            else:
                entry = self.ledger.settle(self.admission, self.guard.report())
        except Stop as stop:
            self.settled, self.settle_stop = stop.code, stop
            raise
        self.settled = entry["state"] if entry["kind"] is None else entry["kind"]
        self.say("settled", state=self.settled, counted_usd_micros=entry["counted_usd_micros"])

    def _score_and_compare(self) -> tuple:
        label = self.labels[MAIN]
        batch_dir = self.batches[label]["dir"]
        failed = self._failed_generations(self.main_index)
        if self.target == "pipeline" and failed:
            # The assembly replay stops on any failed pipeline generation
            # (evals/score_assemble.py:202), so that batch can have no summary. Decided
            # here, from batch.json, before any scorer runs. The scorer is not changed.
            raise Stop(GENERATION_FAILED, ids=(label,), counts={"generations_failed": failed})
        steps = [("score_batch", lambda: SB.write_reports(batch_dir, SB.score_batch(batch_dir)))]
        if self.target == "pipeline":
            steps += [("score_assemble", lambda: SA.write(batch_dir / "scores" / "assemble",
                                                          SA.replay(batch_dir, False, None))),
                      ("haz03_check", lambda: ws.write_haz03(batch_dir))]
        for name, step in steps:
            try:
                with _quiet():
                    step()
            except Stop:
                raise
            except (Exception, SystemExit):         # a scorer that stops is a failed run (D2)
                raise Stop(SCORER_STOPPED, ids=(name,)) from None
        self.batches[label]["scored"] = True
        self.key_parts = ws.key_parts(batch_dir)
        self.waived = self.ledger.assurance_waived(self.held.month)
        report = self.guard.report()
        self.summary = ws.build_summary(batch_dir, own=self.own, report=report, batch_label=label,
                                        month_assurance_waived=self.waived)
        self.documents["summary.json"] = ws.render(self.summary)
        self.say("summary", metrics=len(self.summary["metrics"]))

        work = self.runs_dir / f"{self.run_id}.work"
        work.mkdir(parents=True, exist_ok=True)
        summary_path = work / "summary.json"
        summary_path.write_bytes(self.documents["summary.json"])
        result, exit_code = self._compare(summary_path, work / f"comparison.{label}.json")
        self.baseline_id = result["baseline_id"]
        self.say("compared", exit_code=exit_code)
        return result, exit_code

    def _compare(self, summary_path: Path, out: Path) -> tuple:
        """Call the comparator and decide from its EXIT CODE, before its result is
        read. Returns (the checked result, 0 or 1), or stops."""
        target = self.target
        baseline = Path(self.deps.baseline_dir) / BASELINE_NAME.format(target=target)
        rules = Path(self.deps.baseline_dir) / RULES_NAME.format(target=target)
        if not baseline.is_file() or not rules.is_file():
            raise Stop(NO_BASELINE, ids=("file_missing",))      # never a pass
        # A path that is new for this call. Something already there is not this
        # call's result, and it is never read.
        if out.exists() or out.is_symlink():
            raise Stop(COMPARE_OUTPUT_EXISTS)
        try:
            with _quiet():
                exit_code = compare.main(["--baseline", str(baseline), "--rules", str(rules),
                                          "--run", str(summary_path), "--out", str(out)])
        except (Exception, SystemExit):             # a usage error, or anything else
            raise Stop(COMPARE_ERROR, ids=("comparator_raised",)) from None
        # The exit code is the authority, and it is judged first.
        if type(exit_code) is not int or exit_code not in (0, 1, 2):
            raise Stop(COMPARE_ERROR, ids=("exit_code_unknown",))
        raw = None
        try:
            raw = out.read_bytes()
            result = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            raise Stop(COMPARE_ERROR, ids=("result_unreadable",),
                       counts={"exit_code": exit_code}) from None
        self.documents["comparison.json"] = raw
        if type(result) is not dict or result.get("schema") != compare.RESULT_SCHEMA \
                or type(result.get("exit_code")) is not int or result["exit_code"] != exit_code:
            raise Stop(COMPARE_ERROR, ids=("result_disagrees",), counts={"exit_code": exit_code})
        own_hash = (result.get("inputs") or {}).get("run_sha256") \
            if type(result.get("inputs")) is dict else None
        if exit_code == 2:
            # "No baseline" and "error" are never a pass. The result is read only to
            # tell the two apart.
            mismatch = result.get("key_mismatch")
            if result.get("status") == compare.NO_BASELINE and type(mismatch) is list \
                    and mismatch and all(k in compare.KEY_NAMES for k in mismatch):
                raise Stop(NO_BASELINE, ids=tuple(mismatch), counts={"keys": len(mismatch)})
            errors = result.get("errors")
            raise Stop(COMPARE_ERROR, ids=("comparator_error",),
                       counts={"errors": len(errors) if type(errors) is list else 0})
        try:
            publish.check_result(result, exit_code=exit_code)
        except Stop as stop:
            if stop.code == publish.PART_C_RULED:
                raise
            raise Stop(COMPARE_ERROR, ids=("result_disagrees",),
                       counts={"exit_code": exit_code}) from None
        if result["run_id"] != self.labels[MAIN] \
                or own_hash != _sha256(self.documents["summary.json"]):
            raise Stop(COMPARE_ERROR, ids=("result_not_this_runs",))
        return result, exit_code

    # ---------------------------------------------------------- storing

    def _put(self, key: str, body: bytes, kind: str) -> None:
        try:
            outcome = self.st.create(key, body, attempts=self.cfg.ledger_cas_attempts, kind=kind)
        except store.StoreError as exc:
            code = EVIDENCE_UNSTORABLE if exc.code == store.OBJECT_TOO_LARGE else EVIDENCE_UNAVAILABLE
            raise Stop(code, ids=(exc.code,)) from None
        if outcome != store.CREATED:
            raise Stop(EVIDENCE_UNAVAILABLE, ids=("key_taken",))

    def _store(self, *, strict: bool) -> None:
        """Store the run's records under its own prefix (D8): each batch folder, the
        guard's report, the summary, the comparison result and the manifest.

        Every file is decided before any is sent: a file that is not on the list of
        what the runners and scorers write, is not `.json` or `.md`, or is not a plain
        file, stops the run (`ws7_evidence_unstorable`). It is never skipped."""
        if self.stored != "none" or self.held is None:
            return
        self.stored = "not_finished"
        try:
            objects = self._store_all(strict)
        except Stop as stop:
            self.stored = stop.code
            raise
        self.stored = "stored"
        self.say("stored", objects=objects)

    def _store_all(self, strict: bool) -> int:
        run, uploads = self.held.run_id, []
        for label, batch in sorted(self.batches.items()):
            batch_dir, scenarios = batch["dir"], batch["scenarios"]
            if not batch_dir.is_dir():
                continue
            files, other = files_in(batch_dir)
            allowed = expected_files(self.target, scenarios, scored=batch["scored"])
            unexpected = [name for name in files
                          if name not in allowed or store.content_type(name) is None]
            if unexpected or other:
                raise Stop(EVIDENCE_UNSTORABLE, ids=(label,),
                           counts={"unexpected": len(unexpected) + other})
            for name in files:
                try:
                    body = (batch_dir / name).read_bytes()
                except OSError:
                    raise Stop(EVIDENCE_UNSTORABLE, ids=(label,)) from None
                uploads.append((store.evidence_key(SCHEDULED, run, "batches", label,
                                                   *name.split("/")),
                                body, store.content_type(name)))
        if self.guard is not None:
            self.documents.setdefault("guard_report.json", compare.render(self.guard.report()))
        for name in sorted(self.documents):
            uploads.append((store.evidence_key(SCHEDULED, run, name), self.documents[name],
                            store.JSON_TYPE))
        if any(len(body) > store.MAX_OBJECT_BYTES for _key, body, _kind in uploads):
            raise Stop(EVIDENCE_UNSTORABLE, ids=(store.OBJECT_TOO_LARGE,))
        for key, body, kind in uploads:
            self._put(key, body, kind)
        if self.key_parts is not None:
            manifest = wm.build_manifest(
                held=self.held, own=self.own, who=self.who, cfg=self.cfg,
                reservation=self.reservation, baseline_id=self.baseline_id,
                month_assurance_waived=self.waived, **self.key_parts)
            wm.write_manifest(self.st, manifest, attempts=self.cfg.ledger_cas_attempts)
        elif strict:
            raise Stop(wm.MANIFEST_INVALID)
        return len(uploads) + (self.key_parts is not None)

    # ------------------------------------------------------- the endings

    def _succeed(self, result: dict, exit_code: int) -> int:
        deps, run = self.deps, self.held.run_id
        prefix = store.run_prefix(SCHEDULED, run)
        values = [(publish.NO_BASELINE, 0)] + publish.breach_values(result) \
            + publish.run_counts(self.summary, self.target)
        self.publisher.put(values)
        breaches = sum(row["breach"] for row in result["comparisons"])
        if exit_code == 1:
            subject, text = publish.breach_message(
                target=self.target, run_id=run, result=result, keys=self.summary["keys"],
                bucket=self.st.bucket, prefix=prefix, now=store.read_clock(deps.wall))
            self.publisher.alert(subject, text)
        record = {"schema": SUCCESS_SCHEMA, "run_id": run, "target": self.target,
                  "scheduled_time": self.held.scheduled_time, "status": result["status"],
                  "breaches": breaches, "baseline_id": result["baseline_id"],
                  "summary_sha256": _sha256(self.documents["summary.json"]),
                  "comparison_sha256": _sha256(self.documents["comparison.json"]),
                  "evidence": prefix, "git_commit": self.who.git_commit,
                  "image_digest": self.who.image_digest,
                  "finished_at": store.utc_text(store.read_clock(deps.wall))}
        try:
            outcome = self.st.create(store.success_key(run), compare.render(record),
                                     attempts=self.cfg.ledger_cas_attempts)
        except store.StoreError as exc:
            raise Stop(SUCCESS_RECORD_UNAVAILABLE, ids=(exc.code,)) from None
        if outcome != store.CREATED:
            raise Stop(SUCCESS_RECORD_UNAVAILABLE, ids=("key_taken",))
        self.say("success", status=result["status"], breaches=breaches)
        # The last act. If it fails the success record stands, and no failed record is
        # written beside it: the backstop alarm may then fire, which errs towards an alert.
        try:
            self.publisher.put([(publish.RUN_OK, 1)])
        except Stop as stop:
            self.say("stop", code=RUNOK_PUBLISH_FAILED, cause=stop.code)
            return 1
        return 0

    def _duplicate(self, stop: Stop) -> int:
        """A duplicate delivery: it reserved and spent nothing. It counts one
        duplicate and publishes nothing else, and never `RunOk`."""
        self.deps.out(stop.line())
        try:
            self.publisher.put([(publish.DUPLICATE, 1)])
        except Stop as failed:
            self.say("stop", code=failed.code)
            return 1
        return 0

    def _fail(self, stop: Stop) -> int:
        """A failed run. Each step is tried whatever became of the one before."""
        self.deps.out(stop.line())
        also = []
        # 1. The ledger, first.
        try:
            self._settle()
        except Stop as failed:
            also.append(failed.code)
            self.deps.out(failed.line())
        except Exception as exc:                    # noqa: BLE001
            also.append(CRASHED)
            self.say("crashed", error=type(exc).__name__, during="settle")
        if self.settle_stop is not None and self.settle_stop is not stop \
                and self.settle_stop.code not in also:
            # The settle was tried when the last runner returned, and it was refused.
            also.append(self.settle_stop.code)
            self.deps.out(self.settle_stop.line())
        # 2. What records exist.
        try:
            self._store(strict=False)
        except Stop as failed:
            self.deps.out(failed.line())
        except Exception as exc:                    # noqa: BLE001
            self.stored = CRASHED
            self.say("crashed", error=type(exc).__name__, during="store")
        # 3. The failed record: only for a run that holds its claim.
        if self.held is not None:
            record = {"schema": FAILED_SCHEMA, "run_id": self.held.run_id, "target": self.target,
                      "scheduled_time": self.held.scheduled_time, "code": stop.code,
                      "ids": list(stop.ids), "counts": dict(sorted(stop.counts.items())),
                      "also": also, "ledger": self.settled or "not_admitted",
                      "evidence": self.stored,
                      "git_commit": self.who.git_commit, "image_digest": self.who.image_digest,
                      "failed_at": store.utc_text(store.read_clock(self.deps.wall))}
            try:
                outcome = self.st.create(store.failed_key(self.held.run_id), compare.render(record),
                                         attempts=self.cfg.ledger_cas_attempts)
                self.say("failed_record", written=int(outcome == store.CREATED))
            except Stop as failed:
                self.deps.out(failed.line())
            except Exception as exc:                # noqa: BLE001
                self.say("crashed", error=type(exc).__name__, during="failed_record")
        # 4. The metrics. RunOk = 0 only from the claim's holder.
        if self.publisher is not None:
            values = [(publish.RUN_REFUSED, 1)]
            try:
                if self.held is not None:
                    values.append((publish.RUN_OK, 0))
                if stop.code == NO_BASELINE:
                    values.append((publish.NO_BASELINE, 1))
                if self.summary is not None:
                    values += publish.run_counts(self.summary, self.target)
                elif self.guard is not None:
                    values += publish.guard_counts(self.guard.report())
                self.publisher.put(values)
            except Stop as failed:
                self.deps.out(failed.line())
            except Exception as exc:                # noqa: BLE001
                self.say("crashed", error=type(exc).__name__, during="publish")
        return 1


# ------------------------------------------------------------- the Thursday check

def _listed(st: store.Store, key: str) -> bool:
    """Is exactly this key there? From a listing, never from a GET error."""
    return key in st.list_keys(key)


def _claimed_run(st: store.Store, key: str, target: str) -> str | None:
    """The run ID in the claim object at `key`, or None when the object is not a
    scheduled claim for this target. The ID is the claim's own: it is never built
    here, because the run's UTC hour moves with the London clock."""
    try:
        claim = json.loads(st.get(key)[0].decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if type(claim) is not dict or claim.get("schema") != wl.CLAIM_SCHEMA \
            or claim.get("target") != target or claim.get("mode") != SCHEDULED:
        return None
    run, when = claim.get("run_id"), store.parse_utc(claim.get("scheduled_time"))
    if type(run) is not str or not store.ID_RE.fullmatch(run) or when is None \
            or store.claim_key(target, SCHEDULED, when) != key:
        return None
    return run


def _failed_line(st: store.Store, run: str) -> str:
    """One line for a failed run, from its failed record: the code, and the IDs the
    stop carried. For a run blocked by an unresolved entry, those are the entries."""
    try:
        record = json.loads(st.get(store.failed_key(run))[0].decode("utf-8"))
    except (ValueError, RecursionError):
        record = None
    code = record.get("code") if type(record) is dict else None
    ids = record.get("ids") if type(record) is dict else None
    if type(record) is not dict or record.get("schema") != FAILED_SCHEMA \
            or record.get("run_id") != run or type(code) is not str \
            or not store._LABEL_RE.fullmatch(code) or type(ids) is not list \
            or not all(type(i) is str and store.ID_RE.fullmatch(i) for i in ids):
        return f"failed run {run}: its failed record cannot be read"
    line = f"failed run {run} code {code}"
    if code == wl.BLOCKED_UNRESOLVED and ids:
        return line + ": blocked by unresolved " + ", ".join(ids[:IDS_SHOWN])
    return line + (" ids " + ", ".join(ids[:IDS_SHOWN]) if ids else "")


def check(target: str, scheduled_time: str, deps: Deps) -> int:
    """The Thursday check (D9): did the scheduled run succeed? No Bedrock call, no
    claim, no CloudWatch call. It reads S3 and publishes to the topic."""
    environ = os.environ if deps.environ is None else deps.environ

    def say(stage: str, **fields) -> None:
        deps.out(" ".join([f"ws7 entry mode=check target={target} stage={stage}"]
                          + [f"{name}={value}" for name, value in fields.items()]))

    publisher = None
    try:
        publisher = publish.Publisher(
            target, sns=store.build_client("sns", endpoint_url=deps.endpoints.get("sns")),
            topic=publish.topic_arn(environ.get(TOPIC_VARIABLE)))
        try:
            cfg = reserve.load_config(deps.config_path)
        except reserve.ReserveError:
            raise Stop(CONFIG_INVALID) from None
        st = store.Store(store.build_client("s3", endpoint_url=deps.endpoints.get("s3")),
                         environ.get(BUCKET_VARIABLE))
        day = wl.parse_scheduled_time(scheduled_time) - timedelta(days=CHECK_LOOKBACK_DAYS)
        ledger = wl.Ledger(st, cfg, clock=deps.wall)
        # expire() first: a run that died on Wednesday is abandoned by this call, and
        # so is reported as unresolved by the next one.
        ledger.expire()
        unresolved = ledger.unresolved()

        findings = []
        key = store.claim_key(target, SCHEDULED, day)
        if not _listed(st, key):
            findings.append(publish.NO_CLAIM_LINE)
        else:
            run = _claimed_run(st, key, target)
            if run is None:
                findings.append("the claim object cannot be read as a scheduled claim")
            elif _listed(st, store.failed_key(run)):
                # A failed record wins: if a success record is there too, it is a failed run.
                findings.append(_failed_line(st, run))
            elif not _listed(st, store.success_key(run)):
                findings.append(f"run {run} started and did not finish: no success record")
        for month, run, kind in unresolved:
            findings.append(f"unresolved entry {run} ({kind}, month {month}): "
                            f"it blocks every scheduled run until it is resolved")
        month = f"{day:%Y-%m}"
        if ledger.assurance_waived(month):
            findings.append(f"month {month}: assurance waived")
        say("checked", findings=len(findings))
        if findings:
            subject, text = publish.check_message(
                target=target, day=f"{day:%Y-%m-%d}", findings=findings, bucket=st.bucket,
                now=store.read_clock(deps.wall))
            publisher.alert(subject, text)
            say("alert_sent")
        return 0
    except Stop as stop:
        deps.out(stop.line())
        code = stop.code
    except Exception as exc:                        # noqa: BLE001 - no traceback: see the module docstring
        say("crashed", error=type(exc).__name__)
        code = CRASHED
    # The check itself failed. Say so on the topic if it can still be reached.
    if publisher is not None and code not in (publish.ALERT_FAILED, publish.ALERT_INVALID):
        try:
            subject = f"WS7 check - target {target} - the check failed"
            publisher.alert(subject, f"{subject}\ncode {code}; see the task log")
        except Stop as failed:
            deps.out(failed.line())
        except Exception as exc:                    # noqa: BLE001
            say("crashed", error=type(exc).__name__, during="alert")
    return 1


# --------------------------------------------------------------------------- CLI

USAGE = ("usage: ws7_entry.py {scheduled,check} --target {pipeline,v1} "
         "--scheduled-time <ISO 8601 with a zone>")


class _UsageError(Exception):
    """The command line was refused. It carries nothing: argparse's message quotes
    the argument it refused."""


class _Parser(argparse.ArgumentParser):
    """A parser that prints nothing. Every refusal in argparse goes through `error`,
    whose message quotes the argument that was refused (`invalid choice: '...'`,
    `unrecognized arguments: ...`). Here it raises instead, and `main` prints one
    fixed line."""

    def error(self, message):           # noqa: ARG002 - the message is never used
        raise _UsageError


def build_parser(parser_class=_Parser) -> argparse.ArgumentParser:
    """The command line: two subcommands, two arguments each, and no `--help`.
    `parser_class` is here for one test, which shows what plain argparse prints."""
    parser = parser_class(prog="ws7_entry.py", add_help=False)
    modes = parser.add_subparsers(dest="mode", required=True, parser_class=parser_class)
    for name in ("scheduled",       # the weekly run: generate, score, compare, store, publish
                 "check"):          # the Thursday check: no Bedrock call and no claim
        sub = modes.add_parser(name, add_help=False)
        sub.add_argument("--target", required=True, choices=store.TARGETS)
        # the schedule's own scheduled time, ISO 8601 with a zone
        sub.add_argument("--scheduled-time", required=True)
    return parser


def scheduled(target: str, scheduled_time: str, deps: Deps) -> int:
    return _Scheduled(target, scheduled_time, deps).run()


def main(argv: list | None = None, deps: Deps | None = None) -> int:
    try:
        with _quiet():                  # whatever argparse might print, none of it is shown
            args = build_parser().parse_args(argv)
    except (Exception, SystemExit):     # noqa: BLE001 - a usage error: nothing is built or sent
        try:
            print(USAGE, file=sys.stderr, flush=True)
        except Exception:               # noqa: BLE001 - a closed stderr changes nothing
            pass
        return 2
    deps = deps or Deps()
    if args.mode == "check":
        return check(args.target, args.scheduled_time, deps)
    return scheduled(args.target, args.scheduled_time, deps)


if __name__ == "__main__":
    sys.exit(main())
