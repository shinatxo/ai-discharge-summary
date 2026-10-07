"""evals/score_meds.py — D4 medication reconciliation, the second step-level
metric (ADR-009 (e); EVAL_RESULTS.md D4; CSO decisions of 6 Oct 2026).

Synthetic notes only. Scenario IDs and gold drug names only in test names."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "tests"))

import gold as G  # noqa: E402
import score_batch as SB  # noqa: E402
import score_meds as M  # noqa: E402

RUNS = ROOT / "evals" / "runs"


def out_with(meds: str) -> str:
    return (f"PART A - CLINICIAN SUMMARY\nDIAGNOSIS: test\n\nMEDICATIONS ON DISCHARGE\n{meds}\n\n"
            f"ALLERGIES: NKDA\n\nPART B - GP LETTER\nDear GP\n\nPART C - PATIENT VERSION\nHello.\n")


def kinds(sid, meds, adjudications=None):
    r = M.score(M.view_from_v1(out_with(meds)), G.load(sid), adjudications)
    return r["verdict"], sorted((f["drug"] or "-", f["kind"]) for f in r["findings"])


S1_GOOD = """1. Aspirin 75 mg OD (continued)
2. Ticagrelor — dose and frequency not documented (NEW)
3. Bisoprolol 7.5 mg OD (INCREASED from 5 mg)
4. Isosorbide mononitrate 30 mg OD (NEW)
5. Ramipril 5 mg OD (continued)
6. Furosemide 40 mg OD (continued)
7. Metformin 1 g BD (continued)
8. Atorvastatin 80 mg ON (continued)"""


def test_s1_correct_reconciliation_passes():
    assert kinds("S1", S1_GOOD) == ("pass", [])


def test_s1_the_seed_golds_ticagrelor_dose_is_an_invention():
    meds = S1_GOOD.replace("Ticagrelor — dose and frequency not documented (NEW)", "Ticagrelor 90 mg BD (NEW)")
    assert kinds("S1", meds) == ("fail", [("ticagrelor", "dose_invented"), ("ticagrelor", "frequency_invented")])


def test_inpatient_only_drug_on_the_list_fails_unless_shown_stopped():
    assert kinds("S1", S1_GOOD + "\n9. Fondaparinux 2.5 mg OD (NEW)")[1] == [("fondaparinux", "must_not_appear")]
    assert kinds("S1", S1_GOOD + "\n9. Fondaparinux — STOPPED (inpatient only)") == ("pass", [])


def test_tolerant_dh_drug_dropped_fails_unless_the_section_says_confirm():
    dropped = S1_GOOD.replace("5. Ramipril 5 mg OD (continued)\n", "")
    assert kinds("S1", dropped)[1] == [("ramipril", "dropped")]
    assert kinds("S1", dropped + "\nOther regular medications: confirm against the TTO.") == ("pass", [])


def test_increase_without_the_old_dose_is_partial():
    meds = S1_GOOD.replace("(INCREASED from 5 mg)", "(INCREASED)")
    assert kinds("S1", meds) == ("partial", [("bisoprolol", "previous_dose_missing")])


def test_stopped_drug_shown_continuing_fails():
    meds = """- Apixaban 5 mg BD (NEW)
- Aspirin 75 mg OD (continued)
- Amlodipine 10 mg OD (continued)
- Atorvastatin 80 mg ON (NEW)
- Metformin 1 g BD (continued)"""
    assert ("aspirin", "stopped_shown_continuing") in kinds("S14", meds)[1]


def test_withheld_shown_as_stopped_fails():
    meds = """- Oral PPI high dose (NEW)
- Amoxicillin (NEW)
- Clarithromycin (NEW)
- Ibuprofen 400 mg TDS — STOPPED
- Paracetamol regular (changed from PRN)
- Amlodipine 5 mg OD (continued)
- Apixaban 5 mg BD — STOPPED"""
    # CSO, 7 Oct 2026: STOPPED for WITHHELD risks the drug never being restarted.
    assert kinds("S17", meds) == ("fail", [("apixaban", "withheld_shown_stopped")])


@pytest.mark.parametrize("warfarin,ok", [
    ("Warfarin 5 mg OD (INCREASED from 3 mg)", False),
    ("Warfarin — DISCREPANCY: 5 mg OD (doctor TTO) vs 4 mg OD (pharmacy); INCREASED from 3 mg — confirm", True),
])
def test_b6_conflict_must_be_flagged_never_resolved(warfarin, ok):
    meds = f"""- {warfarin}
- Bisoprolol 5 mg OD (INCREASED from 2.5 mg)
- Furosemide 40 mg OD (continued)
- Digoxin 125 mcg OD (NEW)"""
    verdict, found = kinds("B6", meds)
    assert (("warfarin", "conflict_resolved") not in found) is ok


def test_none_required():
    assert kinds("S4", "None.") == ("pass", [])
    assert ("-", "drugs_where_none_documented") in kinds("S4", "- Paracetamol 120 mg PRN")[1]


def test_s10_iron_is_strict_a_named_salt_is_not_the_documented_item():
    base = """- Enoxaparin prophylactic dose daily for 10 days (NEW)
- {iron}
- Paracetamol regular (NEW)
- Ibuprofen PRN (NEW)"""
    assert kinds("S10", base.format(iron="Oral iron — preparation and dose not documented (NEW)")) == ("pass", [])
    verdict, found = kinds("S10", base.format(iron="Ferrous sulfate 200 mg OD (NEW)"))
    assert verdict == "fail" and ("iron", "missing") in found


def test_unknown_entry_goes_to_review_then_follows_the_ruling():
    meds = S1_GOOD + "\n9. Lansoprazole 30 mg OD (NEW)"
    r = M.score(M.view_from_v1(out_with(meds)), G.load("S1"))
    assert r["verdict"] == "review"
    key = r["extras"][0]["key"]
    assert kinds("S1", meds, {key: {"verdict": "invented"}})[1] == [("-", "invented_drug")]
    assert kinds("S1", meds, {key: {"verdict": "acceptable"}}) == ("pass", [])


def test_an_entry_belongs_to_the_drug_it_names_first():
    meds = """- Oral PPI high dose (NEW)
- Amoxicillin (NEW)
- Clarithromycin (NEW)
- Paracetamol regular (NEW — replacing ibuprofen)
- Ibuprofen 400 mg TDS — STOPPED
- Amlodipine 5 mg OD (continued)
- Apixaban 5 mg BD — WITHHELD"""
    assert ("ibuprofen", "stopped_shown_continuing") not in kinds("S17", meds)[1]


def test_results_carry_no_output_text():
    r = M.score(M.view_from_v1(out_with(S1_GOOD + "\n9. Lansoprazole 30 mg OD")), G.load("S1"))
    assert "Lansoprazole" not in json.dumps(r)


# --- the parser on real v1 output ---------------------------------------------

@pytest.mark.parametrize("run,sid,verdict,found", [
    ("run-2026-09-15-discharge-summary-system-prompt-patient-v2", "S15", "fail", [("rescue pack", "missing")]),
    ("run-2026-09-15-discharge-summary-system-prompt-patient-v2", "S9", "partial", [("insulin", "tag_imprecise")]),
    ("run-2026-09-15-discharge-summary-system-prompt-patient-v2", "S8", "pass", []),
    ("run-2026-09-15-discharge-summary-system-prompt-patient-v2", "S18", "pass", []),
    ("run-2026-05-30-patient-v2", "S14", "pass", []),      # box-drawing table
    ("run-2026-05-30-patient-v2", "S16", "partial", [("analgesia", "tag_imprecise")]),
    ("run-2026-05-30-patient-v2", "S17", "partial", [("paracetamol", "tag_imprecise")]),   # markdown table
    ("run-2026-05-30-patient-v2", "S15", "fail", [("rescue pack", "missing")]),
])
def test_real_v1_outputs(run, sid, verdict, found):
    out = (RUNS / run / f"{sid}.md").read_text(encoding="utf-8").split("## OUTPUT", 1)[1]
    r = M.score(M.view_from_v1(out), G.load(sid))
    assert (r["verdict"], sorted((f["drug"] or "-", f["kind"]) for f in r["findings"])) == (verdict, found)


# --- step 5a's shape (ADR-009 step table), scored by the same rules ------------

S14_5A = [
    {"drug": "apixaban", "dose": "5mg", "route": None, "frequency": "BD", "tag": "new"},
    {"drug": "aspirin", "dose": "75mg", "route": None, "frequency": "OD", "tag": "stopped"},
    {"drug": "amlodipine", "dose": "10mg", "route": None, "frequency": "OD", "tag": "continued"},
    {"drug": "atorvastatin", "dose": "80mg", "route": None, "frequency": "ON", "tag": "new"},
    {"drug": "metformin", "dose": "1g", "route": None, "frequency": "BD", "tag": "continued"},
]


def test_5a_shape_scores_identically():
    assert M.score(M.view_from_5a(S14_5A, "listed"), G.load("S14"))["verdict"] == "pass"
    wrong = [dict(d, dose="40mg") if d["drug"] == "atorvastatin" else d for d in S14_5A]
    r = M.score(M.view_from_5a(wrong, "listed"), G.load("S14"))
    assert ("atorvastatin", "dose_wrong_or_missing") in [(f["drug"], f["kind"]) for f in r["findings"]]


# --- the batch ------------------------------------------------------------------

def test_batch_writes_d4_reports(tmp_path):
    import test_score_safety_net as T
    b, _ = T._batch(tmp_path, T._Fake())
    assert SB.main([str(b)]) == 0
    meds = json.loads((b / "scores" / "meds.json").read_text())
    assert len(meds["results"]) == 18
    # the fake output has no medication section at all
    assert all(f["kind"] == "section_missing" for r in meds["results"] for f in r["findings"][:1])
    assert (b / "scores" / "MEDS.md").exists() and (b / "scores" / "review_meds.md").exists()


def test_a_numbered_item_saying_confirm_is_still_a_drug_entry():
    meds = S1_GOOD.replace("5. Ramipril 5 mg OD (continued)",
                           "5. Ramipril 5 mg OD (continued) — confirm against the discharge prescription")
    assert kinds("S1", meds) == ("pass", [])
