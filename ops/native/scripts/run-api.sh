#!/bin/zsh
# ROB-259: color-aware FastAPI launcher.
set -euo pipefail

COLOR="${AUTO_TRADER_COLOR:-blue}"
case "$COLOR" in
  blue)  DEFAULT_PORT=8001 ;;
  green) DEFAULT_PORT=8002 ;;
  *)
    echo "run-api.sh: invalid AUTO_TRADER_COLOR=$COLOR (expected blue|green)" >&2
    exit 64
    ;;
esac

PORT="${AUTO_TRADER_API_PORT:-$DEFAULT_PORT}"

# Override AUTO_TRADER_CURRENT so common.sh cd's into the per-color symlink.
export AUTO_TRADER_CURRENT="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/current-$COLOR"

source "${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/scripts/common.sh"

exec uv run python -m uvicorn app.main:api --host 127.0.0.1 --port "$PORT"
