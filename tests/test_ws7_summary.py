"""evals/ws7_summary.py - the run summary and the candidate baseline (WS7 slice 2 part 4).

Offline. Every batch here is INVENTED: the real runners, given the argv the
entrypoint will hand them, run against the local stub (tests/ws7_stub.py) with
`runs_dir` in a temporary folder, and the real scorers, unchanged, score that fresh
batch. Nothing is read from evals/runs/, and no test makes an AWS call.

Two layers:

- END TO END. A stub batch through the real runners and the real scorers. It proves
  that the fields the summary reads exist in real scorer output, and it is the
  acceptance check: three summaries make a candidate, and the real comparator
  (`ws7_compare.main`) compares a fourth with it.
- RULES. One scored stub batch is copied for each test, which then writes invented
  values into one score file, one record or the guard's report, and builds a
  summary. Every refusal has a partner that is not refused.

What these tests cannot prove: real token and cost figures, and `review` or
`partial` verdicts from real model output. The stub's replies are the runners' own
dry-run replies.
"""

from __future__ import annotations

import builtins
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "tests", ROOT / "src" / "generate", ROOT / "evals"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold as G  # noqa: E402
import haz03_check as H3  # noqa: E402
import run_cold_eval as rce  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402
import score_assemble as SA  # noqa: E402
import score_batch as SB  # noqa: E402
import score_compose as CP  # noqa: E402
import score_step2_advice as S2  # noqa: E402
import ws7_client as wc  # noqa: E402
import ws7_compare as compare  # noqa: E402
import ws7_ledger as wl  # noqa: E402
import ws7_manifest as wm  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
import ws7_store as store  # noqa: E402
import ws7_stub as stubs  # noqa: E402
import ws7_summary as ws  # noqa: E402
from pipeline import extract, run_step  # noqa: E402
from ws7_stub import THROTTLE, ERROR_500, offline_aws  # noqa: E402,F401  (a fixture)

CFG = reserve.load_config()
MARGIN = CFG.input_allowance_margin_percent
QUIET = rce.dt.datetime(2026, 12, 16, 4, 0, tzinfo=rce.LONDON)     # clear of both canary windows
READ_TIMEOUT_S = 5.0          # a reply that never comes fails in seconds
RUN_LIMIT_S = 60.0            # a runner call that takes longer has waited on something
CHILD_LIMIT_S = 120.0         # one child process: import, score and build
TARGETS = ("pipeline", "v1")
RUN_ID = {t: f"2026-12-16T0400-{t}" for t in TARGETS}              # invented runs
BASELINE_RUN = {t: f"2026-12-14T0130-{t}-manual" for t in TARGETS}
LABEL = {t: f"{RUN_ID[t]}.main" for t in TARGETS}
SENTINEL = "WS7-SUMMARY-SENTINEL text that must never be carried"
P, F, N = "pass", "fail", "no_output"
CORPUS = {s.id: s for s in rce.load_corpus(rce.DEFAULT_CORPUS_PATH)[0]}
SCENARIOS = sorted(CORPUS)
USAGE = stubs.HONEST_USAGE    # what the stub reports for every reply: invented

RUN_METRICS = {"cost_usd_micros", "tokens_in_total", "tokens_out_total", "tokens_cache_read_total",
               "tokens_cache_write_total", "calls_not_response", "calls_refused",
               "spend_guard_trips", "elapsed_p90_ms", "generations_failed", "gold_draft_records",
               "month_assurance_waived"}
SHARED_METRICS = {"safety_net_verdict", "sn_invented_triggers", "sn_pinned_line_missing",
                  "sn_route_incorrect", "sn_review", "meds_verdict", "meds_invented_doses",
                  "meds_invented_frequencies", "meds_partial", "meds_review"}
PIPELINE_STEPS = ("guard_input", "extract_facts", "validate_facts", "retrieve_evidence",
                  "route_resus", "select_safety_net", "reconcile_medications", "compose_clinical",
                  "compose_patient")
MODEL_STEPS = {"pipeline": ("extract_facts", "reconcile_medications", "compose_clinical",
                            "compose_patient"),
               "v1": ("combined", "patient_v2")}
HAZ03_KINDS = ("age_group_mismatch", "wrong_audience_label", "wrong_pinned_line",
               "expected_line_missing_part_c", "expected_line_missing_part_ab",
               "other_line_in_part_ab", "other_line_in_part_c")
PIPELINE_ONLY = (
    {f"step_failures_{s}" for s in PIPELINE_STEPS}
    | {"step2_route_verdict", "step2_gold_items_recalled", "step2_right_route_wrong_item",
       "step2_not_extracted", "step2_lost_at_verification", "step2_non_advice_as_advice"}
    | {"compose_not_reached", "compose_year_added", "compose_boxes_incorrect",
       "compose_unknown_fact_ids", "compose_excluded_fact_ids", "compose_contradictions_missing",
       "compose_field_omitted", "compose_gp_action_missing_in_b", "compose_checks_total",
       "compose_flagged_advice", "compose_flagged_resus", "compose_flagged_medication"}
    | {"haz03_verdict", "haz03_failures"} | {f"haz03_{k}" for k in HAZ03_KINDS}
    | {"assembly_not_bundled", "assembly_upstream_missing", "assembly_gate_failed",
       "assembly_placeholders_left", "assembly_rejected_08a", "assembly_rejected_08b",
       "assembly_gate_clean", "assembly_gate_documented_advice"})
EXPECTED_METRICS = {
    "pipeline": RUN_METRICS | SHARED_METRICS | PIPELINE_ONLY
    | {f"input_tokens_max_{s}" for s in MODEL_STEPS["pipeline"]},
    "v1": RUN_METRICS | SHARED_METRICS | {"sn_gate_failures"}
    | {f"input_tokens_max_{s}" for s in MODEL_STEPS["v1"]}}
VERDICT_METRICS = {"pipeline": {"safety_net_verdict", "meds_verdict", "step2_route_verdict",
                                "haz03_verdict"},
                   "v1": {"safety_net_verdict", "meds_verdict"}}
# The JSON files a summary may open, by name. Nothing else: no .md, no review file.
READABLE = {"manifest.json", "batch.json", "safety_net.json", "meds.json", "step2_advice.json",
            "compose_clinical.json", "assemble.json", "haz03.json"} | {f"{s}.json" for s in SCENARIOS}


# ------------------------------------------------------------------ fixtures

@pytest.fixture(autouse=True)
def _offline(offline_aws, monkeypatch):  # noqa: F811
    """Every test is offline, and every stub reply times out in seconds."""
    monkeypatch.setattr(rpe, "READ_TIMEOUT_S", READ_TIMEOUT_S)
    monkeypatch.setattr(rpe, "READ_TIMEOUT_5A_S", READ_TIMEOUT_S)
    monkeypatch.setattr(reserve, "V1_READ_TIMEOUT_S", READ_TIMEOUT_S)
    return offline_aws


def _never(_prompt):
    raise AssertionError("a WS7 test must never be asked to confirm a live run")


def own(target: str) -> dict:
    """The job's own values, as the match check returns them, for the argv it checked."""
    return wm.own_values(target, wm.runner_argv(target, CFG, preflight=False, batch="b"), CFG)


def make_guard(target: str, run_id: str, generations: int = 19) -> wc.Guard:
    return wc.Guard(reserve.plan(target), margin_percent=MARGIN, run_id=run_id,
                    reservation_nanos=reserve.reservation(target, generations, MARGIN)["total_usd_nanos"],
                    deadline=time.monotonic() + 10 * RUN_LIMIT_S)


def run_batch(target: str, stub, runs_dir: Path, guard: wc.Guard, label: str,
              scenario: str | None = None) -> Path:
    """One runner call, as part 5 will make it: the allow-listed argv, the guard's
    client, and the guard's batch label as the runner's batch ID."""
    runner = rpe if target == "pipeline" else rce
    argv = wm.runner_argv(target, CFG, preflight=False, batch=label)
    if scenario is not None:                       # a pre-flight: one scenario, by its ID
        argv = [scenario] + argv[1:]
    started = time.monotonic()
    guard.begin_batch(label, 1 if scenario else len(SCENARIOS))
    try:
        runner.main(argv, client_factory=wc.client_factory(guard, endpoint_url=stub.url),
                    now=lambda: QUIET, runs_dir=runs_dir, sleep=lambda _s: None, confirm=_never)
    finally:
        guard.end_batch()
    assert time.monotonic() - started < RUN_LIMIT_S, "the runner waited on something"
    return runs_dir / label


def score(batch_dir: Path, target: str) -> None:
    """The real scorers, unchanged, on a fresh stub batch: what part 5 will run."""
    SB.write_reports(batch_dir, SB.score_batch(batch_dir))
    if target == "pipeline":
        SA.write(batch_dir / "scores" / "assemble", SA.replay(batch_dir, False, None))
        ws.write_haz03(batch_dir)


def default_stub(target: str):
    return stubs.pipeline_stub() if target == "pipeline" else stubs.v1_stub()


class Case:
    """One scored batch in a test's own folder, with what a summary is built from."""

    def __init__(self, target: str, batch_dir: Path, report: dict, label: str):
        self.target, self.dir, self.report, self.label = target, batch_dir, report, label
        self.own = own(target)
        self.waived = False

    def build(self) -> dict:
        return ws.build_summary(self.dir, own=self.own, report=self.report,
                                batch_label=self.label, month_assurance_waived=self.waived)

    def path(self, *name: str) -> Path:
        return self.dir.joinpath(*name)

    def edit(self, *name: str):
        """Change one JSON file in the batch: `with case.edit("scores", "meds.json") as d:`."""
        return _Edit(self.path(*name))

    def row(self, file: tuple, scenario: str, field: str = "scenario_id") -> dict:
        data = json.loads(self.path(*file).read_text("utf-8"))
        return next(r for r in data["results"] if r[field] == scenario)


class _Edit:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.data = json.loads(self.path.read_text("utf-8"))
        return self.data

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")


_PRISTINE: dict = {}


@pytest.fixture
def scored(_offline, tmp_path, tmp_path_factory):
    """`scored(target)`: a fresh copy of one scored stub batch of 18, made once per
    target under this module's offline rules and copied for each test."""
    copies: list = []

    def get(target: str) -> Case:
        if target not in _PRISTINE:
            runs_dir = tmp_path_factory.mktemp(f"ws7-summary-{target}")
            stub = stubs.started(default_stub(target), _offline)
            try:
                guard = make_guard(target, RUN_ID[target])
                batch_dir = run_batch(target, stub, runs_dir, guard, LABEL[target])
            finally:
                stub.stop()
            score(batch_dir, target)
            _PRISTINE[target] = (batch_dir, json.loads(json.dumps(guard.report())))
        source, report = _PRISTINE[target]
        copies.append(target)
        batch_dir = tmp_path / f"runs{len(copies)}" / source.name     # each call: its own copy
        shutil.copytree(source, batch_dir)
        return Case(target, batch_dir, copy.deepcopy(report), LABEL[target])
    return get


def stopped(call, code: str) -> store.Stop:
    with pytest.raises(store.Stop) as exc:
        call()
    assert exc.value.code in ws.CODES and exc.value.code == code, exc.value.line()
    return exc.value


def count(summary: dict, metric: str) -> int:
    return summary["metrics"][metric]["count"]


def verdicts(summary: dict, metric: str) -> dict:
    return summary["metrics"][metric]["verdicts"]


# ============================================================ THE RUN SUMMARY ==

@pytest.mark.parametrize("target", TARGETS)
def test_a_summary_is_a_run_summary_the_comparator_accepts(scored, target):
    case = scored(target)
    summary = case.build()
    assert summary["schema"] == "ws7-run/1" and summary["run_id"] == LABEL[target]
    assert compare.check_run(json.loads(ws.render(summary))) == summary
    # The control: the same check refuses the same summary with one float in it.
    broken = json.loads(ws.render(summary))
    broken["metrics"]["cost_usd_micros"]["count"] = 1.5
    with pytest.raises(compare.CompareError):
        compare.check_run(broken)


@pytest.mark.parametrize("target", TARGETS)
def test_the_metric_set_is_fixed_for_each_target(scored, target):
    """Every metric is always there, 0 when nothing was counted: a baseline needs the
    same metrics in all three runs, and a ruled metric missing from a run is an error."""
    summary = scored(target).build()
    assert set(summary["metrics"]) == EXPECTED_METRICS[target]
    assert {m for m, v in summary["metrics"].items() if "verdicts" in v} == VERDICT_METRICS[target]
    assert all(compare._METRIC_RE.fullmatch(m) for m in summary["metrics"])


def test_a_v1_summary_has_no_pipeline_only_family(scored):
    """HAZ-03, step-2 advice, composition and assembly do not exist for v1."""
    summary = scored("v1").build()
    assert not set(summary["metrics"]) & PIPELINE_ONLY
    assert "sn_gate_failures" in summary["metrics"]                 # v1's own


@pytest.mark.parametrize("target", TARGETS)
def test_every_verdict_metric_has_a_verdict_for_exactly_the_golds_scenarios(scored, target):
    summary = scored(target).build()
    assert sorted(summary["keys"]["gold_sha256"]) == SCENARIOS
    for metric in VERDICT_METRICS[target]:
        assert sorted(verdicts(summary, metric)) == SCENARIOS, metric
        assert set(verdicts(summary, metric).values()) <= {P, F, N}


# ----------------------------------------------------------------- the keys

@pytest.mark.parametrize("target", TARGETS)
def test_the_summarys_keys_are_the_manifests_keys(scored, target):
    """One function builds both (`ws7_manifest.build_keys`), from the same parts."""
    case = scored(target)
    when = rce.dt.datetime(2026, 12, 16, 4, 0, tzinfo=rce.dt.timezone.utc)
    held = wl.Claim(target, "scheduled", "2026-12-16T04:00:00Z",
                    store.run_id(target, "scheduled", when), "c" * 32, "2026-12-16T04:00:37Z",
                    store.claim_key(target, "scheduled", when))
    built = wm.build_manifest(
        held=held, own=case.own, cfg=CFG, baseline_id=None, month_assurance_waived=False,
        who=wm.identity({wm.COMMIT_VARIABLE: "0" * 40, wm.DIGEST_VARIABLE: "sha256:" + "ab" * 32}),
        reservation=reserve.reservation(target, 19, MARGIN), **ws.key_parts(case.dir))
    summary = case.build()
    assert summary["keys"] == built["keys"]
    assert ws.render(summary["keys"]) == ws.render(built["keys"])        # and byte for byte
    assert set(summary["keys"]) == set(compare._TARGET_KEYS[target])


@pytest.mark.parametrize("target", TARGETS)
def test_each_key_comes_from_the_batch_its_scores_or_the_scorers_constants(scored, target):
    case = scored(target)
    keys = case.build()["keys"]
    manifest = json.loads(case.path("manifest.json").read_text("utf-8"))
    assert keys["corpus_sha256"] == manifest["corpus"]["sha256"] \
        == hashlib.sha256(rce.DEFAULT_CORPUS_PATH.read_bytes()).hexdigest()
    gold_map = json.loads(case.path("scores", "safety_net.json").read_text("utf-8"))["gold"]
    assert keys["gold_sha256"] == {sid: meta["sha256"] for sid, meta in gold_map.items()}
    assert keys["gold_sha256"]["S1"] == hashlib.sha256(
        (G.GOLD_DIR / "S1.json").read_bytes()).hexdigest()
    versions = {"score_batch": SB.SCORER_VERSION}
    if target == "pipeline":
        versions.update(score_step2_advice=S2.METRIC_VERSION, score_compose=CP.METRIC_VERSION,
                        haz03_check=H3.CHECK_VERSION,
                        # no version constant: the sha256 of the scorer's own bytes
                        score_assemble=hashlib.sha256(
                            (ROOT / "evals" / "score_assemble.py").read_bytes()).hexdigest())
    assert keys["scorer_versions"] == versions
    assert "score_patient" not in keys["scorer_versions"]      # PART C is not published
    for name in ("target", "model_id", "prompt_caching"):
        assert keys[name] == case.own[name]


def test_build_keys_returns_a_copy_and_refuses_values_out_of_shape():
    parts = {"corpus_sha256": "a" * 64, "gold_sha256": {"S1": "b" * 64},
             "scorer_versions": {"score_batch": 2}}
    keys = wm.build_keys(own("pipeline"), **parts)
    keys["gold_sha256"]["S2"] = "c" * 64
    assert parts["gold_sha256"] == {"S1": "b" * 64}                       # never aliased
    for change in ({"corpus_sha256": SENTINEL}, {"gold_sha256": {}},
                   {"scorer_versions": {"score_batch": 2.5}}):
        with pytest.raises(store.Stop) as exc:
            wm.build_keys(own("pipeline"), **dict(parts, **change))
        assert exc.value.code == wm.MANIFEST_INVALID and SENTINEL not in exc.value.line()
    with pytest.raises(store.Stop):
        wm.build_keys({"target": "pipeline"}, **parts)


@pytest.mark.parametrize("change", [
    {"model_id": "another.model-1"},
    {"prompt_caching": False},
    {"target": "v1"},
])
def test_a_batch_that_is_not_what_the_jobs_own_values_say_builds_no_summary(scored, change):
    case = scored("pipeline")
    assert case.build()                                                   # the partner
    case.own = dict(case.own, **change)
    stopped(case.build, ws.OWN_INVALID)


@pytest.mark.parametrize("prompt", ["system_prompt", "patient_system_prompt"])
def test_a_v1_batch_made_with_another_prompt_builds_no_summary(scored, prompt):
    case = scored("v1")
    assert case.build()
    case.own = dict(case.own, v1_prompt_sha256=dict(case.own["v1_prompt_sha256"], **{prompt: "9" * 64}))
    stopped(case.build, ws.OWN_INVALID)


@pytest.mark.parametrize("file,field", [
    (("scores", "safety_net.json"), "scorer_version"),
    (("scores", "meds.json"), "scorer_version"),
    (("scores", "step2_advice.json"), "metric_version"),
    (("scores", "compose_clinical.json"), "metric_version"),
    (("scores", "haz03.json"), "check_version"),
])
def test_scores_stamped_with_another_scorer_version_build_no_summary(scored, file, field):
    """The keys carry the scorers' constants, so the scores must be from those versions."""
    case = scored("pipeline")
    assert case.build()
    with case.edit(*file) as data:
        data[field] += 1
    stopped(case.build, ws.SCORES_INVALID)


@pytest.mark.parametrize("file", [("scores", "safety_net.json"), ("scores", "meds.json"),
                                  ("scores", "step2_advice.json"),
                                  ("scores", "compose_clinical.json"), ("scores", "haz03.json"),
                                  ("scores", "assemble", "assemble.json")])
def test_another_batchs_scores_or_no_scores_build_no_summary(scored, file):
    case = scored("pipeline")
    with case.edit(*file) as data:
        data["batch" if file[1] == "assemble" else "batch_id"] = "another-batch"
    stopped(case.build, ws.SCORES_INVALID)
    case.path(*file).unlink()
    stopped(case.build, ws.SCORES_INVALID)


# ------------------------------------------------------- the verdict mapping

SN, MEDS = ("scores", "safety_net.json"), ("scores", "meds.json")
STEP2, HAZ03 = ("scores", "step2_advice.json"), ("scores", "haz03.json")
ASSEMBLE, COMPOSE = ("scores", "assemble", "assemble.json"), ("scores", "compose_clinical.json")


def set_verdict(case: Case, file: tuple, scenario: str, verdict: str) -> None:
    with case.edit(*file) as data:
        next(r for r in data["results"] if r["scenario_id"] == scenario)["verdict"] = verdict


def drop_row(case: Case, file: tuple, scenario: str, field: str = "scenario_id") -> None:
    with case.edit(*file) as data:
        data["results"] = [r for r in data["results"] if r[field] != scenario]


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("scorer_says,written,reviews", [
    ("pass", P, 0), ("fail", F, 0),
    ("review", F, 1),         # waiting for a CSO ruling: not a pass
])
def test_the_safety_net_verdict_is_the_scorers_own(scored, target, scorer_says, written, reviews):
    case = scored(target)
    set_verdict(case, SN, "S2", scorer_says)
    summary = case.build()
    assert verdicts(summary, "safety_net_verdict")["S2"] == written
    assert count(summary, "sn_review") == reviews                # the reason stays visible


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("scorer_says,written,partial,review", [
    ("pass", P, 0, 0), ("fail", F, 0, 0),
    ("partial", F, 1, 0),     # a tag missing or imprecise: not a pass
    ("review", F, 0, 1),
])
def test_the_medication_verdict_is_the_scorers_own(scored, target, scorer_says, written, partial,
                                                   review):
    case = scored(target)
    baseline = case.build()
    set_verdict(case, MEDS, "S2", scorer_says)
    summary = case.build()
    assert verdicts(summary, "meds_verdict")["S2"] == written
    assert count(summary, "meds_partial") == count(baseline, "meds_partial") + partial
    assert count(summary, "meds_review") == count(baseline, "meds_review") + review


@pytest.mark.parametrize("route_correct,written", [(True, P), (False, F)])
def test_the_step_2_verdict_is_route_correct(scored, route_correct, written):
    case = scored("pipeline")
    with case.edit(*STEP2) as data:
        next(r for r in data["results"] if r["scenario_id"] == "S2")["route_correct"] = route_correct
    assert verdicts(case.build(), "step2_route_verdict")["S2"] == written


@pytest.mark.parametrize("scorer_says,written", [("pass", P), ("fail", F)])
def test_the_haz03_verdict_is_the_checks_own(scored, scorer_says, written):
    case = scored("pipeline")
    with case.edit(*HAZ03) as data:
        row = next(r for r in data["results"] if r["scenario_id"] == "S2")
        row["verdict"], row["failures"] = scorer_says, [] if scorer_says == "pass" else ["wrong_pinned_line"]
    assert verdicts(case.build(), "haz03_verdict")["S2"] == written


def test_a_scorers_not_scored_is_written_no_output(scored):
    case = scored("pipeline")
    assert N not in verdicts(case.build(), "haz03_verdict").values()      # the partner
    with case.edit(*HAZ03) as data:
        data["results"] = [r if r["scenario_id"] != "S2" else
                           H3.check_record({"steps": {}}, {"scenario_id": "S2"})   # the check's own row
                           for r in data["results"]]
    with case.edit(*STEP2) as data:
        data["results"] = [r if r["scenario_id"] != "S2" else
                           dict(S2.score_record({"steps": {}}, {"scenario_id": "S2"}),
                                generation_id="x", scenario_id="S2", run=1)
                           for r in data["results"]]
    summary = case.build()
    assert verdicts(summary, "haz03_verdict")["S2"] == N
    assert verdicts(summary, "step2_route_verdict")["S2"] == N
    assert verdicts(summary, "haz03_verdict")["S3"] != N                 # only the one scenario


@pytest.mark.parametrize("file,metric,field", [
    (SN, "safety_net_verdict", "scenario_id"), (MEDS, "meds_verdict", "scenario_id"),
    (STEP2, "step2_route_verdict", "scenario_id"), (HAZ03, "haz03_verdict", "scenario_id"),
])
def test_a_scenario_with_no_scored_row_is_written_no_output_never_left_out(scored, file, metric,
                                                                          field):
    """A skipped record (score_batch.py:156-161) has no row in the scorer's results."""
    case = scored("pipeline")
    assert verdicts(case.build(), metric)["S2"] in (P, F)                # the partner
    drop_row(case, file, "S2", field)
    summary = case.build()
    assert verdicts(summary, metric)["S2"] == N
    assert sorted(verdicts(summary, metric)) == SCENARIOS                # still every scenario
    assert compare.check_run(json.loads(ws.render(summary)))


@pytest.mark.parametrize("file", [SN, MEDS, HAZ03])
def test_a_verdict_the_mapping_does_not_list_builds_no_summary(scored, file):
    """Nothing is guessed: not a new verdict, not a misspelt one, not free text."""
    case = scored("pipeline")
    for unknown in ("skip", "skipped", "PASS", SENTINEL, None, 1):
        set_verdict(case, file, "S2", unknown)
        assert SENTINEL not in stopped(case.build, ws.SCORES_INVALID).line()
    set_verdict(case, file, "S2", "pass")
    assert case.build()                                                   # the partner


def test_two_rows_for_a_scenario_or_a_row_outside_the_gold_build_no_summary(scored):
    case = scored("pipeline")
    with case.edit(*MEDS) as data:
        data["results"].append(dict(data["results"][0]))
    stopped(case.build, ws.SCORES_INVALID)
    case = scored("v1")
    assert case.build()
    with case.edit(*MEDS) as data:
        data["results"][0]["scenario_id"] = "S99"
    stopped(case.build, ws.SCORES_INVALID)


# -------------------------------------------- the count metrics, by family

def test_safety_netting_counts_come_from_the_scorers_rows(scored):
    case = scored("pipeline")
    before = case.build()
    with case.edit(*SN) as data:
        row = next(r for r in data["results"] if r["scenario_id"] == "S1")
        row["seek_help"]["n_invented"] = 3
        row["failures"] = ["invented_trigger", "pinned_line_missing"]
        row["route"]["correct"] = False
    after = case.build()
    assert count(after, "sn_invented_triggers") == count(before, "sn_invented_triggers") + 3
    assert count(after, "sn_pinned_line_missing") == count(before, "sn_pinned_line_missing") + 1
    assert count(after, "sn_route_incorrect") == count(before, "sn_route_incorrect") + 1


def test_v1s_gate_failures_are_the_current_gate_on_the_stored_output(scored):
    case = scored("v1")
    rows = json.loads(case.path(*SN).read_text("utf-8"))["results"]
    expected = sum(r["gate"]["now_ok"] is not True for r in rows)
    assert count(case.build(), "sn_gate_failures") == expected
    with case.edit(*SN) as data:
        data["results"][0]["gate"]["now_ok"] = not data["results"][0]["gate"]["now_ok"]
    assert count(case.build(), "sn_gate_failures") != expected


def test_medication_counts_come_from_the_scorers_rows(scored):
    case = scored("pipeline")
    with case.edit(*MEDS) as data:
        data["results"][0]["invented_doses"] = 2
        data["results"][1]["invented_doses"] = 1
        data["results"][1]["invented_frequencies"] = 4
    summary = case.build()
    assert (count(summary, "meds_invented_doses"), count(summary, "meds_invented_frequencies")) == (3, 4)


def test_step_2_counts_come_from_the_scorers_rows(scored):
    case = scored("pipeline")
    rows = json.loads(case.path(*STEP2).read_text("utf-8"))["results"]
    summary = case.build()
    assert count(summary, "step2_gold_items_recalled") == sum(r["verified"]["n_gold_matched"] for r in rows)
    for error in S2.ROUTE_ERRORS:
        assert count(summary, f"step2_{error}") == sum(r["route_error"] == error for r in rows)
    with case.edit(*STEP2) as data:
        data["results"][0]["verified"]["n_gold_matched"] += 5
        data["results"][0]["right_route_wrong_item"] = True
    after = case.build()
    assert count(after, "step2_gold_items_recalled") == count(summary, "step2_gold_items_recalled") + 5
    assert count(after, "step2_right_route_wrong_item") == 1


def test_composition_counts_come_from_the_scorers_summary(scored):
    case = scored("pipeline")
    with case.edit(*COMPOSE) as data:
        sm = data["summary"]
        sm["year_added_step6"]["findings"] = 2
        sm["boxes_correct"] = sm["n_scored"] - 1
        sm["unknown_fact_ids"], sm["excluded_fact_ids"] = 3, 4
        sm["contradictions"] = {"given": 7, "in_a": 5}
        sm["field_omitted"], sm["gp_action_missing_in_b"] = 6, 8
        for kind, n in (("advice", 9), ("resus", 10), ("medication", 11)):
            sm["review"][kind]["flagged"] = n
        data["results"][0]["checks"] = {"box_not_exact": 2, "year_added": 1}
        data["results"][1]["checks"] = {}
    summary = case.build()
    got = {m: count(summary, m) for m in summary["metrics"] if m.startswith("compose_")}
    rows = json.loads(case.path(*COMPOSE).read_text("utf-8"))["results"]
    assert got == {
        "compose_not_reached": 0, "compose_year_added": 2, "compose_boxes_incorrect": 1,
        "compose_unknown_fact_ids": 3, "compose_excluded_fact_ids": 4,
        "compose_contradictions_missing": 2, "compose_field_omitted": 6,
        "compose_gp_action_missing_in_b": 8, "compose_flagged_advice": 9,
        "compose_flagged_resus": 10, "compose_flagged_medication": 11,
        "compose_checks_total": sum(sum(r["checks"].values()) for r in rows)}


def test_a_record_that_did_not_reach_step_6_is_counted(scored):
    case = scored("pipeline")
    with case.edit(*COMPOSE) as data:
        data["results"] = data["results"][1:]
        data["summary"]["n_scored"] -= 1
        data["summary"]["boxes_correct"] -= 1
    assert count(case.build(), "compose_not_reached") == 1


def test_haz03_counts_every_failure_label_and_each_kind(scored):
    case = scored("pipeline")
    rows = json.loads(case.path(*HAZ03).read_text("utf-8"))["results"]
    summary = case.build()
    assert count(summary, "haz03_failures") == sum(len(r["failures"]) for r in rows) > 0
    for kind in HAZ03_KINDS:      # the two PART C kinds are published (ruling R1, 20 Nov 2026)
        assert count(summary, f"haz03_{kind}") == sum(kind in r["failures"] for r in rows)
    assert count(summary, "haz03_other_line_in_part_c") > 0     # the stub batch does exercise one


def test_assembly_publishes_outcomes_rejections_by_step_the_gate_and_placeholders(scored):
    case = scored("pipeline")
    clean = case.build()
    assert count(clean, "assembly_not_bundled") == 0
    assert count(clean, "assembly_gate_clean") + count(clean, "assembly_gate_documented_advice") == 18
    with case.edit(*ASSEMBLE) as data:
        by = {r["scenario"]: r for r in data["results"]}
        by["S1"].update(outcome="rejected", step="08a", code="box_structure")
        by["S2"].update(outcome="rejected", step="08b", code="safety_net_gate_failed")
        by["S3"].update(outcome="rejected", step="08b", code="placeholder_unfilled")
        by["S4"].update(outcome="upstream_missing", step="compose_clinical", code="upstream_failed")
        for sid in ("S1", "S2", "S3", "S4"):
            by[sid].pop("gate", None)
        data["summary"]["placeholders_left"] = 5
    summary = case.build()
    got = {m: count(summary, m) for m in summary["metrics"] if m.startswith("assembly_")}
    gates = [r["gate"]["status"] for r in json.loads(case.path(*ASSEMBLE).read_text("utf-8"))["results"]
             if "gate" in r]
    assert got == {"assembly_not_bundled": 4, "assembly_rejected_08a": 1, "assembly_rejected_08b": 2,
                   "assembly_upstream_missing": 1, "assembly_gate_failed": 1,
                   "assembly_placeholders_left": 5, "assembly_gate_clean": gates.count("clean"),
                   "assembly_gate_documented_advice": gates.count("documented_advice")}
    # By step only (ruling R4): no metric is named after a rejection code.
    assert not any("box_structure" in m or "placeholder_unfilled" in m for m in summary["metrics"])


# ----------------------------------------------------- the Run family: cost

@pytest.mark.parametrize("target", TARGETS)
def test_tokens_and_cost_are_the_batchs_own_part_of_the_guards_report(scored, target):
    case = scored(target)
    summary = case.build()
    calls = 18 * len(MODEL_STEPS[target])
    assert count(summary, "tokens_in_total") == calls * USAGE["inputTokens"]
    assert count(summary, "tokens_out_total") == calls * USAGE["outputTokens"]
    assert count(summary, "tokens_cache_read_total") == 0
    assert count(summary, "tokens_cache_write_total") == 0
    for step in MODEL_STEPS[target]:
        assert count(summary, f"input_tokens_max_{step}") == USAGE["inputTokens"]
    one_call = reserve.read_usage(USAGE)["cost_usd_nanos"]
    assert count(summary, "cost_usd_micros") == reserve.to_micros(calls * one_call)
    assert (count(summary, "calls_not_response"), count(summary, "calls_refused"),
            count(summary, "spend_guard_trips")) == (0, 0, 0)


def test_the_cost_is_the_batchs_own_total_not_the_runs(_offline, tmp_path):
    """Ruling R3: the report's total includes the pre-flight (and, for a baseline, the
    other batches). A weekly 18 must compare with a baseline 18."""
    stub = stubs.started(stubs.pipeline_stub(), _offline)
    try:
        guard = make_guard("pipeline", RUN_ID["pipeline"])
        run_batch("pipeline", stub, tmp_path, guard, f"{RUN_ID['pipeline']}.preflight", scenario="S1")
        batch_dir = run_batch("pipeline", stub, tmp_path, guard, LABEL["pipeline"])
    finally:
        stub.stop()
    score(batch_dir, "pipeline")
    report = guard.report()
    case = Case("pipeline", batch_dir, report, LABEL["pipeline"])
    summary = case.build()
    main = report["batches"][LABEL["pipeline"]]["total_usd_nanos"]
    assert count(summary, "cost_usd_micros") == reserve.to_micros(main)
    assert report["total_usd_micros"] > count(summary, "cost_usd_micros")     # the control
    assert count(summary, "tokens_in_total") == 72 * USAGE["inputTokens"]     # 18 x 4, not 19 x 4


def test_a_cost_in_nano_dollars_is_rounded_up_to_micro_dollars(scored):
    case = scored("pipeline")
    row = next(c for c in case.report["calls"] if c["batch"] == case.label)
    row["cost_usd_nanos"] += 1                                    # one nano-dollar more
    case.report["batches"][case.label]["total_usd_nanos"] += 1
    whole = case.report["batches"][case.label]["total_usd_nanos"] // 1000
    assert count(case.build(), "cost_usd_micros") == whole + 1    # never understated


def test_input_tokens_max_is_the_largest_input_side_of_one_call_per_step(scored):
    case = scored("pipeline")
    rows = [c for c in case.report["calls"] if c["step"] == "reconcile_medications"]
    rows[0].update(input_tokens=10, cache_read_tokens=4000, cache_write_tokens=500)
    rows[1].update(input_tokens=3000, cache_read_tokens=0, cache_write_tokens=0)
    summary = case.build()
    assert count(summary, "input_tokens_max_reconcile_medications") == 4510
    assert count(summary, "input_tokens_max_extract_facts") == USAGE["inputTokens"]
    assert count(summary, "tokens_cache_read_total") == 4000


def test_a_spend_guard_refusal_is_counted_as_a_trip(scored):
    case = scored("pipeline")
    refusal = {"seq": 999, "batch": case.label, "generation": None, "step": None,
               "outcome": "refused", "code": wc.SPEND_GUARD, "input_tokens": None,
               "output_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None,
               "cost_usd_nanos": 0, "worst_case": False, "latency_ms": None}
    case.report["calls"] += [refusal, dict(refusal, code=wc.DEADLINE)]
    summary = case.build()
    assert (count(summary, "spend_guard_trips"), count(summary, "calls_refused")) == (1, 2)


# ------------------------------------------- the Run family: the report's own

@pytest.mark.parametrize("damage", [
    lambda r, label: r.update(run_id=None),
    lambda r, label: r.update(run_id="2026-12-16T0400-v1"),                # another run's
    lambda r, label: r.update(target="v1"),
    lambda r, label: r["batches"].pop(label),
    lambda r, label: r["batches"][label].update(planned=17),
    lambda r, label: r["batches"][label].update(total_usd_nanos=r["batches"][label]["total_usd_nanos"] + 1),
    lambda r, label: r["calls"][0].update(outcome="sent"),
    lambda r, label: r["calls"][0].update(outcome=SENTINEL),
    lambda r, label: r["calls"][0].update(step=SENTINEL),
    lambda r, label: r["calls"][0].update(input_tokens=1.5),
    lambda r, label: r["calls"][0].update(input_tokens=True),
    lambda r, label: r.update(unmetered="no"),
    lambda r, label: r.update(calls=None),
])
def test_a_report_that_is_not_this_batchs_own_builds_no_summary(scored, damage):
    case = scored("pipeline")
    assert case.build()                                                   # the partner
    damage(case.report, case.label)
    assert SENTINEL not in stopped(case.build, ws.REPORT_INVALID).line()


def test_the_batch_label_is_the_runners_batch_id_and_starts_with_the_run_id(scored):
    case = scored("pipeline")
    assert case.build()["run_id"] == case.label == json.loads(
        case.path("manifest.json").read_text("utf-8"))["batch_id"]
    for label in (RUN_ID["pipeline"], "another.main", None, SENTINEL):
        case.label = label
        stopped(case.build, ws.REPORT_INVALID)


@pytest.mark.parametrize("damage", [
    lambda r: r.update(unmetered=True),
    lambda r: r.update(usage_unknown=1),
    lambda r: r.update(unmetered_calls=1),
    lambda r: r["calls"][5].update(outcome="response_usage_unknown"),
    lambda r: r["calls"][5].update(outcome="server_error"),
    lambda r: r["calls"][5].update(outcome="read_timeout"),
])
def test_a_report_with_any_usage_unknown_builds_no_summary(scored, damage):
    """That run is unmetered: its cost is an estimate. Part 5 fails it with this code."""
    case = scored("pipeline")
    assert case.build()                                                   # the partner
    damage(case.report)
    assert stopped(case.build, ws.UNMETERED).code == wc.UNMETERED == "ws7_unmetered"


def test_at_the_server_a_500_makes_the_run_unmetered_and_builds_no_summary(_offline, tmp_path):
    stub = stubs.started(stubs.pipeline_stub(), _offline)
    stub.first("reconcile_medications", ERROR_500)
    try:
        guard = make_guard("pipeline", RUN_ID["pipeline"])
        batch_dir = run_batch("pipeline", stub, tmp_path, guard, LABEL["pipeline"])
    finally:
        stub.stop()
    SB.write_reports(batch_dir, SB.score_batch(batch_dir))
    report = guard.report()
    assert report["unmetered"] is True and stub.count("extract_facts") == 1   # nothing after it
    stopped(Case("pipeline", batch_dir, report, LABEL["pipeline"]).build, ws.UNMETERED)


# --------------------------------- a failed generation, made at the server

def throttled_batch(target: str, net, runs_dir: Path) -> tuple:
    """A 429 with ThrottlingException on the first scenario's first step. The run stays
    metered (the request was not admitted), the wrapper refuses the retry, and that
    generation produces nothing to score."""
    stub = stubs.started(default_stub(target), net)
    stub.first(MODEL_STEPS[target][0], THROTTLE)
    try:
        guard = make_guard(target, RUN_ID[target])
        batch_dir = run_batch(target, stub, runs_dir, guard, LABEL[target])
    finally:
        stub.stop()
    first = json.loads((batch_dir / "batch.json").read_text("utf-8"))["generations"][0]["scenario_id"]
    return batch_dir, guard.report(), first


def test_a_throttled_scenario_is_written_no_output_in_every_verdict_metric(_offline, tmp_path):
    """The no_output path, end to end, on the v1 target. Under ruling e2 part 5 fails
    such a run, so it is never compared; its summary is still exact."""
    batch_dir, report, first = throttled_batch("v1", _offline, tmp_path)
    score(batch_dir, "v1")
    summary = Case("v1", batch_dir, report, LABEL["v1"]).build()
    for metric in VERDICT_METRICS["v1"]:
        got = verdicts(summary, metric)
        assert got[first] == N, metric
        assert [s for s, v in got.items() if v == N] == [first], metric     # and only that one
        assert sorted(got) == SCENARIOS
    assert count(summary, "generations_failed") == 1
    assert (count(summary, "calls_not_response"), count(summary, "calls_refused")) == (1, 1)
    # The throttled call is counted at its worst case, as the ledger counts it.
    worst = reserve.worst_case_nanos(reserve.plan("v1").steps[0], MARGIN)
    replies = 17 * 2 * reserve.read_usage(USAGE)["cost_usd_nanos"]
    assert count(summary, "cost_usd_micros") == reserve.to_micros(replies + worst)
    assert count(summary, "tokens_in_total") == 17 * 2 * USAGE["inputTokens"]
    # ... and three such summaries make no candidate.
    runs = [dict(copy.deepcopy(summary), run_id=f"{BASELINE_RUN['v1']}.b{n}") for n in (1, 2, 3)]
    stopped(lambda: ws.build_candidate(runs, baseline_id="b-2026-12-14"), ws.CANDIDATE_NO_OUTPUT)


def test_a_pipeline_batch_with_a_failed_generation_stops_the_replay_and_builds_no_summary(
        _offline, tmp_path):
    """Found in part 4. evals/score_assemble.py stops (a KeyError at `summary()`, line
    202) on any record with an upstream step missing: such a record has no `branches`
    entry. ADR-011 D2 already treats a replay that stops as a failed run. So today a
    pipeline batch with a failed generation has no assembly scores and no summary.
    The scorer is not changed here. If this test fails because the replay no longer
    stops, extend the v1 test above to the pipeline."""
    batch_dir, report, first = throttled_batch("pipeline", _offline, tmp_path)
    SB.write_reports(batch_dir, SB.score_batch(batch_dir))
    ws.write_haz03(batch_dir)
    with pytest.raises(KeyError):
        SA.write(batch_dir / "scores" / "assemble", SA.replay(batch_dir, False, None))
    assert not (batch_dir / "scores" / "assemble" / "assemble.json").exists()
    stopped(Case("pipeline", batch_dir, report, LABEL["pipeline"]).build, ws.SCORES_INVALID)
    # What the other scorers did write is as the summary expects: no row for that scenario.
    rows = json.loads((batch_dir / "scores" / "safety_net.json").read_text("utf-8"))["results"]
    assert first not in {r["scenario_id"] for r in rows} and len(rows) == 17
    haz03 = json.loads((batch_dir / "scores" / "haz03.json").read_text("utf-8"))["results"]
    assert next(r for r in haz03 if r["scenario_id"] == first)["verdict"] == "not_scored"


# ------------------------------------ the Run family: the batch's own records

def set_elapsed(case: Case, values: dict) -> None:
    for sid, seconds in values.items():
        with case.edit("r1", f"{sid}.json") as record:
            record["elapsed_s"] = seconds


@pytest.mark.parametrize("target", TARGETS)
def test_elapsed_p90_is_the_17th_of_18_records_in_whole_milliseconds(scored, target):
    case = scored(target)
    # Invented: 1.001 s, 2.002 s ... 18.018 s, given to the scenarios in no order.
    shuffled = SCENARIOS[5:] + SCENARIOS[:5]
    set_elapsed(case, {sid: round((i + 1) * 1.001, 3) for i, sid in enumerate(shuffled)})
    assert count(case.build(), "elapsed_p90_ms") == 17017


@pytest.mark.parametrize("values,percent,expected", [
    ([7], 90, 7),
    (list(range(1, 11)), 90, 9),            # ceil(9.0) = 9: exactly on a rank
    (list(range(1, 12)), 90, 10),           # ceil(9.9) = 10
    (list(range(1, 19)), 90, 17),           # ceil(16.2) = 17
    (list(range(1, 21)), 90, 18),
    ([30, 10, 20], 50, 20),                 # sorted first
    ([30, 10, 20], 100, 30),
])
def test_the_percentile_is_nearest_rank_with_no_interpolation(values, percent, expected):
    assert ws.nearest_rank(values, percent) == expected
    assert ws.nearest_rank(values, percent) in values


@pytest.mark.parametrize("seconds,ms", [
    (Decimal("0.006"), 6), (Decimal("12.345"), 12345), (0, 0), (3, 3000),
    (Decimal("1.2345"), 1234),      # half-even: 1234.5 rounds to the even 1234
    (Decimal("1.2355"), 1236),      # ... and 1235.5 to the even 1236
    (Decimal("0.0004"), 0),
])
def test_seconds_become_whole_milliseconds_rounded_half_even(seconds, ms):
    assert ws.to_ms(seconds) == ms and type(ws.to_ms(seconds)) is int


@pytest.mark.parametrize("bad", [Decimal("-0.001"), True, "1.5", None, Decimal("NaN"), 1.5])
def test_an_elapsed_time_that_is_not_a_number_of_seconds_builds_no_summary(scored, bad):
    with pytest.raises(store.Stop):
        ws.to_ms(bad)
    case = scored("v1")
    assert case.build()
    with case.edit("r1", "S1.json") as record:
        record["elapsed_s"] = bad if type(bad) in (bool, str, type(None)) else "not a number"
    stopped(case.build, ws.BATCH_INVALID)


def test_a_float_in_a_record_is_read_as_a_decimal_never_as_a_binary_float(scored):
    case = scored("pipeline")
    set_elapsed(case, {sid: 0.001 for sid in SCENARIOS})
    # 1.005 is not exact in binary: float arithmetic gives 1004.9999999999999.
    assert 1.005 * 1000 != 1005
    set_elapsed(case, {SCENARIOS[0]: 1.005, SCENARIOS[1]: 1.005})
    assert count(case.build(), "elapsed_p90_ms") == 1005


def test_step_failures_are_counted_by_step_from_the_records_step_errors(scored):
    case = scored("pipeline")
    assert count(case.build(), "step_failures_compose_patient") == 0
    with case.edit("r1", "S1.json") as record:
        record["step_errors"] = {"compose_clinical": "max_tokens", "compose_patient": "upstream_failed"}
    with case.edit("r1", "S2.json") as record:
        record["step_errors"] = {"compose_patient": "sections_missing"}
    with case.edit("batch.json") as index:
        index["generations"][0]["error_code"] = "max_tokens"
        index["generations"][1]["error_code"] = "sections_missing"
    summary = case.build()
    assert count(summary, "step_failures_compose_clinical") == 1
    # From the runner's record, not from PART C scoring (ruling R9).
    assert count(summary, "step_failures_compose_patient") == 2
    assert count(summary, "generations_failed") == 2
    # By step only (ruling R4): the codes stay in the stored records.
    assert b"max_tokens" not in ws.render(summary) and b"sections_missing" not in ws.render(summary)


def test_draft_gold_and_a_waived_month_are_count_metrics(scored):
    case = scored("pipeline")
    summary = case.build()
    assert (count(summary, "gold_draft_records"), count(summary, "month_assurance_waived")) == (0, 0)
    with case.edit(*SN) as data:
        data["gold"]["S3"]["status"] = "draft"
    case.waived = True
    summary = case.build()
    assert (count(summary, "gold_draft_records"), count(summary, "month_assurance_waived")) == (1, 1)
    for bad in (1, "yes", None):
        case.waived = bad
        stopped(case.build, ws.SUMMARY_INVALID)


# ------------------------------------------------ a batch that is not one of 18

def _stopped_early(case):
    with case.edit("batch.json") as index:
        index["stopped_early"] = True


def _one_generation_missing(case):
    with case.edit("batch.json") as index:
        index["generations"] = index["generations"][:-1]


def _three_runs(case):
    with case.edit("manifest.json") as manifest:
        manifest["runs"] = 3


def _gold_map_short(case):
    with case.edit(*SN) as data:
        del data["gold"]["S2"]


def _gold_map_wide(case):
    with case.edit(*SN) as data:
        data["gold"]["S99"] = dict(data["gold"]["S2"])


@pytest.mark.parametrize("damage", [_stopped_early, _one_generation_missing, _three_runs,
                                    _gold_map_short, _gold_map_wide])
def test_a_batch_that_is_not_exactly_one_run_of_its_scenarios_builds_no_summary(scored, damage):
    """Ruling R7. score_batch's gold map names only the scenarios that were generated
    (score_batch.py:90), so a scenario that was never generated would be missing from
    the key, not written no_output. Such a batch builds nothing."""
    case = scored("pipeline")
    assert case.build()                                                   # the partner
    damage(case)
    stopped(case.build, ws.BATCH_INCOMPLETE)


def _no_manifest(case):
    case.path("manifest.json").unlink()


def _manifest_not_json(case):
    case.path("manifest.json").write_text("{not json", encoding="utf-8")


def _duplicate_key(case):
    case.path("batch.json").write_text(
        case.path("batch.json").read_text("utf-8").replace('"batch_id"', '"stopped_early": false, "batch_id"', 1),
        encoding="utf-8")


def _record_missing(case):
    case.path("r1", "S2.json").unlink()


def _record_of_another_scenario(case):
    with case.edit("r1", "S2.json") as record:
        record["scenario_id"] = "S3"


def _record_path_elsewhere(case):
    with case.edit("batch.json") as index:
        index["generations"][0]["record"] = "../another/S1.json"


def _generator_unknown(case):
    with case.edit("manifest.json") as manifest:
        manifest["generator"] = SENTINEL


@pytest.mark.parametrize("damage", [_no_manifest, _manifest_not_json, _duplicate_key,
                                    _record_missing, _record_of_another_scenario,
                                    _record_path_elsewhere, _generator_unknown])
def test_a_malformed_batch_builds_no_summary(scored, damage):
    case = scored("pipeline")
    assert case.build()                                                   # the partner
    damage(case)
    assert SENTINEL not in stopped(case.build, ws.BATCH_INVALID).line()


# ---------------- a string reaches the summary only through a fixed label set

def _record_step(case, value):
    with case.edit("r1", "S1.json") as record:
        # A JSON object's key is always a string.
        record["step_errors"] = {value if type(value) is str else json.dumps(value): "max_tokens"}


def _sn_failure(case, value):
    with case.edit(*SN) as data:
        data["results"][0]["failures"] = [value]


def _haz03_kind(case, value):
    with case.edit(*HAZ03) as data:
        data["results"][0].update(failures=[value], verdict="fail")


def _step2_error(case, value):
    with case.edit(*STEP2) as data:
        data["results"][0]["route_error"] = value


def _assembly_outcome(case, value):
    with case.edit(*ASSEMBLE) as data:
        data["results"][0]["outcome"] = value


def _assembly_step(case, value):
    with case.edit(*ASSEMBLE) as data:
        data["results"][0].update(outcome="rejected", step=value, code="box_structure")


def _assembly_gate(case, value):
    with case.edit(*ASSEMBLE) as data:
        data["results"][0]["gate"]["status"] = value


def _gold_status(case, value):
    with case.edit(*SN) as data:
        data["gold"]["S1"]["status"] = value


LABEL_FIELDS = [
    # (how to write the field, a label inside its set, the code when it is outside)
    (_record_step, "compose_patient", ws.BATCH_INVALID),       # a record: the 9 step names
    (_sn_failure, "pinned_line_missing", ws.SCORES_INVALID),
    (_haz03_kind, "other_line_in_part_c", ws.SCORES_INVALID),
    (_step2_error, "not_extracted", ws.SCORES_INVALID),
    (_assembly_outcome, "upstream_missing", ws.SCORES_INVALID),
    (_assembly_step, "08a", ws.SCORES_INVALID),
    (_assembly_gate, "clean", ws.SCORES_INVALID),
    (_gold_status, "draft", ws.SCORES_INVALID),
]


@pytest.mark.parametrize("write,inside,code", LABEL_FIELDS)
@pytest.mark.parametrize("outside", [SENTINEL, "step_7", "", "Compose_Patient", 7, None, ["x"]])
def test_a_read_field_holding_anything_outside_its_label_set_builds_no_summary(
        scored, write, inside, code, outside):
    """A string from a record or a score file reaches the summary only by being one of
    a fixed set of labels. The positive control: a label inside the set is read, and
    the summary is built."""
    if write is _step2_error and outside is None:
        outside = "none"                 # None itself is in this field's set: no route error
    case = scored("pipeline")
    write(case, inside)
    assert SENTINEL.encode() not in ws.render(case.build())               # the control: built
    write(case, outside)
    assert SENTINEL not in stopped(case.build, code).line()


def test_the_control_a_label_inside_the_set_changes_the_summary(scored):
    """...so the field in the test above really is read."""
    case = scored("pipeline")
    before = case.build()
    _record_step(case, "compose_patient")
    _haz03_kind(case, "other_line_in_part_ab")
    after = case.build()
    assert count(after, "step_failures_compose_patient") == count(before, "step_failures_compose_patient") + 1
    assert after != before


@pytest.mark.parametrize("target", TARGETS)
def test_every_string_in_a_summary_is_an_id_a_hash_or_a_fixed_label(scored, target):
    case = scored(target)
    summary = json.loads(ws.render(case.build()))
    allowed = {"ws7-run/1", P, F, N, target, case.own["model_id"], LABEL[target]}
    strings: list = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                strings.append(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            strings.append(node)
    walk(summary)
    for text in strings:
        assert (text in allowed or compare._HEX64_RE.fullmatch(text)
                or compare._METRIC_RE.fullmatch(text) or text in SCENARIOS), text
    assert stubs.SENTINEL.encode() not in ws.render(summary)      # the stub's invented output
    if target == "v1":                                            # the control: it is in the record
        assert stubs.SENTINEL in case.path("r1", "S1.json").read_text("utf-8")


# ---------------------------------------------------- PART C is not published

def poison_part_c(case: Case) -> None:
    """Change everything PART C: the patient scores and every assembly warning."""
    with case.edit("scores", "patient_version.json") as data:
        data["summary"] = {"poisoned": SENTINEL}
        data["results"] = [{"scenario_id": "S99", "verdict": SENTINEL}]
    with case.edit(*ASSEMBLE) as data:
        data["summary"]["warnings"] = {"C:poisoned": 999}
        data["summary"]["c_meaning_vs_step7"] = {"records": 999, "match": 0, "only_8b": 999,
                                                 "only_step7": 999}
        for row in data["results"]:
            row["warnings_c"] = 999
            row["warnings"] = {"C:poisoned": 999}
            row["warnings_located"] = [["C", SENTINEL, "poisoned", 1]]
            row["c_meaning_vs_step7"] = {"match": False, "only_8b": 999, "only_step7": 999, "n": 999}
            row["a5"] = {"poisoned": SENTINEL}
            row["branches"] = [SENTINEL]


def test_nothing_from_part_c_scoring_reaches_the_summary(scored):
    """D13: the PART C slice is blocked. The patient scores and every assembly warning
    are never read, so changing all of them changes nothing."""
    case = scored("pipeline")
    clean = ws.render(case.build())
    poison_part_c(case)
    assert ws.render(case.build()) == clean
    # ... and the summary is built with the patient scores not there at all.
    for name in ("patient_version.json", "PATIENT_VERSION.md", "review_patient.md"):
        case.path("scores", name).unlink()
    assert ws.render(case.build()) == clean
    # The control: a field of the same file that IS published does change the bytes.
    with case.edit(*ASSEMBLE) as data:
        data["summary"]["placeholders_left"] = 3
    assert ws.render(case.build()) != clean


def test_no_metric_is_a_part_c_meaning_metric(scored):
    names = set(scored("pipeline").build()["metrics"])
    assert not {n for n in names if n.startswith(("patient", "part_c", "warnings", "c_meaning"))}
    # The two HAZ-03 kinds that name PART C are code checks of step 5b's own output
    # (the line code inserts), published by ruling R1.
    assert {n for n in names if "part_c" in n} == {"haz03_expected_line_missing_part_c",
                                                   "haz03_other_line_in_part_c"}


@pytest.fixture
def opened(monkeypatch):
    """Every file opened for reading through pathlib or open(), by name."""
    seen: list = []
    real_path_open, real_open, real_io_open = Path.open, builtins.open, io.open

    def path_open(self, *args, **kwargs):
        seen.append(Path(self))
        return real_path_open(self, *args, **kwargs)

    def plain_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            seen.append(Path(file))
        return real_open(file, *args, **kwargs)

    def io_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            seen.append(Path(file))
        return real_io_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(builtins, "open", plain_open)
    monkeypatch.setattr(io, "open", io_open)
    return seen


@pytest.mark.parametrize("target", TARGETS)
def test_only_json_files_are_opened_never_a_report_or_a_review_file(scored, opened, target):
    """The .md reports and the review files quote model output."""
    case = scored(target)
    reports = sorted(p.name for p in case.dir.rglob("*.md"))
    assert any(n.startswith("review_") for n in reports) and "BATCH_SUMMARY.md" in reports
    del opened[:]
    case.build()
    inside = [p for p in opened if case.dir in p.parents]
    assert inside, "the recorder saw nothing: it is not recording"
    assert {p.name for p in inside} <= READABLE
    assert not [p for p in opened if p.suffix == ".md" or p.name.startswith("review_")]
    assert "patient_version.json" not in {p.name for p in opened}
    # The positive control: the recorder does see a report being opened.
    del opened[:]
    case.path("scores", "SAFETY_NET.md").read_text("utf-8")
    assert {p.name for p in opened} == {"SAFETY_NET.md"}


# ----------------------------------------------------------------- HAZ-03

def checkers_row(case: Case, scenario: str, gold_dir: Path = G.GOLD_DIR) -> dict:
    """What evals/haz03_check.py itself returns for one record of the batch."""
    record = json.loads(case.path("r1", f"{scenario}.json").read_text("utf-8"))
    return H3.check_record(record, G.load(scenario, gold_dir))


def test_write_haz03_runs_the_check_unchanged_on_every_record(scored):
    """Values inside their sets are written exactly as the checker gave them, with
    the same verdict: the partner of the test below."""
    case = scored("pipeline")
    case.path(*HAZ03).unlink()
    out = ws.write_haz03(case.dir)
    data = json.loads(out.read_text("utf-8"))
    assert out == case.path(*HAZ03) and data["check_version"] == H3.CHECK_VERSION
    assert [r["scenario_id"] for r in data["results"]] == SCENARIOS
    for row in data["results"]:
        assert row == checkers_row(case, row["scenario_id"])              # the check's own row
    assert b"not_in_enum" not in out.read_bytes()
    # Another value inside the set is also written as given.
    with case.edit("r1", "S4.json") as record:
        record["steps"]["validate_facts"]["facts"]["age_group"] = "child"
    ws.write_haz03(case.dir)
    assert case.row(HAZ03, "S4") == checkers_row(case, "S4")
    assert case.row(HAZ03, "S4")["facts_age_group"] == "child"


# A value of every JSON type that is outside every HAZ-03 field's set.
OUTSIDE = {"string": SENTINEL, "integer": 42, "float": 1.5, "boolean": True, "null": None,
           "list": [SENTINEL], "object": {"value": SENTINEL}}
# ... and values that look like one inside a set, and are not: the wrong case, the
# wrong type, a label wrapped in a list, the other boolean, zero.
NEAR_MISSES = ["Adult", "", ["adult"], {"value": "adult"}, False, 0, 2, 2.0]


@pytest.mark.parametrize("outside", list(OUTSIDE.values()) + NEAR_MISSES,
                         ids=list(OUTSIDE) + [f"near{n}" for n in range(len(NEAR_MISSES))])
def test_a_value_outside_its_enum_in_a_record_never_reaches_haz03_json(scored, outside):
    """Codex P2, PR #8, and its re-check. With step 5b present the checker copies the
    facts' `age_group` into its row as it finds it (haz03_check.py:88), whatever its
    type. A record holds model output, so `write_haz03` writes the fixed label
    `not_in_enum` in its place, and keeps the checker's verdict and failures exactly.
    A number or a boolean is not written as given either."""
    case = scored("pipeline")
    with case.edit("r1", "S4.json") as record:
        assert "select_safety_net" in record["steps"]                     # step 5b is there
        record["steps"]["validate_facts"]["facts"]["age_group"] = outside
    given = checkers_row(case, "S4")
    # The positive control: the checker's own row does carry the raw value, as given.
    assert given["facts_age_group"] == outside and type(given["facts_age_group"]) is type(outside)
    if outside == SENTINEL:
        assert SENTINEL in json.dumps(given)

    out = ws.write_haz03(case.dir)
    assert SENTINEL.encode() not in out.read_bytes()
    row = case.row(HAZ03, "S4")
    assert row["facts_age_group"] == "not_in_enum" and type(row["facts_age_group"]) is str
    assert (row["verdict"], row["failures"]) == (given["verdict"], given["failures"])
    assert row == dict(given, facts_age_group="not_in_enum")              # nothing else changed
    assert case.row(HAZ03, "S3") == checkers_row(case, "S3")              # nor any other row
    # The summary reads the file as before: the verdict is still the checker's.
    assert verdicts(case.build(), "haz03_verdict")["S4"] == given["verdict"] == F


def test_a_gold_value_outside_its_enum_never_reaches_haz03_json(scored, tmp_path):
    case = scored("pipeline")
    gold_dir = tmp_path / "gold"
    shutil.copytree(G.GOLD_DIR, gold_dir, ignore=shutil.ignore_patterns("adjudications"))
    gold = json.loads((gold_dir / "S4.json").read_text("utf-8"))
    gold["age_group"]["value"] = SENTINEL
    (gold_dir / "S4.json").write_text(json.dumps(gold), encoding="utf-8")
    with case.edit(*SN) as data:          # ...and the gold map says that is the gold scored against
        data["gold"]["S4"]["sha256"] = hashlib.sha256((gold_dir / "S4.json").read_bytes()).hexdigest()
    given = checkers_row(case, "S4", gold_dir)
    assert given["gold_age_group"] == SENTINEL                            # the control
    out = ws.write_haz03(case.dir, gold_dir)
    assert SENTINEL.encode() not in out.read_bytes()
    assert case.row(HAZ03, "S4") == dict(given, gold_age_group="not_in_enum")


# The two rows the checker can return, with invented values inside every set.
HAZ03_SCORED_ROW = {"check": "HAZ-03", "check_version": H3.CHECK_VERSION, "scenario_id": "S2",
                    "gold_age_group": "infant", "facts_age_group": "child",
                    "expected_audience": "paediatric",
                    "failures": ["age_group_mismatch", "other_line_in_part_c"], "verdict": "fail"}
HAZ03_NOT_SCORED_ROW = H3.check_record({"steps": {}}, {"scenario_id": "S2"})
HAZ03_FIELDS = sorted(set(HAZ03_SCORED_ROW) | set(HAZ03_NOT_SCORED_ROW))


def test_the_two_rows_above_are_the_checkers_own_shapes():
    """So the test below does cover every field `write_haz03` writes."""
    gold = {"scenario_id": "S2", "age_group": {"value": "infant"}}
    real = H3.check(gold, "child", {"audience": "paediatric", "pinned_line": "x"})
    assert set(real) == set(HAZ03_SCORED_ROW)
    assert set(HAZ03_NOT_SCORED_ROW) == {"check", "check_version", "scenario_id", "failures",
                                         "verdict", "reason"}
    assert HAZ03_FIELDS == sorted(["check", "check_version", "scenario_id", "gold_age_group",
                                   "facts_age_group", "expected_audience", "failures", "verdict",
                                   "reason"])


@pytest.mark.parametrize("field", HAZ03_FIELDS)
@pytest.mark.parametrize("json_type", list(OUTSIDE))
def test_every_haz03_field_holding_a_value_of_any_type_outside_its_set_is_written_not_in_enum(
        field, json_type):
    """Codex's re-check of PR #8. The allowlist comes first, for every field and every
    JSON type: there is no type that is written as given."""
    outside = OUTSIDE[json_type]
    base = HAZ03_SCORED_ROW if field in HAZ03_SCORED_ROW else HAZ03_NOT_SCORED_ROW
    given = dict(copy.deepcopy(base), **{field: outside})
    assert given[field] == outside and type(given[field]) is type(outside)   # the control: raw
    written = ws.haz03_row(given, "S2")
    assert written[field] == "not_in_enum" and type(written[field]) is str
    assert {k: v for k, v in written.items() if k != field} \
        == {k: v for k, v in base.items() if k != field}                  # nothing else changed
    assert SENTINEL not in json.dumps(written)
    # The whole file: the same row through the renderer write_haz03 uses.
    assert SENTINEL.encode() not in ws.render({"results": [written]})


@pytest.mark.parametrize("field,near_miss", [
    ("check_version", True), ("check_version", float(H3.CHECK_VERSION)),
    ("check_version", str(H3.CHECK_VERSION)), ("check_version", H3.CHECK_VERSION + 1),
    ("check_version", 0),
    ("facts_age_group", ["adult"]), ("facts_age_group", "Adult"), ("facts_age_group", False),
    ("gold_age_group", 0), ("expected_audience", "everyone"), ("expected_audience", ["adult"]),
    ("verdict", ["pass"]), ("verdict", "review"), ("verdict", True),
    ("failures", "age_group_mismatch"),                       # the right label, not in a list
    ("failures", ["age_group_mismatch", SENTINEL]),           # one label that is not the checker's
    ("failures", [["age_group_mismatch"]]), ("failures", [1]), ("failures", [None]),
    ("scenario_id", "S3"), ("scenario_id", ["S2"]),
    ("check", "haz-03"), ("reason", "no_step_5a"),
])
def test_a_haz03_value_that_only_looks_like_one_in_its_set_is_written_not_in_enum(field, near_miss):
    base = HAZ03_SCORED_ROW if field in HAZ03_SCORED_ROW else HAZ03_NOT_SCORED_ROW
    written = ws.haz03_row(dict(copy.deepcopy(base), **{field: near_miss}), "S2")
    assert written[field] == "not_in_enum"


def test_a_haz03_field_with_no_allowlist_may_hold_nothing():
    for value in (SENTINEL, 7, 1.5, True, None, [], {}):
        written = ws.haz03_row(dict(HAZ03_SCORED_ROW, a_new_field=value), "S2")
        assert written == dict(HAZ03_SCORED_ROW, a_new_field="not_in_enum")


def test_haz03_values_inside_their_sets_are_written_unchanged_with_the_same_verdict():
    """The partner. Every value a field may hold is written exactly as given."""
    assert ws.haz03_row(HAZ03_SCORED_ROW, "S2") == HAZ03_SCORED_ROW
    assert ws.haz03_row(HAZ03_NOT_SCORED_ROW, "S2") == HAZ03_NOT_SCORED_ROW   # `reason` included
    for field, allowed in ws.HAZ03_FIELD_SETS.items():
        base = HAZ03_SCORED_ROW if field in HAZ03_SCORED_ROW else HAZ03_NOT_SCORED_ROW
        for value in allowed:
            row = dict(base, **{field: value})
            assert ws.haz03_row(row, "S2") == row, (field, value)
    for failures in ([], list(HAZ03_KINDS), ["wrong_pinned_line"]):
        row = dict(HAZ03_SCORED_ROW, failures=failures, verdict="fail" if failures else "pass")
        written = ws.haz03_row(row, "S2")
        assert written == row and written["failures"] is failures       # exactly as given
    # Each fixed set is the checker's own, or the schema's it reads: nothing is copied here.
    assert ws.HAZ03_FIELD_SETS["gold_age_group"] == G.AGE_GROUPS
    assert set(ws.HAZ03_FIELD_SETS["facts_age_group"]) == set(G.AGE_GROUPS) \
        >= set(H3.PAEDIATRIC_GROUPS) | {"adult"}
    assert set(ws.HAZ03_FIELD_SETS["expected_audience"]) == set(H3.LINES) \
        == {H3.expected_audience(g) for g in G.AGE_GROUPS}
    assert set(ws.HAZ03_FIELD_SETS["verdict"]) == {"pass", "fail", "not_scored"}
    assert set(HAZ03_FIELDS) == set(ws.HAZ03_FIELD_SETS) | {"check_version", "scenario_id", "failures"}


def test_haz03_is_scored_against_the_gold_score_batch_scored_against(scored, tmp_path):
    case = scored("pipeline")
    gold_dir = tmp_path / "gold"
    shutil.copytree(G.GOLD_DIR, gold_dir, ignore=shutil.ignore_patterns("adjudications"))
    assert ws.write_haz03(case.dir, gold_dir)                             # the partner
    (gold_dir / "S2.json").write_bytes((gold_dir / "S2.json").read_bytes() + b"\n")
    stopped(lambda: ws.write_haz03(case.dir, gold_dir), ws.SCORES_INVALID)


def test_haz03_does_not_cover_v1(scored):
    stopped(lambda: ws.write_haz03(scored("v1").dir), ws.BATCH_INVALID)


# ------------------------------------------------------- the same bytes

CHILD = """
import json, sys
from pathlib import Path
sys.path[:0] = [{evals!r}, {generate!r}]
import score_assemble as SA, score_batch as SB, ws7_summary as ws
batch, own, report, label = Path(sys.argv[1]), *(json.loads(Path(p).read_text()) for p in sys.argv[2:4]), sys.argv[4]
SB.write_reports(batch, SB.score_batch(batch))
SA.write(batch / "scores" / "assemble", SA.replay(batch, False, None))
ws.write_haz03(batch)
sys.stdout.buffer.write(ws.render(ws.build_summary(
    batch, own=own, report=report, batch_label=label, month_assurance_waived=False)))
"""


def test_the_summarys_bytes_are_the_same_whatever_the_hash_seed(scored, tmp_path):
    """score_batch's own output order is not fixed: score_batch.py:90 iterates a set.
    Each child process scores its own copy of one unscored batch under its own
    PYTHONHASHSEED, then builds the summary."""
    case = scored("pipeline")
    shutil.rmtree(case.path("scores"))
    (tmp_path / "own.json").write_text(json.dumps(case.own), encoding="utf-8")
    (tmp_path / "report.json").write_text(json.dumps(case.report), encoding="utf-8")
    script = CHILD.format(evals=str(ROOT / "evals"), generate=str(ROOT / "src" / "generate"))
    orders, summaries = [], []
    for seed in ("0", "1", "2", "3"):
        batch = tmp_path / f"seed{seed}" / case.dir.name
        shutil.copytree(case.dir, batch)
        child = subprocess.run(
            [sys.executable, "-c", script, str(batch), str(tmp_path / "own.json"),
             str(tmp_path / "report.json"), case.label],
            env=dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, timeout=CHILD_LIMIT_S)     # a child that hangs fails the test
        assert child.returncode == 0, child.stderr.decode("utf-8", "replace")[-2000:]
        summaries.append(child.stdout)
        orders.append(tuple(json.loads((batch / "scores" / "safety_net.json").read_text("utf-8"))["gold"]))
    assert len(set(orders)) > 1, "the control: the scorer's own output order did differ"
    assert len(set(summaries)) == 1 and summaries[0].endswith(b"\n")
    assert compare.check_run(json.loads(summaries[0]))


def test_a_summary_is_checked_with_the_comparators_own_check_before_it_is_returned(scored,
                                                                                  monkeypatch):
    """Ruling R8: `build_summary` checks its output with `ws7_compare.check_run`."""
    case = scored("v1")
    real, seen = compare.check_run, []

    def spy(data):
        seen.append(data)
        return real(data)
    monkeypatch.setattr(compare, "check_run", spy)
    summary = case.build()
    assert len(seen) == 1 and seen[0] is summary                 # the very object returned

    def refuse(_data):
        raise compare.CompareError("shape_invalid", "run", "metrics")
    monkeypatch.setattr(compare, "check_run", refuse)
    stopped(case.build, ws.SUMMARY_INVALID)                      # what it refuses is not returned


def test_render_is_the_comparators_own_form(scored):
    summary = scored("v1").build()
    assert ws.render(summary) == compare.render(summary)
    assert ws.render(summary) == (json.dumps(summary, sort_keys=True, indent=2) + "\n").encode("ascii")


# ====================================================== THE CANDIDATE BASELINE ==

def three(case: Case) -> list:
    """Three run summaries of one manual baseline: one run, three batches (ruling e8),
    so three batch labels and three run_ids. Invented from one scored batch."""
    summary = case.build()
    return [dict(copy.deepcopy(summary), run_id=f"{BASELINE_RUN[case.target]}.b{n}") for n in (1, 2, 3)]


def candidate(summaries: list, baseline_id: str = "b-2026-12-14") -> dict:
    return ws.build_candidate(summaries, baseline_id=baseline_id)


@pytest.mark.parametrize("target", TARGETS)
def test_three_run_summaries_make_a_candidate_whose_baseline_the_comparator_accepts(scored, target):
    runs = three(scored(target))
    runs[1]["metrics"]["cost_usd_micros"]["count"] += 7
    runs[2]["metrics"]["tokens_out_total"]["count"] -= 1
    built = candidate(runs)
    assert set(built) == {"schema", "run_ids", "baseline"}
    assert built["schema"] == "ws7-baseline-candidate/1"
    assert built["run_ids"] == [f"{BASELINE_RUN[target]}.b{n}" for n in (1, 2, 3)]
    baseline = built["baseline"]
    assert compare.check_baseline(json.loads(ws.render(baseline))) == baseline
    assert (baseline["schema"], baseline["baseline_id"]) == ("ws7-baseline/1", "b-2026-12-14")
    assert baseline["keys"] == runs[0]["keys"] and set(baseline["metrics"]) == EXPECTED_METRICS[target]
    cost = runs[0]["metrics"]["cost_usd_micros"]["count"]
    assert baseline["metrics"]["cost_usd_micros"] == {"counts": [cost, cost + 7, cost]}   # in run order
    out = runs[0]["metrics"]["tokens_out_total"]["count"]
    assert baseline["metrics"]["tokens_out_total"] == {"counts": [out, out, out - 1]}


def test_verdicts_become_three_item_lists_and_the_unstable_list_is_worked_out(scored):
    runs = three(scored("pipeline"))
    for run, (s1, s2, s3, s4) in zip(runs, [(P, P, F, F), (P, F, F, P), (P, P, F, P)]):
        run["metrics"]["meds_verdict"]["verdicts"].update(S1=s1, S2=s2, S3=s3, S4=s4)
    meds = candidate(runs)["baseline"]["metrics"]["meds_verdict"]
    assert meds["verdicts"]["S1"] == [P, P, P] and meds["verdicts"]["S2"] == [P, F, P]
    assert meds["verdicts"]["S3"] == [F, F, F] and meds["verdicts"]["S4"] == [F, P, P]
    assert "S1" not in meds["unstable"]                    # passed all three: stable
    assert {"S2", "S3", "S4"} <= set(meds["unstable"])     # anything else, a triple fail included
    assert meds["unstable"] == sorted(s for s, v in meds["verdicts"].items() if v != [P, P, P])
    assert sorted(meds["verdicts"]) == SCENARIOS


def test_the_comparator_refuses_the_candidate_envelope_and_accepts_its_baseline_member(scored, tmp_path):
    """A candidate can never be read as a frozen baseline by accident (ruling R5)."""
    case = scored("pipeline")
    runs = three(case)
    built = candidate(runs)
    run = dict(copy.deepcopy(runs[0]), run_id=LABEL["pipeline"])
    (tmp_path / "run.json").write_bytes(ws.render(run))
    (tmp_path / "rules.json").write_bytes(ws.render(invented_rules("pipeline")))
    (tmp_path / "candidate.json").write_bytes(ws.render(built))
    (tmp_path / "baseline.json").write_bytes(ws.render(built["baseline"]))

    code, result = compare_files(tmp_path, "candidate.json", "refused.json")
    assert code == 2 and result["status"] == "error" and result["comparisons"] == []
    assert [(e["code"], e["file"]) for e in result["errors"]] == [("shape_invalid", "baseline")]
    # The partner: the same run and rules against the member the author would copy out.
    code, result = compare_files(tmp_path, "baseline.json", "accepted.json")
    assert code == 0 and result["status"] == "no_breach"


def test_a_candidates_baseline_is_checked_with_the_comparators_own_check_before_it_is_returned(
        scored, monkeypatch):
    """Ruling R8: `build_candidate` checks its `baseline` member with
    `ws7_compare.check_baseline` before it returns anything."""
    runs = three(scored("v1"))
    real, seen = compare.check_baseline, []

    def spy(data):
        seen.append(data)
        return real(data)
    monkeypatch.setattr(compare, "check_baseline", spy)
    built = candidate(runs)
    assert len(seen) == 1 and seen[0] is built["baseline"]       # the very member returned

    def refuse(_data):
        raise compare.CompareError("unstable_list_inconsistent", "baseline", "metrics.unstable")
    monkeypatch.setattr(compare, "check_baseline", refuse)
    stopped(lambda: candidate(runs), ws.CANDIDATE_SUMMARY_INVALID)


# ------------------------------------------------------------ its refusals

@pytest.mark.parametrize("pick", [lambda r: r[:2], lambda r: r + [r[0]], lambda r: [], lambda r: None,
                                  lambda r: {"a": r[0], "b": r[1], "c": r[2]}])
def test_a_candidate_needs_exactly_three_run_summaries(scored, pick):
    runs = three(scored("v1"))
    assert candidate(runs)                                                # the partner
    stopped(lambda: candidate(pick(runs)), ws.NOT_THREE_RUNS)


def _other_gold(run):
    run["keys"]["gold_sha256"]["S1"] = "9" * 64


def _other_scorer(run):
    run["keys"]["scorer_versions"]["score_batch"] += 1


def _other_corpus(run):
    run["keys"]["corpus_sha256"] = "9" * 64


def _other_caching(run):
    run["keys"]["prompt_caching"] = not run["keys"]["prompt_caching"]


@pytest.mark.parametrize("change,key", [(_other_gold, "gold_sha256"),
                                        (_other_scorer, "scorer_versions"),
                                        (_other_corpus, "corpus_sha256"),
                                        (_other_caching, "prompt_caching")])
@pytest.mark.parametrize("which", [0, 1, 2])
def test_three_summaries_whose_keys_are_not_equal_make_no_candidate(scored, change, key, which):
    runs = three(scored("pipeline"))
    assert candidate(runs)                                                # the partner
    change(runs[which])
    refused = stopped(lambda: candidate(runs), ws.KEYS_DIFFER)
    assert refused.ids == (key,)                                          # the name, never a value


@pytest.mark.parametrize("which", [0, 1, 2])
@pytest.mark.parametrize("metric", ["safety_net_verdict", "meds_verdict"])
def test_any_no_output_verdict_makes_no_candidate(scored, which, metric):
    """ws7-baseline/1 has no such verdict: a baseline is three whole runs."""
    runs = three(scored("v1"))
    assert candidate(runs)                                                # the partner
    runs[which]["metrics"][metric]["verdicts"]["S9"] = N
    stopped(lambda: candidate(runs), ws.CANDIDATE_NO_OUTPUT)


@pytest.mark.parametrize("which", [0, 1, 2])
def test_any_draft_gold_record_makes_no_candidate(scored, which):
    """score_batch.py: "Scores against draft gold are not the baseline." """
    runs = three(scored("v1"))
    assert candidate(runs)                                                # the partner
    runs[which]["metrics"]["gold_draft_records"]["count"] = 1
    stopped(lambda: candidate(runs), ws.DRAFT_GOLD)


def _metric_removed(run):
    del run["metrics"]["sn_review"]


def _metric_added(run):
    run["metrics"]["an_extra_metric"] = {"count": 0}


def _count_becomes_verdicts(run):
    run["metrics"]["sn_review"] = {"verdicts": dict(run["metrics"]["meds_verdict"]["verdicts"])}


@pytest.mark.parametrize("change", [_metric_removed, _metric_added, _count_becomes_verdicts])
@pytest.mark.parametrize("which", [0, 2])
def test_a_metric_in_some_runs_and_not_in_others_makes_no_candidate(scored, change, which):
    runs = three(scored("v1"))
    assert candidate(runs)                                                # the partner
    change(runs[which])
    stopped(lambda: candidate(runs), ws.METRICS_DIFFER)
    # The same change in all three runs is one metric set again.
    runs = three(scored("v1"))
    for run in runs:
        change(run)
    assert candidate(runs)


@pytest.mark.parametrize("target", TARGETS)
def test_three_summaries_of_a_one_scenario_batch_make_no_candidate(_offline, tmp_path, target):
    """Codex P2, PR #8. A pre-flight batch may be summarised, and three such summaries
    with three labels are three valid, distinct run summaries with equal keys. A
    baseline frozen from them would give "no baseline" for every real run."""
    preflight = CFG.preflight_scenario
    stub = stubs.started(default_stub(target), _offline)
    guard = make_guard(target, BASELINE_RUN[target], generations=57)
    try:
        def summary_of(label: str, scenario: str | None) -> dict:
            batch_dir = run_batch(target, stub, tmp_path, guard, label, scenario=scenario)
            score(batch_dir, target)
            return Case(target, batch_dir, guard.report(), label).build()

        small = [summary_of(f"{BASELINE_RUN[target]}.p{n}", preflight) for n in (1, 2, 3)]
        whole = [summary_of(f"{BASELINE_RUN[target]}.b{n}", None) for n in (1, 2, 3)]
    finally:
        stub.stop()
    for summary in small:               # nothing else is wrong with them
        assert compare.check_run(json.loads(ws.render(summary)))
        assert list(summary["keys"]["gold_sha256"]) == [preflight]
    assert len({s["run_id"] for s in small}) == 3 and small[0]["keys"] == small[1]["keys"] == small[2]["keys"]
    assert N not in json.dumps(small) and all(count(s, "gold_draft_records") == 0 for s in small)

    stopped(lambda: candidate(small), ws.NOT_WHOLE_CORPUS)
    stopped(lambda: candidate([whole[0], whole[1], small[2]]), ws.NOT_WHOLE_CORPUS)    # even one
    # The partner: three full batches of the corpus are accepted.
    built = candidate(whole)
    assert sorted(built["baseline"]["keys"]["gold_sha256"]) == SCENARIOS


def test_the_whole_corpus_is_read_from_the_corpus_file_not_from_a_number(scored, tmp_path):
    """The scenarios come from the corpus file itself (`run_cold_eval.load_corpus`)."""
    runs = three(scored("v1"))
    assert candidate(runs)                                                # the real corpus: 18
    corpus = json.loads(rce.DEFAULT_CORPUS_PATH.read_text("utf-8"))
    assert sorted(item["id"] for item in corpus["scenarios"]) == SCENARIOS

    def corpus_file(name: str, scenarios: list) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps({"scenarios": scenarios}), encoding="utf-8")
        return path

    invented = {"id": "S99", "title": "Invented", "notes": "Invented notes for an invented scenario."}
    one_more = corpus_file("one-more.json", corpus["scenarios"] + [invented])
    stopped(lambda: ws.build_candidate(runs, baseline_id="b-1", corpus_path=one_more),
            ws.NOT_WHOLE_CORPUS)                           # 18 of 19 is not the whole corpus
    one_fewer = corpus_file("one-fewer.json", corpus["scenarios"][:-1])
    stopped(lambda: ws.build_candidate(runs, baseline_id="b-1", corpus_path=one_fewer),
            ws.NOT_WHOLE_CORPUS)                           # nor is a scenario the corpus lacks
    same = corpus_file("same.json", list(reversed(corpus["scenarios"])))
    assert ws.build_candidate(runs, baseline_id="b-1", corpus_path=same)  # the partner
    # A corpus that cannot be read shows nothing, so nothing is built.
    stopped(lambda: ws.build_candidate(runs, baseline_id="b-1", corpus_path=tmp_path / "none.json"),
            ws.NOT_WHOLE_CORPUS)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    stopped(lambda: ws.build_candidate(runs, baseline_id="b-1", corpus_path=tmp_path / "bad.json"),
            ws.NOT_WHOLE_CORPUS)


def test_the_same_summary_given_twice_makes_no_candidate(scored):
    """Ruling R6: a summary's run_id is its batch label, so three summaries of one
    batch cannot pass as three runs, with every scenario looking stable."""
    runs = three(scored("v1"))
    assert candidate(runs)                                                # the partner
    runs[2]["run_id"] = runs[0]["run_id"]
    stopped(lambda: candidate(runs), ws.RUNS_NOT_DISTINCT)


@pytest.mark.parametrize("bad", [None, "", 7, f"b {SENTINEL}", "b" * 65, "b/2026"])
def test_the_baseline_id_is_given_and_is_an_id(scored, bad):
    runs = three(scored("v1"))
    assert candidate(runs, "b-2026-12-14:v1.2")["baseline"]["baseline_id"] == "b-2026-12-14:v1.2"
    assert SENTINEL not in stopped(lambda: candidate(runs, bad), ws.CANDIDATE_ID_INVALID).line()
    with pytest.raises(TypeError):
        ws.build_candidate(runs)                    # no default, and never from the clock


def _unknown_field(run):
    run["note"] = SENTINEL


def _float_count(run):
    run["metrics"]["cost_usd_micros"]["count"] = 1.5


def _verdict_missing_a_scenario(run):
    del run["metrics"]["meds_verdict"]["verdicts"]["S9"]


def _verdict_for_a_scenario_outside_the_gold(run):
    run["metrics"]["meds_verdict"]["verdicts"]["S99"] = P


def _no_draft_count(run):
    del run["metrics"]["gold_draft_records"]


def _not_a_summary(run):
    run.clear()


@pytest.mark.parametrize("change", [_unknown_field, _float_count, _verdict_missing_a_scenario,
                                    _verdict_for_a_scenario_outside_the_gold, _not_a_summary])
def test_a_summary_the_comparator_would_refuse_makes_no_candidate(scored, change):
    runs = three(scored("v1"))
    assert candidate(runs)                                                # the partner
    change(runs[1])
    assert SENTINEL not in stopped(lambda: candidate(runs), ws.CANDIDATE_SUMMARY_INVALID).line()


def test_summaries_that_do_not_say_whether_the_gold_was_draft_make_no_candidate(scored):
    runs = three(scored("v1"))
    for run in runs:
        _no_draft_count(run)
    stopped(lambda: candidate(runs), ws.CANDIDATE_SUMMARY_INVALID)


def test_building_a_candidate_changes_none_of_its_summaries_and_writes_nothing(scored, tmp_path):
    case = scored("v1")
    runs = three(case)
    before = copy.deepcopy(runs)
    files = sorted(p for p in tmp_path.rglob("*"))
    built = candidate(runs)
    assert runs == before and sorted(p for p in tmp_path.rglob("*")) == files
    built["baseline"]["keys"]["gold_sha256"].clear()
    assert runs == before                                         # the candidate is a copy


def test_every_refusal_has_a_fixed_code():
    assert len(set(ws.CODES)) == len(ws.CODES) == 16
    assert all(code.startswith("ws7_") for code in ws.CODES)
    others = set(wm.CODES) | set(wl.CODES) | set(store.CODES)
    assert not set(ws.CODES) & others
    # One code is shared on purpose: an unmetered run is `ws7_unmetered` everywhere.
    assert set(ws.CODES) & set(wc.CODES) == {wc.UNMETERED}


# ========================================================= THE ACCEPTANCE CHECK ==

def invented_rules(target: str) -> dict:
    """An invented rules file: one metric of each kind. No number here is a ruling."""
    return {"schema": "ws7-rules/1", "target": target, "metrics": [
        {"metric": "sn_invented_triggers", "family": "safety_netting", "kind": 1},
        {"metric": "safety_net_verdict", "family": "safety_netting", "kind": 2, "min_flips": 1},
        {"metric": "meds_verdict", "family": "medications", "kind": 2, "min_flips": 1},
        {"metric": "tokens_out_total", "family": "run", "kind": 3, "margin": 0}]}


def compare_files(folder: Path, baseline: str, out: str, run: str = "run.json") -> tuple:
    """The real comparator, through `main`. The output path is fresh, and the exit
    code is returned beside the result so that it is checked before the result is."""
    path = folder / out
    assert not path.exists(), "a result path must be fresh"
    code = compare.main(["--baseline", str(folder / baseline), "--rules", str(folder / "rules.json"),
                         "--run", str(folder / run), "--out", str(path)])
    return code, json.loads(path.read_text("utf-8"))


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


def advice_the_notes_do_not_document(scenario: str):
    """A quality change, invented: step 2 reports one line of the notes as documented
    advice. The citation is real, so step 3 verifies it and step 5b takes the
    documented route. For a scenario whose gold route is the fall-back, the
    safety-net scorer then fails it on `route`."""
    lines, flagged = lines_of(scenario)
    line = next(lid for lid in sorted(lines) if lines[lid].strip() and lid not in flagged)

    def change(facts: dict) -> None:
        facts["facts"].append({"section": "documented_advice", "value": "Invented advice.",
                               "cites": [{"lines": [line], "quote": lines[line].strip()[:80]}]})
    return change


def test_acceptance_three_summaries_make_a_candidate_and_the_comparator_compares_a_fourth(
        _offline, tmp_path):
    """ADR-011, slice 2 part 4. Exit 0 for an unchanged run, 1 for a flipped stable
    scenario, 2 for a run whose keys differ. Every batch is made by the real runner
    against the stub and scored by the real scorers."""
    target = "pipeline"
    changed: dict = {}
    stub = stubs.started(pipeline_stub_with_changes(changed), _offline)
    runs_dir = tmp_path / "runs"
    try:
        def summary_of(guard: wc.Guard, label: str) -> dict:
            batch_dir = run_batch(target, stub, runs_dir, guard, label)
            score(batch_dir, target)
            return Case(target, batch_dir, guard.report(), label).build()

        # The manual baseline: one run, three batches of 18 (D7; ruling e8).
        baseline_guard = make_guard(target, BASELINE_RUN[target], generations=55)
        three_runs = [summary_of(baseline_guard, f"{BASELINE_RUN[target]}.b{n}") for n in (1, 2, 3)]
        built = ws.build_candidate(three_runs, baseline_id="b-2026-12-14")
        # The author's step, done here by the test: copy the baseline member out.
        (tmp_path / "baseline.json").write_bytes(ws.render(built["baseline"]))
        (tmp_path / "rules.json").write_bytes(ws.render(invented_rules(target)))

        # ---- exit 0: an unchanged run
        unchanged = summary_of(make_guard(target, "2026-12-16T0400-pipeline"),
                               "2026-12-16T0400-pipeline.main")
        (tmp_path / "unchanged.json").write_bytes(ws.render(unchanged))
        code, result = compare_files(tmp_path, "baseline.json", "result-unchanged.json", "unchanged.json")
        assert code == 0
        assert (result["status"], result["errors"], result["key_mismatch"]) == ("no_breach", [], [])
        assert len(result["comparisons"]) == 4 and not any(r["breach"] for r in result["comparisons"])

        # ---- exit 1: a quality change on one stable scenario, chosen when the test runs
        safety_net = built["baseline"]["metrics"]["safety_net_verdict"]
        stable = sorted(set(safety_net["verdicts"]) - set(safety_net["unstable"]))
        assert stable, "the stub batch has no scenario that passed safety-netting three times"
        chosen = stable[0]
        changed[chosen] = advice_the_notes_do_not_document(chosen)
        flipped = summary_of(make_guard(target, "2026-12-23T0400-pipeline"),
                             "2026-12-23T0400-pipeline.main")
        # A scorer's own fail on real scorer output: not no_output, and no call failed.
        assert verdicts(flipped, "safety_net_verdict")[chosen] == F
        assert [s for s in SCENARIOS if verdicts(flipped, "safety_net_verdict")[s]
                != verdicts(unchanged, "safety_net_verdict")[s]] == [chosen]
        assert (count(flipped, "calls_not_response"), count(flipped, "calls_refused"),
                count(flipped, "generations_failed")) == (0, 0, 0)
        assert flipped["keys"] == unchanged["keys"]
        (tmp_path / "flipped.json").write_bytes(ws.render(flipped))
        code, result = compare_files(tmp_path, "baseline.json", "result-flipped.json", "flipped.json")
        assert code == 1
        assert result["status"] == "breach"
        row = next(r for r in result["comparisons"] if (r["family"], r["kind"]) == ("safety_netting", 2))
        assert (row["breach"], row["metrics"][0]["flips"], row["metrics"][0]["no_output"]) == (1, [chosen], [])
        changed.clear()

        # ---- exit 2: a run whose keys differ (another pipeline_version)
        other_guard = make_guard(target, "2026-12-30T0400-pipeline")
        batch_dir = run_batch(target, stub, runs_dir, other_guard, "2026-12-30T0400-pipeline.main")
        score(batch_dir, target)
        case = Case(target, batch_dir, other_guard.report(), "2026-12-30T0400-pipeline.main")
        case.own = dict(case.own, pipeline_version="9" * 64)
        (tmp_path / "other.json").write_bytes(ws.render(case.build()))
        code, result = compare_files(tmp_path, "baseline.json", "result-other.json", "other.json")
        assert code == 2
        assert (result["status"], result["key_mismatch"], result["comparisons"]) == (
            "no_baseline", ["pipeline_version"], [])
    finally:
        stub.stop()
