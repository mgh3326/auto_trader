#!/usr/bin/env bash
# pi 배포 진입점 (hk 5617): 한 번에 한 이미지를 at-api → at-mcp → 싱글턴 순서로 배포하거나 복구한다.
#
# 전진 배포: deploy.sh [--allow-unverified REASON] <tag>
#   0. 호스트 배포 잠금(host-lock.sh)을 잡는다. 다른 배포·롤백·싱글턴 교체가 돌고 있으면 exit 75.
#   1. scripts/deploy_ci_gate.py --image : 그 SHA 의 전체 스위트 초록을 확인하고 불변 digest 를 얻는다.
#      실패하면 exit 78 로 끝나며 이 단계에서는 어떤 컨테이너도 건드리지 않는다.
#   2. shiftgate deploy at-api / at-mcp  : blue/green 교체. 이미 그 digest 가 활성인 서비스는 건너뛴다
#      (shiftgate 는 같은 digest 라도 비활성 색을 지우고 다시 만들어 보관된 롤백 대상을 잃는다).
#   3. swap-singletons.sh                : at-upbit-ws 를 같은 digest 로 교체(이미 그 이미지면 건너뜀).
#      at-worker 는 hk 5753 에 TaskIQ worker 은퇴로 이 배포 경로에서 빠졌다. 컨테이너 정지는 설계 세션이 한다.
#   4. 세 컴포넌트(at-api, at-mcp, at-upbit-ws)가 모두 그 digest 인지 다시 확인한 뒤에만
#      /root/at-run/deployed-digest 를 갱신한다.
#   5. (hk 6437) 마커를 쓴 성공 뒤에만 오래된 auto_trader 이미지를 정리한다: 현재·이전 digest 와 컨테이너가 쓰는 이미지는 남긴다.
#      AT_IMAGE_PRUNE_ENABLED=0 이면 끈다. 복구·롤백·실패 경로는 정리하지 않는다.
#   중간에 실패하면 거기서 멈추고 상태를 출력한다. 같은 명령을 다시 실행하면 이미 끝난 컴포넌트는 건너뛰고
#   나머지만 이어서 한다(보관된 이전 컨테이너와 deployed-digest.previous 는 그대로 보존된다).
# 복구: deploy.sh --rollback
#   deployed-digest(마지막으로 세 컴포넌트가 모두 끝난 릴리스)와 다른 컴포넌트가 있으면 그것들만 그 digest 로
#   되돌린다(마커는 그대로). 전부 같으면 deployed-digest.previous 로 완결 롤백하고 마커를 맞바꾼다.
#   롤백 경로는 CI 게이트를 보지 않는다(긴급 경로).
#   hk 5753: 이 릴리스 이전 이미지로 되돌리면 TaskIQ producer 가 다시 살아난다. 워커가 멈춰 있으면 큐에 메시지가
#   쌓이므로, 롤백 전에 docs/runbooks/pi-shiftgate-deploy.md 「롤백 경계」의 조건(W5 게이트 off, OPS_TASK_KICK_TOKEN
#   비움, NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED=false)을 운영자가 확인한다. 이 스크립트는 그 조건을 검사하지 않는다.
# 이 스크립트는 타이머를 설치하지 않는다. 자동 배포(hk 6037)는 운영자가 설치한 follow.sh 가 같은 진입점(deploy.sh <sha-태그>)을
# 부르는 것이고, shiftgate 자체의 follow/check/prune 타이머는 등록하지 않는다. 수동 실행도 그대로 쓴다.
set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIRECTORY="${AT_RUN_DIR:-/root/at-run}"
SHIFTGATE="${AT_SHIFTGATE_BIN:-shiftgate}"
SHIFTGATE_CONFIG="${AT_SHIFTGATE_CONFIG:-/etc/shiftgate/services.yaml}"
SWAP_SINGLETONS="${AT_SWAP_SINGLETONS:-${SCRIPT_DIRECTORY}/swap-singletons.sh}"
PYTHON="${AT_DEPLOY_GATE_PYTHON:-python3}"
DEPLOYED_DIGEST_FILE="${RUN_DIRECTORY}/deployed-digest"
DEPLOYED_DIGEST_PREVIOUS_FILE="${DEPLOYED_DIGEST_FILE}.previous"
readonly SHIFTGATE_SERVICES=(at-api at-mcp)
readonly SINGLETONS=(at-upbit-ws)
# 성공한 전진 배포 뒤의 오래된 이미지 정리 스위치(hk 6437, task 934 규칙). 1 기본, 0 끔, 그 밖의 값은 경고 후 건너뜀.
readonly IMAGE_PRUNE_ENABLED="${AT_IMAGE_PRUNE_ENABLED-1}"

usage() {
  cat >&2 <<'USAGE'
usage: deploy.sh [--allow-unverified REASON] <tag>
       deploy.sh --rollback
  <tag>  ghcr.io/mgh3326/auto_trader 의 태그(예: main). 게이트가 그 태그를 불변 digest 로 고정한다.
         digest 를 직접 받지 않는다: 게이트는 태그만 받아 이미지의 SHA 를 확인한다.
  --allow-unverified REASON  CI 증거만 면제하는 기록되는 우회(이미지 고정은 그대로).
  --rollback  세 컴포넌트(at-api, at-mcp, at-upbit-ws)를 deployed-digest 로 수렴시키고, 이미 같으면 deployed-digest.previous 로 되돌린다.
  환경 변수: AT_DEPLOY_GATE_PYTHON AT_DEPLOY_GATE_SCRIPT AT_FOLLOW_VERIFIED_DIGEST(follow.sh 전용) AT_SHIFTGATE_BIN AT_SHIFTGATE_CONFIG AT_SWAP_SINGLETONS AT_RUN_DIR AT_IMAGE_PRUNE_ENABLED(0 이면 오래된 이미지 정리를 끈다)
  종료 코드: 2 사용법 · 75 다른 배포가 잠금을 잡음 · 78 게이트/설정 거부 · 그 밖은 실패한 단계의 코드(3 = shiftgate recover 필요)
USAGE
}

ALLOW_UNVERIFIED=""
TAG=""
ROLLBACK=false
while (($#)); do
  case "$1" in
    --allow-unverified) [[ $# -ge 2 ]] || { usage; exit 2; }; ALLOW_UNVERIFIED="$2"; shift 2 ;;
    --rollback) ROLLBACK=true; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; exit 2 ;;
    *) [[ -z "$TAG" ]] || { usage; exit 2; }; TAG="$1"; shift ;;
  esac
done
if [[ "$ROLLBACK" == true ]]; then
  [[ -z "$TAG" && -z "$ALLOW_UNVERIFIED" ]] || { usage; exit 2; }
else
  [[ -n "$TAG" && "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || { usage; exit 2; }
fi

# shellcheck source=host-lock.sh
source "${SCRIPT_DIRECTORY}/host-lock.sh"

is_ref() { [[ "$1" =~ ^${IMAGE_REPOSITORY}@sha256:[0-9a-f]{64}$ ]]; }

gate_script() {
  local candidate
  if [[ -n "${AT_DEPLOY_GATE_SCRIPT:-}" ]]; then
    [[ -r "$AT_DEPLOY_GATE_SCRIPT" ]] && { printf '%s\n' "$AT_DEPLOY_GATE_SCRIPT"; return 0; }
    return 1
  fi
  # 호스트에는 deploy_ci_gate.py 를 이 스크립트 옆에 설치하고, 레포 안에서 실행하면 scripts/ 의 것을 쓴다.
  for candidate in "${SCRIPT_DIRECTORY}/deploy_ci_gate.py" "${SCRIPT_DIRECTORY}/../../../scripts/deploy_ci_gate.py"; do
    [[ -r "$candidate" ]] && { printf '%s\n' "$candidate"; return 0; }
  done
  return 1
}

python_ok() {
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 \
    || { printf 'Python >= 3.11 is required; %s is older or unusable (set AT_DEPLOY_GATE_PYTHON)\n' "$PYTHON" >&2; return 78; }
}

verified_deploy_image() {
  local gate digest resolved
  local -a args=(--image "${IMAGE_REPOSITORY}:${TAG}")
  gate="$(gate_script)" || { printf 'deploy gate is unavailable: install scripts/deploy_ci_gate.py beside this script\n' >&2; return 78; }
  python_ok || return 78
  # follow.sh (hk 6091) has just run the whole gate on this very digest and hands it over so that one tick does
  # not ask GitHub twice (60/hour anonymous). The handoff only replaces the GitHub half: the tag must still
  # resolve, in the registry, to exactly that digest. Anything else (no handoff, a mismatch, a registry error)
  # falls through to the full gate below, so a manual run is exactly as strict as before.
  if [[ -z "$ALLOW_UNVERIFIED" ]] && is_ref "${AT_FOLLOW_VERIFIED_DIGEST:-}"; then
    resolved="$("$PYTHON" "$gate" "${args[@]}" --resolve-only 2>/dev/null)" || resolved=""
    if [[ "${resolved%%$'\t'*}" == "$AT_FOLLOW_VERIFIED_DIGEST" ]]; then
      printf '%s\n' "$AT_FOLLOW_VERIFIED_DIGEST"
      return 0
    fi
  fi
  [[ -z "$ALLOW_UNVERIFIED" ]] || args+=(--allow-unverified "$ALLOW_UNVERIFIED")
  digest="$("$PYTHON" "$gate" "${args[@]}")" || return $?
  is_ref "$digest" || { printf 'deploy gate returned no immutable image digest\n' >&2; return 78; }
  printf '%s\n' "$digest"
}

# --- 컴포넌트 현재 상태(읽기 전용) --------------------------------------------------------------

# shiftgate status --json 에서 서비스의 "<active> <previous> <rollback_available> <pending>" 한 줄을 낸다.
# 읽을 수 없거나 서비스가 없으면 실패한다(모르는 상태에서 건너뛰지 않는다).
sg_state() {
  local service="$1" json
  json="$("$SHIFTGATE" --config "$SHIFTGATE_CONFIG" status --json)" || return 1
  printf '%s' "$json" | "$PYTHON" -c '
import json, sys
rows = json.load(sys.stdin)
row = next(r for r in rows if r["name"] == sys.argv[1])
state = row.get("state") or {}
active = (state.get("active") or {}).get("digest") or "none"
previous = (state.get("previous") or {}).get("digest") or "none"
print(active, previous, "true" if state.get("rollback_available") else "false", "true" if state.get("pending") else "false")
' "$service"
}

sg_active() { local line; line="$(sg_state "$1")" || return 1; printf '%s\n' "${line%% *}"; }

singleton_image() {
  local running image
  running="$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null)" || return 1
  [[ "$running" == true ]] || return 1
  image="$(docker inspect --format '{{.Config.Image}}' "$1" 2>/dev/null)" || return 1
  printf '%s\n' "$image"
}

# 세 컴포넌트가 모두 ref 의 digest 인지 확인한다. 어긋난 이름을 stderr 로 알리고 어긋남이 있으면 1.
all_at() {
  local ref="$1" digest="${1#*@}" name rc=0 active
  for name in "${SHIFTGATE_SERVICES[@]}"; do
    active="$(sg_active "$name")" || { printf 'cannot read state of %s\n' "$name" >&2; rc=1; continue; }
    [[ "$active" == "$digest" ]] || { printf 'not at target: %s (%s)\n' "$name" "$active" >&2; rc=1; }
  done
  for name in "${SINGLETONS[@]}"; do
    [[ "$(singleton_image "$name" 2>/dev/null || true)" == "$ref" ]] || { printf 'not at target: %s\n' "$name" >&2; rc=1; }
  done
  return "$rc"
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
  printf 'rerun the same command to resume: components already at the target are skipped\n' >&2
  print_state
  exit "$rc"
}

# --- deployed-digest 기록: 모든 파일 작업을 명시적으로 확인하고, 마지막 rename 전에는 활성 마커를 바꾸지 않는다 ----

read_marker() { [[ -r "$1" ]] && head -n 1 "$1" || true; }

write_marker() {
  local new="$1" old tmp prev_tmp=""
  is_ref "$new" || { printf 'refusing to record %s\n' "$new" >&2; return 1; }
  [[ ! -d "$DEPLOYED_DIGEST_FILE" && ! -d "$DEPLOYED_DIGEST_PREVIOUS_FILE" ]] || { printf 'deployed-digest paths must be files\n' >&2; return 1; }
  mkdir -p "$RUN_DIRECTORY" || return 1
  old="$(read_marker "$DEPLOYED_DIGEST_FILE")"
  [[ "$old" != "$new" ]] || return 0   # 같은 digest 재기록은 previous 를 지우지 않는다(no-op)
  tmp="$(mktemp "${RUN_DIRECTORY}/.deployed-digest.XXXXXX")" || return 1
  if ! printf '%s\n' "$new" >"$tmp"; then rm -f "$tmp"; return 1; fi
  if [[ "$(head -n 1 "$tmp")" != "$new" ]]; then rm -f "$tmp"; return 1; fi
  if [[ -n "$old" ]]; then
    prev_tmp="$(mktemp "${RUN_DIRECTORY}/.deployed-digest.previous.XXXXXX")" || { rm -f "$tmp"; return 1; }
    if ! printf '%s\n' "$old" >"$prev_tmp"; then rm -f "$tmp" "$prev_tmp"; return 1; fi
    if ! mv -f "$prev_tmp" "$DEPLOYED_DIGEST_PREVIOUS_FILE"; then rm -f "$tmp" "$prev_tmp"; return 1; fi
  fi
  if ! mv -f "$tmp" "$DEPLOYED_DIGEST_FILE"; then rm -f "$tmp"; return 1; fi
}

# --- 오래된 이미지 정리 (hk 6437: task 934 규칙을 pi 로 이식; 정본 scripts/deploy-ncp-pull.sh 와 docs/contracts/task-934-image-prune.md)
# 성공한 전진 배포가 deployed-digest 를 쓴 뒤에만 돈다(롤백·실패·이미 배포됨 경로는 아무것도 지우지 않는다).
# 보존: 현재 digest, deployed-digest.previous(롤백 대상), 상태와 무관하게 컨테이너가 쓰는 모든 이미지.
# 입력을 하나라도 읽지 못하면 아무것도 지우지 않는다. 지우는 것은 docker image rm(-f 없음)뿐이다.
# 이 블록은 scripts/deploy-ncp-pull.sh 의 같은 함수들을 복사한 것이다(NCP 스크립트 동작은 바꾸지 않는다).
is_image_id() { [[ "$1" =~ ^sha256:[[:xdigit:]]{64}$ ]]; }
# 이미지 참조의 저장소 부분: '@' 뒤(digest)를 버리고, '/' 를 포함하지 않는 마지막 ':' 뒤(태그)만 버린다(레지스트리 포트 보존).
ref_repository() {
  local ref="${1%%@*}"
  if [[ "$ref" == *:* && "${ref##*:}" != */* ]]; then ref="${ref%:*}"; fi
  printf '%s\n' "$ref"
}
# 엄격한 digest 기록 판독: 파일은 정확히 digest 한 줄 + 개행 하나여야 한다(write_marker 가 쓰는 형식).
# 바이트 수를 세는 이유는 read 가 NUL 바이트를 버리기 때문이다(숨은 내용을 거른다).
read_digest_record() {
  local file="$1" line size
  [[ -f "$file" ]] || return 1
  IFS= read -r line <"$file" || return 1
  is_ref "$line" || return 1
  size="$(wc -c <"$file")" || return 1
  size="${size//[[:space:]]/}"
  [[ "$size" == "$((${#line} + 1))" ]] || return 1
  printf '%s\n' "$line"
}
image_refs() { docker image inspect --format '{{range .RepoTags}}{{println .}}{{end}}{{range .RepoDigests}}{{println .}}{{end}}' "$1"; }

# PRUNE_IDS(제거 후보, 목록 순서), PRUNE_REFS(id -> 공백으로 구분한 참조), PRUNE_KEPT(id -> 보존 사유)를 채운다.
# docker 조회가 실패하거나 예상 밖의 모양을 내면 아무것도 결정하지 않은 채 0 이 아닌 값으로 돌아온다.
declare -a PRUNE_IDS=()
declare -A PRUNE_REFS=() PRUNE_KEPT=()
plan_image_prune() {
  local current="$1" previous="$2" ref id cid image_id listed refs owned foreign keep
  local -A keep_ids=() seen=()
  PRUNE_IDS=() PRUNE_REFS=() PRUNE_KEPT=()
  for ref in "$current" "$previous"; do
    is_ref "$ref" || return 1
    # 기록된 digest 가 로컬에 없으면 지킬 것이 없다. 후보는 아래 RepoDigests 비교로도 걸러진다.
    if id="$(docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null)"; then
      is_image_id "$id" || return 1
      keep_ids["$id"]="recorded digest"
    fi
  done
  listed="$(docker ps -a --no-trunc --format '{{.ID}}')" || return 1
  while IFS= read -r cid; do
    [[ -n "$cid" ]] || continue
    image_id="$(docker inspect --format '{{.Image}}' "$cid")" || return 1
    is_image_id "$image_id" || return 1
    keep_ids["$image_id"]="used by a container"
  done <<<"$listed"
  listed="$(docker image ls --no-trunc --format '{{.ID}}')" || return 1
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    is_image_id "$id" || return 1
    [[ -z "${seen[$id]:-}" ]] || continue
    seen["$id"]=1
    refs="$(image_refs "$id")" || return 1
    owned=false foreign=false keep=false
    while IFS= read -r ref; do
      [[ -n "$ref" ]] || continue
      if [[ "$(ref_repository "$ref")" == "$IMAGE_REPOSITORY" ]]; then owned=true; else foreign=true; fi
      if [[ "$ref" == "$current" || "$ref" == "$previous" ]]; then keep=true; fi
    done <<<"$refs"
    [[ "$owned" == true && "$foreign" == false ]] || continue # 저장소: 이 저장소의 참조만 가진 이미지만 후보다
    if [[ -n "${keep_ids[$id]:-}" ]]; then PRUNE_KEPT["$id"]="${keep_ids[$id]}"; continue; fi
    if [[ "$keep" == true ]]; then PRUNE_KEPT["$id"]="recorded digest"; continue; fi
    PRUNE_IDS+=("$id")
    PRUNE_REFS["$id"]="${refs//$'\n'/ }"
  done <<<"$listed"
}

prune_enabled() {
  case "$IMAGE_PRUNE_ENABLED" in
    1) return 0 ;;
    0) return 1 ;;
    *) printf 'WARNING: image prune skipped: AT_IMAGE_PRUNE_ENABLED must be 0 or 1\n' >&2; return 1 ;;
  esac
}

prune_old_images() {
  local deployed="$1" current previous id out listed
  local -a refs=() failed=()
  local -A errors=() remaining=()
  prune_enabled || return 0
  previous="$(read_digest_record "$DEPLOYED_DIGEST_PREVIOUS_FILE")" || { printf 'WARNING: image prune skipped: %s is absent or invalid; every image kept\n' "$DEPLOYED_DIGEST_PREVIOUS_FILE" >&2; return 1; }
  current="$(read_digest_record "$DEPLOYED_DIGEST_FILE")" && [[ "$current" == "$deployed" ]] || { printf 'WARNING: image prune skipped: %s does not record %s; every image kept\n' "$DEPLOYED_DIGEST_FILE" "$deployed" >&2; return 1; }
  plan_image_prune "$deployed" "$previous" || { printf 'WARNING: image prune skipped: docker image or container state is unreadable; every image kept\n' >&2; return 1; }
  for id in "${!PRUNE_KEPT[@]}"; do printf 'image prune: kept %s (%s)\n' "$id" "${PRUNE_KEPT[$id]}"; done
  for id in "${PRUNE_IDS[@]}"; do
    read -r -a refs <<<"${PRUNE_REFS[$id]}"
    # 마지막 태그를 지우면 같은 호출의 나중 digest 참조가 실패할 수 있다. 결과는 아래 목록 재조회로 판정한다.
    out="$(docker image rm "${refs[@]}" 2>&1)" || errors["$id"]="${out//$'\n'/ }"
  done
  if ((${#PRUNE_IDS[@]})); then
    listed="$(docker image ls --no-trunc --format '{{.ID}}')" || { printf 'WARNING: image prune could not confirm removals; attempted: %s\n' "${PRUNE_IDS[*]}" >&2; return 1; }
    while IFS= read -r id; do [[ -n "$id" ]] && remaining["$id"]=1; done <<<"$listed"
  fi
  for id in "${PRUNE_IDS[@]}"; do
    if [[ -n "${remaining[$id]:-}" ]]; then
      failed+=("$id (${PRUNE_REFS[$id]% })")
      printf 'WARNING: image prune could not remove %s (%s): %s\n' "$id" "${PRUNE_REFS[$id]% }" "${errors[$id]:-still present}" >&2
      continue
    fi
    printf 'image prune: removed %s (%s)\n' "$id" "${PRUNE_REFS[$id]% }"
  done
  if ((${#failed[@]})); then
    printf 'WARNING: image prune incomplete; not removed: %s\n' "${failed[*]}" >&2
    return 1
  fi
}
# 서브셸이 정리의 모든 실패(errexit·nounset·exit 포함)를 가둔다. 경고만 더할 뿐 배포 결과는 바꾸지 않는다.
run_image_prune() { (prune_old_images "$1") || printf 'WARNING: image prune did not complete; the deployment result is unchanged\n' >&2; }

# --- 컴포넌트를 target 으로 이동 ----------------------------------------------------------------

# forward: 그 digest 가 이미 활성이면 건너뛴다. rollback: 보관된 이전 색이 그 digest 면 pull 없이 shiftgate rollback.
move_shiftgate_service() {
  local service="$1" ref="$2" mode="$3" digest="${2#*@}" line active previous rollback pending
  line="$(sg_state "$service")" || { printf 'cannot read state of %s\n' "$service" >&2; return 1; }
  read -r active previous rollback pending <<<"$line"
  [[ "$pending" == false ]] || { printf '%s has an unresolved operation: run recover first\n' "$service" >&2; return 3; }
  if [[ "$active" == "$digest" ]]; then
    printf 'already at target, skipped: %s\n' "$service" >&2
    return 0
  fi
  if [[ "$mode" == rollback && "$previous" == "$digest" && "$rollback" == true ]]; then
    "$SHIFTGATE" --config "$SHIFTGATE_CONFIG" rollback "$service"
  else
    "$SHIFTGATE" --config "$SHIFTGATE_CONFIG" deploy "$service" --digest "$digest"
  fi
}

converge() {
  local ref="$1" mode="$2" service
  for service in "${SHIFTGATE_SERVICES[@]}"; do
    move_shiftgate_service "$service" "$ref" "$mode" || fail_stage "shiftgate $mode $service" "$?"
  done
  "$SWAP_SINGLETONS" "$ref" || fail_stage "swap-singletons" "$?"
  all_at "$ref" || fail_stage "verify target ${ref#*@}" 1
}

acquire_host_lock || exit "$?"

if [[ "$ROLLBACK" == true ]]; then
  python_ok || exit 78
  marker="$(read_marker "$DEPLOYED_DIGEST_FILE")"
  is_ref "$marker" || { printf 'deployed-digest is missing or invalid; nothing to roll back to\n' >&2; exit 78; }
  if all_at "$marker" 2>/dev/null; then
    previous="$(read_marker "$DEPLOYED_DIGEST_PREVIOUS_FILE")"
    is_ref "$previous" || { printf 'deployed-digest.previous is missing or invalid; no earlier complete release is recorded\n' >&2; exit 78; }
    printf 'all components are at the recorded release; completing rollback to %s\n' "$previous" >&2
    converge "$previous" rollback
    write_marker "$previous" || fail_stage "write deployed-digest" "$?"
    printf 'rollback OK: %s\n' "$previous"
  else
    printf 'components differ from the recorded release; converging them to %s (deployed-digest unchanged)\n' "$marker" >&2
    converge "$marker" rollback
    printf 'rollback OK: %s\n' "$marker"
  fi
  exit 0
fi

# 1. 게이트 — 여기서 실패하면 아래 어떤 명령도 실행되지 않는다.
PINNED="$(verified_deploy_image)" || { rc=$?; printf 'deploy refused at the CI gate (exit %s); no container was touched\n' "$rc" >&2; exit "$rc"; }
printf 'deploying %s\n' "$PINNED" >&2

if [[ "$(read_marker "$DEPLOYED_DIGEST_FILE")" == "$PINNED" ]] && all_at "$PINNED" 2>/dev/null; then
  printf 'already deployed: %s (nothing to do)\n' "$PINNED"
  exit 0
fi

# 2~4. 이미 끝난 컴포넌트는 건너뛰며 이동하고, 전부 확인한 뒤에만 마커를 쓴다.
converge "$PINNED" forward
write_marker "$PINNED" || fail_stage "write deployed-digest" "$?"
printf 'deploy OK: %s\n' "$PINNED"
# 5. 성공한 전진 배포가 마커를 쓴 뒤에만 오래된 이미지를 정리한다(hk 6437). 정리 실패는 배포 결과를 바꾸지 않는다.
run_image_prune "$PINNED"
