#!/usr/bin/env bash
# pi 자동 배포 follow (hk 6037): systemd 타이머가 1분마다 부른다. GitHub Actions 의 promote 가 옮긴 `deploy` 태그가
# 새 digest 를 가리킬 때만 deploy.sh 로 설치한다. pi 로 들어오는 경로는 없다(Actions 는 pi 에 접속하지 않는다).
#
# 태그를 옮긴 쪽을 믿지 않는다 — 설치 전에 이 스크립트가 스스로 다시 확인한다:
#   1. deploy 태그 → digest → 이미지 OCI revision(SHA)           (registry 만 읽는다, GitHub 호출 없음)
#   2. digest 가 /root/at-run/deployed-digest 와 같으면 조용히 exit 0   (GitHub API 는 digest 가 바뀔 때만 부른다)
#   3. 그 digest 가 hold 이면 건너뜀(실패한 digest 를 매 틱 다시 시도하지 않는다. 새 digest 가 오면 풀린다)
#   4. 호스트 배포 잠금(host-lock.sh)을 잡는다. 다른 배포 중이면 exit 75 = 다음 틱에 다시
#   5. deploy_ci_gate.py --github anonymous --main-ancestor : 그 SHA 가 main 의 조상이고 전체 스위트 6개 job 이 초록
#      (레포는 공개라 인증 없는 GitHub API 읽기. 한도 60회/시간이라 digest 가 바뀐 틱에만 부른다)
#   6. sha-<7> 태그가 같은 digest 인지 확인
#   7. deploy.sh sha-<7> (--allow-unverified 는 절대 쓰지 않는다) → 끝나면 deployed-digest 가 그 digest 인지 확인.
#      이 스크립트가 방금 검증한 digest 는 AT_FOLLOW_VERIFIED_DIGEST 로 넘긴다: deploy.sh 는 GitHub 를 다시 묻지 않고
#      sha-<7> 태그가 그 digest 인지만 레지스트리에서 확인한다(수동 deploy.sh 는 그대로 전체 게이트를 돈다)
# 설치 전 DB 백업은 이 스크립트가 하지 않는다(hk 6466, 운영자 결정 decision/2026-11-25/at-pg-backup-dagu-only):
# at-pg-backup.service 는 Dagu 일일 잡(deploy/dagu/at-maintenance/at-pg-backup.yaml, 매일 04:30 KST)이 돌린다.
# 실패하면 그 digest 를 hold 한다(사람이 보고 --clear-hold). GitHub 읽기 실패처럼 digest 와 무관한 일시 장애는 retry-after
# (AT_FOLLOW_RETRY_SECONDS) 뒤 다시 시도하되 digest 마다 최대 AT_FOLLOW_MAX_TRANSIENT_RETRIES 번(기본 3)까지다 — 그 뒤에는
# 영구 hold 로 바뀌어 사람이 --clear-hold 한다. 결과는 로그(/root/at-run/follow.log, journal)와
# /root/at-run/follow-status.json 에 남기고, panewire 가 있고 AT_FOLLOW_PANEWIRE_LANE 이 있으면 알린다(알림 실패는 배포에 영향 없음).
# 배포 롤백: deploy.sh --rollback 하기 전에 follow.sh --hold "이유" 로 지금의 deploy 태그 digest 를 hold 한다.
set -Eeuo pipefail

readonly IMAGE_REPOSITORY="ghcr.io/mgh3326/auto_trader"
SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIRECTORY="${AT_RUN_DIR:-/root/at-run}"
DEPLOY_TAG="${AT_FOLLOW_TAG:-deploy}"
PYTHON="${AT_DEPLOY_GATE_PYTHON:-python3}"
DEPLOY_SH="${AT_FOLLOW_DEPLOY_SH:-${SCRIPT_DIRECTORY}/deploy.sh}"
DEPLOYED_DIGEST_FILE="${RUN_DIRECTORY}/deployed-digest"
HOLD_FILE="${RUN_DIRECTORY}/follow-hold"
STATUS_FILE="${RUN_DIRECTORY}/follow-status.json"
LOG_FILE="${RUN_DIRECTORY}/follow.log"
TRANSIENT_RETRY_SECONDS="${AT_FOLLOW_RETRY_SECONDS:-600}"
MAX_TRANSIENT_RETRIES="${AT_FOLLOW_MAX_TRANSIENT_RETRIES:-3}"
TRANSIENT_FILE="${RUN_DIRECTORY}/follow-transient"

usage() {
  cat >&2 <<'USAGE'
usage: follow.sh                       deploy 태그가 새 digest 면 검증 후 설치한다(타이머가 부르는 형태)
       follow.sh --hold REASON         지금의 deploy 태그 digest 를 hold 한다(롤백 전에)
       follow.sh --clear-hold          hold 를 푼다(원인을 확인한 뒤)
       follow.sh --status              마지막 결과와 hold 를 출력한다
  환경 변수: AT_RUN_DIR AT_DEPLOY_GATE_PYTHON AT_DEPLOY_GATE_SCRIPT AT_FOLLOW_DEPLOY_SH
            AT_FOLLOW_TAG AT_FOLLOW_RETRY_SECONDS AT_FOLLOW_MAX_TRANSIENT_RETRIES AT_FOLLOW_PANEWIRE_LANE
  종료 코드: 0 할 일 없음/설치함 · 1 확인하지 못함/설치 전 실패 · 2 사용법 · 75 다른 배포가 잠금을 잡음(다음 틱) · 78 검증 거부 · 그 밖은 deploy.sh 의 코드
USAGE
}

MODE=run
HOLD_REASON=""
while (($#)); do
  case "$1" in
    --hold) [[ $# -ge 2 && -n "$2" ]] || { usage; exit 2; }; MODE=hold; HOLD_REASON="$2"; shift 2 ;;
    --clear-hold) MODE=clear; shift ;;
    --status) MODE=status; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

now() { date +%s; }

log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) follow: $*"
  printf '%s\n' "$line" >&2
  { mkdir -p "$RUN_DIRECTORY" && printf '%s\n' "$line" >>"$LOG_FILE"; } 2>/dev/null || true
}

gate_script() {
  local candidate
  if [[ -n "${AT_DEPLOY_GATE_SCRIPT:-}" ]]; then
    [[ -r "$AT_DEPLOY_GATE_SCRIPT" ]] && { printf '%s\n' "$AT_DEPLOY_GATE_SCRIPT"; return 0; }
    return 1
  fi
  for candidate in "${SCRIPT_DIRECTORY}/deploy_ci_gate.py" "${SCRIPT_DIRECTORY}/../../../scripts/deploy_ci_gate.py"; do
    [[ -r "$candidate" ]] && { printf '%s\n' "$candidate"; return 0; }
  done
  return 1
}

GATE=""
gate() { "$PYTHON" "$GATE" "$@"; }

# --- hold: "<digest>\t<created>\t<retry-after epoch or 0>\t<reason>" 한 줄 ------------------------------------------------

hold_write() { # digest retry_seconds(0 = 사람이 풀 때까지) reason
  local digest="$1" retry="$2" reason="$3" retry_at=0 tmp
  ((retry > 0)) && retry_at=$(($(now) + retry))
  reason="$(printf '%s' "$reason" | tr -c '[:print:]' ' ' | tr '\t' ' ' | cut -c1-200)"
  mkdir -p "$RUN_DIRECTORY"
  tmp="$(mktemp "${RUN_DIRECTORY}/.follow-hold.XXXXXX")"
  if printf '%s\t%s\t%s\t%s\n' "$digest" "$(now)" "$retry_at" "$reason" >"$tmp" && mv -f "$tmp" "$HOLD_FILE"; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# 일시 장애 횟수는 hold 파일(한 줄 형식)과 따로 "<digest>\t<횟수>" 로 둔다. 다른 digest 가 오면 0 부터 다시 센다.
transient_attempts() { # digest: 이 digest 의 일시 장애 횟수(없으면 0)
  local digest="$1" t_digest t_count
  [[ -r "$TRANSIENT_FILE" ]] || { printf '0\n'; return 0; }
  IFS=$'\t' read -r t_digest t_count <"$TRANSIENT_FILE" || true
  if [[ "$t_digest" == "$digest" && "$t_count" =~ ^[0-9]+$ ]]; then printf '%s\n' "$t_count"; else printf '0\n'; fi
}

# 일시 장애: retry-after 뒤 다시 시도하되 digest 마다 MAX_TRANSIENT_RETRIES 번까지, 그 뒤에는 영구 hold.
hold_transient() { # digest reason → 0 = 다시 시도 예정, 1 = 횟수를 다 써서 영구 hold
  local digest="$1" reason="$2" attempts
  attempts=$(($(transient_attempts "$digest") + 1))
  mkdir -p "$RUN_DIRECTORY" && printf '%s\t%s\n' "$digest" "$attempts" >"$TRANSIENT_FILE" 2>/dev/null || true
  if ((attempts > MAX_TRANSIENT_RETRIES)); then
    hold_write "$digest" 0 "${reason}; retries exhausted (${MAX_TRANSIENT_RETRIES})" || true
    return 1
  fi
  hold_write "$digest" "$TRANSIENT_RETRY_SECONDS" "$reason" || true
  return 0
}

hold_active() { # digest: 이 digest 가 hold 이고 아직 재시도 시각 전이면 0
  local digest="$1" h_digest h_retry
  [[ -r "$HOLD_FILE" ]] || return 1
  IFS=$'\t' read -r h_digest _ h_retry _ <"$HOLD_FILE" || true
  [[ "$h_digest" == "$digest" ]] || return 1
  [[ "$h_retry" =~ ^[0-9]+$ ]] || return 0   # 읽을 수 없는 hold 는 풀리지 않는다
  ((h_retry == 0)) && return 0
  (($(now) < h_retry))
}

# --- 결과 보고: 로그 + 상태 파일 + (있으면) panewire. 보고 실패는 배포에 영향이 없다 ---------------------------------------

report() { # result digest sha detail
  local result="$1" digest="$2" sha="$3" detail="$4" tmp
  log "$result digest=${digest##*@} sha=${sha:0:7} ${detail}"
  mkdir -p "$RUN_DIRECTORY" 2>/dev/null || return 0
  tmp="$(mktemp "${RUN_DIRECTORY}/.follow-status.XXXXXX" 2>/dev/null)" || return 0
  if "$PYTHON" - "$result" "$digest" "$sha" "$detail" >"$tmp" 2>/dev/null <<'PY'
import datetime, json, sys
result, digest, sha, detail = sys.argv[1:5]
print(json.dumps({
    "checked_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "result": result, "digest": digest, "sha": sha, "detail": detail[:400],
}, ensure_ascii=False))
PY
  then mv -f "$tmp" "$STATUS_FILE" 2>/dev/null || rm -f "$tmp"; else rm -f "$tmp"; fi
  if [[ -n "${AT_FOLLOW_PANEWIRE_LANE:-}" ]] && command -v panewire >/dev/null 2>&1; then
    local -a wrap=()
    command -v timeout >/dev/null 2>&1 && wrap=(timeout 20)
    "${wrap[@]+"${wrap[@]}"}" panewire emit --kind lane.event --lane "$AT_FOLLOW_PANEWIRE_LANE" --type info \
      --text "[at-follow] ${result} ${sha:0:7} ${detail}" >/dev/null 2>&1 || true
  fi
  return 0
}

read_marker() { [[ -r "$1" ]] && head -n 1 "$1" || true; }

resolve_tag() { # tag → "<pinned>\t<sha>" (registry 만 읽는다)
  gate --image "${IMAGE_REPOSITORY}:$1" --resolve-only
}

# 게이트가 남긴 거부 사유(한 줄 요약). 게이트가 비정상 종료해 사유 줄이 없어도 실패하지 않는다.
gate_detail() {
  local text
  text="$( { grep -E 'deploy gate:' "$1" | grep -vE 'Test run [0-9]+|REFUSED \(exit' || true; } | sed 's/^deploy gate: //' | tr '\n' ';' | cut -c1-300)"
  [[ -n "$text" ]] || text="$(tail -n 1 "$1" 2>/dev/null | cut -c1-200)"
  printf '%s' "$text"
}

# 게이트 자신의 문구만 일시 장애다(영구 거부 문구에 "registry" 같은 단어가 섞여도 영구 hold 로 간다).
transient_failure() { grep -Eq 'unverified \(|evidence unavailable' "$1"; }
# deploy.sh 의 게이트가 GitHub 를 못 읽은 경우만(deploy.sh 가 78 로 끝났을 때의 출력)
deploy_gate_unavailable() { grep -Eq 'GitHub evidence unavailable|unverified \(' "$1"; }

case "$MODE" in
  status)
    [[ -r "$STATUS_FILE" ]] && cat "$STATUS_FILE" || printf 'no status yet\n'
    [[ -r "$HOLD_FILE" ]] && { printf 'hold: '; cat "$HOLD_FILE"; } || printf 'no hold\n'
    exit 0 ;;
  clear)
    rm -f "$HOLD_FILE" "$TRANSIENT_FILE" && printf 'hold cleared\n' >&2
    exit 0 ;;
esac

GATE="$(gate_script)" || { log "deploy gate is unavailable: install scripts/deploy_ci_gate.py beside this script"; exit 78; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 \
  || { log "Python >= 3.11 is required (set AT_DEPLOY_GATE_PYTHON)"; exit 78; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1. 레지스트리의 deploy 태그. 읽지 못하면 이 틱은 아무것도 하지 않는다(GitHub 호출 없음).
if ! resolved="$(resolve_tag "$DEPLOY_TAG" 2>"$WORK/resolve.err")"; then
  log "cannot resolve the ${DEPLOY_TAG} tag: $(tail -n 1 "$WORK/resolve.err")"
  exit 1
fi
PINNED="${resolved%%$'\t'*}"
SHA="${resolved#*$'\t'}"
[[ "$PINNED" =~ ^${IMAGE_REPOSITORY}@sha256:[0-9a-f]{64}$ && "$SHA" =~ ^[0-9a-f]{40}$ ]] \
  || { log "the ${DEPLOY_TAG} tag did not resolve to a digest and commit"; exit 1; }

if [[ "$MODE" == hold ]]; then
  hold_write "$PINNED" 0 "$HOLD_REASON" || { log "cannot write the hold"; exit 1; }
  report held "$PINNED" "$SHA" "manual hold: ${HOLD_REASON}"
  exit 0
fi

# 2·3. 같은 digest 는 조용한 no-op, hold 된 digest 도 조용히 건너뛴다.
[[ "$(read_marker "$DEPLOYED_DIGEST_FILE")" != "$PINNED" ]] || exit 0
if hold_active "$PINNED"; then exit 0; fi

# 4. 호스트 배포 잠금. 잡으면 deploy.sh 는 AT_DEPLOY_LOCK_HELD 로 이어받는다.
# shellcheck source=host-lock.sh
source "${SCRIPT_DIRECTORY}/host-lock.sh"
rc=0
acquire_host_lock 2>"$WORK/lock.err" || rc=$?
if ((rc != 0)); then
  log "host deploy lock busy or unavailable (exit ${rc}); next tick: $(tail -n 1 "$WORK/lock.err")"
  exit "$rc"
fi
trap 'rm -rf "$WORK"; release_host_lock' EXIT
# 잠금을 기다리는 사이에 수동 배포가 같은 digest 를 끝냈을 수 있다.
[[ "$(read_marker "$DEPLOYED_DIGEST_FILE")" != "$PINNED" ]] || exit 0

log "new ${DEPLOY_TAG} digest ${PINNED##*@} (commit ${SHA:0:7}); verifying"

# 5. 독립 재확인: main 의 조상 + 전체 스위트 초록(인증 없는 GitHub 읽기). deploy 태그를 옮긴 쪽을 믿지 않는다.
export AT_DEPLOY_GATE_GITHUB=anonymous
verified=""
if ! verified="$(gate --image "${IMAGE_REPOSITORY}:${DEPLOY_TAG}" --expect-sha "$SHA" --main-ancestor 2>"$WORK/gate.err")"; then
  detail="$(gate_detail "$WORK/gate.err")"
  if transient_failure "$WORK/gate.err"; then
    if hold_transient "$PINNED" "evidence unavailable: ${detail}"; then
      report refused "$PINNED" "$SHA" "evidence unavailable, retry in ${TRANSIENT_RETRY_SECONDS}s: ${detail}"
    else
      report refused "$PINNED" "$SHA" "evidence unavailable ${MAX_TRANSIENT_RETRIES} retries in a row, held until --clear-hold: ${detail}"
    fi
  else
    hold_write "$PINNED" 0 "gate refused: ${detail}" || true
    report refused "$PINNED" "$SHA" "gate refused: ${detail}"
  fi
  exit 78
fi
if [[ "$verified" != "$PINNED" ]]; then
  hold_write "$PINNED" 0 "the ${DEPLOY_TAG} tag moved during verification" || true
  report refused "$PINNED" "$SHA" "the ${DEPLOY_TAG} tag moved during verification (${verified##*@})"
  exit 78
fi

# 6. 설치기(deploy.sh)는 sha-<7> 태그로 일하므로 그 태그가 검증한 digest 와 같은지 본다.
SHA_TAG="sha-${SHA:0:7}"
if ! sha_resolved="$(resolve_tag "$SHA_TAG" 2>"$WORK/sha.err")" || [[ "${sha_resolved%%$'\t'*}" != "$PINNED" ]]; then
  hold_write "$PINNED" 0 "${SHA_TAG} does not resolve to the verified digest" || true
  report refused "$PINNED" "$SHA" "${SHA_TAG} does not resolve to the verified digest"
  exit 78
fi

# 7. 설치. --allow-unverified 는 이 경로에 없다. DB 백업은 여기서 돌리지 않는다(Dagu 일일 잡이 맡는다).
log "installing ${SHA_TAG}"

rc=0
AT_FOLLOW_VERIFIED_DIGEST="$PINNED" "$DEPLOY_SH" "$SHA_TAG" >"$WORK/deploy.out" 2>&1 || rc=$?
{ printf -- '--- deploy.sh %s (exit %s) ---\n' "$SHA_TAG" "$rc"; cat "$WORK/deploy.out"; } >>"$LOG_FILE" 2>/dev/null || true
tail -n 15 "$WORK/deploy.out" >&2 || true
if ((rc == 75)); then
  log "deploy.sh found the host lock busy; next tick"
  exit 75
fi
if ((rc == 78)) && deploy_gate_unavailable "$WORK/deploy.out"; then
  # deploy.sh's own gate could not read GitHub (an older deploy.sh without the verified-digest handoff, or a
  # failure of the handoff's registry read): nothing was touched and the digest was verified above, so wait
  # and retry — at most MAX_TRANSIENT_RETRIES times per digest, then a permanent hold.
  if hold_transient "$PINNED" "deploy.sh gate evidence unavailable"; then
    report refused "$PINNED" "$SHA" "deploy.sh gate evidence unavailable, retry in ${TRANSIENT_RETRY_SECONDS}s"
  else
    report refused "$PINNED" "$SHA" "deploy.sh gate evidence unavailable ${MAX_TRANSIENT_RETRIES} retries in a row, held until --clear-hold"
  fi
  exit 78
fi
if ((rc != 0)); then
  hold_write "$PINNED" 0 "deploy.sh exit ${rc}" || true
  report failed "$PINNED" "$SHA" "deploy.sh exited ${rc}; see ${LOG_FILE} (rerun deploy.sh or follow.sh --clear-hold after checking)"
  exit "$rc"
fi
if [[ "$(read_marker "$DEPLOYED_DIGEST_FILE")" != "$PINNED" ]]; then
  hold_write "$PINNED" 0 "deployed-digest does not match after deploy.sh" || true
  report failed "$PINNED" "$SHA" "deploy.sh exited 0 but deployed-digest is not ${PINNED##*@}"
  exit 1
fi
rm -f "$HOLD_FILE" "$TRANSIENT_FILE"
report deployed "$PINNED" "$SHA" "installed ${SHA_TAG}"
