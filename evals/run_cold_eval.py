#!/usr/bin/env python3
"""
Cold-eval runner — v1 (the live prompts) on the canonical 18-scenario corpus.

"Cold" means Bedrock is called directly from your laptop with the prompts the
worker Lambda ships, bypassing API Gateway, Cognito, the dispatcher and DynamoDB.
Any difference between two runs is then attributable to the prompt or the model,
not to the stack around it.

WHAT CHANGED IN W4 (ADR-009 "Build record — W4")
------------------------------------------------
- **Input: `src/canary/scenarios.json`, the same 18 scenarios the canary sends.**
  The old runner read `evals/scenarios/eval-scenarios-expansion.md`, which holds
  only S8-S18, and sent each scenario's notes *inside its markdown code fence*.
  The deployed path never sees a fence, so those runs were not byte-faithful.
  Notes are now `.strip()`ped exactly as the dispatcher does before it hashes
  them, so `notes_sha256` here equals the audit row's `input_sha256`.
- **Default prompts: `src/generate/*.md`** — the files the worker zip contains.
  (`prompts/discharge-summary-system-prompt.md` is a copy; a test pins that the
  two bodies are identical.)
- **Batches, not dated folders.** A batch is `--runs N` repeats of the chosen
  scenarios, written to `evals/runs/<batch_id>/r1 … rN/`. The runner refuses to
  write into a folder that exists — the 15 Sep near-miss, where a v2 run
  overwrote a v1 run from the same day, cannot recur.
- **Machine-readable records.** Each generation writes `<ID>.json` (every call's
  tokens, prompt and notes hashes, gate result, estimated cost) beside the
  human-readable `<ID>.md`. The JSON does not copy the notes: it carries
  `notes_sha256`, and the scorer reloads the notes from the corpus and checks
  the hash. Scoring is a separate step, so a gold correction re-scores old
  outputs without re-running Bedrock.
- **Both calls are costed.** On the deployed path the Patient v2 second pass is
  a second Bedrock call; the old SUMMARY.md recorded only the first.
- **Canary-window guard.** The canary schedules (`infra/template.yaml`,
  `ScheduleExpressionTimezone: Europe/London`) fire at 02:00 nightly and 03:00
  Monday. The quota is account-wide (ADR-009 F5), so the runner refuses a batch
  whose projected run overlaps either window, and stops cleanly if a slow batch
  drifts into one.
- **Throttle-only retry**, at most twice per call, 20-30 s jittered. Timeouts and
  every other error are never retried (the 6 Jun 2026 lesson).
- **Console hygiene.** The console shows scenario IDs, timings, tokens and gate
  status only — never note text, model output or gate findings, which may quote
  either. Those stay in the run files.
- **`--dry-run`** prints the plan, the cost upper bound and the window check
  without creating a Bedrock client. A live run asks for confirmation unless
  `--yes` is given.

USAGE
-----
    # The W4 baseline: all 18, deployed path, five runs. Dry run first.
    python evals/run_cold_eval.py --all --runs 5 --deployed-path --dry-run
    python evals/run_cold_eval.py --all --runs 5 --deployed-path

    # A spot check of two scenarios, one run, v1 combined path only
    python evals/run_cold_eval.py S14 S18

REQUIREMENTS
------------
AWS credentials with bedrock:InvokeModel on anthropic.claude-sonnet-4-6 in
eu-west-2, and boto3.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import safety_net_gate  # noqa: E402  (local module, same folder)

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # main() refuses a live run without boto3; --dry-run still works
    boto3 = None

    class ClientError(Exception):  # type: ignore[no-redef]
        response: dict = {}


# ---------------------------------------------------------------------------
# Match the deployed worker's invocation (src/generate/app.py). If the worker
# changes any of these, change them here too.
# ---------------------------------------------------------------------------
REGION = "eu-west-2"
MODEL_ID = "anthropic.claude-sonnet-4-6"
MAX_TOKENS = 4096
TEMPERATURE = 0.0
PATIENT_MAX_TOKENS = 1500
SYSTEM_PROMPT_MARKER = "## SYSTEM PROMPT"

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT_PATH = REPO / "src" / "generate" / "system_prompt.md"
DEFAULT_PATIENT_PROMPT_PATH = REPO / "src" / "generate" / "patient_system_prompt.md"
DEFAULT_CORPUS_PATH = REPO / "src" / "canary" / "scenarios.json"
DEFAULT_RUNS_DIR = REPO / "evals" / "runs"

RECORD_VERSION = 1
GATE_CANONICAL_NAME = "V1_LINES"   # v1's live prompts pin the v1 line (ADR-009 W3)

# Price basis for cost_usd_est. AN ESTIMATE — Cost Explorer is the source of truth
# (docs/COST.md). Sonnet 4.6 on-demand, $/million tokens; cache write 1.25x and
# cache read 0.1x the input price. The 10% regional premium is ADR-009 Q3's open
# question (is single-region eu-west-2 on-demand a "regional endpoint"?), so it is
# applied: the estimate errs high.
PRICE_PER_MTOK = {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}
REGIONAL_PREMIUM = 1.10

# Planning figures for --dry-run and the window check, from the 15 Sep runs
# (combined call 29-40 s; deployed path 44-57 s) with headroom.
EST_SECONDS = {False: 50, True: 75}                    # keyed by second pass on/off
EST_TOKENS = {False: {"input": 5600, "output": 2600},
              True: {"input": 8900, "output": 3500}}   # no cache discount: an upper bound

MAX_THROTTLE_RETRIES = 2
THROTTLE_WAIT_S = (20.0, 30.0)

# The canary schedules. Monday is weekday 0; None means every day.
LONDON = ZoneInfo("Europe/London")
CANARY_STARTS = ((None, 2, 0), (0, 3, 0))
WINDOW_BEFORE = dt.timedelta(minutes=30)
WINDOW_AFTER = dt.timedelta(minutes=20)    # the canary Lambda runs up to 900 s


# ---------------------------------------------------------------------------
# Corpus, prompts, hashes
# ---------------------------------------------------------------------------
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    notes: str

    @property
    def notes_sha256(self) -> str:
        return sha256_text(self.notes)


def load_corpus(path: Path) -> tuple[list[Scenario], str]:
    """The scenarios in file order, and the corpus file's sha256.

    Notes are stripped, as the dispatcher strips them before hashing and before
    the worker sees them. Errors name scenario IDs only, never note text."""
    raw = path.read_bytes()
    data = json.loads(raw)
    scenarios: list[Scenario] = []
    seen: set[str] = set()
    for item in data["scenarios"]:
        sid = item["id"]
        if sid in seen:
            raise ValueError(f"duplicate scenario id in the corpus: {sid}")
        seen.add(sid)
        notes = (item.get("notes") or "").strip()
        if not notes:
            raise ValueError(f"empty notes in the corpus: {sid}")
        scenarios.append(Scenario(sid, item.get("title", ""), notes))
    return scenarios, sha256_bytes(raw)


def select_scenarios(corpus: list[Scenario], ids: list[str], all_: bool) -> list[Scenario]:
    if all_ and ids:
        raise ValueError("pass scenario IDs or --all, not both")
    if not all_ and not ids:
        raise ValueError("pass scenario IDs, or --all for the whole corpus")
    if all_:
        return list(corpus)
    by_id = {s.id: s for s in corpus}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"not in the corpus: {', '.join(unknown)}")
    if len(set(ids)) != len(ids):
        raise ValueError("a scenario ID is repeated; use --runs for repeats")
    return [by_id[i] for i in ids]


def load_system_prompt(prompt_path: Path) -> str:
    """Mirror the worker's _load_system_prompt(): everything after the
    '## SYSTEM PROMPT' marker line, stripped."""
    raw = prompt_path.read_text(encoding="utf-8")
    idx = raw.find(SYSTEM_PROMPT_MARKER)
    if idx == -1:
        raise RuntimeError(f"{prompt_path} is missing the '{SYSTEM_PROMPT_MARKER}' marker")
    body = raw[idx:]
    body = body.split("\n", 1)[1] if "\n" in body else body
    return body.strip()


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def _prompt_meta(path: Path, body: str) -> dict:
    """file_sha256 is what `git show main:<path> | shasum -a 256` prints, so the
    run can be proved to have used main's prompt; body_sha256 is what was sent."""
    return {"path": _rel(path), "file_sha256": sha256_bytes(path.read_bytes()),
            "body_sha256": sha256_text(body)}


# ---------------------------------------------------------------------------
# Output splitting (as the worker's _split_outputs)
# ---------------------------------------------------------------------------
_PART_MARKER = {
    label: re.compile(rf"(?im)^[#*>\s]*PART\s+{label}\b.*$") for label in ("A", "B", "C")
}


def extract_part_a(combined: str) -> str:
    """PART A out of a combined output: A..B if both present, else the head up to
    B. This is the second pass's only input."""
    pos = {}
    for label in ("A", "B", "C"):
        m = _PART_MARKER[label].search(combined)
        if m:
            pos[label] = m.start()
    if {"A", "B"} <= pos.keys() and pos["A"] < pos["B"]:
        return combined[pos["A"]:pos["B"]].strip()
    if "B" in pos and pos["B"] > 0:
        return combined[:pos["B"]].strip()
    return combined.strip()


def _strip_part_c_marker(text: str) -> str:
    m = _PART_MARKER["C"].match(text.lstrip())
    if not m:
        return text.strip()
    return text.lstrip()[m.end():].strip()


# ---------------------------------------------------------------------------
# Canary windows
# ---------------------------------------------------------------------------
def canary_window_clash(start: dt.datetime, end: dt.datetime):
    """The first canary window (opens, closes) that [start, end] overlaps, or None.
    Datetimes must be timezone-aware."""
    first = (start - WINDOW_AFTER).astimezone(LONDON).date() - dt.timedelta(days=1)
    last = (end + WINDOW_BEFORE).astimezone(LONDON).date() + dt.timedelta(days=1)
    day = first
    while day <= last:
        for weekday, hour, minute in CANARY_STARTS:
            if weekday is not None and day.weekday() != weekday:
                continue
            fires = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=LONDON)
            opens, closes = fires - WINDOW_BEFORE, fires + WINDOW_AFTER
            if start < closes and end > opens:
                return opens, closes
        day += dt.timedelta(days=1)
    return None


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
def cost_usd(calls: list[dict]) -> float:
    total = 0.0
    for c in calls:
        total += (c.get("input_tokens") or 0) * PRICE_PER_MTOK["input"]
        total += (c.get("output_tokens") or 0) * PRICE_PER_MTOK["output"]
        total += (c.get("cache_write_tokens") or 0) * PRICE_PER_MTOK["cache_write"]
        total += (c.get("cache_read_tokens") or 0) * PRICE_PER_MTOK["cache_read"]
    return round(total / 1_000_000 * REGIONAL_PREMIUM, 6)


def estimate_cost_usd(n_generations: int, second_pass: bool) -> float:
    t = EST_TOKENS[second_pass]
    return cost_usd([{"input_tokens": t["input"] * n_generations,
                      "output_tokens": t["output"] * n_generations}])


# ---------------------------------------------------------------------------
# One generation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    system_prompt: str
    system_meta: dict
    patient_prompt: str | None
    patient_meta: dict | None
    prompt_caching: bool
    gate: bool

    @property
    def second_pass(self) -> bool:
        return self.patient_prompt is not None

    @property
    def path_label(self) -> str:
        if self.second_pass and self.prompt_caching:
            return "deployed"
        return "combined" + ("+patient-v2" if self.second_pass else "") + (
            "+cache" if self.prompt_caching else "")


def _system_blocks(system_prompt: str, prompt_caching: bool) -> list:
    """The Converse `system` array, with a cachePoint after the prompt when
    caching is on — as the worker's _converse."""
    blocks = [{"text": system_prompt}]
    if prompt_caching:
        blocks.append({"cachePoint": {"type": "default"}})
    return blocks


def _converse(client, system_prompt: str, user_text: str, max_tokens: int,
              prompt_caching: bool, sleep):
    """One Converse call. Retries ThrottlingException only, at most twice."""
    retries = 0
    while True:
        t0 = time.time()
        try:
            resp = client.converse(
                modelId=MODEL_ID,
                system=_system_blocks(system_prompt, prompt_caching),
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": TEMPERATURE},
            )
            return resp, time.time() - t0, retries
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ThrottlingException" and retries < MAX_THROTTLE_RETRIES:
                retries += 1
                sleep(random.uniform(*THROTTLE_WAIT_S))
                continue
            raise


def _call_record(name: str, resp: dict, latency_s: float, retries: int) -> dict:
    usage = resp.get("usage", {}) or {}
    return {
        "name": name,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "cache_read_tokens": usage.get("cacheReadInputTokens"),
        "cache_write_tokens": usage.get("cacheWriteInputTokens"),
        "stop_reason": resp.get("stopReason"),
        "latency_s": round(latency_s, 3),
        "throttle_retries": retries,
    }


def _text(resp: dict) -> str:
    return resp["output"]["message"]["content"][0]["text"]


def run_generation(client, scenario: Scenario, cfg: Config, *, batch_id: str,
                   run: int, started_at: str, sleep=time.sleep) -> dict:
    """Generate one output the way the deployed worker would, gate it, and
    return its record. Never raises for a Bedrock or parsing failure: the record
    carries `error`, and the calls made before it (for cost)."""
    calls: list[dict] = []
    output = None
    patient_version = "v1"
    error = None
    t0 = time.time()
    try:
        resp, lat, rt = _converse(client, cfg.system_prompt, scenario.notes, MAX_TOKENS,
                                  cfg.prompt_caching, sleep)
        calls.append(_call_record("combined", resp, lat, rt))
        output = _text(resp)
        if cfg.second_pass:
            summary = extract_part_a(output)
            if summary:
                resp2, lat2, rt2 = _converse(client, cfg.patient_prompt, summary,
                                             PATIENT_MAX_TOKENS, cfg.prompt_caching, sleep)
                calls.append(_call_record("patient_v2", resp2, lat2, rt2))
                leaflet = _strip_part_c_marker(_text(resp2))
                if leaflet:
                    # Replace PART C with the v2 leaflet: the stored output is what
                    # the deployed worker would store.
                    cpos = _PART_MARKER["C"].search(output)
                    head = output[:cpos.start()].rstrip() if cpos else output.rstrip()
                    output = f"{head}\n\nPART C - PATIENT VERSION (v2 second pass)\n\n{leaflet}\n"
                    patient_version = "v2"
    except ClientError as exc:
        err = exc.response.get("Error", {})
        error = {"code": err.get("Code", "Unknown"), "message": err.get("Message", "")}
    except Exception as exc:  # noqa: BLE001 — recorded, never re-raised mid-batch
        error = {"code": type(exc).__name__, "message": str(exc)}

    gate = None
    if cfg.gate and output is not None and error is None:
        g = safety_net_gate.check_combined(scenario.notes, output, safety_net_gate.V1_LINES)
        gate = {"canonical": GATE_CANONICAL_NAME, "status": g.status, "ok": g.ok,
                "label": g.label, "findings": g.findings}

    return {
        "record_version": RECORD_VERSION,
        "generation_id": f"{batch_id}/r{run}/{scenario.id}",
        "batch_id": batch_id,
        "run": run,
        "scenario_id": scenario.id,
        "generator": "v1",
        "path": cfg.path_label,
        "model_id": MODEL_ID,
        "region": REGION,
        "inference": {"max_tokens": MAX_TOKENS, "temperature": TEMPERATURE,
                      "patient_max_tokens": PATIENT_MAX_TOKENS if cfg.second_pass else None},
        "prompts": {"system": cfg.system_meta, "patient": cfg.patient_meta},
        "prompt_caching": cfg.prompt_caching,
        "notes_sha256": scenario.notes_sha256,
        "started_at": started_at,
        "elapsed_s": round(time.time() - t0, 3),
        "calls": calls,
        "patient_version": patient_version,
        "output": output,
        "gate": gate,
        "cost_usd_est": cost_usd(calls),
        "error": error,
    }


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def _tokens(rec: dict, key: str) -> int:
    return sum((c.get(key) or 0) for c in rec["calls"])


def write_generation(rec: dict, scenario: Scenario, run_dir: Path) -> None:
    (run_dir / f"{scenario.id}.json").write_text(
        json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    calls = "".join(
        f"- Call `{c['name']}`: {c['input_tokens']} in / {c['output_tokens']} out, "
        f"cache read {c['cache_read_tokens']} / write {c['cache_write_tokens']}, "
        f"`{c['stop_reason']}`, {c['latency_s']:.1f}s, throttle retries {c['throttle_retries']}\n"
        for c in rec["calls"])
    gate = rec["gate"]
    gate_md = ""
    if gate:
        gate_md = f"- Safety-net gate (`{gate['canonical']}`): **{gate['label']}** (`{gate['status']}`)\n"
        gate_md += "".join(f"  - {f}\n" for f in gate["findings"])
    err_md = f"- **Error:** `{rec['error']['code']}` — {rec['error']['message']}\n" if rec["error"] else ""
    body = (
        f"# {scenario.id} — {scenario.title}\n\n"
        f"- Generation: `{rec['generation_id']}`\n"
        f"- Generator: v1 · path `{rec['path']}` · patient version `{rec['patient_version']}`\n"
        f"- Model: `{MODEL_ID}` ({REGION}, on-demand), `maxTokens={MAX_TOKENS}`, "
        f"`temperature={TEMPERATURE}`\n"
        f"- Prompt: `{rec['prompts']['system']['path']}` (file sha256 "
        f"`{rec['prompts']['system']['file_sha256'][:12]}…`)\n"
        f"- Notes sha256: `{rec['notes_sha256']}`\n"
        f"- Started: {rec['started_at']} · elapsed {rec['elapsed_s']:.1f}s · "
        f"est. ${rec['cost_usd_est']:.4f}\n"
        f"{calls}{gate_md}{err_md}\n"
        f"## INPUT (ward-round notes, as sent)\n\n```\n{scenario.notes}\n```\n\n"
        f"## OUTPUT\n\n{rec['output'] or '(none)'}\n"
    )
    (run_dir / f"{scenario.id}.md").write_text(body, encoding="utf-8")


def _gate_cell(rec: dict) -> str:
    if rec["error"]:
        return f"ERROR `{rec['error']['code']}`"
    if not rec["gate"]:
        return "not run"
    g = rec["gate"]
    return ("PASS" if g["ok"] else "**FAIL**") + f" ({g['status']})"


def write_run_summary(run_dir: Path, run: int, records: list[dict], manifest: dict) -> None:
    lines = [
        f"# Run r{run} — batch `{manifest['batch_id']}`", "",
        f"- Generator: v1 · path `{manifest['path']}` · model `{MODEL_ID}` ({REGION})",
        f"- Prompt file sha256: `{manifest['prompts']['system']['file_sha256']}`",
        f"- Generations: {len(records)}", "",
        "| Scenario | Safety-net gate | Patient | Elapsed | In | Out | Cache read | Cache write | Stop | Est. $ |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in records:
        stops = ",".join(str(c["stop_reason"]) for c in r["calls"]) or "—"
        lines.append(
            f"| {r['scenario_id']} | {_gate_cell(r)} | {r['patient_version']} | {r['elapsed_s']:.1f}s | "
            f"{_tokens(r, 'input_tokens')} | {_tokens(r, 'output_tokens')} | "
            f"{_tokens(r, 'cache_read_tokens')} | {_tokens(r, 'cache_write_tokens')} | "
            f"`{stops}` | {r['cost_usd_est']:.4f} |")
    total = sum(r["cost_usd_est"] for r in records)
    lines += ["", f"**Run total:** est. ${total:.4f}, "
              f"{sum(r['elapsed_s'] for r in records):.0f}s.", ""]
    (run_dir / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def tier1_5_verdict(records: list[dict], n_scenarios: int, stopped: bool) -> tuple[bool, str]:
    """Safety case §12.3 Tier 1 #5: >= 18 scenarios, gate PASS on all. Set in
    advance (W4 decision): PASS on EVERY generation of EVERY run, no errors, the
    batch not stopped early. A failure is recorded as evidence, not re-run away."""
    passed = sum(1 for r in records if not r["error"] and r["gate"] and r["gate"]["ok"])
    met = (n_scenarios >= 18 and not stopped and records
           and passed == len(records))
    why = (f"{passed}/{len(records)} generations gate PASS across {n_scenarios} scenarios"
           + ("; batch stopped early" if stopped else ""))
    return bool(met), why


def write_batch_summary(batch_dir: Path, records: list[dict], manifest: dict,
                        scenarios: list[Scenario], runs: int, stopped: bool) -> None:
    met, why = tier1_5_verdict(records, len(scenarios), stopped)
    by = {(r["scenario_id"], r["run"]): r for r in records}
    head = " | ".join(f"r{k}" for k in range(1, runs + 1))
    lines = [
        f"# Batch `{manifest['batch_id']}`", "",
        f"- Created: {manifest['created_at']}",
        f"- Generator: v1 · path `{manifest['path']}` · model `{MODEL_ID}` ({REGION}) · "
        f"`maxTokens={MAX_TOKENS}`, `temperature={TEMPERATURE}`",
        f"- System prompt: `{manifest['prompts']['system']['path']}` — file sha256 "
        f"`{manifest['prompts']['system']['file_sha256']}`",
    ]
    if manifest["prompts"]["patient"]:
        lines.append(f"- Patient prompt: `{manifest['prompts']['patient']['path']}` — file sha256 "
                     f"`{manifest['prompts']['patient']['file_sha256']}`")
    lines += [
        f"- Corpus: `{manifest['corpus']['path']}` — sha256 `{manifest['corpus']['sha256']}`",
        f"- Gate: `safety_net_gate.check_combined(..., {GATE_CANONICAL_NAME})`",
        f"- Runs: {runs} · scenarios: {len(scenarios)} · generations recorded: {len(records)}",
        "",
        f"**Safety case Tier 1 #5 completion test** (gate PASS on every generation of every "
        f"run, ≥ 18 scenarios, no errors): **{'MET' if met else 'NOT MET'}** — {why}.",
        "",
        f"| Scenario | {head} | PASS |",
        "| --- | " + " | ".join("---" for _ in range(runs)) + " | --- |",
    ]
    for s in scenarios:
        cells, n_pass, n = [], 0, 0
        for k in range(1, runs + 1):
            r = by.get((s.id, k))
            if r is None:
                cells.append("—")
                continue
            n += 1
            n_pass += bool(not r["error"] and r["gate"] and r["gate"]["ok"])
            cells.append(_gate_cell(r))
        lines.append(f"| {s.id} | " + " | ".join(cells) + f" | {n_pass}/{n} |")
    total_cost = sum(r["cost_usd_est"] for r in records)
    lines += [
        "",
        f"**Batch totals:** est. ${total_cost:.4f} (price basis in `manifest.json`; Cost "
        f"Explorer is the source of truth) · input {sum(_tokens(r, 'input_tokens') for r in records)} · "
        f"output {sum(_tokens(r, 'output_tokens') for r in records)} · "
        f"cache read {sum(_tokens(r, 'cache_read_tokens') for r in records)} · "
        f"cache write {sum(_tokens(r, 'cache_write_tokens') for r in records)} · "
        f"throttle retries {sum(c['throttle_retries'] for r in records for c in r['calls'])} · "
        f"errors {sum(1 for r in records if r['error'])}"
        + (" · **stopped early (canary window)**" if stopped else ""),
        "",
    ]
    (batch_dir / "BATCH_SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    index = [{"run": r["run"], "scenario_id": r["scenario_id"],
              "record": f"r{r['run']}/{r['scenario_id']}.json",
              "gate_status": r["gate"]["status"] if r["gate"] else None,
              "gate_ok": r["gate"]["ok"] if r["gate"] else None,
              "error_code": r["error"]["code"] if r["error"] else None,
              "cost_usd_est": r["cost_usd_est"]} for r in records]
    (batch_dir / "batch.json").write_text(json.dumps(
        {"batch_id": manifest["batch_id"], "tier1_5_met": met, "tier1_5": why,
         "stopped_early": stopped, "generations": index}, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv):
    p = argparse.ArgumentParser(description="Cold-eval runner for the v1 Discharge Summary prompts.")
    p.add_argument("scenarios", nargs="*", help="Scenario IDs (e.g. S14 S18); or use --all")
    p.add_argument("--all", action="store_true", help="every scenario in the corpus, in corpus order")
    p.add_argument("--runs", type=int, default=1, help="repeats of the scenario set (default 1)")
    p.add_argument("--batch", default=None, help="batch ID / folder name (default: timestamped)")
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    p.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT_PATH)
    p.add_argument("--patient-prompt", type=Path, default=DEFAULT_PATIENT_PROMPT_PATH)
    p.add_argument("--patient-second-pass", action="store_true",
                   help="Patient v2: regenerate PART C from PART A in a second call")
    p.add_argument("--prompt-caching", action="store_true", help="cachePoint after the system prompt")
    p.add_argument("--deployed-path", action="store_true",
                   help="= --patient-second-pass --prompt-caching, as CI pins the live stack")
    p.add_argument("--no-gate", dest="gate", action="store_false", help="skip the safety-net gate")
    p.add_argument("--dry-run", action="store_true", help="print the plan; no Bedrock calls")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = p.parse_args(argv)
    if args.deployed_path:
        args.patient_second_pass = True
        args.prompt_caching = True
    if args.runs < 1:
        p.error("--runs must be at least 1")
    if args.batch is not None and not re.fullmatch(r"[A-Za-z0-9._-]+", args.batch):
        p.error("--batch may contain only letters, digits, '.', '_' and '-'")
    return args


def main(argv=None, *, client_factory=None, now=None, sleep=time.sleep,
         confirm=input, runs_dir: Path | None = None) -> int:
    args = parse_args(argv)
    now_fn = now or (lambda: dt.datetime.now(LONDON))
    runs_dir = runs_dir or DEFAULT_RUNS_DIR

    try:
        corpus, corpus_sha = load_corpus(args.corpus)
        scenarios = select_scenarios(corpus, args.scenarios, args.all)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    system_prompt = load_system_prompt(args.prompt)
    patient_prompt = load_system_prompt(args.patient_prompt) if args.patient_second_pass else None
    cfg = Config(system_prompt, _prompt_meta(args.prompt, system_prompt),
                 patient_prompt,
                 _prompt_meta(args.patient_prompt, patient_prompt) if patient_prompt else None,
                 args.prompt_caching, args.gate)

    n_gen = len(scenarios) * args.runs
    est_s = EST_SECONDS[cfg.second_pass]
    start = now_fn()
    end = start + dt.timedelta(seconds=n_gen * est_s)
    batch_id = args.batch or f"{start:%Y-%m-%dT%H%M}-v1-{cfg.path_label}-x{args.runs}"
    batch_dir = runs_dir / batch_id
    est_cost = estimate_cost_usd(n_gen, cfg.second_pass)

    print(f"Batch:       {batch_id}")
    print(f"Generator:   v1, path {cfg.path_label} ({len(scenarios)} scenarios x {args.runs} runs "
          f"= {n_gen} generations, {n_gen * (2 if cfg.second_pass else 1)} Bedrock calls)")
    print(f"Prompt:      {cfg.system_meta['path']} (file sha256 {cfg.system_meta['file_sha256'][:12]}…)")
    if cfg.patient_meta:
        print(f"Patient:     {cfg.patient_meta['path']} (file sha256 {cfg.patient_meta['file_sha256'][:12]}…)")
    print(f"Corpus:      {_rel(args.corpus)} (sha256 {corpus_sha[:12]}…)")
    print(f"Estimate:    <= ${est_cost:.2f} (upper bound, no cache discount); "
          f"~{n_gen * est_s / 60:.0f} min, sequential, finishing ~{end:%H:%M} London")
    print(f"Output:      {_rel(batch_dir)}")

    clash = canary_window_clash(start, end)
    if clash:
        print(f"refused: the projected run overlaps a canary window "
              f"({clash[0]:%a %H:%M}–{clash[1]:%H:%M} London). Start later.", file=sys.stderr)
        return 3
    if batch_dir.exists():
        print(f"refused: {_rel(batch_dir)} already exists — never overwritten.", file=sys.stderr)
        return 2
    if args.dry_run:
        print("Dry run: canary windows clear; no Bedrock client created, nothing written.")
        return 0
    if boto3 is None and client_factory is None:
        print("error: boto3 is not installed (pip install boto3)", file=sys.stderr)
        return 2
    if not args.yes:
        answer = confirm(f"Run {n_gen} live generations (est. <= ${est_cost:.2f})? Type yes: ")
        if answer.strip().lower() != "yes":
            print("Not run.")
            return 1

    batch_dir.mkdir(parents=True)
    manifest = {
        "record_version": RECORD_VERSION, "batch_id": batch_id,
        "created_at": start.isoformat(timespec="seconds"),
        "generator": "v1", "path": cfg.path_label, "model_id": MODEL_ID, "region": REGION,
        "inference": {"max_tokens": MAX_TOKENS, "temperature": TEMPERATURE,
                      "patient_max_tokens": PATIENT_MAX_TOKENS if cfg.second_pass else None},
        "prompts": {"system": cfg.system_meta, "patient": cfg.patient_meta},
        "prompt_caching": cfg.prompt_caching,
        "gate": GATE_CANONICAL_NAME if cfg.gate else None,
        "corpus": {"path": _rel(args.corpus), "sha256": corpus_sha},
        "scenarios": [{"id": s.id, "notes_sha256": s.notes_sha256} for s in scenarios],
        "runs": args.runs,
        "price_basis": {"usd_per_mtok": PRICE_PER_MTOK, "regional_premium": REGIONAL_PREMIUM,
                        "note": "estimate; Cost Explorer is the source of truth"},
        "estimate": {"cost_usd_upper_bound": est_cost, "seconds": n_gen * est_s},
    }
    (batch_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    client = (client_factory or (lambda: boto3.client("bedrock-runtime", region_name=REGION)))()
    records: list[dict] = []
    stopped = False
    for run in range(1, args.runs + 1):
        run_dir = batch_dir / f"r{run}"
        run_dir.mkdir()
        run_records: list[dict] = []
        print(f"\n== run r{run} ==")
        for s in scenarios:
            t = now_fn()
            if canary_window_clash(t, t + dt.timedelta(seconds=est_s)):
                print(f"  stopping before r{run} {s.id}: a canary window is about to open.")
                stopped = True
                break
            rec = run_generation(client, s, cfg, batch_id=batch_id, run=run,
                                 started_at=t.isoformat(timespec="seconds"), sleep=sleep)
            write_generation(rec, s, run_dir)
            run_records.append(rec)
            if rec["error"]:
                print(f"  r{run} {s.id:<4} ERROR {rec['error']['code']}")
            else:
                gate = f"gate={rec['gate']['label']} ({rec['gate']['status']})" if rec["gate"] else "gate=not run"
                retries = sum(c["throttle_retries"] for c in rec["calls"])
                print(f"  r{run} {s.id:<4} ok {rec['elapsed_s']:5.1f}s "
                      f"in/out={_tokens(rec, 'input_tokens')}/{_tokens(rec, 'output_tokens')} "
                      f"cache r/w={_tokens(rec, 'cache_read_tokens')}/{_tokens(rec, 'cache_write_tokens')} "
                      f"patient={rec['patient_version']} {gate}"
                      + (f" throttle-retries={retries}" if retries else ""))
        write_run_summary(run_dir, run, run_records, manifest)
        records.extend(run_records)
        if stopped:
            break

    write_batch_summary(batch_dir, records, manifest, scenarios, args.runs, stopped)
    met, why = tier1_5_verdict(records, len(scenarios), stopped)
    errors = sum(1 for r in records if r["error"])
    gate_fail = sum(1 for r in records if r["gate"] and not r["gate"]["ok"])
    print(f"\nBatch summary -> {_rel(batch_dir / 'BATCH_SUMMARY.md')}")
    print(f"Est. cost ${sum(r['cost_usd_est'] for r in records):.4f} · errors {errors} · "
          f"gate failures {gate_fail}" + (" · stopped early" if stopped else ""))
    print(f"Tier 1 #5 completion test: {'MET' if met else 'NOT MET'} — {why}")
    return 0 if (not errors and not gate_fail and not stopped) else 1


if __name__ == "__main__":
    sys.exit(main())
