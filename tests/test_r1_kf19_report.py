"""Item 3 R1 step 4b — the KF-19 code screen (evals/r1_kf19_report.py): report only, never a go condition.

Everything here is INVENTED: the hand-made records below, and the fixture batches of tests/r1_fixture.py
(the $0 fake Bedrock on the synthetic corpus, rendered in a temporary folder). Nothing is from the 90 saved
p0.4 records; no file of the measurement batch is opened.

Three layers:
  * the classification, on hand-made records whose ledgers are written here — one named test per status;
  * the four gates, on a rendered, sealed and evaluated fixture batch — each refusal with a sentinel in
    place of the ledger analysis, which fails the test if the analysis starts at all;
  * "reporting only" — the evaluator never imports the tool or reads its output, the verdict is the same
    bytes with and without the report, there is no exit 1, and the output holds no input wording.

The class a production term gets from the approved tables is NOT asserted anywhere here (the tests of each
class use invented terms): no committed file predicts what the renderer does with a blind-read item's term.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "tests"))

import r1_check_cases as CC  # noqa: E402
import r1_fixture as FX  # noqa: E402
import r1_freeze as FZ  # noqa: E402
import r1_go_stop as GS  # noqa: E402
import r1_kf19_report as KR  # noqa: E402
import render_offline as H  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402

# ---------------------------------------------------------------------------------------------------------
# invented text
# ---------------------------------------------------------------------------------------------------------
# Lines that hold the eight adopted terms, for the four KF-19 scenarios — invented text around the terms.
EIGHT = {
    "A5": {"key_investigations": "- COPD written on a yellow card.\n- AF written on a green card; COPD again."},
    "S10": {"treatment": "- Bimanual compression in the blue room.\n- Observations in a red book before handover."},
    "S13": {"treatment": "- Septic shock in the orange binder.\n- ICU in the purple binder."},
    "S3": {"treatment": "- Psychotic features in the grey binder."},
    # run 2 only: a conflict line, which the renderer refuses by rule — the term before it is still in the input
    "r2/S13": {"treatment": "- Septic shock in the orange binder.\n"
                            "- Notes conflict: left binder vs right binder — requires clarification."},
}
MARKERS = ("yellow", "card", "green", "again", "blue", "room", "red", "book", "orange", "binder", "purple", "grey",
           "written", "before", "clarification")          # words of EIGHT that are not a term
# Invented terms for the default fixture text (FX.A_TEXT), whose treatment the existing fixture's own rules
# fix (tests/fixtures/render_tables_approved_cases.json "hard"; FX.fixture_cases).
PLAIN_TERMS = (
    ("S1", (("NSTEMI", r"\bNSTEMI\b"), ("patient", r"(?i)\bpatient\b"), ("Asthma", r"\bAsthma\b"),
            ("GRACE", r"\bGRACE\b"), ("was admitted", r"\bwas admitted\b"), ("zebra", r"\bzebra\b"))),
)
ZT = (("Z1", (("zorblat", r"\bzorblat\b"),)),)                         # the term of the hand-made records
ZP = r"\bzorblat\b"


# ---------------------------------------------------------------------------------------------------------
# hand-made records: the input and the ledger are written here, so every class is exact
# ---------------------------------------------------------------------------------------------------------
# The entry ids are real ids of the approved tables (the tool prints an id only if its table holds it); the
# term, the lines and the offsets are invented.
PROSE = "The patient told the nurse a private thing"                   # stands for wording in a metadata field

def rec_of(lines: str, ledger: dict | None = None, *, heading: str = "treatment", refusal: str | None = None,
           rendered: bool = True, upstream: bool = True) -> dict:
    """An invented record: one given section `lines`; `ledger`: {(line, sentence): ops} for its units."""
    steps = {}
    if upstream:
        steps["compose_clinical"] = {"age_group": "adult", "sections": [
            {"section_id": "sections.3", "heading": heading, "part": "part_a", "text": lines}]}
    if rendered and refusal is None:
        steps["compose_patient"] = {"sections": [], "ledger": {"lines": [
            {"source_id": "sections.3", "line": n, "sentence": k, "ops": ops, "c_line": None}
            for (n, k), ops in (ledger or {}).items()]}}
    rec = {"scenario_id": "Z1", "run": 1, "generation_id": "fx/r1/Z1", "steps": steps}
    if refusal is not None:
        rec["render_refusal"] = {"code": refusal, "where": {"source_id": "sections.3", "line": 1, "sentence": None}}
    return rec


def row(rec: dict, pattern: str = ZP) -> dict:
    return KR.term_row(rec, pattern, RP.default_tables())


def sub(a, b, entry="g_nstemi", table="glossary"):
    return {"op": "sub", "a": a, "b": b, "table": table, "entry": entry, "out": "x"}


def copy(a, b):
    return {"op": "copy", "a": a, "b": b}


def drop(a, b, rule="withheld", entry="w_grace", unit="aside"):
    return {"op": "drop", "a": a, "b": b, "rule": rule, "entry": entry, "unit": unit}


ONE = "- A zorblat was seen."                      # the unit's text is "A zorblat was seen." — zorblat at 2..9


def test_status_glossed():
    r = row(rec_of(ONE, {(1, None): [copy(0, 2), sub(2, 9), copy(9, 19)]}))
    assert (r["status"], r["occurrences"], r["entries"]) == ("glossed", 1, ["g_nstemi"])
    assert r["detail"] == [{"heading": "treatment", "source_id": "sections.3", "line": 1, "sentence": None,
                            "class": "glossed", "entry": "g_nstemi"}]


def test_status_glossed_needs_one_glossary_sub_over_the_whole_occurrence():
    wider = row(rec_of(ONE, {(1, None): [sub(0, 13), copy(13, 19)]}))            # a wider entry still covers it
    assert wider["status"] == "glossed"
    for table, entry in (("phrases", "p_patient_was"), ("drug_classes", "d_amoxicillin")):
        r = row(rec_of(ONE, {(1, None): [copy(0, 2), sub(2, 9, entry, table), copy(9, 19)]}))
        assert r["status"] == "substituted"


def test_status_substituted_is_never_counted_as_glossed():
    r = row(rec_of(ONE, {(1, None): [copy(0, 2), sub(2, 9, "p_patient_was", "phrases"), copy(9, 19)]}))
    assert (r["status"], r["entries"]) == ("substituted", [["phrases", "p_patient_was"]])
    assert r["detail"][0] | {"class": "substituted", "table": "phrases", "entry": "p_patient_was"} == r["detail"][0]
    d = row(rec_of(ONE, {(1, None): [copy(0, 2), sub(2, 9, "d_amoxicillin", "drug_classes"), copy(9, 19)]}))
    assert (d["status"], d["entries"]) == ("substituted", [["drug_classes", "d_amoxicillin"]])


def test_status_copied():
    r = row(rec_of(ONE, {(1, None): [copy(0, 19)]}))
    assert (r["status"], r["occurrences"]) == ("copied", 1) and "entries" not in r
    assert r["detail"][0]["class"] == "copied"


def test_status_dropped():
    r = row(rec_of("- Seen today (zorblat noted).", {(1, None): [copy(0, 10), drop(10, 26), copy(26, 27)]}))
    assert (r["status"], r["entries"]) == ("dropped", [["withheld", "w_grace"]])
    assert r["detail"][0] | {"class": "dropped", "rule": "withheld", "entry": "w_grace"} == r["detail"][0]


def test_status_dropped_includes_a_line_dropped_whole():
    whole = drop(0, 19, rule="resus_conflict", entry=None, unit="line")
    r = row(rec_of(ONE, {(1, None): [whole]}))
    assert (r["status"], r["entries"], r["occurrences"]) == ("dropped", [["resus_conflict", None]], 1)


def test_status_partial_a_two_word_term_of_which_an_entry_covers_one_word():
    two = "- A zorblat quux was seen."                                           # "zorblat quux" at 2..14
    r = row(rec_of(two, {(1, None): [copy(0, 2), sub(2, 9), copy(9, 24)]}), r"\bzorblat quux\b")
    assert r["status"] == "partial" and "entries" not in r
    assert r["detail"][0]["ops"] == [{"op": "sub", "table": "glossary", "entry": "g_nstemi"}, {"op": "copy"}]


def test_status_partial_is_anything_else():
    part_drop = row(rec_of(ONE, {(1, None): [copy(0, 5), drop(5, 19)]}))                 # a drop over part of it
    assert part_drop["status"] == "partial"
    assert part_drop["detail"][0]["ops"] == [{"op": "copy"}, {"op": "drop", "rule": "withheld", "entry": "w_grace"}]
    two_copies = row(rec_of(ONE, {(1, None): [copy(0, 5), copy(5, 19)]}))                # under two operations
    assert two_copies["status"] == "partial"
    part_sub = row(rec_of(ONE, {(1, None): [copy(0, 2), sub(2, 6), copy(6, 19)]}))       # a sub over part of it
    assert part_sub["status"] == "partial"
    nothing = row(rec_of(ONE, {(1, None): [copy(0, 2)]}))                                # no operation reaches it
    assert (nothing["status"], nothing["detail"][0]["ops"]) == ("partial", [])
    short = row(rec_of(ONE, {(1, None): [copy(0, 2), sub(2, 6)]}))                       # one operation, part of it
    assert (short["status"], short["detail"][0]["ops"]) == ("partial", [{"op": "sub", "table": "glossary",
                                                                         "entry": "g_nstemi"}])
    over = row(rec_of(ONE, {(1, None): [copy(0, 19), sub(2, 9)]}))                       # two operations claim it
    assert (over["status"], len(over["detail"][0]["ops"])) == ("partial", 2)


def test_a_zero_width_operation_touches_nothing():
    r = row(rec_of(ONE, {(1, None): [copy(0, 2), copy(4, 4), sub(2, 9), copy(9, 19)]}))
    assert r["status"] == "glossed"


def test_status_unaccounted_a_unit_with_no_ledger_entry():
    r = row(rec_of(ONE + "\n- Nothing here.", {(2, None): [copy(0, 13)]}))               # line 1 has no entry
    assert (r["status"], r["occurrences"]) == ("unaccounted", 1)
    assert r["detail"][0]["class"] == "unaccounted"


@pytest.mark.parametrize("ops", [
    None, "copy", [copy(0, 19), "x"], [{"op": "keep", "a": 0, "b": 19}], [{"op": "copy", "a": "0", "b": 19}],
    [{"op": "copy", "a": True, "b": 19}], [copy(0, 99)], [copy(5, 2)], [{"op": "copy", "a": 0}],
    [{"op": "sub", "a": 2, "b": 9, "table": "tables", "entry": "g"}], [{"op": "sub", "a": 2, "b": 9, "table": "glossary"}],
    [{"op": "sub", "a": 2, "b": 9, "table": ["glossary"], "entry": "g"}], [{"op": ["sub"], "a": 2, "b": 9}],
    [{"op": "drop", "a": 2, "b": 9, "entry": "w"}], [{"op": "drop", "a": 2, "b": 9, "rule": "withheld", "entry": 7}],
    # names that are not the renderer's or the approved tables' — Codex 13 Nov 2026 13:38
    [{"op": "sub", "a": 2, "b": 9, "table": "glossary", "entry": PROSE}],              # no such entry
    [{"op": "sub", "a": 2, "b": 9, "table": "glossary", "entry": "p_patient_was"}],    # another table's entry
    [{"op": "sub", "a": 2, "b": 9, "table": "withheld", "entry": "w_grace"}],          # a table, but not a sub table
    [{"op": "drop", "a": 2, "b": 9, "rule": PROSE, "entry": "w_grace"}],               # no such rule
    [{"op": "drop", "a": 2, "b": 9, "rule": "withheld", "entry": PROSE}],              # no such entry
    [{"op": "drop", "a": 2, "b": 9, "rule": "seek_help", "entry": "w_grace"}],         # another rule's entry
    [{"op": "drop", "a": 2, "b": 9, "rule": "withheld", "entry": None}],               # a table rule needs an entry
    [{"op": "drop", "a": 0, "b": 19, "rule": "resus_conflict", "entry": PROSE}],       # this rule has none
    [{"op": "drop", "a": 2, "b": 9, "rule": ["withheld"], "entry": "w_grace"}],
])
def test_status_unaccounted_a_ledger_entry_that_cannot_be_read(ops):
    r = row(rec_of(ONE, {(1, None): ops}))
    assert r["status"] == "unaccounted" and r["detail"][0] == {
        "heading": "treatment", "source_id": "sections.3", "line": 1, "sentence": None, "class": "unaccounted"}
    assert PROSE not in json.dumps(r)


def test_a_known_id_beside_an_unknown_one_is_still_not_printed():
    """One unreadable operation makes the whole unit's ledger entry unreadable: none of its names are emitted."""
    r = row(rec_of(ONE, {(1, None): [copy(0, 2), sub(2, 9), {"op": "drop", "a": 9, "b": 19, "rule": PROSE, "entry": None}]}))
    assert r["status"] == "unaccounted" and "g_nstemi" not in json.dumps(r) and PROSE not in json.dumps(r)


def test_status_unaccounted_not_rendered_and_no_refusal_code():
    r = row(rec_of(ONE, rendered=False))
    assert r == {"status": "unaccounted", "occurrences": 1, "why": "not_rendered_and_no_known_refusal_code"}
    for bad in ({}, {"code": None}, {"code": ""}, {"code": 7}, "refused", {"code": PROSE}, {"code": "refused"},
                {"code": ["conflict_in_input"]}):
        rec = rec_of(ONE, rendered=False) | {"render_refusal": bad}
        assert row(rec) == r, bad                                                # never the saved string


def test_a_refusal_code_is_printed_only_if_it_is_the_renderers():
    for code in RP.REFUSALS:
        assert row(rec_of(ONE, refusal=code)) == {"status": "not_rendered", "occurrences": 1, "refusal": code}
    assert PROSE not in json.dumps(row(rec_of(ONE, refusal=PROSE)))
    assert row(rec_of(ONE, refusal=PROSE))["status"] == "unaccounted"


class _Batch:
    """The three things `analyse` reads of a batch."""
    def __init__(self, *records):
        self.batch_id, self.records = "fx", list(records)
        self.rels = [f"r1/Z{i}.json" for i in range(len(records))]


def test_the_run_and_the_generation_id_are_never_copied_from_a_record():
    good = rec_of(ONE, {(1, None): [copy(0, 19)]})
    odd = rec_of(ONE, {(1, None): [copy(0, 19)]}) | {"run": "1 " + PROSE, "generation_id": "fx/r1 " + PROSE + "/Z1"}
    true = rec_of(ONE, {(1, None): [copy(0, 19)]}) | {"run": True}
    lied = rec_of(ONE, {(1, None): [copy(0, 19)]}) | {"run": 3, "generation_id": PROSE}
    rows, summary = KR.analyse(_Batch(good, odd, true, lied), ZT, RP.default_tables())
    assert [(r["run"], r["generation_id"]) for r in rows] == [(1, "fx/r1/Z1"), (None, None), (None, None), (3, "fx/r3/Z1")]
    assert PROSE not in json.dumps([rows, summary]) and summary[0]["runs"] == 4


def test_status_unaccounted_a_saved_output_that_cannot_be_laid_against_the_input():
    for s7 in ({}, {"ledger": {}}, {"ledger": {"lines": [7]}, "sections": []}, {"ledger": {"lines": []}}):
        rec = rec_of(ONE)
        rec["steps"]["compose_patient"] = s7
        assert row(rec) == {"status": "unaccounted", "occurrences": 1,
                            "why": "ledger_cannot_be_laid_against_the_input"}, s7
    rec = rec_of("* A zorblat was seen.", {(1, None): [copy(0, 19)]})         # a form the renderer's splitter refuses
    assert row(rec)["why"] == "ledger_cannot_be_laid_against_the_input"


def test_status_unaccounted_the_units_do_not_hold_what_the_input_holds(monkeypatch):
    rec = rec_of(ONE, {(1, None): [copy(0, 19)]})
    real = GS.ledger_view
    monkeypatch.setattr(GS, "ledger_view", lambda rec, tables: real(rec, tables)[:0])
    assert row(rec) == {"status": "unaccounted", "occurrences": 1, "why": "units_do_not_match_the_input"}


def test_status_mixed_the_same_term_treated_differently():
    lines = "- A zorblat was seen.\n- No zorblat today.\n- Zorblat, zorblat, zorblat."
    ledger = {(1, None): [copy(0, 2), sub(2, 9), copy(9, 19)],                   # glossed
              (2, None): [copy(0, 17)],                                           # copied
              (3, None): [copy(0, 9), sub(9, 16), copy(16, 18), drop(18, 26)]}    # glossed, then dropped, then partial
    r = row(rec_of(lines, ledger), r"(?i)\bzorblat\b")
    assert r["status"] == "mixed" and r["occurrences"] == 5 and "entries" not in r
    assert list(r["classes"]) == list(KR.CLASSES)                                 # the fixed order, every class
    assert r["classes"] == {"glossed": 2, "substituted": 0, "copied": 2, "dropped": 1, "partial": 0, "unaccounted": 0}
    assert [(d["line"], d["class"]) for d in r["detail"]] == [
        (1, "glossed"), (2, "copied"), (3, "copied"), (3, "glossed"), (3, "dropped")]


def test_status_mixed_counts_an_unaccounted_occurrence():
    r = row(rec_of(ONE + "\n- No zorblat today.", {(1, None): [copy(0, 19)]}))
    assert r["status"] == "mixed" and r["classes"]["copied"] == 1 and r["classes"]["unaccounted"] == 1


def test_a_term_twice_with_the_same_treatment_one_entry():
    lines = "- A zorblat was seen.\n- No zorblat today."
    r = row(rec_of(lines, {(1, None): [copy(0, 2), sub(2, 9), copy(9, 19)], (2, None): [copy(0, 3), sub(3, 10), copy(10, 17)]}))
    assert (r["status"], r["occurrences"], r["entries"]) == ("glossed", 2, ["g_nstemi"])
    assert len(r["detail"]) == 2


def test_a_term_twice_with_the_same_treatment_two_entries():
    lines = "- A zorblat was seen.\n- No zorblat today."
    r = row(rec_of(lines, {(1, None): [copy(0, 2), sub(2, 9, "g_t2dm"), copy(9, 19)],
                           (2, None): [copy(0, 3), sub(3, 10, "g_cap_abbr"), copy(10, 17)]}))
    assert (r["status"], r["occurrences"], r["entries"]) == ("glossed", 2, ["g_cap_abbr", "g_t2dm"])        # sorted
    d = row(rec_of(lines, {(1, None): [copy(0, 2), drop(2, 19, "seek_help", "s_111")],
                           (2, None): [drop(0, 17, "resus_conflict", None, "line")]}))
    assert (d["status"], d["entries"]) == ("dropped", [["resus_conflict", None], ["seek_help", "s_111"]])


def test_status_no_input():
    assert row(rec_of(ONE, upstream=False, rendered=False)) == {"status": "no_input"}
    assert row({"scenario_id": "Z1"}) == {"status": "no_input"}
    broken = rec_of(ONE)
    broken["steps"]["compose_clinical"] = {"sections": "none"}                   # step 7 cannot take it
    assert row(broken) == {"status": "no_input"}


def test_status_absent_from_input_comes_first_rendered_or_refused():
    lines = "- Nothing of note."
    assert row(rec_of(lines, {(1, None): [copy(0, 16)]})) == {"status": "absent_from_input"}
    assert row(rec_of(lines, refusal="conflict_in_input")) == {"status": "absent_from_input"}
    assert row(rec_of(lines, rendered=False)) == {"status": "absent_from_input"}
    assert row(rec_of(lines, {})) == {"status": "absent_from_input"}             # no ledger entry, and no term


def test_status_not_rendered_carries_the_records_own_refusal_code():
    r = row(rec_of(ONE + "\n- Zorblat again? No: zorblat.", refusal="conflict_in_input"), r"(?i)\bzorblat\b")
    assert r == {"status": "not_rendered", "occurrences": 3, "refusal": "conflict_in_input"}


def test_the_input_is_read_before_anything_of_the_output(monkeypatch):
    """Step 1 never asks for the ledger: no_input and absent_from_input are decided from the input alone."""
    monkeypatch.setattr(GS, "ledger_view", lambda *a: pytest.fail("the ledger was read"))
    assert row(rec_of("- Nothing of note.", {(1, None): [copy(0, 16)]}))["status"] == "absent_from_input"
    assert row(rec_of(ONE, upstream=False))["status"] == "no_input"
    assert row(rec_of(ONE, refusal="unsupported_form"))["status"] == "not_rendered"


def test_occurrences_are_left_to_right_and_do_not_overlap():
    assert KR.occurrences(r"\bzorblat\b", "zorblat, zorblats and a zorblat") == [(0, 7), (24, 31)]
    assert KR.occurrences(r"aa", "aaaaa") == [(0, 2), (2, 4)]
    r = row(rec_of("The zorblat came. A zorblat went.", {(1, 1): [copy(0, 17)], (1, 2): [copy(0, 15)]},
                   heading="presenting_complaint"))
    assert [(d["line"], d["sentence"]) for d in r["detail"]] == [(1, 1), (1, 2)]


def test_every_row_status_and_every_class_is_named():
    assert KR.STATUSES == ("no_input", "absent_from_input", "not_rendered", "unaccounted", "glossed", "substituted",
                           "copied", "dropped", "partial", "mixed")
    assert KR.CLASSES == ("glossed", "substituted", "copied", "dropped", "partial", "unaccounted")
    assert set(KR.CLASSES) | {"mixed"} | {"no_input", "absent_from_input", "not_rendered"} == set(KR.STATUSES)


# ---------------------------------------------------------------------------------------------------------
# the eight terms
# ---------------------------------------------------------------------------------------------------------

def test_the_terms_are_the_eight_adopted_ones_by_exact_pattern():
    assert KR.TERMS == (
        ("A5", (("COPD", r"\bCOPD\b"), ("AF", r"\bAF\b"))),
        ("S10", (("bimanual compression", r"(?i)\bbimanual compression\b"), ("observations", r"(?i)\bobservations\b"),
                 ("handover", r"(?i)\bhandover\b"))),
        ("S13", (("septic shock", r"(?i)\bseptic shock\b"), ("ICU", r"\bICU\b"))),
        ("S3", (("psychotic features", r"(?i)\bpsychotic features\b"),)),
    )
    patterns = [p for _s, named in KR.TERMS for _t, p in named]
    assert len(patterns) == 8 and len(set(patterns)) == 8
    assert all(CC.pattern_problem(p) is None for p in patterns)                  # short patterns, under rule 2f
    assert {s for s, _n in KR.TERMS} <= set(GS.MS.ITEMS)                         # all four are blind-read items


def test_other_spellings_are_not_counted():
    by = {t: p for _s, named in KR.TERMS for t, p in named}
    n = lambda term, text: len(KR.occurrences(by[term], text))                   # noqa: E731
    assert n("COPD", "COPD. A COPD nurse; copd, Copd, COPDs, nonCOPD") == 2      # case as written, whole word
    assert n("AF", "AF and PAF, AFB, af, Af; (AF)") == 2
    assert n("ICU", "ICU, ITU, icu, PICU, ICUs") == 1
    assert n("observations", "Observations, observations, OBSERVATIONS; obs, observation") == 3
    assert n("handover", "Handover; handover. hand over, handovers") == 2
    assert n("bimanual compression", "Bimanual compression, bimanual  compression, bimanual\ncompression") == 1
    assert n("septic shock", "Septic Shock; septic shock. sepsis, shock, septic-shock") == 2
    assert n("psychotic features", "Psychotic features; psychotic, features of psychosis") == 1


def test_the_command_line_cannot_change_the_terms():
    opts = sorted(o for a in KR.parser()._actions for o in a.option_strings)
    assert opts == ["--batch", "--freeze", "--help", "--inputs", "--out", "--sheets", "--verdict", "-h"]


# ---------------------------------------------------------------------------------------------------------
# rendered, sealed and evaluated fixture batches
# ---------------------------------------------------------------------------------------------------------

def save_verdict(w: FX.World, rep: dict | None = None) -> Path:
    """Write the evaluator's report where its own command writes it (the bytes `evaluate` writes)."""
    out = w.batch / GS.OUT_DIR
    out.mkdir(exist_ok=True)
    (out / KR.VERDICT_FILE).write_text(GS.dump(rep if rep is not None else w.evaluate()), encoding="utf-8")
    return out / KR.VERDICT_FILE


def argv(w: FX.World, out: Path, **more) -> list[str]:
    a = {"--batch": w.batch, "--sheets": w.sheets, "--out": out, "--freeze": w.freeze, "--inputs": w.inputs} | more
    return [str(x) for kv in a.items() for x in kv]


def run(w: FX.World, out: Path, terms=KR.TERMS, **more) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = KR.main(argv(w, out, **more), _terms=terms)
    return rc, buf.getvalue()


def tree(*folders: Path) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for d in folders for p in sorted(d.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def plain(tmp_path_factory) -> FX.World:
    """The default fixture, one run, taken to the end: a GO, with its verdict saved."""
    root = tmp_path_factory.mktemp("kf19-plain")
    src, inputs = FX.make_source(root / "s", runs=1)
    w = FX.build_world(root / "w", src, inputs)
    save_verdict(w)
    return w


@pytest.fixture(scope="module")
def eight(tmp_path_factory) -> FX.World:
    """Two runs with the eight terms in invented lines; run 2 of S13 is refused (a conflict line) and run 2 of
    S3 has no step-7 input (its upstream step is taken out). Not every generation rendered: a STOP."""
    root = tmp_path_factory.mktemp("kf19-eight")
    src, inputs = FX.make_source(root / "s", runs=2, variants=EIGHT)
    p = src / "r2" / "S3.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    del rec["steps"]["compose_clinical"]
    p.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    inputs.write_text(H._dump(H.make_inputs(src)), encoding="utf-8")
    w = FX.build_world(root / "w", src, inputs, until="score")
    save_verdict(w)
    return w


@pytest.fixture
def analysed(monkeypatch):
    """In place of the ledger analysis: records every call. A refusal test asserts it stayed empty."""
    called = []
    monkeypatch.setattr(KR, "analyse", lambda c, terms, tables: called.append(c.batch_id) or ([], []))
    return called


def refused(w: FX.World, out: Path, analysed: list, **more) -> str:
    """Run the tool, expect a refusal with nothing analysed and nothing written; return what it printed."""
    rc, said = run(w, out, **more)
    assert rc == 2 and said.startswith("REFUSED — nothing reported, nothing written: "), said
    assert analysed == [], "the ledger analysis started"
    assert not out.exists()
    return said.strip().split(": ", 1)[1]


def test_the_conditions_are_the_evaluators_own(plain):
    rep = plain.evaluate()
    assert tuple(rep["conditions"]) == KR.CONDITIONS
    assert KR.CONDITION_STATUSES == (GS.MET, GS.NOT_MET, GS.UNDECIDED)
    assert rep["evaluator_version"] == GS.EVALUATOR_VERSION and rep["blind_read"] == "sealed"


def test_the_saved_verdict_is_what_the_evaluators_command_writes(plain, tmp_path):
    w = FX.copy_world(plain, tmp_path / "w")
    shutil.rmtree(w.batch / GS.OUT_DIR)
    with contextlib.redirect_stdout(io.StringIO()):
        rc = GS.main(["evaluate", "--batch", str(w.batch), "--freeze", str(w.freeze), "--inputs", str(w.inputs),
                      "--store", str(w.store), "--sheets", str(w.sheets), "--run2", str(w.run2)])
    assert rc == GS.EXIT[GS.GO]
    assert (w.batch / GS.OUT_DIR / KR.VERDICT_FILE).read_bytes() == \
        (plain.batch / GS.OUT_DIR / KR.VERDICT_FILE).read_bytes()
    assert run(w, tmp_path / "out", PLAIN_TERMS)[0] == 0                         # the default --verdict path


def test_end_to_end_on_a_go(plain, tmp_path):
    out = tmp_path / "out"
    rc, said = run(plain, out, PLAIN_TERMS)
    assert rc == 0
    rep = json.loads((out / KR.REPORT_JSON).read_text(encoding="utf-8"))
    vfile = plain.batch / GS.OUT_DIR / KR.VERDICT_FILE
    assert rep["verdict_file"] == {"sha256": hashlib.sha256(vfile.read_bytes()).hexdigest(), "verdict": GS.GO}
    assert rep["batch_id"] == FX.BATCH_ID and rep["freeze_sha256"] == plain.freeze_sha()
    assert rep["blind_read"] == "sealed" and rep["tool_version"] == KR.TOOL_VERSION
    assert rep["terms"] == [{"scenario_id": "S1", "term": t, "pattern": p} for t, p in PLAIN_TERMS[0][1]]
    by = {r["term"]: r for r in rep["rows"]}
    assert len(rep["rows"]) == 6 and all(r["record"] == "r1/S1.json" and r["run"] == 1 for r in rep["rows"])
    assert (by["NSTEMI"]["status"], by["NSTEMI"]["entries"]) == ("glossed", ["g_nstemi"])
    assert (by["patient"]["status"], by["patient"]["entries"]) == ("substituted", [["phrases", "p_patient_was"]])
    assert by["Asthma"]["status"] == "copied"
    assert (by["GRACE"]["status"], by["GRACE"]["entries"]) == ("dropped", [["withheld", "w_grace"]])
    assert by["was admitted"]["status"] == "partial"
    assert by["was admitted"]["detail"][0]["ops"] == [{"op": "sub", "table": "phrases", "entry": "p_patient_was"},
                                                      {"op": "copy"}]
    assert by["zebra"] == {"record": "r1/S1.json", "generation_id": f"{FX.BATCH_ID}/r1/S1", "scenario_id": "S1",
                           "run": 1, "term": "zebra", "status": "absent_from_input"}
    assert by["NSTEMI"]["detail"] == [{"heading": "diagnosis", "source_id": by["NSTEMI"]["detail"][0]["source_id"],
                                       "line": 1, "sentence": None, "class": "glossed", "entry": "g_nstemi"}]
    for s in rep["summary"]:
        assert s["runs"] == 1 and list(s["by_status"]) == list(KR.STATUSES) and sum(s["by_status"].values()) == 1
        assert s["by_status"][by[s["term"]]["status"]] == 1
    assert f"report -> {out / KR.REPORT_JSON}" in said


def test_a_whole_section_dropped_is_reported_as_dropped(plain, tmp_path):
    """FX.WHOLE (scenario S8): every line of the section leaves by an authorised drop."""
    rc, _ = run(plain, tmp_path / "out", (("S8", (("GRACE score", r"\bGRACE score\b"), ("CURB", r"\bCURB\b"))),))
    rows = json.loads((tmp_path / "out" / KR.REPORT_JSON).read_text(encoding="utf-8"))["rows"]
    assert rc == 0 and [r["status"] for r in rows] == ["dropped", "dropped"]
    assert rows[0]["entries"] == [["withheld", "w_grace"]]


def test_a_stop_is_still_reported(eight, tmp_path):
    assert eight.evaluate()["verdict"] == GS.STOP
    rc, said = run(eight, tmp_path / "out")
    rep = json.loads((tmp_path / "out" / KR.REPORT_JSON).read_text(encoding="utf-8"))
    assert rc == 0 and rep["verdict_file"]["verdict"] == GS.STOP and "saved verdict: STOP" in said


def test_a_post_seal_incomplete_is_still_reported(tmp_path):
    src, inputs = FX.make_source(tmp_path / "s", runs=1)
    w = FX.build_world(tmp_path / "w", src, inputs, until="score")               # sealed and scored; nothing ruled
    rep = w.evaluate()
    assert rep["verdict"] == GS.INCOMPLETE and rep["blind_read"] == "sealed" and rep["conditions"]
    save_verdict(w, rep)
    rc, _ = run(w, tmp_path / "out", PLAIN_TERMS)
    out = json.loads((tmp_path / "out" / KR.REPORT_JSON).read_text(encoding="utf-8"))
    assert rc == 0 and out["verdict_file"]["verdict"] == GS.INCOMPLETE and len(out["rows"]) == 6


def test_the_eight_terms_on_the_fixture(eight, tmp_path):
    """Structure only: which class the approved tables give a production term is not asserted."""
    rc, _ = run(eight, tmp_path / "out")
    rep = json.loads((tmp_path / "out" / KR.REPORT_JSON).read_text(encoding="utf-8"))
    assert rc == 0 and len(rep["terms"]) == 8 and len(rep["rows"]) == 16         # 8 terms x 2 runs
    assert [(r["record"], r["term"]) for r in rep["rows"]] == [
        (f"r{n}/{s}.json", t) for s, named in KR.TERMS for t, _p in named for n in (1, 2)]
    by = {(r["record"], r["term"]): r for r in rep["rows"]}
    # decided from the input and the record's own refusal code — nothing of a rendered output:
    assert by[("r2/S13.json", "septic shock")] | {"status": "not_rendered", "occurrences": 1,
                                                  "refusal": "conflict_in_input"} == by[("r2/S13.json", "septic shock")]
    assert by[("r2/S13.json", "ICU")]["status"] == "absent_from_input"           # the refused run never held it
    assert by[("r2/S3.json", "psychotic features")]["status"] == "no_input"
    rendered = [r for k, r in by.items() if k[0] not in ("r2/S13.json", "r2/S3.json")]
    assert len(rendered) == 13 and all(r["status"] in KR.CLASSES + ("mixed",) for r in rendered)
    assert by[("r1/A5.json", "COPD")]["occurrences"] == 2 and by[("r1/A5.json", "AF")]["occurrences"] == 1
    assert all(len(r["detail"]) == r["occurrences"] for r in rendered)
    for s in rep["summary"]:
        assert s["runs"] == 2 and sum(s["by_status"].values()) == 2
    line = {(s["scenario_id"], s["term"]): s["by_status"] for s in rep["summary"]}
    assert line[("S13", "ICU")]["absent_from_input"] == 1 and line[("S13", "septic shock")]["not_rendered"] == 1
    assert line[("S3", "psychotic features")]["no_input"] == 1


def test_the_fixture_lines_are_invented():
    """No five-word run of the invented lines is in a scenario file (the 90's source notes)."""
    scen_runs, _whole = CC.scenario_text(CC.SCENARIO_DIR)
    for texts in EIGHT.values():
        for text in texts.values():
            assert not CC.runs(CC.words(text), CC.RUN_WORDS) & scen_runs, text


# ---------------------------------------------------------------------------------------------------------
# the gates: each refusal, with a sentinel in place of the ledger analysis
# ---------------------------------------------------------------------------------------------------------

def test_the_sentinel_fires_when_the_gates_hold(plain, tmp_path, analysed):
    rc, _ = run(plain, tmp_path / "out", PLAIN_TERMS)
    assert rc == 0 and analysed == [FX.BATCH_ID]


def test_gate_1_a_freeze_that_no_longer_matches_the_files(plain, tmp_path, analysed):
    frozen = json.loads(plain.freeze.read_text(encoding="utf-8"))
    name = next(k for k in frozen["files"] if k.endswith("r1_kf19_report.py"))    # the tool itself is pinned
    frozen["files"][name] = "0" * 64
    alt = tmp_path / "FREEZE.json"
    alt.write_text(GS.dump(frozen), encoding="utf-8")
    said = refused(plain, tmp_path / "out", analysed, **{"--freeze": alt})
    assert f"freeze:file_changed:{name}" in said


def test_gate_1_a_record_with_altered_provenance(plain, tmp_path, analysed):
    w = FX.copy_world(plain, tmp_path / "w")
    p = w.batch / "r1" / "S9.json"                                               # not a blind-read item
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["render_provenance"]["from_record_sha256"] = "0" * 64
    p.write_text(H._dump(rec), encoding="utf-8")
    assert refused(w, tmp_path / "out", analysed) == "r1/S9.json:provenance"


def test_gate_1_inputs_that_are_not_the_batchs(plain, eight, tmp_path, analysed):
    assert "batch:inputs" in refused(plain, tmp_path / "out", analysed, **{"--inputs": eight.inputs})
    assert refused(plain, tmp_path / "out", analysed, **{"--batch": tmp_path / "none"}) == "batch:not_readable"


def test_gate_2_no_seal(plain, tmp_path, analysed):
    w = FX.copy_world(plain, tmp_path / "w")
    (w.sheets / GS.SEAL_FILE).unlink()
    assert refused(w, tmp_path / "out", analysed) == "seal:absent"
    assert refused(plain, tmp_path / "out", analysed, **{"--sheets": tmp_path / "none"}) == "seal:absent"


def test_gate_2_an_impossible_seal(tmp_path, analysed):
    """A blind-read item refused at run 1: no blind read can ever be sealed, so the report is never produced."""
    src, inputs = FX.make_source(tmp_path / "s", runs=1, variants={"S13": FX.REFUSE})
    w = FX.build_world(tmp_path / "w", src, inputs, until="score")
    rep = w.evaluate()
    assert rep["blind_read"] == "no blind read possible" and rep["conditions"]
    save_verdict(w, rep)
    assert refused(w, tmp_path / "out", analysed) == "seal:impossible"


def test_gate_2_a_seal_for_another_batch(plain, eight, tmp_path, analysed):
    said = refused(plain, tmp_path / "out", analysed, **{"--sheets": eight.sheets})
    assert said.startswith("seal:") and "seal:manifest_sha256" in said and "seal:freeze_sha256" in said


def test_gate_2_a_seal_that_no_longer_verifies(plain, tmp_path, analysed):
    w = FX.copy_world(plain, tmp_path / "w")
    p = sorted((w.sheets / "scores").glob("*.json"))[0]
    d = json.loads(p.read_text(encoding="utf-8"))
    d["verdicts"]["audience"] = "fail"                                           # changed after sealing
    p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    assert refused(w, tmp_path / "out", analysed).startswith("seal:")


def _with_verdict(plain, tmp_path, change) -> FX.World:
    w = FX.copy_world(plain, tmp_path / "w")
    p = w.batch / GS.OUT_DIR / KR.VERDICT_FILE
    doc = json.loads(p.read_text(encoding="utf-8"))
    change(doc)
    p.write_text(GS.dump(doc), encoding="utf-8")
    return w


def test_gate_3_no_verdict_or_one_that_cannot_be_read(plain, tmp_path, analysed):
    w = FX.copy_world(plain, tmp_path / "w")
    p = w.batch / GS.OUT_DIR / KR.VERDICT_FILE
    p.unlink()
    assert refused(w, tmp_path / "out", analysed) == "verdict:not_readable"
    p.write_text("{ not json", encoding="utf-8")
    assert refused(w, tmp_path / "out", analysed) == "verdict:not_readable"
    p.write_text("[1, 2]\n", encoding="utf-8")                                   # JSON, but not a report
    assert refused(w, tmp_path / "out", analysed) == "verdict:not_readable"
    assert refused(plain, tmp_path / "out", analysed, **{"--verdict": tmp_path / "none.json"}) == "verdict:not_readable"


def test_gate_3_a_verdict_for_another_batch(plain, tmp_path, analysed):
    w = _with_verdict(plain, tmp_path, lambda d: d.update(batch_id="fx-some-other-batch"))
    assert refused(w, tmp_path / "out", analysed) == "verdict:not_this_batch"


def test_gate_3_a_verdict_for_another_freeze(plain, eight, tmp_path, analysed):
    other = eight.batch / GS.OUT_DIR / KR.VERDICT_FILE                           # a real report, of another freeze
    assert json.loads(other.read_text(encoding="utf-8"))["batch_id"] == FX.BATCH_ID
    assert refused(plain, tmp_path / "out", analysed, **{"--verdict": other}) == "verdict:not_this_batch"


def test_gate_3_a_verdict_written_before_the_seal(plain, tmp_path, analysed):
    w = FX.copy_world(plain, tmp_path / "w")
    seal = w.sheets / GS.SEAL_FILE
    kept = seal.read_bytes()
    seal.unlink()
    early = w.evaluate()                                                         # what the evaluator says pre-seal
    assert early["conditions"] == {} and early["verdict"] == GS.INCOMPLETE
    seal.write_bytes(kept)
    save_verdict(w, early)
    assert refused(w, tmp_path / "out", analysed) == "verdict:pre_seal"
    w2 = _with_verdict(plain, tmp_path / "2", lambda d: d.pop("conditions"))
    assert refused(w2, tmp_path / "out", analysed) == "verdict:pre_seal"


@pytest.mark.parametrize("name, change", [
    ("the verdict does not follow", lambda d: d.update(verdict=GS.STOP)),
    ("a verdict the evaluator cannot give", lambda d: d.update(verdict="PASS")),
    ("a condition missing", lambda d: d["conditions"].pop(KR.CONDITIONS[3])),
    ("a condition too many", lambda d: d["conditions"].update({"G9 extra": {"status": GS.MET}})),
    ("the conditions out of order", lambda d: d.update(conditions=dict(reversed(list(d["conditions"].items()))))),
    ("a status the evaluator cannot give", lambda d: d["conditions"][KR.CONDITIONS[0]].update(status="PASS")),
    ("a status of the wrong type", lambda d: d["conditions"][KR.CONDITIONS[0]].update(status=["MET"])),
    ("a condition that is not an object", lambda d: d["conditions"].update({KR.CONDITIONS[0]: "MET"})),
    ("conditions that are not an object", lambda d: d.update(conditions=["MET"] * 8)),
    ("another evaluator version", lambda d: d.update(evaluator_version=GS.EVALUATOR_VERSION + 1)),
    ("a version of the wrong type", lambda d: d.update(evaluator_version=True)),
    ("not a sealed blind read", lambda d: d.update(blind_read="no blind read possible")),
    ("no blind read line", lambda d: d.pop("blind_read")),
])
def test_gate_3_a_verdict_that_is_not_consistent(plain, tmp_path, analysed, name, change):
    w = _with_verdict(plain, tmp_path, change)
    assert refused(w, tmp_path / "out", analysed) == "verdict:not_consistent", name


def test_gate_3_checks_in_a_fixed_order(plain):
    c, fsha = plain.candidate(), plain.freeze_sha()
    good = plain.evaluate()
    assert KR.verdict_problem(good, c, fsha) is None
    assert KR.verdict_problem(good | {"batch_id": "x", "conditions": {}}, c, fsha) == "verdict:not_this_batch"
    assert KR.verdict_problem(good | {"conditions": {}, "verdict": "PASS"}, c, fsha) == "verdict:pre_seal"
    assert KR.verdict_problem(None, c, fsha) == "verdict:not_readable"
    for verdict in (GS.STOP, GS.INCOMPLETE):                                     # GO is not required
        status = GS.NOT_MET if verdict == GS.STOP else GS.UNDECIDED
        doc = json.loads(json.dumps(good))
        doc["conditions"][KR.CONDITIONS[2]]["status"], doc["verdict"] = status, verdict
        assert KR.verdict_problem(doc, c, fsha) is None


def test_the_gates_run_in_order_and_the_first_failure_ends_the_run(plain, tmp_path, analysed, monkeypatch):
    w = FX.copy_world(plain, tmp_path / "w")
    (w.sheets / GS.SEAL_FILE).unlink()
    (w.batch / GS.OUT_DIR / KR.VERDICT_FILE).unlink()
    p = w.batch / "r1" / "S9.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["render_provenance"]["input_sha256"] = "0" * 64
    p.write_text(H._dump(rec), encoding="utf-8")
    later = []
    real_seal, real_verdict = GS.seal_state, KR.gate_verdict
    monkeypatch.setattr(GS, "seal_state", lambda *a: later.append("seal") or real_seal(*a))
    monkeypatch.setattr(KR, "gate_verdict", lambda *a: later.append("verdict") or real_verdict(*a))
    assert refused(w, tmp_path / "out", analysed) == "r1/S9.json:provenance" and later == []      # gate 1 only
    shutil.copy2(plain.batch / "r1" / "S9.json", p)
    assert refused(w, tmp_path / "out", analysed) == "seal:absent" and later == ["seal"]         # then gate 2
    shutil.copy2(plain.sheets / GS.SEAL_FILE, w.sheets / GS.SEAL_FILE)
    assert refused(w, tmp_path / "out", analysed) == "verdict:not_readable"                       # then gate 3
    assert later == ["seal", "seal", "verdict"]


def test_gate_1_is_the_evaluators_own_integrity_check(plain, tmp_path, analysed, monkeypatch):
    def no(*a):
        raise GS.Refused("freeze:from_the_evaluator")
    monkeypatch.setattr(GS, "integrity", no)
    assert refused(plain, tmp_path / "out", analysed) == "freeze:from_the_evaluator"


# ---------------------------------------------------------------------------------------------------------
# reporting only
# ---------------------------------------------------------------------------------------------------------

def test_the_evaluator_never_imports_it_or_reads_its_output():
    source = (ROOT / "evals" / "r1_go_stop.py").read_text(encoding="utf-8")
    for word in ("kf19", "KF19", KR.REPORT_JSON, KR.REPORT_MD):
        assert word not in source
    code = ("import sys; sys.path.insert(0, 'evals'); import r1_go_stop; "
            "print(sorted(m for m in sys.modules if 'kf19' in m.lower()))")
    done = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(ROOT), capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == "[]", done.stdout + done.stderr
    for p in sorted((ROOT / "evals").glob("*.py")) + sorted((ROOT / "src" / "generate").rglob("*.py")):
        if p.name != "r1_kf19_report.py":
            assert "r1_kf19_report" not in p.read_text(encoding="utf-8"), p.name   # nothing imports the tool


def test_the_verdict_is_the_same_bytes_with_and_without_the_report(plain, tmp_path):
    w = FX.copy_world(plain, tmp_path / "w")
    before, files = GS.dump(w.evaluate()), tree(w.root)
    rc, _ = run(w, tmp_path / "out", PLAIN_TERMS)
    assert rc == 0 and (tmp_path / "out" / KR.REPORT_JSON).is_file()
    assert tree(w.root) == files                                                 # it wrote nothing but its report
    assert GS.dump(w.evaluate()) == before
    assert before == (w.batch / GS.OUT_DIR / KR.VERDICT_FILE).read_text(encoding="utf-8")


def test_the_report_cannot_be_written_into_the_batch_folder(plain, tmp_path, analysed):
    w = FX.copy_world(plain, tmp_path / "w")
    files = tree(w.root)
    for out in (w.batch, w.batch / "kf19", w.batch / GS.OUT_DIR, w.batch / "a" / "b"):
        rc, said = run(w, out, PLAIN_TERMS)
        assert rc == 2 and said.strip().endswith("out:inside_the_batch") and analysed == []
    assert tree(w.root) == files


def test_the_report_cannot_be_written_into_the_sheets_folder(plain, tmp_path, analysed):
    w = FX.copy_world(plain, tmp_path / "w")
    files = tree(w.root)
    for out in (w.sheets, w.sheets / "kf19", w.sheets / "scores"):
        rc, said = run(w, out, PLAIN_TERMS)
        assert rc == 2 and said.strip().endswith("out:inside_the_sheets") and analysed == []
    assert tree(w.root) == files
    assert run(w, w.root / "kf19", PLAIN_TERMS)[0] == 0                          # beside them is fine


def test_there_is_no_exit_1(plain, eight, tmp_path, monkeypatch):
    assert (KR.EXIT_REPORTED, KR.EXIT_REFUSED) == (0, 2)
    assert run(plain, tmp_path / "a", PLAIN_TERMS)[0] == 0                       # a GO
    assert run(eight, tmp_path / "b")[0] == 0                                    # a STOP, refused and inputless runs
    worst = (("S1", (("NSTEMI", r"\bNSTEMI\b"),)),)
    monkeypatch.setattr(GS, "ledger_view", lambda rec, tables: None)             # every row unaccounted
    rc, _ = run(plain, tmp_path / "c", worst)
    rows = json.loads((tmp_path / "c" / KR.REPORT_JSON).read_text(encoding="utf-8"))["rows"]
    assert rc == 0 and [r["status"] for r in rows] == ["unaccounted"]
    source = (ROOT / "evals" / "r1_kf19_report.py").read_text(encoding="utf-8")
    assert "return 1" not in source and "exit(1" not in source


def test_an_error_it_did_not_foresee_is_a_refusal_not_a_traceback(plain, tmp_path, monkeypatch):
    def boom(*a):
        raise RuntimeError("unforeseen")
    monkeypatch.setattr(KR, "analyse", boom)
    rc, said = run(plain, tmp_path / "out", PLAIN_TERMS)
    assert rc == 2 and said.strip().endswith("report:internal_error:RuntimeError") and "unforeseen" not in said
    assert not (tmp_path / "out").exists()


def test_a_report_that_cannot_be_written_is_a_refusal(plain, tmp_path):
    blocked = tmp_path / "a-file"
    blocked.write_text("x", encoding="utf-8")
    rc, said = run(plain, blocked / "out", PLAIN_TERMS)
    assert rc == 2 and said.strip().endswith("out:not_writable")


def test_the_output_says_report_only(eight, tmp_path):
    rc, said = run(eight, tmp_path / "out")
    rep = json.loads((tmp_path / "out" / KR.REPORT_JSON).read_text(encoding="utf-8"))
    md = (tmp_path / "out" / KR.REPORT_MD).read_text(encoding="utf-8")
    assert KR.HEADER == "REPORT ONLY — not a go condition; KF-19 is decided by the blind read"
    assert rc == 0 and rep["deciding"] is False and rep["header"] == KR.HEADER
    assert said.splitlines()[0] == KR.HEADER and md.splitlines()[2] == f"**{KR.HEADER}**"
    assert "deciding: no" in md and rep["cannot_show"] == KR.CANNOT_SHOW
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [KR.REPORT_JSON, KR.REPORT_MD]


def test_no_wording_in_the_output(eight, tmp_path):
    """Terms, ids, codes and counts only: nothing else of an input line or of a rendered line."""
    rc, said = run(eight, tmp_path / "out")
    assert rc == 0
    text = (said + (tmp_path / "out" / KR.REPORT_JSON).read_text(encoding="utf-8")
            + (tmp_path / "out" / KR.REPORT_MD).read_text(encoding="utf-8")).replace(str(tmp_path), "")
    low = set(CC.words(text))
    assert not low & set(MARKERS), low & set(MARKERS)
    c, tables = eight.candidate(), RP.default_tables()
    for rel in c.rels:
        rec = c.by_rel[rel]
        if rec["scenario_id"] not in ("A5", "S10", "S13", "S3"):
            continue
        inp = GS.step7_input(rec)
        lines = [ln for s in (inp or {"sections": []})["sections"] for ln in s["text"].split("\n")]
        lines += [r["c_line"] for r in (GS.ledger_view(rec, tables) or []) if r["c_line"]]
        for ln in lines:
            for run3 in CC.runs(CC.words(ln), 3):
                assert " ".join(run3) not in " ".join(CC.words(text)), (rel, run3)
    keys = {k for r in json.loads((tmp_path / "out" / KR.REPORT_JSON).read_text(encoding="utf-8"))["rows"]
            for d in [r] + r.get("detail", []) for k in d}
    assert keys <= {"record", "generation_id", "scenario_id", "run", "term", "status", "occurrences", "refusal",
                    "why", "entries", "classes", "detail", "heading", "source_id", "line", "sentence", "class",
                    "table", "entry", "rule", "ops"}                             # no "out", no "text", no "c_line"


def _ledger_ops(rec: dict, line: int, ops: list) -> None:
    section = next(s["section_id"] for s in rec["steps"]["compose_clinical"]["sections"]
                   if s["part"] == "part_a" and s["heading"] == "key_investigations")
    hit = [x for x in rec["steps"]["compose_patient"]["ledger"]["lines"] if (x["source_id"], x["line"]) == (section, line)]
    assert len(hit) == 1
    hit[0]["ops"] = ops


def test_wording_in_a_metadata_field_never_reaches_the_output(eight, tmp_path):
    """Codex 13 Nov 2026 13:38: all four gates hold, and a saved ledger and a saved refusal carry prose where
    an entry id, a rule and a refusal code should be. None of it is printed; the rows are `unaccounted`."""
    w = FX.copy_world(eight, tmp_path / "w")
    p = w.batch / "r2" / "A5.json"                                               # run 2: not a blind-read item
    rec = json.loads(p.read_text(encoding="utf-8"))
    one, two = (len(ln) - 2 for ln in EIGHT["A5"]["key_investigations"].split("\n"))
    _ledger_ops(rec, 1, [{"op": "sub", "a": 0, "b": 4, "table": "glossary", "entry": PROSE, "out": PROSE},
                         {"op": "copy", "a": 4, "b": one}])
    _ledger_ops(rec, 2, [{"op": "drop", "a": 0, "b": two, "rule": PROSE + " as a rule", "entry": None, "unit": "line"}])
    p.write_text(H._dump(rec), encoding="utf-8")
    q = w.batch / "r2" / "S13.json"                                              # the refused generation
    ref = json.loads(q.read_text(encoding="utf-8"))
    ref["render_refusal"]["code"] = PROSE + " as a code"
    q.write_text(H._dump(ref), encoding="utf-8")
    rc, said = run(w, tmp_path / "out")
    assert rc == 0                                                               # the gates hold; it still reports
    body = (tmp_path / "out" / KR.REPORT_JSON).read_text(encoding="utf-8")
    text = (said + body + (tmp_path / "out" / KR.REPORT_MD).read_text(encoding="utf-8")).replace(str(tmp_path), "")
    assert PROSE not in text and "private" not in text and "nurse" not in text  # (the temp folder's own path
    #                                               is taken out first: on macOS it starts /private/var/...)
    by = {(r["record"], r["term"]): r for r in json.loads(body)["rows"]}
    assert by[("r2/A5.json", "COPD")]["status"] == "unaccounted"                 # both of its units
    assert [d["class"] for d in by[("r2/A5.json", "COPD")]["detail"]] == ["unaccounted", "unaccounted"]
    assert by[("r2/A5.json", "AF")]["status"] == "unaccounted"
    assert by[("r2/S13.json", "septic shock")] | {"status": "unaccounted", "occurrences": 1,
                                                  "why": "not_rendered_and_no_known_refusal_code"} \
        == by[("r2/S13.json", "septic shock")]


def _all_strings(obj, new: str):
    if isinstance(obj, dict):
        return {k: _all_strings(v, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_all_strings(v, new) for v in obj]
    return new if isinstance(obj, str) else obj


def test_no_saved_string_of_a_generations_output_is_printed(eight, tmp_path):
    """The widest form of the same rule: in every run-2 record of the four scenarios, EVERY string of the saved
    step-7 output (all but the renderer's identity block, which gate 1 pins) and of the refusal is prose.
    The gates hold, the tool reports, and none of it is in the output."""
    w = FX.copy_world(eight, tmp_path / "w")
    for sid in ("A5", "S10", "S13", "S3"):
        p = w.batch / "r2" / f"{sid}.json"
        rec = json.loads(p.read_text(encoding="utf-8"))
        s7 = rec["steps"].get("compose_patient")
        if s7 is not None:
            rec["steps"]["compose_patient"] = _all_strings(s7, PROSE) | {"renderer": s7["renderer"]}
        if rec.get("render_refusal") is not None:
            rec["render_refusal"] = _all_strings(rec["render_refusal"], PROSE)
        p.write_text(H._dump(rec), encoding="utf-8")
    rc, said = run(w, tmp_path / "out")
    assert rc == 0
    body = (tmp_path / "out" / KR.REPORT_JSON).read_text(encoding="utf-8")
    assert PROSE not in said + body + (tmp_path / "out" / KR.REPORT_MD).read_text(encoding="utf-8")
    second = [r for r in json.loads(body)["rows"] if r["record"].startswith("r2/")]
    assert len(second) == 8 and {r["status"] for r in second} <= {"unaccounted", "absent_from_input", "no_input"}
    assert sum(r["status"] == "unaccounted" for r in second) == 6               # every term that run 2 holds


# ---------------------------------------------------------------------------------------------------------
# the destination: only ever created, never replaced, never written through a link (Codex 13 Nov 2026 13:38)
# ---------------------------------------------------------------------------------------------------------

def test_an_existing_report_is_never_replaced(plain, tmp_path, analysed):
    out = tmp_path / "out"
    assert run(plain, out, PLAIN_TERMS)[0] == 0 and analysed == [FX.BATCH_ID]
    kept = tree(out)
    rc, said = run(plain, out, PLAIN_TERMS)                                      # the same folder again
    assert rc == 2 and said.strip().endswith("out:report_exists") and tree(out) == kept
    assert analysed == [FX.BATCH_ID]                                             # refused before any gate
    (out / KR.REPORT_JSON).unlink()                                              # either name is enough
    rc, said = run(plain, out, PLAIN_TERMS)
    assert rc == 2 and said.strip().endswith("out:report_exists") and not (out / KR.REPORT_JSON).exists()


@pytest.mark.parametrize("name", [KR.REPORT_JSON, KR.REPORT_MD])
def test_a_link_at_the_destination_cannot_reach_the_verdict_or_the_sealed_sheets(plain, tmp_path, analysed, name):
    w = FX.copy_world(plain, tmp_path / "w")
    targets = (w.batch / GS.OUT_DIR / KR.VERDICT_FILE, w.sheets / GS.SEAL_FILE,
               sorted((w.sheets / "scores").glob("*.json"))[0], w.batch / "r1" / "S1.json",
               w.batch / "not-there-yet.json", w.sheets / "not-there-yet.json")  # the last two: dangling links
    files = tree(w.root)
    for i, target in enumerate(targets):
        out = tmp_path / f"out{i}"
        out.mkdir()
        (out / name).symlink_to(target)
        rc, said = run(w, out, PLAIN_TERMS)
        assert rc == 2 and said.strip().endswith("out:report_exists"), target
        assert (out / name).is_symlink() and sorted(q.name for q in out.iterdir()) == [name]
        assert tree(w.root) == files and analysed == [], target                  # protected files: the same bytes
    assert not (w.batch / "not-there-yet.json").exists() and not (w.sheets / "not-there-yet.json").exists()


@pytest.mark.parametrize("name", [KR.REPORT_JSON, KR.REPORT_MD])
def test_a_link_that_appears_after_the_check_is_still_not_written_through(plain, tmp_path, monkeypatch, name):
    """The write itself creates exclusively: even with the first check out of the way, a link is not followed."""
    w = FX.copy_world(plain, tmp_path / "w")
    files = tree(w.root)
    monkeypatch.setattr(KR, "out_problem", lambda out, batch, sheets: None)
    for i, target in enumerate((w.batch / GS.OUT_DIR / KR.VERDICT_FILE, w.sheets / GS.SEAL_FILE,
                                w.batch / "not-there-yet.json")):
        out = tmp_path / f"out{i}"
        out.mkdir()
        (out / name).symlink_to(target)
        rc, said = run(w, out, PLAIN_TERMS)
        assert rc == 2 and said.strip().endswith("out:not_writable") and "no report file is left" in said
        assert tree(w.root) == files, target
        assert sorted(q.name for q in out.iterdir()) == [name] and (out / name).is_symlink()      # no half pair
    assert not (w.batch / "not-there-yet.json").exists()


def test_the_pair_is_published_whole_or_not_at_all(plain, tmp_path, monkeypatch):
    real, calls = os.open, []

    def second_fails(path, flags, mode=0o777, **kw):
        if str(path).endswith((KR.REPORT_JSON, KR.REPORT_MD)):
            calls.append(Path(path).name)
            if len(calls) == 2:
                raise OSError("disk full")
        return real(path, flags, mode, **kw)
    monkeypatch.setattr(KR.os, "open", second_fails)
    out = tmp_path / "new" / "out"
    rc, said = run(plain, out, PLAIN_TERMS)
    assert calls == [KR.REPORT_JSON, KR.REPORT_MD]
    assert rc == 2 and said.strip().endswith("out:not_writable") and "no report file is left" in said
    assert not out.exists()                                                      # the folder it made is gone too
    kept = tmp_path / "kept"
    kept.mkdir()
    (kept / "mine.txt").write_text("x", encoding="utf-8")
    calls.clear()
    rc, _ = run(plain, kept, PLAIN_TERMS)
    assert rc == 2 and sorted(q.name for q in kept.iterdir()) == ["mine.txt"]    # a folder it did not make stays
    empty = tmp_path / "empty"
    empty.mkdir()
    calls.clear()
    assert run(plain, empty, PLAIN_TERMS)[0] == 2 and empty.is_dir() and not list(empty.iterdir())   # even an empty one


def test_a_half_written_file_is_removed(plain, tmp_path, monkeypatch):
    real = os.fdopen

    def fails_on_write(fd, *a, **kw):
        os.close(fd)
        raise OSError("disk full")
    monkeypatch.setattr(KR.os, "fdopen", fails_on_write)
    rc, said = run(plain, tmp_path / "out", PLAIN_TERMS)
    monkeypatch.setattr(KR.os, "fdopen", real)
    assert rc == 2 and said.strip().endswith("out:not_writable") and not (tmp_path / "out").exists()


def test_a_partial_report_that_cannot_be_removed_is_said_plainly(plain, tmp_path, monkeypatch):
    real, calls = os.open, []

    def second_fails(path, flags, mode=0o777, **kw):
        if str(path).endswith((KR.REPORT_JSON, KR.REPORT_MD)):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disk full")
        return real(path, flags, mode, **kw)

    def no_unlink(self, *a, **kw):
        raise OSError("read-only")
    monkeypatch.setattr(KR.os, "open", second_fails)
    monkeypatch.setattr(Path, "unlink", no_unlink)
    rc, said = run(plain, tmp_path / "out", PLAIN_TERMS)
    monkeypatch.undo()
    assert rc == 2 and said.strip().endswith("out:partial_report_left") and "a partial report is left" in said
    assert (tmp_path / "out" / KR.REPORT_JSON).exists() and not (tmp_path / "out" / KR.REPORT_MD).exists()


def test_the_report_is_the_same_bytes_every_time(eight, tmp_path):
    run(eight, tmp_path / "a")
    run(eight, tmp_path / "b")
    for name in (KR.REPORT_JSON, KR.REPORT_MD):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_it_renders_nothing(eight, tmp_path, monkeypatch):
    def no(name):
        def guard(*a, **kw):
            pytest.fail(f"{name} was called")
        return guard
    for mod, name in ((RP, "render_patient"), (RP, "render_line"), (RP, "audit"), (H, "render_batch"),
                      (GS, "synthetic_case")):
        monkeypatch.setattr(mod, name, no(name))
    assert run(eight, tmp_path / "out")[0] == 0


def test_the_tool_is_pinned_by_the_freeze(plain):
    """Committed before the freeze, so the frozen instrument includes it (and a later edit is refused)."""
    frozen = json.loads(plain.freeze.read_text(encoding="utf-8"))
    assert "evals/r1_kf19_report.py" in frozen["files"] and "tests/test_r1_kf19_report.py" in frozen["files"]
    assert FZ.check(plain.freeze)[2] == []
