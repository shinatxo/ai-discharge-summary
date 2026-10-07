#!/usr/bin/env python3
"""Pipeline eval runner — steps 1 -> 2 -> 3 -> 3b -> 4 -> 5b on the 18-scenario corpus.

The agentic side of ADR-009 (e)'s first step-level metric: the same scenarios,
the same batch layout and the same canary-window guard as run_cold_eval.py, so
score_batch.py scores step 5b's output exactly as it scored v1's. Only step 2
calls Bedrock (one call per generation); every other step is code.

Records keep each step's output — the facts object (quotes <= 200 characters),
evidence line IDs, the route, the safety-net selection — but never the line
index: the notes are carried by notes_sha256, as for v1. A step failure is
recorded with its fixed StepError code and the batch moves on.

USAGE
    python evals/run_pipeline_eval.py --all --runs 5 --batch w4-pipeline-e06-x5 --dry-run
    python evals/run_pipeline_eval.py --all --runs 5 --batch w4-pipeline-e06-x5

--dry-run uses a fake Bedrock that answers with an all-"not_documented" facts
object: the whole path runs, nothing is written, no AWS is touched.
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
from pipeline.extract import PROMPT_PATH, system_prompt, tool_config  # noqa: E402
from pipeline.route import NOT_FORCED  # noqa: E402
from pipeline.schemas import FIELD_NAMES  # noqa: E402

REGION = rce.REGION
MODEL_ID = rce.MODEL_ID
READ_TIMEOUT_S = 90                    # ADR-009 Q3 (2): step 2
RECORD_VERSION = 1
EST_SECONDS = 30                       # step 2 measured 14-21 s (W2 part 2), with headroom
EST_TOKENS = {"input": 6000, "output": 2500}   # per generation, no cache discount: an upper bound
STEP_ORDER = ("guard_input", "extract_facts", "validate_facts", "retrieve_evidence",
              "route_resus", "select_safety_net")


class DryRunBedrock:
    """Schema-valid, all-'not_documented' facts — the probe's dry-run client."""

    def converse(self, **kw):
        assert kw["toolConfig"] == tool_config()
        facts = {
            "field_status": [{"field": n, "status": "not_documented"} for n in FIELD_NAMES],
            "facts": [], "discharge_status": "not_documented",
            "resus": {"form_or_discussion_documented": False, "status_documented": "not_documented",
                      "changed": "not_documented", "cites": []},
            "age_group": "not_documented", "suspicious_text": [],
        }
        return {"output": {"message": {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": "dry", "name": "record_facts", "input": facts}}]}},
                "stopReason": "tool_use", "usage": {"inputTokens": 0, "outputTokens": 0},
                "metrics": {"latencyMs": 0}}


def live_client():
    import boto3
    from botocore.config import Config
    cfg = Config(connect_timeout=10, read_timeout=READ_TIMEOUT_S,
                 retries={"mode": "standard", "max_attempts": 1})   # retries are the step's job
    return boto3.Session(region_name=REGION).client("bedrock-runtime", config=cfg)


def run_pipeline(ctx: StepContext, scenario: rce.Scenario) -> tuple[dict, str | None, str | None]:
    """(step outputs without the line index, failed_step, error_code)."""
    steps: dict = {}
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
        return steps, None, None
    except StepError as exc:
        return steps, current, exc.code


def _call_record(steps: dict) -> list[dict]:
    c = steps.get("extract_facts", {}).get("call")
    if not c:
        return []
    return [{"name": "extract_facts", "input_tokens": c.get("input_tokens"),
             "output_tokens": c.get("output_tokens"), "cache_read_tokens": c.get("cache_read_tokens"),
             "cache_write_tokens": c.get("cache_write_tokens"), "stop_reason": c.get("stop_reason"),
             "latency_s": (c.get("latency_ms") or 0) / 1000, "throttle_retries": (c.get("attempts") or 1) - 1}]


def parse_args(argv):
    p = argparse.ArgumentParser(description="Run pipeline steps 1-5b on the corpus.")
    p.add_argument("scenarios", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--batch", default=None)
    p.add_argument("--corpus", type=Path, default=rce.DEFAULT_CORPUS_PATH)
    p.add_argument("--no-prompt-caching", dest="prompt_caching", action="store_false",
                   help="caching is ON by default, as the live stack runs (PromptCaching=on)")
    p.add_argument("--dry-run", action="store_true")
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
    prompt, prompt_sha = system_prompt()
    prompt_meta = {"path": rce._rel(PROMPT_PATH),
                   "file_sha256": rce.sha256_bytes(PROMPT_PATH.read_bytes()), "body_sha256": prompt_sha}
    n = len(scenarios) * args.runs
    start = now_fn()
    end = start + dt.timedelta(seconds=n * EST_SECONDS)
    batch_id = args.batch or f"{start:%Y-%m-%dT%H%M}-pipeline-x{args.runs}"
    batch_dir = runs_dir / batch_id
    est = rce.cost_usd([{"input_tokens": EST_TOKENS["input"] * n, "output_tokens": EST_TOKENS["output"] * n}])

    print(f"Batch:       {batch_id}")
    print(f"Generator:   pipeline steps 1-5b ({len(scenarios)} scenarios x {args.runs} runs = {n} "
          f"generations, {n} Bedrock calls — step 2 only)")
    print(f"Prompt:      {prompt_meta['path']} (prompt_sha256 {prompt_sha[:12]}…)")
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
    client = DryRunBedrock() if dry else (client_factory or live_client)()
    ctx = StepContext(bedrock=client, model_id=MODEL_ID, prompt_caching=args.prompt_caching, sleep=sleep)

    records = []
    if not dry:
        batch_dir.mkdir(parents=True)
        (batch_dir / "manifest.json").write_text(json.dumps({
            "record_version": RECORD_VERSION, "batch_id": batch_id, "created_at": start.isoformat(timespec="seconds"),
            "generator": "pipeline", "path": "steps-1-5b", "model_id": MODEL_ID, "region": REGION,
            "prompts": {"extract_facts": prompt_meta}, "prompt_caching": args.prompt_caching,
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
            steps, failed, code = run_pipeline(ctx, s)
            calls = _call_record(steps)
            rec = {"record_version": RECORD_VERSION, "generation_id": f"{batch_id}/r{run}/{s.id}",
                   "batch_id": batch_id, "run": run, "scenario_id": s.id, "generator": "pipeline",
                   "path": "steps-1-5b", "model_id": MODEL_ID, "region": REGION,
                   "prompts": {"extract_facts": prompt_meta}, "prompt_caching": args.prompt_caching,
                   "notes_sha256": s.notes_sha256, "started_at": t.isoformat(timespec="seconds"),
                   "elapsed_s": round(time.time() - t0, 3), "calls": calls, "steps": steps,
                   "output": None, "gate": None, "failed_step": failed,
                   "error": {"code": code, "message": ""} if code else None,
                   "cost_usd_est": rce.cost_usd(calls)}
            records.append(rec)
            if not dry:
                (run_dir / f"{s.id}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n",
                                                      encoding="utf-8")
            sn = steps.get("select_safety_net") or {}
            vc = (steps.get("validate_facts") or {}).get("counts", {})
            if code:
                print(f"  r{run} {s.id:<4} FAILED at {failed}: {code}")
            else:
                c = calls[0]
                print(f"  r{run} {s.id:<4} ok {rec['elapsed_s']:5.1f}s in/out={c['input_tokens']}/{c['output_tokens']} "
                      f"cache r/w={c['cache_read_tokens']}/{c['cache_write_tokens']} "
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
        print(f"\nDry run: {len(records)} generations through steps 1-5b, {errors} step failures; "
              f"canary windows clear; nothing written.")
        return 0 if not errors else 1
    (batch_dir / "batch.json").write_text(json.dumps({
        "batch_id": batch_id, "stopped_early": stopped,
        "generations": [{"run": r["run"], "scenario_id": r["scenario_id"],
                         "record": f"r{r['run']}/{r['scenario_id']}.json",
                         "failed_step": r["failed_step"], "error_code": (r["error"] or {}).get("code"),
                         "cost_usd_est": r["cost_usd_est"]} for r in records]}, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Batch `{batch_id}` — pipeline steps 1-5b", "",
             f"- Prompt: `{prompt_meta['path']}` — prompt_sha256 `{prompt_sha}`",
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
