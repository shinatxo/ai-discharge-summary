# Bedrock on-demand quota — capacity plan

> **Correction, 24 Sep 2026 — the live quota is 25 requests/minute, not 5.** Service Quotas,
> queried live: `L-9B878FAE` (on-demand requests/min, Sonnet 4.6, eu-west-2) = **25**, still marked
> not adjustable; `L-5F5A169C` (tokens/min) = 3,000,000. The figure of 5 below was read on 6 Jun 2026.
> Nothing in the repository records why it changed — AWS raising the default, or a granted Support
> case — so treat the history as unknown. The practical consequence: a four-call generation
> (ADR-009) uses ~3–4 requests/min, so about six can run at once, and the weekly 18-scenario canary
> run fits its Lambda at `CanaryMaxConcurrency=4`. The rest of this document is kept as written on
> 6 Jun 2026; §2's increase request is no longer needed, and **§4 is superseded — do not run it**:
> `CanaryMaxConcurrency=8` with four calls per generation would throttle at 25 requests/min, and CI
> pins the value at 4 anyway (ADR-009 keeps it at 4 or below).

## Why this doc exists

The Wave 3 synthetic canary surfaced a real platform limit: firing several generations at
once throttled hard. Root cause is the **low on-demand quota for Claude Sonnet** — confirmed
2026-06-06 via Service Quotas: for the pinned **Claude Sonnet 4.6 in eu-west-2** it is
**5 requests/minute** (quota `L-9B878FAE`) and 3M tokens/minute (`L-5F5A169C`), and both are
marked **not adjustable** in the console. So even a handful of concurrent calls saturates it.
The canary runs at bounded concurrency to live within that limit; this doc is the plan to try
raising it (via a Support case) so the nightly success baseline can move toward 18/18.

This is a deliberate capacity decision, not a bug — and it's worth keeping the residency
posture (ADR-003) in mind while doing it (see below).

## 1. See the current quotas (eu-west-2)

Console: **Service Quotas → AWS services → Amazon Bedrock** (region eu-west-2) → search
`Sonnet`. Look for:

- **On-demand InvokeModel requests per minute** for the pinned Claude Sonnet model
- **On-demand InvokeModel tokens per minute** for the same model

Note each one's current value and whether it's marked **Adjustable**.

CLI equivalent:

```bash
aws service-quotas list-service-quotas --service-code bedrock --region eu-west-2 \
  --query "Quotas[?contains(QuotaName,'Sonnet')].{name:QuotaName,code:QuotaCode,value:Value,adjustable:Adjustable}" \
  --output table
```

## 2. Request the increase

- **If the quota is Adjustable** (self-service): request it from the console "Request increase"
  button, or via CLI:

  ```bash
  aws service-quotas request-service-quota-increase \
    --service-code bedrock --region eu-west-2 \
    --quota-code <QUOTA_CODE_FROM_STEP_1> \
    --desired-value 100
  ```

- **If it's not Adjustable** (common for on-demand model quotas): open an **AWS Support case**
  → *Service limit increase* → service *Bedrock*, region *eu-west-2*, name the model
  (Claude Sonnet 4.6), and the target RPM/TPM. Support handles these and often offers to bump
  related quotas at the same time.

**What to ask for:** the workload is tiny — a 3-scenario smoke replay nightly, all 18 as a weekly
regression, plus interactive demo use — so a modest, easily-justified ask is enough. Something like **~100 requests/minute** and
**a few hundred thousand–1M tokens/minute** comfortably lifts the canary to a clean 18/18 and
lets `CanaryMaxConcurrency` rise. Justify it with exactly that: a synthetic canary that replays
18 scenarios weekly (3 nightly) for regression + alarm baselining. *(The ask is sized on the peak,
which is the weekly full run — the nightly smoke set is well inside it, so the figures above are
unchanged by the cadence split.)*

## 3. Residency note (ADR-003)

The *higher-throughput* path AWS often steers you toward is the **EU cross-region inference
profile**, which spreads inference across EU regions. This project deliberately uses
**single-region on-demand in eu-west-2** to keep inference UK-only (ADR-003 rule 1). So:

- **Preferred:** raise the **eu-west-2 on-demand** RPM/TPM (preserves UK-only inference).
- **Fallback only if needed:** the EU geographic profile is already documented as the rule-2
  fallback — it widens throughput but processes transiently across the EU. Don't switch to it
  silently; it's a residency decision, not just a knob.

## 4. After it's granted

Raise the canary's concurrency back up (it's a stack parameter — no code change):

```bash
cd infra
aws cloudformation deploy --template-file packaged.yaml --stack-name discharge-audit \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Environment=demo PatientV2SecondPass=on CanaryEnabled=on \
    AlertEmail="$ALERT_EMAIL" CanaryMaxConcurrency=8 \
  --region eu-west-2
```

Then watch a couple of nightly runs (or trigger one manually) and confirm `SuccessRatePct`
climbs toward 100. If it does, you can also raise the `CanarySuccessThresholdPct` alarm
threshold to match the new, higher baseline.
