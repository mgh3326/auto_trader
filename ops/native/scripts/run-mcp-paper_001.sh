#!/usr/bin/env bash
# Resident hermes-paper-kis MCP launcher (ROB-1258).
set -euo pipefail

# Defaults preserve compatibility with the pre-ROB-1258 installed plist during
# the narrow window between syncing this wrapper and installing the new plist.
PROFILE="${AUTO_TRADER_MCP_PROFILE:-hermes-paper-kis}"
PORT="${AUTO_TRADER_MCP_PORT:-8771}"
if [[ "$PROFILE" != "hermes-paper-kis" || "$PORT" != "8771" ]]; then
  echo "run-mcp-paper_001.sh: profile/port must remain hermes-paper-kis:8771" >&2
  exit 78
fi

# shellcheck source=/dev/null
source "${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/scripts/common.sh"
_export_selected_env_prefixes MCP_ KIS_MOCK

export MCP_PROFILE="$PROFILE"
# Keep this literal: an installed pre-ROB-1258 plist has no port metadata, so
# deploy preflight reads the resident wrapper during an interrupted upgrade.
export MCP_PORT="8771"
export MCP_HOST="127.0.0.1"

exec uv run python -m app.mcp_server.main
