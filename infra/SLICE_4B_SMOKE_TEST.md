# Slice 4b — Deploy & smoke test

What we're verifying:

- `POST /generate` now returns **202 + `{job_id, status: "pending"}`** in **&lt; 1 s**.
- `GET /generations/{id}` polls cleanly through `pending → complete`, with the
  three outputs landing in the new `ResultsTable` (NOT the audit table).
- The two slice-4a findings are cured:
  - **30 s timeout** — the client never blocks on Bedrock; the dispatcher
    returns 202 immediately. Bedrock can take 20–25 s, 30 s, or 60 s and the
    UX is identical (status stays `pending`, then flips to `complete`).
  - **Ghost record** — a retried POST with the same `Idempotency-Key`
    returns the **same** `job_id` and does **NOT** double-invoke Bedrock.
- Audit row continues to record `user_sub = JWT.sub` (the slice-4a anti-spoof
  contract is preserved).
- A new ledger object lands in the WORM bucket per audit-table change.
- A cross-user `GET` returns 404 (existence is not leaked across users).

Run these from `ai-discharge-summary/infra/`. Region = `eu-west-2`, stack =
`discharge-audit`.

---

## 1) Package + deploy

```bash
cd infra

aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket aws-sam-cli-managed-default-samclisourcebucket-xufyv0yd8kla \
  --output-template-file packaged.yaml

aws cloudformation deploy \
  --template-file packaged.yaml \
  --stack-name discharge-audit \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Environment=demo \
  --region eu-west-2
```

If the deploy reports `No changes to deploy`, that means you ran it from a
session where the previous package step didn't actually upload new code —
re-run `package` first.

When the deploy finishes, capture the slice-4b outputs:

```bash
aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?contains(`["DispatcherFunctionName","StatusFunctionName","ResultsTableName","HttpApiEndpoint","UserPoolId","UserPoolClientId","AuditTableName","LedgerBucketName"]`, OutputKey)]' \
  --output table
```

Export the values used below:

```bash
export USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text)
export CLIENT_ID=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolClientId`].OutputValue' --output text)
export API_ENDPOINT=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`HttpApiEndpoint`].OutputValue' --output text)
export AUDIT_TABLE=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`AuditTableName`].OutputValue' --output text)
export RESULTS_TABLE=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`ResultsTableName`].OutputValue' --output text)
export LEDGER_BUCKET=$(aws cloudformation describe-stacks --stack-name discharge-audit --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`LedgerBucketName`].OutputValue' --output text)
```

---

## 2) Re-use the slice-4a demo user (or create one)

If you already created the demo user from `SLICE_4A_SMOKE_TEST.md`, skip
this. Otherwise:

```bash
export DEMO_EMAIL="demo+slice4a@example.invalid"
export DEMO_PASSWORD='Demo-Slice4a-Passw0rd!'

aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" --username "$DEMO_EMAIL" \
  --user-attributes Name=email,Value="$DEMO_EMAIL" Name=email_verified,Value=true \
  --message-action SUPPRESS --region eu-west-2

aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" --username "$DEMO_EMAIL" \
  --password "$DEMO_PASSWORD" --permanent --region eu-west-2
```

Get an IdToken:

```bash
RAW=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME="$DEMO_EMAIL",PASSWORD="$DEMO_PASSWORD" \
  --region eu-west-2)
export ID_TOKEN=$(echo "$RAW" | python3 -c 'import sys,json;print(json.load(sys.stdin)["AuthenticationResult"]["IdToken"])')
export DEMO_SUB=$(python3 -c '
import base64, json, os
p = os.environ["ID_TOKEN"].split(".")[1]
p += "=" * (-len(p) % 4)
print(json.loads(base64.urlsafe_b64decode(p))["sub"])
')
echo "sub: $DEMO_SUB"
```

---

## 3) Negative tests FIRST — both routes must reject unauthenticated calls

```bash
# Unauth POST -> 401 (API Gateway, before Lambda)
curl -i -X POST "$API_ENDPOINT/generate" \
  -H 'content-type: application/json' \
  -d '{"notes":"unauth test"}'

# Unauth GET -> 401
curl -i "$API_ENDPOINT/generations/01HZZZAAAAAAAAAAAAAAAAAAAA"
```

Both expect `HTTP/2 401` with `{"message":"Unauthorized"}`.

---

## 4) The real call — POST returns 202 in &lt; 1 s

```bash
export IDEM_KEY=$(uuidgen | tr 'A-Z' 'a-z')
echo "Idempotency-Key: $IDEM_KEY"

# Time the call to PROVE it's sub-second (the slice-4a 30s ceiling is gone).
time curl -s -X POST "$API_ENDPOINT/generate" \
  -H "authorization: Bearer $ID_TOKEN" \
  -H 'content-type: application/json' \
  -H "idempotency-key: $IDEM_KEY" \
  -d @- <<'JSON' > /tmp/dispatch.json
{
  "notes": "Admitted 25/05/26. 72M, NSTEMI on troponin. PMH: HTN, T2DM. Started ticagrelor 90mg BD, bisoprolol increased 5->7.5mg. DNACPR placed by Dr Patel 26/05/26 after MDT (family present). Fondaparinux inpatient only. D/c home 27/05/26. GP to repeat U&Es in 1 week. Pre-admission: ramipril 10mg, atorvastatin 80mg, metformin 1g BD."
}
JSON
cat /tmp/dispatch.json | python3 -m json.tool
export JOB_ID=$(python3 -c 'import json; print(json.load(open("/tmp/dispatch.json"))["job_id"])')
echo "Job: $JOB_ID"
```

Expect:
- `real 0m0.5s` or so (well under 1 s)
- `"ok": true`
- `"status": "pending"`
- `"job_id": "01H..."` (a 26-char ULID)
- `"poll_url": "/generations/01H..."`

---

## 5) Poll the status endpoint until terminal

```bash
# Write each response to a temp file and parse from disk. Avoids a Python 3.14
# quirk where `echo "$RESP" | python3 -c 'json.load(sys.stdin)'` rejects
# valid JSON containing multi-byte UTF-8 (em-dashes, backticks) with
# "Invalid control character" — a stdin encoding edge case, not a Lambda bug.
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  sleep 2
  curl -s "$API_ENDPOINT/generations/$JOB_ID" \
    -H "authorization: Bearer $ID_TOKEN" \
    > /tmp/poll.json
  STATUS=$(python3 -c 'import json; print(json.load(open("/tmp/poll.json")).get("status"))')
  echo "t+$((i*2))s : status=$STATUS"
  if [ "$STATUS" = "complete" ] || [ "$STATUS" = "failed" ] || [ "$STATUS" = "expired" ]; then
    echo "--- final response (first 60 lines) ---"
    python3 -m json.tool /tmp/poll.json | head -60
    break
  fi
done
```

Expect: a few `pending` rows, then `complete` with `outputs.summary`,
`outputs.gp_letter`, `outputs.patient` populated. Total wall-clock from the
POST to `complete` is the worker latency (~20–25 s warm, longer on cold
start, **up to Lambda's own 60 s timeout — the old 30 s API Gateway cap is no
longer in play**) — but the client never blocks on it.

---

## 6) Idempotency replay — the ghost-record cure

Re-POST with the SAME `Idempotency-Key`. The dispatcher must return the
SAME `job_id` and must NOT fire the worker a second time.

```bash
curl -s -X POST "$API_ENDPOINT/generate" \
  -H "authorization: Bearer $ID_TOKEN" \
  -H 'content-type: application/json' \
  -H "idempotency-key: $IDEM_KEY" \
  -d '{"notes":"this body is ignored on replay - Stripe-style"}' \
  | python3 -m json.tool
```

Expect:
- `"job_id"` == the original `$JOB_ID`
- `"idempotent_replay": true`
- HTTP `200` (not 202)

Confirm in DynamoDB that only ONE GEN# row exists for this user_sub (no
ghost row from the replay):

```bash
aws dynamodb query \
  --table-name "$AUDIT_TABLE" \
  --key-condition-expression "PK = :pk AND begins_with(SK, :pfx)" \
  --expression-attribute-values "{\":pk\": {\"S\": \"USER#${DEMO_SUB}\"}, \":pfx\": {\"S\": \"GEN#${JOB_ID}\"}}" \
  --region eu-west-2 \
  --query 'Count'
```

Expect: `1`.

---

## 7) Audit row preserves the JWT-as-identity contract

```bash
aws dynamodb get-item \
  --table-name "$AUDIT_TABLE" \
  --key "{\"PK\": {\"S\": \"USER#${DEMO_SUB}\"}, \"SK\": {\"S\": \"GEN#${JOB_ID}\"}}" \
  --region eu-west-2 \
  --query 'Item.{user_sub:user_sub.S, status:status.S, parse_ok:parse_ok.BOOL, input_sha256:input_sha256.S, model_version:model_version.S, idempotency_key:idempotency_key.S}'
```

Expect: `user_sub` matches `$DEMO_SUB` (the JWT `sub`, not anything from the
body). `status: complete`. `idempotency_key` matches `$IDEM_KEY`.

The outputs are NOT in this row — they're in the transient ResultsTable
(ADR-005 keeps the audit log hash-only):

```bash
aws dynamodb get-item \
  --table-name "$RESULTS_TABLE" \
  --key "{\"PK\": {\"S\": \"USER#${DEMO_SUB}\"}, \"SK\": {\"S\": \"RES#${JOB_ID}\"}}" \
  --region eu-west-2 \
  --query 'Item.{job_id:job_id.S, completed_at:completed_at.S, ttl:ttl.N}'
```

Expect: a row exists, with `ttl` ≈ now + 86400.

---

## 8) Ledger picked up the state changes

The audit table emitted two stream events for this job: INSERT (pending) and
MODIFY (pending → complete). Both should land as immutable objects in the
WORM ledger within ~10 s:

```bash
aws s3 ls "s3://${LEDGER_BUCKET}/ledger/" --recursive --region eu-west-2 | tail -10
```

Expect: at least two new objects timestamped within the last minute.

---

## 9) Cross-user 404 — existence is not leaked across users

Create a second demo user, get their IdToken, then GET the first user's
`job_id`. Must return 404 (NOT 403).

```bash
aws cognito-idp admin-create-user --user-pool-id "$USER_POOL_ID" \
  --username 'demo+other@example.invalid' \
  --user-attributes Name=email,Value='demo+other@example.invalid' Name=email_verified,Value=true \
  --message-action SUPPRESS --region eu-west-2
aws cognito-idp admin-set-user-password --user-pool-id "$USER_POOL_ID" \
  --username 'demo+other@example.invalid' --password 'Demo-Other-Passw0rd!' \
  --permanent --region eu-west-2

OTHER_RAW=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME='demo+other@example.invalid',PASSWORD='Demo-Other-Passw0rd!' \
  --region eu-west-2)
OTHER_TOKEN=$(echo "$OTHER_RAW" | python3 -c 'import sys,json;print(json.load(sys.stdin)["AuthenticationResult"]["IdToken"])')

curl -i "$API_ENDPOINT/generations/$JOB_ID" \
  -H "authorization: Bearer $OTHER_TOKEN"
```

Expect: `HTTP/2 404` with `{"ok": false, "error": "not_found", ...}`. The
job DOES exist, just not for this user — the 404 is uniform with "no such
job", which is the correct posture for not confirming/denying existence
across users.

---

## What "PASS" looks like

| step | check |
|------|-------|
| 3 | Both unauth calls → 401. |
| 4 | POST returns 202 in &lt; 1 s with a job_id. |
| 5 | Polling sees pending → complete and the three outputs are populated. |
| 6 | Replay with same Idempotency-Key returns same job_id; only ONE GEN# row exists. |
| 7 | Audit row has user_sub == JWT sub; outputs live in ResultsTable, not the audit table. |
| 8 | New ledger objects landed in the WORM bucket. |
| 9 | Cross-user GET → 404. |

If all seven hold, Slice 4b is verified end-to-end. The slice-4a ghost-record
hazard and 30 s ceiling are demonstrably gone.
