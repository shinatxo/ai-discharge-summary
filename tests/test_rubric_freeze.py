"""The judge's rubric (EVAL_RESULTS.md §2) must match evals/judge/FROZEN.json.

Added 28 Oct 2026 (WS1a step build piece 2): piece 1 edited a §2 row, which
silently made run_judge.check_frozen refuse to run ("a new judge version").
Notes about pipeline scoring go OUTSIDE §2; a real rubric change is a new judge
version, frozen and calibrated on purpose."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import make_blind_sheets as mbs  # noqa: E402


def test_rubric_section_matches_the_frozen_judge():
    frozen = json.loads((ROOT / "evals" / "judge" / "FROZEN.json").read_text())
    assert hashlib.sha256(mbs.rubric_section().encode("utf-8")).hexdigest() == frozen["rubric_sha256"]
