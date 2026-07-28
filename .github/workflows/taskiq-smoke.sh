#!/usr/bin/env bash

set -euo pipefail

usage() {
  echo "usage: $0 LABEL READY_MARKER LOG_PATH -- COMMAND [ARG ...]" >&2
  exit 2
}

if [[ $# -lt 5 || "$4" != "--" ]]; then
  usage
fi

label=$1
ready_marker=$2
log_path=$3
shift 4

readiness_polls=${TASKIQ_READINESS_POLLS:-60}
readiness_interval=${TASKIQ_READINESS_INTERVAL:-0.5}
term_grace_polls=${TASKIQ_TERM_GRACE_POLLS:-20}
term_grace_interval=${TASKIQ_TERM_GRACE_INTERVAL:-0.25}

terminate_and_reap() {
  local process_id=$1
  local status=0

  if kill -0 "$process_id" 2>/dev/null; then
    kill -TERM "$process_id" 2>/dev/null || true
    for ((poll = 1; poll <= term_grace_polls; poll++)); do
      if ! kill -0 "$process_id" 2>/dev/null; then
        wait "$process_id" || status=$?
        return 0
      fi
      sleep "$term_grace_interval"
    done

    echo "$label ignored TERM for ${term_grace_polls} grace polls; sending KILL."
    kill -KILL "$process_id" 2>/dev/null || true
  fi

  wait "$process_id" || status=$?
  return 0
}

"$@" > "$log_path" 2>&1 &
process_id=$!

for ((poll = 1; poll <= readiness_polls; poll++)); do
  if grep -Fq "$ready_marker" "$log_path"; then
    terminate_and_reap "$process_id"
    exit 0
  fi

  if ! kill -0 "$process_id" 2>/dev/null; then
    status=0
    wait "$process_id" || status=$?
    echo "$label exited before readiness (status $status)."
    tail -n 200 "$log_path" || true
    exit 1
  fi

  sleep "$readiness_interval"
done

terminate_and_reap "$process_id"
echo "$label did not become ready within ${readiness_polls} readiness polls."
tail -n 200 "$log_path" || true
exit 1
