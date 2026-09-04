#!/usr/bin/env bash
# Run one statically-audited auto_trader CLI from the deployed image digest.
# This wrapper deliberately refuses a tag or a missing/foreign digest: jobs
# must never silently run a different revision from the serving deployment.
set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
readonly DEPLOYED_DIGEST_FILE="/root/at-run/deployed-digest"
# The production default is root-owned /run/lock.  The override is solely for
# hermetic tests and does not alter the lock key or enable concurrent runs.
readonly LOCK_DIRECTORY="${AT_JOB_LOCK_DIRECTORY:-/run/lock}"

usage() {
  printf 'usage: %s scripts.<module> [args...]\n' "$0" >&2
  exit 64
}

(( $# >= 1 )) || usage
module="$1"
shift
[[ "$module" =~ ^scripts\.[A-Za-z_][A-Za-z0-9_]*$ ]] || usage

case "$module" in
  scripts.build_investor_flow_snapshots) timeout_seconds=1800 ;;
  scripts.sync_toss_warnings) timeout_seconds=3600 ;;
  scripts.build_invest_screener_snapshots) timeout_seconds=7200 ;;
  *)
    printf 'at-job refuses an unaudited module: %s\n' "$module" >&2
    exit 64
    ;;
esac

unit="${module#scripts.}"
lock_file="${LOCK_DIRECTORY}/at-job-${unit}.lock"
mkdir -p "$LOCK_DIRECTORY"
exec 9>"$lock_file"
if ! flock -n 9; then
  printf '{"module":"%s","rc":75,"elapsed_s":0,"image_digest":"unresolved"}\n' "$module"
  exit 75
fi

image_digest=""
if [[ -f "$DEPLOYED_DIGEST_FILE" ]]; then
  IFS= read -r image_digest <"$DEPLOYED_DIGEST_FILE" || true
fi
if [[ ! "$image_digest" =~ ^${IMAGE_REPOSITORY}@sha256:[[:xdigit:]]{64}$ ]]; then
  printf 'at-job refuses unresolved or non-digest deployed image\n' >&2
  printf '{"module":"%s","rc":78,"elapsed_s":0,"image_digest":"unresolved"}\n' "$module"
  exit 78
fi
[[ -n "${AT_RUNTIME_ENV_FILE:-}" && -f "$AT_RUNTIME_ENV_FILE" ]] || {
  printf 'at-job requires AT_RUNTIME_ENV_FILE to name an existing environment file\n' >&2
  printf '{"module":"%s","rc":78,"elapsed_s":0,"image_digest":"%s"}\n' "$module" "$image_digest"
  exit 78
}

start_seconds="$SECONDS"
set +e
timeout --preserve-status "${timeout_seconds}s" \
  docker run --rm --network host --workdir /app \
  --env-file "$AT_RUNTIME_ENV_FILE" "$image_digest" \
  /app/.venv/bin/python -m "$module" "$@"
rc=$?
set -e
elapsed_seconds=$((SECONDS - start_seconds))

printf '{"module":"%s","rc":%d,"elapsed_s":%d,"image_digest":"%s"}\n' \
  "$module" "$rc" "$elapsed_seconds" "$image_digest"
exit "$rc"
