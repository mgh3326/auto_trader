#!/usr/bin/env bash
# Pull and promote a digest-pinned NCP deployment, including the MCP fleet.
#
# The MCP listener is deliberately private: HAProxy binds only loopback and the
# NCP tailnet address. Never add a public bind here.
set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
readonly HAPROXY_IMAGE="haproxy:3.1-alpine"

if (($# == 0)); then
  readonly DEPLOY_MODE="deploy" IMAGE_TAG="main"
elif [[ "$1" == "--rollback" && $# == 1 ]]; then
  readonly DEPLOY_MODE="rollback" IMAGE_TAG=""
elif [[ "$1" != -* && $# == 1 ]]; then
  readonly DEPLOY_MODE="deploy" IMAGE_TAG="$1"
else
  printf 'usage: %s [tag] | --rollback\n' "$0" >&2
  exit 64
fi

readonly IMAGE="${IMAGE_REPOSITORY}:${IMAGE_TAG}"
readonly RUN_DIRECTORY="${AT_RUN_DIRECTORY:-/root/at-run}"
readonly API_CONTAINER="at-api" SCHEDULER_CONTAINER="at-scheduler" WORKER_CONTAINER="at-worker"
readonly DEPLOYED_DIGEST_FILE="${RUN_DIRECTORY}/deployed-digest"
readonly DEPLOYED_DIGEST_PREVIOUS_FILE="${RUN_DIRECTORY}/deployed-digest.previous"
readonly RUNTIME_ENV_FILE="${AT_RUNTIME_ENV_FILE:-${RUN_DIRECTORY}/.env.runtime}"
readonly SECRETS_ENV_FILE="${AT_SECRETS_ENV_FILE:-${RUN_DIRECTORY}/.env.secrets}"
readonly HEALTHZ_URL="${AT_HEALTHZ_URL:-http://127.0.0.1:8000/healthz}"
readonly HEALTHZ_ATTEMPTS="${AT_HEALTHZ_ATTEMPTS:-30}"
readonly HEALTHZ_SLEEP_SECONDS="${AT_HEALTHZ_SLEEP_SECONDS:-2}"

# MCP inventory is intentionally a table, rather than seven nearly identical
# docker invocations. This keeps future unit additions isolated.
readonly MCP_ACTIVE_COLOR_FILE="${RUN_DIRECTORY}/mcp-active-color"
readonly MCP_HEARTBEAT_DIRECTORY="${RUN_DIRECTORY}/mcp-heartbeat"
readonly MCP_HAPROXY_CONFIG="${RUN_DIRECTORY}/haproxy.cfg"
readonly MCP_HAPROXY_TEMPLATE="${MCP_HAPROXY_TEMPLATE:-$(cd "$(dirname "$0")/.." && pwd)/ops/ncp/haproxy/haproxy.cfg.tmpl}"
readonly MCP_HAPROXY_CONTAINER="at-haproxy"
readonly MCP_DRAIN_SECONDS="${MCP_DRAIN_SECONDS:-3600}"
readonly MCP_HEALTH_ATTEMPTS="${MCP_HEALTH_ATTEMPTS:-30}"
readonly MCP_HEALTH_SLEEP_SECONDS="${MCP_HEALTH_SLEEP_SECONDS:-2}"
readonly MCP_UNITS_SKIP="${MCP_UNITS_SKIP:-}"
readonly TAILNET_MCP_BIND="100.122.100.56:8765"

declare -a ENV_FILE_ARGS=(--env-file "${RUNTIME_ENV_FILE}" --env-file "${SECRETS_ENV_FILE}")
declare -a MCP_FIXED_UNIT_NAMES=(analysis-readonly account-read tradingcodex-execution paper-001 kiwoom)
declare -a MCP_FIXED_PROFILES=(analysis_readonly account_read tradingcodex_execution hermes-paper-kis kiwoom)
declare -a MCP_FIXED_PORTS=(8768 8769 8770 8771 8772)
declare -a MCP_FIXED_TOKEN_ENVS=(MCP_ANALYSIS_READONLY_AUTH_TOKEN MCP_ACCOUNT_READ_AUTH_TOKEN MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN MCP_PAPER_001_AUTH_TOKEN MCP_KIWOOM_AUTH_TOKEN)
declare -a MCP_PREVIOUS_NAMES=(blue green analysis-readonly account-read tradingcodex-execution paper-001 kiwoom)
declare -a MCP_PREVIOUS_IMAGES=()
declare -a MCP_PREVIOUS_PRESENT=()
MCP_PREVIOUS_ACTIVE_COLOR=""

require_command() { command -v "$1" >/dev/null 2>&1 || { printf 'required command is unavailable: %s\n' "$1" >&2; exit 127; }; }
require_file() { [[ -f "$1" ]] || { printf 'required env file is unavailable: %s\n' "$1" >&2; exit 78; }; }
configured_image() { docker inspect --format '{{.Config.Image}}' "$1"; }
container_exists() { docker inspect --format '{{.Id}}' "$1" >/dev/null 2>&1; }
is_repo_digest() { [[ "$1" =~ ^${IMAGE_REPOSITORY}@sha256:[[:xdigit:]]{64}$ ]]; }

read_digest_file() {
  local digest
  [[ -f "$1" ]] || return 1
  IFS= read -r digest <"$1" || return 1
  is_repo_digest "$digest" || return 1
  printf '%s\n' "$digest"
}

rollback_reference() {
  local previous_image="$1" component="$2" deployed_digest
  if is_repo_digest "$previous_image"; then printf '%s\n' "$previous_image"; return 0; fi
  if deployed_digest="$(read_digest_file "$DEPLOYED_DIGEST_FILE")"; then printf '%s\n' "$deployed_digest"; return 0; fi
  printf '%s rollback reference is unavailable: container image is not a repo digest and %s is missing or invalid\n' "$component" "$DEPLOYED_DIGEST_FILE" >&2
  return 1
}

worker_rollback_image() {
  local api_image="$1" worker_image
  if worker_image="$(configured_image "$WORKER_CONTAINER" 2>/dev/null)"; then printf '%s\n' "$worker_image"; return 0; fi
  printf 'worker container %s is absent; bootstrapping its rollback reference from %s\n' "$WORKER_CONTAINER" "$API_CONTAINER" >&2
  printf '%s\n' "$api_image"
}

run_api() { docker run -d --name "$API_CONTAINER" --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" "$1"; }
run_scheduler() { docker run -d --name "$SCHEDULER_CONTAINER" --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" "$1" /app/.venv/bin/taskiq scheduler app.core.scheduler:sched app.tasks; }
run_worker() { docker run -d --name "$WORKER_CONTAINER" --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" "$1" /app/.venv/bin/taskiq worker app.core.taskiq_broker:broker app.tasks --workers 1; }

wait_for_healthz() {
  local attempt status
  for ((attempt=1; attempt<=HEALTHZ_ATTEMPTS; attempt++)); do
    if status="$(curl --silent --show-error --max-time 3 --output /dev/null --write-out '%{http_code}' "$HEALTHZ_URL")" && [[ "$status" == 200 ]]; then return 0; fi
    printf 'waiting for API healthz (%s/%s)\n' "$attempt" "$HEALTHZ_ATTEMPTS" >&2; sleep "$HEALTHZ_SLEEP_SECONDS"
  done
  return 1
}

wait_for_worker_startup() {
  local attempt running
  for ((attempt=1; attempt<=HEALTHZ_ATTEMPTS; attempt++)); do
    running="$(docker inspect --format '{{.State.Running}}' "$WORKER_CONTAINER" 2>/dev/null || true)"
    if [[ "$running" == true ]] && docker logs --tail 100 "$WORKER_CONTAINER" 2>&1 | grep --fixed-strings --quiet 'Starting 1 worker processes.'; then return 0; fi
    printf 'waiting for TaskIQ worker startup (%s/%s)\n' "$attempt" "$HEALTHZ_ATTEMPTS" >&2; sleep "$HEALTHZ_SLEEP_SECONDS"
  done
  return 1
}

mcp_unit_is_skipped() {
  local unit="$1" entry
  [[ -n "$MCP_UNITS_SKIP" ]] || return 1
  IFS=, read -r -a entries <<<"$MCP_UNITS_SKIP"
  for entry in "${entries[@]}"; do [[ "$entry" == "$unit" ]] && return 0; done
  return 1
}

# Read a Docker --env-file-compatible assignment without sourcing untrusted
# shell text. Later files override earlier ones, matching docker's argument
# order. Values are never printed.
env_file_value() {
  local key="$1" env_file value="" candidate
  for env_file in "$RUNTIME_ENV_FILE" "$SECRETS_ENV_FILE"; do
    candidate="$(awk -v key="$key" '$0 ~ "^[[:space:]]*(export[[:space:]]+)?" key "=" { sub("^[[:space:]]*(export[[:space:]]+)?" key "=", ""); print }' "$env_file" | tail -n 1)"
    [[ -n "$candidate" ]] && value="$candidate"
  done
  value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"
  [[ -n "${value//[[:space:]]/}" ]] || return 1
  printf '%s' "$value"
}

validate_mcp_tokens() {
  local i unit token_env
  env_file_value MCP_AUTH_TOKEN >/dev/null || { printf 'MCP unit main requires non-empty MCP_AUTH_TOKEN\n' >&2; return 78; }
  for i in "${!MCP_FIXED_UNIT_NAMES[@]}"; do
    unit="${MCP_FIXED_UNIT_NAMES[$i]}"; token_env="${MCP_FIXED_TOKEN_ENVS[$i]}"
    mcp_unit_is_skipped "$unit" && continue
    env_file_value "$token_env" >/dev/null || { printf 'MCP unit %s requires non-empty %s\n' "$unit" "$token_env" >&2; return 78; }
  done
}

# Profile-scoped policy env. The Mac launchd plist for the TradingCodex
# execution profile pins both approval-hash modes to "required" (the app refuses
# to start that profile otherwise); every other unit keeps the env-file default.
# Keep this per unit rather than in .env.api so api/scheduler/worker semantics
# do not change as a side effect of deploying MCP.
mcp_profile_policy_args() {
  local profile="$1"
  case "$profile" in
    tradingcodex_execution)
      printf '%s\n' -e ORDER_APPROVAL_HASH_MODE=required -e TOSS_APPROVAL_HASH_MODE=required
      ;;
  esac
}

run_mcp_container() {
  local name="$1" port="$2" profile="$3" token_env="$4" color="${5:-}" token heartbeat image="$6"
  local -a policy_args=()
  token="$(env_file_value "$token_env")" || return 78
  heartbeat="/var/run/auto-trader/mcp-heartbeat/mcp-${color:-$name}.json"
  mapfile -t policy_args < <(mcp_profile_policy_args "$profile")
  docker run -d --name "at-mcp-${name}" --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" \
    -v "${MCP_HEARTBEAT_DIRECTORY}:/var/run/auto-trader/mcp-heartbeat" \
    "${policy_args[@]}" \
    -e "MCP_AUTH_TOKEN=${token}" -e "MCP_PROFILE=${profile}" -e MCP_HOST=127.0.0.1 \
    -e "MCP_PORT=${port}" -e MCP_TYPE=streamable-http -e MCP_PATH=/mcp -e MCP_USER_ID=1 \
    -e "AUTO_TRADER_COLOR=${color:-$name}" -e "MCP_HEARTBEAT_PATH=${heartbeat}" "$image" \
    python -m app.mcp_server.main
}

wait_for_mcp_health() {
  local port="$1" unit="$2" attempt status
  for ((attempt=1; attempt<=MCP_HEALTH_ATTEMPTS; attempt++)); do
    if status="$(curl --silent --show-error --max-time 3 --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${port}/health")" && [[ "$status" == 200 ]]; then return 0; fi
    printf 'waiting for MCP %s health (%s/%s)\n' "$unit" "$attempt" "$MCP_HEALTH_ATTEMPTS" >&2; sleep "$MCP_HEALTH_SLEEP_SECONDS"
  done
  return 1
}

read_mcp_active_color() {
  local color
  [[ -f "$MCP_ACTIVE_COLOR_FILE" ]] || return 1
  IFS= read -r color <"$MCP_ACTIVE_COLOR_FILE" || return 1
  [[ "$color" == blue || "$color" == green ]] || return 1
  printf '%s\n' "$color"
}

write_mcp_active_color() {
  local color="$1" tmp
  [[ "$color" == blue || "$color" == green ]] || return 64
  mkdir -p "$RUN_DIRECTORY"; umask 077
  tmp="$(mktemp "${RUN_DIRECTORY}/.mcp-active-color.XXXXXX")"
  printf '%s\n' "$color" >"$tmp"; mv -f "$tmp" "$MCP_ACTIVE_COLOR_FILE"
}

render_haproxy_config() {
  local color="$1" port
  case "$color" in blue) port=8766;; green) port=8767;; *) return 64;; esac
  [[ -f "$MCP_HAPROXY_TEMPLATE" ]] || { printf 'missing HAProxy template: %s\n' "$MCP_HAPROXY_TEMPLATE" >&2; return 78; }
  mkdir -p "$RUN_DIRECTORY"
  sed "s/__MCP_ACTIVE_PORT__/${port}/g" "$MCP_HAPROXY_TEMPLATE" >"${MCP_HAPROXY_CONFIG}.tmp"
  if grep --fixed-strings --quiet '0.0.0.0' "${MCP_HAPROXY_CONFIG}.tmp" || ! grep --fixed-strings --quiet 'bind 127.0.0.1:8765' "${MCP_HAPROXY_CONFIG}.tmp" || ! grep --fixed-strings --quiet "bind ${TAILNET_MCP_BIND}" "${MCP_HAPROXY_CONFIG}.tmp"; then
    rm -f "${MCP_HAPROXY_CONFIG}.tmp"; printf 'HAProxy MCP binds must be loopback and the configured tailnet address only\n' >&2; return 78
  fi
  # The rendered config carries ports only (no secrets) and is bind-mounted into
  # the official haproxy image, which drops to the unprivileged "haproxy" user.
  # The deploy umask of 077 would leave it unreadable there and the container
  # crash-loops on "Permission denied" (observed on the first NCP deploy).
  chmod 0644 "${MCP_HAPROXY_CONFIG}.tmp"
  # The config is bind-mounted as a *file* into the haproxy container, and a
  # file bind mount follows the inode. `mv` would swap the inode and leave the
  # container reloading the stale copy forever; copy into place instead.
  if [[ -e "$MCP_HAPROXY_CONFIG" ]]; then
    cat "${MCP_HAPROXY_CONFIG}.tmp" >"$MCP_HAPROXY_CONFIG" && rm -f "${MCP_HAPROXY_CONFIG}.tmp"
  else
    mv -f "${MCP_HAPROXY_CONFIG}.tmp" "$MCP_HAPROXY_CONFIG"
  fi
}

reload_haproxy() {
  if container_exists "$MCP_HAPROXY_CONTAINER"; then docker kill -s HUP "$MCP_HAPROXY_CONTAINER" >/dev/null
  else docker run -d --name "$MCP_HAPROXY_CONTAINER" --restart unless-stopped --network host -v "${MCP_HAPROXY_CONFIG}:/usr/local/etc/haproxy/haproxy.cfg:ro" "$HAPROXY_IMAGE" -W -db -f /usr/local/etc/haproxy/haproxy.cfg >/dev/null
  fi
}

switch_mcp_haproxy() {
  local new_color="$1" old_color="$2"
  write_mcp_active_color "$new_color"
  if render_haproxy_config "$new_color" && reload_haproxy; then return 0; fi
  write_mcp_active_color "$old_color" || true
  render_haproxy_config "$old_color" && reload_haproxy || true
  return 1
}

# The captured ID ensures a delayed timer cannot remove a later replacement.
schedule_mcp_drain() {
  local color="$1" id pid_file
  id="$(docker inspect --format '{{.Id}}' "at-mcp-${color}" 2>/dev/null)" || return 0
  pid_file="${RUN_DIRECTORY}/mcp-drain-${color}.pid"
  nohup bash -c 'sleep "$1"; current="$(docker inspect --format "{{.Id}}" "$2" 2>/dev/null || true)"; [[ "$current" == "$3" ]] && docker rm -f "$2" >/dev/null 2>&1 || true' _ "$MCP_DRAIN_SECONDS" "at-mcp-${color}" "$id" >"${RUN_DIRECTORY}/mcp-drain-${color}.log" 2>&1 &
  printf '%s\n' "$!" >"$pid_file"
}

start_mcp_fixed_units() {
  local image="$1" i unit profile port token_env
  for i in "${!MCP_FIXED_UNIT_NAMES[@]}"; do
    unit="${MCP_FIXED_UNIT_NAMES[$i]}"; profile="${MCP_FIXED_PROFILES[$i]}"; port="${MCP_FIXED_PORTS[$i]}"; token_env="${MCP_FIXED_TOKEN_ENVS[$i]}"
    if mcp_unit_is_skipped "$unit"; then printf 'MCP unit skipped by MCP_UNITS_SKIP: %s\n' "$unit" >&2; continue; fi
    docker rm -f "at-mcp-${unit}" >/dev/null 2>&1 || true
    run_mcp_container "$unit" "$port" "$profile" "$token_env" "" "$image" >/dev/null || return 1
    wait_for_mcp_health "$port" "$unit" || return 1
  done
}

capture_mcp_state() {
  local i name image
  MCP_PREVIOUS_ACTIVE_COLOR="$(read_mcp_active_color 2>/dev/null || true)"
  MCP_PREVIOUS_IMAGES=()
  MCP_PREVIOUS_PRESENT=()
  for i in "${!MCP_PREVIOUS_NAMES[@]}"; do
    name="${MCP_PREVIOUS_NAMES[$i]}"
    image="$(configured_image "at-mcp-${name}" 2>/dev/null || true)"
    MCP_PREVIOUS_IMAGES[$i]="$image"
    [[ -n "$image" ]] && MCP_PREVIOUS_PRESENT[$i]=1 || MCP_PREVIOUS_PRESENT[$i]=0
  done
}

# Recreate exactly the MCP units that existed before this promotion. This is
# separate from core rollback so a first MCP install cannot leave a candidate.
rollback_mcp() {
  local fallback_image="$1" i name image port profile token_env fixed_index
  for name in "${MCP_PREVIOUS_NAMES[@]}"; do
    docker rm -f "at-mcp-${name}" >/dev/null 2>&1 || true
  done
  for i in "${!MCP_PREVIOUS_NAMES[@]}"; do
    [[ "${MCP_PREVIOUS_PRESENT[$i]:-0}" == 1 ]] || continue
    name="${MCP_PREVIOUS_NAMES[$i]}"; image="${MCP_PREVIOUS_IMAGES[$i]}"
    image="$(rollback_reference "$image" "MCP ${name}" 2>/dev/null || printf '%s' "$fallback_image")"
    case "$name" in
      blue|green)
        [[ "$name" == blue ]] && port=8766 || port=8767
        run_mcp_container "$name" "$port" default MCP_AUTH_TOKEN "$name" "$image" >/dev/null || return 1
        ;;
      *)
        for fixed_index in "${!MCP_FIXED_UNIT_NAMES[@]}"; do
          [[ "${MCP_FIXED_UNIT_NAMES[$fixed_index]}" == "$name" ]] && break
        done
        profile="${MCP_FIXED_PROFILES[$fixed_index]}"
        port="${MCP_FIXED_PORTS[$fixed_index]}"
        token_env="${MCP_FIXED_TOKEN_ENVS[$fixed_index]}"
        run_mcp_container "$name" "$port" "$profile" "$token_env" "" "$image" >/dev/null || return 1
        ;;
    esac
  done
  if [[ -n "$MCP_PREVIOUS_ACTIVE_COLOR" ]]; then
    write_mcp_active_color "$MCP_PREVIOUS_ACTIVE_COLOR"
    render_haproxy_config "$MCP_PREVIOUS_ACTIVE_COLOR" && reload_haproxy || return 1
  else
    rm -f "$MCP_ACTIVE_COLOR_FILE"
  fi
}

deploy_mcp() {
  local image="$1" old_color new_color new_port
  # The image runs as the unprivileged appuser. Heartbeats contain only
  # liveness timestamps (never credentials), so a sticky shared directory
  # permits the host watchdog to read every profile without running MCP as root.
  mkdir -p "$MCP_HEARTBEAT_DIRECTORY"; chmod 1777 "$MCP_HEARTBEAT_DIRECTORY"
  validate_mcp_tokens || return $?
  capture_mcp_state
  old_color="$(read_mcp_active_color 2>/dev/null || true)"
  if [[ -z "$old_color" ]]; then new_color=blue
  elif [[ "$old_color" == blue ]]; then new_color=green
  else new_color=blue; fi
  [[ "$new_color" == blue ]] && new_port=8766 || new_port=8767
  # Remove only the inactive candidate. The active color is never removed
  # before HAProxy has switched and its configured drain has elapsed.
  docker rm -f "at-mcp-${new_color}" >/dev/null 2>&1 || true
  run_mcp_container "$new_color" "$new_port" default MCP_AUTH_TOKEN "$new_color" "$image" >/dev/null || return 1
  wait_for_mcp_health "$new_port" "$new_color" || return 1
  start_mcp_fixed_units "$image" || return 1
  # The retired single default server occupies HAProxy's frontend port 8765.
  # It has no rollback role; remove it only after the replacement fleet is
  # healthy and immediately before HAProxy starts/reloads.
  docker rm -f at-mcp-readonly >/dev/null 2>&1 || true
  if [[ -z "$old_color" ]]; then
    write_mcp_active_color "$new_color"; render_haproxy_config "$new_color" && reload_haproxy || return 1
  else
    switch_mcp_haproxy "$new_color" "$old_color" || return 1
    schedule_mcp_drain "$old_color"
  fi
}

write_deployed_digest() {
  local digest="$1" current tmp previous_tmp
  is_repo_digest "$digest" || return 1; umask 077; mkdir -p "$RUN_DIRECTORY"
  tmp="$(mktemp "${RUN_DIRECTORY}/.deployed-digest.XXXXXX")"; printf '%s\n' "$digest" >"$tmp"
  if [[ -f "$DEPLOYED_DIGEST_FILE" ]]; then
    current="$(read_digest_file "$DEPLOYED_DIGEST_FILE")" || { rm -f "$tmp"; return 1; }
    previous_tmp="$(mktemp "${RUN_DIRECTORY}/.deployed-digest.previous.XXXXXX")"; printf '%s\n' "$current" >"$previous_tmp"; mv -f "$previous_tmp" "$DEPLOYED_DIGEST_PREVIOUS_FILE"
  fi
  mv -f "$tmp" "$DEPLOYED_DIGEST_FILE"
}

rollback_core() {
  local api="$1" scheduler="$2" worker="$3" status=0
  printf 'deployment readiness check failed; rolling back to pinned image references.\n' >&2
  docker rm -f "$API_CONTAINER" "$SCHEDULER_CONTAINER" "$WORKER_CONTAINER" >/dev/null 2>&1 || true
  run_api "$api" >/dev/null || status=1; run_scheduler "$scheduler" >/dev/null || status=1; run_worker "$worker" >/dev/null || status=1
  wait_for_healthz || status=1; wait_for_worker_startup || status=1
  if ((status == 0)); then write_deployed_digest "$api" || status=1; fi
  if ((status == 0)); then printf 'rollback completed.\n' >&2; else printf 'rollback failed; operator intervention is required.\n' >&2; fi
  return "$status"
}

main() {
  local previous_api previous_scheduler previous_worker digest rollback_api rollback_scheduler rollback_worker
  require_command docker; require_command curl; require_command awk; require_file "$RUNTIME_ENV_FILE"; require_file "$SECRETS_ENV_FILE"
  previous_api="$(configured_image "$API_CONTAINER")" || { printf 'previous API container is required for rollback: %s\n' "$API_CONTAINER" >&2; exit 78; }
  previous_scheduler="$(configured_image "$SCHEDULER_CONTAINER")" || { printf 'previous scheduler container is required for rollback: %s\n' "$SCHEDULER_CONTAINER" >&2; exit 78; }
  previous_worker="$(worker_rollback_image "$previous_api")"
  rollback_api="$(rollback_reference "$previous_api" API)" || exit 78; rollback_scheduler="$(rollback_reference "$previous_scheduler" scheduler)" || exit 78; rollback_worker="$(rollback_reference "$previous_worker" worker)" || exit 78
  validate_mcp_tokens || exit $?
  printf 'pulling %s\n' "$IMAGE"; docker pull "$IMAGE"; digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE")"
  is_repo_digest "$digest" || { printf 'could not resolve a valid repo digest for %s\n' "$IMAGE" >&2; exit 1; }; printf 'pulled digest: %s\n' "$digest"
  if ! docker rm -f "$API_CONTAINER" "$SCHEDULER_CONTAINER" "$WORKER_CONTAINER" >/dev/null || ! run_api "$digest" >/dev/null || ! run_scheduler "$digest" >/dev/null || ! run_worker "$digest" >/dev/null || ! wait_for_healthz || ! wait_for_worker_startup || ! deploy_mcp "$digest"; then
    rollback_mcp "$rollback_api" || true
    rollback_core "$rollback_api" "$rollback_scheduler" "$rollback_worker" || true; exit 1
  fi
  write_deployed_digest "$digest" || { printf 'could not record deployed digest; rolling back.\n' >&2; rollback_mcp "$rollback_api" || true; rollback_core "$rollback_api" "$rollback_scheduler" "$rollback_worker" || true; exit 1; }
  printf 'deployment completed: %s\n' "$digest"
}

manual_rollback() {
  local previous current_api current_scheduler current_worker
  require_command docker; require_command curl; require_file "$RUNTIME_ENV_FILE"; require_file "$SECRETS_ENV_FILE"
  previous="$(read_digest_file "$DEPLOYED_DIGEST_PREVIOUS_FILE")" || { printf 'manual rollback digest is unavailable or invalid: %s\n' "$DEPLOYED_DIGEST_PREVIOUS_FILE" >&2; return 1; }
  current_api="$(configured_image "$API_CONTAINER")" || return 78; current_scheduler="$(configured_image "$SCHEDULER_CONTAINER")" || return 78; current_worker="$(worker_rollback_image "$current_api")"
  docker pull "$previous"; docker rm -f "$API_CONTAINER" "$SCHEDULER_CONTAINER" "$WORKER_CONTAINER" >/dev/null || return 1
  if ! run_api "$previous" >/dev/null || ! run_scheduler "$previous" >/dev/null || ! run_worker "$previous" >/dev/null || ! wait_for_healthz || ! wait_for_worker_startup || ! deploy_mcp "$previous"; then rollback_mcp "$current_api" || true; rollback_core "$current_api" "$current_scheduler" "$current_worker" || true; return 1; fi
  write_deployed_digest "$previous"
}

[[ "$DEPLOY_MODE" == rollback ]] && manual_rollback || main
