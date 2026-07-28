#!/usr/bin/env bash

set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
helper="$script_dir/taskiq-smoke.sh"
test_dir=$(mktemp -d "${TMPDIR:-/tmp}/taskiq-smoke-test.XXXXXX")

cleanup() {
  rm -rf "$test_dir"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

run_case() {
  local name=$1
  local expected_status=$2
  shift 2

  local output_path="$test_dir/$name.output"
  local log_path="$test_dir/$name.log"
  local actual_status=0

  TASKIQ_READINESS_POLLS=3 \
    TASKIQ_READINESS_INTERVAL=0.05 \
    TASKIQ_TERM_GRACE_POLLS=3 \
    TASKIQ_TERM_GRACE_INTERVAL=0.05 \
    bash "$helper" "$name" READY "$log_path" -- "$@" \
    > "$output_path" 2>&1 || actual_status=$?

  if [[ "$actual_status" -ne "$expected_status" ]]; then
    cat "$output_path" >&2
    fail "$name: expected exit $expected_status, got $actual_status"
  fi

  echo "PASS: $name (exit $actual_status)"
}

assert_processes_gone() {
  local pid_path=$1
  local process_group_id
  local pid
  local poll

  IFS= read -r process_group_id < "$pid_path"
  [[ -n "$process_group_id" ]] || fail "missing process-group leader PID: $pid_path"

  for ((poll = 1; poll <= 20; poll++)); do
    local found=false
    if kill -0 -- "-$process_group_id" 2>/dev/null; then
      found=true
    fi
    while IFS= read -r pid; do
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        found=true
      fi
    done < "$pid_path"
    if [[ "$found" == false ]]; then
      return 0
    fi
    sleep 0.05
  done

  fail "process group $process_group_id or recorded processes remain after helper cleanup: $(tr '\n' ' ' < "$pid_path")"
}

run_case early-exit 1 bash -c 'exit 23'
run_case readiness-timeout 1 bash -c 'trap "exit 0" TERM; while :; do sleep 1; done'
run_case normal-shutdown 0 bash -c \
  'trap "exit 0" TERM; printf "READY\n"; while :; do sleep 1; done'

term_ignore_pids="$test_dir/term-ignore.pids"
# shellcheck disable=SC2016  # The nested bash processes expand these expressions.
run_case term-ignore 0 bash -c \
  'trap "" TERM; printf "%s\n" "$$" > "$1"; bash -c '"'"'trap "" TERM; printf "%s\n" "$$" >> "$1"; while :; do sleep 1; done'"'"' _ "$1" & while [[ $(wc -l < "$1") -lt 2 ]]; do sleep 0.01; done; printf "READY\n"; wait' \
  _ "$term_ignore_pids"
assert_processes_gone "$term_ignore_pids"

post_kill_pids="$test_dir/post-kill-remains.pids"
post_kill_output="$test_dir/post-kill-remains.output"
post_kill_log="$test_dir/post-kill-remains.log"
post_kill_bash_env="$test_dir/post-kill-remains.bash-env"
post_kill_status=0
# The helper still sends a real KILL. This helper-only Bash environment forces
# subsequent group probes to report the group as present, deterministically
# exercising the post-KILL fail-closed path without a production test hook.
cat > "$post_kill_bash_env" <<'BASH_ENV_EOF'
if [[ "${TASKIQ_TEST_INSTALL_KILL_WRAPPER:-0}" == 1 ]]; then
  unset TASKIQ_TEST_INSTALL_KILL_WRAPPER
  kill() {
    local status=0
    if [[ "${1:-}" == "-KILL" ]]; then
      builtin kill "$@" || status=$?
      TASKIQ_TEST_KILL_SEEN=1
      return "$status"
    fi
    if [[ "${1:-}" == "-0" && "${TASKIQ_TEST_KILL_SEEN:-0}" == 1 ]]; then
      return 0
    fi
    builtin kill "$@"
  }
fi
BASH_ENV_EOF
# shellcheck disable=SC2016  # The nested bash processes expand these expressions.
BASH_ENV="$post_kill_bash_env" \
  TASKIQ_TEST_INSTALL_KILL_WRAPPER=1 \
  TASKIQ_READINESS_POLLS=20 \
  TASKIQ_READINESS_INTERVAL=0.05 \
  TASKIQ_TERM_GRACE_POLLS=0 \
  TASKIQ_TERM_GRACE_INTERVAL=0.05 \
  bash "$helper" post-kill-remains READY "$post_kill_log" -- bash -c \
  'if declare -F kill >/dev/null; then printf "HOOK_LEAKED\n"; exit 88; fi; trap "" TERM; printf "%s\n" "$$" > "$1"; bash -c '\''trap "" TERM; printf "%s\n" "$$" >> "$1"; while :; do sleep 1; done'\'' _ "$1" & while [[ $(wc -l < "$1") -lt 2 ]]; do sleep 0.01; done; printf "READY\n"; wait' \
  _ "$post_kill_pids" > "$post_kill_output" 2>&1 || post_kill_status=$?
if grep -Fq "HOOK_LEAKED" "$post_kill_log"; then
  cat "$post_kill_log" >&2
  fail "post-kill-remains: helper probe leaked into managed workload"
fi
[[ "$post_kill_status" -eq 1 ]] || {
  cat "$post_kill_output" >&2
  fail "post-kill-remains: expected helper exit 1, got $post_kill_status"
}
grep -Fq "remains after KILL fallback" "$post_kill_output" || {
  cat "$post_kill_output" >&2
  fail "post-kill-remains: missing process-group diagnostic"
}
assert_processes_gone "$post_kill_pids"
echo "PASS: post-kill-remains (exit $post_kill_status; diagnostic emitted)"

run_case ready-then-exit-42 42 bash -c 'printf "READY\n"; exit 42'

trap_pids="$test_dir/trap.pids"
trap_output="$test_dir/trap.output"
trap_log="$test_dir/trap.log"
# shellcheck disable=SC2016  # The nested bash processes expand these expressions.
TASKIQ_READINESS_POLLS=100 \
  TASKIQ_READINESS_INTERVAL=0.05 \
  TASKIQ_TERM_GRACE_POLLS=3 \
  TASKIQ_TERM_GRACE_INTERVAL=0.05 \
  bash "$helper" trap-cleanup NEVER "$trap_log" -- bash -c \
  'trap "" TERM; printf "%s\n" "$$" > "$1"; bash -c '"'"'trap "" TERM; printf "%s\n" "$$" >> "$1"; while :; do sleep 1; done'"'"' _ "$1" & wait' \
  _ "$trap_pids" > "$trap_output" 2>&1 &
helper_pid=$!

for ((poll = 1; poll <= 20; poll++)); do
  if [[ -s "$trap_pids" ]] && [[ $(wc -l < "$trap_pids") -ge 2 ]]; then
    break
  fi
  sleep 0.05
done
trap_pid_count=0
if [[ -f "$trap_pids" ]]; then
  trap_pid_count=$(wc -l < "$trap_pids")
fi
((trap_pid_count >= 2)) ||
  fail "trap-cleanup: expected leader and descendant PIDs, got $trap_pid_count"

kill -TERM "$helper_pid"
trap_status=0
wait "$helper_pid" || trap_status=$?
[[ "$trap_status" -eq 143 ]] ||
  fail "trap-cleanup: expected helper exit 143, got $trap_status"
assert_processes_gone "$trap_pids"
echo "PASS: trap-cleanup (exit $trap_status; process group reaped)"
