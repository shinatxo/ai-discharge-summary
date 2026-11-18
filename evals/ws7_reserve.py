#!/usr/bin/env python3
"""WS7 reservation: what a run may cost at worst, and what a call did cost (ADR-011 D7).

    python evals/ws7_reserve.py        # prints the reservation tables; numbers only

Three things live here, and nothing else in WS7 holds a price or a token limit:

1. The plan for each target: its model steps, each step's `maxTokens`, and a
   provisional input allowance. Every figure is read from the runner or the
   pipeline module that owns it; none is copied. The file and line of each is
   in docs/WS7-ENTRYPOINT.md, and tests/test_ws7_reserve.py checks those lines.
2. The worst-case reservation: for every model step, `maxTokens` as output and
   one attempt, plus the input allowance times the author's margin, priced at
   the cache-write rate (the highest input rate).
3. The cost of one call from the usage Bedrock returned.

Money is integer nano-dollars inside WS7 (one dollar is 1,000,000,000). Every
rate is a whole number of nano-dollars per token, so the arithmetic is exact.
The ledger and the metrics use micro-dollars; `to_micros` rounds up.

No AWS call and no model call. Importing this module imports the runners and
the pipeline steps to read their constants; it changes nothing in them.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

_EVALS = Path(__file__).resolve().parent
REPO = _EVALS.parent
for _p in (str(REPO / "src" / "generate"), str(_EVALS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_cold_eval as rce  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402
from pipeline import compose, compose_patient, extract, reconcile  # noqa: E402

TARGETS = ("pipeline", "v1")
NANOS_PER_MICRO = 1_000
MICROS_PER_USD = 1_000_000

# v1's client settings. The v1 runner builds its default client with no Config
# (evals/run_cold_eval.py:684), so today it runs on botocore's defaults: 60 s to
# connect and 60 s to read. WS7 pins both and writes them to the run manifest.
V1_READ_TIMEOUT_S = 60
V1_CONNECT_TIMEOUT_S = 60
PIPELINE_CONNECT_TIMEOUT_S = 10      # as live_client, evals/run_pipeline_eval.py:174

# The provisional input allowance, per step, in tokens (ADR-011 D7). The runners
# hold one planning figure per generation; the split by step is the one their
# comments give. `plan()` refuses to build if the split stops adding up to the
# runner's own constant.
#   pipeline: evals/run_pipeline_eval.py:75-78 (the comment), :79 (EST_TOKENS)
#   v1:       evals/run_cold_eval.py:121-122 (EST_TOKENS, second pass off and on)
PIPELINE_INPUT_TOKENS = {"extract_facts": 6000, "reconcile_medications": 7000,
                         "compose_clinical": 5000, "compose_patient": 2500}
V1_STEPS = ("combined", "patient_v2")     # the v1 runner's own call names

CONFIG_SCHEMA = "ws7-config/1"
CONFIG_PATH = _EVALS / "ws7_config.json"
MARGIN_PERCENT_RANGE = (100, 1000)    # under 100 would reserve less than the planning figure
_SCENARIO_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


class ReserveError(Exception):
    """A fixed code. Nothing from a file or a request is kept."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ prices

def rates() -> dict:
    """Nano-dollars per token: the runners' price basis with the regional
    premium applied (evals/run_cold_eval.py:115 and :116)."""
    out = {}
    for kind in ("input", "output", "cache_write", "cache_read"):
        # $1 per million tokens is 1,000 nano-dollars per token.
        value = (Decimal(str(rce.PRICE_PER_MTOK[kind])) * Decimal(str(rce.REGIONAL_PREMIUM))
                 * 1_000)
        if value <= 0 or value != value.to_integral_value():
            raise ReserveError("price_not_whole_nanos")
        out[kind] = int(value)
    return out


def to_micros(nanos: int) -> int:
    """Micro-dollars, rounded up: a cost is never understated."""
    return -(-nanos // NANOS_PER_MICRO)


# -------------------------------------------------------------------- plan

@dataclass(frozen=True)
class Step:
    id: str
    max_tokens: int           # the output limit the step sends
    input_tokens: int         # the provisional input allowance, before the margin
    tool_name: str | None     # pipeline: the tool the step forces; v1: None


@dataclass(frozen=True)
class Plan:
    target: str
    model_id: str
    steps: tuple              # in the order one generation calls them
    read_timeout_s: float | None   # v1: fixed here; pipeline: the runner names it per client
    connect_timeout_s: float


def plan(target: str) -> Plan:
    """The model steps of one generation. Read when called, so a changed
    constant in a runner or a step is seen, never a stale copy."""
    if target == "pipeline":
        modules = {"extract_facts": extract, "reconcile_medications": reconcile,
                   "compose_clinical": compose, "compose_patient": compose_patient}
        if tuple(rpe.MODEL_STEPS) != tuple(modules) \
                or any(rpe.MODEL_STEPS[name] is not mod for name, mod in modules.items()):
            raise ReserveError("pipeline_steps_changed")
        if sum(PIPELINE_INPUT_TOKENS.values()) != rpe.EST_TOKENS["input"] \
                or set(PIPELINE_INPUT_TOKENS) != set(modules):
            raise ReserveError("input_allowance_split_stale")
        steps = tuple(Step(name, mod.MAX_TOKENS, PIPELINE_INPUT_TOKENS[name], mod.TOOL_NAME)
                      for name, mod in modules.items())
        out = Plan("pipeline", rpe.MODEL_ID, steps, None, PIPELINE_CONNECT_TIMEOUT_S)
    elif target == "v1":
        combined = rce.EST_TOKENS[False]["input"]
        second_pass = rce.EST_TOKENS[True]["input"] - combined
        steps = (Step(V1_STEPS[0], rce.MAX_TOKENS, combined, None),
                 Step(V1_STEPS[1], rce.PATIENT_MAX_TOKENS, second_pass, None))
        # A v1 request carries no tool, so its maxTokens is what names its step.
        if rce.MAX_TOKENS == rce.PATIENT_MAX_TOKENS:
            raise ReserveError("v1_steps_indistinct")
        out = Plan("v1", rce.MODEL_ID, steps, V1_READ_TIMEOUT_S, V1_CONNECT_TIMEOUT_S)
    else:
        raise ReserveError("target_unknown")
    for s in out.steps:
        if type(s.max_tokens) is not int or s.max_tokens <= 0 \
                or type(s.input_tokens) is not int or s.input_tokens <= 0:
            raise ReserveError("plan_figure_invalid")
    return out


# ------------------------------------------------------------- reservation

def _check_margin(margin_percent: int) -> None:
    low, high = MARGIN_PERCENT_RANGE
    if type(margin_percent) is not int or not low <= margin_percent <= high:
        raise ReserveError("margin_invalid")


def allowance_tokens(step: Step, margin_percent: int) -> int:
    """The step's input allowance with the margin, rounded up to a whole token."""
    _check_margin(margin_percent)
    return -(-step.input_tokens * margin_percent // 100)


def worst_case_nanos(step: Step, margin_percent: int) -> int:
    """One call at its worst: maxTokens of output, and the input allowance at
    the cache-write rate. Also what a call with no usable usage is counted at."""
    r = rates()
    return step.max_tokens * r["output"] + allowance_tokens(step, margin_percent) * r["cache_write"]


def reservation(target: str, generations: int, margin_percent: int) -> dict:
    """The worst case for `generations` generations, one attempt per step."""
    if type(generations) is not int or generations < 1:
        raise ReserveError("generations_invalid")
    p, r = plan(target), rates()
    per_step = [{"step": s.id, "max_tokens": s.max_tokens,
                 "input_allowance_tokens": allowance_tokens(s, margin_percent),
                 "worst_case_usd_nanos": worst_case_nanos(s, margin_percent)} for s in p.steps]
    output = sum(s.max_tokens for s in p.steps) * r["output"] * generations
    total = sum(row["worst_case_usd_nanos"] for row in per_step) * generations
    return {"target": target, "generations": generations, "margin_percent": margin_percent,
            "per_step": per_step,
            "per_generation_usd_nanos": total // generations,
            "output_part_usd_nanos": output,
            "input_part_usd_nanos": total - output,
            "total_usd_nanos": total,
            "total_usd_micros": to_micros(total)}


# ------------------------------------------------------------ cost of a call

_USAGE_FIELDS = (("inputTokens", "input", "input_tokens", True),
                 ("outputTokens", "output", "output_tokens", True),
                 ("cacheReadInputTokens", "cache_read", "cache_read_tokens", False),
                 ("cacheWriteInputTokens", "cache_write", "cache_write_tokens", False))


def read_usage(usage: object) -> dict | None:
    """Token counts and their cost, or None when the usage cannot be trusted:
    not an object, a count missing, or a count that is not a whole number of
    zero or more. The two cache counts may be absent; that is zero."""
    if type(usage) is not dict:
        return None
    r, out, cost = rates(), {}, 0
    for field, rate, name, required in _USAGE_FIELDS:
        value = usage.get(field)
        if value is None and not required:
            value = 0
        if type(value) is not int or value < 0:     # a bool is not a count
            return None
        out[name] = value
        cost += value * r[rate]
    out["cost_usd_nanos"] = cost
    return out


# ------------------------------------------------------------------ config

@dataclass(frozen=True)
class Config:
    monthly_cap_gbp_pence: int
    gbp_pence_per_usd: int
    input_allowance_margin_percent: int
    preflight_scenario: str
    wall_clock_limit_scheduled_s: int
    wall_clock_limit_manual_s: int
    ledger_cas_attempts: int
    max_start_delay_s: int
    sha256: str               # of the file's bytes, for the run manifest

    @property
    def cap_usd_micros(self) -> int:
        """The monthly cap in micro-dollars, rounded down: the cap errs low."""
        return self.monthly_cap_gbp_pence * MICROS_PER_USD // self.gbp_pence_per_usd


def _no_duplicate_keys(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def load_config(path: Path = CONFIG_PATH) -> Config:
    """The author's settings. Strict: an unknown or missing field, a wrong
    type or a duplicate key is an error, never a default."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise ReserveError("config_unreadable") from None
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        raise ReserveError("config_json_invalid") from None

    fields = {"schema", "monthly_cap_gbp_pence", "gbp_pence_per_usd",
              "input_allowance_margin_percent", "preflight_scenario", "wall_clock_limit_s",
              "ledger_cas_attempts", "max_start_delay_s"}
    if type(data) is not dict or set(data) != fields or data["schema"] != CONFIG_SCHEMA:
        raise ReserveError("config_shape_invalid")
    limits = data["wall_clock_limit_s"]
    if type(limits) is not dict or set(limits) != {"scheduled", "manual"} \
            or not all(_positive(v) for v in limits.values()):
        raise ReserveError("config_shape_invalid")
    if not all(_positive(data[k]) for k in ("monthly_cap_gbp_pence", "gbp_pence_per_usd",
                                            "ledger_cas_attempts", "max_start_delay_s")):
        raise ReserveError("config_shape_invalid")
    if type(data["preflight_scenario"]) is not str \
            or not _SCENARIO_RE.fullmatch(data["preflight_scenario"]):
        raise ReserveError("config_shape_invalid")
    try:
        _check_margin(data["input_allowance_margin_percent"])
    except ReserveError:
        raise ReserveError("config_shape_invalid") from None

    return Config(data["monthly_cap_gbp_pence"], data["gbp_pence_per_usd"],
                  data["input_allowance_margin_percent"], data["preflight_scenario"],
                  limits["scheduled"], limits["manual"], data["ledger_cas_attempts"],
                  data["max_start_delay_s"], hashlib.sha256(raw).hexdigest())


# --------------------------------------------------------------------- CLI

def _pounds(nanos: int, cfg: Config) -> str:
    pence = (nanos * cfg.gbp_pence_per_usd + 500_000_000) // 1_000_000_000
    return f"GBP {pence // 100}.{pence % 100:02d}"


def main() -> int:
    try:
        cfg = load_config()
        margin = cfg.input_allowance_margin_percent
        print(f"ws7_reserve: margin {margin}% | monthly cap {cfg.cap_usd_micros} micro-USD")
        for target in TARGETS:
            for label, generations in (("18 + 1 pre-flight", 19), ("18 x 3 + 1 pre-flight", 55)):
                res = reservation(target, generations, margin)
                print(f"{target:<8} {label:<22} {res['total_usd_micros']:>10} micro-USD "
                      f"({_pounds(res['total_usd_nanos'], cfg)}; output "
                      f"{_pounds(res['output_part_usd_nanos'], cfg)}, input allowance "
                      f"{_pounds(res['input_part_usd_nanos'], cfg)})")
            for row in reservation(target, 1, margin)["per_step"]:
                print(f"  {row['step']:<22} maxTokens {row['max_tokens']:>5}  input allowance "
                      f"{row['input_allowance_tokens']:>5}  worst case "
                      f"{row['worst_case_usd_nanos']:>11} nano-USD")
    except ReserveError as exc:
        print(f"ws7_reserve: error code={exc.code}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
