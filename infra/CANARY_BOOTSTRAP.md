# Wave 3 canary — bootstrap + deploy + smoke-test runbook

One-time setup for the synthetic-traffic canary (`src/canary/app.py`,
`docs/WAVE3_SYNTHETIC_TRAFFIC_DESIGN.md`). The Lambda + role are created by the
normal deploy; this runbook covers the bits plain CloudFormation can't author: the
**synthetic Cognito user** and its **SSM SecureString password**. Run it once, then
flip the nightly schedule on.

All commands assume `discharge-audit` in `eu-west-2`. They're written for zsh, so
shell variables are used instead of `<placeholders>` (zsh reads `<` / `>` as
redirection).

```bash
STACK=discharge-audit
REGION=eu-west-2
CANARY_USER=canary@synthetic.invalid       # matches the CanaryUsername param default
PARAM_NAME=/discharge/canary/password       # matches CanaryPasswordParamName default
CANARY_PW='ReplaceMe-Strong12+Chars!'       # choose a strong password; do NOT reuse a real one

POOL_ID=$(aws cloudformation describe-stacks --stack-name $STACK --region $REGION \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name $STACK --region $REGION \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" --output text)
```

## 1. Create the synthetic user

**What:** an admin-created Cognito user, no MFA, dedicated to the canary.
**Why:** the pool is `AllowAdminCreateUserOnly: true` (no self-signup), so a service
identity must be admin-created. Isolating it from real demo users keeps its traffic
taggable and its blast radius nil — it can only do what any signed-in user can.

```bash
aws cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" --region $REGION \
  --username "$CANARY_USER" \
  --message-action SUPPRESS \
  --user-attributes Name=email,Value="$CANARY_USER" Name=email_verified,Value=true
```

## 2. Set a permanent password

**Why:** `admin-create-user` leaves the user in `FORCE_CHANGE_PASSWORD`, which makes
`USER_PASSWORD_AUTH` return a `NEW_PASSWORD_REQUIRED` challenge instead of tokens.
`--permanent` clears that so a headless `InitiateAuth` returns an IdToken directly.

```bash
aws cognito-idp admin-set-user-password \
  --user-pool-id "$POOL_ID" --region $REGION \
  --username "$CANARY_USER" \
  --password "$CANARY_PW" --permanent
```

## 3. Store the password in SSM (SecureString)

**What / Why:** the password lives in Parameter Store as a KMS-encrypted SecureString,
never in the template or code. The canary role can read this one parameter and decrypt
it only via SSM (`kms:ViaService` condition). Parameter Store (vs Secrets Manager) is
the cheaper SSM-native choice for a single value with no rotation requirement.

```bash
aws ssm put-parameter \
  --name "$PARAM_NAME" --region $REGION \
  --type SecureString \
  --value "$CANARY_PW" --overwrite
```

(Optional sanity — confirm the synthetic user can authenticate and yields an IdToken:)

```bash
aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" --region $REGION \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME="$CANARY_USER",PASSWORD="$CANARY_PW" \
  --query "AuthenticationResult.IdToken" --output text | cut -c1-40
```

## 4. Deploy (creates the canary Lambda + role; schedule still OFF)

The canary code + role ship with the normal stack deploy. `CanaryEnabled` defaults to
`off`, so no nightly schedule is created yet — we smoke-test by hand first. (Unspecified
parameters keep their previous values, so `PatientV2SecondPass=on` is preserved.)

```bash
cd infra
aws cloudformation package --template-file template.yaml \
  --s3-bucket aws-sam-cli-managed-default-samclisourcebucket-xufyv0yd8kla \
  --output-template-file packaged.yaml
aws cloudformation deploy --template-file packaged.yaml --stack-name discharge-audit \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Environment=demo PatientV2SecondPass=on \
  --region eu-west-2
```

## 5. Smoke-test: invoke the canary once by hand

```bash
FN=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query "Stacks[0].Outputs[?OutputKey=='CanaryFunctionName'].OutputValue" --output text)

aws lambda invoke --function-name "$FN" --region eu-west-2 \
  --cli-binary-format raw-in-base64-out --payload '{}' /tmp/canary-out.json
cat /tmp/canary-out.json
```

Expect something like `{"ok": true, "scenarios": 18, "success": 18, "parse_ok": 18,
"throttled": 0, "run_ms": ...}`. (A first cold run may take 1–3 min as all 18
generations run; the Lambda timeout is 300 s and the poll budget 240 s.)

Then confirm the metrics landed:

```bash
aws cloudwatch list-metrics --namespace DischargeAssistant/Canary --region eu-west-2 \
  --query "Metrics[].MetricName" --output text | tr '\t' '\n' | sort -u
```

You should see `CanaryRunOk`, `EndToEndSuccess`, `EndToEndLatencyMs`, `ParseOk`,
`JobFailed`, `BedrockThrottle`, `SuccessRatePct`.

## 6. Turn the nightly schedule ON

Once the manual run is green, redeploy with the schedule enabled (02:00 Europe/London):

```bash
aws cloudformation deploy --template-file packaged.yaml --stack-name discharge-audit \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Environment=demo PatientV2SecondPass=on CanaryEnabled=on \
  --region eu-west-2
```

Let it run for ~5–7 nights to lay down a baseline before Wave 4 sets alarm thresholds.

---

### Teardown / rotate
- Rotate the password: repeat steps 2–3 with a new value (canary picks it up next run).
- Remove the user: `aws cognito-idp admin-delete-user --user-pool-id "$POOL_ID"
  --username "$CANARY_USER" --region $REGION` and delete the SSM parameter; redeploy
  with `CanaryEnabled=off` to drop the schedule.

### Cost / residency
~18 Bedrock Sonnet generations per night, all in eu-west-2 (ADR-003 unchanged).
Negligible spend; the data + inference stay UK-only.
