"""Step 7 meaning flags — does a PART C section say the same thing as the PART A
sections it cites? (WS1a step build, piece 3 follow-up 2; CSO decision G2 A,
3 Nov 2026; HAZ-17.)

Flag, never fix. Pure functions, fixed word lists, no model. Five kinds, each
measured against the text of the PART A sections the C section CITES (what step
7 was given, never the notes):

  cause      a cause / purpose / explanation phrase the cited text does not use
             ("caused by", "because", "to help", "which means" ...)
  certainty  a completion, certainty or grading word the cited text does not use
             ("fully", "recovered", "not damaged", "normal", "carefully" ...)
  qualifier  a qualifier or change word in the cited text with no equivalent in
             the C section ("improving", "initially", "possible", "treat as" ...);
             a diagnosis section's Background is not read for this kind
  test_term  a test / sample / scan word the cited text does not justify
             ("swab" for an aspirate; "scan" for an X-ray)
  diagnosis  a primary or secondary diagnosis line (before "Background") whose
             head phrase (the text before the first bracket, comma, semicolon,
             colon or dash) is not reached in step 7's prose: neither its last
             content word (the head noun) nor at least half its content words

Broad on purpose, as the other step-7 flags: a false flag costs one CSO ruling, a
miss is HAZ-17's path. The word lists and the two plain-word tables below (which
everyday words are justified by which clinical terms; which C words keep which
qualifier) were written on 3 Nov 2026 AFTER reading the p0.2 batch's flags and
BEFORE the p0.3 batch, and are approved by the CSO before that batch — they are
the pre-ruled part of bar line 14 (disclosed). Recall is not complete: plain-word
glosses ("adjust" for "uptitrate") are outside every list.

The step reports these as checks (8b shows them to the reviewing clinician —
item 4 carry-in); evals/score_patient.py computes them from the saved sections
with these same functions, so older batches are measured too.

v4 (9 Nov 2026, scorer v4 — decision S7 of 4 Nov as amended by Shina (author/CSO)
on 9 Nov, 2(a); D8 of the step 7 decision round 2; design adopted 9 Nov ~21:15;
committed before the item-3 R1 run). Three of S7's five items live here:
  - "damage" added to the certainty words (S16's "no sign of damage" was missed);
  - Hb added to the words that justify "blood test" (the only change that can
    REMOVE flags; under 2(a) each flag it removes on the p0.4 re-score must have
    been ruled acceptable at p0.4, or the entry is not adopted);
  - the dash, in the stricter form: a "label — content" diagnosis line is
    reached only when its head phrase (as in v3; head_words unchanged) AND every
    segment after a dash are reached, each by the same rule.
The other two (risk wording beyond the four forms; "will be told") are in
compose_patient.py's _SENSITIVE and _PROMISE.
"""

from __future__ import annotations

import math
import re

KINDS = ("cause", "certainty", "qualifier", "test_term", "diagnosis")
CHECK_OF = {"cause": "cause_added", "certainty": "certainty_added", "qualifier": "qualifier_dropped",
            "test_term": "test_term_added", "diagnosis": "diagnosis_unreached"}

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_BACKGROUND = re.compile(r"^\s*background\b", re.IGNORECASE | re.MULTILINE)

_CAUSE = re.compile(
    r"\b(?:caused by|causes|caused|cause|because|due to|as a result|led to|leads? to|from your|so that|"
    r"to help|to make sure|to keep|which means|this means|in case)\b", re.IGNORECASE)
# Pre-ruled (G2): "because" that states the reason for admission ("You came in because ...")
# links the admission to the presenting complaint the section gives.
_REASON_FRAME = re.compile(
    r"\b(?:came|come|were|was)\s+(?:in|into\s+hospital|admitted|brought\s+in)\s+because\b", re.IGNORECASE)

_CERTAINTY = re.compile(
    r"\b(?:fully|full|completely|complete|recovered|recovery|normal|normally|healed|gone|cured|damaged|"
    r"closed|permanent|permanently|temporary|temporarily|settled|safe|safely|careful|carefully|closely|"
    r"gently|successful|successfully|well|resolved|cleared|clear|fine|healthy|"
    r"damage)\b", re.IGNORECASE)                    # v4 (9 Nov 2026, S7): + damage

# Qualifier groups: (name, in the cited text, an equivalent that keeps it in C).
_QUALIFIERS = (
    ("improving", r"\bimprov\w*", r"\bimprov\w*|\bbetter\b"),
    ("initially", r"\binitial(?:ly)?\b|\bat first\b",
     r"\binitial(?:ly)?\b|\bat first\b|\bto (?:begin|start) with\b|\bfirst\b"),
    ("treated_as", r"\btreat(?:ed|ing)?\s+as\b", r"\btreat\w*"),
    ("possible", r"\bpossib(?:le|ly|ility)\b|\bprobabl[ey]\b|\blikely\b|\bsuspected\b|\bquery\b|\?|"
                 r"\bconcerning for\b|\bmay\b|\bmight\b",
     r"\bpossib\w*|\bprobabl\w*|\blikely\b|\bsuspect\w*|\bmay\b|\bmight\b|\bcould\b|\bthinks?\b|"
     r"\bnot (?:yet )?(?:sure|certain|confirmed)\b|\?"),
    ("partly", r"\bpartial(?:ly)?\b|\bpartly\b", r"\bpartial(?:ly)?\b|\bpartly\b|\bsome\b|\bnot (?:fully|completely)\b"),
    ("mild", r"\bmild(?:ly)?\b", r"\bmild\w*|\bslight\w*|\bsmall\b|\ba little\b"),
    ("until", r"\buntil\b", r"\buntil\b|\btill\b"),
    ("unless", r"\bunless\b", r"\bunless\b"),
    ("only_if", r"\bonly if\b", r"\bonly (?:if|when)\b"),
    ("for_now", r"\bfor now\b|\bfor the time being\b", r"\bfor (?:now|the time being|the moment)\b|\bat the moment\b"),
    ("ongoing", r"\bongoing\b", r"\bongoing\b|\bstill\b|\bcontinu\w*"),
    ("not_yet", r"\bnot yet\b|\bawaited\b|\bpending\b",
     r"\bnot yet\b|\bstill\b|\bawait\w*|\bpending\b|\bto come\b|\bbeing (?:processed|checked|tested)\b"),
)
_QUALIFIERS = tuple((n, re.compile(a, re.IGNORECASE), re.compile(b, re.IGNORECASE)) for n, a, b in _QUALIFIERS)

# Test / sample / scan words in C -> the clinical terms in the cited text that justify them.
_SAMPLE = r"\bsample|\bspecimen|\bswab|\baspirat|\bhistolog|\bpatholog|\bbiops|\bcultur|\bsent (?:off|to (?:the )?lab)"
_TEST_TERMS = (
    ("swab", r"\bswabs?\b", r"\bswab"),
    ("aspirate", r"\baspirates?\b", r"\baspirat"),
    ("sample", r"\bsamples?\b|\bspecimens?\b", _SAMPLE),
    ("biopsy", r"\bbiops(?:y|ies)\b", r"\bbiops"),
    ("culture", r"\bcultures?\b", r"\bcultur"),
    ("smear", r"\bsmears?\b", r"\bsmear"),
    ("scan", r"\bscans?\b|\bscanned\b",
     r"\bct\b|\bmri\b|\bultrasound|\buss\b|\bscan|\bimaging|\becho|\bangiogra|\bdoppler|\bpet\b"),
    ("x-ray", r"\bx-?rays?\b", r"x-?ray|\bcxr\b|\baxr\b|\bxr\b|radiograph"),
    ("ultrasound", r"\bultrasound\b", r"\bultrasound|\buss\b|\bdoppler|\becho"),
    ("mri", r"\bmri\b", r"\bmri\b"),
    ("ct", r"\bct\b", r"\bct\b"),
    ("camera test", r"\bcamera\b",
     r"\bogd\b|endoscop|colonoscop|cystoscop|bronchoscop|gastroscop|sigmoidoscop|\bscope|\bcamera"),
    ("heart trace", r"\bheart (?:trace|tracing)\b|\becg\b",
     r"\becg\b|heart trac|telemetry|\bholt[eo]r\b|\b24\s*-?\s*h(?:ou)?rs?\s+tape\b"),   # CSO 3 Nov: +24hr tape, Holter / Holtor
    ("blood test", r"\bblood tests?\b",
     r"\bblood|\bfbc\b|u&e|\bcrp\b|troponin|hba1c|\binr\b|lactate|\bgas\b|\bvbg\b|\babg\b|\blfts?\b|"
     r"\bculture|\bpanel\b|\btfts?\b|"                        # CSO 3 Nov: +TFTs
     r"\bhb\b"),                                              # v4 (9 Nov 2026, S7 as amended 2(a)): +Hb
    ("urine test", r"\burine tests?\b", r"\burin|dipstick|\bmsu\b"),
)
_TEST_TERMS = tuple((n, re.compile(a, re.IGNORECASE), re.compile(b, re.IGNORECASE)) for n, a, b in _TEST_TERMS)

_STOP = frozenset("""
a an and are as at be by for from has have in is it its of on or the to was were with without into
day days new left right primary secondary background requiring required managed following identified recent
initially noted treat treated possible query likely suspected rate controlled during admission not due per
""".split())


def stem(word: str) -> str:
    w = word.lower().strip("'-")
    for suf in ("ing", "ed", "ly", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def stems(text: str) -> set[str]:
    return {stem(w) for w in _WORD.findall(text)}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s.strip()]


def _without_background(heading: str, text: str) -> str:
    if heading != "diagnosis":
        return text
    m = _BACKGROUND.search(text)
    return text[: m.start()] if m else text


def _word_in(token: str, src_lower: str, src_stems: set[str]) -> bool:
    if " " in token:
        return re.search(rf"\b{re.escape(token)}\b", src_lower) is not None
    return stem(token) in src_stems


def section_flags(text: str, cited: list[dict]) -> list[tuple[str, str, str]]:
    """One C prose section against the PART A sections it cites ({heading, text}).
    Returns (kind, token, shown) — `shown` is the C sentence (cause, certainty,
    test_term) or "input sentence → C section" (qualifier), for the review file."""
    src = "\n".join(s["text"] for s in cited)
    src_lower, src_stems = src.lower(), stems(src)
    out: list[tuple[str, str, str]] = []
    for sent in sentences(text):
        for m in _CAUSE.finditer(sent):
            tok = m.group(0).lower()
            if tok == "because" and _REASON_FRAME.search(sent):
                continue
            if tok in ("cause", "causes", "caused"):
                ok = "caus" in src_stems or re.search(r"\bcaus", src_lower) is not None
            else:
                ok = _word_in(tok, src_lower, src_stems)
            if not ok:
                out.append(("cause", tok, sent))
        for m in _CERTAINTY.finditer(sent):
            tok = m.group(0).lower()
            if not _word_in(tok, src_lower, src_stems):
                out.append(("certainty", tok, sent))
        for name, in_c, justified_by in _TEST_TERMS:
            if in_c.search(sent) and not justified_by.search(src):
                out.append(("test_term", name, sent))
    qsrc = "\n".join(_without_background(s.get("heading", ""), s["text"]) for s in cited)
    for name, in_src, kept_by in _QUALIFIERS:
        m = in_src.search(qsrc)
        if m and not kept_by.search(text):
            where = next((x for x in sentences(qsrc) if in_src.search(x)), m.group(0))
            out.append(("qualifier", name, f"input: {where} → C: {' '.join(text.split())}"))
    seen, uniq = set(), []
    for f in out:                                   # one flag per (kind, token, shown)
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def diagnosis_lines(text: str) -> list[str]:
    """Primary / secondary diagnosis lines of a diagnosis section (Background excluded)."""
    body = _without_background("diagnosis", text)
    lines = []
    for raw in body.splitlines():
        line = re.sub(r"^\s*[-*•]\s*", "", raw).strip()
        line = re.sub(r"^(?:primary|secondary)\s*(?:diagnos[ie]s)?\s*:?\s*", "", line, flags=re.IGNORECASE).strip()
        if len(line) >= 3:
            lines.append(line)
    return lines


_HEAD_END = re.compile(r"\s*(?:[(;,:]|\s[-–—]\s|—)")
# v4 (9 Nov 2026, S7's dash item, the stricter form): the same dash _HEAD_END cuts at.
_DASH = re.compile(r"\s[-–—]\s|—")


def head_words(line: str) -> list[str]:
    """Content-word stems of a diagnosis line's head phrase, in order."""
    head = _HEAD_END.split(line, 1)[0]
    return [stem(w) for w in _WORD.findall(head) if (len(w) >= 3 or w.isupper()) and w.lower() not in _STOP]


def _reached(words: list[str], have: set[str]) -> bool:
    """A head phrase is reached by its last content word or by at least half its content words."""
    return words[-1] in have or len(set(words) & have) >= math.ceil(len(set(words)) / 2)


def diagnoses_unreached(given: list[dict], prose: str) -> list[tuple[str, int, str]]:
    """(section_id, line number from 1, line) for each diagnosis line of a GIVEN
    diagnosis section that step 7's prose does not reach: its head phrase (v3) is not
    reached, OR (v4, 9 Nov 2026, the stricter dash form) a segment after a dash is not
    reached (each segment judged by its own head phrase, the same rule). Never fewer
    lines than v3: the v3 test is one side of the conjunction."""
    have = stems(prose)
    out = []
    for s in given:
        if s["heading"] != "diagnosis":
            continue
        for i, line in enumerate(diagnosis_lines(s["text"]), 1):
            words = head_words(line)
            segments = [w for w in (head_words(seg) for seg in _DASH.split(line)[1:]) if w]
            if not words and not segments:
                continue
            reached = (not words or _reached(words, have)) and all(_reached(w, have) for w in segments)
            if not reached:
                out.append((s["section_id"], i, line))
    return out
