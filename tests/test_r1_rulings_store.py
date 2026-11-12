"""Item 3 R1 step 4 — the fresh rulings store builder (evals/r1_rulings_store.py). Invented fixture
batch and an invented historical folder in a temporary directory; the real historical store is only
read (and shown to be byte-unchanged)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import r1_fixture as FX  # noqa: E402
import r1_rulings_store as RS  # noqa: E402


def _tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(d).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    root = tmp_path_factory.mktemp("r1-store")
    return FX.build_world(root / "w", *FX.make_source(root / "s"), until="render")


def _hist(tmp_path) -> Path:
    h = tmp_path / "hist"
    h.mkdir()
    (h / "S1.json").write_text(json.dumps({
        "scenario_id": "S1", "rulings": {"a": "acceptable"}, "med_rulings": {"b": "defect"},
        "compose_rulings": {"c": "acceptable"}, "compose_rulings_meta": {"by": "x"},
        "patient_rulings": {"d": "defect"}, "patient_rulings_meta": {"by": "x"}, "rubric_rulings": {"e": 1}}))
    (h / "S2.json").write_text(json.dumps({"scenario_id": "S2", "med_rulings": {"b": "acceptable"}}))
    return h


def test_build_copies_the_three_families_unchanged_and_nothing_else(rendered, tmp_path):
    hist = _hist(tmp_path)
    before = _tree_hash(hist)
    meta = RS.build(tmp_path / "store", rendered.batch, historical=hist)
    s1 = json.loads((tmp_path / "store" / "S1.json").read_text())
    assert s1 == {"scenario_id": "S1", "rulings": {"a": "acceptable"}, "med_rulings": {"b": "defect"},
                  "compose_rulings": {"c": "acceptable"}, "patient_rulings": {}}
    assert json.loads((tmp_path / "store" / "S2.json").read_text()) == {
        "scenario_id": "S2", "med_rulings": {"b": "acceptable"}, "patient_rulings": {}}
    assert json.loads((tmp_path / "store" / "S3.json").read_text()) == {"scenario_id": "S3", "patient_rulings": {}}
    assert meta["scenarios"]["S1"] == {
        "source_sha256": hashlib.sha256((hist / "S1.json").read_bytes()).hexdigest(),
        "families_copied": ["rulings", "med_rulings", "compose_rulings"],
        "not_copied": ["compose_rulings_meta", "patient_rulings", "patient_rulings_meta", "rubric_rulings"]}
    assert meta["scenarios"]["S3"] == {"source_sha256": None, "families_copied": [], "not_copied": []}
    assert len(meta["scenarios"]) == 18 and meta["note"] == RS.NOTE
    man = json.loads((rendered.batch / "manifest.json").read_text())
    assert meta["batch_id"] == FX.BATCH_ID and meta["freeze_sha256"] == man["derived_from"]["freeze_sha256"]
    assert meta["manifest_sha256"] == hashlib.sha256((rendered.batch / "manifest.json").read_bytes()).hexdigest()
    assert _tree_hash(hist) == before and RS.check(tmp_path / "store", historical=hist) == []


def test_the_real_historical_store_is_never_written(rendered, tmp_path):
    before = _tree_hash(RS.HISTORICAL)
    RS.build(tmp_path / "store", rendered.batch)
    assert RS.check(tmp_path / "store") == [] and _tree_hash(RS.HISTORICAL) == before
    for p in (tmp_path / "store").glob("S[0-9]*.json"):
        d = json.loads(p.read_text())
        hist = json.loads((RS.HISTORICAL / p.name).read_text())
        assert d["patient_rulings"] == {} and set(d) <= RS.ALLOWED_KEYS
        assert all(d[f] == hist[f] for f in RS.FAMILIES if f in hist)
        assert "patient_rulings_meta" not in d and "rubric_rulings" not in d


def test_build_refuses_to_write_over_or_into_the_historical_folder(rendered, tmp_path):
    hist = _hist(tmp_path)
    RS.build(tmp_path / "store", rendered.batch, historical=hist)
    for out, msg in ((tmp_path / "store", "already exists"), (hist, "already exists"),
                     (hist / "new", "historical folder")):
        with pytest.raises(RS.StoreError, match=msg):
            RS.build(out, rendered.batch, historical=hist)
    with pytest.raises(RS.StoreError, match="not a renderer batch"):
        RS.build(tmp_path / "s2", rendered.src, historical=hist)
    with pytest.raises(RS.StoreError, match="not a readable batch"):
        RS.build(tmp_path / "s3", tmp_path, historical=hist)
    assert not (hist / "new").exists() and not (tmp_path / "s2").exists()


def _edit(p: Path, fn):
    d = json.loads(p.read_text())
    fn(d)
    p.write_text(json.dumps(d))


@pytest.mark.parametrize("change, problem", [
    (lambda s, h: _edit(s / "S1.json", lambda d: d["rulings"].update(a="defect")), "S1:family_differs:rulings"),
    (lambda s, h: _edit(s / "S1.json", lambda d: d.pop("med_rulings")), "S1:family_differs:med_rulings"),
    (lambda s, h: _edit(s / "S2.json", lambda d: d.update(rulings={})), "S2:family_differs:rulings"),
    (lambda s, h: _edit(s / "S3.json", lambda d: d.update(compose_rulings={"x": "acceptable"})), "S3:historical_source"),
    (lambda s, h: _edit(s / "S1.json", lambda d: d.update(rubric_rulings={})), "S1:key_not_allowed:rubric_rulings"),
    (lambda s, h: _edit(s / "S1.json", lambda d: d.update(patient_rulings={"k": "fine"})), "S1:ruling_value:k"),
    (lambda s, h: _edit(s / "S1.json", lambda d: d.update(patient_rulings=[])), "S1:patient_rulings"),
    (lambda s, h: _edit(s / "S1.json", lambda d: d.update(scenario_id="S2")), "S1:scenario_id"),
    (lambda s, h: (s / "S1.json").unlink(), "S1:file_missing"),
    (lambda s, h: (s / "S99.json").write_text("{}"), "S99:file_not_in_STORE"),
    (lambda s, h: _edit(h / "S1.json", lambda d: d["rulings"].update(z="defect")), "S1:historical_changed"),
    (lambda s, h: (h / "S1.json").unlink(), "S1:historical_missing"),
    (lambda s, h: (h / "S3.json").write_text(json.dumps({"scenario_id": "S3"})), "S3:historical_source"),
])
def test_check_names_each_problem(rendered, tmp_path, change, problem):
    hist = _hist(tmp_path)
    store = tmp_path / "store"
    RS.build(store, rendered.batch, historical=hist)
    change(store, hist)
    assert RS.check(store, historical=hist) == [problem]


def test_rulings_and_their_meta_are_allowed_and_extra_files_are_ignored_by_name(rendered, tmp_path):
    hist = _hist(tmp_path)
    store = tmp_path / "store"
    RS.build(store, rendered.batch, historical=hist)
    _edit(store / "S1.json", lambda d: d.update(patient_rulings={"k": "acceptable", "j": "defect"},
                                                patient_rulings_meta={"by": "Shina (author/CSO)"}))
    (store / "RULINGS.json").write_text("{}")
    assert RS.check(store, historical=hist) == []


def test_command_line_prints_counts_only(rendered, tmp_path, capsys):
    assert RS.main(["build", "--out", str(tmp_path / "store"), "--batch", str(rendered.batch)]) == 0
    assert capsys.readouterr().out.startswith("store: 18 scenarios, 18 with upstream families copied")
    assert RS.main(["check", str(tmp_path / "store")]) == 0
    assert RS.main(["build", "--out", str(tmp_path / "store"), "--batch", str(rendered.batch)]) == 2
    (tmp_path / "store" / "STORE.json").unlink()
    assert RS.main(["check", str(tmp_path / "store")]) == 2


# --- scenario ids are file names: checked before any read or write (Codex 12 Nov 06:42 P2) -----------------------

def _with_ids(rendered, tmp_path, fn) -> Path:
    """A copy of the fixture batch's manifest and index, with its scenario ids changed by `fn`."""
    b = tmp_path / "batch"
    b.mkdir()
    (b / "manifest.json").write_bytes((rendered.batch / "manifest.json").read_bytes())
    index = json.loads((rendered.batch / "batch.json").read_text())
    fn(index["generations"])
    (b / "batch.json").write_text(json.dumps(index))
    return b


BAD_IDS = ["../hist/S1", "../S1", "..", ".", "hist/S1", "S1/../S2", "/tmp/S1", "\\tmp\\S1", "C:S1", "S1.json", "S1.",
           "", " S1", "S1 ", "S\x001", "STORE", "store", "RULINGS", "rulings", "-S1", "S" * 33, None, 7, ["S1"]]


@pytest.mark.parametrize("bad", BAD_IDS, ids=repr)
def test_an_unsafe_scenario_id_is_refused_before_anything_is_read_or_written(rendered, tmp_path, bad):
    """The demonstrated overwrite: with sibling `store` and `hist` folders, the id "../hist/S1" made the output
    write land on the historical S1.json. Now nothing is created and the historical folder is byte-unchanged."""
    hist = _hist(tmp_path)
    before = _tree_hash(hist)
    batch = _with_ids(rendered, tmp_path, lambda gens: gens[0].update(scenario_id=bad))
    with pytest.raises(RS.StoreError, match="not a plain name"):
        RS.build(tmp_path / "store", batch, historical=hist)
    assert not (tmp_path / "store").exists() and _tree_hash(hist) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["batch", "hist"]       # nothing else appeared beside them


def test_the_demonstrated_traversal_no_longer_replaces_the_historical_file(rendered, tmp_path):
    hist = _hist(tmp_path)
    s1 = (hist / "S1.json").read_bytes()
    batch = _with_ids(rendered, tmp_path, lambda gens: [g.update(scenario_id="../hist/S1") for g in gens
                                                         if g["scenario_id"] == "S1"])
    with pytest.raises(RS.StoreError):
        RS.build(tmp_path / "store", batch, historical=hist)
    assert (hist / "S1.json").read_bytes() == s1 and json.loads(s1)["patient_rulings"] == {"d": "defect"}


def test_an_absolute_scenario_id_writes_nowhere(rendered, tmp_path):
    hist = _hist(tmp_path)
    target = tmp_path / "elsewhere" / "S1"
    target.parent.mkdir()
    batch = _with_ids(rendered, tmp_path, lambda gens: gens[0].update(scenario_id=str(target)))
    with pytest.raises(RS.StoreError, match="not a plain name"):
        RS.build(tmp_path / "store", batch, historical=hist)
    assert list(target.parent.iterdir()) == [] and not (tmp_path / "store").exists()


def test_two_ids_that_differ_only_by_case_are_refused(rendered, tmp_path):
    hist = _hist(tmp_path)
    batch = _with_ids(rendered, tmp_path, lambda gens: gens[0].update(scenario_id="s2"))     # S2 is already there
    with pytest.raises(RS.StoreError, match="differ only by case"):
        RS.build(tmp_path / "store", batch, historical=hist)
    assert not (tmp_path / "store").exists()


def test_a_historical_file_that_resolves_outside_its_folder_is_refused(rendered, tmp_path):
    hist = _hist(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"scenario_id": "S3", "rulings": {"z": "defect"}}))
    (hist / "S3.json").symlink_to(outside)
    with pytest.raises(RS.StoreError, match="outside its folder"):
        RS.build(tmp_path / "store", rendered.batch, historical=hist)
    assert not (tmp_path / "store").exists()


def test_the_same_scenario_on_several_runs_is_one_store_file(rendered, tmp_path):
    """Legitimate repeats are kept: five runs of 18 scenarios give 18 store files."""
    hist = _hist(tmp_path)

    def five_runs(gens):
        gens.extend([dict(g, run=n) for n in range(2, 6) for g in list(gens[:18])])
    batch = _with_ids(rendered, tmp_path, five_runs)
    meta = RS.build(tmp_path / "store", batch, historical=hist)
    assert len(json.loads((batch / "batch.json").read_text())["generations"]) == 90
    assert len(meta["scenarios"]) == 18
    assert len(list((tmp_path / "store").glob("*.json"))) == 19 and RS.check(tmp_path / "store", historical=hist) == []


@pytest.mark.parametrize("sid", ["letters-and_digits-09", "S1", "a"])
def test_plain_names_are_accepted(sid):
    assert RS.safe_scenario_id(sid)


def test_check_refuses_a_store_record_that_names_an_unsafe_id(rendered, tmp_path):
    hist = _hist(tmp_path)
    RS.build(tmp_path / "store", rendered.batch, historical=hist)
    meta = json.loads((tmp_path / "store" / "STORE.json").read_text())
    meta["scenarios"]["../hist/S1"] = meta["scenarios"]["S1"]
    (tmp_path / "store" / "STORE.json").write_text(json.dumps(meta))
    assert RS.check(tmp_path / "store", historical=hist) == ["STORE.json:scenario_id"]
