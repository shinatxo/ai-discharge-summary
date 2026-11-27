#!/bin/bash
# check5.sh - part 6's check 5, THE CONTROL for what an image holds
# (docs/WS7-ENTRYPOINT.md, "Part 6: the image", "Building and checking it").
#
# Sourced by push-image.sh. It defines CHECK5_SCRIPT and check5 and does nothing else.
# The text between the two marker lines below is byte for byte the text in that guide's
# block; tests/test_eval_scripts.py holds the two together, so there is one check 5.
#
# check5 <image>: status 0 only when the in-image script found neither protected
# folder (evals/runs, evals/calibration, docs, infra) under any spelling, and the
# image root holds exactly the five names. THE STATUS IS THE RESULT. It reflects what
# the script found in the image's file system; it does not show that any line was
# printed, and a run that fails part-way can leave some lines behind. A caller goes
# by the status alone.

# --- begin part 6 check 5 ---
CHECK5_SCRIPT='
LC_ALL=C; export LC_ALL
root=$1; bad=0; absent=0

plain() {   # is $1 made only of bytes from space to tilde?
  case "$1" in *[!\ -~]*) return 1 ;; esac
  return 0
}
selftest() {   # the controls: this shell matches names as the checks below need
  case "RuNs" in [Rr][Uu][Nn][Ss]) ;; *) return 1 ;; esac
  case "runsx" in [Rr][Uu][Nn][Ss]) return 1 ;; esac
  if ! plain "requirements-eval.txt ~"; then return 1; fi
  if plain "runſ"; then return 1; fi
  return 0
}
if ! selftest; then
  echo "STOP: this shell does not match names as check 5 needs"; exit 1
fi

dir=$root/evals
if ! ls -A "$dir" >/dev/null 2>&1; then
  bad=1; echo "STOP: cannot list evals/"
else
  seen=0; odd=0; runs=0; calibration=0
  for p in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
    if [ ! -e "$p" ] && [ ! -L "$p" ]; then continue; fi
    seen=1; name=${p##*/}
    if ! plain "$name"; then bad=1; odd=1; continue; fi
    case "$name" in
      [Rr][Uu][Nn][Ss])
        bad=1; runs=1; echo "PRESENT evals/$name" ;;
      [Cc][Aa][Ll][Ii][Bb][Rr][Aa][Tt][Ii][Oo][Nn])
        bad=1; calibration=1; echo "PRESENT evals/$name" ;;
    esac
  done
  if [ "$runs" = 0 ]; then absent=$((absent + 1)); echo "absent evals/runs"; fi
  if [ "$calibration" = 0 ]; then absent=$((absent + 1)); echo "absent evals/calibration"; fi
  if [ "$odd" != 0 ]; then bad=1; echo "STOP: a name in evals/ is not plain ASCII"; fi
  if [ "$seen" != 1 ]; then bad=1; echo "STOP: evals/ lists no entries"; fi
fi

dir=$root
if ! ls -A "$dir" >/dev/null 2>&1; then
  bad=1; echo "STOP: cannot list the image root"
else
  odd=0; docs=0; infra=0
  for p in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
    if [ ! -e "$p" ] && [ ! -L "$p" ]; then continue; fi
    name=${p##*/}
    if ! plain "$name"; then bad=1; odd=1; continue; fi
    case "$name" in
      [Dd][Oo][Cc][Ss])
        bad=1; docs=1; echo "PRESENT $name" ;;
      [Ii][Nn][Ff][Rr][Aa])
        bad=1; infra=1; echo "PRESENT $name" ;;
    esac
  done
  if [ "$docs" = 0 ]; then absent=$((absent + 1)); echo "absent docs"; fi
  if [ "$infra" = 0 ]; then absent=$((absent + 1)); echo "absent infra"; fi
  if [ "$odd" != 0 ]; then bad=1; echo "STOP: a name in the image root is not plain ASCII"; fi
fi

count=0; other=0
for p in "$root"/* "$root"/.[!.]* "$root"/..?*; do
  if [ ! -e "$p" ] && [ ! -L "$p" ]; then continue; fi
  name=${p##*/}; printf "%s\n" "$name"
  case "$name" in
    evals|prompts|requirements-eval.txt|src|tests) count=$((count + 1)) ;;
    *) other=1 ;;
  esac
done
if [ "$count" != 5 ] || [ "$other" != 0 ]; then
  bad=1; echo "STOP: the image root does not hold exactly the five names"
fi

# A pass is counted, not assumed: nothing bad, four absent lines, the five names.
if [ "$bad" = 0 ] && [ "$absent" = 4 ] && [ "$count" = 5 ] && [ "$other" = 0 ]; then
  exit 0
fi
exit 1
'
check5() {   # check5 <image>: returns 0 only on a pass. It returns; it never exits the shell.
  local id
  if ! id=$(docker image inspect --format '{{.Id}}' "$1" 2>/dev/null) || [ -z "$id" ]; then
    echo "STOP: there is no image $1 to check."; return 1
  fi
  echo "image $id"
  docker run --rm --network none "$id" sh -c "$CHECK5_SCRIPT" check5 /app
}
# --- end part 6 check 5 ---
