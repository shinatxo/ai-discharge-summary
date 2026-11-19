"""evals/ws7_manifest.py - the match check and the run manifest (ADR-011 D11; slice 2
part 2).

Real botocore SSM and S3 clients, against strict fakes on 127.0.0.1
(tests/ws7_fakes.py). `offline_aws` applies to every test in this file, so none
can reach a real AWS endpoint or a metadata endpoint. No runner is run: only
each runner's own argument parser is called.

Every hash, commit, digest and expected value is invented or computed from the
repository's own files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "evals"), str(ROOT / "tests"), str(ROOT / "src" / "generate")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_cold_eval as rce  # noqa: E402
import ws7_compare as compare  # noqa: E402
import ws7_fakes as fakes  # noqa: E402
import ws7_ledger as wl  # noqa: E402
import ws7_manifest as wm  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
import ws7_store as store  # noqa: E402
import ws7_stub as stubs  # noqa: E402
from pipeline import orchestrate  # noqa: E402
from ws7_stub import offline_aws  # noqa: E402,F401  (a fixture)

CFG = reserve.load_config()
MARGIN = CFG.input_allowance_margin_percent
ARGV = {"pipeline": ["--all", "--yes"], "v1": ["--all", "--deployed-path", "--yes"]}
SENTINEL = "WS7-MANIFEST-SENTINEL text that must never be carried"
COMMIT = "0123456789abcdef0123456789abcdef01234567"           # invented
DIGEST = "sha256:" + "ab" * 32                                 # invented
ENVIRON = {wm.COMMIT_VARIABLE: COMMIT, wm.DIGEST_VARIABLE: DIGEST}
OTHER_HASH = "9" * 64
BATCH = {"corpus_sha256": "a" * 64, "gold_sha256": {"S1": "b" * 64, "S2": "c" * 64},
         "scorer_versions": {"score_batch": 2, "haz03_check": 2}}       # invented


@pytest.fixture(autouse=True)
def _offline(offline_aws):  # noqa: F811
    return offline_aws


@pytest.fixture
def ssm(_offline):
    fake = stubs.started(fakes.SsmFake(), _offline)
    fake.client = store.build_client("ssm", endpoint_url=fake.url)
    yield fake
    fake.stop()


@pytest.fixture
def s3(_offline):
    fake = stubs.started(fakes.S3Fake(), _offline)
    fake.store = store.Store(store.build_client("s3", endpoint_url=fake.url), fakes.BUCKET)
    yield fake
    fake.stop()


def expect(ssm, target: str, **change) -> dict:
    """Write the expected record for `target`: the job's own values, changed."""
    record = dict(wm.own_values(target, ARGV[target]), schema=wm.EXPECTED_SCHEMA)
    record.update(change)
    ssm.put(wm.EXPECTED_PARAMETER[target], json.dumps(record))
    return record


def stopped(call, expect_code: str) -> store.Stop:
    with pytest.raises(store.Stop) as exc:
        call()
    assert exc.value.code in wm.CODES and exc.value.code == expect_code, exc.value.line()
    return exc.value


def held(target: str = "pipeline", mode: str = "scheduled") -> wl.Claim:
    from datetime import datetime, timezone
    when = datetime(2026, 12, 16, 4, 0, tzinfo=timezone.utc)                # invented occurrence
    return wl.Claim(target, mode, "2026-12-16T04:00:00Z", store.run_id(target, mode, when),
                    "c" * 32, "2026-12-16T04:00:37Z", store.claim_key(target, mode, when))


def manifest(target: str = "pipeline", mode: str = "scheduled", **change) -> dict:
    kwargs = dict(held=held(target, mode), own=wm.own_values(target, ARGV[target]),
                  who=wm.identity(ENVIRON), cfg=CFG,
                  reservation=reserve.reservation(target, 19, MARGIN), baseline_id="b-2026-12-14",
                  month_assurance_waived=False, **BATCH)
    kwargs.update(change)
    return wm.build_manifest(**kwargs)


# ======================================================= the job's own values ==

def test_the_pipelines_own_values_come_from_the_pipeline_and_the_runners_parser():
    own = wm.own_values("pipeline", ARGV["pipeline"])
    assert own == {"target": "pipeline", "model_id": "anthropic.claude-sonnet-4-6",
                   "prompt_caching": True, "pipeline_version": orchestrate.pipeline_version()}
    assert wm.own_values("pipeline", ["--all", "--no-prompt-caching"])["prompt_caching"] is False


def test_v1s_own_values_include_the_second_pass_and_the_caching_setting():
    own = wm.own_values("v1", ARGV["v1"])
    assert own == {
        "target": "v1", "model_id": "anthropic.claude-sonnet-4-6",
        "prompt_caching": True, "patient_second_pass": True,
        "v1_prompt_sha256": {
            "system_prompt": hashlib.sha256(rce.DEFAULT_PROMPT_PATH.read_bytes()).hexdigest(),
            "patient_system_prompt": hashlib.sha256(
                rce.DEFAULT_PATIENT_PROMPT_PATH.read_bytes()).hexdigest()}}
    # Read from the runner's own parser, so each flag follows what the runner would do.
    plain = wm.own_values("v1", ["--all"])
    assert (plain["patient_second_pass"], plain["prompt_caching"]) == (False, False)
    second_only = wm.own_values("v1", ["--all", "--patient-second-pass"])
    assert (second_only["patient_second_pass"], second_only["prompt_caching"]) == (True, False)


def test_the_prompt_hashes_are_of_the_files_the_runner_would_load(tmp_path):
    other = tmp_path / "other_prompt.md"
    other.write_bytes(b"## SYSTEM PROMPT\ninvented prompt\n")
    own = wm.own_values("v1", ["--all", "--deployed-path", "--prompt", str(other)])
    assert own["v1_prompt_sha256"]["system_prompt"] == hashlib.sha256(other.read_bytes()).hexdigest()
    stopped(lambda: wm.own_values("v1", ["--all", "--prompt", str(tmp_path / "missing.md")]),
            wm.RUNNER_ARGV_INVALID)


@pytest.mark.parametrize("target,argv", [("pipeline", ["--no-such-flag"]), ("v1", ["--runs", "0"]),
                                         ("canary", ["--all"])])
def test_an_argv_the_runner_would_refuse_is_refused(capsys, target, argv):
    stopped(lambda: wm.own_values(target, argv), wm.RUNNER_ARGV_INVALID)
    capsys.readouterr()                                                   # argparse's usage text


# ============================================================ the match check ==

@pytest.mark.parametrize("target", ["pipeline", "v1"])
def test_the_match_check_passes_when_every_value_is_the_expected_one(ssm, target):
    expect(ssm, target)
    assert wm.match_check(ssm.client, target, ARGV[target]) == wm.own_values(target, ARGV[target])
    assert ssm.requests == [f"/discharge-eval/ws7/expected/{target}"]     # one read, of that name


@pytest.mark.parametrize("target,change,field", [
    ("pipeline", {"pipeline_version": OTHER_HASH}, "pipeline_version"),
    ("pipeline", {"model_id": "anthropic.claude-invented-9"}, "model_id"),
    ("pipeline", {"prompt_caching": False}, "prompt_caching"),
    ("v1", {"model_id": "anthropic.claude-invented-9"}, "model_id"),
    ("v1", {"v1_prompt_sha256": {"system_prompt": OTHER_HASH}}, "v1_prompt_sha256.system_prompt"),
    ("v1", {"v1_prompt_sha256": {"patient_system_prompt": OTHER_HASH}},
     "v1_prompt_sha256.patient_system_prompt"),
])
def test_one_value_that_differs_refuses_the_run_and_names_the_field_only(ssm, target, change, field):
    record = expect(ssm, target)                                          # the partner first
    assert wm.match_check(ssm.client, target, ARGV[target])
    for name, value in change.items():
        if isinstance(value, dict):
            value = dict(record[name], **value)
        record[name] = value
    ssm.put(wm.EXPECTED_PARAMETER[target], json.dumps(record))
    stop = stopped(lambda: wm.match_check(ssm.client, target, ARGV[target]), wm.MATCH_DIFFERS)
    assert stop.ids == (field,) and stop.counts == {"fields": 1}
    assert OTHER_HASH not in stop.line() and "invented" not in stop.line()   # names, never values


@pytest.mark.parametrize("argv,fields", [
    (["--all", "--patient-second-pass"], ("prompt_caching",)),            # second pass on, caching off
    (["--all", "--prompt-caching"], ("patient_second_pass",)),            # caching on, second pass off
    (["--all"], ("patient_second_pass", "prompt_caching")),
])
def test_v1_is_refused_unless_the_second_pass_and_caching_are_both_on(ssm, argv, fields):
    expect(ssm, "v1")
    assert wm.match_check(ssm.client, "v1", ARGV["v1"])                   # the partner: --deployed-path
    stop = stopped(lambda: wm.match_check(ssm.client, "v1", argv), wm.MATCH_DIFFERS)
    assert stop.ids == fields


@pytest.mark.parametrize("change", [{"patient_second_pass": False}, {"prompt_caching": False}])
def test_a_v1_expected_record_with_either_setting_off_is_refused_not_followed(ssm, change):
    """D11: second pass on, caching on. An expected record that says otherwise is not a
    record this job may match, even if its own argv would agree with it."""
    expect(ssm, "v1", **change)
    stopped(lambda: wm.match_check(ssm.client, "v1", ARGV["v1"]), wm.EXPECTED_INVALID)
    stopped(lambda: wm.match_check(ssm.client, "v1", ["--all"]), wm.EXPECTED_INVALID)
    expect(ssm, "pipeline", prompt_caching=False)                         # the pipeline's may be off
    assert wm.match_check(ssm.client, "pipeline", ["--all", "--no-prompt-caching"])


def test_a_parameter_that_is_missing_or_cannot_be_read_refuses_the_run(ssm):
    stop = stopped(lambda: wm.match_check(ssm.client, "pipeline", ARGV["pipeline"]),
                   wm.EXPECTED_UNREADABLE)                                # never written
    assert stop.ids == ("pipeline",)
    expect(ssm, "pipeline")
    ssm.script(500)
    stopped(lambda: wm.match_check(ssm.client, "pipeline", ARGV["pipeline"]), wm.EXPECTED_UNREADABLE)
    assert len(ssm.requests) == 2                                         # one attempt each: no retry
    assert wm.match_check(ssm.client, "pipeline", ARGV["pipeline"])       # the partner


def _good(target: str = "pipeline") -> dict:
    return dict(wm.own_values(target, ARGV[target]), schema=wm.EXPECTED_SCHEMA)


@pytest.mark.parametrize("value", [
    "", "not json", "[]", "null",
    json.dumps(dict(_good(), note=SENTINEL)),                             # an unknown field
    json.dumps({k: v for k, v in _good().items() if k != "model_id"}),    # a missing field
    json.dumps(dict(_good(), schema="ws7-expected/2")),
    json.dumps(dict(_good(), target="v1")),                               # another target's record
    json.dumps(dict(_good(), pipeline_version="ABCDEF")),
    json.dumps(dict(_good(), pipeline_version=None)),
    json.dumps(dict(_good(), prompt_caching="on")),
    json.dumps(dict(_good(), model_id=f"anthropic {SENTINEL}")),
    json.dumps(_good())[:-1] + ', "model_id": "anthropic.twice"}',        # a duplicate key
])
def test_an_expected_record_that_is_not_exactly_the_shape_is_refused(ssm, value):
    ssm.put(wm.EXPECTED_PARAMETER["pipeline"], value)
    stop = stopped(lambda: wm.match_check(ssm.client, "pipeline", ARGV["pipeline"]),
                   wm.EXPECTED_INVALID)
    assert SENTINEL not in stop.line() and stop.ids == ("pipeline",)
    expect(ssm, "pipeline")
    assert wm.match_check(ssm.client, "pipeline", ARGV["pipeline"])       # the partner


def test_the_expected_values_must_be_a_plain_string_parameter(ssm):
    ssm.put(wm.EXPECTED_PARAMETER["pipeline"], json.dumps(_good()), "SecureString")
    stopped(lambda: wm.match_check(ssm.client, "pipeline", ARGV["pipeline"]), wm.EXPECTED_INVALID)
    ssm.put(wm.EXPECTED_PARAMETER["pipeline"], json.dumps(_good()), "String")
    assert wm.match_check(ssm.client, "pipeline", ARGV["pipeline"])


def test_the_ssm_fake_faults_on_anything_but_get_parameter(ssm):
    """The control: the fake is strict, so a write to SSM would not pass quietly."""
    with pytest.raises(ClientError):
        ssm.client.put_parameter(Name=wm.EXPECTED_PARAMETER["v1"], Value="{}", Type="String")
    with pytest.raises(ClientError):
        ssm.client.get_parameters(Names=[wm.EXPECTED_PARAMETER["v1"]])
    assert ssm.faults == ["ssm:POST:AmazonSSM.PutParameter", "ssm:POST:AmazonSSM.GetParameters"]
    ssm.faults.clear()                                                    # expected


def test_differing_compares_types_as_well_as_values():
    own = wm.own_values("pipeline", ARGV["pipeline"])
    assert wm.differing(own, dict(own)) == ()
    assert wm.differing(own, dict(own, prompt_caching=1)) == ("prompt_caching",)   # 1 is not True
    assert wm.differing({"a": {"x": 1, "y": 2}}, {"a": {"x": 1}}) == ("a.y",)


# ================================================== the commit and the digest ==

def test_the_commit_and_the_digest_are_read_from_the_mapping_given():
    who = wm.identity(ENVIRON)
    assert (who.git_commit, who.image_digest) == (COMMIT, DIGEST)


@pytest.mark.parametrize("environ", [
    {}, {wm.COMMIT_VARIABLE: COMMIT}, {wm.DIGEST_VARIABLE: DIGEST},
    {wm.COMMIT_VARIABLE: "", wm.DIGEST_VARIABLE: DIGEST},
    {wm.COMMIT_VARIABLE: COMMIT[:7], wm.DIGEST_VARIABLE: DIGEST},         # a short hash is not a commit
    {wm.COMMIT_VARIABLE: COMMIT.upper(), wm.DIGEST_VARIABLE: DIGEST},
    {wm.COMMIT_VARIABLE: "unknown", wm.DIGEST_VARIABLE: DIGEST},
    {wm.COMMIT_VARIABLE: COMMIT, wm.DIGEST_VARIABLE: "ab" * 32},          # no "sha256:"
    {wm.COMMIT_VARIABLE: COMMIT, wm.DIGEST_VARIABLE: "latest"},
    {wm.COMMIT_VARIABLE: COMMIT, wm.DIGEST_VARIABLE: f"sha256:{SENTINEL}"},
])
def test_a_run_with_the_commit_or_the_digest_missing_or_malformed_is_refused(environ):
    assert wm.identity(ENVIRON)                                           # the partner
    stop = stopped(lambda: wm.identity(environ), wm.IDENTITY_MISSING)
    assert SENTINEL not in stop.line()


def test_the_identity_is_never_taken_from_the_process_environment(monkeypatch):
    """Tests pass the values in. With both set in the real environment and an empty
    mapping given, the run is still refused: there is no fallback."""
    monkeypatch.setenv(wm.COMMIT_VARIABLE, COMMIT)
    monkeypatch.setenv(wm.DIGEST_VARIABLE, DIGEST)
    assert wm.identity(os.environ).git_commit == COMMIT                   # the control: they are set
    stopped(lambda: wm.identity({}), wm.IDENTITY_MISSING)


# =============================================================== the manifest ==

def test_the_pipelines_manifest_holds_what_d11_lists():
    built = manifest("pipeline")
    assert built == {
        "schema": "ws7-manifest/1", "run_id": "2026-12-16T0400-pipeline", "target": "pipeline",
        "mode": "scheduled", "scheduled_time": "2026-12-16T04:00:00Z", "month": "2026-12",
        "git_commit": COMMIT, "image_digest": DIGEST, "config_sha256": CFG.sha256,
        "client": {"total_max_attempts": 1, "connect_timeout_s": 10, "read_timeout_s": None},
        "keys": {"target": "pipeline", "model_id": "anthropic.claude-sonnet-4-6",
                 "prompt_caching": True, "pipeline_version": orchestrate.pipeline_version(),
                 **BATCH},
        "reservation": {"generations": 19, "margin_percent": 100, "total_usd_micros": 8_280_476},
        "month_assurance_waived": False, "baseline_id": "b-2026-12-14",
        "part_c": "pipeline_step_7"}
    assert "v1_prompt_sha256" not in built["keys"] and "patient_second_pass" not in built["keys"]


def test_v1s_manifest_holds_the_prompt_hashes_the_second_pass_and_the_caching_setting():
    built = manifest("v1")
    own = wm.own_values("v1", ARGV["v1"])
    assert built["keys"] == {"target": "v1", "model_id": "anthropic.claude-sonnet-4-6",
                             "prompt_caching": True, "patient_second_pass": True,
                             "v1_prompt_sha256": own["v1_prompt_sha256"], **BATCH}
    assert "pipeline_version" not in built["keys"]
    # v1's timeouts are fixed by WS7 and written here (ADR-011 D7).
    assert built["client"] == {"total_max_attempts": 1, "connect_timeout_s": 60, "read_timeout_s": 60}
    assert built["reservation"]["total_usd_micros"] == 2_451_884
    assert (built["part_c"], built["run_id"]) == ("v1_second_pass", "2026-12-16T0400-v1")


@pytest.mark.parametrize("target", ["pipeline", "v1"])
def test_a_manifests_keys_are_keys_the_comparator_accepts(target):
    keys = manifest(target)["keys"]
    assert compare._check_keys(json.loads(json.dumps(keys)), "run") == keys
    assert set(keys) == set(compare._TARGET_KEYS[target])


def test_a_manifest_can_say_no_baseline_and_a_waived_month():
    built = manifest(baseline_id=None, month_assurance_waived=True, mode="manual")
    assert (built["baseline_id"], built["month_assurance_waived"]) == (None, True)
    assert (built["mode"], built["run_id"]) == ("manual", "2026-12-16T0400-pipeline-manual")


@pytest.mark.parametrize("change", [
    {"corpus_sha256": "short"},
    {"corpus_sha256": SENTINEL},
    {"gold_sha256": {}},
    {"gold_sha256": {"S1": "not a hash"}},
    {"scorer_versions": {}},
    {"scorer_versions": {"score_batch": 2.5}},
    {"scorer_versions": {"Score Batch": 2}},
    {"baseline_id": f"b {SENTINEL}"},
    {"baseline_id": 7},
    {"month_assurance_waived": "no"},
    {"reservation": reserve.reservation("v1", 19, MARGIN)},               # another target's
    {"reservation": {"target": "pipeline", "generations": 19}},
    {"reservation": None},
    {"own": {"target": "pipeline"}},
    {"own": wm.own_values("v1", ARGV["v1"])},                             # another target's values
    {"who": {"git_commit": COMMIT, "image_digest": DIGEST}},              # not checked by identity()
    {"held": "2026-12-16T0400-pipeline"},
])
def test_a_manifest_is_not_built_from_a_value_out_of_shape(change):
    assert manifest()                                                     # the partner
    stop = stopped(lambda: manifest(**change), wm.MANIFEST_INVALID)
    assert SENTINEL not in stop.line()


def test_a_v1_manifest_needs_the_second_pass():
    own = wm.own_values("v1", ["--all", "--prompt-caching"])              # no second pass
    stopped(lambda: manifest("v1", own=own), wm.MANIFEST_INVALID)


def test_a_manifest_holds_ids_hashes_numbers_and_flags_only():
    def leaves(value):
        if isinstance(value, dict):
            for key, inner in value.items():
                yield key
                yield from leaves(inner)
        else:
            yield value

    for target in ("pipeline", "v1"):
        for leaf in leaves(manifest(target)):
            assert leaf is None or type(leaf) in (int, bool) or (
                type(leaf) is str and re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", leaf)), leaf   # no spaces


# -------------------------------------------------------- writing it once

@pytest.mark.parametrize("mode,prefix", [("scheduled", "ws7/runs/scheduled/2026-12-16T0400-pipeline/"),
                                         ("manual", "ws7/runs/manual/2026-12-16T0400-pipeline-manual/")])
def test_the_manifest_is_written_once_under_the_runs_own_prefix(s3, mode, prefix):
    """Change 2: a scheduled run's records expire after 90 days and a manual run's are
    kept, so the two never share a prefix."""
    built = manifest(mode=mode)
    key = wm.write_manifest(s3.store, built, attempts=CFG.ledger_cas_attempts)
    assert key == prefix + "manifest.json" == wm.manifest_key(built)
    assert s3.json(key) == built
    assert s3.puts == [(key, "if-none-match", 200)]                       # the header, at the server

    other = manifest(mode=mode, baseline_id=None)                         # a different manifest, same run
    stop = stopped(lambda: wm.write_manifest(s3.store, other, attempts=CFG.ledger_cas_attempts),
                   wm.MANIFEST_EXISTS)
    assert stop.ids == (built["run_id"],)
    assert s3.json(key) == built                                          # the first one stands


@pytest.mark.parametrize("script", [fakes.LAND_THEN_500, fakes.LAND_THEN_DROP])
def test_a_manifest_that_landed_but_whose_reply_failed_is_read_back(s3, script):
    built = manifest()
    s3.script_put(wm.manifest_key(built), script)
    key = wm.write_manifest(s3.store, built, attempts=CFG.ledger_cas_attempts)
    assert s3.json(key) == built and s3.count("PUT", key) == 1


def test_a_manifest_that_cannot_be_written_is_a_failure(s3):
    built = manifest()
    key = wm.manifest_key(built)
    s3.script_put(key, fakes.DENY_403)
    stop = stopped(lambda: wm.write_manifest(s3.store, built, attempts=CFG.ledger_cas_attempts),
                   wm.MANIFEST_UNAVAILABLE)
    assert stop.ids == (store.PUT_REFUSED,) and key not in s3.keys()
    stopped(lambda: wm.write_manifest(s3.store, {"schema": "other"}, attempts=1), wm.MANIFEST_INVALID)
    stopped(lambda: wm.write_manifest(s3.store, {"schema": wm.MANIFEST_SCHEMA}, attempts=1),
            wm.MANIFEST_INVALID)
    assert wm.write_manifest(s3.store, built, attempts=1) == key          # the partner


def test_every_refusal_has_a_fixed_code():
    assert len(set(wm.CODES)) == len(wm.CODES) == 8
    assert all(code.startswith("ws7_") for code in wm.CODES)
    assert not set(wm.CODES) & (set(wl.CODES) | set(store.CODES))
