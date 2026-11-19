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

The limit is ADR-011's: this proves the image agrees with the release record,
not that the running Lambda does.

THE RUN MANIFEST (`ws7-manifest/1`) is written once, with `If-None-Match: *`,
under the run's own prefix. Its `keys` object is the comparator's, checked by
the comparator's own code, so a manifest's keys can be compared with a
baseline's as they stand.

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
_V1_PROMPTS = ("system_prompt", "patient_system_prompt")
_FIELDS = {"pipeline": ("target", "model_id", "prompt_caching", "pipeline_version"),
           "v1": ("target", "model_id", "prompt_caching", "v1_prompt_sha256",
                  "patient_second_pass")}


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

def own_values(target: str, runner_argv: list) -> dict:
    """What this image would run, read from the runner and the pipeline themselves."""
    if target not in store.TARGETS:
        raise Stop(RUNNER_ARGV_INVALID)
    runner = reserve.rpe if target == "pipeline" else reserve.rce
    try:
        args = runner.parse_args([str(a) for a in runner_argv])
    except SystemExit:                      # argparse's way of refusing an argv
        raise Stop(RUNNER_ARGV_INVALID) from None
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
    if not _values_ok(values, target):
        raise Stop(RUNNER_ARGV_INVALID)
    return values


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


def match_check(ssm, target: str, runner_argv: list) -> dict:
    """The job's own values, if they are the expected ones. Otherwise the run is
    refused, before any claim, reservation or model call."""
    own = own_values(target, runner_argv)
    fields = differing(own, read_expected(ssm, target))
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
            or held.target not in store.TARGETS or not _values_ok(own, held.target):
        raise bad
    target = held.target
    if target == "v1" and not own["patient_second_pass"]:
        raise bad                                   # PART C would not be the second pass's
    if baseline_id is not None and not (type(baseline_id) is str
                                        and store.ID_RE.fullmatch(baseline_id)):
        raise bad
    if type(month_assurance_waived) is not bool or type(reservation) is not dict:
        raise bad
    booked = {name: reservation.get(name) for name in ("generations", "margin_percent",
                                                       "total_usd_micros")}
    if reservation.get("target") != target \
            or not all(type(v) is int and v > 0 for v in booked.values()):
        raise bad

    keys = json.loads(json.dumps(own))              # a copy: the manifest never aliases `own`
    keys.update(corpus_sha256=corpus_sha256, gold_sha256=gold_sha256,
                scorer_versions=scorer_versions)
    try:
        compare._check_keys(keys, "manifest")       # the comparator's own check of a `keys` object
    except compare.CompareError:
        raise bad from None

    plan = reserve.plan(target)
    return {
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
        "reservation": booked,
        "month_assurance_waived": month_assurance_waived,
        "baseline_id": baseline_id,
        "part_c": PART_C[target],
    }


def manifest_key(manifest: dict) -> str:
    return store.run_prefix(manifest["mode"], manifest["run_id"]) + MANIFEST_NAME


def write_manifest(st: store.Store, manifest: dict, *, attempts: int) -> str:
    """Write the manifest once. A manifest already there is never replaced."""
    if type(manifest) is not dict or manifest.get("schema") != MANIFEST_SCHEMA:
        raise Stop(MANIFEST_INVALID)
    try:
        key = manifest_key(manifest)
        body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (KeyError, TypeError, ValueError):
        raise Stop(MANIFEST_INVALID) from None
    try:
        outcome = st.create(key, body, attempts=attempts)
    except store.StoreError as exc:
        raise Stop(MANIFEST_UNAVAILABLE, ids=(exc.code,)) from None
    if outcome != store.CREATED:
        raise Stop(MANIFEST_EXISTS, ids=(manifest["run_id"],))
    return key
