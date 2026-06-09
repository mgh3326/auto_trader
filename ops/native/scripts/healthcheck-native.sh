#!/usr/bin/env bash
# ROB-259: healthcheck-native.sh with direct-color and stable modes.
#
# Usage:
#   healthcheck-native.sh                    # probe HAProxy stable ports (8000, 8765)
#   healthcheck-native.sh --direct blue      # probe :8001 + :8766 directly
#   healthcheck-native.sh --direct green     # probe :8002 + :8767 directly
#
# Env:
#   AUTO_TRADER_HEALTHCHECK_SKIP_WS=1 to skip the websocket heartbeat probes.
set -euo pipefail

source "${AUTO_TRADER_BASE:-${HOME:-/Users/mgh3326}/services/auto_trader}/scripts/common.sh"

MODE="stable"
COLOR=""

while (( $# > 0 )); do
  case "$1" in
    --direct)
      MODE="direct"
      shift
      COLOR="${1:-}"
      if [[ -z "$COLOR" ]]; then
        echo "--direct requires color (blue|green)" >&2
        exit 64
      fi
      shift
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 64
      ;;
  esac
done

if [[ "$MODE" == "direct" ]]; then
  case "$COLOR" in
    blue)  API_PORT=8001; MCP_PORT=8766 ;;
    green) API_PORT=8002; MCP_PORT=8767 ;;
    *) echo "invalid color: $COLOR (expected blue|green)" >&2; exit 64 ;;
  esac
else
  API_PORT=8000
  MCP_PORT=8765
fi

rc=0
if ! curl -fsS "http://127.0.0.1:${API_PORT}/healthz" >/dev/null; then
  echo "api healthz failed at :${API_PORT}" >&2
  rc=1
fi

# ROB-469: probe the unauthenticated, dependency-free /health route (200) instead
# of the auth-gated /mcp (401/400). A 200 proves the event loop is responsive — a
# wedged loop stops answering /health.
code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${MCP_PORT}/health" || true)
if [[ "$code" != "200" ]]; then
  echo "mcp health failed at :${MCP_PORT}: $code" >&2
  rc=1
fi

if [[ "${AUTO_TRADER_HEALTHCHECK_SKIP_WS:-0}" != "1" && "$MODE" == "stable" ]]; then
  WS_MONITOR_HEARTBEAT_PATH="$AUTO_TRADER_BASE/state/heartbeat/kis.json" WS_MONITOR_EXPECT_MODE=kis \
    uv run python scripts/websocket_healthcheck.py || rc=1
  WS_MONITOR_HEARTBEAT_PATH="$AUTO_TRADER_BASE/state/heartbeat/upbit.json" WS_MONITOR_EXPECT_MODE=upbit \
    uv run python scripts/websocket_healthcheck.py || rc=1
fi

exit $rc
