# Cost analysis

What the deployed system costs per month, and — more interesting — what drives it. Figures
below are **actuals from AWS Cost Explorer** (last ~30 days), with the original estimate kept
for the estimate-vs-actual comparison.

## Usage profile

This is a portfolio/demo deployment, not a production service. The traffic is almost
entirely **synthetic**: a nightly canary replays the 18 evaluation scenarios through the
live path (~540 generations/month), plus light manual/demo use. Pricing basis:

- **Bedrock Claude Sonnet 4.6:** $3.00 / 1M input tokens, $15.00 / 1M output tokens (on-demand).
- Per generation ≈ 5,000 input + 3,000 output tokens ≈ **$0.06**.
- Converted at ≈ **£0.80 / $1** (approximate; FX moves).

## Actual monthly cost (AWS Cost Explorer, ~30 days)

Project services only (≈ £0.80/$, before 20% UK VAT):

| Service | £/month (actual) | Notes |
|---|---:|---|
| **Bedrock** (Sonnet 4.6) | **~£43** ($53.65) | **~99% of the project's cost.** Slightly inflated in this window by throttle-retry tokens from the worker-hang bug before it was fixed. |
| **KMS** | ~£0.38 ($0.47) | The customer-managed key. |
| **API Gateway** | ~£0.01 | Negligible. |
| **DynamoDB** | ~£0.003 | On-demand, hash-only rows. |
| **S3 + Object Lock (WORM)** | ~£0.007 | Tiny static + ledger objects. |
| **CloudWatch** | **£0** | Within free tier at this volume. |
| **Lambda / CloudFront / Cognito / Route 53 / SNS / EventBridge / ACM** | ~£0 | Free tier. |
| **Project total** | **≈ £43/month** (+ ~20% VAT ≈ £52 inc.) | Bedrock is essentially the entire bill. |

**Excluded from the project total:**
- A one-off **Amazon Registrar $17** (annual domain registration) — not a recurring cost.
- **~$6/month of EC2 + ELB + VPC + EFS** that is **not part of this stack** — this project is
  100% serverless (no compute instances, load balancers, NAT, or file systems). Those are
  other/orphaned resources on the account, worth investigating and removing separately.

### Estimate vs actual (validating a cost model)

The first cut of this doc *estimated* ~£26–36 Bedrock and ~£23 CloudWatch. The bill says
**Bedrock is higher (~£43) and CloudWatch is £0**. Two corrections worth recording: per-call
token usage ran higher than assumed (and the debugging storms added some), and custom-metric
charges sat inside the free tier at this volume rather than the ~95-metric × $0.30 I'd modelled.
Cost Explorer is the source of truth; an estimate is a hypothesis to check against it.

## The headline insight

**The application itself, at rest, is essentially free-tier** — Lambda, DynamoDB, S3,
CloudFront, API Gateway, Cognito all land at pennies or zero. Essentially the *entire* bill is
**Bedrock inference**, driven by the nightly synthetic canary. The cost is the assurance: a
fully-serverless clinical-AI stack — auth, hash-only audit, WORM ledger, and observability —
runs for about the price of its model calls.

## Optimisation levers (in order of impact)

Since the bill is ~99% Bedrock, every meaningful lever is about Bedrock tokens:

1. **Prompt caching (Bedrock) — the single biggest saving.** The ~18k-character system prompt
   is identical on every call; Bedrock prompt caching reads cached tokens at 0.1×, so caching
   the static prefix would cut input-token cost by most of its value. *Not yet implemented.*
   **Correction, 24 Sep 2026: implemented and live.** The live stack runs `PromptCaching=on`
   (queried 24 Sep 2026) — set by hand at some point, not by CI, and from W1 pinned in CI. The
   actuals above predate it; re-read Cost Explorer before quoting them.
2. **Canary cadence.** A nightly *subset* + weekly *full* run (or every-other-night) cuts
   Bedrock spend proportionally while still providing a baseline.
3. **Batch / off-peak.** Bedrock batch inference is ~50% cheaper for non-interactive work like
   the canary (at the cost of latency) — a reasonable trade for a nightly regression run.
4. **CloudWatch metric cardinality** — *not currently a cost* (£0, free tier), but the canary's
   ~95 per-scenario metrics would start to bite if traffic grew; collapsing to the `ALL`
   aggregate + a few key scenarios is the lever if it ever does.

At the floor (no canary, idle app) this deployment costs roughly the price of the KMS key
(~£1/month). The cost *is* the assurance.

## The eval stack (`discharge-eval`, ADR-011): designed, not deployed

Added 26 Nov 2026 (WS7 slice 3). **These are estimates from ADR-011's Costs, not actuals: the
stack is not deployed.** It is a separate stack, deployed by hand (`docs/WS7-RUNBOOK.md`), and
none of it is in the table above. Converted at £0.80 to $1.

| Item | £/month | Basis |
|---|---:|---|
| **Fixed: the customer-managed KMS key** | 0.80 | $1 a key; AWS price list for London, 8 Nov 2026. Rotation is on, so each rotated key version adds its own charge, from a year after the deploy |
| **Fixed: 26 CloudWatch alarms** | 1.84 to 2.08 | $0.10 each beyond the first ten; London, 16 Nov 2026. £1.84 if three of the free ten are left after the live stack's seven; £2.08 if none. **How many are left is unchecked** |
| Fargate (0.5 vCPU, 1 GB, arm64), the weekly run and the Thursday check | under 0.05 | AWS price list for London, 11 Nov 2026 |
| The task's public IPv4 address, while a task runs | about 0.01 | London, 5 Nov 2026 |
| Custom metrics, logs, S3, ECR | pennies | Metric, log and ECR rates checked; **S3 and ECR storage unchecked**. One image in ECR is about 2 pence |
| The VPC, internet gateway, schedules, EventBridge rule, email notifications, two SSM parameters | 0 | **Unchecked**: my reading of each service's free tier at this volume |
| **Fixed total** | **about 2.70 to 2.90** | Not in the WS7 ledger |
| Model spend, the weekly run | 3.30 to 9.90 expected (pipeline); up to 5.74 (v1) | Capped at **£15 a month** by the job's own ledger (ADR-011 D7). Priced at the repository's constants, **not confirmed on AWS's price page** |

- A NAT gateway (about £26 a month) and interface endpoints (about £32) were not chosen: the task
  has a public IP and no inbound rule (ADR-011 D6).
- After tear-down the retained key keeps costing £0.80 a month until it is scheduled for
  deletion, and scheduling it makes the retained bucket unreadable (`docs/WS7-RUNBOOK.md`,
  section 10).
- Replace this table with Cost Explorer actuals a month after the deploy. Use no Service filter
  for the model's cost: Anthropic models are billed under a service name of their own (ADR-011
  D7, point 4).

## Getting the real numbers

Replace the estimates above with actuals from Cost Explorer (last 30 days, by service):

```bash
aws ce get-cost-and-usage \
  --time-period Start=$(date -u -v-30d +%Y-%m-%d),End=$(date -u +%Y-%m-%d) \
  --granularity MONTHLY --metrics "UnblendedCost" \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query "ResultsByTime[].Groups[].{service:Keys[0],cost:Metrics.UnblendedCost.Amount}" \
  --output table
```

(Cost Explorer must be enabled; the API has a small per-request charge. Costs are in USD.)

## Sources

- [Amazon Bedrock pricing (AWS)](https://aws.amazon.com/bedrock/pricing/)
- [CloudWatch pricing (AWS)](https://aws.amazon.com/cloudwatch/pricing/)
