"""Safety-net gate — a hard regression check anchored to the SOURCE NOTES.

WHY THIS EXISTS
---------------
Prompt v0.6 forbade the patient version from adding clinical advice, and gave a
single fixed fall-back line for the case where none is documented:

    "If you become unwell or are worried about anything, contact your GP or call
     NHS 111. Call 999 if it is an emergency."

That line is safe precisely because it is *patient-independent*: it says nothing
about this patient, so it is not clinical advice. The moment it acquires a
condition — "if your phlegm changes", "if you have another seizure" — it becomes
condition-specific advice the clinician never gave.

The WS2a device-determination review (docs/WS2a-DEVICE-DETERMINATION.md §5.2)
found the model doing exactly that, and then found the real cause, which is one
layer higher than anyone had looked. v0.6 scoped its rule to PART C and defined
the boundary as "not in Part A" — so anything PART A contains is permitted
downstream. But PART A's own field template said "[Wound care, safety-net advice,
what to expect, when to seek help.]", which is an instruction to *author* advice.
So the model invented the safety-netting into PART A and PART C copied it
faithfully, exactly as told. Two verified cases:

  * S15 (COPD): the notes contain NO safety-netting at all, yet PART A produced
    "If breathlessness worsens, sputum changes, or you feel unwell, contact your
    GP or call NHS 111."
  * S18 (first seizure): the notes document activity restrictions (no swimming,
    heights, baths) but no re-presentation trigger, yet PART A produced "or have
    another seizure".

This is why the gate anchors to the NOTES and not to PART A. **PART A is model
output; it is not ground truth, and an earlier version of this gate that treated
it as ground truth reported the wrong answer on the real corpus.** For the same
reason the Patient v2 second pass cannot catch this class of error: its only
input is PART A.

It matters beyond patient safety. A fall-back that varies with the diagnosis is
on the determination memo's own list of changes that would move the tool inside
the medical-device definition (§6 item 3).

WHAT IT CHECKS
--------------
1. Do the notes document any seek-help / re-presentation trigger at all?
2. If NOT, then nothing downstream may contain one: PART A's advice field must
   carry no seek-help sentence, and PART C's only signposting must be the
   canonical fall-back line, compared after normalising case, whitespace, quote
   and dash characters, markdown emphasis and bullet/blockquote markers.
3. If the notes DO document one, the gate reports and does not fail — judging
   whether a rephrasing is faithful is a rubric job, not a regex one.

WHAT IT DOES **NOT** CATCH
--------------------------
It keys on urgency tokens (111 / 999 / A&E / emergency department), so invented
advice phrased without them is invisible to it — "go back to the hospital if your
breathing gets worse", a bulleted red-flag list, or a prognostic claim such as
"the cough can last 2 to 3 weeks". Those remain the eval rubric's job
(EVAL_RESULTS.md dimension D2, hallucination). This is a floor, not a proof.

Pure stdlib: no boto3, no network, importable from tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CANONICAL_FALLBACK = (
    "If you become unwell or are worried about anything, contact your GP or "
    "call NHS 111. Call 999 if it is an emergency."
)

# Sentences mentioning any of these are treated as urgent-help signposting.
#
# Deliberately URGENCY tokens only. "See your GP in two weeks" is routine
# follow-up, which PART C should carry when documented; sweeping it in made an
# earlier version of this gate fire on four scenarios behaving correctly. The
# bare word "emergency" is excluded too — a patient version legitimately says
# "you had an emergency operation".
_SIGNPOST = re.compile(
    r"(nhs\s*111|call\s*111|dial\s*111|\b999\b|\ba\s*&\s*e\b|"
    r"accident and emergency|emergency department)",
    re.I,
)

# Seek-help / re-presentation language in the SOURCE NOTES. Broad on purpose:
# a false "documented" only downgrades the gate to advisory, whereas a false
# "not documented" would fail a correct run and get the gate switched off.
# Note what is NOT here: "safety advice", "advised" and "DVLA" on their own.
# S18's notes say "Safety advice (no swimming alone, heights, baths)" — activity
# restrictions, not a trigger for seeking help — so that scenario correctly
# counts as documenting no trigger.
_NOTES_SEEK_HELP = re.compile(
    r"(safety[- ]?net|come back|return if|returns? if|re-?present|"
    r"seek (?:help|advice|medical|urgent|review)|red flag|\b111\b|\b999\b|"
    r"\ba\s*&\s*e\b|\bED\b|emergency department|attend (?:ed|a&e|hospital)|"
    r"call (?:the )?(?:ward|gp|us|surgery)|if (?:it |symptoms |things )?"
    r"(?:gets? )?worse|worsening advice|if unwell)",
    re.I,
)

_ADVICE_HEADING = re.compile(
    r"^[\s#*>\-]*((?:PATIENT|PARENT\s*/?\s*CARER|PARENT|CARER)\s+ADVICE"
    r"|SAFETY[- ]NET(?:TING)?(?:\s+ADVICE)?)\s*:?[ \t]*(?P<inline>.*)$",
    re.I | re.M,
)
# A following field label, whether or not it carries content on the same line
# ("VTE ASSESSMENT: Not documented" must terminate the block — an earlier
# version required the line to end after the colon, so the block ran to the end
# of PART A and picked up somebody else's "Not documented").
_NEXT_HEADING = re.compile(r"^[\s#*>\-]*[A-Z][A-Z &/'\-]{3,}\s*(?::|$)", re.M)

_NOT_DOCUMENTED = re.compile(
    r"^(not documented|none documented|none recorded|not recorded|none|nil|n/?a|"
    r"no specific safety[- ]net[a-z ]*|no safety[- ]net advice[a-z ]*)\b",
    re.I,
)

_PART_A = re.compile(r"^\s*(?:#+\s*)?\**\s*PART\s*A\b", re.I | re.M)
_PART_B = re.compile(r"^\s*(?:#+\s*)?\**\s*PART\s*B\b", re.I | re.M)
_PART_C = re.compile(r"^\s*(?:#+\s*)?\**\s*PART\s*C\b", re.I | re.M)

# 1-2 digits only. An unbounded \d+ treats the continuation of a soft-wrapped
# line as a new list item whenever the wrap lands on a number — "…call NHS\n
# 111. Call 999…" was read as list item 111, splitting the sentence in half and
# hiding an invented trigger from the gate.
_BULLET_START = re.compile(r"^\s*(?:[-*•>]|\d{1,2}[.)])\s+")

# The canonical fall-back is two sentences, and they are matched individually
# when checking PART A — a summary that carries only "Call 999 if it is an
# emergency." has still invented nothing.
_CANONICAL_SENTENCES = frozenset()   # populated below, once _normalise exists


def _normalise(text: str) -> str:
    """Lowercase, strip markdown/quote furniture, unify punctuation, collapse space."""
    t = text
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                 ("—", "-"), ("–", "-"), (" ", " ")):
        t = t.replace(a, b)
    t = re.sub(r"^[\s>*\-•#]+", "", t, flags=re.M)
    t = t.replace("**", "").replace("*", "").replace("`", "")
    t = re.sub(r"\s+", " ", t)
    # The patient-version prompt shows the fall-back line inside quotes, and the
    # model sometimes copies them through.
    return t.strip().strip('"').strip().lower()


_CANONICAL_SENTENCES = frozenset(
    _normalise(part) for part in re.split(r"(?<=[.!?])\s+", CANONICAL_FALLBACK)
    if part.strip()
)


def split_parts(combined: str) -> tuple[str, str]:
    """Return (part_a, part_c) from a combined PART A/B/C response."""
    a = _PART_A.search(combined)
    b = _PART_B.search(combined)
    c = _PART_C.search(combined)
    start_a = a.end() if a else 0
    end_a = b.start() if b else (c.start() if c else len(combined))
    part_a = combined[start_a:end_a] if end_a > start_a else ""
    part_c = combined[c.end():] if c else ""
    return part_a, part_c


def notes_document_seek_help(notes: str) -> bool:
    """True if the SOURCE NOTES record any seek-help / re-presentation trigger."""
    return bool(_NOTES_SEEK_HELP.search(notes or ""))


def advice_block(part_a: str) -> str:
    """The content of PART A's patient-advice field ('' if the field is absent)."""
    m = _ADVICE_HEADING.search(part_a)
    if not m:
        return ""
    block = (m.group("inline") or "").strip()
    rest = part_a[m.end():]
    nxt = _NEXT_HEADING.search(rest)
    tail = (rest[:nxt.start()] if nxt else rest).strip()
    return (block + "\n" + tail).strip() if block else tail


def advice_documented(part_a: str) -> bool:
    """True if PART A's advice field carries real content (not 'Not documented')."""
    block = advice_block(part_a)
    if not block:
        return False
    first = block.splitlines()[0].strip().lstrip("-*• ").strip()
    return not _NOT_DOCUMENTED.match(first)


def _is_decoration(line: str) -> bool:
    return not re.search(r"[A-Za-z0-9]", line)


def _is_heading(line: str) -> bool:
    """Short, un-punctuated line — a section heading, not wrapped prose.

    Model output uses bare headings with no markdown ("WHEN TO GET HELP", "If you
    are worried", "When to get help:"). Wrapped prose runs to ~70 characters and
    a dozen words, so "few words AND no terminal punctuation" separates the two.
    A line carrying a signpost token is never treated as a heading — dropping one
    would hide exactly the text the gate exists to inspect.
    """
    bare = line.strip().strip("*_`#").strip()
    if not bare or _SIGNPOST.search(bare):
        return False
    bare = bare.rstrip(":").strip()          # "When to get help:" is a heading
    if bare.endswith((".", "!", "?", ",", ";")):
        return False
    return len(bare.split()) <= 6


def _text_units(text: str) -> list[str]:
    """Rejoin soft-wrapped lines into logical units before sentence splitting.

    The model wraps at ~75 chars, so the canonical line routinely straddles two
    lines ("...contact your GP or\ncall NHS 111."). Splitting on newlines first
    would match only the second half and report a truncated sentence that can
    never equal the canonical text.
    """
    units: list[str] = []
    current: list[str] = []

    def flush():
        if current:
            units.append(" ".join(current).strip())
            current.clear()

    for line in (text or "").split("\n"):
        if not line.strip():
            flush()
            continue
        stripped = line.strip()
        if _is_decoration(stripped) or _is_heading(stripped):
            flush()
            continue
        if _BULLET_START.match(line) or stripped.startswith("#"):
            flush()
        current.append(stripped)
    flush()
    return [u for u in units if u]


def signpost_sentences(text: str) -> list[str]:
    """Sentences that signpost where to seek urgent help, de-duplicated in order."""
    out: list[str] = []
    seen: set[str] = set()
    for unit in _text_units(text):
        for piece in re.split(r"(?<=[.!?])\s+", unit):
            piece = piece.strip()
            if not piece or not _SIGNPOST.search(piece):
                continue
            key = _normalise(piece)
            if key in seen:
                continue
            seen.add(key)
            out.append(piece)
    return out


@dataclass
class GateResult:
    ok: bool
    status: str   # clean | added_advice | added_advice_part_a | missing_fallback | documented_advice
    findings: list[str] = field(default_factory=list)
    observed: str = ""

    @property
    def label(self) -> str:
        return "PASS (advisory)" if self.status == "documented_advice" else (
            "PASS" if self.ok else "FAIL")


def check(notes: str, part_a: str, part_c: str) -> GateResult:
    """Apply the gate to one generation. ``notes`` is the ground truth."""
    observed = " ".join(signpost_sentences(part_c))

    if notes_document_seek_help(notes):
        return GateResult(
            True, "documented_advice",
            ["The source notes record a seek-help trigger, so the fall-back rule is "
             "not engaged. Whether the rephrasing is faithful is a rubric judgement "
             "(EVAL_RESULTS.md D2), not checked here."],
            observed,
        )

    # From here on: the notes document no trigger, so nothing downstream may invent
    # one. The canonical fall-back line is exempt wherever it appears — it is
    # patient-independent and says nothing about this patient, which is the whole
    # reason it is the permitted default.
    invented_in_part_a = [x for x in signpost_sentences(advice_block(part_a))
                          if _normalise(x) not in _CANONICAL_SENTENCES]
    if invented_in_part_a:
        return GateResult(
            False, "added_advice_part_a",
            ["The source notes record no seek-help trigger, but PART A's patient-advice "
             "field contains one. This is where the failure originates: PART C copies "
             "PART A faithfully, exactly as the prompt tells it to, so an invention here "
             "reaches the patient as a documented instruction.",
             f"PART A: {' '.join(invented_in_part_a)}"],
            observed,
        )

    if not observed:
        return GateResult(
            False, "missing_fallback",
            ["The notes record no seek-help trigger, so PART C must carry the generic "
             "fall-back line. No signposting sentence was found at all."],
            observed,
        )

    if _normalise(observed) == _normalise(CANONICAL_FALLBACK):
        return GateResult(True, "clean", [], observed)

    return GateResult(
        False, "added_advice",
        ["The notes record no seek-help trigger, so PART C's only permitted "
         "signposting is the canonical fall-back line, verbatim.",
         f"expected: {CANONICAL_FALLBACK}",
         f"observed: {observed}"],
        observed,
    )


def check_combined(notes: str, combined: str) -> GateResult:
    """Convenience wrapper: split a combined PART A/B/C response, then check."""
    part_a, part_c = split_parts(combined)
    return check(notes, part_a, part_c)
