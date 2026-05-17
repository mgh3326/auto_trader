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
    set_active_color api "$api_active"
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
