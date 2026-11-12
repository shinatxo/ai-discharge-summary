"""Item 3 R1 step 4 — the go / stop evaluator (decision-2 Round 3 D3' / D9; the step-4 design
Rounds 1-3 and the 18:02 notes, adopted by Shina (author/CSO) 11 Nov 2026: D1-D8).

ONE entry point decides the experiment, over the saved renderer batch:

    python evals/r1_go_stop.py evaluate --batch <dir> --run2 <dir> --store <dir> --sheets <dir>
                                        [--freeze evals/r1/FREEZE.json] [--inputs evals/r1/inputs.json]
                                        [--ledger-review <file>] [--out <dir>]

It prints GO, STOP, INCOMPLETE or REFUSED and names every condition that is not met. It also prints
which source the batch was rendered from, and marks any verdict on a batch that is not the
measurement batch as "on a fixture — NOT the measurement":

  REFUSED     the evidence does not hold together — the freeze record does not match the files on
              disk or the batch; a record's provenance or input hash is wrong; the blind-read seal,
              the rulings binding or the ledger review is for another candidate or was changed after
              it was bound; the store's upstream families differ from their historical source.
  INCOMPLETE  nothing is NOT MET, but something cannot be decided yet (rulings not entered or not
              bound, the ledger review not finished, run 2 missing, the blind read not sealed).
              INCOMPLETE is never GO and cannot become GO by default — only by entering the inputs.
  STOP        any condition NOT MET (STOP wins over anything undecided).
  GO          every condition MET.
Exit codes: 0 GO · 1 STOP · 3 INCOMPLETE · 2 REFUSED.

D9 in code, as far as code can: until the blind read is sealed the evaluator evaluates NOTHING, and
`score` and `ledger-sheet` refuse. (One exception: if one of the eight blind-read items has no step-7
output, no blind read is possible; the run is already STOP by its counts, and everything is reported.)

The conditions (D3'):
  G1  every generation rendered — the source index is the denominator; a refusal fails
  G2  the code-decided bar lines (1, 2, 3, 7, 8, 11, 13 and the code parts of 5, 6, 9) MET on every
      generation, FK <= 8 on every generation — computed from the scorer's summary fields
  G3  every flag ruled in the fresh store, bound to this candidate: 0 defects, 0 pending (promise
      flags included — D8)
  G4  Shina's review of the drop ledger: all generations acknowledged (zero-drop ones too), every
      drop and every wholly dropped section ruled; any required item dropped -> NOT MET even when
      bar line 9 is MET
  G5  render_patient.audit() clean on every record (partition, reproduction, protected tokens,
      authorised ops, section text, the re-render)
  G6  every case at its expected outcome — located cases and synthetic controls alike: retained,
      approved omission (its rule and entry, and no drop that was not prewritten) or refusal; any
      other outcome FAILS; every known failure absent; every diagnosis line accounted for from the
      source to the SAVED C text (Rounds 2-3: a standalone label is structure; an inline diagnosis
      and each bullet keep a diagnosis word; only a confirmed `withheld` sub-line unit may leave;
      the ops cover the line exactly once, each substitution is an approved entry's fixed form, and
      the saved C line equals the line the evaluator writes out from those ops itself)
  G7  blind read 8/8 on five dimensions, sealed before any report
  G8  determinism: run 2 byte-identical to run 1 except run.json
  --  bar line 12 (latency) is always NOT CLAIMED. score_patient's own line 12 prints MET on a
      renderer batch (no step-7 call, so nothing is ever late); it is never read here.
  --  the 5b / D4 / step 2 / step 6 parts of line 11 are the source batch's own (historical).

Helpers, so step 5 builds nothing new:
    freeze        write the freeze record (evals/r1_freeze.py)
    seal          bind the blind-read scores to the eight items of THIS batch (exactly eight score
                  files, each generation_id = the batch's run-1 record, five dimensions, scored_by
                  Shina; sheets byte-identical to a re-render; no scores/ or evaluator output yet)
    score         the UNCHANGED score_batch.score_batch(batch, adjudications_dir=<fresh store>) +
                  write_reports + the review inventory (keys, kinds, headings, tokens — no sentences).
                  score_batch's own command line cannot take a store. Refused before a valid seal;
                  a first scoring needs the store empty and unbound; a later one validates the binding.
    bind-rulings  write <store>/RULINGS.json: the rulings tied to batch, freeze, inventory, review
                  file, reviewer and time; refuses a ruled key that is not in this batch's inventory
    ledger-sheet  the drop-review sheet + the review template (90 generation entries, every drop,
                  every wholly dropped section). Refused before a valid seal.

What the evaluator CANNOT prove — said plainly:
  * that no diagnostic, report, record or ledger was VIEWED before the blind read. Anyone can open a
    file or score a copy. Code shows only: no scores/ in the batch at sealing, the supported scoring
    path refused before the seal, the scores unchanged since. The rest is Shina's attestation;
  * that the attestation is true, or who typed a score file (`scored_by` is recorded, not proved);
  * that nothing rendered the 90 before the freeze (a direct call to render_patient goes round the
    harness guard), or that the freeze was committed and pushed (the guarded commit's receipt);
  * that "absent" by the known-failure checks equals clinically absent — they are located screens;
    the blind read and the rulings carry the judgement;
  * anything beyond this bar on these inputs: 18 scenarios, historically used — not unseen cases.
Not built (Round 1 called it "reported, not deciding"): the per-term glossed / copied screen for the
audience known failures — those are decided by the blind read alone (the BLIND check).

The console and GO-STOP.* name conditions, generations, codes and counts — never a sentence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import make_patient_sheets as MS  # noqa: E402
import r1_freeze as FZ  # noqa: E402
import r1_rulings_store as RS  # noqa: E402
import render_offline as H  # noqa: E402
import score_batch as SB  # noqa: E402
import score_patient as SP  # noqa: E402
from pipeline import compose_patient as CP7  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402
from pipeline.errors import StepError  # noqa: E402

EVALUATOR_VERSION = 1
CASES_VERSION = 1
MET, NOT_MET, UNDECIDED = "MET", "NOT MET", "UNDECIDED"
GO, STOP, INCOMPLETE, REFUSED = "GO", "STOP", "INCOMPLETE", "REFUSED"
EXIT = {GO: 0, STOP: 1, REFUSED: 2, INCOMPLETE: 3}
DEFAULT_FREEZE = ROOT / "evals" / "r1" / "FREEZE.json"
SCORER = "Shina (author/CSO)"
ATTESTATION = ("Scored before score_batch was run and before any report, ledger or renderer diagnostic "
               "was opened")
SEAL_FILE, RULINGS_FILE, INVENTORY_FILE = "BLIND-READ-SEAL.json", "RULINGS.json", "review-inventory.json"
OUT_DIR, LEDGER_DIR = "go-stop", "ledger-review"            # inside the batch; never part of run 2
NOT_PART_OF_THE_RUN = ("run.json", "scores", OUT_DIR, LEDGER_DIR)
LEDGER_VERDICTS = ("approved_omission", "required_dropped")
OMISSION_LIST = ("reason for admission", "diagnosis (Background included)", "procedure / treatment",
                 "stated test conclusion", "appointment", "referral", "GP action")
LINE_12 = "12 latency: NOT CLAIMED (no step-7 call; upstream timing is historical)"
LONDON = ZoneInfo("Europe/London")


class Refused(Exception):
    """The evidence does not hold together; nothing is decided."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems[:3]))


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _now() -> str:
    return dt.datetime.now(LONDON).isoformat(timespec="seconds")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused(f"{path.name}:not_readable") from None


# =================================================================================================
# the candidate batch and its integrity (REFUSED)
# =================================================================================================

class Candidate:
    """The saved renderer batch, read once."""

    def __init__(self, batch_dir: Path):
        self.dir = batch_dir.resolve()
        try:
            self.manifest_bytes = (self.dir / "manifest.json").read_bytes()
            self.index_bytes = (self.dir / "batch.json").read_bytes()
            self.manifest = json.loads(self.manifest_bytes)
            self.index = json.loads(self.index_bytes)
            self.rels = H._record_paths(self.dir, self.index)
            self.record_bytes = [(self.dir / rel).read_bytes() for rel in self.rels]
            self.records = [json.loads(b) for b in self.record_bytes]
            ids = [g.get("scenario_id") for g in self.index["generations"]] + \
                  [r.get("scenario_id") for r in self.records]
        except (OSError, ValueError, KeyError, TypeError, AttributeError, H.HarnessError):
            raise Refused("batch:not_readable") from None
        if not all(RS.safe_scenario_id(i) for i in ids):               # an id is used as a file name
            raise Refused("batch:scenario_id")
        self.derived = self.manifest.get("derived_from") or {}
        self.batch_id = self.manifest.get("batch_id")
        self.by_rel = dict(zip(self.rels, self.records))

    @property
    def is_measurement(self) -> bool:
        return bool(self.derived.get("measurement")) or self.derived.get("from_batch") == FZ.MEASUREMENT_BATCH

    def source_label(self) -> str:
        src = self.derived.get("from_batch")
        return f"{src} (MEASUREMENT)" if self.is_measurement else f"{src} (NOT the measurement batch — fixture)"

    def step7(self, rec: dict):
        return (rec.get("steps") or {}).get("compose_patient")

    def binding(self, freeze_sha: str) -> dict:
        return {"batch_id": self.batch_id, "manifest_sha256": sha_bytes(self.manifest_bytes),
                "batch_sha256": sha_bytes(self.index_bytes), "freeze_sha256": freeze_sha}


def step7_input(rec: dict):
    cc = (rec.get("steps") or {}).get("compose_clinical")
    return None if cc is None else CP7.patient_input(cc)


def integrity(c: Candidate, freeze_path: Path, inputs_path: Path) -> tuple[str, dict]:
    """(freeze sha256, the freeze record). Raises Refused naming every problem (files and fields only)."""
    try:
        freeze_sha, frozen, problems = FZ.check(freeze_path)
    except FZ.FreezeError as exc:
        raise Refused(f"freeze:{exc}") from None
    probs = [f"freeze:{p}" for p in problems]
    d = c.derived
    if d.get("kind") != "offline_renderer" or d.get("dev") is not False or d.get("model_calls") != 0:
        probs.append("batch:not_a_non_dev_renderer_batch")
    if d.get("harness_version") != H.HARNESS_VERSION:
        probs.append("batch:harness_version")
    if d.get("freeze_sha256") != freeze_sha:
        probs.append("batch:freeze_sha256")
    if d.get("renderer") != frozen.get("renderer") or d.get("pipeline_version") != frozen.get("pipeline_version"):
        probs.append("batch:renderer_identity")
    try:
        ib = inputs_path.read_bytes()
        inputs = json.loads(ib)
        gens = inputs["generations"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Refused(probs + ["inputs:not_readable"]) from None
    if d.get("inputs_sha256") != sha_bytes(ib) or d.get("from_batch") != inputs.get("source_batch") \
            or d.get("manifest_sha256") != inputs.get("manifest_sha256") \
            or d.get("batch_sha256") != inputs.get("batch_sha256"):
        probs.append("batch:inputs")
    if c.is_measurement:
        m = frozen.get("measurement") or {}
        if sha_bytes(ib) != FZ.MEASUREMENT_INPUTS_SHA256 or d.get("from_batch") != FZ.MEASUREMENT_BATCH \
                or d.get("manifest_sha256") != m.get("manifest_sha256") or d.get("batch_sha256") != m.get("batch_sha256"):
            probs.append("batch:measurement_identity")
        if (frozen.get("cases") or {}).get("path") != FZ.MEASUREMENT_CASES:
            probs.append("freeze:cases_path")
    entries = c.index.get("generations") or []
    if len(entries) != len(gens) or [e.get("record") for e in entries] != [g["record"] for g in gens]:
        probs.append("batch:index_differs_from_inputs")
        raise Refused(probs)
    for rel, rec, g in zip(c.rels, c.records, gens):
        tag = f"{rel}"
        prov = rec.get("render_provenance") or {}
        if prov.get("from_record") != g["record"] or prov.get("from_record_sha256") != g["record_sha256"] \
                or prov.get("input_sha256") != g["input_sha256"] or prov.get("freeze_sha256") != freeze_sha:
            probs.append(f"{tag}:provenance")
        if rec.get("batch_id") != c.batch_id \
                or rec.get("generation_id") != f"{c.batch_id}/r{rec.get('run')}/{rec.get('scenario_id')}":
            probs.append(f"{tag}:identity")
        if rec.get("calls") != [] or rec.get("cost_usd_est") != 0 \
                or (rec.get("historical") or {}).get("label") != H.HISTORICAL_LABEL:
            probs.append(f"{tag}:claims_a_model_call_or_timing")
        try:
            cc = (rec.get("steps") or {}).get("compose_clinical")
            now = None if cc is None else H.input_sha256(cc)
        except StepError:
            now = None
        if now != g["input_sha256"]:
            probs.append(f"{tag}:input_sha256")
        s7 = c.step7(rec)
        if s7 is not None:
            ident = dict(frozen.get("renderer") or {})
            ident["name"] = ident.pop("renderer", None)             # the step output calls it "name"
            if s7.get("renderer") != ident:
                probs.append(f"{tag}:renderer_block")
    if probs:
        raise Refused(probs)
    return freeze_sha, frozen


# =================================================================================================
# the blind-read seal (G7; D9)
# =================================================================================================

def blind_read_possible(c: Candidate) -> bool:
    recs = {(r.get("run"), r.get("scenario_id")): r for r in c.records}
    return all(c.step7(recs.get((1, sid)) or {}) is not None for sid in MS.ITEMS)


def _seal_items(c: Candidate, sheets: Path, scored_by: str) -> tuple[list[dict], list[str]]:
    """Everything the seal binds, re-derived from the batch and the sheets folder; problems name files and fields."""
    probs: list[str] = []
    try:
        man = json.loads((sheets / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], ["MANIFEST.json:not_readable"]
    if man.get("batch_id") != c.batch_id or man.get("run") != 1:
        probs.append("MANIFEST.json:batch_or_run")
    if tuple(man.get("dimensions") or ()) != MS.DIMENSIONS:
        probs.append("MANIFEST.json:dimensions")
    if [i.get("scenario_id") for i in man.get("items") or []] != list(MS.ITEMS):
        probs.append("MANIFEST.json:items")
    names = sorted(p.name for p in (sheets / "scores").glob("*")) if (sheets / "scores").is_dir() else []
    if names != sorted(f"{sid}.json" for sid in MS.ITEMS):
        probs.append("scores:not_exactly_the_eight_items")
    recs = {(r.get("run"), r.get("scenario_id")): (rel, b, r) for rel, b, r in zip(c.rels, c.record_bytes, c.records)}
    by_item = {i.get("scenario_id"): i for i in man.get("items") or [] if isinstance(i, dict)}
    items = []
    for sid in MS.ITEMS:
        hit = recs.get((1, sid))
        if hit is None or c.step7(hit[2]) is None:
            probs.append(f"{sid}:no_step7_output")
            continue
        rel, rb, rec = hit
        body = json.dumps(rec["steps"]["compose_patient"]["sections"], sort_keys=True, ensure_ascii=False)
        sections_sha = sha_bytes(body.encode("utf-8"))
        mi = by_item.get(sid) or {}
        if mi.get("generation_id") != rec["generation_id"] or mi.get("sections_sha256") != sections_sha:
            probs.append(f"{sid}:manifest_item")
        try:
            sheet = (sheets / "sheets" / f"{sid}.md").read_bytes()
            score_b = (sheets / "scores" / f"{sid}.json").read_bytes()
            score = json.loads(score_b)
        except (OSError, ValueError):
            probs.append(f"{sid}:sheet_or_score_not_readable")
            continue
        if sheet != MS.render_sheet(rec).encode("utf-8"):
            probs.append(f"{sid}:sheet_differs_from_a_re-render")
        if score.get("generation_id") != rec["generation_id"]:
            probs.append(f"{sid}:score_generation_id")
        v = score.get("verdicts")
        if not isinstance(v, dict) or tuple(sorted(v)) != tuple(sorted(MS.DIMENSIONS)) \
                or any(x not in MS.VERDICTS for x in v.values()):
            probs.append(f"{sid}:verdicts")
            v = {}
        for k, x in v.items():
            if x in ("partial", "fail") and not any(isinstance(f, dict) and f.get("dimension") == k
                                                    for f in score.get("findings") or []):
                probs.append(f"{sid}:{k}:no_finding")
        by = score.get("scored_by")
        if not isinstance(by, str) or not by.startswith(SCORER) or not scored_by.startswith(SCORER) \
                or not by.startswith(scored_by):
            probs.append(f"{sid}:scored_by")
        items.append({"scenario_id": sid, "generation_id": rec["generation_id"], "record": rel,
                      "record_sha256": sha_bytes(rb), "sections_sha256": sections_sha,
                      "sheet_sha256": sha_bytes(sheet), "score_sha256": sha_bytes(score_b), "verdicts": v})
    return items, probs


def _aux_hashes(sheets: Path) -> dict:
    out = {}
    for name in ("MANIFEST.json", "README.md"):
        p = sheets / name
        out[name] = sha_bytes(p.read_bytes()) if p.is_file() else None
    return out


def seal(sheets: Path, c: Candidate, freeze_sha: str, scored_by: str, attest: bool) -> dict:
    if not attest:
        raise Refused("seal:not_attested")
    if (sheets / SEAL_FILE).exists():
        raise Refused("seal:already_sealed")
    early = [n for n in ("scores", OUT_DIR, LEDGER_DIR) if (c.dir / n).exists()]
    if early:
        raise Refused([f"seal:{n}_already_exists_in_the_batch" for n in early])
    items, probs = _seal_items(c, sheets, scored_by)
    if probs:
        raise Refused([f"seal:{p}" for p in probs])
    doc = {"seal_version": 1, **c.binding(freeze_sha), "run": 1, "items": items, **_aux_hashes(sheets),
           "scores_dir_absent": True, "scored_by": scored_by, "attestation": ATTESTATION,
           "sealed_at": _now()}
    (sheets / SEAL_FILE).write_text(dump(doc), encoding="utf-8")
    return doc


def seal_state(sheets: Path | None, c: Candidate, freeze_sha: str) -> tuple[str, dict | None]:
    """("valid", seal) | ("absent", None) | ("impossible", None). Raises Refused if a seal does not verify."""
    if not blind_read_possible(c):
        return "impossible", None
    if sheets is None or not (sheets / SEAL_FILE).is_file():
        return "absent", None
    doc = _read_json(sheets / SEAL_FILE)
    probs = [k for k, v in c.binding(freeze_sha).items() if doc.get(k) != v]
    if doc.get("attestation") != ATTESTATION or doc.get("scores_dir_absent") is not True:
        probs.append("attestation")
    items, more = _seal_items(c, sheets, str(doc.get("scored_by") or ""))
    probs += more
    if items != doc.get("items"):
        probs.append("items_changed_since_sealing")
    if {k: doc.get(k) for k in ("MANIFEST.json", "README.md")} != _aux_hashes(sheets):
        probs.append("manifest_or_readme_changed_since_sealing")
    if probs:
        raise Refused([f"seal:{p}" for p in probs])
    return "valid", doc


# =================================================================================================
# scoring, the review inventory and the rulings binding (G3)
# =================================================================================================

def inventory_of(scored: dict) -> dict:
    """Every review item of this batch, by scenario and key — no sentences."""
    inv: dict[str, dict] = {}
    for r in scored["patient"]["results"]:
        for it in r["review"]:
            e = inv.setdefault(r["scenario_id"], {}).setdefault(
                it["key"], {"kind": it["kind"], "heading": it["heading"], "token": it["token"], "generations": []})
            if r["generation_id"] not in e["generations"]:
                e["generations"].append(r["generation_id"])
    return {sid: dict(sorted(v.items())) for sid, v in sorted(inv.items())}


def _score(c: Candidate, store: Path) -> dict:
    try:
        return SB.score_batch(c.dir, adjudications_dir=store)
    except (SB.ScoreError, FileNotFoundError, ValueError) as exc:
        raise Refused(f"score:{type(exc).__name__}") from None


def _store_problems(store: Path, c: Candidate, freeze_sha: str) -> list[str]:
    try:
        meta = RS.load_store(store)
        probs = RS.check(store)
    except RS.StoreError as exc:
        return [f"store:{exc}"]
    probs = [f"store:{p}" for p in probs]
    probs += [f"store:{k}" for k, v in c.binding(freeze_sha).items() if meta.get(k) != v]
    return probs


def _patient_rulings(store: Path, sid: str) -> dict:
    p = store / f"{sid}.json"
    return (json.loads(p.read_text(encoding="utf-8")).get("patient_rulings") or {}) if p.is_file() else {}


def binding_state(store: Path, c: Candidate, freeze_sha: str, inventory: dict) -> tuple[str, dict | None]:
    """("bound", doc) | ("unbound", None). Raises Refused if a binding does not verify."""
    p = store / RULINGS_FILE
    if not p.is_file():
        return "unbound", None
    doc = _read_json(p)
    probs = [k for k, v in c.binding(freeze_sha).items() if doc.get(k) != v]
    if doc.get("inventory_sha256") != sha_bytes(canon(inventory)):
        probs.append("inventory_sha256")
    if not str(doc.get("ruled_by") or "").startswith(SCORER):
        probs.append("ruled_by")
    per = doc.get("per_scenario") or {}
    sids = {r.get("scenario_id") for r in c.records}
    if set(per) != sids:
        probs.append("per_scenario")
    for sid in sorted(sids & set(per)):
        pr = _patient_rulings(store, sid)
        if sha_bytes(canon(pr)) != per[sid].get("patient_rulings_sha256"):
            probs.append(f"{sid}:rulings_changed_since_binding")
        probs += [f"{sid}:ruled_key_not_in_this_batch" for k in pr if k not in (inventory.get(sid) or {})][:1]
    if probs:
        raise Refused([f"rulings:{p}" for p in probs])
    return "bound", doc


def score(c: Candidate, store: Path, sheets: Path | None, freeze_sha: str) -> dict:
    """The supported scoring path: the unchanged score_batch with the fresh store, reports, the inventory."""
    state, _ = seal_state(sheets, c, freeze_sha)
    if state == "absent":
        raise Refused("score:blind_read_not_sealed")
    probs = _store_problems(store, c, freeze_sha)
    if probs:
        raise Refused(probs)
    first = not (c.dir / "scores").exists()
    if first:
        if (store / RULINGS_FILE).exists() or any(_patient_rulings(store, r["scenario_id"]) for r in c.records):
            raise Refused("score:first_scoring_needs_an_empty_unbound_store")
    scored = _score(c, store)
    inv = inventory_of(scored)
    inv_path = c.dir / "scores" / INVENTORY_FILE
    if not first:
        if not inv_path.is_file() or _read_json(inv_path) != inv:
            raise Refused("score:inventory_differs_from_the_first_scoring")
        binding_state(store, c, freeze_sha, inv)
    SB.write_reports(c.dir, scored)
    inv_path.write_text(dump(inv), encoding="utf-8")
    return {"first": first, "scored": len(scored["patient"]["results"]),
            "review_items": sum(len(v) for v in inv.values()), "inventory_sha256": sha_bytes(canon(inv))}


def bind_rulings(c: Candidate, store: Path, freeze_sha: str, ruled_by: str, attest: bool) -> dict:
    if not attest or not ruled_by.startswith(SCORER):
        raise Refused("bind:not_attested_by_the_CSO")
    probs = _store_problems(store, c, freeze_sha)
    if probs:
        raise Refused(probs)
    inv_path = c.dir / "scores" / INVENTORY_FILE
    review = c.dir / "scores" / "review_patient.md"
    if not inv_path.is_file() or not review.is_file():
        raise Refused("bind:the_batch_has_not_been_scored")
    inv = _read_json(inv_path)
    if inv != inventory_of(_score(c, store)):
        raise Refused("bind:inventory_differs_from_a_re-score")
    per = {}
    for sid in sorted({r["scenario_id"] for r in c.records}):
        pr = _patient_rulings(store, sid)
        foreign = [k for k in pr if k not in (inv.get(sid) or {})]
        if foreign:
            probs.append(f"bind:{sid}:ruled_key_not_in_this_batch")
        probs += [f"bind:{sid}:ruling_value" for v in pr.values() if v not in RS.RULINGS][:1]
        per[sid] = {"n_items": len(inv.get(sid) or {}), "n_ruled": len(pr),
                    "patient_rulings_sha256": sha_bytes(canon(pr))}
    if probs:
        raise Refused(probs)
    p = store / RULINGS_FILE
    history = []
    if p.is_file():
        old = _read_json(p)
        history = (old.pop("history", None) or []) + [old]
    doc = {"rulings_version": 1, **c.binding(freeze_sha), "inventory_sha256": sha_bytes(canon(inv)),
           "review_file_sha256": sha_bytes(review.read_bytes()), "ruled_by": ruled_by, "ruled_at": _now(),
           "per_scenario": per, "history": history}
    p.write_text(dump(doc), encoding="utf-8")
    return doc


# =================================================================================================
# the ledger: lines, drops, the review (G4)
# =================================================================================================

def ledger_view(rec: dict, tables: RP.Tables) -> list[dict] | None:
    """One row per source line: its text (from the INPUT), its ledger entry and its C line. None if not rendered."""
    s7 = (rec.get("steps") or {}).get("compose_patient")
    inp = step7_input(rec)
    if s7 is None or inp is None:
        return None
    return _rows(inp, s7, tables)


def _rows(inp: dict, s7: dict, tables: RP.Tables) -> list[dict] | None:
    led = {(x.get("source_id"), x.get("line"), x.get("sentence")): x for x in s7["ledger"]["lines"]}
    saved = {s["heading"]: s for s in s7["sections"]}
    rows = []
    for sec in inp["sections"]:
        try:
            lines = RP.split_lines(sec)
        except RP.RenderRefusal:
            return None
        for u in lines:
            x = led.get((u["source_id"], u["line"], u["sentence"]))
            c_line = None
            if x is not None and x.get("c_line") is not None:
                c_sec = saved.get(RP.C_OF[sec["heading"]])
                body = c_sec["text"].split("\n") if c_sec else []
                lead = tables.lead_ins.get((RP.C_OF[sec["heading"]], inp["audience"])) or ""
                if lead and body and body[0] == lead:
                    body = body[1:]
                idx = x["c_line"]
                c_line = body[idx] if isinstance(idx, int) and 0 <= idx < len(body) else None
            rows.append({"heading": sec["heading"], "u": u, "x": x, "ops": (x or {}).get("ops") or [],
                         "c_line": c_line})
    return rows


def _drop_id(gid: str, u: dict, op: dict) -> str:
    return sha_bytes(f"{gid}|{u['source_id']}|{u['line']}|{u['sentence']}|{op['a']}|{op['b']}|{op.get('rule')}"
                     .encode("utf-8"))[:16]


def ledger_inventory(c: Candidate, tables: RP.Tables) -> tuple[list[dict], list[dict], list[dict]]:
    """(one entry per generation, one per drop, one per wholly dropped section) — hashes and counts, no text."""
    gens, drops, sections = [], [], []
    for rel, rb, rec in zip(c.rels, c.record_bytes, c.records):
        gid = rec["generation_id"]
        rows = ledger_view(rec, tables)
        g = {"generation_id": gid, "record": rel, "record_sha256": sha_bytes(rb)}
        if rows is None:
            gens.append(g | {"refused": (rec.get("step_errors") or {}).get("compose_patient") or "not_rendered",
                             "ledger_sha256": None, "n_lines": 0, "n_rendered": 0, "n_drops": 0,
                             "sections_without_a_rendered_line": []})
            continue
        by_sec: dict[str, list] = {}
        n_drops = 0
        for r in rows:
            by_sec.setdefault(r["u"]["source_id"], []).append(r)
            for op in r["ops"]:
                if op.get("op") == "drop":
                    n_drops += 1
                    drops.append({"drop_id": _drop_id(gid, r["u"], op), "generation_id": gid,
                                  "source_id": r["u"]["source_id"], "heading": r["heading"], "line": r["u"]["line"],
                                  "sentence": r["u"]["sentence"], "rule": op.get("rule"), "entry": op.get("entry"),
                                  "dropped_text_sha256": sha_bytes(r["u"]["text"][op["a"]:op["b"]].encode("utf-8"))})
        empty = sorted(sid for sid, rs in by_sec.items() if all(r["c_line"] is None for r in rs))
        sections += [{"section_key": f"{gid}|{sid}", "generation_id": gid, "source_id": sid} for sid in empty]
        gens.append(g | {"refused": None,
                         "ledger_sha256": sha_bytes(canon(rec["steps"]["compose_patient"]["ledger"])),
                         "n_lines": len(rows), "n_rendered": sum(r["c_line"] is not None for r in rows),
                         "n_drops": n_drops, "sections_without_a_rendered_line": empty})
    return gens, drops, sections


def ledger_template(c: Candidate, freeze_sha: str, tables: RP.Tables) -> dict:
    gens, drops, sections = ledger_inventory(c, tables)
    return {"ledger_review_version": 1,
            "binding": {**c.binding(freeze_sha), "inventory_sha256": sha_bytes(canon(gens)),
                        "reviewed_by": None, "reviewed_at": None},
            "omission_list": list(OMISSION_LIST),
            "generations": [g | {"acknowledged": None} for g in gens],
            "drops": [d | {"verdict": None, "category": None, "note": None} for d in drops],
            "sections": [s | {"verdict": None, "category": None, "note": None} for s in sections]}


def ledger_sheet_md(c: Candidate, tables: RP.Tables) -> str:
    """For Shina, AFTER the seal: every generation's lines with each dropped span marked. Holds A and C wording."""
    out = [f"# R1 drop ledger — batch `{c.batch_id}` (open only after the blind read is sealed)", "",
           "Review every generation against the omission list: " + "; ".join(OMISSION_LIST) + ".",
           "A dropped span is shown as [[…]]. Enter verdicts in ledger-review.json.", ""]
    for rec in c.records:
        gid = rec["generation_id"]
        rows = ledger_view(rec, tables)
        out += [f"## {gid}", ""]
        if rows is None:
            out += [f"Not rendered ({(rec.get('step_errors') or {}).get('compose_patient')}). Nothing to review.", ""]
            continue
        n = sum(1 for r in rows for op in r["ops"] if op.get("op") == "drop")
        out += [f"{len(rows)} lines, {sum(r['c_line'] is not None for r in rows)} rendered, {n} drops"
                + (" — no drops." if n == 0 else "."), ""]
        for r in rows:
            u, text = r["u"], r["u"]["text"]
            ids = [_drop_id(gid, u, op) for op in r["ops"] if op.get("op") == "drop"]
            if not ids:
                continue
            marked = "".join(f"[[{text[op['a']:op['b']]}]]" if op.get("op") == "drop" else text[op["a"]:op["b"]]
                             for op in r["ops"])
            out += [f"- {r['heading']} line {u['line']}" + (f" sentence {u['sentence']}" if u["sentence"] else "")
                    + f" — drops {', '.join(ids)}",
                    f"  - A: `{marked}`",
                    f"  - C: `{r['c_line']}`" if r["c_line"] is not None else "  - C: (line not written)", ""]
    return "\n".join(out) + "\n"


def ledger_condition(c: Candidate, freeze_sha: str, review_path: Path | None, tables: RP.Tables) -> dict:
    if review_path is None or not review_path.is_file():
        return {"status": UNDECIDED, "why": ["ledger review not entered"]}
    doc = _read_json(review_path)
    gens, drops, sections = ledger_inventory(c, tables)
    b = doc.get("binding") or {}
    probs = [k for k, v in c.binding(freeze_sha).items() if b.get(k) != v]
    if b.get("inventory_sha256") != sha_bytes(canon(gens)):
        probs.append("inventory_sha256")
    by_gen = {g.get("generation_id"): g for g in doc.get("generations") or [] if isinstance(g, dict)}
    by_drop = {d.get("drop_id"): d for d in doc.get("drops") or [] if isinstance(d, dict)}
    by_sec = {s.get("section_key"): s for s in doc.get("sections") or [] if isinstance(s, dict)}
    if len(by_gen) != len(doc.get("generations") or []) or set(by_gen) != {g["generation_id"] for g in gens}:
        probs.append("generations:not_exactly_this_batch")
    if len(by_drop) != len(doc.get("drops") or []) or set(by_drop) != {d["drop_id"] for d in drops}:
        probs.append("drops:not_exactly_this_batch")
    if len(by_sec) != len(doc.get("sections") or []) or set(by_sec) != {s["section_key"] for s in sections}:
        probs.append("sections:not_exactly_this_batch")
    for g in gens:
        mine = by_gen.get(g["generation_id"]) or {}
        if any(mine.get(k) != g[k] for k in ("record_sha256", "ledger_sha256", "n_drops")):
            probs.append(f"{g['generation_id']}:changed_since_the_review")
    for d in drops:
        if (by_drop.get(d["drop_id"]) or {}).get("dropped_text_sha256") != d["dropped_text_sha256"]:
            probs.append(f"{d['drop_id']}:dropped_text_changed")
    if probs:
        raise Refused([f"ledger:{p}" for p in probs])
    undecided, failed = [], []
    if not str(b.get("reviewed_by") or "").startswith(SCORER) or not b.get("reviewed_at"):
        undecided.append("reviewed_by / reviewed_at not entered")
    for g in gens:
        ack = by_gen[g["generation_id"]].get("acknowledged")
        if ack is not True:
            undecided.append(f"{g['generation_id']}: not acknowledged")
    for kind, mine, rows, key in (("drop", by_drop, drops, "drop_id"), ("section", by_sec, sections, "section_key")):
        for r in rows:
            v = mine[r[key]].get("verdict")
            if v == "required_dropped":
                failed.append(f"{kind} {r[key]} ({r['generation_id']}): required item dropped")
            elif v not in LEDGER_VERDICTS:
                undecided.append(f"{kind} {r[key]}: no verdict")
    status = NOT_MET if failed else UNDECIDED if undecided else MET
    return {"status": status, "why": failed + undecided, "generations": len(gens), "drops": len(drops),
            "sections_wholly_dropped": len(sections)}


# =================================================================================================
# G6 — cases, known failures, the diagnosis check
# =================================================================================================

def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, re.IGNORECASE))


def _inside(a: int, b: int, ops: list[dict], kind: str):
    return next((o for o in ops if o.get("op") == kind and o["a"] <= a and b <= o["b"]), None)


def own_withheld_coverage(text: str, a: int, b: int, tables: RP.Tables) -> bool:
    """The evaluator's OWN check (written separately from the renderer's drop_problem): every word of
    text[a:b] lies inside a match of an approved withheld entry that itself lies inside the span, or is
    one of the fixed filler words."""
    spans = [(s, e) for ent in tables.withheld for s, e in RP._entry_matches(ent, text) if a <= s and e <= b]
    words = [(m.start(), m.end(), m.group(0)) for m in RP.WORD.finditer(text, a, b)]
    return bool(words) and all(w.lower() in RP.FILLERS or any(s <= ws and we <= e for s, e in spans)
                               for ws, we, w in words)


def _content_left(text: str, start: int, ops: list[dict]) -> bool:
    for m in RP.WORD.finditer(text, start):
        w = m.group(0)
        if any(ch.isalpha() for ch in w) and w.lower() not in RP.FILLERS \
                and _inside(m.start(), m.end(), ops, "drop") is None:
            return True
    return False


def _own_partition(text: str, ops: list[dict]) -> bool:
    """Every character of the line belongs to exactly one op, in order (the evaluator's own check)."""
    pos = 0
    for o in ops:
        if not isinstance(o, dict) or o.get("op") not in ("copy", "sub", "drop") \
                or not isinstance(o.get("a"), int) or not isinstance(o.get("b"), int) \
                or o["a"] != pos or o["b"] <= o["a"]:
            return False
        pos = o["b"]
    return pos == len(text)


def _form_hit(span: str, ent) -> bool:
    forms = [f for f in ent.raw.get("forms") or [] if isinstance(f, str)]
    return span in forms if ent.raw.get("case_sensitive") else span.lower() in {f.lower() for f in forms}


def _own_sub_ok(text: str, o: dict, audience: str, tables: RP.Tables) -> bool:
    """An APPROVED substitution, judged from the table entry alone (written separately from the
    renderer's sub_out): glossary / drug -> the entry's plain words with the original term in brackets;
    phrase table -> that entry's fixed plain form for this audience (label, query, person, literal,
    inferred). A capital first letter is allowed only at the start of the line."""
    ent = tables.entry(o.get("table"), o.get("entry"))
    out = o.get("out")
    if ent is None or o.get("table") not in RP.SUB_TABLES or not isinstance(out, str):
        return False
    span = text[o["a"]:o["b"]]

    def is_form(want: str) -> bool:
        return out == want or (o["a"] == 0 and out == want[:1].upper() + want[1:])
    if o["table"] in ("glossary", "drug_classes"):
        return _form_hit(span, ent) and is_form(f"{ent.raw.get('plain')} ({span})")
    plain = (ent.raw.get("plain") or {}).get(audience)
    kind = ent.raw.get("kind")
    if not isinstance(plain, str):
        return False
    if kind == "label":
        m = RP.LABEL.fullmatch(span)
        return o["a"] == 0 and m is not None and _form_hit(m.group(1), ent) and out == plain
    if kind == "inferred":
        return RP.INFERRED_BRACKET.fullmatch(span) is not None and out == plain
    if kind == "query":
        return _form_hit(span, ent) and is_form(plain + " " if span == "?" else plain)
    return kind in ("person", "literal") and _form_hit(span, ent) and is_form(plain)


def _own_written(marker: str, text: str, ops: list[dict]) -> str | None:
    """The line as it must stand in C, written out from its ops by the evaluator itself (separately
    from the renderer's reproduce / _normalise): the copied pieces and the substitution outputs, in
    order; white space collapsed; no space before , ; : . ) or after (."""
    kept = "".join(text[o["a"]:o["b"]] if o["op"] == "copy" else o["out"] if o["op"] == "sub" else "" for o in ops)
    kept = re.sub(r"\s+", " ", kept)
    kept = re.sub(r" ([,;:.)])", r"\1", kept)
    kept = re.sub(r"\( ", "(", kept).strip()
    return marker + kept if kept else None


def _saved_c_lines(inp: dict, s7: dict, heading: str, tables: RP.Tables) -> list[str]:
    """The saved C section a given A heading is written into, as lines, without its lead-in."""
    c_heading = RP.C_OF[heading]
    sec = next((x for x in s7.get("sections") or [] if x.get("heading") == c_heading), None)
    body = str(sec.get("text") or "").split("\n") if sec else []
    lead = tables.lead_ins.get((c_heading, inp["audience"])) or ""
    return body[1:] if lead and body and body[0] == lead else body


def diagnosis_problems(rec: dict, tables: RP.Tables) -> list[str]:
    """Rounds 2-3: every diagnosis line of the INPUT, by kind, accounted for from the source to the
    SAVED C text (Codex 12 Nov 06:42). [] = every line accounted for. For each line: a ledger line;
    rendered; its ops cover every character exactly once; each substitution is an approved one; each
    drop is a confirmed `withheld` sub-line drop; a diagnosis word is left; and the saved C line at the
    ledger's index equals the line written out from those ops by the evaluator itself — so a C line
    changed under unchanged ops fails here, independently of audit() (G5)."""
    inp = step7_input(rec)
    s7 = (rec.get("steps") or {}).get("compose_patient")
    if inp is None or s7 is None:
        return []
    led = {(x.get("source_id"), x.get("line"), x.get("sentence")): x for x in s7["ledger"]["lines"]}
    c_lines = _saved_c_lines(inp, s7, "diagnosis", tables)
    used: set = set()
    probs = []
    for sec in inp["sections"]:
        if sec["heading"] != "diagnosis":
            continue
        for n, raw in enumerate(sec["text"].split("\n"), 1):          # the evaluator's own numbering
            s = raw.rstrip()
            if not s.strip():
                continue
            tag = f"{sec['section_id']}:{n}"
            x = led.get((sec["section_id"], n, None))
            if x is None:
                probs.append(f"{tag}:no_ledger_line")
                continue
            ops = x.get("ops") or []
            m = RP.LABEL.match(s)
            item = s.startswith("- ")
            text = s[2:] if item else s
            if not item and m is None:
                probs.append(f"{tag}:not_a_label_or_item")
                continue
            idx = x.get("c_line")
            if idx is None:
                probs.append(f"{tag}:not_rendered")
                continue
            if not _own_partition(text, ops):
                probs.append(f"{tag}:ops_do_not_cover_the_line")
                continue
            if not all(_own_sub_ok(text, o, inp["audience"], tables) for o in ops if o["op"] == "sub"):
                probs.append(f"{tag}:substitution_not_approved")
                continue
            placed = isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(c_lines)
            if not placed or idx in used or c_lines[idx] != _own_written("- " if item else "", text, ops):
                probs.append(f"{tag}:c_text_differs")            # the saved C line is not what the ops write
            if placed:
                used.add(idx)                                       # two source lines may not claim one C line
            drops = [o for o in ops if o.get("op") == "drop"]
            if not item and not s[m.end():].strip():                  # a standalone label: structure only
                ent = tables.entry(ops[0].get("table"), ops[0].get("entry")) if len(ops) == 1 else None
                if drops or len(ops) != 1 or ops[0].get("op") != "sub" or (ops[0]["a"], ops[0]["b"]) != (0, len(s)) \
                        or ent is None or ent.table != "phrases" or ent.raw.get("kind") != "label":
                    probs.append(f"{tag}:standalone_label_not_structural")
                continue
            start = 0 if item else RP.content_start(text, "label")
            for o in drops:
                if o.get("rule") != "withheld":
                    probs.append(f"{tag}:drop_rule_{o.get('rule')}")
                elif o["a"] < start or not own_withheld_coverage(text, o["a"], o["b"], tables):
                    probs.append(f"{tag}:drop_not_confirmed_withheld")
            if not _content_left(text, start, ops):
                probs.append(f"{tag}:no_diagnosis_word_left")
    return probs


def _keep_problems(term: str, rows: list[dict]) -> list[str]:
    probs = []
    for r in rows:
        text = r["u"]["text"]
        for m in re.finditer(term, text, re.IGNORECASE):
            tag = f"{r['u']['source_id']}:{r['u']['line']}"
            if r["c_line"] is None:
                probs.append(f"{tag}:line_not_rendered")
            elif _inside(m.start(), m.end(), r["ops"], "copy") is not None:
                continue
            else:
                sub = _inside(m.start(), m.end(), r["ops"], "sub")
                if sub is None or m.group(0).lower() not in str(sub.get("out") or "").lower():
                    probs.append(f"{tag}:term_not_kept")
    return probs


def _add_problems(token: str, not_from: str | None, copy_only: bool, rows: list[dict]) -> list[str]:
    probs = []
    for r in rows:
        if r["c_line"] is None:
            continue
        text = r["u"]["text"]
        allowed = sum(_count(token, text[o["a"]:o["b"]]) for o in r["ops"] if o.get("op") == "copy")
        if not copy_only:
            allowed += sum(_count(token, str(o.get("out") or "")) for o in r["ops"] if o.get("op") == "sub"
                           and not (not_from and re.search(not_from, text[o["a"]:o["b"]], re.IGNORECASE)))
        if _count(token, r["c_line"]) > allowed:
            probs.append(f"{r['u']['source_id']}:{r['u']['line']}:added")
    return probs


def _row_for(rows, source_id, line, sentence):
    return next((r for r in rows if (r["u"]["source_id"], r["u"]["line"], r["u"]["sentence"])
                 == (source_id, line, sentence)), None)


# The case list (evals/r1/cases.json, cases_version 1) — written and adopted BEFORE the run:
#   located[]    {case_id, category, record, source_id, heading, line, sentence, line_sha256, expected}
#   synthetic[]  {case_id, label, input: {audience, sections}, at: {source_id, line, sentence}, expected}
#   expected     {outcome: retained | approved_omission | refusal — anything else FAILS,
#                 code (refusal), drop | drops: {rule, entry} (approved_omission: each must be made, and no
#                 other drop on the line), whole (the line leaves C entirely), no_drop (retained),
#                 must_keep: [regex found in the A line and kept in C], c_must_match / c_must_not_match
#                 (the case's own C line), prose_must_match / prose_must_not_match (synthetic: all of C's prose)}
#   known_failures[] {kf_id, scenario_id, runs: "all" | [n], checks: [ADD | KEEP | DIAG | RULED | WITHHELD | BLIND]}
#   generations[]    {case_id, record, expected: "rendered", c_must_not_match}
OUTCOMES = ("retained", "approved_omission", "refusal")            # D3': the prewritten outcome types


def _refusal_problems(exp: dict, code, where: dict | None, source_id, line) -> list[str]:
    if code != exp.get("code"):
        return [f"refused_with_{code}"]
    if where and line is not None and (where.get("source_id"), where.get("line")) != (source_id, line):
        return ["refused_at_another_line"]
    return []


def _row_outcome(exp: dict, r: dict | None) -> list[str]:
    """A `retained` or `approved_omission` case at its prewritten outcome, checked on its ledger row
    and its saved C line. Used unchanged for located cases and synthetic controls (Codex 12 Nov 06:42):
    the omission's rule and entry, no drop that was not prewritten, `whole`, `no_drop`, `must_keep`,
    and the C-line patterns."""
    if r is None or r["x"] is None:
        return ["no_ledger_line"]
    probs = []
    drops = [o for o in r["ops"] if o.get("op") == "drop"]
    made = {(o.get("rule"), o.get("entry")) for o in drops}
    if exp.get("outcome") == "approved_omission":
        wanted = [d for d in ([exp.get("drop")] + list(exp.get("drops") or [])) if isinstance(d, dict)]
        want = {(d.get("rule"), d.get("entry")) for d in wanted}
        if not want or want - made:
            probs.append("expected_drop_not_made")
        if made - want:
            probs.append("unexpected_drop")
        if r["c_line"] is None and not exp.get("whole"):
            probs.append("line_not_rendered")
        if r["c_line"] is not None and exp.get("whole"):
            probs.append("line_rendered_but_a_whole_drop_was_expected")
    else:                                                           # "retained" — the caller has checked the outcome type
        if r["c_line"] is None:
            probs.append("line_not_rendered")
        if exp.get("no_drop") and drops:
            probs.append("unexpected_drop")
    for term in exp.get("must_keep") or []:
        kept = _keep_problems(term, [r])
        if not re.search(term, r["u"]["text"], re.IGNORECASE):
            probs.append("must_keep:term_not_in_the_line")            # a must_keep that matches nothing proves nothing
        probs += [f"must_keep:{p.rsplit(':', 1)[-1]}" for p in kept]
    for pat in exp.get("c_must_match") or []:
        if r["c_line"] is None or not re.search(pat, r["c_line"]):
            probs.append("c_must_match")
    for pat in exp.get("c_must_not_match") or []:
        if r["c_line"] is not None and re.search(pat, r["c_line"]):
            probs.append("c_must_not_match")
    return probs


def located_case(case: dict, c: Candidate, tables: RP.Tables) -> list[str]:
    """[] = at its expected outcome. Raises Refused if the case does not match the frozen input."""
    rec = c.by_rel.get(case.get("record"))
    inp = step7_input(rec) if rec else None
    sec = next((s for s in (inp or {}).get("sections", []) if s["section_id"] == case.get("source_id")), None)
    phys = sec["text"].split("\n") if sec else []
    n = case.get("line")
    if sec is None or not isinstance(n, int) or not 1 <= n <= len(phys) \
            or sha_bytes(phys[n - 1].rstrip().encode("utf-8")) != case.get("line_sha256"):
        raise Refused(f"cases:{case.get('case_id')}:does_not_match_the_frozen_input")
    exp = case.get("expected") or {}
    if exp.get("outcome") not in OUTCOMES:
        return ["expected_outcome_not_recognised"]
    refusal = rec.get("render_refusal")
    if exp.get("outcome") == "refusal":
        if refusal is None:
            return ["rendered_but_a_refusal_was_expected"]
        return _refusal_problems(exp, refusal.get("code"), refusal.get("where") or {}, case["source_id"], n)
    rows = ledger_view(rec, tables)
    if rows is None:
        return [f"generation_not_rendered:{(refusal or {}).get('code')}"]
    return _row_outcome(exp, _row_for(rows, case["source_id"], n, case.get("sentence")))


def _prose(s7: dict) -> str:
    return "\n".join(s["text"] for s in s7["sections"] if s["heading"] in RP.PROSE_C)


def synthetic_case(case: dict, tables: RP.Tables) -> list[str]:
    """An invented boundary control, rendered NOW by the frozen renderer (it is not one of the 90),
    held to the SAME prewritten outcome types as a located case (Codex 12 Nov 06:42): `refusal` (the
    code, and the line when `at` names one), or `retained` / `approved_omission` checked on the ledger
    row of the line `at` names — rule and entry of the omission, no other drop, `whole`, `no_drop`,
    `must_keep`, the C-line patterns — plus `prose_must_match` / `prose_must_not_match` on the whole of
    C's prose. An outcome outside the three, or a retained / omission control with no `at`, FAILS."""
    exp = case.get("expected") or {}
    at = case.get("at") if isinstance(case.get("at"), dict) else {}
    if exp.get("outcome") not in OUTCOMES:
        return ["expected_outcome_not_recognised"]
    try:
        out = RP.render_patient(case.get("input"), tables)
    except RP.RenderRefusal as exc:
        if exp.get("outcome") != "refusal":
            return [f"refused_with_{exc.code}"]
        return _refusal_problems(exp, exc.code, exc.where or {}, at.get("source_id"), at.get("line"))
    if exp.get("outcome") == "refusal":
        return ["rendered_but_a_refusal_was_expected"]
    try:
        if RP.audit(case["input"], out, tables):
            return ["audit_not_clean"]
    except Exception as exc:                                        # fail closed: a control whose audit cannot run fails
        return [f"audit_raised:{type(exc).__name__}"]
    if not at:
        return ["no_line_named"]
    rows = _rows(case["input"], out, tables) or []
    probs = _row_outcome(exp, _row_for(rows, at.get("source_id"), at.get("line"), at.get("sentence")))
    text = _prose(out)
    probs += ["prose_must_match" for p in exp.get("prose_must_match") or [] if not re.search(p, text)]
    probs += ["prose_must_not_match" for p in exp.get("prose_must_not_match") or [] if re.search(p, text)]
    return probs


def known_failure(kf: dict, c: Candidate, tables: RP.Tables, review_rows: dict, seal_doc: dict | None,
                  bound: bool) -> tuple[list[str], list[str]]:
    """(failed, undecided) — each names the check and the generation."""
    failed, undecided = [], []
    runs = kf.get("runs", "all")
    recs = [r for r in c.records if r.get("scenario_id") == kf.get("scenario_id")
            and (runs == "all" or r.get("run") in runs)]
    if not recs:
        return [f"{kf.get('kf_id')}: no generation of {kf.get('scenario_id')} in the batch"], []
    for rec in recs:
        gid = rec["generation_id"]
        rows = ledger_view(rec, tables)
        for chk in kf.get("checks") or []:
            t = chk.get("type")
            tag = f"{kf.get('kf_id')} {t} {gid}"
            if t == "BLIND":
                item = next((i for i in (seal_doc or {}).get("items") or [] if i["scenario_id"] == chk.get("item")), None)
                if rec.get("run") != 1:
                    continue
                if item is None:
                    undecided.append(f"{tag}: decided by the blind read")
                elif item["verdicts"].get(chk.get("dimension")) != "pass":
                    failed.append(f"{tag}: blind read {chk.get('dimension')} not pass")
                continue
            if rows is None:
                failed.append(f"{tag}: generation not rendered")
                continue
            if t == "RULED":
                mine = [x for x in review_rows.get(gid, []) if x["kind"] in chk.get("kinds", [])
                        and x["heading"] in chk.get("c_headings", [])]
                if any(x["ruling"] == "defect" for x in mine):
                    failed.append(f"{tag}: a flag ruled defect")
                elif mine and (not bound or any(x["ruling"] is None for x in mine)):
                    undecided.append(f"{tag}: flags not yet ruled")
                continue
            if t == "DIAG":
                failed += [f"{tag}: {p}" for p in diagnosis_problems(rec, tables)]
                continue
            if t == "WITHHELD":
                text = _prose(rec["steps"]["compose_patient"])
                if any(RP._entry_matches(ent, text) for ent in tables.withheld):
                    failed.append(f"{tag}: withheld wording in C")
                sens = [x for x in review_rows.get(gid, []) if x["kind"] == "sensitive"]
                if any(x["ruling"] == "defect" for x in sens):
                    failed.append(f"{tag}: a sensitive flag ruled defect")
                elif sens and (not bound or any(x["ruling"] is None for x in sens)):
                    undecided.append(f"{tag}: sensitive flags not yet ruled")
                continue
            scope = [r for r in rows if not chk.get("a_headings") or r["heading"] in chk["a_headings"]]
            if t == "KEEP":
                failed += [f"{tag}: {p}" for p in _keep_problems(chk["term"], scope)]
            elif t == "ADD":
                failed += [f"{tag}: {p}" for p in _add_problems(chk["token"], chk.get("not_from"),
                                                                bool(chk.get("copy_only")), scope)]
            else:
                failed.append(f"{tag}: check type not recognised")
    return failed, undecided


def load_cases(path: Path, c: Candidate) -> dict:
    cases = _read_json(path)
    if not isinstance(cases, dict) or cases.get("cases_version") != CASES_VERSION:
        raise Refused("cases:version")
    if cases.get("inputs_sha256") != c.derived.get("inputs_sha256"):
        raise Refused("cases:written_for_other_inputs")
    return cases


def cases_condition(cases: dict, c: Candidate, tables: RP.Tables, review_rows: dict, seal_doc: dict | None,
                    bound: bool) -> dict:
    failed, undecided = [], []
    for case in cases.get("located") or []:
        failed += [f"{case.get('case_id')}: {p}" for p in located_case(case, c, tables)]
    for case in cases.get("synthetic") or []:
        failed += [f"{case.get('case_id')} (synthetic): {p}" for p in synthetic_case(case, tables)]
    for g in cases.get("generations") or []:
        rec = c.by_rel.get(g.get("record"))
        s7 = c.step7(rec) if rec else None
        if rec is None:
            raise Refused(f"cases:{g.get('case_id')}:no_such_record")
        if g.get("expected") != "rendered":
            failed.append(f"{g.get('case_id')}: expected_outcome_not_recognised")
        elif s7 is None:
            failed.append(f"{g.get('case_id')}: not rendered")
        elif any(re.search(p, _prose(s7), re.IGNORECASE) for p in g.get("c_must_not_match") or []):
            failed.append(f"{g.get('case_id')}: c_must_not_match")
    for kf in cases.get("known_failures") or []:
        f, u = known_failure(kf, c, tables, review_rows, seal_doc, bound)
        failed += f
        undecided += u
    for rec in c.records:                                           # L8 (D8): every diagnosis line, every generation
        failed += [f"diagnosis {rec['generation_id']}: {p}" for p in diagnosis_problems(rec, tables)]
    return {"status": NOT_MET if failed else UNDECIDED if undecided else MET, "why": failed + undecided,
            "located": len(cases.get("located") or []), "synthetic": len(cases.get("synthetic") or []),
            "known_failures": len(cases.get("known_failures") or [])}


# =================================================================================================
# the code-decided conditions (G1, G2, G5, G8)
# =================================================================================================

def rendered_condition(c: Candidate) -> dict:
    refusals = [f"{r['generation_id']}: {(r.get('step_errors') or {}).get('compose_patient')}"
                for r in c.records if c.step7(r) is None]
    n = len(c.index["generations"])
    return {"status": MET if not refusals and len(c.records) == n else NOT_MET, "why": refusals,
            "rendered": len(c.records) - len(refusals), "of": n}


def code_lines(scored: dict) -> dict:
    """The code-decided bar lines from the scorer's SUMMARY FIELDS (never its printed verdicts)."""
    sm = scored["patient"]["summary"]
    res = scored["patient"]["results"]
    n = sm["n_records"]
    full, asm = sm["n_scored"] == n, sm["n_assembled"] == n
    fk_bad = [r["generation_id"] for r in res if r["fk_prose"] is None or r["fk_prose"] > SP.RA.D5_PASS_MAX]
    h = sm["header"]
    lines = {
        "1 reached step 7": full,
        "2 boxes exact, no unknown source ids": sm["boxes_correct"] == n and sm["unknown_source_ids"] == 0,
        "3 no signpost outside the box, gate on assembled A + C": full and asm and sm["signpost_outside_box"] == 0
        and sm["gate"]["pass"] == n,
        "5 (code) no resus text": full and sm["resus_text"] == 0,
        "6 (code) no year added": full and sm["year_added"] == 0,
        "7 FK <= 8 on every generation": full and not fk_bad and sm["fk"]["prose_pass"] == n,
        "8 translation header, no non-English": asm and h["expected_present"] == sm["header_expected_records"]
        and h["unexpected_present"] == 0 and sm["non_english"]["decided"] == 0,
        "9 (code) sources, 'Not documented', follow-up, title": full and asm and sm["unknown_source_ids"] == 0
        and sm["not_documented_text"] == 0 and sm["follow_up_missing"] == 0 and sm["title_ok"] == n,
        "11 (code) HAZ-03 on assembled C": asm and sm["haz03_c"] == n,
        "13 no prose without a source": full and sm["prose_without_source"] == 0,
    }
    why = [k for k, ok in lines.items() if not ok] + [f"FK above 8 or missing: {g}" for g in fk_bad]
    return {"status": MET if all(lines.values()) else NOT_MET, "why": why,
            "lines": {k: (MET if ok else NOT_MET) for k, ok in lines.items()}}


def rulings_condition(scored: dict, bound: bool) -> dict:
    rows = [x | {"generation_id": r["generation_id"]} for r in scored["patient"]["results"] for x in r["review"]]
    defects = sorted({f"{x['generation_id']}: {x['kind']} at {x['heading']} ruled defect" for x in rows
                      if x["ruling"] == "defect"}) if bound else []
    pending = sum(1 for x in rows if x["ruling"] is None)
    why = list(defects)
    if not bound:
        why.append("rulings not bound to this candidate (RULINGS.json) — nothing counts as ruled")
    elif pending:
        why.append(f"{pending} flag(s) pending")
    status = NOT_MET if defects else UNDECIDED if (not bound or pending) else MET
    return {"status": status, "why": why, "flagged": len(rows), "defects": len(defects), "pending": pending}


def audit_condition(c: Candidate, tables: RP.Tables) -> dict:
    why = []
    for rec in c.records:
        s7 = c.step7(rec)
        if s7 is None:
            continue
        try:
            probs = RP.audit(step7_input(rec), s7, tables)
        except Exception as exc:                                    # a check that cannot run fails closed
            probs = [f"audit_raised:{type(exc).__name__}"]
        why += [f"{rec['generation_id']}: {p}" for p in probs]
    return {"status": NOT_MET if why else MET, "why": why}


def _run_files(d: Path) -> dict:
    out = {}
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d)
        if p.is_file() and rel.parts[0] not in NOT_PART_OF_THE_RUN:
            out[rel.as_posix()] = sha_bytes(p.read_bytes())
    return out


def determinism_condition(c: Candidate, run2: Path | None) -> dict:
    if run2 is None or not run2.is_dir():
        return {"status": UNDECIDED, "why": ["run 2 not present"]}
    if run2.resolve() == c.dir:
        return {"status": NOT_MET, "why": ["run 2 is the same folder as run 1"]}
    a, b = _run_files(c.dir), _run_files(run2.resolve())
    why = [f"only in run 1: {k}" for k in sorted(set(a) - set(b))] + \
          [f"only in run 2: {k}" for k in sorted(set(b) - set(a))] + \
          [f"differs: {k}" for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    if not (run2 / "run.json").is_file():
        why.append("run 2 has no run.json")
    return {"status": NOT_MET if why else MET, "why": why, "files_compared": len(a)}


def blind_condition(state: str, seal_doc: dict | None) -> dict:
    if state == "impossible":
        return {"status": NOT_MET, "why": ["no blind read possible: a blind-read item has no step-7 output"]}
    bad = [f"{i['scenario_id']}: {d} {v}" for i in seal_doc["items"] for d, v in sorted(i["verdicts"].items())
           if v != "pass"]
    return {"status": NOT_MET if bad else MET, "why": bad,
            "items_passing": sum(all(v == "pass" for v in i["verdicts"].values()) for i in seal_doc["items"]),
            "of": len(MS.ITEMS)}


# =================================================================================================
# evaluate
# =================================================================================================

def verdict_of(conditions: dict) -> str:
    statuses = [v["status"] for v in conditions.values()]
    return STOP if NOT_MET in statuses else INCOMPLETE if UNDECIDED in statuses else GO


def per_generation(c: Candidate, scored: dict, tables: RP.Tables) -> list[dict]:
    by = {r["generation_id"]: r for r in scored["patient"]["results"]}
    rows = []
    for rec in c.records:
        s7, r = c.step7(rec), by.get(rec["generation_id"])
        flags: dict[str, int] = {}
        for x in (r or {}).get("review", []):
            flags[x["kind"]] = flags.get(x["kind"], 0) + 1
        rows.append({"generation_id": rec["generation_id"], "rendered": s7 is not None,
                     "refusal": (rec.get("render_refusal") or {}).get("code"),
                     "fk_prose": (r or {}).get("fk_prose"),
                     "drops": ((s7 or {}).get("ledger") or {}).get("counts", {}).get("drops_by_rule"),
                     "flags": dict(sorted(flags.items()))})
    return rows


def evaluate(batch: Path, *, run2: Path | None, store: Path, sheets: Path | None, freeze: Path = DEFAULT_FREEZE,
             inputs: Path | None = None, ledger_review: Path | None = None) -> dict:
    """The whole decision. Returns the report; raises Refused."""
    c = Candidate(batch)
    inputs = inputs if inputs is not None else ROOT / FZ.MEASUREMENT_INPUTS
    freeze_sha, frozen = integrity(c, freeze, inputs)
    report = {"evaluator_version": EVALUATOR_VERSION, "batch_id": c.batch_id, "source": c.source_label(),
              "generations": len(c.index["generations"]), "freeze_sha256": freeze_sha, "line_12": LINE_12,
              "line_11_upstream": "the 5b / D4 / step 2 / step 6 parts of line 11 are the source batch's own "
                                  "(historical) — not claimed"}
    state, seal_doc = seal_state(sheets, c, freeze_sha)
    if state == "absent":
        return report | {"verdict": INCOMPLETE, "conditions": {},
                         "note": "blind read not sealed; nothing else evaluated (D9)"}
    tables = RP.default_tables()                  # loaded once per process, against the pinned hashes
    probs = _store_problems(store, c, freeze_sha)
    if probs:
        raise Refused(probs)
    scored = _score(c, store)
    bstate, _bdoc = binding_state(store, c, freeze_sha, inventory_of(scored))
    bound = bstate == "bound"
    cases = load_cases(FZ.cases_path(frozen), c)
    review_rows = {r["generation_id"]: r["review"] for r in scored["patient"]["results"]}
    ledger_path = ledger_review if ledger_review is not None else c.dir / LEDGER_DIR / "ledger-review.json"
    conditions = {
        "G1 every generation rendered": rendered_condition(c),
        "G2 code-decided bar lines": code_lines(scored),
        "G3 every flag ruled in the fresh store": rulings_condition(scored, bound),
        "G4 drop ledger reviewed, no required item dropped": ledger_condition(c, freeze_sha, ledger_path, tables),
        "G5 audit() clean on every record": audit_condition(c, tables),
        "G6 cases at their expected outcome, known failures absent": cases_condition(cases, c, tables, review_rows,
                                                                                     seal_doc, bound),
        "G7 blind read 8/8": blind_condition(state, seal_doc),
        "G8 determinism (run 2 byte-identical except run.json)": determinism_condition(c, run2),
    }
    return report | {"verdict": verdict_of(conditions), "conditions": conditions,
                     "blind_read": "sealed" if state == "valid" else "no blind read possible",
                     "per_generation": per_generation(c, scored, tables)}


def report_md(rep: dict) -> str:
    out = [f"# R1 go / stop — batch `{rep['batch_id']}`", "", f"**{rep['verdict']}**", "",
           f"Source: {rep['source']} · {rep['generations']} generations · freeze `{rep['freeze_sha256'][:12]}…`", ""]
    if rep.get("note"):
        out += [rep["note"], ""]
    if rep["conditions"]:
        out += ["| Condition | Status | Not met / undecided |", "|---|---|---|"]
        for name, cnd in rep["conditions"].items():
            why = cnd.get("why") or []
            out.append(f"| {name} | {cnd['status']} | {'; '.join(why[:6])}{' …' if len(why) > 6 else ''} |")
        out += ["", f"- {rep['line_12']}", f"- {rep['line_11_upstream']}", ""]
    if rep.get("per_generation"):
        out += ["| Generation | Rendered | FK | Drops | Flags |", "|---|---|---|---|---|"]
        out += [f"| {g['generation_id']} | {'yes' if g['rendered'] else g['refusal']} | {g['fk_prose']} | "
                f"{json.dumps(g['drops']) if g['drops'] else '-'} | {json.dumps(g['flags']) if g['flags'] else '-'} |"
                for g in rep["per_generation"]]
        out.append("")
    return "\n".join(out) + "\n"


def print_report(rep: dict, file=None) -> None:
    file = file or sys.stdout
    print(f"source: {rep['source']}", file=file)
    for name, cnd in rep["conditions"].items():
        print(f"  {cnd['status']:<9} {name}", file=file)
        for w in (cnd.get("why") or [])[:8]:
            print(f"            - {w}", file=file)
        if len(cnd.get("why") or []) > 8:
            print(f"            - … {len(cnd['why']) - 8} more (GO-STOP.json)", file=file)
    print(f"  {rep['line_12']}", file=file)
    fixture = "" if rep["source"].endswith("(MEASUREMENT)") else " (on a fixture — NOT the measurement)"
    if rep.get("note"):
        print(f"{rep['verdict']} — {rep['note']}{fixture}", file=file)
    else:
        print(f"{rep['verdict']}{fixture}", file=file)


# =================================================================================================
# command line
# =================================================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, store=True, sheets=True):
        p.add_argument("--batch", type=Path, required=True)
        p.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
        p.add_argument("--inputs", type=Path, default=None)
        if store:
            p.add_argument("--store", type=Path, required=True)
        if sheets:
            p.add_argument("--sheets", type=Path, required=True)

    pf = sub.add_parser("freeze")
    pf.add_argument("--cases", type=Path, default=ROOT / FZ.MEASUREMENT_CASES)
    pf.add_argument("--out", type=Path, default=DEFAULT_FREEZE)
    ps = sub.add_parser("seal")
    common(ps, store=False)
    ps.add_argument("--scored-by", required=True)
    ps.add_argument("--attest", action="store_true")
    pc = sub.add_parser("score")
    common(pc)
    pb = sub.add_parser("bind-rulings")
    common(pb, sheets=False)
    pb.add_argument("--ruled-by", required=True)
    pb.add_argument("--attest", action="store_true")
    pl = sub.add_parser("ledger-sheet")
    common(pl, store=False)
    pe = sub.add_parser("evaluate")
    common(pe)
    pe.add_argument("--run2", type=Path, default=None)
    pe.add_argument("--ledger-review", type=Path, default=None)
    pe.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    c = None
    try:
        if a.cmd == "freeze":
            sha = FZ.write(a.out, FZ.make(a.cases))
            print(f"freeze record -> {a.out} (sha256 {sha})")
            return 0
        if a.cmd == "evaluate":
            rep = evaluate(a.batch, run2=a.run2, store=a.store, sheets=a.sheets, freeze=a.freeze, inputs=a.inputs,
                           ledger_review=a.ledger_review)
            if rep["conditions"]:
                out = a.out if a.out is not None else a.batch / OUT_DIR
                out.mkdir(parents=True, exist_ok=True)
                (out / "GO-STOP.json").write_text(dump(rep), encoding="utf-8")
                (out / "GO-STOP.md").write_text(report_md(rep), encoding="utf-8")
            print_report(rep)
            return EXIT[rep["verdict"]]
        c = Candidate(a.batch)
        inputs = a.inputs if a.inputs is not None else ROOT / FZ.MEASUREMENT_INPUTS
        freeze_sha, _frozen = integrity(c, a.freeze, inputs)
        if a.cmd == "seal":
            doc = seal(a.sheets, c, freeze_sha, a.scored_by, a.attest)
            print(f"sealed: {len(doc['items'])} items of {c.batch_id} -> {a.sheets / SEAL_FILE}")
        elif a.cmd == "score":
            s = score(c, a.store, a.sheets, freeze_sha)
            print(f"scored {s['scored']} generations with the fresh store; {s['review_items']} review item(s); "
                  f"inventory sha256 {s['inventory_sha256']} (reports in scores/ — after the blind read only)")
        elif a.cmd == "bind-rulings":
            doc = bind_rulings(c, a.store, freeze_sha, a.ruled_by, a.attest)
            print(f"rulings bound: {sum(v['n_ruled'] for v in doc['per_scenario'].values())} ruled of "
                  f"{sum(v['n_items'] for v in doc['per_scenario'].values())} item(s) -> {a.store / RULINGS_FILE}")
        elif a.cmd == "ledger-sheet":
            state, _ = seal_state(a.sheets, c, freeze_sha)
            if state == "absent":
                raise Refused("ledger-sheet:blind_read_not_sealed")
            out = c.dir / LEDGER_DIR
            if out.exists():
                raise Refused("ledger-sheet:already_written — never written over")
            tables = RP.default_tables()
            tpl = ledger_template(c, freeze_sha, tables)
            out.mkdir()
            (out / "LEDGER-REVIEW.md").write_text(ledger_sheet_md(c, tables), encoding="utf-8")
            (out / "ledger-review.json").write_text(dump(tpl), encoding="utf-8")
            print(f"ledger sheet: {len(tpl['generations'])} generations, {len(tpl['drops'])} drops, "
                  f"{len(tpl['sections'])} wholly dropped section(s) -> {out}")
        return 0
    except Refused as exc:
        if c is not None:
            print(f"source: {c.source_label()}", file=sys.stderr)
        for p in exc.problems[:20]:
            print(f"  - {p}", file=sys.stderr)
        print(REFUSED, file=sys.stderr)
        return EXIT[REFUSED]
    except (FZ.FreezeError, RS.StoreError, RP.RenderRefusal) as exc:
        print(f"  - {getattr(exc, 'code', None) or exc}", file=sys.stderr)
        print(REFUSED, file=sys.stderr)
        return EXIT[REFUSED]


if __name__ == "__main__":
    sys.exit(main())
