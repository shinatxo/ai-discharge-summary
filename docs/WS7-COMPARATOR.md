# WS7 comparator

`evals/ws7_compare.py` compares one eval run with a frozen baseline and says, for each metric
family and kind of rule, whether there is a breach. It is slice 1 of WS7
([ADR-011](adr/ADR-011-ws7-scheduled-eval.md), D9). It defines the file shapes that the later
entrypoint (slice 2) will write.

- It is offline. It makes no AWS call and no model call, and it costs nothing.
- It imports the Python standard library only. It does not import `boto3`, the pipeline or any
  scorer.
- **No rule number is set here.** Which metric has which kind, each flip threshold and each margin
  are set by the author before the first live run, in a rules file that changes only by pull
  request (ADR-011 D9). Every name and number in this document is an invented example.

## Running it

```zsh
python evals/ws7_compare.py --baseline B.json --rules R.json --run S.json --out RESULT.json
```

All four arguments are required. The result file is written for every status, including errors.
The console prints one line of fixed labels and counts:

```
ws7_compare: status=breach exit=1 rows=3 breaches=2 errors=0 key_mismatches=0 unruled=1
```

## Exit codes

| Code | Status | Meaning |
|---|---|---|
| 0 | `no_breach` | The run was compared with the baseline and no rule was broken |
| 1 | `breach` | At least one family and kind has a breach |
| 2 | `no_baseline` | A key differs, so there was no comparison |
| 2 | `error` | A file is missing or malformed, or a ruled metric is missing |

- **The exit code is the authority.** "No baseline" and "error" are never a pass.
- A usage error (a missing argument) also exits 2 and writes no result.
- An unexpected exception inside the comparator exits 2. Python's own default would be 1, which
  would read as a breach.
- If the result file cannot be written, the comparator exits 2 and prints a fixed line to stderr.
  An older result file at the same path is then stale; trust the exit code.

## The order of checks

1. **Shape.** All three files are read and checked. Any fault is an `error`, and every faulty file
   is reported.
2. **Keys.** The baseline's keys are compared with the run's. Any difference is `no_baseline`.
3. **Rules against data.** The rules file's target must match. Every ruled metric must be in both
   files, with the shape its kind needs. **Coverage:** every ruled kind-2 metric must have a
   verdict for exactly the scenarios in `keys.gold_sha256`, in the baseline and in the run. Any
   fault is an `error`, and every fault is reported: a metric that is missing from one file, or
   has the wrong shape there, does not stop the other file's coverage being checked.
4. **Compare.** One row per family and kind.

An `error` or `no_baseline` result has no rows. There is never a partial comparison.

## What is strict, and why

- Each input file has a fixed `schema` string. A different one is an error.
- An unknown field is an error, at every level. Adding a field means a new schema version, by
  pull request.
- A duplicate JSON key is an error (Python would otherwise keep the last one silently). So are
  `NaN` and `Infinity`.
- Counts are integers of zero or more. A boolean, a float (even `2.0`) or a string is an error.
- **Every string that can reach the result must match an ID pattern.** A string that does not
  match is an error and is not echoed. This is what keeps text out of the result.

| ID | Pattern |
|---|---|
| Metric name, scorer name | `[a-z0-9_]{1,64}` |
| Scenario ID | `[A-Za-z0-9_-]{1,32}` |
| `baseline_id`, `run_id`, a scorer version given as a string | `[A-Za-z0-9._:-]{1,64}` |
| `model_id` | `[A-Za-z0-9._:-]{1,128}` |
| A hash | 64 lowercase hex characters |

## The keys

The baseline and the run summary each carry a `keys` object. It is the baseline key of ADR-011 D9.

| Field | Type | Target |
|---|---|---|
| `target` | `"pipeline"` or `"v1"` | both |
| `pipeline_version` | hash | pipeline only; forbidden for v1 |
| `v1_prompt_sha256` | object with `system_prompt` and `patient_system_prompt`, each a hash | v1 only; forbidden for the pipeline |
| `patient_second_pass` | boolean | v1 only; forbidden for the pipeline |
| `model_id` | ID string | both |
| `prompt_caching` | boolean | both |
| `corpus_sha256` | hash | both |
| `gold_sha256` | object: scenario ID to hash; not empty | both |
| `scorer_versions` | object: scorer name to an integer or an ID string; not empty | both |

- `patient_second_pass` is a key for v1 because it changes v1's patient output.
- The pipeline needs no prompt key: `pipeline_version` already covers the step prompts, the facts
  schema and the code.
- Values are compared exactly. A scorer version of `2` does not match `"2"`.
- A missing or extra key is an error, not a mismatch. A mismatch is two well-formed values that
  differ.
- The check of a `keys` object is public, as `check_keys()`. The run manifest's `keys` are
  checked with the same function (`evals/ws7_manifest.py`).

## The public checks

Three checks are public, so that the code that writes a file checks it with the code that will
read it. Each takes parsed JSON and raises `CompareError` with a fixed code.

| Function | Checks | Used by |
|---|---|---|
| `check_keys(keys, file)` | A `keys` object | `ws7_manifest.build_keys()`, for the run manifest and the run summary (part 3) |
| `check_run(data)` | A whole run summary (`ws7-run/1`) | `ws7_summary.build_summary()`, on its result; `build_candidate()`, on each summary it is given (part 4) |
| `check_baseline(data)` | A whole baseline (`ws7-baseline/1`), the `unstable` list included | `ws7_summary.build_candidate()`, on its `baseline` member, before it returns anything (part 4) |

`check_run` and `check_baseline` were private until part 4. They were renamed only; what they
check did not change. The comparator itself imports nothing from the modules that call them.

## The rules file (`ws7-rules/1`)

| Field | Type | Note |
|---|---|---|
| `schema` | string | `"ws7-rules/1"` |
| `target` | `"pipeline"` or `"v1"` | One rules file per target. It must equal the run's target, or the result is an error |
| `metrics` | list of entries, not empty | A metric named twice is an error |
| entry `metric` | metric name | |
| entry `family` | one of `run`, `safety_netting`, `medications`, `step2_advice`, `composition`, `haz03`, `assembly`, `part_c` | The families of ADR-011 D9 |
| entry `kind` | `1`, `2` or `3` | |
| entry `min_flips` | integer, 1 or more | Kind 2 only: required there, forbidden elsewhere |
| entry `margin` | integer, 0 or more | Kind 3 only: required there, forbidden elsewhere |

```json
{
  "schema": "ws7-rules/1",
  "target": "pipeline",
  "metrics": [
    {"metric": "invented_doses", "family": "medications", "kind": 1},
    {"metric": "safety_net_verdict", "family": "safety_netting", "kind": 2, "min_flips": 1},
    {"metric": "placeholders_left", "family": "assembly", "kind": 3, "margin": 2}
  ]
}
```

## The baseline file (`ws7-baseline/1`)

It holds counts and verdicts from the three baseline runs, never text.

| Field | Type | Note |
|---|---|---|
| `schema` | string | `"ws7-baseline/1"` |
| `baseline_id` | ID string | |
| `keys` | object | See above |
| `metrics` | object: metric name to a count metric or a verdict metric | |
| count metric | `{"counts": [a, b, c]}` | Exactly three integers, one per baseline run |
| verdict metric | `{"verdicts": {...}, "unstable": [...]}` | |
| `verdicts` | object: scenario ID to a list of exactly three of `"pass"` or `"fail"`; not empty | `"no_output"` in a baseline is an error |
| `unstable` | list of scenario IDs | Every scenario that did not pass all three runs. The comparator works the list out again from the verdicts, and a disagreement is an error |

Hashes are shortened in the examples. A real one has 64 characters.

A baseline is frozen by the author, by pull request. Code only builds a **candidate**: an
envelope with the schema `ws7-baseline-candidate/1` and the baseline as its `baseline` member
(`docs/WS7-ENTRYPOINT.md`, part 4). The comparator refuses the envelope as a baseline
(`shape_invalid`, exit code 2), so a candidate is never compared against by accident.

```json
{
  "schema": "ws7-baseline/1",
  "baseline_id": "b-2026-12-14",
  "keys": {
    "target": "pipeline",
    "pipeline_version": "3333…33",
    "model_id": "anthropic.claude-sonnet-4-6",
    "prompt_caching": true,
    "corpus_sha256": "aaaa…aa",
    "gold_sha256": {"S1": "9999…99", "S2": "5555…55", "S3": "eeee…ee"},
    "scorer_versions": {"score_batch": 2, "haz03_check": 1}
  },
  "metrics": {
    "invented_doses": {"counts": [0, 1, 0]},
    "placeholders_left": {"counts": [4, 7, 5]},
    "safety_net_verdict": {
      "verdicts": {
        "S1": ["pass", "pass", "pass"],
        "S2": ["pass", "fail", "pass"],
        "S3": ["pass", "pass", "pass"]
      },
      "unstable": ["S2"]
    }
  }
}
```

For the v1 target the keys are:

```json
{
  "target": "v1",
  "v1_prompt_sha256": {"system_prompt": "cccc…cc", "patient_system_prompt": "dddd…dd"},
  "patient_second_pass": true,
  "model_id": "anthropic.claude-sonnet-4-6",
  "prompt_caching": true,
  "corpus_sha256": "aaaa…aa",
  "gold_sha256": {"S1": "9999…99", "S2": "5555…55", "S3": "eeee…ee"},
  "scorer_versions": {"score_batch": 2}
}
```

## The run summary (`ws7-run/1`)

| Field | Type | Note |
|---|---|---|
| `schema` | string | `"ws7-run/1"` |
| `run_id` | ID string | |
| `keys` | object | See above |
| `metrics` | object: metric name to a count metric or a verdict metric | |
| count metric | `{"count": n}` | One integer |
| verdict metric | `{"verdicts": {...}}` | Scenario ID to `"pass"`, `"fail"` or `"no_output"`; not empty |

`"no_output"` means the generation produced nothing that could be scored.

`evals/ws7_summary.py` builds this file (`docs/WS7-ENTRYPOINT.md`, part 4). That document lists
the real metric names, where each comes from, and how a scorer's verdict becomes `"pass"`,
`"fail"` or `"no_output"`. The names in the example below are invented.

```json
{
  "schema": "ws7-run/1",
  "run_id": "2026-12-16T0400",
  "keys": {"…": "the same values as the baseline"},
  "metrics": {
    "invented_doses": {"count": 1},
    "placeholders_left": {"count": 10},
    "safety_net_verdict": {"verdicts": {"S1": "fail", "S2": "fail", "S3": "no_output"}},
    "tokens_out_total": {"count": 41250}
  }
}
```

## The three kinds of rule

**Kind 1, zero-tolerance counters.** A breach is a count above the highest of the three baseline
counts. Equal or lower is not a breach.

**Kind 2, per-scenario flips.**

- A scenario is *stable* when it passed all three baseline runs. Anything else is *unstable*,
  including a scenario that failed all three.
- A *flip* is a stable scenario whose run verdict is not `"pass"`. Both `"fail"` and `"no_output"`
  count.
- A breach is `min_flips` or more flips. Flipped scenarios are named even when there are too few
  for a breach.
- An unstable scenario is never a flip. It is listed with the number of baseline runs it passed
  (0, 1 or 2).
- Every scenario with a run verdict of `"no_output"` is listed, stable or not.
- **Coverage.** A ruled kind-2 metric must have a verdict for exactly the scenarios in
  `keys.gold_sha256`, in the baseline and in the run. A scenario the gold has and a file leaves
  out, or one a file has and the gold does not, is an error for that file
  (`scenario_coverage_differs`).
  - Why the gold: comparing the two files only with each other would let both leave out the same
    scenario, and it would then never be looked at.
  - The check runs after the keys match, so the baseline's gold and the run's are the same set. A
    run whose gold differs is `no_baseline`, as before, even if its verdicts do not cover its
    own gold.
  - **Each file is checked wherever the metric is there in the right shape, whatever the other
    file holds** (part 4; Codex P3, PR #7). If the metric is missing from the run and the
    baseline leaves out a scenario, the result names both: `metric_missing` for the run and
    `scenario_coverage_differs` for the baseline. Before part 4 it named only the first.
  - A rule cannot name a subset of the scenarios. That would be a new field in the rules file,
    and so a new schema version.
  - A verdict metric the rules do not name is not checked for coverage (see "Metrics the rules do
    not name").
- The run must also have a verdict for exactly the baseline's scenarios
  (`scenario_set_differs`). With the coverage check this is never the only error: two files that
  both cover the gold agree with each other. It is checked only when the metric is usable in
  both files.

**Kind 3, noisy counts.** A breach is a count below `min - margin` or above `max + margin`, where
`min` and `max` are the lowest and highest baseline counts. A count exactly on either boundary is
not a breach. The margin is an absolute number, not a percentage. The lower boundary can be below
zero, in which case it never fires.

A family and kind is a breach when any of its metrics is.

## The result (`ws7-result/1`)

| Field | Type | Note |
|---|---|---|
| `schema` | string | `"ws7-result/1"` |
| `status` | `"no_breach"`, `"breach"`, `"no_baseline"` or `"error"` | |
| `exit_code` | `0`, `1` or `2` | The same as the process exit code |
| `baseline_id`, `run_id` | ID string or null | Null when that file did not pass its checks |
| `inputs` | object: `baseline_sha256`, `rules_sha256`, `run_sha256` | The sha256 of each file's bytes. See "When an input's hash is null" below. No file paths |
| `key_mismatch` | sorted list of key names | The top-level keys that differ. Empty unless `no_baseline` |
| `errors` | list of `{code, file, field, metric}` | See below. Empty unless `error` |
| `comparisons` | list of rows, sorted by family then kind | One row for each family and kind the rules name. Empty for `no_baseline` and `error` |
| row | `family`, `kind`, `breach` (0 or 1), `metrics` | `metrics` is sorted by metric name |
| kind 1 metric | `metric`, `breach`, `run`, `baseline_max` | |
| kind 2 metric | `metric`, `breach`, `flips`, `no_output`, `min_flips`, `stable`, `unstable` | `flips` and `no_output` are sorted scenario IDs; `stable` is a count; `unstable` is a list of `{scenario, baseline_passes}` |
| kind 3 metric | `metric`, `breach`, `direction`, `run`, `low`, `high` | `direction` is `"below"`, `"inside"` or `"above"`; `low` is `min - margin`; `high` is `max + margin` |
| `unruled_metrics` | sorted list of `{metric, in_baseline, in_run}` | Empty for `no_baseline` and `error` |

- The result holds IDs, counts and fixed labels only. It has no text, no file path and no
  timestamp.
- The same three input files always give the same bytes: sorted keys, sorted lists, two-space
  indentation and one trailing newline, whatever `PYTHONHASHSEED` is.
- Scenario IDs are sorted as strings, so `S10` comes before `S2`.

**When an input's hash is null.** In exactly two cases:

- that file could not be read at all: it is missing, or it is not a readable file. Its error is
  `file_unreadable`, and the other two files keep their hashes;
- the comparator met an unexpected exception (`internal_error`). All three are then null.

A file that was read keeps its hash whatever it holds. That includes a file that is not valid
JSON (`json_invalid`) and one that fails its shape check (`shape_invalid`), so the result always
says which bytes were refused.

The result of the three example files above (exit code 1). It is printed compactly here; the file
itself has one value per line.

```json
{
  "schema": "ws7-result/1",
  "status": "breach",
  "exit_code": 1,
  "baseline_id": "b-2026-12-14",
  "run_id": "2026-12-16T0400",
  "inputs": {"baseline_sha256": "79c6…64", "rules_sha256": "99b9…ae", "run_sha256": "cdc4…05"},
  "key_mismatch": [],
  "errors": [],
  "comparisons": [
    {"family": "assembly", "kind": 3, "breach": 1, "metrics": [
      {"metric": "placeholders_left", "breach": 1, "direction": "above", "run": 10,
       "low": 2, "high": 9}]},
    {"family": "medications", "kind": 1, "breach": 0, "metrics": [
      {"metric": "invented_doses", "breach": 0, "run": 1, "baseline_max": 1}]},
    {"family": "safety_netting", "kind": 2, "breach": 1, "metrics": [
      {"metric": "safety_net_verdict", "breach": 1, "flips": ["S1", "S3"], "no_output": ["S3"],
       "min_flips": 1, "stable": 2, "unstable": [{"scenario": "S2", "baseline_passes": 2}]}]}
  ],
  "unruled_metrics": [{"metric": "tokens_out_total", "in_baseline": false, "in_run": true}]
}
```

Reading it:

- `placeholders_left` is 10. The baseline range is 4 to 7, widened by 2 to 2 to 9. Ten is above.
- `invented_doses` is 1 and the baseline maximum is 1. Equal is not a breach.
- `S1` was stable and now fails. `S3` was stable and produced no output. Both are flips. `S2`
  also fails, but it was unstable at baseline, so it is not a flip.

## Metrics the rules do not name

A metric that is in the baseline or the run but not in the rules is listed in `unruled_metrics`.
It does not change the status or the exit code, and it is never dropped silently. It must still
be well-formed. An unruled verdict metric is not checked for coverage: it may have verdicts for
some of the gold's scenarios only.

A misspelt name in the rules file is caught the other way round: the ruled name is then missing
from the run, which is an error.

## Error codes

Each error has a fixed `code`, the `file` it is about (`"baseline"`, `"rules"`, `"run"` or null),
a fixed `field` label written in the code (or null), and a `metric` name (or null) that has
already passed the ID pattern. Nothing else from an input file is copied.

| Code | Meaning |
|---|---|
| `file_unreadable` | The file is missing or cannot be read |
| `json_invalid` | Not valid UTF-8 JSON, a duplicate key, or `NaN`/`Infinity` |
| `shape_invalid` | A missing, unknown or wrongly typed field; `field` says where |
| `unstable_list_inconsistent` | The baseline's `unstable` list disagrees with its verdicts |
| `target_mismatch` | The rules file is for the other target |
| `metric_missing` | A ruled metric is not in the baseline or not in the run |
| `metric_kind_mismatch` | A ruled metric has the wrong shape for its kind: counts for kind 2, or verdicts for kind 1 or 3 |
| `scenario_coverage_differs` | A ruled kind-2 metric's scenarios, in the file named, are not exactly the scenarios in `keys.gold_sha256` |
| `scenario_set_differs` | A kind-2 metric's scenarios differ between the baseline and the run |
| `internal_error` | An unexpected exception |

## Units for later metrics

Counts and margins are integers, so a metric that is not a count needs an integer unit before it
can be ruled:

- **Cost** is integer micro-dollars, in a metric named `cost_usd_micros`. One dollar is 1,000,000.
- **Times** are integer milliseconds, in metrics whose names end `_ms`.

A float in any metric is an error today.

## What it does not do

- It does not decide which baseline a run should use. It is given one of each file.
- It does not publish a metric, send an alert or read S3. The entrypoint does that (slice 2).
- It does not score anything, and it never reads a batch record.
- It does not build a run summary or a baseline. `evals/ws7_summary.py` builds the summary and a
  candidate baseline; the author freezes a baseline by pull request.
- Subgroup tags (ADR-011 D12) are not in the run summary yet. Adding them will be a new schema
  version.

## Tests

```zsh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q tests/test_ws7_compare.py
```

`tests/test_ws7_compare.py` has invented fixtures only, written in the test file. Every breach
test has a matching non-breach test at its boundary. Every result a test produces is also checked,
field by field, to hold only IDs, counts and fixed labels.

Added with slice 2 part 3 (the follow-ups from the review of PR #4):

- a readable file that is not valid JSON keeps its hash, and an unreadable one has null while
  the other two keep theirs;
- every v1 key that differs gives `no_baseline` naming that key, as every pipeline key did;
- a ruled kind-2 metric that does not cover the gold's scenarios is an error, with the case the
  older check could not see: the baseline and the run leaving out the same scenario.

Added with slice 2 part 4 (the two P3s from the review of PR #7):

- a ruled kind-2 metric that is missing from one file, or has the wrong shape there, and a
  coverage gap in the other file: both errors are named. Each case has a partner in which the
  other file covers the gold, and the result is the one error;
- the test that coverage is checked only after the keys match is stronger: the run's gold keeps
  a scenario that its verdicts leave out, and the result is still `no_baseline` with no errors. A
  mutation that moves the coverage check before the key check passed the older test and fails
  this one;
- `check_run` and `check_baseline` are public, and each refuses an unknown field.

`tests/test_ws7_summary.py` runs the real comparator on files that `evals/ws7_summary.py` built
(`docs/WS7-ENTRYPOINT.md`, "Tests").
