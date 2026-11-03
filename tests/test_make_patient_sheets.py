"""evals/make_patient_sheets.py — step 7 blind-read sheets (CSO D5, 1 Nov 2026):
built on a dry-run batch; blind by construction; the 8 fixed items; --check."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import make_patient_sheets as MS  # noqa: E402
import run_cold_eval as rce  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402

QUIET = dt.datetime(2026, 11, 3, 14, 0, tzinfo=rce.LONDON)


@pytest.fixture()
def batch(tmp_path):
    rpe.main(["--all", "--batch", "p", "--yes"], client_factory=rpe.DryRunBedrock, now=lambda: QUIET,
             runs_dir=tmp_path, sleep=lambda _s: None)
    return tmp_path / "p"


def test_items_are_the_fixed_eight():
    assert MS.ITEMS == ("S1", "S4", "S10", "S13", "S16", "A5", "S3", "C7")
    assert MS.DIMENSIONS == ("added", "meaning", "withheld", "omission", "audience")   # v2, F4
    assert MS.SHEETS_VERSION == 3          # v3 (3 Nov): box content shown; dimensions as v2


def test_v2_readme_defines_omission_with_the_by_design_exclusions():
    r = MS.README
    assert "**omission:**" in r and "all five are `pass`" in r
    for x in ("risk scores", "ceilings", "risk_assessment", "allergies", "the two boxes"):
        assert x in r.split("**omission:**", 1)[1]
    assert "Judge disclosure\n  only here" in r


V1 = ROOT / "evals" / "calibration" / "w6-pipeline-e09-m01-c02-p01-x5-patient-r1"


@pytest.mark.skipif(not V1.exists(), reason="saved calibration folder not present")
def test_a_v1_folder_still_checks_as_v1():
    # piece 3's blind read (2 Nov, 0/8): four dimensions in its MANIFEST
    assert MS.check(V1) == (8, 0, [])


def test_build_writes_blind_sheets(batch, tmp_path):
    out = MS.build(batch, 1, tmp_path / "sheets")
    assert json.loads((out / "MANIFEST.json").read_text())["dimensions"] == list(MS.DIMENSIONS)
    assert sorted(p.stem for p in (out / "sheets").glob("*.md")) == sorted(MS.ITEMS)
    sheet = (out / "sheets" / "C7.md").read_text()
    assert "## What step 7 was given" in sheet and "audience: adult" in sheet
    assert "FOR TRANSLATION" in sheet                      # the assembled leaflet, header by code
    assert "[the medicines box" in sheet and "checks" not in sheet.lower()
    man = json.loads((out / "MANIFEST.json").read_text())
    assert [i["scenario_id"] for i in man["items"]] == list(MS.ITEMS)
    assert "notes" in man["excluded"] and man["pass_rule"].startswith("an item passes")
    with pytest.raises(MS.SheetError):
        MS.build(batch, 1, out)


def test_check_counts_scored_and_passing(batch, tmp_path):
    out = MS.build(batch, 1, tmp_path / "sheets")
    for i, sid in enumerate(MS.ITEMS):
        p = out / "scores" / f"{sid}.json"
        d = json.loads(p.read_text())
        d["verdicts"] = {k: "pass" for k in MS.DIMENSIONS}
        if i == 0:
            d["verdicts"]["meaning"] = "partial"
            d["findings"] = [{"dimension": "meaning", "heading": "next_steps", "source_ids": [], "note": "x"}]
        p.write_text(json.dumps(d))
    assert MS.check(out) == (8, 7, [])
    p = out / "scores" / "S4.json"
    d = json.loads(p.read_text())
    d["verdicts"]["added"] = "fail"
    p.write_text(json.dumps(d))
    done, passed, problems = MS.check(out)
    assert (done, passed) == (8, 6) and problems == ["S4.json: added is fail but has no finding"]


def test_missing_step_7_has_no_substitute(batch, tmp_path):
    p = batch / "r1" / "S3.json"
    rec = json.loads(p.read_text())
    del rec["steps"]["compose_patient"]
    p.write_text(json.dumps(rec))
    with pytest.raises(MS.SheetError):
        MS.build(batch, 1, tmp_path / "sheets")


def test_v3_sheets_show_each_boxs_content_as_in_the_leaflet(batch, tmp_path):
    out = MS.build(batch, 1, tmp_path / "sheets")
    sheet = (out / "sheets" / "S1.md").read_text()
    wrote = sheet.split("## What step 7 wrote", 1)[1].split("## The leaflet as assembled", 1)[0]
    for h in ("medicines", "safety_net"):
        assert f"[end of the {h} box]" in wrote
    assert wrote.count("in the leaflet, not step 7's; not an omission") == 2
    assert json.loads((out / "MANIFEST.json").read_text())["box_content_shown"] is True
    assert "it IS in\nthe leaflet, so it is never an omission" in MS.README
