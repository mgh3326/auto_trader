#!/bin/bash
# hk 6436 (decision 508(b), hk 6425): list (default) or remove (--apply) auto_trader job containers that are in state
# "created", were never started, and are older than 24 h. Never touches running/exited containers, images or volumes.
set -euo pipefail
apply=0; [ "${1:-}" = "--apply" ] && apply=1
now=$(date +%s); n=0
for id in $(docker ps -aq --filter status=created); do
  read -r name image created started < <(docker inspect --format '{{.Name}} {{.Config.Image}} {{.Created}} {{.State.StartedAt}}' "$id")
  name=${name#/}
  case "$image" in ghcr.io/mgh3326/auto_trader@*|ghcr.io/mgh3326/auto_trader:*) ;; *) continue ;; esac
  case "$name" in shiftgate-*|at-*) continue ;; esac          # named service containers are never job leftovers
  [ "${started:0:4}" = "0001" ] || continue                    # never started
  age=$(( now - $(date -d "$created" +%s) )); [ "$age" -ge 86400 ] || continue
  n=$((n+1)); echo "candidate $id name=$name age_h=$((age/3600)) image=${image%%@*}"
  [ "$apply" -eq 1 ] && docker rm "$id" >/dev/null && echo "removed $id"
done
echo "candidates=$n apply=$apply"
