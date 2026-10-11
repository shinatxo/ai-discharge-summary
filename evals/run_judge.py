#!/usr/bin/env python3
"""Rubric v2 judge runner — the pinned LLM-as-judge (W5).

    python evals/run_judge.py --freeze                                   # once, then commit FROZEN.json
    python evals/run_judge.py evals/runs/w4-baseline-v1-x5 --run 1 --repeats 3 --dry-run
    python evals/run_judge.py evals/runs/w4-baseline-v1-x5 --run 1 --repeats 3

What it does (EVAL_RESULTS.md section 2, "The judge" and "Calibration"):
- Sends each v1 generation (notes with step-1 line IDs + the output verbatim) to
  `anthropic.claude-sonnet-4-6`, eu-west-2, temperature 0, with the judge prompt
  and section 2 as the system prompt (cached), and one strict, forced tool
  (`record_rubric_scores`) so the answer is structured.
- Verifies every finding in code: the output quote must be in the output and
  every note cite must pass step 3's `verify_cite`. A finding that fails is
  discarded and recorded as a judge defect; a partial/fail left with no
  verified finding is marked `evidence_ok: false`.
- Writes one record per call to <batch>/judge/<judge_id>/rep<k>/r<run>/<ID>.json
  and a manifest pinning model ID, region, temperature, prompt and rubric sha256.

Guards: refuses unless the judge prompt and rubric match `evals/judge/FROZEN.json`
(`--freeze` writes it; commit it before calibration); refuses to judge a run whose
blind sheets (evals/calibration/<batch>-r<run>) are not all scored; refuses a run
projected into a canary window (02:00 daily, Mon 03:00 London) and stops
submitting before one opens; at most 4 calls in flight; throttle-only retry,
twice; never overwrites; `--dry-run` creates no client; a live run asks for
confirmation unless `--yes`. The console shows IDs, timings, tokens and counts —
never note text, output text, quotes or verdicts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gold as G  # noqa: E402
import make_blind_sheets as mbs  # noqa: E402
import run_cold_eval as rce  # noqa: E402
from pipeline.validate import QUOTE_MAX_CHARS, normalise, verify_cite  # noqa: E402

try:
    import boto3
except ImportError:  # --dry-run and --freeze still work
    boto3 = None
try:
    from botocore.exceptions import ClientError
except ImportError:
    class ClientError(Exception):  # type: ignore[no-redef]
        response: dict = {}

JUDGE_VERSION = 1
MODEL_ID = rce.MODEL_ID
REGION = rce.REGION
TEMPERATURE = 0.0
MAX_TOKENS = 4096
MAX_CONCURRENCY = 4
THROTTLE_RETRIES = 2
THROTTLE_WAIT_S = (20.0, 30.0)
TOOL_NAME = "record_rubric_scores"
DIMENSIONS, VERDICTS, NA_ALLOWED = mbs.DIMENSIONS, mbs.VERDICTS, mbs.NA_ALLOWED

JUDGE_DIR = rce.REPO / "evals" / "judge"
PROMPT_PATH = JUDGE_DIR / "judge_prompt.md"
FROZEN_PATH = JUDGE_DIR / "FROZEN.json"
PROMPT_MARKER = "## SYSTEM PROMPT"

# Planning figures (upper bound: no cache discount). Re-base after the first live run.
EST_TOKENS = {"input": 9000, "output": 2000}
EST_SECONDS = 45


class JudgeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Prompt, rubric, freeze
# ---------------------------------------------------------------------------
def load_prompt(prompt_path: Path = PROMPT_PATH, rubric_path: Path = mbs.RUBRIC_PATH) -> tuple[str, dict]:
    raw = prompt_path.read_text(encoding="utf-8")
    if PROMPT_MARKER not in raw:
        raise JudgeError("judge_prompt.md is missing the SYSTEM PROMPT marker")
    instructions = raw.split(PROMPT_MARKER, 1)[1].strip()
    rubric = mbs.rubric_section(rubric_path)
    system = f"{instructions}\n\n<rubric>\n{rubric}</rubric>"
    sha = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()  # noqa: E731
    return system, {"path": rce._rel(prompt_path),
                    "file_sha256": rce.sha256_bytes(prompt_path.read_bytes()),
                    "instructions_sha256": sha(instructions),
                    "rubric_sha256": sha(rubric),
                    "system_sha256": sha(system)}


def freeze(meta: dict, frozen_path: Path = FROZEN_PATH, now=None) -> dict:
    if frozen_path.exists():
        raise JudgeError(f"{frozen_path.name} already exists — a new freeze is a new judge "
                         "version; remove it deliberately (and say why) first")
    entry = {"judge_version": JUDGE_VERSION, "model_id": MODEL_ID, "temperature": TEMPERATURE,
             "instructions_sha256": meta["instructions_sha256"],
             "rubric_sha256": meta["rubric_sha256"], "system_sha256": meta["system_sha256"],
             "frozen_at": (now or dt.datetime.now(rce.LONDON)).isoformat(timespec="seconds")}
    frozen_path.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
    return entry


def check_frozen(meta: dict, frozen_path: Path = FROZEN_PATH) -> dict:
    if not frozen_path.exists():
        raise JudgeError("the judge prompt is not frozen — run --freeze and commit FROZEN.json first")
    f = json.loads(frozen_path.read_text(encoding="utf-8"))
    for key in ("instructions_sha256", "rubric_sha256"):
        if f.get(key) != meta[key]:
            raise JudgeError(f"{key.split('_')[0]} changed since the freeze — that is a new judge version")
    return f


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------
def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


def tool_schema() -> dict:
    cite = _obj({"lines": {"type": "array", "items": {"type": "string"}},
                 "quote": {"type": "string"}})
    finding = _obj({"output_quote": {"type": "string"},
                    "note_cites": {"type": "array", "items": cite},
                    "reason": {"type": "string"}})
    dim = _obj({"verdict": {"type": "string", "enum": list(VERDICTS)},
                "auto_fail": {"type": "boolean"},
                "findings": {"type": "array", "items": finding}})
    return _obj({"dimensions": _obj({d: dim for d in DIMENSIONS})})


def tool_config() -> dict:
    return {"tools": [{"toolSpec": {
                "name": TOOL_NAME,
                "description": "Record the rubric v2 verdicts for D2, D3, D6, D7 and D8, with "
                               "verbatim evidence for every partial or fail.",
                "inputSchema": {"json": tool_schema()}, "strict": True}}],
            "toolChoice": {"tool": {"name": TOOL_NAME}}}


def user_text(lines: dict, output: str) -> str:
    notes = "\n".join(f"{lid}: {lines[lid]}" for lid in sorted(lines))
    return (f"<notes>\n{notes}\n</notes>\n\n<output>\n{output}\n</output>\n\n"
            f"Score the output against the notes. Call {TOOL_NAME} once.")


def _converse(client, request: dict, sleep) -> tuple[dict, int, float]:
    retries = 0
    while True:
        t0 = time.time()
        try:
            return client.converse(**request), retries, time.time() - t0
        except ClientError as exc:
            code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code")
            if code == "ThrottlingException" and retries < THROTTLE_RETRIES:
                retries += 1
                sleep(random.uniform(*THROTTLE_WAIT_S))
                continue
            raise


# ---------------------------------------------------------------------------
# Verification — every finding checked in code
# ---------------------------------------------------------------------------
def verify(judgement, lines: dict, output: str) -> dict:
    """{'dimensions': {D: {verdict, auto_fail, findings, discarded, evidence_ok}},
    'defects': [...]}. Defects carry dimension, index and a fixed reason only."""
    out_norm = normalise(output)
    dims_in = judgement.get("dimensions") if isinstance(judgement, dict) else None
    dims_in = dims_in if isinstance(dims_in, dict) else {}
    dims, defects = {}, []
    for d in DIMENSIONS:
        v = dims_in.get(d)
        if not isinstance(v, dict):
            defects.append({"dimension": d, "reason": "missing_dimension"})
            dims[d] = {"verdict": None, "auto_fail": False, "findings": [], "discarded": [],
                       "evidence_ok": False}
            continue
        verdict = v.get("verdict")
        if verdict not in VERDICTS:
            defects.append({"dimension": d, "reason": "bad_verdict"})
            verdict = None
        elif verdict == "na" and d not in NA_ALLOWED:
            defects.append({"dimension": d, "reason": "na_not_allowed"})
            verdict = None
        auto = v.get("auto_fail") is True
        if auto and verdict != "fail":
            defects.append({"dimension": d, "reason": "auto_fail_without_fail"})
            auto = False
        kept, discarded = [], []
        for i, f in enumerate(v.get("findings") or []):
            reason = _check_finding(f, lines, out_norm)
            if reason:
                discarded.append({"index": i, "reason": reason})
                defects.append({"dimension": d, "index": i, "reason": reason})
            else:
                kept.append({"output_quote": f["output_quote"],
                             "note_cites": [{"lines": c["lines"], "quote": c["quote"]}
                                            for c in f.get("note_cites") or []],
                             "reason": f.get("reason", "")})
        needs = verdict in ("partial", "fail")
        dims[d] = {"verdict": verdict, "auto_fail": auto, "findings": kept, "discarded": discarded,
                   "evidence_ok": verdict is not None and (not needs or bool(kept))}
    return {"dimensions": dims, "defects": defects}


def _check_finding(f, lines: dict, out_norm: str) -> str | None:
    if not isinstance(f, dict):
        return "finding_not_object"
    q = f.get("output_quote")
    if not isinstance(q, str) or not normalise(q):
        return "output_quote_empty"
    if len(q) > QUOTE_MAX_CHARS:
        return "output_quote_too_long"
    if normalise(q) not in out_norm:
        return "output_quote_not_found"
    for c in f.get("note_cites") or []:
        r = verify_cite(c if isinstance(c, dict) else {}, lines)
        if not r["verified"]:
            return f"note_cite_{r['reason']}"
    return None


# ---------------------------------------------------------------------------
# One judgement
# ---------------------------------------------------------------------------
def judge_one(client, rec: dict, lines: dict, system: str, meta: dict, *,
              judge_id: str, repeat: int, started_at: str, sleep) -> dict:
    request = {"modelId": MODEL_ID,
               "system": [{"text": system}, {"cachePoint": {"type": "default"}}],
               "messages": [{"role": "user", "content": [{"text": user_text(lines, rec["output"])}]}],
               "inferenceConfig": {"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
               "toolConfig": tool_config()}
    out = {"judge_version": JUDGE_VERSION, "judge_id": judge_id, "repeat": repeat,
           "generation_id": rec["generation_id"], "scenario_id": rec["scenario_id"],
           "run": rec["run"], "notes_sha256": rec["notes_sha256"],
           "output_sha256": rce.sha256_text(rec["output"]),
           "model_id": MODEL_ID, "region": REGION, "temperature": TEMPERATURE,
           "prompt": meta, "started_at": started_at,
           "call": None, "judgement": None, "verified": None, "error": None}
    t0 = time.time()
    try:
        resp, retries, latency = _converse(client, request, sleep)
    except ClientError as exc:
        code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code") or "ClientError"
        out["error"] = {"code": code}
        out["elapsed_s"] = round(time.time() - t0, 3)
        return out
    usage = resp.get("usage", {}) or {}
    out["call"] = {"stop_reason": resp.get("stopReason"), "throttle_retries": retries,
                   "latency_s": round(latency, 3),
                   "input_tokens": usage.get("inputTokens"),
                   "output_tokens": usage.get("outputTokens"),
                   "cache_read_tokens": usage.get("cacheReadInputTokens"),
                   "cache_write_tokens": usage.get("cacheWriteInputTokens")}
    out["cost_usd_est"] = rce.cost_usd([out["call"]])
    out["elapsed_s"] = round(time.time() - t0, 3)
    uses = [b["toolUse"] for b in (resp.get("output", {}).get("message", {}).get("content") or [])
            if isinstance(b, dict) and "toolUse" in b]
    if resp.get("stopReason") == "max_tokens":
        out["error"] = {"code": "max_tokens"}
    elif len(uses) != 1 or uses[0].get("name") != TOOL_NAME or not isinstance(uses[0].get("input"), dict):
        out["error"] = {"code": "no_tool_call"}
    else:
        out["judgement"] = uses[0]["input"]
        out["verified"] = verify(out["judgement"], lines, rec["output"])
    return out


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------
def estimate_cost_usd(n_calls: int) -> float:
    return rce.cost_usd([{"input_tokens": EST_TOKENS["input"] * n_calls,
                          "output_tokens": EST_TOKENS["output"] * n_calls}])


def parse_args(argv):
    p = argparse.ArgumentParser(description="Rubric v2 judge runner (W5).")
    p.add_argument("batch_dir", nargs="?", type=Path)
    p.add_argument("--run", type=int, action="append", help="run number(s) to judge; repeatable")
    p.add_argument("--repeats", type=int, default=1, help="judge each generation K times")
    p.add_argument("--judge-id")
    p.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY)
    p.add_argument("--freeze", action="store_true", help="freeze the judge prompt + rubric and exit")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    return p.parse_args(argv)


def main(argv=None, *, client_factory=None, now=None, sleep=time.sleep, confirm=input,
         frozen_path: Path = FROZEN_PATH, prompt_path: Path = PROMPT_PATH,
         rubric_path: Path = mbs.RUBRIC_PATH, calibration_root: Path = mbs.DEFAULT_OUT_ROOT,
         corpus_path: Path = rce.DEFAULT_CORPUS_PATH) -> int:
    a = parse_args(argv)
    now_fn = now or (lambda: dt.datetime.now(rce.LONDON))
    try:
        system, meta = load_prompt(prompt_path, rubric_path)
        if a.freeze:
            f = freeze(meta, frozen_path, now_fn())
            print(f"Frozen: instructions {f['instructions_sha256'][:12]}… rubric "
                  f"{f['rubric_sha256'][:12]}… -> {rce._rel(frozen_path)}. Commit it.")
            return 0
        if a.batch_dir is None:
            raise JudgeError("pass a batch folder (or --freeze)")
        check_frozen(meta, frozen_path)
        if not 1 <= a.concurrency <= MAX_CONCURRENCY:
            raise JudgeError(f"--concurrency must be 1..{MAX_CONCURRENCY}")
        if a.repeats < 1:
            raise JudgeError("--repeats must be at least 1")
        runs = sorted(set(a.run or [1]))

        manifest = json.loads((a.batch_dir / "manifest.json").read_text(encoding="utf-8"))
        index = json.loads((a.batch_dir / "batch.json").read_text(encoding="utf-8"))
        corpus, corpus_sha = rce.load_corpus(corpus_path)
        if corpus_sha != manifest["corpus"]["sha256"]:
            raise JudgeError("the corpus has changed since this batch was generated")
        by_id = {s.id: s for s in corpus}
        items = []
        for g in index["generations"]:
            if g["run"] not in runs:
                continue
            rec = json.loads((a.batch_dir / g["record"]).read_text(encoding="utf-8"))
            if rec["generator"] != "v1":
                raise JudgeError(f"{rec['generation_id']}: this runner judges v1 records only")
            if rec["notes_sha256"] != by_id[rec["scenario_id"]].notes_sha256:
                raise JudgeError(f"{rec['generation_id']}: notes_sha256 does not match the corpus")
            if rec.get("error") or not rec.get("output"):
                continue
            items.append((rec, G.line_index(by_id[rec["scenario_id"]].notes)))
        if not items:
            raise JudgeError("nothing to judge for the runs given")

        # Blind means blind: a run with blind sheets is judged only once all are scored.
        for run in runs:
            cal = calibration_root / f"{manifest['batch_id']}-r{run}"
            if cal.exists():
                complete, _ = mbs.check(cal, corpus_path)
                n = len(json.loads((cal / "MANIFEST.json").read_text(encoding="utf-8"))["items"])
                if complete < n:
                    raise JudgeError(f"blind scoring of r{run} is not finished ({complete}/{n}) — "
                                     "the judge does not run before it is")
    except (JudgeError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    n_calls = len(items) * a.repeats
    start = now_fn()
    est_s = n_calls * EST_SECONDS / a.concurrency
    end = start + dt.timedelta(seconds=est_s)
    judge_id = a.judge_id or f"{start:%Y-%m-%dT%H%M}-r{'-'.join(map(str, runs))}-x{a.repeats}"
    out_dir = a.batch_dir / "judge" / judge_id
    est_cost = estimate_cost_usd(n_calls)
    print(f"Judge:       {judge_id} ({MODEL_ID}, {REGION}, temperature {TEMPERATURE})")
    print(f"Prompt:      instructions {meta['instructions_sha256'][:12]}… rubric "
          f"{meta['rubric_sha256'][:12]}… (frozen)")
    print(f"Items:       {len(items)} generations x {a.repeats} = {n_calls} calls, "
          f"concurrency {a.concurrency}")
    print(f"Estimate:    <= ${est_cost:.2f} (upper bound, no cache discount); "
          f"~{est_s / 60:.0f} min, finishing ~{end:%H:%M} London")
    print(f"Output:      {rce._rel(out_dir)}")
    clash = rce.canary_window_clash(start, end)
    if clash:
        print(f"refused: the projected run overlaps a canary window "
              f"({clash[0]:%a %H:%M}–{clash[1]:%H:%M} London). Start later.", file=sys.stderr)
        return 3
    if out_dir.exists():
        print(f"refused: {rce._rel(out_dir)} already exists — never overwritten.", file=sys.stderr)
        return 2
    if a.dry_run:
        print("Dry run: frozen prompt matches; canary windows clear; no client created, nothing written.")
        return 0
    if boto3 is None and client_factory is None:
        print("error: boto3 is not installed", file=sys.stderr)
        return 2
    if not a.yes and confirm(f"Run {n_calls} live judge calls (est. <= ${est_cost:.2f})? Type yes: "
                             ).strip().lower() != "yes":
        print("Not run.")
        return 1

    out_dir.mkdir(parents=True)
    (out_dir / "manifest.json").write_text(json.dumps({
        "judge_version": JUDGE_VERSION, "judge_id": judge_id, "batch_id": manifest["batch_id"],
        "created_at": start.isoformat(timespec="seconds"), "model_id": MODEL_ID, "region": REGION,
        "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS, "prompt": meta,
        "frozen": json.loads(frozen_path.read_text(encoding="utf-8")),
        "runs": runs, "repeats": a.repeats, "concurrency": a.concurrency,
        "corpus_sha256": corpus_sha,
        "estimate": {"cost_usd_upper_bound": est_cost, "seconds": est_s},
    }, indent=2) + "\n", encoding="utf-8")

    client = (client_factory or (lambda: boto3.client("bedrock-runtime", region_name=REGION)))()
    lock = threading.Lock()
    results, stopped = [], False

    def work(rec, lines, k):
        t = now_fn()
        r = judge_one(client, rec, lines, system, meta, judge_id=judge_id, repeat=k,
                      started_at=t.isoformat(timespec="seconds"), sleep=sleep)
        d = out_dir / f"rep{k}" / f"r{rec['run']}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rec['scenario_id']}.json").write_text(json.dumps(r, indent=2) + "\n", encoding="utf-8")
        with lock:
            results.append(r)
            if r["error"]:
                print(f"  rep{k} r{rec['run']} {rec['scenario_id']:<4} ERROR {r['error']['code']}")
            else:
                c = r["call"]
                print(f"  rep{k} r{rec['run']} {rec['scenario_id']:<4} ok {r['elapsed_s']:5.1f}s "
                      f"in/out={c['input_tokens']}/{c['output_tokens']} "
                      f"cache r/w={c['cache_read_tokens']}/{c['cache_write_tokens']} "
                      f"defects={len(r['verified']['defects'])}")

    with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        futures = []
        for k in range(1, a.repeats + 1):
            for rec, lines in items:
                t = now_fn()
                if rce.canary_window_clash(t, t + dt.timedelta(seconds=EST_SECONDS)):
                    print("  stopping: a canary window is about to open; nothing more submitted.")
                    stopped = True
                    break
                futures.append(pool.submit(work, rec, lines, k))
            if stopped:
                break
        for f in futures:
            f.result()

    errors = sum(1 for r in results if r["error"])
    defects = sum(len(r["verified"]["defects"]) for r in results if r["verified"])
    cost = sum(r.get("cost_usd_est") or 0 for r in results)
    summary = {"calls": len(results), "errors": errors, "defects": defects,
               "stopped_early": stopped, "cost_usd_est": round(cost, 4)}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\n{len(results)} calls, {errors} errors, {defects} judge defects, "
          f"est. ${cost:.2f}{' (stopped early)' if stopped else ''} -> {rce._rel(out_dir)}")
    return 0 if not errors and not stopped else 1


if __name__ == "__main__":
    sys.exit(main())
