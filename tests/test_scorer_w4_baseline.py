"""Scorer v2 regressions pinned to the real W4 baseline (evals/runs/w4-baseline-v1-x5,
generated 7 Oct 2026). Each case is a v1 output that scorer v1 misread; the
expected verdict was checked by hand against the output and the gold
(ADR-009 "Build record — W4"). Scenario IDs and run numbers only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import gold as G  # noqa: E402
import run_cold_eval as rce  # noqa: E402
import safety_net_gate as gate  # noqa: E402
import score_meds as M  # noqa: E402
import score_safety_net as S  # noqa: E402

BATCH = ROOT / "evals" / "runs" / "w4-baseline-v1-x5"
CORPUS = {s.id: s for s in rce.load_corpus(rce.DEFAULT_CORPUS_PATH)[0]}
pytestmark = pytest.mark.skipif(not BATCH.exists(), reason="W4 baseline batch not present")


def _out(sid, run):
    return json.loads((BATCH / f"r{run}" / f"{sid}.json").read_text(encoding="utf-8"))["output"]


def _meds(sid, run):
    r = M.score(M.view_from_v1(_out(sid, run)), G.load(sid))
    return r["verdict"], sorted({(f["drug"] or "-", f["kind"]) for f in r["findings"]})


@pytest.mark.parametrize("sid,run,verdict,found,why", [
    ("S13", 1, "review", [], "indented list with no markers: four entries, not one"),
    ("S16", 1, "pass", [], "column table, wrapped cells continue their row"),
    ("A5", 5, "review", [], "an indented ⚠ note inside item 2 no longer swallows items 3-6"),
    ("B6", 3, "pass", [], "'Pre-admission dose: 3 mg' inside the warfarin entry keeps the conflict"),
    ("B6", 5, "pass", [], "a warning block naming both doses flags the conflict"),
    ("S10", 2, "fail", [("enoxaparin", "frequency_wrong_or_missing")], "'Hb 84 g/L' is not an iron dose"),
    ("S10", 3, "pass", [], "'*** PRESCRIBER NOTE:' does not end the section"),
    ("S1", 2, "fail", [("fondaparinux", "must_not_appear")], "'OD/BD not documented' is not an invented frequency"),
    ("S8", 3, "pass", [], "'Discharge medications: None documented.' is a statement"),
    ("S8", 5, "pass", [], "'COURSE COMPLETED' antibiotics are not a reconstruction"),
    ("S18", 3, "pass", [], "'No pre-admission medications' is a statement"),
    ("C7", 4, "pass", [], "a lower-case continuation joins the statement"),
    ("S12", 1, "review", [], "'Medications prescribed during admission:' is not a discharge list"),
    ("S17", 4, "pass", [], "an indented ⚠ inside item 4 no longer swallows amlodipine"),
    ("S9", 1, "partial", [("insulin", "tag_imprecise")], "an indented list after a skipped line is kept"),
    ("S16", 2, "fail", [("warfarin", "frequency_invented")], "box table without inner dividers; 'OD' is not in the notes"),
])
def test_meds_scorer_v2_on_the_baseline(sid, run, verdict, found, why):
    assert _meds(sid, run) == (verdict, found), why


@pytest.mark.parametrize("sid,run,verdict,failures,route,why", [
    ("S10", 2, "review", [], "documented", "short bullets under a lead-in are advice, not headings"),
    ("S4", 2, "pass", [], "documented", "'PATIENT / PARENT / CARER ADVICE' heading is found"),
    ("S4", 5, "fail", ["invented_trigger"], "documented", "the quoted record splits from the note after it"),
    ("S16", 3, "pass", [], "fallback", "a bracketed 'fall-back applies' note is not advice"),
    ("S3", 5, "review", [], "documented", "advice with its own '(not transcribed)' aside is still advice"),
    ("S9", 1, "fail", ["route"], "fallback", "v0.7 calls the education record 'Not documented'"),
    ("S11", 1, "fail", ["route"], "fallback", "v1 wrote 'Not documented' over documented advice"),
])
def test_safety_net_scorer_v2_on_the_baseline(sid, run, verdict, failures, route, why):
    s = CORPUS[sid]
    r = S.score(S.view_from_v1(_out(sid, run)), G.load(sid), G.line_index(s.notes))
    assert (r["verdict"], r["failures"], r["route"]["predicted"]) == (verdict, failures, route), why


def test_gate_finds_the_patient_parent_carer_advice_heading():
    part_a = "PART A\nPATIENT / PARENT / CARER ADVICE\nIf the wheeze returns, call 999.\n\nVTE ASSESSMENT\nN/A\n"
    assert "call 999" in gate.advice_block(part_a)
    assert gate.check("No advice.", part_a, gate.V1_CANONICAL, gate.V1_LINES).status == "added_advice_part_a"


def test_gate_advice_block_stops_at_a_bold_heading_and_finds_the_ampersand_heading():
    part_a = ("PATIENT / PARENT & CARER ADVICE\nNo swimming alone.\n\n**VTE ASSESSMENT**\n"
              "Not applicable.\n\n**Author:** [Name]\n")
    assert gate.advice_block(part_a) == "No swimming alone."


def test_gate_advice_block_does_not_stop_at_a_mixed_case_label():
    part_a = 'PATIENT ADVICE\nNotes record: "Advice given."\nCall 999 if worse.\n\nVTE ASSESSMENT\nN/A\n'
    assert "Call 999" in gate.advice_block(part_a)
