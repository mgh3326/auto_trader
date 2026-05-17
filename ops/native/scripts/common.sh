#!/usr/bin/env bash
# Shared bootstrap for all native run-* wrappers.
# - Resolves AUTO_TRADER_BASE / AUTO_TRADER_CURRENT / AUTO_TRADER_ENV_FILE
# - cds into the active release directory
# - Exposes _export_selected_env_prefixes for selective env var loading
#
# Sourced (not executed). The wrappers that source this run under bash
# (see ROB-259 review fix); the file contains no zsh-specific syntax.
set -euo pipefail

export AUTO_TRADER_BASE="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}"
export AUTO_TRADER_CURRENT="${AUTO_TRADER_CURRENT:-$AUTO_TRADER_BASE/current}"
export AUTO_TRADER_ENV_FILE="${AUTO_TRADER_ENV_FILE:-$AUTO_TRADER_BASE/shared/.env.prod.native}"
export ENV_FILE="$AUTO_TRADER_ENV_FILE"
export HOME="${HOME:-/Users/mgh3326}"
export PATH="$HOME/.local/bin:$HOME/.hermes/node/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [[ ! -d "$AUTO_TRADER_CURRENT" ]]; then
  echo "AUTO_TRADER_CURRENT missing: $AUTO_TRADER_CURRENT" >&2
  exit 70
fi
if [[ ! -f "$AUTO_TRADER_ENV_FILE" ]]; then
  echo "AUTO_TRADER_ENV_FILE missing: $AUTO_TRADER_ENV_FILE" >&2
  exit 78
fi

cd "$AUTO_TRADER_CURRENT"

# Export only env vars that are read directly with os.getenv() by runtime bootstrap.
# Do not source the whole env file: this repo has JSON/list-like values such as PUBLIC_API_PATHS.
_export_selected_env_prefixes() {
  local prefixes=("$@")
  local key value prefix
  while IFS='=' read -r key value; do
    [[ -z "${key:-}" || "$key" == \#* ]] && continue
    key="${key%%[[:space:]]*}"
    for prefix in "${prefixes[@]}"; do
      if [[ "$key" == ${prefix}* ]]; then
        value="${value%$''}"
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"
        export "$key=$value"
      fi
    done
  done <"$AUTO_TRADER_ENV_FILE"
}
