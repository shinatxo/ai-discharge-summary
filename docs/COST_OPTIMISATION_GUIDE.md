# Cost optimisation guide — step by step

Companion to `docs/COST.md` (the *analysis*). This is the *playbook*: how to actually
bring the Bedrock bill down, why each step works, what to look for, and how to verify it
afterwards. Written 2026-06-16 against the real code in `src/generate/app.py`,
`src/canary/`, and `evals/run_cold_eval.py`.

**The one fact that drives everything:** ~99% of the bill is Bedrock inference, and almost
all of those calls come from the **nightly synthetic canary** (18 scenarios replayed through
the live path, ~540 generations/month), *not* human demos. So optimisation = (a) make each
call cheaper, and (b) make fewer unnecessary calls. Hosting is already free-tier; there is
nothing to cut there.

Pricing basis (Bedrock, eu-west-2, on-demand, June 2026): **Sonnet 4.6 = $3 / 1M input,
$15 / 1M output**. Output is **5× the price of input** — remember that, it shapes most of the
levers below. Haiku 4.5 = $1 / $5 (≈3× cheaper) *if* available in your region (see Lever 4).

## Measured baseline (your CloudWatch logs, to 2026-06-16)

Confirmed from the logged `worker_complete` token counts — not estimates:

- **Volume:** a flat **~18 generations/night** (the canary), with occasional manual eval
  spikes (33 on 2026-05-30, 26 on 2026-06-03). No human-demo signature.
- **Patient v2 is ON** and switched on ~2026-05-30 (every day since shows ~900
  `patient_output_tokens`; 2026-05-26/27 show none). The second pass adds **~$0.017/call
  (~22%)** on top of the main call.
- **Per call:** ~5,066 input + ~3,108 output tokens → **$0.062**, plus the v2 pass ≈ **$0.079
  combined**. Of the main call, **75% of the cost is output** (it's only 38% of the tokens).
- **Input is ~89% the static system prompt** (~4,500 of ~5,066 tokens) → near-ideal for caching.
- **Run-rate:** ~18/night × $0.079 × 30.4 ≈ **$43/month** Bedrock pre-VAT (June actual ~$54
  was inflated by the manual eval days).

**Projections (pre-VAT):** nightly-smoke(3) + weekly-full(18) ≈ 5.4 gen/day ≈ **$13/mo**;
add caching + output trim on the remaining calls ≈ **~$9/mo**. Volume (Lever 1) is the
dominant lever — per-call tuning alone (caching+trim, still 18/night) only reaches ~$30/mo.

---

## Step 0 — Measure before you change anything

**Why:** every lever below trades something (engineering effort, latency, a little
complexity, or — for model swaps — a compliance decision). You only know which trades are
worth it once you know where the tokens actually go. You are already logging the numbers you
need — `app.py` writes `input_tokens` / `output_tokens` per generation to both the audit row
and the structured log. Use them.

**How — pull the real spend by service (you already have this in COST.md):**

```bash
aws ce get-cost-and-usage \
  --time-period Start=$(date -u -v-30d +%Y-%m-%d),End=$(date -u +%Y-%m-%d) \
  --granularity MONTHLY --metrics "UnblendedCost" \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query "ResultsByTime[].Groups[].{service:Keys[0],cost:Metrics.UnblendedCost.Amount}" \
  --output table
```

**How — get per-call token means and call volume** (CloudWatch Logs Insights, on the
generate function's log group):

```
fields @timestamp, input_tokens, output_tokens, patient_output_tokens
| filter event = "worker_complete" or event = "direct_invoke_complete"
| stats count() as calls,
        avg(input_tokens) as avg_in,
        avg(output_tokens) as avg_out,
        avg(patient_output_tokens) as avg_patient_out,
        sum(input_tokens) as total_in,
        sum(output_tokens) as total_out
  by bin(1d)
```

**What to look for:**
- **Calls/day.** ~18/night ⇒ the canary is the whole story. A sudden doubling around the
  May→June boundary almost certainly means the **Patient v2 second pass got switched on**
  (it fires a *second* Sonnet call per generation — see Lever 5).
- **avg_out vs avg_in.** If `avg_out` is large relative to `avg_in`, output discipline
  (Lever 3) is your best lever, because output costs 5× input.
- **avg_out vs MAX_TOKENS (4096).** If outputs sit far below 4096 you have headroom to lower
  the cap (a safety net, not a saving in itself — see Lever 3).
- **Cost per call.** `(avg_in × $3 + avg_out × $15) / 1e6`. The doc's working figure is
  ~$0.06/generation. Multiply by calls/month to sanity-check against Cost Explorer.

Write the numbers down. Every claim below should be checked against *your* figures, not the
estimates.

---

## Lever 1 — Canary cadence (biggest, simplest, zero code risk)

**Why it's first:** if ~540 generations/month are the canary, halving the canary roughly
halves the bill, with no change to the application code and no quality risk to real outputs.
This is the highest impact-to-effort lever you have.

**The idea:** you don't need the *full* 18-scenario regression every single night. Run a
small **smoke set nightly** (the few scenarios that catch the failure modes you care about,
e.g. S16 — Ibrahim's flagged case — plus one or two adversarial ones), and the **full 18 on a
weekly schedule**.

**How:**
1. Open `infra/template.yaml` and find the EventBridge schedule rule that triggers the canary
   (`src/canary/`). Today it's a single nightly rule running all scenarios.
2. Split it into two rules:
   - **Nightly smoke:** `cron(0 2 * * ? *)`, passing a short scenario list (e.g. `S16,S14,S18`).
   - **Weekly full:** `cron(0 2 ? * MON *)`, the full set.
3. The canary already reads its scenario list from `src/canary/scenarios.json` /
   `build_scenarios.py` — parameterise which list each rule sends (e.g. via the event payload
   or an env var the canary reads) rather than hard-coding 18.

**What to look for / compare:**
- Nightly-3 + weekly-18 ≈ (3×30 + 18×4)/30 ≈ **5.4 generations/day** vs 18 today — roughly a
  **70% cut** in canary volume while still getting a daily baseline and a weekly full sweep.
- Don't drop the scenarios that guard your known failure modes (the Run 4 "added-advice"
  cases). Cost-cutting must not quietly remove the regression coverage that justifies the
  project. Coverage you keep should be the coverage you'd be embarrassed to lose.

**Verify:** after a week, re-run the Logs Insights query — calls/day should drop to your new
cadence, and the next Cost Explorer month should fall proportionally.

---

## Lever 2 — Prompt caching (best per-call saving for the canary)

**Why:** your system prompt is ~18k characters and **identical on every call**. Without
caching you pay full input-token price to re-read it every time. Bedrock prompt caching reads
cached tokens at **~0.1× the normal input price**. Because the nightly canary fires its 18
calls back-to-back with the *same* prompt, calls 2…N within the cache TTL read the prompt from
cache — a large input-token saving across each batch.

**The catch to understand (this is the teaching point):** caching only pays off when the same
prefix is reused *within the TTL* (5 min default; 1 h extended on Sonnet 4.5+). The *first*
call in a window is a cache **write**, billed at ~1.25× normal input. So:
- Bursty / batched traffic (your canary, eval batches) → big net win.
- Sporadic one-off demo calls spaced hours apart → each is a fresh cache write, little or no
  win. Don't expect caching to help isolated manual demos.

**How — one change, in `_converse()` in `src/generate/app.py`:**

```python
def _converse(system_prompt: str, user_text: str, max_tokens: int):
    return _bedrock.converse(
        modelId=MODEL_ID,
        system=[
            {"text": system_prompt},
            {"cachePoint": {"type": "default"}},   # <-- cache everything above this point
        ],
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": TEMPERATURE},
    )
```

Mirror the same change in `evals/run_cold_eval.py`'s `client.converse(...)` so cold evals
match the deployed path (they're meant to be byte-faithful).

**What to look for:**
- **Region support.** Confirm prompt caching is enabled for Sonnet 4.6 in **eu-west-2** before
  relying on it — test one call and read the usage block. If unsupported in-region, the
  `cachePoint` may error; fail fast in a test, don't ship blind.
- **Min cacheable size.** The cached prefix must clear the model's minimum (~1,024 tokens).
  Your 18k-char prompt clears it comfortably.
- **Proof it's working:** the Converse `usage` block returns `cacheReadInputTokens` and
  `cacheWriteInputTokens`. Log them (extend the `usage.get(...)` lines in `app.py`). First call
  in a batch shows a write; subsequent ones show reads. No reads ⇒ caching isn't actually
  engaging (TTL expired, or calls aren't really sharing the prefix).

**Verify:** in a canary run you should see 1 cache-write + 17 cache-reads. Input-token spend
for the batch should drop toward `(1 prompt + 18 × notes)` instead of `18 × (prompt + notes)`.

---

## Lever 3 — Output discipline (output is 5× input)

**Why:** at $15/1M, output tokens are the most expensive thing you buy. Trimming them is the
highest-value *per-token* lever. `temperature=0` is already set (good — deterministic, no
saving left there), so the play is to generate fewer, tighter tokens.

**How:**
1. **Tighten the prompt's format demands.** In `prompts/discharge-summary-system-prompt.md`,
   ask for concise clinical phrasing and cap section lengths where clinically safe. Shorter
   required output = fewer output tokens on *every* call, forever.
2. **Right-size `MAX_TOKENS`.** It's env-driven (`MAX_TOKENS`, default 4096). From Step 0,
   check the `stop_reason` distribution in your runs: if generations almost always stop at
   `end_turn` well under 4096, the cap isn't costing you (you only pay for tokens produced) —
   but lowering it to, say, 3000 is a cheap *safety rail* against a runaway generation. If you
   ever see `max_tokens` as the stop reason, you're truncating clinical output — raise it, don't
   lower it. **Note:** `MAX_TOKENS` caps spend per call; it does not by itself reduce normal
   spend. The prompt change is what reduces the typical bill.

**What to look for / compare:** measure `avg_out` before and after a prompt tweak on the same
scenarios via `run_cold_eval.py`. Compare not just tokens but **quality** — a cheaper prompt
that degrades the summary is a false economy for a clinical tool. The cold-eval files let you
diff output side-by-side.

**Verify:** re-run the 5-scenario cold eval, compare `Out tokens` in the two `SUMMARY.md`
files. Confirm the outputs still pass your review bar.

---

## Lever 4 — Model right-sizing (powerful, but a compliance decision)

**Why:** Haiku 4.5 ($1/$5) is ~3× cheaper than Sonnet 4.6 ($3/$15). You don't need
Sonnet-grade reasoning for *every* token — the **patient leaflet** (Patient v2's PART C) is a
plain-language rewrite of an already-curated, clinician-safe PART A. That's exactly the kind of
bounded, lower-risk task a smaller model handles well.

**The hard constraint — read this before touching it:** `ADR-003 rule 1` pins inference to
**UK-only, eu-west-2, on-demand** for data-residency reasons, and *deliberately* avoids
Haiku/EU-profile passes. Two things follow:
- A model swap is only acceptable if **Haiku 4.5 is available as on-demand in eu-west-2**.
  Routing via a *cross-region* or *global* inference profile would send clinical text outside
  the UK and **break ADR-003** — that is not a cost decision, it's a governance one, and cost
  must not win it.
- If you do this, you must **update ADR-003** to record the new decision and its residency
  justification. Keep the audit trail honest.

**How (only if eu-west-2 on-demand Haiku is confirmed in your Bedrock console):**
- Keep Sonnet for the clinical summary (PART A/B) — quality and safety matter most there.
- Route the **second pass only** to Haiku: parameterise the model in `_maybe_second_pass()` /
  `_converse()` with a separate `PATIENT_MODEL_ID` env var, defaulting to the Sonnet ID so the
  change is opt-in and reversible.

**What to look for / compare:** run the patient pass under both models on your eval scenarios
and compare leaflet quality *and* the residency guarantee. If Haiku isn't in-region on-demand,
**this lever is off the table** — say so in the ADR and move on. Don't trade compliance for
~$0.01/call.

---

## Lever 5 — Know what Patient v2 costs (don't cut it; account for it)

**Why:** `PATIENT_V2_SECOND_PASS` fires a **second Sonnet call** per generation (capped at
`PATIENT_MAX_TOKENS=1500` output, with the curated PART A as input). It exists to stop the
Run 4 "helpful hallucination" failure (Ibrahim's flag) — that's a real safety win, so the
default answer is **keep it**. But it roughly adds one extra (smaller) call per generation, and
if it was switched on between May and June it's a prime suspect for the cost jump.

**How / what to look for:**
- From Step 0, compare `patient_output_tokens` against `output_tokens` to see the marginal cost
  of the second pass. Marginal cost ≈ `(len(PART A) × $3 + patient_out × $15) / 1e6`.
- Decide deliberately: the safety benefit almost certainly justifies the few cents. If you want
  the safety *and* a saving, Lever 4 (Haiku for this pass, residency permitting) is the way —
  not switching v2 off.

---

## Lever 6 — Batch inference for the canary (50% off, async)

**Why:** Bedrock batch inference is ~50% cheaper than on-demand for non-interactive work. The
nightly canary is non-interactive by definition, so it's a textbook fit.

**The trade-off:** batch has a ~24-hour SLA and a job-submission model (write inputs to S3,
submit a batch job, collect outputs) — it changes the canary's architecture and isn't
byte-faithful to the live synchronous path the canary is *meant* to exercise. So:

**Compare honestly:**
- If the canary's job is to **prove the live path works**, batch defeats the purpose — keep it
  on-demand and lean on Levers 1+2 instead.
- If you add a **separate, larger offline eval** (e.g. nightly scoring over many scenarios that
  doesn't need to mirror the live stack), batch is the right tool and halves that slice.
- For just 18 scenarios, batch's complexity rarely pays off — revisit only if eval volume grows.

---

## Lever 7 — Put a floor and a ceiling on surprises (do this today)

**Why:** you pay out of pocket. A runaway loop (the kind the 2026-06-06 worker-hang bug
caused, burning throttle-retry tokens) should ping you, not appear on a bill weeks later.

**How — AWS Budgets:**
```bash
# Create an $80/month cost budget with an 80% actual + 100% forecast alert to your email.
# Console: Billing > Budgets > Create budget > Cost budget > $80/month > alert thresholds.
```
Set one alert at 80% of your normal month and one *forecasted* alert at 100%, both to your
email. Optionally add a second budget scoped to the Bedrock service only, so a Bedrock-specific
spike is unambiguous.

**Also worth keeping:** the resilience fix already in `app.py` (`BEDROCK_MAX_ATTEMPTS=1`,
explicit `read_timeout`) is itself a cost control — it stops silent retries from
quota-burning. Don't regress it.

---

## What's already implemented in the code (2026-06-16)

The code/template changes for Levers 1 and 2 are committed and **off by default** — they
change nothing until you deploy and flip the parameters. Tests: all 39 pass.

- **Lever 2 (prompt caching):** `src/generate/app.py` `_converse()` appends a `cachePoint`
  when `PROMPT_CACHING` is on; `worker_complete` logs now include `cache_read_tokens` /
  `cache_write_tokens`. Template parameter `PromptCaching` (default `off`) wires the env var.
  `evals/run_cold_eval.py` gained a `--prompt-caching` flag and cache columns in `SUMMARY.md`.
- **Lever 1 (canary cadence):** `src/canary/app.py` `_load_scenarios(event)` reads a per-run
  `scenarios` payload (falling back to the env). `infra/template.yaml` now has a nightly
  **smoke** schedule (`CanaryNightlyScenarios`, default `S14,S16,S18`) and a new weekly
  **full** schedule (`CanaryWeeklyScheduleExpression`, default Mondays 03:00). Set
  `CanaryNightlyScenarios=''` to restore nightly-full.

Still your call (account actions / clinical content): the output prompt tightening (Lever 3),
and the Lever 4 region/compliance check.

## Verified live (2026-06-17)

Deployed to stack `discharge-audit` (`PromptCaching=on`, `CanaryEnabled=on`,
`PatientV2SecondPass=on`, nightly smoke `S14,S16,S18` + weekly full). Confirmed from the
deployed logs, not projections:

- **Cadence (Lever 1):** nightly canary now runs **3 scenarios** (was 18). Three smoke runs
  came back 3/3 success, 3/3 parse_ok, **0 throttled**.
- **Caching (Lever 2) works in eu-west-2:** `worker_complete` shows one `cache_write_tokens:
  4693` then `cache_read_tokens: 4693` on subsequent calls — the ~4,693-token system prompt is
  served from cache at ~10% price. Fresh `input_tokens` collapsed to **230–403** (just the
  notes). That's ~90% off the input side ≈ ~20% off each main call (~$0.062 → ~$0.049).
- **Net:** Bedrock run-rate projected **~$43/mo → ~$10/mo**, both levers proven in-log.
  Patient v2 safety pass intact (`patient_version: v2` on every call).
- **Budget alarm** set at $80/mo (actual 80% + forecast 100%).

Operational notes learned during rollout: invoke the canary **async** (`--invocation-type
Event`) — a synchronous `lambda invoke` times out the CLI against its ~900s runtime; and always
pass `--start-time` to `filter-log-events` or it returns the oldest logs first.

## Runbook — exact commands

Replace `STACK` with your stack name and `eu-west-2` if different. Run from the repo root.

```bash
# 0a) Spend by service, last 30 days (USD)
aws ce get-cost-and-usage \
  --time-period Start=$(date -u -v-30d +%Y-%m-%d),End=$(date -u +%Y-%m-%d) \
  --granularity MONTHLY --metrics "UnblendedCost" \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query "ResultsByTime[].Groups[].{service:Keys[0],cost:Metrics.UnblendedCost.Amount}" \
  --output table

# 0b) Per-day token means + volume (paste the Logs Insights query from Step 0 in the console,
#     log group /aws/lambda/STACK-generate), or via CLI:
aws logs start-query --log-group-name /aws/lambda/STACK-generate \
  --start-time $(date -u -v-14d +%s) --end-time $(date -u +%s) \
  --query-string 'fields input_tokens, output_tokens, patient_output_tokens | filter event="worker_complete" | stats count() as calls, avg(input_tokens) as avg_in, avg(output_tokens) as avg_out by bin(1d)'
# then: aws logs get-query-results --query-id <id-from-above>

# 7) Budget alert FIRST (downside protection). Create budget.json + notifications.json:
cat > /tmp/budget.json <<'JSON'
{ "BudgetName": "discharge-monthly", "BudgetLimit": {"Amount": "80", "Unit": "USD"},
  "TimeUnit": "MONTHLY", "BudgetType": "COST" }
JSON
cat > /tmp/notify.json <<'JSON'
[ { "Notification": {"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":80,"ThresholdType":"PERCENTAGE"},
    "Subscribers":[{"SubscriptionType":"EMAIL","Address":"YOU@example.com"}] },
  { "Notification": {"NotificationType":"FORECASTED","ComparisonOperator":"GREATER_THAN","Threshold":100,"ThresholdType":"PERCENTAGE"},
    "Subscribers":[{"SubscriptionType":"EMAIL","Address":"YOU@example.com"}] } ]
JSON
aws budgets create-budget --account-id $(aws sts get-caller-identity --query Account --output text) \
  --budget file:///tmp/budget.json --notifications-with-subscribers file:///tmp/notify.json

# --- Before deploying app changes: run the tests ---
python3 -m pytest tests/ -q

# 2-verify) Cold-eval WITH caching on the smoke set (proves cacheRead engages in-region):
pip install boto3 --break-system-packages
python evals/run_cold_eval.py S14 S16 S18 --prompt-caching --out runs/cache-check
#   open evals/runs/cache-check/SUMMARY.md — "Cache read" should be > 0 on calls after the first.

# --- Deploy (plain CloudFormation; this repo does NOT use the SAM transform) ---
# package zips each Lambda's code dir and rewrites Code: to S3:
aws cloudformation package \
  --template-file infra/template.yaml \
  --s3-bucket YOUR-CFN-ARTIFACTS-BUCKET \
  --output-template-file /tmp/template.packaged.yaml

# deploy with the new levers ON (Lever 2 caching + Lever 1 nightly smoke / weekly full):
aws cloudformation deploy \
  --template-file /tmp/template.packaged.yaml \
  --stack-name STACK \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      PromptCaching=on \
      CanaryEnabled=on \
      PatientV2SecondPass=on \
      CanaryNightlyScenarios=S14,S16,S18 \
      CanaryWeeklyScheduleExpression="cron(0 3 ? * MON *)"

# 1-verify) confirm the two schedules exist:
aws scheduler list-schedules --query "Schedules[?contains(Name, 'canary')].Name"

# 2-verify, live) after the next nightly run, confirm cache reads in the deployed worker:
aws logs filter-log-events --log-group-name /aws/lambda/STACK-generate \
  --filter-pattern '{ $.event = "worker_complete" }' --limit 20 \
  --query 'events[].message' --output text
#   look for cache_read_tokens > 0 on calls after the first in a batch.

# 3) Output discipline: measure avg_out (Step 0), tighten prompts/discharge-summary-system-prompt.md,
#    re-run cold eval and DIFF quality + tokens before/after — don't trade quality for a few cents:
python evals/run_cold_eval.py --out runs/baseline
# (edit the prompt)
python evals/run_cold_eval.py --out runs/trimmed
diff evals/runs/baseline/SUMMARY.md evals/runs/trimmed/SUMMARY.md
```

To roll back any lever: redeploy with `PromptCaching=off`, or `CanaryNightlyScenarios=''`
(nightly runs all 18 again), or remove the weekly schedule.

## Suggested order of attack

1. **Step 0** — measure (confirm canary-vs-human, token means, whether Patient v2 doubled calls).
2. **Lever 7** — set the budget alert (5 minutes, removes downside risk).
3. **Lever 1** — split the canary into nightly-smoke + weekly-full (~70% volume cut, no code risk).
4. **Lever 2** — add the `cachePoint` (big per-call input saving across each batch).
5. **Lever 3** — tighten the prompt, measure output tokens, then optionally lower `MAX_TOKENS`.
6. **Levers 4/5/6** — only if the numbers justify the effort *and* the compliance check passes.

**Plausible combined result:** Lever 1 (~70% fewer canary calls) and Lever 2 (most of the
input cost on the calls that remain) together can take the Bedrock bill from ~$54/month to
roughly **$15–25/month**, without touching output quality or data residency.

---

## SAA-C03 tie-in

This whole exercise is the **Cost Optimisation pillar** of the Well-Architected Framework made
concrete: *measure before optimising* (Cost Explorer + Logs Insights), *match the pricing model
to the workload* (on-demand for live, batch for offline, caching for repeated context),
*right-size* (Sonnet where reasoning matters, Haiku where it doesn't), and *guardrails*
(AWS Budgets, capped retries). The compliance constraint in Lever 4 is the framework's reminder
that pillars trade against each other — here, Security/residency outranks Cost, and the ADR is
where you record that the trade was made on purpose.

## Sources
- [Amazon Bedrock pricing (AWS)](https://aws.amazon.com/bedrock/pricing/)
- [Claude API pricing — Haiku 4.5 / Sonnet 4.6 (Anthropic)](https://platform.claude.com/docs/en/about-claude/pricing)
- [Prompt caching with the Bedrock Converse API (AWS re:Post)](https://repost.aws/questions/QUIuMZAYVBQHuVYl6tMu82rQ/clarification-on-prompt-caching-usage-with-converse-api-in-amazon-bedrock)
- [Claude Haiku 4.5 on Amazon Bedrock (AWS)](https://aws.amazon.com/about-aws/whats-new/2025/10/claude-4-5-haiku-anthropic-amazon-bedrock)
