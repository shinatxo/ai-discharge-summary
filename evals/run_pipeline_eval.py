#!/usr/bin/env python3
"""Pipeline eval runner — steps 1 -> 2 -> 3 -> 3b -> 4 -> 5b -> 5a -> 6 -> 7 on the 18-scenario corpus.

The agentic side of ADR-009 (e)'s step-level metrics: the same scenarios, the
same batch layout and the same canary-window guard as run_cold_eval.py, so
score_batch.py scores step 5b's output (safety-netting) and step 5a's (D4)
exactly as it scored v1's, step 2's advice against gold, and step 6's
composition by code checks (score_compose.py). Steps 2, 5a and 6 call Bedrock
(three calls per generation); every other step is code.

5a runs LAST here (27 Oct 2026): nothing else in the harness reads it, so a 5a
failure leaves steps 1-5b in the record and score_batch still scores them. In
the worker, 5a runs in parallel with step 6 (ADR-009 [A5]) — the orchestrator's
job (checklist item 5), not this runner's. Sequential: concurrency 1 — which
also keeps each step's latency clean for the 270 s deadline check.

Step 6 runs after 5a (28 Oct 2026, step build piece 2) in its OWN try-block:
it needs only step 3, so a 5a failure must not skip it (and vice versa). The
record's failed_step / error are the first failure; `step_errors` lists all.

Step 7 runs after step 6 (1 Nov 2026, step build piece 3) in its own try-block:
it needs step 6's PART A (compose_patient.patient_input — the boxes, risk,
allergies, VTE, conflicts and "Not documented" sections left out by code), so a
step-6 failure records step 7 as `upstream_failed`; a 5a failure does not stop
it (5a fills C's medicines box at 8b, scored offline). Its client is 5a's (60 s,
ADR-009 Q3 (2)). The console prints step 7's tokens, latency and section count
— never its check counts (piece 2 disclosed that step 6's were shown before the
blind read; step 7's blind read stays blind).

Records keep each step's output — the facts object (quotes <= 200 characters),
evidence line IDs, the route, the safety-net selection — but never the line
index: the notes are carried by notes_sha256, as for v1. A step failure is
recorded with its fixed StepError code and the batch moves on.

USAGE
    python evals/run_pipeline_eval.py --all --runs 5 --batch <batch> --dry-run
    python evals/run_pipeline_eval.py --all --runs 5 --batch <batch>

--dry-run uses a fake Bedrock that answers with an all-"not_documented" facts
object and an empty reconciliation: the whole path runs, nothing is written, no
AWS is touched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "generate"))
import run_cold_eval as rce  # noqa: E402
from pipeline import StepContext, StepError, run_step  # noqa: E402
from pipeline import compose, compose_patient, extract, reconcile, verify_patient  # noqa: E402
from pipeline.route import NOT_FORCED  # noqa: E402
from pipeline.schemas import FIELD_NAMES  # noqa: E402

REGION = rce.REGION
MODEL_ID = rce.MODEL_ID
READ_TIMEOUT_S = 90                    # ADR-009 Q3 (2): step 2
READ_TIMEOUT_5A_S = reconcile.READ_TIMEOUT_S   # 60 s
READ_TIMEOUT_6_S = compose.READ_TIMEOUT_S      # 90 s — the same client as step 2
READ_TIMEOUT_7_S = compose_patient.READ_TIMEOUT_S   # 60 s — the same client as 5a
READ_TIMEOUT_7V_S = verify_patient.READ_TIMEOUT_S  # 60 s — the same client as 5a and 7 (ADR-010 V6)
UPSTREAM_FAILED = "upstream_failed"           # runner-level: step 7 not run because step 6 failed
RECORD_VERSION = 1
PATH = "steps-1-5b-5a-6-7"
# Piece 1 measured steps 1-5b + 5a at 8.8-26.7 s; step 6 is estimated at ~30-40 s
# (ADR-009 Q3: ~1.8k out at ~63 tok/s) until its first live call. With headroom.
EST_SECONDS = 90
# Per generation, no cache discount: an upper bound. Step 2 ~6k in / 2.5k out (W4);
# 5a ~7k in / ~1.5k out (piece 1 measured far less); step 6 ~5k in (prompt + facts)
# / ~3k out (estimate until the first live call).
# Step 7 (piece 3): ~1.2k prompt + ~0.5k PART A sections in / ~0.7k out — +2.5k / +1k.
EST_TOKENS = {"input": 20500, "output": 8000}
STEP_ORDER = ("guard_input", "extract_facts", "validate_facts", "retrieve_evidence",
              "route_resus", "select_safety_net", "reconcile_medications", "compose_clinical",
              "compose_patient")
MODEL_STEPS = {"extract_facts": extract, "reconcile_medications": reconcile,
               "compose_clinical": compose, "compose_patient": compose_patient}
# --verify (ADR-010, 4 Nov 2026): step 7v after step 7, one regeneration, hold back.
# Opt-in so the existing path (and its batches' records) is unchanged.
PATH_VERIFY = "steps-1-5b-5a-6-7-7v"
VERIFY_STEPS = ("verify_patient", "compose_patient_first", "verify_patient_2", "patient_held_back")
# Step records that hold a model call beside MODEL_STEPS' own: the first draft (when step 7
# was regenerated) and the second verification. Their module is the step's.
EXTRA_CALLS = {"verify_patient": "verify_patient", "compose_patient_first": "compose_patient",
               "verify_patient_2": "verify_patient"}
# Step 7v per call (measured, v0.1 pilot 4 Nov: output p50 1.3k, p90 1.8k): 3.6k in / 1.7k out as an
# upper bound; worst case a regeneration (+ step 7) and a second 7v.
EST_TOKENS_VERIFY = {"input": 3600 * 2 + 4500, "output": 1700 * 2 + 700}


DRY_REASON = "Dry run: no facts."      # the dry composition's one PART A prose line (F1)


class DryRunBedrock:
    """Schema-valid, all-'not_documented' facts, an empty reconciliation and a
    composition with every required section ("Not documented" and the three
    boxes) except PART A presenting_complaint, which carries a fixed dry-run line so
    step 7 is given a section and runs (2 Nov 2026: step 7 refuses with
    no_sections_given when given neither a presenting_complaint nor a diagnosis —
    F1). Cost: one step-6 check (not_documented_with_content) per dry generation.
    Accepts (and ignores) a read timeout, like live_client."""

    def __init__(self, read_timeout=None):
        self.read_timeout = read_timeout

    def converse(self, **kw):
        if kw["toolConfig"] == reconcile.tool_config():
            return self._tool_use("record_reconciliation", {"drugs": []})
        if kw["toolConfig"] == compose.tool_config():
            return self._tool_use("record_composition", {"sections": self.composition()})
        if kw["toolConfig"] == compose_patient.tool_config():
            return self._tool_use("record_patient_version", {"sections": self.patient_version()})
        if kw["toolConfig"] == verify_patient.tool_config():
            # the dry leaflet's one sentence, supported: no regeneration in dry mode
            return self._tool_use("record_verification", {"items": [
                {"c_section_id": "c_sections.0", "sentence_id": "c_sections.0.s1",
                 "a_section_id": "sections.0", "span": "", "reason": "none", "verdict": "supported"}]})
        assert kw["toolConfig"] == extract.tool_config()
        facts = {
            "field_status": [{"field": n, "status": "not_documented"} for n in FIELD_NAMES],
            "facts": [], "discharge_status": "not_documented",
            "resus": {"form_or_discussion_documented": False, "status_documented": "not_documented",
                      "changed": "not_documented", "cites": []},
            "age_group": "not_documented", "suspicious_text": [],
        }
        return self._tool_use("record_facts", facts)

    @staticmethod
    def composition():
        def sec(part, heading):
            # A's gp_actions reads "Not documented"; B's "No GP actions documented." (c0.1 C4).
            # Fixed 1 Nov 2026: the fake wrote B's wording in A too (1 check per dry generation).
            b_gp = part == "part_b" and heading == "gp_actions"
            text = compose.BOXES.get(heading, "Admitted; no facts documented." if heading == "opening"
                                     else "No GP actions documented." if b_gp
                                     else DRY_REASON if (part, heading) == ("part_a", "presenting_complaint")
                                     else "Not documented")
            return {"part": part, "heading": heading, "fact_ids": [], "text": text}
        return ([sec("part_a", h) for h in compose.A_REQUIRED]
                + [sec("part_b", h) for h in compose.B_REQUIRED])

    @staticmethod
    def patient_version():
        # The dry composition's PART A gives step 7 one section, presenting_complaint
        # (sections.0): why_in_hospital cites it, then the two boxes — zero step-7 checks.
        return [{"heading": "why_in_hospital", "source_ids": ["sections.0"], "text": "You were in hospital."},
                {"heading": "medicines", "source_ids": [], "text": compose_patient.BOXES["medicines"]},
                {"heading": "safety_net", "source_ids": [], "text": compose_patient.BOXES["safety_net"]}]

    @staticmethod
    def _tool_use(name, payload):
        return {"output": {"message": {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": "dry", "name": name, "input": payload}}]}},
                "stopReason": "tool_use", "usage": {"inputTokens": 0, "outputTokens": 0},
                "metrics": {"latencyMs": 0}}


def live_client(read_timeout=READ_TIMEOUT_S):
    """One client per read timeout (ADR-009 W2 part 2): 90 s for steps 2 and 6, 60 s for 5a.

    total_max_attempts=1 since 5 Nov 2026 (step build piece 5). Batches made before then
    used max_attempts=1, which botocore reads as ONE RETRY: a throttle or a read timeout
    could be retried once, invisibly, inside a step attempt. Their recorded attempt and
    throttle_retries counts are therefore a lower bound on requests sent. Records unchanged."""
    import boto3
    from botocore.config import Config
    cfg = Config(connect_timeout=10, read_timeout=read_timeout,
                 retries={"mode": "standard", "total_max_attempts": 1})   # retries are the step's job
    return boto3.Session(region_name=REGION).client("bedrock-runtime", config=cfg)


def run_pipeline(ctx: StepContext, scenario: rce.Scenario,
                 ctx_5a: StepContext | None = None,
                 ctx_6: StepContext | None = None,
                 ctx_7: StepContext | None = None,
                 ctx_7v: StepContext | None = None) -> tuple[dict, str | None, str | None, dict]:
    """(step outputs without the line index, failed_step, error_code, step_errors).
    ctx_5a / ctx_6 / ctx_7 None -> that step is not run. 5a and 6 each need only
    steps 1-3b, so each runs in its own try-block: one failing never skips the other
    (in the worker they run in parallel, ADR-009 [A5]). Step 7 needs step 6."""
    steps: dict = {}
    errors: dict = {}
    current = "guard_input"
    try:
        g = run_step("guard_input", {"notes": scenario.notes})
        lines, flags = g["lines"], g["flags"]
        steps["guard_input"] = {k: v for k, v in g.items() if k != "lines"}
        current = "extract_facts"
        x = run_step("extract_facts", {"lines": lines}, ctx=ctx)
        steps["extract_facts"] = {"call": x["call"]}
        current = "validate_facts"
        v = run_step("validate_facts", {"facts": x["facts"], "lines": lines})
        steps["validate_facts"] = v
        facts = v["facts"]
        current = "retrieve_evidence"
        ev = run_step("retrieve_evidence", {"facts": facts, "lines": lines, "flags": flags})
        steps["retrieve_evidence"] = ev
        current = "route_resus"
        steps["route_resus"] = run_step("route_resus", {"facts": facts, "evidence": ev, "lines": lines})
        current = "select_safety_net"
        steps["select_safety_net"] = run_step("select_safety_net", {"facts": facts, "flags": flags})
    except StepError as exc:
        return steps, current, exc.code, {current: exc.code}
    if ctx_5a is not None:
        try:
            steps["reconcile_medications"] = run_step(
                "reconcile_medications", {"lines": lines, "facts": facts, "evidence": ev}, ctx=ctx_5a)
        except StepError as exc:
            errors["reconcile_medications"] = exc.code
    if ctx_6 is not None:
        try:
            # facts only: step 6 never receives the notes (the step refuses a "lines" key)
            steps["compose_clinical"] = run_step("compose_clinical", {"facts": facts}, ctx=ctx_6)
        except StepError as exc:
            errors["compose_clinical"] = exc.code
    if ctx_7 is not None:
        if "compose_clinical" not in steps:
            errors["compose_patient"] = UPSTREAM_FAILED
        else:
            try:
                # PART A's prose only (D1 I3): never the notes, the facts or the boxes
                inp = compose_patient.patient_input(steps["compose_clinical"])
                if ctx_7v is None:
                    steps["compose_patient"] = run_step("compose_patient", inp, ctx=ctx_7)
                else:
                    # ADR-010 V3: 7 -> 7v -> (one regeneration -> 7v -> hold back); any
                    # failure inside is compose_patient's error (fail closed)
                    steps.update(verify_patient.verified_patient_version(
                        inp,
                        lambda i: run_step("compose_patient", i, ctx=ctx_7),
                        lambda i: run_step("verify_patient", i, ctx=ctx_7v)))
            except StepError as exc:
                errors["compose_patient"] = exc.code
    first = next(iter(errors), None)
    return steps, first, errors.get(first), errors


def _call_record(steps: dict) -> list[dict]:
    out = []
    for name in (*MODEL_STEPS, *EXTRA_CALLS):
        c = (steps.get(name) or {}).get("call")
        if c:
            out.append({"name": name, "input_tokens": c.get("input_tokens"),
                        "output_tokens": c.get("output_tokens"), "cache_read_tokens": c.get("cache_read_tokens"),
                        "cache_write_tokens": c.get("cache_write_tokens"), "stop_reason": c.get("stop_reason"),
                        "latency_s": (c.get("latency_ms") or 0) / 1000,
                        "throttle_retries": (c.get("attempts") or 1) - 1})
    return out


def _prompt_meta(module) -> dict:
    _, body_sha = module.system_prompt()
    return {"path": rce._rel(module.PROMPT_PATH),
            "file_sha256": rce.sha256_bytes(module.PROMPT_PATH.read_bytes()), "body_sha256": body_sha}


def parse_args(argv):
    p = argparse.ArgumentParser(description="Run pipeline steps 1-5b, 5a, 6 and 7 on the corpus.")
    p.add_argument("scenarios", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--batch", default=None)
    p.add_argument("--corpus", type=Path, default=rce.DEFAULT_CORPUS_PATH)
    p.add_argument("--no-prompt-caching", dest="prompt_caching", action="store_false",
                   help="caching is ON by default, as the live stack runs (PromptCaching=on)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verify", action="store_true",
                   help="run step 7v after step 7 (ADR-010): one regeneration, then hold back")
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    if a.runs < 1:
        p.error("--runs must be at least 1")
    if a.batch is not None and not re.fullmatch(r"[A-Za-z0-9._-]+", a.batch):
        p.error("--batch may contain only letters, digits, '.', '_' and '-'")
    return a


def main(argv=None, *, client_factory=None, now=None, confirm=input, runs_dir: Path | None = None,
         sleep=time.sleep) -> int:
    args = parse_args(argv)
    now_fn = now or (lambda: dt.datetime.now(rce.LONDON))
    runs_dir = runs_dir or rce.DEFAULT_RUNS_DIR
    try:
        corpus, corpus_sha = rce.load_corpus(args.corpus)
        scenarios = rce.select_scenarios(corpus, args.scenarios, args.all)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    prompts = {name: _prompt_meta(mod) for name, mod in MODEL_STEPS.items()}
    if args.verify:
        prompts["verify_patient"] = _prompt_meta(verify_patient)
    path = PATH_VERIFY if args.verify else PATH
    n = len(scenarios) * args.runs
    start = now_fn()
    end = start + dt.timedelta(seconds=n * EST_SECONDS)
    batch_id = args.batch or f"{start:%Y-%m-%dT%H%M}-pipeline-x{args.runs}"
    batch_dir = runs_dir / batch_id
    tok_in = EST_TOKENS["input"] + (EST_TOKENS_VERIFY["input"] if args.verify else 0)
    tok_out = EST_TOKENS["output"] + (EST_TOKENS_VERIFY["output"] if args.verify else 0)
    est = rce.cost_usd([{"input_tokens": tok_in * n, "output_tokens": tok_out * n}])

    print(f"Batch:       {batch_id}")
    print(f"Generator:   pipeline {path} ({len(scenarios)} scenarios x {args.runs} runs = {n} "
          f"generations, {4 * n} Bedrock calls — steps 2, 5a, 6 and 7"
          + (f", + 7v: {n}-{3 * n} more (7v, and a regeneration + second 7v when flagged))" if args.verify else ")"))
    for name, meta in prompts.items():
        print(f"Prompt:      {meta['path']} (prompt_sha256 {meta['body_sha256'][:12]}…)")
    print(f"Caching:     {'on' if args.prompt_caching else 'off'}")
    print(f"Corpus:      {rce._rel(args.corpus)} (sha256 {corpus_sha[:12]}…)")
    print(f"Estimate:    <= ${est:.2f} (upper bound); ~{n * EST_SECONDS / 60:.0f} min, sequential, "
          f"finishing ~{end:%H:%M} London")
    clash = rce.canary_window_clash(start, end)
    if clash:
        print(f"refused: the projected run overlaps a canary window "
              f"({clash[0]:%a %H:%M}–{clash[1]:%H:%M} London). Start later.", file=sys.stderr)
        return 3
    if batch_dir.exists():
        print(f"refused: {rce._rel(batch_dir)} already exists — never overwritten.", file=sys.stderr)
        return 2
    dry = args.dry_run
    if not dry and not args.yes:
        if confirm(f"Run {n} live generations (est. <= ${est:.2f})? Type yes: ").strip().lower() != "yes":
            print("Not run.")
            return 1
    factory = DryRunBedrock if dry else (client_factory or live_client)
    ctx = StepContext(bedrock=factory(READ_TIMEOUT_S), model_id=MODEL_ID,
                      prompt_caching=args.prompt_caching, sleep=sleep)
    ctx_5a = StepContext(bedrock=factory(READ_TIMEOUT_5A_S), model_id=MODEL_ID,
                         prompt_caching=args.prompt_caching, sleep=sleep)
    ctx_6 = ctx                                  # 90 s, as step 2 (ADR-009 Q3 (2))
    ctx_7 = ctx_5a                               # 60 s, as 5a (ADR-009 Q3 (2))
    ctx_7v = ctx_5a if args.verify else None     # 60 s, as 7 (ADR-010 V6)

    records = []
    if not dry:
        batch_dir.mkdir(parents=True)
        (batch_dir / "manifest.json").write_text(json.dumps({
            "record_version": RECORD_VERSION, "batch_id": batch_id, "created_at": start.isoformat(timespec="seconds"),
            "generator": "pipeline", "path": path, "model_id": MODEL_ID, "region": REGION,
            "prompts": prompts, "prompt_caching": args.prompt_caching,
            "corpus": {"path": rce._rel(args.corpus), "sha256": corpus_sha},
            "scenarios": [{"id": s.id, "notes_sha256": s.notes_sha256} for s in scenarios],
            "runs": args.runs, "price_basis": {"usd_per_mtok": rce.PRICE_PER_MTOK,
                                               "regional_premium": rce.REGIONAL_PREMIUM},
        }, indent=2) + "\n", encoding="utf-8")
    stopped = False
    for run in range(1, args.runs + 1):
        run_dir = batch_dir / f"r{run}"
        if not dry:
            run_dir.mkdir()
        print(f"\n== run r{run} ==")
        for s in scenarios:
            t = now_fn()
            if not dry and rce.canary_window_clash(t, t + dt.timedelta(seconds=EST_SECONDS)):
                print(f"  stopping before r{run} {s.id}: a canary window is about to open.")
                stopped = True
                break
            t0 = time.time()
            steps, failed, code, step_errors = run_pipeline(ctx, s, ctx_5a, ctx_6, ctx_7, ctx_7v)
            calls = _call_record(steps)
            rec = {"record_version": RECORD_VERSION, "generation_id": f"{batch_id}/r{run}/{s.id}",
                   "batch_id": batch_id, "run": run, "scenario_id": s.id, "generator": "pipeline",
                   "path": path, "model_id": MODEL_ID, "region": REGION,
                   "prompts": prompts, "prompt_caching": args.prompt_caching,
                   "notes_sha256": s.notes_sha256, "started_at": t.isoformat(timespec="seconds"),
                   "elapsed_s": round(time.time() - t0, 3), "calls": calls, "steps": steps,
                   "output": None, "gate": None, "failed_step": failed,
                   "error": {"code": code, "message": ""} if code else None,
                   "step_errors": step_errors,
                   "cost_usd_est": rce.cost_usd(calls)}
            records.append(rec)
            if not dry:
                (run_dir / f"{s.id}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n",
                                                      encoding="utf-8")
            sn = steps.get("select_safety_net") or {}
            vc = (steps.get("validate_facts") or {}).get("counts", {})
            rm = steps.get("reconcile_medications") or {}
            cm = steps.get("compose_clinical") or {}
            if code and "select_safety_net" not in steps:
                print(f"  r{run} {s.id:<4} FAILED at {failed}: {code}")
            else:
                by_name = {c["name"]: c for c in calls}
                c = by_name["extract_facts"]
                c5 = by_name.get("reconcile_medications", {})
                c6 = by_name.get("compose_clinical", {})
                c7 = by_name.get("compose_patient", {})
                pm = steps.get("compose_patient") or {}
                rc = rm.get("counts", {})
                cc = cm.get("counts", {})
                if step_errors:
                    print(f"  r{run} {s.id:<4} step failures: "
                          + ", ".join(f"{k}={v}" for k, v in step_errors.items()))
                print(f"  r{run} {s.id:<4} ok {rec['elapsed_s']:5.1f}s in/out={c['input_tokens']}/{c['output_tokens']} "
                      f"5a in/out={c5.get('input_tokens')}/{c5.get('output_tokens')} "
                      f"drugs={rc.get('entries')} unverified={rc.get('entries_unverified')} "
                      f"flagged={rc.get('entries_flagged')} dh_unreconciled={rc.get('dh_lines_unreconciled')} "
                      f"6 in/out={c6.get('input_tokens')}/{c6.get('output_tokens')} "
                      f"6 latency={c6.get('latency_s')} sections={cc.get('sections')} "
                      f"6 checks={cc.get('checks')} "
                      f"7 in/out={c7.get('input_tokens')}/{c7.get('output_tokens')} "
                      f"7 latency={c7.get('latency_s')} 7 sections={(pm.get('counts') or {}).get('sections')} "
                      + (f"7v flagged={(steps.get('verify_patient') or {}).get('flagged')} "
                         f"regenerated={'compose_patient_first' in steps} "
                         f"held_back={len(steps.get('patient_held_back') or [])} " if args.verify else "")
                      + f"cache r/w={c['cache_read_tokens']}/{c['cache_write_tokens']} "
                      f"route={sn.get('route')} audience={sn.get('audience')} "
                      f"resus={steps['route_resus'].get('route')}"
                      + (f" forced={steps['route_resus']['forced']}"
                         if steps['route_resus'].get('forced') not in (None, NOT_FORCED) else "")
                      + (f" cites_unverified={vc.get('citation_unverified')}" if vc.get('citation_unverified') else ""))
        if stopped:
            break

    errors = sum(1 for r in records if r["error"])
    total = sum(r["cost_usd_est"] for r in records)
    if dry:
        print(f"\nDry run: {len(records)} generations through {path}, {errors} step failures; "
              f"canary windows clear; nothing written.")
        return 0 if not errors else 1
    (batch_dir / "batch.json").write_text(json.dumps({
        "batch_id": batch_id, "stopped_early": stopped,
        "generations": [{"run": r["run"], "scenario_id": r["scenario_id"],
                         "record": f"r{r['run']}/{r['scenario_id']}.json",
                         "failed_step": r["failed_step"], "error_code": (r["error"] or {}).get("code"),
                         "cost_usd_est": r["cost_usd_est"]} for r in records]}, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Batch `{batch_id}` — pipeline {path}", "",
             *[f"- Prompt ({name}): `{m['path']}` — prompt_sha256 `{m['body_sha256']}`"
               for name, m in prompts.items()],
             f"- Model: `{MODEL_ID}` ({REGION}); caching {'on' if args.prompt_caching else 'off'}",
             f"- Corpus sha256 `{corpus_sha}`; runs {args.runs}; generations {len(records)}; "
             f"step failures {errors}" + ("; **stopped early**" if stopped else ""),
             f"- Est. cost ${total:.4f} (Cost Explorer is the source of truth)", "",
             "Scores: `python evals/score_batch.py` on this folder.", ""]
    (batch_dir / "BATCH_SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nBatch summary -> {rce._rel(batch_dir / 'BATCH_SUMMARY.md')}")
    print(f"Est. cost ${total:.4f} · step failures {errors}" + (" · stopped early" if stopped else ""))
    return 0 if not errors and not stopped else 1


if __name__ == "__main__":
    sys.exit(main())
