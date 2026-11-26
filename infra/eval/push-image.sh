#!/bin/bash
# push-image.sh - build the eval image from a merged commit, check it, and push it.
# (ADR-011 D11; docs/WS7-ENTRYPOINT.md, "Part 6: the image"; docs/WS7-RUNBOOK.md.)
#
#   infra/eval/push-image.sh --account <12 digits> --commit <40 hex>
#   infra/eval/push-image.sh --check-only --commit <40 hex>     (no AWS call, no push)
#
# THE RULE: no image is pushed unless check 5 has passed on that same image, identified
# by its image ID, after its last build. This script builds once, takes the image ID,
# runs every check against that ID, runs check 5 last and goes by its STATUS, and
# pushes that ID. After the push it reads the digest back from ECR: unless it is the
# image ID, the script says so and prints no digest to deploy.
#
# WHAT IT BUILDS FROM. Never the local checkout. It clones the repository from GitHub
# into a temporary folder, with this machine's git configuration left out
# (GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM=1), and refuses unless the commit
# is feat/agentic-pipeline as fetched, or an ancestor of it: an image is only ever of
# a reviewed, merged commit. It exports only the paths the image is built from, never
# evals/runs or evals/calibration, and then checks the export against the commit:
# the same set of files, and each file's bytes hash to the blob the commit names. So
# the commit label is true of every copied file.
#
# The temporary folder is removed on every exit.
#
# COST: an image in ECR is about 2 pence a month (unchecked). The build and the checks
# are local. The AWS calls are sts:GetCallerIdentity, ecr:DescribeRepositories,
# ecr:GetAuthorizationToken, the push, and ecr:DescribeImages.
#
# Every command is checked before its output is compared: a command that fails is a
# STOP, never a match. docker, aws and git are called by name only.

set -u

REMOTE=https://github.com/shinatxo/ai-discharge-summary.git
BRANCH=feat/agentic-pipeline
HERE=$(cd "$(dirname "$0")" && pwd) || exit 1
# shellcheck source=guard.sh
. "$HERE/guard.sh"
# shellcheck source=check5.sh
. "$HERE/check5.sh"

WORK=""
cleanup() {
  if [ -n "$WORK" ] && [ -d "$WORK" ]; then
    chmod -R u+w "$WORK" 2>/dev/null
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

stop() {
  echo "STOP: $*" >&2
  echo "Nothing was pushed." >&2
  exit 1
}

# ----------------------------------------------------------------- arguments

ACCOUNT="" C="" CHECK_ONLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --account) [ "$#" -ge 2 ] || stop "--account needs a value"; ACCOUNT="$2"; shift 2 ;;
    --commit) [ "$#" -ge 2 ] || stop "--commit needs a value"; C="$2"; shift 2 ;;
    --check-only) CHECK_ONLY=1; shift ;;
    *) stop "usage: push-image.sh --account <12 digits> --commit <40 hex>   |   push-image.sh --check-only --commit <40 hex>" ;;
  esac
done
case "$C" in
  ''|*[!0-9a-f]*) stop "--commit must be the full 40-character commit, in lower-case hex" ;;
esac
[ "${#C}" -eq 40 ] || stop "--commit must be the full 40-character commit, in lower-case hex"
if [ "$CHECK_ONLY" -eq 1 ]; then
  [ -z "$ACCOUNT" ] || stop "--check-only makes no AWS call: give it no --account"
else
  ws7_guard "$ACCOUNT" || stop "the identity and region check failed"
fi

# ------------------------------------------------- the commit, from GitHub

# This machine's git configuration is left out, and nothing in the environment may
# point git at another repository, index or configuration.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
  GIT_NAMESPACE GIT_CONFIG GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT GIT_CEILING_DIRECTORIES \
  GIT_TEMPLATE_DIR GIT_EXEC_PATH
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_ATTR_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0

if ! WORK=$(mktemp -d "${TMPDIR:-/tmp}/ws7-push.XXXXXXXX") || [ -z "$WORK" ] || [ ! -d "$WORK" ]; then
  WORK=""; stop "no temporary folder could be made"
fi
REPO="$WORK/repo.git" EXPORT="$WORK/export"
mkdir "$EXPORT" || stop "the export folder could not be made"

echo "push: cloning $BRANCH from $REMOTE"
git clone --quiet --bare --single-branch --branch "$BRANCH" "$REMOTE" "$REPO" \
  || stop "the clone from GitHub failed"

if ! TIP=$(git --git-dir="$REPO" rev-parse --verify --quiet "refs/heads/$BRANCH^{commit}") \
    || [ -z "$TIP" ]; then
  stop "$BRANCH was not found in the clone"
fi
# $C must resolve to a commit (a tree's or a blob's SHA does not), and to itself.
if ! GOT=$(git --git-dir="$REPO" rev-parse --verify --quiet "$C^{commit}") || [ -z "$GOT" ]; then
  stop "$C is not a commit on $BRANCH as fetched"
fi
[ "$GOT" = "$C" ] || stop "$C does not name a commit by its own SHA"
# A reviewed, merged commit: the branch as fetched, or an ancestor of it.
git --git-dir="$REPO" merge-base --is-ancestor "$C" "$TIP" \
  || stop "$C is not $BRANCH as fetched, nor an ancestor of it"
echo "push: commit $C ($BRANCH is at $TIP)"

# ------------------------------------------------------------- the export

# What the image is built from. evals/runs and evals/calibration are never exported.
LIST="$WORK/tree.txt"
if ! git --git-dir="$REPO" ls-tree -r --full-tree "$C" -- \
    Dockerfile .dockerignore requirements-eval.txt src prompts evals tests >"$LIST"; then
  stop "the commit's tree could not be listed"
fi
[ -s "$LIST" ] || stop "the commit's tree lists none of the image's paths"

WANT="$WORK/want.txt"
: >"$WANT" || stop "the temporary folder cannot be written"
TAB=$(printf '\t')
count=0
while IFS= read -r line || [ -n "$line" ]; do
  meta=${line%%"$TAB"*}
  path=${line#*"$TAB"}
  [ "$meta" != "$line" ] || stop "a line of the commit's tree could not be read"
  # A name that is not plain ASCII is refused, as check 5 refuses one in the image.
  case "$path" in
    *[!\ -~]*|\"*) stop "a tracked path is not plain ASCII; it is not exported" ;;
  esac
  # The two protected folders, under any spelling, are never exported.
  case "$path" in
    evals/runs/*|evals/calibration/*) continue ;;
    [Ee][Vv][Aa][Ll][Ss]/[Rr][Uu][Nn][Ss]/*|[Ee][Vv][Aa][Ll][Ss]/[Cc][Aa][Ll][Ii][Bb][Rr][Aa][Tt][Ii][Oo][Nn]/*)
      stop "the commit holds a protected folder under another spelling: $path" ;;
  esac
  set -- $meta
  [ "$#" -eq 3 ] || stop "a line of the commit's tree could not be read"
  mode=$1 kind=$2 sha=$3
  [ "$kind" = blob ] || stop "the commit holds something that is not a file: $path"
  case "$mode" in
    100644|100755) ;;
    *) stop "the commit holds a link or an unusual file: $path" ;;
  esac
  printf '%s %s\n' "$sha" "$path" >>"$WANT" || stop "the temporary folder cannot be written"
  count=$((count + 1))
done <"$LIST"
[ "$count" -gt 0 ] || stop "the commit holds none of the image's files"

git --git-dir="$REPO" archive --format=tar "$C" -- \
  Dockerfile .dockerignore requirements-eval.txt src prompts evals tests \
  ':(exclude)evals/runs' ':(exclude)evals/calibration' >"$WORK/export.tar" \
  || stop "the commit could not be exported"
tar -x -f "$WORK/export.tar" -C "$EXPORT" || stop "the export could not be unpacked"

# The export against the commit, both ways. First: every file the commit names is
# there, is a plain file, and its raw bytes hash to the blob the commit names.
while IFS= read -r line || [ -n "$line" ]; do
  sha=${line%% *}
  path=${line#* }
  file="$EXPORT/$path"
  if [ -L "$file" ] || [ ! -f "$file" ]; then
    stop "the export is missing a file of the commit: $path"
  fi
  if ! have=$(git --git-dir="$REPO" hash-object --no-filters -- "$file") || [ -z "$have" ]; then
    stop "a file of the export could not be hashed: $path"
  fi
  [ "$have" = "$sha" ] || stop "the export's bytes are not the commit's: $path"
done <"$WANT"
# Second: the export holds nothing else. Every entry that is not a folder is counted.
if ! found=$(find "$EXPORT" -mindepth 1 ! -type d -print | wc -l) || [ -z "$found" ]; then
  stop "the export could not be listed"
fi
found=$((found + 0))
[ "$found" -eq "$count" ] || stop "the export holds $found files and the commit names $count"
if [ -e "$EXPORT/evals/runs" ] || [ -L "$EXPORT/evals/runs" ] \
    || [ -e "$EXPORT/evals/calibration" ] || [ -L "$EXPORT/evals/calibration" ]; then
  stop "the export holds a protected folder"
fi
# The builder reads Dockerfile.dockerignore INSTEAD OF .dockerignore when it exists.
if [ -e "$EXPORT/Dockerfile.dockerignore" ] || [ -L "$EXPORT/Dockerfile.dockerignore" ]; then
  stop "the export holds a Dockerfile.dockerignore"
fi
echo "push: the export is the commit's $count files, byte for byte"

# --------------------------------------------------------------- the build

LOCAL="discharge-eval:push-$(printf '%s' "$C" | cut -c1-12)"
docker build --platform linux/arm64 --build-arg "WS7_GIT_COMMIT=$C" -t "$LOCAL" "$EXPORT" \
  || stop "the build failed"
if ! ID=$(docker image inspect --format '{{.Id}}' "$LOCAL") || [ -z "$ID" ]; then
  stop "the built image's ID could not be read"
fi
hex=${ID#sha256:}
case "$hex" in
  ''|*[!0-9a-f]*) stop "the built image's ID is not a sha256 digest" ;;
esac
if [ "$hex" = "$ID" ] || [ "${#hex}" -ne 64 ]; then
  stop "the built image's ID is not a sha256 digest"
fi
echo "push: built image $ID"
# From here on every command names the image by its ID, never by a tag.

# ------------------------------------------------------ part 6's checks, by ID

# Check 3. The default command, offline: the nine WS7 test files pass, none skipped.
OUT="$WORK/tests.txt"
docker run --rm --network none "$ID" >"$OUT" 2>&1
status=$?
tail -n 3 "$OUT"
[ "$status" -eq 0 ] || stop "check 3: the image's own tests did not pass (status $status)"
if ! last=$(tail -n 1 "$OUT") || [ -z "$last" ]; then
  stop "check 3: the tests printed no summary"
fi
case "$last" in
  *skipped*|*xfailed*|*xpassed*|*deselected*|*error*|*failed*)
    stop "check 3: the tests' summary is not all passed" ;;
  *[0-9]\ passed*) ;;
  *) stop "check 3: the tests printed no summary of passes" ;;
esac

# Check 4. The commit in the image is the commit built from.
if ! GOT=$(docker run --rm --network none "$ID" printenv WS7_GIT_COMMIT) || [ -z "$GOT" ]; then
  stop "check 4: the image's WS7_GIT_COMMIT could not be read"
fi
[ "$GOT" = "$C" ] || stop "check 4: the image names another commit"

# Check 6. No arguments: a usage error, exit code 2, nothing built or sent.
docker run --rm --network none "$ID" python evals/ws7_entry.py >/dev/null 2>&1
status=$?
[ "$status" -eq 2 ] || stop "check 6: the entrypoint with no arguments ended with status $status, not 2"

# Check 8. identity() inside the image, with an invented digest, gives the commit.
INVENTED=sha256:0000000000000000000000000000000000000000000000000000000000000000
if ! GOT=$(docker run --rm --network none -e "WS7_IMAGE_DIGEST=$INVENTED" "$ID" python -c '
import os, sys
sys.path.insert(0, "evals")
import ws7_manifest as wm
print(wm.identity(os.environ).git_commit)') || [ -z "$GOT" ]; then
  stop "check 8: identity() did not run in the image"
fi
[ "$GOT" = "$C" ] || stop "check 8: identity() names another commit"

# Check 9. The architecture and the user.
if ! GOT=$(docker image inspect --format '{{.Architecture}} {{.Os}} {{.Config.User}}' "$ID") \
    || [ -z "$GOT" ]; then
  stop "check 9: the image could not be inspected"
fi
[ "$GOT" = "arm64 linux app" ] || stop "check 9: the image is not arm64 linux run as app"

# Check 10. The image's environment, by name: the commit is there once; nothing that
# belongs to the task definition, and no credential.
ENVS="$WORK/env.txt"
docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$ID" >"$ENVS" \
  || stop "check 10: the image's environment could not be read"
commits=0
while IFS= read -r line || [ -n "$line" ]; do
  name=${line%%=*}
  case "$name" in
    WS7_GIT_COMMIT) commits=$((commits + 1)) ;;
    AWS_*|WS7_*|PYTHONHASHSEED) stop "check 10: the image sets a variable it must not" ;;
  esac
done <"$ENVS"
[ "$commits" -eq 1 ] || stop "check 10: the image does not set WS7_GIT_COMMIT exactly once"

# Check 5, LAST: THE CONTROL. By status alone.
check5 "$ID"
status=$?
[ "$status" -eq 0 ] || stop "check 5 did not pass on $ID (status $status)"
echo "push: every check passed on $ID"

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "check-only: built and checked $ID from commit $C. Nothing was pushed."
  exit 0
fi

# ---------------------------------------------------------------- the push

REGISTRY="$ACCOUNT.dkr.ecr.$WS7_REGION.amazonaws.com"
URI="$REGISTRY/$WS7_REPOSITORY"
if ! GOT=$(aws ecr describe-repositories --region "$WS7_REGION" --registry-id "$ACCOUNT" \
    --repository-names "$WS7_REPOSITORY" --query 'repositories[0].repositoryUri' --output text) \
    || [ -z "$GOT" ]; then
  stop "the eval repository could not be read: is the stack deployed?"
fi
[ "$GOT" = "$URI" ] || stop "the eval repository is not where this script expects it"

PASSWORD="$WORK/ecr-password"
( umask 077 && aws ecr get-login-password --region "$WS7_REGION" >"$PASSWORD" ) \
  || stop "the ECR login password could not be read"
[ -s "$PASSWORD" ] || stop "the ECR login password could not be read"
docker login --username AWS --password-stdin "$REGISTRY" <"$PASSWORD" >/dev/null \
  || stop "the login to ECR failed"
rm -f "$PASSWORD"

TAG="$C-$(printf '%s' "$hex" | cut -c1-12)"
docker tag "$ID" "$URI:$TAG" || stop "the image could not be tagged"
# The tag must still be the image the checks ran on, immediately before the push.
if ! GOT=$(docker image inspect --format '{{.Id}}' "$URI:$TAG") || [ -z "$GOT" ]; then
  stop "the tagged image could not be read"
fi
[ "$GOT" = "$ID" ] || stop "the tag does not name the image the checks ran on"

docker push "$URI:$TAG" || stop "the push failed"

# What ECR holds under that tag must be the image ID check 5 ran on.
if ! PUSHED=$(aws ecr describe-images --region "$WS7_REGION" --registry-id "$ACCOUNT" \
    --repository-name "$WS7_REPOSITORY" --image-ids "imageTag=$TAG" \
    --query 'imageDetails[0].imageDigest' --output text) || [ -z "$PUSHED" ]; then
  echo "STOP: the image was pushed as $TAG, but its digest could not be read back from ECR." >&2
  echo "DO NOT DEPLOY IT. No digest is given." >&2
  exit 1
fi
if [ "$PUSHED" != "$ID" ]; then
  echo "STOP: the image was pushed as $TAG, but ECR's digest is not the image ID check 5 ran on." >&2
  echo "DO NOT DEPLOY IT. No digest is given." >&2
  exit 1
fi

echo "PUSHED $URI@$ID"
echo "commit $C"
echo "tag    $TAG"
echo "digest $ID   (the ImageDigest for infra/eval/deploy.sh)"
exit 0
