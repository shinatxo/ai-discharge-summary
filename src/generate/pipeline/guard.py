"""Step 1 — guard_input (ADR-009, step table row 1; HAZ-08).

Turns the raw notes into a line index (L001, L002, ...) that lives in memory
only, and flags lines that look like instructions aimed at the model.

What may be traced from this step: notes_sha256, line_count and the flags —
and a flag is a line ID plus a fixed rule name, never the matched text. The
line index itself travels under the reserved key "lines", which
registry.trace_view() strips.

The flags are for the clinician, not a block: HAZ-08's residual rests on the
pipeline having no action surface, so a false negative here only degrades a
warning (ADR-009, open question 2).
"""

from __future__ import annotations

import hashlib
import re

from .errors import StepError

MAX_LINES = 999  # "L" + 3 digits. Beyond this, fail closed rather than emit L1000 (which sorts before L200).

# Rule name -> pattern. Names are a fixed vocabulary (safe to trace); patterns
# are case-insensitive. Each is tested against every scenario in
# src/canary/scenarios.json for false positives — a flag that fires on routine
# notes gets ignored within a week.
RULES: dict[str, re.Pattern] = {
    # "Ignore all previous instructions", "disregard the template"
    "override_instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}?"
        r"\b(?:instructions?|prompts?|rules|template)\b",
        re.IGNORECASE,
    ),
    # "SYSTEM INSTRUCTION TO THE AI", "you are now an assistant"
    "role_claim": re.compile(
        r"\b(?:system|developer)\s+(?:instructions?|prompts?|messages?|override)\b"
        r"|\byou\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|assistant|language\s+model|chatbot)\b",
        re.IGNORECASE,
    ),
    # "Do NOT mention ...", "Just reply ...", "and nothing else"
    # Deliberately not "only responds" (GCS wording) — the \b after the verb stops it.
    "output_control": re.compile(
        r"\b(?:do\s+not|don't|never)\s+(?:mention|include|output|say|write|report)\b"
        r"|\b(?:just|only)\s+(?:reply|respond|output|say|write)\b"
        r"|\breply\s+(?:only\s+)?with\b"
        r"|\band\s+nothing\s+else\b",
        re.IGNORECASE,
    ),
    # ">>> ... <<<", code fences, <system>-style tags. Never a lone ">" — "sats >94%".
    "delimiter_markup": re.compile(
        r">>>|<<<|```|</?\s*(?:system|instructions?|prompt|assistant|user)\s*>",
        re.IGNORECASE,
    ),
}


def _line_id(n: int) -> str:
    return f"L{n:03d}"


def guard_input(inp: dict) -> dict:
    """{"notes": str} -> {"lines", "notes_sha256", "line_count", "flags"}."""
    notes = inp.get("notes") if isinstance(inp, dict) else None
    if not isinstance(notes, str):
        raise StepError("bad_input")

    # Hash the exact string received, before anything else touches it: it must
    # equal the dispatcher's input_sha256, which is computed on the same string.
    notes_sha256 = hashlib.sha256(notes.encode("utf-8")).hexdigest()

    if not notes.strip():
        raise StepError("empty_notes")

    # splitlines(): \n, \r\n and \r all end a line, and a trailing newline does
    # not create an empty last line. Every physical line gets an ID, blanks
    # included, so L012 is the twelfth line a clinician would count.
    raw_lines = notes.splitlines()
    if len(raw_lines) > MAX_LINES:
        raise StepError("too_many_lines")

    lines = {_line_id(i): text for i, text in enumerate(raw_lines, start=1)}
    ids = list(lines)

    found: set[tuple[str, str]] = set()

    # Pass 1 — each line on its own.
    for line_id in ids:
        for rule, pattern in RULES.items():
            if pattern.search(lines[line_id]):
                found.add((line_id, rule))

    # Pass 2 — adjacent pairs joined with one space, keeping only matches that
    # CROSS the join. That catches "Ignore all previous" / "instructions" split
    # by a line wrap, without dragging a blank neighbour into a flag that pass 1
    # already raised.
    for a, b in zip(ids, ids[1:]):
        left = lines[a]
        joined = left + " " + lines[b]
        boundary = len(left)
        for rule, pattern in RULES.items():
            for m in pattern.finditer(joined):
                if m.start() < boundary and m.end() > boundary + 1:
                    found.add((a, rule))
                    found.add((b, rule))

    flags = [{"line": line_id, "rule": rule} for line_id, rule in sorted(found)]

    return {
        "lines": lines,
        "notes_sha256": notes_sha256,
        "line_count": len(ids),
        "flags": flags,
    }
