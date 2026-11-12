#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  printf 'usage: haproxy-check.sh <candidate-config-file>\n' >&2
  exit 64
fi

config=$1

# Keep the generated HAProxy language closed. The sole extra listener-like
# directive is shiftgate's one Unix admin socket; all TCP binds and servers
# remain constrained to the committed loopback/tailnet addresses.
if ! awk '
  /^[[:space:]]*(#|$)/ { next }
  /^[^[:space:]]/ {
    if ($1 !~ /^(global|defaults|frontend|backend)$/ ||
        NF != ($1 ~ /^(frontend|backend)$/ ? 2 : 1)) bad = 1
    next
  }
  $1 == "stats" {
    if (NF != 7 || $2 != "socket" || $3 !~ /^\/[A-Za-z0-9._\/-]+$/ ||
        $4 != "mode" || $5 != "600" || $6 != "level" || $7 != "admin") bad = 1
    stats++
    next
  }
  $1 !~ /^(log|master-worker|mode|timeout|bind|default_backend|option|http-check|default-server|server)$/ { bad = 1 }
  $1 == "bind" && $0 !~ /^[[:space:]]*bind[[:space:]]+(127\.0\.0\.1|100\.122\.100\.56):[0-9]+[[:space:]]*$/ { bad = 1 }
  $1 == "server" && $3 !~ /^127\.0\.0\.1:[0-9]+$/ { bad = 1 }
  END { if (stats != 1) bad = 1; exit bad }
' "$config"; then
  printf 'HAProxy config violates the closed private-listener shape\n' >&2
  exit 78
fi

for required_bind in '127.0.0.1:8000' '100.122.100.56:8000'; do
  if ! awk -v required="$required_bind" '$1 == "bind" && $2 == required { found = 1 } END { exit !found }' "$config"; then
    printf 'HAProxy config is missing required bind %s\n' "$required_bind" >&2
    exit 78
  fi
done

if ! command -v haproxy >/dev/null 2>&1; then
  printf 'haproxy is required to validate the candidate config\n' >&2
  exit 127
fi

exec haproxy -c -f "$config"
