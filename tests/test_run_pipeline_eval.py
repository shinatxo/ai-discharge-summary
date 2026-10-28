"""evals/run_pipeline_eval.py — pipeline steps 1-5b on the corpus, scored by
score_batch.py like v1 (ADR-009 (e), W4 stretch). Fake Bedrock; no AWS."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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


def test_a_pipeline_batch_scores_safety_net_d4_and_step_2(tmp_path):
    _run(tmp_path, ["--all", "--batch", "p", "--yes"])
    assert SB.main([str(tmp_path / "p")]) == 0
    out = tmp_path / "p" / "scores"
    scored = json.loads((out / "safety_net.json").read_text())
    assert scored["generator"] == "pipeline" and len(scored["results"]) == 18
    assert "step2_advice" not in scored                       # it has its own files
    by = {r["scenario_id"]: r for r in scored["results"]}
    # the fake extracts nothing: every route is fallback — right on the 8 fall-back scenarios
    assert sum(r["route"]["correct"] for r in by.values()) == 8
    assert all(r["pinned"]["in_part_c"] for r in by.values())
    # D4 is scored on 5a's (empty) list: the 5 scenarios with no gold drug pass
    meds = json.loads((out / "meds.json").read_text())
    assert len(meds["results"]) == 18 and meds["skipped"] == []
    assert sum(r["verdict"] == "pass" for r in meds["results"]) == 5
    s2 = json.loads((out / "step2_advice.json").read_text())
    assert s2["summary"]["n_scored"] == 18 and s2["summary"]["route_errors"]["not_extracted"] == 10
    assert "Step 2 documented advice" in (out / "STEP2_ADVICE.md").read_text()


def test_both_model_steps_are_recorded_with_their_own_prompts(tmp_path):
    _run(tmp_path, ["S1", "--batch", "p", "--yes"])
    man = json.loads((tmp_path / "p" / "manifest.json").read_text())
    assert man["path"] == "steps-1-5b-5a-6"
    assert set(man["prompts"]) == {"extract_facts", "reconcile_medications", "compose_clinical"}
    rec = json.loads((tmp_path / "p" / "r1" / "S1.json").read_text())
    assert [c["name"] for c in rec["calls"]] == ["extract_facts", "reconcile_medications", "compose_clinical"]
    assert rec["step_errors"] == {}


def test_5a_gets_its_own_60_s_client(tmp_path):
    made = []

    def factory(read_timeout):
        made.append(read_timeout)
        return rpe.DryRunBedrock(read_timeout)
    rpe.main(["S1", "--batch", "p", "--yes"], client_factory=factory, now=lambda: QUIET,
             runs_dir=tmp_path, sleep=lambda _s: None)
    assert made == [90, 60]


class _FiveAFails(rpe.DryRunBedrock):
    def converse(self, **kw):
        resp = super().converse(**kw)
        if kw["toolConfig"]["tools"][0]["toolSpec"]["name"] == "record_reconciliation":
            resp["stopReason"] = "max_tokens"
        return resp


def test_a_5a_failure_keeps_the_record_in_the_step_2_and_5b_scores(tmp_path):
    rpe.main(["--all", "--batch", "p", "--yes"], client_factory=_FiveAFails, now=lambda: QUIET,
             runs_dir=tmp_path, sleep=lambda _s: None)
    rec = json.loads((tmp_path / "p" / "r1" / "S1.json").read_text())
    assert rec["failed_step"] == "reconcile_medications" and rec["error"]["code"] == "max_tokens"
    assert "select_safety_net" in rec["steps"] and "reconcile_medications" not in rec["steps"]
    assert SB.main([str(tmp_path / "p")]) == 0
    out = tmp_path / "p" / "scores"
    scored = json.loads((out / "safety_net.json").read_text())
    assert len(scored["results"]) == 18 and scored["skipped"] == []
    meds = json.loads((out / "meds.json").read_text())
    assert meds["results"] == [] and len(meds["skipped"]) == 18
    assert json.loads((out / "step2_advice.json").read_text())["summary"]["n_scored"] == 18
    # step 6 needs only step 3: a 5a failure must not skip it (28 Oct 2026)
    assert "compose_clinical" in rec["steps"] and rec["step_errors"] == {"reconcile_medications": "max_tokens"}
    assert json.loads((out / "compose_clinical.json").read_text())["summary"]["n_scored"] == 18


class _SixFails(rpe.DryRunBedrock):
    def converse(self, **kw):
        resp = super().converse(**kw)
        if kw["toolConfig"]["tools"][0]["toolSpec"]["name"] == "record_composition":
            resp["stopReason"] = "max_tokens"
        return resp


def test_a_step_6_failure_keeps_5a_and_counts_as_not_reached(tmp_path):
    rpe.main(["--all", "--batch", "p", "--yes"], client_factory=_SixFails, now=lambda: QUIET,
             runs_dir=tmp_path, sleep=lambda _s: None)
    rec = json.loads((tmp_path / "p" / "r1" / "S1.json").read_text())
    assert rec["failed_step"] == "compose_clinical" and rec["error"]["code"] == "max_tokens"
    assert "reconcile_medications" in rec["steps"] and "compose_clinical" not in rec["steps"]
    assert SB.main([str(tmp_path / "p")]) == 0
    out = tmp_path / "p" / "scores"
    assert len(json.loads((out / "meds.json").read_text())["results"]) == 18
    cp = json.loads((out / "compose_clinical.json").read_text())["summary"]
    assert cp["n_scored"] == 0 and len(cp["skipped"]) == 18
    assert cp["skipped"][0]["error_code"] == "max_tokens"
    assert "| 1 Reached step 6" in (out / "COMPOSE_CLINICAL.md").read_text()
    assert "0/18 | **NOT MET**" in (out / "COMPOSE_CLINICAL.md").read_text()


def test_step_6_is_sent_facts_only_never_the_notes(tmp_path):
    sent = []

    class Spy(rpe.DryRunBedrock):
        def converse(self, **kw):
            if kw["toolConfig"]["tools"][0]["toolSpec"]["name"] == "record_composition":
                sent.append(kw["messages"][0]["content"][0]["text"])
            return super().converse(**kw)
    rpe.main(["S1", "--batch", "p", "--yes"], client_factory=Spy, now=lambda: QUIET,
             runs_dir=tmp_path, sleep=lambda _s: None)
    s1 = next(s for s in CORPUS if s.id == "S1")
    assert len(sent) == 1 and not rce.re.search(r"\bL\d{3}\b", sent[0])
    assert not any(ln.strip() and ln.strip() in sent[0] for ln in s1.notes.splitlines() if len(ln.strip()) >= 15)


def test_dry_run_composition_passes_every_step_6_bar_line(tmp_path):
    _run(tmp_path, ["--all", "--batch", "p", "--yes"])
    assert SB.main([str(tmp_path / "p")]) == 0
    md = (tmp_path / "p" / "scores" / "COMPOSE_CLINICAL.md").read_text()
    assert md.count("**MET**") == 8 and "NOT MET" not in md and "PENDING" not in md
    assert (tmp_path / "p" / "scores" / "review_compose.md").read_text().strip().endswith("Nothing to rule on.")


W4_PIPELINE = ROOT / "evals" / "runs" / "w4-pipeline-e06-x5"


@pytest.mark.skipif(not (W4_PIPELINE / "batch.json").exists(), reason="saved W4 runs not present")
def test_rescoring_w4_reproduces_every_committed_report_and_the_w5_step_2_figures(tmp_path):
    import shutil
    b = tmp_path / "b"
    shutil.copytree(W4_PIPELINE, b, ignore=shutil.ignore_patterns("scores"))
    assert SB.main([str(b)]) == 0
    for f in ("SAFETY_NET.md", "MEDS.md", "review_meds.md", "review_safety_net.md"):
        assert (b / "scores" / f).read_bytes() == (W4_PIPELINE / "scores" / f).read_bytes(), f
    for f in ("safety_net.json", "meds.json"):     # key order varies (score_batch:87, carried open)
        assert json.loads((b / "scores" / f).read_text()) == json.loads((W4_PIPELINE / "scores" / f).read_text()), f
    sm = json.loads((b / "scores" / "step2_advice.json").read_text())["summary"]
    assert (sm["verified"]["n_gold"], sm["verified"]["recall"]) == (85, 56 / 85)
    assert sm["route_errors"] == {"not_extracted": 6, "lost_at_verification": 3, "non_advice_as_advice": 7}


def test_refuses_an_existing_batch(tmp_path):
    (tmp_path / "p").mkdir()
    assert _run(tmp_path, ["S1", "--batch", "p", "--yes"]) == 2
