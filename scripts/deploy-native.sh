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

# The blue/green helper libraries are sourced later and expect these values as
# environment variables rather than deploy-native.sh-local shell variables.
export AUTO_TRADER_BASE="$BASE"
export AUTO_TRADER_SOURCE_REPO="$SOURCE_REPO"
export AUTO_TRADER_ENV_FILE="$SHARED_ENV"
export AUTO_TRADER_PLIST_DIR="$PLIST_DIR"

# NCP owns the worker, scheduler, and WebSocket monitors.
# The Mac native deploy has no single-active launchd jobs to restart.
SINGLE_ACTIVE_LABELS=()

NEW_RELEASE="$RELEASES/$SHA"
PREVIOUS_RELEASE="$(readlink "$CURRENT" 2>/dev/null || true)"
SWITCHED=0
# Snapshot of the API blue/green state captured BEFORE deploy_bluegreen_flow.
# Set to 1 once deploy_bluegreen_flow has committed (state files + HAProxy live
# cfg now point at the new color). The rollback handler uses this to decide
# whether it also needs to roll back the API half (color state, color
# symlinks, color launchd jobs, HAProxy cfg).
BLUEGREEN_COMMITTED=0
RETROSPECTIVE_ACTION_CUTOVER_ATTEMPTED=0
RETROSPECTIVE_ACTION_SAFE_PRECOMMIT_EXIT=10
API_PRE_COLOR=""
BLUE_PRE_TARGET=""
GREEN_PRE_TARGET=""

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

sync_release_ops_to_base() {
  log "Syncing ops/native plists+scripts+haproxy from release"
  rsync -a --delete "$NEW_RELEASE/ops/native/plists/" "$PLIST_DIR/"
  rsync -a "$NEW_RELEASE/ops/native/scripts/" "$BASE/scripts/"
  mkdir -p "$BASE/scripts/haproxy"
  rsync -a "$NEW_RELEASE/ops/native/haproxy/" "$BASE/scripts/haproxy/"
  chmod +x "$BASE/scripts/"*.sh 2>/dev/null || true
}

build_frontend_workspace() {
  local name="$1"
  local relative_path="$2"
  local workspace="$NEW_RELEASE/$relative_path"
  local index="$workspace/dist/index.html"

  if [[ ! -d "$workspace" ]]; then
    log "Frontend workspace $name not present at $workspace; skipping SPA build"
    return 0
  fi

  if ! command -v npm >/dev/null 2>&1; then
    echo "npm not found on PATH for native deploy; cannot build $name SPA" >&2
    echo "PATH=$PATH" >&2
    return 78
  fi

  log "Building $name SPA in $workspace"
  log "node $(node --version 2>/dev/null || echo 'unknown')"
  log "npm  $(npm --version 2>/dev/null || echo 'unknown')"

  (
    cd "$workspace"
    npm ci
    npm run build
  )

  if [[ ! -f "$index" ]]; then
    echo "$name frontend build did not produce $index" >&2
    return 1
  fi

  log "$name SPA build present: $index"
}

build_frontend() {
  build_frontend_workspace "invest" "frontend/invest"
}

restart_single_active_services() {
  local uid_num label plist target attempt
  uid_num="$(id -u)"

  for label in "${SINGLE_ACTIVE_LABELS[@]}"; do
    plist="$PLIST_DIR/$label.plist"
    target="$HOME/Library/LaunchAgents/$label.plist"

    if [[ ! -f "$plist" ]]; then
      echo "Missing launchd plist: $plist" >&2
      return 78
    fi

    install -m 0644 "$plist" "$target"
    launchctl bootout "gui/$uid_num/$label" 2>/dev/null || true
    # macOS launchd can keep a failed/disabled plist-path registration around
    # after label bootout, causing bootstrap to return EIO even though the label
    # is no longer visible. Also boot out the installed plist path before retrying.
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
  local rc=0

  curl -fsS http://127.0.0.1:8000/healthz >/dev/null || {
    echo "API healthz failed" >&2
    rc=1
  }

  return $rc
}

run_healthcheck() {
  local attempts="${AUTO_TRADER_HEALTHCHECK_ATTEMPTS:-6}"
  local interval="${AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS:-5}"
  local attempt

  log "Running API healthcheck with retries"

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

run_retrospective_action_cutover() {
  local rc

  RETROSPECTIVE_ACTION_CUTOVER_ATTEMPTED=1
  set +e
  ENV_FILE="$SHARED_ENV" uv run python scripts/retrospective_action_cutover.py --if-shadow
  rc=$?
  set -e

  if (( rc == RETROSPECTIVE_ACTION_SAFE_PRECOMMIT_EXIT )); then
    # The CLI guarantees no canonical commit for this exit state, so the
    # normal deploy rollback path remains safe and needs no roll-forward warning.
    RETROSPECTIVE_ACTION_CUTOVER_ATTEMPTED=0
  fi
  return "$rc"
}

rollback() {
  local exit_code=$?
  echo "Deploy failed with exit code $exit_code" >&2

  if (( RETROSPECTIVE_ACTION_CUTOVER_ATTEMPTED == 1 )); then
    echo "WARNING: retrospective action cutover was attempted; the database may already be canonical." >&2
    echo "Disable retrospective action mutation, roll forward, and do not schema-downgrade." >&2
  fi

  # ROB-259 review: when deploy_bluegreen_flow committed but a later step
  # (restart_single_active_services, run_healthcheck) failed, the API
  # half is now on the new release while worker/scheduler/websocket are
  # still on PREVIOUS_RELEASE. Roll back API first so the whole system
  # ends up on the previous release together.
  if (( BLUEGREEN_COMMITTED == 1 )); then
    if declare -F rollback_bluegreen_post_deploy >/dev/null 2>&1; then
      echo "Rolling back API blue/green to api=$API_PRE_COLOR" >&2
      rollback_bluegreen_post_deploy \
        "$API_PRE_COLOR" \
        "${BLUE_PRE_TARGET:--}" "${GREEN_PRE_TARGET:--}" || \
        echo "warning: API blue/green rollback reported errors; verify manually" >&2
    else
      echo "warning: rollback_bluegreen_post_deploy not loaded; API not rolled back" >&2
    fi
  fi

  if [[ "$SWITCHED" == "1" && -n "${PREVIOUS_RELEASE:-}" && -d "$PREVIOUS_RELEASE" ]]; then
    echo "Rolling back current symlink to: $PREVIOUS_RELEASE" >&2
    ln -sfn "$PREVIOUS_RELEASE" "$CURRENT"
    restart_single_active_services || true
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

log "Building Trading Decision SPA"
build_frontend

log "Running Alembic migrations"
# Online deploy rollback only reverts code/services. Production migrations must be
# expansion-only/backwards-compatible with the previous release; do not merge
# destructive downgrades into this path without a separate data rollback runbook.
ENV_FILE="$SHARED_ENV" uv run alembic upgrade head

log "Preflight: verifying HAProxy baseline from previous cutover"
# Source the lib from the release dir so the preflight is checked BEFORE the
# rsync --delete in sync_release_ops_to_base. If this script fails preflight,
# legacy plists in $PLIST_DIR are untouched and the operator can recover by
# running scripts/native_haproxy_first_cutover.sh.
# shellcheck source=/dev/null
source "$NEW_RELEASE/ops/native/scripts/native_deploy_lib.sh"
require_haproxy_baseline

log "Capturing pre-deploy API blue/green state for rollback"
read -r API_PRE_COLOR BLUE_PRE_TARGET GREEN_PRE_TARGET <<<"$(capture_bluegreen_state)"
log "pre-deploy: api=$API_PRE_COLOR blue=$BLUE_PRE_TARGET green=$GREEN_PRE_TARGET"

log "Syncing release ops into base"
sync_release_ops_to_base

log "Running blue/green deploy for API"
deploy_bluegreen_flow "$NEW_RELEASE"
# deploy_bluegreen_flow only returns success after state files, HAProxy cfg,
# new-color bootstrap, and public smoke all succeeded. From here on, any
# failure must also roll back the API half via rollback_bluegreen_post_deploy.
BLUEGREEN_COMMITTED=1

log "Switching current symlink (worker/scheduler/websockets)"
ln -sfn "$NEW_RELEASE" "$CURRENT"
SWITCHED=1

log "Restarting single-active services"
restart_single_active_services

log "Running healthcheck"
run_healthcheck

# ROB-880: post-switch canonical cutover for retrospective actions.
# Runs only after blue/green is committed, traffic switched, services
# restarted, and healthcheck passed. --if-shadow makes it idempotent.
if (( BLUEGREEN_COMMITTED == 1 )); then
  log "Running retrospective action canonical cutover (--if-shadow)"
  run_retrospective_action_cutover
else
  log "Skipping retrospective action cutover (BLUEGREEN_COMMITTED != 1)"
fi

trap - ERR
log "Deploy complete: $SHA"
