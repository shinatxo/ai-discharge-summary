# W1 change set — deploy runbook (24 Sep 2026)

What this deploys, and why: `docs/ADR-phase1.md` ADR-009, *The W1 change set*. This file is the
**how**. It is one hand deploy (D1) and **two pushes to `main`**, each with a smoke test and a
rollback.

**Timing:** run it on a weekday in daytime. The canary fires at **02:00 nightly and 03:00 on
Mondays (UK time)**; keep clear of both.

**The one idea to hold:** on this repository **a push to `main` is a deploy**. CI runs
`aws cloudformation deploy` on every push. So the commit plan and the deploy plan are one plan.
Commit and push **one step at a time** — never several steps' commits in a single `git push`.

```bash
cd ~/Documents/Claude/Projects/"Cloud Projects 1"/ai-discharge-summary
setopt interactivecomments
export AWS_REGION=eu-west-2 AWS_PAGER=""
mkdir -p ~/w1-rollback
```

`setopt interactivecomments` makes zsh treat `#` as a comment. Without it, a `# note` at the end of
a line is passed to the command as arguments, and `-> x` becomes a redirection into a file named
`x`. The commands below keep comments on their own lines anyway.

## Checklist, in order (24 Sep 2026)

Tick each row as it finishes. Record the result where the *Record* column says.

| # | Task | Runbook | Time | Record | Done |
|---|---|---|---|---|---|
| 1 | Move `w1-live-state.txt` out of the repo; save rollback files; CloudTrail check; tests + lint; simulator; GitHub label | 0a–0c, 0e–0g | 45 min | — || ✅ 25 Sep — 68 passed, lint OK, simulator as expected (after fixing the CLI input), label created |
| 2 | Review the changes: ADR-008/009 v1.1, notice v1.4, DPIA v2.5, WS4 v1.4, incident log, template, workflow | 0d | 2–2.5 h | — || ✅ 25 Sep — approved by the author (IAM whitelist, OIDC trust, CI pins, status fix, notice v1.4, incident log, A1–A7) |
| 3 | Record the CloudTrail answer | 0c | 5 min | ADR-009 *Live-state reconciliation*, "Stack as deployed" row; WS4 §13.1 precondition 3 | ✅ 24 Sep — CI |
| 4 | D1 — OIDC trust | D1 | 15 min | — || ✅ 25 Sep 13:15 — `StringEquals` on `…:ref:refs/heads/main`, no `StringLike` |
| 5 | Push 1 + smoke tests; record the no-op answer | Push 1 | 30 min | ADR-009 (d), the *Unverified* paragraph | ✅ 25 Sep — `9bcd798`, run 36134417966 green (68 passed; OIDC assumed on `main`); all 5 `CodeSha256` changed; SPA generation OK; README section + issue template with label live |
| 6 | Push 2 + smoke tests (daytime, clear of the canary windows) | Push 2 | 45–60 min | — | |
| 7 | Create `feat/agentic-pipeline` | After 1 | 5 min | — | |
| 8 | Re-check live state; tick the §13.1 preconditions; decide HAZ-11's score; sign v1.4 | After 3 | 30–45 min | WS4 configuration lines (dates); §13.1 | |
| 9 | *(Optional, next day)* live proof of the expiry fix | After 2 | 15 min | — | |

---

## Step 0 — before anything changes (read-only, ~30 min)

**0a. Move the live-state dump out of the repository.** It holds the account ID, your IAM user,
the alert email and the role policies. It must never be committed.

```bash
mv w1-live-state.txt ~/w1-rollback/
```

**0b. Save what you would roll back to**, from the live account and from `origin/main`, before
anything changes:

```bash
aws iam get-role --role-name GitHubActionsDischargeDeploy --query "Role.AssumeRolePolicyDocument" > ~/w1-rollback/trust-before.json
git show origin/main:infra/cicd-oidc-role.yaml > ~/w1-rollback/oidc-old.yaml
for r in discharge-audit-generate-fn-role discharge-audit-dispatcher-fn-role; do for p in $(aws iam list-role-policies --role-name "$r" --query "PolicyNames[]" --output text); do aws iam get-role-policy --role-name "$r" --policy-name "$p" --output json; done; done > ~/w1-rollback/role-policies-before.json
for f in generate dispatcher status ledger canary; do aws lambda get-function --function-name "discharge-audit-$f" --query "Configuration.[FunctionName,CodeSha256,LastModified]" --output text; done > ~/w1-rollback/code-before.txt
cat ~/w1-rollback/code-before.txt
```

**0c. Explain the 20 Sep deploy.** Safety case §13.1 precondition 3. Stack events cannot tell a
CI deploy from a hand deploy; CloudTrail can, because it records **who** created the change set.
CloudTrail's event history keeps management events for 90 days with no trail configured.

```bash
aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=CreateChangeSet --start-time 2026-09-20T00:00:00Z --end-time 2026-09-21T00:00:00Z --query "Events[].{t:EventTime,who:Username,raw:CloudTrailEvent}" --output json > ~/w1-rollback/sep20-changesets.json
python3 -c "import json;[print(e['t'],e['who'],json.loads(e['raw'])['userIdentity'].get('arn')) for e in json.load(open('$HOME/w1-rollback/sep20-changesets.json'))]"
```

How to read the result:
- **An assumed-role ARN containing `GitHubActionsDischargeDeploy`:** CI made the change —
  consistent with the push of 20 Sep.
- **`user/Shina`:** you deployed by hand. Look in that event's `requestParameters.parameters` to
  see which values were set. `PromptCaching=on` is the likely one.

Write one line in `docs/ADR-phase1.md` ADR-009, *Live-state reconciliation*, on the "Stack as
deployed 16 Sep" row.

**0d. Review every change** before committing any of it:

```bash
git status --short
git diff --stat
git diff infra/ .github/ src/ tests/
```

**0e. Run the gate locally** — the same checks CI runs. Expect `68 passed` and no lint errors:

```bash
source .venv/bin/activate
python -m pytest tests/ -q
cfn-lint --non-zero-exit-code error infra/template.yaml infra/web-template.yaml infra/cicd-oidc-role.yaml
```

**0f. Test the new IAM policies before they exist.**

*What:* `simulate-custom-policy` evaluates a policy you hand it against a request you describe,
condition keys included, without deploying anything.

*Why:* the attribute whitelist is the one change here that could break the live path. If an
attribute the code really writes is missing from it, every generation fails at the audit flip.

*(SAA: IAM policy evaluation — an explicit Allow is required, and a failed condition means an
implicit deny.)*

```bash
TABLE=arn:aws:dynamodb:eu-west-2:061051247394:table/discharge-audit-audit
cat > /tmp/worker-audit.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":"dynamodb:UpdateItem","Resource":"$TABLE",
  "Condition":{"ForAllValues:StringEquals":{"dynamodb:Attributes":["PK","SK","status","completed_at","model_version","output_sha256","parse_ok","input_tokens","output_tokens","patient_version","patient_model_version","patient_parse_ok","patient_output_tokens","error_code","error_message","failed_at","pipeline_version","failed_step","model_calls","safety_net_gate"]},
               "StringEqualsIfExists":{"dynamodb:ReturnValues":"NONE"}}}]}
JSON
# The request is built as JSON by Python and passed whole with --cli-input-json.
# (25 Sep 2026: `--policy-input-list file://...` failed with "Policy input list
# item 1 has invalid content" - the CLI does not pass a JSON file through as one
# policy string. Building the request removes the CLI's guesswork.)
sim () {
python3 - "$1" > /tmp/sim-in.json <<'PY'
import json, sys
pol = open('/tmp/worker-audit.json').read()
print(json.dumps({
  "PolicyInputList": [pol],
  "ActionNames": ["dynamodb:UpdateItem"],
  "ResourceArns": ["arn:aws:dynamodb:eu-west-2:061051247394:table/discharge-audit-audit"],
  "ContextEntries": [{"ContextKeyName": "dynamodb:Attributes",
                      "ContextKeyValues": sys.argv[1].split(","),
                      "ContextKeyType": "stringList"}]}))
PY
aws iam simulate-custom-policy --cli-input-json file:///tmp/sim-in.json --query "EvaluationResults[0].EvalDecision" --output text
}
```

Then run these four checks. Each comment line gives the expected answer:

```bash
# worker marks a job complete: expect allowed
sim PK,SK,status,completed_at,model_version,output_sha256,parse_ok,input_tokens,output_tokens,patient_version,patient_model_version,patient_parse_ok,patient_output_tokens
# worker or dispatcher marks a job failed: expect allowed
sim PK,SK,status,error_code,error_message,failed_at
# attempt to rewrite the input hash: expect implicitDeny
sim PK,SK,input_sha256
# attempt to re-attribute a row: expect implicitDeny
sim PK,SK,user_sub
```

**Any mismatch: stop, and do not push Push 2.**

*The limit of this test:* the simulator shows how IAM *evaluates* a request. It cannot show which
attribute names DynamoDB actually puts into the request context, and the list above is a hand copy
of the template. Push 2's live smoke tests, and the `simulate-principal-policy` check against the
deployed roles, close both gaps.

**0g. Create the GitHub label** the issue template uses. Without it, the template's `labels:`
line does nothing:

```bash
gh label create safety-incident --color B60205 --description "Possible patient-safety impact — see docs/WS4-SAFETY-INCIDENT-LOG.md"
```

---

## D1 — narrow the OIDC trust to `main` (hand deploy, `discharge-cicd` stack, ~15 min)

*What:* the GitHub deploy role will accept a token only from a workflow running on
`refs/heads/main`.

*Why:* it used to accept any branch or pull request (ADR-009 F10).

*(SAA: a role's trust policy is a resource-based policy — it says WHO may assume the role.
`StringEquals` on the OIDC token's `sub` claim is how federation is scoped to a branch.)*

```bash
aws cloudformation deploy --template-file infra/cicd-oidc-role.yaml --stack-name discharge-cicd --capabilities CAPABILITY_NAMED_IAM --region eu-west-2
aws iam get-role --role-name GitHubActionsDischargeDeploy --query "Role.AssumeRolePolicyDocument.Statement[0].Condition" --output json
```

**Smoke test:** the condition shows `StringEquals` with
`token.actions.githubusercontent.com:sub = repo:shinatxo/ai-discharge-summary:ref:refs/heads/main`,
and no `StringLike`. The positive test — that `main` can still deploy — is Push 1's CI run.

**Rollback**, from the file saved in 0b (not from `HEAD`, which may already be the new version):

```bash
aws cloudformation deploy --template-file ~/w1-rollback/oidc-old.yaml --stack-name discharge-cicd --capabilities CAPABILITY_NAMED_IAM --region eu-west-2
```

---

## Push 1 — documents, the published contact, and the status-endpoint fix (~30 min)

**Why these travel together:** privacy notice v1.4 and DPIA v2.5 say the status endpoint enforces
the 24-hour window. Shipping the fix in the same push means the published documents are never
ahead of the system.

**Why it is safe to go first:** the only code changes are the status function's `ttl` check
(three unit tests) and a comment in the dispatcher. No infrastructure changes.

```bash
git add docs/ADR-phase1.md
git commit -m "docs(adr): ADR-008 serverless substitution; ADR-009 agentic pipeline (accepted); ADR-002/005 corrections"
git add PRIVACY-NOTICE.md docs/WS3-DPIA.md docs/WS4-HAZARD-LOG.md docs/WS4-SAFETY-CASE.md docs/WS4-SAFETY-INCIDENT-LOG.md docs/BEDROCK_QUOTA.md docs/COST.md docs/CICD.md README.md .github/ISSUE_TEMPLATE/safety-incident.md
git commit -m "docs(governance): privacy notice v1.4, DPIA v2.5, WS4 v1.4 drafts, safety incident log + published contact"
git add src/status/app.py tests/test_status.py src/dispatcher/app.py
git commit -m "fix(status): treat results past their ttl as expired (HAZ-19); TTL wording"
git log --oneline -4
git push origin main
```

**Smoke tests:**

1. **CI is green on both jobs.** The `test` job reports 68 passed. The `deploy` job assuming the
   role **is the positive test of D1**. If it cannot assume the role, run D1's rollback.

2. **The no-op-merge question** (ADR-009 (d)). The generate, ledger and canary functions' code
   did not change in this push. Did they redeploy anyway?

   ```bash
   for f in generate dispatcher status ledger canary; do aws lambda get-function --function-name "discharge-audit-$f" --query "Configuration.[FunctionName,CodeSha256,LastModified]" --output text; done > ~/w1-rollback/code-after-push1.txt
   diff ~/w1-rollback/code-before.txt ~/w1-rollback/code-after-push1.txt
   ```

   Status and dispatcher **should** differ. How to read generate, ledger and canary:
   - **Unchanged:** a push that touches no code deploys an empty change set.
   - **`LastModified` changed but `CodeSha256` the same:** the same zip was redeployed.
   - **`CodeSha256` changed:** the package differs byte-for-byte — file timestamps inside the zip.

   Record which in ADR-009 (d), in the paragraph marked *Unverified*.

   **Result, 25 Sep 2026:** all five `CodeSha256` values changed — a push redeploys every
   function with a new package. Recorded in ADR-009 (d).

3. **A generation still works.** Generate one draft through the SPA with synthetic notes.

4. **The published contact works.** `README.md` on GitHub shows *Reporting a safety concern*, and
   *New issue* offers the *Safety incident* template with its label.

**Rollback:** `git revert --no-edit HEAD` (the fix commit only), then `git push origin main`.
The document commits need no rollback.

---

## Push 2 — infrastructure (~45 min including checks)

This push contains:
- `AuditKey` Retain;
- the `UpdateItem` whitelists on both roles and the worker's legacy `PutItem` whitelist;
- the corrected IAM comments;
- CI pins for `ModelId`, `PromptCaching` and `LedgerRetentionDays=183`;
- this runbook and the OIDC template, which record what D1 deployed.

**Push only if Step 0f passed**, and at a time you can watch for 30 minutes.

```bash
git add infra/template.yaml .github/workflows/ci-cd.yml infra/cicd-oidc-role.yaml infra/W1_CHANGE_SET_RUNBOOK.md
git commit -m "infra(w1): OIDC trust to main; AuditKey Retain; UpdateItem attribute whitelists; ledger 183d; pin live parameters in CI"
git push origin main
```

**Smoke tests, in order:**

1. **Nothing was replaced.**

   ```bash
   aws cloudformation describe-stack-events --stack-name discharge-audit --max-items 40 --query "StackEvents[].[Timestamp,LogicalResourceId,ResourceType,ResourceStatus,ResourceStatusReason]" --output table
   ```

   Expect `UPDATE_COMPLETE`. `AuditKey`, `AuditTable`, `ResultsTable` and `LedgerBucket` must show
   no `DELETE_*` and no `CREATE_*` events. A replacement is a stop-and-roll-back event.

2. **The live configuration matches CI.**

   ```bash
   aws cloudformation describe-stacks --stack-name discharge-audit --query "Stacks[0].Parameters[?contains(['ModelId','PromptCaching','LedgerRetentionDays','PatientV2SecondPass'],ParameterKey)]" --output table
   aws s3api get-object-lock-configuration --bucket discharge-audit-ledger-061051247394-eu-west-2
   aws cloudformation get-template --stack-name discharge-audit --query TemplateBody --output text | grep -A3 "AuditKey:"
   ```

   Expect:
   - `LedgerRetentionDays=183`;
   - Object Lock `Days: 183` in `GOVERNANCE` mode;
   - `DeletionPolicy: Retain` under `AuditKey`.

3. **The deployed policies say what the template says.** This simulates against the **live**
   roles, so the policy under test is the real one, not a hand copy:

   ```bash
   W=arn:aws:iam::061051247394:role/discharge-audit-generate-fn-role
   D=arn:aws:iam::061051247394:role/discharge-audit-dispatcher-fn-role
   TABLE=arn:aws:dynamodb:eu-west-2:061051247394:table/discharge-audit-audit
   # Same JSON-built request as Step 0f, for the same reason.
live () {
python3 - "$1" "$2" "$3" > /tmp/live-in.json <<'PY'
import json, sys
print(json.dumps({
  "PolicySourceArn": sys.argv[1],
  "ActionNames": [sys.argv[2]],
  "ResourceArns": ["arn:aws:dynamodb:eu-west-2:061051247394:table/discharge-audit-audit"],
  "ContextEntries": [{"ContextKeyName": "dynamodb:Attributes",
                      "ContextKeyValues": sys.argv[3].split(","),
                      "ContextKeyType": "stringList"}]}))
PY
aws iam simulate-principal-policy --cli-input-json file:///tmp/live-in.json --query "EvaluationResults[0].EvalDecision" --output text
}
   ```

   Each comment line gives the expected answer:

   ```bash
   # worker marks a job complete: allowed
   live "$W" dynamodb:UpdateItem PK,SK,status,completed_at,model_version,output_sha256,parse_ok,input_tokens,output_tokens,patient_version,patient_model_version,patient_parse_ok,patient_output_tokens
   # worker legacy PutItem: allowed
   live "$W" dynamodb:PutItem PK,SK,generation_id,user_sub,timestamp,input_sha256,output_sha256,model_version,output_type,draft,reviewed_at,request_region,inference_profile,parse_ok,patient_version,patient_model_version,patient_parse_ok,input_tokens,output_tokens,schema_version,status,started_at,completed_at
   # dispatcher marks a job failed: allowed
   live "$D" dynamodb:UpdateItem PK,SK,status,error_code,error_message,failed_at
   # dispatcher rewrites the input hash: implicitDeny
   live "$D" dynamodb:UpdateItem PK,SK,input_sha256
   ```

4. **The live path still writes** — the test that matters most. Generate one draft through the SPA
   with synthetic notes and note its job ID. Then read the audit row back. Replace `YOUR_SUB` and
   `JOB_ID`, keeping the quotes:

   ```bash
   aws dynamodb get-item --table-name discharge-audit-audit --key '{"PK":{"S":"USER#YOUR_SUB"},"SK":{"S":"GEN#JOB_ID"}}' --projection-expression "#s, parse_ok, completed_at" --expression-attribute-names '{"#s":"status"}'
   ```

   Expect `status = complete`. **If it stays `pending` beyond 5 minutes, the worker's
   `UpdateItem` was denied.** Search `/aws/lambda/discharge-audit-generate` for
   `AccessDeniedException` and roll back.

5. **The canary path still works.** This runs one scenario, synchronously. The read timeout is
   raised so the CLI does not time out at 60 s and silently re-invoke the function:

   ```bash
   aws lambda invoke --function-name discharge-audit-canary --cli-binary-format raw-in-base64-out --cli-read-timeout 900 --cli-connect-timeout 10 --payload '{"scenarios":"S14"}' /tmp/canary.json
   cat /tmp/canary.json
   ```

6. **The legacy direct-invoke path**, which exercises the whitelisted `PutItem`. **Know before
   you run it:** it makes two Bedrock calls, and it writes **one permanent audit row** under the
   synthetic subject `w1-smoke-test-synthetic`, which cannot be deleted and is copied to the
   183-day ledger. That is intended — it is evidence that the smoke test ran.

   ```bash
   aws lambda invoke --function-name discharge-audit-generate --cli-binary-format raw-in-base64-out --cli-read-timeout 300 --cli-connect-timeout 10 --payload '{"user_sub":"w1-smoke-test-synthetic","notes":"Synthetic. 70M, community-acquired pneumonia, amoxicillin 500 mg TDS 5 days, discharged home."}' /tmp/direct.json
   python3 -c "import json;d=json.load(open('/tmp/direct.json'));print(d.get('ok'),d.get('error'))"
   ```

   Expect `True None`. `audit_write_error` means the `PutItem` list is missing an attribute: roll
   back, and compare the list with `_run_direct_invoke` in `src/generate/app.py`.

**Rollback, fastest first:**
- In GitHub → Actions, open the **previous** green run of *CI / CD* and choose *Re-run all jobs*.
  A re-run uses that run's own commit and workflow file, so it redeploys the old template in about
  5 minutes. Re-runs are only offered for 30 days after a run.
- Or `git revert --no-edit HEAD && git push origin main`.
- **A rollback does not undo the parameter pins.** The old workflow does not mention
  `LedgerRetentionDays`, `ModelId` or `PromptCaching`, so `deploy` keeps their current values —
  183 days stays. That is harmless. Change it deliberately if you ever need to.
- **Never** hand-edit the IAM policies in the console. The next CI deploy silently undoes the
  edit, and in the meantime the live state no longer matches the repository.

---

## After both pushes

1. **Create the build branch.** Pushing it starts no workflow (`on.push` is `main` only), and since
   D1 it could not obtain deploy credentials even if one ran:

   ```bash
   git switch -c feat/agentic-pipeline
   git push -u origin feat/agentic-pipeline
   git switch main
   ```

2. **Optional — the live proof of the `ttl` fix, the next day.**
   - Find a job you ran more than 24 hours earlier whose results row **still exists** (DynamoDB has
     not yet deleted it):

     ```bash
     aws dynamodb get-item --table-name discharge-audit-results --key '{"PK":{"S":"USER#YOUR_SUB"},"SK":{"S":"RES#OLD_JOB_ID"}}' --projection-expression "ttl"
     ```

     A returned `ttl` earlier than `date +%s` means the row is expired but not yet deleted — the
     case the fix is for.
   - Then, with an IdToken copied from the browser's network panel (the `Authorization: Bearer`
     value on a `/generations/` request), call the status endpoint:

     ```bash
     TOKEN='PASTE_ID_TOKEN'
     curl -s -H "Authorization: Bearer $TOKEN" "https://discharge.shinaoguntoye.dev/generations/OLD_JOB_ID"
     ```

     Expect `"status": "expired"`. Before the fix, this exact case returned the drafts. Tokens last
     an hour, and should go into your own terminal only.

3. **Re-run the Step 0 live-state block**, then sign WS4 v1.4 against it once every precondition
   in `docs/WS4-SAFETY-CASE.md` §13.1 is ticked.
