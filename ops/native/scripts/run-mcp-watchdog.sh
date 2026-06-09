#!/usr/bin/env bash
# ROB-469 PR3: launchd wrapper for the MCP self-heal watchdog.
set -euo pipefail

export AUTO_TRADER_CURRENT="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/current-blue"
source "${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/scripts/common.sh"

# Arm the kickstart only when the operator sets MCP_WATCHDOG_DRY_RUN=false in the env
# file; default stays dry-run (observe-only).
_export_selected_env_prefixes MCP_WATCHDOG_

exec uv run python -m scripts.mcp_watchdog
