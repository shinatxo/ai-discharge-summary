"""Offline end-to-end replay of the ORCHESTRATOR on a saved pipeline batch (WS1a step build
piece 5, round 4, 5 Nov 2026). No model, no network, no AWS, no timing claims.

    python evals/replay_orchestrate.py <batch dir> --out DIR

For every record in the batch, `pipeline.orchestrate.run` runs the whole order — 1, 2, 3,
3b, 4, 5b, 5a || 6, pre-flight, 7 beside 8a, 8b — with a step runner that substitutes the
batch's SAVED outputs for the four model steps at the run_step boundary, and runs every
code step for real. Each record is ACCOUNTED FOR: a released bundle, or a fixed code and
no document.

DISCLOSED — the step-2 input is RECONSTRUCTED. A batch record keeps step 2's call block and
step 3's VALIDATED facts, never step 2's raw tool output. The replay rebuilds that wire form
from step 3's saved output (removing the keys step 3 adds: fact_id, citation_status, reason,
verified, field_check, medications_check) and step 3 then runs for real on it. Two limits:
  - a citation step 3 dropped as quote_too_long kept no quote text; the reconstruction puts a
    201-character placeholder there so step 3 reaches the same verdict (quote "" +
    quote_too_long) — counted and reported;
  - step 3's output is compared with the saved one: records that differ ONLY by the R3
    change (an over-long quote now cleared on every path, CSO 5 Nov 2026) are reported
    separately from any other difference.
The four model steps are substituted, so no Bedrock request is made: model_calls is 0 here
and request accounting, throttling, the deadline and cancellation are evidenced by the
unit tests, not by this replay. Real timing against the 300 s Timeout is checklist item 7.

Also checked per record: the orchestrator's bundle equals 8a/8b run DIRECTLY on the saved
inputs (piece 4's replay path) — document hashes, gate, warnings; the outcome and gate
status equal the committed piece-4 replay (assemble.json); the re-run code steps equal the
saved ones; the trace has 11 step items + META, no raw line index, no raw notes string, the
notes text nowhere, every quote <= 200 characters; item sizes against DynamoDB's 400 KB.

Writes REPLAY.md and replay.json to --out: counts, codes, sizes, hashes — never note or
output text.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "generate"))
import run_cold_eval as rce  # noqa: E402
import score_assemble as SA  # noqa: E402
from pipeline import StepError, run_step  # noqa: E402
from pipeline import orchestrate as O  # noqa: E402
from pipeline.schemas import wire_facts  # noqa: E402
from pipeline.validate import QUOTE_MAX_CHARS  # noqa: E402

MODEL_STEPS = ("reconcile_medications", "compose_clinical", "compose_patient")
CODE_STEPS_COMPARED = ("guard_input", "retrieve_evidence", "route_resus", "select_safety_net")
PLACEHOLDER_QUOTE = "#" * (QUOTE_MAX_CHARS + 1)
RESULTS_ITEM_MAX_BYTES = 400_000


# --- the reconstruction ----------------------------------------------------------------

def _raw_cite(c: dict, stats: Counter) -> dict:
    quote = c.get("quote", "")
    if c.get("reason") == "quote_too_long":
        stats["quote_too_long_placeholders"] += 1
        quote = PLACEHOLDER_QUOTE
    return {"lines": list(c.get("lines") or []), "quote": quote}


def _raw_item(item: dict, stats: Counter) -> dict:
    out = {k: v for k, v in item.items() if k not in ("fact_id", "citation_status", "reason")}
    out["cites"] = [_raw_cite(c, stats) for c in item.get("cites", [])]
    return out


def reconstruct_wire(validated: dict, stats: Counter) -> dict:
    """Step 3's saved grouped output -> the record_facts wire form step 2 returned."""
    v = copy.deepcopy(validated)
    grouped = {
        "fields": {n: {"status": f["status"], "items": [_raw_item(i, stats) for i in f["items"]]}
                   for n, f in v["fields"].items()},
        "medications": {"pre_admission": [_raw_item(i, stats) for i in v["medications"]["pre_admission"]],
                        "discharge": [_raw_item(i, stats) for i in v["medications"]["discharge"]],
                        "discharge_status": v["medications"]["discharge_status"]},
        "documented_advice": [_raw_item(i, stats) for i in v["documented_advice"]],
        "contradictions": [_raw_item(i, stats) for i in v["contradictions"]],
        "age_group": v["age_group"],
        "suspicious_text": [_raw_cite(c, stats) for c in v["suspicious_text"]],
    }
    r = {k: val for k, val in v["resus"].items() if k not in ("fact_id", "citation_status", "reason")}
    r["cites"] = [_raw_cite(c, stats) for c in r.get("cites", [])]
    grouped["resus"] = r
    return wire_facts(grouped)


def _clear_long_quotes(obj):
    """The R3 view of a saved object: every quote > 200 characters cleared."""
    if isinstance(obj, dict):
        return {k: ("" if k == "quote" and isinstance(v, str) and len(v) > QUOTE_MAX_CHARS
                    else _clear_long_quotes(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clear_long_quotes(x) for x in obj]
    return obj


# --- the run ----------------------------------------------------------------------------

class Writer:
    def __init__(self):
        self.items = []

    def put(self, item):
        assert item["SK"] not in {i["SK"] for i in self.items}
        self.items.append(item)


def _walk(o):
    yield o
    if isinstance(o, dict):
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def strings(o):
    """Every string value AND key in a JSON-shaped object, unescaped. Checks for note text
    must look here, not in json.dumps output: JSON escaping turns the notes' newlines into
    \\n, so a multi-line notes string could never match a dump (review 21:28, 5 Nov 2026)."""
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)


def notes_text_present(items, notes: str) -> bool:
    return any(notes in s for i in items for s in strings(i))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def replay_record(rec: dict, notes: str) -> dict:
    s = rec["steps"]
    out = {"scenario": rec["scenario_id"], "run": rec["run"]}
    stats = Counter()
    wire = reconstruct_wire(s["validate_facts"]["facts"], stats)
    out["quote_too_long_placeholders"] = stats["quote_too_long_placeholders"]

    real = {}

    def runner(name, inp, *, ctx=None):
        if name == "extract_facts":
            return {"facts": copy.deepcopy(wire), "call": s["extract_facts"]["call"]}   # reconstructed
        if name in MODEL_STEPS:
            return json.loads(json.dumps(s[name]))                                       # saved
        res = run_step(name, inp, ctx=ctx)                                               # real code
        real[name] = res                       # unscrubbed, for the comparisons below
        return res

    started = datetime.fromisoformat(rec["started_at"]).timestamp()
    w, fails = Writer(), []
    o = O.run(notes, generation_id=rec["generation_id"].replace("/", "_"), clients={90: None, 60: None}, model_id=rec["model_id"],
              trace=w, deadline=float(O.DEADLINE_S), generated_on=rec["started_at"][:10], on_failure=fails.append,
              now=lambda: 0.0, wall=lambda: started, step_runner=runner)
    out["outcome"] = "bundle" if o.ok else "rejected"
    out["code"] = None if o.ok else o.failure.code
    out["failed_step"] = None if o.ok else o.failure.step
    out["callback_calls"] = len(fails)
    out["model_calls"] = o.model_calls
    out["released_without_bundle"] = bool(o.ok and not o.bundle)
    out["document_on_failure"] = bool((not o.ok) and (o.bundle or o.stored))

    # step 3 re-run on the reconstructed input vs saved — UNSCRUBBED (the reconstruction check)
    traced = {i["step_name"]: i for i in w.items if "step_name" in i}
    v_new = real.get("validate_facts")
    if v_new is None:
        out["validate_vs_saved"] = "not_run"
    elif v_new == s["validate_facts"]:
        out["validate_vs_saved"] = "identical"
    elif v_new == _clear_long_quotes(s["validate_facts"]):
        out["validate_vs_saved"] = "r3_only"
    else:
        out["validate_vs_saved"] = "other"

    # the other re-run code steps vs saved (unscrubbed; the saved guard record never had the index)
    def _cmp(name):
        new = {k: v for k, v in real[name].items() if k != "lines"} if name == "guard_input" else real[name]
        return new != s[name]
    out["code_steps_differ"] = [n for n in CODE_STEPS_COMPARED if n in real and _cmp(n)]
    if "guard_input" in real:
        out["line_index_matches_count"] = len(real["guard_input"]["lines"]) == s["guard_input"]["line_count"]

    # trace checks
    out["trace_sks_ok"] = (sorted(i["SK"] for i in w.items) == sorted(
        [f"STEP#{O.STEPS[n].seq}#{n}#A1" for n in (
            "guard_input", "extract_facts", "validate_facts", "retrieve_evidence", "route_resus",
            "select_safety_net", "reconcile_medications", "compose_clinical", "compose_patient",
            "assemble_clinical", "assemble_patient")] + ["META"])) if o.ok else None
    raw_index = raw_notes = long_quote = 0
    for i in w.items:
        for node in _walk(i):
            if isinstance(node, dict):
                raw_index += O._is_raw_index(node)
                raw_notes += isinstance(node.get("notes"), str)
                q = node.get("quote")
                long_quote += isinstance(q, str) and len(q) > QUOTE_MAX_CHARS
    dumps = [json.dumps(i, ensure_ascii=False) for i in w.items]
    out["trace"] = {"items": len(w.items), "raw_index": raw_index, "raw_notes_strings": raw_notes,
                    "quotes_over_200": long_quote, "notes_text_present": notes_text_present(w.items, notes),
                    "max_item_bytes": max(len(d.encode()) for d in dumps),
                    "total_bytes": sum(len(d.encode()) for d in dumps)}

    if o.ok:
        b = o.bundle
        out["hashes"] = b["hashes"]
        out["gate"] = b["gate"]
        out["warnings"] = len(b["warnings"])
        stored_quotes = [n["quote"] for n in _walk(o.stored) if isinstance(n, dict) and isinstance(n.get("quote"), str)]
        out["stored_quotes_over_200"] = sum(len(q) > QUOTE_MAX_CHARS for q in stored_quotes)
        # ResultsTable item size, computed as app._write_results does
        item = {"summary": b["part_a"], "gp_letter": b["part_b"], "patient": b["part_c"],
                "units_json": json.dumps(o.stored["units"], ensure_ascii=False),
                "templates_json": json.dumps(o.stored["templates"], ensure_ascii=False),
                "warnings_json": json.dumps(o.stored["warnings"], ensure_ascii=False),
                "output_sha256_json": json.dumps(o.stored["hashes"])}
        out["results_item_bytes"] = sum(len(k) + len(json.dumps({"S": v}).encode()) for k, v in item.items()) + 400

        # the same bundle by piece 4's direct path (8a/8b on the SAVED inputs)
        inp = SA.inputs_8a(rec)
        g = run_step("guard_input", {"notes": notes})
        try:
            o8a = run_step("assemble_clinical", inp)
            d = run_step("assemble_patient", inp | {"lines": g["lines"], "patient": s["compose_patient"],
                                                    "clinical": o8a})
            out["direct_8b_equal"] = (d["hashes"] == b["hashes"] and d["gate"] == b["gate"]
                                      and d["warnings"] == b["warnings"])
        except StepError as e:
            out["direct_8b_equal"] = f"direct path rejected: {e.code}"
    return out


def run(batch: Path, piece4: Path | None) -> dict:
    manifest = json.loads((batch / "manifest.json").read_text())
    corpus, corpus_sha = rce.load_corpus(rce.DEFAULT_CORPUS_PATH)
    if corpus_sha != manifest["corpus"]["sha256"]:
        raise SystemExit("the corpus has changed since this batch was generated")
    by_id = {sc.id: sc for sc in corpus}
    p4 = {}
    if piece4 and piece4.exists():
        for r in json.loads(piece4.read_text())["results"]:
            p4[(r["scenario"], r["run"])] = r
    results = []
    for path in sorted(batch.glob("r*/*.json")):
        rec = json.loads(path.read_text())
        sc = by_id[rec["scenario_id"]]
        if rec["notes_sha256"] != sc.notes_sha256:
            raise SystemExit(f"{rec['generation_id']}: notes_sha256 does not match the corpus")
        r = replay_record(rec, sc.notes)
        q = p4.get((r["scenario"], r["run"]))
        if q is not None:
            r["piece4_outcome_equal"] = (q["outcome"] == r["outcome"])
            r["piece4_gate_equal"] = (q.get("gate") == r.get("gate"))
        results.append(r)
    return {"batch": batch.name, "pipeline_version": O.pipeline_version(), "results": results,
            "summary": summarise(results)}


def summarise(results: list[dict]) -> dict:
    ok = [r for r in results if r["outcome"] == "bundle"]
    return {
        "records": len(results),
        "accounted_for": sum(r["outcome"] in ("bundle", "rejected") for r in results),
        "bundles": len(ok),
        "rejections": dict(Counter(f"{r['failed_step']}:{r['code']}" for r in results if r["outcome"] == "rejected")),
        "document_on_failure": sum(r["document_on_failure"] for r in results),
        "released_without_bundle": sum(r["released_without_bundle"] for r in results),
        "callback_on_success": sum(r["callback_calls"] for r in ok),
        "model_calls_total": sum(r["model_calls"] for r in results),
        "validate_vs_saved": dict(Counter(r["validate_vs_saved"] for r in results)),
        "quote_too_long_placeholders": sum(r["quote_too_long_placeholders"] for r in results),
        "code_steps_differ": dict(Counter(n for r in results for n in r["code_steps_differ"])),
        "r3_records": sorted(f"{r['scenario']} r{r['run']}" for r in results if r["validate_vs_saved"] == "r3_only"),
        "trace_sks_ok": sum(bool(r["trace_sks_ok"]) for r in ok),
        "trace_raw_index": sum(r["trace"]["raw_index"] for r in results),
        "trace_raw_notes_strings": sum(r["trace"]["raw_notes_strings"] for r in results),
        "trace_quotes_over_200": sum(r["trace"]["quotes_over_200"] for r in results),
        "trace_notes_text_present": sum(r["trace"]["notes_text_present"] for r in results),
        "trace_max_item_bytes": max(r["trace"]["max_item_bytes"] for r in results),
        "trace_max_total_bytes": max(r["trace"]["total_bytes"] for r in results),
        "stored_quotes_over_200": sum(r.get("stored_quotes_over_200", 0) for r in ok),
        "results_item_max_bytes": max((r["results_item_bytes"] for r in ok), default=0),
        "gate": dict(Counter(r["gate"]["status"] for r in ok)),
        "direct_8b_equal": sum(r.get("direct_8b_equal") is True for r in ok),
        "direct_8b_not_equal": [f"{r['scenario']} r{r['run']}: {r.get('direct_8b_equal')}"
                                for r in ok if r.get("direct_8b_equal") is not True],
        "piece4_outcome_equal": sum(r.get("piece4_outcome_equal") is True for r in results),
        "piece4_gate_equal": sum(r.get("piece4_gate_equal") is True for r in results),
        "piece4_compared": sum("piece4_outcome_equal" in r for r in results),
    }


def write(out: Path, rep: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "replay.json").write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    s = rep["summary"]
    lines = [
        f"# Orchestrator replay — `{rep['batch']}`", "",
        f"pipeline_version `{rep['pipeline_version']}`. Offline: saved model outputs at the run_step "
        "boundary, step-2 input RECONSTRUCTED from step 3's saved output (see the module docstring); "
        "every code step re-run; no Bedrock request (model_calls "
        f"{s['model_calls_total']}); no timing claims (item 7).", "",
        f"- **Records accounted for: {s['accounted_for']}/{s['records']}** — {s['bundles']} released bundles, "
        f"rejections {s['rejections'] or 0}; a document on a failure: {s['document_on_failure']}; "
        f"a release without a bundle: {s['released_without_bundle']}; failure callbacks on a success: "
        f"{s['callback_on_success']}.",
        f"- Gate on assembled A + C: {s['gate']}.",
        f"- Same bundle as 8a/8b run directly on the saved inputs (piece 4's path): "
        f"{s['direct_8b_equal']}/{s['bundles']}" + (f" — not equal: {s['direct_8b_not_equal']}" if s['direct_8b_not_equal'] else "") + ".",
        f"- Committed piece-4 replay: outcome equal {s['piece4_outcome_equal']}/{s['piece4_compared']}, "
        f"gate equal {s['piece4_gate_equal']}/{s['piece4_compared']}.",
        f"- Step 3 re-run on the reconstructed input vs saved: {s['validate_vs_saved']} "
        f"(quote_too_long placeholders: {s['quote_too_long_placeholders']}; R3-only records: "
        f"{', '.join(s['r3_records']) or 'none'}). Other re-run code steps "
        f"differing from saved: {s['code_steps_differ'] or 0}.",
        f"- Trace: complete (11 step items + META) {s['trace_sks_ok']}/{s['bundles']}; raw line index "
        f"{s['trace_raw_index']}; raw notes strings {s['trace_raw_notes_strings']}; notes text present "
        f"{s['trace_notes_text_present']}; quotes > 200 {s['trace_quotes_over_200']}; largest item "
        f"{s['trace_max_item_bytes']:,} bytes; largest run {s['trace_max_total_bytes']:,} bytes.",
        f"- Stored units: quotes > 200 {s['stored_quotes_over_200']}; largest ResultsTable item "
        f"~{s['results_item_max_bytes']:,} bytes (limit {RESULTS_ITEM_MAX_BYTES:,}).", "",
    ]
    (out / "REPLAY.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Offline replay of the orchestrator on a saved batch.")
    p.add_argument("batch", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--piece4", type=Path, default=None,
                   help="a committed piece-4 assemble.json to compare outcome and gate with")
    a = p.parse_args(argv)
    rep = run(a.batch, a.piece4)
    write(a.out, rep)
    print((a.out / "REPLAY.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
