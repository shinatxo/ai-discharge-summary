"""Tests for evals/replay_orchestrate.py (WS1a step build piece 5, round 4): the
reconstructed step-2 input must re-validate to step 3's saved output, and one saved record
must replay through the orchestrator to a released bundle equal to piece 4's direct path."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import replay_orchestrate as RO  # noqa: E402
import run_cold_eval as rce  # noqa: E402
from pipeline import run_step  # noqa: E402

BATCH = ROOT / "evals" / "runs" / "w6-pipeline-e09-m01-c02-p04-x5"
pytestmark = pytest.mark.skipif(not BATCH.exists(), reason="saved batch not present")


def _record(name):
    rec = json.loads((BATCH / name).read_text())
    corpus, _ = rce.load_corpus(rce.DEFAULT_CORPUS_PATH)
    return rec, {s.id: s for s in corpus}[rec["scenario_id"]].notes


def test_reconstructed_wire_revalidates_to_the_saved_step3_output():
    rec, notes = _record("r1/A5.json")
    lines = run_step("guard_input", {"notes": notes})["lines"]
    wire = RO.reconstruct_wire(rec["steps"]["validate_facts"]["facts"], Counter())
    assert run_step("validate_facts", {"facts": wire, "lines": lines}) == rec["steps"]["validate_facts"]


def test_an_r3_record_differs_only_by_the_cleared_quote():
    rec, notes = _record("r2/S11.json")
    out = RO.replay_record(rec, notes)
    assert out["validate_vs_saved"] == "r3_only" and out["outcome"] == "bundle"
    assert out["trace"]["quotes_over_200"] == 0 and out["direct_8b_equal"] is True


def test_one_record_replays_end_to_end():
    rec, notes = _record("r1/S1.json")
    out = RO.replay_record(rec, notes)
    assert out["outcome"] == "bundle" and out["direct_8b_equal"] is True and out["trace_sks_ok"]
    assert out["trace"]["raw_index"] == 0 and not out["trace"]["notes_text_present"]


def test_notes_text_check_finds_multiline_notes_that_json_escaping_hides():
    notes = "72M\nCP\nDH: nil"
    planted = [{"SK": "META", "output": {"deep": ["x", "pre " + notes + " post"]}}]
    assert RO.notes_text_present(planted, notes) is True
    assert notes not in json.dumps(planted[0])          # the old check could not see it
    assert RO.notes_text_present([{"SK": "META", "output": {"deep": ["72M", "CP"]}}], notes) is False
