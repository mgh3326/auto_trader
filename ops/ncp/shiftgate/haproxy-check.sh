#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  printf 'usage: haproxy-check.sh <candidate-config-file>\n' >&2
  exit 64
fi

config=$1

# Retired ports are forbidden anywhere in an active config directive, not only
# as recognized bind/server arguments. Keep the diagnostic tied to the source
# line so an operator can locate the stale route.
if ! awk '
  /^[[:space:]]*#/ { next }
  {
    for (port in retired) {
      if ($0 ~ ("(^|[^0-9])" port "([^0-9]|$)")) {
        printf "HAProxy config uses retired port %s on line %d\n", port, FNR > "/dev/stderr"
        bad = 1
      }
    }
  }
  BEGIN {
    split("8768 8769 8770 8771 8773 8774 8775", ports)
    for (i in ports) retired[ports[i]] = 1
  }
  END { exit bad }
' "$config"; then
  exit 78
fi

# Keep listener and upstream ports tied to the rendered service section. The
# S1 static mirrors are the only backends allowed to target legacy color ports;
# fixed MCP units retain their documented static route ports.
if ! awk '
  function reject(kind, port, scope) {
    printf "HAProxy config %s port %s outside allowed set for %s on line %d\n", kind, port, scope, FNR > "/dev/stderr"
    bad = 1
  }
  /^[[:space:]]*(#|$)/ { next }
  /^[^[:space:]]/ {
    section = $1
    section_name = ($1 == "frontend" || $1 == "backend") ? $2 : ""
    next
  }
  $1 == "bind" {
    port = $2
    sub(/^.*:/, "", port)
    expected = ""
    if (section == "frontend") {
      if (section_name == "api" || section_name == "at-api" || section_name == "at-api-legacy") expected = "8000"
      else if (section_name == "mcp" || section_name == "at-mcp" || section_name == "at-mcp-legacy") expected = "8765"
      else if (section_name == "at-mcp-kiwoom") expected = "8772"
      else if (section_name == "at-mcp-h3-crypto-paper") expected = "8776"
      else if (section_name == "at-mcp-h3-us-paper") expected = "8777"
    }
    if (port != expected) reject("bind", port, "frontend " section_name)
    next
  }
  $1 == "server" {
    port = $3
    sub(/^.*:/, "", port)
    allowed = ""
    if (section == "backend") {
      if (section_name == "api_backend" || section_name == "at-api") allowed = " 18000 28000 "
      else if (section_name == "mcp_backend" || section_name == "at-mcp") allowed = " 18765 28765 "
      else if (section_name == "at-api-legacy") allowed = " 8001 8002 "
      else if (section_name == "at-mcp-legacy") allowed = " 8766 8767 "
      else if (section_name == "at-mcp-kiwoom") allowed = " 8772 "
      else if (section_name == "at-mcp-h3-crypto-paper") allowed = " 8776 "
      else if (section_name == "at-mcp-h3-us-paper") allowed = " 8777 "
    }
    if (allowed !~ (" " port " ")) reject("server", port, "backend " section_name)
  }
  END { exit bad }
' "$config"; then
  exit 78
fi

# Keep the generated HAProxy language and directive arguments closed. The sole
# extra listener-like directive is shiftgate's one Unix admin socket; all TCP
# binds and health-check targets remain constrained to the committed addresses.
if ! awk '
  /^[[:space:]]*(#|$)/ { next }
  /^[^[:space:]]/ {
    if ($1 !~ /^(global|defaults|frontend|backend)$/ ||
        NF != ($1 ~ /^(frontend|backend)$/ ? 2 : 1)) bad = 1
    section = $1
    next
  }
  $1 == "stats" {
    if (section != "global" || NF != 7 || $2 != "socket" ||
        $3 !~ /^\/[A-Za-z0-9._\/-]+$/ || $3 ~ /__LIVE_CHECK_/ ||
        $4 != "mode" || $5 != "600" || $6 != "level" || $7 != "admin") bad = 1
    stats++
    next
  }
  $1 !~ /^(log|master-worker|mode|timeout|bind|default_backend|option|http-check|default-server|server)$/ { bad = 1 }
  $1 == "log" && !((section == "global" && NF == 5 && $2 == "stdout" && $3 == "format" && $4 == "raw" && $5 == "local0") || (section == "defaults" && NF == 2 && $2 == "global") ) { bad = 1 }
  $1 == "master-worker" && NF != 1 { bad = 1 }
  $1 == "mode" && (NF != 2 || $2 != "http") { bad = 1 }
  $1 == "timeout" && (NF != 3 || $2 !~ /^(connect|client|server|tunnel|http-request)$/ || $3 !~ /^[1-9][0-9]*(ms|s|m|h|d)$/) { bad = 1 }
  $1 == "bind" && $0 !~ /^[[:space:]]*bind[[:space:]]+(127\.0\.0\.1|100\.122\.100\.56):[0-9]+[[:space:]]*$/ { bad = 1 }
  $1 == "default_backend" && (NF != 2 || $2 !~ /^[A-Za-z][A-Za-z0-9_.-]*$/) { bad = 1 }
  $1 == "option" && !((section == "defaults" && NF == 2 && $2 == "dontlognull") || (section == "backend" && NF == 4 && $2 == "httpchk" && $3 == "GET" && $4 ~ /^\/[A-Za-z0-9._~!$&\047()*+,;=:@%\/-]*$/)) { bad = 1 }
  $1 == "http-check" && (section != "backend" || NF != 4 || $2 != "expect" || $3 != "status" || $4 != "200") { bad = 1 }
  $1 == "default-server" && (section != "backend" || NF != 7 || $2 != "inter" || $4 != "fall" || $6 != "rise" || $3 !~ /^[1-9][0-9]*(ms|s|m|h|d)$/ || $5 !~ /^[1-9][0-9]*$/ || $7 !~ /^[1-9][0-9]*$/) { bad = 1 }
  $1 == "server" && (section != "backend" || (NF != 4 && NF != 5) || $2 !~ /^[A-Za-z][A-Za-z0-9_.-]*$/ || $3 !~ /^127\.0\.0\.1:[0-9]+$/ || $4 != "check" || (NF == 5 && $5 != "disabled")) { bad = 1 }
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

# Validate with the same pinned image the deploy script runs HAProxy from
# (scripts/deploy-ncp-pull.sh HAPROXY_IMAGE); the NCP host has no haproxy
# binary. No network, config mounted read-only.
readonly HAPROXY_IMAGE="haproxy:3.1-alpine"
if ! command -v docker >/dev/null 2>&1; then
  printf 'docker is required to validate the candidate config\n' >&2
  exit 127
fi

config_dir=$(cd "$(dirname "$config")" && pwd)
config_path="$config_dir/$(basename "$config")"
exec docker run --rm --network none -v "$config_dir:$config_dir:ro" \
  "$HAPROXY_IMAGE" haproxy -c -f "$config_path"
