"""evals/make_compose_sheets.py — the step 6 blind-read sheets (CSO decision 4C).
Built from a dry-run batch; no AWS."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import make_compose_sheets as MS  # noqa: E402
import run_cold_eval as rce  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402

QUIET = rce.dt.datetime(2026, 10, 7, 14, 0, tzinfo=rce.LONDON)


def _batch(tmp_path):
    rpe.main(["--all", "--batch", "p", "--yes"], client_factory=rpe.DryRunBedrock, now=lambda: QUIET,
             runs_dir=tmp_path, sleep=lambda _s: None)
    return tmp_path / "p"


def test_sheets_are_blind_and_show_input_and_output_only(tmp_path):
    out = MS.build(_batch(tmp_path), 1, tmp_path / "sheets")
    assert sorted(p.stem for p in (out / "sheets").glob("*.md")) == sorted(MS.ITEMS)
    corpus = {s.id: s for s in rce.load_corpus(rce.DEFAULT_CORPUS_PATH)[0]}
    for sid in MS.ITEMS:
        text = (out / "sheets" / f"{sid}.md").read_text()
        assert "## Facts given to step 6" in text and "[filled by code: medications]" in text
        assert "{{" not in text
        for word in ("checks", "year_added", "box_not_exact", "MET", "counts"):
            assert word not in text
        long_lines = [ln.strip() for ln in corpus[sid].notes.splitlines() if len(ln.strip()) >= 15]
        assert not any(ln in text for ln in long_lines)                   # no notes
    man = json.loads((out / "MANIFEST.json").read_text())
    assert [i["scenario_id"] for i in man["items"]] == list(MS.ITEMS)


def test_refuses_an_existing_folder(tmp_path):
    b = _batch(tmp_path)
    MS.build(b, 1, tmp_path / "s")
    try:
        MS.build(b, 1, tmp_path / "s")
        raise AssertionError("overwrote")
    except MS.SheetError:
        pass


def test_check_validates_verdicts_and_findings(tmp_path):
    out = MS.build(_batch(tmp_path), 1, tmp_path / "s")
    assert MS.check(out) == (0, [])
    p = out / "scores" / "S1.json"
    d = json.loads(p.read_text())
    d["verdicts"] = {"D2": "pass", "D6": "fail", "D7": "pass", "omission": "ok"}
    p.write_text(json.dumps(d))
    done, problems = MS.check(out)
    assert done == 0 and any("omission" in x for x in problems) and any("D6 is fail" in x for x in problems)
    d["verdicts"]["omission"] = "pass"
    d["findings"] = [{"dimension": "D6", "part": "part_a", "heading": "diagnosis", "fact_ids": [], "note": "x"}]
    p.write_text(json.dumps(d))
    assert MS.check(out) == (1, [])
