#!/usr/bin/env python3
"""
Cold-eval runner for the Discharge Summary Assistant.

"Cold" means we hit Bedrock directly from your laptop with the canonical
system prompt loaded from prompts/, bypassing the deployed Lambda + API
Gateway + Cognito + DynamoDB path. We isolate the prompt change so that
any behavioural difference vs an earlier run is attributable to the
prompt itself and not to anything in the surrounding stack.

This mirrors src/generate/app.py exactly:
- Same model ID, same region, same on-demand profile
- Same inferenceConfig (maxTokens=4096, temperature=0)
- Same _load_system_prompt() logic (finds the '## SYSTEM PROMPT' marker
  and sends everything after it)

So: a cold-eval output for scenario S{N} with prompt v{X} is byte-faithful
to what the deployed Lambda would emit for the same input under the same
prompt version, modulo network/account-side non-determinism (which at
temperature=0 should be near-zero).

USAGE
-----
    # All five v0.6 regression scenarios (S16 primary + S14/15/17/18 sample)
    python evals/run_cold_eval.py

    # Just one scenario
    python evals/run_cold_eval.py S16

    # Explicit list, custom output folder
    python evals/run_cold_eval.py S14 S16 S18 --out runs/spot-check

    # Different prompt version (e.g. comparing v0.5 against v0.6 later)
    python evals/run_cold_eval.py --prompt prompts/discharge-summary-system-prompt-v0.5.md

REQUIREMENTS
------------
- AWS credentials configured for an account with bedrock:InvokeModel on
  anthropic.claude-sonnet-4-6 in eu-west-2 (your normal deploy creds).
- boto3 (`pip install boto3` if not already on your PATH).

OUTPUT
------
Each scenario produces one markdown file under evals/runs/<run-folder>/:
- frontmatter with model + inference config + timing + token counts
- the INPUT (ward-round notes) used
- the full model OUTPUT (PART A + PART B + PART C)

A summary line is also written to <run-folder>/SUMMARY.md with timing,
token usage, and stop reasons for the whole batch.
"""

import argparse
import datetime as dt
import re
import sys
import time
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    sys.exit(
        "boto3 not installed. Run:\n"
        "    pip install boto3\n"
        "(or `pip install boto3 --break-system-packages` if your Python is system-managed)"
    )


# ---------------------------------------------------------------------------
# Match the deployed Lambda's invocation parameters exactly.
# These are the same constants you'd see in src/generate/app.py at the time
# this script was written. If the Lambda ever changes them, change here too.
# ---------------------------------------------------------------------------
REGION = "eu-west-2"
MODEL_ID = "anthropic.claude-sonnet-4-6"
MAX_TOKENS = 4096
TEMPERATURE = 0.0
SYSTEM_PROMPT_MARKER = "## SYSTEM PROMPT"

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT_PATH = REPO / "prompts" / "discharge-summary-system-prompt.md"
DEFAULT_SCENARIOS_PATH = REPO / "evals" / "scenarios" / "eval-scenarios-expansion.md"

# Default v0.6 regression set: S16 is Ibrahim's flagged Gen Surg case;
# S14/15/17/18 are post-discharge scenarios where the model is tempted
# to invent condition-specific safety-netting from standard-of-care knowledge.
DEFAULT_SCENARIOS = ["S14", "S15", "S16", "S17", "S18"]


def load_system_prompt(prompt_path: Path) -> str:
    """Mirror the Lambda's _load_system_prompt(): find the '## SYSTEM PROMPT'
    marker and return everything after the marker line. Strips leading/trailing
    whitespace to match the Lambda's behaviour."""
    raw = prompt_path.read_text(encoding="utf-8")
    idx = raw.find(SYSTEM_PROMPT_MARKER)
    if idx == -1:
        raise RuntimeError(
            f"{prompt_path} is missing the '{SYSTEM_PROMPT_MARKER}' marker"
        )
    body = raw[idx:]
    body = body.split("\n", 1)[1] if "\n" in body else body
    return body.strip()


def extract_scenario_input(scenarios_path: Path, scenario_id: str) -> str:
    """Pull the '### INPUT — free-text ward-round notes' block for one scenario.

    The eval-scenarios-expansion.md file uses this structure:
        # S16 — Emergency laparotomy for small bowel obstruction, with stoma
        ...
        ### INPUT — free-text ward-round notes
        <the notes>
        ### EXPECTED OUTPUT — structured discharge summary
        ...

    We extract everything between '### INPUT' and the next '###' header.
    """
    raw = scenarios_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^# {re.escape(scenario_id)}\b.*?^### INPUT[^\n]*\n(.*?)(?=^### )",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(raw)
    if not m:
        raise ValueError(
            f"could not find INPUT block for {scenario_id} in {scenarios_path}"
        )
    return m.group(1).strip()


def run_one(client, scenario_id: str, system_prompt: str, notes: str) -> dict:
    """Single Bedrock Converse call — same shape as the Lambda's call."""
    t0 = time.time()
    response = client.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": notes}]}],
        inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
    )
    elapsed = time.time() - t0
    output_text = response["output"]["message"]["content"][0]["text"]
    usage = response.get("usage", {})
    return {
        "scenario_id": scenario_id,
        "elapsed_s": elapsed,
        "stop_reason": response.get("stopReason"),
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "notes": notes,
        "output": output_text,
    }


def save_result(result: dict, out_dir: Path, prompt_path: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{result['scenario_id']}.md"
    body = (
        f"# {result['scenario_id']} — cold-eval output\n\n"
        f"- Model: `{MODEL_ID}` ({REGION}, on-demand)\n"
        f"- Inference: `maxTokens={MAX_TOKENS}`, `temperature={TEMPERATURE}`\n"
        f"- Elapsed: {result['elapsed_s']:.2f}s\n"
        f"- Tokens (in/out): {result['input_tokens']} / {result['output_tokens']}\n"
        f"- Stop reason: `{result['stop_reason']}`\n"
        f"- Prompt source: `{prompt_path.relative_to(REPO)}`\n"
        f"- Run at: {dt.datetime.now().isoformat(timespec='seconds')}\n\n"
        f"## INPUT (ward-round notes)\n\n"
        f"```\n{result['notes']}\n```\n\n"
        f"## OUTPUT\n\n"
        f"{result['output']}\n"
    )
    out_path.write_text(body, encoding="utf-8")
    return out_path


def write_summary(results: list, errors: list, out_dir: Path, prompt_path: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "SUMMARY.md"
    total_in = sum(r.get("input_tokens") or 0 for r in results)
    total_out = sum(r.get("output_tokens") or 0 for r in results)
    total_s = sum(r.get("elapsed_s") or 0 for r in results)
    lines = [
        f"# Cold-eval batch — {dt.datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- Prompt: `{prompt_path.relative_to(REPO)}`",
        f"- Model: `{MODEL_ID}` ({REGION}, on-demand)",
        f"- Inference: `maxTokens={MAX_TOKENS}`, `temperature={TEMPERATURE}`",
        f"- Scenarios attempted: {len(results) + len(errors)}",
        f"- Scenarios passed: {len(results)}",
        f"- Scenarios errored: {len(errors)}",
        "",
        "## Per-scenario",
        "",
        "| Scenario | Elapsed | In tokens | Out tokens | Stop reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r['scenario_id']} | {r['elapsed_s']:.2f}s | "
            f"{r['input_tokens']} | {r['output_tokens']} | `{r['stop_reason']}` |"
        )
    if errors:
        lines += ["", "## Errors", ""]
        for scenario_id, exc in errors:
            lines.append(f"- **{scenario_id}** — {exc}")
    lines += [
        "",
        f"**Batch totals:** {total_s:.1f}s, {total_in} input tokens, {total_out} output tokens.",
        "",
    ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def main():
    parser = argparse.ArgumentParser(
        description="Cold-eval runner for the Discharge Summary Assistant prompt."
    )
    parser.add_argument(
        "scenarios",
        nargs="*",
        default=DEFAULT_SCENARIOS,
        help=f"Scenario IDs (default: {' '.join(DEFAULT_SCENARIOS)})",
    )
    parser.add_argument(
        "--prompt",
        type=Path,
        default=DEFAULT_PROMPT_PATH,
        help=f"Path to system prompt markdown (default: {DEFAULT_PROMPT_PATH.relative_to(REPO)})",
    )
    parser.add_argument(
        "--scenarios-file",
        type=Path,
        default=DEFAULT_SCENARIOS_PATH,
        help=f"Path to scenarios markdown (default: {DEFAULT_SCENARIOS_PATH.relative_to(REPO)})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output folder under evals/runs/ (default: auto-named with today's date and prompt version)",
    )
    args = parser.parse_args()

    prompt_path = args.prompt.resolve()
    scenarios_path = args.scenarios_file.resolve()

    if args.out is None:
        # Auto-name: evals/runs/run-YYYY-MM-DD-<prompt-stem>
        today = dt.date.today().isoformat()
        out_dir = REPO / "evals" / "runs" / f"run-{today}-{prompt_path.stem}"
    else:
        out_dir = args.out if args.out.is_absolute() else REPO / args.out

    print(f"Prompt:    {prompt_path.relative_to(REPO)}")
    print(f"Scenarios: {scenarios_path.relative_to(REPO)}")
    print(f"Output:    {out_dir.relative_to(REPO)}\n")

    system_prompt = load_system_prompt(prompt_path)
    print(f"Loaded system prompt: {len(system_prompt)} chars\n")

    print(f"Connecting to Bedrock: region={REGION}, model={MODEL_ID}\n")
    client = boto3.client("bedrock-runtime", region_name=REGION)

    results = []
    errors = []
    for scenario_id in args.scenarios:
        print(f"== {scenario_id} ==")
        try:
            notes = extract_scenario_input(scenarios_path, scenario_id)
        except ValueError as exc:
            print(f"  SKIP: {exc}\n")
            errors.append((scenario_id, str(exc)))
            continue
        try:
            result = run_one(client, scenario_id, system_prompt, notes)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            print(f"  BEDROCK ERROR ({code}): {msg}\n")
            errors.append((scenario_id, f"{code}: {msg}"))
            continue
        except Exception as exc:
            print(f"  ERROR: {exc}\n")
            errors.append((scenario_id, str(exc)))
            continue
        out_path = save_result(result, out_dir, prompt_path)
        results.append(result)
        print(
            f"  ok  {result['elapsed_s']:.1f}s  "
            f"tokens in/out={result['input_tokens']}/{result['output_tokens']}  "
            f"stop={result['stop_reason']}"
        )
        print(f"  saved -> {out_path.relative_to(REPO)}\n")

    summary_path = write_summary(results, errors, out_dir, prompt_path)
    print(f"Batch summary -> {summary_path.relative_to(REPO)}")

    if errors:
        print(f"\nFinished with {len(errors)} error(s). See {summary_path.name}.")
        sys.exit(1)


if __name__ == "__main__":
    main()
