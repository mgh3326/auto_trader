#!/usr/bin/env bash
# Read-only one-shot witness. No broker, database, ports or long-lived PID change.
set -Eeuo pipefail
digest_file="${AT_RUN_DIRECTORY:-/root/at-run}/deployed-digest"
IFS= read -r image <"$digest_file"
[[ "$image" =~ ^ghcr\.io/mgh3326/auto_trader@sha256:[[:xdigit:]]{64}$ ]] || exit 78
host_pid_ns="$(stat -Lc %i /proc/1/ns/pid)"
[[ "$host_pid_ns" =~ ^[0-9]+$ ]] || exit 78
docker run --rm --pid=host --network none \
  -v /etc/machine-id:/etc/machine-id:ro \
  "$image" python -m scripts.nhplug_t14_host_witness \
  --host-pid-ns "$host_pid_ns" "$@"
