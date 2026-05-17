#!/usr/bin/env bash
# ROB-259: One-shot migration from direct-port deployment to HAProxy blue/green.
#
# Run this ONCE on the production Mac after the auto_trader release containing
# the ops/native/* assets is on disk under $AUTO_TRADER_BASE/current.
#
# What it does:
#   1) Verifies HAProxy is installed via Homebrew
#   2) Syncs new plists/scripts/haproxy template into $AUTO_TRADER_BASE
#   3) Sets api/mcp active color = blue and writes state files
#   4) Symlinks current-blue -> current
#   5) Drains existing single-port api/mcp plists
#   6) Bootstraps api-blue + mcp-blue on :8001 / :8766
#   7) Direct-probes blue at :8001 and :8766
#   8) Renders + starts HAProxy on :8000 / :8765
#   9) Public-port probes via :8000 / :8765
#  10) Removes the now-stale single-port plists from LaunchAgents/

set -Eeuo pipefail

BASE="${AUTO_TRADER_BASE:-/Users/mgh3326/services/auto_trader}"
export AUTO_TRADER_BASE="$BASE"
RELEASE_OPS="$BASE/current/ops/native"

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

drain_old_single_active_api_mcp() {
  local uid; uid="$(id -u)"
  for label in com.robinco.auto-trader.api com.robinco.auto-trader.mcp; do
    log "Draining $label"
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  done
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

start_haproxy() {
  local uid; uid="$(id -u)"
  local label=com.robinco.auto-trader.haproxy
  local plist="$BASE/plists/$label.plist"
  local target="$HOME/Library/LaunchAgents/$label.plist"
  install -m 0644 "$plist" "$target"
  # Render initial config from active-color state files (now both "blue")
  AUTO_TRADER_HAPROXY_RELOAD=skip bash "$BASE/scripts/haproxy_switch.sh"
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

main() {
  require_brew_haproxy
  require_release_ops
  sync_repo_assets
  init_state_and_symlinks
  drain_old_single_active_api_mcp
  bootstrap_blue
  probe_blue_direct
  start_haproxy
  probe_public_stable
  remove_stale_plists
  log "first cutover complete; subsequent deploys use blue/green"
}

main "$@"
