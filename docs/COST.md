# Cost analysis

A monthly cost estimate for the deployed system, and — more interesting — what drives it.
These are **estimates**; the authoritative numbers are in AWS Cost Explorer (see
[§ Getting the real numbers](#getting-the-real-numbers)).

## Usage profile

This is a portfolio/demo deployment, not a production service. The traffic is almost
entirely **synthetic**: a nightly canary replays the 18 evaluation scenarios through the
live path (~540 generations/month), plus light manual/demo use. Pricing basis:

- **Bedrock Claude Sonnet 4.6:** $3.00 / 1M input tokens, $15.00 / 1M output tokens (on-demand).
- Per generation ≈ 5,000 input + 3,000 output tokens ≈ **$0.06**.
- Converted at ≈ **£0.80 / $1** (approximate; FX moves).

## Estimated monthly cost

| Service | ≈ £/month | Driver |
|---|---:|---|
| **Bedrock** (Sonnet 4.6) | **~£26–36** | ~540 generations/mo × ~$0.06. The dominant cost. |
| **CloudWatch** (custom metrics) | **~£23** | ~95 unique metrics (5 metrics × 18 scenarios + aggregates) at $0.30/metric/mo. |
| **KMS** | ~£0.80 | $1/mo flat per customer-managed key. |
| **Route 53** | ~£0 | Hosted zone shared with the portfolio domain — no incremental cost. |
| **Lambda** | ~£0 | ~20k GB-s/mo vs the 400k GB-s free tier. |
| **DynamoDB** | ~£0 | On-demand, hash-only rows — pennies. |
| **S3 + Object Lock (WORM)** | ~£0.10 | Tiny static assets + small ledger objects. |
| **CloudFront / API Gateway / Cognito / EventBridge / SNS / ACM** | ~£0 | Within free tier at this volume. |
| **Total** | **~£50–60/month** | ~95% is Bedrock + CloudWatch metrics. |

## The headline insight

**The application itself, at rest, is essentially free-tier.** Lambda, DynamoDB, S3,
CloudFront, API Gateway, Cognito — all pennies or zero at demo volume. Almost the entire
bill is the **observability that was added on top**: the canary's nightly Bedrock calls and
its per-scenario CloudWatch metrics. That's a deliberate trade (continuous assurance costs
money) — and it's fully optimisable.

## Optimisation levers (in order of impact)

1. **Prompt caching (Bedrock).** The ~18k-character system prompt is identical on every
   call. Bedrock prompt caching reads cached tokens at 0.1× — caching the static prefix
   would cut the input-token cost by most of its value. *Not yet implemented; the single
   biggest Bedrock saving.*
2. **CloudWatch metric cardinality.** The canary emits a metric per scenario (18×) per
   dimension. Keeping only the `ALL` aggregate plus 2–3 canary scenarios would drop
   CloudWatch from ~£23 to ~£3 — at the cost of per-scenario granularity.
3. **Canary cadence.** A nightly *subset* + weekly *full* run, or every-other-night, cuts
   Bedrock spend proportionally while still providing a baseline.
4. **Batch / off-peak.** Bedrock batch inference is ~50% cheaper for non-interactive work
   like the canary (at the cost of latency) — a viable trade for a nightly regression run.

At the floor (no canary, idle app) this deployment costs roughly the price of the KMS key
(~£1/month). The cost *is* the assurance.

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
