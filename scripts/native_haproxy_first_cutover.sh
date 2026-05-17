#!/usr/bin/env bash
# ROB-259: One-shot migration from direct-port deployment to HAProxy blue/green.
#
# Run this ONCE on the production Mac after the auto_trader release containing
# the ops/native/* assets is on disk under $AUTO_TRADER_BASE/current.
#
# Flow (review-driven, minimizes the legacy-api/mcp downtime window):
#   1) verify haproxy + release ops + capture legacy plist content
#   2) sync repo assets into $BASE (rsync --delete on plists)
#   3) write color state files + current-blue symlink
#   4) bootstrap api-blue + mcp-blue on :8001 / :8766
#      (different ports than legacy :8000 / :8765 — no conflict)
#   5) direct-probe blue at :8001 / :8766 — fail fast if blue is unhealthy
#   6) pre-render + validate HAProxy config (skip mode, no reload)
#   7) install ERR trap that re-bootstraps legacy api/mcp from the backup
#   8) drain legacy api / mcp (start of the brief cutover window)
#   9) start HAProxy on :8000 / :8765
#  10) probe the public-port path
#  11) clear ERR trap, remove stale plists, cleanup backup
#
# If anything between drain (step 8) and probe success (step 10) fails, the
# trap re-installs the legacy plists into ~/Library/LaunchAgents/ and bootstraps
# them so the service is restored to the pre-cutover state.

set -Eeuo pipefail

BASE="${AUTO_TRADER_BASE:-/Users/mgh3326/services/auto_trader}"
export AUTO_TRADER_BASE="$BASE"
RELEASE_OPS="$BASE/current/ops/native"
LEGACY_BACKUP=""

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

require_brew_haproxy() {
  if ! command -v haproxy >/dev/null 2>&1; then
    echo "haproxy not installed. Run: brew install haproxy" >&2
    exit 78
  fi
  log "haproxy: $(haproxy -v 2>&1 | head -1)"
}

require_release_ops() {
  if [[ ! -d "$RELEASE_OPS" ]]; then
    echo "release ops dir missing: $RELEASE_OPS" >&2
    echo "Ensure a release containing ops/native/ is deployed under $BASE/current" >&2
    exit 78
  fi
}

# Capture the existing legacy api/mcp plists BEFORE sync_repo_assets'
# rsync --delete wipes them from $BASE/plists/. Backup is restored by the
# ERR trap if the cutover window aborts mid-flight.
capture_legacy_plists() {
  LEGACY_BACKUP="$(mktemp -d -t auto-trader-legacy.XXXXXX)"
  local label
  for label in com.robinco.auto-trader.api com.robinco.auto-trader.mcp; do
    if [[ -f "$BASE/plists/$label.plist" ]]; then
      cp "$BASE/plists/$label.plist" "$LEGACY_BACKUP/$label.plist"
    fi
  done
  log "captured legacy plists in $LEGACY_BACKUP"
}

sync_repo_assets() {
  log "Syncing ops/native -> $BASE"
  rsync -a --delete "$RELEASE_OPS/plists/" "$BASE/plists/"
  rsync -a "$RELEASE_OPS/scripts/" "$BASE/scripts/"
  mkdir -p "$BASE/scripts/haproxy" "$BASE/shared/haproxy"
  rsync -a "$RELEASE_OPS/haproxy/" "$BASE/scripts/haproxy/"
  chmod +x "$BASE/scripts/"*.sh 2>/dev/null || true
}

init_state_and_symlinks() {
  # shellcheck source=/dev/null
  source "$BASE/scripts/native_bluegreen_lib.sh"
  set_active_color api blue
  set_active_color mcp blue
  local current_target
  current_target="$(readlink "$BASE/current")"
  if [[ -z "$current_target" ]]; then
    echo "$BASE/current is not a symlink; cannot derive blue release" >&2
    exit 78
  fi
  ln -sfn "$current_target" "$BASE/current-blue"
  # current-green starts unset; first blue/green deploy will create it.
  log "active colors set to blue; current-blue -> $(readlink "$BASE/current-blue")"
}

bootstrap_blue() {
  # shellcheck source=/dev/null
  source "$BASE/scripts/native_deploy_lib.sh"
  bootstrap_color api blue
  bootstrap_color mcp blue
}

probe_blue_direct() {
  AUTO_TRADER_HEALTHCHECK_ATTEMPTS=12 \
  AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS=2 \
  AUTO_TRADER_HEALTHCHECK_SKIP_WS=1 \
    "$BASE/scripts/healthcheck-native.sh" --direct blue
}

# Render the haproxy.cfg from state files and run `haproxy -c -f` validation
# WITHOUT starting/reloading the daemon. We want any cfg error to surface BEFORE
# we drain the legacy api/mcp.
prepare_haproxy_cfg() {
  log "Pre-rendering + validating HAProxy config"
  AUTO_TRADER_HAPROXY_RELOAD=skip bash "$BASE/scripts/haproxy_switch.sh"
}

drain_old_single_active_api_mcp() {
  local uid
  uid="$(id -u)"
  for label in com.robinco.auto-trader.api com.robinco.auto-trader.mcp; do
    log "Draining $label"
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  done
}

# ERR trap: if anything between drain and probe_public_stable fails, put the
# legacy api/mcp back on :8000/:8765 by re-installing the captured plists and
# re-bootstrapping the launchd jobs. Best-effort — if launchd refuses, the
# operator gets an explicit warning and must intervene.
restore_legacy_or_fail() {
  local exit_code=$?
  echo "" >&2
  echo "cutover step failed (exit $exit_code) — attempting to restore legacy api/mcp" >&2
  if [[ -z "$LEGACY_BACKUP" || ! -d "$LEGACY_BACKUP" ]]; then
    echo "FATAL: no legacy plist backup available; manual intervention required" >&2
    exit "$exit_code"
  fi
  local uid label backup target
  uid="$(id -u)"
  for label in com.robinco.auto-trader.api com.robinco.auto-trader.mcp; do
    backup="$LEGACY_BACKUP/$label.plist"
    target="$HOME/Library/LaunchAgents/$label.plist"
    if [[ -f "$backup" ]]; then
      install -m 0644 "$backup" "$target"
      launchctl bootout "gui/$uid/$label" 2>/dev/null || true
      if launchctl bootstrap "gui/$uid" "$target" 2>/dev/null; then
        launchctl enable "gui/$uid/$label" 2>/dev/null || true
        launchctl kickstart -k "gui/$uid/$label" 2>/dev/null || true
        echo "restored $label from legacy backup" >&2
      else
        echo "warning: could not re-bootstrap $label — manual intervention required" >&2
      fi
    else
      echo "warning: no backup for $label — manual restore required" >&2
    fi
  done
  exit "$exit_code"
}

start_haproxy() {
  local uid
  uid="$(id -u)"
  local label=com.robinco.auto-trader.haproxy
  local plist="$BASE/plists/$label.plist"
  local target="$HOME/Library/LaunchAgents/$label.plist"
  install -m 0644 "$plist" "$target"
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$uid" "$target"
  launchctl enable "gui/$uid/$label"
  launchctl kickstart -k "gui/$uid/$label"
}

probe_public_stable() {
  AUTO_TRADER_HEALTHCHECK_ATTEMPTS=12 \
  AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS=2 \
  AUTO_TRADER_HEALTHCHECK_SKIP_WS=1 \
    "$BASE/scripts/healthcheck-native.sh"
}

remove_stale_plists() {
  for stale in com.robinco.auto-trader.api com.robinco.auto-trader.mcp; do
    rm -f "$HOME/Library/LaunchAgents/$stale.plist"
    rm -f "$BASE/plists/$stale.plist"
  done
}

cleanup_legacy_backup() {
  if [[ -n "$LEGACY_BACKUP" && -d "$LEGACY_BACKUP" ]]; then
    rm -rf "$LEGACY_BACKUP"
  fi
}

main() {
  require_brew_haproxy
  require_release_ops
  capture_legacy_plists
  sync_repo_assets
  init_state_and_symlinks
  # Steps 4-6: bring blue up + validate cfg WITHOUT touching legacy yet.
  bootstrap_blue
  probe_blue_direct
  prepare_haproxy_cfg
  # Steps 7-10: short cutover window with restore-on-failure trap.
  trap restore_legacy_or_fail ERR
  drain_old_single_active_api_mcp
  start_haproxy
  probe_public_stable
  trap - ERR
  # Step 11: housekeeping.
  remove_stale_plists
  cleanup_legacy_backup
  log "first cutover complete; subsequent deploys use blue/green"
}

# Only auto-run main when executed directly. Sourcing the file (for tests)
# leaves the functions defined without running anything.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
