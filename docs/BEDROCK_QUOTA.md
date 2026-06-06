# Bedrock on-demand quota — capacity plan

## Why this doc exists

The Wave 3 synthetic canary surfaced a real platform limit: firing several generations at
once throttled hard. Root cause is the **default on-demand quota for Claude Sonnet, which is
very low (~2 requests/minute)** — so even a handful of concurrent calls saturates it. The
canary now runs at bounded concurrency to live within that limit; this doc is the plan to
raise it so the nightly success baseline can move toward 18/18 and concurrency can rise.

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

**What to ask for:** the workload is tiny — a nightly 18-scenario replay plus interactive demo
use — so a modest, easily-justified ask is enough. Something like **~100 requests/minute** and
**a few hundred thousand–1M tokens/minute** comfortably lifts the canary to a clean 18/18 and
lets `CanaryMaxConcurrency` rise. Justify it with exactly that: a synthetic canary that replays
18 scenarios nightly for regression + alarm baselining.

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
