"""Item 3 R1 step 4 — the freeze record (evals/r1_freeze.py) and the harness's freeze guard
(evals/render_offline.py; decision D6 (a), Shina 11 Nov 2026). Invented fixture batches only."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import r1_fixture as FX  # noqa: E402
import r1_freeze as FZ  # noqa: E402
import render_offline as H  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    return FX.make_source(tmp_path_factory.mktemp("r1-fz"))


@pytest.fixture
def frozen(tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text("{}")
    path = tmp_path / "FREEZE.json"
    sha = FZ.write(path, FZ.make(cases))
    return path, sha, cases


# --- the record ---------------------------------------------------------------------------------------

def test_the_pinned_measurement_inputs_file_is_the_90(tmp_path):
    d = json.loads((ROOT / FZ.MEASUREMENT_INPUTS).read_text())
    assert FZ.sha_file(ROOT / FZ.MEASUREMENT_INPUTS) == FZ.MEASUREMENT_INPUTS_SHA256
    assert d["source_batch"] == FZ.MEASUREMENT_BATCH == H.MEASUREMENT_BATCH and len(d["generations"]) == 90
    assert len(H.measurement_record_hashes()) == 90
    assert all(g["input_sha256"] and set(g) == {"record", "record_sha256", "input_sha256", "reason"}
               for g in d["generations"])                       # hashes only — no text


def test_a_fresh_record_matches_and_pins_what_the_decision_depends_on(frozen):
    path, sha, cases = frozen
    got_sha, rec, problems = FZ.check(path)
    assert (got_sha, problems) == (sha, []) and sha == FZ.sha_file(path)
    for needed in ("evals/r1_go_stop.py", "evals/render_offline.py", "evals/r1_freeze.py", "evals/r1_locate_cases.py",
                   "evals/r1_rulings_store.py", "evals/score_batch.py", "evals/score_patient.py",
                   "evals/make_patient_sheets.py", "evals/reading_age.py", "evals/gold/S1.json",
                   "evals/gold/adjudications/S1.json", "evals/r1/inputs.json", "tests/test_r1_go_stop.py",
                   "tests/r1_fixture.py", "evals/scenarios/discharge-eval-scenarios.md"):
        assert needed in rec["files"], needed
    assert rec["renderer"]["tables"] == RP.TABLE_SHA256 and rec["renderer"]["tables_status"] == "approved"
    assert rec["renderer"]["module_sha256"] == RP.module_sha256()
    assert rec["measurement"]["inputs_sha256"] == FZ.MEASUREMENT_INPUTS_SHA256 and rec["measurement"]["records"] == 90
    assert rec["cases"] == {"path": str(cases), "sha256": FZ.sha_file(cases)}
    assert rec["renderer"] == {k if k != "name" else "renderer": v
                               for k, v in H._renderer_identity(RP.default_tables()).items()}
    with pytest.raises(FZ.FreezeError, match="never written over"):
        FZ.write(path, rec)


def test_check_names_every_kind_of_drift(frozen, monkeypatch):
    path, _sha, cases = frozen
    pins = FZ.file_pins()
    cases.write_text('{"changed": true}')
    assert FZ.check(path)[2] == ["cases"]
    cases.write_text("{}")
    monkeypatch.setattr(FZ, "file_pins", lambda root=ROOT: {k: v for k, v in pins.items() if k != "evals/gold/S1.json"}
                        | {"evals/score_patient.py": "0" * 64, "evals/new_tool.py": "1" * 64})
    assert FZ.check(path)[2] == ["file_changed:evals/score_patient.py", "file_missing:evals/gold/S1.json",
                                 "file_not_pinned:evals/new_tool.py"]
    monkeypatch.setattr(FZ, "file_pins", lambda root=ROOT: pins)
    monkeypatch.setattr(FZ, "identity", lambda: {"pipeline_version": "0" * 64, "renderer": {"version": 3}})
    assert FZ.check(path)[2] == ["pipeline_version", "renderer"]


def test_check_refuses_an_unreadable_record_and_an_unknown_version(tmp_path, frozen):
    with pytest.raises(FZ.FreezeError, match="not readable"):
        FZ.check(tmp_path / "missing.json")
    path, _sha, _cases = frozen
    other = tmp_path / "v2.json"
    other.write_text(FZ.dump(json.loads(path.read_text()) | {"freeze_version": 2}))
    assert FZ.check(other)[2] == ["freeze_version"]
    with pytest.raises(FZ.FreezeError, match="case list does not exist"):
        FZ.make(tmp_path / "no-cases.json")


def test_a_changed_table_on_disk_is_a_problem_not_a_crash(frozen, monkeypatch, tmp_path):
    path, _sha, _cases = frozen
    alt = tmp_path / "tables"
    shutil.copytree(RP.TABLES_DIR, alt)
    (alt / "glossary.json").write_text((alt / "glossary.json").read_text() + "\n")
    monkeypatch.setattr(RP, "TABLES_DIR", alt)
    assert FZ.check(path)[2][0] == "tables:tables_changed"
    with pytest.raises(RP.RenderRefusal):
        FZ.make(frozen[2])


def test_the_measurement_case_list_is_pinned_by_its_repository_path(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evals" / "r1").mkdir(parents=True)
    shutil.copy(ROOT / FZ.MEASUREMENT_INPUTS, root / FZ.MEASUREMENT_INPUTS)
    (root / FZ.MEASUREMENT_CASES).write_text("{}")
    rec = FZ.make(root / FZ.MEASUREMENT_CASES, root=root)
    assert rec["cases"]["path"] == FZ.MEASUREMENT_CASES and list(rec["files"]) == [FZ.MEASUREMENT_INPUTS]
    assert FZ.cases_path(rec, root) == (root / FZ.MEASUREMENT_CASES).resolve()
    (root / FZ.MEASUREMENT_INPUTS).write_text("{}")
    with pytest.raises(FZ.FreezeError, match="not the pinned one"):
        FZ.make(root / FZ.MEASUREMENT_CASES, root=root)


# --- the harness guard -----------------------------------------------------------------------------------

def _pinned(src_dir: Path, tmp_path: Path, n: int = 1) -> tuple[Path, str]:
    """A stand-in for the pinned inputs file that lists n of the fixture's records as 'measurement' records."""
    d = H.make_inputs(src_dir)
    d["generations"] = d["generations"][:n]
    p = tmp_path / "pinned-inputs.json"
    p.write_text(H._dump(d))
    return p, FZ.sha_file(p)


def test_a_fixture_batch_renders_with_or_without_a_freeze_and_records_its_hash(src, frozen, tmp_path):
    w_src, inputs = src
    path, sha, _cases = frozen
    H.render_batch(w_src, tmp_path / "a", inputs, "fx-a", freeze=path)
    H.render_batch(w_src, tmp_path / "b", inputs, "fx-b")
    for out, want in (("a", sha), ("b", None)):
        man = json.loads((tmp_path / out / "manifest.json").read_text())["derived_from"]
        rec = json.loads((tmp_path / out / "r1" / "S1.json").read_text())
        assert man["freeze_sha256"] == want and rec["render_provenance"]["freeze_sha256"] == want
        assert man["measurement"] is False and man["harness_version"] == 2


def test_a_freeze_that_does_not_match_the_disk_refuses_the_render(src, frozen, tmp_path):
    w_src, inputs = src
    path, _sha, cases = frozen
    cases.write_text('{"changed": true}')
    with pytest.raises(H.HarnessError, match=r"freeze record does not match the files on disk \(1 problem\(s\), first: cases\)"):
        H.render_batch(w_src, tmp_path / "out", inputs, "fx", freeze=path)
    with pytest.raises(H.HarnessError, match="freeze record not readable"):
        H.render_batch(w_src, tmp_path / "out", inputs, "fx", freeze=tmp_path / "missing.json")
    assert not (tmp_path / "out").exists()


def test_a_measurement_record_is_recognised_by_its_hash_whatever_the_batch_is_called(src, frozen, tmp_path):
    """One of the 'measurement' records inside a renamed batch is still the measurement (D6 (a))."""
    w_src, inputs = src
    pinned = _pinned(w_src, tmp_path)
    assert w_src.name != H.MEASUREMENT_BATCH
    with pytest.raises(H.HarnessError, match="only with a freeze record"):
        H.render_batch(w_src, tmp_path / "out", inputs, "fx", measurement_inputs=pinned)
    with pytest.raises(H.HarnessError, match="--dev is refused on the measurement batch"):
        H.render_batch(w_src, tmp_path / "out", inputs, "fx", dev=True, measurement_inputs=pinned)
    with pytest.raises(H.HarnessError, match="only with the package's own tables"):
        H.render_batch(w_src, tmp_path / "out", inputs, "fx", freeze=frozen[0], tables=RP.default_tables(),
                       measurement_inputs=pinned)
    # even with a matching freeze record, a batch that is not the pinned 90 can never pass as the measurement
    with pytest.raises(H.HarnessError, match="inputs file is not the one the freeze record pins"):
        H.render_batch(w_src, tmp_path / "out", inputs, "fx", freeze=frozen[0], measurement_inputs=pinned)
    assert not (tmp_path / "out").exists()
    H.render_batch(w_src, tmp_path / "ok", inputs, "fx")        # not in the real pinned set: an ordinary fixture


def test_the_batch_id_and_the_folder_name_still_mark_the_measurement(tmp_path, frozen):
    src_dir, inputs = FX.make_source(tmp_path / "s")
    named = tmp_path / H.MEASUREMENT_BATCH
    shutil.copytree(src_dir, named)
    with pytest.raises(H.HarnessError, match="only with a freeze record"):
        H.render_batch(named, tmp_path / "out", inputs, "fx")
    man = json.loads((src_dir / "manifest.json").read_text())
    (src_dir / "manifest.json").write_text(json.dumps(man | {"batch_id": H.MEASUREMENT_BATCH}))
    with pytest.raises(H.HarnessError, match="only with a freeze record"):
        H.render_batch(src_dir, tmp_path / "out", inputs, "fx")


def test_a_missing_or_changed_pinned_inputs_file_refuses_every_render(src, tmp_path):
    w_src, inputs = src
    pinned, sha = _pinned(w_src, tmp_path)
    with pytest.raises(H.HarnessError, match="pinned measurement inputs file has changed — every render is refused"):
        H.render_batch(w_src, tmp_path / "out", inputs, "fx", measurement_inputs=(pinned, "0" * 64))
    with pytest.raises(H.HarnessError, match="pinned measurement inputs file is missing — every render is refused"):
        H.render_batch(w_src, tmp_path / "out", inputs, "fx", measurement_inputs=(tmp_path / "gone.json", sha))
    assert not (tmp_path / "out").exists()


def test_the_real_measurement_path_passes_only_with_the_pinned_inputs(src, frozen, tmp_path, monkeypatch):
    """The positive path of the guard, shown by standing a fixture in for the 90: with the fixture's own inputs
    file as the pinned one, a freeze made for it, and no --dev, the 'measurement' renders and is marked."""
    w_src, inputs = src
    monkeypatch.setattr(FZ, "MEASUREMENT_INPUTS", str(inputs))
    monkeypatch.setattr(FZ, "MEASUREMENT_INPUTS_SHA256", FZ.sha_file(inputs))
    cases = tmp_path / "c.json"
    cases.write_text("{}")
    path = tmp_path / "F.json"
    sha = FZ.write(path, FZ.make(cases))
    s = H.render_batch(w_src, tmp_path / "out", inputs, "fx-measure", freeze=path)
    man = json.loads((tmp_path / "out" / "manifest.json").read_text())["derived_from"]
    assert s["records"] == 18 and man["measurement"] is True and man["freeze_sha256"] == sha and man["dev"] is False


def test_command_line_takes_the_freeze_record(src, frozen, tmp_path, capsys):
    w_src, inputs = src
    argv = ["render", str(w_src), "--out", str(tmp_path / "out"), "--inputs", str(inputs), "--batch-id", "fx",
            "--freeze", str(frozen[0])]
    assert H.main(argv) == 0
    assert json.loads((tmp_path / "out" / "manifest.json").read_text())["derived_from"]["freeze_sha256"] == frozen[1]
    frozen[2].write_text("[]")
    assert H.main(argv[:3] + [str(tmp_path / "out2")] + argv[4:]) == 2
    assert "freeze record does not match" in capsys.readouterr().err
