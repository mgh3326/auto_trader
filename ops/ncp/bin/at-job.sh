#!/usr/bin/env bash
# Run one statically-audited auto_trader CLI from the image used by at-api.
# This wrapper deliberately refuses a tag or a missing/foreign digest: jobs
# must never silently run a different revision from the serving deployment.
set -Eeuo pipefail

readonly API_CONTAINER="at-api"
readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
readonly API_ENV_FILE="/root/at-secrets/.env.api"
readonly SCHEDULER_ENV_FILE="/root/at-secrets/.env.scheduler"
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

image_digest="$(docker inspect --format '{{.Config.Image}}' "$API_CONTAINER" 2>/dev/null || true)"
if [[ ! "$image_digest" =~ ^${IMAGE_REPOSITORY}@sha256:[[:xdigit:]]{64}$ ]]; then
  printf 'at-job refuses unresolved or non-digest at-api image\n' >&2
  printf '{"module":"%s","rc":78,"elapsed_s":0,"image_digest":"unresolved"}\n' "$module"
  exit 78
fi
[[ -f "$API_ENV_FILE" && -f "$SCHEDULER_ENV_FILE" ]] || {
  printf 'at-job requires configured environment files\n' >&2
  printf '{"module":"%s","rc":78,"elapsed_s":0,"image_digest":"%s"}\n' "$module" "$image_digest"
  exit 78
}

start_seconds="$SECONDS"
set +e
timeout --preserve-status "${timeout_seconds}s" \
  docker run --rm --network host \
  --env-file "$API_ENV_FILE" --env-file "$SCHEDULER_ENV_FILE" \
  --name "at-job-${unit}" "$image_digest" \
  uv run python -m "$module" "$@"
rc=$?
set -e
elapsed_seconds=$((SECONDS - start_seconds))

# Healthchecks pings are intentionally best-effort and do not mask the job's
# exit status. Values stay in the root-owned environment file and are never
# printed. Unit identifiers are module-derived, so no service-side environment
# assignment can change the selected endpoint.
hc_variable="HC_PING_URL_${unit^^}"
hc_url="${!hc_variable:-}"
if [[ -n "$hc_url" ]]; then
  if (( rc == 0 )); then
    curl --fail --silent --show-error --max-time 10 --output /dev/null "$hc_url" || true
  else
    curl --fail --silent --show-error --max-time 10 --output /dev/null "${hc_url%/}/fail" || true
  fi
fi

printf '{"module":"%s","rc":%d,"elapsed_s":%d,"image_digest":"%s"}\n' \
  "$module" "$rc" "$elapsed_seconds" "$image_digest"
exit "$rc"
