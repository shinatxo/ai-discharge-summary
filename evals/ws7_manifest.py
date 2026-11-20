#!/usr/bin/env python3
"""WS7 match check and run manifest (ADR-011 D11).

THE MATCH CHECK runs first and costs nothing. The job works out its own values,
reads the expected ones from SSM Parameter Store, and does not generate if any
differs.

    pipeline   /discharge-eval/ws7/expected/pipeline
               pipeline_version, model_id, prompt_caching
    v1         /discharge-eval/ws7/expected/v1
               model_id, the sha256 of the two prompt files, patient_second_pass
               and prompt_caching, both of which must be true

Each parameter is one String holding strict JSON (`ws7-expected/1`). The author
writes them by hand; the task can read them and cannot write them.

Where the job's own values come from, so that they are what the runner will
really do and not a second copy:

- the model ID: the plan's (evals/ws7_reserve.py), which is the runner's own;
- `pipeline_version`: `pipeline.orchestrate.pipeline_version()`, called, not
  changed;
- the prompt hashes: the sha256 of the bytes of the two files the v1 runner
  will load, as its own record's `file_sha256` is;
- the caching setting and the second pass: the runner's own `parse_args` on the
  argv the entrypoint will hand it.

THE RUNNER'S ARGV IS AN ALLOW-LIST. Before a runner's parser sees an argv it must
be exactly one of the forms `runner_argv()` builds, which are the forms the
entrypoint passes (part 5):

    pipeline   <--all | the pre-flight scenario> --batch <id> --yes
    v1         <--all | the pre-flight scenario> --deployed-path --batch <id> --yes

Anything else is `ws7_runner_argv_invalid`, and nothing is printed: v1's
`--no-gate`, the pipeline's `--verify`, `--dry-run`, `--runs`, another prompt
or corpus, another scenario, another order. So a setting the match check does
not compare cannot be changed by the argv either. `runner_argv()` checks the
config's pre-flight scenario again with the config loader's own pattern, so a
Config built in code cannot put `--all` where one scenario goes. For v1 the
safety-net gate is on as a WS7 rule: it is in the job's own values and in the
manifest, and it is not an SSM value.

The limit is ADR-011's: this proves the image agrees with the release record,
not that the running Lambda does.

THE RUN MANIFEST (`ws7-manifest/1`) is written once, with `If-None-Match: *`,
under the run's own prefix. Its `keys` object is the comparator's, checked by
the comparator's own code (`ws7_compare.check_keys`), so a manifest's keys can
be compared with a baseline's as they stand. A v1 manifest's
`patient_second_pass` and `prompt_caching` must both be true.

THE COMMIT AND THE IMAGE DIGEST. The image has no `.git`, and a container cannot
hash its own image. Both are handed in as environment variables, and this
module reads them only from the mapping it is given:

- WS7_GIT_COMMIT: the 40-character commit the image was built from, set by a
  build argument in the Dockerfile (slice 2 part 6).
- WS7_IMAGE_DIGEST: `sha256:` and 64 hex characters, set in the ECS task
  definition beside the image reference, which pins the image by the same
  digest (slice 3). It is the deployer's statement; slice 3's set-up check
  compares the two.

A run with either missing or malformed is refused (`ws7_identity_missing`).
No metadata endpoint is asked.

No AWS call is made by importing this module.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

_EVALS = Path(__file__).resolve().parent
for _p in (str(_EVALS.parent / "src" / "generate"), str(_EVALS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ws7_compare as compare  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
import ws7_store as store  # noqa: E402
from ws7_ledger import Claim  # noqa: E402
from ws7_store import Stop  # noqa: E402

EXPECTED_SCHEMA = "ws7-expected/1"
MANIFEST_SCHEMA = "ws7-manifest/1"
EXPECTED_PARAMETER = {"pipeline": "/discharge-eval/ws7/expected/pipeline",
                      "v1": "/discharge-eval/ws7/expected/v1"}
MANIFEST_NAME = "manifest.json"
COMMIT_VARIABLE, DIGEST_VARIABLE = "WS7_GIT_COMMIT", "WS7_IMAGE_DIGEST"
# How PART C was produced (D11, D13): a fixed label per target.
PART_C = {"pipeline": "pipeline_step_7", "v1": "v1_second_pass"}

RUNNER_ARGV_INVALID = "ws7_runner_argv_invalid"
EXPECTED_UNREADABLE = "ws7_expected_unreadable"
EXPECTED_INVALID = "ws7_expected_invalid"
MATCH_DIFFERS = "ws7_match_differs"
IDENTITY_MISSING = "ws7_identity_missing"
MANIFEST_INVALID = "ws7_manifest_invalid"
MANIFEST_EXISTS = "ws7_manifest_exists"
MANIFEST_UNAVAILABLE = "ws7_manifest_unavailable"
CODES = (RUNNER_ARGV_INVALID, EXPECTED_UNREADABLE, EXPECTED_INVALID, MATCH_DIFFERS,
         IDENTITY_MISSING, MANIFEST_INVALID, MANIFEST_EXISTS, MANIFEST_UNAVAILABLE)

_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_MODEL_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
# A batch ID never starts with a dash, so it can never be read as an option.
_BATCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_V1_PROMPTS = ("system_prompt", "patient_system_prompt")
# What the match check compares with SSM, per target.
_FIELDS = {"pipeline": ("target", "model_id", "prompt_caching", "pipeline_version"),
           "v1": ("target", "model_id", "prompt_caching", "v1_prompt_sha256",
                  "patient_second_pass")}
GATE = "safety_net_gate"      # v1 only: a WS7 rule, in the job's own values, not in SSM
_MANIFEST_FIELDS = ("schema", "run_id", "target", "mode", "scheduled_time", "month", "git_commit",
                    "image_digest", "config_sha256", "client", "keys", "reservation", GATE,
                    "month_assurance_waived", "baseline_id", "part_c")


def _hex64(value: object) -> bool:
    return type(value) is str and _HEX64_RE.fullmatch(value) is not None


def _values_ok(values: object, target: str) -> bool:
    """The shape both the job's own values and the expected ones must have."""
    if type(values) is not dict or set(values) != set(_FIELDS[target]) \
            or values["target"] != target:
        return False
    if type(values["model_id"]) is not str or not _MODEL_RE.fullmatch(values["model_id"]) \
            or type(values["prompt_caching"]) is not bool:
        return False
    if target == "pipeline":
        return _hex64(values["pipeline_version"])
    prompts = values["v1_prompt_sha256"]
    return (type(prompts) is dict and set(prompts) == set(_V1_PROMPTS)
            and all(_hex64(v) for v in prompts.values())
            and type(values["patient_second_pass"]) is bool)


# -------------------------------------------------------- the job's own values

def runner_argv(target: str, cfg: reserve.Config, *, preflight: bool, batch: str) -> list:
    """The argv the entrypoint hands a runner: the only forms `own_values` accepts.
    `preflight` picks the one pre-flight scenario from the config; otherwise all 18."""
    if target not in store.TARGETS or type(preflight) is not bool \
            or type(batch) is not str or not _BATCH_RE.fullmatch(batch):
        raise Stop(RUNNER_ARGV_INVALID)
    # The config loader's own rule, checked again here: a Config built in code never
    # went through the loader, and a pre-flight "scenario" of --all would run all 18.
    # Checked for both forms, so such a config builds no argv at all.
    scenario = getattr(cfg, "preflight_scenario", None)
    if type(scenario) is not str or not reserve._SCENARIO_RE.fullmatch(scenario):
        raise Stop(RUNNER_ARGV_INVALID)
    scenarios = scenario if preflight else "--all"
    return [scenarios, *(["--deployed-path"] if target == "v1" else []), "--batch", batch, "--yes"]


def _allowed(target: str, argv: object, cfg: reserve.Config) -> bool:
    """Is `argv` exactly a form `runner_argv` builds for this target?"""
    if type(argv) is not list or not all(type(a) is str for a in argv) or len(argv) < 3:
        return False
    batch = argv[-2]
    if not _BATCH_RE.fullmatch(batch):
        return False
    return any(argv == runner_argv(target, cfg, preflight=preflight, batch=batch)
               for preflight in (True, False))


def own_values(target: str, argv: list, cfg: reserve.Config) -> dict:
    """What this image would run, read from the runner and the pipeline themselves.
    `argv` is checked against the allow-list before the runner's parser sees it."""
    if target not in store.TARGETS or not _allowed(target, argv, cfg):
        raise Stop(RUNNER_ARGV_INVALID)
    runner = reserve.rpe if target == "pipeline" else reserve.rce
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            args = runner.parse_args(list(argv))
    except SystemExit:                      # argparse's way of refusing an argv
        raise Stop(RUNNER_ARGV_INVALID) from None
    # What the allow-list implies, checked on what the parser made of it.
    if args.runs != 1 or args.dry_run or (target == "pipeline" and args.verify) \
            or (target == "v1" and args.gate is not True):
        raise Stop(RUNNER_ARGV_INVALID)
    values = {"target": target, "model_id": reserve.plan(target).model_id,
              "prompt_caching": args.prompt_caching}
    if target == "pipeline":
        from pipeline import orchestrate
        values["pipeline_version"] = orchestrate.pipeline_version()
    else:
        try:
            values["v1_prompt_sha256"] = {
                "system_prompt": reserve.rce.sha256_bytes(Path(args.prompt).read_bytes()),
                "patient_system_prompt": reserve.rce.sha256_bytes(
                    Path(args.patient_prompt).read_bytes())}
        except OSError:
            raise Stop(RUNNER_ARGV_INVALID) from None
        values["patient_second_pass"] = args.patient_second_pass
        values[GATE] = args.gate
    if not _own_ok(values, target):
        raise Stop(RUNNER_ARGV_INVALID)
    return values


def _own_ok(values: object, target: str) -> bool:
    """The job's own values: the compared fields, and for v1 the gate, on."""
    if type(values) is not dict:
        return False
    compared = {k: v for k, v in values.items() if k != GATE}
    gate_ok = (values.get(GATE) is True) if target == "v1" else GATE not in values
    return gate_ok and _values_ok(compared, target)


def _compared(own: dict, target: str) -> dict:
    return {name: own[name] for name in _FIELDS[target]}


# ---------------------------------------------------------- the expected values

def _no_duplicate_keys(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def parse_expected(text: object, target: str) -> dict:
    """One parameter's value, strictly. For v1 the second pass and caching must both
    be on (D11): an expected record that says otherwise is refused, not followed."""
    bad = Stop(EXPECTED_INVALID, ids=(target,))
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys) if type(text) is str else None
    except (ValueError, RecursionError):
        raise bad from None
    if type(data) is not dict or data.pop("schema", None) != EXPECTED_SCHEMA \
            or not _values_ok(data, target):
        raise bad
    if target == "v1" and not (data["patient_second_pass"] is True and data["prompt_caching"] is True):
        raise bad
    return data


def read_expected(ssm, target: str) -> dict:
    """The expected values for `target`, from SSM Parameter Store."""
    if target not in EXPECTED_PARAMETER:
        raise Stop(EXPECTED_UNREADABLE)
    try:
        reply = ssm.get_parameter(Name=EXPECTED_PARAMETER[target])
    except (ClientError, BotoCoreError):
        raise Stop(EXPECTED_UNREADABLE, ids=(target,)) from None
    parameter = reply.get("Parameter") if type(reply) is dict else None
    if type(parameter) is not dict or parameter.get("Type") != "String":
        raise Stop(EXPECTED_INVALID, ids=(target,))
    return parse_expected(parameter.get("Value"), target)


def _flat(values: dict) -> dict:
    out = {}
    for name, value in values.items():
        if type(value) is dict:
            out.update({f"{name}.{inner}": v for inner, v in value.items()})
        else:
            out[name] = value
    return out


def differing(own: dict, expected: dict) -> tuple:
    """The names of the fields that differ. Names only, never the values."""
    mine, theirs = _flat(own), _flat(expected)
    return tuple(sorted(name for name in set(mine) | set(theirs)
                        if name not in mine or name not in theirs
                        or type(mine[name]) is not type(theirs[name]) or mine[name] != theirs[name]))


def match_check(ssm, target: str, argv: list, cfg: reserve.Config) -> dict:
    """The job's own values, if they are the expected ones. Otherwise the run is
    refused, before any claim, reservation or model call."""
    own = own_values(target, argv, cfg)
    fields = differing(_compared(own, target), read_expected(ssm, target))
    if fields:
        raise Stop(MATCH_DIFFERS, ids=fields, counts={"fields": len(fields)})
    return own


# ----------------------------------------------------------------- identity

@dataclass(frozen=True)
class Identity:
    git_commit: str
    image_digest: str


def identity(environ) -> Identity:
    """The commit and the image digest, from the mapping given (the entrypoint
    passes its environment; a test passes a dict). Either missing or malformed
    refuses the run."""
    commit, digest = environ.get(COMMIT_VARIABLE), environ.get(DIGEST_VARIABLE)
    if type(commit) is not str or not _HEX40_RE.fullmatch(commit) \
            or type(digest) is not str or not _DIGEST_RE.fullmatch(digest):
        raise Stop(IDENTITY_MISSING)
    return Identity(commit, digest)


# ----------------------------------------------------------------- manifest

def validate_manifest(manifest: object) -> dict:
    """The whole manifest, strictly: exactly the fields, each in shape, and each
    consistent with the others. `build_manifest` ends with it, and `write_manifest`
    runs it on the exact payload immediately before the conditional PUT."""
    bad = Stop(MANIFEST_INVALID)
    m = manifest
    if type(m) is not dict or set(m) != set(_MANIFEST_FIELDS) or m["schema"] != MANIFEST_SCHEMA:
        raise bad
    target, mode = m["target"], m["mode"]
    if type(target) is not str or target not in store.TARGETS \
            or type(mode) is not str or mode not in store.MODES:
        raise bad
    scheduled = store.parse_utc(m["scheduled_time"])
    if scheduled is None or m["run_id"] != store.run_id(target, mode, scheduled) \
            or m["month"] != m["scheduled_time"][:7]:
        raise bad
    if type(m["git_commit"]) is not str or not _HEX40_RE.fullmatch(m["git_commit"]) \
            or type(m["image_digest"]) is not str or not _DIGEST_RE.fullmatch(m["image_digest"]) \
            or not _hex64(m["config_sha256"]):
        raise bad
    plan = reserve.plan(target)
    client = m["client"]
    if type(client) is not dict or set(client) != {"total_max_attempts", "connect_timeout_s",
                                                    "read_timeout_s"} \
            or type(client["total_max_attempts"]) is not int or client["total_max_attempts"] != 1 \
            or client["connect_timeout_s"] != plan.connect_timeout_s \
            or client["read_timeout_s"] != plan.read_timeout_s:
        raise bad
    keys = m["keys"]
    try:
        compare.check_keys(keys, "manifest")        # the comparator's own check of a `keys` object
    except (compare.CompareError, TypeError):
        raise bad from None
    if keys["target"] != target:
        raise bad
    if target == "v1" and not (keys["patient_second_pass"] is True
                               and keys["prompt_caching"] is True):
        raise bad                                   # v1 runs with the second pass and caching on (D11)
    booked = m["reservation"]
    if type(booked) is not dict or set(booked) != {"generations", "margin_percent",
                                                    "total_usd_micros"} \
            or not all(type(v) is int and v > 0 for v in booked.values()):
        raise bad
    if m[GATE] is not (True if target == "v1" else None):
        raise bad                                   # v1: the gate on; the pipeline has no such switch
    if type(m["month_assurance_waived"]) is not bool or m["part_c"] != PART_C[target]:
        raise bad
    if m["baseline_id"] is not None and not (type(m["baseline_id"]) is str
                                             and store.ID_RE.fullmatch(m["baseline_id"])):
        raise bad
    return m


def build_keys(own: object, *, corpus_sha256: object, gold_sha256: object,
               scorer_versions: object) -> dict:
    """A run's `keys` object (the baseline key of ADR-011 D9): the job's own compared
    values, with the corpus hash, the gold hashes and the scorer versions.

    One function for the run manifest and the run summary (evals/ws7_summary.py), so
    the two cannot be built differently. The result is a copy: it never aliases an
    argument. It has passed the comparator's own check (`ws7_compare.check_keys`).
    Anything out of shape is `ws7_manifest_invalid`.

    THE ORIGINAL OBJECTS ARE CHECKED BEFORE THEY ARE COPIED (Codex P3, PR #8). The copy
    is a JSON round trip, and JSON has only string keys: an integer scenario key `1`
    would come back as `"1"` and pass. So the check runs on what was given, and the
    copy is made only of what passed."""
    bad = Stop(MANIFEST_INVALID)
    target = own.get("target") if type(own) is dict else None
    if type(target) is not str or target not in store.TARGETS or not _own_ok(own, target):
        raise bad
    try:
        given = dict(_compared(own, target), corpus_sha256=corpus_sha256, gold_sha256=gold_sha256,
                     scorer_versions=scorer_versions)
        compare.check_keys(given, "manifest")               # as given: before any copy
        return compare.check_keys(json.loads(json.dumps(given)), "manifest")
    except (compare.CompareError, TypeError, ValueError):
        raise bad from None


def build_manifest(*, held: Claim, own: dict, who: Identity, cfg: reserve.Config,
                   reservation: dict, corpus_sha256: str, gold_sha256: dict,
                   scorer_versions: dict, baseline_id: str | None,
                   month_assurance_waived: bool) -> dict:
    """The run manifest of D11. `own` is what `match_check` returned; the corpus
    hash, the gold hashes and the scorer versions come from the finished batch
    and its scores (parts 4 and 5); `baseline_id` is None when no baseline was
    compared."""
    bad = Stop(MANIFEST_INVALID)
    if not isinstance(held, Claim) or not isinstance(who, Identity) \
            or not isinstance(cfg, reserve.Config) \
            or held.target not in store.TARGETS or not _own_ok(own, held.target):
        raise bad
    target = held.target
    if type(reservation) is not dict or reservation.get("target") != target:
        raise bad
    keys = build_keys(own, corpus_sha256=corpus_sha256, gold_sha256=gold_sha256,
                      scorer_versions=scorer_versions)
    plan = reserve.plan(target)
    return validate_manifest({
        "schema": MANIFEST_SCHEMA,
        "run_id": held.run_id, "target": target, "mode": held.mode,
        "scheduled_time": held.scheduled_time, "month": held.month,
        "git_commit": who.git_commit, "image_digest": who.image_digest,
        "config_sha256": cfg.sha256,
        # One attempt per call. v1's read timeout is fixed by WS7; the pipeline runner
        # names one for each client it asks for, so there is none to record here.
        "client": {"total_max_attempts": 1, "connect_timeout_s": plan.connect_timeout_s,
                   "read_timeout_s": plan.read_timeout_s},
        "keys": keys,
        "reservation": {name: reservation.get(name) for name in ("generations", "margin_percent",
                                                                 "total_usd_micros")},
        GATE: own.get(GATE),
        "month_assurance_waived": month_assurance_waived,
        "baseline_id": baseline_id,
        "part_c": PART_C[target],
    })


def manifest_key(manifest: dict) -> str:
    return store.run_prefix(manifest["mode"], manifest["run_id"]) + MANIFEST_NAME


def write_manifest(st: store.Store, manifest: dict, *, attempts: int) -> str:
    """Write the manifest once. A manifest already there is never replaced."""
    try:
        body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        # The exact payload, checked in full immediately before the conditional PUT.
        key = manifest_key(validate_manifest(json.loads(body.decode("utf-8"))))
    except (KeyError, TypeError, ValueError):
        raise Stop(MANIFEST_INVALID) from None
    try:
        outcome = st.create(key, body, attempts=attempts)
    except store.StoreError as exc:
        raise Stop(MANIFEST_UNAVAILABLE, ids=(exc.code,)) from None
    if outcome != store.CREATED:
        raise Stop(MANIFEST_EXISTS, ids=(manifest["run_id"],))
    return key
