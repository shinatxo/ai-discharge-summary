"""Item 3 R1 step 3 — the one input-side coverage pass (evals/render_coverage.py).

Invented fixture batch only (never the 90). The scan must read exactly
compose_patient.patient_input(record["steps"]["compose_clinical"])["sections"] — the six given PART A
headings, "Not documented" excluded — and nothing else in a record; it uses the renderer's own word
rules, never calls render_patient, and reports terms and counts only."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "tests"))

import render_coverage as C  # noqa: E402
import render_offline as H  # noqa: E402
import test_pipeline_render_patient as F  # noqa: E402
from pipeline import render_patient as R  # noqa: E402

# Distinctive invented terms. PERMITTED: in a given PART A section the scan must read.
# Every other word in these lines is a fixture general word, a stop word or a label, so the scan's
# exact output is predictable.
PERMITTED = {"presenting_complaint": "Zorblaxitis noted.", "diagnosis": "Primary: Quindrovia",
             "key_investigations": "- Plimtrex repeat.", "treatment": "- Vortaprine antibiotic.",
             "follow_up": "- Snellwick clinic.", "gp_actions": "- Draxomel check."}
PERMITTED_TERMS = {"zorblaxitis", "quindrovia", "plimtrex", "vortaprine", "snellwick", "draxomel"}
# EXCLUDED: every other place a word can sit in a record.
EXCLUDED_TERMS = {"fennibular", "glumphoria", "krellistan", "wexmortal", "obblequin", "trivelline", "hesperwick",
                  "quarnish", "mollidrex", "brenthaven", "notdocword"}


def _tables():
    raw, words = F.raw_tables()
    return R.build_tables(raw, words, {"glossary.json": "fixture"})


def _sec(part, heading, i, text):
    return {"part": part, "heading": heading, "section_id": f"sections.{i}", "text": text, "fact_ids": [],
            "unverified_fact_ids": []}


def _compose_clinical(extra_not_documented: bool):
    a = [_sec("part_a", h, i, t) for i, (h, t) in enumerate(PERMITTED.items())]
    a += [_sec("part_a", "risk_assessment", 10, "Fennibular score high."),
          _sec("part_a", "allergies", 11, "Glumphoria"),
          _sec("part_a", "vte_assessment", 12, "Krellistan prophylaxis"),
          _sec("part_a", "medications", 13, "{{MEDICATIONS}}"),
          _sec("part_a", "medication_conflicts", 14, "Mollidrex conflict"),
          _sec("part_b", "admission_summary", 15, "Wexmortal summary"),
          _sec("part_b", "follow_up", 16, "Brenthaven letter")]
    if extra_not_documented:      # a given heading whose text is "Not documented": excluded by patient_input
        a = [s for s in a if s["heading"] != "gp_actions"] + [_sec("part_a", "gp_actions", 17, "Not documented")]
    return {"sections": a, "age_group": "adult", "checks": [], "counts": {}}


def _record(name, sid, cc, extra=None):
    rec = {"record_version": 1, "batch_id": name, "run": 1, "scenario_id": sid, "generator": "pipeline",
           "notes": "Hesperwick history", "prompts": {}, "calls": [],
           "steps": {"extract_facts": {"facts": {"x": "Obblequin"}},
                     "compose_patient": {"sections": [{"heading": "why_in_hospital", "text": "Trivelline"}]},
                     "assemble": {"part_c": "Quarnish leaflet"}}}
    if cc is not None:
        rec["steps"]["compose_clinical"] = cc
    rec.update(extra or {})
    return rec


def make_batch(root: Path, name="cov-src") -> Path:
    src = root / name
    (src / "r1").mkdir(parents=True)
    specs = [("Y1", _compose_clinical(False)), ("Y2", _compose_clinical(True)), ("Y3", None)]
    gens = []
    for sid, cc in specs:
        (src / "r1" / f"{sid}.json").write_text(json.dumps(_record(name, sid, cc), indent=2), encoding="utf-8")
        gens.append({"run": 1, "scenario_id": sid, "record": f"r1/{sid}.json"})
    (src / "manifest.json").write_text(json.dumps({"batch_id": name, "generator": "pipeline"}), encoding="utf-8")
    (src / "batch.json").write_text(json.dumps({"batch_id": name, "generations": gens}), encoding="utf-8")
    return src


@pytest.fixture
def batch(tmp_path):
    src = make_batch(tmp_path)
    inputs = tmp_path / "inputs.json"
    inputs.write_text(H._dump(H.make_inputs(src)), encoding="utf-8")
    return src, inputs


def _terms(data, key="unknown"):
    return {r["term"] for r in data[key]}


def test_scan_reads_only_the_permitted_subset(batch):
    src, inputs = batch
    data = C.coverage(src, inputs, _tables())
    got = _terms(data)
    assert PERMITTED_TERMS <= got                     # every permitted distinctive term is found
    assert not (got & EXCLUDED_TERMS)                 # nothing from any excluded place in a record
    assert got <= PERMITTED_TERMS                     # nothing else: the fixture's other words are covered
    # "Not documented" gp_actions in Y2 excluded: Draxomel comes from Y1 only
    dr = next(r for r in data["unknown"] if r["term"] == "draxomel")
    assert dr["records"] == 1 and dr["headings"] == {"gp_actions": 1}
    zo = next(r for r in data["unknown"] if r["term"] == "zorblaxitis")
    assert zo["records"] == 2 and zo["occurrences"] == 2 and zo["forms"] == ["Zorblaxitis"]
    assert data["counts"]["records"] == 3 and data["counts"]["records_with_input"] == 2


def test_scan_input_is_exactly_patient_input_sections(batch, monkeypatch):
    src, inputs = batch
    seen = []
    real = C.CP7.patient_input

    def spy(cc):
        out = real(cc)
        seen.append(out)
        return out
    monkeypatch.setattr(C.CP7, "patient_input", spy)
    data = C.coverage(src, inputs, _tables())
    assert len(seen) == 4         # 2 records with a step 6: once in the inputs check, once in the scan
    text = " ".join(s["text"] for out in seen for s in out["sections"])
    assert {r["term"] for r in data["unknown"]} <= {w.lower() for w in R.WORD.findall(text)}


def test_the_renderer_is_never_called(batch, monkeypatch):
    src, inputs = batch

    def boom(*a, **k):
        raise AssertionError("render_patient must not be called by the coverage scan")
    monkeypatch.setattr(R, "render_patient", boom)
    monkeypatch.setattr(R, "_render", boom)
    monkeypatch.setattr(R, "render_line", boom)
    C.coverage(src, inputs, _tables())


def test_glossary_and_general_words_are_covered(tmp_path):
    raw, words = F.raw_tables()
    raw = copy.deepcopy(raw)
    raw["glossary"]["entries"].append({"id": "g_zorb", "forms": ["Zorblaxitis"], "case_sensitive": False,
                                       "plain": "a made-up illness", "category": "condition", "origin": "general",
                                       "flag_words": [], "keeps": {}})
    words = words + "\nplimtrex\n"
    T = R.build_tables(raw, words, {"glossary.json": "fixture"})
    src = make_batch(tmp_path)
    inputs = tmp_path / "inputs.json"
    inputs.write_text(H._dump(H.make_inputs(src)), encoding="utf-8")
    got = _terms(C.coverage(src, inputs, T))
    assert "zorblaxitis" not in got and "plimtrex" not in got and "quindrovia" in got


def test_non_alphabetic_tokens_listed_apart_and_digits_ignored(tmp_path):
    src = make_batch(tmp_path)
    rec = json.loads((src / "r1" / "Y1.json").read_text())
    rec["steps"]["compose_clinical"]["sections"][2]["text"] = "- Grublet-9 rose 287 → 412, x2."
    (src / "r1" / "Y1.json").write_text(json.dumps(rec), encoding="utf-8")
    inputs = tmp_path / "inputs.json"
    inputs.write_text(H._dump(H.make_inputs(src)), encoding="utf-8")
    data = C.coverage(src, inputs, _tables())
    other = _terms(data, "other_tokens")
    assert {"grublet-9", "x2"} <= other and not ({"287", "412"} & other)
    assert "grublet-9" not in _terms(data)


def test_output_holds_terms_and_counts_never_sentences(batch):
    src, inputs = batch
    data = C.coverage(src, inputs, _tables())
    blob = json.dumps(data)
    for t in PERMITTED.values():
        assert t not in blob and t.rstrip(".") not in blob
    for row in data["unknown"] + data["other_tokens"]:
        assert set(row) == {"term", "forms", "occurrences", "records", "scenarios", "headings"}
        assert " " not in row["term"] and all(" " not in f for f in row["forms"])
    assert "r1/Y1.json" not in blob and "Y1" not in blob


def test_refuses_when_the_inputs_file_does_not_match(batch):
    src, inputs = batch
    rec = json.loads((src / "r1" / "Y1.json").read_text())
    rec["steps"]["compose_clinical"]["sections"][0]["text"] = "Changed."
    (src / "r1" / "Y1.json").write_text(json.dumps(rec), encoding="utf-8")
    with pytest.raises(C.CoverageError):
        C.coverage(src, inputs, _tables())


def test_cli_refuses_to_overwrite_and_prints_counts_only(batch, tmp_path, capsys):
    src, inputs = batch
    tdir = tmp_path / "tables"
    tdir.mkdir()
    raw, words = F.raw_tables()
    for t in R.JSON_TABLES:
        (tdir / f"{t}.json").write_text(json.dumps(raw[t]), encoding="utf-8")
    (tdir / R.WORDS_FILE).write_text(words, encoding="utf-8")
    out = tmp_path / "cov.json"
    assert C.main([str(src), "--inputs", str(inputs), "--tables", str(tdir), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "zorblaxitis" not in printed.lower() and "unknown terms" in printed
    assert C.main([str(src), "--inputs", str(inputs), "--tables", str(tdir), "--out", str(out)]) == 2
