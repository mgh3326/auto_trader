#!/usr/bin/env bash
# Run statically-audited auto_trader CLIs from the deployed image digest.
# This wrapper deliberately refuses a tag or a missing/foreign digest: jobs
# must never silently run a different revision from the serving deployment.
set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
readonly DEPLOYED_DIGEST_FILE="/root/at-run/deployed-digest"
# The production default is root-owned /run/lock.  The override is solely for
# hermetic tests and does not alter the lock key or enable concurrent runs.
readonly LOCK_DIRECTORY="${AT_JOB_LOCK_DIRECTORY:-/run/lock}"

usage() {
  printf 'usage: %s scripts.<module> [args...] [--at-job-step scripts.<module> [args...]]\n' "$0" >&2
  exit 64
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

variable_is_set() {
  declare -p "$1" >/dev/null 2>&1
}

module_timeout() {
  case "$1" in
    scripts.build_investor_flow_snapshots) printf '%s' 1800 ;;
    scripts.sync_toss_warnings) printf '%s' 3600 ;;
    scripts.build_invest_screener_snapshots) printf '%s' 7200 ;;
    scripts.build_invest_crypto_screener_snapshots) printf '%s' 3600 ;;
    scripts.build_crypto_insight_snapshots) printf '%s' 3600 ;;
    scripts.build_invest_kr_fundamentals_snapshots) printf '%s' 7200 ;;
    scripts.build_market_valuation_snapshots) printf '%s' 10800 ;;
    scripts.build_us_fundamentals_snapshots) printf '%s' 10800 ;;
    scripts.sync_toss_symbol_master) printf '%s' 900 ;;
    *)
      printf 'at-job refuses an unaudited module: %s\n' "$1" >&2
      exit 64
      ;;
  esac
}

commit_gate_enabled() {
  local key="$1"
  local value=""
  local line stripped

  if variable_is_set "$key"; then
    value="${!key}"
  elif [[ -f "$AT_RUNTIME_ENV_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      stripped="$(trim "$line")"
      [[ -z "$stripped" || "${stripped:0:1}" == "#" ]] && continue
      if [[ "$stripped" == "$key="* ]]; then
        value="$(trim "${stripped#*=}")"
        value="${value#\"}"
        value="${value%\"}"
        value="${value#\'}"
        value="${value%\'}"
        break
      fi
    done <"$AT_RUNTIME_ENV_FILE"
  fi

  case "$(trim "$value" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

emit_summary() {
  local summary_module="$1" summary_rc="$2" summary_elapsed="$3" summary_digest="$4"
  local step="$5" steps_total="$6"
  if (( steps_total == 1 )) && [[ -z "$commit_env" ]]; then
    # Legacy single-step output is intentionally byte-for-byte unchanged.
    printf '{"module":"%s","rc":%d,"elapsed_s":%d,"image_digest":"%s"}\n' \
      "$summary_module" "$summary_rc" "$summary_elapsed" "$summary_digest"
  elif (( steps_total == 1 )); then
    printf '{"module":"%s","rc":%d,"elapsed_s":%d,"image_digest":"%s","commit":%s,"commit_env":"%s"}\n' \
      "$summary_module" "$summary_rc" "$summary_elapsed" "$summary_digest" \
      "$commit_enabled" "$commit_env"
  elif [[ -n "$commit_env" ]]; then
    printf '{"module":"%s","rc":%d,"elapsed_s":%d,"image_digest":"%s","commit":%s,"commit_env":"%s","step":%d,"steps_total":%d}\n' \
      "$summary_module" "$summary_rc" "$summary_elapsed" "$summary_digest" \
      "$commit_enabled" "$commit_env" "$step" "$steps_total"
  else
    printf '{"module":"%s","rc":%d,"elapsed_s":%d,"image_digest":"%s","step":%d,"steps_total":%d}\n' \
      "$summary_module" "$summary_rc" "$summary_elapsed" "$summary_digest" \
      "$step" "$steps_total"
  fi
}

(( $# >= 1 )) || usage
tokens=("$@")
step_starts=(0)
step_ends=()
for (( token_index = 0; token_index < ${#tokens[@]}; token_index++ )); do
  if [[ "${tokens[token_index]}" == "--at-job-step" ]]; then
    (( token_index > step_starts[${#step_starts[@]} - 1] )) || usage
    step_ends+=("$token_index")
    (( token_index + 1 < ${#tokens[@]} )) || usage
    step_starts+=("$((token_index + 1))")
  fi
done
step_ends+=("${#tokens[@]}")
steps_total="${#step_starts[@]}"

if variable_is_set AT_JOB_STEPS; then
  [[ "$AT_JOB_STEPS" =~ ^[1-9][0-9]*$ ]] || usage
  [[ "$AT_JOB_STEPS" -eq "$steps_total" ]] || usage
elif (( steps_total != 1 )); then
  usage
fi

for (( step_index = 0; step_index < steps_total; step_index++ )); do
  start="${step_starts[step_index]}"
  module="${tokens[start]}"
  [[ "$module" =~ ^scripts\.[A-Za-z_][A-Za-z0-9_]*$ ]] || usage
  module_timeout "$module" >/dev/null
done

first_module="${tokens[0]}"
unit="${first_module#scripts.}"
lock_file="${LOCK_DIRECTORY}/at-job-${unit}.lock"
mkdir -p "$LOCK_DIRECTORY"
exec 9>"$lock_file"
commit_env="${AT_JOB_COMMIT_ENV:-}"
commit_enabled=false
if variable_is_set AT_JOB_COMMIT_ENV; then
  [[ "$commit_env" =~ ^[A-Z][A-Z0-9_]*$ ]] || usage
fi
if ! flock -n 9; then
  emit_summary "$first_module" 75 0 unresolved 1 "$steps_total"
  exit 75
fi

image_digest=""
if [[ -f "$DEPLOYED_DIGEST_FILE" ]]; then
  IFS= read -r image_digest <"$DEPLOYED_DIGEST_FILE" || true
fi
if [[ ! "$image_digest" =~ ^${IMAGE_REPOSITORY}@sha256:[[:xdigit:]]{64}$ ]]; then
  printf 'at-job refuses unresolved or non-digest deployed image\n' >&2
  emit_summary "$first_module" 78 0 unresolved 1 "$steps_total"
  exit 78
fi
[[ -n "${AT_RUNTIME_ENV_FILE:-}" && -f "$AT_RUNTIME_ENV_FILE" ]] || {
  printf 'at-job requires AT_RUNTIME_ENV_FILE to name an existing environment file\n' >&2
  emit_summary "$first_module" 78 0 "$image_digest" 1 "$steps_total"
  exit 78
}

if variable_is_set AT_JOB_COMMIT_ENV; then
  if commit_gate_enabled "$commit_env"; then
    commit_enabled=true
  fi
fi

first_failure=0
failed_steps=()
for (( step_index = 0; step_index < steps_total; step_index++ )); do
  start="${step_starts[step_index]}"
  end="${step_ends[step_index]}"
  module="${tokens[start]}"
  timeout_seconds="$(module_timeout "$module")"
  docker_args=(
    docker run --rm --network host --workdir /app
    --env-file "$AT_RUNTIME_ENV_FILE" "$image_digest"
    /app/.venv/bin/python -m "$module"
  )
  if (( end - start > 1 )); then
    docker_args+=("${tokens[@]:start + 1:end - start - 1}")
  fi
  if [[ "$commit_enabled" == true ]]; then
    docker_args+=(--commit)
  fi

  start_seconds="$SECONDS"
  set +e
  timeout --preserve-status "${timeout_seconds}s" "${docker_args[@]}"
  rc=$?
  set -e
  elapsed_seconds=$((SECONDS - start_seconds))
  emit_summary "$module" "$rc" "$elapsed_seconds" "$image_digest" \
    "$((step_index + 1))" "$steps_total"
  if (( rc != 0 )); then
    failed_steps+=("$((step_index + 1))")
    (( first_failure == 0 )) && first_failure="$rc"
  fi
done

if (( steps_total > 1 )); then
  printf '{"steps_total":%d,"steps_failed":[' "$steps_total"
  (IFS=,; printf '%s' "${failed_steps[*]}")
  printf ']}\n'
fi
exit "$first_failure"
