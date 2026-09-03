#!/usr/bin/env bash
# Pull and promote a digest-pinned NCP deployment. API and MCP backends are
# private behind HAProxy; never add a wildcard or public bind here.
set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
readonly HAPROXY_IMAGE="haproxy:3.1-alpine"
if (($# == 0)); then DEPLOY_MODE=deploy; IMAGE_TAG=main
elif [[ "$1" == --rollback && $# == 1 ]]; then DEPLOY_MODE=rollback; IMAGE_TAG=""
elif [[ "$1" != -* && $# == 1 ]]; then DEPLOY_MODE=deploy; IMAGE_TAG="$1"
else printf 'usage: %s [tag] | --rollback\n' "$0" >&2; exit 64; fi

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
declare -a MCP_PREVIOUS_NAMES=(blue green analysis-readonly account-read tradingcodex-execution paper-001 kiwoom)
declare -a MCP_PREVIOUS_IMAGES=() MCP_PREVIOUS_PRESENT=()
API_DRAIN_PENDING_COLOR=""
MCP_DRAIN_PENDING_COLOR=""

require_command() { command -v "$1" >/dev/null 2>&1 || { printf 'required command is unavailable: %s\n' "$1" >&2; exit 127; }; }
require_file() { [[ -f "$1" ]] || { printf 'required env file is unavailable: %s\n' "$1" >&2; exit 78; }; }
is_digest() { [[ "$1" =~ ^${IMAGE_REPOSITORY}@sha256:[[:xdigit:]]{64}$ ]]; }
configured_image() { docker inspect --format '{{.Config.Image}}' "$1"; }
container_exists() { docker inspect --format '{{.Id}}' "$1" >/dev/null 2>&1; }
read_color() { local color; [[ -f "$2" ]] && IFS= read -r color <"$2" && [[ "$color" == blue || "$color" == green ]] && printf '%s\n' "$color"; }
write_color() { local color="$1" file="$2" tmp; [[ "$color" == blue || "$color" == green ]] || return 64; mkdir -p "$RUN_DIRECTORY"; umask 077; tmp="$(mktemp "${RUN_DIRECTORY}/.$(basename "$file").XXXXXX")"; printf '%s\n' "$color" >"$tmp"; mv -f "$tmp" "$file"; }
other_color() { [[ "$1" == blue ]] && printf '%s\n' green || printf '%s\n' blue; }
api_port() { [[ "$1" == blue ]] && printf 8001 || printf 8002; }
mcp_port() { [[ "$1" == blue ]] && printf 8766 || printf 8767; }
read_digest() { [[ -f "$1" ]] && IFS= read -r digest <"$1" && is_digest "$digest" && printf '%s\n' "$digest"; }

# Rollback references must be immutable. A tag is resolved from the container's
# RepoDigests; if that is unavailable, only the previously validated API digest
# may bootstrap it. Without either, fail before replacing a runnable unit.
rollback_reference() { local image="$1" component="$2"; is_digest "$image" && { printf '%s\n' "$image"; return 0; }; read_digest "$DEPLOYED_DIGEST_FILE" || { printf '%s rollback reference is unavailable\n' "$component" >&2; return 1; }; }
unit_rollback_image() {
  local container="$1" api_digest="$2" image resolved
  image="$(configured_image "$container" 2>/dev/null)" || { printf 'rollback image is unavailable: %s is absent\n' "$container" >&2; return 1; }
  is_digest "$image" && { printf '%s\n' "$image"; return 0; }
  resolved="$(docker inspect --format '{{index .RepoDigests 0}}' "$container" 2>/dev/null || true)"
  is_digest "$resolved" && { printf '%s\n' "$resolved"; return 0; }
  is_digest "$api_digest" || { printf 'rollback image is unavailable for %s\n' "$container" >&2; return 1; }
  printf 'container %s has no immutable image; bootstrapping from API digest\n' "$container" >&2
  printf '%s\n' "$api_digest"
}

mcp_unit_is_skipped() { [[ ",$MCP_UNITS_SKIP," == *",$1,"* ]]; }
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
reload_haproxy() { if container_exists "$HAPROXY_CONTAINER"; then docker kill -s HUP "$HAPROXY_CONTAINER" >/dev/null; else docker run -d --name "$HAPROXY_CONTAINER" --restart unless-stopped --network host -v "${HAPROXY_CONFIG}:/usr/local/etc/haproxy/haproxy.cfg:ro" "$HAPROXY_IMAGE" -W -db -f /usr/local/etc/haproxy/haproxy.cfg >/dev/null; fi; curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8000/healthz >/dev/null && curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8765/health >/dev/null; }
restore_haproxy_config() { [[ -f "$HAPROXY_CONFIG_PREVIOUS" ]] || return 0; cat "$HAPROXY_CONFIG_PREVIOUS" >"$HAPROXY_CONFIG"; reload_haproxy || true; }

# The foreground inspect is a deterministic scheduling record; the detached
# child removes only the ID captured before a later replacement can occur.
schedule_drain() { local name="$1" seconds="$2" id pid_file; id="$(docker inspect --format '{{.Id}}' "$name" 2>/dev/null)" || return 0; printf 'scheduled drain: %s\n' "$name" >&2; pid_file="${RUN_DIRECTORY}/${name}-drain.pid"; nohup bash -c 'sleep "$1"; [[ "$(docker inspect --format "{{.Id}}" "$2" 2>/dev/null || true)" == "$3" ]] && docker rm -f "$2" >/dev/null 2>&1 || true' _ "$seconds" "$name" "$id" >"${RUN_DIRECTORY}/${name}-drain.log" 2>&1 & printf '%s\n' "$!" >"$pid_file"; }

deploy_api() {
  local image="$1" old new mcp old_legacy=""
  old="$(read_color api "$API_ACTIVE_COLOR_FILE" 2>/dev/null || true)"; mcp="$(read_color mcp "$MCP_ACTIVE_COLOR_FILE" 2>/dev/null || printf blue)"; [[ -n "$old" ]] && new="$(other_color "$old")" || new=blue
  docker rm -f "at-api-${new}" >/dev/null 2>&1 || true; run_api "$new" "$image" >/dev/null || return 1; wait_health "$(api_port "$new")" || { docker rm -f "at-api-${new}" >/dev/null 2>&1 || true; return 1; }
  if [[ -z "$old" ]]; then
    old_legacy="$(configured_image at-api 2>/dev/null || true)"; docker rm -f at-api >/dev/null 2>&1 || true
    if ! render_haproxy "$new" "$mcp" || ! reload_haproxy; then restore_haproxy_config; [[ -n "$old_legacy" ]] && docker run -d --name at-api --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" "$old_legacy" >/dev/null || true; return 1; fi
  elif ! render_haproxy "$new" "$mcp" || ! reload_haproxy; then restore_haproxy_config; return 1; fi
  write_color "$new" "$API_ACTIVE_COLOR_FILE" || { restore_haproxy_config; return 1; }; API_DRAIN_PENDING_COLOR="$old"
}
rollback_api() { local old="$1" mcp="$2"; [[ -n "$old" ]] || return 0; container_exists "at-api-${old}" || { printf 'API rollback color is unavailable; retaining current routing\n' >&2; return 1; }; render_haproxy "$old" "$mcp" && reload_haproxy && write_color "$old" "$API_ACTIVE_COLOR_FILE"; }

deploy_worker() { local image="$1"; docker rm -f at-worker-new >/dev/null 2>&1 || true; run_worker at-worker-new "$image" >/dev/null; wait_worker at-worker-new || { docker rm -f at-worker-new >/dev/null 2>&1 || true; return 1; }; docker stop -t 60 at-worker >/dev/null 2>&1 || true; docker rm at-worker >/dev/null 2>&1 || true; docker rename at-worker-new at-worker; }
deploy_singletons() {
  local image="$1" api_digest="$2" scheduler upbit kis
  scheduler="$(unit_rollback_image at-scheduler "$api_digest")" || return 1; upbit="$(unit_rollback_image at-upbit-ws "$api_digest")" || return 1; kis="$(unit_rollback_image at-kis-ws "$api_digest")" || return 1
  docker rm -f at-scheduler >/dev/null 2>&1 || true
  if ! run_scheduler "$image" >/dev/null; then run_scheduler "$scheduler" >/dev/null || true; return 1; fi
  docker rm -f at-upbit-ws at-kis-ws >/dev/null 2>&1 || true
  if ! run_ws at-upbit-ws "$image" upbit >/dev/null || ! run_ws at-kis-ws "$image" kis >/dev/null || ! wait_ws at-upbit-ws || ! wait_ws at-kis-ws; then docker rm -f at-scheduler at-upbit-ws at-kis-ws >/dev/null 2>&1 || true; run_scheduler "$scheduler" >/dev/null || true; run_ws at-upbit-ws "$upbit" upbit >/dev/null || true; run_ws at-kis-ws "$kis" kis >/dev/null || true; return 1; fi
}

run_mcp() { local name="$1" port="$2" profile="$3" token_env="$4" color="$5" image="$6" token heartbeat; local -a policy_args=(); token="$(env_value "$token_env")" || return 78; heartbeat="/var/run/auto-trader/mcp-heartbeat/mcp-${color:-$name}.json"; [[ "$profile" == tradingcodex_execution ]] && policy_args=(-e ORDER_APPROVAL_HASH_MODE=required -e TOSS_APPROVAL_HASH_MODE=required); docker run -d --name "at-mcp-${name}" --restart unless-stopped --network host "${ENV_FILE_ARGS[@]}" -v "${MCP_HEARTBEAT_DIRECTORY}:/var/run/auto-trader/mcp-heartbeat" "${policy_args[@]}" -e "MCP_AUTH_TOKEN=${token}" -e "MCP_PROFILE=${profile}" -e MCP_HOST=127.0.0.1 -e "MCP_PORT=${port}" -e MCP_TYPE=streamable-http -e MCP_PATH=/mcp -e MCP_USER_ID=1 -e "AUTO_TRADER_COLOR=${color:-$name}" -e "MCP_HEARTBEAT_PATH=${heartbeat}" "$image" python -m app.mcp_server.main; }
wait_mcp() { local port="$1" attempt status; for ((attempt=1; attempt<=MCP_HEALTH_ATTEMPTS; attempt++)); do status="$(curl --silent --show-error --max-time 3 --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${port}/health")" && [[ "$status" == 200 ]] && return 0; sleep "$MCP_HEALTH_SLEEP_SECONDS"; done; return 1; }
capture_mcp_state() { local api_digest="$1" i name; MCP_PREVIOUS_IMAGES=(); MCP_PREVIOUS_PRESENT=(); for i in "${!MCP_PREVIOUS_NAMES[@]}"; do name="${MCP_PREVIOUS_NAMES[$i]}"; if container_exists "at-mcp-${name}"; then MCP_PREVIOUS_IMAGES[$i]="$(unit_rollback_image "at-mcp-${name}" "$api_digest")" || return 1; MCP_PREVIOUS_PRESENT[$i]=1; else MCP_PREVIOUS_IMAGES[$i]=""; MCP_PREVIOUS_PRESENT[$i]=0; fi; done; }
rollback_mcp() { local i name image; for i in "${!MCP_NAMES[@]}"; do name="${MCP_NAMES[$i]}"; mcp_unit_is_skipped "$name" && continue; [[ "${MCP_PREVIOUS_PRESENT[$((i + 2))]:-0}" == 1 ]] || continue; image="${MCP_PREVIOUS_IMAGES[$((i + 2))]:-}"; is_digest "$image" || { printf 'MCP rollback image is unavailable for %s\n' "$name" >&2; return 1; }; docker rm -f "at-mcp-${name}" >/dev/null 2>&1 || true; run_mcp "$name" "${MCP_PORTS[$i]}" "${MCP_PROFILES[$i]}" "${MCP_TOKENS[$i]}" '' "$image" >/dev/null || return 1; done; }
deploy_mcp() {
  local image="$1" api_digest="$2" old new i
  mkdir -p "$MCP_HEARTBEAT_DIRECTORY"; chmod 1777 "$MCP_HEARTBEAT_DIRECTORY"; capture_mcp_state "$api_digest" || return 1
  old="$(read_color mcp "$MCP_ACTIVE_COLOR_FILE" 2>/dev/null || true)"; [[ -n "$old" ]] && new="$(other_color "$old")" || new=blue
  docker rm -f "at-mcp-${new}" >/dev/null 2>&1 || true; run_mcp "$new" "$(mcp_port "$new")" default MCP_AUTH_TOKEN "$new" "$image" >/dev/null || return 1; wait_mcp "$(mcp_port "$new")" || { docker rm -f "at-mcp-${new}" >/dev/null 2>&1 || true; return 1; }
  for i in "${!MCP_NAMES[@]}"; do mcp_unit_is_skipped "${MCP_NAMES[$i]}" && continue; docker rm -f "at-mcp-${MCP_NAMES[$i]}" >/dev/null 2>&1 || true; run_mcp "${MCP_NAMES[$i]}" "${MCP_PORTS[$i]}" "${MCP_PROFILES[$i]}" "${MCP_TOKENS[$i]}" '' "$image" >/dev/null || { rollback_mcp; return 1; }; wait_mcp "${MCP_PORTS[$i]}" || { rollback_mcp; return 1; }; done
  render_haproxy "$(read_color api "$API_ACTIVE_COLOR_FILE")" "$new" && reload_haproxy || { restore_haproxy_config; rollback_mcp; return 1; }; write_color "$new" "$MCP_ACTIVE_COLOR_FILE" || { restore_haproxy_config; rollback_mcp; return 1; }; MCP_DRAIN_PENDING_COLOR="$old"
}

write_digest() { local digest="$1" tmp old; is_digest "$digest" || return 1; mkdir -p "$RUN_DIRECTORY"; umask 077; tmp="$(mktemp "${RUN_DIRECTORY}/.deployed-digest.XXXXXX")"; printf '%s\n' "$digest" >"$tmp"; if old="$(read_digest "$DEPLOYED_DIGEST_FILE")"; then printf '%s\n' "$old" >"${DEPLOYED_DIGEST_PREVIOUS_FILE}"; fi; mv -f "$tmp" "$DEPLOYED_DIGEST_FILE"; }
finalize_drains() { [[ -z "$API_DRAIN_PENDING_COLOR" ]] || schedule_drain "at-api-${API_DRAIN_PENDING_COLOR}" "$API_DRAIN_SECONDS"; [[ -z "$MCP_DRAIN_PENDING_COLOR" ]] || schedule_drain "at-mcp-${MCP_DRAIN_PENDING_COLOR}" "$MCP_DRAIN_SECONDS"; }
rollback_after_mcp_failure() { local api_old="$1" mcp_old="$2"; rollback_api "$api_old" "$mcp_old" || true; printf 'MCP promotion failed; prior API routing was restored when available.\n' >&2; }
current_api_rollback_digest() { local color image; image="$(configured_image at-api 2>/dev/null || true)"; if [[ -z "$image" ]]; then color="$(read_color api "$API_ACTIVE_COLOR_FILE" 2>/dev/null || true)"; [[ -n "$color" ]] && image="$(configured_image "at-api-${color}" 2>/dev/null || true)"; fi; [[ -n "$image" ]] || { printf 'previous API container is required for rollback\n' >&2; return 1; }; rollback_reference "$image" API; }
promote_digest() {
  local digest="$1" api_rollback="$2" old_api_color="$3" old_mcp_color="$4"
  deploy_api "$digest" || return 1
  if ! deploy_worker "$digest" || ! deploy_singletons "$digest" "$api_rollback"; then rollback_api "$old_api_color" "$old_mcp_color" || true; return 1; fi
  if ! deploy_mcp "$digest" "$api_rollback"; then rollback_after_mcp_failure "$old_api_color" "$old_mcp_color"; return 1; fi
  write_digest "$digest" || { rollback_after_mcp_failure "$old_api_color" "$old_mcp_color"; return 1; }
  finalize_drains; printf 'deployment completed: %s\n' "$digest"
}
prepare() { require_command docker; require_command curl; require_command awk; require_file "$RUNTIME_ENV_FILE"; require_file "$SECRETS_ENV_FILE"; validate_mcp_tokens; }
main() { local api_rollback old_api_color old_mcp_color digest; prepare || exit $?; api_rollback="$(current_api_rollback_digest)" || exit 78; old_api_color="$(read_color api "$API_ACTIVE_COLOR_FILE" 2>/dev/null || true)"; old_mcp_color="$(read_color mcp "$MCP_ACTIVE_COLOR_FILE" 2>/dev/null || printf blue)"; docker pull "$IMAGE"; digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE")"; is_digest "$digest" || { printf 'could not resolve repo digest\n' >&2; exit 1; }; promote_digest "$digest" "$api_rollback" "$old_api_color" "$old_mcp_color"; }
manual_rollback() { local previous api_rollback old_api_color old_mcp_color; prepare || return $?; previous="$(read_digest "$DEPLOYED_DIGEST_PREVIOUS_FILE")" || { printf 'manual rollback digest is unavailable\n' >&2; return 1; }; api_rollback="$(current_api_rollback_digest)" || return 78; old_api_color="$(read_color api "$API_ACTIVE_COLOR_FILE" 2>/dev/null || true)"; old_mcp_color="$(read_color mcp "$MCP_ACTIVE_COLOR_FILE" 2>/dev/null || printf blue)"; docker pull "$previous"; promote_digest "$previous" "$api_rollback" "$old_api_color" "$old_mcp_color"; }

if [[ "$DEPLOY_MODE" == rollback ]]; then manual_rollback
else main
fi
