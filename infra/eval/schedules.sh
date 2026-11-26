#!/bin/bash
# schedules.sh - the only thing that switches the two eval schedules on or off
# (ADR-011, "The order of work", slice 6; docs/WS7-RUNBOOK.md).
#
#   infra/eval/schedules.sh --account <12 digits> status
#   infra/eval/schedules.sh --account <12 digits> on
#   infra/eval/schedules.sh --account <12 digits> off
#   infra/eval/schedules.sh --account <12 digits> force-off
#
# NOTHING ON A PATH TO DISABLED ENABLES ANYTHING (the author's ruling, 26 Nov 2026).
# When a live schedule is ENABLED and should not be, the answer is `force-off`: it
# reads each live eval schedule that is ENABLED and writes it back with State
# DISABLED and its definition otherwise unchanged. It changes nothing else, reads both
# again, and exits 0 only if both are DISABLED. It does not touch the stack; if the
# stack's ScheduleState is ENABLED it says to run `off` afterwards. It needs python3
# to copy the schedule's definition.
#
# It changes the stack's ScheduleState parameter through a change set that reuses
# the deployed template and every other parameter, and it refuses a change set that
# holds anything but the two schedules.
#
# `on` REFUSES unless every alarm in the stack has its actions on, with the eval topic
# as its one action, and the alert email subscription is confirmed. A schedule that
# runs with a silent alarm is the failure this guards against.
#
# Switching on needs the CSO's approval of the incident-log wording (slice 5) and the
# author's yes. Switch on between Thursday 10:00 and Wednesday 04:00 London, so that
# the first event is the Wednesday run and not a check that finds no claim.
#
# COST: none for the switch. Once on, a weekly run spends up to its reservation
# (ADR-011, Costs), inside the £15 a month ceiling.

set -u

HERE=$(cd "$(dirname "$0")" && pwd) || exit 1
# shellcheck source=guard.sh
. "$HERE/guard.sh"

stop() {
  echo "STOP: $*" >&2
  exit 1
}

ACCOUNT="" ACTION=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --account) [ "$#" -ge 2 ] || stop "--account needs a value"; ACCOUNT="$2"; shift 2 ;;
    status|on|off|force-off) [ -z "$ACTION" ] || stop "give one of status, on, off, force-off"; ACTION="$1"; shift ;;
    *) stop "usage: schedules.sh --account <12 digits> status|on|off|force-off" ;;
  esac
done
[ -n "$ACTION" ] || stop "usage: schedules.sh --account <12 digits> status|on|off|force-off"

ws7_guard "$ACCOUNT" || stop "the identity and region check failed"

cfn() { aws cloudformation --region "$WS7_REGION" "$@"; }

stack_parameter() {   # stack_parameter <key>: prints its value; fails if it cannot be read
  local value
  if ! value=$(cfn describe-stacks --stack-name "$WS7_STACK" \
      --query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue | [0]" --output text); then
    return 1
  fi
  printf '%s' "$value"
}

schedule_state() {    # schedule_state <name>: prints ENABLED or DISABLED
  local value
  if ! value=$(aws scheduler get-schedule --region "$WS7_REGION" \
      --group-name "$WS7_SCHEDULE_GROUP" --name "$1" --query State --output text) \
      || [ -z "$value" ]; then
    return 1
  fi
  printf '%s' "$value"
}

SCHEDULES="discharge-eval-ws7-run discharge-eval-ws7-check"

# EVERY SUCCESS THIS SCRIPT REPORTS IS ABOUT THE LIVE SCHEDULES (the author's ruling,
# 26 Nov 2026). The class it closes: exit 0 while a live schedule's state differs from
# the stack's parameter (a schedule changed outside the stack, which CloudFormation
# does not see). `status`, a switch to the state the stack is already in, and a
# switch that was made all end here: both live states are read, and both must be the
# wanted one.
#
# The STOP names each schedule that is out of step and points to `force-off`. IT NEVER
# TELLS THE OPERATOR TO SWITCH ANYTHING ON: a path to DISABLED that enables the
# Wednesday run on the way could start a paid run.
live_must_be() {      # live_must_be <ENABLED|DISABLED>
  local name state drifted=""
  for name in $SCHEDULES; do
    state=$(schedule_state "$name") || stop "the schedule $name could not be read"
    echo "schedule $name is $state"
    if [ "$state" != "$1" ]; then
      drifted="$drifted $name($state)"
    fi
  done
  if [ -n "$drifted" ]; then
    echo "STOP: not $1, which is what the stack's ScheduleState says:$drifted" >&2
    echo "Changed outside the stack. To switch every live eval schedule off, enabling nothing:" >&2
    echo "  infra/eval/schedules.sh --account $ACCOUNT force-off" >&2
    if [ "$1" = ENABLED ]; then
      echo "and then, to bring the stack into line with them:" >&2
      echo "  infra/eval/schedules.sh --account $ACCOUNT off" >&2
    fi
    exit 1
  fi
}

# force-off: every live eval schedule that is ENABLED is written back DISABLED, with
# its definition otherwise unchanged. Nothing is enabled, and the stack is not touched.
if [ "$ACTION" = force-off ]; then
  TMP=$(mktemp -d "${TMPDIR:-/tmp}/ws7-force-off.XXXXXXXX") || stop "no temporary folder could be made"
  [ -n "$TMP" ] && [ -d "$TMP" ] || stop "no temporary folder could be made"
  trap 'rm -rf "$TMP"' EXIT
  todo=""
  for name in $SCHEDULES; do
    state=$(schedule_state "$name") || stop "the schedule $name could not be read. Nothing was changed."
    echo "schedule $name is $state"
    case "$state" in
      ENABLED) todo="$todo $name" ;;
      DISABLED) ;;
      *) stop "the schedule $name is neither ENABLED nor DISABLED. Nothing was changed." ;;
    esac
  done
  if [ -n "$todo" ]; then
    printf 'This writes State DISABLED to:%s. Type the stack name to go on: ' "$todo"
    IFS= read -r answer || answer=""
    [ "$answer" = "$WS7_STACK" ] || stop "not confirmed. Nothing was changed."
    for name in $todo; do
      # The schedule as it is, whole.
      aws scheduler get-schedule --region "$WS7_REGION" --group-name "$WS7_SCHEDULE_GROUP" \
        --name "$name" --output json >"$TMP/$name.json" \
        || stop "the schedule $name could not be read. It was not changed."
      # The same definition with State DISABLED and nothing else different. The three
      # fields the service sets itself are left out, and so is a field with no value.
      python3 -c '
import json, sys
have = json.load(open(sys.argv[1]))
if have.get("Name") != sys.argv[3] or have.get("State") != "ENABLED":
    sys.exit(1)
want = {k: v for k, v in have.items()
        if k not in ("Arn", "CreationDate", "LastModificationDate") and v is not None}
want["State"] = "DISABLED"
json.dump(want, open(sys.argv[2], "w"))
' "$TMP/$name.json" "$TMP/$name.off.json" "$name" \
        || stop "the definition of $name could not be copied. It was not changed."
      [ -s "$TMP/$name.off.json" ] || stop "the definition of $name could not be copied. It was not changed."
      aws scheduler update-schedule --region "$WS7_REGION" \
        --cli-input-json "file://$TMP/$name.off.json" >/dev/null \
        || stop "the schedule $name could not be written. Read both schedules again: run force-off once more."
      echo "schedule $name: written with State DISABLED"
    done
  fi
  # Believed only when read back: both DISABLED, or this is not a success.
  left=""
  for name in $SCHEDULES; do
    state=$(schedule_state "$name") || stop "the schedule $name could not be read after the change"
    echo "schedule $name is $state"
    if [ "$state" != DISABLED ]; then
      left="$left $name($state)"
    fi
  done
  [ -z "$left" ] || stop "still not DISABLED:$left"
  echo "force-off: both live eval schedules are DISABLED."
  if NOW=$(stack_parameter ScheduleState) && [ "$NOW" = DISABLED ]; then
    echo "The stack's ScheduleState is DISABLED too: nothing more to do."
  else
    echo "The stack's ScheduleState is not DISABLED (or could not be read). Bring the stack into line:"
    echo "  infra/eval/schedules.sh --account $ACCOUNT off"
  fi
  exit 0
fi

NOW=$(stack_parameter ScheduleState) || stop "the stack's ScheduleState could not be read"
case "$NOW" in
  DISABLED|ENABLED) ;;
  *) stop "the stack's ScheduleState is neither DISABLED nor ENABLED" ;;
esac
DIGEST=$(stack_parameter ImageDigest) || stop "the stack's ImageDigest could not be read"
case "$DIGEST" in
  sha256:*) ;;
  *) stop "the stack has no image yet, so it has no schedules (deploy phase 2 first)" ;;
esac

if [ "$ACTION" = status ]; then
  echo "the stack's ScheduleState is $NOW"
  live_must_be "$NOW"
  exit 0
fi

if [ "$ACTION" = on ]; then WANT=ENABLED WORD=ENABLE; else WANT=DISABLED WORD=DISABLE; fi
if [ "$NOW" = "$WANT" ]; then
  echo "the stack's ScheduleState is already $WANT"
  live_must_be "$WANT"
  exit 0
fi

if [ "$ACTION" = on ]; then
  # Every alarm's actions are on, and each alarm's one action is the eval topic.
  if ! TOPIC=$(cfn describe-stacks --stack-name "$WS7_STACK" \
      --query "Stacks[0].Outputs[?OutputKey=='AlertsTopicArn'].OutputValue | [0]" --output text) \
      || [ -z "$TOPIC" ]; then
    stop "the eval topic's ARN could not be read"
  fi
  case "$TOPIC" in
    arn:aws:sns:"$WS7_REGION":"$ACCOUNT":discharge-eval-alerts) ;;
    *) stop "the eval topic is not the one this script expects" ;;
  esac
  alarms() {            # alarms <JMESPath that gives a number>
    aws cloudwatch describe-alarms --region "$WS7_REGION" --alarm-name-prefix "$WS7_ALARM_PREFIX" \
      --alarm-types MetricAlarm --query "$1" --output text
  }
  if ! total=$(alarms 'length(MetricAlarms)') || [ -z "$total" ]; then
    stop "the alarms could not be read"
  fi
  [ "$total" = "$WS7_ALARM_COUNT" ] || stop "the stack should have $WS7_ALARM_COUNT alarms; $total were found"
  if ! armed=$(alarms 'length(MetricAlarms[?ActionsEnabled==`true`])') || [ -z "$armed" ]; then
    stop "the alarms could not be read"
  fi
  [ "$armed" = "$WS7_ALARM_COUNT" ] \
    || stop "only $armed of $WS7_ALARM_COUNT alarms have their actions on. Run: aws cloudwatch enable-alarm-actions, then try again."
  if ! wired=$(alarms "length(MetricAlarms[?length(AlarmActions)==\`1\` && AlarmActions[0]=='$TOPIC'])") \
      || [ -z "$wired" ]; then
    stop "the alarms could not be read"
  fi
  [ "$wired" = "$WS7_ALARM_COUNT" ] \
    || stop "only $wired of $WS7_ALARM_COUNT alarms have the eval topic as their one action"
  # Someone is listening: at least one confirmed email subscription.
  if ! heard=$(aws sns list-subscriptions-by-topic --region "$WS7_REGION" --topic-arn "$TOPIC" \
      --query "length(Subscriptions[?Protocol=='email' && starts_with(SubscriptionArn, 'arn:aws:sns:')])" \
      --output text) || [ -z "$heard" ]; then
    stop "the topic's subscriptions could not be read"
  fi
  case "$heard" in
    ''|*[!0-9]*|0) stop "no email subscription to the eval topic is confirmed" ;;
  esac
  echo "schedules: $armed alarms armed, each with the eval topic; $heard confirmed email subscription(s)"
fi

NAME="ws7-schedules-$(date -u +%Y%m%dT%H%M%SZ)"
cfn create-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" \
  --change-set-type UPDATE --use-previous-template --capabilities CAPABILITY_NAMED_IAM \
  --parameters "ParameterKey=Target,UsePreviousValue=true" \
               "ParameterKey=ImageDigest,UsePreviousValue=true" \
               "ParameterKey=AlertEmail,UsePreviousValue=true" \
               "ParameterKey=ScheduleState,ParameterValue=$WANT" >/dev/null \
  || stop "the change set could not be created"

discard() {
  cfn delete-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" >/dev/null 2>&1
  echo "The change set was not executed. The schedules are as they were." >&2
}

cfn wait change-set-create-complete --stack-name "$WS7_STACK" --change-set-name "$NAME" \
  || { discard; stop "the change set was not created"; }

# The change set must be the two schedules, modified, and nothing else.
query() {
  cfn describe-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" --query "$1" --output text
}
if ! all=$(query 'length(Changes)') || [ -z "$all" ]; then
  discard; stop "the change set could not be read"
fi
if ! ours=$(query "length(Changes[?ResourceChange.ResourceType=='AWS::Scheduler::Schedule' && ResourceChange.Action=='Modify' && (ResourceChange.LogicalResourceId=='RunSchedule' || ResourceChange.LogicalResourceId=='CheckSchedule')])") \
    || [ -z "$ours" ]; then
  discard; stop "the change set could not be read"
fi
if [ "$all" != "2" ] || [ "$ours" != "2" ]; then
  discard; stop "the change set is not the two schedules and nothing else ($all changes, $ours of them the schedules)"
fi

printf 'This sets both schedules to %s. Type %s to go on: ' "$WANT" "$WORD"
IFS= read -r answer || answer=""
if [ "$answer" != "$WORD" ]; then
  discard; stop "not confirmed"
fi

cfn execute-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" \
  || stop "the change set could not be executed"
cfn wait stack-update-complete --stack-name "$WS7_STACK" || stop "the stack did not finish updating"

live_must_be "$WANT"
echo "schedules: done. Both are $WANT."
exit 0
