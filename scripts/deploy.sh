#!/bin/bash
# Deprecated legacy Docker deployment entrypoint.
#
# ROB-263 retired the Raspberry Pi / Docker Compose production deploy path.
# Production serving now runs on NCP through scripts/deploy-ncp-pull.sh.
#
# This script intentionally fails closed so old GitHub Actions, cron jobs,
# or manual muscle-memory commands cannot resurrect the Raspberry Pi Docker
# stack and accidentally create a second KIS websocket owner.

set -euo pipefail

cat >&2 <<'EOF'
❌ scripts/deploy.sh is retired.

The legacy Raspberry Pi Docker production deploy path was decommissioned in ROB-263.
Do not use docker-compose.prod.yml to run production API/worker/MCP/websocket services.

Use the NCP pull-deploy path instead:
  - Script: scripts/deploy-ncp-pull.sh
  - Runbook: docs/runbooks/ncp-pull-deploy.md

If you are cleaning up an old Raspberry Pi host, stop the legacy stack manually:
  docker compose --env-file .env.prod -f docker-compose.prod.yml down --remove-orphans
EOF

exit 1
