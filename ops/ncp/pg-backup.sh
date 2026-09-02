#!/usr/bin/env bash
# Create PostgreSQL logical backups on NCP and mirror them off-host.
#
# Credentials are supplied by the systemd EnvironmentFile; this script never
# writes them to an archive, command line, or report.

set -Euo pipefail

readonly DEFAULT_DATABASES="auto_trader handoffkeep"
readonly DEFAULT_LOCAL_RETENTION_DAYS=7
readonly DEFAULT_REMOTE_RETENTION_DAYS=30
readonly DEFAULT_IMAGE="postgres:17"

report() {
  printf '%s\n' "$*" >&2
}

die() {
  report "pg-backup: $*"
  exit 1
}

require_env() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "missing required environment variable: ${name}"
}

require_nonnegative_integer() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die "${name} must be a non-negative integer"
}

shell_quote() {
  local value="$1"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || return 1
  printf "'%s'" "${value//\'/\'\"\'\"\'}"
}

validate_remote_target() {
  local target="$1"
  local host_var="$2"
  local path_var="$3"
  local host path

  [[ "$target" =~ ^([^:/[:space:]]+):(/[^[:space:]]*/?)$ ]] || return 1
  host="${BASH_REMATCH[1]}"
  path="${BASH_REMATCH[2]}"
  [[ "$host" =~ ^[A-Za-z0-9_.@-]+$ ]] || return 1
  printf -v "$host_var" '%s' "$host"
  printf -v "$path_var" '%s' "$path"
}

client_mode=""
declare -a client_prefix=()

select_client() {
  # PG_BACKUP_CLIENT=auto|host|docker. "docker" pins the containerised client
  # even when a host pg_dump exists (version pinning; also used by tests).
  local client_pref="${PG_BACKUP_CLIENT:-auto}"
  case "$client_pref" in auto|host|docker) ;; *) die "PG_BACKUP_CLIENT must be auto, host or docker" ;; esac
  if [[ "$client_pref" != docker ]] && command -v pg_dump >/dev/null 2>&1 && command -v pg_dumpall >/dev/null 2>&1; then
    client_mode="host-pg_dump"
    client_prefix=()
    return
  fi

  [[ "$client_pref" != host ]] || die "PG_BACKUP_CLIENT=host but pg_dump/pg_dumpall are unavailable"
  command -v docker >/dev/null 2>&1 || die "pg_dump/pg_dumpall unavailable and docker fallback is unavailable"
  client_mode="docker-${PG_BACKUP_IMAGE}"
  client_prefix=(docker run --rm --network host)
  local env_name
  for env_name in PGHOST PGPORT PGUSER PGPASSWORD PGPASSFILE PGSSLMODE PGCONNECT_TIMEOUT; do
    if [[ -n "${!env_name:-}" ]]; then
      client_prefix+=(-e "$env_name")
    fi
  done
  if [[ -n "${PGPASSFILE:-}" ]]; then
    client_prefix+=(-v "${PGPASSFILE}:${PGPASSFILE}:ro")
  fi
  # pg_dump writes --file inside the container namespace: the backup directory
  # must be bind-mounted at the same path or the dump fails with "could not
  # open output file" (first NCP run, 2026-09-03).
  client_prefix+=(-v "${PG_BACKUP_DIRECTORY}:${PG_BACKUP_DIRECTORY}")
  client_prefix+=("$PG_BACKUP_IMAGE")
}

run_pg_dump() {
  local database="$1"
  local output="$2"
  if [[ "$client_mode" == host-pg_dump ]]; then
    pg_dump -Fc --no-owner --no-privileges --file "$output" --dbname "$database"
  else
    "${client_prefix[@]}" pg_dump -Fc --no-owner --no-privileges --file "$output" --dbname "$database"
  fi
}

run_pg_dumpall() {
  if [[ "$client_mode" == host-pg_dump ]]; then
    pg_dumpall --globals-only --no-role-passwords
  else
    "${client_prefix[@]}" pg_dumpall --globals-only --no-role-passwords
  fi
}

is_missing_database_error() {
  local error_file="$1"
  grep -Eqi 'database .+ does not exist' "$error_file"
}

cleanup_local() {
  find "$PG_BACKUP_DIRECTORY" -mindepth 1 -maxdepth 1 -type f -mtime "+${PG_BACKUP_RETENTION_DAYS_LOCAL}" -delete
}

sync_remote() {
  local label="$1"
  local target="$2"
  local remote_host remote_path remote_path_q ssh_command

  if ! validate_remote_target "$target" remote_host remote_path; then
    report "pg-backup: remote=${label} invalid PG_BACKUP_REMOTE target"
    return 1
  fi
  remote_path_q="$(shell_quote "$remote_path")" || {
    report "pg-backup: remote=${label} path contains an unsupported newline"
    return 1
  }

  local -a ssh_args=(ssh -i "$PG_BACKUP_SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes)
  printf -v ssh_command '%q ' "${ssh_args[@]}"

  "${ssh_args[@]}" "$remote_host" "mkdir -p -- ${remote_path_q}" || {
    report "pg-backup: remote=${label} mkdir failed"
    return 1
  }
  # Do not use rsync --delete here: the remote retention window is longer
  # than NCP's local window and is enforced independently below.
  rsync -a -e "$ssh_command" "${PG_BACKUP_DIRECTORY}/" "${target%/}/" || {
    report "pg-backup: remote=${label} rsync failed"
    return 1
  }
  "${ssh_args[@]}" "$remote_host" "find ${remote_path_q} -mindepth 1 -maxdepth 1 -type f -mtime +${PG_BACKUP_RETENTION_DAYS_REMOTE} -delete" || {
    report "pg-backup: remote=${label} retention cleanup failed"
    return 1
  }
  report "pg-backup: remote=${label} mirrored"
}

main() {
  require_env PGHOST
  require_env PGPORT
  require_env PGUSER
  require_env PG_BACKUP_REMOTE
  require_env PG_BACKUP_SSH_KEY

  PG_BACKUP_DATABASES="${PG_BACKUP_DATABASES:-$DEFAULT_DATABASES}"
  PG_BACKUP_DIRECTORY="${PG_BACKUP_DIRECTORY:-/var/backups/ncp-pg}"
  PG_BACKUP_RETENTION_DAYS_LOCAL="${PG_BACKUP_RETENTION_DAYS_LOCAL:-$DEFAULT_LOCAL_RETENTION_DAYS}"
  PG_BACKUP_RETENTION_DAYS_REMOTE="${PG_BACKUP_RETENTION_DAYS_REMOTE:-$DEFAULT_REMOTE_RETENTION_DAYS}"
  PG_BACKUP_IMAGE="${PG_BACKUP_IMAGE:-$DEFAULT_IMAGE}"
  require_nonnegative_integer PG_BACKUP_RETENTION_DAYS_LOCAL "$PG_BACKUP_RETENTION_DAYS_LOCAL"
  require_nonnegative_integer PG_BACKUP_RETENTION_DAYS_REMOTE "$PG_BACKUP_RETENTION_DAYS_REMOTE"
  [[ -r "$PG_BACKUP_SSH_KEY" ]] || die "PG_BACKUP_SSH_KEY is not readable"
  [[ "$PG_BACKUP_DIRECTORY" == /* && "$PG_BACKUP_DIRECTORY" != / ]] || die "PG_BACKUP_DIRECTORY must be a non-root absolute path"

  [[ -n "${PGPASSWORD:-}" || -n "${PGPASSFILE:-}" ]] || die "missing PostgreSQL authentication environment variable: PGPASSWORD or PGPASSFILE"
  [[ -z "${PGPASSFILE:-}" || -r "$PGPASSFILE" ]] || die "PGPASSFILE is not readable"
  install -d -m 0700 "$PG_BACKUP_DIRECTORY" || die "cannot create PG_BACKUP_DIRECTORY"
  select_client
  report "pg-backup: client=${client_mode}"

  local stamp database partial output error_file globals_partial globals checksum_partial checksum
  stamp="$(date +%Y%m%d-%H%M)"
  declare -a completed=()
  for database in $PG_BACKUP_DATABASES; do
    [[ "$database" =~ ^[A-Za-z0-9_][A-Za-z0-9_-]*$ ]] || die "invalid database name: ${database}"
    output="${PG_BACKUP_DIRECTORY}/${database}-${stamp}.dump"
    partial="${output}.partial"
    error_file="${output}.error"
    if ! run_pg_dump "$database" "$partial" 2>"$error_file"; then
      if is_missing_database_error "$error_file"; then
        report "pg-backup: database=${database} missing; skipped"
        rm -f "$partial" "$error_file"
        continue
      fi
      cat "$error_file" >&2
      rm -f "$partial" "$error_file"
      die "database=${database} dump failed"
    fi
    rm -f "$error_file"
    mv -f "$partial" "$output" || die "database=${database} cannot finalize dump"
    completed+=("$output")
    report "pg-backup: database=${database} dumped"
  done

  globals="${PG_BACKUP_DIRECTORY}/globals-${stamp}.sql"
  globals_partial="${globals}.partial"
  if ! run_pg_dumpall >"$globals_partial"; then
    rm -f "$globals_partial"
    die "globals dump failed"
  fi
  mv -f "$globals_partial" "$globals" || die "cannot finalize globals dump"
  completed+=("$globals")

  checksum="${PG_BACKUP_DIRECTORY}/backup-${stamp}.sha256"
  checksum_partial="${checksum}.partial"
  sha256sum "${completed[@]}" >"$checksum_partial" || die "checksum creation failed"
  mv -f "$checksum_partial" "$checksum" || die "cannot finalize checksum"
  cleanup_local || die "local retention cleanup failed"

  local remote_failed=0
  sync_remote primary "$PG_BACKUP_REMOTE" || remote_failed=1
  if [[ -n "${PG_BACKUP_REMOTE_SECONDARY:-}" ]]; then
    sync_remote secondary "$PG_BACKUP_REMOTE_SECONDARY" || remote_failed=1
  else
    report "pg-backup: remote=secondary skipped"
  fi
  if ((remote_failed)); then
    report "pg-backup: status=remote_failed client=${client_mode}"
    return 3
  fi
  report "pg-backup: status=success client=${client_mode} databases=${#completed[@]}"
}

main "$@"
