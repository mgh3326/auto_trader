#!/usr/bin/env bash
# ROB-259: validate and atomically swap HAProxy config, then reload.
#
# Env:
#   AUTO_TRADER_BASE                  (required)
#   AUTO_TRADER_HAPROXY_TEMPLATE      defaults to <base>/scripts/haproxy/haproxy.cfg.tmpl
#                                     (deploy syncs this from release ops/native/haproxy/)
#   AUTO_TRADER_HAPROXY_LIVE          defaults to <base>/shared/haproxy/haproxy.cfg
#   AUTO_TRADER_HAPROXY_RELOAD        "launchctl" (default on prod), "skip" (tests)
#   AUTO_TRADER_HAPROXY_LABEL         defaults to com.robinco.auto-trader.haproxy
set -Eeuo pipefail

BASE="${AUTO_TRADER_BASE:?AUTO_TRADER_BASE required}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=native_bluegreen_lib.sh
source "$SCRIPT_DIR/native_bluegreen_lib.sh"

DEFAULT_TEMPLATE="$BASE/scripts/haproxy/haproxy.cfg.tmpl"
TEMPLATE="${AUTO_TRADER_HAPROXY_TEMPLATE:-$DEFAULT_TEMPLATE}"
LIVE="${AUTO_TRADER_HAPROXY_LIVE:-$BASE/shared/haproxy/haproxy.cfg}"
RELOAD_MODE="${AUTO_TRADER_HAPROXY_RELOAD:-launchctl}"
LABEL="${AUTO_TRADER_HAPROXY_LABEL:-com.robinco.auto-trader.haproxy}"

API_COLOR="$(detect_active_color api)"
MCP_COLOR="$(detect_active_color mcp)"

mkdir -p "$(dirname "$LIVE")"
TMP_OUT="$(mktemp "$(dirname "$LIVE")/haproxy-cfg.XXXXXX")"
trap 'rm -f "$TMP_OUT"' EXIT

AUTO_TRADER_API_ACTIVE_COLOR="$API_COLOR" \
AUTO_TRADER_MCP_ACTIVE_COLOR="$MCP_COLOR" \
  bash "$SCRIPT_DIR/haproxy_render.sh" "$TEMPLATE" "$TMP_OUT"

# Validate only when haproxy is installed. Tests usually don't have it.
if command -v haproxy >/dev/null 2>&1; then
  haproxy -c -f "$TMP_OUT" >/dev/null
fi

mv "$TMP_OUT" "$LIVE"
trap - EXIT

case "$RELOAD_MODE" in
  skip)
    ;;
  launchctl)
    uid_num="$(id -u)"
    # SIGUSR2 to master triggers seamless reload when master-worker is in haproxy.cfg.
    launchctl kill SIGUSR2 "gui/$uid_num/$LABEL"
    ;;
  *)
    echo "unknown AUTO_TRADER_HAPROXY_RELOAD: $RELOAD_MODE" >&2
    exit 64
    ;;
esac

echo "haproxy switched: api=$API_COLOR mcp=$MCP_COLOR live=$LIVE"
