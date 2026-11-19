#!/usr/bin/env bash
# pi 배포 진입점 (hk 5617): 한 번에 한 이미지를 at-api → at-mcp → 싱글턴 순서로 배포한다.
#
#   1. scripts/deploy_ci_gate.py --image : 그 SHA 의 전체 스위트 초록을 확인하고 불변 digest 를 얻는다.
#      실패하면 exit 78 로 끝나며 이 단계에서는 어떤 컨테이너도 건드리지 않는다.
#   2. shiftgate deploy at-api / at-mcp  : blue/green 교체 (실패하면 옛 색이 계속 서비스한다).
#   3. swap-singletons.sh                : at-worker, at-upbit-ws 를 같은 digest 로 교체.
#   4. /root/at-run/deployed-digest 갱신 : 1~3 이 모두 성공한 뒤 한 번만.
# 실패 시 멈추고 상태를 출력한다. 이미 끝난 앞 단계를 자동으로 되돌리지는 않는다(런북 §5 롤백).
# 스케줄러·shiftgate follow 타이머는 등록하지 않는다. 수동 실행만.
set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIRECTORY="${AT_RUN_DIR:-/root/at-run}"
SHIFTGATE="${AT_SHIFTGATE_BIN:-shiftgate}"
SHIFTGATE_CONFIG="${AT_SHIFTGATE_CONFIG:-/etc/shiftgate/services.yaml}"
SWAP_SINGLETONS="${AT_SWAP_SINGLETONS:-${SCRIPT_DIRECTORY}/swap-singletons.sh}"
DEPLOYED_DIGEST_FILE="${RUN_DIRECTORY}/deployed-digest"

usage() {
  cat >&2 <<'USAGE'
usage: deploy.sh [--allow-unverified REASON] <tag>
  <tag>  ghcr.io/mgh3326/auto_trader 의 태그(예: main). 게이트가 그 태그를 불변 digest 로 고정한다.
         digest 를 직접 받지 않는다: deploy_ci_gate.py --image 는 태그만 받아 SHA 를 확인한다.
  --allow-unverified REASON  CI 증거만 면제하는 기록되는 우회(이미지 고정은 그대로).
  환경 변수: AT_DEPLOY_GATE_PYTHON AT_DEPLOY_GATE_SCRIPT AT_SHIFTGATE_BIN AT_SHIFTGATE_CONFIG AT_SWAP_SINGLETONS AT_RUN_DIR
USAGE
}

ALLOW_UNVERIFIED=""
TAG=""
while (($#)); do
  case "$1" in
    --allow-unverified) [[ $# -ge 2 ]] || { usage; exit 2; }; ALLOW_UNVERIFIED="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; exit 2 ;;
    *) [[ -z "$TAG" ]] || { usage; exit 2; }; TAG="$1"; shift ;;
  esac
done
[[ -n "$TAG" && "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || { usage; exit 2; }

gate_script() {
  local candidate
  if [[ -n "${AT_DEPLOY_GATE_SCRIPT:-}" ]]; then printf '%s\n' "$AT_DEPLOY_GATE_SCRIPT"; return 0; fi
  # 호스트에는 deploy_ci_gate.py 를 이 스크립트 옆에 설치하고, 레포 안에서 실행하면 scripts/ 의 것을 쓴다.
  for candidate in "${SCRIPT_DIRECTORY}/deploy_ci_gate.py" "${SCRIPT_DIRECTORY}/../../../scripts/deploy_ci_gate.py"; do
    [[ -r "$candidate" ]] && { printf '%s\n' "$candidate"; return 0; }
  done
  return 1
}

verified_deploy_image() {
  local gate python digest
  local -a args=(--image "${IMAGE_REPOSITORY}:${TAG}")
  gate="$(gate_script)" || { printf 'deploy gate is unavailable: install scripts/deploy_ci_gate.py beside this script\n' >&2; return 78; }
  python="${AT_DEPLOY_GATE_PYTHON:-python3}"
  "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 \
    || { printf 'deploy gate requires Python >= 3.11; %s is older or unusable (set AT_DEPLOY_GATE_PYTHON)\n' "$python" >&2; return 78; }
  [[ -z "$ALLOW_UNVERIFIED" ]] || args+=(--allow-unverified "$ALLOW_UNVERIFIED")
  digest="$("$python" "$gate" "${args[@]}")" || return $?
  [[ "$digest" =~ ^${IMAGE_REPOSITORY}@sha256:[0-9a-f]{64}$ ]] \
    || { printf 'deploy gate returned no immutable image digest\n' >&2; return 78; }
  printf '%s\n' "$digest"
}

print_state() {
  printf '\n--- state after failure ---\n' >&2
  "$SHIFTGATE" --config "$SHIFTGATE_CONFIG" status >&2 || true
  docker ps -a --filter name=at- --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' >&2 || true
}

fail_stage() {
  local stage="$1" rc="$2"
  printf 'deploy FAILED at %s (exit %s); later stages were not run and deployed-digest is unchanged\n' "$stage" "$rc" >&2
  [[ "$rc" != 3 ]] || printf 'exit 3 = recovery required: run `%s recover <service>` before anything else\n' "$SHIFTGATE" >&2
  print_state
  exit "$rc"
}

write_digest() {
  local pinned="$1" tmp old
  mkdir -p "$RUN_DIRECTORY"
  umask 077
  tmp="$(mktemp "${RUN_DIRECTORY}/.deployed-digest.XXXXXX")"
  printf '%s\n' "$pinned" >"$tmp"
  if [[ -r "$DEPLOYED_DIGEST_FILE" ]]; then
    cp -f "$DEPLOYED_DIGEST_FILE" "${DEPLOYED_DIGEST_FILE}.previous"
  fi
  mv -f "$tmp" "$DEPLOYED_DIGEST_FILE"
}

# 1. 게이트 — 여기서 실패하면 아래 어떤 명령도 실행되지 않는다.
PINNED="$(verified_deploy_image)" || { rc=$?; printf 'deploy refused at the CI gate (exit %s); no container was touched\n' "$rc" >&2; exit "$rc"; }
DIGEST="${PINNED#*@}"
printf 'deploying %s\n' "$PINNED" >&2

# 2. shiftgate 두 서비스(순서 고정: api → mcp).
for service in at-api at-mcp; do
  "$SHIFTGATE" --config "$SHIFTGATE_CONFIG" deploy "$service" --digest "$DIGEST" || fail_stage "shiftgate deploy $service" "$?"
done

# 3. 싱글턴.
"$SWAP_SINGLETONS" "$PINNED" || fail_stage "swap-singletons" "$?"

# 4. 전부 성공한 뒤 한 번만 기록한다.
write_digest "$PINNED" || fail_stage "write deployed-digest" "$?"
printf 'deploy OK: %s\n' "$PINNED"
