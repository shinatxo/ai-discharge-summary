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

import dataclasses
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
# The argv part 5 will hand each runner for the 18: the only form, with the pre-flight one.
ARGV = {"pipeline": ["--all", "--batch", "b-main", "--yes"],
        "v1": ["--all", "--deployed-path", "--batch", "b-main", "--yes"]}
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


def own(target: str) -> dict:
    return wm.own_values(target, ARGV[target], CFG)


def compared(target: str) -> dict:
    """The job's own values that the match check compares with SSM."""
    return wm._compared(own(target), target)


def expect(ssm, target: str, **change) -> dict:
    """Write the expected record for `target`: the job's own values, changed."""
    record = dict(compared(target), schema=wm.EXPECTED_SCHEMA)
    record.update(change)
    ssm.put(wm.EXPECTED_PARAMETER[target], json.dumps(record))
    return record


def check(ssm, target: str, argv: list | None = None) -> dict:
    return wm.match_check(ssm.client, target, ARGV[target] if argv is None else argv, CFG)


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
    kwargs = dict(held=held(target, mode), own=own(target),
                  who=wm.identity(ENVIRON), cfg=CFG,
                  reservation=reserve.reservation(target, 19, MARGIN), baseline_id="b-2026-12-14",
                  month_assurance_waived=False, **BATCH)
    kwargs.update(change)
    return wm.build_manifest(**kwargs)


# ======================================================= the job's own values ==

@pytest.mark.parametrize("raised", [
    RuntimeError("invented"), KeyboardInterrupt(), SystemExit(0), SystemExit(2),
    store.Stop("ws7_no_baseline"), store.Stop(wm.MATCH_DIFFERS)], ids=repr)
@pytest.mark.parametrize("target, where, name", [
    ("pipeline", "runner", "parse_args"), ("v1", "runner", "parse_args"),
    ("pipeline", "orchestrate", "pipeline_version"), ("v1", "runner", "sha256_bytes")])
def test_whatever_the_runners_code_raises_in_own_values_is_one_fixed_refusal(
        monkeypatch, target, where, name, raised):
    """`own_values` calls the runner's parser, the pipeline's version and the runner's
    hash function. None is the job's code: whatever one raises, a `Stop` of WS7's own
    included, is `ws7_runner_argv_invalid`. The partner is `own(target)`, unbroken."""
    from pipeline import orchestrate
    assert own(target)
    module = orchestrate if where == "orchestrate" else (
        reserve.rpe if target == "pipeline" and name == "parse_args" else reserve.rce)

    def broken(*_args, **_kwargs):
        raise raised
    monkeypatch.setattr(module, name, broken)
    stop = stopped(lambda: own(target), wm.RUNNER_ARGV_INVALID)
    assert stop.ids == () and stop.counts == {}


def test_the_pipelines_own_values_come_from_the_pipeline_and_the_runners_parser():
    assert own("pipeline") == {"target": "pipeline", "model_id": "anthropic.claude-sonnet-4-6",
                               "prompt_caching": True,
                               "pipeline_version": orchestrate.pipeline_version()}


def test_v1s_own_values_include_the_second_pass_the_caching_setting_and_the_gate():
    assert own("v1") == {
        "target": "v1", "model_id": "anthropic.claude-sonnet-4-6",
        "prompt_caching": True, "patient_second_pass": True, "safety_net_gate": True,
        "v1_prompt_sha256": {
            "system_prompt": hashlib.sha256(rce.DEFAULT_PROMPT_PATH.read_bytes()).hexdigest(),
            "patient_system_prompt": hashlib.sha256(
                rce.DEFAULT_PATIENT_PROMPT_PATH.read_bytes()).hexdigest()}}
    # The gate is a WS7 rule, not an SSM value: it is not among the compared fields.
    assert "safety_net_gate" not in compared("v1") and "safety_net_gate" not in own("pipeline")
    # Read from the runner's own parser: these are what the runner would do with this argv.
    args = rce.parse_args(ARGV["v1"])
    assert (args.patient_second_pass, args.prompt_caching, args.gate) == (True, True, True)


# ------------------------------------------------- the runner argv allow-list

@pytest.mark.parametrize("target", ["pipeline", "v1"])
def test_the_only_argv_accepted_is_the_one_part_5_will_pass(target):
    """Ruling 5 (Codex P2). `runner_argv` builds the forms; `own_values` accepts those
    and nothing else."""
    extra = ["--deployed-path"] if target == "v1" else []
    assert wm.runner_argv(target, CFG, preflight=False, batch="2026-12-16T0400.main") \
        == ["--all", *extra, "--batch", "2026-12-16T0400.main", "--yes"]
    assert wm.runner_argv(target, CFG, preflight=True, batch="b-pre") \
        == [CFG.preflight_scenario, *extra, "--batch", "b-pre", "--yes"] and CFG.preflight_scenario == "S1"
    for preflight in (True, False):
        argv = wm.runner_argv(target, CFG, preflight=preflight, batch="b1")
        assert wm.own_values(target, argv, CFG) == own(target)            # the batch and scenarios do not change them
    for bad in ("has space", "", "x" * 65, f"b{SENTINEL}", None, "--yes", "-b", ".hidden"):
        stopped(lambda: wm.runner_argv(target, CFG, preflight=False, batch=bad), wm.RUNNER_ARGV_INVALID)
    stopped(lambda: wm.runner_argv("canary", CFG, preflight=False, batch="b1"), wm.RUNNER_ARGV_INVALID)


def _config_with(tmp_path, **change) -> Path:
    """The repository's config with one field changed, written to a temporary file."""
    data = json.loads(reserve.CONFIG_PATH.read_text("utf-8"))
    data.update(change)
    path = tmp_path / "ws7_config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("scenario", ["--all", "-x", "-", "--yes", "-S1"])
def test_the_config_loader_refuses_a_pre_flight_scenario_that_starts_with_a_dash(tmp_path, scenario):
    """Ruling B (Codex re-check). A pre-flight "scenario" of --all would make the
    pre-flight run the whole corpus: `runner_argv` puts it where a scenario ID goes."""
    with pytest.raises(reserve.ReserveError) as exc:
        reserve.load_config(_config_with(tmp_path, preflight_scenario=scenario))
    assert exc.value.code == "config_shape_invalid"


@pytest.mark.parametrize("scenario", ["S1", "S18", "s_1", "S-1", "1"])
def test_the_config_loader_accepts_an_ordinary_scenario_id(tmp_path, scenario):
    """The partner. A dash inside an ID is still allowed; only a leading one is not."""
    cfg = reserve.load_config(_config_with(tmp_path, preflight_scenario=scenario))
    assert cfg.preflight_scenario == scenario
    argv = wm.runner_argv("pipeline", cfg, preflight=True, batch="b1")
    assert argv == [scenario, "--batch", "b1", "--yes"] and argv[0] != "--all"   # one scenario, never all


REFUSED_ARGV = [
    ("v1", ["--all", "--deployed-path", "--no-gate", "--batch", "b1", "--yes"]),       # the gate off
    ("pipeline", ["--all", "--verify", "--batch", "b1", "--yes"]),                      # step 7v
    ("pipeline", ["--all", "--no-prompt-caching", "--batch", "b1", "--yes"]),
    ("pipeline", ["--all", "--dry-run", "--batch", "b1", "--yes"]),
    ("pipeline", ["--all", "--runs", "3", "--batch", "b1", "--yes"]),
    ("pipeline", ["--all", "--corpus", "other.json", "--batch", "b1", "--yes"]),
    ("pipeline", ["--all", "--deployed-path", "--batch", "b1", "--yes"]),               # v1's flag
    ("pipeline", ["--all", "--batch", "b1"]),                                           # no --yes
    ("pipeline", ["--all", "--yes"]),                                                   # no --batch
    ("pipeline", ["--yes", "--batch", "b1", "--all"]),                                  # another order
    ("pipeline", ["S2", "--batch", "b1", "--yes"]),                                     # not the pre-flight scenario
    ("pipeline", ["S1", "S2", "--batch", "b1", "--yes"]),
    ("pipeline", ["--all", "S1", "--batch", "b1", "--yes"]),
    ("pipeline", ["--all", "--batch", "has space", "--yes"]),
    ("pipeline", ["--all", "--batch", f"b {SENTINEL}", "--yes"]),
    ("pipeline", ["--all", "--batch", "--yes", "--yes"]),
    ("pipeline", ["--all", "--batch=b1", "--yes"]),
    ("pipeline", ["--no-such-flag"]),
    ("pipeline", []),
    ("pipeline", "--all --batch b1 --yes"),
    ("pipeline", ["--all", "--batch", 7, "--yes"]),
    ("v1", ["--all", "--batch", "b1", "--yes"]),                                        # no --deployed-path
    ("v1", ["--all", "--patient-second-pass", "--batch", "b1", "--yes"]),
    ("v1", ["--all", "--prompt-caching", "--batch", "b1", "--yes"]),
    ("v1", ["--all", "--deployed-path", "--prompt", "other.md", "--batch", "b1", "--yes"]),
    ("v1", ["--all", "--deployed-path", "--patient-prompt", "other.md", "--batch", "b1", "--yes"]),
    ("v1", ["--all", "--deployed-path", "--dry-run", "--batch", "b1", "--yes"]),
    ("v1", ["--all", "--deployed-path", "--runs", "0", "--batch", "b1", "--yes"]),
    ("canary", ["--all", "--batch", "b1", "--yes"]),
]


@pytest.mark.parametrize("target,argv", REFUSED_ARGV)
def test_any_other_argv_is_refused_and_nothing_is_printed(capsys, monkeypatch, target, argv):
    """The runner's parser never sees a refused argv, so argparse prints nothing."""
    seen = []
    for runner in (wm.reserve.rpe, wm.reserve.rce):
        monkeypatch.setattr(runner, "parse_args", lambda a, real=runner.parse_args: (seen.append(a),
                                                                                    real(a))[1])
    stop = stopped(lambda: wm.own_values(target, argv, CFG), wm.RUNNER_ARGV_INVALID)
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "") and SENTINEL not in stop.line()
    assert seen == []                                                     # the parser was never called
    assert wm.own_values("pipeline", ARGV["pipeline"], CFG) and len(seen) == 1   # the partner; the spy sees


def test_what_the_parser_made_of_the_argv_is_checked_too(monkeypatch):
    """If a runner's defaults changed under an allowed argv, the rule still holds:
    for v1, a parsed gate that is off is refused."""
    real = rce.parse_args

    def gate_off(argv):
        args = real(argv)
        args.gate = False
        return args

    assert own("v1")["safety_net_gate"] is True                           # the partner
    monkeypatch.setattr(rce, "parse_args", gate_off)
    stopped(lambda: own("v1"), wm.RUNNER_ARGV_INVALID)


# ============================================================ the match check ==

@pytest.mark.parametrize("target", ["pipeline", "v1"])
def test_the_match_check_passes_when_every_value_is_the_expected_one(ssm, target):
    expect(ssm, target)
    assert check(ssm, target) == own(target)
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
    assert check(ssm, target)
    for name, value in change.items():
        if isinstance(value, dict):
            value = dict(record[name], **value)
        record[name] = value
    ssm.put(wm.EXPECTED_PARAMETER[target], json.dumps(record))
    stop = stopped(lambda: check(ssm, target), wm.MATCH_DIFFERS)
    assert stop.ids == (field,) and stop.counts == {"fields": 1}
    assert OTHER_HASH not in stop.line() and "invented" not in stop.line()   # names, never values


def test_v1_cannot_be_run_with_the_second_pass_or_caching_off(ssm):
    """D11: second pass on, caching on. The argv that would turn either off is not on
    the allow-list, so the run is refused before the comparison; and if the job's own
    value were off all the same, the comparison names the field."""
    expect(ssm, "v1")
    assert check(ssm, "v1")                                               # the partner: --deployed-path
    for argv in (["--all", "--patient-second-pass", "--batch", "b1", "--yes"],
                 ["--all", "--prompt-caching", "--batch", "b1", "--yes"],
                 ["--all", "--batch", "b1", "--yes"]):
        stopped(lambda: check(ssm, "v1", argv), wm.RUNNER_ARGV_INVALID)
    expected = wm.read_expected(ssm.client, "v1")
    assert wm.differing(dict(compared("v1"), patient_second_pass=False), expected) \
        == ("patient_second_pass",)
    assert wm.differing(dict(compared("v1"), prompt_caching=False), expected) == ("prompt_caching",)


@pytest.mark.parametrize("change", [{"patient_second_pass": False}, {"prompt_caching": False}])
def test_a_v1_expected_record_with_either_setting_off_is_refused_not_followed(ssm, change):
    """An expected record that says otherwise is not a record this job may match."""
    expect(ssm, "v1", **change)
    stopped(lambda: check(ssm, "v1"), wm.EXPECTED_INVALID)
    expect(ssm, "pipeline", prompt_caching=False)         # the pipeline's record may say off...
    stop = stopped(lambda: check(ssm, "pipeline"), wm.MATCH_DIFFERS)      # ...but this job runs with it on
    assert stop.ids == ("prompt_caching",)


def test_a_parameter_that_is_missing_or_cannot_be_read_refuses_the_run(ssm):
    stop = stopped(lambda: check(ssm, "pipeline"), wm.EXPECTED_UNREADABLE)   # never written
    assert stop.ids == ("pipeline",)
    expect(ssm, "pipeline")
    ssm.script(500)
    stopped(lambda: check(ssm, "pipeline"), wm.EXPECTED_UNREADABLE)
    assert len(ssm.requests) == 2                                         # one attempt each: no retry
    assert check(ssm, "pipeline")                                         # the partner


def _good(target: str = "pipeline") -> dict:
    return dict(compared(target), schema=wm.EXPECTED_SCHEMA)


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
    stop = stopped(lambda: check(ssm, "pipeline"), wm.EXPECTED_INVALID)
    assert SENTINEL not in stop.line() and stop.ids == ("pipeline",)
    expect(ssm, "pipeline")
    assert check(ssm, "pipeline")                                         # the partner


def test_the_expected_values_must_be_a_plain_string_parameter(ssm):
    ssm.put(wm.EXPECTED_PARAMETER["pipeline"], json.dumps(_good()), "SecureString")
    stopped(lambda: check(ssm, "pipeline"), wm.EXPECTED_INVALID)
    ssm.put(wm.EXPECTED_PARAMETER["pipeline"], json.dumps(_good()), "String")
    assert check(ssm, "pipeline")


def test_the_ssm_fake_faults_on_anything_but_get_parameter(ssm):
    """The control: the fake is strict, so a write to SSM would not pass quietly."""
    with pytest.raises(ClientError):
        ssm.client.put_parameter(Name=wm.EXPECTED_PARAMETER["v1"], Value="{}", Type="String")
    with pytest.raises(ClientError):
        ssm.client.get_parameters(Names=[wm.EXPECTED_PARAMETER["v1"]])
    assert ssm.faults == ["ssm:POST:AmazonSSM.PutParameter", "ssm:POST:AmazonSSM.GetParameters"]
    ssm.faults.clear()                                                    # expected


def test_differing_compares_types_as_well_as_values():
    mine = compared("pipeline")
    assert wm.differing(mine, dict(mine)) == ()
    assert wm.differing(mine, dict(mine, prompt_caching=1)) == ("prompt_caching",)   # 1 is not True
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
        "safety_net_gate": None,                    # the pipeline runner has no such switch
        "month_assurance_waived": False, "baseline_id": "b-2026-12-14",
        "part_c": "pipeline_step_7"}
    assert "v1_prompt_sha256" not in built["keys"] and "patient_second_pass" not in built["keys"]


def test_v1s_manifest_holds_the_prompt_hashes_the_second_pass_and_the_caching_setting():
    built = manifest("v1")
    assert built["keys"] == {"target": "v1", "model_id": "anthropic.claude-sonnet-4-6",
                             "prompt_caching": True, "patient_second_pass": True,
                             "v1_prompt_sha256": own("v1")["v1_prompt_sha256"], **BATCH}
    assert "pipeline_version" not in built["keys"]
    assert built["safety_net_gate"] is True and "safety_net_gate" not in built["keys"]   # a WS7 rule
    # v1's timeouts are fixed by WS7 and written here (ADR-011 D7).
    assert built["client"] == {"total_max_attempts": 1, "connect_timeout_s": 60, "read_timeout_s": 60}
    assert built["reservation"]["total_usd_micros"] == 2_451_884
    assert (built["part_c"], built["run_id"]) == ("v1_second_pass", "2026-12-16T0400-v1")


@pytest.mark.parametrize("target", ["pipeline", "v1"])
def test_a_manifests_keys_are_keys_the_comparator_accepts(target):
    keys = manifest(target)["keys"]
    assert compare.check_keys(json.loads(json.dumps(keys)), "run") == keys   # the public name (part 3)
    assert set(keys) == set(compare._TARGET_KEYS[target])
    # The control: the same check refuses the other target's keys under this target's name.
    other = manifest("v1" if target == "pipeline" else "pipeline")["keys"]
    with pytest.raises(compare.CompareError):
        compare.check_keys(dict(json.loads(json.dumps(other)), target=target), "run")


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
    {"own": wm.own_values("v1", ARGV["v1"], CFG)},                        # another target's values
    {"own": dict(wm.own_values("pipeline", ARGV["pipeline"], CFG), safety_net_gate=True)},
    {"cfg": None},
    {"who": {"git_commit": COMMIT, "image_digest": DIGEST}},              # not checked by identity()
    {"held": "2026-12-16T0400-pipeline"},
])
def test_a_manifest_is_not_built_from_a_value_out_of_shape(change):
    assert manifest()                                                     # the partner
    stop = stopped(lambda: manifest(**change), wm.MANIFEST_INVALID)
    assert SENTINEL not in stop.line()


@pytest.mark.parametrize("target", ["pipeline", "v1"])
@pytest.mark.parametrize("part, given", [
    ("gold_sha256", {1: "b" * 64}), ("gold_sha256", {True: "b" * 64}),
    ("gold_sha256", {"S1": "b" * 64, 2: "c" * 64}), ("scorer_versions", {1: 2}),
    ("scorer_versions", {None: 2})], ids=repr)
def test_a_manifest_is_not_built_from_keys_that_pass_only_after_a_json_round_trip(target, part,
                                                                               given):
    """Codex P3, PR #8 (part 5, carried item C). `build_keys()` copies its inputs with
    a JSON round trip, which turns an integer key 1 into "1". It now checks what it was
    given before it copies it. The partner: the same keys as strings build a manifest."""
    as_strings = json.loads(json.dumps(given))
    assert all(type(key) is str for key in as_strings) and as_strings != given
    assert manifest(target, **{part: as_strings})["keys"][part] == as_strings
    stopped(lambda: manifest(target, **{part: given}), wm.MANIFEST_INVALID)


@pytest.mark.parametrize("change", [{"patient_second_pass": False}, {"safety_net_gate": False},
                                    {"safety_net_gate": None}])
def test_a_v1_manifest_needs_the_second_pass_and_the_gate(change):
    assert manifest("v1")                                                 # the partner
    stopped(lambda: manifest("v1", own=dict(own("v1"), **change)), wm.MANIFEST_INVALID)
    without_gate = {k: v for k, v in own("v1").items() if k != "safety_net_gate"}
    stopped(lambda: manifest("v1", own=without_gate), wm.MANIFEST_INVALID)


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


MALFORMED = [
    lambda m: m.pop("git_commit"),                                        # a field missing
    lambda m: m.update(note=SENTINEL),                                    # a field too many
    lambda m: m.update(schema="ws7-manifest/2"),
    lambda m: m.update(run_id="2026-12-17T0400-pipeline"),                # not this scheduled time's run
    lambda m: m.update(run_id="2026-12-16T0400-v1"),                      # another target's run
    lambda m: m.update(mode="manual"),                                    # the run ID is a scheduled one
    lambda m: m.update(target="canary"),
    lambda m: m.update(scheduled_time="2026-12-16T04:00:00"),
    lambda m: m.update(month="2026-11"),
    lambda m: m.update(git_commit="unknown"),
    lambda m: m.update(image_digest="latest"),
    lambda m: m.update(config_sha256="short"),
    lambda m: m["client"].update(total_max_attempts=3),
    lambda m: m["client"].update(total_max_attempts=True),
    lambda m: m["client"].update(connect_timeout_s=600),
    lambda m: m["client"].update(retries=2),
    lambda m: m["keys"].update(corpus_sha256=SENTINEL),
    lambda m: m["keys"].update(target="v1"),
    lambda m: m["keys"].pop("pipeline_version"),
    lambda m: m.update(keys=None),
    lambda m: m["reservation"].update(total_usd_micros=0),
    lambda m: m["reservation"].update(total_usd_micros="8280476"),
    lambda m: m["reservation"].update(target="pipeline"),
    lambda m: m.update(safety_net_gate=True),                             # the pipeline has no gate switch
    lambda m: m.update(month_assurance_waived="no"),
    lambda m: m.update(baseline_id=f"b {SENTINEL}"),
    lambda m: m.update(part_c="v1_second_pass"),                          # another target's label
]


@pytest.mark.parametrize("damage", MALFORMED)
def test_a_malformed_manifest_sends_no_request(s3, damage):
    """Ruling 6 (Codex P2). The whole payload is validated immediately before the
    conditional PUT, so a manifest changed after it was built never reaches S3."""
    bad = json.loads(json.dumps(manifest()))
    damage(bad)
    stop = stopped(lambda: wm.write_manifest(s3.store, bad, attempts=CFG.ledger_cas_attempts),
                   wm.MANIFEST_INVALID)
    assert SENTINEL not in stop.line()
    assert s3.requests == [] and s3.keys() == []                          # nothing was sent
    stopped(lambda: wm.validate_manifest(bad), wm.MANIFEST_INVALID)

    key = wm.write_manifest(s3.store, manifest(), attempts=CFG.ledger_cas_attempts)   # the partner
    assert s3.requests == [("PUT", key)]


@pytest.mark.parametrize("damage", [
    lambda m: m["keys"].update(patient_second_pass=False),                # PART C not the second pass's
    lambda m: m.update(safety_net_gate=None),                             # v1 with no gate recorded
    lambda m: m.update(safety_net_gate=False),
    lambda m: m["client"].update(read_timeout_s=None),                    # v1's is fixed at 60
    lambda m: m.update(part_c="pipeline_step_7"),
])
def test_a_malformed_v1_manifest_sends_no_request(s3, damage):
    bad = json.loads(json.dumps(manifest("v1")))
    damage(bad)
    stopped(lambda: wm.write_manifest(s3.store, bad, attempts=CFG.ledger_cas_attempts),
            wm.MANIFEST_INVALID)
    assert s3.requests == []
    assert wm.validate_manifest(json.loads(json.dumps(manifest("v1"))))   # the partner


def test_a_v1_manifest_with_caching_off_is_refused_and_sends_no_request(s3):
    """Part 3, item 6 (follow-up b of PR #6). v1 runs with caching on (D11); the
    validator already required the second pass."""
    # It is not built: the job's own values, with caching off.
    stopped(lambda: manifest("v1", own=dict(own("v1"), prompt_caching=False)), wm.MANIFEST_INVALID)
    # And one changed after it was built is not written.
    bad = json.loads(json.dumps(manifest("v1")))
    bad["keys"]["prompt_caching"] = False
    assert compare.check_keys(json.loads(json.dumps(bad["keys"])), "run")  # well-formed keys: the rule is WS7's
    stopped(lambda: wm.validate_manifest(bad), wm.MANIFEST_INVALID)
    stopped(lambda: wm.write_manifest(s3.store, bad, attempts=CFG.ledger_cas_attempts),
            wm.MANIFEST_INVALID)
    assert s3.requests == [] and s3.keys() == []                          # nothing was sent

    # The partner, and the control for "nothing was sent": caching on is written.
    good = manifest("v1")
    assert good["keys"]["prompt_caching"] is True and good["keys"]["patient_second_pass"] is True
    key = wm.write_manifest(s3.store, good, attempts=CFG.ledger_cas_attempts)
    assert s3.requests == [("PUT", key)]


NOT_A_SCENARIO = ["--all", "-x", "-", "--yes", "-S1", "", "has space", "S" * 33, None, 7]


@pytest.mark.parametrize("scenario", NOT_A_SCENARIO, ids=repr)
@pytest.mark.parametrize("target", ["pipeline", "v1"])
def test_runner_argv_refuses_a_config_built_in_code_whose_pre_flight_scenario_is_not_one(
        monkeypatch, target, scenario):
    """Part 3, item 7 (follow-up c of PR #6). The loader refuses these, but a Config
    built in code never went through the loader: with "--all" the pre-flight would
    run all 18."""
    built_in_code = dataclasses.replace(CFG, preflight_scenario=scenario)
    for preflight in (True, False):
        stopped(lambda: wm.runner_argv(target, built_in_code, preflight=preflight, batch="b1"),
                wm.RUNNER_ARGV_INVALID)
    # So no argv is accepted under such a config, and the runner's parser sees none.
    seen = []
    for runner in (wm.reserve.rpe, wm.reserve.rce):
        monkeypatch.setattr(runner, "parse_args", lambda a, real=runner.parse_args: (seen.append(a),
                                                                                    real(a))[1])
    stopped(lambda: wm.own_values(target, ARGV[target], built_in_code), wm.RUNNER_ARGV_INVALID)
    assert seen == []
    assert wm.own_values(target, ARGV[target], CFG) and len(seen) == 1    # the partner; the spy sees


@pytest.mark.parametrize("scenario", ["S1", "S18", "s_1", "S-1", "1"])
def test_runner_argv_accepts_a_config_built_in_code_with_an_ordinary_scenario_id(scenario):
    """The partner. The rule is the loader's own pattern, read from the loader."""
    built_in_code = dataclasses.replace(CFG, preflight_scenario=scenario)
    assert wm.runner_argv("pipeline", built_in_code, preflight=True, batch="b1") \
        == [scenario, "--batch", "b1", "--yes"]
    assert wm.runner_argv("v1", built_in_code, preflight=False, batch="b1") \
        == ["--all", "--deployed-path", "--batch", "b1", "--yes"]
    assert reserve._SCENARIO_RE.fullmatch(scenario)


def test_every_refusal_has_a_fixed_code():
    assert len(set(wm.CODES)) == len(wm.CODES) == 8
    assert all(code.startswith("ws7_") for code in wm.CODES)
    assert not set(wm.CODES) & (set(wl.CODES) | set(store.CODES))
