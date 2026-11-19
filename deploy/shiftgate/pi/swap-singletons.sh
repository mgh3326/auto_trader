#!/usr/bin/env bash
# pi 싱글턴 컨테이너(at-worker, at-upbit-ws) 교체 (hk 5617).
#
# shiftgate 는 HTTP 서비스(binds + health 필수)만 다룬다. worker 와 upbit websocket 은 한 번에 하나만
# 있어야 하는 싱글턴이라 shiftgate 범위 밖이고(shiftgate docs/design.md "gap"), 현재 at-swap 동작 그대로
# 이 스크립트가 같은 digest 로 교체한다:
#   옛 컨테이너 정지(-t 60) → <이름>-prev 로 보관 → 새 digest 로 docker run → 로그/상태 확인.
#   어느 하나라도 실패하면 이미 바꾼 것까지 역순으로 옛 컨테이너를 복원하고 exit 1.
# deployed-digest 는 여기서 쓰지 않는다: shiftgate 두 서비스와 싱글턴이 모두 성공한 뒤 deploy.sh 가 한 번 쓴다.
# 스케줄러·타이머는 등록하지 않는다. 수동 실행만.
set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
RUN_DIRECTORY="${AT_RUN_DIR:-/root/at-run}"
ENV_API="${AT_ENV_API:-/srv/at/secrets/.env.api}"
ENV_SCHEDULER="${AT_ENV_SCHEDULER:-/srv/at/secrets/.env.scheduler}"
WAIT_ATTEMPTS="${AT_SWAP_WAIT_ATTEMPTS:-60}"
WAIT_SLEEP_SECONDS="${AT_SWAP_WAIT_SLEEP:-2}"
STOP_TIMEOUT_SECONDS=60

usage() {
  cat >&2 <<'USAGE'
usage: swap-singletons.sh [--wait-attempts N] <ghcr.io/mgh3326/auto_trader@sha256:DIGEST>
  at-worker 와 at-upbit-ws 를 주어진 불변 이미지로 교체한다. 실패하면 옛 컨테이너를 복원하고 exit 1.
  환경 변수: AT_RUN_DIR AT_ENV_API AT_ENV_SCHEDULER AT_SWAP_WAIT_ATTEMPTS AT_SWAP_WAIT_SLEEP
USAGE
}

IMAGE=""
while (($#)); do
  case "$1" in
    --wait-attempts) [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || { usage; exit 2; }; WAIT_ATTEMPTS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; exit 2 ;;
    *) [[ -z "$IMAGE" ]] || { usage; exit 2; }; IMAGE="$1"; shift ;;
  esac
done
[[ "$IMAGE" =~ ^${IMAGE_REPOSITORY}@sha256:[0-9a-f]{64}$ ]] || { usage; printf 'image must be %s@sha256:<64hex>\n' "$IMAGE_REPOSITORY" >&2; exit 2; }

SINGLETONS=(at-worker at-upbit-ws)
log_pattern() {
  case "$1" in
    at-worker) printf 'Listening started|Starting 1 worker processes.' ;;
    at-upbit-ws) printf 'connected=True' ;;
  esac
}
run_command() {
  case "$1" in
    at-worker) printf '%s\n' '/app/.venv/bin/taskiq worker app.core.taskiq_broker:broker app.tasks --workers 1' ;;
    at-upbit-ws) printf '%s\n' '/app/.venv/bin/python websocket_monitor.py --mode upbit' ;;
  esac
}

exists() { docker inspect --format '{{.Id}}' "$1" >/dev/null 2>&1; }

# SWAPPED: 이번 실행이 건드린 컨테이너(역순 복원용). HAD_OLD: 그중 옛 컨테이너를 <이름>-prev 로 보관한 것(공백 구분).
SWAPPED=()
HAD_OLD=" "

save_failure_logs() {
  local name="$1" file
  file="${RUN_DIRECTORY}/swap-failure-${name}-$(date -u +%Y%m%dT%H%M%SZ).log"
  ( umask 077; docker logs --timestamps --tail 60 "$name" >"$file" 2>&1 ) \
    && printf 'failure logs saved: %s\n' "$file" >&2 \
    || printf 'warning: could not save failure logs for %s\n' "$name" >&2
}

restore_one() {
  local name="$1"
  docker rm -f "$name" >/dev/null 2>&1 || true
  if [[ "$HAD_OLD" == *" ${name} "* ]]; then
    docker rename "${name}-prev" "$name" && docker start "$name" >/dev/null \
      || { printf 'RESTORE FAILED for %s: rename/start %s-prev by hand\n' "$name" "$name" >&2; return 1; }
    printf 'restored: %s\n' "$name" >&2
  fi
}

restore_all() {
  local i rc=0
  for ((i=${#SWAPPED[@]}-1; i>=0; i--)); do restore_one "${SWAPPED[i]}" || rc=1; done
  return "$rc"
}

wait_ready() {
  local name="$1" pattern attempt state
  pattern="$(log_pattern "$name")"
  for ((attempt=1; attempt<=WAIT_ATTEMPTS; attempt++)); do
    state="$(docker inspect --format '{{.State.Running}}' "$name" 2>/dev/null || printf false)"
    [[ "$state" == true ]] || { printf '%s is not running\n' "$name" >&2; return 1; }
    if docker logs --tail 100 "$name" 2>&1 | grep -Eq "$pattern"; then return 0; fi
    sleep "$WAIT_SLEEP_SECONDS"
  done
  printf '%s did not log ready within %s attempts\n' "$name" "$WAIT_ATTEMPTS" >&2
  return 1
}

start_new() {
  local name="$1"
  local -a cmd
  read -r -a cmd <<<"$(run_command "$name")"
  docker run -d --name "$name" --restart unless-stopped --network host \
    --env-file "$ENV_API" --env-file "$ENV_SCHEDULER" \
    "$IMAGE" "${cmd[@]}" >/dev/null
}

swap_one() {
  local name="$1"
  if exists "$name"; then
    # 보관 자리는 한 세대만 둔다: 직전 세대는 이번 교체가 시작될 때 버린다.
    docker rm -f "${name}-prev" >/dev/null 2>&1 || true
    docker stop -t "$STOP_TIMEOUT_SECONDS" "$name" >/dev/null || { printf 'could not stop %s\n' "$name" >&2; docker start "$name" >/dev/null 2>&1 || true; return 1; }
    docker rename "$name" "${name}-prev" || { docker start "$name" >/dev/null 2>&1 || true; return 1; }
    HAD_OLD="${HAD_OLD}${name} "
  fi
  SWAPPED+=("$name")
  start_new "$name" || { printf 'docker run failed for %s\n' "$name" >&2; return 1; }
  wait_ready "$name" || { save_failure_logs "$name"; return 1; }
  printf 'swapped: %s -> %s\n' "$name" "$IMAGE"
}

for name in "${SINGLETONS[@]}"; do
  if ! swap_one "$name"; then
    printf 'singleton swap FAILED at %s; restoring previous containers\n' "$name" >&2
    restore_all || exit 1
    exit 1
  fi
done
printf 'singletons OK: %s\n' "${SINGLETONS[*]}"
