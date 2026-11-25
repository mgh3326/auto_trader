#!/bin/bash
# hk 6438 (결정 508(b), hk 6425): list (default) or remove (--apply) auto_trader job containers that are in state
# "created", were never started, and are older than 24 h. Never touches running/exited containers, images or volumes.
# 서비스 컨테이너(이름이 아래 목록과 일치)는 제외한다. 잡 컨테이너(at-toss-fill-poll 등)는 created + 미시작 + 24h 초과 규칙으로 정리 대상이다.
set -euo pipefail
apply=0; [ "${1:-}" = "--apply" ] && apply=1
now=$(date +%s); n=0
ids=$(docker ps -aq --filter status=created) || { echo "created-gc: docker ps 조회 실패" >&2; exit 1; }
for id in $ids; do
  read -r name image created started < <(docker inspect --format '{{.Name}} {{.Config.Image}} {{.Created}} {{.State.StartedAt}}' "$id")
  name=${name#/}
  case "$image" in ghcr.io/mgh3326/auto_trader@*|ghcr.io/mgh3326/auto_trader:*) ;; *) continue ;; esac
  case "$name" in shiftgate-*|at-api*|at-mcp*|at-upbit-ws*|at-worker*|at-scheduler*|at-pg|at-redis|at-trend-*|factortrail|broker-edge|gatewayd) continue ;; esac          # 서비스 컨테이너만 제외 — 잡 컨테이너(at-toss-fill-poll 등)는 정리 대상
  [ "${started:0:4}" = "0001" ] || continue                    # never started
  age=$(( now - $(date -d "$created" +%s) )); [ "$age" -ge 86400 ] || continue
  n=$((n+1)); echo "candidate $id name=$name age_h=$((age/3600)) image=${image%%@*}"
  [ "$apply" -eq 1 ] && docker rm "$id" >/dev/null && echo "removed $id"
done
echo "candidates=$n apply=$apply"
