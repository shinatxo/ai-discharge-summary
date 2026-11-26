#!/bin/bash
# deploy.sh - create or update the discharge-eval stack by hand, through a change set
# the operator reads before it is executed (ADR-011 D4; docs/WS7-RUNBOOK.md).
#
#   The first deploy:  infra/eval/deploy.sh --account <12 digits> --target v1|pipeline
#   Every later one:   infra/eval/deploy.sh --account <12 digits> [--image-digest sha256:<64 hex>]
#   At W11 only:       infra/eval/deploy.sh --account <12 digits> --change-target pipeline
#
# Phase 1 (the first deploy, no image): everything except the task definition, the
# scheduler's role and the two schedules. Phase 2 (--image-digest, from
# push-image.sh): the task definition, pinned by that digest, and the two schedules.
#
# THE SCHEDULES' STATE IS NEVER CHANGED HERE. At creation it is DISABLED. On every
# update this script passes the stack's previous value, and it refuses a change set
# that would change any schedule's State. Only schedules.sh changes it. Otherwise
# every image release after slice 6 would switch both schedules off. It also STOPS,
# before any change set, if either live schedule is not in the state the stack's
# parameter says: a schedule changed outside the stack is put right first.
#
# THE TARGET IS KEPT TOO (the author's ruling, 26 Nov 2026). --target is for the first
# deploy only. On an update the stack's Target is handed back unchanged, so a new
# image after W11 cannot switch the pipeline back to v1. Only --change-target changes
# it, and the script prints the old and the new value before you confirm.
#
# The alert email comes from WS7_ALERT_EMAIL in the operator's environment. It is
# never written to the repository. On an update, leave it unset to keep the address.
#
# COST: creating the stack starts its fixed cost, about £2.70 to £2.90 a month (the
# key and 26 alarms; docs/COST.md). A change set and a deploy cost nothing themselves.
#
# The template is sent with --template-body, which AWS limits to 51,200 bytes. Above
# that this script STOPS: it chooses no bucket.

set -u

HERE=$(cd "$(dirname "$0")" && pwd) || exit 1
# shellcheck source=guard.sh
. "$HERE/guard.sh"
TEMPLATE="$HERE/../eval-template.yaml"
TEMPLATE_BODY_LIMIT=51200

stop() {
  echo "STOP: $*" >&2
  exit 1
}

USAGE="usage: deploy.sh --account <12 digits> [--target v1|pipeline | --change-target v1|pipeline] [--image-digest sha256:<64 hex>]"
ACCOUNT="" TARGET="" NEW_TARGET="" DIGEST=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --account) [ "$#" -ge 2 ] || stop "--account needs a value"; ACCOUNT="$2"; shift 2 ;;
    --target) [ "$#" -ge 2 ] || stop "--target needs a value"; TARGET="$2"; shift 2 ;;
    --change-target) [ "$#" -ge 2 ] || stop "--change-target needs a value"; NEW_TARGET="$2"; shift 2 ;;
    --image-digest) [ "$#" -ge 2 ] || stop "--image-digest needs a value"; DIGEST="$2"; shift 2 ;;
    *) stop "$USAGE" ;;
  esac
done
case "$TARGET" in
  ''|v1|pipeline) ;;
  *) stop "--target must be v1 or pipeline" ;;
esac
case "$NEW_TARGET" in
  ''|v1|pipeline) ;;
  *) stop "--change-target must be v1 or pipeline" ;;
esac
if [ -n "$TARGET" ] && [ -n "$NEW_TARGET" ]; then
  stop "give --target (the first deploy) or --change-target (a later one), not both"
fi
if [ -n "$DIGEST" ]; then
  hex=${DIGEST#sha256:}
  case "$hex" in
    ''|*[!0-9a-f]*) stop "--image-digest must be sha256: and 64 hex characters" ;;
  esac
  if [ "$hex" = "$DIGEST" ] || [ "${#hex}" -ne 64 ]; then
    stop "--image-digest must be sha256: and 64 hex characters"
  fi
fi
[ -f "$TEMPLATE" ] || stop "the template is not at infra/eval-template.yaml"
if ! size=$(wc -c <"$TEMPLATE") || [ -z "$size" ]; then
  stop "the template's size could not be read"
fi
size=$((size + 0))
if [ "$size" -gt "$TEMPLATE_BODY_LIMIT" ]; then
  stop "the template is $size bytes, over the $TEMPLATE_BODY_LIMIT a change set takes inline. Tell Shina: no bucket is chosen here."
fi

ws7_guard "$ACCOUNT" || stop "the identity and region check failed"

cfn() { aws cloudformation --region "$WS7_REGION" "$@"; }

# Does the stack exist? Only a clear "does not exist" is read as no.
ERR=$(mktemp "${TMPDIR:-/tmp}/ws7-deploy.XXXXXXXX") || stop "no temporary file could be made"
trap 'rm -f "$ERR"' EXIT
if cfn describe-stacks --stack-name "$WS7_STACK" --query 'Stacks[0].StackStatus' --output text \
    >/dev/null 2>"$ERR"; then
  KIND=UPDATE
elif grep -q "Stack with id $WS7_STACK does not exist" "$ERR"; then
  KIND=CREATE
else
  cat "$ERR" >&2
  stop "the stack's state could not be read"
fi

stack_value() {   # stack_value <JMESPath under Stacks[0]>: prints it; fails if it cannot be read
  local value
  if ! value=$(cfn describe-stacks --stack-name "$WS7_STACK" --query "Stacks[0].$1" --output text) \
      || [ -z "$value" ]; then
    return 1
  fi
  printf '%s' "$value"
}

EMAIL="${WS7_ALERT_EMAIL:-}"
if [ "$KIND" = CREATE ]; then
  [ -z "$NEW_TARGET" ] || stop "there is no stack yet: the first deploy takes --target, not --change-target"
  [ -n "$TARGET" ] || stop "the first deploy needs --target v1 or --target pipeline"
  WANT_TARGET="$TARGET"
  [ -n "$EMAIL" ] || stop "set WS7_ALERT_EMAIL for the first deploy: the alarms need someone to tell"
  WANT_STATE=DISABLED
  PARAMS=("ParameterKey=Target,ParameterValue=$TARGET"
          "ParameterKey=ImageDigest,ParameterValue=$DIGEST"
          "ParameterKey=ScheduleState,ParameterValue=DISABLED"
          "ParameterKey=AlertEmail,ParameterValue=$EMAIL")
else
  [ -z "$TARGET" ] || stop "the stack exists and keeps its target: leave --target out. Only --change-target changes it."
  # The state as it is now: this script hands it back unchanged.
  WANT_STATE=$(stack_value "Parameters[?ParameterKey=='ScheduleState'].ParameterValue | [0]") \
    || stop "the stack's ScheduleState could not be read"
  case "$WANT_STATE" in
    DISABLED|ENABLED) ;;
    *) stop "the stack's ScheduleState is neither DISABLED nor ENABLED" ;;
  esac
  # The target as it is now: handed back unchanged unless --change-target was given.
  TARGET_NOW=$(stack_value "Parameters[?ParameterKey=='Target'].ParameterValue | [0]") \
    || stop "the stack's Target could not be read"
  case "$TARGET_NOW" in
    v1|pipeline) ;;
    *) stop "the stack's Target is neither v1 nor pipeline" ;;
  esac
  if [ -n "$NEW_TARGET" ] && [ "$NEW_TARGET" != "$TARGET_NOW" ]; then
    WANT_TARGET="$NEW_TARGET"
    PARAMS=("ParameterKey=Target,ParameterValue=$NEW_TARGET")
    echo "deploy: THE TARGET CHANGES from $TARGET_NOW to $NEW_TARGET (both schedules and every alarm follow it)"
  else
    WANT_TARGET="$TARGET_NOW"
    PARAMS=("ParameterKey=Target,UsePreviousValue=true")
    echo "deploy: the target stays $TARGET_NOW"
  fi
  PARAMS+=("ParameterKey=ScheduleState,UsePreviousValue=true")
  # The live schedules must be in the state the stack says. One changed outside the
  # stack is a STOP here, before any change set: CloudFormation cannot see it.
  # (Read directly: an empty value is a real answer here, and a failed read is a STOP.)
  if ! DIGEST_NOW=$(cfn describe-stacks --stack-name "$WS7_STACK" \
      --query "Stacks[0].Parameters[?ParameterKey=='ImageDigest'].ParameterValue | [0]" --output text); then
    stop "the stack's ImageDigest could not be read"
  fi
  case "$DIGEST_NOW" in
    sha256:*)
      for name in discharge-eval-ws7-run discharge-eval-ws7-check; do
        if ! live=$(aws scheduler get-schedule --region "$WS7_REGION" \
            --group-name "$WS7_SCHEDULE_GROUP" --name "$name" --query State --output text) \
            || [ -z "$live" ]; then
          stop "the live schedule $name could not be read"
        fi
        if [ "$live" != "$WANT_STATE" ]; then
          stop "the live schedule $name is $live, and the stack's ScheduleState says $WANT_STATE. It was changed outside the stack. Put it right first: infra/eval/schedules.sh --account $ACCOUNT status"
        fi
      done ;;
    ''|None) ;;      # no image yet, so the stack has no schedules
    *) stop "the stack's ImageDigest could not be read" ;;
  esac
  if [ -n "$DIGEST" ]; then
    PARAMS+=("ParameterKey=ImageDigest,ParameterValue=$DIGEST")
  else
    PARAMS+=("ParameterKey=ImageDigest,UsePreviousValue=true")
  fi
  if [ -n "$EMAIL" ]; then
    PARAMS+=("ParameterKey=AlertEmail,ParameterValue=$EMAIL")
  else
    PARAMS+=("ParameterKey=AlertEmail,UsePreviousValue=true")
  fi
fi

# A digest must be an image that is in the eval repository.
if [ -n "$DIGEST" ]; then
  if ! GOT=$(aws ecr describe-images --region "$WS7_REGION" --registry-id "$ACCOUNT" \
      --repository-name "$WS7_REPOSITORY" --image-ids "imageDigest=$DIGEST" \
      --query 'imageDetails[0].imageDigest' --output text) || [ -z "$GOT" ]; then
    stop "that digest is not an image in the eval repository"
  fi
  [ "$GOT" = "$DIGEST" ] || stop "that digest is not an image in the eval repository"
fi

NAME="ws7-$(date -u +%Y%m%dT%H%M%SZ)"
echo "deploy: $KIND change set $NAME on $WS7_STACK (target $WANT_TARGET, schedules stay $WANT_STATE)"
cfn create-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" \
  --change-set-type "$KIND" --template-body "file://$TEMPLATE" \
  --capabilities CAPABILITY_NAMED_IAM --parameters "${PARAMS[@]}" >/dev/null \
  || stop "the change set could not be created"

discard() {
  cfn delete-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" >/dev/null 2>&1
  echo "The change set was not executed." >&2
}

if ! cfn wait change-set-create-complete --stack-name "$WS7_STACK" --change-set-name "$NAME"; then
  cfn describe-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" \
    --query 'StatusReason' --output text >&2
  discard
  stop "the change set was not created (if the reason above says it holds no changes, there is nothing to deploy)"
fi

cfn describe-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" \
  --query 'Changes[].ResourceChange.[Action,LogicalResourceId,ResourceType,Replacement]' \
  --output table || { discard; stop "the change set could not be read"; }

# The schedules' state: the parameter is what it was, and no schedule's State changes.
if ! GOT=$(cfn describe-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" \
    --query "Parameters[?ParameterKey=='ScheduleState'].ParameterValue | [0]" --output text) \
    || [ -z "$GOT" ]; then
  discard; stop "the change set's ScheduleState could not be read"
fi
if [ "$GOT" != "$WANT_STATE" ]; then
  discard; stop "this change set would set ScheduleState to $GOT. Only schedules.sh changes it."
fi
if ! GOT=$(cfn describe-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" \
    --query "length(Changes[?ResourceChange.ResourceType=='AWS::Scheduler::Schedule'].ResourceChange.Details[] | [?Target.Name=='State'])" \
    --output text) || [ -z "$GOT" ]; then
  discard; stop "the change set's schedule changes could not be read"
fi
if [ "$GOT" != "0" ]; then
  discard; stop "this change set would change a schedule's State. Only schedules.sh changes it."
fi
# The target: the change set carries the one this script meant, and says so.
if ! GOT=$(cfn describe-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" \
    --query "Parameters[?ParameterKey=='Target'].ParameterValue | [0]" --output text) \
    || [ -z "$GOT" ]; then
  discard; stop "the change set's Target could not be read"
fi
if [ "$GOT" != "$WANT_TARGET" ]; then
  discard; stop "this change set would set Target to $GOT, not $WANT_TARGET"
fi
echo "deploy: this change set's Target is $GOT"

printf 'Read the table above. Type the stack name to execute this change set: '
IFS= read -r answer || answer=""
if [ "$answer" != "$WS7_STACK" ]; then
  discard; stop "not confirmed"
fi

cfn execute-change-set --stack-name "$WS7_STACK" --change-set-name "$NAME" \
  || stop "the change set could not be executed"
if [ "$KIND" = CREATE ]; then
  cfn wait stack-create-complete --stack-name "$WS7_STACK" || stop "the stack did not finish creating"
else
  cfn wait stack-update-complete --stack-name "$WS7_STACK" || stop "the stack did not finish updating"
fi
cfn update-termination-protection --stack-name "$WS7_STACK" --enable-termination-protection \
  >/dev/null || stop "the stack is deployed, but termination protection could not be switched on"
cfn describe-stacks --stack-name "$WS7_STACK" --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
  --output table || stop "the stack is deployed, but its outputs could not be read"
echo "deploy: done. The target is $WANT_TARGET. The schedules are $WANT_STATE."
exit 0
