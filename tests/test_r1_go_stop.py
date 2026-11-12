"""Item 3 R1 step 4 — the go / stop evaluator (evals/r1_go_stop.py), on an invented fixture world
(tests/r1_fixture.py — never the 90 saved p0.4 records).

The GO control shows the evaluator can pass; every other test is the GO control plus one change. The
six STOP fixtures (D3's three omission fixtures and step 2's three) are tampered in BOTH run folders
before the seal and the review are made, so only the intended condition fails; each asserts the
diagnostic that names it. Where a test breaks a check on purpose (monkeypatch), it shows what the
fixture's STOP depends on: the targeted diagnostic disappears while an independent guard still stops
it, and with the evaluator's audit condition ignored the fixture would pass (Codex 16:22 P2)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import r1_fixture as FX  # noqa: E402
import r1_go_stop as GS  # noqa: E402
import score_batch as SB  # noqa: E402
import score_patient as SP  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402

TAMPERED = "r1/S11.json"            # an adult scenario that is not one of the eight blind-read items


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    return FX.make_source(tmp_path_factory.mktemp("r1-src"))


@pytest.fixture(scope="module")
def go_world(source, tmp_path_factory):
    return FX.build_world(tmp_path_factory.mktemp("r1-go") / "w", *source)


@pytest.fixture
def world(go_world, tmp_path):
    return FX.copy_world(go_world, tmp_path / "w")


@pytest.fixture(scope="module")
def unsealed_world(source, tmp_path_factory):
    return FX.build_world(tmp_path_factory.mktemp("r1-unsealed") / "w", *source, until="sheets")


@pytest.fixture
def unsealed(unsealed_world, tmp_path):
    """Rendered, the store built, sheets built and scored — not yet sealed."""
    return FX.copy_world(unsealed_world, tmp_path / "w")


def statuses(rep):
    return {k.split(" ", 1)[0]: v["status"] for k, v in rep["conditions"].items()}


def why(rep, g):
    return next(v["why"] for k, v in rep["conditions"].items() if k.startswith(g + " "))


def edit_json(path: Path, fn):
    d = json.loads(path.read_text(encoding="utf-8"))
    fn(d)
    path.write_text(GS.dump(d), encoding="utf-8")


def bar_line(batch: Path, store: Path, n: str) -> str:
    scored = SB.score_batch(batch, adjudications_dir=store)
    return next(v for line, _r, v in SP.bar(scored["patient"]["summary"]) if line.startswith(n + " "))


# --- the positive control ------------------------------------------------------------------------------

def test_go_control_every_condition_met(go_world):
    rep = go_world.evaluate()
    assert rep["verdict"] == GS.GO
    assert statuses(rep) == {f"G{i}": GS.MET for i in range(1, 9)}
    assert rep["generations"] == 18 and len(rep["per_generation"]) == 18
    assert "NOT the measurement batch" in rep["source"]


def test_line_12_is_never_claimed_though_the_scorer_prints_met(go_world):
    assert bar_line(go_world.batch, go_world.store, "12") == "MET"        # no step-7 call: nothing is ever late
    rep = go_world.evaluate()
    assert rep["line_12"].startswith("12 latency: NOT CLAIMED")
    assert not any(k.startswith("12") for k in rep["conditions"]["G2 code-decided bar lines"]["lines"])


def test_code_lines_agree_with_the_scorers_bar_on_the_code_only_lines(go_world):
    scored = SB.score_batch(go_world.batch, adjudications_dir=go_world.store)
    bar = {line.split(" ", 1)[0]: v for line, _r, v in SP.bar(scored["patient"]["summary"])}
    mine = {k.split(" ", 1)[0]: v for k, v in GS.code_lines(scored)["lines"].items() if "(code)" not in k}
    assert set(mine) == {"1", "2", "3", "7", "8", "13"} and all(bar[k] == v for k, v in mine.items())


def test_score_batch_and_the_sheet_builder_ran_unchanged_on_the_renderer_batch(go_world):
    assert (go_world.batch / "scores" / "review_patient.md").is_file()
    assert (go_world.batch / "scores" / "PATIENT_VERSION.md").is_file()
    assert sorted(p.stem for p in (go_world.sheets / "sheets").glob("*.md")) == sorted(GS.MS.ITEMS)
    inv = json.loads((go_world.batch / "scores" / GS.INVENTORY_FILE).read_text())
    assert inv and not any("sentence" in item for items in inv.values() for item in items.values())


def test_the_command_line_writes_the_report_and_prints_no_sentence(world, capsys):
    argv = ["evaluate", "--batch", str(world.batch), "--run2", str(world.run2), "--store", str(world.store),
            "--sheets", str(world.sheets), "--freeze", str(world.freeze), "--inputs", str(world.inputs)]
    assert GS.main(argv) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("GO (on a fixture — NOT the measurement)")
    rep = json.loads((world.batch / GS.OUT_DIR / "GO-STOP.json").read_text())
    text = out + json.dumps(rep) + (world.batch / GS.OUT_DIR / "GO-STOP.md").read_text()
    c_line = json.loads((world.batch / "r1" / "S1.json").read_text())["steps"]["compose_patient"]["sections"][0]["text"]
    assert c_line.split("\n")[0] in (world.batch / GS.LEDGER_DIR / "LEDGER-REVIEW.md").read_text() + c_line  # control
    assert "chest pain" not in text and "Found wandering" not in text and "heart attack" not in text
    assert GS.main(argv) == 0                                    # its own output is not part of run 1


# --- D9: nothing before the seal -----------------------------------------------------------------------

def test_before_the_seal_nothing_is_evaluated_and_scoring_is_refused(unsealed):
    w = unsealed
    rep = w.evaluate()
    assert rep["verdict"] == GS.INCOMPLETE and rep["conditions"] == {} and "not sealed" in rep["note"]
    with pytest.raises(GS.Refused, match="blind_read_not_sealed"):
        GS.score(w.candidate(), w.store, w.sheets, w.freeze_sha())
    args = ["--batch", str(w.batch), "--sheets", str(w.sheets), "--freeze", str(w.freeze), "--inputs", str(w.inputs)]
    assert GS.main(["ledger-sheet", *args]) == 2 and not (w.batch / GS.LEDGER_DIR).exists()
    assert GS.main(["evaluate", *args, "--store", str(w.store)]) == 3
    assert not (w.batch / "scores").exists() and not (w.batch / GS.OUT_DIR).exists()


def _seal(w):
    return GS.seal(w.sheets, w.candidate(), w.freeze_sha(), FX.SCORED_BY, attest=True)


def test_seal_refuses_scores_that_belong_to_another_batch(unsealed):
    """Codex 16:22 P2: eight complete verdict sets from another batch must not seal beside fresh sheets."""
    w = unsealed
    for p in (w.sheets / "scores").glob("*.json"):
        edit_json(p, lambda d: d.update(generation_id=d["generation_id"].replace(FX.BATCH_ID, "w6-other-batch")))
    assert GS.MS.check(w.sheets) == (8, 8, [])                   # the unchanged checker sees nothing wrong
    with pytest.raises(GS.Refused, match="score_generation_id"):
        _seal(w)


@pytest.mark.parametrize("change, problem", [
    (lambda w: (w.sheets / "scores" / "C7.json").unlink(), "not_exactly_the_eight_items"),
    (lambda w: shutil.copy(w.sheets / "scores" / "C7.json", w.sheets / "scores" / "S2.json"),
     "not_exactly_the_eight_items"),
    (lambda w: (w.sheets / "scores" / "C7.json").rename(w.sheets / "scores" / "c7-old.json"),
     "not_exactly_the_eight_items"),
    (lambda w: edit_json(w.sheets / "scores" / "S3.json", lambda d: d["verdicts"].pop("omission")), "S3:verdicts"),
    (lambda w: edit_json(w.sheets / "scores" / "S3.json", lambda d: d["verdicts"].update(added=None)), "S3:verdicts"),
    (lambda w: edit_json(w.sheets / "scores" / "S3.json", lambda d: d["verdicts"].update(added="fail")),
     "S3:added:no_finding"),
    (lambda w: edit_json(w.sheets / "scores" / "S3.json", lambda d: d.pop("scored_by")), "S3:scored_by"),
    (lambda w: edit_json(w.sheets / "scores" / "S3.json", lambda d: d.update(scored_by="Codex")), "S3:scored_by"),
    (lambda w: edit_json(w.sheets / "MANIFEST.json", lambda d: d["dimensions"].pop()), "MANIFEST.json:dimensions"),
    (lambda w: (w.sheets / "sheets" / "A5.md").write_text("edited"), "A5:sheet_differs_from_a_re-render"),
    (lambda w: (w.batch / "scores").mkdir(), "scores_already_exists_in_the_batch"),
    (lambda w: (w.batch / GS.OUT_DIR).mkdir(), "go-stop_already_exists_in_the_batch"),
])
def test_seal_refuses(unsealed, change, problem):
    w = unsealed
    change(w)
    with pytest.raises(GS.Refused, match=problem):
        _seal(w)
    assert not (w.sheets / GS.SEAL_FILE).exists()


def test_seal_needs_the_attestation_and_is_made_once(unsealed):
    w = unsealed
    with pytest.raises(GS.Refused, match="not_attested"):
        GS.seal(w.sheets, w.candidate(), w.freeze_sha(), FX.SCORED_BY, attest=False)
    doc = _seal(w)
    assert [i["scenario_id"] for i in doc["items"]] == list(GS.MS.ITEMS) and doc["attestation"] == GS.ATTESTATION
    with pytest.raises(GS.Refused, match="already_sealed"):
        _seal(w)


@pytest.mark.parametrize("change", [
    lambda w: edit_json(w.sheets / "scores" / "S16.json", lambda d: d["verdicts"].update(meaning="pass ")),
    lambda w: edit_json(w.sheets / "scores" / "S16.json", lambda d: d.update(findings=[{"dimension": "added"}])),
    lambda w: (w.sheets / "sheets" / "S16.md").write_text("edited"),
    lambda w: edit_json(w.sheets / GS.SEAL_FILE, lambda d: d.update(batch_id="another")),
    lambda w: edit_json(w.sheets / GS.SEAL_FILE, lambda d: d.update(freeze_sha256="0" * 64)),
    lambda w: edit_json(w.sheets / GS.SEAL_FILE, lambda d: d.update(attestation="scored later")),
    lambda w: edit_json(w.sheets / "MANIFEST.json", lambda d: d.update(note="x")),
])
def test_anything_changed_after_sealing_is_refused(world, change):
    change(world)
    with pytest.raises(GS.Refused, match="seal:"):
        world.evaluate()


def test_a_blind_read_score_below_pass_stops(source, tmp_path):
    def partial(w):
        edit_json(w.sheets / "scores" / "S1.json", lambda d: (d["verdicts"].update(audience="partial"),
                                                              d.update(findings=[{"dimension": "audience"}])))
    rep = FX.build_world(tmp_path / "w", *source, hooks={"before_seal": partial}).evaluate()
    assert rep["verdict"] == GS.STOP and statuses(rep)["G7"] == GS.NOT_MET
    assert why(rep, "G7") == ["S1: audience partial"]
    assert any("KF-FX BLIND" in x for x in why(rep, "G6"))        # the audience known failure is decided by the read


# --- G3: rulings, bound to this candidate ----------------------------------------------------------------

def test_rulings_not_bound_is_incomplete_never_go(world):
    (world.store / GS.RULINGS_FILE).unlink()
    rep = world.evaluate()
    assert rep["verdict"] == GS.INCOMPLETE and statuses(rep)["G3"] == GS.UNDECIDED
    assert sum(v == GS.MET for v in statuses(rep).values()) == 6          # G6's RULED check is undecided too


def _drop_one_ruling(w):
    edit_json(w.store / "S5.json" if (w.store / "S5.json").exists() else w.store / "S1.json",
              lambda d: d["patient_rulings"].pop(sorted(d["patient_rulings"])[0]))


def test_a_pending_flag_is_incomplete(source, tmp_path):
    rep = FX.build_world(tmp_path / "w", *source, hooks={"before_bind": _drop_one_ruling}).evaluate()
    assert rep["verdict"] == GS.INCOMPLETE and statuses(rep)["G3"] == GS.UNDECIDED
    assert why(rep, "G3") == ["1 flag(s) pending"] or why(rep, "G3") == ["2 flag(s) pending"]


def test_a_defect_stops_even_with_other_flags_pending(source, tmp_path):
    def one_defect(w):
        _drop_one_ruling(w)
        edit_json(w.store / "S3.json", lambda d: d["patient_rulings"].update({sorted(d["patient_rulings"])[0]: "defect"}))
    rep = FX.build_world(tmp_path / "w", *source, hooks={"before_bind": one_defect}).evaluate()
    assert rep["verdict"] == GS.STOP and statuses(rep)["G3"] == GS.NOT_MET
    assert any("ruled defect" in x for x in why(rep, "G3"))


def test_a_ruling_changed_after_binding_is_refused(world):
    edit_json(world.store / "S3.json", lambda d: d["patient_rulings"].update({sorted(d["patient_rulings"])[0]: "defect"}))
    with pytest.raises(GS.Refused, match="S3:rulings_changed_since_binding"):
        world.evaluate()


def test_binding_refuses_a_ruled_key_that_is_not_in_this_batch(source, tmp_path):
    foreign = lambda w: edit_json(w.store / "S3.json", lambda d: d["patient_rulings"].update({"0" * 16: "acceptable"}))  # noqa: E731
    with pytest.raises(GS.Refused, match="S3:ruled_key_not_in_this_batch"):
        FX.build_world(tmp_path / "w", *source, hooks={"before_bind": foreign})


def test_a_store_bound_to_another_candidate_is_refused_though_its_keys_coincide(go_world, source, tmp_path):
    """Codex 16:22 P2: ruling keys carry no batch identity, so the binding must."""
    cases = FX.fixture_cases(*source)
    cases["adopted"]["by"] = "another candidate"                 # another case list -> another freeze -> another candidate
    other = FX.build_world(tmp_path / "other", *source, cases=cases)
    assert other.evaluate()["verdict"] == GS.GO
    same = lambda w: json.loads((w.store / "S1.json").read_text())["patient_rulings"]  # noqa: E731
    assert same(other) == same(go_world) and same(other)         # every key coincides
    with pytest.raises(GS.Refused, match="store:freeze_sha256"):
        go_world.evaluate(store=other.store)
    w = FX.copy_world(go_world, tmp_path / "w")
    shutil.copy(other.store / GS.RULINGS_FILE, w.store / GS.RULINGS_FILE)     # only the binding swapped
    with pytest.raises(GS.Refused, match="rulings:freeze_sha256"):
        w.evaluate()


def test_the_binding_keeps_its_history_when_rulings_are_corrected(world):
    c = world.candidate()
    edit_json(world.store / "S3.json", lambda d: d["patient_rulings"].update({sorted(d["patient_rulings"])[0]: "defect"}))
    doc = GS.bind_rulings(c, world.store, world.freeze_sha(), FX.SCORED_BY, attest=True)
    assert len(doc["history"]) == 1 and world.evaluate()["verdict"] == GS.STOP
    with pytest.raises(GS.Refused, match="not_attested"):
        GS.bind_rulings(c, world.store, world.freeze_sha(), "Codex", attest=True)


def test_scoring_first_needs_an_empty_unbound_store_and_later_validates_the_binding(source, tmp_path):
    w = FX.build_world(tmp_path / "w", *source, until="seal")
    edit_json(w.store / "S1.json", lambda d: d["patient_rulings"].update({"0" * 16: "acceptable"}))
    with pytest.raises(GS.Refused, match="first_scoring_needs_an_empty_unbound_store"):
        GS.score(w.candidate(), w.store, w.sheets, w.freeze_sha())
    edit_json(w.store / "S1.json", lambda d: d["patient_rulings"].clear())
    first = GS.score(w.candidate(), w.store, w.sheets, w.freeze_sha())
    assert first["first"] and first["review_items"] > 0
    FX.rule_everything(w)
    GS.bind_rulings(w.candidate(), w.store, w.freeze_sha(), FX.SCORED_BY, attest=True)
    again = GS.score(w.candidate(), w.store, w.sheets, w.freeze_sha())      # re-scoring after binding is supported
    assert not again["first"] and again["inventory_sha256"] == first["inventory_sha256"]
    edit_json(w.store / "S1.json", lambda d: d["patient_rulings"].clear())
    with pytest.raises(GS.Refused, match="rulings_changed_since_binding"):
        GS.score(w.candidate(), w.store, w.sheets, w.freeze_sha())


def test_a_changed_upstream_family_is_refused(world):
    edit_json(world.store / "S1.json", lambda d: d["compose_rulings"].update({"x": "acceptable"}))
    with pytest.raises(GS.Refused, match="store:S1:family_differs:compose_rulings"):
        world.evaluate()


# --- G4: the ledger review covers every generation --------------------------------------------------------

def test_an_empty_review_is_incomplete(world):
    edit_json(world.review, lambda d: ([g.update(acknowledged=None) for g in d["generations"]],
                                       [x.update(verdict=None) for x in d["drops"] + d["sections"]]))
    rep = world.evaluate()
    assert rep["verdict"] == GS.INCOMPLETE and statuses(rep)["G4"] == GS.UNDECIDED


def test_one_zero_drop_generation_not_acknowledged_prevents_go(world):
    """Codex 16:22 P1: a generation with no drops still needs its ledger reviewed."""
    d = json.loads(world.review.read_text())
    zero = [g for g in d["generations"] if g["n_drops"] == 0]
    assert [g["generation_id"] for g in zero] == [f"{FX.BATCH_ID}/r1/S2"]
    edit_json(world.review, lambda d: next(g for g in d["generations"]
                                           if g["generation_id"] == zero[0]["generation_id"]).update(acknowledged=None))
    rep = world.evaluate()
    assert rep["verdict"] == GS.INCOMPLETE and why(rep, "G4") == [f"{zero[0]['generation_id']}: not acknowledged"]


@pytest.mark.parametrize("change, expect", [
    (lambda d: d["drops"][0].update(verdict=None), "drop {drop_id}: no verdict"),
    (lambda d: d["drops"][0].update(verdict="fine"), "drop {drop_id}: no verdict"),
    (lambda d: d["sections"][0].update(verdict=None), "section {section_key}: no verdict"),
    (lambda d: d["binding"].update(reviewed_by="Codex"), "reviewed_by / reviewed_at not entered"),
    (lambda d: d["binding"].update(reviewed_at=None), "reviewed_by / reviewed_at not entered"),
])
def test_a_drop_a_section_or_the_reviewer_missing_is_incomplete(world, change, expect):
    d = json.loads(world.review.read_text())
    edit_json(world.review, change)
    rep = world.evaluate()
    assert rep["verdict"] == GS.INCOMPLETE
    assert why(rep, "G4") == [expect.format(drop_id=d["drops"][0]["drop_id"], section_key=d["sections"][0]["section_key"])]


def test_no_review_file_is_incomplete(world):
    world.review.unlink()
    assert statuses(world.evaluate())["G4"] == GS.UNDECIDED


def test_f1b_a_required_dropped_verdict_stops_with_line_9_met(world):
    """Verdict propagation (NOT a content-loss case): S8's investigation section is dropped whole by
    authorised ops — audit clean, line 9 MET — and the reviewer's verdict alone stops the run."""
    d = json.loads(world.review.read_text())
    assert [(s["generation_id"], s["source_id"]) for s in d["sections"]] == [(f"{FX.BATCH_ID}/r1/S8", "sections.2")]
    edit_json(world.review, lambda d: d["sections"][0].update(verdict="required_dropped", category="stated test conclusion"))
    rep = world.evaluate()
    assert rep["verdict"] == GS.STOP
    assert statuses(rep) == {f"G{i}": GS.MET for i in range(1, 9)} | {"G4": GS.NOT_MET}
    assert "required item dropped" in why(rep, "G4")[0]
    assert bar_line(world.batch, world.store, "9") == "MET"


@pytest.mark.parametrize("change, problem", [
    (lambda d: d["binding"].update(batch_id="another"), "ledger:batch_id"),
    (lambda d: d["binding"].update(freeze_sha256="0" * 64), "ledger:freeze_sha256"),
    (lambda d: d["binding"].update(inventory_sha256="0" * 64), "ledger:inventory_sha256"),
    (lambda d: d["generations"].pop(), "ledger:generations:not_exactly_this_batch"),
    (lambda d: d["drops"].pop(), "ledger:drops:not_exactly_this_batch"),
    (lambda d: d["drops"].append(dict(d["drops"][0], drop_id="0" * 16)), "ledger:drops:not_exactly_this_batch"),
    (lambda d: d["sections"].pop(), "ledger:sections:not_exactly_this_batch"),
    (lambda d: d["generations"][0].update(ledger_sha256="0" * 64), "changed_since_the_review"),
    (lambda d: d["drops"][0].update(dropped_text_sha256="0" * 64), "dropped_text_changed"),
])
def test_a_review_for_another_candidate_or_a_changed_ledger_is_refused(world, change, problem):
    edit_json(world.review, change)
    with pytest.raises(GS.Refused, match=problem):
        world.evaluate()


# --- the tampered fixtures (F1a, F2, F3, F4, F5): STOP through the ordinary evaluator -----------------------

def _s7(rec):
    return rec["steps"]["compose_patient"]


def _led(rec, source_id, line):
    return next(x for x in _s7(rec)["ledger"]["lines"] if (x["source_id"], x["line"]) == (source_id, line))


def _lines(rec, heading):
    return next(s for s in _s7(rec)["sections"] if s["heading"] == heading)["text"].split("\n")


def _write(rec, heading, lines):
    next(s for s in _s7(rec)["sections"] if s["heading"] == heading)["text"] = "\n".join(lines)


def _unwrite(rec, x, text_len, unit="line"):
    """Remove a ledger line's C line and label it a withheld drop (a false label)."""
    lines = _lines(rec, x["c_heading"])
    idx = x["c_line"]
    del lines[idx]
    _write(rec, x["c_heading"], lines)
    for y in _s7(rec)["ledger"]["lines"]:
        if y["c_heading"] == x["c_heading"] and y["c_line"] is not None and y["c_line"] > idx:
            y["c_line"] -= 1
    x["c_line"] = None
    x["ops"] = [{"op": "drop", "a": 0, "b": text_len, "rule": "withheld", "entry": "w_grace", "unit": unit}]


def f1a(rec):
    """D3' — a wholly dropped investigation section holding a stated test conclusion (real content loss)."""
    for line, text in ((1, "CTPA: no PE."), (2, "CXR consolidation, CURB-65 2.")):
        _unwrite(rec, _led(rec, "sections.2", line), len(text))


def f2(rec):
    """D3' — a clause lost inside a line that stays rendered and cited (ops unchanged)."""
    lines = _lines(rec, "what_we_did")
    assert lines[1] == "- Chest X-ray (CXR) consolidation"
    lines[1] = "- Chest X-ray (CXR)"
    _write(rec, "what_we_did", lines)


def f3(rec):
    """D3' — an omitted Background diagnosis."""
    _unwrite(rec, _led(rec, "sections.1", 4), len("Asthma"))


def f4(rec):
    """Step 2 — a fabricated substitution (the op's `out` and the C line changed together)."""
    x = _led(rec, "sections.1", 1)
    op = next(o for o in x["ops"] if o.get("entry") == "g_nstemi")
    lines = _lines(rec, "why_in_hospital")
    lines[x["c_line"]] = lines[x["c_line"]].replace(op["out"], "mild heart strain (NSTEMI)")
    op["out"] = "mild heart strain (NSTEMI)"
    _write(rec, "why_in_hospital", lines)


def f5(rec):
    """Step 2 — a falsely labelled drop: a GP action's timing dropped under a withheld label, ops and C line consistent."""
    x = _led(rec, "sections.8", 1)
    text = "Please check U&E in 1 week."
    cut = text.index(" in 1 week")
    assert [o["op"] for o in x["ops"]] == ["copy", "sub", "copy"] and x["ops"][2]["a"] == cut
    x["ops"] = x["ops"][:2] + [{"op": "drop", "a": cut, "b": len(text), "rule": "withheld", "entry": "w_grace",
                                "unit": "comma"}]
    lines = _lines(rec, "next_steps")
    lines[x["c_line"]] = "- " + text[:x["ops"][1]["a"]] + x["ops"][1]["out"]
    _write(rec, "next_steps", lines)


def dxc(rec):
    """Codex 12 Nov 06:42 — a Background diagnosis changed in C only: its ledger index and ops untouched."""
    x = _led(rec, "sections.1", 4)
    lines = _lines(rec, "why_in_hospital")
    assert lines[x["c_line"]] == "- Asthma"
    lines[x["c_line"]] = "- Mild asthma"
    _write(rec, "why_in_hospital", lines)


SECOND_CATCH = {"f1a": "located_case", "f3": "diagnosis_problems", "f4": "diagnosis_problems",
                "dxc": "diagnosis_problems"}       # also stopped by G6, independently of the audit (G5)


def tampered(source, root, fn):
    return FX.build_world(root, *source, hooks={"after_render": lambda w: FX.both_runs(w, TAMPERED, fn)})


FIXTURES = {"f1a": (f1a, ":drop_"), "f2": (f2, ":reproduction"), "f3": (f3, ":drop_"),
            "f4": (f4, ":sub_out_differs"), "f5": (f5, ":drop_"), "dxc": (dxc, ":reproduction")}


@pytest.fixture(scope="module")
def stop_worlds(source, tmp_path_factory):
    root = tmp_path_factory.mktemp("r1-stop")
    return {name: tampered(source, root / name, fn) for name, (fn, _d) in FIXTURES.items()}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_tampered_fixture_stops_on_the_audit_with_line_9_met(stop_worlds, name):
    w, diag = stop_worlds[name], FIXTURES[name][1]
    rep = w.evaluate()
    st = statuses(rep)
    assert rep["verdict"] == GS.STOP and st["G5"] == GS.NOT_MET
    # F1a, F3, F4 and the C-only change are caught a second time, independently of the audit, by G6 (a located
    # case / the diagnosis check's own source-to-C accounting)
    assert {g for g, v in st.items() if v != GS.MET} == ({"G5", "G6"} if name in SECOND_CATCH else {"G5"})
    g5 = why(rep, "G5")
    assert all(x.startswith(f"{FX.BATCH_ID}/r1/S11: ") for x in g5)
    assert any(diag in x for x in g5) and any(x.endswith("rerender_differs") for x in g5)
    assert bar_line(w.batch, w.store, "9") == "MET"             # line 9 does not see any of these (D3')


def test_f1a_the_dropped_section_is_put_to_the_reviewer_and_his_verdict_also_stops(stop_worlds, tmp_path):
    w = FX.copy_world(stop_worlds["f1a"], tmp_path / "w")
    d = json.loads(w.review.read_text())
    mine = [s for s in d["sections"] if s["generation_id"].endswith("/r1/S11")]
    assert [s["source_id"] for s in mine] == ["sections.2"]
    edit_json(w.review, lambda d: next(s for s in d["sections"] if s["section_key"] == mine[0]["section_key"])
              .update(verdict="required_dropped", category="stated test conclusion"))
    st = statuses(w.evaluate())
    assert st["G4"] == GS.NOT_MET and st["G5"] == GS.NOT_MET


def test_f3_is_caught_by_the_diagnosis_check_independently_of_the_audit(stop_worlds):
    assert why(stop_worlds["f3"].evaluate(), "G6") == [f"diagnosis {FX.BATCH_ID}/r1/S11: sections.1:4:not_rendered"]


def test_a_c_only_change_and_a_fabricated_substitution_fail_the_diagnosis_check_by_name(stop_worlds):
    """Codex 12 Nov 06:42 P2: the saved C text itself is checked, not only the ledger's claims."""
    gid = f"diagnosis {FX.BATCH_ID}/r1/S11: "
    assert why(stop_worlds["dxc"].evaluate(), "G6") == [gid + "sections.1:4:c_text_differs"]
    assert why(stop_worlds["f4"].evaluate(), "G6") == [gid + "sections.1:1:substitution_not_approved"]


def _without(orig, needle):
    return lambda *a, **k: [p for p in orig(*a, **k) if needle not in p]


@pytest.mark.parametrize("name, target, needle", [
    ("f1a", "authorise_ops", ":drop_"), ("f2", "verify_line", "reproduction"), ("f3", "authorise_ops", ":drop_"),
    ("f4", "authorise_ops", ":sub_"), ("f5", "authorise_ops", ":drop_"), ("dxc", "verify_line", "reproduction"),
])
def test_with_the_targeted_check_broken_the_rerender_still_stops_it(stop_worlds, monkeypatch, name, target, needle):
    """Codex 16:22 P2: audit() also re-renders, so one broken line check does not turn a tampered record into GO."""
    monkeypatch.setattr(RP, target, _without(getattr(RP, target), needle))
    rep = stop_worlds[name].evaluate()
    g5 = why(rep, "G5")
    assert rep["verdict"] == GS.STOP and not any(needle in x for x in g5)
    assert any(x.endswith("rerender_differs") for x in g5)


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_with_the_rerender_broken_the_line_check_still_stops_it(stop_worlds, monkeypatch, name):
    monkeypatch.setattr(RP, "audit", _without(RP.audit, "rerender"))
    rep = stop_worlds[name].evaluate()
    assert rep["verdict"] == GS.STOP and any(FIXTURES[name][1] in x for x in why(rep, "G5"))


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_the_evaluator_depends_on_its_audit_condition(stop_worlds, monkeypatch, name):
    """With G5 ignored a tampered record would pass (F2, F5) — F1a, F3, F4 and the C-only diagnosis change are
    still stopped by G6, independently of the audit, and pass only when that second catch is ignored too."""
    monkeypatch.setattr(GS, "audit_condition", lambda *a, **k: {"status": GS.MET, "why": []})
    rep = stop_worlds[name].evaluate()
    if name not in SECOND_CATCH:
        assert rep["verdict"] == GS.GO
        return
    assert rep["verdict"] == GS.STOP and {g for g, v in statuses(rep).items() if v != GS.MET} == {"G6"}
    monkeypatch.setattr(GS, SECOND_CATCH[name], lambda *a, **k: [])
    assert stop_worlds[name].evaluate()["verdict"] == GS.GO


def test_fk_above_8_on_one_generation_fails_the_code_lines(source, tmp_path):
    def hard(rec):
        _write(rec, "what_we_did", ["- Comprehensive multidisciplinary gastroenterological investigations demonstrated "
                                    "unremarkable echocardiographic characteristics throughout the hospitalisation."] * 4)
    w = FX.build_world(tmp_path / "w", *source, hooks={"after_render": lambda w: FX.both_runs(w, TAMPERED, hard)})
    rep = w.evaluate()
    g2 = rep["conditions"]["G2 code-decided bar lines"]
    assert rep["verdict"] == GS.STOP and g2["status"] == GS.NOT_MET
    assert g2["why"] == ["7 FK <= 8 on every generation", f"FK above 8 or missing: {FX.BATCH_ID}/r1/S11"]


def test_an_audit_that_cannot_run_fails_closed(go_world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cannot run")
    monkeypatch.setattr(RP, "audit", boom)
    rep = go_world.evaluate()
    assert rep["verdict"] == GS.STOP and statuses(rep)["G5"] == GS.NOT_MET
    assert len(why(rep, "G5")) == 18 and all(x.endswith("audit_raised:RuntimeError") for x in why(rep, "G5"))
    assert why(rep, "G6") == ["SX2 (synthetic): audit_raised:RuntimeError"]   # a control whose audit cannot run fails too


# --- F6: the denominator is the source index ---------------------------------------------------------------

def test_f6_one_refusal_stops_on_the_count_and_on_every_line_that_needs_c(tmp_path):
    src = FX.make_source(tmp_path / "s", variants={"S11": FX.REFUSE})
    rep = FX.build_world(tmp_path / "w", *src).evaluate()
    st = statuses(rep)
    assert rep["verdict"] == GS.STOP and st["G1"] == st["G2"] == GS.NOT_MET
    g1 = rep["conditions"]["G1 every generation rendered"]
    assert (g1["rendered"], g1["of"]) == (17, 18)
    assert g1["why"] == [f"{FX.BATCH_ID}/r1/S11: unsupported_form"]
    lines = rep["conditions"]["G2 code-decided bar lines"]["lines"]
    assert {k.split(" ", 1)[0] for k, v in lines.items() if v == GS.NOT_MET} == {"1", "2", "3", "5", "6", "7", "8", "9", "11", "13"}
    assert st["G5"] == GS.MET                                   # a refusal is not an audit problem
    g4 = json.loads((tmp_path / "w" / FX.BATCH_ID / GS.LEDGER_DIR / "ledger-review.json").read_text())
    assert [g["generation_id"] for g in g4["generations"] if g["refused"] == "unsupported_form"] == \
        [f"{FX.BATCH_ID}/r1/S11"]                                # a refused generation is still listed


def test_a_refused_blind_read_item_means_no_blind_read_and_everything_is_reported(tmp_path):
    src = FX.make_source(tmp_path / "s", variants={"S4": FX.REFUSE})
    w = FX.build_world(tmp_path / "w", *src)
    assert not w.sheets.exists()
    rep = w.evaluate(sheets=None)
    st = statuses(rep)
    assert rep["verdict"] == GS.STOP and st["G1"] == st["G7"] == GS.NOT_MET
    assert rep["blind_read"] == "no blind read possible" and len(rep["conditions"]) == 8


# --- the full size: five runs, 90 generations (Codex 12 Nov 06:42 — the 18-generation fixture left this untested) --

@pytest.fixture(scope="module")
def world90(tmp_path_factory):
    root = tmp_path_factory.mktemp("r1-go90")
    return FX.build_world(root / "w", *FX.make_source(root / "s", runs=5))


def test_go_control_at_five_runs_and_90_generations(world90):
    rep = world90.evaluate()
    assert rep["verdict"] == GS.GO and set(statuses(rep).values()) == {GS.MET}
    cond = rep["conditions"]
    g1 = cond["G1 every generation rendered"]
    assert (g1["rendered"], g1["of"]) == (90, 90) and len(rep["per_generation"]) == 90
    g4 = next(v for k, v in cond.items() if k.startswith("G4 "))
    assert g4["generations"] == 90 and g4["drops"] > 0 and g4["sections_wholly_dropped"] == 5     # S8, once per run
    assert next(v for k, v in cond.items() if k.startswith("G8 "))["files_compared"] == 93      # 90 records + 3
    review = json.loads(world90.review.read_text())
    gens = [g["generation_id"] for g in review["generations"]]
    assert len(gens) == len(set(gens)) == 90
    assert all(sum(1 for g in gens if f"/r{n}/" in g) == 18 for n in range(1, 6))
    assert sorted(g["generation_id"] for g in review["generations"] if g["n_drops"] == 0) == \
        [f"{FX.BATCH_ID}/r{n}/S2" for n in range(1, 6)]                                           # zero-drop, every run
    seal = json.loads((world90.sheets / GS.SEAL_FILE).read_text())
    assert len(seal["items"]) == 8 and all("/r1/" in i["generation_id"] for i in seal["items"])   # the read is run 1
    c = world90.candidate()
    assert len(c.records) == 90 and sorted({r["run"] for r in c.records}) == [1, 2, 3, 4, 5]


def test_at_90_one_run_5_generation_not_acknowledged_is_incomplete_and_named(world90, tmp_path):
    w = FX.copy_world(world90, tmp_path / "w")
    gid = f"{FX.BATCH_ID}/r5/S2"                                 # a zero-drop generation of the LAST run
    edit_json(w.review, lambda d: next(g for g in d["generations"] if g["generation_id"] == gid).update(acknowledged=None))
    rep = w.evaluate()
    assert rep["verdict"] == GS.INCOMPLETE and why(rep, "G4") == [f"{gid}: not acknowledged"]
    assert {g for g, v in statuses(rep).items() if v != GS.MET} == {"G4"}


def test_at_90_a_run_5_generation_missing_from_the_review_is_refused(world90, tmp_path):
    w = FX.copy_world(world90, tmp_path / "w")
    gid = f"{FX.BATCH_ID}/r5/S2"
    edit_json(w.review, lambda d: d.update(generations=[g for g in d["generations"] if g["generation_id"] != gid]))
    with pytest.raises(GS.Refused, match="ledger:"):
        w.evaluate()


def test_at_90_a_refusal_in_run_5_only_stops_and_is_named(tmp_path):
    src = FX.make_source(tmp_path / "s", runs=5, variants={"r5/S11": FX.REFUSE})
    w = FX.build_world(tmp_path / "w", *src)
    rep = w.evaluate()
    g1 = rep["conditions"]["G1 every generation rendered"]
    assert rep["verdict"] == GS.STOP and (g1["rendered"], g1["of"]) == (89, 90)
    assert g1["why"] == [f"{FX.BATCH_ID}/r5/S11: unsupported_form"]
    review = json.loads(w.review.read_text())
    assert [g["generation_id"] for g in review["generations"] if g["refused"]] == [f"{FX.BATCH_ID}/r5/S11"]
    assert len(review["generations"]) == 90                       # the refused generation is still listed
    assert statuses(rep)["G5"] == GS.MET and statuses(rep)["G7"] == GS.MET


def test_a_known_failure_check_limited_to_run_5_reads_run_5(world90):
    c, tables = world90.candidate(), RP.default_tables()
    kf = {"kf_id": "KF-R5", "scenario_id": "S11", "runs": [5], "checks": [{"type": "KEEP", "term": "GRACE"}]}
    failed, undecided = GS.known_failure(kf, c, tables, {}, None, True)
    assert undecided == [] and len(failed) == 1 and f"{FX.BATCH_ID}/r5/S11" in failed[0]
    failed, _ = GS.known_failure(kf | {"runs": "all"}, c, tables, {}, None, True)
    assert sorted(x.split(" ")[2].rstrip(":") for x in failed) == [f"{FX.BATCH_ID}/r{n}/S11" for n in range(1, 6)]


# --- G8: determinism ---------------------------------------------------------------------------------------

def test_run_2_missing_is_incomplete(world):
    rep = world.evaluate(run2=None)
    assert rep["verdict"] == GS.INCOMPLETE and statuses(rep)["G8"] == GS.UNDECIDED


def test_run_json_may_differ_but_nothing_else(world):
    edit_json(world.run2 / "run.json", lambda d: d.update(started_at="2026-11-12T09:00:00+00:00"))
    assert world.evaluate()["verdict"] == GS.GO
    p = world.run2 / "r1" / "S17.json"
    p.write_text(p.read_text() + "\n")
    rep = world.evaluate()
    assert rep["verdict"] == GS.STOP and why(rep, "G8") == ["differs: r1/S17.json"]


@pytest.mark.parametrize("change, problem", [
    (lambda w: (w.run2 / "r1" / "S17.json").unlink(), "only in run 1: r1/S17.json"),
    (lambda w: (w.run2 / "extra.json").write_text("{}"), "only in run 2: extra.json"),
    (lambda w: (w.run2 / "run.json").unlink(), "run 2 has no run.json"),
])
def test_run_2_must_hold_exactly_the_same_files(world, change, problem):
    change(world)
    rep = world.evaluate()
    assert rep["verdict"] == GS.STOP and why(rep, "G8") == [problem]


def test_run_2_cannot_be_run_1_itself(world):
    assert statuses(world.evaluate(run2=world.batch))["G8"] == GS.NOT_MET


# --- integrity: REFUSED ------------------------------------------------------------------------------------

def _manifest(fn):
    return lambda w: edit_json(w.batch / "manifest.json", lambda d: fn(d["derived_from"]))


def _record(fn, rel="r1/S17.json"):
    return lambda w: edit_json(w.batch / rel, fn)


@pytest.mark.parametrize("change, problem", [
    (_manifest(lambda d: d.update(freeze_sha256=None)), "batch:freeze_sha256"),
    (_manifest(lambda d: d.update(dev=True)), "batch:not_a_non_dev_renderer_batch"),
    (_manifest(lambda d: d.update(model_calls=1)), "batch:not_a_non_dev_renderer_batch"),
    (_manifest(lambda d: d.update(pipeline_version="0" * 64)), "batch:renderer_identity"),
    (_manifest(lambda d: d["renderer"]["tables"].update({"glossary.json": "0" * 64})), "batch:renderer_identity"),
    (_manifest(lambda d: d.update(inputs_sha256="0" * 64)), "batch:inputs"),
    (_manifest(lambda d: d.update(harness_version=1)), "batch:harness_version"),
    (_record(lambda d: d.update(calls=[{"name": "compose_patient"}])), "r1/S17.json:claims_a_model_call_or_timing"),
    (_record(lambda d: d.update(cost_usd_est=0.01)), "r1/S17.json:claims_a_model_call_or_timing"),
    (_record(lambda d: d["historical"].update(label="measured")), "r1/S17.json:claims_a_model_call_or_timing"),
    (_record(lambda d: d["render_provenance"].update(from_record_sha256="0" * 64)), "r1/S17.json:provenance"),
    (_record(lambda d: d["render_provenance"].update(freeze_sha256=None)), "r1/S17.json:provenance"),
    (_record(lambda d: d.update(generation_id="x/r1/S17")), "r1/S17.json:identity"),
    (_record(lambda d: d["steps"]["compose_clinical"]["sections"][0].update(text="Changed.")), "r1/S17.json:input_sha256"),
    (_record(lambda d: d["steps"]["compose_patient"]["renderer"].update(version=1)), "r1/S17.json:renderer_block"),
    (lambda w: edit_json(w.batch / "batch.json", lambda d: d["generations"].pop()), "batch:index_differs_from_inputs"),
])
def test_integrity_problems_are_refused_not_decided(world, change, problem):
    change(world)
    with pytest.raises(GS.Refused, match=problem):
        world.evaluate()


def test_a_freeze_record_that_no_longer_matches_the_files_is_refused(world, tmp_path, monkeypatch):
    pins = GS.FZ.file_pins()
    monkeypatch.setattr(GS.FZ, "file_pins", lambda root=ROOT: pins | {"evals/score_patient.py": "0" * 64})
    with pytest.raises(GS.Refused, match="freeze:file_changed:evals/score_patient.py"):
        world.evaluate()
    argv = ["evaluate", "--batch", str(world.batch), "--store", str(world.store), "--sheets", str(world.sheets),
            "--freeze", str(world.freeze), "--inputs", str(world.inputs)]
    assert GS.main(argv) == 2 and not (world.batch / GS.OUT_DIR).exists()


def test_another_freeze_record_is_refused(world, tmp_path):
    other = tmp_path / "FREEZE.json"
    d = json.loads(world.freeze.read_text())
    other.write_text(GS.FZ.dump(d | {"note": "re-frozen later"}))
    with pytest.raises(GS.Refused, match="batch:freeze_sha256"):
        world.evaluate(freeze=other)


@pytest.mark.parametrize("sid", ["../hist/S1", "/tmp/S1", "a/b", "S1.json", "", "STORE", None, 7])
def test_a_batch_whose_scenario_id_is_not_a_plain_name_is_refused(world, sid):
    """Codex 12 Nov 06:42 P2: a scenario id is used as a file name (the store, the sheets)."""
    def tamper(d):
        d["scenario_id"] = sid
    edit_json(world.batch / "r1" / "S11.json", tamper)
    with pytest.raises(GS.Refused, match="batch:scenario_id"):
        world.candidate()


# --- G6: cases and known failures ---------------------------------------------------------------------------

_PLAIN_SX = {"input": {"audience": "adult", "sections": [
    {"section_id": "sections.0", "heading": "presenting_complaint", "text": "Admitted with chest pain."}]},
    "at": {"source_id": "sections.0", "line": 1, "sentence": 1}}


def _cases(source, fn):
    cases = FX.fixture_cases(*source)
    fn(cases)
    return cases


def _first(cases, category, heading=None):
    return next(c for c in cases["located"] if c["category"] == category and "/S11" in c["record"]
                and heading in (None, c["heading"]))


@pytest.fixture(scope="module")
def go_parts(go_world):
    """What cases_condition needs, taken once from the GO world (the case list itself is the test's)."""
    c = go_world.candidate()
    scored = SB.score_batch(go_world.batch, adjudications_dir=go_world.store)
    rows = {r["generation_id"]: r["review"] for r in scored["patient"]["results"]}
    return c, RP.default_tables(), rows, json.loads((go_world.sheets / GS.SEAL_FILE).read_text())


@pytest.mark.parametrize("fn, expect", [
    (lambda c: None, None),
    (lambda c: _first(c, "L3")["expected"].update(outcome="refusal", code="instruction_overlap"),
     "rendered_but_a_refusal_was_expected"),
    (lambda c: _first(c, "L1")["expected"].update(drop={"rule": "withheld", "entry": "w_news"}), "expected_drop_not_made"),
    (lambda c: _first(c, "L1")["expected"].update(outcome="retained", no_drop=True), "unexpected_drop"),
    (lambda c: _first(c, "L3")["expected"].update(must_keep=["OGD", "outpatient"]), None),
    (lambda c: _first(c, "L1", "presenting_complaint")["expected"].update(must_keep=["police"]), "must_keep:term_not_kept"),
    (lambda c: _first(c, "L3")["expected"].update(c_must_match=["gastroscopy"]), "c_must_match"),
    (lambda c: _first(c, "L3")["expected"].update(c_must_not_match=["[Cc]amera test"]), "c_must_not_match"),
    (lambda c: _first(c, "L3")["expected"].update(outcome="kept"), "expected_outcome_not_recognised"),
    (lambda c: _first(c, "L3").update(sentence=4), "no_ledger_line"),
    (lambda c: c["synthetic"][0]["expected"].update(code="unsupported_form"), "refused_with_conflict_in_input"),
    (lambda c: c["synthetic"][0]["input"]["sections"].pop(), "rendered_but_a_refusal_was_expected"),
    (lambda c: c["synthetic"][0].update(at={"source_id": "sections.2", "line": 2, "sentence": None}),
     "SX1 (synthetic): refused_at_another_line"),
    (lambda c: c["synthetic"][0].update(_PLAIN_SX, expected={
        "outcome": "retained", "no_drop": True, "must_keep": ["chest pain"], "c_must_match": ["chest pain"],
        "c_must_not_match": ["fever"], "prose_must_match": ["^Admitted"]}), None),
    (lambda c: c["synthetic"][0].update(_PLAIN_SX, expected={"outcome": "retained", "c_must_match": ["fever"]}),
     "SX1 (synthetic): c_must_match"),
    (lambda c: c["synthetic"][0].update(_PLAIN_SX, expected={"outcome": "rendered", "c_must_match": ["chest pain"]}),
     "SX1 (synthetic): expected_outcome_not_recognised"),            # the old fall-through: an unknown outcome passed
    (lambda c: c["synthetic"][0].update(_PLAIN_SX, expected={}), "SX1 (synthetic): expected_outcome_not_recognised"),
    (lambda c: c["synthetic"][0].update(_PLAIN_SX, expected={"outcome": "approved_omission",
                                                             "drop": {"rule": "withheld", "entry": "w_grace"}}),
     "SX1 (synthetic): expected_drop_not_made"),                     # the omission never happens
    (lambda c: c["synthetic"][0].update(_PLAIN_SX, expected={"outcome": "approved_omission"}),
     "SX1 (synthetic): expected_drop_not_made"),                     # no omission prewritten at all
    (lambda c: c["synthetic"][0].update(_PLAIN_SX, at=None, expected={"outcome": "retained"}),
     "SX1 (synthetic): no_line_named"),
    (lambda c: c["synthetic"][0].update(_PLAIN_SX, at={"source_id": "sections.0", "line": 3, "sentence": 1},
                                        expected={"outcome": "retained"}), "SX1 (synthetic): no_ledger_line"),
    (lambda c: c["synthetic"][1]["expected"].update(drop={"rule": "withheld", "entry": "w_news"}),
     "SX2 (synthetic): expected_drop_not_made"),                     # the wrong omission
    (lambda c: c["synthetic"][1]["expected"].update(drop={"rule": "seek_help", "entry": "w_grace"}),
     "SX2 (synthetic): unexpected_drop"),                            # ... and the drop that was made was not prewritten
    (lambda c: c["synthetic"][1]["input"]["sections"][1].update(text="Primary: NSTEMI"),
     "SX2 (synthetic): expected_drop_not_made"),                     # the missing omission
    (lambda c: c["synthetic"][1].update(expected={"outcome": "retained", "no_drop": True}),
     "SX2 (synthetic): unexpected_drop"),
    (lambda c: c["synthetic"][1].update(expected={"outcome": "retained", "must_keep": ["NSTEMI"]}), None),
    (lambda c: c["synthetic"][1]["expected"].update(must_keep=["GRACE"]), "SX2 (synthetic): must_keep:term_not_kept"),
    (lambda c: c["synthetic"][1]["expected"].update(must_keep=["asthma"]),
     "SX2 (synthetic): must_keep:term_not_in_the_line"),
    (lambda c: c["synthetic"][1]["expected"].update(whole=True),
     "SX2 (synthetic): line_rendered_but_a_whole_drop_was_expected"),
    (lambda c: c["synthetic"][1]["expected"].update(prose_must_not_match=["heart attack"]),
     "SX2 (synthetic): prose_must_not_match"),
    (lambda c: c["synthetic"][1]["expected"].update(prose_must_match=["GRACE"]), "SX2 (synthetic): prose_must_match"),
    (lambda c: c["synthetic"][1]["expected"].update(c_must_not_match=["NSTEMI"]), "SX2 (synthetic): c_must_not_match"),
    (lambda c: c["synthetic"][1]["expected"].update(drop=None, drops=[{"rule": "withheld", "entry": "w_grace"}]), None),
    (lambda c: c["synthetic"][1]["expected"].update(drops=[{"rule": "withheld", "entry": "w_news"}]),
     "SX2 (synthetic): expected_drop_not_made"),
    (lambda c: c["synthetic"][1].update(input=None), "SX2 (synthetic): refused_with_bad_input"),
    (lambda c: _first(c, "L3")["expected"].update(must_keep=["zebra"]), "must_keep:term_not_in_the_line"),
    (lambda c: _first(c, "L1")["expected"].update(drops=[{"rule": "withheld", "entry": "w_news"}]),
     "expected_drop_not_made"),
    (lambda c: _first(c, "L1")["expected"].update(whole=True), "line_rendered_but_a_whole_drop_was_expected"),
    (lambda c: c["generations"][0].update(expected="maybe"), "U2-FX: expected_outcome_not_recognised"),
    (lambda c: c["generations"][0].update(c_must_not_match=["heart attack"]), "U2-FX: c_must_not_match"),
    (lambda c: c["known_failures"][0]["checks"].append({"type": "ADD", "token": "heart attack", "copy_only": True}),
     "sections.1:1:added"),
    (lambda c: c["known_failures"][0]["checks"].append({"type": "ADD", "token": "camera test", "not_from": "OGD"}),
     "sections.7:2:added"),
    (lambda c: c["known_failures"][0]["checks"].append({"type": "KEEP", "term": "GRACE"}), "sections.1:1:term_not_kept"),
    (lambda c: c["known_failures"][0]["checks"].append({"type": "KEEP", "term": "NSTEMI"}), None),
    (lambda c: c["known_failures"][0].update(scenario_id="S99"), "no generation of S99"),
    (lambda c: c["known_failures"][0].update(runs=[2]), "no generation of S1"),
    (lambda c: c["known_failures"][0].update(runs=[1]), None),
    (lambda c: c["known_failures"][0]["checks"].append({"type": "GUESS"}), "check type not recognised"),
])
def test_cases_and_known_failures(source, go_parts, fn, expect):
    c, tables, rows, seal = go_parts
    cnd = GS.cases_condition(_cases(source, fn), c, tables, rows, seal, True)
    if expect is None:
        assert cnd == {"status": GS.MET, "why": [], "located": cnd["located"], "synthetic": 2, "known_failures": 1}
    else:
        assert cnd["status"] == GS.NOT_MET and any(expect in x for x in cnd["why"])


def test_a_failing_case_stops_the_run_on_g6_alone(source, tmp_path):
    cases = _cases(source, lambda c: _first(c, "L3")["expected"].update(c_must_match=["gastroscopy"]))
    rep = FX.build_world(tmp_path / "w", *source, cases=cases).evaluate()
    assert rep["verdict"] == GS.STOP and {g for g, v in statuses(rep).items() if v != GS.MET} == {"G6"}


def test_known_failure_flags_are_undecided_until_ruled_and_bound(source, go_parts):
    c, tables, rows, seal = go_parts
    cnd = GS.cases_condition(FX.fixture_cases(*source), c, tables, rows, seal, False)
    assert cnd["status"] == GS.UNDECIDED and all("flags not yet ruled" in x for x in cnd["why"])
    cnd = GS.cases_condition(FX.fixture_cases(*source), c, tables, rows, None, True)
    assert cnd["status"] == GS.UNDECIDED and all("decided by the blind read" in x for x in cnd["why"])


def test_a_refusal_case_passes_only_at_its_code_and_line(tmp_path):
    src = FX.make_source(tmp_path / "s", variants={"S11": FX.REFUSE})

    def refusal(c, code="unsupported_form", line=1):
        loc = _first(c, "L6i")                                  # a case on the treatment section of S11
        loc.update(line=line, line_sha256=GS.sha_bytes(b"* IV antibiotics"), sentence=None,
                   expected={"outcome": "refusal", "code": code})
    for kw, expect in (({}, None), ({"code": "conflict_in_input"}, "refused_with_unsupported_form")):
        cases = FX.fixture_cases(*src)
        located = [c for c in cases["located"] if "/S11" not in c["record"]]
        extra = _first(FX.fixture_cases(*FX.make_source(tmp_path / f"s{len(kw)}")), "L6i")
        cases["located"] = located + [extra]
        refusal(cases, **kw)
        rep = FX.build_world(tmp_path / f"w{len(kw)}", *src, cases=cases).evaluate()
        g6 = [x for x in why(rep, "G6") if x.startswith(extra["case_id"])]
        assert (g6 == []) if expect is None else any(expect in x for x in g6)


def test_a_case_that_does_not_match_the_frozen_input_is_refused(source, tmp_path):
    cases = _cases(source, lambda c: _first(c, "L3").update(line_sha256="0" * 64))
    with pytest.raises(GS.Refused, match="does_not_match_the_frozen_input"):
        FX.build_world(tmp_path / "w", *source, cases=cases).evaluate()
    cases = _cases(source, lambda c: c.update(inputs_sha256="0" * 64))
    with pytest.raises(GS.Refused, match="cases:written_for_other_inputs"):
        FX.build_world(tmp_path / "w2", *source, cases=cases).evaluate()


def test_known_failure_flags_must_be_ruled_acceptable(source, tmp_path):
    def defect(w):
        inv = json.loads((w.batch / "scores" / GS.INVENTORY_FILE).read_text())["S1"]
        key = next(k for k, v in inv.items() if v["kind"] == "advice")
        edit_json(w.store / "S1.json", lambda d: d["patient_rulings"].update({key: "defect"}))
    rep = FX.build_world(tmp_path / "w", *source, hooks={"before_bind": defect}).evaluate()
    assert any("KF-FX RULED" in x and "ruled defect" in x for x in why(rep, "G6"))


def test_withheld_wording_left_in_c_fails_the_withheld_check(source, tmp_path):
    def leak(rec):
        lines = _lines(rec, "why_in_hospital")
        lines[0] += " GRACE score 142."
        _write(rec, "why_in_hospital", lines)
    w = FX.build_world(tmp_path / "w", *source, hooks={"after_render": lambda w: FX.both_runs(w, "r1/S1.json", leak)})
    assert any("KF-FX WITHHELD" in x and "withheld wording in C" in x for x in why(w.evaluate(), "G6"))


# --- the diagnosis check (Round 3) -----------------------------------------------------------------------------

def _dx(text, tamper=None, age_group="adult"):
    cc = {"sections": [{"part": "part_a", "heading": h, "section_id": f"sections.{i}", "text": t, "fact_ids": [],
                        "unverified_fact_ids": []}
                       for i, (h, t) in enumerate((("presenting_complaint", "Admitted with chest pain."),
                                                   ("diagnosis", text)))],
          "age_group": age_group, "checks": [], "counts": {}}
    tables = RP.default_tables()
    out = RP.render_patient(GS.CP7.patient_input(cc), tables)
    rec = {"steps": {"compose_clinical": cc, "compose_patient": out}}
    if tamper:
        tamper(rec)
    return GS.diagnosis_problems(rec, tables)


def test_standalone_labels_with_their_items_pass(source):
    """Codex 17:51 P2: the adopted grammar's label-alone-then-bullets form must not STOP."""
    assert _dx("Primary:\n- Right quinsy\nBackground:\n- Asthma\n- T2DM") == []
    assert _dx("Primary: NSTEMI\nSecondary diagnoses:\n- possible UTI") == []


def test_a_diagnosis_with_an_authorised_score_removal_passes():
    assert _dx("Primary: NSTEMI (GRACE 142)") == []
    assert _dx("Primary:\n- NSTEMI (GRACE score 142)\nBackground: T2DM, HTN") == []


def test_a_removed_background_item_stops():
    assert _dx("Primary: NSTEMI\nBackground:\n- T2DM\n- Asthma",
               lambda rec: _unwrite(rec, _led(rec, "sections.1", 4), len("Asthma"))) == ["sections.1:4:not_rendered"]


def test_an_emptied_item_stops():
    def empty(rec):
        x = _led(rec, "sections.1", 3)
        x["ops"] = [{"op": "drop", "a": 0, "b": len("T2DM"), "rule": "withheld", "entry": "w_grace", "unit": "line"}]
    assert _dx("Primary: NSTEMI\nBackground:\n- T2DM\n- Asthma", empty) == [
        "sections.1:3:c_text_differs", "sections.1:3:drop_not_confirmed_withheld", "sections.1:3:no_diagnosis_word_left"]


def test_an_inline_diagnosis_reduced_to_its_label_stops():
    def reduce(rec):
        x = _led(rec, "sections.1", 1)
        text = "Primary: NSTEMI"
        x["ops"] = [x["ops"][0], {"op": "drop", "a": 8, "b": len(text), "rule": "withheld", "entry": "w_grace",
                                  "unit": "aside"}]
    assert _dx("Primary: NSTEMI", reduce) == ["sections.1:1:c_text_differs", "sections.1:1:drop_not_confirmed_withheld",
                                              "sections.1:1:no_diagnosis_word_left"]


def test_only_a_withheld_drop_may_leave_a_diagnosis_line_and_only_after_the_label():
    def other_rule(rec):
        next(o for o in _led(rec, "sections.1", 1)["ops"] if o["op"] == "drop")["rule"] = "seek_help"
    assert _dx("Primary: NSTEMI (GRACE 142)", other_rule) == ["sections.1:1:drop_rule_seek_help"]

    def whole_label(rec):
        x = _led(rec, "sections.1", 2)
        x["ops"] = [{"op": "drop", "a": 0, "b": len("Background:"), "rule": "withheld", "entry": "w_grace", "unit": "line"}]
    assert _dx("Primary: NSTEMI\nBackground:\n- Asthma", whole_label) == [
        "sections.1:2:c_text_differs", "sections.1:2:standalone_label_not_structural"]


def test_a_diagnosis_line_missing_from_the_ledger_stops():
    assert _dx("Primary: NSTEMI\nBackground:\n- Asthma",
               lambda rec: _s7(rec)["ledger"]["lines"].pop()) == ["sections.1:3:no_ledger_line"]


# --- the diagnosis check's own source-to-C accounting (Codex 12 Nov 06:42 P2) -----------------------------------

DX = "Primary: NSTEMI (GRACE 142)\nBackground:\n- T2DM\n- Asthma"
# C: ["Admitted with chest pain.", "Main problem: heart attack (NSTEMI)", "Health history:", "- Diabetes (T2DM)", "- Asthma"]


def _c(rec, fn):
    lines = _lines(rec, "why_in_hospital")
    fn(lines)
    _write(rec, "why_in_hospital", lines)


@pytest.mark.parametrize("change, expect", [
    (lambda ls: ls.__setitem__(4, "- No asthma"), ["sections.1:4:c_text_differs"]),             # a word added
    (lambda ls: ls.__setitem__(4, "- "), ["sections.1:4:c_text_differs"]),                       # the word removed
    (lambda ls: ls.__setitem__(4, "- asthma"), ["sections.1:4:c_text_differs"]),                 # only the case
    (lambda ls: ls.__setitem__(4, "Asthma"), ["sections.1:4:c_text_differs"]),                   # the bullet marker lost
    (lambda ls: ls.__setitem__(3, "- Diabetes"), ["sections.1:3:c_text_differs"]),               # the bracketed term lost
    (lambda ls: ls.__setitem__(1, "Main problem: mild heart attack (NSTEMI)"), ["sections.1:1:c_text_differs"]),
    (lambda ls: ls.__setitem__(1, "Main problem: heart attack (NSTEMI) (GRACE 142)"), ["sections.1:1:c_text_differs"]),
    (lambda ls: ls.__setitem__(2, "Other problems:"), ["sections.1:2:c_text_differs"]),          # a standalone label changed
    (lambda ls: ls.__setitem__(slice(3, 5), [ls[4], ls[3]]), ["sections.1:3:c_text_differs", "sections.1:4:c_text_differs"]),
    (lambda ls: ls.pop(), ["sections.1:4:c_text_differs"]),                                      # the C line is not there
    (lambda ls: ls.insert(1, "- Epilepsy"), ["sections.1:1:c_text_differs", "sections.1:2:c_text_differs",
                                             "sections.1:3:c_text_differs", "sections.1:4:c_text_differs"]),
    (lambda ls: ls.__setitem__(0, "Admitted with chest pain and fever."), []),   # not a diagnosis line: G5's, not this check's
])
def test_a_diagnosis_line_changed_in_c_with_its_ops_unchanged_fails(change, expect):
    assert _dx(DX) == []
    assert _dx(DX, lambda rec: _c(rec, change)) == expect


def test_approved_substitutions_labels_and_score_removals_still_pass():
    assert _dx("Primary: ?quinsy\nSecondary: query PE\nBackground:\n- h/o asthma\n"
               "- T2DM (inferred — not documented, confirm)") == []
    assert _dx("Primary: ?Quinsy\nBackground:\n- Asthma\n- IDA", age_group="child") == []
    assert _dx("Primary:\n- NSTEMI (GRACE score 142)\nBackground: T2DM, HTN") == []
    assert _dx("Primary: NSTEMI  with anaemia\nBackground:\n- Pneumonia (CURB-65 2, severe)") == []   # two spaces in A


def _op(rec, line, pick):
    return next(o for o in _led(rec, "sections.1", line)["ops"] if pick(o))


def _swap_out(rec, line, pick, new):
    """Change a substitution's output in the ledger AND in C, consistently (so only its approval can catch it)."""
    op = _op(rec, line, pick)
    x = _led(rec, "sections.1", line)
    _c(rec, lambda ls: ls.__setitem__(x["c_line"], ls[x["c_line"]].replace(op["out"], new)))
    op["out"] = new


@pytest.mark.parametrize("tamper", [
    lambda rec: _swap_out(rec, 1, lambda o: o.get("entry") == "g_nstemi", "mild heart strain (NSTEMI)"),   # another plain form
    lambda rec: _swap_out(rec, 1, lambda o: o.get("entry") == "g_nstemi", "heart attack"),                 # the term lost
    lambda rec: _swap_out(rec, 1, lambda o: o.get("entry") == "g_nstemi", "Heart attack (NSTEMI)"),        # a capital mid-line
    lambda rec: _swap_out(rec, 1, lambda o: o.get("entry") == "p_primary", "Other problems:"),             # another label's form
    lambda rec: _op(rec, 1, lambda o: o.get("entry") == "g_nstemi").update(entry="g_no_such_entry"),
    lambda rec: _op(rec, 1, lambda o: o.get("entry") == "g_nstemi").update(entry="g_stemi"),               # same plain words,
    lambda rec: (_swap_out(rec, 1, lambda o: o.get("entry") == "p_primary", "Other problems:"),           # another term's entry
                 _op(rec, 1, lambda o: o.get("out") == "Other problems:").update(entry="p_secondary")),
    lambda rec: _op(rec, 1, lambda o: o.get("entry") == "g_nstemi").update(table="withheld", entry="w_grace"),
    lambda rec: (_swap_out(rec, 1, lambda o: o.get("entry") == "g_nstemi", "you"),                         # a real entry on the
                 _op(rec, 1, lambda o: o.get("out") == "you").update(table="phrases", entry="p_patient")),  # wrong words
    lambda rec: _op(rec, 1, lambda o: o.get("entry") == "g_nstemi").update(out=None),
])
def test_a_substitution_that_is_not_an_approved_entrys_fixed_form_fails(tamper):
    assert _dx(DX, tamper) == ["sections.1:1:substitution_not_approved"]


@pytest.mark.parametrize("tamper", [
    lambda x: x["ops"].pop(1),                                       # a gap: one character belongs to no op
    lambda x: x["ops"].append(dict(x["ops"][1])),                    # the same characters twice
    lambda x: x["ops"][-1].update(b=x["ops"][-1]["b"] - 1),          # the line's end not covered
    lambda x: x["ops"][0].update(op="keep"),
    lambda x: x["ops"][0].update(a="0"),
    lambda x: x.update(ops=[]),
    lambda x: x.update(ops=["copy"]),
])
def test_ops_that_do_not_cover_the_line_exactly_once_fail(tamper):
    assert _dx(DX, lambda rec: tamper(_led(rec, "sections.1", 1))) == ["sections.1:1:ops_do_not_cover_the_line"]


@pytest.mark.parametrize("idx", [3, 99, -1, "4", True, [4]])
def test_a_c_line_index_that_is_wrong_or_claimed_twice_fails(idx):
    assert _dx(DX, lambda rec: _led(rec, "sections.1", 4).update(c_line=idx)) == ["sections.1:4:c_text_differs"]


def test_two_identical_items_cannot_share_one_c_line():
    """One Background item silently missing from C: its twin's C line would otherwise satisfy both."""
    def share(rec):
        x3, x4 = _led(rec, "sections.1", 3), _led(rec, "sections.1", 4)
        _c(rec, lambda ls: ls.pop(x4["c_line"]))
        x4["c_line"] = x3["c_line"]
    text = "Primary: NSTEMI\nBackground:\n- Asthma\n- Asthma"
    assert _dx(text) == [] and _dx(text, share) == ["sections.1:4:c_text_differs"]


@pytest.mark.parametrize("text, written", [
    ("NSTEMI  with\tanaemia", "NSTEMI with anaemia"),                              # white space collapsed
    ("NSTEMI , anaemia ; asthma : mild .", "NSTEMI, anaemia; asthma: mild."),      # no space before , ; : .
    ("NSTEMI (mild )", "NSTEMI (mild)"),                                           # ... or before )
    ("NSTEMI ( mild)", "NSTEMI (mild)"),                                           # no space after (
    ("  NSTEMI  ", "NSTEMI"),
])
def test_the_own_writing_out_rules_one_by_one(text, written):
    ops = [{"op": "copy", "a": 0, "b": len(text)}]
    assert GS._own_written("", text, ops) == written == RP.reproduce(text, ops)
    assert GS._own_written("- ", text, ops) == "- " + written
    assert GS._own_written("- ", text, [{"op": "drop", "a": 0, "b": len(text)}]) is None


def test_the_evaluators_own_writing_out_agrees_with_the_renderer_on_every_invented_case():
    """No false STOP: on every renderable case of the step-3 fixture (forms, paired controls, hard cases — all
    invented) the evaluator's own partition, substitution approval and written-out line agree with the frozen
    renderer, line by line, in every section."""
    tables = RP.default_tables()
    d = json.loads((ROOT / "tests" / "fixtures" / "render_tables_approved_cases.json").read_text(encoding="utf-8"))
    lines = 0
    for kind in ("forms", "paired", "hard"):
        for case in d[kind]:
            audience = case.get("audience", "adult")
            secs = [] if case["heading"] == "presenting_complaint" else [
                {"section_id": "sections.0", "heading": "presenting_complaint", "text": "Admitted with chest pain."}]
            inp = {"audience": audience, "sections": secs + [
                {"section_id": "sections.1", "heading": case["heading"], "text": case["text"]}]}
            try:
                out = RP.render_patient(inp, tables)
            except RP.RenderRefusal:
                continue
            for r in GS._rows(inp, out, tables):
                u, ops = r["u"], r["ops"]
                assert GS._own_partition(u["text"], ops)
                assert all(GS._own_sub_ok(u["text"], o, audience, tables) for o in ops if o["op"] == "sub")
                assert GS._own_written(u["marker"], u["text"], ops) == r["c_line"]
                lines += 1
    assert lines > 2000


def test_the_go_world_has_no_diagnosis_problem_and_every_line_is_checked(go_world):
    tables, c = RP.default_tables(), go_world.candidate()
    assert all(GS.diagnosis_problems(rec, tables) == [] for rec in c.records) and len(c.records) == 18


@pytest.mark.parametrize("broken, text, tamper_line", [
    ("own_withheld_coverage", "Primary: NSTEMI\nBackground:\n- T2DM", 3),
    ("_content_left", "Primary: NSTEMI\nBackground:\n- T2DM", 3),
])
def test_each_part_of_the_diagnosis_check_matters(monkeypatch, broken, text, tamper_line):
    def empty(rec):
        x = _led(rec, "sections.1", tamper_line)
        x["ops"] = [{"op": "drop", "a": 0, "b": 4, "rule": "withheld", "entry": "w_grace", "unit": "line"}]
    assert len(_dx(text, empty)) == 3
    monkeypatch.setattr(GS, broken, lambda *a, **k: True)
    assert len(_dx(text, empty)) == 2


def test_the_evaluators_own_coverage_check():
    t = RP.default_tables()
    text = "NSTEMI (GRACE score 142)"
    assert GS.own_withheld_coverage(text, 6, len(text), t)
    assert not GS.own_withheld_coverage(text, 0, len(text), t)          # the diagnosis word is not withheld wording
    assert not GS.own_withheld_coverage("NSTEMI and asthma", 6, 17, t)  # fillers alone are not a withheld unit... plus a word
    assert not GS.own_withheld_coverage(text, 6, 7, t)                  # no word at all


# --- the verdict rule ---------------------------------------------------------------------------------------

def test_only_the_measurement_source_prints_a_bare_verdict(capsys):
    rep = {"source": "w6-pipeline-e09-m01-c02-p04-x5 (MEASUREMENT)", "conditions": {}, "line_12": GS.LINE_12, "verdict": GS.GO}
    GS.print_report(rep)
    assert capsys.readouterr().out.rstrip().splitlines()[-1] == "GO"
    GS.print_report(rep | {"source": "x (NOT the measurement batch — fixture)"})
    assert capsys.readouterr().out.rstrip().splitlines()[-1] == "GO (on a fixture — NOT the measurement)"


def test_stop_wins_over_undecided_and_incomplete_is_never_go():
    c = lambda *s: {str(i): {"status": x} for i, x in enumerate(s)}      # noqa: E731
    assert GS.verdict_of(c(GS.MET, GS.MET)) == GS.GO
    assert GS.verdict_of(c(GS.MET, GS.UNDECIDED)) == GS.INCOMPLETE
    assert GS.verdict_of(c(GS.UNDECIDED, GS.NOT_MET, GS.MET)) == GS.STOP
    assert GS.EXIT == {GS.GO: 0, GS.STOP: 1, GS.REFUSED: 2, GS.INCOMPLETE: 3}
