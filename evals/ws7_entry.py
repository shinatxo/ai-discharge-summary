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

CODE THE JOB DOES NOT OWN ENDS ONLY BY RETURNING (Codex P2, PR #9, re-check). The
runners, the scorers and the comparator are called in this process. Whatever one
of them RAISES is a crash of that code, with a fixed code: any exception, a
`KeyboardInterrupt`, and a `SystemExit` with any code (0, 1, 2, None, a string). A
raise is never read as an exit code and never as a pass. THAT INCLUDES WS7's OWN
EXCEPTION TYPES: a `Stop` or a `Duplicate` raised by a runner, a scorer or the
comparator is that call's fixed code, like any other raise. Only the job's own
code may raise a `Stop` the run acts on, and a `Duplicate` is acted on only when
the claim itself raises it. The same holds at the
outermost handler of a run, of a duplicate and of the check: each is for
`BaseException`, so nothing leaves the job without the failure path having run.

ONCE A RUN HAS FAILED IT SPENDS NOTHING MORE (the author's change A, 20 Nov 2026).
Under ruling e2 any call whose outcome is not `response`, and any refusal, fails the
run. The runner does not know that and goes on to the next scenario, and the
wrapper itself refuses the rest only when a call is unmetered. So every client the
runners get is wrapped once more here (`_StoppingClient`): once the guard's report
shows such a call, every further call in the run is refused with
`ws7_run_failed`, raised as the wrapper raises its own refusals. The run fails with
the FIRST failure's code and settles from the guard's report.

A FAILURE. Every handled failure is a `Stop` with a fixed code. The failure path
(`_fail`) is five steps, in this order, EACH TRIED ON ITS OWN: whatever becomes of
one, the next is still tried (Codex P2, PR #9). That holds for anything a step
raises, of any type: the handlers are for `BaseException`, because `SystemExit`
and `KeyboardInterrupt` do not derive from `Exception`.

 1. The ledger is settled (from the guard's report; `mark_crashed()` only when
    there is no guard).
 2. What records exist are stored: the guard's report first, then every file the
    runners and scorers write. A file that is not on that list is never uploaded;
    here it does not stop the rest, and the failed record counts it.
 3. The failed record the Thursday check reads is built (the clock read included)
    and written.
 4. `RunRefused` = 1 and `RunOk` = 0 are published.
 5. The run's counters are published, unless CloudWatch already has them.

`RunOk` is published only by the process that holds the scheduled claim: before
the claim a process cannot know it is not a duplicate, and a duplicate never
publishes `RunOk`. A stop before the claim publishes `RunRefused` alone and
writes no S3 object.

EACH METRIC IS PUBLISHED AT MOST ONCE PER RUN. The publisher keeps the names
CloudWatch acknowledged. A step that fails after the run's counters and breach
values were accepted (the breach message, the success record, `RunOk` = 1) sends
only the failure's own markers. What was delivered before the failure stands: the
counters, the breach values and, if it was sent, the breach message.

A BREACH IS A SUCCESSFUL RUN (D9): it generated, scored, compared and stored. It
publishes its breach values, sends the job's message, writes its success record,
publishes `RunOk` = 1 and exits 0. A failed scheduled run sends no message of its
own; the Thursday check and the alarms report it.

IF `RunOk` = 1 IS NOT ACKNOWLEDGED the run fails like any other run that holds its
claim, with `ws7_runok_publish_failed`: the failed record is written beside the
success record (the Thursday check then reports a failed run) and `RunOk` = 0 is
tried.

THE PRE-FLIGHT'S GO/NO-GO (D7) is three things: no failed generation, only the
expected files, and a cost within its reservation. A v1 safety-net gate failure
on the pre-flight scenario is NOT one of them: it is quality evidence, and the
comparator judges it on the main batch. A runner's exit code 1 is accepted, from
either batch, only when the batch's own index explains it, by the fields THAT
TARGET'S runner writes, each read by exact type: a failed generation (`error_code`
a non-empty string) or, for v1 only, a gate failure (`gate_ok` exactly `false`).
The pipeline has no safety-net gate, so nothing but a failed generation explains
a pipeline 1. An exit code that disagrees with the index, or an index with a
field of any other type, is `ws7_runner_refused`.

THE COMPARATOR is called through `ws7_compare.main`, with an output path that is
new for the call and must not exist before it. Its exit code is checked BEFORE its
result is read. Exit 2 is a failed run: `ws7_no_baseline` (which publishes
`NoBaseline` = 1) or `ws7_compare_error`. A missing baseline or rules file is "no
baseline", decided here; the comparator is then not called.

THE THURSDAY CHECK makes no Bedrock call, no claim and no CloudWatch call. It
calls `Ledger.expire()` first, then reads the scheduled claim of the day
CHECK_LOOKBACK_DAYS before its own scheduled time, takes the run's ID FROM THE
CLAIM OBJECT (it never builds one from its own scheduled time), and reads that
run's success and failed records. IT TRUSTS NONE OF THE THREE AS READ: every field
of each is checked (its type, its pattern or range, its agreement with the claim
and the occurrence; an unknown field is refused), by ONE VALIDATOR PER OBJECT
THAT ITS WRITER ALSO RUNS BEFORE THE PUT (`ws7_ledger.claim_fault`,
`success_fault`, `failed_fault`). So the job never writes a record its own check
would reject. One that fails is reported as invalid, with a fixed label, and
never counts as a success. It sends one message when there is anything to report.

THE LOG. One fixed line per stage: labels, IDs and counts. The runners' and the
scorers' own output is discarded, and no traceback is printed: either could quote a
record. BUILDING A DIAGNOSTIC NEVER RAISES AND NEVER CARRIES TEXT (Codex P2, PR #9,
second re-check): every value in a stop or a log line is a fixed label, a bounded
count or a string that matched an ID pattern. An exception is logged by a fixed
label for where it happened (`during=run`, `during=settle`, ...), never by its
class name or message. A runner's return code is used only once it is an integer
from 0 to 255. Every line is built inside the best-effort boundary. LOGGING IS BEST
EFFORT: a log stream that fails never changes what a run
does, whatever it raises.

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
import math
import os
import re
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
EXIT_CODE_MAX = 255           # what a process can return; a runner's code beyond it is not one
COUNT_MAX = 2 ** 63 - 1       # a count in a record: far above any real one, and printable
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
SUCCESS_RECORD_INVALID = "ws7_success_record_invalid"
FAILED_RECORD_INVALID = "ws7_failed_record_invalid"
CRASHED = "ws7_crashed"
RUNOK_PUBLISH_FAILED = "ws7_runok_publish_failed"
CHECK_FAILED = "ws7_check_failed"
CODES = (CONFIG_INVALID, CORPUS_UNREADABLE, PRECHECK_FAILED, SCENARIOS_NOT_DISTINCT,
         PREFLIGHT_SCENARIO_UNKNOWN, RUN_FAILED, CALL_FAILED, RUNNER_REFUSED, RUNNER_CRASHED,
         RUNNER_STOPPED_EARLY, PREFLIGHT_FAILED, PREFLIGHT_FILES_UNEXPECTED,
         PREFLIGHT_OVER_RESERVATION, GENERATION_FAILED, SCORER_STOPPED, NO_BASELINE,
         COMPARE_ERROR, COMPARE_OUTPUT_EXISTS, EVIDENCE_UNSTORABLE, EVIDENCE_UNAVAILABLE,
         SUCCESS_RECORD_UNAVAILABLE, SUCCESS_RECORD_INVALID, FAILED_RECORD_INVALID, CRASHED,
         RUNOK_PUBLISH_FAILED, CHECK_FAILED)

_CAPTURED = "ws7_precheck_captured"

# THE STOPS THE JOB RENDERS AND ACTS ON (Codex, PR #9, third re-check). A `Stop` is an
# ordinary Python class: a subclass can give itself a `line()` that raises, or that
# returns any text. So what the job has caught is never rendered, and no method of it is
# ever called. `own_stop` turns it into a NEW `Stop` of the exact class, or refuses it.
#
# The job's own three stop classes, each BY EXACT TYPE. A subclass of any of them is not
# one of them: `ws7_store.Stop`, `ws7_store.StoreError` (what the store raises) and
# `ws7_ledger.Duplicate` (what the claim raises).
OWN_STOP_TYPES = (Stop, store.StoreError, wl.Duplicate)
# Every fixed code a stop of the job's can carry: each module's own list, and the one
# code of ws7_client's that is raised as a stop here and is not in its list of refusals.
ALL_CODES = frozenset(CODES + publish.CODES + store.CODES + wl.CODES + wm.CODES + ws.CODES
                      + wc.CODES + (wc.ENDPOINT_REFUSED,))
IDS_MAX = 256                 # IDs in one stop: far more than any stop names
NOT_A_JOB_STOP = "not_a_job_stop"


def own_stop(caught: object) -> Stop | None:
    """`caught`, as a stop the job may render and act on, or None.

    It is one only if it is EXACTLY one of the job's own stop classes (not a subclass),
    its code is one of the job's fixed codes, every ID matches the ID pattern, and
    every count is a bounded whole number under a fixed label. The fields are read
    from the instance's own dictionary: no property and no method of the object is
    used. What is returned is a new `Stop`, built here from those checked fields."""
    if type(caught) not in OWN_STOP_TYPES:
        return None
    fields = vars(caught)
    code, ids, counts = fields.get("code"), fields.get("ids"), fields.get("counts")
    if type(code) is not str or code not in ALL_CODES:
        return None
    if type(ids) is not tuple or len(ids) > IDS_MAX \
            or not all(type(i) is str and store.ID_RE.fullmatch(i) for i in ids):
        return None
    if type(counts) is not dict or not all(
            type(k) is str and store._LABEL_RE.fullmatch(k)
            and type(v) is int and -COUNT_MAX <= v <= COUNT_MAX for k, v in counts.items()):
        return None
    return Stop(code, ids=ids, counts=counts)


def stop_line(stop: Stop) -> str:
    """One log line for a stop that `own_stop` made, BUILT HERE from its checked fields:
    the code, the IDs and the counts. The stop's own method is not used."""
    parts = [f"ws7 stop code={stop.code}"]
    if stop.ids:
        parts.append("ids=" + ",".join(stop.ids))
    parts.extend(f"{name}={value}" for name, value in sorted(stop.counts.items()))
    return " ".join(parts)
IDS_SHOWN = 8                 # IDs named in one stop; the rest are counted


def _say(line: str) -> None:
    print(line, flush=True)


def best_effort(out):
    """`out`, made unable to raise. LOGGING IS BEST EFFORT EVERYWHERE: a closed pipe
    or a full disk under the log must never stop a settle, a failed record or a
    metric that would have followed the line.

    It catches `BaseException`, not `Exception`: `SystemExit` and `KeyboardInterrupt`
    do not derive from `Exception`, and a sink that raised one of them would
    otherwise end the failure path early (Codex P2, PR #9).

    BUILDING THE LINE IS INSIDE THE BOUNDARY TOO (Codex P2, PR #9, second re-check).
    `line` is a string, or a function that builds one, and the function is called
    here, inside the `try`. So a line that cannot be built (a count too long for
    Python to print, say) costs that one line and nothing else."""
    def emit(line) -> None:
        try:
            out(line() if callable(line) else line)
        except BaseException:           # noqa: BLE001 - whatever the stream raised, the run goes on
            pass
    return emit


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


SENT = "sent"        # the guard's word for a call that has no final outcome yet


def first_failure(report: dict) -> Stop | None:
    """Ruling e2 (18 Nov 2026): the first call in the guard's report whose outcome is
    not `response`, as the stop that fails the run. A refusal keeps its own code. A
    call that makes the run unmetered is `ws7_unmetered`. A call that did not run the
    model (a throttle, no connection, a request botocore refused) is
    `ws7_call_failed`, naming the outcome. A call still `sent` is
    `ws7_report_incomplete`."""
    for row in report["calls"]:
        outcome = row["outcome"]
        if outcome == wc.RESPONSE:
            continue
        if outcome == SENT:
            # Sent, and never given an outcome: the accounting of that call did not
            # finish. The run's cost is unknown; the ledger will not settle from this.
            return Stop(wl.REPORT_INCOMPLETE, counts={"call": row["seq"]})
        if outcome == wc.REFUSED:
            code = row["code"] if row["code"] in wc.CODES else CALL_FAILED
            return Stop(code, counts={"call": row["seq"]})
        if outcome in wc.NOT_RUN:
            return Stop(CALL_FAILED, ids=(outcome,), counts={"call": row["seq"]})
        label = outcome if outcome in wc.NO_RESPONSE or outcome == wc.USAGE_UNKNOWN else "unknown"
        return Stop(wc.UNMETERED, ids=(label,), counts={"call": row["seq"]})
    return None


class _Tripwire:
    """Whether the run has failed, read from the guard's own report.

    It is looked at only between calls: before one is sent, and after one has returned
    or raised (both runners send one call at a time). So at every look each call
    should have its final outcome and the totals should be the sum of the rows. A row
    still `sent`, or a report the ledger would not settle from, means the accounting
    of a call did not finish. The run's cost is then unknown, and it sends no further
    call (Codex P1, PR #9, second re-check). A report that cannot be read at all is
    treated the same way: only what can be checked is trusted."""

    def __init__(self, guard: wc.Guard):
        self._guard = guard
        self.tripped = False
        self.refused = 0          # calls refused here, after the first failure

    def look(self) -> bool:
        if not self.tripped:
            try:
                report = self._guard.report()
                self.tripped = any(row["outcome"] != wc.RESPONSE for row in report["calls"]) \
                    or wl.report_fault(report) is not None
            except BaseException:               # noqa: BLE001 - a report that cannot be read
                self.tripped = True
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
    except BaseException:                         # noqa: BLE001 - whatever the loader raised
        raise Stop(CORPUS_UNREADABLE) from None
    v1 = None
    if target == "v1":
        try:
            system = rce.load_system_prompt(args.prompt)
            patient = rce.load_system_prompt(args.patient_prompt) \
                if args.patient_second_pass else None
        except BaseException:                     # noqa: BLE001 - not the job's code: any raise
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
        except BaseException:                     # noqa: BLE001 - not the job's code: any raise
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


NOT_JSON = object()           # what `strict_json` gives for bytes it will not read
# The longest integer `strict_json` returns, in digits: Python's own default limit for
# turning an integer into text and back (4,300). An integer beyond it could be read,
# with the interpreter's limit turned off, and then not printed.
MAX_INT_DIGITS = sys.int_info.default_max_str_digits


def _finite_float(text: str) -> float:
    """A JSON number with a fraction or an exponent, as a float that is a NUMBER. Python
    reads `1e400` as infinity and `-1e400` as minus infinity, and says nothing."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("a number too large to be one")
    return value


def _bounded_int(text: str) -> int:
    """A JSON integer of no more digits than Python will convert, whatever the
    interpreter's own limit is set to at the moment."""
    limit = sys.get_int_max_str_digits() or MAX_INT_DIGITS
    if len(text.lstrip("-")) > min(limit, MAX_INT_DIGITS):
        raise ValueError("an integer too long to be one")
    return int(text)


def strict_json(raw: object) -> object:
    """THE ONE LOADER FOR EVERY JSON OBJECT THE ENTRYPOINT READS (Codex P2, PR #9,
    second re-check). Strict, as the comparator is (`ws7_compare._parse`, whose two
    hooks are used here): UTF-8 only, NO DUPLICATE MEMBER in any object, and no NaN or
    Infinity. Anything else is `NOT_JSON`, never a value.

    EVERY NUMBER IT RETURNS IS FINITE AND WITHIN PYTHON'S INTEGER CONVERSION LIMIT,
    HOWEVER THE JSON SPELLS IT (Codex, PR #9, third re-check). `1e400` and `-1e400`
    are valid JSON, and Python reads them as plus and minus infinity: the same values
    `Infinity` and `-Infinity` give, by another spelling. An integer of 5,000 digits
    is valid JSON too, and Python will not print it. Each is `NOT_JSON`.

    Why duplicates: Python's own loader keeps the LAST of two members with one name
    and says nothing. A file that holds `"error_code": "x"` and then
    `"error_code": null` would be read as a clean generation, and a record that holds
    a wrong run ID and then the right one would pass its validator.

    The three reads: a batch's own index (`batch.json`), the comparator's result, and
    each object the Thursday check reads (the claim, the success record, the failed
    record)."""
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=compare._no_duplicate_keys,
                          parse_constant=compare._no_constants, parse_float=_finite_float,
                          parse_int=_bounded_int)
    except (AttributeError, ValueError, RecursionError):
        # not bytes; not UTF-8; not JSON; a duplicate member; NaN or Infinity; a number
        # that is not finite, or too long to be one
        return NOT_JSON


def _read_json(path: Path) -> object:
    """A file's JSON, strictly, or None."""
    try:
        value = strict_json(path.read_bytes())
    except OSError:
        return None
    return None if value is NOT_JSON else value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------- a batch's own index

def index_counts(target: str, index: object) -> tuple | None:
    """(failed generations, safety-net gate failures) in a batch's own index
    (`batch.json`), or None when the index is not one this target's runner wrote.

    AN EXIT CODE IS EXPLAINED ONLY BY THE FIELDS THAT TARGET'S RUNNER WRITES, EACH
    READ BY EXACT TYPE (Codex P2, PR #9, re-check):

    - `error_code`, written by both runners for every generation
      (run_pipeline_eval.py:431, run_cold_eval.py:568): null, or a non-empty string.
      A string is a failed generation.
    - `gate_ok`, written by the v1 runner only (run_cold_eval.py:567): exactly
      `true`, `false` or null. `false` is a gate failure. THE PIPELINE HAS NO
      SAFETY-NET GATE: its runner writes no such field, so for the pipeline the
      field is not read at all and can explain nothing.

    Nothing else in the index explains anything: not `failed_step`, not
    `gate_status`. A field of any other type (`0`, `""`, `"false"`, a list) is not a
    value either runner writes, so the index is refused; it is never read as
    "failed" or as "clean"."""
    rows = index.get("generations") if type(index) is dict else None
    if type(rows) is not list:
        return None
    failed = gates = 0
    for row in rows:
        if type(row) is not dict or "error_code" not in row:
            return None
        code = row["error_code"]
        if code is not None and (type(code) is not str or not code):
            return None
        failed += code is not None
        if target == "v1":
            if "gate_ok" not in row:
                return None
            gate = row["gate_ok"]
            if gate is not None and type(gate) is not bool:
                return None
            gates += gate is False
    return failed, gates


def exit_code_explained(target: str, index: object, exit_code: object) -> bool:
    """Does this batch's own index explain this exit code? 0: the index records no
    failed generation and no gate failure. 1: it records at least one. Anything else,
    or an index `index_counts` refuses, explains nothing."""
    counts = index_counts(target, index)
    if type(exit_code) is not int or exit_code not in (0, 1) or counts is None:
        return False
    return (exit_code == 1) == bool(counts[0] or counts[1])


# ------------------------------------------------------------------ a scheduled run

class _Scheduled:
    """One scheduled run. The fields fill in as it goes; `fail()` reads them to know
    how far it got."""

    def __init__(self, target: str, scheduled_time: str, deps: Deps):
        self.target, self.scheduled_time, self.deps = target, scheduled_time, deps
        self.environ = os.environ if deps.environ is None else deps.environ
        self.out = best_effort(deps.out)
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
        self.stored = "none"            # "none", "not_finished", "stored", or a code
        self.files_refused = 0          # files left out of the store on the failure path

    def say(self, stage: str, **fields) -> None:
        """One log line: fixed labels, IDs and counts the job made itself. NEVER an
        object it did not create: an exception is named by the fixed label of where it
        happened (`during=`), never by its class name or its message, because a class
        from outside the job can be named anything and its name can raise. The line is
        built inside `best_effort`."""
        def line() -> str:
            parts = [f"ws7 entry mode=scheduled target={self.target} stage={stage}"]
            parts += [f"{name}={value}" for name, value in fields.items()]
            return " ".join(parts)
        self.out(line)

    def stopped(self, stop: Stop) -> None:
        """Log a stop `own_stop` made. The line is built here, inside the best-effort
        boundary, from the stop's checked fields."""
        self.out(lambda: stop_line(stop))

    def taken(self, caught: object, where: str) -> Stop:
        """A stop the job has caught, as one it may act on. Anything that is not the
        job's own stop (`own_stop`) is a crash, logged by the fixed label of where it
        was caught, and it becomes `ws7_crashed`."""
        stop = own_stop(caught)
        if stop is None:
            self.say("crashed", during=where)
            stop = Stop(CRASHED, ids=(NOT_A_JOB_STOP,))
        return stop

    # ------------------------------------------------------------- the run

    def run(self) -> int:
        try:
            self._before_the_claim()
            # A DUPLICATE IS ACTED ON ONLY WHEN THE CLAIM ITSELF RAISES IT. Raised by
            # anything else, at any time, it is not a duplicate delivery: it must never
            # end a run that holds its claim with exit code 0.
            try:
                self._claim()
            except wl.Duplicate as caught:
                stop = own_stop(caught)             # the claim's own Duplicate, exactly
                if stop is None:
                    return self._fail(self.taken(caught, "claim"))
                return self._duplicate(stop)
            self._admit()
            self._generate()
            result, exit_code = self._score_and_compare()
            self._store(strict=True)
            return self._succeed(result, exit_code)
        except wl.Duplicate:
            self.say("crashed", during="run")
            return self._fail(Stop(CRASHED, ids=("duplicate_not_from_the_claim",)))
        except Stop as caught:
            return self._fail(self.taken(caught, "run"))
        except BaseException:                       # noqa: BLE001 - the outermost handler
            # Whatever it is: a SystemExit and a KeyboardInterrupt are not Exceptions, and a
            # run they ended must still settle and write its failed record.
            # No traceback, no class name and no message: any of them could carry text,
            # and looking up a class's name can itself raise. A fixed label says where.
            self.say("crashed", during="run")
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

    def _claim(self) -> None:
        cfg, deps = self.cfg, self.deps
        held = wl.claim(self.st, cfg, target=self.target, mode=SCHEDULED,
                        scheduled_time=self.scheduled_time, clock=deps.wall)
        if held.run_id != self.run_id:
            raise Stop(wl.SCHEDULED_TIME_INVALID)
        self.held = held
        self.say("claim", run=held.run_id)

    def _admit(self) -> None:
        cfg, deps, held = self.cfg, self.deps, self.held
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
        except BaseException:                       # noqa: BLE001 - a SystemExit too, with any code
            # A RUNNER ENDS ONLY BY RETURNING. Anything it raises is a crash of that
            # runner, a SystemExit included, whatever its code (0, 1, 2, None, a string):
            # the argv is an allow-list, so a parser exit already means something is
            # wrong. It is never read as an exit code (Codex P2, PR #9, re-check).
            exit_code, raised = None, True
        finally:
            self.guard.end_batch()
        return exit_code, raised

    def _judge(self, kind: str, exit_code: object, raised: bool) -> dict:
        """What a runner's return means. Returns the batch's index (`batch.json`)."""
        label = self.labels[kind]
        report = self.guard.report()
        failure = first_failure(report)
        if failure is not None:                     # ruling e2: the first failure's code
            raise Stop(failure.code, ids=failure.ids + (label,),
                       counts=dict(failure.counts, calls_stopped=self.wire.refused))
        fault = wl.report_fault(report)
        if fault is not None:
            # Every call has an outcome, and the totals are not the sum of the rows: the
            # accounting of a call did not finish. The run's cost is unknown.
            raise Stop(fault, ids=(label,), counts={"calls_stopped": self.wire.refused})
        if raised:
            raise Stop(RUNNER_CRASHED, ids=(label,))
        index = _read_json(self.batches[label]["dir"] / "batch.json")
        counts = index_counts(self.target, index)
        # A RETURN CODE IS AN INTEGER IN A SMALL FIXED RANGE BEFORE IT IS USED IN ANY
        # DIAGNOSTIC. Anything else (None, a string, True, a negative number, a number
        # of 5,001 digits, which Python will not even print) is named by a fixed label
        # and never copied into a stop, a log line or a record.
        if type(exit_code) is not int or not 0 <= exit_code <= EXIT_CODE_MAX:
            raise Stop(RUNNER_REFUSED, ids=(label, "exit_code_not_a_code"))
        # Anything but 0 or 1 (a canary window, an existing batch folder, a corpus it
        # could not read) is a runner that refused to start. So is an index this
        # target's runner did not write.
        if exit_code not in (0, 1) or counts is None:
            raise Stop(RUNNER_REFUSED, ids=(label,), counts={"exit_code": exit_code})
        if index.get("stopped_early") is not False:
            raise Stop(RUNNER_STOPPED_EARLY, ids=(label,))
        # 0: clean. 1: a failed generation or, for v1, a safety-net gate failure. Both
        # runners return 1 for other reasons too (run_cold_eval.py:663, a confirmation
        # that was not given), so 1 is accepted ONLY when the batch's own index explains
        # it, and 0 only when the index has nothing to explain (Codex P2, PR #9).
        if not exit_code_explained(self.target, index, exit_code):
            raise Stop(RUNNER_REFUSED, ids=(label,),
                       counts={"exit_code": exit_code, "generations_failed": counts[0],
                               "gate_failures": counts[1]})
        return index

    def _generate(self) -> None:
        # ---- the pre-flight: one scenario, alone, as its own batch (D7). Its go/no-go
        # is three things: no failed generation, only the expected files, within its
        # reservation. A v1 gate failure is not one of them: it is quality evidence,
        # which the comparator judges on the main batch. Stopping here for it would
        # turn a safety-netting regression into "pre-flight failed", with no
        # comparison and no breach alarm.
        label = self.labels[PREFLIGHT]
        index = self._judge(PREFLIGHT, *self._dispatch(PREFLIGHT))
        if len(index["generations"]) != 1 or index_counts(self.target, index)[0]:
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
        """Settle from the guard's report, or mark the run crashed. Tried once:
        whatever comes of it, it is not tried again.

        THE RUN IS MARKED CRASHED WHEN THERE IS NO GUARD REPORT, OR WHEN THE REPORT
        CANNOT BE SETTLED FROM (the author's ruling, 21 Nov 2026, widening change A).
        The ledger refuses a report that is incomplete or inconsistent
        (`ws7_ledger.report_fault`), or that cannot be read: the run's cost is then
        unknown, so its entry becomes unresolved at its reservation and blocks every
        later scheduled run until the author resolves it. The ledger's refusal is
        still raised, after the mark has landed, so the run fails by it."""
        if self.admission is None or self.settled is not None:
            return
        self.settled = "failed"
        refused = None
        try:
            if self.guard is None:
                entry = self.ledger.mark_crashed(self.admission)
            else:
                try:
                    report = self.guard.report()
                except BaseException:               # noqa: BLE001 - a guard left half-written
                    report = None                   # the ledger refuses it: ws7_report_invalid
                try:
                    entry = self.ledger.settle(self.admission, report)
                except Stop as caught:
                    stop = own_stop(caught)
                    if stop is None or stop.code not in wl.REPORT_CODES:
                        raise
                    refused = stop
                    entry = self.ledger.mark_crashed(self.admission)
        except Stop as caught:
            stop = own_stop(caught)
            if stop is None:                        # not the ledger's own stop: the settle crashed
                self.say("crashed", during="settle")
                stop = Stop(CRASHED, ids=("settle",))
            else:
                self.settled = stop.code
            self.settle_stop = stop
            raise stop from None
        self.settled = entry["state"] if entry["kind"] is None else entry["kind"]
        self.say("settled", state=self.settled, counted_usd_micros=entry["counted_usd_micros"])
        if refused is not None:
            self.settle_stop = refused
            raise refused

    def _score_and_compare(self) -> tuple:
        label = self.labels[MAIN]
        batch_dir = self.batches[label]["dir"]
        failed = index_counts(self.target, self.main_index)[0]
        if self.target == "pipeline" and failed:
            # The assembly replay stops on any failed pipeline generation
            # (evals/score_assemble.py:202), so that batch can have no summary. Decided
            # here, from batch.json, before any scorer runs. The scorer is not changed.
            raise Stop(GENERATION_FAILED, ids=(label,), counts={"generations_failed": failed})
        # (name, the call, whether the call is the job's own code). The scorers are not
        # the job's code. `write_haz03` is (evals/ws7_summary.py), and it turns whatever
        # the scorer it calls raises into `ScorerStopped`.
        steps = [("score_batch", lambda: SB.write_reports(batch_dir, SB.score_batch(batch_dir)),
                  False)]
        if self.target == "pipeline":
            steps += [("score_assemble", lambda: SA.write(batch_dir / "scores" / "assemble",
                                                          SA.replay(batch_dir, False, None)),
                       False),
                      ("haz03_check", lambda: ws.write_haz03(batch_dir), True)]
        # Scoring begins: from here the known score files may be in the folder. A scorer
        # that stops after it wrote its reports must not make them unknown files.
        self.batches[label]["scored"] = True
        for name, step, own in steps:
            try:
                with _quiet():
                    step()
            except BaseException as exc:            # noqa: BLE001 - a scorer that stops, however
                # it stops, is a failed run (D2): any exception, a SystemExit with any code,
                # AND A STOP OR A DUPLICATE. Only the job's own code may raise a Stop the
                # run acts on, and no code but the claim a Duplicate.
                if own and isinstance(exc, Stop) and not isinstance(exc, wl.Duplicate):
                    raise
                raise Stop(SCORER_STOPPED, ids=(name,)) from None
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
        except BaseException:                       # noqa: BLE001 - a usage error (SystemExit,
            # whatever its code), or anything else: the comparator ends only by returning
            raise Stop(COMPARE_ERROR, ids=("comparator_raised",)) from None
        # The exit code is the authority, and it is judged first.
        if type(exit_code) is not int or exit_code not in (0, 1, 2):
            raise Stop(COMPARE_ERROR, ids=("exit_code_unknown",))
        try:
            raw = out.read_bytes()
        except OSError:
            raw = None
        result = strict_json(raw)                   # None is not bytes: NOT_JSON
        if result is NOT_JSON:
            raise Stop(COMPARE_ERROR, ids=("result_unreadable",),
                       counts={"exit_code": exit_code})
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
        except Stop as caught:
            stop = own_stop(caught)
            if stop is not None and stop.code == publish.PART_C_RULED:
                raise stop from None
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
        except store.StoreError as caught:
            stop = own_stop(caught)                 # the store's own error, exactly, or not at all
            if stop is None:
                raise Stop(EVIDENCE_UNAVAILABLE, ids=(NOT_A_JOB_STOP,)) from None
            code = EVIDENCE_UNSTORABLE if stop.code == store.OBJECT_TOO_LARGE \
                else EVIDENCE_UNAVAILABLE
            raise Stop(code, ids=(stop.code,)) from None
        if outcome != store.CREATED:
            raise Stop(EVIDENCE_UNAVAILABLE, ids=("key_taken",))

    def _store(self, *, strict: bool) -> None:
        """Store the run's records under its own prefix (D8): the guard's report, the
        summary, the comparison result, each batch folder and the manifest.

        EVERY FILE IS DECIDED BEFORE ANY IS SENT. A file may be stored only if it is
        on the list of what the runners and scorers write, is `.json` or `.md`, is a
        plain file that can be read, and is no larger than one object may be.

        - `strict` (the success path, ruling 7): one file that may not be stored
          stops the run with `ws7_evidence_unstorable`, and nothing is sent.
        - not `strict` (the failure path): such a file is never uploaded, and it does
          not stop the rest. It is counted in `files_refused`, which the failed
          record carries.

        Sending stops at the first object S3 will not take: if S3 is refusing writes,
        trying the rest would only delay the failed record and `RunOk` = 0."""
        if self.stored != "none" or self.held is None:
            return
        uploads, refused = self._plan()
        if strict and refused:
            # Nothing was sent, and `stored` is still "none": the failure path stores
            # what is allowed, and leaves these out.
            where = sorted(refused)[0]
            raise Stop(EVIDENCE_UNSTORABLE, ids=(where,), counts=refused[where])
        if strict and self.key_parts is None:
            raise Stop(wm.MANIFEST_INVALID)
        self.files_refused = sum(n for counts in refused.values() for n in counts.values())
        self.stored = "not_finished"
        try:
            for key, body, kind in uploads:
                self._put(key, body, kind)
            if self.key_parts is not None:
                manifest = wm.build_manifest(
                    held=self.held, own=self.own, who=self.who, cfg=self.cfg,
                    reservation=self.reservation, baseline_id=self.baseline_id,
                    month_assurance_waived=self.waived, **self.key_parts)
                wm.write_manifest(self.st, manifest, attempts=self.cfg.ledger_cas_attempts)
        except Stop as caught:
            stop = own_stop(caught)
            if stop is None:
                self.stored = CRASHED
                raise Stop(CRASHED, ids=("store",)) from None
            self.stored = stop.code
            raise stop from None
        self.stored = "stored"
        self.say("stored", objects=len(uploads) + (self.key_parts is not None),
                 files_refused=self.files_refused)

    def _plan(self) -> tuple:
        """(what to send, what may not be sent). The first is a list of
        `(key, body, content type)`, in the order it is sent: the guard's report
        first, then the run's other documents, then each batch folder. The second
        maps a batch label (or "documents") to `{reason: how many}`, for the files
        that may not be stored: "unexpected", "unreadable", "too_large". Nothing is
        sent here."""
        run, uploads, refused = self.held.run_id, [], {}

        def refuse(where: str, why: str) -> None:
            counts = refused.setdefault(where, {})
            counts[why] = counts.get(why, 0) + 1

        if self.guard is not None:
            self.documents.setdefault("guard_report.json", compare.render(self.guard.report()))
        for name in sorted(self.documents, key=lambda n: (n != "guard_report.json", n)):
            if len(self.documents[name]) > store.MAX_OBJECT_BYTES:
                refuse("documents", "too_large")
                continue
            uploads.append((store.evidence_key(SCHEDULED, run, name), self.documents[name],
                            store.JSON_TYPE))
        for label, batch in sorted(self.batches.items()):
            batch_dir, scenarios = batch["dir"], batch["scenarios"]
            if not batch_dir.is_dir():
                continue
            files, other = files_in(batch_dir)
            allowed = expected_files(self.target, scenarios, scored=batch["scored"])
            for _ in range(other):                  # a link, a socket: never followed, never sent
                refuse(label, "unexpected")
            for name in files:
                kind = store.content_type(name)
                if name not in allowed or kind is None:
                    refuse(label, "unexpected")
                    continue
                try:
                    body = (batch_dir / name).read_bytes()
                except OSError:
                    refuse(label, "unreadable")
                    continue
                if len(body) > store.MAX_OBJECT_BYTES:
                    refuse(label, "too_large")
                    continue
                uploads.append((store.evidence_key(SCHEDULED, run, "batches", label,
                                                   *name.split("/")), body, kind))
        return uploads, refused

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
                  "mode": SCHEDULED, "scheduled_time": self.held.scheduled_time,
                  "status": result["status"],
                  "breaches": breaches, "baseline_id": result["baseline_id"],
                  "summary_sha256": _sha256(self.documents["summary.json"]),
                  "comparison_sha256": _sha256(self.documents["comparison.json"]),
                  "evidence": prefix, "git_commit": self.who.git_commit,
                  "image_digest": self.who.image_digest,
                  "finished_at": store.utc_text(store.read_clock(deps.wall))}
        # Before the PUT: the validator the Thursday check runs on what it reads. A
        # success record the check would reject is never written.
        fault = success_fault(record, claim=claim_view(self.held))
        if fault is not None:
            raise Stop(SUCCESS_RECORD_INVALID, ids=(fault,))
        try:
            outcome = self.st.create(store.success_key(run), compare.render(record),
                                     attempts=self.cfg.ledger_cas_attempts)
        except store.StoreError as caught:
            stop = own_stop(caught)
            raise Stop(SUCCESS_RECORD_UNAVAILABLE,
                       ids=(stop.code if stop is not None else NOT_A_JOB_STOP,)) from None
        if outcome != store.CREATED:
            raise Stop(SUCCESS_RECORD_UNAVAILABLE, ids=("key_taken",))
        self.say("success", status=result["status"], breaches=breaches)
        # The last act. If CloudWatch does not acknowledge it, the run fails as any run
        # that holds its claim fails: the failed record is written beside the success
        # record, so the Thursday check reports a failed run, and RunOk = 0 is tried.
        try:
            self.publisher.put([(publish.RUN_OK, 1)])
        except Stop as caught:
            stop = own_stop(caught)
            raise Stop(RUNOK_PUBLISH_FAILED,
                       ids=(stop.code if stop is not None else NOT_A_JOB_STOP,)) from None
        return 0

    def _duplicate(self, stop: Stop) -> int:
        """A duplicate delivery: it reserved and spent nothing. It counts one
        duplicate and publishes nothing else, and never `RunOk`."""
        self.stopped(stop)
        try:
            self.publisher.put([(publish.DUPLICATE, 1)])
        except Stop as caught:
            failed = own_stop(caught)
            if failed is None:
                self.say("crashed", during="duplicate")
            else:
                self.say("stop", code=failed.code)
            return 1
        except BaseException:                       # noqa: BLE001 - this is outside run()'s try
            self.say("crashed", during="duplicate")
            return 1
        return 0

    def _fail(self, stop: Stop) -> int:
        """A failed run. THE FAILURE PATH NEVER FAILS OPEN: its five steps are tried
        in this order, each on its own. A step that stops or raises is logged, and
        the next one is still tried.

        NOTHING A STEP RAISES ENDS THE PATH EARLY, WHATEVER ITS TYPE. The handler is
        for `BaseException`: `SystemExit` and `KeyboardInterrupt` do not derive from
        `Exception`, and a log sink, the clock, a store call or a publish call that
        raised one of them would otherwise skip every step after it.

        ONLY THE JOB'S OWN STOPS ARE RENDERED OR ACTED ON (`own_stop`). The stop the
        run failed by, and every stop a step raises, is taken as a new, exact `Stop`
        with checked fields, or it is a crash named by where it was caught. Its line
        is built here; no method of a caught object is called."""
        stop = own_stop(stop) or Stop(CRASHED, ids=(NOT_A_JOB_STOP,))
        self.stopped(stop)
        also: list = []
        for name, step in (("settle", self._fail_ledger), ("store", self._fail_evidence),
                           ("failed_record", self._fail_record),
                           ("markers", self._fail_markers), ("counters", self._fail_counters)):
            try:
                step(stop, also)
            except Stop as caught:
                failed = own_stop(caught)
                if failed is None:
                    self.say("crashed", during=name)
                else:
                    self.stopped(failed)
            except BaseException:                   # noqa: BLE001 - never stops the next
                self.say("crashed", during=name)
        return 1

    def _fail_ledger(self, stop: Stop, also: list) -> None:
        """1. The ledger, first: settled from the guard's report, or marked crashed
        when there is no guard."""
        earlier = self.settle_stop
        if earlier is not None and earlier is not stop:
            # The settle was tried when the last runner returned, and it was refused.
            also.append(earlier.code)
            self.stopped(earlier)
        try:
            self._settle()
        except Stop as caught:
            failed = own_stop(caught)
            also.append(failed.code if failed is not None else CRASHED)
            raise
        except BaseException:
            also.append(CRASHED)
            raise

    def _fail_evidence(self, _stop: Stop, _also: list) -> None:
        """2. What records exist: the guard's report first, then every allowed file."""
        try:
            self._store(strict=False)
        except Stop:
            raise
        except BaseException:
            self.stored = CRASHED
            raise

    def _failed_record(self, stop: Stop, also: list) -> dict:
        return {"schema": FAILED_SCHEMA, "run_id": self.held.run_id, "target": self.target,
                "mode": SCHEDULED, "scheduled_time": self.held.scheduled_time,
                "code": stop.code, "ids": list(stop.ids),
                "counts": dict(sorted(stop.counts.items())), "also": list(also),
                "ledger": self.settled or "not_admitted", "evidence": self.stored,
                "files_refused": self.files_refused,
                "git_commit": self.who.git_commit, "image_digest": self.who.image_digest,
                "failed_at": store.utc_text(store.read_clock(self.deps.wall))}

    def _fail_record(self, stop: Stop, also: list) -> None:
        """3. The failed record the Thursday check reads: only for a run that holds its
        claim. It is built here, the clock read included, so that nothing that goes
        wrong in building it can stop the metrics that follow."""
        if self.held is None:
            return
        record = self._failed_record(stop, also)
        # Before the PUT: the validator the Thursday check runs on what it reads. A
        # failed record the check would reject is never written; the steps after this
        # one still run, so RunOk = 0 is still published.
        fault = failed_fault(record, claim=claim_view(self.held))
        if fault is not None:
            raise Stop(FAILED_RECORD_INVALID, ids=(fault,))
        outcome = self.st.create(store.failed_key(self.held.run_id), compare.render(record),
                                 attempts=self.cfg.ledger_cas_attempts)
        self.say("failed_record", written=int(outcome == store.CREATED))

    def _fail_markers(self, stop: Stop, _also: list) -> None:
        """4. The failure's own markers. `RunOk` = 0 only from the claim's holder."""
        if self.publisher is None:
            return
        values = [(publish.RUN_REFUSED, 1)]
        if self.held is not None:
            values.append((publish.RUN_OK, 0))
        if stop.code == NO_BASELINE:
            values.append((publish.NO_BASELINE, 1))
        self.publisher.put(values)

    def _fail_counters(self, _stop: Stop, _also: list) -> None:
        """5. The run's counters: from its summary if it has one, else from the guard's
        report. A counter CloudWatch has already acknowledged is not sent again."""
        if self.publisher is None:
            return
        if self.summary is not None:
            values = publish.run_counts(self.summary, self.target)
        elif self.guard is not None:
            values = publish.guard_counts(self.guard.report())
        else:
            return
        values = self.publisher.unsent(values)
        if values:
            self.publisher.put(values)


# ------------------------------------------------------------- the Thursday check

def _listed(st: store.Store, key: str) -> bool:
    """Is exactly this key there? From a listing, never from a GET error."""
    return key in st.list_keys(key)


def _read_object(st: store.Store, key: str) -> object:
    """The object at `key`, read strictly (`strict_json`), or `NOT_JSON`."""
    return strict_json(st.get(key)[0])


# ONE VALIDATOR FOR EACH RECORD, SHARED BY THE CODE THAT WRITES IT AND THE CHECK THAT
# READS IT (Codex P2, PR #9, re-check), as the comparator's public checks are shared.
# `_succeed` and `_fail_record` call these on the object they are about to PUT, and the
# Thursday check calls them on the object it has just read: the job can never write a
# record its own check would reject. The claim's validator is `ws7_ledger.claim_fault`,
# which `ws7_ledger.claim()` calls before its own PUT.
#
# EVERY FIELD IS CHECKED: its type (exactly: `True` is not an integer, `1` is not a
# string), its pattern or range, and its agreement with the claim. An unknown or
# missing field is refused. Each returns a fixed label naming the first field that
# fails, or None.

SUCCESS_FIELDS = ("schema", "run_id", "target", "mode", "scheduled_time", "status", "breaches",
                  "baseline_id", "summary_sha256", "comparison_sha256", "evidence", "git_commit",
                  "image_digest", "finished_at")
FAILED_FIELDS = ("schema", "run_id", "target", "mode", "scheduled_time", "code", "ids", "counts",
                 "also", "ledger", "evidence", "files_refused", "git_commit", "image_digest",
                 "failed_at")
# One breach value for each family and kind there is a metric name for.
MAX_BREACHES = len(publish.BREACH_FAMILIES) * len(compare.KINDS)
# What became of the ledger entry: not admitted; a settle that was tried and did not
# return; the entry's state or kind; or the code the settle stopped with.
LEDGER_WORDS = ("not_admitted", "failed") + wl.STATES + wl.KINDS
EVIDENCE_WORDS = ("none", "not_finished", "stored")     # or the code storing stopped with
_CODE_RE = re.compile(r"ws7_[a-z0-9_]{1,60}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")                # as ws7_manifest.identity() holds it
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def claim_view(held: wl.Claim) -> dict:
    """The fields of a held claim that a record must agree with, as the claim object
    has them."""
    return {"run_id": held.run_id, "target": held.target, "mode": held.mode,
            "scheduled_time": held.scheduled_time}


def _text(value: object, pattern) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= COUNT_MAX       # a bool is not a count


def _head_fault(record: object, schema: str, fields: tuple, claim: dict) -> str | None:
    """The five fields both records share, and the shape of the object."""
    if type(record) is not dict:
        return "not_an_object"
    if set(record) != set(fields):
        return "fields"
    for name, wanted in (("schema", schema), ("run_id", claim["run_id"]),
                         ("target", claim["target"]), ("mode", claim["mode"]),
                         ("scheduled_time", claim["scheduled_time"])):
        if type(record[name]) is not str or record[name] != wanted:
            return name
    return None


def _not_after_the_schedule(text: object, claim: dict) -> bool:
    """Is `text` not a UTC time at or after the claim's scheduled time?"""
    when, scheduled = store.parse_utc(text), store.parse_utc(claim["scheduled_time"])
    return when is None or scheduled is None or when < scheduled


def _identity_fault(record: dict) -> str | None:
    if not _text(record["git_commit"], _COMMIT_RE):
        return "git_commit"
    if not _text(record["image_digest"], _DIGEST_RE):
        return "image_digest"
    return None


def success_fault(record: object, *, claim: dict) -> str | None:
    """Why `record` is not the success record (`ws7-success/1`) of the run `claim`
    names: a fixed label, or None when it is. `claim` is a claim object that has
    passed `ws7_ledger.claim_fault`, or `claim_view()` of the claim the job holds.

    - `schema`, `run_id`, `target`, `mode`, `scheduled_time`: strings, the claim's own;
    - `status`: `no_breach` or `breach`;
    - `breaches`: a whole number from 0 to MAX_BREACHES, above 0 exactly when the
      status is `breach`;
    - `baseline_id`: an ID;
    - `summary_sha256`, `comparison_sha256`: 64 hex characters;
    - `evidence`: the run's own prefix;
    - `git_commit`: 40 hex characters; `image_digest`: `sha256:` and 64 hex characters;
    - `finished_at`: a UTC time, not before the scheduled time."""
    fault = _head_fault(record, SUCCESS_SCHEMA, SUCCESS_FIELDS, claim)
    if fault is not None:
        return fault
    if type(record["status"]) is not str or record["status"] not in (compare.NO_BREACH,
                                                                     compare.BREACH):
        return "status"
    breaches = record["breaches"]
    if not _count(breaches) or breaches > MAX_BREACHES \
            or (breaches > 0) != (record["status"] == compare.BREACH):
        return "breaches"
    if not _text(record["baseline_id"], store.ID_RE):
        return "baseline_id"
    for name in ("summary_sha256", "comparison_sha256"):
        if not _text(record[name], _HEX64_RE):
            return name
    if type(record["evidence"]) is not str \
            or record["evidence"] != store.run_prefix(claim["mode"], claim["run_id"]):
        return "evidence"
    fault = _identity_fault(record)
    if fault is not None:
        return fault
    if _not_after_the_schedule(record["finished_at"], claim):
        return "finished_at"
    return None


def failed_fault(record: object, *, claim: dict) -> str | None:
    """Why `record` is not the failed record (`ws7-failed/1`) of the run `claim`
    names: a fixed label, or None when it is.

    - `schema`, `run_id`, `target`, `mode`, `scheduled_time`: strings, the claim's own;
    - `code`: a `ws7_` code; `also`: a list of them;
    - `ids`: a list of IDs; `counts`: an object of fixed labels to integers;
    - `ledger`: one of LEDGER_WORDS, or a `ws7_` code;
    - `evidence`: one of EVIDENCE_WORDS, or a `ws7_` code;
    - `files_refused`: a whole number;
    - `git_commit`: 40 hex characters; `image_digest`: `sha256:` and 64 hex characters;
    - `failed_at`: a UTC time, not before the scheduled time."""
    fault = _head_fault(record, FAILED_SCHEMA, FAILED_FIELDS, claim)
    if fault is not None:
        return fault
    if not _text(record["code"], _CODE_RE):
        return "code"
    ids, counts, also = record["ids"], record["counts"], record["also"]
    if type(ids) is not list or not all(_text(i, store.ID_RE) for i in ids):
        return "ids"
    if type(counts) is not dict or not all(
            _text(k, store._LABEL_RE) and type(v) is int and -COUNT_MAX <= v <= COUNT_MAX
            for k, v in counts.items()):
        return "counts"
    if type(also) is not list or not all(_text(code, _CODE_RE) for code in also):
        return "also"
    for name, words in (("ledger", LEDGER_WORDS), ("evidence", EVIDENCE_WORDS)):
        value = record[name]
        if type(value) is not str or not (value in words or _CODE_RE.fullmatch(value)):
            return name
    if not _count(record["files_refused"]):
        return "files_refused"
    fault = _identity_fault(record)
    if fault is not None:
        return fault
    if _not_after_the_schedule(record["failed_at"], claim):
        return "failed_at"
    return None


def _failed_line(st: store.Store, claim: dict) -> str:
    """One line for a failed run, from its failed record: the code, and the IDs the
    stop carried. For a run blocked by an unresolved entry, those are the entries. A
    record that fails its check is reported as invalid; the run is still a failed run."""
    run = claim["run_id"]
    record = _read_object(st, store.failed_key(run))
    fault = "not_json" if record is NOT_JSON else failed_fault(record, claim=claim)
    if fault is not None:
        return f"failed run {run}: failed record invalid ({fault})"
    code, ids = record["code"], record["ids"]
    line = f"failed run {run} code {code}"
    if code == wl.BLOCKED_UNRESOLVED and ids:
        return line + ": blocked by unresolved " + ", ".join(ids[:IDS_SHOWN])
    return line + (" ids " + ", ".join(ids[:IDS_SHOWN]) if ids else "")


def _occurrence(st: store.Store, key: str, target: str, cfg: reserve.Config) -> str | None:
    """What the check has to say about the occurrence whose claim key is `key`, or
    None when its run succeeded. NOTHING READ HERE IS TRUSTED AS READ: the claim, the
    failed record and the success record are each checked first, every field of each,
    by the validator its writer ran before the PUT.

    The run's ID is the claim's own. The check never builds one from its own
    scheduled time, because the run's UTC hour moves with the London clock; the
    claim's ID is only held to the claim's own scheduled time."""
    if not _listed(st, key):
        return publish.NO_CLAIM_LINE
    claim = _read_object(st, key)
    fault = "not_json" if claim is NOT_JSON else wl.claim_fault(
        claim, key=key, target=target, mode=SCHEDULED, max_start_delay_s=cfg.max_start_delay_s)
    if fault is not None:
        return f"claim object invalid ({fault}): it is not read as a scheduled claim"
    run = claim["run_id"]
    if _listed(st, store.failed_key(run)):
        # A failed record wins: if a success record is there too, it is a failed run.
        return _failed_line(st, claim)
    if not _listed(st, store.success_key(run)):
        return f"run {run} started and did not finish: no success record"
    record = _read_object(st, store.success_key(run))
    fault = "not_json" if record is NOT_JSON else success_fault(record, claim=claim)
    if fault is not None:
        return f"run {run}: success record invalid ({fault}); it is not counted as a success"
    return None


def check(target: str, scheduled_time: str, deps: Deps) -> int:
    """The Thursday check (D9): did the scheduled run succeed? No Bedrock call, no
    claim, no CloudWatch call. It reads S3 and publishes to the topic."""
    environ = os.environ if deps.environ is None else deps.environ
    out = best_effort(deps.out)

    def say(stage: str, **fields) -> None:
        out(lambda: " ".join([f"ws7 entry mode=check target={target} stage={stage}"]
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

        found = _occurrence(st, store.claim_key(target, SCHEDULED, day), target, cfg)
        findings = [] if found is None else [found]
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
    except Stop as caught:
        stop = own_stop(caught)                     # the job's own stop, or a crash
        if stop is None:
            say("crashed", during="check")
            code = CRASHED
        else:
            out(lambda: stop_line(stop))
            code = stop.code
    except BaseException:                           # noqa: BLE001 - whatever its type
        say("crashed", during="check")              # no class name: a fixed label says where
        code = CRASHED
    # The check itself failed. Say so on the topic if it can still be reached.
    if publisher is not None and code not in (publish.ALERT_FAILED, publish.ALERT_INVALID):
        try:
            subject = f"WS7 check - target {target} - the check failed"
            publisher.alert(subject, f"{subject}\ncode {code}; see the task log")
        except Stop as caught:
            failed = own_stop(caught)
            if failed is None:
                say("crashed", during="alert")
            else:
                out(lambda: stop_line(failed))
        except BaseException:                       # noqa: BLE001 - whatever its type
            say("crashed", during="alert")
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
    except BaseException:               # noqa: BLE001 - a usage error: nothing is built or sent
        try:
            print(USAGE, file=sys.stderr, flush=True)
        except BaseException:           # noqa: BLE001 - a closed stderr changes nothing
            pass
        return 2
    deps = deps or Deps()
    if args.mode == "check":
        return check(args.target, args.scheduled_time, deps)
    return scheduled(args.target, args.scheduled_time, deps)


if __name__ == "__main__":
    sys.exit(main())
