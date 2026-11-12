"""Item 3 R1 step 4 — the case locator (evals/r1_locate_cases.py), on the invented fixture source
(never the 90). It must number lines as the ledger does, read only step 7's input, never call the
renderer's line splitter, and write locations without any wording."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import r1_fixture as FX  # noqa: E402
import r1_locate_cases as LC  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402

T = RP.default_tables()


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    return FX.make_source(tmp_path_factory.mktemp("r1-loc"))


def _screens(heading, text, audience="adult", sid="S9"):
    return dict(LC.screens(heading, text, audience, sid, T))


def test_counts_and_locations_on_the_fixture(src):
    located, reading = LC.locate(*src)
    assert located["records"] == 18 and located["source_batch"] == "fx-source"
    assert located["counts"] == dict(Counter(c["category"] for c in located["cases"]))
    assert located["counts"]["L5"] == 3                         # the three paediatric scenarios' "patient" line
    assert located["counts"]["L6i"] == 17                       # every scenario but the plain one (S2)
    assert len(reading) == len(located["cases"])
    one = next(c for c in located["cases"] if c["case_id"] == "L1-S11-r1-sections.1-1-0")
    assert one == {"case_id": "L1-S11-r1-sections.1-1-0", "category": "L1", "scenario_id": "S11", "run": 1,
                   "record": "r1/S11.json", "source_id": "sections.1", "heading": "diagnosis", "line": 1,
                   "sentence": None, "line_sha256": LC._sha("Primary: NSTEMI (GRACE 142)"), "screen": ["w_grace"]}
    police = next(c for c in located["cases"] if c["case_id"] == "L1-S11-r1-sections.0-1-2")
    assert police["sentence"] == 2 and police["screen"] == ["w_police"]


def test_the_located_file_holds_no_wording_and_the_reading_sheet_does(src):
    located, reading = LC.locate(*src)
    blob = json.dumps(located)
    sheet = LC.reading_sheet(located, reading)
    for fragment in ("chest pain", "wandering", "NSTEMI", "consolidation", "outpatient", "antibiotics"):
        assert fragment not in blob
        assert fragment in sheet or fragment in ("consolidation",)   # the control: the sheet DOES hold the wording
    assert "NEVER COMMITTED" in sheet.splitlines()[0]
    assert set(located["cases"][0]) == {"case_id", "category", "scenario_id", "run", "record", "source_id",
                                        "heading", "line", "sentence", "line_sha256", "screen"}


def test_line_numbering_is_the_ledgers(src):
    """Every located (source_id, line, sentence) is a ledger line of the rendered fixture — rendering the
    FIXTURE is allowed; the locator itself never renders."""
    located, _ = LC.locate(*src)
    w_src, _inputs = src
    for rel in ("r1/S11.json", "r1/S4.json", "r1/S2.json"):
        rec = json.loads((w_src / rel).read_text())
        out = RP.render_patient(LC.CP7.patient_input(rec["steps"]["compose_clinical"]), T)
        ledger = {(x["source_id"], x["line"], x["sentence"]) for x in out["ledger"]["lines"]}
        mine = {(c["source_id"], c["line"], c["sentence"]) for c in located["cases"] if c["record"] == rel}
        assert mine and mine <= ledger


def test_the_locator_never_calls_the_renderers_line_splitter_or_the_renderer(src, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the locator must not call the renderer")
    for name in ("split_lines", "render_patient", "render_line", "_render", "audit", "_plan_drops"):
        monkeypatch.setattr(RP, name, boom)
    assert LC.locate(*src)[0]["counts"]["L1"] > 0


def test_a_form_the_renderer_would_refuse_is_still_numbered_not_judged(tmp_path):
    src = FX.make_source(tmp_path, variants={"S11": {"treatment": "* IV antibiotics\n\n- Not for ITU if worse."}})
    located, _ = LC.locate(*src)
    mine = [c for c in located["cases"] if c["record"] == "r1/S11.json" and c["source_id"] == "sections.3"]
    assert [(c["category"], c["line"], c["sentence"]) for c in mine] == [("L2", 3, None), ("L4", 3, None)]


def test_only_step_7s_input_is_read(src, tmp_path):
    """Poison everything else in every record: the result must not change."""
    base, _ = LC.locate(*src)
    w_src, _inputs = src
    alt = tmp_path / "fx-source"
    alt.mkdir()
    for p in sorted(w_src.rglob("*.json")):
        (alt / p.relative_to(w_src)).parent.mkdir(parents=True, exist_ok=True)
        d = json.loads(p.read_text())
        if "steps" in d:
            d["steps"]["compose_patient"] = {"sections": [{"heading": "why_in_hospital", "source_ids": [],
                                                           "text": "MODEL TEXT police GRACE not planned"}]}
            for s in d["steps"]["compose_clinical"]["sections"]:
                if s["part"] == "part_b":
                    s["text"] = "PART B no risk planned (GRACE 142)"
            d["steps"]["validate_facts"] = {"facts": "no risk planned"}
        (alt / p.relative_to(w_src)).write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    inputs = tmp_path / "inputs.json"
    inputs.write_text(LC.H._dump(LC.H.make_inputs(alt)))
    got, _ = LC.locate(alt, inputs)
    assert got["cases"] == base["cases"]


@pytest.mark.parametrize("heading, text, audience, sid, expect", [
    ("diagnosis", "Primary: NSTEMI (GRACE 142)", "adult", "S9", {"L1": ["w_grace"]}),
    ("presenting_complaint", "Falls risk at home.", "adult", "S9", {"L1": ["risk"]}),
    ("treatment", "- Falls risk assessed.", "adult", "S9", {}),
    ("key_investigations", "- CTPA: no PE.", "adult", "S9", {"L2": ["no"]}),
    ("key_investigations", "- PE ruled out.", "adult", "S9", {"L2": ["ruled out"]}),
    ("follow_up", "- OGD planned as an outpatient.", "adult", "S9", {"L3": ["planned"]}),
    ("follow_up", "- Return to clinic if the leg swells.", "adult", "S9", {"L4": ["if"]}),
    ("presenting_complaint", "Seen if unwell.", "adult", "S9", {}),
    ("presenting_complaint", "The patient was seen with his mother.", "parent_or_carer", "S9",
     {"L5": ["his", "mother", "patient"]}),
    ("presenting_complaint", "The patient was seen.", "adult", "S9", {}),
    ("treatment", "- Oral antibiotics (inferred — not documented, confirm)", "adult", "S9",
     {"L2": ["not"], "L6i": ["inferred"]}),
    ("diagnosis", "Primary: ?PE", "adult", "S9", {"L6u": ["?"]}),
    ("diagnosis", "Secondary: possible UTI", "adult", "S9", {"L6u": ["possible"]}),
    ("presenting_complaint", "Admitted after an overdose.", "adult", "S9", {"L7": ["f3"]}),
    ("treatment", "- Overdose pathway followed.", "adult", "S9", {}),
    ("follow_up", "- Review in 2 weeks.", "adult", "S1", {"U1": ["2 weeks"]}),
    ("follow_up", "- Review in 2 weeks.", "adult", "S9", {}),
    ("follow_up", "- Review in 12 weeks.", "adult", "S1", {}),
])
def test_screens(heading, text, audience, sid, expect):
    assert _screens(heading, text, audience, sid) == expect


def test_a_negated_line_that_meets_a_box_owned_screen_says_so():
    got = _screens("follow_up", "- No concerns, NHS 111 advice given.")
    assert got["L2"][0] == "no" and any(x.startswith("meets:s_") for x in got["L2"])


def test_refuses_inputs_that_are_not_this_batchs(src, tmp_path):
    w_src, inputs = src
    bad = tmp_path / "inputs.json"
    d = json.loads(inputs.read_text())
    d["generations"][0]["record_sha256"] = "0" * 64
    bad.write_text(LC.H._dump(d))
    with pytest.raises(LC.LocateError, match="does not match this batch"):
        LC.locate(w_src, bad)


def test_command_line_prints_counts_only_and_keeps_the_sheet_out_of_the_repository(src, tmp_path, capsys):
    w_src, inputs = src
    out = tmp_path / "cases-located.json"
    args = [str(w_src), "--inputs", str(inputs), "--out", str(out)]
    assert LC.main(args + ["--reading-out", str(ROOT / "evals" / "r1" / "CASE-READING.md")]) == 2
    assert not out.exists() and not (ROOT / "evals" / "r1" / "CASE-READING.md").exists()
    assert LC._reading_path_ok(ROOT / "docs" / "_local" / "r1" / "s4" / "reading" / "CASE-READING.md")
    capsys.readouterr()
    assert LC.main(args + ["--reading-out", str(tmp_path / "CASE-READING.md")]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("located: 18 records") and "chest pain" not in printed and "NSTEMI" not in printed
    assert "chest pain" in (tmp_path / "CASE-READING.md").read_text()
    assert LC.main(args) == 2                                   # never written over
