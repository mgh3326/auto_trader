#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'USAGE' >&2
Usage: scripts/deploy-native.sh <commit-sha> [branch]

Deploys auto_trader to a macOS native launchd production layout:
  $AUTO_TRADER_BASE/releases/<sha>
  $AUTO_TRADER_BASE/current -> releases/<sha>

Expected server-side layout defaults:
  AUTO_TRADER_BASE=/Users/mgh3326/services/auto_trader
  AUTO_TRADER_SOURCE_REPO=/Users/mgh3326/work/auto_trader
  AUTO_TRADER_ENV_FILE=$AUTO_TRADER_BASE/shared/.env.prod.native
USAGE
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 64
fi

SHA="$1"
BRANCH="${2:-production}"

if ! printf '%s' "$SHA" | grep -Eq '^[0-9a-fA-F]{40}$'; then
  echo "Expected a full 40-character git commit SHA, got: $SHA" >&2
  exit 64
fi

export HOME="${HOME:-/Users/mgh3326}"
export PATH="$HOME/.local/bin:$HOME/.hermes/node/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

BASE="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}"
RELEASES="$BASE/releases"
CURRENT="$BASE/current"
SOURCE_REPO="${AUTO_TRADER_SOURCE_REPO:-$HOME/work/auto_trader}"
SHARED_ENV="${AUTO_TRADER_ENV_FILE:-$BASE/shared/.env.prod.native}"
LOG_DIR="$BASE/logs"
PLIST_DIR="${AUTO_TRADER_PLIST_DIR:-$BASE/plists}"
SERVER_HEALTHCHECK="$BASE/scripts/healthcheck-native.sh"

LABELS=(
  "com.robinco.auto-trader.api"
  "com.robinco.auto-trader.mcp"
  "com.robinco.auto-trader.worker"
  "com.robinco.auto-trader.scheduler"
  "com.robinco.auto-trader.kis-websocket"
  "com.robinco.auto-trader.upbit-websocket"
)

NEW_RELEASE="$RELEASES/$SHA"
PREVIOUS_RELEASE="$(readlink "$CURRENT" 2>/dev/null || true)"
SWITCHED=0

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "Missing required file: $path" >&2
    exit 78
  fi
}

restart_services() {
  local uid_num label plist target attempt
  uid_num="$(id -u)"

  for label in "${LABELS[@]}"; do
    plist="$PLIST_DIR/$label.plist"
    target="$HOME/Library/LaunchAgents/$label.plist"

    if [[ ! -f "$plist" ]]; then
      echo "Missing launchd plist: $plist" >&2
      return 78
    fi

    install -m 0644 "$plist" "$target"
    launchctl bootout "gui/$uid_num/$label" 2>/dev/null || true
    launchctl bootout "gui/$uid_num" "$target" 2>/dev/null || true

    for attempt in {1..5}; do
      if launchctl bootstrap "gui/$uid_num" "$target"; then
        break
      fi
      if (( attempt == 5 )); then
        echo "Failed to bootstrap $label after $attempt attempts" >&2
        return 5
      fi
      sleep 1
    done

    launchctl enable "gui/$uid_num/$label"
    launchctl kickstart -k "gui/$uid_num/$label"
  done
}

run_healthcheck_once() {
  if [[ -x "$SERVER_HEALTHCHECK" ]]; then
    "$SERVER_HEALTHCHECK"
    return $?
  fi

  local rc=0 code

  curl -fsS http://127.0.0.1:8000/healthz >/dev/null || {
    echo "API healthz failed" >&2
    rc=1
  }

  code="$(curl -sS -o /dev/null -w '%{http_code}' -H 'Accept: text/event-stream' http://127.0.0.1:8765/mcp || true)"
  if [[ "$code" != "401" && "$code" != "400" ]]; then
    echo "MCP unexpected status: $code" >&2
    rc=1
  fi

  if [[ -f "$CURRENT/scripts/websocket_healthcheck.py" ]]; then
    WS_MONITOR_HEARTBEAT_PATH="$BASE/state/heartbeat/kis.json" \
      WS_MONITOR_EXPECT_MODE=kis \
      uv run python scripts/websocket_healthcheck.py || rc=1
    WS_MONITOR_HEARTBEAT_PATH="$BASE/state/heartbeat/upbit.json" \
      WS_MONITOR_EXPECT_MODE=upbit \
      uv run python scripts/websocket_healthcheck.py || rc=1
  else
    echo "websocket_healthcheck.py not found; skipping websocket heartbeat checks" >&2
  fi

  return $rc
}

run_healthcheck() {
  local attempts="${AUTO_TRADER_HEALTHCHECK_ATTEMPTS:-6}"
  local interval="${AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS:-5}"
  local attempt

  if [[ -x "$SERVER_HEALTHCHECK" ]]; then
    log "Running server native healthcheck with retries: $SERVER_HEALTHCHECK"
  else
    log "Running built-in native healthcheck fallback with retries"
  fi

  for ((attempt = 1; attempt <= attempts; attempt++)); do
    log "Healthcheck attempt $attempt/$attempts"
    if run_healthcheck_once; then
      log "Healthcheck passed"
      return 0
    fi

    if (( attempt < attempts )); then
      sleep "$interval"
    fi
  done

  echo "Healthcheck failed after $attempts attempts" >&2
  return 1
}

rollback() {
  local exit_code=$?
  echo "Deploy failed with exit code $exit_code" >&2

  if [[ "$SWITCHED" == "1" && -n "${PREVIOUS_RELEASE:-}" && -d "$PREVIOUS_RELEASE" ]]; then
    echo "Rolling back current symlink to: $PREVIOUS_RELEASE" >&2
    ln -sfn "$PREVIOUS_RELEASE" "$CURRENT"
    restart_services || true
  else
    echo "No symlink switch happened, or previous release is unavailable; skipping rollback restart" >&2
  fi

  exit "$exit_code"
}

trap rollback ERR

require_file "$SHARED_ENV"
mkdir -p "$RELEASES" "$LOG_DIR" "$BASE/state/heartbeat"

if [[ ! -d "$SOURCE_REPO/.git" ]]; then
  echo "Missing source git repository: $SOURCE_REPO" >&2
  exit 78
fi

log "Deploying auto_trader commit $SHA from $BRANCH"
log "Base: $BASE"
log "Source repo: $SOURCE_REPO"
log "New release: $NEW_RELEASE"

log "Fetching source repository"
git -C "$SOURCE_REPO" fetch origin "$BRANCH" --tags
if ! git -C "$SOURCE_REPO" cat-file -e "$SHA^{commit}" 2>/dev/null; then
  log "Commit not found after branch fetch; trying explicit SHA fetch"
  git -C "$SOURCE_REPO" fetch origin "$SHA" --tags || \
    git -C "$SOURCE_REPO" fetch origin '+refs/heads/*:refs/remotes/origin/*' --tags
fi
git -C "$SOURCE_REPO" cat-file -e "$SHA^{commit}"

if [[ ! -d "$NEW_RELEASE/.git" ]]; then
  log "Creating release checkout"
  git clone --local "$SOURCE_REPO" "$NEW_RELEASE"
fi

cd "$NEW_RELEASE"
log "Preparing release checkout"
if ! git cat-file -e "$SHA^{commit}" 2>/dev/null; then
  log "Release checkout missing commit; fetching refs from source repo"
  git fetch "$SOURCE_REPO" \
    '+refs/heads/*:refs/remotes/source/*' \
    '+refs/remotes/origin/*:refs/remotes/source-origin/*' \
    --tags
fi
git cat-file -e "$SHA^{commit}"
git checkout --detach "$SHA"
git clean -fdx -e .venv

log "Installing dependencies with uv"
uv sync --frozen

log "Running Alembic migrations"
# Online deploy rollback only reverts code/services. Production migrations must be
# expansion-only/backwards-compatible with the previous release; do not merge
# destructive downgrades into this path without a separate data rollback runbook.
ENV_FILE="$SHARED_ENV" uv run alembic upgrade head

log "Switching current symlink"
ln -sfn "$NEW_RELEASE" "$CURRENT"
SWITCHED=1

log "Restarting launchd services"
restart_services

log "Running healthcheck"
run_healthcheck

trap - ERR
log "Deploy complete: $SHA"
