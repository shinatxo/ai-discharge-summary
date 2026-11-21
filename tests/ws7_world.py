"""The five local servers a WS7 entrypoint test runs against, and what goes with them
(slice 2 part 5).

`World` starts a Bedrock stub (tests/ws7_stub.py) and strict S3, SSM, CloudWatch and
SNS fakes (tests/ws7_fakes.py), all on 127.0.0.1, registers each with `offline_aws`,
and has every one of them append to ONE locked sequence log. The entrypoint is
called in-process, with:

- invented settings where the task definition's would be;
- one injected clock that is both the wall clock and the monotonic clock, and that
  moves only when a test moves it;
- `runs_dir` in the test's temporary folder;
- the frozen baseline and the rules read from the test's temporary folder.

Every batch is INVENTED: the real runners against the stub, and the real scorers,
unchanged, on that fresh batch. Nothing is read from evals/runs/.
"""

from __future__ import annotations

import io
import contextlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "tests", ROOT / "src" / "generate", ROOT / "evals"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import run_cold_eval as rce  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402
import score_assemble as SA  # noqa: E402
import score_batch as SB  # noqa: E402
import score_meds as MD  # noqa: E402
import gold as G  # noqa: E402
import ws7_client as wc  # noqa: E402
import ws7_entry as entry  # noqa: E402
import ws7_fakes as fakes  # noqa: E402
import ws7_manifest as wm  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
import ws7_store as store  # noqa: E402
import ws7_stub as stubs  # noqa: E402
import ws7_summary as ws  # noqa: E402
from pipeline import extract, run_step  # noqa: E402

CFG = reserve.load_config()
MARGIN = CFG.input_allowance_margin_percent
TARGETS = ("pipeline", "v1")
QUIET = rce.dt.datetime(2026, 12, 16, 4, 0, tzinfo=rce.LONDON)     # clear of both canary windows
READ_TIMEOUT_S = 5.0          # a reply that never comes fails in seconds
RUN_LIMIT_S = 60.0            # an entrypoint call that takes longer has waited on something

# An invented Wednesday in winter: 04:00 London is 04:00 UTC.
WHEN = "2026-12-16T04:00:00Z"
START = datetime(2026, 12, 16, 4, 0, 37, tzinfo=timezone.utc)      # the injected clock's start
RUN_ID = {t: f"2026-12-16T0400-{t}" for t in TARGETS}
CHECK_WHEN = "2026-12-17T10:00:00Z"                                # the Thursday after
# An invented Wednesday in summer: 04:00 London is 03:00 UTC.
BST_WHEN = "2027-06-16T03:00:00Z"
BST_START = datetime(2027, 6, 16, 3, 0, 37, tzinfo=timezone.utc)
BST_RUN_ID = {t: f"2027-06-16T0300-{t}" for t in TARGETS}
BST_CHECK_WHEN = "2027-06-17T09:00:00Z"

BUCKET = fakes.BUCKET
TOPIC = "arn:aws:sns:eu-west-2:000000000000:ws7-fake-eval-alerts"   # invented
COMMIT = "c0ffee" + "0" * 34                                        # invented
DIGEST = "sha256:" + "d1" * 32                                      # invented
BASELINE_ID = "b-2026-12-14"                                        # invented
SENTINEL = stubs.SENTINEL
CORPUS = {s.id: s for s in rce.load_corpus(rce.DEFAULT_CORPUS_PATH)[0]}
SCENARIOS = list(CORPUS)                                            # corpus order
STEPS = {t: tuple(step.id for step in reserve.plan(t).steps) for t in TARGETS}
FIRST_STEP = {t: STEPS[t][0] for t in TARGETS}
LEDGER_KEY = store.ledger_key("2026-12")
CLAIM_KEY = {t: f"ws7/claims/{t}/2026-12-16" for t in TARGETS}


class Clock:
    """One clock for the wall and the monotonic time. It moves only when told to."""

    def __init__(self, start: datetime = START):
        self.start, self.offset = start, 0.0

    def wall(self) -> datetime:
        return self.start + timedelta(seconds=self.offset)

    def monotonic(self) -> float:
        return 5000.0 + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


def own(target: str) -> dict:
    """The job's own values, as the match check works them out."""
    return wm.own_values(target, wm.runner_argv(target, CFG, preflight=False, batch="b"), CFG)


def expected_json(target: str, **changes) -> str:
    """An SSM parameter value that matches this checkout, with `changes` applied."""
    values = dict(wm._compared(own(target), target), schema=wm.EXPECTED_SCHEMA)
    values.update(changes)
    return json.dumps(values)


def invented_rules(target: str) -> dict:
    """An invented rules file. No number here is a ruling."""
    metrics = [
        {"metric": "sn_invented_triggers", "family": "safety_netting", "kind": 1},
        {"metric": "safety_net_verdict", "family": "safety_netting", "kind": 2, "min_flips": 1},
        {"metric": "meds_invented_doses", "family": "medications", "kind": 1},
        {"metric": "tokens_out_total", "family": "run", "kind": 3, "margin": 0}]
    return {"schema": "ws7-rules/1", "target": target, "metrics": metrics}


# ------------------------------------------------------- batches made without the entrypoint

def default_stub(target: str):
    return stubs.pipeline_stub() if target == "pipeline" else stubs.v1_stub()


def _never(_prompt):
    raise AssertionError("a WS7 test must never be asked to confirm a live run")


def stub_batch(target: str, stub, runs_dir: Path, guard: wc.Guard, label: str) -> Path:
    """One runner call of the whole corpus against the stub, scored by the real
    scorers: what part 4's tests do, used here to make an invented baseline."""
    runner = rpe if target == "pipeline" else rce
    guard.begin_batch(label, len(SCENARIOS))
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            runner.main(wm.runner_argv(target, CFG, preflight=False, batch=label),
                        client_factory=wc.client_factory(guard, endpoint_url=stub.url),
                        now=lambda: QUIET, runs_dir=runs_dir, sleep=lambda _s: None, confirm=_never)
    finally:
        guard.end_batch()
    batch_dir = runs_dir / label
    SB.write_reports(batch_dir, SB.score_batch(batch_dir))
    if target == "pipeline":
        SA.write(batch_dir / "scores" / "assemble", SA.replay(batch_dir, False, None))
        ws.write_haz03(batch_dir)
    return batch_dir


_BASELINES: dict = {}


def invented_baseline(target: str, net, tmp_path_factory) -> dict:
    """A `ws7-baseline/1` object made from three stub batches, as the author would
    freeze one from a manual baseline's candidate. Made once per target."""
    if target not in _BASELINES:
        runs_dir = tmp_path_factory.mktemp(f"ws7-entry-baseline-{target}")
        run = f"2026-12-14T0130-{target}-manual"
        stub = stubs.started(default_stub(target), net)
        try:
            guard = wc.Guard(reserve.plan(target), margin_percent=MARGIN, run_id=run,
                             reservation_nanos=reserve.reservation(target, 55, MARGIN)["total_usd_nanos"],
                             deadline=time.monotonic() + 10 * RUN_LIMIT_S)
            summaries = []
            for n in (1, 2, 3):
                label = f"{run}.b{n}"
                batch_dir = stub_batch(target, stub, runs_dir, guard, label)
                summaries.append(ws.build_summary(batch_dir, own=own(target), report=guard.report(),
                                                  batch_label=label, month_assurance_waived=False))
        finally:
            net.allowed.discard(stub.address)
            stub.stop()
        _BASELINES[target] = ws.build_candidate(summaries, baseline_id=BASELINE_ID)["baseline"]
    return json.loads(json.dumps(_BASELINES[target]))


# --------------------------------------------------------------- stub replies

def lines_of(scenario: str) -> tuple:
    guarded = run_step("guard_input", {"notes": CORPUS[scenario].notes})
    return guarded["lines"], {flag["line"] for flag in guarded["flags"]}


def pipeline_stub_with_changes(changed: dict):
    """The pipeline stub, with a different step-2 reply for the scenarios in `changed`
    (scenario -> a function that edits the dry-run facts). A scenario is recognised
    by its step-2 request, which carries its notes."""
    stub = stubs.pipeline_stub()
    dry = stub.ok_response

    def ok_response(step: str, body: dict) -> dict:
        reply = dry(step, body)
        if step == "extract_facts":
            asked = body["messages"][0]["content"][0]["text"]
            for scenario, change in changed.items():
                if asked == extract.notes_as_user_text(lines_of(scenario)[0]):
                    change(reply["output"]["message"]["content"][0]["toolUse"]["input"])
        return reply
    stub.ok_response = ok_response
    return stub


def advice_the_notes_do_not_document(scenario: str, value: str = "Invented advice."):
    """A quality change, invented: step 2 reports one line of the notes as documented
    advice. For a scenario whose gold route is the fall-back, the safety-net scorer
    then fails it on `route`, and no call fails."""
    lines, flagged = lines_of(scenario)
    line = next(lid for lid in sorted(lines) if lines[lid].strip() and lid not in flagged)

    def change(facts: dict) -> None:
        facts["facts"].append({"section": "documented_advice", "value": value,
                               "cites": [{"lines": [line], "quote": lines[line].strip()[:80]}]})
    return change


def broken_facts(facts: dict) -> None:
    """A step-2 reply the pipeline cannot use: the facts lose a required field. The
    call itself succeeds; the generation fails."""
    facts.pop("field_status", None)


def v1_text_with_an_invented_dose(scenario: str) -> str | None:
    """A v1 combined reply, invented, whose PART A lists a gold drug that has no
    documented dose, with a dose. The medication scorer, unchanged, counts that as
    an invented dose. None when this scenario's gold cannot give one."""
    gold = json.loads((G.GOLD_DIR / f"{scenario}.json").read_text("utf-8"))
    drugs = [d["drug"] for d in gold["medications"]["drugs"] if not d.get("dose")]
    if not drugs:
        return None
    listed = "\n".join(f"- {drug} 999 mg" for drug in drugs)
    text = (f"PART A - DISCHARGE SUMMARY\n\nInvented stub summary. {SENTINEL}\n\n"
            f"MEDICATIONS ON DISCHARGE\n{listed}\n\n"
            "PART B - GP LETTER\n\nInvented stub letter.\n\n"
            "PART C - PATIENT VERSION\n\nInvented stub leaflet.\n")
    scored = MD.score(MD.view_from_v1(text), gold)
    return text if scored["invented_doses"] > 0 else None


def v1_stub_with_text(changed: dict):
    """The v1 stub, with another combined reply for the scenarios in `changed`
    (scenario -> text). A scenario is recognised by its notes in the request."""
    stub = stubs.v1_stub()
    usual = stub.ok_response

    def ok_response(step: str, body: dict) -> dict:
        reply = usual(step, body)
        if step == "combined":
            asked = body["messages"][0]["content"][0]["text"]
            for scenario, text in changed.items():
                if asked == CORPUS[scenario].notes:
                    reply["output"]["message"]["content"][0]["text"] = text
        return reply
    stub.ok_response = ok_response
    return stub


def fail_nth(stub, step: str, nth: int, kind: str) -> None:
    """Answer the `nth` request the stub receives for `step` (counting from 1) with
    `kind`, and every other request as usual."""
    receive = stub.receive

    def scripted(name: str) -> str:
        usual = receive(name)
        return kind if name == step and stub.received.count(step) == nth else usual
    stub.receive = scripted


def on_nth(stub, step: str, nth: int, action) -> None:
    """Run `action()` as the `nth` request for `step` arrives, before it is answered."""
    receive = stub.receive

    def acting(name: str) -> str:
        usual = receive(name)
        if name == step and stub.received.count(step) == nth:
            action()
        return usual
    stub.receive = acting


# ------------------------------------------------------------------ the world

class World:
    def __init__(self, net, tmp_path: Path, monkeypatch, start: datetime = START):
        self.net, self.tmp = net, tmp_path
        self.log = fakes.SequenceLog()
        self.s3, self.ssm = fakes.S3Fake(), fakes.SsmFake()
        self.cw, self.sns = fakes.CloudWatchFake(), fakes.SnsFake()
        self._servers = []
        for name, server in (("s3", self.s3), ("ssm", self.ssm), ("cloudwatch", self.cw),
                             ("sns", self.sns)):
            self._start(name, server)
        self.stub = None
        self.clock = Clock(start)
        self.runner_now = lambda: QUIET             # what the runners read as "now"
        self.lines: list = []
        self.config_path = reserve.CONFIG_PATH      # a test may point this at a copy
        self.runs_dir = tmp_path / "runs"
        self.frozen = tmp_path / "frozen"
        self.runs_dir.mkdir()
        self.frozen.mkdir()
        self.environ = {entry.BUCKET_VARIABLE: BUCKET, entry.TOPIC_VARIABLE: TOPIC,
                        wm.COMMIT_VARIABLE: COMMIT, wm.DIGEST_VARIABLE: DIGEST}
        self.endpoints = {"s3": self.s3.url, "ssm": self.ssm.url, "cloudwatch": self.cw.url,
                          "sns": self.sns.url}
        # Every stub reply times out in seconds.
        monkeypatch.setattr(rpe, "READ_TIMEOUT_S", READ_TIMEOUT_S)
        monkeypatch.setattr(rpe, "READ_TIMEOUT_5A_S", READ_TIMEOUT_S)
        monkeypatch.setattr(reserve, "V1_READ_TIMEOUT_S", READ_TIMEOUT_S)

    def _start(self, name: str, server) -> None:
        stubs.started(server, self.net)
        self.log.watch(name, server)
        self._servers.append(server)

    def bedrock(self, target: str, stub=None):
        """Start the Bedrock stub for this test (the default one for `target`, or the
        one given) and point the entrypoint at it."""
        assert self.stub is None, "one Bedrock stub per test"
        self.stub = stub or default_stub(target)
        self._start("bedrock", self.stub)
        self.endpoints["bedrock"] = self.stub.url
        return self.stub

    def expect(self, target: str, **changes) -> None:
        self.ssm.put(wm.EXPECTED_PARAMETER[target], expected_json(target, **changes))

    def freeze(self, target: str, baseline: dict, rules: dict | None = None) -> None:
        (self.frozen / entry.BASELINE_NAME.format(target=target)).write_bytes(ws.render(baseline))
        (self.frozen / entry.RULES_NAME.format(target=target)).write_bytes(
            ws.render(rules or invented_rules(target)))

    def deps(self) -> entry.Deps:
        return entry.Deps(environ=self.environ, endpoints=self.endpoints, wall=self.clock.wall,
                          monotonic=self.clock.monotonic, runner_now=lambda: self.runner_now(),
                          runs_dir=self.runs_dir, baseline_dir=self.frozen,
                          config_path=self.config_path, out=self.lines.append)

    def call(self, argv: list) -> int:
        started = time.monotonic()
        code = entry.main(argv, self.deps())
        assert time.monotonic() - started < RUN_LIMIT_S, "the entrypoint waited on something"
        return code

    def run(self, target: str, when: str = WHEN) -> int:
        return self.call(["scheduled", "--target", target, "--scheduled-time", when])

    def check(self, target: str, when: str = CHECK_WHEN) -> int:
        return self.call(["check", "--target", target, "--scheduled-time", when])

    def ready(self, target: str, tmp_path_factory, stub=None, baseline: dict | None = None):
        """Everything a scheduled run needs: the stub, the expected values in SSM,
        and a frozen baseline with invented rules."""
        self.bedrock(target, stub)
        self.expect(target)
        self.freeze(target, baseline or invented_baseline(target, self.net, tmp_path_factory))
        return self

    # -- what happened
    def entry_of(self, run: str, month: str = "2026-12") -> dict:
        ledger = self.s3.json(store.ledger_key(month))
        return next(e for e in ledger["entries"] if e["run_id"] == run)

    def failed(self, run: str) -> dict:
        return self.s3.json(store.failed_key(run))

    def metric(self, name: str) -> list:
        return self.cw.values(name)

    def published(self) -> dict:
        """Metric name -> the values published for it, in order."""
        out: dict = {}
        for name, value, _dims in self.cw.datums():
            out.setdefault(name, []).append(value)
        return out

    def bedrock_requests(self) -> int:
        return len(self.stub.received) if self.stub is not None else 0

    def stop(self) -> None:
        for server in self._servers:
            server.stop()
