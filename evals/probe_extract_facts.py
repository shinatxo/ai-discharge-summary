"""W2 part 2 — the first real extract_facts calls (ADR-009 Q3 (4): grammar compilation).

Runs step 1 -> step 2 -> step 3 on synthetic canary scenarios from a laptop,
with the author's own AWS credentials, against the pinned model in eu-west-2,
and prints NUMBERS AND FIXED CODES ONLY — never note text, never a facts value.

The measurement it exists for can be taken ONCE: Bedrock compiles the
record_facts grammar on the first request that uses this exact schema and
caches it for 24 h from first access. So:
  1. run it dry first (the default — a fake client, no AWS) and check the output;
  2. then run with --live.
Order on --live: A5 call 1 (compile), A5 call 2 (grammar cached), then S12, S8.
Prompt caching is OFF throughout, so call 1 vs call 2 differs by the grammar
compile (and ordinary run-to-run variance), not by a cached prompt prefix.

Usage (from the repo root, branch feat/agentic-pipeline):
  /tmp/py313/bin/python evals/probe_extract_facts.py                 # dry run
  AWS_PROFILE=<you> /tmp/py313/bin/python evals/probe_extract_facts.py --live
  ... --live --save-facts    # also write the facts objects to docs/_local/ (gitignored)

--save-facts writes the model's facts objects (which quote the synthetic notes)
to docs/_local/, which .gitignore excludes. Synthetic notes only; never point
this script at real notes.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

import botocore  # noqa: E402
import botocore.session  # noqa: E402

from pipeline import StepContext, StepError, run_step  # noqa: E402
from pipeline.extract import MAX_TOKENS, system_prompt, tool_config  # noqa: E402
from pipeline.schemas import FIELD_NAMES, record_facts_schema  # noqa: E402

REGION = "eu-west-2"
MODEL_ID = "anthropic.claude-sonnet-4-6"          # infra/template.yaml ModelId default (pinned)
READ_TIMEOUT_S = 90                               # ADR-009 Q3 (2): step 2
PLAN = [("A5", 1), ("A5", 2), ("S12", 1), ("S8", 1)]
LONDON = ZoneInfo("Europe/London")


# --- guards ------------------------------------------------------------------

def canary_window(now: dt.datetime) -> str | None:
    """The quota is account-wide: no Bedrock calls near the canary (02:00 daily, 03:00 Mon)."""
    t = now.hour * 60 + now.minute
    if 105 <= t < 165:                                   # 01:45-02:45 every day
        return "nightly canary window (01:45-02:45 Europe/London)"
    if now.weekday() == 0 and 165 <= t < 225:           # Mon 02:45-03:45
        return "weekly canary window (Mon 02:45-03:45 Europe/London)"
    return None


def sdk_knows_strict() -> bool:
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    return "strict" in model.shape_for("ToolSpecification").members


# --- fake client for the dry run ---------------------------------------------

class DryRunBedrock:
    """Answers every call with a schema-valid, all-'not_documented' facts object
    after a short sleep, so the whole path (request, response parsing, step 3,
    report) runs without AWS."""

    def converse(self, **kw):
        assert kw["toolConfig"] == tool_config()
        time.sleep(0.05)
        facts = {
            "field_status": [{"field": n, "status": "not_documented"} for n in FIELD_NAMES],
            "facts": [],
            "discharge_status": "not_documented",
            "resus": {"form_or_discussion_documented": False, "status_documented": "not_documented",
                      "changed": "not_documented", "cites": []},
            "age_group": "not_documented", "suspicious_text": [],
        }
        return {"output": {"message": {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": "dry", "name": "record_facts", "input": facts}}]}},
                "stopReason": "tool_use",
                "usage": {"inputTokens": 0, "outputTokens": 0},
                "metrics": {"latencyMs": 50}}


def live_client():
    import boto3
    from botocore.config import Config
    cfg = Config(connect_timeout=10, read_timeout=READ_TIMEOUT_S,
                 retries={"mode": "standard", "max_attempts": 1})   # retries: the step's job
    session = boto3.Session(region_name=REGION)
    ident = session.client("sts").get_caller_identity()
    print(f"caller: {ident['Arn'].split(':')[-1]}  account: …{ident['Account'][-4:]}  region: {REGION}")
    return session.client("bedrock-runtime", config=cfg)


# --- report (numbers and fixed words only) -------------------------------------

def summarise(scenario: str, n: int, wall_s: float, step2: dict, step1: dict, step3: dict) -> dict:
    facts, counts, call = step3["facts"], step3["counts"], step2["call"]
    reasons = collections.Counter()
    for f in FIELD_NAMES:
        for item in facts["fields"][f]["items"]:
            for c in item["cites"]:
                if not c["verified"]:
                    reasons[c["reason"]] += 1
    for grp in ("pre_admission", "discharge"):
        for item in facts["medications"][grp]:
            for c in item["cites"]:
                if not c["verified"]:
                    reasons[c["reason"]] += 1
    for key in ("documented_advice", "contradictions"):
        for item in facts[key]:
            for c in item["cites"]:
                if not c["verified"]:
                    reasons[c["reason"]] += 1
    for c in facts["resus"].get("cites", []):
        if not c["verified"]:
            reasons[c["reason"]] += 1
    for c in facts["suspicious_text"]:
        if not c["verified"]:
            reasons[c["reason"]] += 1

    status = collections.Counter(facts["fields"][f]["status"] for f in FIELD_NAMES)
    rate = counts["cites_verified"] / counts["cites_total"] if counts["cites_total"] else None
    return {
        "scenario": scenario, "call": n,
        "wall_s": round(wall_s, 1),
        "bedrock_latency_ms": call["latency_ms"],
        "stop_reason": call["stop_reason"], "attempts": call["attempts"],
        "input_tokens": call["input_tokens"], "output_tokens": call["output_tokens"],
        "cache_read_tokens": call["cache_read_tokens"], "cache_write_tokens": call["cache_write_tokens"],
        "line_count": step1["line_count"], "guard_flags": len(step1["flags"]),
        "cites_total": counts["cites_total"], "cites_verified": counts["cites_verified"],
        "verify_rate": None if rate is None else round(rate, 3),
        "unverified_reasons": dict(reasons),
        "items_unverified": counts["items_unverified"],
        "fields_inconsistent": counts["fields_inconsistent"],
        "medications_check": facts["medications"]["medications_check"],
        "uncited_lines": len(step3["uncited_lines"]),
        "field_status": dict(status),
        "pre_admission_items": len(facts["medications"]["pre_admission"]),
        "discharge_items": len(facts["medications"]["discharge"]),
        "discharge_status": facts["medications"]["discharge_status"],
        "resus": {k: facts["resus"][k] for k in
                  ("form_or_discussion_documented", "status_documented", "changed")},
        "resus_citation_status": facts["resus"]["citation_status"],
        "age_group": facts["age_group"],
        "advice_items": len(facts["documented_advice"]),
        "contradictions": len(facts["contradictions"]),
        "suspicious_text": len(facts["suspicious_text"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="call Bedrock (default: dry run, no AWS)")
    ap.add_argument("--save-facts", action="store_true",
                    help="write facts objects to docs/_local/ (gitignored; synthetic notes only)")
    args = ap.parse_args()

    now = dt.datetime.now(LONDON)
    scenarios = {s["id"]: s["notes"] for s in
                 json.loads((ROOT / "src" / "canary" / "scenarios.json").read_text())["scenarios"]}
    prompt, prompt_sha = system_prompt()
    schema_sha = hashlib.sha256(json.dumps(record_facts_schema(), sort_keys=True).encode()).hexdigest()

    print(f"mode: {'LIVE' if args.live else 'DRY RUN (no AWS)'}   {now:%Y-%m-%d %H:%M %Z}")
    print(f"model: {MODEL_ID}  maxTokens: {MAX_TOKENS}  read_timeout: {READ_TIMEOUT_S}s  prompt_caching: off")
    print(f"prompt_sha256: {prompt_sha[:12]}  schema_sha256: {schema_sha[:12]}  "
          f"botocore: {botocore.__version__}  strict known: {sdk_knows_strict()}")

    if args.live:
        if not sdk_knows_strict():
            print("STOP: this botocore does not know toolSpec.strict — upgrade boto3 first.")
            return 2
        why = canary_window(now)
        if why:
            print(f"STOP: {why}.")
            return 2
        client = live_client()
    else:
        client = DryRunBedrock()

    ctx = StepContext(bedrock=client, model_id=MODEL_ID, prompt_caching=False)
    results, saved = [], {}
    for scenario, n in PLAN:
        step1 = run_step("guard_input", {"notes": scenarios[scenario]})
        t0 = time.monotonic()
        try:
            step2 = run_step("extract_facts", {"lines": step1["lines"]}, ctx=ctx)
        except StepError as e:
            wall = time.monotonic() - t0
            row = {"scenario": scenario, "call": n, "wall_s": round(wall, 1), "step_error": e.code}
            print(json.dumps(row))
            results.append(row)
            if scenario == "A5" and n == 1:
                print("STOP: the compile call failed — fix before spending more calls.")
                break
            continue
        wall = time.monotonic() - t0
        try:
            step3 = run_step("validate_facts", {"facts": step2["facts"], "lines": step1["lines"]})
        except StepError as e:
            row = {"scenario": scenario, "call": n, "wall_s": round(wall, 1),
                   "stop_reason": step2["call"]["stop_reason"], "step3_error": e.code}
            print(json.dumps(row))
            results.append(row)
            continue
        row = summarise(scenario, n, wall, step2, step1, step3)
        print(json.dumps(row))
        results.append(row)
        saved[f"{scenario}#{n}"] = step3

    a5 = [r for r in results if r["scenario"] == "A5" and "wall_s" in r and "step_error" not in r]
    if len(a5) == 2:
        d = a5[0]["wall_s"] - a5[1]["wall_s"]
        per_tok = [r["wall_s"] / r["output_tokens"] for r in a5 if r.get("output_tokens")]
        print(f"\nA5 call 1 − call 2 wall time: {d:+.1f} s  (compile estimate; outputs "
              f"{a5[0].get('output_tokens')} vs {a5[1].get('output_tokens')} tokens)")
        if len(per_tok) == 2:
            print(f"seconds per output token: call 1 {per_tok[0]:.4f}, call 2 {per_tok[1]:.4f}")

    if args.save_facts and args.live:
        out = ROOT / "docs" / "_local" / f"probe-extract-{now:%Y%m%d-%H%M}.json"
        out.write_text(json.dumps({"prompt_sha256": prompt_sha, "schema_sha256": schema_sha,
                                   "results": results, "facts": saved}, indent=2))
        print(f"facts saved: {out.relative_to(ROOT)} (gitignored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
