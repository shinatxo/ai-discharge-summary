"""The one exception type every pipeline step raises (ADR-009).

A step that cannot produce a trustworthy output raises StepError with a fixed
code. The orchestrator (W2 part 2) copies `code` into the audit row's
`error_code` and the trace's `failed_step` — so a code is always a fixed word
from the step's own vocabulary, NEVER text taken from the notes. CloudWatch and
the audit row may see codes; they must never see note content.
"""

from __future__ import annotations


class StepError(Exception):
    """A fail-closed step failure. `code` is a fixed identifier, e.g. "empty_notes"."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
