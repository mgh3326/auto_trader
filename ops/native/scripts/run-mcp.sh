#!/usr/bin/env bash
# ROB-259: color-aware FastMCP launcher.
#
# The MCP server reads `MCP_PORT` (see app/mcp_server/main.py: _env_int("MCP_PORT", 8765)).
# We translate the per-color AUTO_TRADER_MCP_PORT into MCP_PORT.
set -euo pipefail

COLOR="${AUTO_TRADER_COLOR:-blue}"
case "$COLOR" in
  blue)  DEFAULT_PORT=8766 ;;
  green) DEFAULT_PORT=8767 ;;
  *)
    echo "run-mcp.sh: invalid AUTO_TRADER_COLOR=$COLOR (expected blue|green)" >&2
    exit 64
    ;;
esac

PORT="${AUTO_TRADER_MCP_PORT:-$DEFAULT_PORT}"

# Override AUTO_TRADER_CURRENT so common.sh cd's into the per-color symlink.
export AUTO_TRADER_CURRENT="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/current-$COLOR"

source "${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/scripts/common.sh"

# _export_selected_env_prefixes may pull MCP_PORT from the env file; export after
# the prefix call so the per-color value wins.
_export_selected_env_prefixes MCP_
export MCP_PORT="$PORT"
# ROB-469 PR3: per-color liveness heartbeat the watchdog polls.
export MCP_HEARTBEAT_PATH="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/state/heartbeat/mcp-${COLOR}.json"

exec uv run python -m app.mcp_server.main
