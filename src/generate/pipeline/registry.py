"""Step registry and run_step (ADR-009: "every step is a function with JSON in
and JSON out — run_step(name, input) -> output").

The explicit tuple below is the ADR's step table in code: one place lists every
step, its trace sequence number and whether it is code or a model call. Only the
orchestrator knows the order; the W4–W5 harness calls run_step on a fixture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .context import StepContext
from .errors import StepError
from .extract import extract_facts
from .guard import guard_input
from .validate import validate_facts

# The line-indexed notes travel under this key in step inputs and outputs. It is
# the one key the trace writer must never persist (ADR-009 (b), [A6]).
NOTES_KEY = "lines"


@dataclass(frozen=True)
class Step:
    name: str
    seq: str    # zero-padded, sorts in step order: "01", "02", "03", "03b", ... (trace SK)
    kind: str   # "code" or "model" (trace attribute)
    fn: Callable[..., dict]   # code: fn(input); model: fn(input, ctx)


_ALL_STEPS = (
    Step("guard_input", "01", "code", guard_input),
    Step("extract_facts", "02", "model", extract_facts),
    Step("validate_facts", "03", "code", validate_facts),
)

STEPS: dict[str, Step] = {s.name: s for s in _ALL_STEPS}


def _json_round_trip(value, code: str):
    # allow_nan=False: NaN/Infinity are not JSON, and a fixture file could never
    # contain them, so a step must not depend on them either.
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise StepError(code) from None


def run_step(name: str, input: dict, *, ctx: StepContext | None = None) -> dict:
    """Run one named step on a JSON-shaped input; return its JSON-shaped output.

    The round trips make "JSON in, JSON out" true rather than aspirational: a
    step sees exactly what it would see if its input came from a fixture file,
    and tuples/sets/datetimes are caught here, not in the W4 harness.

    `ctx` carries what a model step needs besides data (its Bedrock client);
    code steps never receive it, and a model step without it fails closed.
    """
    step = STEPS.get(name)
    if step is None:
        raise StepError("unknown_step")
    payload = _json_round_trip(input, "input_not_json")
    if step.kind == "model":
        if ctx is None:
            raise StepError("no_client")
        output = step.fn(payload, ctx)
    else:
        output = step.fn(payload)
    return _json_round_trip(output, "output_not_json")


def trace_view(output: dict) -> dict:
    """The part of a step output that may be written to a trace: everything but the notes."""
    return {k: v for k, v in output.items() if k != NOTES_KEY}
