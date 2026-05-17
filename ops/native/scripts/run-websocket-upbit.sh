#!/usr/bin/env bash
set -euo pipefail
source "${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/scripts/common.sh"
export WS_MONITOR_HEARTBEAT_PATH="$AUTO_TRADER_BASE/state/heartbeat/upbit.json"
export WS_MONITOR_HEARTBEAT_INTERVAL_SECONDS="${WS_MONITOR_HEARTBEAT_INTERVAL_SECONDS:-5}"
export WS_MONITOR_RECONNECT_DELAY_SECONDS="${WS_MONITOR_RECONNECT_DELAY_SECONDS:-5}"
export WS_MONITOR_EXPECT_MODE="upbit"
exec uv run python websocket_monitor.py --mode upbit
