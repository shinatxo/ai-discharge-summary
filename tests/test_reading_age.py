"""evals/reading_age.py — Flesch–Kincaid grade for rubric D5, hand-counted cases
(written and frozen with the step-7 scorer, 1 Nov 2026)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import reading_age as RA  # noqa: E402


@pytest.mark.parametrize("word,n", [
    ("the", 1), ("cat", 1), ("hospital", 3), ("medicine", 3), ("table", 2), ("people", 2),
    ("were", 1), ("heart", 1), ("attack", 2), ("pressure", 2), ("infection", 3), ("free", 1),
    ("eye", 1), ("you're", 1), ("stayed", 1), ("wanted", 2), ("treated", 2), ("home", 1),
    ("tablets", 2), ("2", 1), ("12.5", 1), ("rhythm", 1)])
def test_syllables(word, n):
    assert RA.syllables(word) == n


def test_words_split_hyphens_and_keep_apostrophes():
    assert RA.words("A follow-up, don't worry - 6 weeks.") == ["A", "follow", "up", "don't", "worry", "6", "weeks"]


def test_sentences_end_at_punctuation_and_line_breaks():
    assert len(RA.sentences("One. Two!\n- Three\n\n...")) == 3


def test_hand_counted_grades():
    # 6 words, 6 syllables, 1 sentence: 0.39*6 + 11.8*1 - 15.59
    assert RA.fk_grade("The cat sat on the mat.") == -1.45
    # 15 words, 18 syllables, 3 sentences (the bullet line is one): 0.39*5 + 11.8*1.2 - 15.59 = 0.52
    assert RA.fk_grade("You had a heart attack. You stayed in hospital for three days.\n- Rest at home") == 0.52
    # 3 words, 4 syllables, 1 sentence: 1.17 + 15.7333 - 15.59
    assert RA.fk_grade("Take 2 tablets.") == 1.31


def test_empty_has_no_grade():
    assert RA.fk_grade("") is None and RA.fk_grade("...\n- \n") is None and RA.d5_verdict(None) is None


@pytest.mark.parametrize("g,v", [(8.0, "pass"), (8.01, "partial"), (10.0, "partial"), (10.01, "fail"), (-1, "pass")])
def test_d5_bands_are_the_rubrics(g, v):
    assert RA.d5_verdict(g) == v
