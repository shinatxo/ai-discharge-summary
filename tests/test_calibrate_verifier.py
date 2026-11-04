"""evals/calibrate_verifier.py — offline calibration of step 7v (ADR-010 V5).
Fake Bedrock; no AWS. The label-join self-tests read the saved step-7 batches and
skip when they are not mounted."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "evals"))

import calibrate_verifier as C  # noqa: E402
import run_cold_eval as rce  # noqa: E402
from pipeline import verify_patient as V  # noqa: E402

QUIET = dt.datetime(2026, 11, 4, 15, 0, tzinfo=rce.LONDON)
DEV = "w6-pipeline-e09-m01-c02-p04-x5"
HELD = ("w6-pipeline-e09-m01-c02-p02-x5", "w6-pipeline-e09-m01-c02-p01-x5")
HAVE_RUNS = all((C.RUNS_DIR / b).is_dir() for b in (DEV, *HELD))
needs_runs = pytest.mark.skipif(not HAVE_RUNS, reason="saved step-7 batches not mounted")


def test_bar_is_the_one_written_before_calibration():
    assert C.BAR == {"missed_max": 0, "false_flag_max": 0.25, "self_agreement_min": 0.90}


def _args(*argv):
    return C.parse_args(list(argv))


def test_a_held_out_run_needs_the_frozen_prompt_sha(tmp_path):
    batch = ROOT / "evals" / "runs" / DEV
    if not batch.is_dir():
        pytest.skip("saved batch not mounted")
    assert C.cmd_run(_args("run", str(batch), "--held-out", "--yes"), client_factory=C.DryVerifier,
                     now=lambda: QUIET, cal_dir=tmp_path) == 2
    assert C.cmd_run(_args("run", str(batch), "--held-out", "--frozen", "000000000000", "--yes"),
                     client_factory=C.DryVerifier, now=lambda: QUIET, cal_dir=tmp_path) == 2
    assert not any(tmp_path.iterdir())


@needs_runs
def test_run_writes_one_file_per_leaflet_and_repeat_and_never_overwrites(tmp_path):
    batch = C.RUNS_DIR / DEV
    a = _args("run", str(batch), "--repeats", "2", "--yes")
    assert C.cmd_run(a, client_factory=C.DryVerifier, now=lambda: QUIET, cal_dir=tmp_path, sleep=lambda s: None) == 0
    out = tmp_path / C.prompt_sha12() / DEV
    assert len(list(out.glob("rep*/r*/*.json"))) == 2 * len(C.records(batch))
    man = json.loads((out / "manifest.json").read_text())
    assert man["prompt_sha256"] == V.system_prompt()[1] and man["held_out"] is False
    one = json.loads(next(out.glob("rep1/r1/*.json")).read_text())
    assert one["error"] is None and one["verify_patient"]["checks"] == []      # the dry fake is well-formed
    assert C.cmd_run(a, client_factory=C.DryVerifier, now=lambda: QUIET, cal_dir=tmp_path) == 2


def test_a_canary_window_is_refused(tmp_path):
    batch = ROOT / "evals" / "runs" / DEV
    if not batch.is_dir():
        pytest.skip("saved batch not mounted")
    night = dt.datetime(2026, 11, 5, 1, 50, tzinfo=rce.LONDON)
    assert C.cmd_run(_args("run", str(batch), "--yes"), client_factory=C.DryVerifier,
                     now=lambda: night, cal_dir=tmp_path) == 3


@needs_runs
@pytest.mark.parametrize("bid", (DEV, *HELD))
def test_every_ruled_row_joins_to_a_sentence_or_a_diagnosis_section(bid):
    """Instrument self-test: a label the harness cannot place would silently drop out."""
    empty = {"verdicts": [], "unreached": []}
    n = 0
    for rec in C.records(C.RUNS_DIR / bid):
        for lab in C.labels_for(rec):
            n += 1
            caught, ids = C.match(lab, rec, empty)
            assert caught is False
            if lab["kind"] not in C.UNREACHED_KINDS:
                assert ids, (rec["generation_id"], lab["kind"], lab["heading"])
    assert n > 0


def _oracle(rec, flag_keys):
    """A verifier output that flags exactly the sentences/sections of the given label keys."""
    vin = V.verify_input(C.CP7.patient_input(rec["steps"]["compose_clinical"]), rec["steps"]["compose_patient"])
    flag_ids, unreached = set(), []
    for lab in C.labels_for(rec):
        if lab["key"] not in flag_keys:
            continue
        if lab["kind"] in C.UNREACHED_KINDS:
            unreached.append({"c_section_id": "c_sections.0", "heading": "why_in_hospital", "a_section_id": "",
                              "reason": "never_drop_item", "span": lab["sentence"].removeprefix("not reached: ")})
        else:
            flag_ids |= set(C.match(lab, rec, {"verdicts": [], "unreached": []})[1])
    verdicts = [{"sentence_id": t["sentence_id"], "c_section_id": s["section_id"], "heading": s["heading"],
                 "verdict": "added" if t["sentence_id"] in flag_ids else "supported",
                 "reason": "other" if t["sentence_id"] in flag_ids else "none", "a_section_id": "", "span": ""}
                for s in vin["c_sections"] for t in s["sentences"]]
    return {"verdicts": verdicts, "unreached": unreached, "flagged": 0, "checks": [], "counts": {}, "call": {}}


def _write(tmp_path, bid, make, repeats=1):
    for k in range(1, repeats + 1):
        for rec in C.records(C.RUNS_DIR / bid):
            d = tmp_path / bid / f"rep{k}" / f"r{rec['run']}"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{rec['scenario_id']}.json").write_text(json.dumps(
                {"generation_id": rec["generation_id"], "verify_patient": make(rec), "error": None}))
    (tmp_path / bid / "manifest.json").write_text(json.dumps({"prompt_sha256": V.system_prompt()[1]}))


@needs_runs
def test_a_perfect_verifier_meets_items_1_and_2_and_a_silent_one_misses_every_defect(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "blind_read_map", lambda: [])
    _write(tmp_path / "perfect", DEV,
           lambda rec: _oracle(rec, {l["key"] for l in C.labels_for(rec) if l["ruling"] == "defect"}))
    ev = C.evaluate([DEV], tmp_path / "perfect")
    assert ev["defects"] == 41 and ev["missed"] == [] and ev["errors"] == 0
    # flagging a defect's whole section can sweep in an acceptable row of the same section: report, don't hide
    assert ev["false_rate"] is not None and ev["false_rate"] < 0.10
    _write(tmp_path / "silent", DEV, lambda rec: _oracle(rec, set()))
    ev = C.evaluate([DEV], tmp_path / "silent")
    assert len(ev["missed"]) == 41 and ev["false_flags"] == 0 and ev["unlabelled"] == []


@needs_runs
def test_self_agreement_is_one_for_identical_repeats(tmp_path):
    _write(tmp_path, DEV, lambda rec: _oracle(rec, set()), repeats=2)
    assert C.self_agreement(DEV, tmp_path) == 1.0


@needs_runs
def test_score_refuses_mixed_prompts_and_writes_its_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "blind_read_map", lambda: [])
    _write(tmp_path, DEV, lambda rec: _oracle(rec, set()), repeats=2)
    assert C.cmd_score(_args("score", "--dev", DEV), sha_dir=tmp_path) == 1          # silent verifier: NOT MET
    md = (tmp_path / "scores" / "CALIBRATION.md").read_text()
    assert "dev only — not the bar" in md and "**NOT MET**" in md
    _write(tmp_path, HELD[0], lambda rec: _oracle(rec, set()))
    (tmp_path / HELD[0] / "manifest.json").write_text(json.dumps({"prompt_sha256": "f" * 64}))
    assert C.cmd_score(_args("score", "--dev", DEV, "--held-out", HELD[0]), sha_dir=tmp_path) == 2
