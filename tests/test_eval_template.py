"""infra/eval-template.yaml - the discharge-eval stack (ADR-011 D4 to D11; WS7 slice 3).

What cfn-lint cannot see: whether the template agrees with the job. Each test here
holds one part of the template to the constant the job itself uses (a prefix, a
namespace, a metric name, a variable name, a parameter name), or to a rule in
ADR-011. The template is read as text, as tests/test_template_trace.py reads the live
one: no YAML library is needed, and none is installed in the test environment.

Nothing here calls AWS, and nothing is deployed. Whether AWS accepts the stack, and
whether each policy grants what the job needs, is shown only by the deploy and the
set-up checks (docs/WS7-RUNBOOK.md).

This file is not one of the image's nine WS7 test files: it reads `infra/`, which is
not in the image.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "evals"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ws7_compare as compare  # noqa: E402
import ws7_entry as entry  # noqa: E402
import ws7_manifest as wm  # noqa: E402
import ws7_publish as publish  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
import ws7_store as store  # noqa: E402

TEMPLATE = ROOT / "infra" / "eval-template.yaml"
GUARD = ROOT / "infra" / "eval" / "guard.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci-cd.yml"
TEMPLATE_BODY_LIMIT = 51_200        # what --template-body takes; above it a bucket is needed
CI_ROLE = "GitHubActionsDischargeDeploy"
ACCOUNT = "000000000000"            # invented


def code_only(text: str) -> str:
    """The text without its comment lines: a rule is never satisfied by a comment."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def section(text: str, name: str) -> str:
    """One top-level section, for example `Resources`."""
    match = re.search(rf"^{name}:\n(.*?)(?=^\S|\Z)", text, re.S | re.M)
    assert match, f"no {name} section"
    return match.group(1)


def blocks(text: str, name: str = "Resources") -> dict:
    """Logical ID to its block of text, comments removed."""
    out, current = {}, None
    for line in code_only(section(text, name)).splitlines():
        head = re.fullmatch(r"  ([A-Za-z0-9]+):", line)
        if head:
            current = head.group(1)
            out[current] = ""
        elif current is not None:
            out[current] += line + "\n"
    return out


def one(pattern: str, text: str) -> str:
    found = re.findall(pattern, text, re.M)
    assert len(found) == 1, (pattern, found)
    return found[0]


def actions(block: str) -> set:
    """Every IAM action named in a block: `'service:Action'`, quoted, as this template
    writes them. A condition key is written the same way and is followed by a colon."""
    return set(re.findall(r"'([a-z0-9-]+:[A-Za-z*]+)'(?!:)", block))


@pytest.fixture(scope="module")
def text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def res(text) -> dict:
    return blocks(text)


def of_type(res: dict, kind: str) -> dict:
    return {name: block for name, block in res.items() if f"Type: {kind}\n" in block}


# ============================================================ the helpers ==

def test_the_readers_see_what_is_there_and_not_what_is_in_a_comment():
    """The control for every test below: a rule written only in a comment is not found,
    and a block ends where the next one starts."""
    sample = ("Resources:\n  A:\n    Type: X\n    # State: ENABLED\n    Name: a\n"
              "  B:\n    Type: Y\n    Action: ['s3:GetObject', 's3:PutObject']\n"
              "    Condition:\n      StringLike:\n        's3:prefix': 'ws7/*'\nOutputs:\n  O:\n    Value: v\n")
    found = blocks(sample)
    assert sorted(found) == ["A", "B"] and "ENABLED" not in found["A"] and "Name: a" in found["A"]
    assert "Type: Y" not in found["A"] and actions(found["B"]) == {"s3:GetObject", "s3:PutObject"}
    assert sorted(blocks(sample, "Outputs")) == ["O"]


# ============================================================== the stack ==

def test_the_template_fits_in_a_change_sets_template_body(text):
    """Above 51,200 bytes a deploy needs a bucket for --template-url. None is chosen:
    this test fails first, and infra/eval/deploy.sh stops (the author's ruling, 26 Nov 2026)."""
    size = len(TEMPLATE.read_bytes())
    assert size <= TEMPLATE_BODY_LIMIT, f"the template is {size} bytes"
    assert f"TEMPLATE_BODY_LIMIT={TEMPLATE_BODY_LIMIT}\n" in (GUARD.parent / "deploy.sh").read_text()


def test_it_is_plain_cloudformation_with_no_tie_to_the_live_stack(text, res):
    code = code_only(text)
    assert not re.search(r"^Transform:", code, re.M) and "ImportValue" not in code
    # D6: no permission on any discharge-audit resource. Nothing here even names one.
    assert "discharge-audit" not in code and "discharge-web" not in code
    assert "alias/aws/sns" not in code                       # ADR-011, finding 5
    assert len(res) == 54, sorted(res)


def test_every_role_is_named_discharge_eval_so_the_ci_role_cannot_manage_it(res):
    roles = of_type(res, "AWS::IAM::Role")
    names = {one(r"RoleName: (\S+)", block) for block in roles.values()}
    assert names == {"discharge-eval-task-role", "discharge-eval-execution-role",
                     "discharge-eval-scheduler-role"}
    assert "ManagedPolicyArns" not in "".join(roles.values())


def test_the_ci_workflow_lints_this_template():
    line = one(r"run: (cfn-lint .*)", WORKFLOW.read_text())
    assert line == ("cfn-lint --non-zero-exit-code error infra/template.yaml "
                    "infra/web-template.yaml infra/eval-template.yaml")


# ============================================================ the network ==

def test_the_security_group_has_no_inbound_rule_and_https_out_only(text, res):
    code = code_only(text)
    assert "SecurityGroupIngress" not in code and not of_type(res, "AWS::EC2::SecurityGroupIngress")
    groups = of_type(res, "AWS::EC2::SecurityGroup")
    assert list(groups) == ["TaskSecurityGroup"]
    group = groups["TaskSecurityGroup"]
    assert group.count("- IpProtocol:") == 1 and "IpProtocol: tcp\n" in group
    assert "FromPort: 443\n" in group and "ToPort: 443\n" in group
    assert len(of_type(res, "AWS::EC2::Subnet")) == 1 and len(of_type(res, "AWS::EC2::VPC")) == 1
    assert not of_type(res, "AWS::EC2::NatGateway") and not of_type(res, "AWS::EC2::VPCEndpoint")


# =============================================================== the task ==

def test_the_task_definition_is_d5s_and_sets_exactly_the_variables_the_job_reads(res):
    task = res["TaskDefinition"]
    assert "Condition: HasImage\n" in task
    assert "RequiresCompatibilities: [FARGATE]\n" in task and "CpuArchitecture: ARM64\n" in task
    assert "Cpu: '512'\n" in task and "Memory: '1024'\n" in task
    env = dict(re.findall(r"- Name: (\w+)\n +Value: (.+)\n", task.split("Environment:\n")[1]))
    assert env == {"PYTHONHASHSEED": "'0'",
                   entry.BUCKET_VARIABLE: "!Ref Bucket",
                   entry.OWNER_VARIABLE: "!Ref AWS::AccountId",
                   entry.TOPIC_VARIABLE: "!Ref AlertsTopic",
                   wm.DIGEST_VARIABLE: "!Ref ImageDigest"}
    assert "Command: [python, evals/ws7_entry.py]\n" in task
    assert "ReadonlyRootFilesystem" not in task              # the job needs a writable /tmp


def test_the_task_definition_never_sets_the_commit(text, res):
    """Part 6: the image sets WS7_GIT_COMMIT when it is built. An override here would let
    a task claim a commit its image was not built from."""
    assert wm.COMMIT_VARIABLE == "WS7_GIT_COMMIT"
    assert wm.COMMIT_VARIABLE not in code_only(text)
    assert wm.COMMIT_VARIABLE in text                        # the comment that says why is there


def test_the_image_is_pinned_by_the_same_digest_the_job_is_told(text, res):
    task = res["TaskDefinition"]
    image = one(r"Image: !Sub '(.+)'", task)
    assert image == "${AWS::AccountId}.dkr.ecr.${AWS::Region}.amazonaws.com/${Repository}@${ImageDigest}"
    parameter = blocks(text, "Parameters")["ImageDigest"]
    assert "Default: ''\n" in parameter
    pattern = re.compile(one(r"AllowedPattern: '(.+)'", parameter))
    digest = "sha256:" + "0" * 64
    assert pattern.fullmatch("") and pattern.fullmatch(digest)
    assert wm.identity({wm.COMMIT_VARIABLE: "0" * 40, wm.DIGEST_VARIABLE: digest}).image_digest == digest
    for bad in ("latest", "sha256:" + "0" * 63, "sha256:" + "G" * 64, digest + "\n", ":tag"):
        assert not pattern.fullmatch(bad), bad
    # Nothing that needs an image exists without one: no task definition, no schedule.
    conditional = sorted(name for name, block in blocks(text).items() if "Condition: HasImage\n" in block)
    assert conditional == ["CheckSchedule", "RunSchedule", "SchedulerRole", "TaskDefinition"]


def test_the_repository_scans_on_push_and_its_one_rule_touches_untagged_images_only(res):
    repo = res["Repository"]
    assert "ImageTagMutability: IMMUTABLE\n" in repo and "ScanOnPush: true\n" in repo
    policy = json.loads(one(r"LifecyclePolicyText: \|\n\s+(\{.+\})", repo))
    assert len(policy["rules"]) == 1
    rule = policy["rules"][0]
    assert rule["selection"] == {"tagStatus": "untagged", "countType": "sinceImagePushed",
                                 "countUnit": "days", "countNumber": 14}
    assert rule["action"] == {"type": "expire"}


# ============================================================== the roles ==

def test_the_task_role_is_d6s_list_and_nothing_else(res):
    role = res["TaskRole"]
    trust, policy = role.split("      Policies:\n")
    assert actions(trust) == {"sts:AssumeRole"} and "Service: ecs-tasks.amazonaws.com\n" in trust
    # No person and no account may assume it: the one principal is the ECS tasks service.
    assert "aws:SourceAccount" in trust and re.findall(r"^ +(AWS|Federated): ", trust, re.M) == []
    assert trust.count("Principal:") == 1
    assert actions(policy) == {"bedrock:InvokeModel", "cloudwatch:PutMetricData", "s3:GetObject",
                               "s3:PutObject", "s3:ListBucket", "ssm:GetParameter", "sns:Publish",
                               "kms:Decrypt", "kms:GenerateDataKey"}
    assert policy.count("- Sid: ") == 7 and "Effect: Deny" not in policy
    # Each statement, by its Sid: the actions and the resource or condition that limits them.
    by_sid = dict(re.findall(r"- Sid: (\w+)\n(.*?)(?=              - Sid: |\Z)", policy, re.S))
    model = by_sid["InvokeTheOnePinnedModel"]
    assert one(r"Resource: !Sub '(.+)'", model) == \
        "arn:${AWS::Partition}:bedrock:${AWS::Region}::foundation-model/" + reserve.rce.MODEL_ID
    metrics = by_sid["PutMetricsInOneNamespace"]
    assert f"'cloudwatch:namespace': {publish.NAMESPACE}\n" in metrics and "StringEquals:" in metrics
    objects = by_sid["GetAndPutUnderWs7"]
    assert one(r"Resource: !Sub '(.+)'", objects) == "${Bucket.Arn}/" + store.PREFIX + "*"
    listing = by_sid["ListWs7Only"]
    assert "Resource: !GetAtt Bucket.Arn\n" in listing and "StringLike:" in listing
    assert f"'s3:prefix': '{store.PREFIX}*'\n" in listing
    expected = by_sid["ReadTheExpectedValues"]
    assert set(re.findall(r":parameter(/\S+)'", expected)) == set(wm.EXPECTED_PARAMETER.values())
    assert "Resource: !Ref AlertsTopic\n" in by_sid["PublishToTheEvalTopic"]
    key = by_sid["UseTheKeyThroughS3AndSns"]
    assert "Resource: !GetAtt Key.Arn\n" in key and "'kms:ViaService':" in key
    assert set(re.findall(r"- !Sub '(\w+)\.\$\{AWS::Region\}\.amazonaws\.com'", key)) == {"s3", "sns"}
    # The one statement with no resource is the one AWS gives no resource level.
    assert [sid for sid, body in by_sid.items() if "Resource: '*'" in body] == ["PutMetricsInOneNamespace"]


def test_the_task_role_cannot_delete_write_a_parameter_or_reach_outside_ws7(res):
    policy = res["TaskRole"].split("      Policies:\n")[1]
    for never in ("Delete", "PutParameter", "s3:*", "kms:*", "'*:*'", "retained", "logs:", "ecr:",
                  "dynamodb", "lambda", "iam:", "sts:"):
        assert never not in policy, never
    assert store.RETAINED_PREFIX == "retained/" and not store.RETAINED_PREFIX.startswith(store.PREFIX)


def test_the_execution_role_pulls_this_repository_and_writes_this_tasks_logs(res):
    role = res["ExecutionRole"]
    policy = role.split("      Policies:\n")[1]
    assert actions(policy) == {"ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability",
                               "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
                               "logs:CreateLogStream", "logs:PutLogEvents"}
    assert "Resource: !GetAtt Repository.Arn\n" in policy
    assert "Resource: !Sub '${TaskLogGroup.Arn}:*'\n" in policy
    assert policy.count("Resource: '*'") == 1                # ecr:GetAuthorizationToken only


def test_the_scheduler_role_can_only_run_this_exact_task_revision_on_this_cluster(res):
    """Ruling H5 (26 Nov 2026). The class: a task definition the scheduler's role may run
    that is not the one the stack pins by digest. `!Ref TaskDefinition` is one revision's
    ARN: no wildcard, so an older revision, or one registered by hand under the same
    family, cannot be started with this role. The role exists only with an image."""
    role = res["SchedulerRole"]
    assert "Condition: HasImage\n" in role
    trust, policy = role.split("      Policies:\n")
    assert "Service: scheduler.amazonaws.com\n" in trust
    assert trust.rstrip().endswith(":schedule-group/discharge-eval'")
    assert actions(policy) == {"ecs:RunTask", "iam:PassRole"}
    run = re.search(r"- Sid: RunTheEvalTaskOnTheEvalCluster\n(.*?)- Sid: ", policy, re.S).group(1)
    assert one(r"Resource: (.+)\n", run) == "!Ref TaskDefinition"
    assert "task-definition/" not in policy and "*" not in run
    assert "'ecs:cluster': !GetAtt Cluster.Arn" in run
    # The same revision the two schedules start.
    for name in ("RunSchedule", "CheckSchedule"):
        assert "TaskDefinitionArn: !Ref TaskDefinition\n" in res[name]
    assert "Resource: [!GetAtt TaskRole.Arn, !GetAtt ExecutionRole.Arn]\n" in policy
    assert "'iam:PassedToService': ecs-tasks.amazonaws.com\n" in policy


# ======================================================= the bucket, the key ==

def test_the_bucket_is_encrypted_versioned_private_and_named_as_the_store_accepts(res):
    bucket = res["Bucket"]
    assert "SSEAlgorithm: 'aws:kms'\n" in bucket and "KMSMasterKeyID: !GetAtt Key.Arn\n" in bucket
    assert "VersioningConfiguration:\n        Status: Enabled\n" in bucket
    for block in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"):
        assert f"{block}: true\n" in bucket
    assert "DeletionPolicy: Retain\n" in bucket and "DeletionPolicy: Retain\n" in res["Key"]
    name = one(r"BucketName: !Sub '(.+)'", bucket)
    real = name.replace("${AWS::AccountId}", ACCOUNT).replace("${AWS::Region}", store.LIVE_REGION)
    assert store.Store(object(), real, ACCOUNT).bucket == real       # the store's own rule for a name


def test_the_one_lifecycle_rule_expires_scheduled_runs_and_nothing_else(res):
    """D8. One rule, one prefix: the prefix the store gives a scheduled run, under which
    nothing that is kept sits (tests/test_ws7_store.py). Versioned, so the rule also
    removes the old version, 30 days after the 90."""
    bucket = res["Bucket"]
    assert bucket.count("- Id: ") == 1
    prefix = one(r"Prefix: (\S+)", bucket)
    assert store.run_prefix(store.SCHEDULED, "RUN") == prefix + "RUN/"
    assert not store.run_prefix(store.MANUAL, "RUN").startswith(prefix)
    for kept in (store.LEDGER_PREFIX, store.SUCCESS_PREFIX, store.FAILED_PREFIX, store.RETAINED_PREFIX,
                 store.PREFIX + "claims/"):
        assert not kept.startswith(prefix) and not prefix.startswith(kept)
    assert "ExpirationInDays: 90\n" in bucket
    assert "NoncurrentVersionExpiration:\n              NoncurrentDays: 30\n" in bucket
    assert "Transition" not in bucket and "AbortIncompleteMultipartUpload" not in bucket


def test_the_topic_uses_the_stacks_own_key_and_cloudwatch_may_use_both(res):
    """The fix the live topic needs (ADR-011, finding 5; D9: one key, not two)."""
    assert len(of_type(res, "AWS::KMS::Key")) == 1
    topic = res["AlertsTopic"]
    assert "KmsMasterKeyId: !Ref Key\n" in topic
    name = one(r"TopicName: (\S+)", topic)
    arn = f"arn:aws:sns:{store.LIVE_REGION}:{ACCOUNT}:{name}"
    assert publish.topic_arn(arn) == arn                              # the job's own rule for a topic
    key, policy = res["Key"], res["AlertsTopicPolicy"]
    for block in (key, policy):
        assert "Service: cloudwatch.amazonaws.com\n" in block and "Service: events.amazonaws.com\n" in block
        assert ":alarm:discharge-eval-*'" in block
    assert "'aws:SourceArn': !GetAtt TaskStoppedRule.Arn\n" in policy
    cloudwatch = re.search(r"- Sid: CloudWatchAlarmsUseTheKeyForTheTopic\n(.*?)- Sid: ", key, re.S).group(1)
    assert actions(cloudwatch) == {"kms:Decrypt", "kms:GenerateDataKey*"}
    assert "'aws:SourceAccount': !Ref AWS::AccountId\n" in cloudwatch


@pytest.mark.parametrize("resource, wide", [("Key", "'kms:*'"), ("BucketPolicy", "'s3:*'"),
                                            ("AlertsTopicPolicy", "'sns:Publish'")])
def test_the_ci_deploy_role_is_denied_the_key_the_bucket_and_the_topic(res, resource, wide):
    """The author's ruling, 26 Nov 2026. The CI deploy role holds kms:*, s3:* and sns:* on
    everything in the region (infra/cicd-oidc-role.yaml). An explicit deny wins over that."""
    block = res[resource]
    deny = re.search(r"- Sid: DenyTheCiDeployRole\n(.*?)(?=          - Sid: |\Z)", block, re.S).group(1)
    assert "Effect: Deny\n" in deny and "Principal: '*'\n" in deny and wide in deny
    assert ("'aws:PrincipalArn': !Sub 'arn:${AWS::Partition}:iam::${AWS::AccountId}:role/"
            + CI_ROLE + "'\n") in deny and "ArnEquals:" in deny
    # The name is the role the workflow assumes.
    assert f":role/{CI_ROLE}\n" in WORKFLOW.read_text()
    if resource == "BucketPolicy":
        assert "Resource: [!GetAtt Bucket.Arn, !Sub '${Bucket.Arn}/*']\n" in deny


# ========================================================== the parameters ==

def test_the_two_expected_value_parameters_are_the_ones_the_job_reads(res):
    """D11. Plain Strings (the job refuses any other type). The value is a placeholder the
    match check refuses, and it must never change here: CloudFormation would overwrite
    what the author wrote by hand."""
    parameters = of_type(res, "AWS::SSM::Parameter")
    names = {one(r"Name: (\S+)", block) for block in parameters.values()}
    assert names == set(wm.EXPECTED_PARAMETER.values()) and len(parameters) == 2
    for block in parameters.values():
        assert "      Type: String\n" in block and "      Value: unset\n" in block
    with pytest.raises(json.JSONDecodeError):
        json.loads("unset")


# ========================================================== the schedules ==

@pytest.mark.parametrize("name, expression, mode", [
    ("RunSchedule", "cron(0 4 ? * WED *)", "scheduled"),
    ("CheckSchedule", "cron(0 10 ? * THU *)", "check")])
def test_each_schedule_passes_its_subcommand_the_target_and_its_own_scheduled_time(
        text, res, name, expression, mode):
    block = res[name]
    assert f"ScheduleExpression: {expression}\n" in block
    assert "ScheduleExpressionTimezone: Europe/London\n" in block and "Mode: 'OFF'\n" in block
    assert "MaximumRetryAttempts: 0\n" in block               # D7: the schedule's retry policy is zero
    assert "TaskDefinitionArn: !Ref TaskDefinition\n" in block and "AssignPublicIp: ENABLED\n" in block
    assert "GroupName: !Ref ScheduleGroup\n" in block
    for target in store.TARGETS:
        given = json.loads(one(r"Input: !Sub '(.+)'", block).replace("${Target}", target))
        assert list(given) == ["containerOverrides"] and len(given["containerOverrides"]) == 1
        override = given["containerOverrides"][0]
        assert override["name"] == one(r"^        - Name: (\w+)\n", res["TaskDefinition"])   # the container
        command = override["command"]
        assert command == ["python", "evals/ws7_entry.py", mode, "--target", target,
                           "--scheduled-time", "<aws.scheduler.scheduled-time>"]
        # The job's own parser takes exactly this command line.
        parsed = entry.build_parser().parse_args(command[2:])
        assert (parsed.mode, parsed.target) == (mode, target)
    assert sorted(re.findall(r"AllowedValues: \[(.+)\]", blocks(text, "Parameters")["Target"])) == \
        [", ".join(sorted(store.TARGETS, reverse=True))]


def test_both_schedules_are_created_disabled_and_take_their_state_from_one_parameter(text, res):
    schedules = of_type(res, "AWS::Scheduler::Schedule")
    assert sorted(schedules) == ["CheckSchedule", "RunSchedule"]
    for block in schedules.values():
        assert "      State: !Ref ScheduleState\n" in block and "ENABLED" not in block.replace(
            "AssignPublicIp: ENABLED", "")
    parameter = blocks(text, "Parameters")["ScheduleState"]
    assert "Default: DISABLED\n" in parameter and "AllowedValues: [DISABLED, ENABLED]\n" in parameter
    assert one(r"      Name: (\S+)", res["ScheduleGroup"]) == \
        one(r"WS7_SCHEDULE_GROUP=(\S+)", GUARD.read_text())


def test_the_check_is_one_utc_day_after_the_run_in_winter_and_in_summer():
    """Wednesday 04:00 and Thursday 10:00 London. The check looks CHECK_LOOKBACK_DAYS back
    from its own scheduled time, by UTC date."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    assert entry.CHECK_LOOKBACK_DAYS == 1
    for year, month, day in ((2026, 12, 16), (2027, 6, 16), (2027, 3, 31), (2027, 10, 27)):
        run = datetime(year, month, day, 4, 0, tzinfo=london)
        check = run.replace(hour=10) + timedelta(days=1)
        assert run.strftime("%a") == "Wed" and check.strftime("%a") == "Thu"
        back = check.astimezone(timezone.utc) - timedelta(days=entry.CHECK_LOOKBACK_DAYS)
        assert back.date() == run.astimezone(timezone.utc).date()


# ============================================================= the alarms ==

def metric_alarms(res: dict) -> dict:
    return {one(r"MetricName: (\w+)", block): block
            for block in of_type(res, "AWS::CloudWatch::Alarm").values()}


def test_every_alarm_is_armed_and_its_one_action_is_the_eval_topic(res):
    alarms = of_type(res, "AWS::CloudWatch::Alarm")
    assert len(alarms) == int(one(r"WS7_ALARM_COUNT=(\d+)", GUARD.read_text())) == 26
    prefix = one(r"WS7_ALARM_PREFIX=(\S+)", GUARD.read_text())
    for block in alarms.values():
        assert "      ActionsEnabled: true\n" in block
        assert "      AlarmActions: [!Ref AlertsTopic]\n" in block
        # The name the key policy and the topic policy let through, and the one schedules.sh counts.
        assert one(r"AlarmName: (\S+)", block).startswith(prefix)
        assert one(r"AlarmName: \S+-(\w+)\n", block)[:5] in one(r"MetricName: (\w+)", block) \
            or "Scheduler" in block


def test_there_is_one_breach_alarm_for_each_family_and_kind_the_job_can_publish(res):
    """Both ways: a published breach metric with no alarm is a silent gap, and an alarm
    on a name the job never publishes can never fire."""
    alarms = metric_alarms(res)
    published = {publish.breach_name(family, kind)
                 for family in publish.BREACH_FAMILIES for kind in compare.KINDS}
    assert len(published) == 21
    assert {name for name in alarms if name.startswith("Breach")} == published
    for name in published:
        block = alarms[name]
        assert f"Namespace: {publish.NAMESPACE}\n" in block
        assert f"- Name: {publish.DIMENSION}\n          Value: !Ref Target\n" in block
        assert "Statistic: Maximum\n" in block and "Threshold: 0\n" in block
        assert "ComparisonOperator: GreaterThanThreshold\n" in block
        assert "EvaluationPeriods: 1\n" in block and "DatapointsToAlarm: 1\n" in block


def test_only_the_heartbeat_treats_missing_data_as_a_breach(res):
    """A failed run publishes no breach value, so a breach alarm must not read silence as
    a breach. RunOk is the one alarm that must."""
    alarms = metric_alarms(res)
    treats = {name: one(r"TreatMissingData: (\w+)", block) for name, block in alarms.items()}
    assert treats.pop(publish.RUN_OK) == "breaching"
    assert set(treats.values()) == {"notBreaching"} and len(treats) == 25


def test_the_heartbeat_alarm_is_d9s_table(res):
    block = metric_alarms(res)[publish.RUN_OK]
    for line in (f"Namespace: {publish.NAMESPACE}",
                 f"Dimensions:\n        - Name: {publish.DIMENSION}\n          Value: !Ref Target",
                 "Statistic: Maximum", "Period: 86400", "EvaluationPeriods: 7", "DatapointsToAlarm: 7",
                 "Threshold: 1", "ComparisonOperator: LessThanThreshold", "TreatMissingData: breaching",
                 "EvaluationWindow:\n        WallClockWindow:\n          Timezone: Europe/London"):
        assert f"      {line}\n" in block, line
    assert publish.LONDON.key == "Europe/London"


def test_no_baseline_and_the_spend_guard_each_have_an_alarm_on_the_name_the_job_publishes(res):
    alarms = metric_alarms(res)
    assert publish.NO_BASELINE in alarms and publish.GUARD_METRICS[0] == "SpendGuardTrips"
    assert publish.GUARD_METRICS[0] in alarms
    others = set(alarms) - {publish.RUN_OK, publish.NO_BASELINE, publish.GUARD_METRICS[0]}
    assert {name for name in others if not name.startswith("Breach")} == \
        {"TargetErrorCount", "InvocationDroppedCount"}
    for name in ("TargetErrorCount", "InvocationDroppedCount"):
        assert "Namespace: AWS/Scheduler\n" in alarms[name]
        assert "- Name: ScheduleGroup\n          Value: !Ref ScheduleGroup\n" in alarms[name]


def test_the_early_signal_rule_watches_this_cluster_for_a_task_that_failed(res):
    rule = res["TaskStoppedRule"]
    assert "clusterArn: [!GetAtt Cluster.Arn]\n" in rule and "lastStatus: [STOPPED]\n" in rule
    assert "stopCode: [TaskFailedToStart]\n" in rule and "exitCode: [{anything-but: 0}]\n" in rule
    assert "Arn: !Ref AlertsTopic\n" in rule
    # The message is a fixed line and three fields: no command line and no reason text.
    assert set(re.findall(r"<(\w+)>", one(r"InputTemplate: '(.+)'", rule))) == {"task", "stop", "time"}
