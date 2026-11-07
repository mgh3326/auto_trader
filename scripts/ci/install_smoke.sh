#!/usr/bin/env bash
# Install smoke for the deploy tier (hk 3445).
#
# Boots the built image the way NCP runs it, against CI service containers on the
# runner's loopback, with placeholder settings only (no secrets):
#   1. `alembic upgrade head` from the image applies every migration to an empty DB
#   2. `uvicorn app.main:app` answers /healthz 200 within SMOKE_ATTEMPTS x SMOKE_SLEEP
#   3. the TaskIQ scheduler, TaskIQ worker and websocket_monitor.py entrypoints import
# On any failure the failing container's last 60 log lines are printed.
#
# usage: install_smoke.sh IMAGE      (an image reference, normally name@sha256:...)
# env:   DATABASE_URL REDIS_URL SECRET_KEY KIS_APP_KEY KIS_APP_SECRET
#        OPENDART_API_KEY UPBIT_ACCESS_KEY UPBIT_SECRET_KEY (placeholder values)
#        SMOKE_ATTEMPTS (30)  SMOKE_SLEEP (2)  SMOKE_PORT (8000)
set -euo pipefail

image="${1:?usage: install_smoke.sh IMAGE}"
attempts="${SMOKE_ATTEMPTS:-30}"
pause="${SMOKE_SLEEP:-2}"
port="${SMOKE_PORT:-8000}"

# Names only: docker copies the value from this process, so nothing is echoed.
env_args=()
for name in DATABASE_URL REDIS_URL SECRET_KEY KIS_APP_KEY KIS_APP_SECRET \
            OPENDART_API_KEY UPBIT_ACCESS_KEY UPBIT_SECRET_KEY; do
  : "${!name:?$name must be set to a placeholder value}"
  env_args+=(-e "$name")
done
env_args+=(-e ENVIRONMENT=production)

containers=()
# A self-hosted runner can still hold a container from an aborted earlier run.
docker rm -f smoke-migrate smoke-api smoke-import-scheduler smoke-import-worker \
  smoke-import-websocket >/dev/null 2>&1 || true
cleanup() {
  if [ "${#containers[@]}" -gt 0 ]; then
    docker rm -f "${containers[@]}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

fail() {
  # $1 = container name, $2 = what failed
  echo "::error::install smoke: $2"
  echo "--- last 60 log lines of $1"
  docker logs --tail 60 "$1" 2>&1 || true
  exit 1
}

# One-shot command in the image; its container's logs are the evidence on failure.
run_once() {
  local name="$1" label="$2"
  shift 2
  containers+=("$name")
  echo "::group::$label"
  if ! docker run --name "$name" --network host "${env_args[@]}" "$image" "$@"; then
    echo "::endgroup::"
    fail "$name" "$label failed"
  fi
  echo "::endgroup::"
}

run_once smoke-migrate "alembic upgrade head" alembic upgrade head

containers+=(smoke-api)
docker run -d --name smoke-api --network host "${env_args[@]}" "$image" \
  python -m uvicorn app.main:app --host 127.0.0.1 --port "$port" >/dev/null
healthy=0
for attempt in $(seq 1 "$attempts"); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${port}/healthz"; then
    echo "/healthz answered 200 on attempt $attempt"
    healthy=1
    break
  fi
  echo "waiting for /healthz ($attempt/$attempts)"
  sleep "$pause"
done
[ "$healthy" = 1 ] || fail smoke-api "/healthz did not answer 200 in $attempts attempts"
docker rm -f smoke-api >/dev/null 2>&1 || true

run_once smoke-import-scheduler "scheduler entrypoint imports" \
  python -c "import app.core.scheduler, app.tasks"
run_once smoke-import-worker "TaskIQ worker entrypoint imports" \
  python -c "import app.core.taskiq_broker, app.tasks"
run_once smoke-import-websocket "websocket_monitor.py imports" \
  python -c "import websocket_monitor"

echo "install smoke passed for $image"
