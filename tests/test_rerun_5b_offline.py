"""evals/rerun_5b_offline.py — 5b recomputed from saved step-3 facts into a NEW
derived batch (CSO 23 Oct 2026). Offline: no model, no network. Synthetic facts
only in the unit tests; the W4 test reads saved records and writes to tmp_path.
No note text in tests or results."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import rerun_5b_offline as R  # noqa: E402
import score_batch as SB  # noqa: E402
from pipeline.safety_net import AB_PINNED_LABEL, select_safety_net  # noqa: E402

W4 = ROOT / "evals" / "runs" / "w4-pipeline-e06-x5"
FIXED_NOW = lambda: R.dt.datetime(2026, 10, 23, 14, 0, tzinfo=R.LONDON)  # noqa: E731


def _facts(age_group, advice):
    items = [{"fact_id": "A1", "citation_status": "verified",
              "cites": [{"lines": ["L001"], "quote": "Synthetic advice sentence.", "verified": True}]}]
    return {"documented_advice": items if advice else [], "age_group": age_group}


def _pre_decision_5b(facts):
    """5b as it rendered before 23 Oct: documented-route part_ab without the tail."""
    sn = select_safety_net({"facts": facts, "flags": []})
    sn["part_ab"] = sn["part_ab"].split("\n\n" + AB_PINNED_LABEL)[0]
    return sn


def _record(run, sid, facts, **extra):
    return {"generation_id": f"g-{run}-{sid}", "run": run, "scenario_id": sid, "generator": "pipeline",
            "error": None, "failed_step": None, "calls": [{"step": "extract_facts"}], "cost_usd_est": 0.01,
            "steps": {"guard_input": {"flags": []}, "validate_facts": {"facts": facts},
                      "route_resus": {"route": "x"}, "select_safety_net": _pre_decision_5b(facts)},
            **extra}


def _batch(tmp_path, records, generator="pipeline"):
    src = tmp_path / "src"
    gens = []
    for rec in records:
        rel = f"r{rec['run']}/{rec['scenario_id']}.json"
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(json.dumps(rec))
        gens.append({"run": rec["run"], "scenario_id": rec["scenario_id"], "record": rel})
    (src / "manifest.json").write_text(json.dumps({"batch_id": "src", "generator": generator,
                                                   "corpus": {"sha256": "c"}}))
    (src / "batch.json").write_text(json.dumps({"batch_id": "src", "generations": gens}))
    return src


def _tree_hash(d: Path) -> dict:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}


@pytest.fixture
def mini(tmp_path):
    src = _batch(tmp_path, [_record(1, "S1", _facts("adult", True)),
                            _record(1, "S8", _facts("neonate", False))])
    return src, tmp_path / "derived"


# --- what is written --------------------------------------------------------

def test_derived_record_has_new_5b_and_everything_else_from_the_source(mini):
    src, out = mini
    R.rerun(src, out, now=FIXED_NOW)
    old = json.loads((src / "r1/S1.json").read_text())
    new = json.loads((out / "r1/S1.json").read_text())
    assert new["steps"]["select_safety_net"]["part_ab"].endswith(
        AB_PINNED_LABEL + "\n" + new["steps"]["select_safety_net"]["pinned_line"])
    for k in ("guard_input", "validate_facts", "route_resus"):
        assert new["steps"][k] == old["steps"][k]
    assert {k: v for k, v in new.items() if k not in ("steps", "rerun")} == \
           {k: v for k, v in old.items() if k != "steps"}
    assert new["rerun"]["model_calls"] == 0 and new["rerun"]["from_record"] == "r1/S1.json"
    assert new["rerun"]["from_record_sha256"] == hashlib.sha256((src / "r1/S1.json").read_bytes()).hexdigest()


def test_manifest_and_index_name_the_derived_batch_and_its_source(mini):
    src, out = mini
    R.rerun(src, out, now=FIXED_NOW)
    m = json.loads((out / "manifest.json").read_text())
    b = json.loads((out / "batch.json").read_text())
    assert m["batch_id"] == b["batch_id"] == "derived"
    assert m["derived_from"]["from_batch"] == "src" and m["corpus"] == {"sha256": "c"}
    assert [g["record"] for g in b["generations"]] == ["r1/S1.json", "r1/S8.json"]


def test_source_batch_is_never_written(mini):
    src, out = mini
    before = _tree_hash(src)
    R.rerun(src, out, now=FIXED_NOW)
    assert _tree_hash(src) == before


def test_summary_counts(mini):
    src, out = mini
    s = R.rerun(src, out, now=FIXED_NOW)
    assert s["n"] == 2 and s["routes"] == {"documented": 1, "fallback": 1}
    assert all(v == 2 for v in s["unchanged"].values())
    assert s["part_ab_changed"] == {"documented": 1, "fallback": 0}
    assert s["pinned_in_part_ab"] == {"before": 1, "after": 2}
    assert s["haz03_v2_pass"] == {"before": 1, "after": 2}


def test_results_carry_no_note_or_output_text(mini):
    src, out = mini
    R.rerun(src, out, now=FIXED_NOW)
    blob = (out / "rerun.json").read_text() + (out / "RERUN.md").read_text()
    assert "Synthetic advice" not in blob and "NHS 111" not in blob and AB_PINNED_LABEL not in blob


def test_incomplete_source_record_is_skipped_and_counted(tmp_path):
    bad = _record(1, "S2", _facts("adult", True), error={"code": "x"})
    src = _batch(tmp_path, [_record(1, "S1", _facts("adult", True)), bad])
    s = R.rerun(src, tmp_path / "d", now=FIXED_NOW)
    assert s["n"] == 1 and s["skipped"] == [{"record": "r1/S2.json", "reason": "incomplete_source"}]
    assert not (tmp_path / "d" / "r1/S2.json").exists()


# --- refusals ---------------------------------------------------------------

def test_refuses_an_existing_output_directory(mini):
    src, out = mini
    out.mkdir()
    with pytest.raises(R.RerunError):
        R.rerun(src, out)


@pytest.mark.parametrize("where", ["same", "inside"])
def test_refuses_output_in_the_source(mini, where):
    src, _ = mini
    with pytest.raises(R.RerunError):
        R.rerun(src, src if where == "same" else src / "rerun")


def test_refuses_a_v1_batch(tmp_path):
    src = _batch(tmp_path, [_record(1, "S1", _facts("adult", True))], generator="v1")
    with pytest.raises(R.RerunError):
        R.rerun(src, tmp_path / "d")


def test_cli_refuses_with_exit_2(mini, capsys):
    src, out = mini
    out.mkdir()
    assert R.main([str(src), "--out", str(out)]) == 2


# --- the real W4 batch --------------------------------------------------------

@pytest.mark.skipif(not W4.is_dir(), reason="saved W4 pipeline runs not present")
def test_w4_rerun_changes_only_documented_route_part_ab(tmp_path):
    before = _tree_hash(W4)
    out = tmp_path / "w4-rerun"
    s = R.rerun(W4, out, now=FIXED_NOW)
    assert _tree_hash(W4) == before
    assert s["n"] == 90 and s["skipped"] == []
    assert s["routes"] == {"documented": 48, "fallback": 42}
    assert all(v == 90 for v in s["unchanged"].values()), s["unchanged"]
    assert s["part_ab_changed"] == {"documented": 48, "fallback": 0}
    assert s["pinned_in_part_ab"] == {"before": 42, "after": 90}
    assert s["haz03_v2_pass"] == {"before": 42, "after": 90}


@pytest.mark.skipif(not (W4 / "scores" / "safety_net.json").is_file(), reason="saved W4 scores not present")
def test_w4_rerun_scores_identically_on_the_safety_net_metric(tmp_path):
    out = tmp_path / "w4-rerun"
    R.rerun(W4, out, now=FIXED_NOW)
    old = {r["generation_id"]: r for r in json.loads((W4 / "scores" / "safety_net.json").read_text())["results"]}
    new = {r["generation_id"]: r for r in SB.score_batch(out)["results"]}
    assert new.keys() == old.keys() and len(new) == 90
    for gid, r in new.items():
        o = old[gid]
        assert (r["verdict"], r["failures"], r["route"]) == (o["verdict"], o["failures"], o["route"]), gid
        sh, osh = r["seek_help"], o["seek_help"]
        assert (sh["gold_documented"], sh["n_invented"]) == (osh["gold_documented"], osh["n_invented"]), gid
        # n_signposts is reported, not gated: the pinned line's two signpost sentences
        # (111, 999) now also sit in part_ab on the documented route — none invented.
        added = 2 if r["route"]["predicted"] == "documented" else 0
        assert sh["n_signposts"] - osh["n_signposts"] == added, gid
