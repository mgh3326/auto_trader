#!/usr/bin/env bash
# Pull and promote a digest-pinned NCP deployment. API and MCP backends are
# private behind HAProxy; never add a wildcard or public bind here.
set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
readonly HAPROXY_IMAGE="haproxy:3.1-alpine"
usage() { printf 'usage: %s [tag|--rollback] [--skip-kis-ws] [--dry-run]\n' "$0" >&2; exit 64; }

SKIP_KIS_WS=0 DRY_RUN=0 DEPLOY_MODE=deploy IMAGE_TAG=main TAG_SET=0
for arg in "$@"; do
  case "$arg" in
    --skip-kis-ws) SKIP_KIS_WS=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --rollback) DEPLOY_MODE=rollback ;;
    -*) usage ;;
    *)
      if ((TAG_SET)); then usage; fi
      TAG_SET=1 IMAGE_TAG="$arg" ;;
  esac
done
if [[ "$DEPLOY_MODE" == rollback ]]; then
  if ((TAG_SET)); then usage; fi
  IMAGE_TAG=""
fi
readonly SKIP_KIS_WS DRY_RUN DEPLOY_MODE IMAGE_TAG

readonly IMAGE="${IMAGE_REPOSITORY}:${IMAGE_TAG}"
readonly RUN_DIRECTORY="${AT_RUN_DIRECTORY:-/root/at-run}"
readonly API_ACTIVE_COLOR_FILE="${RUN_DIRECTORY}/api-active-color"
readonly MCP_ACTIVE_COLOR_FILE="${RUN_DIRECTORY}/mcp-active-color"
readonly HAPROXY_CONFIG="${RUN_DIRECTORY}/haproxy.cfg"
readonly HAPROXY_CONFIG_PREVIOUS="${RUN_DIRECTORY}/haproxy.cfg.previous"
readonly HAPROXY_TEMPLATE="${MCP_HAPROXY_TEMPLATE:-$(cd "$(dirname "$0")/.." && pwd)/ops/ncp/haproxy/haproxy.cfg.tmpl}"
readonly HAPROXY_CONTAINER=at-haproxy
readonly API_DRAIN_SECONDS="${API_DRAIN_SECONDS:-120}"
readonly MCP_DRAIN_SECONDS="${MCP_DRAIN_SECONDS:-3600}"
readonly MCP_HEARTBEAT_DIRECTORY="${RUN_DIRECTORY}/mcp-heartbeat"
readonly MCP_HEALTH_ATTEMPTS="${MCP_HEALTH_ATTEMPTS:-30}"
readonly MCP_HEALTH_SLEEP_SECONDS="${MCP_HEALTH_SLEEP_SECONDS:-2}"
readonly HAPROXY_READY_ATTEMPTS="${HAPROXY_READY_ATTEMPTS:-20}"
readonly HAPROXY_READY_INTERVAL="${HAPROXY_READY_INTERVAL:-0.5}"
readonly MCP_UNITS_SKIP="${MCP_UNITS_SKIP:-}"
readonly HEALTHZ_ATTEMPTS="${AT_HEALTHZ_ATTEMPTS:-30}"
readonly HEALTHZ_SLEEP_SECONDS="${AT_HEALTHZ_SLEEP_SECONDS:-2}"
readonly RUNTIME_ENV_FILE="${AT_RUNTIME_ENV_FILE:-${RUN_DIRECTORY}/.env.runtime}"
readonly SECRETS_ENV_FILE="${AT_SECRETS_ENV_FILE:-${RUN_DIRECTORY}/.env.secrets}"
readonly DEPLOYED_DIGEST_FILE="${RUN_DIRECTORY}/deployed-digest"
readonly DEPLOYED_DIGEST_PREVIOUS_FILE="${RUN_DIRECTORY}/deployed-digest.previous"

declare -a ENV_FILE_ARGS=(--env-file "$RUNTIME_ENV_FILE" --env-file "$SECRETS_ENV_FILE")
declare -a MCP_NAMES=(analysis-readonly account-read tradingcodex-execution paper-001 kiwoom)
declare -a MCP_PROFILES=(analysis_readonly account_read tradingcodex_execution hermes-paper-kis kiwoom)
declare -a MCP_PORTS=(8768 8769 8770 8771 8772)
declare -a MCP_TOKENS=(MCP_ANALYSIS_READONLY_AUTH_TOKEN MCP_ACCOUNT_READ_AUTH_TOKEN MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN MCP_PAPER_001_AUTH_TOKEN MCP_KIWOOM_AUTH_TOKEN)
API_DRAIN_PENDING_COLOR=""
MCP_DRAIN_PENDING_COLOR=""
declare -a APP_CONTAINERS=(at-api at-api-blue at-api-green at-worker at-worker-new at-scheduler at-upbit-ws at-kis-ws at-mcp-blue at-mcp-green at-mcp-analysis-readonly at-mcp-account-read at-mcp-tradingcodex-execution at-mcp-paper-001 at-mcp-kiwoom)
declare -a REPLACED_CONTAINERS=()
declare -A ORIGINAL_IMAGES=() EXPECTED_IMAGES=()
ORIGINAL_API_COLOR=""
ORIGINAL_MCP_COLOR=""
ORIGINAL_HAPROXY_CONFIG=""
ORIGINAL_HAPROXY_CONFIG_EXISTS=false
ORIGINAL_HAPROXY_PRESENT=false
DRAIN_GUARD=""
ORIGINAL_DEPLOYED_DIGEST_CONTENT="" ORIGINAL_PREVIOUS_DIGEST_CONTENT=""
ORIGINAL_DEPLOYED_DIGEST_EXISTS=false ORIGINAL_PREVIOUS_DIGEST_EXISTS=false
DIGEST_RECORD_MUTATED=false

require_command() { command -v "$1" >/dev/null 2>&1 || { printf 'required command is unavailable: %s\n' "$1" >&2; exit 127; }; }
require_file() { [[ -f "$1" ]] || { printf 'required env file is unavailable: %s\n' "$1" >&2; exit 78; }; }
is_digest() { [[ "$1" =~ ^${IMAGE_REPOSITORY}@sha256:[[:xdigit:]]{64}$ ]]; }
configured_image() { docker inspect --format '{{.Config.Image}}' "$1"; }
container_presence() {
  local name="$1" detail listed found
  if detail="$(docker inspect --format '{{.Id}}' "$name" 2>&1)"; then
    [[ -n "$detail" ]] && return 0
  fi
  # Confirm absence through a second daemon query. An inspect error alone may
  # be a transient daemon failure and must never turn a prior unit into ABSENT.
  listed="$(docker ps -a --format '{{.Names}}' 2>/dev/null)" || { printf 'cannot determine container presence: %s\n' "$name" >&2; return 2; }
  found=false
  while IFS= read -r detail; do if [[ "$detail" == "$name" ]]; then found=true; fi; done <<<"$listed"
  [[ "$found" == false ]] && return 1
  printf 'cannot determine container presence: %s\n' "$name" >&2
  return 2
}
container_running() { [[ "$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null)" == true ]]; }
read_color() { local color; [[ -f "$2" ]] && IFS= read -r color <"$2" && [[ "$color" == blue || "$color" == green ]] && printf '%s\n' "$color"; }
write_color() { local color="$1" file="$2" tmp; [[ "$color" == blue || "$color" == green ]] || return 64; mkdir -p "$RUN_DIRECTORY"; umask 077; tmp="$(mktemp "${RUN_DIRECTORY}/.$(basename "$file").XXXXXX")"; printf '%s\n' "$color" >"$tmp"; mv -f "$tmp" "$file"; }
other_color() { [[ "$1" == blue ]] && printf '%s\n' green || printf '%s\n' blue; }
api_port() { [[ "$1" == blue ]] && printf 8001 || printf 8002; }
mcp_port() { [[ "$1" == blue ]] && printf 8766 || printf 8767; }
read_digest() { [[ -f "$1" ]] && IFS= read -r digest <"$1" && is_digest "$digest" && printf '%s\n' "$digest"; }

# Resolve the running container's own immutable repository digest. Docker's
# container inspect may not expose RepoDigests; image inspect by content ID is
# the authoritative fallback. A different unit's digest is not a rollback
# reference for this container.
unit_rollback_image() {
  local container="$1" image resolved image_id
  image="$(configured_image "$container" 2>/dev/null)" || { printf 'rollback image is unavailable: %s is absent\n' "$container" >&2; return 1; }
  is_digest "$image" && { printf '%s\n' "$image"; return 0; }
  resolved="$(docker inspect --format '{{index .RepoDigests 0}}' "$container" 2>/dev/null || true)"
  is_digest "$resolved" && { printf '%s\n' "$resolved"; return 0; }
  image_id="$(docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true)"
  if [[ -n "$image_id" ]]; then
    resolved="$(docker image inspect --format '{{index .RepoDigests 0}}' "$image_id" 2>/dev/null || true)"
    is_digest "$resolved" && { printf '%s\n' "$resolved"; return 0; }
  fi
  printf 'rollback digest is unavailable for %s\n' "$container" >&2
  return 1
}

# Capture every app container before the first mutation. The replacement log
# records intent immediately before removing an instance, including failed
# starts; rollback visits that log backwards and restores only logged units.
capture_initial_state() {
  local name presence
  for name in "${APP_CONTAINERS[@]}"; do
    presence=0
    container_presence "$name" || presence=$?
    if ((presence == 0)); then
      container_running "$name" || { printf 'container is not running: %s\n' "$name" >&2; return 78; }
      ORIGINAL_IMAGES["$name"]="$(unit_rollback_image "$name")" || return 1
    elif ((presence == 1)); then
      ORIGINAL_IMAGES["$name"]=ABSENT
    else
      return 78
    fi
    EXPECTED_IMAGES["$name"]="${ORIGINAL_IMAGES[$name]}"
  done
  ORIGINAL_API_COLOR="$(read_color api "$API_ACTIVE_COLOR_FILE" 2>/dev/null || true)"
  ORIGINAL_MCP_COLOR="$(read_color mcp "$MCP_ACTIVE_COLOR_FILE" 2>/dev/null || true)"
  presence=0
  container_presence "$HAPROXY_CONTAINER" || presence=$?
  if ((presence == 0)); then ORIGINAL_HAPROXY_PRESENT=true
  elif ((presence != 1)); then return 78
  fi
  if [[ -f "$HAPROXY_CONFIG" ]]; then
    ORIGINAL_HAPROXY_CONFIG="$(cat "$HAPROXY_CONFIG" && printf .)" || return 1
    ORIGINAL_HAPROXY_CONFIG_EXISTS=true
  fi
  if [[ -f "$DEPLOYED_DIGEST_FILE" ]]; then
    ORIGINAL_DEPLOYED_DIGEST_CONTENT="$(cat "$DEPLOYED_DIGEST_FILE" && printf .)" || return 1
    ORIGINAL_DEPLOYED_DIGEST_EXISTS=true
  fi
  if [[ -f "$DEPLOYED_DIGEST_PREVIOUS_FILE" ]]; then
    ORIGINAL_PREVIOUS_DIGEST_CONTENT="$(cat "$DEPLOYED_DIGEST_PREVIOUS_FILE" && printf .)" || return 1
    ORIGINAL_PREVIOUS_DIGEST_EXISTS=true
  fi
}
record_replacement() { REPLACED_CONTAINERS+=("$1"); }

running_digest() {
  local name="$1" presence=0 state
  container_presence "$name" 2>/dev/null || presence=$?
  if ((presence == 1)); then printf 'ABSENT\n'; return 0; fi
  if ((presence != 0)); then printf 'UNKNOWN\n'; return 0; fi
  state="$(docker inspect --format '{{.State.Running}}' "$name" 2>/dev/null)" || { printf 'UNKNOWN\n'; return 0; }
  [[ "$state" == true ]] || { printf 'STOPPED\n'; return 0; }
  unit_rollback_image "$name" 2>/dev/null || printf 'UNKNOWN\n'
}
report_digests() {
  local name expected running status failed=0
  printf 'container\texpected\trunning\tstatus\n'
  for name in "${APP_CONTAINERS[@]}"; do
    expected="${EXPECTED_IMAGES[$name]:-ABSENT}"
    running="$(running_digest "$name")"
    status=MATCH
    if [[ "$expected" != "$running" ]]; then status=MISMATCH; failed=1; fi
    printf '%s\t%s\t%s\t%s\n' "$name" "$expected" "$running" "$status"
  done
  return "$failed"
}

restore_unit() {
  local name="$1" image="${ORIGINAL_IMAGES[$1]}" i
  docker rm -f "$name" >/dev/null 2>&1 || true
  [[ "$image" != ABSENT ]] || return 0
  case "$name" in
    at-api-blue|at-api-green) run_api "${name#at-api-}" "$image" >/dev/null ;;
    at-api) docker run -d --name at-api --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" "$image" >/dev/null ;;
    at-worker|at-worker-new) run_worker "$name" "$image" >/dev/null ;;
    at-scheduler) run_scheduler "$image" >/dev/null ;;
    at-upbit-ws) run_ws "$name" "$image" upbit >/dev/null ;;
    at-kis-ws) run_ws "$name" "$image" kis >/dev/null ;;
    at-mcp-blue|at-mcp-green) run_mcp "${name#at-mcp-}" "$(mcp_port "${name#at-mcp-}")" default MCP_AUTH_TOKEN "${name#at-mcp-}" "$image" >/dev/null ;;
    at-mcp-*)
      for i in "${!MCP_NAMES[@]}"; do
        if [[ "$name" == "at-mcp-${MCP_NAMES[$i]}" ]]; then
          run_mcp "${MCP_NAMES[$i]}" "${MCP_PORTS[$i]}" "${MCP_PROFILES[$i]}" "${MCP_TOKENS[$i]}" '' "$image" >/dev/null
          return $?
        fi
      done
      printf 'unknown MCP rollback unit: %s\n' "$name" >&2
      return 1 ;;
    *) printf 'unknown rollback unit: %s\n' "$name" >&2; return 1 ;;
  esac
}

rollback_replaced() {
  local i name failed=0
  cancel_drains || failed=1
  for ((i=${#REPLACED_CONTAINERS[@]}-1; i>=0; i--)); do
    name="${REPLACED_CONTAINERS[$i]}"
    printf 'restoring %s to %s\n' "$name" "${ORIGINAL_IMAGES[$name]}" >&2
    restore_unit "$name" || failed=1
  done
  if [[ "$ORIGINAL_HAPROXY_PRESENT" == false ]]; then
    docker rm -f "$HAPROXY_CONTAINER" >/dev/null 2>&1 || true
    local presence=0
    container_presence "$HAPROXY_CONTAINER" || presence=$?
    ((presence == 1)) || failed=1
    if [[ "$ORIGINAL_HAPROXY_CONFIG_EXISTS" == true ]]; then
      printf '%s' "${ORIGINAL_HAPROXY_CONFIG%.}" >"$HAPROXY_CONFIG" || failed=1
    else
      rm -f "$HAPROXY_CONFIG" || failed=1
    fi
  elif [[ "$ORIGINAL_HAPROXY_CONFIG_EXISTS" == true ]]; then
    printf '%s' "${ORIGINAL_HAPROXY_CONFIG%.}" >"$HAPROXY_CONFIG"
    reload_haproxy || failed=1
  elif [[ -n "$ORIGINAL_API_COLOR" ]]; then
    render_haproxy "$ORIGINAL_API_COLOR" "${ORIGINAL_MCP_COLOR:-blue}" && reload_haproxy || failed=1
  else
    rm -f "$HAPROXY_CONFIG" || failed=1
  fi
  if [[ -n "$ORIGINAL_API_COLOR" ]]; then write_color "$ORIGINAL_API_COLOR" "$API_ACTIVE_COLOR_FILE" || failed=1
  else rm -f "$API_ACTIVE_COLOR_FILE" || failed=1; fi
  if [[ -n "$ORIGINAL_MCP_COLOR" ]]; then write_color "$ORIGINAL_MCP_COLOR" "$MCP_ACTIVE_COLOR_FILE" || failed=1
  else rm -f "$MCP_ACTIVE_COLOR_FILE" || failed=1; fi
  if [[ "$DIGEST_RECORD_MUTATED" == true ]]; then
    restore_digest_record "$DEPLOYED_DIGEST_FILE" "$ORIGINAL_DEPLOYED_DIGEST_EXISTS" "$ORIGINAL_DEPLOYED_DIGEST_CONTENT" || failed=1
    restore_digest_record "$DEPLOYED_DIGEST_PREVIOUS_FILE" "$ORIGINAL_PREVIOUS_DIGEST_EXISTS" "$ORIGINAL_PREVIOUS_DIGEST_CONTENT" || failed=1
  fi
  return "$failed"
}

restore_digest_record() {
  local file="$1" existed="$2" content="$3"
  if [[ "$existed" == true ]]; then
    printf '%s' "${content%.}" >"$file"
  elif [[ ! -d "$file" ]]; then
    rm -f "$file"
  fi
}

mcp_unit_is_skipped() { [[ ",$MCP_UNITS_SKIP," == *",$1,"* ]]; }

# Read-only summary used by the dry-run plan: the unit's immutable repo digest
# when one is discoverable, or an honest marker when it is absent/unresolved.
unit_image_summary() {
  local container="$1" image resolved presence=0 state
  container_presence "$container" 2>/dev/null || presence=$?
  if ((presence == 1)); then printf 'absent'; return 0; fi
  if ((presence != 0)); then printf 'unresolved (inspect failed)'; return 0; fi
  state="$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null)" || { printf 'unresolved (inspect failed)'; return 0; }
  [[ "$state" == true ]] || { printf 'stopped'; return 0; }
  resolved="$(unit_rollback_image "$container" 2>/dev/null || true)"
  is_digest "$resolved" && { printf '%s\n' "$resolved"; return 0; }
  image="$(configured_image "$container" 2>/dev/null || true)"
  if [[ -n "$image" ]]; then printf 'unresolved (configured: %s)' "$image"; else printf 'unresolved'; fi
}
env_value() { local key="$1" file line value=""; for file in "$RUNTIME_ENV_FILE" "$SECRETS_ENV_FILE"; do line="$(awk -v key="$key" '$0 ~ "^[[:space:]]*(export[[:space:]]+)?" key "=" { sub("^[[:space:]]*(export[[:space:]]+)?" key "=", ""); print }' "$file" | tail -n 1)"; [[ -n "$line" ]] && value="$line"; done; value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"; [[ -n "${value//[[:space:]]/}" ]] && printf '%s' "$value"; }
validate_mcp_tokens() { local i; env_value MCP_AUTH_TOKEN >/dev/null || { printf 'MCP_AUTH_TOKEN is required\n' >&2; return 78; }; for i in "${!MCP_NAMES[@]}"; do mcp_unit_is_skipped "${MCP_NAMES[$i]}" && continue; env_value "${MCP_TOKENS[$i]}" >/dev/null || { printf '%s is required\n' "${MCP_TOKENS[$i]}" >&2; return 78; }; done; }

run_api() { local color="$1" image="$2" port; port="$(api_port "$color")"; docker run -d --name "at-api-${color}" --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" "$image" /app/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$port"; }
run_scheduler() { docker run -d --name at-scheduler --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" "$1" /app/.venv/bin/taskiq scheduler app.core.scheduler:sched app.tasks; }
run_worker() { docker run -d --name "$1" --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" "$2" /app/.venv/bin/taskiq worker app.core.taskiq_broker:broker app.tasks --workers 1; }
run_ws() { docker run -d --name "$1" --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" "$2" /app/.venv/bin/python websocket_monitor.py --mode "$3"; }
wait_health() { local port="$1" attempt status; for ((attempt=1; attempt<=HEALTHZ_ATTEMPTS; attempt++)); do status="$(curl --silent --show-error --max-time 3 --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${port}/healthz")" && [[ "$status" == 200 ]] && return 0; printf 'waiting for API healthz (%s/%s)\n' "$attempt" "$HEALTHZ_ATTEMPTS" >&2; sleep "$HEALTHZ_SLEEP_SECONDS"; done; return 1; }
wait_worker() { local name="$1" attempt; for ((attempt=1; attempt<=HEALTHZ_ATTEMPTS; attempt++)); do docker logs --tail 100 "$name" 2>&1 | grep -Eq 'Listening started|Starting 1 worker processes.' && return 0; sleep "$HEALTHZ_SLEEP_SECONDS"; done; printf 'worker did not report Listening started\n' >&2; return 1; }
wait_ws() { local name="$1" attempt; for ((attempt=1; attempt<=HEALTHZ_ATTEMPTS; attempt++)); do docker logs --tail 100 "$name" 2>&1 | grep -Eq 'Unified WebSocket health:.*connected=True|connected=True' && return 0; sleep "$HEALTHZ_SLEEP_SECONDS"; done; return 1; }

# Keep 0644 and preserve the existing inode: deploy umask 077 otherwise makes
# the bind-mounted config unreadable, and mv leaves a file bind mount stale.
render_haproxy() {
  local api mcp tmp
  api="$(api_port "$1")"; mcp="$(mcp_port "$2")"; tmp="${HAPROXY_CONFIG}.tmp"
  [[ -f "$HAPROXY_TEMPLATE" ]] || return 78; mkdir -p "$RUN_DIRECTORY"
  sed -e "s/__API_ACTIVE_PORT__/${api}/g" -e "s/__MCP_ACTIVE_PORT__/${mcp}/g" "$HAPROXY_TEMPLATE" >"$tmp"
  if grep -q '0.0.0.0' "$tmp" || ! grep -q 'bind 127.0.0.1:8000' "$tmp" || ! grep -q 'bind 100.122.100.56:8000' "$tmp"; then rm -f "$tmp"; printf 'HAProxy binds must be loopback and tailnet only\n' >&2; return 78; fi
  chmod 0644 "$tmp"
  if [[ -e "$HAPROXY_CONFIG" ]]; then cp "$HAPROXY_CONFIG" "$HAPROXY_CONFIG_PREVIOUS"; cat "$tmp" >"$HAPROXY_CONFIG" && rm -f "$tmp"; else mv -f "$tmp" "$HAPROXY_CONFIG"; fi
}
wait_haproxy_ready() {
  local attempt
  for ((attempt=1; attempt<=HAPROXY_READY_ATTEMPTS; attempt++)); do
    if curl --fail --silent --max-time 3 http://127.0.0.1:8000/healthz >/dev/null \
      && curl --fail --silent --max-time 3 http://127.0.0.1:8765/health >/dev/null; then
      return 0
    fi
    sleep "$HAPROXY_READY_INTERVAL"
  done
  printf 'haproxy routes not ready after reload\n' >&2
  return 1
}

reload_haproxy() {
  local presence=0
  container_presence "$HAPROXY_CONTAINER" || presence=$?
  if ((presence == 0)); then docker kill -s HUP "$HAPROXY_CONTAINER" >/dev/null || return 1
  elif ((presence == 1)); then docker run -d --name "$HAPROXY_CONTAINER" --restart unless-stopped --network host -v "${HAPROXY_CONFIG}:/usr/local/etc/haproxy/haproxy.cfg:ro" "$HAPROXY_IMAGE" -W -db -f /usr/local/etc/haproxy/haproxy.cfg >/dev/null || return 1
  else return 78
  fi
  wait_haproxy_ready
}

# The foreground inspect is a deterministic scheduling record. The detached
# child removes only the captured ID, and only after the pending guard is armed
# at the very end of a successful promotion. Rollback deletes that guard.
schedule_drain() { local name="$1" seconds="$2" id pid_file log_file; id="$(docker inspect --format '{{.Id}}' "$name" 2>/dev/null)" || return 0; printf 'scheduled drain: %s\n' "$name" >&2; pid_file="${RUN_DIRECTORY}/${name}-drain.pid"; log_file="${RUN_DIRECTORY}/${name}-drain.log"; : >"$log_file" || return 1; nohup bash -c 'sleep "$1"; [[ "$(cat "$4" 2>/dev/null || true)" == armed ]] && [[ "$(docker inspect --format "{{.Id}}" "$2" 2>/dev/null || true)" == "$3" ]] && docker rm -f "$2" >/dev/null 2>&1 || true' _ "$seconds" "$name" "$id" "$DRAIN_GUARD" >"$log_file" 2>&1 & printf '%s\n' "$!" >"$pid_file"; }

deploy_api() {
  local image="$1" old new mcp old_legacy=""
  old="$(read_color api "$API_ACTIVE_COLOR_FILE" 2>/dev/null || true)"; mcp="$(read_color mcp "$MCP_ACTIVE_COLOR_FILE" 2>/dev/null || printf blue)"; [[ -n "$old" ]] && new="$(other_color "$old")" || new=blue
  record_replacement "at-api-${new}"
  docker rm -f "at-api-${new}" >/dev/null 2>&1 || true; run_api "$new" "$image" >/dev/null || return 1; wait_health "$(api_port "$new")" || return 1
  if [[ -z "$old" ]]; then
    old_legacy="$(configured_image at-api 2>/dev/null || true)"
    if [[ -n "$old_legacy" ]]; then record_replacement at-api; docker rm -f at-api >/dev/null 2>&1 || true; fi
  fi
  render_haproxy "$new" "$mcp" && reload_haproxy && write_color "$new" "$API_ACTIVE_COLOR_FILE" || return 1
  API_DRAIN_PENDING_COLOR="$old"
}
deploy_worker() { local image="$1"; record_replacement at-worker-new; docker rm -f at-worker-new >/dev/null 2>&1 || true; run_worker at-worker-new "$image" >/dev/null || return 1; wait_worker at-worker-new || return 1; record_replacement at-worker; docker stop -t 60 at-worker >/dev/null 2>&1 || true; docker rm at-worker >/dev/null 2>&1 || true; docker rename at-worker-new at-worker; }
deploy_singletons() {
  local image="$1"
  record_replacement at-scheduler; docker rm -f at-scheduler >/dev/null 2>&1 || true; run_scheduler "$image" >/dev/null || return 1
  record_replacement at-upbit-ws; docker rm -f at-upbit-ws >/dev/null 2>&1 || true; run_ws at-upbit-ws "$image" upbit >/dev/null || return 1; wait_ws at-upbit-ws || return 1
  if ((SKIP_KIS_WS)); then
    printf 'at-kis-ws skipped; retaining digest %s\n' "${ORIGINAL_IMAGES[at-kis-ws]}"
  else
    record_replacement at-kis-ws; docker rm -f at-kis-ws >/dev/null 2>&1 || true; run_ws at-kis-ws "$image" kis >/dev/null || return 1; wait_ws at-kis-ws || return 1
  fi
}

run_mcp() { local name="$1" port="$2" profile="$3" token_env="$4" color="$5" image="$6" token heartbeat; local -a policy_args=(); token="$(env_value "$token_env")" || return 78; heartbeat="/var/run/auto-trader/mcp-heartbeat/mcp-${color:-$name}.json"; [[ "$profile" == tradingcodex_execution ]] && policy_args=(-e ORDER_APPROVAL_HASH_MODE=required -e TOSS_APPROVAL_HASH_MODE=required); docker run -d --name "at-mcp-${name}" --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" -v "${MCP_HEARTBEAT_DIRECTORY}:/var/run/auto-trader/mcp-heartbeat" "${policy_args[@]}" -e "MCP_AUTH_TOKEN=${token}" -e "MCP_PROFILE=${profile}" -e MCP_HOST=127.0.0.1 -e "MCP_PORT=${port}" -e MCP_TYPE=streamable-http -e MCP_PATH=/mcp -e MCP_USER_ID=1 -e "AUTO_TRADER_COLOR=${color:-$name}" -e "MCP_HEARTBEAT_PATH=${heartbeat}" "$image" python -m app.mcp_server.main; }
wait_mcp() { local port="$1" attempt status; for ((attempt=1; attempt<=MCP_HEALTH_ATTEMPTS; attempt++)); do status="$(curl --silent --show-error --max-time 3 --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${port}/health")" && [[ "$status" == 200 ]] && return 0; sleep "$MCP_HEALTH_SLEEP_SECONDS"; done; return 1; }
deploy_mcp() {
  local image="$1" old new i
  mkdir -p "$MCP_HEARTBEAT_DIRECTORY"; chmod 1777 "$MCP_HEARTBEAT_DIRECTORY"
  old="$(read_color mcp "$MCP_ACTIVE_COLOR_FILE" 2>/dev/null || true)"; [[ -n "$old" ]] && new="$(other_color "$old")" || new=blue
  record_replacement "at-mcp-${new}"
  docker rm -f "at-mcp-${new}" >/dev/null 2>&1 || true; run_mcp "$new" "$(mcp_port "$new")" default MCP_AUTH_TOKEN "$new" "$image" >/dev/null || return 1; wait_mcp "$(mcp_port "$new")" || return 1
  for i in "${!MCP_NAMES[@]}"; do mcp_unit_is_skipped "${MCP_NAMES[$i]}" && continue; record_replacement "at-mcp-${MCP_NAMES[$i]}"; docker rm -f "at-mcp-${MCP_NAMES[$i]}" >/dev/null 2>&1 || true; run_mcp "${MCP_NAMES[$i]}" "${MCP_PORTS[$i]}" "${MCP_PROFILES[$i]}" "${MCP_TOKENS[$i]}" '' "$image" >/dev/null || return 1; wait_mcp "${MCP_PORTS[$i]}" || return 1; done
  render_haproxy "$(read_color api "$API_ACTIVE_COLOR_FILE")" "$new" && reload_haproxy && write_color "$new" "$MCP_ACTIVE_COLOR_FILE" || return 1
  MCP_DRAIN_PENDING_COLOR="$old"
}

write_digest() {
  local digest="$1" tmp old
  is_digest "$digest" || return 1
  [[ ! -d "$DEPLOYED_DIGEST_FILE" && ! -d "$DEPLOYED_DIGEST_PREVIOUS_FILE" ]] || return 1
  mkdir -p "$RUN_DIRECTORY" || return 1
  umask 077
  tmp="$(mktemp "${RUN_DIRECTORY}/.deployed-digest.XXXXXX")" || return 1
  printf '%s\n' "$digest" >"$tmp" || { rm -f "$tmp"; return 1; }
  if old="$(read_digest "$DEPLOYED_DIGEST_FILE")"; then
    printf '%s\n' "$old" >"$DEPLOYED_DIGEST_PREVIOUS_FILE" || { rm -f "$tmp"; return 1; }
  fi
  mv -f "$tmp" "$DEPLOYED_DIGEST_FILE" || { rm -f "$tmp"; return 1; }
}
finalize_drains() {
  [[ -n "$API_DRAIN_PENDING_COLOR" || -n "$MCP_DRAIN_PENDING_COLOR" ]] || return 0
  DRAIN_GUARD="$(mktemp "${RUN_DIRECTORY}/.drain-guard.XXXXXX")" || return 1
  printf 'pending\n' >"$DRAIN_GUARD" || return 1
  if [[ -n "$API_DRAIN_PENDING_COLOR" ]]; then schedule_drain "at-api-${API_DRAIN_PENDING_COLOR}" "$API_DRAIN_SECONDS" || return 1; fi
  if [[ -n "$MCP_DRAIN_PENDING_COLOR" ]]; then schedule_drain "at-mcp-${MCP_DRAIN_PENDING_COLOR}" "$MCP_DRAIN_SECONDS" || return 1; fi
}
arm_drains() {
  local ready
  [[ -n "$DRAIN_GUARD" ]] || return 0
  ready="${DRAIN_GUARD}.ready"
  printf 'armed\n' >"$ready" || return 1
  mv -f "$ready" "$DRAIN_GUARD"
}
cancel_drains() { [[ -z "$DRAIN_GUARD" ]] || rm -f "$DRAIN_GUARD" "${DRAIN_GUARD}.ready"; }
current_api_rollback_digest() { local color presence=0; container_presence at-api || presence=$?; if ((presence == 0)); then unit_rollback_image at-api; return $?; fi; ((presence == 1)) || return 78; color="$(read_color api "$API_ACTIVE_COLOR_FILE" 2>/dev/null || true)"; [[ -n "$color" ]] || { printf 'previous API container is required for rollback\n' >&2; return 1; }; unit_rollback_image "at-api-${color}"; }
set_promoted_expectations() {
  local name
  for name in "${REPLACED_CONTAINERS[@]}"; do
    case "$name" in
      at-worker-new|at-api) EXPECTED_IMAGES["$name"]=ABSENT ;;
      *) EXPECTED_IMAGES["$name"]="$1" ;;
    esac
  done
}
deployment_failed() {
  local failed=0
  rollback_replaced || failed=1
  report_digests || failed=1
  if ((failed)); then printf 'rollback or digest verification is incomplete\n' >&2; fi
  return 1
}
promote_digest() {
  local digest="$1" name
  deploy_api "$digest" || { deployment_failed; return 1; }
  deploy_worker "$digest" || { deployment_failed; return 1; }
  deploy_singletons "$digest" || { deployment_failed; return 1; }
  deploy_mcp "$digest" || { deployment_failed; return 1; }
  set_promoted_expectations "$digest"
  if ! report_digests; then
    printf 'deployment digest mismatch; restoring prior containers\n' >&2
    for name in "${APP_CONTAINERS[@]}"; do EXPECTED_IMAGES["$name"]="${ORIGINAL_IMAGES[$name]}"; done
    deployment_failed
    return 1
  fi
  if ! finalize_drains; then
    printf 'drain scheduling failed; restoring prior containers\n' >&2
    for name in "${APP_CONTAINERS[@]}"; do EXPECTED_IMAGES["$name"]="${ORIGINAL_IMAGES[$name]}"; done
    deployment_failed
    return 1
  fi
  DIGEST_RECORD_MUTATED=true
  if ! write_digest "$digest"; then
    printf 'digest record failed; restoring prior containers\n' >&2
    for name in "${APP_CONTAINERS[@]}"; do EXPECTED_IMAGES["$name"]="${ORIGINAL_IMAGES[$name]}"; done
    deployment_failed
    return 1
  fi
  if ! arm_drains; then
    printf 'drain activation failed; restoring prior containers\n' >&2
    for name in "${APP_CONTAINERS[@]}"; do EXPECTED_IMAGES["$name"]="${ORIGINAL_IMAGES[$name]}"; done
    deployment_failed
    return 1
  fi
  printf 'deployment completed: %s\n' "$digest"
}
prepare() { require_command docker; require_command curl; require_command awk; require_file "$RUNTIME_ENV_FILE"; require_file "$SECRETS_ENV_FILE"; validate_mcp_tokens; }
main() { local digest; prepare || exit $?; current_api_rollback_digest >/dev/null || exit 78; capture_initial_state || exit 78; docker pull "$IMAGE"; digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE")"; is_digest "$digest" || { printf 'could not resolve repo digest\n' >&2; exit 1; }; promote_digest "$digest"; }
manual_rollback() { local previous; prepare || return $?; previous="$(read_digest "$DEPLOYED_DIGEST_PREVIOUS_FILE")" || { printf 'manual rollback digest is unavailable\n' >&2; return 1; }; current_api_rollback_digest >/dev/null || return 78; capture_initial_state || return 78; docker pull "$previous"; promote_digest "$previous"; }

# Read-only plan. Inspect calls only: no pull/run/rm/stop/rename/kill and no
# route or color file writes. When no immutable reference is available the
# plan says unresolved rather than claiming a digest.
dry_run() {
  local target="" planned verb old_api old_mcp new_api new_mcp i name
  require_command docker
  printf 'dry-run: read-only plan; no image pull, no container mutation, no route or file writes\n'
  if [[ "$DEPLOY_MODE" == rollback ]]; then
    printf 'mode: rollback\n'
    if target="$(read_digest "$DEPLOYED_DIGEST_PREVIOUS_FILE")"; then
      printf 'rollback target digest: %s\n' "$target"
    else
      printf 'rollback target digest: unresolved (%s is absent or invalid)\n' "$DEPLOYED_DIGEST_PREVIOUS_FILE"
    fi
    verb='restore to'
  else
    printf 'mode: deploy\n'
    printf 'target image: %s\n' "$IMAGE"
    target="$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null || true)"
    if is_digest "$target"; then
      printf 'intended digest: %s\n' "$target"
    else
      target=""
      printf 'intended digest: unresolved (no local repo digest for %s; a real run pulls first)\n' "$IMAGE"
    fi
    verb='promote to'
  fi
  planned="${target:-unresolved}"
  old_api="$(read_color api "$API_ACTIVE_COLOR_FILE" 2>/dev/null || true)"
  old_mcp="$(read_color mcp "$MCP_ACTIVE_COLOR_FILE" 2>/dev/null || true)"
  if [[ -n "$old_api" ]]; then new_api="$(other_color "$old_api")"; else new_api=blue; fi
  if [[ -n "$old_mcp" ]]; then new_mcp="$(other_color "$old_mcp")"; else new_mcp=blue; fi
  printf 'planned actions per unit:\n'
  if [[ -n "$old_api" ]]; then
    printf '  at-api-%s -> at-api-%s: %s %s; current: %s\n' "$old_api" "$new_api" "$verb" "$planned" "$(unit_image_summary "at-api-$old_api")"
  else
    printf '  at-api -> at-api-%s: %s %s; current: %s\n' "$new_api" "$verb" "$planned" "$(unit_image_summary at-api)"
  fi
  printf '  at-worker: %s %s via at-worker-new rename; current: %s\n' "$verb" "$planned" "$(unit_image_summary at-worker)"
  printf '  at-scheduler: %s %s; current: %s\n' "$verb" "$planned" "$(unit_image_summary at-scheduler)"
  printf '  at-upbit-ws: %s %s; current: %s\n' "$verb" "$planned" "$(unit_image_summary at-upbit-ws)"
  if ((SKIP_KIS_WS)); then
    printf '  at-kis-ws: skip; reason: explicit --skip-kis-ws request; retained digest: %s\n' "$(unit_image_summary at-kis-ws)"
  else
    printf '  at-kis-ws: %s %s; current: %s\n' "$verb" "$planned" "$(unit_image_summary at-kis-ws)"
  fi
  if [[ -n "$old_mcp" ]]; then
    printf '  at-mcp-%s -> at-mcp-%s: %s %s; current: %s\n' "$old_mcp" "$new_mcp" "$verb" "$planned" "$(unit_image_summary "at-mcp-$old_mcp")"
  else
    printf '  at-mcp -> at-mcp-%s: %s %s; current active: unknown\n' "$new_mcp" "$verb" "$planned"
  fi
  for i in "${!MCP_NAMES[@]}"; do
    name="at-mcp-${MCP_NAMES[$i]}"
    if mcp_unit_is_skipped "${MCP_NAMES[$i]}"; then
      printf '  %s: skip; reason: MCP_UNITS_SKIP; retained: %s\n' "$name" "$(unit_image_summary "$name")"
    else
      printf '  %s: %s %s; current: %s\n' "$name" "$verb" "$planned" "$(unit_image_summary "$name")"
    fi
  done
  printf '  at-haproxy: render config and reload via SIGHUP (start if absent); deferred in dry-run\n'
  printf 'end of plan; a real run prints a per-container digest table for operator comparison\n'
}

if ((DRY_RUN)); then dry_run
elif [[ "$DEPLOY_MODE" == rollback ]]; then manual_rollback
else main
fi
