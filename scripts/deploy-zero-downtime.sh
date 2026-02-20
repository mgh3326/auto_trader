#!/bin/bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV_FILE="${ROOT_DIR}/.env.prod"
BASE_COMPOSE="${ROOT_DIR}/docker-compose.prod.yml"
ZERO_COMPOSE="${ROOT_DIR}/docker-compose.prod.zero.yml"
STATE_FILE="${ROOT_DIR}/tmp/deploy/zero-downtime-state.env"
UPSTREAM_DIR="${ROOT_DIR}/caddy/upstreams"

ACTIVE_API_URL="http://127.0.0.1:18080"
ACTIVE_MCP_URL="http://127.0.0.1:18065/mcp"

HEALTH_TIMEOUT=90
WORKER_TIMEOUT=120
SKIP_PULL=false
DRY_RUN=false
SKIP_WORKER_ROTATE=false
IMAGE_REF=""

RESOLVED_IMAGE_DIGEST=""
PREVIOUS_SLOT="blue"
TARGET_SLOT="green"
PREVIOUS_IMAGE_DIGEST=""
CURRENT_ACTIVE_IMAGE_DIGEST=""

CUTOVER_DONE=false
TARGET_WORKER_STARTED=false
OLD_WORKER_STOPPED=false
OLD_WORKER_RUNNING_BEFORE_DRAIN=false

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'


log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}


log_success() {
    echo -e "${GREEN}[OK]${NC} $*"
}


log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}


log_error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}


show_help() {
    cat <<EOF
Zero-downtime deployment for Raspberry Pi (blue-green slots)

Usage: $(basename "$0") [OPTIONS]

Options:
  --image-ref <ref>         Deployment image reference
                            (default: ghcr.io/\${GITHUB_REPOSITORY}:production)
  --skip-pull               Skip docker pull
  --dry-run                 Print commands only
  --skip-worker-rotate      Skip worker slot rotate (emergency use)
  --health-timeout <sec>    Health wait timeout in seconds (default: 90)
  --worker-timeout <sec>    Worker readiness timeout in seconds (default: 120)
  --help                    Show this help

Notes:
  - Uses docker-compose.prod.yml + docker-compose.prod.zero.yml
  - Cutover method: swap active Caddy include files -> caddy validate -> caddy reload
  - State file: tmp/deploy/zero-downtime-state.env
EOF
}


run_cmd() {
    if [ "$DRY_RUN" = true ]; then
        printf '[dry-run]'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}


compose_zero() {
    if [ "$DRY_RUN" = true ]; then
        printf '[dry-run] AUTO_TRADER_RUNTIME_IMAGE=%q docker compose -f %q -f %q' \
            "$RESOLVED_IMAGE_DIGEST" "$BASE_COMPOSE" "$ZERO_COMPOSE"
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi

    AUTO_TRADER_RUNTIME_IMAGE="$RESOLVED_IMAGE_DIGEST" \
        docker compose -f "$BASE_COMPOSE" -f "$ZERO_COMPOSE" "$@"
}


compose_base() {
    if [ "$DRY_RUN" = true ]; then
        printf '[dry-run] docker compose -f %q' "$BASE_COMPOSE"
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    docker compose -f "$BASE_COMPOSE" "$@"
}


require_file() {
    local path="$1"
    if [ ! -f "$path" ]; then
        log_error "Required file not found: $path"
        exit 1
    fi
}


require_command() {
    local name="$1"
    if ! command -v "$name" >/dev/null 2>&1; then
        log_error "Required command not found: $name"
        exit 1
    fi
}


read_env_value() {
    local key="$1"
    if [ ! -f "$ENV_FILE" ]; then
        return 0
    fi

    local line
    line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
    if [ -n "$line" ]; then
        echo "${line#*=}"
    fi
}


read_state_value() {
    local key="$1"
    if [ ! -f "$STATE_FILE" ]; then
        return 0
    fi

    local line
    line="$(grep -E "^${key}=" "$STATE_FILE" | tail -n 1 || true)"
    if [ -n "$line" ]; then
        echo "${line#*=}"
    fi
}


detect_active_slot() {
    local active_from_state
    active_from_state="$(read_state_value "ACTIVE_SLOT")"
    if [ "$active_from_state" = "blue" ] || [ "$active_from_state" = "green" ]; then
        echo "$active_from_state"
        return 0
    fi

    if [ -f "${UPSTREAM_DIR}/api_active.caddy" ]; then
        if grep -Eq 'api_green\.caddy|18001' "${UPSTREAM_DIR}/api_active.caddy"; then
            echo "green"
            return 0
        fi
        if grep -Eq 'api_blue\.caddy|18000' "${UPSTREAM_DIR}/api_active.caddy"; then
            echo "blue"
            return 0
        fi
    fi

    echo "blue"
}


other_slot() {
    local slot="$1"
    if [ "$slot" = "blue" ]; then
        echo "green"
    else
        echo "blue"
    fi
}


slot_api_port() {
    local slot="$1"
    if [ "$slot" = "blue" ]; then
        echo "18000"
    else
        echo "18001"
    fi
}


slot_mcp_port() {
    local slot="$1"
    if [ "$slot" = "blue" ]; then
        echo "18650"
    else
        echo "18651"
    fi
}


slot_worker_container() {
    local slot="$1"
    if [ "$slot" = "blue" ]; then
        echo "auto_trader_worker_blue_prod"
    else
        echo "auto_trader_worker_green_prod"
    fi
}


container_running() {
    local name="$1"
    if [ "$DRY_RUN" = true ]; then
        return 0
    fi

    local running
    running="$(docker inspect --format '{{.State.Running}}' "$name" 2>/dev/null || true)"
    [ "$running" = "true" ]
}


set_active_slot_files() {
    local slot="$1"
    local api_src="${UPSTREAM_DIR}/api_${slot}.caddy"
    local mcp_src="${UPSTREAM_DIR}/mcp_${slot}.caddy"
    local api_dst="${UPSTREAM_DIR}/api_active.caddy"
    local mcp_dst="${UPSTREAM_DIR}/mcp_active.caddy"

    require_file "$api_src"
    require_file "$mcp_src"

    run_cmd cp "$api_src" "${api_dst}.tmp"
    run_cmd mv "${api_dst}.tmp" "$api_dst"

    run_cmd cp "$mcp_src" "${mcp_dst}.tmp"
    run_cmd mv "${mcp_dst}.tmp" "$mcp_dst"
}


reload_caddy() {
    run_cmd docker exec caddy caddy validate --config /etc/caddy/Caddyfile
    run_cmd docker exec caddy caddy reload --config /etc/caddy/Caddyfile
}


wait_for_readyz() {
    local port="$1"
    local timeout="$2"

    if [ "$DRY_RUN" = true ]; then
        log_info "[dry-run] wait for http://127.0.0.1:${port}/readyz"
        return 0
    fi

    local started
    started="$(date +%s)"

    while true; do
        if curl -fsS "http://127.0.0.1:${port}/readyz" >/dev/null 2>&1; then
            return 0
        fi

        local now
        now="$(date +%s)"
        if (( now - started >= timeout )); then
            return 1
        fi
        sleep 2
    done
}


wait_for_mcp_http() {
    local port="$1"
    local timeout="$2"

    if [ "$DRY_RUN" = true ]; then
        log_info "[dry-run] wait for MCP http://127.0.0.1:${port}/mcp"
        return 0
    fi

    local started
    started="$(date +%s)"

    while true; do
        local code
        code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/mcp" || true)"
        if [[ "$code" =~ ^[0-9]{3}$ ]] && (( code < 500 )); then
            return 0
        fi

        local now
        now="$(date +%s)"
        if (( now - started >= timeout )); then
            return 1
        fi
        sleep 2
    done
}


wait_for_container_ready() {
    local container_name="$1"
    local timeout="$2"

    if [ "$DRY_RUN" = true ]; then
        log_info "[dry-run] wait for container readiness: ${container_name}"
        return 0
    fi

    local started
    started="$(date +%s)"

    while true; do
        if docker inspect "$container_name" >/dev/null 2>&1; then
            local running
            running="$(docker inspect --format '{{.State.Running}}' "$container_name" 2>/dev/null || true)"
            if [ "$running" = "true" ]; then
                local health_status
                health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_name" 2>/dev/null || true)"

                if [ "$health_status" = "healthy" ]; then
                    return 0
                fi

                if [ "$health_status" = "none" ] || [ -z "$health_status" ]; then
                    if docker exec "$container_name" pgrep -f "taskiq worker" >/dev/null 2>&1; then
                        return 0
                    fi
                fi
            fi
        fi

        local now
        now="$(date +%s)"
        if (( now - started >= timeout )); then
            log_error "Timed out waiting for worker readiness: ${container_name}"
            return 1
        fi
        sleep 2
    done
}


assert_post_cutover_health() {
    if [ "$DRY_RUN" = true ]; then
        log_info "[dry-run] post-cutover health checks"
        return 0
    fi

    curl -fsS "${ACTIVE_API_URL}/healthz" >/dev/null
    curl -fsS "${ACTIVE_API_URL}/readyz" >/dev/null

    local mcp_code
    mcp_code="$(curl -s -o /dev/null -w '%{http_code}' "${ACTIVE_MCP_URL}" || true)"
    if ! [[ "$mcp_code" =~ ^[0-9]{3}$ ]] || (( mcp_code >= 500 )); then
        log_error "Active MCP endpoint unhealthy: ${ACTIVE_MCP_URL} (http ${mcp_code})"
        return 1
    fi
}


write_state_file() {
    local deployed_at
    deployed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if [ "$DRY_RUN" = true ]; then
        log_info "[dry-run] update state file: $STATE_FILE"
        log_info "ACTIVE_SLOT=${TARGET_SLOT}"
        log_info "ACTIVE_IMAGE_DIGEST=${RESOLVED_IMAGE_DIGEST}"
        log_info "PREVIOUS_SLOT=${PREVIOUS_SLOT}"
        log_info "PREVIOUS_IMAGE_DIGEST=${PREVIOUS_IMAGE_DIGEST}"
        log_info "LAST_DEPLOYED_AT=${deployed_at}"
        return 0
    fi

    mkdir -p "$(dirname "$STATE_FILE")"

    cat >"${STATE_FILE}.tmp" <<EOF
ACTIVE_SLOT=${TARGET_SLOT}
ACTIVE_IMAGE_DIGEST=${RESOLVED_IMAGE_DIGEST}
PREVIOUS_SLOT=${PREVIOUS_SLOT}
PREVIOUS_IMAGE_DIGEST=${PREVIOUS_IMAGE_DIGEST}
LAST_DEPLOYED_AT=${deployed_at}
EOF

    mv "${STATE_FILE}.tmp" "$STATE_FILE"
}


resolve_image_digest() {
    local ref="$1"

    if [[ "$ref" == *@sha256:* ]]; then
        echo "$ref"
        return 0
    fi

    if [ "$DRY_RUN" = true ]; then
        echo "${ref}@sha256:dryrun"
        return 0
    fi

    local repo
    repo="${ref%%:*}"

    local digests
    digests="$(docker image inspect "$ref" --format '{{join .RepoDigests "\n"}}' 2>/dev/null || true)"
    if [ -z "$digests" ]; then
        log_error "Failed to inspect image digest for ${ref}"
        return 1
    fi

    local matched
    matched="$(printf '%s\n' "$digests" | grep -E "^${repo}@sha256:" | head -n 1 || true)"
    if [ -n "$matched" ]; then
        echo "$matched"
        return 0
    fi

    printf '%s\n' "$digests" | head -n 1
}


cleanup_inactive_slot_on_failure() {
    set +e
    log_warn "Cleanup: stopping slot ${TARGET_SLOT} services"
    compose_zero stop "api_${TARGET_SLOT}" "mcp_${TARGET_SLOT}"
    if [ "$TARGET_WORKER_STARTED" = true ]; then
        compose_zero stop -t 660 "worker_${TARGET_SLOT}"
    fi
    set -e
}


rollback_cutover_on_failure() {
    set +e
    log_warn "Rollback: restoring active slot to ${PREVIOUS_SLOT}"
    set_active_slot_files "$PREVIOUS_SLOT"
    reload_caddy
    compose_zero stop "api_${TARGET_SLOT}" "mcp_${TARGET_SLOT}"
    if [ "$TARGET_WORKER_STARTED" = true ]; then
        # If the previous worker has already been drained, keep target worker running
        # to avoid ending up with zero workers during rollback.
        if [ "$OLD_WORKER_STOPPED" = true ] || [ "$OLD_WORKER_RUNNING_BEFORE_DRAIN" = false ]; then
            log_warn "Keeping worker_${TARGET_SLOT} running during rollback to prevent zero-worker outage"
        else
            compose_zero stop -t 660 "worker_${TARGET_SLOT}"
        fi
    fi
    set -e
}


on_error() {
    local exit_code=$?
    local line_no="$1"

    log_error "Deployment failed at line ${line_no} (exit ${exit_code})"

    if [ "$CUTOVER_DONE" = true ]; then
        rollback_cutover_on_failure || true
    else
        cleanup_inactive_slot_on_failure || true
    fi

    exit "$exit_code"
}


trap 'on_error $LINENO' ERR


while [[ $# -gt 0 ]]; do
    case "$1" in
        --image-ref)
            if [[ $# -lt 2 ]]; then
                log_error "--image-ref requires a value"
                exit 1
            fi
            IMAGE_REF="$2"
            shift 2
            ;;
        --skip-pull)
            SKIP_PULL=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --skip-worker-rotate)
            SKIP_WORKER_ROTATE=true
            shift
            ;;
        --health-timeout)
            if [[ $# -lt 2 ]]; then
                log_error "--health-timeout requires a value"
                exit 1
            fi
            HEALTH_TIMEOUT="$2"
            shift 2
            ;;
        --worker-timeout)
            if [[ $# -lt 2 ]]; then
                log_error "--worker-timeout requires a value"
                exit 1
            fi
            WORKER_TIMEOUT="$2"
            shift 2
            ;;
        --help)
            show_help
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done


if ! [[ "$HEALTH_TIMEOUT" =~ ^[0-9]+$ ]] || [ "$HEALTH_TIMEOUT" -le 0 ]; then
    log_error "--health-timeout must be a positive integer"
    exit 1
fi

if ! [[ "$WORKER_TIMEOUT" =~ ^[0-9]+$ ]] || [ "$WORKER_TIMEOUT" -le 0 ]; then
    log_error "--worker-timeout must be a positive integer"
    exit 1
fi


require_command docker
require_command curl
require_command grep
require_command awk

require_file "$ENV_FILE"
require_file "$BASE_COMPOSE"
require_file "$ZERO_COMPOSE"
require_file "${UPSTREAM_DIR}/api_blue.caddy"
require_file "${UPSTREAM_DIR}/api_green.caddy"
require_file "${UPSTREAM_DIR}/mcp_blue.caddy"
require_file "${UPSTREAM_DIR}/mcp_green.caddy"

if [ "$DRY_RUN" = false ] && ! docker ps --format '{{.Names}}' | grep -qx caddy; then
    log_error "Caddy container 'caddy' is not running"
    exit 1
fi


if [ -z "$IMAGE_REF" ]; then
    env_repo="$(read_env_value "GITHUB_REPOSITORY")"
    if [ -z "$env_repo" ]; then
        log_error "GITHUB_REPOSITORY is not set in .env.prod. Use --image-ref explicitly."
        exit 1
    fi
    IMAGE_REF="ghcr.io/${env_repo}:production"
    export GITHUB_REPOSITORY="$env_repo"
else
    env_repo="$(read_env_value "GITHUB_REPOSITORY")"
    if [ -n "$env_repo" ]; then
        export GITHUB_REPOSITORY="$env_repo"
    fi
fi

log_info "Starting zero-downtime deployment"
log_info "Image ref: ${IMAGE_REF}"
log_info "Health timeout: ${HEALTH_TIMEOUT}s"
log_info "Worker timeout: ${WORKER_TIMEOUT}s"

if [ "$SKIP_PULL" = false ]; then
    log_info "Pulling image: ${IMAGE_REF}"
    run_cmd docker pull "$IMAGE_REF"
else
    log_info "Skipping docker pull"
fi

RESOLVED_IMAGE_DIGEST="$(resolve_image_digest "$IMAGE_REF")"
log_info "Resolved runtime image: ${RESOLVED_IMAGE_DIGEST}"

PREVIOUS_SLOT="$(detect_active_slot)"
TARGET_SLOT="$(other_slot "$PREVIOUS_SLOT")"
CURRENT_ACTIVE_IMAGE_DIGEST="$(read_state_value "ACTIVE_IMAGE_DIGEST")"
PREVIOUS_IMAGE_DIGEST="$CURRENT_ACTIVE_IMAGE_DIGEST"

log_info "Current active slot: ${PREVIOUS_SLOT}"
log_info "Target slot: ${TARGET_SLOT}"

local_api_port="$(slot_api_port "$TARGET_SLOT")"
local_mcp_port="$(slot_mcp_port "$TARGET_SLOT")"

log_info "Starting inactive slot api/mcp services"
compose_zero up -d "api_${TARGET_SLOT}" "mcp_${TARGET_SLOT}"

log_info "Waiting for api_${TARGET_SLOT} readiness on :${local_api_port}/readyz"
wait_for_readyz "$local_api_port" "$HEALTH_TIMEOUT"

log_info "Waiting for mcp_${TARGET_SLOT} on :${local_mcp_port}/mcp"
wait_for_mcp_http "$local_mcp_port" "$HEALTH_TIMEOUT"

log_info "Cutover: switch active upstream files to ${TARGET_SLOT}"
set_active_slot_files "$TARGET_SLOT"
reload_caddy
CUTOVER_DONE=true

log_info "Post-cutover health checks via active local endpoints"
assert_post_cutover_health

if [ "$SKIP_WORKER_ROTATE" = false ]; then
    log_info "Starting worker_${TARGET_SLOT}"
    compose_zero up -d "worker_${TARGET_SLOT}"
    TARGET_WORKER_STARTED=true

    target_worker_container="$(slot_worker_container "$TARGET_SLOT")"
    log_info "Waiting for ${target_worker_container} readiness"
    wait_for_container_ready "$target_worker_container" "$WORKER_TIMEOUT"

    old_worker_container="$(slot_worker_container "$PREVIOUS_SLOT")"
    if container_running "$old_worker_container"; then
        OLD_WORKER_RUNNING_BEFORE_DRAIN=true
        log_info "Draining ${old_worker_container} with docker stop -t 660"
        run_cmd docker stop -t 660 "$old_worker_container"
        OLD_WORKER_STOPPED=true
    else
        log_info "Old slot worker not running: ${old_worker_container}"
    fi
else
    log_warn "Skipping worker rotate (--skip-worker-rotate)"
fi

log_info "Restarting scheduler sequentially"
compose_base stop scheduler
compose_base up -d --no-deps --force-recreate scheduler

log_info "Restarting websocket monitors sequentially"
for service in upbit_websocket kis_websocket; do
    compose_base stop "$service"
    compose_base up -d --no-deps --force-recreate "$service"
done

log_info "Handling legacy single-slot containers if they exist"
for legacy in auto_trader_api_prod auto_trader_mcp_prod; do
    if container_running "$legacy"; then
        run_cmd docker stop -t 30 "$legacy"
    fi
done

if container_running auto_trader_worker_prod; then
    run_cmd docker stop -t 660 auto_trader_worker_prod
fi

write_state_file

log_info "Stopping previous slot api/mcp services"
set +e
compose_zero stop "api_${PREVIOUS_SLOT}" "mcp_${PREVIOUS_SLOT}"
set -e

log_success "Zero-downtime deployment completed"
log_success "Active API: ${ACTIVE_API_URL}"
log_success "Active MCP: ${ACTIVE_MCP_URL}"
