#!/usr/bin/env bash
# ROB-259: shared helpers for blue/green color detection + state file writes.
# Source-only; do not execute directly.
#
# Required env:
#   AUTO_TRADER_BASE  - production base dir (e.g. /Users/mgh3326/services/auto_trader)

if [[ -z "${AUTO_TRADER_BASE:-}" ]]; then
  echo "native_bluegreen_lib: AUTO_TRADER_BASE not set" >&2
  return 1 2>/dev/null || exit 1
fi

_bg_shared_dir() {
  echo "$AUTO_TRADER_BASE/shared"
}

_bg_color_file() {
  local service="$1"
  echo "$(_bg_shared_dir)/${service}-active-color"
}

_bg_validate_color() {
  local color="$1"
  if [[ "$color" != "blue" && "$color" != "green" ]]; then
    echo "invalid color: $color (expected blue|green)" >&2
    return 64
  fi
}

_bg_validate_service() {
  local service="$1"
  if [[ "$service" != "api" && "$service" != "mcp" ]]; then
    echo "invalid service: $service (expected api|mcp)" >&2
    return 64
  fi
}

# detect_active_color <service>
# Prints "blue" or "green". Defaults to "blue" if the state file is missing.
detect_active_color() {
  local service="$1"
  _bg_validate_service "$service" || return $?
  local file
  file="$(_bg_color_file "$service")"
  if [[ -f "$file" ]]; then
    local raw
    raw="$(tr -d '[:space:]' <"$file")"
    _bg_validate_color "$raw" || return $?
    echo "$raw"
  else
    echo "blue"
  fi
}

# inactive_color <color>
# Echo the opposite color.
inactive_color() {
  local color="$1"
  _bg_validate_color "$color" || return $?
  if [[ "$color" == "blue" ]]; then
    echo "green"
  else
    echo "blue"
  fi
}

# set_active_color <service> <color>
# Atomically replace the state file.
set_active_color() {
  local service="$1" color="$2"
  _bg_validate_service "$service" || return $?
  _bg_validate_color "$color" || return $?
  local file tmp
  file="$(_bg_color_file "$service")"
  mkdir -p "$(dirname "$file")"
  tmp="$(mktemp "${file}.XXXXXX")"
  echo "$color" >"$tmp"
  mv "$tmp" "$file"
}

# color_port <service> <color>
color_port() {
  local service="$1" color="$2"
  _bg_validate_service "$service" || return $?
  _bg_validate_color "$color" || return $?
  case "${service}_${color}" in
    api_blue)   echo 8001 ;;
    api_green)  echo 8002 ;;
    mcp_blue)   echo 8766 ;;
    mcp_green)  echo 8767 ;;
  esac
}

# color_label <service> <color>
color_label() {
  local service="$1" color="$2"
  _bg_validate_service "$service" || return $?
  _bg_validate_color "$color" || return $?
  echo "com.robinco.auto-trader.${service}-${color}"
}

# color_current_symlink <color>
# Path to the per-color "current" symlink consumed by run-api.sh/run-mcp.sh.
color_current_symlink() {
  local color="$1"
  _bg_validate_color "$color" || return $?
  echo "$AUTO_TRADER_BASE/current-${color}"
}
