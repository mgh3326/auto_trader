#!/usr/bin/env bash
# ROB-259: blue/green deploy primitives. Source-only.
#
# Required env:
#   AUTO_TRADER_BASE
# Optional env:
#   AUTO_TRADER_HEALTHCHECK_ATTEMPTS         (default 6)
#   AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS (default 5)

SCRIPT_DIR_NDL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=native_bluegreen_lib.sh
source "$SCRIPT_DIR_NDL/native_bluegreen_lib.sh"

_ndl_uid() { id -u; }

# require_haproxy_baseline
# Fail-fast preflight before deploy-native.sh rsync's plists with --delete.
# Verifies that scripts/native_haproxy_first_cutover.sh has run and the HAProxy
# blue/green baseline is in place. Without this guard, a first deploy that
# skips cutover would `rsync --delete` the legacy api/mcp plists, SIGUSR2 a
# nonexistent HAProxy launchd job, and leave the system half-installed.
#
# Required:
#   AUTO_TRADER_BASE
# Optional override (mainly for tests):
#   AUTO_TRADER_HAPROXY_LABEL   (default com.robinco.auto-trader.haproxy)
#   LAUNCHCTL_BIN               (default launchctl)
require_haproxy_baseline() {
  local errors=0
  local label="${AUTO_TRADER_HAPROXY_LABEL:-com.robinco.auto-trader.haproxy}"
  local launchctl_bin="${LAUNCHCTL_BIN:-launchctl}"

  if ! "$launchctl_bin" list "$label" >/dev/null 2>&1; then
    echo "preflight: launchd job '$label' is not loaded" >&2
    errors=1
  fi

  for f in \
    "$AUTO_TRADER_BASE/shared/api-active-color" \
    "$AUTO_TRADER_BASE/shared/mcp-active-color" \
    "$AUTO_TRADER_BASE/shared/haproxy/haproxy.cfg"
  do
    if [[ ! -f "$f" ]]; then
      echo "preflight: missing required file $f" >&2
      errors=1
    fi
  done

  if [[ ! -e "$AUTO_TRADER_BASE/current-blue" && ! -e "$AUTO_TRADER_BASE/current-green" ]]; then
    echo "preflight: neither current-blue nor current-green symlink exists" >&2
    errors=1
  fi

  if (( errors > 0 )); then
    echo "" >&2
    echo "preflight: HAProxy blue/green baseline is not set up." >&2
    echo "Run scripts/native_haproxy_first_cutover.sh first (see docs/runbooks/native-haproxy-blue-green.md)." >&2
    return 78
  fi
}

_ndl_plist_path() {
  local service="$1" color="$2"
  echo "$AUTO_TRADER_BASE/plists/com.robinco.auto-trader.${service}-${color}.plist"
}

# sync_release_to_color_symlink <color> <release_path>
sync_release_to_color_symlink() {
  local color="$1" release="$2"
  _bg_validate_color "$color" || return $?
  [[ -d "$release" ]] || { echo "release dir missing: $release" >&2; return 78; }
  local symlink
  symlink="$(color_current_symlink "$color")"
  ln -sfn "$release" "$symlink"
}

# bootstrap_color <service> <color>
#
# Retry/path-bootout semantics mirror restart_single_active_services() in
# scripts/deploy-native.sh: macOS launchd can keep a failed/disabled
# plist-PATH registration around after a label bootout (and tears jobs down
# asynchronously), so an immediate bootstrap can return EIO
# ("Bootstrap failed: 5: Input/output error") even though the label is no
# longer visible. Boot out the installed plist PATH too, then retry the
# bootstrap a few times to ride out the teardown before giving up. Without
# this a single transient EIO aborts the whole blue/green deploy (observed in
# GitHub Actions run 28408243128). The two implementations are kept separate
# on purpose: restart_single_active_services() must run even when the deploy
# rollback fires before native_deploy_lib.sh is sourced.
#
# Tunables (defaults match the single-active path: 5 attempts, 1s backoff):
#   AUTO_TRADER_BOOTSTRAP_ATTEMPTS        (default 5)
#   AUTO_TRADER_BOOTSTRAP_RETRY_SECONDS   (default 1)
bootstrap_color() {
  local service="$1" color="$2"
  _bg_validate_service "$service" || return $?
  _bg_validate_color "$color" || return $?
  local label plist target uid
  label="$(color_label "$service" "$color")"
  plist="$(_ndl_plist_path "$service" "$color")"
  target="$HOME/Library/LaunchAgents/$label.plist"
  uid="$(_ndl_uid)"
  [[ -f "$plist" ]] || { echo "missing plist: $plist" >&2; return 78; }
  mkdir -p "$(dirname "$target")"
  install -m 0644 "$plist" "$target"
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  launchctl bootout "gui/$uid" "$target" 2>/dev/null || true

  local attempts="${AUTO_TRADER_BOOTSTRAP_ATTEMPTS:-5}"
  local interval="${AUTO_TRADER_BOOTSTRAP_RETRY_SECONDS:-1}"
  # Clamp a misconfigured (non-numeric or <1) attempt count back to the default
  # so the loop always runs at least once; otherwise execution would fall
  # through to enable/kickstart against a label that was never bootstrapped.
  [[ "$attempts" =~ ^[0-9]+$ ]] && (( attempts >= 1 )) || attempts=5
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if launchctl bootstrap "gui/$uid" "$target"; then
      break
    fi
    if (( attempt == attempts )); then
      echo "bootstrap_color: $label failed to bootstrap after $attempt attempts" >&2
      return 5
    fi
    sleep "$interval"
  done

  launchctl enable "gui/$uid/$label"
  launchctl kickstart -k "gui/$uid/$label"
}

# drain_color <service> <color>
drain_color() {
  local service="$1" color="$2"
  _bg_validate_service "$service" || return $?
  _bg_validate_color "$color" || return $?
  local label uid target
  label="$(color_label "$service" "$color")"
  uid="$(_ndl_uid)"
  target="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  # Also boot out by plist PATH. macOS launchd can keep a stale plist-path
  # registration after a label-only bootout; left to accumulate these surface
  # later as a bootstrap EIO ("5: Input/output error"). Path-bootout keeps the
  # domain clean so the next bootstrap_color starts from a clean slate. Best
  # effort, like the label bootout. Plist stays on disk for re-bootstrap.
  launchctl bootout "gui/$uid" "$target" 2>/dev/null || true
}

# probe_color_direct <color>
probe_color_direct() {
  local color="$1"
  _bg_validate_color "$color" || return $?
  local attempts="${AUTO_TRADER_HEALTHCHECK_ATTEMPTS:-24}"
  local interval="${AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS:-5}"
  local hc="$AUTO_TRADER_BASE/scripts/healthcheck-native.sh"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if AUTO_TRADER_HEALTHCHECK_SKIP_WS=1 "$hc" --direct "$color"; then
      return 0
    fi
    if (( attempt < attempts )); then
      sleep "$interval"
    fi
  done
  echo "probe_color_direct: $color failed after $attempts attempts" >&2
  return 1
}

# haproxy_swap_to_color <service> <new_color>
haproxy_swap_to_color() {
  local service="$1" new_color="$2"
  _bg_validate_service "$service" || return $?
  _bg_validate_color "$new_color" || return $?
  set_active_color "$service" "$new_color"
  AUTO_TRADER_HAPROXY_TEMPLATE="$AUTO_TRADER_BASE/scripts/haproxy/haproxy.cfg.tmpl" \
    bash "$AUTO_TRADER_BASE/scripts/haproxy_switch.sh"
}

# probe_public_stable
probe_public_stable() {
  local attempts="${AUTO_TRADER_HEALTHCHECK_ATTEMPTS:-24}"
  local interval="${AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS:-5}"
  local hc="$AUTO_TRADER_BASE/scripts/healthcheck-native.sh"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    # Deploy-time stable probing should validate the newly switched API/MCP
    # routing only. KIS/Upbit websocket monitors are singleton background
    # services with independent external session state; a transient KIS appkey
    # lock must not roll back an otherwise healthy API/MCP deployment.
    if AUTO_TRADER_HEALTHCHECK_SKIP_WS=1 "$hc"; then
      return 0
    fi
    if (( attempt < attempts )); then
      sleep "$interval"
    fi
  done
  return 1
}

# capture_bluegreen_state
# Print the pre-deploy api/mcp colors and color-symlink targets as four
# space-separated fields on a single line:
#   <api_color> <mcp_color> <blue_target_or_-> <green_target_or_->
# Missing color symlinks are reported as `-`.
capture_bluegreen_state() {
  local api mcp blue green
  api="$(detect_active_color api)"
  mcp="$(detect_active_color mcp)"
  blue="$(readlink "$AUTO_TRADER_BASE/current-blue" 2>/dev/null || true)"
  green="$(readlink "$AUTO_TRADER_BASE/current-green" 2>/dev/null || true)"
  printf '%s %s %s %s\n' "$api" "$mcp" "${blue:--}" "${green:--}"
}

# rollback_bluegreen_post_deploy <api_pre> <mcp_pre> <blue_pre> <green_pre>
# Restore api/mcp state files, color symlinks, color launchd jobs, and HAProxy
# config to the snapshot captured before deploy_bluegreen_flow succeeded.
# Use `-` for blue_pre/green_pre to indicate "was not a symlink".
#
# Best-effort: each step continues on failure. The caller (deploy-native.sh
# rollback) logs warnings; manual intervention may still be needed if launchd
# refuses to bootstrap.
rollback_bluegreen_post_deploy() {
  local api_pre="$1" mcp_pre="$2" blue_pre="$3" green_pre="$4"
  _bg_validate_color "$api_pre" || return $?
  _bg_validate_color "$mcp_pre" || return $?

  local api_cur mcp_cur
  api_cur="$(detect_active_color api)"
  mcp_cur="$(detect_active_color mcp)"
  echo "rollback_bluegreen_post_deploy: restoring api $api_cur->$api_pre mcp $mcp_cur->$mcp_pre" >&2

  # 1. State files (atomic via mktemp+mv inside set_active_color)
  set_active_color api "$api_pre" || true
  set_active_color mcp "$mcp_pre" || true

  # 2. Color symlinks (only if we captured a real target)
  if [[ "$blue_pre" != "-" && -n "$blue_pre" ]]; then
    ln -sfn "$blue_pre" "$AUTO_TRADER_BASE/current-blue"
  fi
  if [[ "$green_pre" != "-" && -n "$green_pre" ]]; then
    ln -sfn "$green_pre" "$AUTO_TRADER_BASE/current-green"
  fi

  # 3. Launchd jobs — re-bootstrap pre-active color, drain current-active.
  if [[ "$api_pre" != "$api_cur" ]]; then
    bootstrap_color api "$api_pre" || \
      echo "warning: failed to re-bootstrap api-$api_pre; manual launchctl bootstrap needed" >&2
    drain_color api "$api_cur" || true
  fi
  if [[ "$mcp_pre" != "$mcp_cur" ]]; then
    bootstrap_color mcp "$mcp_pre" || \
      echo "warning: failed to re-bootstrap mcp-$mcp_pre; manual launchctl bootstrap needed" >&2
    drain_color mcp "$mcp_cur" || true
  fi

  # 4. HAProxy live cfg + reload (uses restored state files)
  AUTO_TRADER_HAPROXY_TEMPLATE="$AUTO_TRADER_BASE/scripts/haproxy/haproxy.cfg.tmpl" \
    bash "$AUTO_TRADER_BASE/scripts/haproxy_switch.sh" || \
    echo "warning: post-rollback haproxy_switch failed; verify $AUTO_TRADER_BASE/shared/haproxy/haproxy.cfg manually" >&2
}

# deploy_bluegreen_flow <release_path>
deploy_bluegreen_flow() {
  local release="$1"
  [[ -d "$release" ]] || { echo "release dir missing: $release" >&2; return 78; }

  local api_active mcp_active api_new mcp_new
  api_active="$(detect_active_color api)"
  mcp_active="$(detect_active_color mcp)"
  api_new="$(inactive_color "$api_active")"
  mcp_new="$(inactive_color "$mcp_active")"
  echo "active api=$api_active mcp=$mcp_active; bootstrapping api=$api_new mcp=$mcp_new"

  sync_release_to_color_symlink "$api_new" "$release"
  if [[ "$api_new" != "$mcp_new" ]]; then
    sync_release_to_color_symlink "$mcp_new" "$release"
  fi

  if ! bootstrap_color api "$api_new"; then
    echo "bootstrap api-$api_new failed" >&2
    drain_color api "$api_new" || true
    return 1
  fi
  if ! bootstrap_color mcp "$mcp_new"; then
    echo "bootstrap mcp-$mcp_new failed" >&2
    drain_color mcp "$mcp_new" || true
    drain_color api "$api_new" || true
    return 1
  fi

  if ! probe_color_direct "$api_new"; then
    drain_color api "$api_new" || true
    drain_color mcp "$mcp_new" || true
    return 1
  fi
  if [[ "$api_new" != "$mcp_new" ]]; then
    if ! probe_color_direct "$mcp_new"; then
      drain_color api "$api_new" || true
      drain_color mcp "$mcp_new" || true
      return 1
    fi
  fi

  if ! haproxy_swap_to_color api "$api_new"; then
    # ROB-259 review: restore api state AND run compensating switch so the
    # live HAProxy cfg matches the restored state. Without the switch, a
    # partially-succeeded haproxy_swap_to_color could leave the live cfg
    # pointing at the new color while the state file says old.
    set_active_color api "$api_active"
    AUTO_TRADER_HAPROXY_TEMPLATE="$AUTO_TRADER_BASE/scripts/haproxy/haproxy.cfg.tmpl" \
      bash "$AUTO_TRADER_BASE/scripts/haproxy_switch.sh" || true
    drain_color api "$api_new" || true
    drain_color mcp "$mcp_new" || true
    return 1
  fi
  if ! haproxy_swap_to_color mcp "$mcp_new"; then
    set_active_color api "$api_active"
    set_active_color mcp "$mcp_active"
    AUTO_TRADER_HAPROXY_TEMPLATE="$AUTO_TRADER_BASE/scripts/haproxy/haproxy.cfg.tmpl" \
      bash "$AUTO_TRADER_BASE/scripts/haproxy_switch.sh" || true
    drain_color api "$api_new" || true
    drain_color mcp "$mcp_new" || true
    return 1
  fi

  if ! probe_public_stable; then
    set_active_color api "$api_active"
    set_active_color mcp "$mcp_active"
    AUTO_TRADER_HAPROXY_TEMPLATE="$AUTO_TRADER_BASE/scripts/haproxy/haproxy.cfg.tmpl" \
      bash "$AUTO_TRADER_BASE/scripts/haproxy_switch.sh" || true
    drain_color api "$api_new" || true
    drain_color mcp "$mcp_new" || true
    return 1
  fi

  drain_color api "$api_active"
  drain_color mcp "$mcp_active"

  echo "deploy_bluegreen_flow: success api=$api_new mcp=$mcp_new"
}
