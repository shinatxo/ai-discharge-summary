# Ephemeral stack — runbook (W2, first run 28 Sep 2026)

What this is, and why: `docs/ADR-phase1.md` ADR-009 (d), *Isolation during W2–W3*. This file is the
**how** for one throwaway stack, `discharge-eph-<yyyymmdd>`, deployed by hand from
`feat/agentic-pipeline`, tested, and **fully** deleted. It never touches `discharge-audit`.

**Goals of the first run (28 Sep 2026):**
- (a) prove an ephemeral stack can be created and fully deleted;
- (b) find out whether `dynamodb:Attributes` is evaluated for a `PutItem` **inside
  `TransactWriteItems`** (ADR-009, W1 change set (c); *Owed* table);
- (c) check DPIA Annex C.2 — does the structured-output grammar cache hold only the schema?

**Key decision (author, 27 Sep 2026): the stack creates its own KMS key** — ADR-009's cut-order
item (1). No `ExistingCmkArn` template change. Why: none of the goals needs the live key; the
template under test is exactly the one that merges at W11; unreleased code gets no grants on the
live audit key; its CloudTrail stays clean. Cost: one extra delete step (schedule the stack's own
key for deletion) and pennies of key time. Revisit `ExistingCmkArn` only if ephemeral stacks
become frequent enough that the extra step is a burden.

**Rules.** Your own credentials (`user/Shina`), never the CI role. Synthetic notes only.
`CanaryEnabled=off`. Stack name ≤ 33 characters (the ledger bucket name caps at 63). Keep clear
of **02:00 nightly and 03:00 Monday** — the Bedrock quota is account-wide. Every synchronous invoke
carries `--cli-read-timeout`. CLI JSON inputs are built by Python and passed with
`--cli-input-json`. **If any step fails: stop, run the delete sequence (Steps 8–9), and record
what, if anything, is left.**

---

## Checklist

| # | Step | Time | Result | Done |
|---|---|---|---|---|
| 0 | Branch: switch to `feat/agentic-pipeline`, fast-forward to `main` | 2 min | `3186da4..213417a`, fast-forward | ✅ 28 Sep |
| 1 | Session setup + pre-flight (read-only) | 5 min | 22 chars; `user/Shina`; no stack, no bucket | ✅ 28 Sep |
| 2 | Package + deploy | 10 min | `CREATE_COMPLETE` | ✅ 28 Sep |
| 3 | Save outputs | 2 min | own key created 11:45:51 BST | ✅ 28 Sep |
| 4 | Cognito test user + token | 5 min | token issued | ✅ 28 Sep |
| 5 | Baseline generation (unchanged policy) | 5 min | `complete`, 42 s | ✅ 28 Sep |
| 6 | Transaction test: full whitelist, then whitelist minus one | 15 min | 6a `complete` 41 s; 6b `AccessDeniedException`, nothing written — **enforced** | ✅ 28 Sep |
| 7 | DPIA Annex C.2 check | 5 min | documentation only (v1 path compiles no grammar) | ✅ 28 Sep |
| 8 | Delete, day 1: deletion protection off → `delete-stack` | 10 min | `DELETE_COMPLETE` 15:13; skipped: key, bucket | ✅ 28 Sep |
| 9 | Delete, day 2: empty + remove the ledger bucket → schedule the stack's key | 10 min | 6 versions, bucket deleted 07:54; key `PendingDeletion` → 7 Oct | ✅ 30 Sep |
| 10 | Record in ADR-009 (stack log + *Owed*) | 10 min | stack log row; Owed rows closed; DPIA v2.6 | ✅ 30 Sep |

---

## Step 0 — the branch

The build branch was created from `3186da4`; `main` has since gained the records commit
`213417a`. The branch is an ancestor of `main`, so this is a **fast-forward** — no merge commit.

```bash
cd ~/Documents/Claude/Projects/"Cloud Projects 1"/ai-discharge-summary
git status --short
git switch feat/agentic-pipeline
git merge --ff-only main
git log --oneline -1
```

Expect `213417a` on the last line. `infra/EPHEMERAL_STACK_RUNBOOK.md` shows as untracked (`??`) —
it is not in either commit, and it moves with you. **Do not push yet**; nothing here needs it.

## Step 1 — session setup and pre-flight (read-only)

Everything later reads these variables. They are also written to a file **outside the repo**, so
the day-2 delete can reload them.

```bash
setopt interactivecomments
export AWS_REGION=eu-west-2 AWS_PAGER=""
export STACK=discharge-eph-20260928
export EPH=~/eph-20260928
mkdir -p $EPH && chmod 700 $EPH
echo "export STACK=$STACK EPH=$EPH AWS_REGION=eu-west-2 AWS_PAGER=''" > $EPH/env.sh
echo ${#STACK}
aws sts get-caller-identity --query Arn --output text
aws cloudformation describe-stacks --stack-name $STACK 2>&1 | tail -1
aws s3api head-bucket --bucket $STACK-ledger-061051247394-eu-west-2 2>&1 | tail -1
date
```

Expect:
- a length of **22** (≤ 33);
- `arn:aws:iam::061051247394:user/Shina`;
- `Stack with id discharge-eph-20260928 does not exist`;
- `Not Found` (or a 404) for the bucket — a leftover bucket would make the deploy fail;
- a time well clear of 02:00 and, on a Monday, 03:00.

## Step 2 — package and deploy

*What:* `package` zips each function's source, uploads it to the SAM bucket under a prefix of its
own, and writes a template that points at those zips; `deploy` creates the stack from it.

*Why these parameters:* the stack **mirrors the live code path** (`PatientV2SecondPass=on`,
`PromptCaching=on`, same `ModelId`) so the baseline in Step 5 is a real v1 generation; the canary
schedule is **off**; `LedgerRetentionDays` stays at the template default of **1**, so the ledger
bucket can be emptied tomorrow. `AlertEmail` is empty, so the stack's alarms have no subscriber.

```bash
cd infra
aws cloudformation package --template-file template.yaml --s3-bucket aws-sam-cli-managed-default-samclisourcebucket-xufyv0yd8kla --s3-prefix $STACK --output-template-file $EPH/packaged.yaml
aws cloudformation deploy --template-file $EPH/packaged.yaml --stack-name $STACK --capabilities CAPABILITY_NAMED_IAM --region eu-west-2 --tags purpose=ephemeral-w2 --parameter-overrides Environment=demo ModelId=anthropic.claude-sonnet-4-6 PatientV2SecondPass=on PromptCaching=on CanaryEnabled=off
cd ..
aws cloudformation describe-stacks --stack-name $STACK --query "Stacks[0].[StackStatus,StackId]" --output text
```

Expect `Successfully created/updated stack` after ~5–8 minutes, then `CREATE_COMPLETE` and the
stack ID. **Write the stack ID into `$EPH/env.sh`** (Step 3 does it) — after deletion, only the ID
can still read the stack's events.

**If the create fails:** CloudFormation rolls back, but it **cannot** delete a table that has
deletion protection on, so the stack may end `ROLLBACK_FAILED`. Read the first failure reason:

```bash
aws cloudformation describe-stack-events --stack-name $STACK --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" --output table
```

Then run Step 8 (protection off on whichever tables exist → `delete-stack`) and Step 9.

## Step 3 — save the outputs

```bash
aws cloudformation describe-stacks --stack-name $STACK --query "Stacks[0].Outputs" --output json > $EPH/outputs.json
python3 - "$EPH" <<'PY'
import json, sys
eph = sys.argv[1]
o = {x["OutputKey"]: x["OutputValue"] for x in json.load(open(f"{eph}/outputs.json"))}
keys = ["AuditKeyArn","LedgerBucketName","UserPoolId","UserPoolClientId","HttpApiEndpoint","AuditTableName","AuditTableArn","DispatcherFunctionName"]
with open(f"{eph}/env.sh","a") as f:
    for k in keys:
        f.write(f"export {k}='{o[k]}'\n")
for k in keys: print(k, "=", o[k])
PY
echo "export STACK_ID='$(aws cloudformation describe-stacks --stack-name $STACK --query 'Stacks[0].StackId' --output text)'" >> $EPH/env.sh
source $EPH/env.sh
echo "export DISPATCH_ROLE=$STACK-dispatcher-fn-role" >> $EPH/env.sh
```

Expect eight lines, every name starting `discharge-eph-20260928` (the key ARN excepted). **Check that
`AuditKeyArn` is not the live key** — it should be a key created a few minutes ago:

```bash
aws kms describe-key --key-id $AuditKeyArn --query "KeyMetadata.[CreationDate,Description]" --output text
```

Expect today's date and `CMK for the discharge-summary audit log (demo)`.

## Step 4 — a Cognito test user, and a token

*What:* an admin-created user with a random password held in a file only you can read. The
password never appears on a command line or in shell history.

```bash
source $EPH/env.sh
python3 -c "import secrets;print(secrets.token_urlsafe(18)+'Aa1!')" > $EPH/pw && chmod 600 $EPH/pw
aws cognito-idp admin-create-user --user-pool-id $UserPoolId --username eph-test@synthetic.invalid --user-attributes Name=email,Value=eph-test@synthetic.invalid Name=email_verified,Value=true --message-action SUPPRESS --query "User.UserStatus" --output text
python3 - "$EPH" "$UserPoolId" "$UserPoolClientId" <<'PY'
import json, os, sys
eph, pool, client = sys.argv[1:4]
pw = open(f"{eph}/pw").read().strip()
def w(name, obj):
    p = f"{eph}/{name}"
    with open(p, "w") as f: json.dump(obj, f)
    os.chmod(p, 0o600)
w("setpw.json", {"UserPoolId": pool, "Username": "eph-test@synthetic.invalid", "Password": pw, "Permanent": True})
w("auth.json", {"AuthFlow": "USER_PASSWORD_AUTH", "ClientId": client,
                "AuthParameters": {"USERNAME": "eph-test@synthetic.invalid", "PASSWORD": pw}})
PY
aws cognito-idp admin-set-user-password --cli-input-json file://$EPH/setpw.json
export TOKEN=$(aws cognito-idp initiate-auth --cli-input-json file://$EPH/auth.json --query "AuthenticationResult.IdToken" --output text)
echo ${#TOKEN}
```

Expect `FORCE_CHANGE_PASSWORD`, then no output from `admin-set-user-password`, then a token length
of roughly 1,000+. The token lasts **one hour**; re-run the last two lines to refresh it.

## Step 5 — baseline generation (the policy as the template wrote it)

*What:* one real generation through the ephemeral API: `POST /generate` → 202 → poll
`GET /generations/{id}` until `complete` or `failed`. The script prints status only, never the
drafts.

*Why:* before changing anything, prove the stack works as deployed. Without a working baseline, a
failure in Step 6 would mean nothing.

Save the script once:

```bash
cat > $EPH/gen.py <<'PY'
import json, os, sys, time, uuid, urllib.request, urllib.error
EP, TOKEN = os.environ["HttpApiEndpoint"], os.environ["TOKEN"]
NOTES = ("SYNTHETIC. 70M admitted with community-acquired pneumonia. CURB-65 1. "
         "Treated with amoxicillin 500 mg TDS, 5 days total. Afebrile 48h, eating and mobile. "
         "DH: ramipril 5 mg OD, atorvastatin 20 mg ON - both continued. "
         "Discharged home. GP to review in 1 week.")
def call(method, path, body=None):
    req = urllib.request.Request(EP + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {TOKEN}", "content-type": "application/json",
                 "idempotency-key": str(uuid.uuid4())})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
t0 = time.time()
code, body = call("POST", "/generate", {"notes": NOTES})
print("POST", code, {k: body.get(k) for k in ("job_id", "status", "error", "message")})
if code != 202: sys.exit(1)
job = body["job_id"]
while time.time() - t0 < 300:
    time.sleep(10)
    code, s = call("GET", f"/generations/{job}")
    if s.get("status") in ("complete", "failed", "expired"):
        print("FINAL", code, s.get("status"), s.get("error_code"), f"{time.time()-t0:.0f}s", job)
        sys.exit(0)
print("TIMEOUT after 300 s, still", s.get("status"), job)
PY
source $EPH/env.sh && python3 $EPH/gen.py
```

Expect `POST 202 {'job_id': '01…', 'status': 'pending', …}` then, after ~50–70 s,
`FINAL 200 complete None <n>s <job_id>`. Keep the job ID.

## Step 6 — the transaction test

**The question.** The dispatcher writes its two rows (`GEN#` pending + `IDEM#` receipt) in one
`TransactWriteItems`. IAM authorises each `Put` inside it as `dynamodb:PutItem`. AWS does not
document whether the `dynamodb:Attributes` condition key is **populated** for those puts. W1 left
the dispatcher's `PutItem` unconditioned for that reason: guessing wrong would deny every live
generation.

**Why one test is not enough.** `ForAllValues:StringEquals` is **true when the key is absent** —
there are no values to fail the test. So a full whitelist that *passes* cannot tell "evaluated and
allowed" from "never evaluated". Only a whitelist that **should fail** can:

| Run | Policy on the ephemeral dispatcher role | If the key IS evaluated | If it is NOT |
|---|---|---|---|
| 6a | `PutItem` conditioned on the **full** list of attributes the dispatcher writes | `complete` | `complete` |
| 6b | Same list **minus `inference_profile`** (written on every `GEN#` row) | **`POST 500 dispatch_failed`**, `AccessDeniedException` in the log | `complete` |

6a proves the list is complete; 6b answers the question.

*Why it is safe to edit IAM by hand here, when W1 forbids it on live:* this role belongs to a stack
that is deleted today, and nothing redeploys it. `put-role-policy` **replaces** the inline policy
of the same name.

**6a — full whitelist.** The 17 names are the union of `_pending_job_item` and `_idem_receipt_item`
in `src/dispatcher/app.py`:

```bash
source $EPH/env.sh
aws iam get-role-policy --role-name $DISPATCH_ROLE --policy-name audit-table-dispatch --output json > $EPH/dispatch-policy-before.json
cat > $EPH/mkpol.py <<'PY'
import json, sys
table, drop, out = sys.argv[1], sys.argv[2], sys.argv[3]
attrs = ["PK","SK","generation_id","user_sub","idempotency_key","status","draft","reviewed_at",
         "started_at","input_sha256","output_type","request_region","inference_profile",
         "schema_version","kind","created_at","ttl"]
if drop != "-": attrs.remove(drop)
pol = {"Version": "2012-10-17", "Statement": [
  {"Sid": "ReplayLookup", "Effect": "Allow", "Action": ["dynamodb:GetItem"], "Resource": table},
  {"Sid": "CreateWhitelisted", "Effect": "Allow", "Action": ["dynamodb:PutItem"], "Resource": table,
   "Condition": {"ForAllValues:StringEquals": {"dynamodb:Attributes": attrs}}},
  {"Sid": "FailedTransitionUpdateWhitelisted", "Effect": "Allow", "Action": ["dynamodb:UpdateItem"],
   "Resource": table,
   "Condition": {"ForAllValues:StringEquals": {"dynamodb:Attributes":
                   ["PK","SK","status","error_code","error_message","failed_at"]},
                 "StringEqualsIfExists": {"dynamodb:ReturnValues": "NONE"}}}]}
json.dump(pol, open(out, "w"), indent=1)
print(len(attrs), "attributes; dropped:", drop)
PY
python3 $EPH/mkpol.py $AuditTableArn - $EPH/pol-full.json
aws iam put-role-policy --role-name $DISPATCH_ROLE --policy-name audit-table-dispatch --policy-document file://$EPH/pol-full.json
```

Then confirm IAM holds the new policy and evaluates it as intended, before trusting a live result
(IAM changes take seconds to spread):

```bash
cat > $EPH/sim.py <<'PY'
import json, sys
role, table, attrs = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({"PolicySourceArn": role, "ActionNames": ["dynamodb:PutItem"], "ResourceArns": [table],
  "ContextEntries": [{"ContextKeyName": "dynamodb:Attributes", "ContextKeyValues": attrs.split(","),
                      "ContextKeyType": "stringList"}]}))
PY
GENROW=PK,SK,generation_id,user_sub,idempotency_key,status,draft,reviewed_at,started_at,input_sha256,output_type,request_region,inference_profile,schema_version
ROLE_ARN=arn:aws:iam::061051247394:role/$DISPATCH_ROLE
sleep 30
python3 $EPH/sim.py $ROLE_ARN $AuditTableArn $GENROW > $EPH/sim-in.json
aws iam simulate-principal-policy --cli-input-json file://$EPH/sim-in.json --query "EvaluationResults[0].EvalDecision" --output text
python3 $EPH/gen.py
```

Expect `17 attributes; dropped: -`, then `allowed`, then `POST 202` and `FINAL 200 complete`.
**If 6a fails,** the list is missing a name the dispatcher writes: read the log (below), fix the list,
re-run 6a. Do not go to 6b until 6a passes.

**6b — the same list minus one.**

```bash
python3 $EPH/mkpol.py $AuditTableArn inference_profile $EPH/pol-minus1.json
aws iam put-role-policy --role-name $DISPATCH_ROLE --policy-name audit-table-dispatch --policy-document file://$EPH/pol-minus1.json
sleep 60
aws iam simulate-principal-policy --cli-input-json file://$EPH/sim-in.json --query "EvaluationResults[0].EvalDecision" --output text
python3 $EPH/gen.py
aws logs filter-log-events --log-group-name /aws/lambda/$STACK-dispatcher --start-time $(( ($(date +%s) - 300) * 1000 )) --filter-pattern dispatch_transact_failed --query "events[].message" --output text
```

Expect `16 attributes; dropped: inference_profile`, then **`implicitDeny`** from the simulator (the
policy is live). Then one of two outcomes:
- **`POST 500 … dispatch_failed`** and a log line containing `AccessDeniedException` → **the key is
  evaluated inside transactions.** Conditioning the live dispatcher's `PutItem` at W11 is possible,
  with the 6a list.
- **`POST 202 … complete`** → **the key is not evaluated inside transactions** (or arrives empty,
  which `ForAllValues` treats as a pass). A `PutItem` whitelist would protect nothing on the
  dispatcher's path. **Repeat 6b once after 2 minutes** to rule out IAM propagation before recording
  this answer.

The role is left as it is; it is deleted with the stack in Step 8.

## Step 7 — DPIA Annex C.2: what the grammar cache holds

**Limit, stated first:** the branch has no structured-output code yet, so nothing on this stack
compiles a grammar. The cache cannot be observed here; the check is against AWS's documentation.

- AWS, [structured output](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html),
  read 27 Sep 2026: *"Successfully compiled grammars are cached for 24 hours from first access.
  Cached grammars are encrypted with AWS-managed keys."* and *"Identical schemas from the same
  account use cached grammars."* — the cached object is the grammar compiled **from the schema**;
  the page says nothing of caching prompts or completions.
- The cache is encrypted with **AWS-managed keys, not this project's CMK** — acceptable only because
  the schema carries no patient data. **That is the control:** schemas hold field names and fixed
  enums only — never note content, never examples drawn from notes.
- AWS, [abuse detection](https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html),
  read 27 Sep 2026: the retention exceptions name OpenAI GPT models and **Anthropic Claude Fable 5
  and 5.1**. **Claude Sonnet 4.6 is not named.**

Re-check both at W11, on a stack that runs the step prompts.

## Step 8 — delete, day 1

*Why this order:* `delete-stack` fails on a table with deletion protection on, so protection comes
off first. Today's template has **two** tables (`TraceTable` arrives with the W2 build — from then
on, three).

```bash
source $EPH/env.sh
for t in audit results; do aws dynamodb update-table --table-name $STACK-$t --no-deletion-protection-enabled --query "TableDescription.DeletionProtectionEnabled" --output text; done
aws cloudformation delete-stack --stack-name $STACK
aws cloudformation wait stack-delete-complete --stack-name $STACK && echo DELETED
aws cloudformation describe-stack-events --stack-name $STACK_ID --query "StackEvents[?ResourceStatus=='DELETE_SKIPPED'].[LogicalResourceId,PhysicalResourceId]" --output table
```

Expect `False` twice, then `DELETED`, then exactly **two** `DELETE_SKIPPED` rows — `AuditKey` and
`LedgerBucket`, both `Retain` by design. Everything else is gone, including the Cognito pool and the
test user.

**Left running tonight, and billing:** the stack's KMS key (~$1/month, charged by the hour) and the
ledger bucket (a few KB, locked for 1 day). Nothing else.

## Step 9 — delete, day 2 (at least 24 h after the last generation)

**9a. Empty and remove the ledger bucket.** Object Lock is in **Governance** mode with a 1-day
default, so once the day has passed, plain deletes succeed — no bypass permission needed. A
versioned bucket must have every **version** and **delete marker** removed, not only the current
objects.

```bash
source ~/eph-20260928/env.sh
aws s3api list-object-versions --bucket $LedgerBucketName --output json > $EPH/versions.json
python3 - "$EPH" <<'PY'
import json, sys
eph = sys.argv[1]
v = json.load(open(f"{eph}/versions.json"))
objs = [{"Key": o["Key"], "VersionId": o["VersionId"]} for o in v.get("Versions", []) + v.get("DeleteMarkers", [])]
json.dump({"Objects": objs, "Quiet": True}, open(f"{eph}/delete.json", "w"))
print(len(objs), "versions to delete")
PY
aws s3api delete-objects --bucket $LedgerBucketName --delete file://$EPH/delete.json
aws s3api delete-bucket --bucket $LedgerBucketName && echo BUCKET_DELETED
```

Expect a small count (a few objects per generation), no `Errors` block, then `BUCKET_DELETED`.
`AccessDenied … WORM protected` means the retention has not expired yet: wait and repeat.

**9b. Schedule the stack's own key for deletion — after a guard.** The guard compares it with the
live key, which must **never** be scheduled:

```bash
LIVE_KEY=$(aws cloudformation describe-stacks --stack-name discharge-audit --query "Stacks[0].Outputs[?OutputKey=='AuditKeyArn'].OutputValue" --output text)
echo "live: $LIVE_KEY"; echo "eph:  $AuditKeyArn"
[ -n "$LIVE_KEY" ] && [ "$LIVE_KEY" != "$AuditKeyArn" ] && aws kms schedule-key-deletion --key-id $AuditKeyArn --pending-window-in-days 7 --query "[KeyState,DeletionDate]" --output text
```

Expect two **different** ARNs, then `PendingDeletion` and a date seven days out. If the two ARNs
match, nothing runs — stop and ask.

**9c. Optional tidy:** the packaged zips, `aws s3 rm --recursive
s3://aws-sam-cli-managed-default-samclisourcebucket-xufyv0yd8kla/$STACK/`. Then `rm -r ~/eph-20260928`
(it holds the test password and the policy files).

**After 9b nothing is left running or billing** except the key's seven-day pending-deletion window,
during which it can still be cancelled with `aws kms cancel-key-deletion`.

## Step 10 — record

In `docs/ADR-phase1.md` ADR-009:
- *Ephemeral stack log*: one row — stack, creating commit, created, deleted (day 1), ledger bucket
  removed (day 2), own key scheduled (date), purpose.
- *Owed*: the `TransactWriteItems` row (the 6a/6b answer) and the Annex C.2 row.
