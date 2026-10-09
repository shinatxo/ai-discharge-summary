"""evals/make_blind_sheets.py — rubric v2 calibration sheets (W5).

No network. Scenario IDs only in test names and messages — never note text."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import gold as G  # noqa: E402
import make_blind_sheets as mbs  # noqa: E402
import run_cold_eval as rce  # noqa: E402

CORPUS, CORPUS_SHA = rce.load_corpus(rce.DEFAULT_CORPUS_PATH)
BY_ID = {s.id: s for s in CORPUS}
IDS = ("S1", "S3")
SENTINELS = ("SENTINEL_GATE_7f3a", "SENTINEL_COST_7f3a", "987.654321")
OUTPUT = ("PART A - CLINICIAN SUMMARY\n```\nDIAGNOSIS: test\n```\n"
          "PART B - GP LETTER\nDear Dr\n"
          "PART C - PATIENT VERSION\n````\nnested fence\n````\n")


def _record(sid, run=1, generator="v1", **over):
    rec = {"generation_id": f"t/r{run}/{sid}", "scenario_id": sid, "run": run,
           "generator": generator, "notes_sha256": BY_ID[sid].notes_sha256,
           "output": OUTPUT, "error": None,
           "gate": {"label": SENTINELS[0], "status": SENTINELS[0]},
           "cost_usd_est": float(SENTINELS[2]), "note": SENTINELS[1]}
    rec.update(over)
    return rec


def make_batch(tmp_path, records, corpus_sha=CORPUS_SHA):
    b = tmp_path / "batch"
    gens = []
    for rec in records:
        p = b / f"r{rec['run']}" / f"{rec['scenario_id']}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec), encoding="utf-8")
        gens.append({"run": rec["run"], "scenario_id": rec["scenario_id"],
                     "record": f"r{rec['run']}/{rec['scenario_id']}.json",
                     "gate_status": SENTINELS[0], "cost_usd_est": float(SENTINELS[2])})
    (b / "manifest.json").write_text(json.dumps(
        {"batch_id": "t", "generator": "v1", "corpus": {"sha256": corpus_sha}}), encoding="utf-8")
    (b / "batch.json").write_text(json.dumps({"batch_id": "t", "generations": gens}), encoding="utf-8")
    return b


def _all_written(out: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())


def test_builds_run_1_only(tmp_path):
    recs = [_record(s) for s in IDS] + [_record("S1", run=2)]
    out = mbs.build(make_batch(tmp_path, recs), 1, tmp_path / "out")
    m = json.loads((out / "MANIFEST.json").read_text())
    assert [i["scenario_id"] for i in m["items"]] == list(IDS)
    assert sorted(p.name for p in (out / "sheets").iterdir()) == ["S1.md", "S3.md"]
    assert m["rubric"]["sha256"] == mbs.rubric_sha256()
    assert m["items"][0]["output_sha256"] == rce.sha256_text(OUTPUT)


def test_sheets_are_blind_to_gate_cost_and_everything_else(tmp_path):
    out = mbs.build(make_batch(tmp_path, [_record(s) for s in IDS]), 1, tmp_path / "out")
    written = _all_written(out)
    for s in SENTINELS:
        assert s not in written


def test_sheet_carries_every_note_line_with_its_id_and_the_output_verbatim(tmp_path):
    out = mbs.build(make_batch(tmp_path, [_record("S3")]), 1, tmp_path / "out")
    sheet = (out / "sheets" / "S3.md").read_text()
    for lid, text in G.line_index(BY_ID["S3"].notes).items():
        assert f"{lid}  {text}" in sheet
    assert OUTPUT in sheet


def test_fence_is_longer_than_any_backtick_run():
    assert mbs._fence("a ```` b") == "`````"
    assert mbs._fence("plain") == "```"


def test_template_is_empty_and_has_the_five_dimensions(tmp_path):
    out = mbs.build(make_batch(tmp_path, [_record("S1")]), 1, tmp_path / "out")
    t = json.loads((out / "scores" / "S1.json").read_text())
    assert tuple(t["dimensions"]) == mbs.DIMENSIONS
    assert all(v == {"verdict": None, "auto_fail": False, "findings": []}
               for v in t["dimensions"].values())


def test_refuses_an_existing_folder(tmp_path):
    b = make_batch(tmp_path, [_record("S1")])
    (tmp_path / "out").mkdir()
    with pytest.raises(mbs.SheetError, match="refusing to overwrite"):
        mbs.build(b, 1, tmp_path / "out")


@pytest.mark.parametrize("bad, match", [
    ({"corpus_sha": "0" * 64}, "corpus has changed"),
    ({"notes_sha256": "0" * 64}, "notes_sha256"),
    ({"generator": "pipeline"}, "v1 records only"),
    ({"output": None}, "no output"),
])
def test_refuses_and_writes_nothing(tmp_path, bad, match):
    corpus_sha = bad.pop("corpus_sha", CORPUS_SHA)
    b = make_batch(tmp_path, [_record("S1", **bad)], corpus_sha=corpus_sha)
    with pytest.raises(mbs.SheetError, match=match):
        mbs.build(b, 1, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_rubric_section_is_section_2_only():
    sec = mbs.rubric_section()
    assert sec.startswith("## 2. Scoring rubric")
    assert "\n## 3. " not in sec
    assert "**D8 — Appropriate withholding" in sec


def _fill(out, sid, **dims):
    p = out / "scores" / f"{sid}.json"
    t = json.loads(p.read_text())
    for d in mbs.DIMENSIONS:
        t["dimensions"][d]["verdict"] = "pass"
    for d, v in dims.items():
        t["dimensions"][d].update(v)
    t["scored_at"] = "2026-10-10"
    p.write_text(json.dumps(t))


def test_check_unfilled_then_filled(tmp_path):
    out = mbs.build(make_batch(tmp_path, [_record("S1")]), 1, tmp_path / "out")
    complete, problems = mbs.check(out)
    assert complete == 0 and any("no verdict yet" in p for p in problems)
    _fill(out, "S1", D8={"verdict": "na"},
          D7={"verdict": "fail", "auto_fail": True,
              "findings": [{"output_quote": "x", "note_cites": ["L001"], "reason": "y"}]})
    assert mbs.check(out) == (1, [])


@pytest.mark.parametrize("dims, match", [
    ({"D2": {"verdict": "na"}}, "na is not allowed for D2"),
    ({"D3": {"verdict": "pass", "auto_fail": True}}, "auto_fail needs verdict fail"),
    ({"D6": {"verdict": "maybe"}}, "verdict must be one of"),
    ({"D2": {"verdict": "fail", "findings": [{"note_cites": ["L999"]}]}}, "unknown line ID"),
])
def test_check_catches_bad_scores(tmp_path, dims, match):
    out = mbs.build(make_batch(tmp_path, [_record("S1")]), 1, tmp_path / "out")
    _fill(out, "S1", **dims)
    complete, problems = mbs.check(out)
    assert complete == 0 and any(match in p for p in problems)


def test_console_never_prints_note_text(tmp_path, capsys):
    b = make_batch(tmp_path, [_record(s) for s in IDS])
    assert mbs.main([str(b), "--run", "1", "--out", str(tmp_path / "out")]) == 0
    assert mbs.main(["--check", str(tmp_path / "out")]) == 1
    printed = capsys.readouterr()
    text = printed.out + printed.err
    for sid in IDS:
        for ln in BY_ID[sid].notes.splitlines():
            if len(ln.strip()) >= 15:
                assert ln.strip() not in text
