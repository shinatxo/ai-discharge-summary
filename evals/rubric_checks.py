"""Rubric v2 deterministic checks — the layer that runs before the judge (W5).

    EVAL_RESULTS.md section 2, "The instrument (v2)". Four checks on v1's output:

  added_year     D2  DECIDES Fail   a 4-digit year (1900-2099) the notes never contain
  non_english    D2  DECIDES Fail   a PART C sentence in another language (the tool
                     / FLAGS          never translates); one weak signal only -> flag
  allergy        D7  DECIDES        NKDA shown as an allergy, or a documented allergy
                     AUTO-FAIL        shown as NKDA, in any A/B/C allergy statement
  laterality     D7  FLAGS          a side in the output the notes never give; the
                                      judge rules (structure matching is not code's job)

A check only ever decides a FAILURE. No finding means "no decision" — the
dimension goes to the judge — never "pass". Like the W4 scorers, results carry
labels, parts, character spans into the output and short fixed tokens (a year, a
side) — never note text or output sentences; a report renders spans for review.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_cold_eval as rce  # noqa: E402

CHECKS_VERSION = 1

# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------
def split_parts(output: str) -> dict[str, tuple[int, int]]:
    """Character ranges of PART A, B and C in a v1 output (the worker's markers).
    Text before PART A (a preamble) belongs to no part. Missing parts are absent."""
    starts = sorted((m.start(), label) for label in ("A", "B", "C")
                    for m in [rce._PART_MARKER[label].search(output)] if m)
    out = {}
    for i, (start, label) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(output)
        out[label] = (start, end)
    return out


def _part_of(pos: int, parts: dict) -> str:
    for label, (s, e) in parts.items():
        if s <= pos < e:
            return label
    return "preamble"


def _finding(check: str, reason: str, start: int, end: int, parts: dict, token: str | None = None) -> dict:
    f = {"check": check, "reason": reason, "part": _part_of(start, parts), "span": [start, end]}
    if token is not None:
        f["token"] = token
    return f


# ---------------------------------------------------------------------------
# D2 — added year
# ---------------------------------------------------------------------------
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _in_digit_run(text: str, start: int, end: int) -> bool:
    """'0800 2000 123' — a phone or reference number, not a year."""
    before, after = text[max(0, start - 2):start], text[end:end + 2]
    return bool(re.fullmatch(r"\d[\s-]", before) or re.fullmatch(r"[\s-]\d", after))


def added_years(notes: str, output: str, parts: dict) -> list[dict]:
    in_notes = set(_YEAR.findall(notes))
    found = []
    for m in _YEAR.finditer(output):
        if m.group() in in_notes or _in_digit_run(output, m.start(), m.end()):
            continue
        found.append(_finding("added_year", "year_not_in_notes", m.start(), m.end(), parts, m.group()))
    return found


# ---------------------------------------------------------------------------
# D2 — non-English text in PART C
# ---------------------------------------------------------------------------
# Letters English never uses. Covers the languages most often met on UK wards.
_FOREIGN_LETTERS = set("ąćęłńśźżčďěňřšťůžőűğışãõñåøæ")
# Function words that are unambiguous (no English homograph). Polish first —
# the corpus case (C7) — then Romanian, Portuguese, Lithuanian.
_FOREIGN_WORDS = {
    "się", "jest", "nie", "oraz", "jeśli", "jezeli", "jeżeli", "lub", "aby", "proszę",
    "że", "przez", "może", "należy", "dla", "pani", "lekarza", "lekarz", "szpitala",
    "și", "este", "pentru", "medicul", "você", "não", "médico", "jūs", "gydytoją",
}
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def non_english(notes: str, output: str, parts: dict) -> list[dict]:
    if "C" not in parts:
        return []
    note_words = {w.lower() for w in _WORD.findall(notes)}
    s0, e0 = parts["C"]
    found = []
    for m in _SENTENCE.finditer(output, s0, e0):
        words = [w.lower() for w in _WORD.findall(m.group())]
        # a word with a non-English letter that is not in the notes (a name in the
        # notes is a proper name, not a translation)
        letters = any(set(w) & _FOREIGN_LETTERS for w in words if w not in note_words)
        hits = {w for w in words if w in _FOREIGN_WORDS and w not in note_words}
        if letters or len(hits) >= 2:
            found.append(_finding("non_english", "non_english_sentence", m.start(), m.end(), parts))
        elif len(hits) == 1:
            found.append(_finding("non_english", "possible_non_english", m.start(), m.end(), parts))
    return found


# ---------------------------------------------------------------------------
# D7 — allergy polarity
# ---------------------------------------------------------------------------
_NKDA = re.compile(r"\bNKDA\b|\b(?:no|not|don't|do\s+not)\b[^.\n]{0,20}?\bknown\s+(?:drug\s+)?allerg"
                   r"|\bno\s+allergies\b|\bnot\s+allergic\b|\bno\s+drug\s+allerg", re.I)
_ALLERGY_WORD = re.compile(r"allerg|\bNKDA\b", re.I)
_ALLERGEN = re.compile(r"\b([a-z][a-z-]{3,})\s+allerg|allergic\s+to\s+([a-z][a-z-]{3,})"
                       r"|allergy\s+to\s+([a-z][a-z-]{3,})", re.I)
_NOT_ALLERGENS = {"drug", "known", "allergies", "allergy", "food", "documented", "with",
                  "nkda", "previous", "patient", "reported", "noted", "recorded", "check",
                  "confirm", "review", "your", "their", "status", "record", "records",
                  "document", "update", "list", "verify", "other", "new", "any", "about"}
_UNDOCUMENTED = re.compile(r"not\s+documented|not\s+recorded|unknown|not\s+stated|to\s+be\s+confirmed", re.I)
_HEADING_ONLY = re.compile(r"^[\s#*>|-]*allerg(?:y|ies)(?:\s*status)?[\s:*|-]*$", re.I)
_CONFLICT = re.compile(r"conflict|discrepan|inconsisten|contradict|differ", re.I)


def named_allergens(text: str) -> set[str]:
    """Words naming an allergen: 'penicillin allergy', 'allergic to latex'."""
    words = set()
    for m in _ALLERGEN.finditer(text):
        word = next(g for g in m.groups() if g).lower()
        if word not in _NOT_ALLERGENS:
            words.add(word)
    return words


def allergy_status(notes: str) -> dict:
    """{'nkda': bool, 'allergens': set} from the notes."""
    allergens = set()
    for line in notes.splitlines():
        if _ALLERGY_WORD.search(line):
            allergens |= named_allergens(line)
    return {"nkda": bool(_NKDA.search(notes)), "allergens": allergens}


def _statements(output: str):
    """(start, end, text) for every output line about allergies; a bare heading
    ('ALLERGIES') is joined to the next non-empty line."""
    lines, pos = [], 0
    for raw in output.splitlines(keepends=True):
        lines.append((pos, pos + len(raw.rstrip("\n")), raw.rstrip("\n")))
        pos += len(raw)
    i = 0
    while i < len(lines):
        s, e, t = lines[i]
        if _ALLERGY_WORD.search(t):
            if _HEADING_ONLY.match(t):
                j = i + 1
                while j < len(lines) and not lines[j][2].strip():
                    j += 1
                if j < len(lines):
                    yield s, lines[j][1], t + " " + lines[j][2]
                    i = j + 1
                    continue
            yield s, e, t
        i += 1


def allergy(notes: str, output: str, parts: dict) -> list[dict]:
    st = allergy_status(notes)
    if not st["nkda"] and not st["allergens"]:
        return []
    found = []
    for s, e, text in _statements(output):
        low = text.lower()
        says_nkda = bool(_NKDA.search(text))
        names = {a for a in st["allergens"] if a in low}
        if st["allergens"] and says_nkda and not names and not _CONFLICT.search(text):
            found.append(_finding("allergy", "documented_allergy_shown_as_nkda", s, e, parts))
        elif (not st["allergens"] and st["nkda"] and not says_nkda
              and not _UNDOCUMENTED.search(text) and named_allergens(text)):
            found.append(_finding("allergy", "nkda_shown_as_allergy", s, e, parts))
    return found


# ---------------------------------------------------------------------------
# D7 — laterality (flags only)
# ---------------------------------------------------------------------------
_SIDE_WORD = re.compile(r"\b(left|right)\b", re.I)
_SIDE_ABBR = re.compile(r"(?<![\w/])([LR])(?=\s+(?:\d|[a-z]))")   # "L 7-9 rib", not "L1"
_NOT_A_SIDE = re.compile(r"\bright\s+(?:away|now|time|thing|to\b|of\b)|\ball\s+right\b"
                         r"|\bleft\s+(?:the|a|an|it|them|you|him|her|over|out|behind|home|hospital|ward)\b",
                         re.I)


def _sides(text: str) -> set[str]:
    sides = set()
    for m in _SIDE_WORD.finditer(text):
        if not _NOT_A_SIDE.match(text, max(0, m.start() - 4)) and not _NOT_A_SIDE.match(text, m.start()):
            sides.add(m.group(1).lower())
    for m in _SIDE_ABBR.finditer(text):
        sides.add("left" if m.group(1) == "L" else "right")
    return sides


def laterality(notes: str, output: str, parts: dict) -> list[dict]:
    note_sides = _sides(notes)
    if note_sides >= {"left", "right"}:
        return []                          # both sides documented: only the judge can tell
    found = []
    for m in _SIDE_WORD.finditer(output):
        if _NOT_A_SIDE.match(output, max(0, m.start() - 4)) or _NOT_A_SIDE.match(output, m.start()):
            continue
        side = m.group(1).lower()
        if side not in note_sides:
            found.append(_finding("laterality", "side_not_in_notes", m.start(), m.end(), parts, side))
    return found


# ---------------------------------------------------------------------------
# All four, as verdict inputs
# ---------------------------------------------------------------------------
def run_checks(notes: str, output: str) -> dict:
    """{'checks_version', 'D2': {...}, 'D7': {...}, 'flags': [...]}. `verdict` is
    'fail' when a deciding check fired, else None (no decision — the judge rules)."""
    parts = split_parts(output)
    years = added_years(notes, output, parts)
    lang = non_english(notes, output, parts)
    allergies = allergy(notes, output, parts)
    sides = laterality(notes, output, parts)
    d2_fail = years + [f for f in lang if f["reason"] == "non_english_sentence"]
    return {
        "checks_version": CHECKS_VERSION,
        "parts_found": sorted(parts),
        "D2": {"verdict": "fail" if d2_fail else None, "auto_fail": False, "findings": d2_fail},
        "D7": {"verdict": "fail" if allergies else None, "auto_fail": bool(allergies),
               "findings": allergies},
        "flags": [f for f in lang if f["reason"] == "possible_non_english"] + sides,
    }
