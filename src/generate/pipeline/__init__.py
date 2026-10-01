"""The ADR-009 agentic pipeline, packaged inside the worker (CodeUri src/generate).

Rule for every module in this package: nothing at import time but definitions.
No AWS clients here — model steps will receive their Bedrock client as an
argument — because the test loaders re-import the worker per test and a
module-level client would be created once, under whichever patch was active.
"""

from .context import StepContext
from .errors import StepError
from .registry import NOTES_KEY, STEPS, run_step, trace_view

__all__ = ["NOTES_KEY", "STEPS", "StepContext", "StepError", "run_step", "trace_view"]
