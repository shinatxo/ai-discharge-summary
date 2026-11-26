# WS7 runbook: the `discharge-eval` stack

How the author deploys and operates the WS7 eval stack by hand (ADR-011 D4; slice 3). The design
is in `docs/adr/ADR-011-ws7-scheduled-eval.md`; the job is described in `docs/WS7-ENTRYPOINT.md`.

**Nothing in this runbook has been run.** It was written and tested offline (26 Nov 2026): the
template against `cfn-lint`, and the scripts against stubs. Every AWS fact marked
"unchecked" below is from AWS's documentation or from memory, not from this account. The first
deploy is what checks them, and each section says what to do if AWS disagrees.

## The rules

- **Every step that calls AWS needs the author's yes at the time.** A deploy, a push, a task and a
  switch are each a separate yes. Nothing here is run by CI or by Claude.
- **Every AWS step starts with the identity and region check.** The scripts in `infra/eval/` call
  `ws7_guard` themselves. Each runnable block below starts with it. It stops unless the
  credentials in use belong to the account you typed, and nothing in the shell points at another
  region. The stack is in `eu-west-2` only.
- **Nothing here touches `discharge-audit` or `discharge-web`.** No command names either stack,
  either stack's buckets, or the SAM bucket.
- **No real patient data.** The stack evaluates synthetic runs only.
- **Read each block before you run it.** Run the blocks from the repository's root, in `zsh`.
  Each block is `ws7_guard $ACCOUNT && () { … }`: the guard first, then an anonymous function, so
  that a `STOP` inside it ends the block and not your shell.

Set this once in the shell you use. The account is the one in `.github/workflows/ci-cd.yml`.

```zsh
ACCOUNT=<the 12-digit account>
source infra/eval/guard.sh
```

## What it costs

| Step | Cost |
|---|---|
| Deploy phase 1 | Starts the stack's fixed cost: about £2.70 to £2.90 a month (the key, £0.80; 26 alarms, £1.84 to £2.08). See `docs/COST.md` for which prices are unchecked |
| Push an image | About 2 pence a month for each image kept in ECR (unchecked) |
| Deploy phase 2, a parameter, a switch | Nothing |
| A task for a set-up check | Under one penny (Fargate, 0.5 vCPU and 1 GB, for a minute or two) |
| The Thursday check | Under one penny a week |
| A scheduled run | Model spend up to its reservation: £1.96 (v1) or £6.62 (pipeline), inside the £15 a month ceiling (ADR-011, Costs) |
| Tear-down | The retained key keeps costing £0.80 a month until it is scheduled for deletion |

## 1. Deploy phase 1: everything except the task

Phase 1 has no image, so it has no task definition and no schedule. Nothing can run.

```zsh
WS7_ALERT_EMAIL=<your address> infra/eval/deploy.sh --account $ACCOUNT --target v1
```

- `--target` is `v1` until W11 and `pipeline` after it (ADR-011 D2). If the stack is not up by about
  30 Nov 2026, the v1 target is dropped (D4): use `pipeline`, and nothing runs until W11.
- The script makes a change set, prints its table, and waits for you to type the stack name.
  Read the table first. A first deploy adds 51 resources.
- AWS then emails a subscription confirmation. **Confirm it.** An alarm cannot tell you anything
  until you do.
- The script switches on termination protection.

If it fails:

| What AWS says | What it most likely is |
|---|---|
| The alarm `discharge-eval-RunOk` is refused | Seven one-day periods with a wall clock window is the most CloudWatch allows (ADR-011 D9), and it is untested. The fallback in D9 is one one-week period. Stop and tell Claude: it is a template change |
| A policy is refused for an invalid principal or action | The statement that denies the CI role, or a service principal. Stop: it is a template change |
| The bucket name is taken | Someone else owns `discharge-eval-<account>-eu-west-2`. Stop |

A failed first create leaves the stack in `ROLLBACK_COMPLETE`. Delete it (section 9) before the next
try. The key and the bucket are retained when a create rolls back: remove them by hand, or the next
create fails on the bucket's name.

## 2. Build and push the image

The image is built from a merged commit of `feat/agentic-pipeline`, which the script clones from
GitHub. It never reads this checkout.

```zsh
C=<the 40-character merge commit, as `git log --first-parent origin/feat/agentic-pipeline` shows it>

# A rehearsal: builds and runs every check, makes no AWS call, pushes nothing.
infra/eval/push-image.sh --check-only --commit $C

# The push.
infra/eval/push-image.sh --account $ACCOUNT --commit $C
```

What the script does, in order, and it stops at the first thing that fails:

1. The identity and region check.
2. Clones `feat/agentic-pipeline` from GitHub into a temporary folder, with this machine's git
   configuration left out. Refuses unless `$C` is a commit **on that branch's first-parent chain
   as fetched** (`git rev-list --first-parent`): the branch's head, or a commit the branch itself
   was at before. In practice that is a pull request's merge commit. A commit from inside a merged
   task branch is refused, though it is an ancestor of the branch: it may be a head that review
   found faults in (the author's ruling, 26 Nov 2026).
3. Exports only the image's paths. `evals/runs` and `evals/calibration` are never exported.
4. Checks the export against the commit: the same files, and each file's bytes hash to the blob
   the commit names.
5. Builds for `linux/arm64` and takes the image ID. Every later command uses the ID.
6. Runs part 6's checks 3, 4, 6, 8, 9 and 10 on that ID.
7. Runs **check 5** last, and goes by its status.
8. Tags the image `<commit>-<first 12 of the ID>`, checks the tag still names that ID, and pushes.
9. Reads the digest back from ECR. Unless it is the image ID, it prints `DO NOT DEPLOY IT` and no
   digest.

The last lines of a good push give the digest for phase 2:

```
PUSHED <account>.dkr.ecr.eu-west-2.amazonaws.com/discharge-eval@sha256:…
digest sha256:…   (the ImageDigest for infra/eval/deploy.sh)
```

**Which digest this is.** It is the image ID check 5 ran on, which on this machine is the digest of
the image's manifest list (part 6's checks showed it). The task definition pins that digest and
passes the same value as `WS7_IMAGE_DIGEST`, so the chain from check 5 to the running task is one
string. A rebuild of the same commit gives another digest, because the list carries a build
attestation; that is expected, and the new image is checked again.

- The script logs Docker in to the account's ECR registry (`docker login`). The token lasts 12
  hours and stays in Docker's credential store; `docker logout <account>.dkr.ecr.eu-west-2.amazonaws.com`
  removes it.
- Tags in the repository are immutable. Running the push twice for one commit builds again, gets
  another image ID and so another tag. Pushing the very same image twice may be refused by ECR
  (unchecked); nothing is lost if it is.

If step 9 says `DO NOT DEPLOY IT`: Docker on this machine is giving image IDs that are not registry
digests (the classic image store does). The image in ECR is not pinned by anything and can be
deleted. Stop and tell Claude.

### Check: the lifecycle rule cannot expire the pinned image's parts

The repository's one rule expires **untagged** images 14 days after their push. The pinned list
is tagged, and tags are immutable. Its two children (the arm64 manifest and the attestation) are
untagged. AWS documents that an image a manifest list refers to cannot expire before the list
(*Automate the cleanup of images by using lifecycle policies in Amazon ECR*, read 26 Nov 2026).
That page says "manifest list", and this build makes an OCI image index, so this check shows it on
this repository. It is a preview: it expires nothing.

```zsh
ws7_guard $ACCOUNT && () {
  D=<the pushed digest>
  # A test rule strict enough to catch every untagged image, however new (a count of 1
  # keeps one). The repository's own rule is not changed.
  RULE='{"rules":[{"rulePriority":1,"description":"preview only","selection":{"tagStatus":"untagged","countType":"imageCountMoreThan","countNumber":1},"action":{"type":"expire"}}]}'
  aws ecr start-lifecycle-policy-preview --region eu-west-2 --repository-name discharge-eval \
    --lifecycle-policy-text "$RULE" --query status --output text
  sleep 30
  # The children of the pinned list.
  aws ecr batch-get-image --region eu-west-2 --repository-name discharge-eval \
    --image-ids imageDigest=$D --query 'images[0].imageManifest' --output text \
    | python3 -c 'import json,sys; [print(m["digest"], m.get("platform")) for m in json.load(sys.stdin)["manifests"]]'
  # What the preview would expire.
  aws ecr get-lifecycle-policy-preview --region eu-west-2 --repository-name discharge-eval \
    --lifecycle-policy-text "$RULE" --query '[status, previewResults[].imageDigest]'
}
```

- **Pass:** the preview's status is `COMPLETE`, and its list holds neither child digest and not the
  pinned digest.
- **Fail:** a child digest is listed. Then the rule in the template could expire part of the pinned
  image after 14 days. Stop, and tell Claude: the rule is removed from the template before
  phase 2. Nothing has expired yet.
- With a single image in the repository the preview may list nothing at all. That is a pass only
  with the status `COMPLETE`.

## 3. Deploy phase 2: the task and the schedules, DISABLED

```zsh
infra/eval/deploy.sh --account $ACCOUNT --target v1 --image-digest <the pushed digest>
```

- The change set adds the task definition and the two schedules. **Both schedules are created
  `DISABLED`.** This script never changes their state: at creation it passes `DISABLED`, and on
  every later update it passes the stack's previous value and refuses a change set that would
  change a schedule's `State`.
- The script first checks that the digest is an image in the eval repository.

## 4. Set the two expected-version parameters

The job reads them before any claim and stops if they differ from its own values (ADR-011 D11).
The stack creates both with the value `unset`, which the match check refuses. The author writes
the real values by hand, from the release record. CI does not write them and the task cannot.

**v1: once, now.** From the commit deployed on `main` at this time.

```zsh
ws7_guard $ACCOUNT && () {
  M=$(git rev-parse --verify --quiet 'origin/main^{commit}') && [ -n "$M" ] || { echo "STOP: no origin/main"; return 1; }
  P1=$(git show "$M:src/generate/system_prompt.md" | shasum -a 256 | cut -d' ' -f1)
  P2=$(git show "$M:src/generate/patient_system_prompt.md" | shasum -a 256 | cut -d' ' -f1)
  [ ${#P1} -eq 64 ] && [ ${#P2} -eq 64 ] || { echo "STOP: a prompt hash could not be made"; return 1; }
  V='{"schema": "ws7-expected/1", "target": "v1", "model_id": "anthropic.claude-sonnet-4-6", "prompt_caching": true, "v1_prompt_sha256": {"system_prompt": "'$P1'", "patient_system_prompt": "'$P2'"}, "patient_second_pass": true}'
  echo "main is at $M"; echo "$V"
  aws ssm put-parameter --region eu-west-2 --name /discharge-eval/ws7/expected/v1 \
    --type String --overwrite --value "$V"
}
```

- Fetch first (`git fetch origin main`), and check that `$M` is the commit the last deploy of `main`
  ran from. `ModelId`, `PatientV2SecondPass` and `PromptCaching` are pinned in
  `.github/workflows/ci-cd.yml` at that commit: the value above states them as they are pinned
  today (the model, second pass on, caching on). If the workflow pins anything else, stop.
- The type must stay `String`. A `SecureString` is refused by the job.

**Pipeline: at each release to `main`, starting with W11**, as a step of the release: the same
command with `/discharge-eval/ws7/expected/pipeline` and

```
{"schema": "ws7-expected/1", "target": "pipeline", "pipeline_version": "<from the release commit>", "model_id": "anthropic.claude-sonnet-4-6", "prompt_caching": true}
```

`pipeline_version` is `pipeline.orchestrate.pipeline_version()` computed at the release commit.

**Never deploy a template change to either parameter's `Value`.** CloudFormation would overwrite
what you wrote. `tests/test_eval_template.py` pins the placeholder.

## 5. The set-up checks

Every check in ADR-011's list and in both "What the fakes cannot prove" sections of
`docs/WS7-ENTRYPOINT.md`. Record what each one printed, with the date, in the pull request that
follows the deploy.

### All of them, and what each needs

| # | Check | How | Bedrock call | Frozen baseline | Calendar | Cost | By about 30 Nov |
|---|---|---|---|---|---|---|---|
| A1 | The stack is as designed | Block below | No | No | No | None | Yes |
| A2 | The digest chain: check 5's image ID is the image the task ran | Block below, with C10's stopped task | No | No | No | None | Yes |
| A3 | An alarm email arrives through the encrypted topic | Block below | No | No | No | None | Yes |
| A4 | `RunOk`'s state before any success | Block below | No | No | About an hour | None | Yes |
| A5 | The task role allows D6's list and nothing else | Block below (a simulation) | No | No | No | None | Yes |
| C10 | The bare task: ECS starts it, it exits 2, and the early-signal email arrives | Block below | No | No | No | Under 1p | Yes |
| L | The lifecycle rule and the pinned image's parts | Section 2 | No | No | No | None | Yes |
| B0 | In the task: role credentials, a writable temporary folder as uid 10001, arm64, the commit, the command replaced | A task with an inline script (the next pull request) | No | No | No | Under 1p | Yes |
| B1 | Real S3 through the image's own `Store`: the 412s, the 404, a real ETag, 404 or 403 for a missing key, a `.md` object | The same | No | No | No | Under 1p | Yes |
| B2 | What the role cannot do, each as a real call that changes nothing if it is wrongly allowed | The same | No | No | No | Under 1p | Yes |
| B3 | Two tasks racing for one claim: one holder | Two tasks | No | No | No | Under 1p | Yes |
| B4 | The parameters are plain Strings, readable and not writable | A task | No | No | No | Under 1p | Yes |
| B5 | CloudWatch accepts every metric name, the dimension and the values; each breach alarm and `NoBaseline` sees its datapoint. **`RunOk` is never published by a check** | A task | No | No | No | Under 1p | Yes |
| B6 | The role's own message is delivered and readable | A task | No | No | No | Under 1p | Yes |
| C1 | `check` on a day with no claim sends the "no claim" email | A task, the real subcommand | No | No | No | Under 1p | Yes |
| C2 | The scheduled time's format, and that it is the schedule's time, not the start time | A one-off schedule that runs `check` only and deletes itself | No | No | No | Under 1p | Yes |
| C3 | A duplicate delivery exits 0 and spends nothing | Seed the claim, list to confirm that exact key, then run `scheduled` with the same time | **No Bedrock call only if the seeded claim is found; worst case £1.96 (v1's reservation)** | No | Not a Wednesday | Under 1p, or the worst case. **Needs the author's yes at the time** | Yes |
| C4 | The check reports "started and did not finish" | `check` after C3 | No | No | No | Under 1p | Yes |
| R | A bucket in another region: what real S3 answers, and the job stops | A throwaway empty bucket in another region, made and deleted by the author | No | No | No | None | Yes |
| C5 | **The first success**, and `RunOk` returns to OK at the next London midnight | A real scheduled run | **Yes** | **Yes** (part 5b and slice 4) | The next midnight | £0.76 to £2.28 expected (pipeline), up to £1.32 (v1) | No |
| C6 | A skipped Wednesday: the alarm and the check both alert; the times against 21 and 30 hours | The schedule off for one week, after a success | No | No, but it follows C5 | 8 days or more | None | No |
| C7 | **A late-finishing run** still counts for Wednesday; one that would pass midnight is stopped | **Not provoked** (the author's ruling, 26 Nov 2026). Not proved on AWS; covered by the offline deadline tests and the config's figures | **Yes** | **Yes** | No | A run's cost | No |
| C8 | The clock changes | The weeks of 28 March and 31 October 2027 | No | No | Yes | None | No |

- B0 to B6, C1 to C4 and R are run by scripts the next pull request adds
  (`task/ws7-setup-checks`). They write only under `ws7/setup/`, except C3's one seeded claim, and
  never a ledger object.
- **What cannot be provoked:** a real duplicate delivery by EventBridge Scheduler, a throttle from
  CloudWatch or SNS, the two scheduler alarms firing, and S3's 409 unless B3 happens to show one.
  Each is covered offline only.

### A1. The stack is as designed

```zsh
ws7_guard $ACCOUNT && () {
  # No inbound rule; one outbound rule, TCP 443.
  aws ec2 describe-security-groups --region eu-west-2 --filters Name=group-name,Values=discharge-eval-task-sg \
    --query 'SecurityGroups[0].[length(IpPermissions), IpPermissionsEgress[].[IpProtocol,FromPort,ToPort]]'
  # Both schedules DISABLED, Europe/London, no retry.
  for S in discharge-eval-ws7-run discharge-eval-ws7-check; do
    aws scheduler get-schedule --region eu-west-2 --group-name discharge-eval --name $S \
      --query '[Name,State,ScheduleExpression,ScheduleExpressionTimezone,Target.RetryPolicy.MaximumRetryAttempts]' --output text
  done
  # The heartbeat alarm as D9 has it: 86400, 7, 7, breaching, a wall clock window in Europe/London.
  aws cloudwatch describe-alarms --region eu-west-2 --alarm-names discharge-eval-RunOk \
    --query 'MetricAlarms[0].[Period,EvaluationPeriods,DatapointsToAlarm,TreatMissingData,EvaluationWindow]'
  # 26 alarms, all armed.
  aws cloudwatch describe-alarms --region eu-west-2 --alarm-name-prefix discharge-eval- \
    --query '[length(MetricAlarms), length(MetricAlarms[?ActionsEnabled==`true`])]' --output text
  # The task definition: the image by digest, and the names of its variables (no WS7_GIT_COMMIT).
  aws ecs describe-task-definition --region eu-west-2 --task-definition discharge-eval-ws7 \
    --query 'taskDefinition.containerDefinitions[0].[image, environment[].name]'
  # The bucket: versioned, encrypted with the stack's key, public access blocked, one lifecycle rule.
  B=discharge-eval-$ACCOUNT-eu-west-2
  aws s3api get-bucket-versioning --bucket $B --expected-bucket-owner $ACCOUNT --query Status
  aws s3api get-bucket-encryption --bucket $B --expected-bucket-owner $ACCOUNT \
    --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault'
  aws s3api get-public-access-block --bucket $B --expected-bucket-owner $ACCOUNT
  aws s3api get-bucket-lifecycle-configuration --bucket $B --expected-bucket-owner $ACCOUNT \
    --query 'Rules[].[ID,Filter.Prefix||Prefix,Expiration.Days,NoncurrentVersionExpiration.NoncurrentDays]'
}
```

Pass: `0` inbound rules and `tcp 443 443`; both schedules `DISABLED`, `Europe/London`, `0`;
`86400 7 7 breaching` and a wall clock window with `Europe/London`; `26 26`; the image ends
`@sha256:…` and the names are `PYTHONHASHSEED`, `WS7_BUCKET`, `WS7_BUCKET_OWNER`,
`WS7_ALERT_TOPIC_ARN`, `WS7_IMAGE_DIGEST`; `Enabled`; `aws:kms` with the stack's key; four `true`;
one rule, `ws7/runs/scheduled/`, `90`, `30`.

### C10. The bare task, and the early-signal email

With no override the task runs `python evals/ws7_entry.py`, which is a usage error: exit code 2,
and nothing is built or sent. It makes no claim, reads nothing and calls no model.

```zsh
ws7_guard $ACCOUNT && () {
  SUBNET=$(aws cloudformation describe-stacks --region eu-west-2 --stack-name discharge-eval \
    --query "Stacks[0].Outputs[?OutputKey=='SubnetId'].OutputValue | [0]" --output text)
  SG=$(aws cloudformation describe-stacks --region eu-west-2 --stack-name discharge-eval \
    --query "Stacks[0].Outputs[?OutputKey=='SecurityGroupId'].OutputValue | [0]" --output text)
  [ -n "$SUBNET" ] && [ -n "$SG" ] && [ "$SUBNET" != None ] && [ "$SG" != None ] || { echo "STOP: no subnet or group"; return 1; }
  TASK=$(aws ecs run-task --region eu-west-2 --cluster discharge-eval --task-definition discharge-eval-ws7 \
    --launch-type FARGATE --count 1 \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$SG],assignPublicIp=ENABLED}" \
    --query 'tasks[0].taskArn' --output text)
  echo "task $TASK"
  aws ecs wait tasks-stopped --region eu-west-2 --cluster discharge-eval --tasks "$TASK"
  aws ecs describe-tasks --region eu-west-2 --cluster discharge-eval --tasks "$TASK" \
    --query 'tasks[0].[lastStatus, stopCode, containers[0].exitCode, containers[0].imageDigest, platformFamily, attributes[?name==`ecs.cpu-architecture`].value | [0]]'
  aws logs tail /discharge-eval/ws7 --region eu-west-2 --since 15m
}
```

Pass: `STOPPED`, `EssentialContainerExited`, exit code `2`; the log holds the one usage line; and an
email "WS7 eval task stopped: … stopCode EssentialContainerExited …" arrives. Keep `$TASK` for A2.

If the task never starts (`TaskFailedToStart`): read `stoppedReason`.

| The reason names | Most likely |
|---|---|
| The execution role cannot be assumed | The source conditions on its trust policy (unchecked for the execution role). A template change |
| The image cannot be pulled | The digest, or the outbound 443 rule |
| A timeout fetching credentials | The security group; the credentials endpoint should not need a rule (unchecked) |

No email: the rule's path to the topic (the topic policy's statement for EventBridge, or the key
policy's). The task-stopped rule is an early signal that ADR-011 does not rely on; record it.

### A2. The digest chain

```zsh
ws7_guard $ACCOUNT && () {
  D=<the pushed digest: the image ID check 5 ran on>
  # 1. The task definition pins it, and tells the job the same value.
  aws ecs describe-task-definition --region eu-west-2 --task-definition discharge-eval-ws7 \
    --query 'taskDefinition.containerDefinitions[0].[image, environment[?name==`WS7_IMAGE_DIGEST`].value | [0]]' --output text
  # 2. What ECR holds under that digest hashes to that digest, and names one arm64 image.
  aws ecr batch-get-image --region eu-west-2 --repository-name discharge-eval --image-ids imageDigest=$D \
    --query 'images[0].imageManifest' --output text > /tmp/ws7-manifest.json
  python3 - "$D" <<'PY'
import hashlib, json, sys
raw = open("/tmp/ws7-manifest.json", "rb").read().rstrip(b"\n")
print("sha256 of the stored manifest:", "sha256:" + hashlib.sha256(raw).hexdigest(), "(pinned:", sys.argv[1] + ")")
arm = [m["digest"] for m in json.loads(raw).get("manifests", [])
       if m.get("platform", {}).get("architecture") == "arm64" and m.get("platform", {}).get("os") == "linux"]
print("arm64 image manifests in it:", arm)
PY
  # 3. What ECS reported for C10's task.
  aws ecs describe-tasks --region eu-west-2 --cluster discharge-eval --tasks "$TASK" \
    --query 'tasks[0].containers[0].[image, imageDigest]' --output text
}
```

Pass, all of: the image ends `@$D` and the variable is `$D`; the stored manifest's sha256 is `$D`;
it names exactly one `linux/arm64` manifest; and the digest ECS reported is **either `$D` or that
arm64 manifest's digest**. Record which. AWS's `DescribeTasks` reference does not say which one ECS
reports for a manifest list, so either is right and anything else is a failure.

If the sha256 printed is not `$D`, the manifest was reformatted on its way through the CLI: compare
with `aws ecr describe-images --image-ids imageDigest=$D` instead, and say so in the record.

### A3. An alarm email arrives through the encrypted topic

This is the fix the live stack's alarms need (ADR-011, finding 5), proved here. Confirm the
subscription first.

```zsh
ws7_guard $ACCOUNT && () {
  aws sns list-subscriptions-by-topic --region eu-west-2 \
    --topic-arn arn:aws:sns:eu-west-2:$ACCOUNT:discharge-eval-alerts \
    --query 'Subscriptions[].[Protocol,SubscriptionArn]' --output text
  # Force one breach alarm into ALARM. It publishes no metric, and the alarm returns to
  # OK by itself at its next evaluation, within a few minutes.
  aws cloudwatch set-alarm-state --region eu-west-2 --alarm-name discharge-eval-BreachRunKind1 \
    --state-value ALARM --state-reason "WS7 set-up check A3: a forced state, not a breach"
  sleep 90
  aws cloudwatch describe-alarm-history --region eu-west-2 --alarm-name discharge-eval-BreachRunKind1 \
    --history-item-type Action --max-items 3 --query 'AlarmHistoryItems[].[Timestamp,HistorySummary]' --output text
}
```

Pass: the subscription shows an ARN, not `PendingConfirmation`; the history says the action was
**successfully executed**; and **the email arrives**. A history line that says the action failed
is the same failure the live topic is thought to have: the key policy's CloudWatch statement.

### A4. `RunOk` before any success

```zsh
ws7_guard $ACCOUNT && () {
  aws cloudwatch describe-alarms --region eu-west-2 --alarm-names discharge-eval-RunOk \
    --query 'MetricAlarms[0].[StateValue,StateUpdatedTimestamp,StateReason]' --output text
  aws cloudwatch describe-alarm-history --region eu-west-2 --alarm-name discharge-eval-RunOk \
    --history-item-type StateUpdate --query 'AlarmHistoryItems[].[Timestamp,HistorySummary]' --output text
}
```

**Expected: `ALARM`.** No success exists, and for this alarm missing data is a breach. It moves
there at its first evaluation, within about an hour of the deploy, and sends one email then (if the
subscription was already confirmed). It stays in `ALARM`, silently, until the first successful
scheduled run, and returns to OK at the London midnight after it (check C5). Record the time of
the first change.

**No set-up check publishes `RunOk`.** A synthetic success would be a false heartbeat.

### A5. The task role allows D6's list and nothing else

A simulation, with your own credentials. It makes no call as the role and changes nothing. It
reads the role's own policy, so it shows what the policy says; the task checks (B1 to B6) show what
AWS then does.

```zsh
ws7_guard $ACCOUNT && () {
  R=arn:aws:iam::$ACCOUNT:role/discharge-eval-task-role
  B=arn:aws:s3:::discharge-eval-$ACCOUNT-eu-west-2
  Q='EvaluationResults[0].[EvalActionName,EvalResourceName,EvalDecision]'
  sim() {
    if [ -n "${3:-}" ]; then
      aws iam simulate-principal-policy --policy-source-arn $R --action-names "$1" --resource-arns "$2" \
        --context-entries "$3" --query "$Q" --output text
    else
      aws iam simulate-principal-policy --policy-source-arn $R --action-names "$1" --resource-arns "$2" \
        --query "$Q" --output text
    fi
  }
  echo "--- each of these must say allowed"
  sim bedrock:InvokeModel arn:aws:bedrock:eu-west-2::foundation-model/anthropic.claude-sonnet-4-6
  sim s3:GetObject $B/ws7/x; sim s3:PutObject $B/ws7/x
  sim s3:ListBucket $B "ContextKeyName=s3:prefix,ContextKeyValues=ws7/ledger/,ContextKeyType=string"
  sim ssm:GetParameter arn:aws:ssm:eu-west-2:$ACCOUNT:parameter/discharge-eval/ws7/expected/v1
  sim ssm:GetParameter arn:aws:ssm:eu-west-2:$ACCOUNT:parameter/discharge-eval/ws7/expected/pipeline
  sim sns:Publish arn:aws:sns:eu-west-2:$ACCOUNT:discharge-eval-alerts
  sim cloudwatch:PutMetricData '*' "ContextKeyName=cloudwatch:namespace,ContextKeyValues=DischargeAssistant/Eval,ContextKeyType=string"
  echo "--- each of these must say implicitDeny"
  sim bedrock:InvokeModel arn:aws:bedrock:eu-west-2::foundation-model/anthropic.claude-haiku-4-5
  sim bedrock:InvokeModelWithResponseStream arn:aws:bedrock:eu-west-2::foundation-model/anthropic.claude-sonnet-4-6
  sim s3:DeleteObject $B/ws7/x; sim s3:PutObject $B/retained/x; sim s3:GetObject $B/retained/x
  sim s3:PutObject $B/x; sim s3:DeleteBucket $B
  sim s3:ListBucket $B "ContextKeyName=s3:prefix,ContextKeyValues=retained/,ContextKeyType=string"
  sim s3:ListBucket $B
  sim ssm:PutParameter arn:aws:ssm:eu-west-2:$ACCOUNT:parameter/discharge-eval/ws7/expected/v1
  sim ssm:GetParameter arn:aws:ssm:eu-west-2:$ACCOUNT:parameter/discharge-audit/anything
  sim sns:Publish arn:aws:sns:eu-west-2:$ACCOUNT:discharge-audit-alerts
  sim cloudwatch:PutMetricData '*' "ContextKeyName=cloudwatch:namespace,ContextKeyValues=DischargeAssistant/Canary,ContextKeyType=string"
  sim dynamodb:GetItem "arn:aws:dynamodb:eu-west-2:$ACCOUNT:table/discharge-audit-audit"
  sim dynamodb:Scan "arn:aws:dynamodb:eu-west-2:$ACCOUNT:table/discharge-audit-results"
  sim lambda:InvokeFunction "arn:aws:lambda:eu-west-2:$ACCOUNT:function:discharge-audit-generate"
  sim kms:Decrypt "$(aws cloudformation describe-stacks --region eu-west-2 --stack-name discharge-eval --query "Stacks[0].Outputs[?OutputKey=='KeyArn'].OutputValue | [0]" --output text)"
  sim iam:PassRole $R; sim sts:AssumeRole $R; sim logs:PutLogEvents '*'; sim ecr:BatchGetImage '*'
}
```

- The names after `discharge-audit` are only strings for the simulator. Nothing is sent to any
  live resource.
- `kms:Decrypt` on the stack's key says `implicitDeny` here because the simulation gives no
  `kms:ViaService`: the role can use the key only through S3 and SNS.
- A model ID that does not exist is still a valid string for the simulator.

## 6. Switch the schedules on and off

**Switching on is slice 6.** It needs the CSO's approval of the incident-log wording (slice 5), a
frozen baseline (slice 4), and the author's yes.

```zsh
infra/eval/schedules.sh --account $ACCOUNT status
infra/eval/schedules.sh --account $ACCOUNT on      # type ENABLE to confirm
infra/eval/schedules.sh --account $ACCOUNT off     # type DISABLE to confirm
```

- `on` refuses unless **all 26 alarms have their actions on**, each alarm's one action is the eval
  topic, and an email subscription is confirmed. If it names alarms with their actions off:

  ```zsh
  ws7_guard $ACCOUNT && aws cloudwatch enable-alarm-actions --region eu-west-2 --alarm-names \
    $(aws cloudwatch describe-alarms --region eu-west-2 --alarm-name-prefix discharge-eval- \
        --query 'MetricAlarms[?ActionsEnabled==`false`].AlarmName' --output text)
  ```

- Both scripts change the stack's `ScheduleState` parameter through a change set that holds the two
  schedules and nothing else. Do not use `aws scheduler update-schedule`: the next stack update
  would silently undo it.
- **Switch on between Thursday 10:00 and Wednesday 04:00 London**, so the first event is the
  Wednesday run. Switched on between the two, the first event is a check that finds no claim.
- **The backstop alarm is not armed on the first day.** `discharge-eval-RunOk` is already in
  `ALARM` (A4), and an alarm that is already in `ALARM` sends nothing more. Until the first
  successful run takes it to OK, the Thursday check is the only heartbeat. Check C5 records the
  first OK.
- A scheduled run with no frozen baseline is a failed run, **after it has spent**: the comparison
  comes after the generation. Never switch on without a baseline for the stack's target.

## 7. Triage an alarm

An alarm is a candidate, not an incident (ADR-011 D10). The clocks are the Safety Incident
Management Log's own and start at the alarm.

1. Read the email. A breach message from the job names the run, the family and kind, and the
   evidence prefix. A message from the Thursday check names what it found. A CloudWatch email
   names the alarm.
2. Read the run's records (your own credentials):

   ```zsh
   ws7_guard $ACCOUNT && () {
     RUN=<the run ID from the message>
     B=discharge-eval-$ACCOUNT-eu-west-2
     aws s3api list-objects-v2 --bucket $B --expected-bucket-owner $ACCOUNT \
       --prefix ws7/runs/scheduled/$RUN/ --query 'Contents[].Key' --output text | tr '\t' '\n'
     aws s3api get-object --bucket $B --expected-bucket-owner $ACCOUNT \
       --key ws7/runs/scheduled/$RUN/comparison.json /tmp/ws7-comparison.json >/dev/null && cat /tmp/ws7-comparison.json
     aws logs tail /discharge-eval/ws7 --region eu-west-2 --since 2d
   }
   ```

   A failed run has `ws7/failed/<run_id>.json`, with its code.
3. Decide the outcome: a false alarm, a quality finding, or an incident (D10).
4. **An incident: copy the run to `retained/<run_id>/` before its 90 days are up.** The task role
   cannot reach `retained/`, and the lifecycle rule does not touch it.

   ```zsh
   ws7_guard $ACCOUNT && () {
     RUN=<the run ID>
     B=discharge-eval-$ACCOUNT-eu-west-2
     case "$RUN" in ''|*/*|*' '*) echo "STOP: not a run ID"; return 1 ;; esac
     KEYS=$(aws s3api list-objects-v2 --bucket $B --expected-bucket-owner $ACCOUNT \
       --prefix ws7/runs/scheduled/$RUN/ --query 'Contents[].Key' --output text) || { echo "STOP: the run could not be listed"; return 1; }
     [ -n "$KEYS" ] && [ "$KEYS" != None ] || { echo "STOP: the run has no objects"; return 1; }
     N=0
     for K in ${=KEYS}; do
       aws s3api copy-object --bucket $B --expected-bucket-owner $ACCOUNT --expected-source-bucket-owner $ACCOUNT \
         --copy-source "$B/$K" --key "retained/$RUN/${K#ws7/runs/scheduled/$RUN/}" \
         --query 'CopyObjectResult.ETag' --output text || { echo "STOP: $K was not copied"; return 1; }
       N=$((N + 1))
     done
     M=$(aws s3api list-objects-v2 --bucket $B --expected-bucket-owner $ACCOUNT \
       --prefix retained/$RUN/ --query 'length(Contents)' --output text)
     echo "copied $N; retained/$RUN/ now holds $M"
   }
   ```

   Pass: the two numbers are equal. Copy `ws7/success/$RUN.json` or `ws7/failed/$RUN.json` the same
   way if the incident needs it; both are kept in any case.
5. **If the copy was missed.** After the 90 days the run's objects are hidden, not gone: the old
   versions are deleted 30 days later. In those 30 days, `aws s3api list-object-versions` shows
   them and `copy-object` with `?versionId=` restores one. After that there is no undo.
6. Record the outcome. An incident gets its `INC-YYYY-NNN` entry in
   `docs/WS4-SAFETY-INCIDENT-LOG.md`, by the author. The job never edits the log.

## 8. Resolve an unresolved entry

**Not available yet.** The resolve command is part 5b, which is not built
(`docs/WS7-ENTRYPOINT.md`, "Part 5b: the manual modes and the resolve command"). This section is
its contract, so the step exists when it is needed. Part 5b's pull request replaces it with the
command.

- An unresolved entry (an unmetered run, an abandoned reservation or a crashed run) blocks every
  later scheduled run until the author resolves it. The Thursday check names it every week.
- The author records a settled figure and its basis: `upper_bound`, the Cost Explorer figure
  exactly as ADR-011 D7 point 4 defines it; or `estimate_accepted`, which waives the month's
  assurance.
- Before its first use, confirm against real billing rows that the model's rows carry Charge type
  = Usage (the same section of the guide).
- Part 5b changes `evals/ws7_entry.py`, so it needs a new image: section 9.

## 9. Release a new image

Whenever a merged change touches what the image holds (`src/`, `prompts/`, `evals/`, `tests/`,
`requirements-eval.txt`, the `Dockerfile`):

1. Section 2, with the new merge commit.
2. The lifecycle check in section 2.
3. Section 3, with the new digest. The schedules keep their state.
4. For the pipeline target, section 4 at each release to `main`.
5. C10 and A2 again.

The old image stays in the repository, tagged. Delete it by hand when you no longer need it
(`aws ecr batch-delete-image`); never the one the task definition pins.

## 10. Tear down

Only if the stack is to be removed. Every step is a separate yes.

1. `infra/eval/schedules.sh --account $ACCOUNT off`.
2. Delete every image, or the stack delete fails on the repository:

   ```zsh
   ws7_guard $ACCOUNT && aws ecr list-images --region eu-west-2 --repository-name discharge-eval \
     --query 'imageIds[].imageDigest' --output text
   # then, for each digest:  aws ecr batch-delete-image --region eu-west-2 --repository-name discharge-eval --image-ids imageDigest=<digest>
   ```

3. Switch termination protection off and delete the stack:

   ```zsh
   ws7_guard $ACCOUNT && () {
     aws cloudformation update-termination-protection --region eu-west-2 --stack-name discharge-eval --no-enable-termination-protection
     aws cloudformation delete-stack --region eu-west-2 --stack-name discharge-eval
     aws cloudformation wait stack-delete-complete --region eu-west-2 --stack-name discharge-eval
   }
   ```

4. **Two things are retained on purpose: the bucket and the key.**
   - **The retained key costs £0.80 a month until it is scheduled for deletion.**
   - **Scheduling the key for deletion makes every object in the retained bucket unreadable,
     `retained/` incident copies included.** There is no undo once the waiting period ends (7 to 30
     days; it can be cancelled inside it).
   - So decide about the bucket first. To keep the records, keep both and accept the £0.80. To
     remove both: copy out anything you need, empty the bucket (every version and delete marker:
     it is versioned), delete it, and only then schedule the key's deletion.
5. The log group is deleted with the stack. The two parameters are deleted with the stack: note
   their values first if you will deploy again.

## Known limits

- **The CI deploy role can change parts of this stack** (`infra/cicd-oidc-role.yaml`). Only a
  workflow on `main` can assume it. As documented it cannot enable the schedules, because updating
  a schedule needs `iam:PassRole` for its role and CI may pass only `discharge-audit-*` roles. It
  can write the two parameters, delete the schedules, and disable or delete the alarms. The
  bucket, the key and the topic deny that role by name. Narrowing the role is a separate pull
  request, before slice 6 (ADR-011, "Known limits of the stack").
- **The base image is named by tag**, `python:3.13.13-slim-trixie`, not by digest. Two builds of
  one commit can differ in the base layers. Each pushed image is checked as built.
- **One subnet, one availability zone.** An outage there is a missed run, which the heartbeat
  reports.
