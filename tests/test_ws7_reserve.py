"""evals/ws7_reserve.py - the worst-case reservation, the cost of a call and the
config file (ADR-011 D7). No AWS, no model call; invented usage figures only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import run_cold_eval as rce  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
from pipeline import compose, compose_patient, extract, reconcile  # noqa: E402

PENCE_PER_USD = 80      # ADR-011 "Costs": GBP 0.80 to USD 1


def pence(nanos: int) -> int:
    """Whole pence, rounded half up, at the ADR's rate."""
    return (nanos * PENCE_PER_USD + 500_000_000) // 1_000_000_000


# ------------------------------------------------------------ the constants

# Every figure the reservation rests on, with the file and line that owns it.
# If a line moves or a value changes, this test fails and the reservation, the
# docs and ADR-011's table are looked at together.
CITED = [
    ("src/generate/pipeline/extract.py", 75, "MAX_TOKENS = 8192"),
    ("src/generate/pipeline/extract.py", 121, '"maxTokens": MAX_TOKENS'),
    ("src/generate/pipeline/reconcile.py", 58, "MAX_TOKENS = 4096"),
    ("src/generate/pipeline/reconcile.py", 251, '"maxTokens": MAX_TOKENS'),
    ("src/generate/pipeline/compose.py", 85, "MAX_TOKENS = 6000"),
    ("src/generate/pipeline/compose.py", 411, '"maxTokens": MAX_TOKENS'),
    ("src/generate/pipeline/compose_patient.py", 103, "MAX_TOKENS = 3000"),
    ("src/generate/pipeline/compose_patient.py", 431, '"maxTokens": MAX_TOKENS'),
    ("src/generate/pipeline/converse.py", 22, "THROTTLE_RETRIES = 2"),
    ("src/generate/pipeline/converse.py", 34, "except StepError:"),
    ("evals/run_cold_eval.py", 96, "MAX_TOKENS = 4096"),
    ("evals/run_cold_eval.py", 98, "PATIENT_MAX_TOKENS = 1500"),
    ("evals/run_cold_eval.py", 115, "PRICE_PER_MTOK = "),
    ("evals/run_cold_eval.py", 116, "REGIONAL_PREMIUM = 1.10"),
    ("evals/run_cold_eval.py", 121, 'EST_TOKENS = {False: {"input": 5600'),
    ("evals/run_cold_eval.py", 122, 'True: {"input": 8900'),
    ("evals/run_cold_eval.py", 124, "MAX_THROTTLE_RETRIES = 2"),
    ("evals/run_cold_eval.py", 333, '"maxTokens": max_tokens'),
    ("evals/run_cold_eval.py", 395, "except Exception as exc:"),
    ("evals/run_cold_eval.py", 684, 'boto3.client("bedrock-runtime", region_name=REGION)'),
    ("evals/run_pipeline_eval.py", 75, "Step 2 ~6k in"),
    ("evals/run_pipeline_eval.py", 76, "5a ~7k in"),
    ("evals/run_pipeline_eval.py", 76, "step 6 ~5k in"),
    ("evals/run_pipeline_eval.py", 78, "+2.5k"),
    ("evals/run_pipeline_eval.py", 79, 'EST_TOKENS = {"input": 20500'),
    ("evals/run_pipeline_eval.py", 64, "READ_TIMEOUT_S = 90"),
    ("evals/run_pipeline_eval.py", 65, "READ_TIMEOUT_5A_S = reconcile.READ_TIMEOUT_S"),
    ("evals/run_pipeline_eval.py", 174, "connect_timeout=10"),
    ("evals/run_pipeline_eval.py", 175, '"total_max_attempts": 1'),
    ("evals/run_pipeline_eval.py", 333, "factory(READ_TIMEOUT_S)"),
    ("evals/run_pipeline_eval.py", 335, "factory(READ_TIMEOUT_5A_S)"),
    ("src/generate/pipeline/extract.py", 45, 'TOOL_NAME = "record_facts"'),
    ("src/generate/pipeline/reconcile.py", 52, 'TOOL_NAME = "record_reconciliation"'),
    ("src/generate/pipeline/reconcile.py", 60, "READ_TIMEOUT_S = 60"),
    ("src/generate/pipeline/compose.py", 79, 'TOOL_NAME = "record_composition"'),
    ("src/generate/pipeline/compose_patient.py", 97, 'TOOL_NAME = "record_patient_version"'),
]


@pytest.mark.parametrize("path,line,text", CITED)
def test_every_cited_line_still_says_what_the_docs_say(path, line, text):
    assert text in (ROOT / path).read_text(encoding="utf-8").splitlines()[line - 1]


def test_the_plan_reads_each_limit_from_the_module_that_owns_it():
    steps = {s.id: s for s in reserve.plan("pipeline").steps}
    assert [s.id for s in reserve.plan("pipeline").steps] == list(rpe.MODEL_STEPS)
    for name, module, limit in (("extract_facts", extract, 8192),
                                ("reconcile_medications", reconcile, 4096),
                                ("compose_clinical", compose, 6000),
                                ("compose_patient", compose_patient, 3000)):
        assert steps[name].max_tokens == module.MAX_TOKENS == limit    # ADR-011 D7's table
        assert steps[name].tool_name == module.TOOL_NAME
    v1 = {s.id: s for s in reserve.plan("v1").steps}
    assert v1["combined"].max_tokens == rce.MAX_TOKENS == 4096
    assert v1["patient_v2"].max_tokens == rce.PATIENT_MAX_TOKENS == 1500
    assert all(s.tool_name is None for s in v1.values())
    assert reserve.plan("pipeline").model_id == reserve.plan("v1").model_id == rce.MODEL_ID


def test_the_input_allowances_add_up_to_the_runners_planning_figures():
    assert sum(s.input_tokens for s in reserve.plan("pipeline").steps) == rpe.EST_TOKENS["input"]
    assert sum(s.input_tokens for s in reserve.plan("v1").steps) == rce.EST_TOKENS[True]["input"]
    assert reserve.plan("v1").steps[0].input_tokens == rce.EST_TOKENS[False]["input"]


def test_a_changed_limit_in_a_step_is_seen_not_a_stale_copy(monkeypatch):
    monkeypatch.setattr(extract, "MAX_TOKENS", 9000)
    assert reserve.plan("pipeline").steps[0].max_tokens == 9000


def test_the_plan_refuses_a_split_that_no_longer_adds_up(monkeypatch):
    assert reserve.plan("pipeline")                       # the partner: today it builds
    monkeypatch.setitem(rpe.EST_TOKENS, "input", 20501)
    with pytest.raises(reserve.ReserveError) as exc:
        reserve.plan("pipeline")
    assert exc.value.code == "input_allowance_split_stale"


def test_the_plan_refuses_an_unknown_target_and_indistinct_v1_steps(monkeypatch):
    with pytest.raises(reserve.ReserveError) as exc:
        reserve.plan("v2")
    assert exc.value.code == "target_unknown"
    monkeypatch.setattr(rce, "PATIENT_MAX_TOKENS", rce.MAX_TOKENS)
    with pytest.raises(reserve.ReserveError) as exc:
        reserve.plan("v1")
    assert exc.value.code == "v1_steps_indistinct"


def test_the_timeouts_are_the_runners_own():
    assert reserve.plan("v1").read_timeout_s == 60 and reserve.plan("v1").connect_timeout_s == 60
    assert reserve.plan("pipeline").read_timeout_s is None      # the runner names it per client
    assert reserve.plan("pipeline").connect_timeout_s == 10


def test_v1s_fixed_timeouts_are_the_botocore_defaults_the_v1_runner_runs_on_today():
    """The v1 runner's default client passes no Config (evals/run_cold_eval.py:684)."""
    from botocore.config import Config
    defaults = Config()                    # what a client built with no Config is given
    assert defaults.read_timeout == reserve.V1_READ_TIMEOUT_S == 60
    assert defaults.connect_timeout == reserve.V1_CONNECT_TIMEOUT_S == 60


# ------------------------------------------------------------------- prices

def test_rates_are_whole_nano_dollars_per_token_with_the_premium():
    assert reserve.rates() == {"input": 3300, "output": 16500, "cache_write": 4125,
                               "cache_read": 330}


def test_a_price_that_is_not_a_whole_number_of_nano_dollars_is_refused(monkeypatch):
    monkeypatch.setitem(rce.PRICE_PER_MTOK, "input", 3.0001234)
    with pytest.raises(reserve.ReserveError) as exc:
        reserve.rates()
    assert exc.value.code == "price_not_whole_nanos"


@pytest.mark.parametrize("nanos,micros", [(0, 0), (1, 1), (999, 1), (1000, 1), (1001, 2)])
def test_micro_dollars_round_up(nanos, micros):
    assert reserve.to_micros(nanos) == micros


# -------------------------------------------------------------- reservation

def test_the_pipeline_reservation_is_adr_011s_figure():
    res = reserve.reservation("pipeline", 19, 100)             # 18 and one pre-flight
    assert res["per_generation_usd_nanos"] == 435_814_500
    assert res["total_usd_nanos"] == 8_280_475_500 and res["total_usd_micros"] == 8_280_476
    assert (pence(res["total_usd_nanos"]), pence(res["output_part_usd_nanos"]),
            pence(res["input_part_usd_nanos"])) == (662, 534, 129)      # GBP 6.62 = 5.34 + 1.29
    assert res["output_part_usd_nanos"] + res["input_part_usd_nanos"] == res["total_usd_nanos"]


def test_the_v1_reservation_is_adr_011s_figure():
    res = reserve.reservation("v1", 19, 100)
    assert (pence(res["total_usd_nanos"]), pence(res["output_part_usd_nanos"]),
            pence(res["input_part_usd_nanos"])) == (196, 140, 56)       # GBP 1.96 = 1.40 + 0.56


def test_the_baseline_reservation_with_and_without_its_pre_flight():
    assert pence(reserve.reservation("pipeline", 54, 100)["total_usd_nanos"]) == 1883
    assert pence(reserve.reservation("pipeline", 55, 100)["total_usd_nanos"]) == 1918


def test_the_dearest_single_request_is_step_2_at_about_13_pence():
    worst = {s.id: reserve.worst_case_nanos(s, 100) for s in reserve.plan("pipeline").steps}
    assert max(worst, key=worst.get) == "extract_facts"
    assert worst["extract_facts"] == 8192 * 16500 + 6000 * 4125 == 159_918_000
    assert pence(worst["extract_facts"]) == 13


def test_the_reservation_is_the_sum_of_its_calls_at_their_worst():
    for target in reserve.TARGETS:
        res = reserve.reservation(target, 7, 100)
        per_generation = sum(reserve.worst_case_nanos(s, 100) for s in reserve.plan(target).steps)
        assert res["total_usd_nanos"] == 7 * per_generation
        assert sum(r["worst_case_usd_nanos"] for r in res["per_step"]) == per_generation


def test_the_margin_widens_the_input_part_only_and_rounds_tokens_up():
    base, wide = reserve.reservation("pipeline", 19, 100), reserve.reservation("pipeline", 19, 150)
    assert wide["output_part_usd_nanos"] == base["output_part_usd_nanos"]
    assert wide["input_part_usd_nanos"] * 2 == base["input_part_usd_nanos"] * 3
    step = reserve.Step("x", 1, 3, None)
    assert reserve.allowance_tokens(step, 100) == 3
    assert reserve.allowance_tokens(step, 150) == 5        # 4.5 tokens is reserved as 5


@pytest.mark.parametrize("margin", [99, 1001, 100.0, "100", True, None])
def test_a_margin_outside_its_range_is_refused(margin):
    assert reserve.reservation("v1", 1, 100) and reserve.reservation("v1", 1, 1000)   # partners
    with pytest.raises(reserve.ReserveError) as exc:
        reserve.reservation("v1", 1, margin)
    assert exc.value.code == "margin_invalid"


@pytest.mark.parametrize("generations", [0, -1, 1.0, True, "19"])
def test_generations_must_be_a_whole_number_of_one_or_more(generations):
    with pytest.raises(reserve.ReserveError) as exc:
        reserve.reservation("pipeline", generations, 100)
    assert exc.value.code == "generations_invalid"


# ----------------------------------------------------------- cost of a call

def test_usage_is_costed_at_each_token_kinds_own_rate():
    got = reserve.read_usage({"inputTokens": 1000, "outputTokens": 200, "totalTokens": 1200,
                              "cacheReadInputTokens": 3000, "cacheWriteInputTokens": 40})
    assert got == {"input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 3000,
                   "cache_write_tokens": 40,
                   "cost_usd_nanos": 1000 * 3300 + 200 * 16500 + 3000 * 330 + 40 * 4125}


def test_usage_agrees_with_the_runners_own_cost_estimate():
    usage = {"inputTokens": 5321, "outputTokens": 1777, "cacheReadInputTokens": 2100,
             "cacheWriteInputTokens": 900}
    runner = rce.cost_usd([{"input_tokens": 5321, "output_tokens": 1777,
                            "cache_read_tokens": 2100, "cache_write_tokens": 900}])
    assert round(reserve.read_usage(usage)["cost_usd_nanos"] / 1e9, 6) == runner


def test_absent_cache_counts_are_zero():
    got = reserve.read_usage({"inputTokens": 10, "outputTokens": 0})
    assert (got["cache_read_tokens"], got["cache_write_tokens"]) == (0, 0)
    assert got["cost_usd_nanos"] == 33_000


@pytest.mark.parametrize("usage", [
    None, "usage", [], {}, {"inputTokens": 10}, {"outputTokens": 10},
    {"inputTokens": True, "outputTokens": 1}, {"inputTokens": 1.0, "outputTokens": 1},
    {"inputTokens": -1, "outputTokens": 1}, {"inputTokens": "10", "outputTokens": 1},
    {"inputTokens": 1, "outputTokens": None},
    {"inputTokens": 1, "outputTokens": 1, "cacheReadInputTokens": -5},
    {"inputTokens": 1, "outputTokens": 1, "cacheWriteInputTokens": "3"},
])
def test_usage_that_cannot_be_trusted_is_unknown(usage):
    assert reserve.read_usage({"inputTokens": 1, "outputTokens": 1}) is not None   # the partner
    assert reserve.read_usage(usage) is None


# ------------------------------------------------------------------- config

APPROVED = {"schema": "ws7-config/1", "monthly_cap_gbp_pence": 1500, "gbp_pence_per_usd": 80,
            "input_allowance_margin_percent": 100, "preflight_scenario": "S1",
            "wall_clock_limit_s": {"scheduled": 7200, "manual": 18000},
            "ledger_cas_attempts": 4, "max_start_delay_s": 3600}


def _write(tmp_path, data) -> Path:
    path = tmp_path / "config.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_the_repository_config_holds_the_authors_approved_values():
    """The author's rulings of 18 Nov 2026. A change here is a pull request."""
    assert json.loads(reserve.CONFIG_PATH.read_text(encoding="utf-8")) == APPROVED
    cfg = reserve.load_config()
    assert (cfg.input_allowance_margin_percent, cfg.preflight_scenario) == (100, "S1")
    assert (cfg.wall_clock_limit_scheduled_s, cfg.wall_clock_limit_manual_s) == (7200, 18000)
    assert (cfg.ledger_cas_attempts, cfg.max_start_delay_s) == (4, 3600)
    assert cfg.cap_usd_micros == 18_750_000                       # GBP 15 at 80 pence a dollar
    assert len(cfg.sha256) == 64


def test_the_pre_flight_scenario_is_in_the_corpus():
    corpus, _sha = rce.load_corpus(rce.DEFAULT_CORPUS_PATH)
    assert reserve.load_config().preflight_scenario in {s.id for s in corpus}


def test_what_the_ceiling_allows_matches_adr_011():
    cfg = reserve.load_config()
    res = reserve.reservation("pipeline", 19, cfg.input_allowance_margin_percent)
    headroom_micros = cfg.cap_usd_micros - res["total_usd_micros"]
    assert pence(headroom_micros * 1000) == 838        # a run starts while the month is GBP 8.38 or less


def test_the_cap_rounds_down(tmp_path):
    cfg = reserve.load_config(_write(tmp_path, APPROVED | {"gbp_pence_per_usd": 70}))
    assert cfg.cap_usd_micros == 21_428_571            # 21,428,571.43 is not rounded up


@pytest.mark.parametrize("change", [
    {"schema": "ws7-config/2"},
    {"extra": 1},
    {"monthly_cap_gbp_pence": 0},
    {"monthly_cap_gbp_pence": 1500.0},
    {"gbp_pence_per_usd": True},
    {"input_allowance_margin_percent": 99},
    {"input_allowance_margin_percent": "100"},
    {"preflight_scenario": "S 1"},
    {"preflight_scenario": 1},
    {"wall_clock_limit_s": 7200},
    {"wall_clock_limit_s": {"scheduled": 7200}},
    {"wall_clock_limit_s": {"scheduled": 7200, "manual": 0}},
    {"wall_clock_limit_s": {"scheduled": 7200, "manual": 18000, "other": 1}},
    {"ledger_cas_attempts": -1},
    {"max_start_delay_s": None},
])
def test_a_config_with_a_wrong_field_is_an_error_never_a_default(tmp_path, change):
    assert reserve.load_config(_write(tmp_path, APPROVED))        # the partner: unchanged, it loads
    with pytest.raises(reserve.ReserveError) as exc:
        reserve.load_config(_write(tmp_path, APPROVED | change))
    assert exc.value.code == "config_shape_invalid"


def test_a_config_with_a_missing_field_is_an_error(tmp_path):
    for field in APPROVED:
        with pytest.raises(reserve.ReserveError) as exc:
            reserve.load_config(_write(tmp_path, {k: v for k, v in APPROVED.items() if k != field}))
        assert exc.value.code == "config_shape_invalid"


def test_a_config_that_is_missing_unparseable_or_has_a_duplicate_key(tmp_path):
    with pytest.raises(reserve.ReserveError) as exc:
        reserve.load_config(tmp_path / "absent.json")
    assert exc.value.code == "config_unreadable"
    doubled = json.dumps(APPROVED)[:-1] + ', "ledger_cas_attempts": 9}'
    for text in ("{not json", doubled):
        with pytest.raises(reserve.ReserveError) as exc:
            reserve.load_config(_write(tmp_path, text))
        assert exc.value.code == "config_json_invalid"


def test_the_command_prints_numbers_and_fixed_labels_only(capsys):
    assert reserve.main() == 0
    out = capsys.readouterr().out
    assert "8280476 micro-USD (GBP 6.62; output GBP 5.34, input allowance GBP 1.29)" in out
    assert "2451884 micro-USD (GBP 1.96; output GBP 1.40, input allowance GBP 0.56)" in out
    assert "GBP 19.18" in out
