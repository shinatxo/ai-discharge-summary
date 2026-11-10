#!/usr/bin/env python3
"""Item 3 R1 — the offline harness for the constrained PART C renderer (D6; the R1 build record,
session 3 Rounds 1-2, adopted by Shina (author/CSO) 10 Nov 2026 07:54).

Two commands, no model call, no network:

  python evals/render_offline.py inputs <batch> --out <inputs.json>
      Per generation of a saved pipeline batch: the record's path and sha256, and the sha256 of
      its step-7 input (the canonical JSON of compose_patient.patient_input(steps.compose_clinical));
      null + a reason when there is no step 6. Run on the 90 only at step 4/5 and committed before
      the run (D3').

  python evals/render_offline.py render <batch> --out <dir> --inputs <inputs.json> --batch-id <id> [--dev]
      A DERIVED batch in the standard record layout, scored later by the unchanged
      score_batch.score_batch(<dir>, adjudications_dir=<fresh store>):
        <dir>/r<n>/<ID>.json  every source record (90 in, 90 out, same order — Codex 07:38:27 P2),
                              with steps.compose_patient REPLACED by the renderer's output, or
                              REMOVED on a refusal (no historical PART C anywhere in the record)
                              with the fixed code in step_errors / failed_step / error
        <dir>/batch.json      every source index entry, same order
        <dir>/manifest.json   the source manifest; batch_id; prompts.compose_patient -> the
                              renderer identity (score_batch keys step-7 scoring on that key);
                              derived_from; historical.created_at
        <dir>/render.json     COUNTS ONLY: records, rendered, refused, upstream_failed (D9: refusal
                              codes stay in the records, opened after the blind read)
        <dir>/run.json        the run's start / finish time — the ONLY file with a timestamp, so
                              two runs into two folders are byte-identical but for run.json (D3')
      Refuses: <dir> exists, is the source or inside it; a batch id from outside
      [A-Za-z0-9._-]; not a pipeline batch; an index record path that is not a standard relative
      r<n>/<id>.json, resolves outside the source, or repeats; the inputs file not matching the batch
      (every record, every hash); tables not "approved" unless --dev; --dev on the measurement batch;
      any source file changed after the check (nothing is written).
      The source is read ONCE: the inputs check, the rendering and the provenance all use those
      bytes (Codex 09:04:06 P2 x2).

What each derived record says about itself: `calls` = [] and `cost_usd_est` = 0 (step 7 made no
model call); the source's calls (minus step 7 / 7v), started_at, elapsed_s and cost_usd_est move
under `historical`, labelled — steps 1-6, 5a and 5b are the p0.4 batch's own outputs, not the
candidate's; line 12 (latency) is not claimed. `render_provenance` names the source record, its
sha256 and the input sha256. The console prints counts only.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline import compose_patient as CP7  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402
from pipeline.errors import StepError  # noqa: E402

HARNESS_VERSION = 1
INPUTS_VERSION = 1
MEASUREMENT_BATCH = "w6-pipeline-e09-m01-c02-p04-x5"     # D3': the 90 saved p0.4 records
PATH = "steps-1-5b-5a-6-historical-7-renderer-R1"
STEP7_KEYS = ("compose_patient", "verify_patient", "compose_patient_first", "verify_patient_2", "patient_held_back")
UPSTREAM_FAILED = "upstream_failed"
HISTORICAL_LABEL = "historical — from the source batch, not the candidate"
LONDON = ZoneInfo("Europe/London")
_BATCH_ID = re.compile(r"[A-Za-z0-9._-]+")


class HarnessError(ValueError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def input_sha256(compose_clinical: dict) -> str:
    inp = CP7.patient_input(compose_clinical)
    return _sha(json.dumps(inp, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _load_batch(src: Path) -> tuple[dict, dict, bytes, bytes]:
    try:
        mb = (src / "manifest.json").read_bytes()
        bb = (src / "batch.json").read_bytes()
        manifest, index = json.loads(mb), json.loads(bb)
    except (OSError, ValueError):
        raise HarnessError("not a readable batch (manifest.json / batch.json)") from None
    if manifest.get("generator") != "pipeline":
        raise HarnessError("not a pipeline batch")
    if not isinstance(index.get("generations"), list):
        raise HarnessError("batch.json has no generations list")
    return manifest, index, mb, bb


# A standard record path, as run_pipeline_eval writes it: r<run>/<scenario_id>.json (Codex 09:04:06 P2).
_RECORD_PATH = re.compile(r"r[0-9]+/[A-Za-z0-9][A-Za-z0-9._-]*\.json")


def _record_paths(src: Path, index: dict) -> list[str]:
    """Each index entry's record path, checked before anything is read or written: a standard
    relative path, unique (case-insensitively), and resolving inside the source batch."""
    rels: list[str] = []
    for g in index["generations"]:
        rel = g.get("record") if isinstance(g, dict) else None
        if not isinstance(rel, str) or not _RECORD_PATH.fullmatch(rel):
            raise HarnessError("an index entry's record is not a standard relative path (r<n>/<id>.json)")
        if src not in (src / rel).resolve().parents:
            raise HarnessError("an index entry's record resolves outside the source batch")
        rels.append(rel)
    if len({r.lower() for r in rels}) != len(rels):
        raise HarnessError("an index entry's record appears more than once")
    return rels


def _read_source(src: Path) -> tuple[dict, dict, bytes, bytes, list[str], list[bytes]]:
    """The source batch read ONCE: manifest, index, their bytes, the checked record paths and each
    record's bytes. Everything downstream (inputs, rendering, provenance) uses these bytes."""
    manifest, index, mb, bb = _load_batch(src)
    rels = _record_paths(src, index)
    try:
        rbs = [(src / rel).read_bytes() for rel in rels]
    except OSError:
        raise HarnessError("a record listed in batch.json cannot be read") from None
    return manifest, index, mb, bb, rels, rbs


def _inputs_from(manifest: dict, mb: bytes, bb: bytes, rels: list[str], rbs: list[bytes]) -> dict:
    gens = []
    for rel, rb in zip(rels, rbs):
        rec = json.loads(rb)
        cc = (rec.get("steps") or {}).get("compose_clinical")
        sha, reason = None, None
        if cc is None:
            reason = UPSTREAM_FAILED
        else:
            try:
                sha = input_sha256(cc)
            except StepError as exc:
                reason = exc.code
        gens.append({"record": rel, "record_sha256": _sha(rb), "input_sha256": sha, "reason": reason})
    return {"inputs_version": INPUTS_VERSION, "source_batch": manifest["batch_id"],
            "manifest_sha256": _sha(mb), "batch_sha256": _sha(bb), "generations": gens}


def make_inputs(src: Path) -> dict:
    src = src.resolve()
    manifest, _index, mb, bb, rels, rbs = _read_source(src)
    return _inputs_from(manifest, mb, bb, rels, rbs)


def _source_unchanged(src: Path, mb: bytes, bb: bytes, rels: list[str], rbs: list[bytes]) -> bool:
    """True if every source file still holds the bytes that passed the frozen-input check."""
    try:
        now = [(src / "manifest.json").read_bytes(), (src / "batch.json").read_bytes()] + \
              [(src / rel).read_bytes() for rel in rels]
    except OSError:
        return False
    return [_sha(b) for b in now] == [_sha(b) for b in [mb, bb] + rbs]


def _renderer_identity(tables: RP.Tables) -> dict:
    return {"renderer": RP.RENDERER_NAME, "version": RP.RENDERER_VERSION, "module_sha256": RP.module_sha256(),
            "tables": dict(sorted(tables.sha256.items())), "tables_status": tables.status}


def _pipeline_version() -> str:
    from pipeline import orchestrate as O
    return O.pipeline_version()


def render_batch(src: Path, out: Path, inputs_path: Path, batch_id: str, *, dev: bool = False,
                 tables: RP.Tables | None = None, now=None) -> dict:
    src, out = src.resolve(), out.resolve()
    if not _BATCH_ID.fullmatch(batch_id or ""):
        raise HarnessError("batch id may contain only letters, digits, '.', '_' and '-'")
    if out.exists():
        raise HarnessError("output directory already exists — never written over")
    if out == src or src in out.parents:
        raise HarnessError("output must not be the source batch or inside it")
    manifest, index, mb, bb, rels, rbs = _read_source(src)
    if dev and (manifest.get("batch_id") == MEASUREMENT_BATCH or src.name == MEASUREMENT_BATCH):
        raise HarnessError("--dev is refused on the measurement batch")
    tables = tables if tables is not None else RP.load_tables()
    if tables.status != "approved" and not dev:
        raise HarnessError("tables are not approved (stub) — only --dev may use them")
    try:
        ib = inputs_path.read_bytes()
        inputs = json.loads(ib)
    except (OSError, ValueError):
        raise HarnessError("inputs file not readable") from None
    expected = _inputs_from(manifest, mb, bb, rels, rbs)           # from the bytes read once (P2, 09:04:06)
    if inputs != expected:
        raise HarnessError("the inputs file does not match this batch (records or hashes changed)")

    clock = now or (lambda: dt.datetime.now(LONDON))
    started = clock().isoformat(timespec="seconds")
    ident = _renderer_identity(tables)
    pv = _pipeline_version()
    records: list[tuple[str, dict]] = []
    entries = []
    counts = {"records": 0, "rendered": 0, "refused": 0, "upstream_failed": 0}
    for rel, rb, gi in zip(rels, rbs, inputs["generations"]):
        rec = json.loads(rb)                                        # the validated bytes, never re-read
        d = copy.deepcopy(rec)
        steps = d.get("steps") or {}
        for k in STEP7_KEYS:
            steps.pop(k, None)
        d["steps"] = steps
        src_errors = rec.get("step_errors") or {}
        errors = {k: v for k, v in src_errors.items() if k not in STEP7_KEYS}
        hist_calls = [c for c in (rec.get("calls") or []) if c.get("name") not in STEP7_KEYS]
        d["historical"] = {"label": HISTORICAL_LABEL, "calls": hist_calls, "started_at": rec.get("started_at"),
                           "elapsed_s": rec.get("elapsed_s"), "cost_usd_est": rec.get("cost_usd_est")}
        for k in ("started_at", "elapsed_s"):
            d.pop(k, None)
        d["calls"] = []
        d["cost_usd_est"] = 0.0
        d["batch_id"] = batch_id
        d["generation_id"] = f"{batch_id}/r{rec['run']}/{rec['scenario_id']}"
        d["path"] = PATH
        prompts = {k: v for k, v in (rec.get("prompts") or {}).items() if k not in STEP7_KEYS}
        prompts["compose_patient"] = ident
        d["prompts"] = prompts
        d.pop("render_refusal", None)
        counts["records"] += 1
        cc = steps.get("compose_clinical")
        if cc is None:
            errors["compose_patient"] = UPSTREAM_FAILED
            counts["upstream_failed"] += 1
        else:
            try:
                inp = CP7.patient_input(cc)
                result = json.loads(json.dumps(RP.render_patient(inp, tables), allow_nan=False))
            except StepError as exc:
                errors["compose_patient"] = exc.code
                d["render_refusal"] = {"code": exc.code, "where": getattr(exc, "where", None)}
                counts["refused"] += 1
            else:
                steps["compose_patient"] = result
                counts["rendered"] += 1
        first = next(iter(errors), None)
        d["step_errors"] = errors
        d["failed_step"] = first
        d["error"] = {"code": errors[first], "message": ""} if first else None
        d["render_provenance"] = {"from_record": rel, "from_record_sha256": _sha(rb),
                                  "input_sha256": gi["input_sha256"]}
        records.append((rel, d))
        entries.append({"run": rec["run"], "scenario_id": rec["scenario_id"], "record": rel,
                        "failed_step": d["failed_step"], "error_code": (d["error"] or {}).get("code"),
                        "cost_usd_est": 0.0})

    m = {k: v for k, v in manifest.items() if k != "created_at"}
    m["batch_id"] = batch_id
    m["path"] = PATH
    mp = {k: v for k, v in (manifest.get("prompts") or {}).items() if k not in STEP7_KEYS}
    mp["compose_patient"] = ident
    m["prompts"] = mp
    m["historical"] = {"label": HISTORICAL_LABEL, "created_at": manifest.get("created_at")}
    m["derived_from"] = {"kind": "offline_renderer", "harness_version": HARNESS_VERSION,
                         "from_batch": manifest["batch_id"], "manifest_sha256": _sha(mb), "batch_sha256": _sha(bb),
                         "inputs_sha256": _sha(ib), "renderer": ident, "pipeline_version": pv,
                         "model_calls": 0, "dev": bool(dev)}
    summary = {"batch_id": batch_id, **counts}

    # Nothing is published if the source changed after the frozen-input check (Codex 09:04:06 P2).
    if not _source_unchanged(src, mb, bb, rels, rbs):
        raise HarnessError("the source batch changed during the run — nothing written")
    # Writes stay inside <dir>: every rel matched _RECORD_PATH (no "/" start, no "..", one r<n>/ folder)
    # and <dir> is created fresh below, so it holds no link a write could follow.
    out.mkdir(parents=True)
    for rel, d in records:
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_dump(d), encoding="utf-8")
    (out / "batch.json").write_text(_dump({"batch_id": batch_id, "stopped_early": index.get("stopped_early", False),
                                           "generations": entries}), encoding="utf-8")
    (out / "manifest.json").write_text(_dump(m), encoding="utf-8")
    (out / "render.json").write_text(_dump(summary), encoding="utf-8")
    (out / "run.json").write_text(_dump({"started_at": started,
                                         "finished_at": clock().isoformat(timespec="seconds")}), encoding="utf-8")
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("inputs")
    pi.add_argument("batch", type=Path)
    pi.add_argument("--out", type=Path, required=True)
    pr = sub.add_parser("render")
    pr.add_argument("batch", type=Path)
    pr.add_argument("--out", type=Path, required=True)
    pr.add_argument("--inputs", type=Path, required=True)
    pr.add_argument("--batch-id", required=True)
    pr.add_argument("--dev", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.cmd == "inputs":
            if a.out.exists():
                raise HarnessError("inputs file already exists — never written over")
            data = make_inputs(a.batch)
            a.out.write_text(_dump(data), encoding="utf-8")
            n = len(data["generations"])
            missing = sum(1 for g in data["generations"] if g["input_sha256"] is None)
            print(f"inputs: {n} generations, {n - missing} with a step-7 input, {missing} without -> {a.out}")
            return 0
        s = render_batch(a.batch, a.out, a.inputs, a.batch_id, dev=a.dev)
    except (HarnessError, RP.RenderRefusal, FileNotFoundError, KeyError) as exc:
        print(f"refused: {getattr(exc, 'code', None) or exc}", file=sys.stderr)
        return 2
    print(f"{s['records']} records: {s['rendered']} rendered, {s['refused']} refused, "
          f"{s['upstream_failed']} without step 6 -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
