"""evals/run_pipeline_eval.py — pipeline steps 1-5b on the corpus, scored by
score_batch.py like v1 (ADR-009 (e), W4 stretch). Fake Bedrock; no AWS."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import run_cold_eval as rce  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402
import score_batch as SB  # noqa: E402

QUIET = rce.dt.datetime(2026, 10, 7, 14, 0, tzinfo=rce.LONDON)
CORPUS = rce.load_corpus(rce.DEFAULT_CORPUS_PATH)[0]


def _run(tmp_path, argv):
    return rpe.main(argv, client_factory=rpe.DryRunBedrock, now=lambda: QUIET,
                    runs_dir=tmp_path, sleep=lambda _s: None)


def test_dry_run_writes_nothing(tmp_path):
    assert _run(tmp_path, ["--all", "--dry-run"]) == 0 and not any(tmp_path.iterdir())


def test_live_batch_records_every_step_but_never_the_line_index(tmp_path):
    assert _run(tmp_path, ["--all", "--batch", "p", "--yes"]) == 0
    recs = [json.loads(p.read_text()) for p in sorted((tmp_path / "p" / "r1").glob("*.json"))]
    assert len(recs) == 18
    for r in recs:
        assert r["generator"] == "pipeline" and r["error"] is None
        assert list(r["steps"]) == list(rpe.STEP_ORDER)
        assert "lines" not in r["steps"]["guard_input"]
        assert r["steps"]["guard_input"]["notes_sha256"] == r["notes_sha256"]
    # The notes are carried by notes_sha256. Step outputs may QUOTE cited lines (step 4's
    # rendered resus block, step 3's facts — <= 200 characters, ADR-009 (b)), but the
    # record never holds the notes as a whole or the line index.
    s1 = next(s for s in CORPUS if s.id == "S1")
    text = (tmp_path / "p" / "r1" / "S1.json").read_text()
    long_lines = [ln.strip() for ln in s1.notes.splitlines() if len(ln.strip()) >= 15]
    n_copied = sum(1 for ln in long_lines if ln in text)
    assert n_copied < len(long_lines) // 2


def test_a_pipeline_batch_scores_like_v1_and_says_d4_is_not_scored(tmp_path):
    _run(tmp_path, ["--all", "--batch", "p", "--yes"])
    assert SB.main([str(tmp_path / "p")]) == 0
    scored = json.loads((tmp_path / "p" / "scores" / "safety_net.json").read_text())
    assert scored["generator"] == "pipeline" and len(scored["results"]) == 18
    by = {r["scenario_id"]: r for r in scored["results"]}
    # the fake extracts nothing: every route is fallback — right on the 8 fall-back scenarios
    assert sum(r["route"]["correct"] for r in by.values()) == 8
    assert all(r["pinned"]["in_part_c"] for r in by.values())
    assert "Not scored" in (tmp_path / "p" / "scores" / "MEDS.md").read_text()


def test_refuses_an_existing_batch(tmp_path):
    (tmp_path / "p").mkdir()
    assert _run(tmp_path, ["S1", "--batch", "p", "--yes"]) == 2
