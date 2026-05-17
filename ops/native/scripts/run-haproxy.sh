#!/usr/bin/env bash
# ROB-259 review fix: HAProxy launchd wrapper.
#
# Resolves the haproxy binary via PATH at runtime instead of hardcoding a
# Homebrew-prefix-specific path in the launchd plist. This makes the plist
# work on both Apple Silicon and Intel Macs (where Homebrew picks different
# install prefixes), as long as `haproxy` is on the plist's PATH.
set -euo pipefail

BASE="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}"
CFG="${AUTO_TRADER_HAPROXY_LIVE:-$BASE/shared/haproxy/haproxy.cfg}"

HAPROXY_BIN="$(command -v haproxy || true)"
if [[ -z "$HAPROXY_BIN" ]]; then
  echo "run-haproxy.sh: haproxy binary not found on PATH ($PATH)" >&2
  echo "Install with: brew install haproxy" >&2
  exit 78
fi

if [[ ! -f "$CFG" ]]; then
  echo "run-haproxy.sh: haproxy config missing: $CFG" >&2
  echo "Run scripts/native_haproxy_first_cutover.sh to bootstrap the config" >&2
  exit 78
fi

# -W enables master-worker mode so SIGUSR2 triggers seamless reload.
exec "$HAPROXY_BIN" -W -f "$CFG"
