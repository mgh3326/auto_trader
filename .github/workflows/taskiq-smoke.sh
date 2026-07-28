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

process_id=
process_group_id=
child_reaped=false
child_status=0
shutdown_requested=false
term_sent=false
kill_sent=false

leader_is_running() {
  jobs -pr | grep -Fxq "$process_id"
}

process_group_exists() {
  [[ -n "$process_group_id" ]] &&
    kill -0 -- "-$process_group_id" 2>/dev/null
}

reap_child() {
  if [[ "$child_reaped" == false ]]; then
    child_status=0
    wait "$process_id" || child_status=$?
    child_reaped=true
  fi
}

terminate_and_reap() {
  local poll

  if [[ "$child_reaped" == false ]] && leader_is_running; then
    shutdown_requested=true
  fi

  if process_group_exists; then
    if kill -TERM -- "-$process_group_id" 2>/dev/null; then
      term_sent=true
    fi
  fi

  for ((poll = 1; poll <= term_grace_polls; poll++)); do
    if [[ "$child_reaped" == false ]] && ! leader_is_running; then
      reap_child
    fi
    if ! process_group_exists; then
      return 0
    fi
    sleep "$term_grace_interval"
  done

  if process_group_exists; then
    echo "$label ignored TERM for ${term_grace_polls} grace polls; sending KILL."
    if kill -KILL -- "-$process_group_id" 2>/dev/null; then
      kill_sent=true
    fi
  fi

  reap_child

  for ((poll = 1; poll <= term_grace_polls; poll++)); do
    if ! process_group_exists; then
      break
    fi
    sleep "$term_grace_interval"
  done

  return 0
}

# shellcheck disable=SC2329  # Invoked by the EXIT trap.
cleanup_on_exit() {
  local status=$?

  trap - EXIT INT TERM
  if [[ -n "$process_id" ]]; then
    terminate_and_reap
  fi
  exit "$status"
}

# shellcheck disable=SC2329  # Invoked by the INT/TERM traps.
exit_on_signal() {
  exit "$1"
}

trap cleanup_on_exit EXIT
trap 'exit_on_signal 130' INT
trap 'exit_on_signal 143' TERM

if command -v setsid >/dev/null 2>&1; then
  setsid "$@" > "$log_path" 2>&1 &
else
  python3 -c \
    'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
    "$@" > "$log_path" 2>&1 &
fi
process_id=$!
process_group_id=$process_id

finish_after_readiness() {
  terminate_and_reap
  if ((child_status == 0)) ||
    { [[ "$shutdown_requested" == true && "$term_sent" == true ]] &&
      ((child_status == 143)); } ||
    { [[ "$shutdown_requested" == true && "$kill_sent" == true ]] &&
      ((child_status == 137)); }; then
    exit 0
  fi

  echo "$label exited after readiness (status $child_status)."
  tail -n 200 "$log_path" || true
  exit "$child_status"
}

for ((poll = 1; poll <= readiness_polls; poll++)); do
  if grep -Fq "$ready_marker" "$log_path"; then
    finish_after_readiness
  fi

  if ! leader_is_running; then
    terminate_and_reap
    if grep -Fq "$ready_marker" "$log_path"; then
      finish_after_readiness
    fi
    echo "$label exited before readiness (status $child_status)."
    tail -n 200 "$log_path" || true
    exit 1
  fi

  sleep "$readiness_interval"
done

terminate_and_reap
echo "$label did not become ready within ${readiness_polls} readiness polls."
tail -n 200 "$log_path" || true
exit 1
