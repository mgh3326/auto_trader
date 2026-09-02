#!/usr/bin/env bash
# Pull and roll forward the NCP Docker deployment from GHCR.
#
# Usage: scripts/deploy-ncp-pull.sh [tag]
#        scripts/deploy-ncp-pull.sh --rollback
#
# The target image is pulled before any container is replaced. A failed
# readiness check recreates every unit from pinned prior references.
# This script intentionally performs no migrations.

set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"

if (($# == 0)); then
  readonly DEPLOY_MODE="deploy"
  readonly IMAGE_TAG="main"
elif [[ "$1" == "--rollback" ]]; then
  (($# == 1)) || {
    printf 'usage: %s [tag] | --rollback\n' "$0" >&2
    exit 64
  }
  readonly DEPLOY_MODE="rollback"
  readonly IMAGE_TAG=""
elif [[ "$1" == -* ]]; then
  printf 'usage: %s [tag] | --rollback\n' "$0" >&2
  exit 64
else
  (($# == 1)) || {
    printf 'usage: %s [tag] | --rollback\n' "$0" >&2
    exit 64
  }
  readonly DEPLOY_MODE="deploy"
  readonly IMAGE_TAG="$1"
fi

readonly IMAGE="${IMAGE_REPOSITORY}:${IMAGE_TAG}"

readonly RUN_DIRECTORY="${AT_RUN_DIRECTORY:-/root/at-run}"
readonly API_CONTAINER="at-api"
readonly SCHEDULER_CONTAINER="at-scheduler"
readonly WORKER_CONTAINER="at-worker"
readonly DEPLOYED_DIGEST_FILE="${RUN_DIRECTORY}/deployed-digest"
readonly DEPLOYED_DIGEST_PREVIOUS_FILE="${RUN_DIRECTORY}/deployed-digest.previous"

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

is_repo_digest() {
  [[ "$1" =~ ^${IMAGE_REPOSITORY}@sha256:[[:xdigit:]]{64}$ ]]
}

read_digest_file() {
  local digest_file="$1"
  local digest

  [[ -f "${digest_file}" ]] || return 1
  IFS= read -r digest <"${digest_file}" || return 1
  is_repo_digest "${digest}" || return 1
  printf '%s\n' "${digest}"
}

rollback_reference() {
  local previous_image="$1"
  local component="$2"
  local deployed_digest

  if is_repo_digest "${previous_image}"; then
    printf '%s\n' "${previous_image}"
    return 0
  fi

  if deployed_digest="$(read_digest_file "${DEPLOYED_DIGEST_FILE}")"; then
    printf '%s\n' "${deployed_digest}"
    return 0
  fi

  printf '%s rollback reference is unavailable: container image is not a repo digest and %s is missing or invalid\n' \
    "${component}" "${DEPLOYED_DIGEST_FILE}" >&2
  return 1
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

run_worker() {
  local image="$1"
  docker run -d \
    --name "${WORKER_CONTAINER}" \
    --restart unless-stopped \
    --network host \
    "${ENV_FILE_ARGS[@]}" \
    "$image" \
    /app/.venv/bin/taskiq worker app.core.taskiq_broker:broker app.tasks --workers 1
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

wait_for_worker_startup() {
  local attempt
  local running

  for ((attempt = 1; attempt <= HEALTHZ_ATTEMPTS; attempt += 1)); do
    running="$(docker inspect --format '{{.State.Running}}' "${WORKER_CONTAINER}" 2>/dev/null || true)"
    if [[ "${running}" == "true" ]] && docker logs --tail 100 "${WORKER_CONTAINER}" 2>&1 | grep --fixed-strings --quiet 'Starting 1 worker processes.'; then
      return 0
    fi
    printf 'waiting for TaskIQ worker startup (%s/%s)\n' "$attempt" "${HEALTHZ_ATTEMPTS}" >&2
    sleep "${HEALTHZ_SLEEP_SECONDS}"
  done
  return 1
}

rollback() {
  local previous_api_image="$1"
  local previous_scheduler_image="$2"
  local previous_worker_image="$3"
  local rollback_api_image
  local rollback_scheduler_image
  local rollback_worker_image
  local rollback_status=0

  rollback_api_image="$(rollback_reference "${previous_api_image}" "API")" || return 1
  rollback_scheduler_image="$(rollback_reference "${previous_scheduler_image}" "scheduler")" || return 1
  rollback_worker_image="$(rollback_reference "${previous_worker_image}" "worker")" || return 1

  printf 'deployment readiness check failed; rolling back to pinned image references.\n' >&2
  docker rm -f "${API_CONTAINER}" "${SCHEDULER_CONTAINER}" "${WORKER_CONTAINER}" >/dev/null 2>&1 || true

  run_api "${rollback_api_image}" >/dev/null || rollback_status=1
  run_scheduler "${rollback_scheduler_image}" >/dev/null || rollback_status=1
  run_worker "${rollback_worker_image}" >/dev/null || rollback_status=1
  if ! wait_for_healthz; then
    rollback_status=1
  fi
  if ! wait_for_worker_startup; then
    rollback_status=1
  fi

  if ((rollback_status == 0)); then
    write_deployed_digest "${rollback_api_image}" || rollback_status=1
  fi

  if ((rollback_status == 0)); then
    printf 'rollback completed.\n' >&2
  else
    printf 'rollback failed; operator intervention is required.\n' >&2
  fi
  return "${rollback_status}"
}

write_deployed_digest() {
  local digest="$1"
  local deployed_digest
  local temporary_file
  local temporary_previous_file

  is_repo_digest "${digest}" || return 1
  umask 077
  temporary_file="$(mktemp "${RUN_DIRECTORY}/.deployed-digest.XXXXXX")"
  printf '%s\n' "${digest}" >"${temporary_file}"

  if [[ -f "${DEPLOYED_DIGEST_FILE}" ]]; then
    deployed_digest="$(read_digest_file "${DEPLOYED_DIGEST_FILE}")" || {
      rm -f "${temporary_file}"
      return 1
    }
    temporary_previous_file="$(mktemp "${RUN_DIRECTORY}/.deployed-digest.previous.XXXXXX")"
    printf '%s\n' "${deployed_digest}" >"${temporary_previous_file}"
    mv -f "${temporary_previous_file}" "${DEPLOYED_DIGEST_PREVIOUS_FILE}"
  fi
  mv -f "${temporary_file}" "${DEPLOYED_DIGEST_FILE}"
}

rollback_to_previous_digest() {
  local previous_digest
  local current_api_image
  local current_scheduler_image
  local current_worker_image

  previous_digest="$(read_digest_file "${DEPLOYED_DIGEST_PREVIOUS_FILE}")" || {
    printf 'manual rollback digest is unavailable or invalid: %s\n' "${DEPLOYED_DIGEST_PREVIOUS_FILE}" >&2
    return 1
  }
  current_api_image="$(configured_image "${API_CONTAINER}")" || {
    printf 'current API container is required for rollback: %s\n' "${API_CONTAINER}" >&2
    return 78
  }
  current_scheduler_image="$(configured_image "${SCHEDULER_CONTAINER}")" || {
    printf 'current scheduler container is required for rollback: %s\n' "${SCHEDULER_CONTAINER}" >&2
    return 78
  }
  current_worker_image="$(configured_image "${WORKER_CONTAINER}")" || {
    printf 'current worker container is required for rollback: %s\n' "${WORKER_CONTAINER}" >&2
    return 78
  }
  rollback_reference "${current_api_image}" "API" >/dev/null || return 1
  rollback_reference "${current_scheduler_image}" "scheduler" >/dev/null || return 1
  rollback_reference "${current_worker_image}" "worker" >/dev/null || return 1

  printf 'pulling rollback digest: %s\n' "${previous_digest}"
  docker pull "${previous_digest}"
  docker rm -f "${API_CONTAINER}" "${SCHEDULER_CONTAINER}" "${WORKER_CONTAINER}" >/dev/null || return 1
  if ! run_api "${previous_digest}" >/dev/null || ! run_scheduler "${previous_digest}" >/dev/null || ! run_worker "${previous_digest}" >/dev/null || ! wait_for_healthz || ! wait_for_worker_startup; then
    rollback "${current_api_image}" "${current_scheduler_image}" "${current_worker_image}" || true
    return 1
  fi
  write_deployed_digest "${previous_digest}"
}

main() {
  local previous_api_image
  local previous_scheduler_image
  local previous_worker_image
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
  previous_worker_image="$(configured_image "${WORKER_CONTAINER}")" || {
    printf 'previous worker container is required for rollback: %s\n' "${WORKER_CONTAINER}" >&2
    exit 78
  }

  # Verify rollback is possible before a floating tag is pulled and before any
  # running container is replaced. A legacy floating container may use the
  # persisted digest exactly once during the transition to pinned containers.
  rollback_reference "${previous_api_image}" "API" >/dev/null || exit 78
  rollback_reference "${previous_scheduler_image}" "scheduler" >/dev/null || exit 78
  rollback_reference "${previous_worker_image}" "worker" >/dev/null || exit 78

  printf 'pulling %s\n' "${IMAGE}"
  docker pull "${IMAGE}"
  deployed_digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "${IMAGE}")"
  is_repo_digest "${deployed_digest}" || {
    printf 'could not resolve a valid repo digest for %s\n' "${IMAGE}" >&2
    exit 1
  }
  printf 'pulled digest: %s\n' "${deployed_digest}"

  if ! docker rm -f "${API_CONTAINER}" "${SCHEDULER_CONTAINER}" "${WORKER_CONTAINER}" >/dev/null; then
    printf 'could not remove the current containers; no replacement was started.\n' >&2
    exit 1
  fi

  if ! run_api "${deployed_digest}" >/dev/null || ! run_scheduler "${deployed_digest}" >/dev/null || ! run_worker "${deployed_digest}" >/dev/null || ! wait_for_healthz || ! wait_for_worker_startup; then
    rollback "${previous_api_image}" "${previous_scheduler_image}" "${previous_worker_image}" || true
    exit 1
  fi

  if ! write_deployed_digest "${deployed_digest}"; then
    printf 'could not record deployed digest; rolling back.\n' >&2
    rollback "${previous_api_image}" "${previous_scheduler_image}" "${previous_worker_image}" || true
    exit 1
  fi

  printf 'deployment completed: %s\n' "${deployed_digest}"
}

if [[ "${DEPLOY_MODE}" == "rollback" ]]; then
  require_command docker
  require_command curl
  require_file "${RUNTIME_ENV_FILE}"
  require_file "${SECRETS_ENV_FILE}"
  rollback_to_previous_digest
else
  main
fi
