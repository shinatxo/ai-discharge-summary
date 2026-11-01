"""Flesch–Kincaid grade — rubric D5 (EVAL_RESULTS.md §2: Pass ≤ 8, Partial ≤ 10, Fail > 10).

Written 1 Nov 2026 (step build piece 3). Until now the repo had NO Flesch–Kincaid
code: EVAL_RESULTS lists it in the W4 instrument, but every FK number recorded so
far came from ad hoc runs ("formula + a vowel-group syllable heuristic", §6).
This module is that method, written down, pinned by hand-counted tests and frozen
with the step-7 scorer before its first live batch. Pure stdlib (textstat is not
in requirements-eval.txt, and its dictionary would make the number depend on a
download).

    grade = 0.39 × (words / sentences) + 11.8 × (syllables / words) − 15.59

Conventions (fixed — a change is a new D5 instrument, disclosed):
- A word is a run of letters (an apostrophe inside it is kept: "don't" is one
  word); hyphenated words count as their parts ("follow-up" = 2 words). A run of
  digits is one word of one syllable (numbers are rare in a leaflet; read aloud
  they are usually longer, so this errs toward a LOWER grade — stated).
- Sentences end at . ! ? followed by a space or the end ("12.5" is one word),
  and at a line break: a bullet line without a full stop is its own sentence
  (a leaflet's lists are read line by line). Units with no word are not
  sentences.
- Syllables: groups of vowels (a e i o u y) in the lower-cased word; a final
  silent "e" is dropped when the word has another group, except "-le" after a
  consonant ("table") and "-ee"/"-ye"; a final "-ed" not after t or d is one
  syllable less ("stayed", not "wanted"); at least 1.
- Empty text has no grade (None).
"""

from __future__ import annotations

import re

D5_PASS_MAX = 8.0
D5_PARTIAL_MAX = 10.0

_WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*|\d+(?:[.,]\d+)*")
_UNIT_SPLIT = re.compile(r"[.!?]+(?=\s|$)|\n+")   # "12.5" is not a sentence end
_VOWELS = re.compile(r"[aeiouy]+")


def syllables(word: str) -> int:
    w = word.lower().replace("’", "'").replace("'", "")
    if w.isdigit() or re.fullmatch(r"\d+(?:[.,]\d+)*", w):
        return 1
    n = len(_VOWELS.findall(w))
    if n > 1 and w.endswith("e") and not re.search(r"[^aeiouy]le$", w) and not w.endswith(("ee", "ye")):
        n -= 1
    elif n > 1 and w.endswith("ed") and not w.endswith(("ted", "ded")):
        n -= 1
    return max(1, n)


def words(text: str) -> list[str]:
    return _WORD.findall(text or "")


def sentences(text: str) -> list[str]:
    return [u for u in _UNIT_SPLIT.split(text or "") if words(u)]


def fk_grade(text: str) -> float | None:
    ws = words(text)
    ss = sentences(text)
    if not ws or not ss:
        return None
    syl = sum(syllables(w) for w in ws)
    return round(0.39 * (len(ws) / len(ss)) + 11.8 * (syl / len(ws)) - 15.59, 2)


def d5_verdict(grade: float | None) -> str | None:
    if grade is None:
        return None
    return "pass" if grade <= D5_PASS_MAX else "partial" if grade <= D5_PARTIAL_MAX else "fail"
