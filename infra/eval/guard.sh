# guard.sh - the identity and region check that comes before every AWS step
# (docs/WS7-RUNBOOK.md). Sourced, never run: by the scripts in this folder (bash) and
# by the runbook's blocks (zsh). It defines names and one function, and calls nothing.
#
#   ws7_guard <account>    returns 0 only when the credentials in use belong to the
#                          12-digit account the operator typed, and nothing in the
#                          shell points the AWS CLI at another region.
#
# It RETURNS; it never exits, so a failed check does not close the operator's shell.
# A script calls it as:   ws7_guard "$ACCOUNT" || exit 1
# A runbook block as:     ws7_guard <account> && { ... }
#
# The one AWS call it makes is sts:GetCallerIdentity, which is free and changes nothing.
# `aws` is called by name only: the tests put a stub first on PATH.

WS7_REGION=eu-west-2
WS7_STACK=discharge-eval
WS7_REPOSITORY=discharge-eval
WS7_CLUSTER=discharge-eval
WS7_SCHEDULE_GROUP=discharge-eval
WS7_ALARM_PREFIX=discharge-eval-
WS7_ALARM_COUNT=26

ws7_guard() {
  local want="$1" have arn name
  case "$want" in
    ''|*[!0-9]*) echo "STOP: give the 12-digit AWS account you mean to act on." >&2; return 1 ;;
  esac
  if [ "${#want}" -ne 12 ]; then
    echo "STOP: give the 12-digit AWS account you mean to act on." >&2; return 1
  fi
  # A shell set up for another region is a mistake to stop on, not one to correct.
  for name in "${AWS_REGION:-}" "${AWS_DEFAULT_REGION:-}"; do
    if [ -n "$name" ] && [ "$name" != "$WS7_REGION" ]; then
      echo "STOP: this shell is set for region $name. The eval stack is in $WS7_REGION only." >&2
      return 1
    fi
  done
  # An endpoint override would send the calls somewhere else.
  if [ -n "${AWS_ENDPOINT_URL:-}" ]; then
    echo "STOP: AWS_ENDPOINT_URL is set. Unset it first." >&2; return 1
  fi
  # Each command is checked before its output is used.
  if ! have=$(aws sts get-caller-identity --region "$WS7_REGION" --query Account --output text) \
      || [ -z "$have" ]; then
    echo "STOP: the caller's identity could not be read. Nothing was done." >&2; return 1
  fi
  if [ "$have" != "$want" ]; then
    echo "STOP: these credentials are for another account, not the one you named. Nothing was done." >&2
    return 1
  fi
  if ! arn=$(aws sts get-caller-identity --region "$WS7_REGION" --query Arn --output text) \
      || [ -z "$arn" ]; then
    echo "STOP: the caller's identity could not be read. Nothing was done." >&2; return 1
  fi
  export AWS_REGION="$WS7_REGION" AWS_DEFAULT_REGION="$WS7_REGION" AWS_PAGER=""
  echo "guard: account $have, region $WS7_REGION, as $arn"
  return 0
}
