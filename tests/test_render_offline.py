"""Item 3 R1 step 2 — the offline renderer harness (evals/render_offline.py).

Built on an invented fixture batch in a temporary folder (never the 90 saved p0.4 records). The
denominator is the source index (Codex 07:38:27 P2): every record and index entry is written, a
refusal removes the source's step-7 output and records its fixed code."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "tests"))

import render_offline as H  # noqa: E402
import test_pipeline_render_patient as F  # noqa: E402
from pipeline import render_patient as R  # noqa: E402

MODEL_C = "MODEL-WRITTEN PART C TEXT THAT MUST NOT SURVIVE"


def _tables():
    raw, words = F.raw_tables()
    return R.build_tables(raw, words, {"glossary.json": "fixture"})


def _cc(sections_a):
    return {"sections": [{"part": "part_a", "heading": s["heading"], "section_id": s["section_id"], "text": s["text"],
                          "fact_ids": [], "unverified_fact_ids": []} for s in sections_a],
            "age_group": "adult", "checks": [], "counts": {}}


def _record(batch_id, run, sid, steps, calls, step_errors=None, failed=None):
    return {"record_version": 1, "generation_id": f"{batch_id}/r{run}/{sid}", "batch_id": batch_id, "run": run,
            "scenario_id": sid, "generator": "pipeline", "path": "steps-1-5b-5a-6-7", "model_id": "m", "region": "r",
            "prompts": {"extract_facts": {"p": 1}, "compose_clinical": {"p": 6}, "compose_patient": {"p": 7}},
            "prompt_caching": True, "notes_sha256": "n" + sid, "started_at": "2026-11-04T10:00:00+00:00",
            "elapsed_s": 21.5, "calls": calls, "steps": steps, "output": None, "gate": None,
            "failed_step": failed, "error": {"code": step_errors[failed], "message": ""} if failed else None,
            "step_errors": step_errors or {}, "cost_usd_est": 0.0831}


CALLS = [{"name": "extract_facts", "latency_s": 9.1}, {"name": "compose_clinical", "latency_s": 7.0},
         {"name": "compose_patient", "latency_s": 4.2}]


def make_batch(root: Path, name="fx-src") -> Path:
    src = root / name
    (src / "r1").mkdir(parents=True)
    good = F.full_input()["sections"]
    refused = copy.deepcopy(good)
    refused[3] = {**refused[3], "text": "- Wound repaired in the emergency department."}
    step7 = {"sections": [{"heading": "why_in_hospital", "source_ids": ["sections.0"], "text": MODEL_C}],
             "checks": [], "counts": {}, "audience": "adult", "revised": False, "call": {"x": 1}}
    gens = []
    specs = [("X1", good, None), ("X2", refused, None), ("X3", None, "compose_clinical"),
             ("X4", good, None), ("X5", good, None)]
    for sid, a, fail in specs:
        steps = {"guard_input": {"flags": []}, "validate_facts": {"facts": {}}, "select_safety_net": {"part_c": "B"}}
        errors = None
        if a is not None:
            steps["compose_clinical"] = _cc(a)
            steps["compose_patient"] = copy.deepcopy(step7)
        else:
            errors = {"compose_clinical": "max_tokens", "compose_patient": "upstream_failed"}
        if sid == "X5":
            steps["verify_patient"] = {"flagged": 1}
            steps["compose_patient_first"] = copy.deepcopy(step7)
        rec = _record(name, 1, sid, steps, copy.deepcopy(CALLS), errors, fail)
        (src / "r1" / f"{sid}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        gens.append({"run": 1, "scenario_id": sid, "record": f"r1/{sid}.json", "failed_step": fail,
                     "error_code": (errors or {}).get(fail), "cost_usd_est": 0.0831})
    (src / "manifest.json").write_text(json.dumps({
        "record_version": 1, "batch_id": name, "created_at": "2026-11-04T10:00:00+00:00", "generator": "pipeline",
        "path": "steps-1-5b-5a-6-7", "prompts": {"extract_facts": {}, "compose_clinical": {}, "compose_patient": {}},
        "corpus": {"path": "x", "sha256": "c"}, "runs": 1}, indent=2), encoding="utf-8")
    (src / "batch.json").write_text(json.dumps({"batch_id": name, "stopped_early": False, "generations": gens},
                                               indent=2), encoding="utf-8")
    return src


def write_inputs(src: Path, path: Path) -> Path:
    path.write_text(H._dump(H.make_inputs(src)), encoding="utf-8")
    return path


@pytest.fixture
def batch(tmp_path):
    src = make_batch(tmp_path)
    return src, write_inputs(src, tmp_path / "inputs.json")


def test_inputs_lists_every_generation_with_hashes(batch):
    src, inputs = batch
    d = json.loads(inputs.read_text())
    assert d["source_batch"] == "fx-src" and len(d["generations"]) == 5
    x3 = d["generations"][2]
    assert x3["input_sha256"] is None and x3["reason"] == "upstream_failed"
    assert all(len(g["input_sha256"]) == 64 for i, g in enumerate(d["generations"]) if i != 2)


def test_render_keeps_the_denominator_and_records_the_refusal(batch, tmp_path):
    src, inputs = batch
    out = tmp_path / "out"
    s = H.render_batch(src, out, inputs, "fx-render", tables=_tables())
    assert s == {"batch_id": "fx-render", "records": 5, "rendered": 3, "refused": 1, "upstream_failed": 1}
    idx = json.loads((out / "batch.json").read_text())
    assert [g["scenario_id"] for g in idx["generations"]] == ["X1", "X2", "X3", "X4", "X5"]
    assert [g["error_code"] for g in idx["generations"]] == [None, "signpost_not_isolable", "max_tokens", None, None]
    x2 = json.loads((out / "r1" / "X2.json").read_text())
    assert "compose_patient" not in x2["steps"]
    assert x2["step_errors"] == {"compose_patient": "signpost_not_isolable"}
    assert x2["failed_step"] == "compose_patient" and x2["error"] == {"code": "signpost_not_isolable", "message": ""}
    assert x2["render_refusal"] == {"code": "signpost_not_isolable",
                                    "where": {"source_id": "sections.3", "line": 1, "sentence": None}}
    x3 = json.loads((out / "r1" / "X3.json").read_text())
    assert x3["step_errors"] == {"compose_clinical": "max_tokens", "compose_patient": "upstream_failed"}
    assert x3["failed_step"] == "compose_clinical"
    assert json.loads((out / "render.json").read_text()) == s


def test_no_historical_part_c_survives_anywhere(batch, tmp_path):
    src, inputs = batch
    out = tmp_path / "out"
    H.render_batch(src, out, inputs, "fx-render", tables=_tables())
    for p in out.rglob("*.json"):
        assert MODEL_C not in p.read_text(), p.name
    x5 = json.loads((out / "r1" / "X5.json").read_text())
    assert not set(x5["steps"]) & {"verify_patient", "compose_patient_first"}
    assert x5["steps"]["compose_patient"]["renderer"]["name"] == R.RENDERER_NAME
    assert "call" not in x5["steps"]["compose_patient"]


def test_record_labels_history_and_claims_no_timing(batch, tmp_path):
    src, inputs = batch
    out = tmp_path / "out"
    H.render_batch(src, out, inputs, "fx-render", tables=_tables())
    srec = json.loads((src / "r1" / "X1.json").read_text())
    d = json.loads((out / "r1" / "X1.json").read_text())
    assert d["calls"] == [] and d["cost_usd_est"] == 0.0
    assert "started_at" not in d and "elapsed_s" not in d
    assert d["historical"] == {"label": H.HISTORICAL_LABEL, "calls": CALLS[:2], "started_at": srec["started_at"],
                               "elapsed_s": 21.5, "cost_usd_est": 0.0831}
    for k in ("guard_input", "validate_facts", "select_safety_net", "compose_clinical"):
        assert d["steps"][k] == srec["steps"][k]                  # upstream steps unchanged
    assert d["batch_id"] == "fx-render" and d["generation_id"] == "fx-render/r1/X1"
    assert d["prompts"]["compose_patient"]["renderer"] == R.RENDERER_NAME
    assert d["prompts"]["extract_facts"] == {"p": 1}
    assert d["render_provenance"]["from_record"] == "r1/X1.json"
    assert d["render_provenance"]["input_sha256"] == H.input_sha256(srec["steps"]["compose_clinical"])
    inp = R.CP7.patient_input(srec["steps"]["compose_clinical"])
    assert d["steps"]["compose_patient"] == json.loads(json.dumps(R.render_patient(inp, _tables())))
    assert R.audit(inp, d["steps"]["compose_patient"], _tables()) == []
    m = json.loads((out / "manifest.json").read_text())
    assert "created_at" not in m and m["historical"]["created_at"] == "2026-11-04T10:00:00+00:00"
    assert m["derived_from"]["kind"] == "offline_renderer" and m["derived_from"]["model_calls"] == 0
    assert m["derived_from"]["dev"] is False and m["prompts"]["compose_patient"]["tables_status"] == "approved"
    assert set(json.loads((out / "render.json").read_text())) == {"batch_id", "records", "rendered", "refused",
                                                                  "upstream_failed"}
    assert set(json.loads((out / "run.json").read_text())) == {"started_at", "finished_at"}


@pytest.mark.parametrize("case", ["exists", "inside", "bad_id", "not_pipeline", "record_changed",
                                  "inputs_changed", "stub", "dev_on_measurement"])
def test_refusals(tmp_path, case):
    src = make_batch(tmp_path, "w6-pipeline-e09-m01-c02-p04-x5" if case == "dev_on_measurement" else "fx-src")
    inputs = write_inputs(src, tmp_path / "inputs.json")
    out, bid, kw = tmp_path / "out", "fx-render", {"tables": _tables()}
    if case == "exists":
        out.mkdir()
    elif case == "inside":
        out = src / "derived"
    elif case == "bad_id":
        bid = "../x"
    elif case == "not_pipeline":
        m = json.loads((src / "manifest.json").read_text())
        m["generator"] = "v1"
        (src / "manifest.json").write_text(json.dumps(m))
    elif case == "record_changed":
        p = src / "r1" / "X4.json"
        p.write_text(p.read_text() + " ")
    elif case == "inputs_changed":
        d = json.loads(inputs.read_text())
        d["generations"][0]["input_sha256"] = "0" * 64
        inputs.write_text(json.dumps(d))
    elif case == "stub":
        kw = {}                                   # the package's stub tables, without --dev
    elif case == "dev_on_measurement":
        kw["dev"] = True
    with pytest.raises(H.HarnessError):
        H.render_batch(src, out, inputs, bid, **kw)
    if case != "exists":
        assert not out.exists() or case == "inside" and not any(out.iterdir())


def test_stub_tables_run_only_with_dev_and_are_marked(batch, tmp_path):
    src, inputs = batch
    s = H.render_batch(src, tmp_path / "out", inputs, "fx-dev", dev=True)
    assert s["records"] == 5
    m = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert m["derived_from"]["dev"] is True and m["prompts"]["compose_patient"]["tables_status"] == "stub"


def _cli(args, seed):
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-B", str(ROOT / "evals" / "render_offline.py"), *args], cwd=ROOT,
                          env=env, capture_output=True, text=True)


def test_two_runs_are_byte_identical_but_for_run_json(batch, tmp_path):
    src, inputs = batch
    a, b = tmp_path / "one" / "first", tmp_path / "two" / "second"
    ra = _cli(["render", str(src), "--out", str(a), "--inputs", str(inputs), "--batch-id", "fx", "--dev"], "0")
    rb = _cli(["render", str(src), "--out", str(b), "--inputs", str(inputs), "--batch-id", "fx", "--dev"], "777")
    assert ra.returncode == rb.returncode == 0, ra.stderr + rb.stderr
    fa = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    fb = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert fa == fb
    for rel in fa:
        if rel.name != "run.json":
            assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel
    assert ra.stdout == "5 records: 3 rendered, 1 refused, 1 without step 6 -> " + str(a) + "\n"


def test_cli_inputs_and_refusal_exit_codes(batch, tmp_path):
    src, _inputs = batch
    p = tmp_path / "in2.json"
    r = _cli(["inputs", str(src), "--out", str(p)], "0")
    assert r.returncode == 0 and p.exists()
    assert json.loads(p.read_text()) == H.make_inputs(src)
    assert _cli(["inputs", str(src), "--out", str(p)], "0").returncode == 2            # never written over
    r = _cli(["render", str(src), "--out", str(tmp_path / "o"), "--inputs", str(p), "--batch-id", "fx"], "0")
    assert r.returncode == 2 and "refused" in r.stderr and not (tmp_path / "o").exists()   # stub tables, no --dev


def test_only_run_json_depends_on_the_clock(batch, tmp_path):
    src, inputs = batch
    import datetime as dt
    t1 = lambda: dt.datetime(2026, 11, 10, 8, 0, 0, tzinfo=H.LONDON)   # noqa: E731
    t2 = lambda: dt.datetime(2027, 1, 1, 23, 59, 59, tzinfo=H.LONDON)  # noqa: E731
    a, b = tmp_path / "a" / "x", tmp_path / "b" / "y"
    H.render_batch(src, a, inputs, "fx", tables=_tables(), now=t1)
    H.render_batch(src, b, inputs, "fx", tables=_tables(), now=t2)
    for p in a.rglob("*.json"):
        rel = p.relative_to(a)
        same = p.read_bytes() == (b / rel).read_bytes()
        assert same == (rel.name != "run.json"), rel


# --- Round 4 (Codex 09:04:06) ------------------------------------------------------------------------

def _tree_hashes(root: Path) -> dict:
    import hashlib
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def _inputs_without_path_checks(src: Path, path: Path) -> Path:
    # What an unchecked reader would compute for a tampered index — so render_batch's own path check,
    # not an inputs mismatch, is what must refuse.
    manifest, index, mb, bb = H._load_batch(src)
    rels = [g["record"] for g in index["generations"]]
    rbs = [(src / r).read_bytes() for r in rels]
    path.write_text(H._dump(H._inputs_from(manifest, mb, bb, rels, rbs)), encoding="utf-8")
    return path


@pytest.mark.parametrize("case", ["absolute", "traversal", "dotted", "duplicate", "case_duplicate", "symlink"])
def test_record_paths_are_checked_before_any_read_or_write(tmp_path, case):
    # P2: an index record path is trusted by nothing — an absolute path would make `out / rel` the source
    # record itself; traversal escapes; a repeat writes one file twice; a link reads outside the batch.
    src = make_batch(tmp_path)
    idx = json.loads((src / "batch.json").read_text())
    g = idx["generations"]
    if case == "absolute":
        g[0]["record"] = str(src / "r1" / "X1.json")
    elif case == "traversal":
        g[0]["record"] = "../fx-src/r1/X1.json"
    elif case == "dotted":
        g[0]["record"] = "r1/../r1/X1.json"
    elif case == "duplicate":
        g[3]["record"] = "r1/X1.json"
    elif case == "case_duplicate":
        g[3]["record"] = "r1/x1.json"
        (src / "r1" / "x1.json").write_bytes((src / "r1" / "X1.json").read_bytes())
    elif case == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_bytes((src / "r1" / "X1.json").read_bytes())
        (src / "r1" / "X1.json").unlink()
        (src / "r1" / "X1.json").symlink_to(outside)
    (src / "batch.json").write_text(json.dumps(idx, indent=2), encoding="utf-8")
    inputs = _inputs_without_path_checks(src, tmp_path / "inputs.json")
    before = _tree_hashes(tmp_path)
    with pytest.raises(H.HarnessError):
        H.make_inputs(src)
    out = tmp_path / "out"
    with pytest.raises(H.HarnessError):
        H.render_batch(src, out, inputs, "fx-render", tables=_tables())
    assert not out.exists()
    assert _tree_hashes(tmp_path) == before                        # the source records are unchanged


def _swap_during_render(monkeypatch, src, on_call):
    real = R.render_patient
    n = {"calls": 0}

    def wrapped(inp, tables=None):
        n["calls"] += 1
        on_call(n["calls"])
        return real(inp, tables)
    monkeypatch.setattr(R, "render_patient", wrapped)


def _changed_x4(src: Path) -> bytes:
    rec = json.loads((src / "r1" / "X4.json").read_text())
    rec["steps"]["compose_clinical"]["sections"][0]["text"] = "A different reason entirely."
    return json.dumps(rec, indent=2).encode("utf-8")


def test_a_record_changed_after_the_check_prevents_publication(batch, tmp_path, monkeypatch):
    # P2: the frozen-input check passed; then a LATER record changes before it is rendered.
    src, inputs = batch
    p, changed = src / "r1" / "X4.json", _changed_x4(src)
    _swap_during_render(monkeypatch, src, lambda k: p.write_bytes(changed) if k == 1 else None)
    out = tmp_path / "out"
    with pytest.raises(H.HarnessError, match="changed during the run"):
        H.render_batch(src, out, inputs, "fx-render", tables=_tables())
    assert not out.exists()


def test_the_rendered_bytes_are_the_bytes_that_passed_the_check(batch, tmp_path, monkeypatch):
    # The same change, undone before the end (so the final re-check alone cannot see it): the derived
    # record must still be the render of the CHECKED bytes, matching its provenance hash.
    src, inputs = batch
    clean = tmp_path / "clean"
    H.render_batch(src, clean, inputs, "fx-render", tables=_tables())
    p = src / "r1" / "X4.json"
    original, changed = p.read_bytes(), _changed_x4(src)
    _swap_during_render(monkeypatch, src, lambda k: p.write_bytes(changed) if k == 1 else
                        (p.write_bytes(original) if k == 3 else None))
    out = tmp_path / "out"
    H.render_batch(src, out, inputs, "fx-render", tables=_tables())
    assert (out / "r1" / "X4.json").read_bytes() == (clean / "r1" / "X4.json").read_bytes()
