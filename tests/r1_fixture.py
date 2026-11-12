"""Item 3 R1 step 4 — the fixture world for the go / stop evaluator's tests.

Everything here is INVENTED and built in a temporary folder — never the 90 saved p0.4 records:
  * the source batch is a `run_pipeline_eval.py --dry-run`-style batch (the existing $0 fake Bedrock on
    the synthetic corpus) whose fake step 6 writes the invented PART A lines below (taken from the
    step-3 hard cases, which a test keeps clear of the scenario files) and whose fake step 2 reports
    each scenario's gold age group, so the unchanged scorer's code lines can all be met;
  * it is rendered twice by the unchanged harness with the approved tables and a fixture freeze record;
  * the rulings, the ledger review and the blind-read scores are written by this file and are marked
    as fixtures — they are NOT CSO rulings.

`build_world` runs the same order step 5 will: freeze -> render x2 -> the store -> sheets -> scores ->
seal -> score -> rulings -> bind -> the ledger sheet -> the review. Hooks let a test change one thing
at the right moment (a tampered record must be tampered in both run folders, before the seal and the
review are made, so that only the intended condition fails).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import make_patient_sheets as MS  # noqa: E402
import r1_freeze as FZ  # noqa: E402
import r1_go_stop as GS  # noqa: E402
import r1_locate_cases as LC  # noqa: E402
import r1_rulings_store as RS  # noqa: E402
import render_offline as H  # noqa: E402
import run_cold_eval as rce  # noqa: E402
import run_pipeline_eval as rpe  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402

FIXTURE_NOTE = "TEST FIXTURE — not a CSO ruling"
SCORED_BY = GS.SCORER + f" — {FIXTURE_NOTE}"
BATCH_ID = "fx-r1-renderer"
QUIET = rce.dt.datetime(2026, 10, 7, 14, 0, tzinfo=rce.LONDON)
A_TEXT = {      # invented PART A lines in c0.2's forms (tests/fixtures/render_tables_approved_cases.json "hard")
    "presenting_complaint": "The patient was admitted with chest pain. Found wandering at night (police informed).",
    "diagnosis": "Primary: NSTEMI (GRACE 142)\nBackground:\n- T2DM\n- Asthma",
    "key_investigations": "- CTPA: no PE.\n- CXR consolidation, CURB-65 2.",
    "treatment": "- Treated as CAP.\n- Oral antibiotics (inferred — not documented, confirm)",
    "follow_up": "- Cardiology clinic in 6 weeks; NHS 111 advice given.\n- OGD planned as an outpatient.",
    "gp_actions": "- Please check U&E in 1 week.",
}
PLAIN = {       # a generation with no drops at all (its ledger still has to be acknowledged — Codex 16:22 P1)
    "presenting_complaint": "The patient was admitted with chest pain.",
    "diagnosis": "Primary: NSTEMI\nBackground:\n- T2DM\n- Asthma",
    "key_investigations": "- CTPA: no PE.", "treatment": "- Treated as CAP.",
    "follow_up": "- OGD planned as an outpatient.", "gp_actions": "- Please check U&E in 1 week.",
}
WHOLE = {"key_investigations": "- GRACE score 142.\n- CURB-65 2."}     # a section dropped whole by authorised ops
REFUSE = {"treatment": "* IV antibiotics"}                               # an unsupported form -> refusal
VARIANTS = {"S2": PLAIN, "S8": WHOLE}                                    # neither is a blind-read item
SX2_ENTRY = "w_grace"                                                    # the approved withheld entry SX2 expects
_CORPUS = None


def corpus():
    global _CORPUS
    if _CORPUS is None:
        _CORPUS = rce.load_corpus(rce.DEFAULT_CORPUS_PATH)[0]
    return _CORPUS


def _age_groups() -> dict:
    return {s.id: json.loads((ROOT / "evals" / "gold" / f"{s.id}.json").read_text(encoding="utf-8"))
            ["age_group"]["value"] for s in corpus()}


class FixtureBedrock(rpe.DryRunBedrock):
    """The dry-run fake, with the scenario's gold age group and the invented PART A lines."""

    def converse(self, **kw):
        r = super().converse(**kw)
        tu = r["output"]["message"]["content"][0]["toolUse"]
        if tu["name"] == "record_facts":
            text = " ".join(c.get("text", "") for m in kw["messages"] for c in m["content"])
            hit = [s.id for s in corpus() if s.notes.strip().splitlines()[0][:30] in text]
            assert len(hit) == 1, hit
            tu["input"]["age_group"] = _age_groups()[hit[0]]
        return r

    @staticmethod
    def composition():
        secs = rpe.DryRunBedrock.composition()
        for s in secs:
            if s["part"] == "part_a" and s["heading"] in A_TEXT:
                s["text"] = A_TEXT[s["heading"]]
        return secs


def make_source(root: Path, runs: int = 1, variants: dict | None = None) -> tuple[Path, Path]:
    """(the invented source batch, its inputs file). `variants`: scenario id -> {A heading: text} for
    every run of that scenario, or "r<n>/<scenario id>" for one record only (a run-5-only change)."""
    with contextlib.redirect_stdout(io.StringIO()):
        rc = rpe.main(["--all", "--runs", str(runs), "--batch", "fx-source", "--yes"],
                      client_factory=FixtureBedrock, now=lambda: QUIET, runs_dir=root, sleep=lambda _s: None)
    assert rc == 0
    src = root / "fx-source"
    for sid, texts in (VARIANTS | (variants or {})).items():
        for p in ([src / f"{sid}.json"] if "/" in sid else sorted(src.glob(f"r*/{sid}.json"))):
            rec = json.loads(p.read_text(encoding="utf-8"))
            for sec in rec["steps"]["compose_clinical"]["sections"]:
                if sec["part"] == "part_a" and sec["heading"] in texts:
                    sec["text"] = texts[sec["heading"]]
            p.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    inputs = root / "inputs.json"
    inputs.write_text(H._dump(H.make_inputs(src)), encoding="utf-8")
    return src, inputs


def fixture_cases(src: Path, inputs: Path) -> dict:
    """A case list for the fixture source: locations from the locator, expected outcomes from the rules."""
    located, _reading = LC.locate(src, inputs)
    expected = {
        "L2": {"outcome": "retained", "must_keep": [r"\bno\b"]},
        "L3": {"outcome": "retained", "no_drop": True, "must_keep": ["planned"]},
        "L5": {"outcome": "retained", "c_must_not_match": [r"\b[Pp]atient\b"], "c_must_match": ["[Yy]our child"]},
        "L6i": {"outcome": "retained", "c_must_match": [re.escape(RP.ADOPTED_INFERRED)]},
    }
    cases = []
    for c in located["cases"]:
        exp = expected.get(c["category"])
        if c["category"] == "L2" and c["heading"] == "treatment":    # "not documented" inside the inferred bracket:
            exp = expected["L6i"]                                     # it leaves with the adopted 1(a) wording
        if c["category"] == "L1":
            entry = next(x for x in c["screen"] if x.startswith("w_"))
            exp = {"outcome": "approved_omission", "drop": {"rule": "withheld", "entry": entry}}
            if c["heading"] == "diagnosis":
                exp["must_keep"] = ["NSTEMI"]
        if exp is not None:
            cases.append({k: c[k] for k in ("case_id", "category", "record", "source_id", "heading", "line",
                                            "sentence", "line_sha256")} | {"expected": exp})
    return {
        "cases_version": GS.CASES_VERSION, "inputs_sha256": located["inputs_sha256"],
        "adopted": {"by": FIXTURE_NOTE},
        "located": cases,
        "synthetic": [{
            "case_id": "SX1", "label": "SYNTHETIC — invented boundary control, not from the 90",
            "input": {"audience": "adult", "sections": [
                {"section_id": "sections.0", "heading": "presenting_complaint", "text": "Admitted with chest pain."},
                {"section_id": "sections.2", "heading": "key_investigations",
                 "text": "- Notes conflict: weight 70 kg vs 80 kg — requires clarification."}]},
            "at": {"source_id": "sections.2", "line": 1, "sentence": None},
            "expected": {"outcome": "refusal", "code": "conflict_in_input"}}, {
            "case_id": "SX2", "label": "SYNTHETIC — invented boundary control, not from the 90",
            "input": {"audience": "adult", "sections": [
                {"section_id": "sections.0", "heading": "presenting_complaint", "text": "Admitted with chest pain."},
                {"section_id": "sections.1", "heading": "diagnosis", "text": "Primary: NSTEMI (GRACE 142)"}]},
            "at": {"source_id": "sections.1", "line": 1, "sentence": None},
            "expected": {"outcome": "approved_omission", "drop": {"rule": "withheld", "entry": SX2_ENTRY},
                         "must_keep": ["NSTEMI"], "prose_must_not_match": ["GRACE"]}}],
        "known_failures": [{
            "kf_id": "KF-FX", "scenario_id": "S1", "runs": "all", "checks": [
                {"type": "ADD", "token": "camera test", "not_from": "angiogra"},
                {"type": "ADD", "token": r"\bswab", "copy_only": True},
                {"type": "KEEP", "term": "OGD", "a_headings": ["follow_up"]},
                {"type": "DIAG"},
                {"type": "RULED", "kinds": ["advice"], "c_headings": ["what_we_did", "next_steps"]},
                {"type": "WITHHELD"},
                {"type": "BLIND", "item": "S1", "dimension": "audience"}]}],
        "generations": [{"case_id": "U2-FX", "record": "r1/B6.json", "expected": "rendered",
                         "c_must_not_match": [r"notes\s+conflict"]}],
    }


@dataclass
class World:
    root: Path
    src: Path
    inputs: Path
    cases: Path
    freeze: Path
    batch: Path
    run2: Path
    store: Path
    sheets: Path

    @property
    def review(self) -> Path:
        return self.batch / GS.LEDGER_DIR / "ledger-review.json"

    def evaluate(self, **kw) -> dict:
        args = dict(run2=self.run2, store=self.store, sheets=self.sheets, freeze=self.freeze, inputs=self.inputs)
        args.update(kw)
        return GS.evaluate(self.batch, **args)

    def candidate(self) -> GS.Candidate:
        return GS.Candidate(self.batch)

    def freeze_sha(self) -> str:
        return FZ.sha_file(self.freeze)


def fill_scores(sheets: Path, verdict: str = "pass") -> None:
    for p in sorted((sheets / "scores").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["verdicts"] = {k: verdict for k in MS.DIMENSIONS}
        d["scored_by"] = SCORED_BY
        p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")


def rule_everything(w: World, ruling: str = "acceptable") -> None:
    inv = json.loads((w.batch / "scores" / GS.INVENTORY_FILE).read_text(encoding="utf-8"))
    for sid, items in inv.items():
        p = w.store / f"{sid}.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["patient_rulings"] = {k: ruling for k in items}
        d["patient_rulings_meta"] = {"note": FIXTURE_NOTE}
        p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fill_review(w: World) -> dict:
    d = json.loads(w.review.read_text(encoding="utf-8"))
    d["binding"]["reviewed_by"], d["binding"]["reviewed_at"] = SCORED_BY, "2026-11-11T18:00:00+00:00"
    for g in d["generations"]:
        g["acknowledged"] = True
    for x in d["drops"] + d["sections"]:
        x["verdict"], x["note"] = "approved_omission", FIXTURE_NOTE
    w.review.write_text(GS.dump(d), encoding="utf-8")
    return d


def build_world(root: Path, src: Path, inputs: Path, *, cases: dict | None = None, hooks: dict | None = None,
                until: str = "review") -> World:
    """freeze -> render x2 -> [after_render] -> store -> sheets -> scores -> [before_seal] -> seal -> score ->
    rulings -> [before_bind] -> bind -> ledger sheet -> review -> [after_review]. `until` stops early
    (after "render", "sheets", "seal", "score", "bind")."""
    hooks = hooks or {}
    root.mkdir(parents=True, exist_ok=True)
    w = World(root, src, inputs, root / "cases.json", root / "FREEZE.json", root / BATCH_ID, root / "run2" / BATCH_ID,
              root / "store", root / "sheets")
    w.cases.write_text(GS.dump(cases if cases is not None else fixture_cases(src, inputs)), encoding="utf-8")
    FZ.write(w.freeze, FZ.make(w.cases))
    tables = RP.default_tables()                 # the approved tables, loaded once per process
    H.render_batch(src, w.batch, inputs, BATCH_ID, freeze=w.freeze, tables=tables)
    H.render_batch(src, w.run2, inputs, BATCH_ID, freeze=w.freeze, tables=tables)
    if "after_render" in hooks:
        hooks["after_render"](w)
    if until == "render":
        return w
    RS.build(w.store, w.batch)
    c, fsha = w.candidate(), w.freeze_sha()
    if GS.blind_read_possible(c):
        MS.build(w.batch, 1, w.sheets)
        fill_scores(w.sheets)
    if "before_seal" in hooks:
        hooks["before_seal"](w)
    if until == "sheets":
        return w
    if GS.blind_read_possible(c):
        GS.seal(w.sheets, c, fsha, SCORED_BY, attest=True)
    if until == "seal":
        return w
    GS.score(c, w.store, w.sheets, fsha)
    if until == "score":
        return w
    rule_everything(w)
    if "before_bind" in hooks:
        hooks["before_bind"](w)
    GS.bind_rulings(c, w.store, fsha, SCORED_BY, attest=True)
    if until == "bind":
        return w
    (w.batch / GS.LEDGER_DIR).mkdir()
    (w.batch / GS.LEDGER_DIR / "LEDGER-REVIEW.md").write_text(GS.ledger_sheet_md(c, tables), encoding="utf-8")
    w.review.write_text(GS.dump(GS.ledger_template(c, fsha, tables)), encoding="utf-8")
    fill_review(w)
    if "after_review" in hooks:
        hooks["after_review"](w)
    return w


def copy_world(w: World, dst: Path) -> World:
    """A private copy for a test that changes something after the fact. The freeze record and the case list
    stay the originals (the freeze pins the case list by its path)."""
    shutil.copytree(w.root, dst)
    m = lambda p: dst / p.relative_to(w.root)          # noqa: E731
    return World(dst, w.src, w.inputs, w.cases, w.freeze, m(w.batch), m(w.run2), m(w.store), m(w.sheets))


def both_runs(w: World, rel: str, edit) -> None:
    """Apply the same change to a record in both run folders (so run 2 stays byte-identical)."""
    for d in (w.batch, w.run2):
        p = d / rel
        rec = json.loads(p.read_text(encoding="utf-8"))
        edit(rec)
        p.write_text(H._dump(rec), encoding="utf-8")
