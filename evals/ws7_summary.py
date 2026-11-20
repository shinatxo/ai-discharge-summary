#!/usr/bin/env python3
"""WS7 run summary and candidate baseline (ADR-011 D7, D9, D11, D13; slice 2 part 4).

Offline. No AWS call, no model call. Part 5 wires these into the modes.

THE RUN SUMMARY (`ws7-run/1`, docs/WS7-COMPARATOR.md) is built from one finished
batch of 18, its score outputs and the guard's report:

    write_haz03(batch_dir)      runs evals/haz03_check.py, unchanged, on each record
                                and writes scores/haz03.json (pipeline only; nothing
                                else produces HAZ-03 output for a batch)
    key_parts(batch_dir)        the corpus hash, the gold hashes and the scorer
                                versions, for the manifest and the summary alike
    build_summary(batch_dir, own=..., report=..., batch_label=...,
                  month_assurance_waived=...)

- KEYS. `ws7_manifest.build_keys()` builds them, for the manifest and the summary,
  from the job's own values and `key_parts()`. `corpus_sha256` is the batch
  manifest's, `gold_sha256` is score_batch's gold map, and `scorer_versions` are the
  scorers' own constants. evals/score_assemble.py has no version constant, so it is
  keyed by the sha256 of its file's bytes.
- METRICS are a fixed set for each target, always present. Integers only: cost in
  `cost_usd_micros` (the batch's own total in the guard's report, rounded up), times
  in metrics whose names end `_ms`.
- VERDICTS. "pass" is the scorer's own "pass", exactly. A scorer's `review` or
  `partial` is written "fail", and counted in a metric of its own. A scenario with
  no scored row, and a scorer's `not_scored`, are written "no_output". Every verdict
  metric has a verdict for exactly the scenarios in `keys.gold_sha256`.
- PART C IS NOT PUBLISHED (D13). score_batch's patient block, and every warning in
  score_assemble's output, are never read.
- WHAT IS READ. Fixed file names only: the batch's manifest.json, batch.json and
  records, and the JSON score outputs. No `.md` report and no review file is opened:
  they quote model output. A record is read for named fields only. A string from a
  record or a score file reaches the summary only by being one of a fixed set of
  labels written in this file or in the code it imports; anything else builds no
  summary.
- A run whose report says a call's usage is unknown is unmetered and builds no
  summary (`ws7_unmetered`).

THE CANDIDATE BASELINE is built from the three run summaries of a manual baseline
(D7: 18 x 3):

    build_candidate([s1, s2, s3], baseline_id="...")

It returns an envelope, `ws7-baseline-candidate/1`, whose `baseline` member is a
`ws7-baseline/1` object. The comparator refuses the envelope, so a candidate is
never read as a frozen baseline by accident. Code never freezes a baseline: the
author copies the `baseline` member into the repository by pull request.

Every refusal is a `Stop` with a fixed code (CODES).
"""

from __future__ import annotations

import hashlib
import json
import sys
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

_EVALS = Path(__file__).resolve().parent
for _p in (str(_EVALS.parent / "src" / "generate"), str(_EVALS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gold as G  # noqa: E402
import haz03_check as H3  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402
import score_batch as SB  # noqa: E402
import score_compose as CP  # noqa: E402
import score_step2_advice as S2  # noqa: E402
import ws7_client as wc  # noqa: E402
import ws7_compare as compare  # noqa: E402
import ws7_manifest as manifest  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
from ws7_store import Stop  # noqa: E402

CANDIDATE_SCHEMA = "ws7-baseline-candidate/1"
HAZ03_NAME = "haz03.json"
ASSEMBLE_SCORER = _EVALS / "score_assemble.py"      # no version constant: keyed by its bytes

UNMETERED = wc.UNMETERED
OWN_INVALID = "ws7_summary_own_invalid"
BATCH_INVALID = "ws7_summary_batch_invalid"
BATCH_INCOMPLETE = "ws7_summary_batch_incomplete"
SCORES_INVALID = "ws7_summary_scores_invalid"
REPORT_INVALID = "ws7_summary_report_invalid"
SUMMARY_INVALID = "ws7_summary_invalid"
NOT_THREE_RUNS = "ws7_candidate_not_three_runs"
CANDIDATE_ID_INVALID = "ws7_candidate_id_invalid"
CANDIDATE_SUMMARY_INVALID = "ws7_candidate_summary_invalid"
RUNS_NOT_DISTINCT = "ws7_candidate_runs_not_distinct"
KEYS_DIFFER = "ws7_candidate_keys_differ"
METRICS_DIFFER = "ws7_candidate_metrics_differ"
DRAFT_GOLD = "ws7_candidate_draft_gold"
CANDIDATE_NO_OUTPUT = "ws7_candidate_no_output"
CODES = (UNMETERED, OWN_INVALID, BATCH_INVALID, BATCH_INCOMPLETE, SCORES_INVALID, REPORT_INVALID,
         SUMMARY_INVALID, NOT_THREE_RUNS, CANDIDATE_ID_INVALID, CANDIDATE_SUMMARY_INVALID,
         RUNS_NOT_DISTINCT, KEYS_DIFFER, METRICS_DIFFER, DRAFT_GOLD, CANDIDATE_NO_OUTPUT)

PASS, FAIL, NO_OUTPUT = compare.PASS, compare.FAIL, compare.NO_OUTPUT

# ---- the verdict mapping: every value each scorer can emit ----------------------
# "pass" is the scorer's own "pass", exactly. `review` (waiting for a CSO ruling) and
# `partial` are not a pass. Mapping either to "pass" would make WS7's verdict looser
# than the scorer's. A value that is not listed builds no summary.
SAFETY_NET_VERDICT = {"pass": PASS, "review": FAIL, "fail": FAIL}      # score_safety_net.py:257
MEDS_VERDICT = {"pass": PASS, "partial": FAIL, "review": FAIL, "fail": FAIL}  # score_meds.py:527-535
HAZ03_VERDICT = {"pass": PASS, "fail": FAIL, "not_scored": NO_OUTPUT}  # haz03_check.py:91, :104

# ---- the fixed label sets: the only strings a file can add to a metric ----------
SAFETY_NET_FAILURES = ("route", "invented_trigger", "pinned_line_missing", "wrong_audience",
                       "unsupported_advice")                           # score_safety_net.py:245-255
HAZ03_KINDS = ("age_group_mismatch", "wrong_audience_label", "wrong_pinned_line",
               "expected_line_missing_part_c", "expected_line_missing_part_ab",
               "other_line_in_part_ab", "other_line_in_part_c")        # haz03_check.py:69-82
GOLD_STATUSES = G.STATUSES                                             # ("draft", "approved")
ASSEMBLY_OUTCOMES = ("bundle", "rejected", "upstream_missing")         # score_assemble.py:148-163
ASSEMBLY_STEPS = ("08a", "08b")                                        # score_assemble.py:156, :163
# 8b raises `safety_net_gate_failed` on any other status (pipeline/assemble.py:606), so a
# bundle's gate is one of these two.
BUNDLE_GATE_STATUSES = ("clean", "documented_advice")
GATE_FAILED = "safety_net_gate_failed"
COMPOSE_REVIEW_KINDS = ("advice", "resus", "medication")               # score_compose.py:279
PIPELINE_STEPS = rpe.STEP_ORDER                                        # the 9 step names
_METERED_OUTCOMES = (wc.RESPONSE, wc.REFUSED) + wc.NOT_RUN             # a call whose cost is known
_OUTCOMES = (wc.RESPONSE, wc.USAGE_UNKNOWN, wc.REFUSED) + wc.NO_RESPONSE
P90 = 90


# ---------------------------------------------------------------- reading

def _no_duplicate_keys(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_constants(_name: str):
    raise ValueError("NaN or Infinity")


def _read(path: Path, code: str) -> object:
    """One JSON file, strictly. A number with a decimal point is parsed as a Decimal,
    so no binary float is ever involved in a conversion."""
    try:
        return json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                          parse_float=Decimal, parse_constant=_no_constants)
    except (OSError, ValueError, RecursionError):
        raise Stop(code) from None


def _get(obj: object, *path: str, code: str = SCORES_INVALID) -> object:
    for name in path:
        if type(obj) is not dict or name not in obj:
            raise Stop(code)
        obj = obj[name]
    return obj


def _count(value: object, code: str = SCORES_INVALID) -> int:
    """A whole number of zero or more. A bool, a float or a string is refused."""
    if type(value) is not int or value < 0:
        raise Stop(code)
    return value


def _flag(value: object, code: str = SCORES_INVALID) -> bool:
    if type(value) is not bool:
        raise Stop(code)
    return value


def _label(value: object, allowed: tuple, code: str = SCORES_INVALID) -> str:
    """`value` is one of a fixed set of labels, or nothing is built."""
    if type(value) is not str or value not in allowed:
        raise Stop(code)
    return value


def _labels(value: object, allowed: tuple, code: str = SCORES_INVALID) -> list:
    if type(value) is not list:
        raise Stop(code)
    return [_label(item, allowed, code) for item in value]


def _is_id(value: object, pattern) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


# ------------------------------------------------------------------ units

def to_ms(seconds: object) -> int:
    """Seconds, as the runners record them, to whole milliseconds. The value is read
    as a decimal from its JSON text, multiplied by 1000 and rounded half-even. The
    runners write three decimal places, so the result is exact."""
    if type(seconds) is int:
        seconds = Decimal(seconds)
    if type(seconds) is not Decimal or not seconds.is_finite() or seconds < 0:
        raise Stop(BATCH_INVALID)
    return int((seconds * 1000).to_integral_value(rounding=ROUND_HALF_EVEN))


def nearest_rank(values: list, percent: int) -> int:
    """The percentile by the nearest-rank method, with no interpolation: sort the
    values and take item ceil(percent/100 x n), counting from 1. For 18 values and
    90 that is the 17th. The result is always one of the values."""
    ordered = sorted(values)
    return ordered[-(-percent * len(ordered) // 100) - 1]


# -------------------------------------------------------------- the batch

class _Batch:
    """What a finished batch folder says about itself: IDs, hashes and flags only."""

    def __init__(self, batch_dir: Path):
        self.dir = Path(batch_dir)
        man = _read(self.dir / "manifest.json", BATCH_INVALID)
        index = _read(self.dir / "batch.json", BATCH_INVALID)
        code = BATCH_INVALID
        self.target = _label(_get(man, "generator", code=code), compare.TARGETS, code)
        self.batch_id = _get(man, "batch_id", code=code)
        if not _is_id(self.batch_id, compare._ID_RE) or _get(index, "batch_id", code=code) != self.batch_id:
            raise Stop(code)
        self.model_id = _get(man, "model_id", code=code)
        self.prompt_caching = _flag(_get(man, "prompt_caching", code=code), code)
        self.corpus_sha256 = _get(man, "corpus", "sha256", code=code)
        self.prompts = _get(man, "prompts", code=code)
        listed = _get(man, "scenarios", code=code)
        if type(listed) is not list or not listed:
            raise Stop(code)
        self.scenarios = [_get(item, "id", code=code) for item in listed]
        if not all(_is_id(s, compare._SCENARIO_RE) for s in self.scenarios) \
                or len(set(self.scenarios)) != len(self.scenarios):
            raise Stop(code)

        # One batch of 18, once (D9: baseline and weekly runs are counted per 18-scenario
        # run). A batch that stopped early, or that did not generate exactly the
        # manifest's scenarios, builds nothing.
        generations = _get(index, "generations", code=code)
        if type(generations) is not list or not all(type(g) is dict for g in generations):
            raise Stop(code)
        if _get(man, "runs", code=code) != 1 or _flag(_get(index, "stopped_early", code=code), code) \
                or sorted(str(g.get("scenario_id")) for g in generations) != sorted(self.scenarios):
            raise Stop(BATCH_INCOMPLETE)
        self.failed = 0
        for g in generations:
            sid = g["scenario_id"]
            if type(sid) is not str or g.get("run") != 1 or g.get("record") != f"r1/{sid}.json" \
                    or "error_code" not in g or not (g["error_code"] is None
                                                     or type(g["error_code"]) is str):
                raise Stop(code)
            self.failed += g["error_code"] is not None     # the code itself is never copied

    def record(self, sid: str) -> dict:
        rec = _read(self.dir / "r1" / f"{sid}.json", BATCH_INVALID)
        if type(rec) is not dict or rec.get("scenario_id") != sid or rec.get("run") != 1 \
                or rec.get("batch_id") != self.batch_id:
            raise Stop(BATCH_INVALID)
        return rec

    def scores(self, *name: str) -> dict:
        """One JSON score output, by its fixed name. It must be this batch's."""
        data = _read(self.dir.joinpath("scores", *name), SCORES_INVALID)
        own_id = data.get("batch" if name[0] == "assemble" else "batch_id") if type(data) is dict else None
        if own_id != self.batch_id:
            raise Stop(SCORES_INVALID)
        return data

    def gold(self) -> dict:
        """score_batch's gold map (score_batch.py:101-102): scenario to status and hash.
        It must name exactly the manifest's scenarios."""
        sn = self.scores("safety_net.json")
        gold = _get(sn, "gold")
        if type(gold) is not dict:
            raise Stop(SCORES_INVALID)
        if set(gold) != set(self.scenarios):
            raise Stop(BATCH_INCOMPLETE)
        for meta in gold.values():
            _label(_get(meta, "status"), GOLD_STATUSES)
            if not _is_id(_get(meta, "sha256"), compare._HEX64_RE):
                raise Stop(SCORES_INVALID)
        return gold


def scorer_versions(target: str) -> dict:
    """The versions of the scorers whose metrics are published for `target`, from the
    scorers' own constants. score_patient is not here: none of its metrics is
    published (D13)."""
    versions = {"score_batch": SB.SCORER_VERSION}
    if target == "pipeline":
        versions.update(score_step2_advice=S2.METRIC_VERSION, score_compose=CP.METRIC_VERSION,
                        haz03_check=H3.CHECK_VERSION,
                        score_assemble=hashlib.sha256(ASSEMBLE_SCORER.read_bytes()).hexdigest())
    return versions


def _key_parts(batch: _Batch) -> dict:
    return {"corpus_sha256": batch.corpus_sha256,
            "gold_sha256": {sid: meta["sha256"] for sid, meta in batch.gold().items()},
            "scorer_versions": scorer_versions(batch.target)}


def key_parts(batch_dir: Path) -> dict:
    """What a finished, scored batch adds to a run's keys. Part 5 hands these to
    `ws7_manifest.build_manifest(**parts)`; `build_summary` uses the same ones."""
    return _key_parts(_Batch(batch_dir))


def _check_own(batch: _Batch, own: dict) -> None:
    """The batch must be what the job's own values say was run."""
    target = own.get("target") if type(own) is dict else None
    ok = target == batch.target and own.get("model_id") == batch.model_id \
        and own.get("prompt_caching") is batch.prompt_caching
    if ok and target == "v1":
        # run_cold_eval.py:672 and :215: the file hashes of the prompts that were loaded.
        hashes = own.get("v1_prompt_sha256")
        prompts = batch.prompts if type(batch.prompts) is dict else {}
        system, patient = prompts.get("system"), prompts.get("patient")
        ok = type(hashes) is dict and type(system) is dict \
            and system.get("file_sha256") == hashes.get("system_prompt") \
            and (own.get("patient_second_pass") is True) == (patient is not None) \
            and (patient is None or (type(patient) is dict and patient.get("file_sha256")
                                     == hashes.get("patient_system_prompt")))
    if not ok:
        raise Stop(OWN_INVALID)


# ----------------------------------------------------------------- HAZ-03

def write_haz03(batch_dir: Path, gold_dir: Path = G.GOLD_DIR) -> Path:
    """Run the HAZ-03 check, unchanged, on every record of a pipeline batch, and
    write `scores/haz03.json`. The rows are the check's own: IDs, enum values and
    fixed labels (haz03_check.py:35). The gold is the gold score_batch scored
    against: each file's sha256 must be the one in score_batch's gold map."""
    batch = _Batch(batch_dir)
    if batch.target != "pipeline":
        raise Stop(BATCH_INVALID)          # the check does not cover v1 (haz03_check.py:33)
    gold_map = batch.gold()
    results = []
    for sid in sorted(gold_map):
        try:
            raw = (Path(gold_dir) / f"{sid}.json").read_bytes()
            if hashlib.sha256(raw).hexdigest() != gold_map[sid]["sha256"]:
                raise Stop(SCORES_INVALID)
            gold = json.loads(raw.decode("utf-8"))
            record = json.loads((batch.dir / "r1" / f"{sid}.json").read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            raise Stop(SCORES_INVALID) from None
        if type(gold) is not dict or gold.get("scenario_id") != sid or type(record) is not dict \
                or record.get("scenario_id") != sid:
            raise Stop(SCORES_INVALID)
        results.append(H3.check_record(record, gold))
    out = batch.dir / "scores" / HAZ03_NAME
    out.write_bytes(compare.render({"check": "HAZ-03", "check_version": H3.CHECK_VERSION,
                                    "batch_id": batch.batch_id, "results": results}))
    return out


# ---------------------------------------------------------- the families

def _rows(results: object, gold: dict, sid_field: str = "scenario_id") -> dict:
    """The scored rows, by scenario. A row for a scenario the gold does not have, or
    two rows for one scenario, builds no summary."""
    if type(results) is not list:
        raise Stop(SCORES_INVALID)
    rows: dict = {}
    for row in results:
        sid = row.get(sid_field) if type(row) is dict else None
        if type(sid) is not str or sid not in gold or sid in rows or row.get("run", 1) != 1:
            raise Stop(SCORES_INVALID)
        rows[sid] = row
    return rows


def _verdicts(gold: dict, rows: dict, verdict_of) -> dict:
    """A verdict for every scenario in the gold, and for no other. A scenario with no
    scored row is "no_output", never left out."""
    return {"verdicts": {sid: verdict_of(rows[sid]) if sid in rows else NO_OUTPUT
                         for sid in sorted(gold)}}


def _mapped(row: dict, mapping: dict) -> str:
    verdict = row.get("verdict")
    if type(verdict) is not str or verdict not in mapping:
        raise Stop(SCORES_INVALID)
    return mapping[verdict]


def _safety_netting(batch: _Batch, gold: dict) -> dict:
    sn = batch.scores("safety_net.json")
    if sn.get("scorer_version") != SB.SCORER_VERSION or sn.get("generator") != batch.target:
        raise Stop(SCORES_INVALID)
    rows = _rows(_get(sn, "results"), gold)
    scored = list(rows.values())
    failures = [_labels(r.get("failures"), SAFETY_NET_FAILURES) for r in scored]
    metrics = {
        "safety_net_verdict": _verdicts(gold, rows, lambda r: _mapped(r, SAFETY_NET_VERDICT)),
        "sn_invented_triggers": sum(_count(_get(r, "seek_help", "n_invented")) for r in scored),
        "sn_pinned_line_missing": sum("pinned_line_missing" in f for f in failures),
        "sn_route_incorrect": sum(not _flag(_get(r, "route", "correct")) for r in scored),
        "sn_review": sum(r["verdict"] == "review" for r in scored),
    }
    if batch.target == "v1":
        # The current gate, re-run by the scorer on the stored output (score_batch.py:167).
        metrics["sn_gate_failures"] = sum(not _flag(_get(r, "gate", "now_ok")) for r in scored)
    return metrics


def _medications(batch: _Batch, gold: dict) -> dict:
    meds = batch.scores("meds.json")
    if meds.get("scorer_version") != SB.SCORER_VERSION:
        raise Stop(SCORES_INVALID)
    rows = _rows(_get(meds, "results"), gold)
    scored = list(rows.values())
    return {
        "meds_verdict": _verdicts(gold, rows, lambda r: _mapped(r, MEDS_VERDICT)),
        "meds_invented_doses": sum(_count(r.get("invented_doses")) for r in scored),
        "meds_invented_frequencies": sum(_count(r.get("invented_frequencies")) for r in scored),
        "meds_partial": sum(r["verdict"] == "partial" for r in scored),
        "meds_review": sum(r["verdict"] == "review" for r in scored),
    }


def _step2_advice(batch: _Batch, gold: dict) -> dict:
    s2 = batch.scores("step2_advice.json")
    if s2.get("metric_version") != S2.METRIC_VERSION:
        raise Stop(SCORES_INVALID)
    rows = _rows(_get(s2, "results"), gold)
    # `not_scored` (score_step2_advice.py:106): no step-3 facts, so nothing to judge.
    scored = [r for r in rows.values() if "not_scored" not in r]
    errors = [None if r.get("route_error") is None else _label(r.get("route_error"), S2.ROUTE_ERRORS)
              for r in scored]
    metrics = {
        "step2_route_verdict": _verdicts(
            gold, rows, lambda r: NO_OUTPUT if "not_scored" in r
            else PASS if _flag(r.get("route_correct")) else FAIL),
        "step2_gold_items_recalled": sum(_count(_get(r, "verified", "n_gold_matched"))
                                         for r in scored),
        "step2_right_route_wrong_item": sum(_flag(r.get("right_route_wrong_item")) for r in scored),
    }
    for name in S2.ROUTE_ERRORS:
        metrics[f"step2_{name}"] = errors.count(name)
    return metrics


def _composition(batch: _Batch, gold: dict) -> dict:
    """Counts only: score_compose emits no verdict for a record."""
    cp = batch.scores("compose_clinical.json")
    if cp.get("metric_version") != CP.METRIC_VERSION:
        raise Stop(SCORES_INVALID)
    rows = _rows(_get(cp, "results"), gold)
    sm = _get(cp, "summary")

    def of(*path: str) -> int:
        return _count(_get(sm, *path))

    def less(whole: int, part: int) -> int:
        return _count(whole - part)            # a part above its whole is refused, not clipped

    if of("n_records") != len(gold) or of("n_scored") != len(rows):
        raise Stop(SCORES_INVALID)
    checks = 0
    for row in rows.values():
        by_check = row.get("checks")
        if type(by_check) is not dict:
            raise Stop(SCORES_INVALID)
        checks += sum(_count(n) for n in by_check.values())
    metrics = {
        "compose_not_reached": less(of("n_records"), of("n_scored")),
        "compose_year_added": of("year_added_step6", "findings"),
        "compose_boxes_incorrect": less(of("n_scored"), of("boxes_correct")),
        "compose_unknown_fact_ids": of("unknown_fact_ids"),
        "compose_excluded_fact_ids": of("excluded_fact_ids"),
        "compose_contradictions_missing": less(of("contradictions", "given"),
                                               of("contradictions", "in_a")),
        "compose_field_omitted": of("field_omitted"),
        "compose_gp_action_missing_in_b": of("gp_action_missing_in_b"),
        "compose_checks_total": checks,
    }
    for kind in COMPOSE_REVIEW_KINDS:
        metrics[f"compose_flagged_{kind}"] = of("review", kind, "flagged")
    return metrics


def _haz03(batch: _Batch, gold: dict) -> dict:
    hz = batch.scores(HAZ03_NAME)
    if hz.get("check_version") != H3.CHECK_VERSION:
        raise Stop(SCORES_INVALID)
    rows = _rows(_get(hz, "results"), gold)
    kinds = [kind for r in rows.values() for kind in _labels(r.get("failures"), HAZ03_KINDS)]
    metrics = {"haz03_verdict": _verdicts(gold, rows, lambda r: _mapped(r, HAZ03_VERDICT)),
               "haz03_failures": len(kinds)}
    for kind in HAZ03_KINDS:
        metrics[f"haz03_{kind}"] = kinds.count(kind)
    return metrics


def _assembly(batch: _Batch, gold: dict) -> dict:
    """The outcomes, the rejections by step, the gate status and the placeholders
    left (ADR-011 D2). Nothing else in assemble.json is read: not `warnings`,
    `warnings_c`, `warnings_located`, `c_meaning_vs_step7`, `a5` or `branches`."""
    asm = batch.scores("assemble", "assemble.json")
    if asm.get("ab_only") is not False:
        raise Stop(SCORES_INVALID)
    rows = _rows(_get(asm, "results"), gold, "scenario")
    if set(rows) != set(gold):             # the replay accounts for every record (F2)
        raise Stop(SCORES_INVALID)
    outcomes = {sid: _label(r.get("outcome"), ASSEMBLY_OUTCOMES) for sid, r in rows.items()}
    rejected = [r for sid, r in rows.items() if outcomes[sid] == "rejected"]
    steps = [_label(r.get("step"), ASSEMBLY_STEPS) for r in rejected]
    gates = [_label(_get(r, "gate", "status"), BUNDLE_GATE_STATUSES)
             for sid, r in rows.items() if outcomes[sid] == "bundle"]
    metrics = {
        "assembly_not_bundled": sum(o != "bundle" for o in outcomes.values()),
        "assembly_upstream_missing": sum(o == "upstream_missing" for o in outcomes.values()),
        # The code is compared with one literal and is never copied.
        "assembly_gate_failed": sum(r.get("step") == "08b" and r.get("code") == GATE_FAILED
                                    for r in rejected),
        "assembly_placeholders_left": _count(_get(asm, "summary", "placeholders_left")),
    }
    for step in ASSEMBLY_STEPS:
        metrics[f"assembly_rejected_{step}"] = steps.count(step)
    for status in BUNDLE_GATE_STATUSES:
        metrics[f"assembly_gate_{status}"] = gates.count(status)
    return metrics


def _calls(batch: _Batch, report: object, label: object) -> dict:
    """The Run family's cost, token and call metrics, from this batch's part of the
    guard's report (ws7_client.py:320-354). The report is in integers, and it is what
    the ledger settles from."""
    bad = Stop(REPORT_INVALID)
    plan = reserve.plan(batch.target)
    step_ids = tuple(step.id for step in plan.steps)
    if type(report) is not dict or report.get("target") != batch.target:
        raise bad
    run_id = report.get("run_id")
    # The label is this batch's own name everywhere: the runner's batch ID, the batch in
    # the guard's report, and the summary's run_id. It starts with the run's ID.
    if not _is_id(run_id, compare._ID_RE) or not _is_id(label, compare._ID_RE) \
            or not label.startswith(run_id + ".") or label != batch.batch_id:
        raise bad
    booked = _get(report, "batches", label, code=REPORT_INVALID)
    total = _count(_get(booked, "total_usd_nanos", code=REPORT_INVALID), REPORT_INVALID)
    if _get(booked, "planned", code=REPORT_INVALID) != len(batch.scenarios):
        raise bad
    calls = report.get("calls")
    if type(calls) is not list or not all(type(c) is dict for c in calls) \
            or type(report.get("unmetered")) is not bool:
        raise bad
    for name in ("usage_unknown", "unmetered_calls"):
        _count(report.get(name), REPORT_INVALID)
    mine = [c for c in calls if c.get("batch") == label]
    for call in mine:
        _label(call.get("outcome"), _OUTCOMES, REPORT_INVALID)
        if call.get("step") is not None:
            _label(call.get("step"), step_ids, REPORT_INVALID)
    # A call whose usage is unknown, anywhere in the run: its cost is an estimate, so
    # the run is unmetered and no summary is built (ADR-011 D7, point 3).
    if report["unmetered"] or report["usage_unknown"] or report["unmetered_calls"] \
            or any(c["outcome"] not in _METERED_OUTCOMES for c in mine):
        raise Stop(UNMETERED)

    sent = [c for c in mine if c["outcome"] != wc.REFUSED]
    replies = [c for c in sent if c["outcome"] == wc.RESPONSE]
    if sum(_count(c.get("cost_usd_nanos"), REPORT_INVALID) for c in sent) != total:
        raise bad                                   # the rows are not this batch's total

    def tokens(call: dict, *names: str) -> int:
        return sum(_count(call.get(name), REPORT_INVALID) for name in names)

    metrics = {
        "cost_usd_micros": reserve.to_micros(total),      # rounds up: never understated
        "tokens_in_total": sum(tokens(c, "input_tokens") for c in replies),
        "tokens_out_total": sum(tokens(c, "output_tokens") for c in replies),
        "tokens_cache_read_total": sum(tokens(c, "cache_read_tokens") for c in replies),
        "tokens_cache_write_total": sum(tokens(c, "cache_write_tokens") for c in replies),
        "calls_not_response": len(sent) - len(replies),
        "calls_refused": len(mine) - len(sent),
        "spend_guard_trips": sum(c["outcome"] == wc.REFUSED and c.get("code") == wc.SPEND_GUARD
                                 for c in mine),
    }
    for step in step_ids:
        # Everything on the input side of one call, for the measured input allowance
        # (ADR-011 D7, point 1; slice 4).
        metrics[f"input_tokens_max_{step}"] = max(
            (tokens(c, "input_tokens", "cache_read_tokens", "cache_write_tokens")
             for c in replies if c.get("step") == step), default=0)
    return metrics


def _records(batch: _Batch, gold: dict) -> dict:
    """The Run family's metrics from the batch's own records: named fields only."""
    elapsed, failures = [], dict.fromkeys(PIPELINE_STEPS, 0)
    for sid in batch.scenarios:
        record = batch.record(sid)
        elapsed.append(to_ms(record.get("elapsed_s")))            # run_pipeline_eval.py:373
        if batch.target == "pipeline":
            step_errors = record.get("step_errors")               # run_pipeline_eval.py:376
            if type(step_errors) is not dict:
                raise Stop(BATCH_INVALID)
            for step in step_errors:      # the step's name; its error code is never copied
                failures[_label(step, PIPELINE_STEPS, BATCH_INVALID)] += 1
    metrics = {"elapsed_p90_ms": nearest_rank(elapsed, P90),
               "generations_failed": batch.failed,
               "gold_draft_records": sum(meta["status"] != "approved" for meta in gold.values())}
    if batch.target == "pipeline":
        metrics.update({f"step_failures_{step}": n for step, n in failures.items()})
    return metrics


# ------------------------------------------------------------ the summary

def build_summary(batch_dir: Path, *, own: dict, report: dict, batch_label: str,
                  month_assurance_waived: bool) -> dict:
    """The run summary (`ws7-run/1`) of one finished, scored batch of 18.

    `own` is what `ws7_manifest.match_check` returned. `report` is `Guard.report()`.
    `batch_label` is the batch's label in that report, which is also the runner's
    batch ID and becomes the summary's `run_id`. `month_assurance_waived` is the
    month's flag in the ledger. The result has passed the comparator's own check
    (`ws7_compare.check_run`)."""
    batch = _Batch(batch_dir)
    _check_own(batch, own)
    gold = batch.gold()
    try:
        keys = manifest.build_keys(own, **_key_parts(batch))
    except Stop:
        raise Stop(OWN_INVALID) from None
    if type(month_assurance_waived) is not bool:
        raise Stop(SUMMARY_INVALID)

    counts = _calls(batch, report, batch_label)
    counts.update(_records(batch, gold))
    counts["month_assurance_waived"] = int(month_assurance_waived)
    families = [_safety_netting, _medications]
    if batch.target == "pipeline":
        families += [_step2_advice, _composition, _haz03, _assembly]
    for family in families:
        counts.update(family(batch, gold))

    metrics = {name: value if type(value) is dict else {"count": _count(value, SUMMARY_INVALID)}
               for name, value in counts.items()}
    summary = {"schema": compare.RUN_SCHEMA, "run_id": batch_label, "keys": keys,
               "metrics": metrics}
    try:
        compare.check_run(summary)
    except compare.CompareError:
        raise Stop(SUMMARY_INVALID) from None
    if any(set(m["verdicts"]) != set(keys["gold_sha256"]) for m in metrics.values()
           if "verdicts" in m):
        raise Stop(SUMMARY_INVALID)
    return summary


def render(document: dict) -> bytes:
    """A summary's or a candidate's bytes: the comparator's own form (sorted keys,
    two-space indentation, one trailing newline), whatever PYTHONHASHSEED is."""
    return compare.render(document)


# ---------------------------------------------------- the candidate baseline

def _differing_keys(summaries: list) -> tuple:
    first = summaries[0]["keys"]
    return tuple(sorted(name for name in compare.KEY_NAMES
                        if any(s["keys"].get(name) != first.get(name) for s in summaries[1:])))


def build_candidate(summaries: object, *, baseline_id: object) -> dict:
    """A candidate baseline from the three run summaries of a manual baseline (D7).

    Returns `{"schema": "ws7-baseline-candidate/1", "run_ids": [...], "baseline": {...}}`.
    The `baseline` member is a `ws7-baseline/1` object that has passed the
    comparator's own check (`ws7_compare.check_baseline`). `baseline_id` is given by
    the author; it has no default and never comes from the clock. Nothing here
    writes a file, and nothing here freezes a baseline."""
    if type(summaries) not in (list, tuple) or len(summaries) != compare.BASELINE_RUNS:
        raise Stop(NOT_THREE_RUNS)
    if not _is_id(baseline_id, compare._ID_RE):
        raise Stop(CANDIDATE_ID_INVALID)
    runs = []
    for summary in summaries:
        try:
            run = compare.check_run(json.loads(json.dumps(summary)))      # a copy
        except (compare.CompareError, TypeError, ValueError):
            raise Stop(CANDIDATE_SUMMARY_INVALID) from None
        drafts = run["metrics"].get("gold_draft_records")
        if type(drafts) is not dict or "count" not in drafts or any(
                set(m["verdicts"]) != set(run["keys"]["gold_sha256"])
                for m in run["metrics"].values() if "verdicts" in m):
            raise Stop(CANDIDATE_SUMMARY_INVALID)
        runs.append(run)
    run_ids = [run["run_id"] for run in runs]
    if len(set(run_ids)) != len(run_ids):
        raise Stop(RUNS_NOT_DISTINCT)             # the same batch given twice
    differing = _differing_keys(runs)
    if differing:
        raise Stop(KEYS_DIFFER, ids=differing)
    shapes = [{name: sorted(value) for name, value in run["metrics"].items()} for run in runs]
    if any(shape != shapes[0] for shape in shapes[1:]):
        raise Stop(METRICS_DIFFER)                # in some runs and not others, or another type
    if any(run["metrics"]["gold_draft_records"]["count"] for run in runs):
        raise Stop(DRAFT_GOLD)                    # scores against draft gold are not the baseline
    metrics: dict = {}
    for name in sorted(runs[0]["metrics"]):
        values = [run["metrics"][name] for run in runs]
        if "count" in values[0]:
            metrics[name] = {"counts": [value["count"] for value in values]}
            continue
        verdicts = {sid: [value["verdicts"][sid] for value in values]
                    for sid in sorted(values[0]["verdicts"])}
        if any(NO_OUTPUT in three for three in verdicts.values()):
            raise Stop(CANDIDATE_NO_OUTPUT)       # ws7-baseline/1 has no such verdict
        metrics[name] = {"verdicts": verdicts,
                         "unstable": [sid for sid, three in verdicts.items()
                                      if not all(v == PASS for v in three)]}
    baseline = {"schema": compare.BASELINE_SCHEMA, "baseline_id": baseline_id,
                "keys": runs[0]["keys"], "metrics": metrics}
    try:
        compare.check_baseline(baseline)
    except compare.CompareError:
        raise Stop(CANDIDATE_SUMMARY_INVALID) from None
    return {"schema": CANDIDATE_SCHEMA, "run_ids": run_ids, "baseline": baseline}
