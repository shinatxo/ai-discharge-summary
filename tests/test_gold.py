"""evals/gold.py and evals/gold/*.json — the gold records for the step-level
metrics (ADR-009 (e), "Build record — W4"). Scenario IDs only in names and
messages."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import gold as G  # noqa: E402
import run_cold_eval as rce  # noqa: E402

CORPUS = {s.id: s for s in rce.load_corpus(rce.DEFAULT_CORPUS_PATH)[0]}
PRESENT = sorted(p.stem for p in G.GOLD_DIR.glob("*.json"))


def _errors(gold, sid="S1"):
    s = CORPUS[sid]
    return G.validate(gold, s.notes, s.notes_sha256)


@pytest.mark.parametrize("sid", PRESENT)
def test_every_gold_record_validates(sid):
    s = CORPUS[sid]
    errors = G.validate(G.load(sid), s.notes, s.notes_sha256)
    assert errors == []


def test_every_scenario_has_gold():
    assert sorted(CORPUS) == PRESENT


def test_gold_files_are_named_for_corpus_scenarios():
    assert set(PRESENT) <= set(CORPUS)


# --- the validator catches what it must (mutations of S1) ---------------------

BASE = G.load("S1")


def _mut(fn):
    g = copy.deepcopy(BASE)
    fn(g)
    return _errors(g)


def _drug(g, name):
    return next(d for d in g["medications"]["drugs"] if d["drug"] == name)


@pytest.mark.parametrize("label,fn,expect", [
    ("paraphrased quote", lambda g: _drug(g, "bisoprolol")["dh_cites"][0].update(quote="bisoprolol 5 mg OD"),
     "quote_not_found"),
    ("wrong line", lambda g: _drug(g, "bisoprolol")["dh_cites"][0].update(lines=["L004"]), "quote_not_found"),
    ("out of range", lambda g: g["age_group"]["cites"][0].update(lines=["L099"]), "line_out_of_range"),
    ("route without advice", lambda g: g["safety_net"].update(route="documented"), "if and only if"),
    ("bad tag", lambda g: _drug(g, "aspirin").update(tag="restarted"), "tag: must be one of"),
    ("increase without previous", lambda g: _drug(g, "bisoprolol").update(previous=None), "previous"),
    ("previous on continued", lambda g: _drug(g, "aspirin").update(previous={"dose": "75mg", "frequency": "OD"}),
     "null unless"),
    ("inferred non-continued", lambda g: _drug(g, "ramipril").update(tag="stopped"), "only a 'continued'"),
    ("documented without evidence", lambda g: _drug(g, "aspirin").update(discharge_cites=[]),
     "at least one citation"),
    ("continued without DH", lambda g: _drug(g, "ramipril").update(dh_cites=[]), "at least one citation"),
    ("new with DH", lambda g: _drug(g, "ticagrelor").update(dh_cites=_drug(g, "aspirin")["dh_cites"]),
     "NEW drug has no DH"),
    ("duplicate alias", lambda g: _drug(g, "aspirin")["aliases"].append("ISMN"), "used twice"),
    ("must-not also listed", lambda g: g["medications"]["must_not_appear"][0].update(drug="aspirin"),
     "also listed"),
    ("status needs evidence", lambda g: g["medications"].update(status_cites=[]), "at least one citation"),
    ("drugs beside none_required", lambda g: g["medications"].update(discharge_status="none_required"),
     "only STOPPED"),
    ("approved without approver",
     lambda g: (g.update(status="approved"), g["provenance"].update(approved_by=None)), "approved record names"),
    ("draft with approver",
     lambda g: (g.update(status="draft"), g["provenance"].update(approved_by="x", approved_on="y")),
     "draft carries no approval"),
    ("unknown key", lambda g: g["safety_net"].update(extra=1), "unknown extra"),
    ("missing key", lambda g: g["medications"].pop("must_not_appear"), "missing must_not_appear"),
    ("cite with extra key", lambda g: g["age_group"]["cites"][0].update(verified=True), "exactly {lines, quote}"),
])
def test_validator_rejects(label, fn, expect):
    errors = _mut(fn)
    assert any(expect in e for e in errors), label


def test_changed_notes_invalidate_the_gold():
    s = CORPUS["S1"]
    errors = G.validate(BASE, s.notes + "\nDay 6 extra line.", rce.sha256_text(s.notes + "\nDay 6 extra line."))
    assert errors and "re-derive" in errors[0]


def test_line_ids_are_the_pipelines():
    lines = G.line_index(CORPUS["S1"].notes)
    assert len(lines) == len(CORPUS["S1"].notes.splitlines())
    assert sorted(lines)[0] == "L001"


def test_error_messages_never_carry_note_text():
    g = copy.deepcopy(BASE)
    for d in g["medications"]["drugs"]:
        for c in d["dh_cites"] + d["discharge_cites"]:
            c["lines"] = ["L999"]
    msgs = " ".join(_errors(g))
    note_lines = [ln.strip() for ln in CORPUS["S1"].notes.splitlines() if len(ln.strip()) >= 8]
    n_leaked = sum(1 for ln in note_lines if ln in msgs)
    assert msgs and n_leaked == 0


# --- the conflict field (B6's warfarin) ----------------------------------------

B6 = G.load("B6")


def _mut_b6(fn):
    g = copy.deepcopy(B6)
    fn(g)
    return _errors(g, "B6")


def _warfarin(g):
    return next(d for d in g["medications"]["drugs"] if d["drug"] == "warfarin")


@pytest.mark.parametrize("label,fn,expect", [
    ("conflict resolved to one dose", lambda g: _warfarin(g).update(dose="5mg"), "never resolved"),
    ("one-sided conflict", lambda g: _warfarin(g)["conflict"]["cites"].pop(), "one citation per side"),
    ("values not different", lambda g: _warfarin(g)["conflict"].update(values=["5mg", "5mg"]),
     "two different values"),
    ("bad field", lambda g: _warfarin(g)["conflict"].update(field="route"), "field: must be one of"),
])
def test_conflict_rules(label, fn, expect):
    assert any(expect in e for e in _mut_b6(fn)), label


def test_every_gold_record_is_cso_approved():
    # Approved by the CSO on 6 Oct 2026. A gold change after approval must go back to draft.
    assert [sid for sid in PRESENT if G.load(sid)["status"] != "approved"] == []


# --- seek_help and advice kind (CSO, 6 Oct 2026) --------------------------------

S8 = G.load("S8")


@pytest.mark.parametrize("label,sid,fn,expect", [
    ("documented seek-help without cites", "S8", lambda g: g["safety_net"]["seek_help"].update(cites=[]),
     "at least one citation"),
    ("cites on undocumented seek-help", "S1",
     lambda g: g["safety_net"]["seek_help"].update(cites=[{"lines": ["L001"], "quote": "82F"}]),
     "no citations unless documented"),
    ("seek-help on a fallback route", "S8",
     lambda g: g["safety_net"].update(route="fallback", advice=[]), "a documented seek-help trigger is documented advice"),
    ("bad advice kind", "S8", lambda g: g["safety_net"]["advice"][0].update(kind="hint"), "kind: must be one of"),
    ("missing advice kind", "S8", lambda g: g["safety_net"]["advice"][0].pop("kind"), "missing kind"),
])
def test_seek_help_and_kind_rules(label, sid, fn, expect):
    g = copy.deepcopy(G.load(sid))
    fn(g)
    assert any(expect in e for e in _errors(g, sid)), label


# --- CSO adjudications (evals/gold/adjudications/) ------------------------------

ADJ = sorted(p for p in (G.GOLD_DIR / "adjudications").glob("*.json"))


@pytest.mark.parametrize("path", ADJ, ids=[p.stem for p in ADJ])
def test_adjudication_files_are_well_formed_and_signed(path):
    import json
    import score_meds as M
    import score_safety_net as S
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["scenario_id"] == path.stem and path.stem in CORPUS
    for section, verdicts, keyfn in (("rulings", {"supported", "unsupported"}, S.unit_key),
                                     ("med_rulings", {"acceptable", "invented"}, M.entry_key)):
        for key, r in data.get(section, {}).items():
            assert r["verdict"] in verdicts and r["by"] and r["on"]
            assert keyfn(r["text"]) == key          # the key really is this sentence's
