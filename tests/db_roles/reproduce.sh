#!/usr/bin/env bash
# Disposable PostgreSQL 17 / TimescaleDB 2.22.1 role-cutover fixture.
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
container="at789-db-roles-$$"
fixture_dir="$(mktemp -d)"
cleanup() {
  local rc=$?
  if [[ "${AT789_KEEP_FIXTURE_ON_FAILURE:-0}" == 1 && "$rc" != 0 ]]; then
    echo "fixture retained for local diagnosis: container=$container directory=$fixture_dir" >&2
    return
  fi
  docker stop "$container" >/dev/null 2>&1 || true
  rm -rf "$fixture_dir"
}
trap cleanup EXIT

docker run -d --rm --name "$container" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_USER=mgh3326 \
  -e POSTGRES_DB=auto_trader \
  -p 127.0.0.1::5432 \
  timescale/timescaledb:2.22.1-pg17 >/dev/null

for attempt in {1..60}; do
  # The image first starts a temporary socket-only postmaster for init scripts,
  # then shuts it down. TCP readiness identifies the final server instead.
  if docker exec "$container" pg_isready -h 127.0.0.1 -U mgh3326 \
      -d auto_trader >/dev/null 2>&1; then
    break
  fi
  if ((attempt == 60)); then
    echo 'fixture database did not become ready' >&2
    exit 1
  fi
  sleep 1
done

port_line="$(docker port "$container" 5432/tcp)"
port="${port_line##*:}"
if [[ ! "$port" =~ ^[0-9]+$ ]]; then
  echo 'fixture port invalid' >&2
  exit 1
fi
fixture_dsn="postgresql+asyncpg://mgh3326@127.0.0.1:${port}/auto_trader"

# Settings validation runs during Alembic import. These literal fixture values
# are never used to contact an external service.
export ENV_FILE=/dev/null DATABASE_URL="$fixture_dsn"
export DB_ROLES_DSN="postgresql://mgh3326@127.0.0.1:${port}/auto_trader"
export KIS_APP_KEY=t789_fixture KIS_APP_SECRET=t789_fixture
export OPENDART_API_KEY=t789_fixture
export UPBIT_ACCESS_KEY=t789_fixture UPBIT_SECRET_KEY=t789_fixture
export SECRET_KEY=T789FixtureOnlyStrongSecretKeyA9cE4fGh2Ij

# This initialization gives mgh3326 OID 10 and makes it the extension owner
# and TimescaleDB background scheduler identity, as in the operator preflight.
docker exec "$container" psql -X -U mgh3326 -d auto_trader -v ON_ERROR_STOP=1 \
  -c 'CREATE ROLE postgres LOGIN SUPERUSER' >/dev/null
docker exec "$container" psql -X -U mgh3326 -d auto_trader -v ON_ERROR_STOP=1 \
  -c 'CREATE DATABASE handoffkeep OWNER postgres' \
  -c 'CREATE DATABASE prefect OWNER postgres' >/dev/null

# The owner is LOGIN only so TimescaleDB can start its jobs. Reject all
# interactive connection routes before Stage 1 creates that role.
hba_file="$(docker exec "$container" psql -X -U mgh3326 -d auto_trader \
  -At -c 'SHOW hba_file')"
docker exec "$container" cat "$hba_file" > "$fixture_dir/pg_hba.original"
cat docs/runbooks/db-role-cutover-pg_hba.reject \
  "$fixture_dir/pg_hba.original" > "$fixture_dir/pg_hba.conf"
docker exec -i "$container" sh -c 'cat > "$1"' sh "$hba_file" \
  < "$fixture_dir/pg_hba.conf"
docker exec "$container" psql -X -U mgh3326 -d auto_trader \
  -v ON_ERROR_STOP=1 -At -c 'SELECT pg_reload_conf()' >/dev/null

uv run alembic upgrade head
docker exec -i "$container" psql -X -U postgres -d auto_trader \
  -v ON_ERROR_STOP=1 < tests/db_roles/fixture.sql

echo "fixture ready: container=$container port=$port"

stage1_journal="$fixture_dir/stage1.json"
uv run python scripts/db_roles/stage1_apply.py --database auto_trader \
  --journal "$stage1_journal"
for address in socket tcp; do
  if [[ "$address" == tcp ]]; then
    connection_args=(-h 127.0.0.1)
  else
    connection_args=()
  fi
  if docker exec "$container" psql -X -w "${connection_args[@]}" \
      -U at_migration_owner -d auto_trader -c 'SELECT 1' \
      > "$fixture_dir/$address.out" 2>&1; then
    echo "at_migration_owner $address login was not rejected" >&2
    exit 1
  fi
  if ! rg -q 'pg_hba.conf rejects connection' "$fixture_dir/$address.out"; then
    echo "at_migration_owner $address failed for a reason other than pg_hba" >&2
    exit 1
  fi
done
docker exec -i "$container" psql -X -U mgh3326 -d auto_trader \
  -v ON_ERROR_STOP=1 < scripts/db_roles/stage1_verify.sql
uv run python scripts/db_roles/stage1_rollback.py --database auto_trader \
  --journal "$stage1_journal"
role_count="$(docker exec "$container" psql -X -U mgh3326 -d auto_trader \
  -At -c "SELECT count(*) FROM pg_roles WHERE rolname IN ('at_app','at_migration_owner','nhplug_security_owner')")"
if [[ "$role_count" != 0 ]]; then
  echo 'stage 1 rollback left newly created group roles' >&2
  exit 1
fi
rm "$stage1_journal"
uv run python scripts/db_roles/stage1_apply.py --database auto_trader \
  --journal "$stage1_journal"
echo 'stage 1 forward, rollback, forward: verified'

docker exec "$container" psql -X -U mgh3326 -d auto_trader \
  -v ON_ERROR_STOP=1 \
  -c 'CREATE ROLE at_api_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT' \
  -c 'GRANT at_app TO at_api_login WITH ADMIN FALSE, INHERIT TRUE, SET FALSE' \
  -c 'CREATE ROLE at_scheduler_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT' \
  -c 'GRANT at_app TO at_scheduler_login WITH ADMIN FALSE, INHERIT TRUE, SET FALSE' \
  -c 'CREATE ROLE at_migration_runner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT' \
  -c 'GRANT at_migration_owner TO at_migration_runner WITH ADMIN FALSE, INHERIT FALSE, SET TRUE' >/dev/null
runner_identity="$(docker exec "$container" psql -X -h 127.0.0.1 -U at_migration_runner \
  -d auto_trader -v ON_ERROR_STOP=1 -At \
  -c 'SET ROLE at_migration_owner' \
  -c 'SELECT session_user, current_user')"
if [[ "$runner_identity" != *'at_migration_runner|at_migration_owner'* ]]; then
  echo 'migration runner could not assume the job-owning role' >&2
  exit 1
fi
echo 'owner direct login rejected; migration SET ROLE verified'

make_approval() {
  local stage="$1" path="$2"
  uv run python -m tests.db_roles.fixture_approvals \
    --stage "$stage" --output "$path"
}

apply_stage() {
  local stage="$1" manifest="$2" sha="$3" journal="$4"
  uv run python "scripts/db_roles/stage${stage}_apply.py" \
    --database auto_trader --manifest "$manifest" --sha256 "$sha" \
    --journal "$journal"
  docker exec -i "$container" psql -X -U mgh3326 -d auto_trader \
    -v ON_ERROR_STOP=1 < "scripts/db_roles/stage${stage}_verify.sql"
}

rollback_stage() {
  local stage="$1" journal="$2"
  uv run python "scripts/db_roles/stage${stage}_rollback.py" \
    --database auto_trader --journal "$journal"
  docker exec -i "$container" psql -X -U mgh3326 -d auto_trader \
    -v ON_ERROR_STOP=1 < "scripts/db_roles/stage${stage}_verify.sql"
}

run_fixture_jobs() {
  local job_id job_count aggregate_rows tick_rows
  job_count="$(docker exec "$container" psql -X -U mgh3326 -d auto_trader \
    -At -c "SELECT count(*) FROM timescaledb_information.jobs WHERE hypertable_name IN ('t789_ticks','t789_ticks_hour') AND owner::text='at_migration_owner'")"
  if [[ "$job_count" != 2 ]]; then
    echo 'fixture policy jobs do not both belong to the new owner' >&2
    exit 1
  fi
  while IFS= read -r job_id; do
    if [[ ! "$job_id" =~ ^[0-9]+$ ]]; then
      echo 'fixture policy job ID invalid' >&2
      exit 1
    fi
    docker exec "$container" psql -X -U at_migration_runner -d auto_trader \
      -v ON_ERROR_STOP=1 -c 'SET ROLE at_migration_owner' \
      -c "CALL run_job($job_id)" >/dev/null
  done < <(docker exec "$container" psql -X -U mgh3326 -d auto_trader \
    -At -c "SELECT job_id FROM timescaledb_information.jobs WHERE hypertable_name IN ('t789_ticks','t789_ticks_hour') ORDER BY job_id")
  aggregate_rows="$(docker exec "$container" psql -X -U mgh3326 -d auto_trader \
    -At -c 'SELECT count(*) FROM public.t789_ticks_hour')"
  tick_rows="$(docker exec "$container" psql -X -U mgh3326 -d auto_trader \
    -At -c 'SELECT count(*) FROM public.t789_ticks')"
  if [[ "$aggregate_rows" != 3 || "$tick_rows" != 3 ]]; then
    echo 'policy execution did not preserve ticks and materialize the aggregate' >&2
    exit 1
  fi
}

stage2_manifest="$fixture_dir/stage2.json"
stage2_journal="$fixture_dir/stage2.journal.json"
stage2_sha="$(make_approval 2 "$stage2_manifest")"
apply_stage 2 "$stage2_manifest" "$stage2_sha" "$stage2_journal"
run_fixture_jobs
rollback_stage 2 "$stage2_journal"
old_job_count="$(docker exec "$container" psql -X -U mgh3326 -d auto_trader \
  -At -c "SELECT count(*) FROM timescaledb_information.jobs WHERE hypertable_name IN ('t789_ticks','t789_ticks_hour') AND owner::text='mgh3326'")"
if [[ "$old_job_count" != 2 ]]; then
  echo 'stage 2 rollback did not restore fixture job owner' >&2
  exit 1
fi
mv "$stage2_journal" "$stage2_journal.cycle1"
stage2_sha="$(make_approval 2 "$stage2_manifest")"
apply_stage 2 "$stage2_manifest" "$stage2_sha" "$stage2_journal"
run_fixture_jobs
echo 'stage 2 forward, rollback, forward: verified'

DATABASE_URL="postgresql+asyncpg://at_migration_runner@127.0.0.1:${port}/auto_trader" \
  AT_MIGRATION_SET_ROLE=at_migration_owner uv run alembic current

stage3_manifest="$fixture_dir/stage3.json"
stage3_journal="$fixture_dir/stage3.journal.json"
stage3_sha="$(make_approval 3 "$stage3_manifest")"
apply_stage 3 "$stage3_manifest" "$stage3_sha" "$stage3_journal"
docker exec -i "$container" psql -X -h 127.0.0.1 -U at_migration_runner \
  -d auto_trader -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
SET ROLE at_migration_owner;
CREATE TABLE public.t789_future_acl (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY);
CREATE FUNCTION public.t789_future_acl_function() RETURNS integer LANGUAGE sql AS 'SELECT 1';
DO $$
BEGIN
  IF has_table_privilege('at_app', 'public.t789_future_acl', 'SELECT')
     OR has_sequence_privilege('at_app', 'public.t789_future_acl_id_seq', 'USAGE')
     OR has_function_privilege('PUBLIC', 'public.t789_future_acl_function()', 'EXECUTE') THEN
    RAISE EXCEPTION 'future migration object default ACL leaked privilege';
  END IF;
END
$$;
ROLLBACK;
SQL
DATABASE_URL="postgresql+asyncpg://at_api_login@127.0.0.1:${port}/auto_trader" \
  uv run pytest --noconftest -q tests/db_roles/test_app_role_smoke.py
rollback_stage 3 "$stage3_journal"
mv "$stage3_journal" "$stage3_journal.cycle1"
apply_stage 3 "$stage3_manifest" "$stage3_sha" "$stage3_journal"
echo 'stage 3 forward, rollback, forward: verified'

# Keep two real client backends alive through Stage 5 so its signed session
# evidence is checked against live pg_stat_activity PIDs, not a static claim.
docker exec -d -e PGAPPNAME=t789-fixture-api "$container" psql -X \
  -U at_api_login -d auto_trader -c 'SELECT pg_sleep(600)' >/dev/null
docker exec -d -e PGAPPNAME=t789-fixture-scheduler "$container" psql -X \
  -U at_scheduler_login -d auto_trader -c 'SELECT pg_sleep(600)' >/dev/null
for attempt in {1..20}; do
  live_count="$(docker exec "$container" psql -X -U mgh3326 -d auto_trader -At \
    -c "SELECT count(*) FROM pg_stat_activity WHERE usename IN ('at_api_login','at_scheduler_login') AND application_name IN ('t789-fixture-api','t789-fixture-scheduler')")"
  if [[ "$live_count" == 2 ]]; then
    break
  fi
  if ((attempt == 20)); then
    echo 'fixture application sessions did not become visible' >&2
    exit 1
  fi
  sleep 1
done

for stage in 4 5; do
  manifest="$fixture_dir/stage${stage}.json"
  journal="$fixture_dir/stage${stage}.journal.json"
  sha="$(make_approval "$stage" "$manifest")"
  apply_stage "$stage" "$manifest" "$sha" "$journal"
  rollback_stage "$stage" "$journal"
  mv "$journal" "$journal.cycle1"
  apply_stage "$stage" "$manifest" "$sha" "$journal"
  echo "stage $stage forward, rollback, forward: verified"
done

DATABASE_URL="postgresql+asyncpg://at_api_login@127.0.0.1:${port}/auto_trader" \
  uv run pytest --noconftest -q tests/db_roles/test_app_role_smoke.py
echo 'all five stages and app-role smoke verified in disposable container'
