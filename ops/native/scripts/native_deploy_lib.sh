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
  launchctl bootstrap "gui/$uid" "$target"
  launchctl enable "gui/$uid/$label"
  launchctl kickstart -k "gui/$uid/$label"
}

# drain_color <service> <color>
drain_color() {
  local service="$1" color="$2"
  _bg_validate_service "$service" || return $?
  _bg_validate_color "$color" || return $?
  local label uid
  label="$(color_label "$service" "$color")"
  uid="$(_ndl_uid)"
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  # Leave plist on disk so re-bootstrap works on next deploy.
}

# probe_color_direct <color>
probe_color_direct() {
  local color="$1"
  _bg_validate_color "$color" || return $?
  local attempts="${AUTO_TRADER_HEALTHCHECK_ATTEMPTS:-6}"
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
  local attempts="${AUTO_TRADER_HEALTHCHECK_ATTEMPTS:-6}"
  local interval="${AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS:-5}"
  local hc="$AUTO_TRADER_BASE/scripts/healthcheck-native.sh"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if "$hc"; then
      return 0
    fi
    if (( attempt < attempts )); then
      sleep "$interval"
    fi
  done
  return 1
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
