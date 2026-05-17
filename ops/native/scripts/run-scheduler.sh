#!/usr/bin/env bash
set -euo pipefail
source "${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/scripts/common.sh"
exec uv run taskiq scheduler app.core.scheduler:sched app.tasks
