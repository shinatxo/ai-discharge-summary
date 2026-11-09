"""Export ONE saved piece-7 run into the committed worked example (WS1a step build piece 8, DoD 7;
9 Nov 2026). Reads the operator's saved evidence only — no AWS, no network, no model.

    python evals/export_worked_example.py [--evidence ~/eph-20261107] [--out DIR]

Run by the operator (Shina) on the machine holding ~/eph-20261107, in the d313 environment with
PYTHONDONTWRITEBYTECODE=1. Prints fixed codes, counts and hashes only — never note, document or
model-output text, never user_sub or the idempotency key.

The run is fixed by evals/check_worked_example.BINDING (scenario, full job id, snapshot,
deployed commit, pipeline_version, and the five source files' byte hashes from a read-only
probe run BEFORE this exporter existed). Every step re-checks; the first failure STOPs with a
fixed code. Nothing is ever written to --out on a STOP; every STOP before step 6 writes nothing
at all; a STOP at step 6's re-check (`recheck`) KEEPS <out>.partial for inspection, and that
folder blocks another export (`out_exists`) until the operator removes it:
  1. manifest: exactly one attempt for the bound job — the bound scenario, met, checked at the
     bound snapshot, which is the latest complete snapshot, with nothing unresolved;
     preflight.json's commit = the bound commit;
  2. source bytes = the bound hashes (trace.json, audit.json, results.json, check.json,
     _snapshot.json); the stripped corpus notes hash = the bound notes hash = the attempt's;
  3. projections by ALLOW-LIST: audit row → 19 fields (dropped exactly PK, SK, user_sub,
     idempotency_key — any other attribute STOPs for review); ResultsTable item → 11 fields
     (dropped exactly PK, SK, user_sub, ttl); values unchanged (JSON-string fields kept as the
     same strings); audit status complete;
  4. trace.json copied BYTE FOR BYTE (export hash = source hash);
  5. privacy before any write to --out: the checker's detectors over every file and every
     embedded JSON, plus the actual user_sub and idempotency key (read from the source audit
     row, never printed) absent from every output byte;
  6. written to <out>.partial, re-checked by evals/check_worked_example.check — every code must
     pass except `readme`, which waits only for the CSO read placeholder — then renamed to --out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_worked_example as C  # noqa: E402

SOURCE_FILES = ("trace.json", "audit.json", "results.json", "check.json", "_snapshot.json")


class Stop(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _need(code: str, ok) -> None:
    if ok is not True:
        raise Stop(code)


def _dump(obj) -> bytes:
    return (json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _load(p: Path):
    return json.loads(p.read_bytes())


def _snapshots(d: Path) -> list[str]:
    return sorted((p.name for p in d.glob("snap-*") if p.is_dir()), key=lambda x: int(x.split("-")[1]))


def _ms_after(t0: float, iso: str) -> int:
    return round((C._epoch(iso) - t0) * 1000)


def build_readme(binding: dict, trace: list, trail: dict, audit: dict, notes_lines: int) -> str:
    s = C._by_sk(trace)
    meta = s["META"]
    t0 = C._epoch(meta["started_at"])
    calls = {}
    for r in meta.get("requests") or []:
        calls[r.get("step")] = calls.get(r.get("step"), 0) + 1
    rows = []
    for sk in C.STEP_SKS:
        it = s[sk]
        rows.append(f"| `{sk}` | {it['kind']} | {it['status']} | {_ms_after(t0, it['started_at'])} | "
                    f"{it['latency_ms']} | {calls.get(it['step_name'], 0)} |")
    starts = [_ms_after(t0, s[sk]["started_at"]) for sk in C.STEP_SKS]
    ties = len(starts) - len(set(starts))
    t3, t6, t7 = trail["step3_facts_to_lines"], trail["step6_sections_to_facts"], trail["step7_sections_to_a_sections"]
    reasons: dict = {}
    for u in t3["unverified_cites"]:
        reasons[u["reason"]] = reasons.get(u["reason"], 0) + 1
    # one line followed end to end — IDs only (the trace holds the text)
    st3 = s["STEP#03#validate_facts#A1"]["output"]["facts"]
    status = {fid: it for fid, it, _ in C._step3_items(st3)}
    walk_a = None
    for sec in s["STEP#06#compose_clinical#A1"]["output"]["sections"]:
        if sec["part"] != "part_a" or sec["heading"] in C.STEP6_BOXES:
            continue
        for fid in sec["fact_ids"]:
            it = status.get(fid)
            good = [c for c in (it or {}).get("cites", []) if c.get("verified") is True]
            if it and it.get("citation_status") == "verified" and good:
                walk_a = (sec["section_id"], sec["heading"], fid, good[0]["lines"])
                break
        if walk_a:
            break
    walk_c = next(((x["heading"], x["source_ids"]) for x in s["STEP#07#compose_patient#A1"]["output"]["sections"]
                   if x["source_ids"]), None)
    h = audit["output_sha256"]
    L = []
    L += [f"# Worked example — one synthetic generation, end to end (scenario {binding['scenario']})", ""]
    L += ["WS1a step build piece 8 · DoD 7 (*\"one worked example followed end to end\"*) · exported 9 Nov 2026.", ""]
    L += ["## What this is", ""]
    L += ["This is a synthetic example. "
          f"Scenario `{binding['scenario']}` from the committed synthetic corpus `src/canary/scenarios.json` "
          f"was run once through the deployed pipeline on the ephemeral stack `{binding['stack_name']}` "
          f"(deployed from `{binding['deployed_commit'][:7]}`, 7 Nov 2026; torn down 7–8 Nov) as job "
          f"`{binding['job_id']}`, and exported here from its saved trace (piece 7 evidence, `{binding['snapshot']}`). "
          "Traces expire after 30 days and only an operator can read them (ADR-009 (b)); this folder keeps one "
          "synthetic run readable.", ""]
    L += ["## What this is not", ""]
    L += ["- It is not clinical validation, evidence of safety, or a quality verdict. Scored batches and their bars "
          "are in `evals/EVAL_RESULTS.md` and the step-build records.",
          "- It is not real patient data, and not an incident export. On a real deployment a trace may be used "
          "only to investigate its own generation (ADR-009 (b), DPIA).",
          "- All three documents are drafts for clinician review. "
          "PART C is a draft flagged for clinician review: WS1a checklist item 3 (compose_patient) is NOT MET.",
          "- Structural consistency does not establish faithful clinical meaning.", ""]
    L += ["## Identity", "", "| | |", "|---|---|",
          f"| scenario | `{binding['scenario']}` (notes by reference: sha256 `{binding['notes_sha256']}`, {notes_lines} lines) |",
          f"| job / generation | `{binding['job_id']}` |",
          f"| deployed commit | `{binding['deployed_commit']}` |",
          f"| pipeline_version | `{binding['pipeline_version']}` |",
          f"| model | `{meta.get('model_id')}` · {meta.get('model_calls')} requests · "
          f"{meta.get('input_tokens')} input / {meta.get('output_tokens')} output tokens |",
          f"| run | {meta['started_at']} → {meta['completed_at']} (UTC) |", ""]
    L += ["## How to verify", "", "```", "python evals/check_worked_example.py", "```", "",
          "Prints `MET` or the failed codes. The checker verifies offline consistency, binding to this run and the "
          "privacy rules; it does not prove origin. Origin rests on the export record: the five saved source files' "
          "byte hashes (pinned in the checker's `BINDING` from a read-only probe of the saved evidence, before "
          "export) and `trace.json` being a byte-identical copy of the saved trace. `provenance.json` records each "
          "file's relation to its source.", ""]
    L += ["## The run, step by step", "",
          "SK order (one DynamoDB Query returns the run in this order). Start = ms after META.started_at; "
          "execution order comes from these timestamps, not from the step numbers.", "",
          "| SK | kind | status | start ms | latency ms | model requests |", "|---|---|---|---|---|---|", *rows, "",
          f"Plus `META` (the pipeline outcome: `{meta['status']}`) and `DELIVERY` (the worker's record: results "
          f"`{s['DELIVERY'].get('results')}`, audit `{s['DELIVERY'].get('audit')}`). Start-time ties: {ties} "
          "(tied steps have no order between them). 5a and 6 overlap; 8a is scheduled beside step 7.", ""]
    L += ["## Following one line end to end", "",
          "Released PART A (`release.json` → `summary`) is rendered by 8a from step 6's sections; each A/B section "
          "lists the step-3 `fact_ids` it uses; each fact carries citations (line IDs + a quote ≤ 200 characters) "
          "with step 3's recorded verification outcome (verified, or rejected with a reason); PART C's sections list the PART A `section_id`s they re-express. "
          "The notes themselves are not here — they are in the corpus, joined by `notes_sha256`.", ""]
    if walk_a:
        L += [f"- Example (IDs only): PART A section `{walk_a[0]}` (`{walk_a[1]}`) → fact `{walk_a[2]}` "
              f"(step 3 recorded its citation as verified) → note line(s) {', '.join(f'`{x}`' for x in walk_a[3])}."]
    if walk_c:
        L += [f"- PART C section `{walk_c[0]}` → PART A sections {', '.join(f'`{x}`' for x in walk_c[1])}."]
    L += ["", "`trail.json` is the full report, recomputed by the checker from `trace.json` on every run:", "",
          f"- **Facts → note lines (step 3):** step 3 recorded {t3['cites_verified']} of {t3['cites_total']} citations "
          f"as verified and {t3['items_unverified']} item(s) as unverified. Rejected citations are kept as saved, with the producer's "
          f"reasons ({', '.join(f'{k} {v}' for k, v in sorted(reasons.items())) or 'none'}) — diagnostics, not repaired.",
          f"- **A/B sections → facts (step 6):** {t6['links_resolved']} references resolved to facts step 6 was "
          f"given; {len(t6['held_back'])} fact(s) held back by the resus guard; producer flags "
          f"{sum(x['occurrences'] for x in t6['flags'])}; boxes with no references by design: {len(t6['boxes'])}.",
          f"- **C sections → A sections (step 7):** {t7['links_resolved']} references resolved; producer flags "
          f"{sum(x['occurrences'] for x in t7['flags'])}; boxes: {len(t7['boxes'])}.",
          "", "Structural resolution says each reference points at something that exists, as the producer "
          "recorded it. Whether each sentence says what its sources say is a clinician question.", ""]
    L += ["## The release", "",
          "Release truth is the audit row (`status` complete) plus the ResultsTable item — META alone does not "
          "prove release. Each document's sha256 is recorded three times, independently — the audit row "
          "(`audit.json` → `output_sha256`), the ResultsTable item (`release.json` → `output_sha256_json`) and "
          "8b in the trace (`STEP#08b` → `output.hashes`) — and the checker recomputes all three from the "
          "documents.", "",
          f"- PART A (`summary`): `{h['summary']}`", f"- PART B (`gp_letter`): `{h['gp_letter']}`",
          f"- PART C (`patient`): `{h['patient']}`", ""]
    L += ["## Left out, and why", "",
          "- `user_sub` and the clinician linkage (`PK`): a trace never holds it (ADR-009 (b)); the audit row that "
          "links a run to a clinician stays in AWS.",
          "- The idempotency key, tokens and passwords; log events and worker logs (CloudWatch content, request "
          "IDs, function names); account IDs, ARNs, the StackId, key IDs and the endpoint.",
          "- The ResultsTable `ttl` (its own 24-hour bound, not the trace's 30 days).", ""]
    L += ["## CSO read", "",
          f"- Recorded S1 step-2 temporality defect (status doc, carried open): {C.README_PENDING} — Shina "
          "(author/CSO) reads the three synthetic documents and records here whether it appears in this run.", ""]
    return "\n".join(L)


def export(evidence: Path, out: Path, binding: dict = C.BINDING, corpus_path: Path = C.CORPUS,
           rules_pv: str = C.RULES_PIPELINE_VERSION, now: str | None = None) -> dict:
    job, snap = binding["job_id"], binding["snapshot"]
    staged = out.with_name(out.name + ".partial")
    _need("out_exists", not out.exists() and not staged.exists())
    _need("evidence_missing", (evidence / "manifest.json").is_file())

    # 1. the manifest entry and the snapshot
    m = _load(evidence / "manifest.json")
    att = [a for a in m.get("attempts", []) if a.get("job_id") == job]
    _need("bound_job_once", len(att) == 1)
    a = att[0]
    _need("scenario", a.get("scenario") == binding["scenario"])
    _need("met", a.get("met") is True)
    _need("checked_snapshot", a.get("checked_snapshot") == snap)
    d = evidence / "evidence" / job
    done = [x for x in _snapshots(d) if (d / x / "_snapshot.json").is_file()]
    _need("latest_complete", bool(done) and done[-1] == snap)
    sd = d / snap
    _need("unresolved", not (_load(sd / "_snapshot.json").get("unresolved") or []))
    pre = evidence / "preflight.json"
    _need("preflight_commit", pre.is_file() and _load(pre).get("commit") == binding["deployed_commit"])

    # 2. source bytes and the notes
    raw = {}
    for n in SOURCE_FILES:
        _need("source_missing", (sd / n).is_file())
        raw[n] = (sd / n).read_bytes()
    _need("source_hashes", {n: C.sha256_bytes(raw[n]) for n in SOURCE_FILES} == binding["source_sha256"])
    notes = C.corpus_notes(corpus_path).get(binding["scenario"], "")
    nsha = C.sha256_text(notes) if notes else None
    _need("notes_sha256", nsha == binding["notes_sha256"] and a.get("input_sha256") == nsha)

    # 3. projections by allow-list
    trace = json.loads(raw["trace.json"])
    au, rs, ck = json.loads(raw["audit.json"]), json.loads(raw["results.json"]), json.loads(raw["check.json"])
    _need("audit_attributes", set(au) - C.AUDIT_FIELDS == C.AUDIT_DROPPED and C.AUDIT_FIELDS <= set(au))
    _need("results_attributes", set(rs) - C.RELEASE_FIELDS == C.RELEASE_DROPPED and C.RELEASE_FIELDS <= set(rs))
    _need("audit_complete", au.get("status") == "complete")
    audit_p = {k: au[k] for k in sorted(C.AUDIT_FIELDS)}
    rel_p = {k: rs[k] for k in sorted(C.RELEASE_FIELDS)}
    sub = au["PK"].split("#", 1)[1] if str(au.get("PK", "")).startswith("USER#") else ""
    idem = au.get("idempotency_key") or ""
    _need("identity_values", bool(sub) and bool(idem) and au.get("user_sub") == sub)
    try:
        trail = C.trail_report(trace)
    except C.TrailError as e:
        raise Stop(e.code) from None
    receipt = {"check_json_sha256": C.sha256_bytes(raw["check.json"]),
               "criteria": ck.get("criteria"), "coverage": ck.get("coverage"), "met": ck.get("met"),
               "hard_stops_clear": ck.get("hard_stops_clear"), "repeat_eligible": ck.get("repeat_eligible")}
    readme = build_readme(binding, trace, trail, audit_p, trail["lines"]["line_ids"]).encode("utf-8")
    files = {"trace.json": raw["trace.json"], "audit.json": _dump(audit_p), "release.json": _dump(rel_p),
             "trail.json": _dump(trail)}
    here = Path(__file__).resolve()
    prov = {
        "schema": C.SCHEMA,
        "binding": {k: v for k, v in binding.items() if k != "source_sha256"},
        "corpus": {"path": "src/canary/scenarios.json", "scenario_id": binding["scenario"], "notes_sha256": nsha,
                   "corpus_file_sha256_at_export": C.sha256_bytes(Path(corpus_path).read_bytes())},
        "source": {"root": "~/eph-20261107 on the operator's machine (piece 7 evidence; not in the repository)",
                   "snapshot": snap, "snapshot_at": _load(sd / "_snapshot.json").get("at"),
                   "manifest_attempt_n": a.get("n"),
                   "files": {n: {"sha256": C.sha256_bytes(raw[n]), "bytes": len(raw[n])} for n in SOURCE_FILES}},
        "export": {
            "files": {
                "trace.json": {"sha256": C.sha256_bytes(files["trace.json"]), "bytes": len(files["trace.json"]),
                               "relation": "byte-identical copy of the source trace.json (p7.py's saved form: DynamoDB values decoded, numbers as int)"},
                "audit.json": {"sha256": C.sha256_bytes(files["audit.json"]), "bytes": len(files["audit.json"]),
                               "relation": "allow-list projection of the source audit.json: 19 fields, values unchanged; dropped PK, SK, user_sub, idempotency_key; re-serialised"},
                "release.json": {"sha256": C.sha256_bytes(files["release.json"]), "bytes": len(files["release.json"]),
                                 "relation": "allow-list projection of the source results.json: 11 fields, values unchanged (JSON-string fields kept as the same strings); dropped PK, SK, user_sub, ttl; re-serialised"},
                "trail.json": {"sha256": C.sha256_bytes(files["trail.json"]), "bytes": len(files["trail.json"]),
                               "relation": "computed from trace.json by evals/check_worked_example.trail_report"}},
            "exporter_sha256": C.sha256_bytes(here.read_bytes()),
            "checker_sha256": C.sha256_bytes(Path(C.__file__).resolve().read_bytes()),
            "exported_at": now or datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "receipt": receipt,
    }
    files["provenance.json"] = _dump(prov)
    files["README.md"] = readme

    # value equality of the projections, through their serialised bytes
    _need("projection_values", json.loads(files["audit.json"]) == {k: au[k] for k in C.AUDIT_FIELDS}
          and json.loads(files["release.json"]) == {k: rs[k] for k in C.RELEASE_FIELDS})
    _need("trace_copy", C.sha256_bytes(files["trace.json"]) == binding["source_sha256"]["trace.json"])

    # 5. privacy before any write to --out
    views = C.collect_views({n: json.loads(files[n]) for n in ("trace.json", "audit.json", "release.json",
                                                             "trail.json", "provenance.json")})
    hits = C.privacy_scan(views, notes, [readme.decode("utf-8")])
    _need("privacy", sum(hits.values()) == 0)
    _need("identity_absent", all(sub.encode() not in b and idem.encode() not in b for b in files.values()))

    # 6. stage, re-check, rename
    staged.mkdir(parents=True)
    for n, b in files.items():
        (staged / n).write_bytes(b)
    fails, info = C.check(staged, binding=binding, rules_pv=rules_pv, corpus_path=corpus_path)
    _need("recheck", fails == ["readme"] and all(x in readme.decode("utf-8") for x in C.REQUIRED_README)
          and readme.decode("utf-8").count(C.README_PENDING) == 1)
    os.replace(staged, out)
    return {"out": out.name, "files": {n: {"sha256": C.sha256_bytes(b), "bytes": len(b)} for n, b in sorted(files.items())},
            "trail": info.get("trail"), "pending": "readme (CSO read placeholder)"}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Export the bound piece-7 run into the worked example (no AWS).")
    p.add_argument("--evidence", type=Path, default=Path.home() / "eph-20261107")
    p.add_argument("--out", type=Path, default=C.DEFAULT_DIR)
    a = p.parse_args(argv)
    try:
        r = export(a.evidence.expanduser(), a.out)
    except Stop as e:
        extra = f" ({a.out.name}.partial kept for inspection)" if e.code == "recheck" else ""
        print(f"STOP: {e.code} — nothing written to {a.out.name}{extra}")
        return 1
    print(json.dumps(r, indent=1, sort_keys=True))
    print(f"EXPORTED (checker: every code passes except `readme` — the CSO read placeholder). "
          f"Next: the CSO read, then python evals/check_worked_example.py → MET.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
