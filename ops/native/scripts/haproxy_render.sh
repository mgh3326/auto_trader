#!/usr/bin/env bash
# ROB-259: render HAProxy config from template with active color env vars.
# Usage: haproxy_render.sh <template> <out>
# Env:
#   AUTO_TRADER_API_ACTIVE_COLOR = blue | green
#   AUTO_TRADER_MCP_ACTIVE_COLOR = blue | green
set -Eeuo pipefail

TEMPLATE="${1:?template path required}"
OUT="${2:?output path required}"
API_COLOR="${AUTO_TRADER_API_ACTIVE_COLOR:?AUTO_TRADER_API_ACTIVE_COLOR required}"
MCP_COLOR="${AUTO_TRADER_MCP_ACTIVE_COLOR:?AUTO_TRADER_MCP_ACTIVE_COLOR required}"

validate_color() {
  local kind="$1" color="$2"
  if [[ "$color" != "blue" && "$color" != "green" ]]; then
    echo "invalid color for $kind: $color (expected blue|green)" >&2
    exit 64
  fi
}

validate_color api "$API_COLOR"
validate_color mcp "$MCP_COLOR"

api_primary_line() {
  if [[ "$API_COLOR" == "blue" ]]; then
    echo "server api_blue 127.0.0.1:8001 check"
  else
    echo "server api_green 127.0.0.1:8002 check"
  fi
}

api_backup_line() {
  if [[ "$API_COLOR" == "blue" ]]; then
    echo "server api_green 127.0.0.1:8002 check backup"
  else
    echo "server api_blue 127.0.0.1:8001 check backup"
  fi
}

mcp_primary_line() {
  if [[ "$MCP_COLOR" == "blue" ]]; then
    echo "server mcp_blue 127.0.0.1:8766 check"
  else
    echo "server mcp_green 127.0.0.1:8767 check"
  fi
}

mcp_backup_line() {
  if [[ "$MCP_COLOR" == "blue" ]]; then
    echo "server mcp_green 127.0.0.1:8767 check backup"
  else
    echo "server mcp_blue 127.0.0.1:8766 check backup"
  fi
}

TMP="$(mktemp -t haproxy-render.XXXXXX)"
trap 'rm -f "$TMP"' EXIT

sed \
  -e "s|{{API_PRIMARY_LINE}}|$(api_primary_line)|g" \
  -e "s|{{API_BACKUP_LINE}}|$(api_backup_line)|g" \
  -e "s|{{MCP_PRIMARY_LINE}}|$(mcp_primary_line)|g" \
  -e "s|{{MCP_BACKUP_LINE}}|$(mcp_backup_line)|g" \
  "$TEMPLATE" >"$TMP"

mv "$TMP" "$OUT"
