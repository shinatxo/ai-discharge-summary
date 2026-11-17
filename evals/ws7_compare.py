#!/usr/bin/env python3
"""WS7 offline comparator: one run against a frozen baseline (ADR-011, D9).

    python evals/ws7_compare.py --baseline B.json --rules R.json --run S.json --out RESULT.json

Reads three JSON files and writes one JSON result: a breach, or not, for each
family and kind the rules name, with the reasons. The file shapes are in
docs/WS7-COMPARATOR.md.

Exit codes: 0 no breach, 1 a breach, 2 no baseline or an error. The exit code
is the authority; "no baseline" and "error" are never a pass.

Standard library only. No AWS, no model call, no scorer and no pipeline
import. The result holds IDs, counts and fixed labels, never text: every
string copied from an input file has first matched a strict ID pattern, and a
string that does not match is an error that is not echoed. The console shows
fixed labels and counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

RULES_SCHEMA = "ws7-rules/1"
BASELINE_SCHEMA = "ws7-baseline/1"
RUN_SCHEMA = "ws7-run/1"
RESULT_SCHEMA = "ws7-result/1"

BASELINE_RUNS = 3  # ADR-011 D7: a baseline is one batch of 18, three times

TARGETS = ("pipeline", "v1")
FAMILIES = ("run", "safety_netting", "medications", "step2_advice",
            "composition", "haz03", "assembly", "part_c")
KINDS = (1, 2, 3)

PASS, FAIL, NO_OUTPUT = "pass", "fail", "no_output"
BASELINE_VERDICTS = (PASS, FAIL)
RUN_VERDICTS = (PASS, FAIL, NO_OUTPUT)

NO_BREACH, BREACH, NO_BASELINE, ERROR = "no_breach", "breach", "no_baseline", "error"
EXIT_CODES = {NO_BREACH: 0, BREACH: 1, NO_BASELINE: 2, ERROR: 2}

# The baseline key (ADR-011 D9). Every name here is compared; a name absent
# from both files (pipeline_version on a v1 target, say) compares equal.
KEY_NAMES = ("target", "pipeline_version", "v1_prompt_sha256", "patient_second_pass",
             "model_id", "prompt_caching", "corpus_sha256", "gold_sha256",
             "scorer_versions")
_COMMON_KEYS = ("target", "model_id", "prompt_caching", "corpus_sha256",
                "gold_sha256", "scorer_versions")
_TARGET_KEYS = {
    "pipeline": _COMMON_KEYS + ("pipeline_version",),
    "v1": _COMMON_KEYS + ("v1_prompt_sha256", "patient_second_pass"),
}
_V1_PROMPTS = ("system_prompt", "patient_system_prompt")

_METRIC_RE = re.compile(r"[a-z0-9_]{1,64}")
_SCENARIO_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_MODEL_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


class CompareError(Exception):
    """A fixed code, the file it came from, a field label and a metric ID.

    `field` is always a literal written in this file. `metric` is set only
    after the ID has matched _METRIC_RE. Nothing else from an input is kept.
    """

    def __init__(self, code: str, file: str | None = None, field: str | None = None,
                 metric: str | None = None):
        super().__init__(code)
        self.code, self.file, self.field, self.metric = code, file, field, metric

    def as_dict(self) -> dict:
        return {"code": self.code, "file": self.file, "field": self.field,
                "metric": self.metric}


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


def _read(path: Path, file: str) -> tuple[object, str]:
    """The parsed JSON and the sha256 of the file's bytes."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise CompareError("file_unreadable", file) from None
    sha = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_no_constants)
    except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise CompareError("json_invalid", file) from None
    return data, sha


# ------------------------------------------------------------- validating

def _bad(file: str, field: str, metric: str | None = None) -> CompareError:
    return CompareError("shape_invalid", file, field, metric)


def _is_int(value: object) -> bool:
    return type(value) is int  # a bool is not a count


def _is_id(value: object, pattern: re.Pattern) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _object(value: object, fields: tuple, file: str, field: str,
            metric: str | None = None) -> dict:
    """`value` must be an object with exactly these fields."""
    if type(value) is not dict or set(value) != set(fields):
        raise _bad(file, field, metric)
    return value


def _check_keys(keys: object, file: str) -> dict:
    if type(keys) is not dict or keys.get("target") not in TARGETS:
        raise _bad(file, "keys.target")
    target = keys["target"]
    _object(keys, _TARGET_KEYS[target], file, "keys")

    if target == "pipeline":
        if not _is_id(keys["pipeline_version"], _HEX64_RE):
            raise _bad(file, "keys.pipeline_version")
    else:
        prompts = keys["v1_prompt_sha256"]
        if type(prompts) is not dict or set(prompts) != set(_V1_PROMPTS) \
                or not all(_is_id(v, _HEX64_RE) for v in prompts.values()):
            raise _bad(file, "keys.v1_prompt_sha256")
        if type(keys["patient_second_pass"]) is not bool:
            raise _bad(file, "keys.patient_second_pass")

    if not _is_id(keys["model_id"], _MODEL_RE):
        raise _bad(file, "keys.model_id")
    if type(keys["prompt_caching"]) is not bool:
        raise _bad(file, "keys.prompt_caching")
    if not _is_id(keys["corpus_sha256"], _HEX64_RE):
        raise _bad(file, "keys.corpus_sha256")

    gold = keys["gold_sha256"]
    if type(gold) is not dict or not gold or not all(
            _is_id(k, _SCENARIO_RE) and _is_id(v, _HEX64_RE) for k, v in gold.items()):
        raise _bad(file, "keys.gold_sha256")

    scorers = keys["scorer_versions"]
    if type(scorers) is not dict or not scorers or not all(
            _is_id(k, _METRIC_RE) and ((_is_int(v) and v >= 0) or _is_id(v, _ID_RE))
            for k, v in scorers.items()):
        raise _bad(file, "keys.scorer_versions")
    return keys


def _check_rules(data: object) -> dict:
    file = "rules"
    rules = _object(data, ("schema", "target", "metrics"), file, "top")
    if rules["schema"] != RULES_SCHEMA:
        raise _bad(file, "schema")
    if rules["target"] not in TARGETS:
        raise _bad(file, "target")
    if type(rules["metrics"]) is not list or not rules["metrics"]:
        raise _bad(file, "metrics")  # no rules would be a silent pass

    seen: set = set()
    for entry in rules["metrics"]:
        if type(entry) is not dict or not _is_id(entry.get("metric"), _METRIC_RE):
            raise _bad(file, "metrics.metric")
        metric = entry["metric"]
        if metric in seen:
            raise _bad(file, "metrics.metric", metric)
        seen.add(metric)
        kind = entry.get("kind")
        if not _is_int(kind) or kind not in KINDS:
            raise _bad(file, "metrics.kind", metric)
        extra = {1: (), 2: ("min_flips",), 3: ("margin",)}[kind]
        _object(entry, ("metric", "family", "kind") + extra, file, "metrics.entry", metric)
        if entry["family"] not in FAMILIES:
            raise _bad(file, "metrics.family", metric)
        if kind == 2 and not (_is_int(entry["min_flips"]) and entry["min_flips"] >= 1):
            raise _bad(file, "metrics.min_flips", metric)
        if kind == 3 and not (_is_int(entry["margin"]) and entry["margin"] >= 0):
            raise _bad(file, "metrics.margin", metric)
    return rules


def _check_metric_ids(metrics: object, file: str) -> dict:
    if type(metrics) is not dict or not all(_is_id(m, _METRIC_RE) for m in metrics):
        raise _bad(file, "metrics")
    return metrics


def _check_baseline(data: object) -> dict:
    file = "baseline"
    base = _object(data, ("schema", "baseline_id", "keys", "metrics"), file, "top")
    if base["schema"] != BASELINE_SCHEMA:
        raise _bad(file, "schema")
    if not _is_id(base["baseline_id"], _ID_RE):
        raise _bad(file, "baseline_id")
    _check_keys(base["keys"], file)

    for metric, value in _check_metric_ids(base["metrics"], file).items():
        if type(value) is not dict:
            raise _bad(file, "metrics.value", metric)
        if set(value) == {"counts"}:
            counts = value["counts"]
            if type(counts) is not list or len(counts) != BASELINE_RUNS \
                    or not all(_is_int(c) and c >= 0 for c in counts):
                raise _bad(file, "metrics.counts", metric)
        elif set(value) == {"verdicts", "unstable"}:
            verdicts = value["verdicts"]
            if type(verdicts) is not dict or not verdicts:
                raise _bad(file, "metrics.verdicts", metric)
            for scenario, three in verdicts.items():
                if not _is_id(scenario, _SCENARIO_RE) or type(three) is not list \
                        or len(three) != BASELINE_RUNS \
                        or not all(v in BASELINE_VERDICTS for v in three):
                    raise _bad(file, "metrics.verdicts", metric)
            unstable = value["unstable"]
            if type(unstable) is not list \
                    or not all(_is_id(s, _SCENARIO_RE) for s in unstable) \
                    or len(set(unstable)) != len(unstable):
                raise _bad(file, "metrics.unstable", metric)
            # The file names its unstable scenarios (ADR-011 D9). The list is
            # checked against the verdicts so the two cannot disagree.
            if set(unstable) != {s for s, three in verdicts.items() if not _stable(three)}:
                raise CompareError("unstable_list_inconsistent", file,
                                   "metrics.unstable", metric)
        else:
            raise _bad(file, "metrics.value", metric)
    return base


def _check_run(data: object) -> dict:
    file = "run"
    run = _object(data, ("schema", "run_id", "keys", "metrics"), file, "top")
    if run["schema"] != RUN_SCHEMA:
        raise _bad(file, "schema")
    if not _is_id(run["run_id"], _ID_RE):
        raise _bad(file, "run_id")
    _check_keys(run["keys"], file)

    for metric, value in _check_metric_ids(run["metrics"], file).items():
        if type(value) is not dict:
            raise _bad(file, "metrics.value", metric)
        if set(value) == {"count"}:
            if not (_is_int(value["count"]) and value["count"] >= 0):
                raise _bad(file, "metrics.count", metric)
        elif set(value) == {"verdicts"}:
            verdicts = value["verdicts"]
            if type(verdicts) is not dict or not verdicts or not all(
                    _is_id(s, _SCENARIO_RE) and v in RUN_VERDICTS
                    for s, v in verdicts.items()):
                raise _bad(file, "metrics.verdicts", metric)
        else:
            raise _bad(file, "metrics.value", metric)
    return run


# -------------------------------------------------------------- comparing

def _stable(three: list) -> bool:
    """Stable at baseline: the same passing verdict in all three runs."""
    return all(v == PASS for v in three)


def _kind1(metric: str, base: dict, run: dict) -> dict:
    count, base_max = run["count"], max(base["counts"])
    return {"metric": metric, "breach": int(count > base_max), "run": count,
            "baseline_max": base_max}


def _kind2(metric: str, base: dict, run: dict, min_flips: int) -> dict:
    verdicts, now = base["verdicts"], run["verdicts"]
    stable = sorted(s for s, three in verdicts.items() if _stable(three))
    # "no_output" is not a pass: on a stable scenario it is a flip.
    flips = [s for s in stable if now[s] != PASS]
    return {
        "metric": metric,
        "breach": int(len(flips) >= min_flips),
        "flips": flips,
        "no_output": sorted(s for s, v in now.items() if v == NO_OUTPUT),
        "min_flips": min_flips,
        "stable": len(stable),
        "unstable": [{"scenario": s, "baseline_passes": verdicts[s].count(PASS)}
                     for s in sorted(verdicts) if not _stable(verdicts[s])],
    }


def _kind3(metric: str, base: dict, run: dict, margin: int) -> dict:
    count = run["count"]
    low, high = min(base["counts"]) - margin, max(base["counts"]) + margin
    direction = "below" if count < low else "above" if count > high else "inside"
    return {"metric": metric, "breach": int(direction != "inside"),
            "direction": direction, "run": count, "low": low, "high": high}


def _compare(rules: dict, base: dict, run: dict) -> tuple[list, list]:
    """The rows (one per family and kind) and the errors. Errors mean no rows."""
    errors: list = []
    if rules["target"] != run["keys"]["target"]:  # the keys already match
        return [], [CompareError("target_mismatch", "rules", "target")]

    base_metrics, run_metrics = base["metrics"], run["metrics"]
    for entry in rules["metrics"]:
        metric, kind = entry["metric"], entry["kind"]
        wanted = ("verdicts", "verdicts") if kind == 2 else ("counts", "count")
        present = True
        for file, metrics, field in (("baseline", base_metrics, wanted[0]),
                                     ("run", run_metrics, wanted[1])):
            if metric not in metrics:
                errors.append(CompareError("metric_missing", file, None, metric))
                present = False
            elif field not in metrics[metric]:
                errors.append(CompareError("metric_kind_mismatch", file, None, metric))
                present = False
        if present and kind == 2 and \
                set(base_metrics[metric]["verdicts"]) != set(run_metrics[metric]["verdicts"]):
            errors.append(CompareError("scenario_set_differs", "run", None, metric))
    if errors:
        return [], errors

    rows: dict = {}
    for entry in rules["metrics"]:
        metric, kind = entry["metric"], entry["kind"]
        b, r = base_metrics[metric], run_metrics[metric]
        if kind == 1:
            detail = _kind1(metric, b, r)
        elif kind == 2:
            detail = _kind2(metric, b, r, entry["min_flips"])
        else:
            detail = _kind3(metric, b, r, entry["margin"])
        rows.setdefault((entry["family"], kind), []).append(detail)

    return [{"family": family, "kind": kind,
             "breach": int(any(d["breach"] for d in details)),
             "metrics": sorted(details, key=lambda d: d["metric"])}
            for (family, kind), details in sorted(rows.items())], []


def _result(status: str, *, baseline_id: str | None, run_id: str | None, inputs: dict,
            key_mismatch: list = (), errors: list = (), comparisons: list = (),
            unruled: list = ()) -> dict:
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        "exit_code": EXIT_CODES[status],
        "baseline_id": baseline_id,
        "run_id": run_id,
        "inputs": inputs,
        "key_mismatch": list(key_mismatch),
        "errors": sorted((e.as_dict() for e in errors),
                         key=lambda e: tuple(e[k] or "" for k in
                                             ("code", "file", "field", "metric"))),
        "comparisons": list(comparisons),
        "unruled_metrics": list(unruled),
    }


def compare_files(baseline_path: Path, rules_path: Path, run_path: Path) -> dict:
    """Compare one run with one baseline under one rules file. Never raises
    for a bad input: that is an "error" result."""
    inputs = {"baseline_sha256": None, "rules_sha256": None, "run_sha256": None}
    loaded: dict = {}
    errors: list = []
    for file, path, check in (("baseline", baseline_path, _check_baseline),
                              ("rules", rules_path, _check_rules),
                              ("run", run_path, _check_run)):
        try:
            data, inputs[f"{file}_sha256"] = _read(Path(path), file)
            loaded[file] = check(data)
        except CompareError as exc:
            errors.append(exc)

    ids = {"baseline_id": loaded["baseline"]["baseline_id"] if "baseline" in loaded else None,
           "run_id": loaded["run"]["run_id"] if "run" in loaded else None}
    if errors:
        return _result(ERROR, inputs=inputs, errors=errors, **ids)

    rules, base, run = loaded["rules"], loaded["baseline"], loaded["run"]
    mismatch = sorted(k for k in KEY_NAMES if base["keys"].get(k) != run["keys"].get(k))
    if mismatch:
        return _result(NO_BASELINE, inputs=inputs, key_mismatch=mismatch, **ids)

    comparisons, errors = _compare(rules, base, run)
    if errors:
        return _result(ERROR, inputs=inputs, errors=errors, **ids)

    ruled = {entry["metric"] for entry in rules["metrics"]}
    unruled = [{"metric": m, "in_baseline": m in base["metrics"], "in_run": m in run["metrics"]}
               for m in sorted((set(base["metrics"]) | set(run["metrics"])) - ruled)]
    status = BREACH if any(row["breach"] for row in comparisons) else NO_BREACH
    return _result(status, inputs=inputs, comparisons=comparisons, unruled=unruled, **ids)


def render(result: dict) -> bytes:
    """The result file's bytes: the same result always gives the same bytes."""
    return (json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _console(result: dict) -> str:
    rows = result["comparisons"]
    return ("ws7_compare: status={} exit={} rows={} breaches={} errors={} "
            "key_mismatches={} unruled={}").format(
        result["status"], result["exit_code"], len(rows),
        sum(row["breach"] for row in rows), len(result["errors"]),
        len(result["key_mismatch"]), len(result["unruled_metrics"]))


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="WS7 offline comparator: a run against a frozen baseline (ADR-011 D9).")
    parser.add_argument("--baseline", required=True, help="the baseline JSON file")
    parser.add_argument("--rules", required=True, help="the rules JSON file")
    parser.add_argument("--run", required=True, help="the run summary JSON file")
    parser.add_argument("--out", required=True, help="where to write the result JSON")
    args = parser.parse_args(argv)  # a usage error exits 2

    # Nothing below may end in Python's default exit code of 1 for an uncaught
    # exception: 1 means "a breach".
    try:
        try:
            result = compare_files(Path(args.baseline), Path(args.rules), Path(args.run))
        except Exception:
            result = _result(ERROR, baseline_id=None, run_id=None,
                             inputs={"baseline_sha256": None, "rules_sha256": None,
                                     "run_sha256": None},
                             errors=[CompareError("internal_error")])
        try:
            Path(args.out).write_bytes(render(result))
        except OSError:
            print("ws7_compare: status=error exit=2 code=output_unwritable", file=sys.stderr)
            return EXIT_CODES[ERROR]
        print(_console(result))
        return result["exit_code"]
    except Exception:
        print("ws7_compare: status=error exit=2 code=internal_error", file=sys.stderr)
        return EXIT_CODES[ERROR]


if __name__ == "__main__":
    sys.exit(main())
