#!/bin/bash
# Deprecated legacy Docker production health check.
#
# ROB-263 retired the Raspberry Pi / Docker Compose production runtime. This
# script intentionally fails closed instead of checking legacy Docker container
# names that should no longer exist in production.

set -euo pipefail

cat >&2 <<'EOF'
❌ scripts/healthcheck.sh is retired for production.

The Raspberry Pi Docker production stack was decommissioned in ROB-263.
Do not use legacy Docker container health checks for production API/worker/MCP/websocket services.

Use the MacBook native launchd/service runbook instead, or inspect the native host directly.
Helpful entrypoints:
  - .github/workflows/deploy-macos-native.yml
  - scripts/deploy-native.sh
  - ops/native/
EOF

exit 1
