#!/bin/bash
# KR-B1 파일럿(2026-08-31): kiwoom 프로필 MCP — mock/CLAUDE.md §3 레인 계약의 실체화.
set -euo pipefail
PROFILE="${AUTO_TRADER_MCP_PROFILE:-kiwoom}"
PORT="${AUTO_TRADER_MCP_PORT:-8772}"
if [[ "$PROFILE" != "kiwoom" || "$PORT" != "8772" ]]; then
  echo "run-mcp-kiwoom.sh: profile/port must remain kiwoom:8772" >&2
  exit 78
fi
source "${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/scripts/common.sh"
_export_selected_env_prefixes MCP_ KIWOOM_MOCK
export MCP_PROFILE="$PROFILE"
export MCP_PORT="8772"
export MCP_HOST="127.0.0.1"
exec uv run python -m app.mcp_server.main
