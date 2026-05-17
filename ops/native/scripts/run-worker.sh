#!/usr/bin/env bash
set -euo pipefail
source "${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/scripts/common.sh"
exec uv run taskiq worker app.core.taskiq_broker:broker app.tasks --workers 1
