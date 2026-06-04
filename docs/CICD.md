# CI/CD pipeline

`.github/workflows/ci-cd.yml` runs on every push and pull request:

- **`test` job** (always) — installs `pytest` + `boto3` + `cfn-lint`, runs the 38 unit
  tests (they mock AWS, so no credentials needed), checks the canary scenario bundle is
  current, and lints both CloudFormation templates. This is the gate.
- **`deploy` job** (only on a push to `main`, and only if `test` passed) — assumes an AWS
  role via **OIDC** (short-lived credentials, no stored keys) and `package` + `deploy`s the
  `discharge-audit` stack.

The `test` job works immediately with no setup. The `deploy` job needs the two one-time
steps below.

## One-time setup for the deploy job

### 1. Create the OIDC deploy role

```bash
aws cloudformation deploy \
  --template-file infra/cicd-oidc-role.yaml \
  --stack-name discharge-cicd \
  --capabilities CAPABILITY_NAMED_IAM \
  --region eu-west-2
```

This creates `GitHubActionsDischargeDeploy`, trusted only by workflows in
`shinatxo/ai-discharge-summary` (via the GitHub OIDC provider your portfolio-site pipeline
already set up). Its ARN already matches the `role-to-assume` in the workflow.

> If the account has no GitHub OIDC provider yet, create it first (provider URL
> `https://token.actions.githubusercontent.com`, audience `sts.amazonaws.com`), then deploy
> the role.

### 2. Set the alert-email repo variable

The deploy passes `AlertEmail` to the stack. Store it as an Actions **variable** (not a
secret — it's just an address):

```bash
gh variable set ALERT_EMAIL --repo shinatxo/ai-discharge-summary --body "shinaoguntoye@hotmail.co.uk"
```

(or GitHub → repo → Settings → Secrets and variables → Actions → Variables → New variable).

That's it. Push to `main`: the gate runs, and on green the stack deploys with no long-lived
keys anywhere.

## Status badge

Add to the top of `README.md` (already included):

```markdown
[![CI/CD](https://github.com/shinatxo/ai-discharge-summary/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/shinatxo/ai-discharge-summary/actions/workflows/ci-cd.yml)
```

## Honest notes / scope

- **The deploy role is deliberately broad.** CloudFormation must manage ~20 resources across
  Lambda, DynamoDB, KMS, S3, Cognito, API Gateway, EventBridge Scheduler, SNS, CloudWatch and
  Logs, so a per-resource least-privilege policy would be large and brittle. The controls that
  make this acceptable for a demo account: the **OIDC trust scopes assumption to this one
  repo**, permissions are **bounded to eu-west-2 / us-east-1** via `aws:RequestedRegion`, and
  IAM actions are scoped to the stack's `discharge-audit-*` role prefix. Tightening to
  per-resource ARNs is a documented future hardening — consistent with this project's habit of
  naming what isn't done rather than hiding it.
- **CD covers the `discharge-audit` stack only.** The `discharge-web` (CloudFront + custom
  domain) stack is intentionally deployed by hand: it changes rarely, needs the us-east-1 cert
  ARN + Route 53 zone id as inputs, and CloudFront propagation makes fast iteration pointless.
  See [`infra/WAVE4_DEPLOY.md`](../infra/WAVE4_DEPLOY.md).
- **Pull requests run the gate but never deploy** — the `deploy` job is `if`-guarded to direct
  pushes to `main`, so a fork PR can't trigger a deploy.
