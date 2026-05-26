# Slice 4a — Deploy & smoke test

What we're verifying: a clinician's IdToken, obtained from the Cognito User
Pool, lets them call `POST /generate` through the HTTP API; an unauthenticated
caller gets 401; the audit row records the `sub` from the **JWT**, not from the
request body; and one new ledger object lands in the WORM bucket as a result.

Run these from `ai-discharge-summary/infra/`. Region = `eu-west-2`, stack =
`discharge-audit`.

---

## 1) Package + deploy

```bash
cd infra

# Upload the Lambda zips to the SAM-managed bucket, get a packaged template
# that has S3 URLs in place of local Code: paths.
aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket aws-sam-cli-managed-default-samclisourcebucket-xufyv0yd8kla \
  --output-template-file packaged.yaml

# Deploy the packaged template. CAPABILITY_NAMED_IAM is needed because we name
# the IAM roles (RoleName: ...). The parameter overrides keep this a demo deploy.
aws cloudformation deploy \
  --template-file packaged.yaml \
  --stack-name discharge-audit \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Environment=demo \
  --region eu-west-2
```

Why these flags, briefly:

- `package` is what lets us avoid the SAM transform (blocked on this account —
  see the comment at the top of `template.yaml`). It rewrites the local `Code:`
  paths to S3 URLs without needing any macro permission.
- `CAPABILITY_NAMED_IAM` is required because we name our IAM roles (the
  `*-generate-fn-role` and `*-ledger-fn-role`). Naming roles makes them
  reviewable in the console; the cost is needing this capability flag.

When deploy finishes, capture the outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name discharge-audit \
  --region eu-west-2 \
  --query 'Stacks[0].Outputs' \
  --output table
```

You should see new outputs `UserPoolId`, `UserPoolClientId`, `HttpApiId`,
`HttpApiEndpoint`, `JwtIssuerUrl`, plus the existing ones from slices 1–3.

Export them for the rest of the recipe:

```bash
export USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text)
export CLIENT_ID=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolClientId`].OutputValue' --output text)
export API_ENDPOINT=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`HttpApiEndpoint`].OutputValue' --output text)
echo "Pool: $USER_POOL_ID"
echo "Client: $CLIENT_ID"
echo "API: $API_ENDPOINT"
```

---

## 2) Create a demo Cognito user

We admin-create the user (no self-signup), suppress the welcome email, then
admin-set a permanent password so the user can sign in immediately without the
"new password required" challenge.

```bash
# Use a throwaway email; no real mail is sent because MessageAction=SUPPRESS.
export DEMO_EMAIL="demo+slice4a@example.invalid"
export DEMO_PASSWORD='Demo-Slice4a-Passw0rd!'   # meets the 12-char policy

aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$DEMO_EMAIL" \
  --user-attributes Name=email,Value="$DEMO_EMAIL" Name=email_verified,Value=true \
  --message-action SUPPRESS \
  --region eu-west-2

aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" \
  --username "$DEMO_EMAIL" \
  --password "$DEMO_PASSWORD" \
  --permanent \
  --region eu-west-2
```

---

## 3) Get an IdToken

`USER_PASSWORD_AUTH` is the simplest flow for a curl smoke test; SRP is what
Amplify will use in slice 4b but it needs a client library to compute the
verifier.

```bash
RAW=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME="$DEMO_EMAIL",PASSWORD="$DEMO_PASSWORD" \
  --region eu-west-2)
export ID_TOKEN=$(echo "$RAW" | python3 -c 'import sys,json;print(json.load(sys.stdin)["AuthenticationResult"]["IdToken"])')
echo "IdToken length: ${#ID_TOKEN}"   # expect a long string
```

Sanity-check the token's `sub`/`aud`/`iss` claims (don't trust this for auth —
it's the *server* that verifies the signature; this is just for humans):

```bash
python3 -c '
import base64, json, os, sys
payload = os.environ["ID_TOKEN"].split(".")[1]
payload += "=" * (-len(payload) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
for k in ("sub","aud","iss","token_use","email","exp"):
    print(f"  {k}: {claims.get(k)}")
'
```

Expected: `token_use = id`, `aud = $CLIENT_ID`, `iss` ends with the User Pool
id. The `sub` is the opaque UUID — this is what should land in the audit row.

---

## 4) Negative test FIRST — unauthenticated call must be rejected

```bash
curl -i -X POST "$API_ENDPOINT/generate" \
  -H 'content-type: application/json' \
  -d '{"notes":"should not reach lambda"}'
```

Expect: **`HTTP/2 401`** with a body like `{"message":"Unauthorized"}`. This
proves the JWT authoriser is wired — API Gateway rejected the request before
Lambda was ever invoked.

Also try a malformed token (proves signature validation, not just presence):

```bash
curl -i -X POST "$API_ENDPOINT/generate" \
  -H "authorization: Bearer not.a.real.jwt" \
  -d '{"notes":"x"}'
```

Expect: **`HTTP/2 401`**.

---

## 5) The real call — authenticated, expect 200 + three outputs

```bash
curl -s -X POST "$API_ENDPOINT/generate" \
  -H "authorization: Bearer $ID_TOKEN" \
  -H 'content-type: application/json' \
  -d @- <<'JSON' | python3 -m json.tool | head -40
{
  "notes": "Admitted 25/05/26. 72M, NSTEMI on troponin. PMH: HTN, T2DM. Started ticagrelor 90mg BD, bisoprolol increased 5->7.5mg. DNACPR placed by Dr Patel 26/05/26 after MDT (family present). Fondaparinux inpatient only. D/c home 27/05/26. GP to repeat U&Es in 1 week. Pre-admission: ramipril 10mg, atorvastatin 80mg, metformin 1g BD."
}
JSON
```

Expect:

- `"ok": true`
- `"generation_id"` like `01HZ...` (a ULID)
- `"parse_ok": true`
- `"outputs"` containing `summary`, `gp_letter`, `patient` keys

If you see `"error": "bedrock_error"` with message about marketplace
subscription / `AccessDeniedException`, that's the same transient blip we hit
on 2026-05-25 — wait ~2 minutes and retry before changing anything.

> **⚠ Known constraint (logged 2026-05-26).** API Gateway HTTP API has a HARD
> 30-second integration timeout. Bedrock Sonnet at `MAX_TOKENS = 4096` runs
> ~20–25 s warm and longer on a cold start. If you see `503 Service
> Unavailable` on the very first call, especially with a longer ward-round
> note, the most likely cause is that cap. The Lambda will keep running
> server-side, complete the generation, and leave an audit row + ledger
> object behind — so when you check `aws logs tail` a minute later you *will*
> see the `START`/`END`/`REPORT` lines for that "failed" call, and you *will*
> see a ledger object for it. Don't redeploy; just rerun the curl on a warm
> container or with a shorter input. Slice 4b moves to an async `202 + poll`
> pattern that removes this constraint entirely — see `docs/ADR-phase1.md`
> §ADR-004 "Slice 4a findings".

---

## 6) Verify the audit row records the JWT `sub` (not anything from the body)

```bash
# Pull the most recent audit row for our demo user.
SUB=$(python3 -c 'import base64,json,os; p=os.environ["ID_TOKEN"].split(".")[1]; p+="="*(-len(p)%4); print(json.loads(base64.urlsafe_b64decode(p))["sub"])')

aws dynamodb query \
  --table-name discharge-audit-audit \
  --key-condition-expression "PK = :pk" \
  --expression-attribute-values "{\":pk\": {\"S\": \"USER#${SUB}\"}}" \
  --region eu-west-2 \
  --query 'Items[0].{user_sub:user_sub.S, generation_id:generation_id.S, draft:draft.BOOL, input_sha256:input_sha256.S, model_version:model_version.S, parse_ok:parse_ok.BOOL}'
```

Expect: `user_sub` matches the `sub` from the IdToken (not anything you put
in the request body). `draft: true`. `parse_ok: true`.

---

## 7) Verify the ledger picked up the new event

The DynamoDB Streams → WORM bucket pipeline (slice 3) should land a fresh
ledger object within ~10 seconds of the audit write:

```bash
LEDGER_BUCKET=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`LedgerBucketName`].OutputValue' --output text)
aws s3 ls "s3://${LEDGER_BUCKET}/ledger/" --recursive --region eu-west-2 | tail -5
```

Expect: a new `ledger/year=YYYY/month=MM/day=DD/...json` object timestamped
within the last minute.

---

## What "PASS" looks like

- Step 4: 401 from both unauth + malformed-token calls.
- Step 5: 200 + three outputs.
- Step 6: audit row's `user_sub` == JWT `sub` (not body).
- Step 7: new ledger object exists, immutable for the retention window.

If all four hold, Slice 4a is verified end-to-end.
