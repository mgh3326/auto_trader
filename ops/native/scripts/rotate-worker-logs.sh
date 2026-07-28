#!/usr/bin/env bash
set -euo pipefail

# ROB-1118: bounded launchd stdout/stderr rotation for the TaskIQ worker.
#
# launchd gives the worker an open descriptor for StandardOutPath and
# StandardErrorPath. Renaming the path while the worker is running leaves that
# descriptor attached to the old inode, so this script stops the launchd job,
# verifies that all descriptors are closed, rotates with newsyslog, and then
# restores the job.

BASE="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}"
LOG_DIR="${AUTO_TRADER_LOG_DIR:-$BASE/logs}"
ARM_FILE="${AUTO_TRADER_LOG_ROTATION_ARM_FILE:-$BASE/shared/log-rotation.enabled}"
MAX_BYTES="${AUTO_TRADER_LOG_MAX_BYTES:-134217728}"
ARCHIVE_COUNT="${AUTO_TRADER_LOG_ARCHIVE_COUNT:-4}"
WORKER_LABEL="${AUTO_TRADER_WORKER_LABEL:-com.robinco.auto-trader.worker}"
WORKER_PLIST="${AUTO_TRADER_WORKER_PLIST:-$HOME/Library/LaunchAgents/$WORKER_LABEL.plist}"
LAUNCHCTL_BIN="${AUTO_TRADER_LAUNCHCTL_BIN:-/bin/launchctl}"
NEWSYSLOG_BIN="${AUTO_TRADER_NEWSYSLOG_BIN:-/usr/sbin/newsyslog}"
LSOF_BIN="${AUTO_TRADER_LSOF_BIN:-/usr/sbin/lsof}"
FD_WAIT_ATTEMPTS="${AUTO_TRADER_LOG_FD_WAIT_ATTEMPTS:-40}"
FD_WAIT_SECONDS="${AUTO_TRADER_LOG_FD_WAIT_SECONDS:-0.25}"

case "$MAX_BYTES" in
  *[!0-9]*|"")
    echo "AUTO_TRADER_LOG_MAX_BYTES must be a positive integer" >&2
    exit 64
    ;;
esac
if (( MAX_BYTES < 1024 )); then
  echo "AUTO_TRADER_LOG_MAX_BYTES must be at least 1024" >&2
  exit 64
fi

case "$ARCHIVE_COUNT" in
  *[!0-9]*|"")
    echo "AUTO_TRADER_LOG_ARCHIVE_COUNT must be a positive integer" >&2
    exit 64
    ;;
esac
if (( ARCHIVE_COUNT < 1 )); then
  echo "AUTO_TRADER_LOG_ARCHIVE_COUNT must be at least 1" >&2
  exit 64
fi

case "$FD_WAIT_ATTEMPTS" in
  *[!0-9]*|"")
    echo "AUTO_TRADER_LOG_FD_WAIT_ATTEMPTS must be a positive integer" >&2
    exit 64
    ;;
esac
if (( FD_WAIT_ATTEMPTS < 1 )); then
  echo "AUTO_TRADER_LOG_FD_WAIT_ATTEMPTS must be at least 1" >&2
  exit 64
fi

# Deployment deliberately does not create this marker. The operator must first
# complete the legacy 25GB-file runbook and then arm future rotations.
if [[ ! -f "$ARM_FILE" ]]; then
  exit 0
fi

logs=(
  "$LOG_DIR/com.robinco.auto-trader.worker.err.log"
  "$LOG_DIR/com.robinco.auto-trader.worker.out.log"
)
rotate_logs=()

file_size() {
  local path="$1"
  if stat -f '%z' "$path" >/dev/null 2>&1; then
    stat -f '%z' "$path"
  else
    stat -c '%s' "$path"
  fi
}

for log_path in "${logs[@]}"; do
  if [[ -f "$log_path" ]] && (( $(file_size "$log_path") >= MAX_BYTES )); then
    rotate_logs+=("$log_path")
  fi
done

if (( ${#rotate_logs[@]} == 0 )); then
  exit 0
fi

for required in "$LAUNCHCTL_BIN" "$NEWSYSLOG_BIN" "$LSOF_BIN"; do
  if [[ ! -x "$required" ]]; then
    echo "required executable is missing: $required" >&2
    exit 69
  fi
done

uid_num="$(id -u)"
domain="gui/$uid_num"
service="$domain/$WORKER_LABEL"
worker_was_loaded=0
worker_stopped=0
config_file=""

restart_worker() {
  if (( worker_was_loaded == 0 || worker_stopped == 0 )); then
    return 0
  fi
  if [[ ! -f "$WORKER_PLIST" ]]; then
    echo "worker plist is missing; cannot restore service: $WORKER_PLIST" >&2
    return 78
  fi
  "$LAUNCHCTL_BIN" bootstrap "$domain" "$WORKER_PLIST" || return $?
  "$LAUNCHCTL_BIN" enable "$service" || return $?
  "$LAUNCHCTL_BIN" kickstart -k "$service" || return $?
  worker_stopped=0
}

cleanup() {
  local rc=$?
  local restart_rc=0
  set +e
  if [[ -n "$config_file" ]]; then
    rm -f "$config_file"
  fi
  restart_worker
  restart_rc=$?
  if (( rc == 0 && restart_rc != 0 )); then
    rc=$restart_rc
  fi
  exit "$rc"
}
trap cleanup EXIT

if "$LAUNCHCTL_BIN" print "$service" >/dev/null 2>&1; then
  worker_was_loaded=1
  "$LAUNCHCTL_BIN" bootout "$service"
  worker_stopped=1
fi

# bootout is synchronous for the launchd job, but descendants can take a
# moment to close. Never rotate until every writer has released the old inode.
for log_path in "${rotate_logs[@]}"; do
  holders=""
  for (( attempt = 1; attempt <= FD_WAIT_ATTEMPTS; attempt++ )); do
    holders="$("$LSOF_BIN" -t "$log_path" 2>/dev/null || true)"
    [[ -z "$holders" ]] && break
    sleep "$FD_WAIT_SECONDS"
  done
  if [[ -n "$holders" ]]; then
    echo "refusing rotation; open file descriptors remain for $log_path: $holders" >&2
    exit 75
  fi
done

mkdir -p "$BASE/run"
config_file="$(mktemp "$BASE/run/worker-newsyslog.XXXXXX")"
chmod 600 "$config_file"
owner="$(id -un)"
group="$(id -gn)"
max_kib=$(( (MAX_BYTES + 1023) / 1024 ))
# macOS newsyslog retains .0 plus the configured count (.1 .. .count).
# Convert the operator-facing maximum archive count to that field.
newsyslog_count=$(( ARCHIVE_COUNT - 1 ))

for log_path in "${rotate_logs[@]}"; do
  printf '%s %s:%s 640 %s %s * NZ\n' \
    "$log_path" "$owner" "$group" "$newsyslog_count" "$max_kib" >>"$config_file"
done

"$NEWSYSLOG_BIN" -r -s -f "$config_file" -R ROB-1118 "${rotate_logs[@]}"

shopt -s nullglob
for log_path in "${rotate_logs[@]}"; do
  new_size="$(file_size "$log_path")"
  if (( new_size >= MAX_BYTES )); then
    echo "rotation verification failed; current file is still oversized: $log_path ($new_size bytes)" >&2
    exit 70
  fi
  archives=("$log_path".[0-9]*)
  if (( ${#archives[@]} > ARCHIVE_COUNT )); then
    echo "rotation verification failed; archive count exceeds $ARCHIVE_COUNT for $log_path" >&2
    exit 70
  fi
done

restart_worker
