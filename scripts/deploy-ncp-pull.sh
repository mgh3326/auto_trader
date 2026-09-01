#!/usr/bin/env bash
# Pull and roll forward the NCP Docker deployment from GHCR.
#
# Usage: scripts/deploy-ncp-pull.sh [tag]
#
# The target image is pulled before either container is replaced.  A failed
# API health check recreates both containers from their previously configured
# image references.  This script intentionally performs no migrations.

set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
readonly IMAGE_TAG="${1:-main}"
readonly IMAGE="${IMAGE_REPOSITORY}:${IMAGE_TAG}"

readonly RUN_DIRECTORY="${AT_RUN_DIRECTORY:-/root/at-run}"
readonly API_CONTAINER="at-api"
readonly SCHEDULER_CONTAINER="at-scheduler"
readonly DEPLOYED_DIGEST_FILE="${RUN_DIRECTORY}/deployed-digest"

# Keep the established two-file environment shape. Operators may set these
# names for an existing NCP host without placing environment contents in git.
readonly RUNTIME_ENV_FILE="${AT_RUNTIME_ENV_FILE:-${RUN_DIRECTORY}/.env.runtime}"
readonly SECRETS_ENV_FILE="${AT_SECRETS_ENV_FILE:-${RUN_DIRECTORY}/.env.secrets}"
readonly HEALTHZ_URL="${AT_HEALTHZ_URL:-http://127.0.0.1:8000/healthz}"
readonly HEALTHZ_ATTEMPTS="${AT_HEALTHZ_ATTEMPTS:-30}"
readonly HEALTHZ_SLEEP_SECONDS="${AT_HEALTHZ_SLEEP_SECONDS:-2}"

declare -a ENV_FILE_ARGS=(
  --env-file "${RUNTIME_ENV_FILE}"
  --env-file "${SECRETS_ENV_FILE}"
)

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'required command is unavailable: %s\n' "$1" >&2
    exit 127
  }
}

require_file() {
  [[ -f "$1" ]] || {
    printf 'required env file is unavailable: %s\n' "$1" >&2
    exit 78
  }
}

configured_image() {
  docker inspect --format '{{.Config.Image}}' "$1"
}

run_api() {
  local image="$1"
  docker run -d \
    --name "${API_CONTAINER}" \
    --restart unless-stopped \
    --network host \
    "${ENV_FILE_ARGS[@]}" \
    "$image"
}

run_scheduler() {
  local image="$1"
  docker run -d \
    --name "${SCHEDULER_CONTAINER}" \
    --restart unless-stopped \
    --network host \
    "${ENV_FILE_ARGS[@]}" \
    "$image" \
    /app/.venv/bin/taskiq scheduler app.core.scheduler:sched app.tasks
}

wait_for_healthz() {
  local attempt
  local http_status
  for ((attempt = 1; attempt <= HEALTHZ_ATTEMPTS; attempt += 1)); do
    if http_status="$(curl --silent --show-error --max-time 3 --output /dev/null --write-out '%{http_code}' "${HEALTHZ_URL}")" && [[ "${http_status}" == "200" ]]; then
      return 0
    fi
    printf 'waiting for API healthz (%s/%s)\n' "$attempt" "${HEALTHZ_ATTEMPTS}" >&2
    sleep "${HEALTHZ_SLEEP_SECONDS}"
  done
  return 1
}

rollback() {
  local previous_api_image="$1"
  local previous_scheduler_image="$2"
  local rollback_status=0

  printf 'API health check failed; rolling back to the previous image references.\n' >&2
  docker rm -f "${API_CONTAINER}" "${SCHEDULER_CONTAINER}" >/dev/null 2>&1 || true

  run_api "${previous_api_image}" >/dev/null || rollback_status=1
  run_scheduler "${previous_scheduler_image}" >/dev/null || rollback_status=1
  if ! wait_for_healthz; then
    rollback_status=1
  fi

  if ((rollback_status != 0)); then
    printf 'rollback failed; operator intervention is required.\n' >&2
  else
    printf 'rollback completed.\n' >&2
  fi
  return "${rollback_status}"
}

write_deployed_digest() {
  local digest="$1"
  local temporary_file

  umask 077
  temporary_file="$(mktemp "${RUN_DIRECTORY}/.deployed-digest.XXXXXX")"
  printf '%s\n' "${digest}" >"${temporary_file}"
  mv -f "${temporary_file}" "${DEPLOYED_DIGEST_FILE}"
}

main() {
  local previous_api_image
  local previous_scheduler_image
  local deployed_digest

  require_command docker
  require_command curl
  require_file "${RUNTIME_ENV_FILE}"
  require_file "${SECRETS_ENV_FILE}"

  # A rollback is mandatory for this promotion path, so do not turn a first
  # install into an un-recoverable deployment attempt.
  previous_api_image="$(configured_image "${API_CONTAINER}")" || {
    printf 'previous API container is required for rollback: %s\n' "${API_CONTAINER}" >&2
    exit 78
  }
  previous_scheduler_image="$(configured_image "${SCHEDULER_CONTAINER}")" || {
    printf 'previous scheduler container is required for rollback: %s\n' "${SCHEDULER_CONTAINER}" >&2
    exit 78
  }

  printf 'pulling %s\n' "${IMAGE}"
  docker pull "${IMAGE}"
  deployed_digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "${IMAGE}")"
  [[ -n "${deployed_digest}" ]] || {
    printf 'could not resolve a repo digest for %s\n' "${IMAGE}" >&2
    exit 1
  }
  printf 'pulled digest: %s\n' "${deployed_digest}"

  if ! docker rm -f "${API_CONTAINER}" "${SCHEDULER_CONTAINER}" >/dev/null; then
    printf 'could not remove the current containers; no replacement was started.\n' >&2
    exit 1
  fi

  if ! run_api "${IMAGE}" >/dev/null || ! run_scheduler "${IMAGE}" >/dev/null || ! wait_for_healthz; then
    rollback "${previous_api_image}" "${previous_scheduler_image}" || true
    exit 1
  fi

  if ! write_deployed_digest "${deployed_digest}"; then
    printf 'could not record deployed digest; rolling back.\n' >&2
    rollback "${previous_api_image}" "${previous_scheduler_image}" || true
    exit 1
  fi

  printf 'deployment completed: %s\n' "${deployed_digest}"
}

main
