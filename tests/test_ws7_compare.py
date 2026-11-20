"""Tests for evals/ws7_compare.py, the WS7 offline comparator (ADR-011 D9).

Every fixture here is invented and is written in this file. The numbers
(counts, margins, flip thresholds) are invented too: they are not the
author's rule numbers, and no rule number is set by these tests.

Sections follow the task's acceptance criteria, 1 to 8. Every breach test has
a matching non-breach test at its boundary. No test uses the clock or a date.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "evals" / "ws7_compare.py"
sys.path.insert(0, str(ROOT / "evals"))
import ws7_compare as W  # noqa: E402

P, F, N = "pass", "fail", "no_output"
MISSING = object()  # "do not create this file"
SENTINEL = "LEAK free text 0451"

Outcome = namedtuple("Outcome", "result code raw")


# ------------------------------------------------------------ fixtures

def hx(char: str) -> str:
    return char * 64


def keys(target: str = "pipeline") -> dict:
    k = {
        "target": target,
        "model_id": "invented.model-v1:0",
        "prompt_caching": True,
        "corpus_sha256": hx("a"),
        "gold_sha256": {"S1": hx("1"), "S2": hx("2"), "S3": hx("3")},
        "scorer_versions": {"score_batch": 2, "haz03_check": 1},
    }
    if target == "pipeline":
        k["pipeline_version"] = hx("b")
    else:
        k["v1_prompt_sha256"] = {"system_prompt": hx("c"), "patient_system_prompt": hx("d")}
        k["patient_second_pass"] = True
    return k


def keys_for(scenarios, target: str = "pipeline") -> dict:
    """Keys whose gold covers exactly `scenarios` (IDs of the form S<number>). For
    S1, S2 and S3 this is `keys()` itself."""
    k = keys(target)
    k["gold_sha256"] = {s: hx(format(int(s[1:]) % 16, "x")) for s in sorted(scenarios)}
    return k


def k1(metric: str = "invented_doses", family: str = "medications") -> dict:
    return {"metric": metric, "family": family, "kind": 1}


def k2(metric: str = "safety_net_verdict", family: str = "safety_netting",
       min_flips: int = 1) -> dict:
    return {"metric": metric, "family": family, "kind": 2, "min_flips": min_flips}


def k3(metric: str = "placeholders_left", family: str = "assembly", margin: int = 2) -> dict:
    return {"metric": metric, "family": family, "kind": 3, "margin": margin}


def rules(*entries: dict, target: str = "pipeline") -> dict:
    return {"schema": "ws7-rules/1", "target": target, "metrics": list(entries)}


def baseline(metrics: dict, k: dict | None = None) -> dict:
    return {"schema": "ws7-baseline/1", "baseline_id": "b-invented-01",
            "keys": k if k is not None else keys(), "metrics": metrics}


def run_summary(metrics: dict, k: dict | None = None) -> dict:
    return {"schema": "ws7-run/1", "run_id": "r-invented-01",
            "keys": k if k is not None else keys(), "metrics": metrics}


def verdict_metric(by_scenario: dict) -> dict:
    """A baseline verdict metric; `unstable` is worked out here, not by the code under test."""
    return {"verdicts": by_scenario,
            "unstable": [s for s, three in by_scenario.items() if three != [P, P, P]]}


def valid_triple(target: str = "pipeline") -> tuple[dict, dict, dict]:
    """A complete baseline, rules and run with one metric of each kind: no breach."""
    base = baseline({
        "invented_doses": {"counts": [0, 1, 0]},
        "placeholders_left": {"counts": [5, 4, 7]},
        "safety_net_verdict": verdict_metric({"S1": [P, P, P], "S2": [P, P, P], "S3": [P, F, P]}),
    }, keys(target))
    run = run_summary({
        "invented_doses": {"count": 1},
        "placeholders_left": {"count": 6},
        "safety_net_verdict": {"verdicts": {"S1": P, "S2": P, "S3": F}},
    }, keys(target))
    return base, rules(k1(), k2(), k3(), target=target), run


def write_inputs(folder: Path, base, rules_, run) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, content in (("baseline", base), ("rules", rules_), ("run", run)):
        path = folder / f"{name}.json"
        if content is MISSING:
            path.unlink(missing_ok=True)
        elif isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(json.dumps(content), encoding="utf-8")
        paths[name] = path
    paths["out"] = folder / "result.json"
    return paths


def argv(paths: dict) -> list:
    return ["--baseline", str(paths["baseline"]), "--rules", str(paths["rules"]),
            "--run", str(paths["run"]), "--out", str(paths["out"])]


def run_case(folder: Path, base, rules_, run) -> Outcome:
    """Run the comparator in-process. Every result is also checked for
    criterion 7 (IDs, counts and fixed labels only) and for its exit code."""
    paths = write_inputs(folder, base, rules_, run)
    code = W.main(argv(paths))
    raw = paths["out"].read_bytes()
    result = json.loads(raw)
    assert result["exit_code"] == code
    assert_ids_counts_labels_only(result)
    return Outcome(result, code, raw)


def count_case(folder: Path, entry: dict, counts: list, now: int) -> Outcome:
    name = entry["metric"]
    return run_case(folder, baseline({name: {"counts": counts}}), rules(entry),
                    run_summary({name: {"count": now}}))


def verdict_case(folder: Path, entry: dict, at_baseline: dict, now: dict,
                 gold=None) -> Outcome:
    """One ruled verdict metric. The gold covers the baseline's scenarios unless
    `gold` names others: a ruled verdict metric must cover exactly the gold's."""
    name = entry["metric"]
    k = keys_for(at_baseline if gold is None else gold)
    return run_case(folder, baseline({name: verdict_metric(at_baseline)}, k), rules(entry),
                    run_summary({name: {"verdicts": now}}, copy.deepcopy(k)))


def detail(outcome: Outcome, metric: str) -> dict:
    found = [d for row in outcome.result["comparisons"] for d in row["metrics"]
             if d["metric"] == metric]
    assert len(found) == 1
    return found[0]


def row(outcome: Outcome, family: str, kind: int) -> dict:
    found = [r for r in outcome.result["comparisons"]
             if (r["family"], r["kind"]) == (family, kind)]
    assert len(found) == 1
    return found[0]


def assert_no_breach(outcome: Outcome) -> None:
    assert (outcome.result["status"], outcome.code) == ("no_breach", 0)
    assert outcome.result["errors"] == [] and outcome.result["key_mismatch"] == []
    assert all(r["breach"] == 0 for r in outcome.result["comparisons"])


def assert_breach(outcome: Outcome) -> None:
    assert (outcome.result["status"], outcome.code) == ("breach", 1)
    assert any(r["breach"] == 1 for r in outcome.result["comparisons"])


def assert_error(outcome: Outcome, code: str, file: str | None, *, metric: str | None = None,
                 field: str | None = None) -> None:
    assert (outcome.result["status"], outcome.code) == ("error", 2)
    assert outcome.result["comparisons"] == []  # an error is never a partial comparison
    wanted = {"code": code, "file": file}
    if metric is not None:
        wanted["metric"] = metric
    if field is not None:
        wanted["field"] = field
    assert any(all(e[k] == v for k, v in wanted.items()) for e in outcome.result["errors"]), \
        outcome.result["errors"]


# ------------------------------------------ criterion 7: the result's shape

_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_METRIC = re.compile(r"[a-z0-9_]{1,64}")
_SCENARIO = re.compile(r"[A-Za-z0-9_-]{1,32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")

STATUSES = {"no_breach", "breach", "no_baseline", "error"}
FAMILIES = {"run", "safety_netting", "medications", "step2_advice", "composition", "haz03",
            "assembly", "part_c"}
KEY_NAMES = {"target", "pipeline_version", "v1_prompt_sha256", "patient_second_pass",
             "model_id", "prompt_caching", "corpus_sha256", "gold_sha256", "scorer_versions"}
ERROR_CODES = {"file_unreadable", "json_invalid", "shape_invalid", "unstable_list_inconsistent",
               "target_mismatch", "metric_missing", "metric_kind_mismatch",
               "scenario_coverage_differs", "scenario_set_differs", "internal_error"}
ERROR_FIELDS = {
    None, "top", "schema", "target", "baseline_id", "run_id", "keys", "keys.target",
    "keys.pipeline_version", "keys.v1_prompt_sha256", "keys.patient_second_pass",
    "keys.model_id", "keys.prompt_caching", "keys.corpus_sha256", "keys.gold_sha256",
    "keys.scorer_versions", "metrics", "metrics.metric", "metrics.kind", "metrics.entry",
    "metrics.family", "metrics.min_flips", "metrics.margin", "metrics.value",
    "metrics.counts", "metrics.count", "metrics.verdicts", "metrics.unstable",
}
DETAIL_FIELDS = {
    1: {"metric", "breach", "run", "baseline_max"},
    2: {"metric", "breach", "flips", "no_output", "min_flips", "stable", "unstable"},
    3: {"metric", "breach", "direction", "run", "low", "high"},
}


def _count(value) -> bool:
    return type(value) is int


def _matches(value, pattern) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def assert_ids_counts_labels_only(result: dict) -> None:
    """Every field of a result is a fixed label, a count or a validated ID.

    The check is by position, with its own copies of the patterns and label
    sets, so a new field or a free string anywhere in the result fails here."""
    assert set(result) == {"schema", "status", "exit_code", "baseline_id", "run_id", "inputs",
                           "key_mismatch", "errors", "comparisons", "unruled_metrics"}
    assert result["schema"] == "ws7-result/1"
    assert result["status"] in STATUSES
    assert _count(result["exit_code"]) and result["exit_code"] in (0, 1, 2)
    for name in ("baseline_id", "run_id"):
        assert result[name] is None or _matches(result[name], _ID)
    assert set(result["inputs"]) == {"baseline_sha256", "rules_sha256", "run_sha256"}
    assert all(v is None or _matches(v, _HEX64) for v in result["inputs"].values())
    assert all(k in KEY_NAMES for k in result["key_mismatch"])

    for error in result["errors"]:
        assert set(error) == {"code", "file", "field", "metric"}
        assert error["code"] in ERROR_CODES
        assert error["file"] in (None, "baseline", "rules", "run")
        assert error["field"] in ERROR_FIELDS
        assert error["metric"] is None or _matches(error["metric"], _METRIC)

    for r in result["comparisons"]:
        assert set(r) == {"family", "kind", "breach", "metrics"}
        assert r["family"] in FAMILIES and r["kind"] in (1, 2, 3) and _count(r["kind"])
        assert _count(r["breach"]) and r["breach"] in (0, 1)
        for d in r["metrics"]:
            assert set(d) == DETAIL_FIELDS[r["kind"]]
            assert _matches(d["metric"], _METRIC)
            assert _count(d["breach"]) and d["breach"] in (0, 1)
            if r["kind"] == 1:
                assert _count(d["run"]) and _count(d["baseline_max"])
            elif r["kind"] == 2:
                assert _count(d["min_flips"]) and _count(d["stable"])
                assert all(_matches(s, _SCENARIO) for s in d["flips"] + d["no_output"])
                for u in d["unstable"]:
                    assert set(u) == {"scenario", "baseline_passes"}
                    assert _matches(u["scenario"], _SCENARIO)
                    assert _count(u["baseline_passes"]) and u["baseline_passes"] in (0, 1, 2)
            else:
                assert d["direction"] in ("below", "inside", "above")
                assert _count(d["run"]) and _count(d["low"]) and _count(d["high"])

    for u in result["unruled_metrics"]:
        assert set(u) == {"metric", "in_baseline", "in_run"}
        assert _matches(u["metric"], _METRIC)
        assert type(u["in_baseline"]) is bool and type(u["in_run"]) is bool


def test_the_shape_check_itself_rejects_free_text(tmp_path):
    """The guard used by every test below is not a rubber stamp."""
    base, rules_, run = valid_triple()
    run["metrics"]["tokens_out_total"] = {"count": 41250}
    good = run_case(tmp_path, base, rules_, run).result
    for tamper in (
        lambda r: r.update(note=SENTINEL),
        lambda r: r.update(run_id=SENTINEL),
        lambda r: r["comparisons"][0]["metrics"][0].update(metric=SENTINEL),
        lambda r: r["comparisons"][0].update(family="free text"),
        lambda r: r["unruled_metrics"].append({"metric": SENTINEL, "in_baseline": True,
                                               "in_run": True}),
    ):
        bad = copy.deepcopy(good)
        tamper(bad)
        with pytest.raises(AssertionError):
            assert_ids_counts_labels_only(bad)


# ------------------------------------------- criterion 1: kind 1, counters

@pytest.mark.parametrize("counts", [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
def test_kind1_count_above_the_baseline_maximum_is_a_breach(tmp_path, counts):
    outcome = count_case(tmp_path, k1(), counts, 2)
    assert_breach(outcome)
    assert detail(outcome, "invented_doses") == {
        "metric": "invented_doses", "breach": 1, "run": 2, "baseline_max": 1}
    assert row(outcome, "medications", 1)["breach"] == 1


@pytest.mark.parametrize("counts", [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
@pytest.mark.parametrize("now", [1, 0])
def test_kind1_count_equal_to_or_below_the_maximum_is_not_a_breach(tmp_path, counts, now):
    outcome = count_case(tmp_path, k1(), counts, now)
    assert_no_breach(outcome)
    assert detail(outcome, "invented_doses") == {
        "metric": "invented_doses", "breach": 0, "run": now, "baseline_max": 1}


def test_kind1_zero_baseline_one_event_is_a_breach_and_none_is_not(tmp_path):
    assert_breach(count_case(tmp_path / "a", k1(), [0, 0, 0], 1))
    assert_no_breach(count_case(tmp_path / "b", k1(), [0, 0, 0], 0))


# ---------------------------------------------- criterion 2: kind 2, flips

STABLE_AND_ONE_MIXED = {"S1": [P, P, P], "S2": [P, P, P], "S3": [P, F, P]}


def test_kind2_stable_scenario_now_failing_is_a_breach_and_is_named(tmp_path):
    outcome = verdict_case(tmp_path, k2(), STABLE_AND_ONE_MIXED, {"S1": P, "S2": F, "S3": P})
    assert_breach(outcome)
    d = detail(outcome, "safety_net_verdict")
    assert d["flips"] == ["S2"] and d["breach"] == 1 and d["stable"] == 2
    assert row(outcome, "safety_netting", 2)["breach"] == 1


def test_kind2_stable_scenarios_still_passing_is_not_a_breach(tmp_path):
    outcome = verdict_case(tmp_path, k2(), STABLE_AND_ONE_MIXED, {"S1": P, "S2": P, "S3": P})
    assert_no_breach(outcome)
    assert detail(outcome, "safety_net_verdict")["flips"] == []


@pytest.mark.parametrize("three, passes", [
    ([P, P, F], 2), ([P, F, P], 2), ([F, P, P], 2),
    ([P, F, F], 1), ([F, P, F], 1), ([F, F, P], 1),
    ([F, F, F], 0),
])
def test_kind2_scenario_not_stable_at_baseline_is_reported_unstable_not_a_breach(
        tmp_path, three, passes):
    outcome = verdict_case(tmp_path, k2(), {"S1": [P, P, P], "S3": three}, {"S1": P, "S3": F})
    assert_no_breach(outcome)
    d = detail(outcome, "safety_net_verdict")
    assert d["unstable"] == [{"scenario": "S3", "baseline_passes": passes}]
    assert d["flips"] == [] and d["stable"] == 1


def test_kind2_the_same_failure_on_a_stable_scenario_is_a_breach(tmp_path):
    """The boundary partner of the test above: three passes, not two."""
    outcome = verdict_case(tmp_path, k2(), {"S1": [P, P, P], "S3": [P, P, P]},
                           {"S1": P, "S3": F})
    assert_breach(outcome)
    assert detail(outcome, "safety_net_verdict")["flips"] == ["S3"]


FOUR_STABLE = {"S1": [P, P, P], "S2": [P, P, P], "S3": [P, P, P], "S4": [P, P, P]}


def test_kind2_flip_threshold_comes_from_the_rules_file(tmp_path):
    one = {"S1": P, "S2": F, "S3": P, "S4": P}
    two = {"S1": P, "S2": F, "S3": P, "S4": F}

    below = verdict_case(tmp_path / "a", k2(min_flips=2), FOUR_STABLE, one)
    assert_no_breach(below)
    assert detail(below, "safety_net_verdict")["flips"] == ["S2"]  # named even when no breach
    assert detail(below, "safety_net_verdict")["min_flips"] == 2

    at = verdict_case(tmp_path / "b", k2(min_flips=2), FOUR_STABLE, two)
    assert_breach(at)
    assert detail(at, "safety_net_verdict")["flips"] == ["S2", "S4"]

    # The same two flips under a threshold of three: the number is the rule's.
    assert_no_breach(verdict_case(tmp_path / "c", k2(min_flips=3), FOUR_STABLE, two))
    # And one flip under a threshold of one.
    assert_breach(verdict_case(tmp_path / "d", k2(min_flips=1), FOUR_STABLE, one))


def test_kind2_no_output_on_a_stable_scenario_is_a_flip(tmp_path):
    outcome = verdict_case(tmp_path / "a", k2(), STABLE_AND_ONE_MIXED,
                           {"S1": P, "S2": N, "S3": P})
    assert_breach(outcome)
    d = detail(outcome, "safety_net_verdict")
    assert d["flips"] == ["S2"] and d["no_output"] == ["S2"]

    # Boundary: the same scenario with a pass is neither.
    passing = verdict_case(tmp_path / "b", k2(), STABLE_AND_ONE_MIXED,
                           {"S1": P, "S2": P, "S3": P})
    assert_no_breach(passing)
    assert detail(passing, "safety_net_verdict")["no_output"] == []


def test_kind2_no_output_on_an_unstable_scenario_is_listed_and_is_not_a_flip(tmp_path):
    outcome = verdict_case(tmp_path, k2(), STABLE_AND_ONE_MIXED, {"S1": P, "S2": P, "S3": N})
    assert_no_breach(outcome)
    d = detail(outcome, "safety_net_verdict")
    assert d["flips"] == [] and d["no_output"] == ["S3"]
    assert d["unstable"] == [{"scenario": "S3", "baseline_passes": 2}]


def test_kind2_no_output_counts_towards_the_flip_threshold(tmp_path):
    mixed = {"S1": P, "S2": F, "S3": N, "S4": P}
    outcome = verdict_case(tmp_path / "a", k2(min_flips=2), FOUR_STABLE, mixed)
    assert_breach(outcome)
    assert detail(outcome, "safety_net_verdict")["flips"] == ["S2", "S3"]
    assert_no_breach(verdict_case(tmp_path / "b", k2(min_flips=3), FOUR_STABLE, mixed))


def test_kind2_no_output_in_a_baseline_is_an_error(tmp_path):
    base = baseline({"safety_net_verdict": {"verdicts": {"S1": [P, P, N]}, "unstable": ["S1"]}})
    outcome = run_case(tmp_path, base, rules(k2()),
                       run_summary({"safety_net_verdict": {"verdicts": {"S1": P}}}))
    assert_error(outcome, "shape_invalid", "baseline", metric="safety_net_verdict",
                 field="metrics.verdicts")


@pytest.mark.parametrize("now", [
    {"S1": P, "S2": P},                    # S3 has no verdict
    {"S1": P, "S2": P, "S3": P, "S9": P},  # a scenario the baseline does not have
])
def test_kind2_scenario_missing_from_or_added_to_the_run_is_an_error(tmp_path, now):
    outcome = verdict_case(tmp_path, k2(), STABLE_AND_ONE_MIXED, now)
    assert_error(outcome, "scenario_set_differs", "run", metric="safety_net_verdict")


# ---------------- kind 2: a ruled verdict metric covers exactly the gold's scenarios
# Part 3, item 3 (Codex's question on PR #4). The gold here covers S1, S2 and S3.

def _codes(outcome: Outcome) -> list:
    return [(e["code"], e["file"], e["metric"]) for e in outcome.result["errors"]]


TWO_STABLE = {"S1": [P, P, P], "S2": [P, P, P]}
THREE_STABLE = {"S1": [P, P, P], "S2": [P, P, P], "S3": [P, P, P]}
THREE_AND_ONE_MORE = dict(THREE_STABLE, S9=[P, P, P])


def test_kind2_baseline_and_run_leaving_out_the_same_scenario_is_an_error(tmp_path):
    """The gap: the two files agree with each other, so `scenario_set_differs` alone
    says nothing, and S3 would never be looked at."""
    outcome = verdict_case(tmp_path / "a", k2(), TWO_STABLE, {"S1": P, "S2": P},
                           gold=["S1", "S2", "S3"])
    assert_error(outcome, "scenario_coverage_differs", "baseline", metric="safety_net_verdict")
    assert_error(outcome, "scenario_coverage_differs", "run", metric="safety_net_verdict")
    assert _codes(outcome) == [
        ("scenario_coverage_differs", "baseline", "safety_net_verdict"),
        ("scenario_coverage_differs", "run", "safety_net_verdict")]    # and not scenario_set_differs

    # The partner: the same gold, with S3 in both files, is compared.
    full = verdict_case(tmp_path / "b", k2(), THREE_STABLE, {"S1": P, "S2": P, "S3": P},
                        gold=["S1", "S2", "S3"])
    assert_no_breach(full)
    assert detail(full, "safety_net_verdict")["stable"] == 3


def test_kind2_baseline_and_run_adding_the_same_scenario_the_gold_lacks_is_an_error(tmp_path):
    now = {"S1": P, "S2": P, "S3": P, "S9": P}
    outcome = verdict_case(tmp_path / "a", k2(), THREE_AND_ONE_MORE, now, gold=["S1", "S2", "S3"])
    assert _codes(outcome) == [
        ("scenario_coverage_differs", "baseline", "safety_net_verdict"),
        ("scenario_coverage_differs", "run", "safety_net_verdict")]
    assert outcome.code == 2 and outcome.result["comparisons"] == []

    # The partner: with S9 in the gold as well, the same two files are compared.
    assert_no_breach(verdict_case(tmp_path / "b", k2(), THREE_AND_ONE_MORE, now,
                                  gold=["S1", "S2", "S3", "S9"]))


@pytest.mark.parametrize("short", ["baseline", "run"])
def test_kind2_one_file_short_of_the_gold_is_an_error_for_that_file(tmp_path, short):
    at_baseline = TWO_STABLE if short == "baseline" else THREE_STABLE
    now = {"S1": P, "S2": P} if short == "run" else {"S1": P, "S2": P, "S3": P}
    outcome = verdict_case(tmp_path, k2(), at_baseline, now, gold=["S1", "S2", "S3"])
    assert _codes(outcome) == [
        ("scenario_coverage_differs", short, "safety_net_verdict"),
        ("scenario_set_differs", "run", "safety_net_verdict")]     # the files disagree as well


def test_kind2_coverage_is_checked_only_after_the_keys_match(tmp_path):
    """Stage 3, not stage 1: a run whose gold differs from the baseline's is "no
    baseline", whatever its verdicts cover."""
    base = baseline({"safety_net_verdict": verdict_metric(THREE_STABLE)},
                    keys_for(["S1", "S2", "S3"]))
    run = run_summary({"safety_net_verdict": {"verdicts": {"S1": P, "S2": P}}},
                      keys_for(["S1", "S2"]))
    outcome = run_case(tmp_path, base, rules(k2()), run)
    assert (outcome.result["status"], outcome.code) == ("no_baseline", 2)
    assert outcome.result["key_mismatch"] == ["gold_sha256"] and outcome.result["errors"] == []


def test_kind2_coverage_is_for_ruled_metrics_an_unruled_one_is_listed_and_changes_nothing(
        tmp_path):
    base, rules_, run = valid_triple()
    base["metrics"]["haz03_verdict"] = verdict_metric({"S1": [P, P, P]})   # one scenario of three
    run["metrics"]["haz03_verdict"] = {"verdicts": {"S1": P}}

    unruled = run_case(tmp_path / "a", base, rules_, run)
    assert_no_breach(unruled)
    assert unruled.result["unruled_metrics"] == [
        {"metric": "haz03_verdict", "in_baseline": True, "in_run": True}]

    # The partner: the same two files, with a rule that names the metric.
    rules_["metrics"].append(k2("haz03_verdict", "haz03"))
    ruled = run_case(tmp_path / "b", base, rules_, run)
    assert _codes(ruled) == [("scenario_coverage_differs", "baseline", "haz03_verdict"),
                             ("scenario_coverage_differs", "run", "haz03_verdict")]


def test_count_metrics_are_not_checked_for_coverage(tmp_path):
    """Kinds 1 and 3 hold one count for the whole run, not a verdict per scenario."""
    base, _, run = valid_triple()
    del base["metrics"]["safety_net_verdict"], run["metrics"]["safety_net_verdict"]
    outcome = run_case(tmp_path, base, rules(k1(), k3()), run)
    assert_no_breach(outcome)
    assert len(outcome.result["comparisons"]) == 2 and outcome.result["unruled_metrics"] == []


# --------------------------------------- criterion 3: kind 3, noisy counts
# Baseline [5, 4, 7] with margin 2: the range is 4 to 7, widened to 2 to 9.

@pytest.mark.parametrize("now, direction", [(1, "below"), (0, "below"), (10, "above"),
                                            (25, "above")])
def test_kind3_count_outside_the_widened_range_is_a_breach_in_either_direction(
        tmp_path, now, direction):
    outcome = count_case(tmp_path, k3(margin=2), [5, 4, 7], now)
    assert_breach(outcome)
    assert detail(outcome, "placeholders_left") == {
        "metric": "placeholders_left", "breach": 1, "direction": direction, "run": now,
        "low": 2, "high": 9}
    assert row(outcome, "assembly", 3)["breach"] == 1


@pytest.mark.parametrize("now", [2, 9, 3, 4, 5, 7, 8])
def test_kind3_count_on_a_boundary_or_inside_is_not_a_breach(tmp_path, now):
    outcome = count_case(tmp_path, k3(margin=2), [5, 4, 7], now)
    assert_no_breach(outcome)
    assert detail(outcome, "placeholders_left") == {
        "metric": "placeholders_left", "breach": 0, "direction": "inside", "run": now,
        "low": 2, "high": 9}


@pytest.mark.parametrize("now, breach", [(3, 1), (4, 0), (7, 0), (8, 1)])
def test_kind3_margin_comes_from_the_rules_file(tmp_path, now, breach):
    outcome = count_case(tmp_path, k3(margin=0), [5, 4, 7], now)
    assert detail(outcome, "placeholders_left")["breach"] == breach
    assert outcome.code == breach


def test_kind3_a_lower_bound_below_zero_never_fires(tmp_path):
    outcome = count_case(tmp_path, k3(margin=2), [0, 1, 0], 0)
    assert_no_breach(outcome)
    assert detail(outcome, "placeholders_left")["low"] == -2


# ------------------------------------------------ criterion 4: key mismatch

def _set(path: tuple, value):
    def change(k: dict) -> None:
        for step in path[:-1]:
            k = k[step]
        k[path[-1]] = value
    return change


def _drop(path: tuple):
    def change(k: dict) -> None:
        for step in path[:-1]:
            k = k[step]
        del k[path[-1]]
    return change


PIPELINE_KEY_CHANGES = [
    ("pipeline_version", _set(("pipeline_version",), hx("f"))),
    ("model_id", _set(("model_id",), "invented.model-v2:0")),
    ("prompt_caching", _set(("prompt_caching",), False)),
    ("corpus_sha256", _set(("corpus_sha256",), hx("f"))),
    ("gold_sha256", _set(("gold_sha256", "S2"), hx("f"))),
    ("gold_sha256", _set(("gold_sha256", "S4"), hx("4"))),
    ("gold_sha256", _drop(("gold_sha256", "S3"))),
    ("scorer_versions", _set(("scorer_versions", "score_batch"), 3)),
    ("scorer_versions", _set(("scorer_versions", "score_batch"), "2")),
    ("scorer_versions", _set(("scorer_versions", "score_meds"), 1)),
    ("scorer_versions", _drop(("scorer_versions", "haz03_check"))),
]
V1_KEY_CHANGES = [
    ("v1_prompt_sha256", _set(("v1_prompt_sha256", "system_prompt"), hx("f"))),
    ("v1_prompt_sha256", _set(("v1_prompt_sha256", "patient_system_prompt"), hx("f"))),
    ("patient_second_pass", _set(("patient_second_pass",), False)),
    ("model_id", _set(("model_id",), "invented.model-v2:0")),
    # Part 3, item 2 (Codex review of PR #4): the keys v1 shares with the pipeline.
    ("prompt_caching", _set(("prompt_caching",), False)),
    ("corpus_sha256", _set(("corpus_sha256",), hx("f"))),
    ("gold_sha256", _set(("gold_sha256", "S2"), hx("f"))),
    ("gold_sha256", _set(("gold_sha256", "S4"), hx("4"))),
    ("gold_sha256", _drop(("gold_sha256", "S3"))),
    ("scorer_versions", _set(("scorer_versions", "score_batch"), 3)),
    ("scorer_versions", _set(("scorer_versions", "score_batch"), "2")),
    ("scorer_versions", _set(("scorer_versions", "score_meds"), 1)),
    ("scorer_versions", _drop(("scorer_versions", "haz03_check"))),
]
ALL_KEY_CHANGES = ([("pipeline", n, c) for n, c in PIPELINE_KEY_CHANGES]
                   + [("v1", n, c) for n, c in V1_KEY_CHANGES])


@pytest.mark.parametrize("side", ["run", "baseline"])
@pytest.mark.parametrize("would_breach", [True, False])
@pytest.mark.parametrize("target, name, change", ALL_KEY_CHANGES,
                         ids=[f"{t}-{n}-{i}" for i, (t, n, _) in enumerate(ALL_KEY_CHANGES)])
def test_any_differing_key_means_no_baseline_and_no_comparison(
        tmp_path, target, name, change, would_breach, side):
    base, rules_, run = valid_triple(target)
    if would_breach:
        run["metrics"]["invented_doses"]["count"] = 50
    change((run if side == "run" else base)["keys"])
    outcome = run_case(tmp_path, base, rules_, run)
    assert (outcome.result["status"], outcome.code) == ("no_baseline", 2)
    assert outcome.result["key_mismatch"] == [name]
    assert outcome.result["comparisons"] == []
    assert outcome.result["errors"] == []


@pytest.mark.parametrize("target", ["pipeline", "v1"])
def test_matching_keys_are_compared(tmp_path, target):
    base, rules_, run = valid_triple(target)
    outcome = run_case(tmp_path / "a", base, rules_, run)
    assert_no_breach(outcome)
    assert len(outcome.result["comparisons"]) == 3

    run["metrics"]["invented_doses"]["count"] = 50
    assert_breach(run_case(tmp_path / "b", base, rules_, run))


def test_a_different_target_is_no_baseline(tmp_path):
    base, rules_, _ = valid_triple("pipeline")
    _, _, run = valid_triple("v1")
    outcome = run_case(tmp_path, base, rules_, run)
    assert (outcome.result["status"], outcome.code) == ("no_baseline", 2)
    assert outcome.result["key_mismatch"] == [
        "patient_second_pass", "pipeline_version", "target", "v1_prompt_sha256"]
    assert outcome.result["comparisons"] == []


@pytest.mark.parametrize("target, change, field", [
    ("pipeline", _set(("patient_second_pass",), True), "keys"),   # forbidden for the pipeline
    ("pipeline", _set(("v1_prompt_sha256",), {"system_prompt": hx("c"),
                                              "patient_system_prompt": hx("d")}), "keys"),
    ("pipeline", _drop(("pipeline_version",)), "keys"),
    ("v1", _drop(("patient_second_pass",)), "keys"),              # required for v1
    ("v1", _set(("patient_second_pass",), "yes"), "keys.patient_second_pass"),
    ("v1", _set(("patient_second_pass",), 1), "keys.patient_second_pass"),
    ("v1", _set(("pipeline_version",), hx("b")), "keys"),
    ("v1", _drop(("v1_prompt_sha256", "patient_system_prompt")), "keys.v1_prompt_sha256"),
    ("pipeline", _set(("target",), "other"), "keys.target"),
    ("pipeline", _set(("prompt_caching",), "on"), "keys.prompt_caching"),
    ("pipeline", _set(("pipeline_version",), "abc"), "keys.pipeline_version"),
    ("pipeline", _set(("corpus_sha256",), hx("A")), "keys.corpus_sha256"),
    ("pipeline", _set(("gold_sha256",), {}), "keys.gold_sha256"),
    ("pipeline", _set(("scorer_versions",), {}), "keys.scorer_versions"),
    ("pipeline", _set(("scorer_versions", "score_batch"), 2.0), "keys.scorer_versions"),
    ("pipeline", _set(("scorer_versions", "score_batch"), True), "keys.scorer_versions"),
])
def test_malformed_keys_are_an_error_even_when_both_files_agree(tmp_path, target, change, field):
    base, rules_, run = valid_triple(target)
    change(base["keys"])
    change(run["keys"])
    outcome = run_case(tmp_path, base, rules_, run)
    assert_error(outcome, "shape_invalid", "baseline", field=field)
    assert_error(outcome, "shape_invalid", "run", field=field)


# -------------------------------------------------- criterion 5: fail closed

def test_the_complete_fixture_is_not_an_error(tmp_path):
    """The non-error partner of every test in this section."""
    assert_no_breach(run_case(tmp_path, *valid_triple()))


@pytest.mark.parametrize("metric", ["invented_doses", "safety_net_verdict", "placeholders_left"])
@pytest.mark.parametrize("file", ["baseline", "run"])
def test_ruled_metric_missing_from_the_baseline_or_the_run_is_an_error(tmp_path, file, metric):
    base, rules_, run = valid_triple()
    del (base if file == "baseline" else run)["metrics"][metric]
    outcome = run_case(tmp_path, base, rules_, run)
    assert_error(outcome, "metric_missing", file, metric=metric)


def test_a_breach_elsewhere_does_not_hide_a_missing_metric(tmp_path):
    base, rules_, run = valid_triple()
    run["metrics"]["invented_doses"]["count"] = 50
    del run["metrics"]["placeholders_left"]
    outcome = run_case(tmp_path, base, rules_, run)
    assert_error(outcome, "metric_missing", "run", metric="placeholders_left")


BAD_JSON = [
    "{not json",
    '{"schema": "ws7-run/1", "run_id": ',  # cut short
    "",
    "[]",
    "null",
    '"a string"',
    b"\xff\xfe{}",                          # not UTF-8
]


@pytest.mark.parametrize("which", [0, 1, 2], ids=["baseline", "rules", "run"])
@pytest.mark.parametrize("content", BAD_JSON, ids=range(len(BAD_JSON)))
def test_a_malformed_file_is_an_error(tmp_path, which, content):
    triple = list(valid_triple())
    triple[which] = content
    outcome = run_case(tmp_path, *triple)
    file = ("baseline", "rules", "run")[which]
    code = "shape_invalid" if content in ("[]", "null", '"a string"') else "json_invalid"
    assert_error(outcome, code, file)
    if file != "rules":
        assert outcome.result[f"{file}_id"] is None  # nothing is taken from a bad file


@pytest.mark.parametrize("which", [0, 1, 2], ids=["baseline", "rules", "run"])
def test_a_missing_file_is_an_error(tmp_path, which):
    triple = list(valid_triple())
    triple[which] = MISSING
    outcome = run_case(tmp_path, *triple)
    file = ("baseline", "rules", "run")[which]
    assert_error(outcome, "file_unreadable", file)
    assert outcome.result["inputs"][f"{file}_sha256"] is None


# Part 3, item 1 (Codex review of PR #4): a file's hash is known once it has been read.

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


NOT_JSON = [b"{not json", b'{"schema": "ws7-run/1", "run_id": ', b"", b"\xff\xfe{}",
            b'{"a": 1, "a": 2}', b'{"a": NaN}']


@pytest.mark.parametrize("which", [0, 1, 2], ids=["baseline", "rules", "run"])
@pytest.mark.parametrize("content", NOT_JSON, ids=range(len(NOT_JSON)))
def test_a_readable_file_that_is_not_valid_json_keeps_its_sha256(tmp_path, which, content):
    triple = list(valid_triple())
    triple[which] = content
    outcome = run_case(tmp_path, *triple)
    file = ("baseline", "rules", "run")[which]
    assert_error(outcome, "json_invalid", file)
    assert outcome.result["inputs"][f"{file}_sha256"] == hashlib.sha256(content).hexdigest()
    # The other two were read and parsed, and keep theirs as before.
    assert outcome.result["inputs"] == {f"{name}_sha256": _sha(tmp_path / f"{name}.json")
                                        for name in ("baseline", "rules", "run")}


@pytest.mark.parametrize("which", [0, 1, 2], ids=["baseline", "rules", "run"])
@pytest.mark.parametrize("how", ["missing", "a_directory"])
def test_an_unreadable_file_has_a_null_sha256_and_the_others_keep_theirs(tmp_path, which, how):
    triple = list(valid_triple())
    triple[which] = MISSING
    paths = write_inputs(tmp_path, *triple)
    file = ("baseline", "rules", "run")[which]
    if how == "a_directory":
        paths[file].mkdir()                       # it exists, and it cannot be read as a file
    assert W.main(argv(paths)) == 2
    result = json.loads(paths["out"].read_bytes())
    assert_ids_counts_labels_only(result)
    assert [(e["code"], e["file"]) for e in result["errors"]] == [("file_unreadable", file)]
    assert result["inputs"][f"{file}_sha256"] is None
    # The control: the null is this file's alone. The two that were read have a hash.
    others = [name for name in ("baseline", "rules", "run") if name != file]
    assert all(result["inputs"][f"{name}_sha256"] == _sha(paths[name]) for name in others)


def test_a_file_that_parses_but_fails_its_shape_check_keeps_its_sha256(tmp_path):
    base, rules_, _ = valid_triple()
    outcome = run_case(tmp_path, base, rules_, "[]")
    assert_error(outcome, "shape_invalid", "run")
    assert outcome.result["inputs"]["run_sha256"] == hashlib.sha256(b"[]").hexdigest()


def test_a_duplicate_json_key_is_an_error_not_last_one_wins(tmp_path):
    base, rules_, run = valid_triple()
    text = json.dumps(run).replace('"count": 1', '"count": 50, "count": 1')
    assert text.count('"count": 50') == 1
    assert_error(run_case(tmp_path, base, rules_, text), "json_invalid", "run")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_an_error(tmp_path, constant):
    base, rules_, run = valid_triple()
    text = json.dumps(run).replace('"count": 1', f'"count": {constant}')
    assert_error(run_case(tmp_path, base, rules_, text), "json_invalid", "run")


@pytest.mark.parametrize("value", ["3", True, False, 2.0, -1, None, [1], {"n": 1}],
                         ids=lambda v: repr(v))
def test_a_run_count_that_is_not_a_non_negative_integer_is_an_error(tmp_path, value):
    base, rules_, run = valid_triple()
    run["metrics"]["invented_doses"]["count"] = value
    assert_error(run_case(tmp_path, base, rules_, run), "shape_invalid", "run",
                 metric="invented_doses", field="metrics.count")


@pytest.mark.parametrize("counts", [[0, 1], [0, 1, 0, 0], [], [0, True, 0], [0, 1.0, 0],
                                    [0, -1, 0], [0, "1", 0], 1, None],
                         ids=lambda v: repr(v))
def test_baseline_counts_must_be_exactly_three_non_negative_integers(tmp_path, counts):
    base, rules_, run = valid_triple()
    base["metrics"]["invented_doses"]["counts"] = counts
    assert_error(run_case(tmp_path, base, rules_, run), "shape_invalid", "baseline",
                 metric="invented_doses", field="metrics.counts")


@pytest.mark.parametrize("three", [[P, P], [P, P, P, P], [P, "maybe", P], [P, True, P], P, []],
                         ids=lambda v: repr(v))
def test_baseline_verdicts_must_be_exactly_three_of_pass_or_fail(tmp_path, three):
    base, rules_, run = valid_triple()
    base["metrics"]["safety_net_verdict"]["verdicts"]["S1"] = three
    assert_error(run_case(tmp_path, base, rules_, run), "shape_invalid", "baseline",
                 metric="safety_net_verdict", field="metrics.verdicts")


@pytest.mark.parametrize("verdict", ["maybe", "", True, None, [P], 1], ids=lambda v: repr(v))
def test_a_run_verdict_outside_the_three_labels_is_an_error(tmp_path, verdict):
    base, rules_, run = valid_triple()
    run["metrics"]["safety_net_verdict"]["verdicts"]["S1"] = verdict
    assert_error(run_case(tmp_path, base, rules_, run), "shape_invalid", "run",
                 metric="safety_net_verdict", field="metrics.verdicts")


@pytest.mark.parametrize("unstable, code", [
    ([], "unstable_list_inconsistent"),             # S3 is unstable and is not named
    (["S3", "S1"], "unstable_list_inconsistent"),   # S1 is stable and is named
    (["S3", "S8"], "unstable_list_inconsistent"),   # S8 has no verdicts
    (["S3", "S3"], "shape_invalid"),
    ("S3", "shape_invalid"),
])
def test_the_baseline_unstable_list_must_agree_with_its_verdicts(tmp_path, unstable, code):
    base, rules_, run = valid_triple()
    base["metrics"]["safety_net_verdict"]["unstable"] = unstable
    assert_error(run_case(tmp_path, base, rules_, run), code, "baseline",
                 metric="safety_net_verdict", field="metrics.unstable")


def _rules_with(change) -> dict:
    rules_ = rules(k1(), k2(), k3())
    change(rules_)
    return rules_


BAD_RULES = [
    ("kind-4", lambda r: r["metrics"][0].update(kind=4), "metrics.kind"),
    ("kind-0", lambda r: r["metrics"][0].update(kind=0), "metrics.kind"),
    ("kind-string", lambda r: r["metrics"][0].update(kind="1"), "metrics.kind"),
    ("kind-true", lambda r: r["metrics"][0].update(kind=True), "metrics.kind"),
    ("kind-float", lambda r: r["metrics"][0].update(kind=1.0), "metrics.kind"),
    ("kind-missing", lambda r: r["metrics"][0].pop("kind"), "metrics.kind"),
    ("kind2-no-min-flips", lambda r: r["metrics"][1].pop("min_flips"), "metrics.entry"),
    ("kind3-no-margin", lambda r: r["metrics"][2].pop("margin"), "metrics.entry"),
    ("kind1-with-margin", lambda r: r["metrics"][0].update(margin=1), "metrics.entry"),
    ("kind2-with-margin", lambda r: r["metrics"][1].update(margin=1), "metrics.entry"),
    ("kind3-with-min-flips", lambda r: r["metrics"][2].update(min_flips=1), "metrics.entry"),
    ("entry-unknown-field", lambda r: r["metrics"][0].update(note="x"), "metrics.entry"),
    ("min-flips-0", lambda r: r["metrics"][1].update(min_flips=0), "metrics.min_flips"),
    ("min-flips-true", lambda r: r["metrics"][1].update(min_flips=True), "metrics.min_flips"),
    ("min-flips-float", lambda r: r["metrics"][1].update(min_flips=1.0), "metrics.min_flips"),
    ("margin-negative", lambda r: r["metrics"][2].update(margin=-1), "metrics.margin"),
    ("margin-float", lambda r: r["metrics"][2].update(margin=1.5), "metrics.margin"),
    ("margin-string", lambda r: r["metrics"][2].update(margin="2"), "metrics.margin"),
    ("family-unknown", lambda r: r["metrics"][0].update(family="other"), "metrics.family"),
    ("family-missing", lambda r: r["metrics"][0].pop("family"), "metrics.entry"),
    ("metric-duplicate", lambda r: r["metrics"].append(k1()), "metrics.metric"),
    ("metric-bad-id", lambda r: r["metrics"][0].update(metric="Invented Doses"),
     "metrics.metric"),
    ("metrics-empty", lambda r: r.update(metrics=[]), "metrics"),
    ("metrics-object", lambda r: r.update(metrics={}), "metrics"),
    ("entry-not-object", lambda r: r["metrics"].append("invented_doses"), "metrics.metric"),
    ("schema-wrong", lambda r: r.update(schema="ws7-rules/2"), "schema"),
    ("target-unknown", lambda r: r.update(target="v2"), "target"),
    ("top-unknown-field", lambda r: r.update(note="x"), "top"),
    ("top-missing-field", lambda r: r.pop("target"), "top"),
]


@pytest.mark.parametrize("change, field", [(c, f) for _, c, f in BAD_RULES],
                         ids=[i for i, _, _ in BAD_RULES])
def test_malformed_rules_are_an_error(tmp_path, change, field):
    base, _, run = valid_triple()
    outcome = run_case(tmp_path, base, _rules_with(change), run)
    assert_error(outcome, "shape_invalid", "rules", field=field)


BAD_DATA_FILES = [
    ("schema-wrong", lambda d: d.update(schema="ws7-other/1"), "schema"),
    ("top-unknown-field", lambda d: d.update(note="x"), "top"),
    ("keys-missing", lambda d: d.pop("keys"), "top"),
    ("metrics-list", lambda d: d.update(metrics=[]), "metrics"),
    ("metric-bad-id", lambda d: d["metrics"].update({"Bad Name": d["metrics"]["invented_doses"]}),
     "metrics"),
    ("metric-not-object", lambda d: d["metrics"].update(invented_doses=3), "metrics.value"),
    ("metric-empty", lambda d: d["metrics"].update(invented_doses={}), "metrics.value"),
    ("metric-unknown-field", lambda d: d["metrics"]["invented_doses"].update(note="x"),
     "metrics.value"),
    ("verdicts-empty", lambda d: d["metrics"]["safety_net_verdict"].update(verdicts={}),
     "metrics.verdicts"),
]


@pytest.mark.parametrize("which", [0, 2], ids=["baseline", "run"])
@pytest.mark.parametrize("change, field", [(c, f) for _, c, f in BAD_DATA_FILES],
                         ids=[i for i, _, _ in BAD_DATA_FILES])
def test_a_malformed_baseline_or_run_is_an_error(tmp_path, which, change, field):
    triple = list(valid_triple())
    change(triple[which])
    outcome = run_case(tmp_path, *triple)
    assert_error(outcome, "shape_invalid", ("baseline", "rules", "run")[which], field=field)


@pytest.mark.parametrize("which, bad_id", [(0, "has spaces"), (0, ""), (0, 7), (0, None),
                                           (2, "has spaces"), (2, ""), (2, 7), (2, None)])
def test_a_baseline_or_run_id_that_is_not_an_id_is_an_error(tmp_path, which, bad_id):
    triple = list(valid_triple())
    name = "baseline_id" if which == 0 else "run_id"
    triple[which][name] = bad_id
    outcome = run_case(tmp_path, *triple)
    assert_error(outcome, "shape_invalid", ("baseline", "rules", "run")[which], field=name)
    assert outcome.result[name] is None


# A metric whose shape does not match its kind.
@pytest.mark.parametrize("file, metric, wrong", [
    ("baseline", "safety_net_verdict", {"counts": [0, 1, 0]}),   # counts for kind 2
    ("run", "safety_net_verdict", {"count": 1}),                 # a count for kind 2
    ("baseline", "invented_doses", "VERDICTS"),                  # verdicts for kind 1
    ("run", "invented_doses", "VERDICTS"),
    ("baseline", "placeholders_left", "VERDICTS"),               # verdicts for kind 3
    ("run", "placeholders_left", "VERDICTS"),
])
def test_a_metric_whose_shape_does_not_match_its_kind_is_an_error(tmp_path, file, metric, wrong):
    base, rules_, run = valid_triple()
    if wrong == "VERDICTS":
        wrong = (verdict_metric({"S1": [P, P, P]}) if file == "baseline"
                 else {"verdicts": {"S1": P}})
    (base if file == "baseline" else run)["metrics"][metric] = wrong
    outcome = run_case(tmp_path, base, rules_, run)
    assert_error(outcome, "metric_kind_mismatch", file, metric=metric)
    assert outcome.result["errors"] == [
        {"code": "metric_kind_mismatch", "file": file, "field": None, "metric": metric}]


def test_rules_for_the_other_target_are_an_error(tmp_path):
    base, _, run = valid_triple("pipeline")
    outcome = run_case(tmp_path / "a", base, rules(k1(), k2(), k3(), target="v1"), run)
    assert_error(outcome, "target_mismatch", "rules", field="target")
    # Partner: the right target compares.
    assert_no_breach(run_case(tmp_path / "b", base, rules(k1(), k2(), k3()), run))


def test_every_error_in_the_three_files_is_reported(tmp_path):
    outcome = run_case(tmp_path, "{", MISSING, "[]")
    assert [(e["code"], e["file"]) for e in outcome.result["errors"]] == [
        ("file_unreadable", "rules"), ("json_invalid", "baseline"), ("shape_invalid", "run")]
    assert outcome.code == 2


# ------------------------------------------- metrics the rules do not name

def test_a_run_metric_the_rules_do_not_name_is_visible_and_changes_nothing(tmp_path):
    base, rules_, run = valid_triple()
    run["metrics"]["tokens_out_total"] = {"count": 41250}
    base["metrics"]["haz03_verdict"] = verdict_metric({"S1": [P, P, P]})
    run["metrics"]["haz03_verdict"] = {"verdicts": {"S1": F}}
    base["metrics"]["retired_count"] = {"counts": [1, 1, 1]}

    outcome = run_case(tmp_path / "a", base, rules_, run)
    assert_no_breach(outcome)  # an unruled failing verdict is not a breach: no rule names it
    assert outcome.result["unruled_metrics"] == [
        {"metric": "haz03_verdict", "in_baseline": True, "in_run": True},
        {"metric": "retired_count", "in_baseline": True, "in_run": False},
        {"metric": "tokens_out_total", "in_baseline": False, "in_run": True},
    ]

    run["metrics"]["invented_doses"]["count"] = 50
    breached = run_case(tmp_path / "b", base, rules_, run)
    assert_breach(breached)
    assert len(breached.result["unruled_metrics"]) == 3


def test_a_fully_ruled_run_has_no_unruled_metrics(tmp_path):
    assert run_case(tmp_path, *valid_triple()).result["unruled_metrics"] == []


def test_an_unruled_metric_must_still_be_well_formed(tmp_path):
    base, rules_, run = valid_triple()
    run["metrics"]["tokens_out_total"] = {"count": 1.5}
    assert_error(run_case(tmp_path, base, rules_, run), "shape_invalid", "run",
                 metric="tokens_out_total", field="metrics.count")


# ------------------------------------------------ rows: one per family+kind

def test_one_row_per_family_and_kind_named_by_the_rules(tmp_path):
    base = baseline({
        "invented_doses": {"counts": [0, 0, 0]},
        "invented_frequencies": {"counts": [0, 0, 0]},
        "meds_verdict": verdict_metric({"S1": [P, P, P]}),
        "gate_failures": {"counts": [0, 0, 0]},
    }, keys_for(["S1"]))
    run = run_summary({
        "invented_doses": {"count": 0},
        "invented_frequencies": {"count": 1},
        "meds_verdict": {"verdicts": {"S1": P}},
        "gate_failures": {"count": 0},
    }, keys_for(["S1"]))
    rules_ = rules(k1("invented_frequencies"), k1("gate_failures", "safety_netting"),
                   k2("meds_verdict", "medications"), k1("invented_doses"))
    outcome = run_case(tmp_path, base, rules_, run)
    assert_breach(outcome)
    assert [(r["family"], r["kind"], r["breach"]) for r in outcome.result["comparisons"]] == [
        ("medications", 1, 1), ("medications", 2, 0), ("safety_netting", 1, 0)]
    # One breaching metric makes its row a breach; the other metric is still listed.
    assert [(d["metric"], d["breach"]) for d in row(outcome, "medications", 1)["metrics"]] == [
        ("invented_doses", 0), ("invented_frequencies", 1)]


# ------------------------------------------------- criterion 6: determinism

def big_triple() -> tuple[dict, dict, dict]:
    """Eighteen scenarios and eleven metrics: enough for an unordered
    container anywhere in the comparator to show."""
    ids = [f"S{n}" for n in range(1, 19)]
    k = keys()
    k["gold_sha256"] = {s: hx(format(i % 16, "x")) for i, s in enumerate(ids)}

    def at_baseline(shift: int) -> dict:
        return {s: [P, F, P] if (i + shift) % 5 == 0 else [F, F, F] if (i + shift) % 7 == 0
                else [P, P, P] for i, s in enumerate(ids)}

    def now(shift: int) -> dict:
        return {s: N if (i + shift) % 6 == 0 else F if (i + shift) % 4 == 0 else P
                for i, s in enumerate(ids)}

    base = baseline({
        "invented_doses": {"counts": [0, 1, 0]},
        "invented_frequencies": {"counts": [0, 0, 0]},
        "invented_triggers": {"counts": [2, 0, 1]},
        "safety_net_verdict": verdict_metric(at_baseline(0)),
        "meds_verdict": verdict_metric(at_baseline(1)),
        "haz03_verdict": verdict_metric(at_baseline(2)),
        "placeholders_left": {"counts": [5, 4, 7]},
        "check_count": {"counts": [30, 28, 33]},
        "rejections_8a": {"counts": [1, 3, 2]},
        "retired_count": {"counts": [1, 1, 1]},
    }, k)
    run = run_summary({
        "invented_doses": {"count": 3},
        "invented_frequencies": {"count": 0},
        "invented_triggers": {"count": 2},
        "safety_net_verdict": {"verdicts": now(0)},
        "meds_verdict": {"verdicts": now(1)},
        "haz03_verdict": {"verdicts": now(2)},
        "placeholders_left": {"count": 12},
        "check_count": {"count": 20},
        "rejections_8a": {"count": 2},
        "tokens_out_total": {"count": 41250},
    }, copy.deepcopy(k))
    rules_ = rules(
        k3("rejections_8a", "assembly", 1), k2("haz03_verdict", "haz03", 2),
        k1("invented_triggers", "safety_netting"), k3("check_count", "composition", 3),
        k1("invented_doses"), k2("safety_net_verdict", "safety_netting", 1),
        k3("placeholders_left", "assembly", 2), k1("invented_frequencies"),
        k2("meds_verdict", "medications", 3))
    return base, rules_, run


def run_cli(paths: dict, seed: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONHASHSEED", None)
    if seed is not None:
        env["PYTHONHASHSEED"] = seed
    return subprocess.run([sys.executable, str(SCRIPT)] + argv(paths), env=env,
                          capture_output=True, text=True, timeout=60)


def test_the_same_inputs_give_byte_identical_output_whatever_the_hash_seed(tmp_path):
    paths = write_inputs(tmp_path, *big_triple())
    outputs, consoles = [], []
    for seed in ("0", "1", "4242", "987654321"):
        paths["out"] = tmp_path / f"result-{seed}.json"
        done = run_cli(paths, seed)
        assert done.returncode == 1, done.stderr
        outputs.append(paths["out"].read_bytes())
        consoles.append(done.stdout)
    assert len(set(outputs)) == 1
    assert len(set(consoles)) == 1

    result = json.loads(outputs[0])
    assert_ids_counts_labels_only(result)
    # The fixture exercises what could be unordered: flips, no_output, unstable, rows.
    assert sum(len(d["flips"]) for r in result["comparisons"] if r["kind"] == 2
               for d in r["metrics"]) >= 6
    assert outputs[0] == W.render(result)
    assert outputs[0].endswith(b"\n") and not outputs[0].endswith(b"\n\n")


def test_the_seeds_really_reach_the_subprocess(tmp_path):
    """Guards the test above: with these seeds a plain set of strings does
    come out in more than one order, so identical output is not an accident."""
    orders = set()
    for seed in ("0", "1", "4242", "987654321"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        done = subprocess.run(
            [sys.executable, "-c",
             "print(list({'S%d' % n for n in range(1, 19)}))"],
            env=env, capture_output=True, text=True, timeout=60)
        orders.add(done.stdout)
    assert len(orders) > 1


def _reordered(value):
    if isinstance(value, dict):
        return {k: _reordered(value[k]) for k in reversed(list(value))}
    return value


def test_the_order_of_the_input_does_not_change_the_comparison(tmp_path):
    base, rules_, run = big_triple()
    first = run_case(tmp_path / "a", base, rules_, run)

    shuffled_rules = dict(rules_, metrics=list(reversed(rules_["metrics"])))
    second = run_case(tmp_path / "b", _reordered(base), shuffled_rules, _reordered(run))
    for name in ("status", "comparisons", "unruled_metrics", "key_mismatch", "errors"):
        assert first.result[name] == second.result[name]

    for r in first.result["comparisons"]:
        assert [d["metric"] for d in r["metrics"]] == sorted(d["metric"] for d in r["metrics"])
        for d in r["metrics"]:
            if r["kind"] == 2:
                assert d["flips"] == sorted(d["flips"])
                assert d["no_output"] == sorted(d["no_output"])
                assert [u["scenario"] for u in d["unstable"]] == sorted(
                    u["scenario"] for u in d["unstable"])
    rows = [(r["family"], r["kind"]) for r in first.result["comparisons"]]
    assert rows == sorted(rows)


def test_in_process_and_subprocess_write_the_same_bytes(tmp_path):
    paths = write_inputs(tmp_path, *big_triple())
    assert W.main(argv(paths)) == 1
    in_process = paths["out"].read_bytes()
    paths["out"] = tmp_path / "result-cli.json"
    assert run_cli(paths).returncode == 1
    assert paths["out"].read_bytes() == in_process


# ----------------------------------------------------- criterion 7: no text

def _plant(path: tuple, value=SENTINEL):
    def change(doc: dict) -> None:
        for step in path[:-1]:
            doc = doc[step]
        doc[path[-1]] = value
    return change


def _plant_key(path: tuple):
    def change(doc: dict) -> None:
        for step in path:
            doc = doc[step]
        doc[SENTINEL] = doc[next(iter(doc))]
    return change


PLANTS = [
    ("baseline", _plant(("baseline_id",))),
    ("baseline", _plant(("schema",))),
    ("baseline", _plant(("keys", "model_id"))),
    ("baseline", _plant(("keys", "target"))),
    ("baseline", _plant(("keys", "pipeline_version"))),
    ("baseline", _plant(("keys", "scorer_versions", "score_batch"))),
    ("baseline", _plant_key(("keys", "scorer_versions"))),
    ("baseline", _plant_key(("keys", "gold_sha256"))),
    ("baseline", _plant_key(("keys",))),
    ("baseline", _plant_key(())),
    ("baseline", _plant_key(("metrics",))),
    ("baseline", _plant_key(("metrics", "safety_net_verdict", "verdicts"))),
    ("baseline", _plant(("metrics", "safety_net_verdict", "verdicts", "S1"), [P, SENTINEL, P])),
    ("baseline", _plant(("metrics", "safety_net_verdict", "unstable"), ["S3", SENTINEL])),
    ("baseline", _plant(("metrics", "invented_doses", "counts"), [0, SENTINEL, 0])),
    ("baseline", _plant(("metrics", "invented_doses", "note"))),
    ("rules", _plant(("schema",))),
    ("rules", _plant(("target",))),
    ("rules", _plant(("note",))),
    ("rules", _plant(("metrics", 0, "metric"))),
    ("rules", _plant(("metrics", 0, "family"))),
    ("rules", _plant(("metrics", 0, "kind"))),
    ("rules", _plant(("metrics", 1, "min_flips"))),
    ("rules", _plant(("metrics", 2, "margin"))),
    ("rules", _plant(("metrics", 0, "reason"))),
    ("run", _plant(("run_id",))),
    ("run", _plant(("schema",))),
    ("run", _plant(("keys", "model_id"))),
    ("run", _plant(("keys", "corpus_sha256"))),
    ("run", _plant_key(("keys", "gold_sha256"))),
    ("run", _plant_key(("metrics",))),
    ("run", _plant_key(("metrics", "safety_net_verdict", "verdicts"))),
    ("run", _plant(("metrics", "safety_net_verdict", "verdicts", "S1"))),
    ("run", _plant(("metrics", "invented_doses", "count"))),
    ("run", _plant(("metrics", "invented_doses", "output"))),
    ("run", _plant(("output",))),
]


def _assert_no_leak(*texts) -> None:
    for text in texts:
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        for word in ("LEAK", "free text", "0451"):
            assert word not in text


@pytest.mark.parametrize("file, change", PLANTS, ids=range(len(PLANTS)))
def test_free_text_in_an_input_is_an_error_and_is_never_echoed(tmp_path, capsys, file, change):
    docs = dict(zip(("baseline", "rules", "run"), valid_triple()))
    change(docs[file])
    assert SENTINEL in json.dumps(docs[file])
    outcome = run_case(tmp_path, docs["baseline"], docs["rules"], docs["run"])
    assert (outcome.result["status"], outcome.code) == ("error", 2)
    assert any(e["file"] == file for e in outcome.result["errors"])
    captured = capsys.readouterr()
    _assert_no_leak(outcome.raw, captured.out, captured.err)


@pytest.mark.parametrize("content", [
    SENTINEL,
    '{"schema": "' + SENTINEL,
    '{"' + SENTINEL + '": 1, "' + SENTINEL + '": 2}',
    json.dumps([SENTINEL]),
    json.dumps(SENTINEL),
])
def test_text_in_a_file_that_does_not_parse_is_never_echoed(tmp_path, capsys, content):
    base, rules_, _ = valid_triple()
    outcome = run_case(tmp_path, base, rules_, content)
    assert (outcome.result["status"], outcome.code) == ("error", 2)
    captured = capsys.readouterr()
    _assert_no_leak(outcome.raw, captured.out, captured.err)


def test_file_paths_are_not_in_the_result_or_on_the_console(tmp_path, capsys):
    folder = tmp_path / SENTINEL
    ok = run_case(folder, *valid_triple())
    assert ok.code == 0
    base, rules_, _ = valid_triple()
    absent = run_case(folder, base, rules_, MISSING)
    assert absent.code == 2
    captured = capsys.readouterr()
    _assert_no_leak(ok.raw, absent.raw, captured.out, captured.err)


def test_the_result_has_no_free_strings_for_any_status(tmp_path):
    """run_case checks every result by position; this pins that all four statuses pass it."""
    base, rules_, run = big_triple()
    statuses = {run_case(tmp_path / "breach", base, rules_, run).result["status"]}
    statuses.add(run_case(tmp_path / "ok", *valid_triple()).result["status"])
    other = copy.deepcopy(run)
    other["keys"]["model_id"] = "invented.model-v2:0"
    statuses.add(run_case(tmp_path / "nb", base, rules_, other).result["status"])
    statuses.add(run_case(tmp_path / "err", base, rules_, "{").result["status"])
    assert statuses == STATUSES


CONSOLE = re.compile(r"ws7_compare: status=(no_breach|breach|no_baseline|error) exit=[012] "
                     r"rows=\d+ breaches=\d+ errors=\d+ key_mismatches=\d+ unruled=\d+\n")


def test_the_console_shows_fixed_labels_and_counts_only(tmp_path, capsys):
    run_case(tmp_path / "a", *big_triple())
    run_case(tmp_path / "b", *valid_triple())
    run_case(tmp_path / "c", "{", MISSING, "[]")
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines(keepends=True)
    assert len(lines) == 3 and all(CONSOLE.fullmatch(line) for line in lines)
    assert lines[0].startswith("ws7_compare: status=breach exit=1 rows=7 breaches=")
    assert lines[2].startswith("ws7_compare: status=error exit=2 rows=0 breaches=0 errors=3 ")


# -------------------------------------------------- criterion 8: exit codes

def _cli_case(tmp_path, base, rules_, run) -> tuple[int, dict]:
    paths = write_inputs(tmp_path, base, rules_, run)
    done = run_cli(paths)
    result = json.loads(paths["out"].read_bytes())
    assert result["exit_code"] == done.returncode
    return done.returncode, result


def test_exit_0_for_no_breach(tmp_path):
    code, result = _cli_case(tmp_path, *valid_triple())
    assert (code, result["status"]) == (0, "no_breach")


def test_exit_1_for_a_breach(tmp_path):
    base, rules_, run = valid_triple()
    run["metrics"]["invented_doses"]["count"] = 2
    code, result = _cli_case(tmp_path, base, rules_, run)
    assert (code, result["status"]) == (1, "breach")


def test_exit_2_for_no_baseline(tmp_path):
    base, rules_, run = valid_triple()
    run["keys"]["prompt_caching"] = False
    code, result = _cli_case(tmp_path, base, rules_, run)
    assert (code, result["status"]) == (2, "no_baseline")


def test_exit_2_for_an_error(tmp_path):
    base, rules_, run = valid_triple()
    del run["metrics"]["invented_doses"]
    code, result = _cli_case(tmp_path, base, rules_, run)
    assert (code, result["status"]) == (2, "error")


def test_exit_2_for_a_usage_error_and_nothing_is_written(tmp_path):
    paths = write_inputs(tmp_path, *valid_triple())
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, str(SCRIPT), "--baseline", str(paths["baseline"])],
                          env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 2
    assert not paths["out"].exists()


def test_exit_2_when_the_result_cannot_be_written(tmp_path, capsys):
    paths = write_inputs(tmp_path, *valid_triple())
    paths["out"] = tmp_path / "no-such-folder" / "result.json"
    assert W.main(argv(paths)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ws7_compare: status=error exit=2 code=output_unwritable\n"


def test_an_unexpected_exception_exits_2_never_pythons_default_1(tmp_path, capsys, monkeypatch):
    """Python exits 1 on an uncaught exception, and 1 means "a breach"."""
    def explode(*_args):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(W, "compare_files", explode)
    paths = write_inputs(tmp_path, *valid_triple())
    assert W.main(argv(paths)) == 2
    result = json.loads(paths["out"].read_bytes())
    assert_ids_counts_labels_only(result)
    assert result["status"] == "error" and result["exit_code"] == 2
    assert result["errors"] == [
        {"code": "internal_error", "file": None, "field": None, "metric": None}]
    captured = capsys.readouterr()
    _assert_no_leak(paths["out"].read_bytes(), captured.out, captured.err)


def test_an_exception_while_reporting_still_exits_2(tmp_path, capsys, monkeypatch):
    def explode(*_args):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(W, "render", explode)
    paths = write_inputs(tmp_path, *valid_triple())
    assert W.main(argv(paths)) == 2
    captured = capsys.readouterr()
    assert captured.err == "ws7_compare: status=error exit=2 code=internal_error\n"
    _assert_no_leak(captured.out, captured.err)


def test_only_no_breach_maps_to_exit_0():
    assert W.EXIT_CODES == {"no_breach": 0, "breach": 1, "no_baseline": 2, "error": 2}


# ---------------------------------------------------- the task's hard limits

def test_the_comparator_imports_the_standard_library_only():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module.split(".")[0])
    assert imported == {"__future__", "argparse", "hashlib", "json", "re", "sys", "pathlib"}
