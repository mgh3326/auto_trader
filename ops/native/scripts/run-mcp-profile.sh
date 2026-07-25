#!/usr/bin/env bash
# Fixed-profile FastMCP launcher for non-blue/green read-only MCP services.
set -euo pipefail

PROFILE="${AUTO_TRADER_MCP_PROFILE:?AUTO_TRADER_MCP_PROFILE is required}"
PORT="${AUTO_TRADER_MCP_PORT:?AUTO_TRADER_MCP_PORT is required}"
TOKEN_ENV="${AUTO_TRADER_MCP_AUTH_TOKEN_ENV:?AUTO_TRADER_MCP_AUTH_TOKEN_ENV is required}"

export AUTO_TRADER_CURRENT="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/current"

source "${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/scripts/common.sh"

_export_selected_env_prefixes MCP_

TOKEN="${!TOKEN_ENV:-}"
if [[ -z "${TOKEN//[[:space:]]/}" ]]; then
  echo "run-mcp-profile.sh: ${TOKEN_ENV} is required for MCP_PROFILE=${PROFILE}" >&2
  exit 78
fi

export MCP_PROFILE="$PROFILE"
export MCP_PORT="$PORT"
export MCP_AUTH_TOKEN="$TOKEN"
export MCP_HEARTBEAT_PATH="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/state/heartbeat/mcp-${PROFILE}.json"

exec uv run python -m app.mcp_server.main
