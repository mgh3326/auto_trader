#!/usr/bin/env bash
# deploy.sh 와 swap-singletons.sh 가 source 하는 호스트 배포 잠금 (hk 5617).
# shiftgate 의 잠금은 서비스 하나·명령 하나 단위라, 배포 두 개가 서로 다른 서비스 단계에서 엇갈려 끝나면
# API 와 MCP 가 서로 다른 digest 에 남고 deployed-digest 가 그것과 다를 수 있다. 그래서 한 배포(또는 롤백,
# 싱글턴 단독 교체)가 끝날 때까지 호스트 하나에 잠금 디렉터리 하나를 잡는다.
# mkdir 은 원자적이고 pid 파일로 죽은 소유자의 잠금을 치운다. 이미 잡혀 있으면 exit 75.
# 부모(deploy.sh)가 잡은 잠금은 AT_DEPLOY_LOCK_HELD=<부모 pid> 로 자식(swap-singletons.sh)에게 이어진다.

acquire_host_lock() {
  local dir="${RUN_DIRECTORY}/shiftgate-deploy.lock" holder
  mkdir -p "$RUN_DIRECTORY" || return 1
  if [[ -n "${AT_DEPLOY_LOCK_HELD:-}" ]]; then
    if [[ -r "$dir/pid" && "$(cat "$dir/pid" 2>/dev/null)" == "$AT_DEPLOY_LOCK_HELD" ]] && kill -0 "$AT_DEPLOY_LOCK_HELD" 2>/dev/null; then
      return 0
    fi
    printf 'AT_DEPLOY_LOCK_HELD does not match the host deploy lock\n' >&2
    return 75
  fi
  if ! mkdir "$dir" 2>/dev/null; then
    holder="$(cat "$dir/pid" 2>/dev/null || true)"
    if [[ "$holder" =~ ^[0-9]+$ ]] && kill -0 "$holder" 2>/dev/null; then
      printf 'another deployment holds %s (pid %s); wait for it to finish\n' "$dir" "$holder" >&2
      return 75
    fi
    # 소유자가 죽었다: 잠금만 치우고 다시 잡는다(배포가 중간에 죽었다면 상태는 §5 로 확인한다).
    rm -rf "$dir" && mkdir "$dir" 2>/dev/null || { printf 'cannot take %s\n' "$dir" >&2; return 75; }
  fi
  printf '%s\n' "$$" >"$dir/pid" || { rm -rf "$dir"; return 1; }
  HOST_LOCK_DIR="$dir"
  HOST_LOCK_OWNER="$$"
  trap release_host_lock EXIT
  export AT_DEPLOY_LOCK_HELD="$$"
}

release_host_lock() {
  [[ -n "${HOST_LOCK_DIR:-}" && "$(cat "$HOST_LOCK_DIR/pid" 2>/dev/null)" == "${HOST_LOCK_OWNER:-}" ]] && rm -rf "$HOST_LOCK_DIR"
  return 0
}
