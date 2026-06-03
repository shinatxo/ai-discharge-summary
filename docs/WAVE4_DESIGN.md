# Wave 4 — custom domain + observability (design / scope)

> Status: **DESIGN / for greenlight** · Cowork session 2026-06-03 · Phase 3, Wave 4
> Builds on: the issued us-east-1 ACM cert for `discharge.shinaoguntoye.dev`, the
> `discharge-web` CloudFront stack (slice 4c), and the Wave 3 canary baseline
> (~15–16/18 success nightly, off-peak). No code here — greenlight first.

## 1. Goal & scope
Turn the working-but-anonymous stack into a branded, watched service:
1. **Custom domain go-live** — `discharge.shinaoguntoye.dev` serves the app over the
   issued cert (the visible, recruiter-facing win).
2. **Observability** — CloudWatch alarms on the canary baseline + real-traffic Lambda/API
   metrics, wired to an SNS email topic.
3. **(Optional) WAFv2** — a CloudFront web ACL with AWS managed rules + rate limiting.
4. **(Optional) access logging** — CloudFront + API Gateway access logs.

## 2. The region map (the SAA thread running through this wave)
Wave 4 is a good lesson in *which CloudFront-adjacent things are pinned to us-east-1*:

| Thing | Region | Why |
|---|---|---|
| ACM viewer cert | **us-east-1** | CloudFront reads viewer certs from us-east-1 only (already done) |
| WAFv2 web ACL (`SCOPE=CLOUDFRONT`) | **us-east-1** | CloudFront WAF lives in us-east-1 |
| CloudFront distribution | global | edge network; config is global |
| Route 53 record | global (hosted zone) | alias → distribution (zone id `Z2FDTNDATAQYW2`) |
| Canary metrics + alarms + SNS | **eu-west-2** | alarms must live where the metrics are published |
| App data + inference | **eu-west-2** | ADR-003 residency, unchanged |

So Wave 4 deploys touch three places: `discharge-web` (eu-west-2) for the domain, `discharge-audit`
(eu-west-2) for alarms+SNS, and a **new us-east-1 stack** for WAF (if included).

## 3. Component A — custom domain go-live (`discharge-web` stack)
`web-template.yaml` already carries the exact cutover runbook in comments (lines ~381–428).
The cert exists, so we skip the "create a us-east-1 cert stack" step and pass the ARN in:

- New parameters: `ViewerCertArn` (the issued us-east-1 ACM ARN), `CustomDomain`
  (`discharge.shinaoguntoye.dev`), `HostedZoneId` (the `shinaoguntoye.dev` Route 53 zone id).
- Swap the distribution's `ViewerCertificate` from `CloudFrontDefaultCertificate: true` to:
  ```yaml
  AcmCertificateArn: !Ref ViewerCertArn
  SslSupportMethod: sni-only
  MinimumProtocolVersion: TLSv1.2_2021
  ```
  and add `Aliases: [ !Ref CustomDomain ]`.
- Add a `AWS::Route53::RecordSet` (A + AAAA alias) → `Distribution.DomainName`, alias hosted
  zone `Z2FDTNDATAQYW2`.
- **No app change.** The SPA is same-origin and Cognito uses SRP / USER_PASSWORD (no hosted-UI
  redirect), so there are no callback-URL or CORS changes; the existing CSP already permits the
  Cognito IdP endpoints. (Sanity-check the CSP once live.)
- **Portfolio link** — add a card/link on `shinaoguntoye.dev` (the `portfolio-site` repo)
  pointing to the new URL, so the flagship is reachable from your homepage.
- **Verify:** `dig discharge.shinaoguntoye.dev` resolves to the distribution; browser loads over
  HTTPS with the real cert; a generate round-trips. (CloudFront config changes take ~5–15 min
  to propagate — expect a short wait.)

## 4. Component B — alarms + SNS (`discharge-audit` stack, eu-west-2)
An **SNS topic** + email subscription (one manual confirm click), and alarms in two tiers:

### B1. Synthetic (canary) alarms — namespace `DischargeAssistant/Canary`, dim `Scenario=ALL`
- **`CanaryRunOk` heartbeat** — alarm if the metric is missing or < 1 (`treatMissingData:
  breaching`). Catches the canary *not running* — otherwise an outage hides in silence.
- **`SuccessRatePct` low** — alarm when it drops well below the ~85% baseline (proposed
  threshold **< 65%** so normal night-to-night variation, 83–89%, doesn't flap).
- **`EndToEndLatencyMs` (p90) high** — threshold from the observed baseline p90 + headroom.
- **`BedrockThrottle` (ALL) high** — sustained throttling beyond the 0–3/night baseline.

### B2. Real-traffic operational alarms (complement the synthetic ones)
The canary proves the path works; these watch *actual* usage:
- Lambda **Errors** and **Throttles** on `generate`, `dispatcher`, `status`.
- API Gateway **5xx** count on the HTTP API.
- DynamoDB throttled requests on the audit/results tables.

### Threshold rationale (write it down)
The canary runs **once/night**, so each metric is ~1 datapoint/day — alarms evaluate on a single
daily datapoint and would fire the morning after a bad night. That's fine for a portfolio demo;
if finer resolution is wanted, raise the canary cadence (e.g. every 6 h) — a one-line schedule
change. Thresholds are set to the *observed* baseline, not an idealised 100%, because the residual
~15% is known, quota-driven, and itself documented (Wave 3). **Alarms catch deviation from normal.**

## 5. Component C — WAFv2 (optional; new us-east-1 stack)
A `AWS::WAFv2::WebACL` with `Scope: CLOUDFRONT` in **us-east-1**, associated to the distribution:
- AWS managed rule groups: `AWSManagedRulesCommonRuleSet`, `AWSManagedRulesKnownBadInputsRuleSet`.
- A **rate-based rule** (e.g. 1000 req / 5 min / IP) — basic flood protection.
- `CloudWatchMetricsEnabled` so blocked-request counts are visible (and alarmable).
- **Tradeoff:** adds a third region/stack and a cross-region association (the distribution
  references the us-east-1 web ACL ARN). High security value + SAA-relevant, but it's the one
  piece that adds real surface area — reasonable to sequence **last** or defer to a Wave 4b.

## 6. Component D — access logging (optional, light)
- CloudFront **standard logs v2** → an S3 logs bucket (lifecycle-expired).
- API Gateway **access logs** → a CloudWatch log group.
Useful for the governance story; can be deferred without blocking anything.

## 7. Sequencing & dependencies
```
A. Custom domain  (independent; needs cert ARN + zone id)        ← quick visible win
B. Alarms + SNS   (independent; needs the Wave 3 baseline ✓)     ← closes the loop
C. WAF            (new us-east-1 stack; associate to distribution) ← optional, last
D. Access logs    (optional, anytime)
```
A and B can be done in either order / together. C depends on nothing but adds the most surface,
so it goes last. SNS email subscription needs **one manual confirm click**.

## 8. What needs you (AWS / decisions)
- The **us-east-1 ACM cert ARN** and the **`shinaoguntoye.dev` Route 53 hosted zone id** (deploy params).
- An **email address** for SNS alerts (+ confirm the subscription).
- All `cloudformation deploy`s (the sandbox has no AWS creds), incl. the new us-east-1 WAF stack if included.
- The portfolio-site link edit (your `portfolio-site` repo).

## 9. Out of scope
- Shield Advanced, Lambda@Edge, multi-region DR.
- Raising the Bedrock quota (separate, worthwhile, but not a Wave 4 deliverable).
- Patient v2b review-gated endpoint (separate follow-on).

## 10. Open decisions for greenlight
1. **WAF in or out** for this wave (adds a us-east-1 stack) — recommend *in, sequenced last*.
2. **Access logging in or out** — recommend *defer* unless you want it for the governance narrative now.
3. **Canary cadence** — keep nightly (coarse but cheap) or raise to every 6 h for finer alarm resolution.
