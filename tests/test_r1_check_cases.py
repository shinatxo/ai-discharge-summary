"""Item 3 R1 step 4b — the case-list validator (evals/r1_check_cases.py), on an invented fixture.

Everything here is INVENTED and built in a temporary folder: a five-run source batch from the existing
$0 fake (tests/r1_fixture.py), its own locating pass, its own reading sheet and a case list written by
this file. Never the 90 saved records, never the candidate list, the located file or the reading
sheet of the real pass, and no wording from them. What IS taken from the adopted design, because the
fixed obligations cannot be exercised without it: scenario ids, run and record paths, section ids,
line and sentence numbers, known-failure numbers, table entry ids, the adopted short patterns and the
ids, outcome types and refusal codes of the three controls (SX1-SX3) — their text is invented here.

The positive control passes the PRODUCTION obligations table with invented text at those coordinates.
Every other test is the positive control plus one deliberate break, and asserts the fixed code that
names it. The fixture's identities are passed from Python; the command line cannot take them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import r1_fixture as FX  # noqa: E402
import r1_check_cases as CC  # noqa: E402
import r1_go_stop as GS  # noqa: E402
import r1_locate_cases as LC  # noqa: E402
import render_offline as H  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402

# Invented PART A text at the coordinates the adopted obligations name (S1 presenting_complaint; S18
# sections.3 line 1 sentence 1; A5 diagnosis; S14 sections.3 line 5 in runs 2 and 4).
_S14 = {"treatment": "- Fluids given.\n- Pain relief given.\n- Seen by the physio.\n- Diet as usual.\n"
                     "- Oral iron (inferred — not documented, confirm)"}
VARIANTS = {
    "S1": {"presenting_complaint": "Came in after 2 weeks of a sore left knee."},
    "S18": {"treatment": "No stitches were needed. Dressing changed on the ward."},
    "A5": {"diagnosis": "Primary: Chest infection, CURB-65 1\nBackground:\n- Asthma"},
    "r2/S14": _S14, "r4/S14": _S14,
}
SYN = "SYNTHETIC — invented boundary control, not from the 90"
REASON = {"section_id": "sections.0", "heading": "presenting_complaint"}
CONTROLS = [        # invented here, with the adopted ids, outcome types and refusal codes (SX1-SX3)
    {"case_id": "SX1", "label": SYN,
     "input": {"audience": "adult", "sections": [
         REASON | {"text": "Came in after slipping on wet leaves."},
         {"section_id": "sections.7", "heading": "follow_up",
          "text": "- Notes conflict: eye clinic in 3 days vs 9 days — requires clarification."}]},
     "at": {"source_id": "sections.7", "line": 1, "sentence": None},
     "expected": {"outcome": "refusal", "code": "conflict_in_input"}},
    {"case_id": "SX2", "label": SYN,
     "input": {"audience": "adult", "sections": [
         REASON | {"text": "Came in with a painful right wrist."},
         {"section_id": "sections.1", "heading": "diagnosis", "text": "Primary: High risk right wrist sprain"}]},
     "at": {"source_id": "sections.1", "line": 1, "sentence": None},
     "expected": {"outcome": "refusal", "code": "withheld_not_isolable"}},
    {"case_id": "SX3", "label": SYN,
     "input": {"audience": "adult", "sections": [
         REASON | {"text": "Came in with an itchy rash."},
         {"section_id": "sections.7", "heading": "follow_up",
          "text": "- District nurse to look at the rash in 4 days; stop the cream if the itching settles."}]},
     "at": {"source_id": "sections.7", "line": 1, "sentence": None},
     "expected": {"outcome": "retained", "no_drop": True, "must_keep": [r"\bif\b", r"\bdays\b", r"\b4\b"]}},
]
KF18 = ("sections.3", 1, 1)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def expected_for(c: dict) -> dict:
    """An expectation per located fixture case — written from the category, never from an output."""
    cat, screen = c["category"], [t for t in c["screen"] if not t.startswith("meets:")]
    if cat == "U1":
        e = {"outcome": "retained", "no_drop": True, "must_keep": [r"\b2\b", r"\bweeks\b"],
             "c_must_match": [r"\b2 weeks\b"]}
    elif cat == "L1":
        e = {"outcome": "approved_omission",
             "drop": {"rule": "withheld", "entry": next(t for t in screen if t.startswith("w_"))}}
    elif cat == "L6i":
        e = {"outcome": "retained", "no_drop": True, "c_must_match": [CC.INFERRED_EXACT]}
    elif cat == "L2" and c["scenario_id"] == "S18" and (c["source_id"], c["line"], c["sentence"]) == KF18:
        e = {"outcome": "retained", "must_keep": [r"\bno\b"], "c_must_not_match": [r"(?i)\brisk\b"]}
    elif cat == "L2":
        e = {"outcome": "retained", "no_drop": True, "must_keep": [rf"\b{t}\b" for t in screen]}
    else:
        e = {"outcome": "retained", "no_drop": True}
    return e | {"rule": "fixture"}


def kf_entries(ob: CC.Obligations = CC.OBLIGATIONS) -> list:
    n_of = Counter(n for n, _sid, _checks in ob.known_failures)
    return [{"kf_id": f"KF-{n:02d}" + (f"-{sid}" if n_of[n] > 1 else ""), "scenario_id": sid, "runs": "all",
             "source": "fixture", "checks": copy.deepcopy(list(checks))}
            for n, sid, checks in ob.known_failures]


@dataclass
class Fx:
    root: Path
    src: Path
    inputs: Path
    located: Path
    sheet: Path
    identity: CC.Identity
    cases: dict                     # the valid fixture list (never changed: tests work on a deep copy)
    located_doc: dict

    def write(self, where: Path, cases: dict) -> Path:
        where.mkdir(parents=True, exist_ok=True)
        p = where / "cases.json"
        p.write_text(json.dumps(cases, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return p

    def check(self, tmp_path: Path, edit=None, **kw) -> CC.Report:
        cases = copy.deepcopy(self.cases)
        if edit is not None:
            edit(cases)
        args = dict(identity=self.identity)
        args.update(kw)
        return CC.check(self.write(tmp_path, cases), args.pop("batch", self.src), args.pop("inputs", self.inputs),
                        args.pop("located", self.located), args.pop("sheet", self.sheet), **args)

    def argv(self, cases: Path) -> list:
        return [str(cases), "--batch", str(self.src), "--inputs", str(self.inputs), "--located", str(self.located),
                "--reading-sheet", str(self.sheet)]


def _write_pass(root: Path, located: dict, reading: list) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    lp, sp = root / "cases-located.json", root / "CASE-READING.md"
    lp.write_text(json.dumps(located, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sp.write_text(LC.reading_sheet(located, reading), encoding="utf-8")
    return lp, sp


def _identity(inputs: Path, lp: Path, sp: Path) -> CC.Identity:
    return CC.Identity(sha(inputs.read_bytes()), sha(lp.read_bytes()), sha(sp.read_bytes()),
                       CC.PRODUCTION.locator_sha256)


def make_list(located: dict, identity: CC.Identity) -> dict:
    return {
        "cases_version": GS.CASES_VERSION, "inputs_sha256": located["inputs_sha256"],
        "provenance": {"status": FX.FIXTURE_NOTE, "locator_sha256": identity.locator_sha256,
                       "located_sha256": identity.located_sha256,
                       "reading_sheet_sha256": identity.reading_sheet_sha256},
        "located": [{k: c[k] for k in CC.PASS_KEYS} | {"expected": expected_for(c)} for c in located["cases"]],
        "synthetic": copy.deepcopy(CONTROLS),
        "known_failures": kf_entries(),
        "generations": [{"case_id": "U2-B6-r4", "record": "r4/B6.json", "expected": "rendered",
                         "c_must_not_match": [r"notes\s+conflict"], "rule": "fixture"}],
    }


@pytest.fixture(scope="module")
def fx(tmp_path_factory) -> Fx:
    root = tmp_path_factory.mktemp("r1-check")
    src, inputs = FX.make_source(root / "s", runs=5, variants=VARIANTS)
    located, reading = LC.locate(src, inputs)
    lp, sp = _write_pass(root / "pass", located, reading)
    identity = _identity(inputs, lp, sp)
    return Fx(root, src, inputs, lp, sp, identity, make_list(located, identity), located)


@pytest.fixture(scope="module")
def rendered(fx, tmp_path_factory):
    """The fixture source rendered once by the unchanged harness (invented text) — for the tests that
    compare the validator with what the evaluator will do on a rendered batch."""
    out = tmp_path_factory.mktemp("r1-check-rendered") / "fx-check"
    H.render_batch(fx.src, out, fx.inputs, "fx-check", tables=RP.default_tables())
    return GS.Candidate(out)


def case(cases: dict, case_id: str) -> dict:
    return next(c for part in CC.PARTS for c in cases[part] if c.get("case_id") == case_id)


def kf(cases: dict, kf_id: str) -> dict:
    return next(k for k in cases["known_failures"] if k["kf_id"] == kf_id)


def drop(cases: dict, part: str, key: str, value: str) -> None:
    cases[part] = [c for c in cases[part] if c.get(key) != value]


FIRST = "L2-S2-r1-sections.2-1-0"            # an ordinary located fixture case: "- CTPA: no PE.", keeping "no"
S18_R3 = "L2-S18-r3-sections.3-1-1"


# =================================================================================================
# the positive control, the report and the exit codes
# =================================================================================================

def test_positive_control_the_fixture_list_passes_the_production_obligations_table(fx, tmp_path):
    rep = fx.check(tmp_path)                                   # obligations = the tool's own default
    assert rep.problems == [] and rep.notes == [] and rep.exit_code == 0
    c = rep.counts
    assert (c["records"], c["synthetic"], c["known_failure_entries"], c["known_failure_checks"],
            c["generations"]) == (90, 3, 28, 58, 1)
    assert c["located"] == c["pass_cases"] == len(fx.located_doc["cases"]) > 300
    assert c["retained"] + c["approved_omission"] + c["refusal"] == c["located"] and c["approved_omission"] > 0
    assert rep.sha256 == sha((tmp_path / "cases.json").read_bytes())


def test_the_fixture_exercises_every_adopted_coordinate(fx):
    ids = {c["case_id"] for c in fx.cases["located"]}
    assert {f"U1-S1-r{n}-sections.0-1-1" for n in range(1, 6)} <= ids
    assert {f"L2-S18-r{n}-sections.3-1-1" for n in range(1, 6)} <= ids
    assert {f"L1-A5-r{n}-sections.1-1-0" for n in range(1, 6)} <= ids
    assert {"L6i-S14-r2-sections.3-5-0", "L6i-S14-r4-sections.3-5-0"} <= ids


def test_the_obligations_table_is_the_adopted_set():
    """Typed here from the adopted table's row (PART3-R2-fixes.md, Fix 1) — 28 entries, 58 checks."""
    adopted = {1: ["S1"], 2: ["S4"], 3: ["S12"], 4: ["S10", "S1", "S14"], 5: ["S1", "S10"], 6: ["S16"],
               7: ["S2", "S4", "S18"], 8: ["S11", "S3"], 9: ["S13"], 10: ["S3"], 11: ["S16"], 12: ["S1"],
               13: ["S1"], 14: ["S13"], 15: ["S16"], 16: ["A5"], 17: ["S3"], 18: ["S18"],
               19: ["A5", "S10", "S13", "S3"]}
    table = CC.OBLIGATIONS.known_failures
    got: dict = {}
    for n, sid, _checks in table:
        got.setdefault(n, []).append(sid)
    assert got == adopted and len(table) == 28 and sum(len(c) for _n, _s, c in table) == 58
    assert len({(n, s) for n, s, _c in table}) == 28
    kinds = {1: "ADD KEEP RULED", 2: "ADD KEEP KEEP RULED", 3: "ADD RULED", 4: "ADD RULED", 5: "ADD RULED",
             6: "ADD RULED", 7: "ADD RULED", 8: "ADD RULED", 9: "KEEP RULED", 10: "DIAG RULED",
             11: "ADD ADD KEEP RULED", 12: "ADD KEEP", 13: "DIAG", 14: "ADD KEEP KEEP", 15: "ADD KEEP",
             16: "ADD KEEP KEEP", 17: "KEEP KEEP KEEP", 18: "WITHHELD", 19: "BLIND"}
    assert all(" ".join(c["type"] for c in cs) == kinds[n] for n, _s, cs in table)
    copy_only = sorted((n, s) for n, s, cs in table for c in cs if c.get("copy_only"))
    assert copy_only == [(2, "S4"), (4, "S1"), (4, "S10"), (4, "S14"), (5, "S1"), (5, "S10"), (6, "S16"), (7, "S18"),
                         (7, "S2"), (7, "S4"), (8, "S11"), (8, "S3"), (11, "S16"), (12, "S1"), (14, "S13"), (15, "S16")]
    assert sorted((n, c["not_from"]) for n, _s, cs in table for c in cs if "not_from" in c) == \
        [(1, "angiogra"), (3, r"AXR|CXR|X-?ray"), (11, "communit"), (16, r"CRP|WCC")]
    assert CC.OBLIGATIONS.runs == (1, 2, 3, 4, 5)
    assert CC.OBLIGATIONS.synthetic == (("SX1", "refusal", "conflict_in_input"),
                                        ("SX2", "refusal", "withheld_not_isolable"), ("SX3", "retained", None))
    assert all(c == {"type": "BLIND", "item": s, "dimension": "audience"} for n, s, cs in table if n == 19 for c in cs)


def test_main_prints_the_sha256_counts_ids_and_codes_and_no_wording(fx, tmp_path, capsys):
    good = fx.write(tmp_path / "good", fx.cases)
    assert CC.main(fx.argv(good), _identity=fx.identity) == 0
    out = capsys.readouterr().out
    assert sha(good.read_bytes()) in out and "WELL-FORMED (exit 0)" in out and "problems: 0" in out
    bad = copy.deepcopy(fx.cases)
    case(bad, FIRST)["expected"]["must_keep"] = [r"\bzebra\b"]
    case(bad, "SX1")["label"] = "a control"
    assert CC.main(fx.argv(fx.write(tmp_path / "bad", bad)), _identity=fx.identity) == 1
    out = capsys.readouterr().out
    assert f"  {FIRST}: must_keep[0]:term_not_in_its_unit" in out and "  SX1: synthetic:not_labelled" in out
    assert "PROBLEMS (exit 1)" in out and "zebra" not in out
    texts = list(FX.A_TEXT.values()) + [t for v in VARIANTS.values() for t in v.values()] + \
        [s["text"] for c in CONTROLS for s in c["input"]["sections"]]
    for line in (ln.lstrip("- ") for t in texts for ln in t.split("\n")):
        assert len(line.split()) < 3 or line not in out, line


def test_exit_2_is_a_refusal_and_names_its_code(fx, tmp_path, capsys):
    good = fx.write(tmp_path, fx.cases)
    assert CC.main(fx.argv(good) + ["--expect-sha256", "0" * 64], _identity=fx.identity) == 2
    assert "REFUSED (exit 2): cases:sha256_not_the_expected" in capsys.readouterr().err
    assert CC.main(fx.argv(good) + ["--expect-sha256", sha(good.read_bytes())], _identity=fx.identity) == 0


def test_an_unsafe_id_is_printed_as_a_position_never_as_text(fx, tmp_path):
    def edit(cases):
        cases["located"][0]["case_id"] = "some words that could be wording"
    rep = fx.check(tmp_path, edit)
    assert ("located[0]", "located:added_since_the_pass") in rep.problems
    assert not any("wording" in w for w, _c in rep.problems)


# =================================================================================================
# B — the identities: the tests pass invented ones from Python; the command line cannot
# =================================================================================================

def test_production_identities_are_the_recorded_hashes():
    assert CC.PRODUCTION == CC.Identity(
        inputs_sha256="028b6ae2d276225b5fc526bb5ae576809dee48fa461f11edec8a20276f98d8c1",
        located_sha256="b502a496e0ff2937ecee57496d77c36989139521e10820c8890b87874423a42f",
        reading_sheet_sha256="fc22d7309a7ecba728f4b454436c2b73cb4df6a6e3d94b2021f913a8dd414279",
        locator_sha256="a4df06525836cd53ebae1cde9757c0872d6ddf441380bcbad043826a2e093dd5")
    assert CC.PRODUCTION.inputs_sha256 == sha((ROOT / "evals" / "r1" / "inputs.json").read_bytes())


def test_the_command_line_has_exactly_six_arguments_and_none_sets_an_identity():
    actions = [a for a in CC.parser()._actions if a.dest != "help"]
    assert sorted(a.dest for a in actions) == ["batch", "cases", "expect_sha256", "inputs", "located", "reading_sheet"]
    assert {a.dest for a in actions if a.required} == {"cases", "batch", "inputs", "located", "reading_sheet"}
    source = (ROOT / "evals" / "r1_check_cases.py").read_text(encoding="utf-8")
    assert "os.environ" not in source and "getenv" not in source and source.rstrip().endswith("sys.exit(main())")


def test_main_uses_the_production_constants_unless_python_passes_others(fx, tmp_path, monkeypatch):
    seen = {}

    def spy(*a, **kw):
        seen.update(kw)
        raise CC.Refused("spy")
    monkeypatch.setattr(CC, "check", spy)
    assert CC.main(fx.argv(tmp_path / "x.json")) == 2
    assert seen["identity"] is CC.PRODUCTION and seen["obligations"] is CC.OBLIGATIONS and seen["expect_sha256"] is None


def test_the_real_command_refuses_a_valid_fixture_at_the_located_file(fx, tmp_path):
    """A separate process, no Python override: a fixture can never pass the command line."""
    good = fx.write(tmp_path, fx.cases)
    done = subprocess.run([sys.executable, "-B", str(ROOT / "evals" / "r1_check_cases.py")] + fx.argv(good),
                          capture_output=True, text=True, cwd=str(tmp_path))
    assert done.returncode == 2 and done.stdout == ""
    assert done.stderr.strip().splitlines()[-1] == "REFUSED (exit 2): located:identity_mismatch"


def test_both_pass_files_are_mandatory_on_the_command_line(fx, tmp_path):
    argv = fx.argv(fx.write(tmp_path, fx.cases))
    for flag in ("--located", "--reading-sheet"):
        i = argv.index(flag)
        with pytest.raises(SystemExit) as exc:
            CC.main(argv[:i] + argv[i + 2:], _identity=fx.identity)
        assert exc.value.code == 2


def test_a_substituted_located_file_is_refused(fx, tmp_path):
    other = tmp_path / "other.json"
    other.write_bytes(fx.located.read_bytes() + b"\n")
    with pytest.raises(CC.Refused, match="^located:identity_mismatch$"):
        fx.check(tmp_path, located=other)


def test_a_shortened_list_beside_a_matching_shortened_located_file_is_refused(fx, tmp_path):
    """Round 2's case: with the pass's identity unchanged, a shorter located file cannot stand in for it."""
    short = copy.deepcopy(fx.located_doc)
    gone = short["cases"].pop()["case_id"]
    lp, _sp = _write_pass(tmp_path / "short", short, [])
    with pytest.raises(CC.Refused, match="^located:identity_mismatch$"):
        fx.check(tmp_path, lambda cases: drop(cases, "located", "case_id", gone), located=lp)
    # ...and only an identity that names the shortened file would let it through — the command line has none.
    rep = fx.check(tmp_path, lambda cases: drop(cases, "located", "case_id", gone))
    assert (gone, "located:lost_since_the_pass") in rep.problems


def test_a_substituted_reading_sheet_is_refused(fx, tmp_path):
    other = tmp_path / "sheet.md"
    other.write_bytes(fx.sheet.read_bytes().replace(b"NEVER COMMITTED", b"never committed"))
    with pytest.raises(CC.Refused, match="^reading_sheet:identity_mismatch$"):
        fx.check(tmp_path, sheet=other)


def test_a_located_file_written_for_other_inputs_is_refused(fx, tmp_path):
    doc = copy.deepcopy(fx.located_doc) | {"inputs_sha256": "0" * 64}
    lp, sp = _write_pass(tmp_path / "p", doc, [])
    ident = replace(fx.identity, located_sha256=sha(lp.read_bytes()), reading_sheet_sha256=sha(sp.read_bytes()))
    with pytest.raises(CC.Refused, match="^located:written_for_other_inputs$"):
        fx.check(tmp_path, located=lp, sheet=sp, identity=ident)


@pytest.mark.parametrize("which", ["cases", "located", "sheet", "inputs", "batch"])
def test_a_missing_file_is_refused(fx, tmp_path, which):
    codes = {"cases": "cases:not_readable", "located": "located:not_readable", "sheet": "reading_sheet:not_readable",
             "inputs": "inputs:not_readable", "batch": "batch:not_readable"}
    if which == "cases":
        with pytest.raises(CC.Refused, match=f"^{codes[which]}$"):
            CC.check(tmp_path / "none.json", fx.src, fx.inputs, fx.located, fx.sheet, identity=fx.identity)
    else:
        with pytest.raises(CC.Refused, match=f"^{codes[which]}$"):
            fx.check(tmp_path, **{which: tmp_path / "none"})


def test_a_list_that_is_not_a_json_object_is_refused(fx, tmp_path):
    for text in ("[]", "{not json"):
        p = tmp_path / "cases.json"
        p.write_text(text, encoding="utf-8")
        with pytest.raises(CC.Refused, match="^cases:not_a_json_object$"):
            CC.check(p, fx.src, fx.inputs, fx.located, fx.sheet, identity=fx.identity)


def test_an_inputs_file_that_is_not_the_pinned_one_is_refused(fx, tmp_path):
    other = tmp_path / "inputs.json"
    other.write_bytes(fx.inputs.read_bytes() + b" ")
    with pytest.raises(CC.Refused, match="^inputs:identity_mismatch$"):
        fx.check(tmp_path, inputs=other)


def test_inputs_that_do_not_match_the_batch_are_refused(fx, tmp_path):
    """A1: the inputs file must equal what the harness's own function derives from the batch."""
    doc = json.loads(fx.inputs.read_text(encoding="utf-8"))
    doc["generations"][0]["input_sha256"] = "0" * 64
    other = tmp_path / "inputs.json"
    other.write_text(H._dump(doc), encoding="utf-8")
    ident = replace(fx.identity, inputs_sha256=sha(other.read_bytes()))
    lp, sp = _write_pass(tmp_path / "p", fx.located_doc | {"inputs_sha256": ident.inputs_sha256}, [])
    ident = replace(ident, located_sha256=sha(lp.read_bytes()), reading_sheet_sha256=sha(sp.read_bytes()))
    with pytest.raises(CC.Refused, match="^inputs:do_not_match_the_batch$"):
        fx.check(tmp_path, inputs=other, located=lp, sheet=sp, identity=ident)


@pytest.mark.parametrize("field", ["located_sha256", "reading_sheet_sha256", "locator_sha256", None])
def test_the_lists_provenance_must_name_the_pass(fx, tmp_path, field):
    def edit(cases):
        if field is None:
            del cases["provenance"]
        else:
            cases["provenance"][field] = "0" * 64
    assert fx.check(tmp_path, edit).problems == [("list", "provenance:pass_identity_mismatch")]


# =================================================================================================
# the fourteen checks — one named test each, one deliberate break each
# =================================================================================================

@pytest.mark.parametrize("edit, where, code", [
    (lambda c: c.update(adopted={"by": "x"}), "list", "schema:unknown_key"),
    (lambda c: c.update(cases_version=2), "list", "schema:version"),
    (lambda c: c.update(cases_version=True), "list", "schema:version"),
    (lambda c: c.update(generations={}), "generations", "schema:part_not_a_list"),
    (lambda c: c["generations"].append("U3"), "generations[1]", "schema:not_an_object"),
    (lambda c: case(c, FIRST).update(note="x"), FIRST, "schema:unknown_key"),
    (lambda c: case(c, FIRST).pop("heading"), FIRST, "schema:key_missing:heading"),
    (lambda c: case(c, "SX3").update(why="x"), "SX3", "schema:unknown_key"),
    (lambda c: kf(c, "KF-13").update(note="x"), "KF-13", "schema:unknown_key"),
    (lambda c: case(c, "U2-B6-r4").update(note="x"), "U2-B6-r4", "schema:unknown_key"),
])
def test_check_1_schema_and_version(fx, tmp_path, edit, where, code):
    assert (where, code) in fx.check(tmp_path, edit).problems


def test_check_2_the_lists_inputs_hash_is_the_pinned_one(fx, tmp_path):
    rep = fx.check(tmp_path, lambda c: c.update(inputs_sha256="0" * 64))
    assert rep.problems == [("list", "inputs:sha256_mismatch")]


@pytest.mark.parametrize("edit, where, code", [
    (lambda c: case(c, "SX2").update(case_id="SX1"), "SX1", "ids:duplicate"),
    (lambda c: case(c, "U2-B6-r4").update(case_id=FIRST), FIRST, "ids:duplicate"),          # across two parts
    (lambda c: kf(c, "KF-13").update(kf_id="KF-12"), "KF-12", "ids:duplicate"),
    (lambda c: case(c, "U2-B6-r4").update(case_id=""), "generations[0]", "ids:missing"),
    (lambda c: case(c, "SX3").pop("case_id"), "synthetic[2]", "ids:missing"),
])
def test_check_3_ids_are_present_and_never_repeated(fx, tmp_path, edit, where, code):
    assert (where, code) in fx.check(tmp_path, edit).problems


LOCATION_BREAKS = [
    (lambda c: c.update(record="r9/S2.json"), "located:record_not_in_the_inputs"),
    (lambda c: c.update(source_id="sections.44"), "located:no_such_section"),
    (lambda c: c.update(line=0), "located:line_out_of_range"),
    (lambda c: c.update(line=99), "located:line_out_of_range"),
    (lambda c: c.update(line_sha256="0" * 64), "located:line_sha256_mismatch"),
]


@pytest.mark.parametrize("edit, code", LOCATION_BREAKS)
def test_check_4_every_located_case_is_in_the_frozen_input(fx, tmp_path, edit, code):
    rep = fx.check(tmp_path, lambda cases: edit(case(cases, FIRST)))
    assert (FIRST, code) in rep.problems


@pytest.mark.parametrize("edit, code", [
    (lambda e: e.update(outcome="dropped"), "outcome:not_recognised"),
    (lambda e: e.pop("outcome"), "outcome:not_recognised"),
    (lambda e: e.update(drop={"rule": "withheld", "entry": "w_grace"}), "outcome:key_not_allowed:drop"),
    (lambda e: e.update(code="conflict_in_input"), "outcome:key_not_allowed:code"),
    (lambda e: e.update(prose_must_match=["x"]), "outcome:key_not_allowed:prose_must_match"),   # located: no prose
    (lambda e: e.update(must_keep="no"), "schema:type:must_keep"),
    (lambda e: e.update(no_drop="yes"), "schema:type:no_drop"),
])
def test_check_5_outcome_types_and_their_keys(fx, tmp_path, edit, code):
    assert (FIRST, code) in fx.check(tmp_path, lambda cases: edit(case(cases, FIRST)["expected"])).problems


def test_check_6_the_loaded_withheld_table_holds_the_built_in_resuscitation_entry():
    """Check 6 accepts `withheld` + RP.RESUS_ENTRY because the table as loaded holds it (no second list here)."""
    tables = RP.default_tables()
    assert RP.RESUS_ENTRY in {e.id for e in tables.withheld} and tables.home_medicine == ()
    assert RP.RESUS_ENTRY not in {e.id for e in tables.seek_help}


def test_check_5_a_refusal_names_a_code_the_renderer_can_raise(fx, tmp_path):
    def refusal(code, **more):
        return lambda cases: case(cases, FIRST).update(expected={"outcome": "refusal", "code": code} | more)
    assert fx.check(tmp_path, refusal("unsupported_form")).codes(FIRST) == []
    assert fx.check(tmp_path, refusal("no_such_code")).codes(FIRST) == ["outcome:refusal_code"]
    assert fx.check(tmp_path, refusal(None)).codes(FIRST) == ["outcome:refusal_code"]
    assert fx.check(tmp_path, refusal("unsupported_form", must_keep=[r"\bno\b"])).codes(FIRST) == \
        ["outcome:key_not_allowed:must_keep"]                       # the evaluator would never read it


A5_R1 = "L1-A5-r1-sections.1-1-0"


@pytest.mark.parametrize("drops, codes", [
    ({"drop": {"rule": "withheld", "entry": "w_curb65"}}, []),
    ({"drop": {"rule": "withheld", "entry": "w_curb65"}, "drops": [{"rule": "withheld", "entry": RP.RESUS_ENTRY}]}, []),
    ({"drop": {"rule": "withheld", "entry": "w_curb65"}, "drops": [{"rule": "resus_conflict", "entry": None}]}, []),
    ({"drop": {"rule": "withheld", "entry": "w_curb65"}, "drops": [{"rule": "seek_help", "entry": "s_111"}]}, []),
    ({"drop": {"rule": "withheld", "entry": "w_curb65"}, "drops": [{"rule": "resus_conflict", "entry": RP.RESUS_ENTRY}]},
     ["omission:entry"]),
    ({"drop": {"rule": "withheld", "entry": "w_curb65"}, "drops": [{"rule": "seek_help", "entry": RP.RESUS_ENTRY}]},
     ["omission:entry"]),
    ({"drop": {"rule": "withheld", "entry": "w_curb65"}, "drops": [{"rule": "home_medicine", "entry": "h_any"}]},
     ["omission:entry"]),                                           # the approved home-medicine table is empty
    ({"drop": {"rule": "withheld", "entry": "s_111"}}, ["obligation:boundary_case_missing", "omission:entry"]),
    ({"drop": {"rule": "glossary", "entry": "g_nstemi"}}, ["obligation:boundary_case_missing", "omission:rule"]),
    ({}, ["obligation:boundary_case_missing", "omission:no_drop_named"]),
    ({"drop": {"rule": "withheld", "entry": "w_curb65"}, "drops": ["w_grace"]}, ["schema:type:drop"]),
])
def test_check_6_an_omission_names_an_existing_entry_of_its_rule(fx, tmp_path, drops, codes):
    def edit(cases):
        case(cases, A5_R1)["expected"] = {"outcome": "approved_omission"} | drops
    rep = fx.check(tmp_path, edit)
    assert sorted(rep.codes(A5_R1) + rep.codes("A5-r1")) == codes


@pytest.mark.parametrize("pattern, code", [
    (r"\bno\b", None), ("camera test", None), (r"\bday \d+\b", None), (r"notes\s+conflict", None),
    (r"AXR|CXR|X-?ray", None), ("alpha|beta gamma", None), ("[Yy]our child", None), (r"(?i)\bpossible\b", None),
    (r"\?", None), (r"\bswabs?\b", None), (r"\bcaus\w*", None), ("one two three", None),
    ("alpha (?:beta|gamma) delta epsilon", "pattern:unsupported_form"),      # Codex 12 Nov 21:39
    (r"alpha beta\|gamma delta", "pattern:too_long"),                        # Codex 13 Nov 07:05: \| is a sign
    ("one two three four", "pattern:too_long"), (r"a\s+b c|d e\s*f-g h", "pattern:too_long"),
    (r"one\s*two-three\?four", "pattern:too_long"),
    ("(?:no)", "pattern:unsupported_form"), ("(?=no)no", "pattern:unsupported_form"), ("n.", "pattern:unsupported_form"),
    ("^no", "pattern:unsupported_form"), ("no$", "pattern:unsupported_form"), ("no{2}", "pattern:unsupported_form"),
    (r"(no)\1", "pattern:unsupported_form"), (r"\Bno", "pattern:unsupported_form"), ("[a-z]o", "pattern:unsupported_form"),
    ("[^n]o", "pattern:unsupported_form"), ("no|", "pattern:unsupported_form"), ("|no", "pattern:unsupported_form"),
    (r"\b", "pattern:unsupported_form"), (" ", "pattern:unsupported_form"), ("-", "pattern:unsupported_form"),
    ("no'one", "pattern:unsupported_form"), ("no,", "pattern:unsupported_form"),
    ("(", "pattern:does_not_compile"), ("[no", "pattern:does_not_compile"),
    ("", "pattern:empty"), ("s?", "pattern:empty"), (r"\w*", "pattern:empty"), (None, "pattern:empty"), (7, "pattern:empty"),
])
def test_check_7_the_pattern_rule(pattern, code):
    assert CC.pattern_problem(pattern) == code


def test_check_7_a_quantifier_stands_only_after_one_piece():
    """Whether Python itself rejects these differs by version; neither is ever accepted."""
    for p in (r"\b?no", "no?*", "*no"):
        assert CC.pattern_problem(p) in ("pattern:unsupported_form", "pattern:does_not_compile"), p


def test_check_7_the_two_fixed_phrases_are_exempt_by_exact_text_only():
    assert CC.INFERRED_EXACT == r"\(the notes point to this but do not say it; the team must check it\)"
    assert CC.INFERRED_HEAD == "the notes point to this"
    assert CC.pattern_problem(CC.INFERRED_EXACT) is None and CC.pattern_problem(CC.INFERRED_HEAD) is None
    assert CC.pattern_problem(CC.INFERRED_HEAD + " but") == "pattern:too_long"
    assert CC.pattern_problem(CC.INFERRED_EXACT.replace("check", "read")) == "pattern:unsupported_form"
    assert CC.pattern_problem("the notes point to that") == "pattern:too_long"


@pytest.mark.parametrize("edit, where, code", [
    (lambda c: case(c, FIRST)["expected"].update(must_keep=["one two three four"]), FIRST, "must_keep[0]:pattern:too_long"),
    (lambda c: case(c, FIRST)["expected"].update(c_must_match=[r"\bno\b", "("]), FIRST,
     "c_must_match[1]:pattern:does_not_compile"),
    (lambda c: case(c, FIRST)["expected"].update(c_must_not_match=[""]), FIRST, "c_must_not_match[0]:pattern:empty"),
    (lambda c: case(c, "SX3")["expected"].update(prose_must_not_match=["a.b"]), "SX3",
     "prose_must_not_match[0]:pattern:unsupported_form"),
    (lambda c: kf(c, "KF-01")["checks"][0].update(token="one two three four"), "KF-01", "token[0]:pattern:too_long"),
    (lambda c: kf(c, "KF-01")["checks"][0].update(not_from="(?:x)"), "KF-01", "not_from[0]:pattern:unsupported_form"),
    (lambda c: kf(c, "KF-01")["checks"][1].update(term=""), "KF-01", "term[0]:pattern:empty"),
    (lambda c: case(c, "U2-B6-r4").update(c_must_not_match=["one two three four"]), "U2-B6-r4",
     "c_must_not_match[0]:pattern:too_long"),
])
def test_check_7_every_pattern_field_is_held_to_the_rule(fx, tmp_path, edit, where, code):
    assert (where, code) in fx.check(tmp_path, edit).problems


def test_check_8_a_must_keep_term_is_found_in_its_own_unit(fx, tmp_path):
    def edit(cases):
        case(cases, FIRST)["expected"]["must_keep"] = [r"\bno\b", r"\bzebra\b"]
        case(cases, S18_R3)["expected"]["must_keep"] = [r"\bno\b", r"\bdressing\b"]   # in sentence 2, not in its own
    rep = fx.check(tmp_path, edit)
    assert rep.codes(FIRST) == ["must_keep[1]:term_not_in_its_unit"]
    assert rep.codes(S18_R3) == ["must_keep[1]:term_not_in_its_unit"]


def test_check_8_tests_an_item_without_its_marker_as_the_renderer_will(fx):
    sec = {"heading": "treatment", "text": "- No rash.\nNo fever. No cough.\n\n- -5 kg"}
    assert CC.unit_text(sec, 1, None) == "No rash." and CC.unit_text(sec, 2, 2) == "No cough."
    assert CC.unit_text(sec, 4, None) == "-5 kg" and CC.unit_text(sec, 3, None) is None
    assert CC.unit_text({"heading": "diagnosis", "text": "Primary: Gout"}, 1, None) == "Primary: Gout"
    # the same texts the renderer's own first stage gives (invented lines only)
    lines = RP.split_lines({"section_id": "sections.3", "heading": "treatment", "text": sec["text"]})
    assert [u["text"] for u in lines] == [CC.unit_text(sec, u["line"], u["sentence"]) for u in lines]


def test_check_9_a_retained_case_that_checks_nothing_is_reported(fx, tmp_path):
    def edit(cases):
        case(cases, FIRST)["expected"] = {"outcome": "retained", "rule": "fixture"}
        case(cases, S18_R3)["expected"] = {"outcome": "retained", "no_drop": False, "must_keep": []}
    rep = fx.check(tmp_path, edit)
    assert rep.codes(FIRST) == ["case:checks_nothing"]
    assert "case:checks_nothing" in rep.codes(S18_R3)


@pytest.fixture
def sentinel(monkeypatch):
    """In place of the evaluator's `synthetic_case()`: records every control that reaches the renderer."""
    called = []
    monkeypatch.setattr(GS, "synthetic_case", lambda c, tables: called.append(c.get("case_id")) or [])
    return called


def _control(text: str, heading: str = "treatment", **more) -> dict:
    return {"case_id": "SX9", "label": SYN,
            "input": {"audience": "adult", "sections": [REASON | {"text": "Came in with a sore toe."},
                                                        {"section_id": "sections.3", "heading": heading, "text": text}]},
            "at": {"source_id": "sections.3", "line": 1, "sentence": None if text.startswith("- ") else 1},
            "expected": {"outcome": "retained", "no_drop": True}} | more


def test_check_10_the_controls_are_rendered_and_are_at_their_outcomes(fx, tmp_path, sentinel):
    assert fx.check(tmp_path).problems == [] and sentinel == ["SX1", "SX2", "SX3"]


def test_check_10_the_controls_through_the_evaluators_own_function():
    tables = RP.default_tables()
    assert [GS.synthetic_case(c, tables) for c in CONTROLS] == [[], [], []]


def test_check_10_a_control_that_is_not_labelled_synthetic_is_never_rendered(fx, tmp_path, sentinel):
    for label in ("Invented control", None, "synthetic"):
        sentinel.clear()
        rep = fx.check(tmp_path, lambda cases: case(cases, "SX2").update(label=label))
        assert rep.codes("SX2") == ["synthetic:not_labelled"] and sentinel == ["SX1", "SX3"]


@pytest.mark.parametrize("text", [
    "- CTPA: no PE.",                                           # a whole line of the inputs, fewer than five words
    "- A new line.\n- CXR consolidation, CURB-65 2.",           # the second line is a line of the inputs
    "Found wandering at night (police informed).",              # a sentence unit of the inputs
])
def test_check_10_a_control_holding_a_line_or_unit_of_the_inputs_is_never_rendered(fx, tmp_path, sentinel, text):
    rep = fx.check(tmp_path, lambda cases: cases["synthetic"].append(_control(text)))
    assert rep.codes("SX9") == ["synthetic:measurement_text"] and "SX9" not in sentinel


def test_check_10_a_five_word_run_split_across_two_control_lines_is_never_rendered(fx, tmp_path, sentinel):
    """Codex 12 Nov 21:39 (3): neither line is a line of the inputs, and neither holds five shared words —
    the run "cardiology clinic in 6 weeks" is found only across the line break."""
    text = "- Sent to the cardiology clinic\n- in 6 weeks by taxi."
    hashes = {GS.sha_bytes(ln.encode("utf-8")) for ln in text.split("\n")}
    inputs = CC.Inputs(fx.src, fx.inputs, fx.identity)
    assert not hashes & inputs.line_hashes
    assert all(not CC.runs(CC.words(ln), 5) & inputs.runs for ln in text.split("\n"))
    rep = fx.check(tmp_path, lambda cases: cases["synthetic"].append(_control(text)))
    assert rep.codes("SX9") == ["synthetic:measurement_text"] and "SX9" not in sentinel
    ok = fx.check(tmp_path, lambda cases: cases["synthetic"].append(_control("- Sent to the eye clinic\n- in 6 days.")))
    assert ok.codes("SX9") == [] and sentinel[-1] == "SX9"                 # the same shape, nothing shared


def test_check_10_a_run_shared_with_a_scenario_file_is_never_rendered(fx, tmp_path, sentinel):
    scen = tmp_path / "scenarios"
    scen.mkdir()
    (scen / "invented.md").write_text("# S99\n\nPurple socks worn daily; green\nhat on Sundays only.\n", encoding="utf-8")
    add = lambda t: (lambda cases: cases["synthetic"].append(_control(t)))             # noqa: E731
    rep = fx.check(tmp_path, add("- Has purple socks worn daily, green hat."), scenario_dir=scen)
    assert rep.codes("SX9") == ["synthetic:measurement_text"] and "SX9" not in sentinel
    rep = fx.check(tmp_path, add("- Wears: daily; green. Hat on\n- Sundays."), scenario_dir=scen)      # across lines
    assert rep.codes("SX9") == ["synthetic:measurement_text"] and "SX9" not in sentinel
    rep = fx.check(tmp_path, add("- Purple socks worn most days."), scenario_dir=scen)
    assert rep.codes("SX9") == [] and sentinel[-1] == "SX9"


def test_check_10_without_scenario_files_there_is_no_screen_and_no_verdict(fx, tmp_path, sentinel):
    (tmp_path / "empty").mkdir()
    for d in (tmp_path / "empty", tmp_path / "missing"):
        with pytest.raises(CC.Refused, match="^scenarios:not_readable$"):
            fx.check(tmp_path, scenario_dir=d)
    assert sentinel == []


def test_check_10_the_existing_whole_line_rule_is_kept(fx, tmp_path, sentinel):
    """The rule of test_fixture_lines_are_not_copied_from_the_scenario_files: a whole line of five or more
    space-separated words — here only four of them are words to the run rule."""
    scen = tmp_path / "scenarios"
    scen.mkdir()
    (scen / "invented.md").write_text("Obs: stable — eating & drinking\n", encoding="utf-8")
    line = "- Obs: stable — eating & drinking"
    assert len(line.split()) >= 5 and len(CC.words(line)) < 5
    rep = fx.check(tmp_path, lambda cases: cases["synthetic"].append(_control(line)), scenario_dir=scen)
    assert rep.codes("SX9") == ["synthetic:measurement_text"] and "SX9" not in sentinel


def test_check_10_this_files_invented_text_shares_nothing_with_the_scenario_files():
    scen_runs, scen_whole = CC.scenario_text(CC.SCENARIO_DIR)
    assert len(scen_runs) > 1000
    texts = [s["text"] for c in CONTROLS for s in c["input"]["sections"]] + \
        [t for v in VARIANTS.values() for t in v.values()]
    for t in texts:
        assert not CC.runs(CC.words(t), 5) & scen_runs, t
        for ln in t.split("\n"):
            assert len(ln.split()) < 5 or " ".join(ln.lstrip("- ").split()).lower() not in scen_whole, ln


@pytest.mark.parametrize("edit, code", [
    (lambda s: s.update(at=None), "synthetic:at"),
    (lambda s: s.update(at={"source_id": "sections.5", "line": 1, "sentence": None}), "synthetic:at"),
    (lambda s: s.update(at={"source_id": "sections.7", "line": 2, "sentence": None}), "synthetic:at"),
    (lambda s: s.update(at={"source_id": "sections.7", "line": 1, "sentence": 1}), "synthetic:at"),
    (lambda s: s.update(input={"audience": "adult", "sections": "x"}), "synthetic:input_shape"),
    (lambda s: s["input"]["sections"][1].pop("text"), "synthetic:input_shape"),
])
def test_check_10_a_control_names_a_line_of_its_own_input(fx, tmp_path, sentinel, edit, code):
    rep = fx.check(tmp_path, lambda cases: edit(case(cases, "SX3")))
    assert rep.codes("SX3") == [code] and "SX3" not in sentinel


def test_check_10_a_control_that_is_not_at_its_prewritten_outcome_is_reported(fx, tmp_path):
    def edit(cases):
        case(cases, "SX1")["expected"]["code"] = "unsupported_form"
        case(cases, "SX3")["expected"]["must_keep"] = [r"\bif\b", r"\bzebra\b"]
        cases["synthetic"].append(_control("- Rest for 2 days.", expected={"outcome": "refusal", "code": "bad_input"}))
    rep = fx.check(tmp_path, edit)
    assert rep.codes("SX1") == ["obligation:synthetic_missing", "synthetic:not_at_its_outcome:refused_with_conflict_in_input"]
    assert rep.codes("SX3") == ["synthetic:not_at_its_outcome:must_keep:term_not_in_the_line"]
    assert rep.codes("SX9") == ["synthetic:not_at_its_outcome:rendered_but_a_refusal_was_expected"]


def test_check_10_a_renderer_that_raises_fails_closed(fx, tmp_path, monkeypatch):
    def boom(c, tables):
        raise RuntimeError("no wording here")
    monkeypatch.setattr(GS, "synthetic_case", boom)
    assert fx.check(tmp_path).codes("SX1") == ["synthetic:not_at_its_outcome:raised:RuntimeError"]


@pytest.mark.parametrize("edit, where, codes", [
    (lambda k: k.update(kf_id="KF-13-S2"), "KF-13-S2", ["kf:id_form"]),
    (lambda k: k.update(kf_id="thirteen"), "thirteen", ["kf:id_form"]),
    (lambda k: k.update(runs="ALL"), "KF-13", ["kf:runs_invalid", "obligation:runs_not_all"]),
    (lambda k: k.update(runs=[]), "KF-13", ["kf:runs_invalid", "obligation:runs_not_all"]),
    (lambda k: k.update(runs=[True]), "KF-13", ["kf:runs_invalid", "obligation:runs_not_all"]),
    (lambda k: k.update(checks=[]), "KF-13", ["kf:no_checks"]),
    (lambda k: k["checks"].append({"type": "SEEN"}), "KF-13", ["kf:check_type[1]"]),
    (lambda k: k["checks"].append("DIAG"), "KF-13", ["kf:check_type[1]"]),
    (lambda k: k["checks"].append({"type": "KEEP"}), "KF-13", ["kf:check_fields[1]"]),
    (lambda k: k["checks"].append({"type": "DIAG", "term": "x"}), "KF-13", ["kf:check_fields[1]"]),
    (lambda k: k["checks"].append({"type": "KEEP", "term": "x", "a_headings": ["medications"]}), "KF-13",
     ["kf:check_fields[1]"]),
    (lambda k: k["checks"].append({"type": "KEEP", "term": "x", "a_headings": []}), "KF-13", ["kf:check_fields[1]"]),
    (lambda k: k["checks"].append({"type": "ADD", "token": "x", "copy_only": "yes"}), "KF-13", ["kf:check_fields[1]"]),
    (lambda k: k["checks"].append({"type": "ADD", "token": "x", "copy_only": True, "not_from": "y"}), "KF-13",
     ["kf:check_fields[1]"]),
    (lambda k: k["checks"].append({"type": "RULED", "kinds": ["tone"], "c_headings": ["what_we_did"]}), "KF-13",
     ["kf:check_fields[1]"]),
    (lambda k: k["checks"].append({"type": "RULED", "kinds": ["cause"], "c_headings": ["medicines"]}), "KF-13",
     ["kf:check_fields[1]"]),
    (lambda k: k["checks"].append({"type": "BLIND", "item": "S1", "dimension": "tone"}), "KF-13",
     ["kf:blind_dimension[1]"]),
    (lambda k: k.pop("runs"), "KF-13", ["schema:key_missing:runs"]),
])
def test_check_11_a_known_failure_entry_is_well_formed(fx, tmp_path, edit, where, codes):
    rep = fx.check(tmp_path, lambda cases: edit(kf(cases, "KF-13")))
    assert sorted(c for c in rep.codes(where) if not c.startswith("obligation:kf_")) == codes


def test_check_11_a_known_failure_names_a_scenario_of_the_batch(fx, tmp_path):
    rep = fx.check(tmp_path, lambda cases: kf(cases, "KF-13").update(scenario_id="S77"))
    assert "kf:scenario_not_in_the_batch" in rep.codes("KF-13")


@pytest.mark.parametrize("edit, code", [
    (lambda g: g.update(record="r4/B66.json"), "generation:record_not_in_the_inputs"),
    (lambda g: g.update(expected="refused"), "generation:expected"),
    (lambda g: g.update(c_must_not_match=["("]), "c_must_not_match[0]:pattern:does_not_compile"),
    (lambda g: g.update(c_must_not_match=r"notes\s+conflict"), "schema:type:c_must_not_match"),
])
def test_check_12_per_generation_cases(fx, tmp_path, edit, code):
    assert code in fx.check(tmp_path, lambda cases: edit(case(cases, "U2-B6-r4"))).codes("U2-B6-r4")


@pytest.mark.parametrize("edit, where", [
    (lambda c: case(c, FIRST)["expected"].update(rule="kept because it says Found wandering at night"), FIRST),
    (lambda c: kf(c, "KF-13").update(source="seen in: the patient was admitted with"), "KF-13"),
    (lambda c: case(c, "U2-B6-r4").update(rule="OGD planned as an outpatient"), "U2-B6-r4"),
])
def test_check_13_no_string_shares_four_words_with_the_reading_sheet(fx, tmp_path, edit, where):
    assert fx.check(tmp_path, edit).codes(where) == ["wording:shared_with_the_reading_sheet"]


@pytest.mark.parametrize("edit, where", [
    (lambda c: case(c, FIRST)["expected"].update(rule={"why": {"found wandering at night": 1}}), FIRST),
    (lambda c: case(c, FIRST)["expected"].update(rule=[{"Found wandering at night": []}]), FIRST),
    (lambda c: kf(c, "KF-13").update({"OGD planned as an outpatient": True}), "KF-13"),
    (lambda c: case(c, "U2-B6-r4").update(rule={"the patient was admitted with": "x"}), "U2-B6-r4"),
])
def test_check_13_reads_dictionary_keys_as_well_as_values(fx, tmp_path, capsys, edit, where):
    """Codex 13 Nov 07:53 (2): four words of the sheet hidden in a KEY are wording all the same — and the
    report names the entry and the code without printing the phrase."""
    cases = copy.deepcopy(fx.cases)
    edit(cases)
    assert "wording:shared_with_the_reading_sheet" in fx.check(tmp_path, edit).codes(where)
    assert CC.main(fx.argv(fx.write(tmp_path / "m", cases)), _identity=fx.identity) == 1
    out = capsys.readouterr().out.lower()
    assert f"  {where.lower()}: wording:shared_with_the_reading_sheet" in out
    for phrase in ("wandering", "outpatient", "admitted with"):
        assert phrase not in out


def test_check_13_reads_words_through_punctuation_and_leaves_the_controls_alone(fx, tmp_path):
    def edit(cases):
        case(cases, FIRST)["expected"]["rule"] = "FOUND: wandering, at\nnight"          # the same four words
        case(cases, S18_R3)["expected"]["rule"] = "found wandering at dusk"            # three shared, not four
    rep = fx.check(tmp_path, edit)
    assert rep.codes(FIRST) == ["wording:shared_with_the_reading_sheet"] and rep.codes(S18_R3) == []
    # a control's invented input is not one of the three parts check 13 covers
    sheet_runs = CC.runs(CC.words(fx.sheet.read_text(encoding="utf-8")), 4)
    assert sheet_runs and not any(CC.runs(CC.words(s), 4) & sheet_runs for c in CONTROLS for s in CC._strings(c))


@pytest.mark.parametrize("edit, where, code", [
    (lambda c: drop(c, "located", "case_id", FIRST), FIRST, "located:lost_since_the_pass"),
    (lambda c: c["located"].append(case(c, FIRST) | {"case_id": "L2-S2-r1-sections.2-1-9"}),
     "L2-S2-r1-sections.2-1-9", "located:added_since_the_pass"),
    (lambda c: case(c, FIRST).update(category="L3"), FIRST, "located:changed_since_the_pass"),
    (lambda c: case(c, FIRST).update(sentence=1), FIRST, "located:changed_since_the_pass"),
    (lambda c: case(c, FIRST).update(line=True), FIRST, "located:changed_since_the_pass"),       # true is not 1
    (lambda c: case(c, FIRST).update(heading="treatment"), FIRST, "located:changed_since_the_pass"),
])
def test_check_14_nothing_added_lost_or_changed_since_the_pass(fx, tmp_path, edit, where, code):
    assert (where, code) in fx.check(tmp_path, edit).problems


# =================================================================================================
# the obligations table — one named test per fixed code (PART3-R2-fixes.md, Fix 1)
# =================================================================================================

def test_obligation_kf_entry_missing(fx, tmp_path):
    """KF-04 is still "present" through S10 and S14 — its S1 entry is the one that is gone."""
    rep = fx.check(tmp_path, lambda cases: drop(cases, "known_failures", "kf_id", "KF-04-S1"))
    assert rep.problems == [("KF-04/S1", "obligation:kf_entry_missing")]


def test_obligation_kf_entry_duplicated(fx, tmp_path):
    def edit(cases):
        cases["known_failures"].append(copy.deepcopy(kf(cases, "KF-13")) | {"kf_id": "KF-13-S1"})
    assert fx.check(tmp_path, edit).problems == [("KF-13/S1", "obligation:kf_entry_duplicated")]


@pytest.mark.parametrize("kf_id, edit, index", [
    ("KF-09", lambda cs: cs.pop(0), 0),                                              # a required KEEP deleted
    ("KF-01", lambda cs: cs[0].update(not_from="angio"), 0),                         # ADD's not_from changed
    ("KF-01", lambda cs: cs[0].pop("not_from"), 0),
    ("KF-06", lambda cs: cs[0].pop("copy_only"), 0),                                 # ADD's copy_only dropped
    ("KF-02", lambda cs: cs[0].update(a_headings=["treatment"]), 0),                 # all headings -> one
    ("KF-09", lambda cs: cs[0].update(a_headings=["treatment"]), 0),                 # two headings -> one
    ("KF-09", lambda cs: cs[0].update(term=r"\bimproving\b"), 0),                    # the pattern changed
    ("KF-10", lambda cs: cs[1].update(kinds=["cause"]), 1),
    ("KF-10", lambda cs: cs[1].update(c_headings=["what_we_did"]), 1),
    ("KF-10", lambda cs: cs[0].update(type="WITHHELD"), 0),
    ("KF-19-S3", lambda cs: cs[0].update(dimension="added"), 0),
])
def test_obligation_kf_check_missing(fx, tmp_path, kf_id, edit, index):
    rep = fx.check(tmp_path, lambda cases: edit(kf(cases, kf_id)["checks"]))
    assert f"obligation:kf_check_missing[{index}]" in rep.codes(kf_id) and rep.exit_code == 1


def test_obligation_runs_not_all(fx, tmp_path):
    rep = fx.check(tmp_path, lambda cases: kf(cases, "KF-17").update(runs=[1]))       # a valid value, narrowed
    assert rep.problems == [("KF-17", "obligation:runs_not_all")]
    rep = fx.check(tmp_path, lambda cases: kf(cases, "KF-17").update(runs=[1, 2, 3, 4, 5]))
    assert rep.problems == [("KF-17", "obligation:runs_not_all")]                     # exactly "all", nothing else


def test_obligation_blind_item_mismatch(fx, tmp_path):
    rep = fx.check(tmp_path, lambda cases: kf(cases, "KF-19-S3")["checks"][0].update(item="S10"))
    assert sorted(rep.codes("KF-19-S3")) == ["obligation:blind_item_mismatch", "obligation:kf_check_missing[0]"]


def test_obligation_blind_item_not_an_item(fx, tmp_path):
    rep = fx.check(tmp_path, lambda cases: kf(cases, "KF-19-S3")["checks"][0].update(item="S2"))    # not one of the eight
    assert sorted(rep.codes("KF-19-S3")) == ["obligation:blind_item_not_an_item", "obligation:kf_check_missing[0]"]


@pytest.mark.parametrize("edit", [
    lambda e: e.update(outcome="approved_omission", drop={"rule": "withheld", "entry": "w_grace"}),
    lambda e: e.update(must_keep=[r"\b2\b"]),
    lambda e: e.update(c_must_match=[r"\b2\s+weeks\b"]),                              # not the adopted text
    lambda e: e.pop("c_must_match"),
])
def test_obligation_u1_missing(fx, tmp_path, edit):
    rep = fx.check(tmp_path, lambda cases: edit(case(cases, "U1-S1-r4-sections.0-1-1")["expected"]))
    assert "obligation:u1_missing" in rep.codes("U1-r4") and not rep.codes("U1-r3")


def test_obligation_u1_missing_when_one_case_is_deleted(fx, tmp_path):
    rep = fx.check(tmp_path, lambda cases: drop(cases, "located", "case_id", "U1-S1-r2-sections.0-1-1"))
    assert ("U1-r2", "obligation:u1_missing") in rep.problems
    assert fx.check(tmp_path, lambda cases: case(cases, "U1-S1-r2-sections.0-1-1")["expected"].pop("no_drop")).problems == []


@pytest.mark.parametrize("edit", [
    lambda c: drop(c, "generations", "case_id", "U2-B6-r4"),
    lambda c: case(c, "U2-B6-r4").update(c_must_not_match=[]),
    lambda c: case(c, "U2-B6-r4").update(record="r3/B6.json"),
    lambda c: case(c, "U2-B6-r4").update(expected="refused"),
])
def test_obligation_u2_missing(fx, tmp_path, edit):
    assert ("U2", "obligation:u2_missing") in fx.check(tmp_path, edit).problems


@pytest.mark.parametrize("edit", [
    lambda e: e.update(no_drop=True),                                # a correct drop of the level would then fail
    lambda e: e.update(must_keep=[r"\bstitches\b"]),
    lambda e: e.update(outcome="refusal"),
])
def test_obligation_kf18_case_missing(fx, tmp_path, edit):
    rep = fx.check(tmp_path, lambda cases: edit(case(cases, S18_R3)["expected"]))
    assert rep.codes("KF-18-r3") == ["obligation:kf18_case_missing"] and not rep.codes("KF-18-r2")


def test_obligation_kf18_pattern_missing(fx, tmp_path):
    rep = fx.check(tmp_path, lambda cases: case(cases, S18_R3)["expected"].pop("c_must_not_match"))
    assert rep.problems == [("KF-18-r3", "obligation:kf18_pattern_missing")]
    rep = fx.check(tmp_path, lambda cases: case(cases, S18_R3)["expected"].update(c_must_not_match=[r"\brisk\b"]))
    assert rep.problems == [("KF-18-r3", "obligation:kf18_pattern_missing")]           # the adopted text, exactly


def test_obligation_boundary_case_missing(fx, tmp_path):
    def edit(cases):
        case(cases, "L1-A5-r5-sections.1-1-0")["expected"] = {"outcome": "retained", "no_drop": True}
        case(cases, "L6i-S14-r4-sections.3-5-0")["expected"] = {"outcome": "retained", "no_drop": True}
    rep = fx.check(tmp_path, edit)
    assert rep.problems == [("A5-r5", "obligation:boundary_case_missing"),
                            ("r4/S14.json", "obligation:boundary_case_missing")]


@pytest.mark.parametrize("edit, where", [
    (lambda c: drop(c, "synthetic", "case_id", "SX1"), "SX1"),
    (lambda c: case(c, "SX2")["expected"].update(code="conflict_in_input"), "SX2"),
    (lambda c: case(c, "SX3").update(expected={"outcome": "refusal", "code": "unsupported_form"}), "SX3"),
])
def test_obligation_synthetic_missing(fx, tmp_path, edit, where):
    assert "obligation:synthetic_missing" in fx.check(tmp_path, edit).codes(where)


def test_an_extra_check_or_entry_is_a_note_never_a_failure(fx, tmp_path):
    def edit(cases):
        kf(cases, "KF-13")["checks"].append({"type": "KEEP", "term": r"\basthma\b", "a_headings": ["diagnosis"]})
        cases["known_failures"].append({"kf_id": "KF-20", "scenario_id": "S2", "runs": "all", "source": "fixture",
                                        "checks": [{"type": "DIAG"}]})
    rep = fx.check(tmp_path, edit)
    assert rep.problems == [] and rep.exit_code == 0
    assert rep.notes == [("KF-13", "note:kf_check_extra"), ("KF-20", "note:kf_entry_extra")]
    narrowed = fx.check(tmp_path, lambda cases: (edit(cases), kf(cases, "KF-20").update(runs=[1])))
    assert narrowed.problems == [("KF-20", "obligation:runs_not_all")]                 # an extra entry too


def test_a_replacement_table_changes_the_obligations_only_from_python(fx, tmp_path):
    small = CC.Obligations(known_failures=CC.KNOWN_FAILURES[:1], u1=None, u2=None, kf18=None, omissions=(),
                           inferred=(), synthetic=())

    def edit(cases):
        cases["known_failures"] = cases["known_failures"][:1]
        cases["synthetic"], cases["generations"] = [], []
    assert fx.check(tmp_path, edit, obligations=small).problems == []
    assert len(fx.check(tmp_path, edit).problems) > 30                                  # the production table


# =================================================================================================
# malformed types (Codex 13 Nov 07:53, 1): a named problem and exit 1 — never a pass, never a traceback
# =================================================================================================

@pytest.fixture(scope="module")
def loaded(fx):
    """The fixture's inputs, pass files, tables and scenario text read once, for the many-lists tests."""
    located, sheet = CC.read_pass_files(fx.located, fx.sheet, fx.identity)
    inputs, scenario = CC.Inputs(fx.src, fx.inputs, fx.identity), CC.scenario_text(CC.SCENARIO_DIR)

    def run(cases: dict, only=None) -> CC.Report:
        """`only`: the ids of a short list — the pass is then cut to the same cases, so nothing is "lost"."""
        doc = located if only is None else located | {"cases": [c for c in located["cases"] if c["case_id"] in only]}
        rep = CC.Report("fixture")
        CC.run_checks(rep, cases, doc, sheet, inputs, RP.default_tables(), fx.identity, CC.OBLIGATIONS, scenario)
        return rep
    return run


def test_the_many_lists_helper_agrees_with_check(fx, tmp_path, loaded):
    assert loaded(copy.deepcopy(fx.cases)).problems == []

    def edit(cases):
        case(cases, FIRST)["expected"]["must_keep"] = [r"\bzebra\b"]
        kf(cases, "KF-13")["runs"] = [1]
    broken = copy.deepcopy(fx.cases)
    edit(broken)
    assert sorted(loaded(broken).problems) == sorted(fx.check(tmp_path, edit).problems) != []


@pytest.mark.parametrize("extra, codes", [
    ({"type": "KEEP", "term": ["zebra"]}, ["kf:check_fields[1]", "term[0]:pattern:empty"]),      # Codex's example
    ({"type": "KEEP", "term": ["zebra"], "a_headings": ["diagnosis"]}, ["kf:check_fields[1]", "term[0]:pattern:empty"]),
    ({"type": "ADD", "token": ["zebra"]}, ["kf:check_fields[1]", "token[0]:pattern:empty"]),
    ({"type": "ADD", "token": "zebra", "not_from": ["stripes"]}, ["kf:check_fields[1]", "not_from[0]:pattern:empty"]),
    ({"type": "ADD", "token": {"a": "zebra"}}, ["kf:check_fields[1]", "token[0]:pattern:empty"]),
    ({"type": "KEEP", "term": 7}, ["kf:check_fields[1]", "term[0]:pattern:empty"]),
    ({"type": ["KEEP"], "term": "zebra"}, ["kf:check_type[1]"]),
    ({"type": {"KEEP": 1}}, ["kf:check_type[1]"]),
    ({"type": None}, ["kf:check_type[1]"]),
    ({"type": "BLIND", "item": ["S1"], "dimension": "audience"}, ["kf:check_fields[1]"]),
    ({"type": "BLIND", "item": "S1", "dimension": {"a": 1}}, ["kf:check_fields[1]"]),
    ({"type": "RULED", "kinds": "cause", "c_headings": ["what_we_did"]}, ["kf:check_fields[1]"]),
    ({"type": "RULED", "kinds": [["cause"]], "c_headings": ["what_we_did"]}, ["kf:check_fields[1]"]),
])
def test_a_malformed_extra_check_is_a_named_problem_not_a_note(fx, tmp_path, extra, codes):
    """An EXTRA check is allowed only if it is well-formed: the evaluator would hand a list-valued pattern
    to `re` and crash on the one run."""
    rep = fx.check(tmp_path, lambda cases: kf(cases, "KF-13")["checks"].append(extra))
    assert sorted(rep.codes("KF-13")) == codes and rep.exit_code == 1


@pytest.mark.parametrize("edit, where, code", [
    (lambda c: case(c, FIRST).update(record=["r1/S2.json"]), FIRST, "located:record_not_in_the_inputs"),
    (lambda c: case(c, FIRST).update(record={"r1/S2.json": 1}), FIRST, "schema:type"),
    (lambda c: case(c, FIRST).update(source_id=["sections.2"]), FIRST, "located:no_such_section"),
    (lambda c: case(c, FIRST).update(line=1.0), FIRST, "schema:type"),
    (lambda c: case(c, FIRST).update(line_sha256=[]), FIRST, "located:line_sha256_mismatch"),
    (lambda c: c["located"][0].update(case_id=["L2"]), "located[0]", "ids:missing"),
    (lambda c: c["located"][0].update(case_id={"a": 1}), "located[0]", "located:added_since_the_pass"),
    (lambda c: case(c, FIRST)["expected"].update(outcome=["retained"]), FIRST, "outcome:not_recognised"),
    (lambda c: case(c, FIRST)["expected"].update(rule={"a": 1}), FIRST, "schema:type:rule"),
    (lambda c: case(c, S18_R3)["expected"].update(predicted=["NOT MET"]), S18_R3, "schema:type:predicted"),
    (lambda c: case(c, A5_R1)["expected"].update(drop={"rule": ["withheld"], "entry": "w_curb65"}), A5_R1, "omission:rule"),
    (lambda c: case(c, A5_R1)["expected"].update(drop={"rule": "withheld", "entry": ["w_curb65"]}), A5_R1, "omission:entry"),
    (lambda c: case(c, A5_R1)["expected"].update(drop={"rule": "withheld", "entry": {"a": 1}}), "A5-r1",
     "obligation:boundary_case_missing"),
    (lambda c: case(c, "SX1")["expected"].update(code=["conflict_in_input"]), "SX1", "outcome:refusal_code"),
    (lambda c: case(c, "SX1").update(case_id=["SX1"]), "SX1", "obligation:synthetic_missing"),
    (lambda c: case(c, "SX3").update(shape=["a note"]), "SX3", "schema:type:shape"),
    (lambda c: kf(c, "KF-13").update(scenario_id=["S1"]), "KF-13", "kf:scenario_not_in_the_batch"),
    (lambda c: kf(c, "KF-13").update(scenario_id={"S1": 1}), "KF-13", "kf:id_form"),
    (lambda c: kf(c, "KF-13").update(source={"a": 1}), "KF-13", "schema:type:source"),
    (lambda c: kf(c, "KF-13").update(kf_id=["KF-13"]), "known_failures[18]", "ids:missing"),
    (lambda c: case(c, "U2-B6-r4").update(record=["r4/B6.json"]), "U2-B6-r4", "schema:type:record"),
    (lambda c: case(c, "U2-B6-r4").update(record={"a": 1}), "U2-B6-r4", "generation:record_not_in_the_inputs"),
    (lambda c: case(c, "U2-B6-r4").update(rule=["x"]), "U2-B6-r4", "schema:type:rule"),
    (lambda c: c.update(provenance=["x"]), "list", "provenance:pass_identity_mismatch"),
])
def test_a_field_of_the_wrong_type_is_a_named_problem(fx, tmp_path, edit, where, code):
    rep = fx.check(tmp_path, edit)
    assert (where, code) in rep.problems and not [c for c in rep.codes() if c.startswith("internal_error")]


def _wrong(value) -> list:
    if isinstance(value, bool):
        return [None, "yes", 1, [True], {"x": True}]
    if isinstance(value, str):
        return [None, 7, True, ["x"], {"x": "y"}]
    if isinstance(value, int):
        return [None, "1", 1.5, [1], {"x": 1}]
    if value is None:
        return ["1", 1.5, True, [1], {"x": 1}]
    if isinstance(value, list):
        return [None, "x", 7, {"x": "y"}, [7], [["x"]], [{"x": "y"}], [None]]
    return [None, "x", 7, [], ["x"]]                                  # an object


def _sites(cases: dict) -> list:
    """(a name, the holder, the key) for every field this file's list has — two levels below each entry."""
    entries = [("list", cases)]
    entries += [(n, case(cases, n)) for n in (FIRST, S18_R3, A5_R1, "L6i-S14-r2-sections.3-5-0", "SX1", "SX3", "U2-B6-r4")]
    entries += [(n, kf(cases, n)) for n in ("KF-01", "KF-02", "KF-06", "KF-10", "KF-18", "KF-19-S3")]
    out = []
    for name, obj in entries:
        todo = [(name, obj)]
        while todo:
            label, holder = todo.pop()
            for k, v in (holder.items() if isinstance(holder, dict) else enumerate(holder)):
                if label == "list" and k in CC.PARTS and len(out) > 0 and isinstance(holder, dict) and holder is cases:
                    out.append((f"{label}.{k}", holder, k))
                    continue                                         # the parts' entries are named above
                out.append((f"{label}.{k}", holder, k))
                if isinstance(v, dict) or (isinstance(v, list) and v and isinstance(v[0], dict)):
                    todo.append((f"{label}.{k}", v))
    return out


def test_no_wrong_type_anywhere_passes_or_raises(fx, loaded, monkeypatch):
    """Every field of a short version of the fixture list, replaced in turn by values of the wrong type: the
    validator always answers, always with at least one NEW named problem, and never through its
    fail-closed net. (Short = five located cases and a pass cut to the same five, so that hundreds of lists
    stay quick; what the short list lacks for the obligations is the same before and after, and is set aside.
    The three controls are really rendered only when the changed field is a control's.)"""
    real = GS.synthetic_case
    keep = {FIRST, S18_R3, A5_R1, "L6i-S14-r2-sections.3-5-0", "U1-S1-r1-sections.0-1-1"}
    base = copy.deepcopy(fx.cases)
    base["located"] = [c for c in base["located"] if c["case_id"] in keep]
    before = set(loaded(copy.deepcopy(base), keep).problems)
    assert not [c for _w, c in before if not c.startswith("obligation:")]
    tried = 0
    for i, (name, _holder, key) in enumerate(_sites(base)):
        if name == "list.provenance.status":
            continue                                                 # the provenance's own note: not a checked field
        original = _sites(base)[i][1][key]
        for value in _wrong(original):
            cases = copy.deepcopy(base)
            site = _sites(cases)[i]
            assert site[0] == name
            site[1][key] = value
            monkeypatch.setattr(GS, "synthetic_case", real if name.startswith("SX") else lambda c, tables: [])
            rep = loaded(cases, keep)                                # must not raise
            assert set(rep.problems) - before, (name, value)
            assert not [c for c in rep.codes() if c.startswith("internal_error")], (name, value, rep.problems[:3])
            assert all(isinstance(w, str) and isinstance(c, str) for w, c in rep.problems)
            tried += 1
    assert tried > 500


def test_an_error_the_validator_did_not_foresee_fails_closed(fx, tmp_path, monkeypatch, capsys):
    """The net under every group of checks: a named problem and exit 1 — not exit 0, not a traceback."""
    def boom(*a, **kw):
        raise KeyError("nothing printed")
    good = fx.write(tmp_path, fx.cases)
    for name, group in (("list", "list_checks"), ("pass", "pass_checks"), ("inputs", "input_checks"),
                        ("controls", "control_checks")):
        with monkeypatch.context() as m:
            m.setattr(CC, group, boom)
            rep = fx.check(tmp_path)
            assert rep.codes("validator") == [f"internal_error:{name}:KeyError"] and rep.exit_code == 1
            assert CC.main(fx.argv(good), _identity=fx.identity) == 1
            io = capsys.readouterr()
            assert f"  validator: internal_error:{name}:KeyError" in io.out and "PROBLEMS (exit 1)" in io.out
            assert "Traceback" not in io.out + io.err and "nothing printed" not in io.out + io.err


def test_a_refusal_inside_the_checks_is_still_a_refusal(fx, tmp_path, monkeypatch):
    def refuse(*a, **kw):
        raise CC.Refused("tables:changed")
    monkeypatch.setattr(CC, "input_checks", refuse)
    with pytest.raises(CC.Refused, match="^tables:changed$"):
        fx.check(tmp_path)


def test_malformed_lists_through_the_command_exit_1_without_a_traceback(fx, tmp_path, capsys):
    cases = copy.deepcopy(fx.cases)
    kf(cases, "KF-13")["checks"] += [{"type": "KEEP", "term": ["zebra"]}, {"type": ["KEEP"]}]
    case(cases, FIRST)["record"] = ["r1/S2.json"]
    case(cases, "U2-B6-r4")["record"] = {"r4": "B6"}
    cases["synthetic"].append(7)
    assert CC.main(fx.argv(fx.write(tmp_path, cases)), _identity=fx.identity) == 1
    io = capsys.readouterr()
    for line in ("  KF-13: kf:check_fields[1]", "  KF-13: term[0]:pattern:empty", "  KF-13: kf:check_type[2]",
                 f"  {FIRST}: located:record_not_in_the_inputs", "  U2-B6-r4: schema:type:record",
                 "  synthetic[3]: schema:not_an_object"):
        assert line in io.out, line
    assert "Traceback" not in io.out + io.err and "zebra" not in io.out and "internal_error" not in io.out


# =================================================================================================
# A2 — the validator and the evaluator: what agrees, what is the validator's own, and what is neither
# =================================================================================================

def _refuses(c: dict, rendered) -> bool:
    try:
        GS.located_case(c, rendered, RP.default_tables())
    except GS.Refused:
        return True
    return False


def test_a2_1_the_location_predicate_agrees_with_the_evaluator_both_ways(fx, rendered):
    inputs = CC.Inputs(fx.src, fx.inputs, fx.identity)
    for c in fx.cases["located"]:                                    # every valid case: neither objects
        assert CC.location_problem(c, inputs) is None and not _refuses(c, rendered)
    for edit, code in LOCATION_BREAKS + [(lambda c: c.update(line="1"), "located:line_out_of_range"),
                                         (lambda c: c.pop("source_id"), "located:no_such_section")]:
        broken = copy.deepcopy(case(fx.cases, FIRST))
        edit(broken)
        assert CC.location_problem(broken, inputs) == code and _refuses(broken, rendered)


def test_a2_2_the_heading_is_the_validators_own_check(fx, tmp_path, rendered):
    def edit(cases):
        case(cases, FIRST)["heading"] = "treatment"
    rep = fx.check(tmp_path, edit)
    assert "located:heading_mismatch" in rep.codes(FIRST)
    wrong = copy.deepcopy(case(fx.cases, FIRST)) | {"heading": "treatment"}
    assert GS.located_case(wrong, rendered, RP.default_tables()) == []      # the evaluator never reads it


def test_a2_3_the_sentence_is_the_validators_own_check(fx, tmp_path, rendered):
    def edit(cases):
        case(cases, FIRST)["sentence"] = 1                           # an item has no sentence number
        case(cases, S18_R3)["sentence"] = 7                          # a prose line with two sentences
    rep = fx.check(tmp_path, edit)
    assert "located:no_such_sentence" in rep.codes(FIRST) and "located:no_such_sentence" in rep.codes(S18_R3)
    for cid, n in ((FIRST, 1), (S18_R3, 7)):
        wrong = copy.deepcopy(case(fx.cases, cid)) | {"sentence": n}
        # not a refusal there: a NOT MET, so a STOP that is not the renderer's
        assert GS.located_case(wrong, rendered, RP.default_tables()) == ["no_ledger_line"]


def test_a2_4_the_must_keep_verdict_agrees_with_the_evaluator(fx, rendered):
    inputs, tables = CC.Inputs(fx.src, fx.inputs, fx.identity), RP.default_tables()
    marker = "L2-S2-r1-sections.2-1-0"                               # "- CTPA: no PE." — an item
    for cid, term in ((marker, r"\bno\b"), (marker, r"\bzebra\b"), (marker, "CTPA"), (marker, r"\?"),
                      (S18_R3, r"\bno\b"), (S18_R3, r"\bdressing\b"), ("U1-S1-r1-sections.0-1-1", r"\bweeks\b"),
                      ("L1-A5-r1-sections.1-1-0", r"\binfection\b"), ("L1-A5-r1-sections.1-1-0", r"\basthma\b")):
        c = copy.deepcopy(case(fx.cases, cid))
        c["expected"] = {"outcome": "retained", "must_keep": [term]}
        sec = inputs.section(c["record"], c["source_id"])
        mine = re.search(term, CC.unit_text(sec, c["line"], c["sentence"]), re.IGNORECASE) is not None
        theirs = "must_keep:term_not_in_the_line" not in GS.located_case(c, rendered, tables)
        assert mine == theirs, (cid, term)


def test_a2_5_a_well_formed_case_can_be_not_met_on_the_run(fx, tmp_path, rendered):
    """Validity is not the measured outcome: the validator passes this list; the evaluator, on the rendered
    fixture, reports the case NOT MET (the fixture renderer copies the word the case says must not reach C)."""
    def edit(cases):
        case(cases, S18_R3)["expected"]["c_must_not_match"] = [r"(?i)\brisk\b", r"(?i)\bstitches\b"]
    assert fx.check(tmp_path, edit).problems == []
    c = copy.deepcopy(case(fx.cases, S18_R3))
    c["expected"]["c_must_not_match"].append(r"(?i)\bstitches\b")
    assert GS.located_case(c, rendered, RP.default_tables()) == ["c_must_not_match"]
    assert GS.located_case(case(fx.cases, S18_R3), rendered, RP.default_tables()) == []


# =================================================================================================
# what the validator never does
# =================================================================================================

def test_it_never_calls_the_renderers_line_splitter_or_renders_on_the_inputs(fx, tmp_path, monkeypatch):
    """With the controls taken out, nothing of the renderer may run at all: units come from the locator."""
    def never(*a, **kw):
        raise AssertionError("the renderer was called")
    for name in ("split_lines", "render_patient", "render_line", "audit"):
        monkeypatch.setattr(RP, name, never)
    no_controls = replace(CC.OBLIGATIONS, synthetic=())
    rep = fx.check(tmp_path, lambda cases: cases.update(synthetic=[]), obligations=no_controls)
    assert rep.problems == []
    with_controls = fx.check(tmp_path)                               # the controls do reach the renderer
    assert with_controls.codes("SX1") == ["synthetic:not_at_its_outcome:raised:AssertionError"]


def test_it_reads_only_step_7s_input_from_a_record(fx, monkeypatch):
    seen = []
    real = GS.step7_input
    monkeypatch.setattr(GS, "step7_input", lambda rec: seen.append(rec["generation_id"]) or real(rec))
    inputs = CC.Inputs(fx.src, fx.inputs, fx.identity)
    assert len(seen) == len(set(seen)) == 90 and inputs.records == 90
    assert all(set(v) == {"audience", "sections"} for v in inputs.by_rel.values())      # step 7's A-only input
    assert all(set(s) == {"section_id", "heading", "text"} for v in inputs.by_rel.values() for s in v["sections"])


def test_words_and_runs():
    assert CC.words("- No PE;\nCURB-65: 2 (x2/7).") == ["no", "pe", "curb-65", "2", "x2/7"]
    assert CC.runs(["a", "b", "c"], 2) == {("a", "b"), ("b", "c")} and CC.runs(["a"], 2) == set()
