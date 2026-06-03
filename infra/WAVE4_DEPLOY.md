# Wave 4 — deploy + verify runbook (custom domain + alarms)

Design: `docs/WAVE4_DESIGN.md`. Scope: custom domain go-live + alarms/SNS (WAF and
access logging deferred). Two stacks: `discharge-web` (eu-west-2, domain) and
`discharge-audit` (eu-west-2, alarms + SNS). zsh; values held in shell variables.

```bash
REGION=eu-west-2
ALERT_EMAIL='you@example.com'     # <- your email for alarm notifications
```

## 1. Gather the deploy values

```bash
# us-east-1 ACM cert ARN for the domain (CloudFront reads certs from us-east-1)
CERT_ARN=$(aws acm list-certificates --region us-east-1 \
  --query "CertificateSummaryList[?DomainName=='discharge.shinaoguntoye.dev'].CertificateArn | [0]" \
  --output text)
echo "CERT_ARN=$CERT_ARN"      # sanity: must start with arn:aws:acm:us-east-1:...

# Route 53 hosted zone id for the parent domain (strip the /hostedzone/ prefix)
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name shinaoguntoye.dev \
  --query "HostedZones[0].Id" --output text | sed 's#/hostedzone/##')
echo "ZONE_ID=$ZONE_ID"
```

## 2. Deploy the domain (`discharge-web`)

No `package` step — the web template has no local Lambda code. (CloudFront config
changes take ~5–15 min to propagate.)

```bash
cd infra
aws cloudformation deploy --template-file web-template.yaml --stack-name discharge-web \
  --parameter-overrides \
    AuditStackName=discharge-audit \
    ViewerCertArn="$CERT_ARN" \
    CustomDomain=discharge.shinaoguntoye.dev \
    HostedZoneId="$ZONE_ID" \
  --region $REGION
```

## 3. Deploy the alarms + SNS (`discharge-audit`)

`package` first (this stack has Lambda code). Unspecified parameters keep their
previous values, so only `AlertEmail` is new here.

```bash
aws cloudformation package --template-file template.yaml \
  --s3-bucket aws-sam-cli-managed-default-samclisourcebucket-xufyv0yd8kla \
  --output-template-file packaged.yaml
aws cloudformation deploy --template-file packaged.yaml --stack-name discharge-audit \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    Environment=demo PatientV2SecondPass=on CanaryEnabled=on \
    AlertEmail="$ALERT_EMAIL" \
  --region $REGION
```

Then **confirm the SNS subscription** — AWS sends a "Subscription Confirmation"
email; click the link or the alarms email nobody.

## 4. Verify the domain

```bash
# DNS resolves to the CloudFront distribution (alias A/AAAA)
dig +short discharge.shinaoguntoye.dev

# HTTPS serves with the real cert (look for HTTP/2 200 and a valid TLS handshake)
curl -sI https://discharge.shinaoguntoye.dev/ | head -5

# the app URL output:
aws cloudformation describe-stacks --stack-name discharge-web --region $REGION \
  --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue" --output text
```

Then open `https://discharge.shinaoguntoye.dev/` in a browser: it should load over the
new cert, sign in via Cognito, and a generate should round-trip (same-origin — no CORS
preflight, exactly as on the `*.cloudfront.net` host). If the browser shows a cert
warning, give CloudFront a few more minutes to propagate.

## 5. Verify the alarm → email path

Force one alarm into ALARM state to confirm the SNS email actually arrives (it
self-resets on the next evaluation — nothing is broken):

```bash
aws cloudwatch set-alarm-state --region $REGION \
  --alarm-name discharge-audit-canary-not-running \
  --state-value ALARM --state-reason "wave4 smoke test"
```

You should get an email within a minute. Confirm the alarm list looks right:

```bash
aws cloudwatch describe-alarms --region $REGION \
  --alarm-name-prefix discharge-audit- \
  --query "MetricAlarms[].{name:AlarmName,state:StateValue}" --output table
```

Expect 7 alarms: `canary-not-running`, `canary-success-low`, `canary-latency-high`,
`canary-throttle-high`, `generate-errors`, `dispatcher-errors`, `api-5xx`.

## 6. Portfolio link (separate repo)

Add a link/card on `shinaoguntoye.dev` (the `portfolio-site` repo) pointing to
`https://discharge.shinaoguntoye.dev/`, so the flagship is reachable from your homepage.
(Ask Cowork to wire this in if you'd like.)

## Tuning notes
- Alarm thresholds are stack parameters: `CanarySuccessThresholdPct` (65),
  `CanaryLatencyThresholdMs` (200000). Adjust to the baseline as it settles.
- The canary metric is one datapoint/night, so canary alarms evaluate daily and fire
  the morning after a bad night. Raise the canary cadence (its schedule expression) if
  you want finer resolution.
- Deferred to a later pass: WAFv2 (us-east-1 web ACL) and CloudFront/API access logs.
