# NCP PostgreSQL daily backup and off-host replication

`at-pg-backup.timer` creates PostgreSQL custom-format dumps on NCP at 04:10
KST, then mirrors them to the MacBook server through tailnet. It is a backup
job, not a restore automation: all restores require the rehearsal procedure
below.

## Environment file

Create `/root/at-secrets/.env.pg-backup` on NCP with mode `0600`. Do not put
this file, a database URL, or any credential in the repository.

| Name | Required | Meaning |
| --- | --- | --- |
| `PGHOST` | yes | PostgreSQL host; NCP uses `127.0.0.1`. |
| `PGPORT` | yes | PostgreSQL port; NCP uses `25432`. |
| `PGUSER` | yes | Backup role name. |
| `PGPASSWORD` or `PGPASSFILE` | deployment-specific | Authentication material; never log its value. |
| `PG_BACKUP_DATABASES` | no | Space-separated database names; default `auto_trader panewire`. Missing databases are warned and skipped. `prefect` is deliberately excluded. |
| `PG_BACKUP_DIRECTORY` | no | NCP archive directory; default `/var/backups/ncp-pg`. |
| `PG_BACKUP_RETENTION_DAYS_LOCAL` | no | NCP retention; default `7`. |
| `PG_BACKUP_REMOTE` | yes | Primary MacBook mirror, e.g. `mgh3326@100.73.173.44:/Users/mgh3326/backups/ncp-pg/`. |
| `PG_BACKUP_REMOTE_SECONDARY` | no | Optional OCI mirror; empty means skipped. |
| `PG_BACKUP_RETENTION_DAYS_REMOTE` | no | Each remote's retention; default `30`. |
| `PG_BACKUP_SSH_KEY` | yes | Dedicated tailnet SSH private-key path, readable only by root. |
| `PG_BACKUP_IMAGE` | no | Docker fallback client image; default `postgres:17`, and must match the server major. |
| `PG_BACKUP_HC_URL` | no | Healthchecks ping URL. Empty skips the `ExecStartPost` ping. |

The job uses host `pg_dump`/`pg_dumpall` when both exist. Otherwise it uses
`docker run --rm --network host "$PG_BACKUP_IMAGE"`; the report identifies
which client path ran. Dumps use `pg_dump -Fc`, while globals use
`pg_dumpall --globals-only --no-role-passwords`. The latter deliberately does
not export role passwords.

## Install and verify

On NCP, after a reviewed pull of the versioned files:

```bash
install -d -m 0700 /var/backups/ncp-pg /root/at-secrets
install -m 0700 ops/ncp/pg-backup.sh /root/at-run/pg-backup.sh
install -m 0644 ops/ncp/systemd/at-pg-backup.{service,timer} /etc/systemd/system/
install -m 0600 /dev/null /root/at-secrets/.env.pg-backup
# edit /root/at-secrets/.env.pg-backup without echoing credentials
systemctl daemon-reload
systemctl enable --now at-pg-backup.timer
systemctl list-timers at-pg-backup.timer
```

Before relying on the timer, run one controlled manual backup and inspect only
safe metadata:

```bash
systemctl start at-pg-backup.service
systemctl status at-pg-backup.service --no-pager
find /var/backups/ncp-pg -maxdepth 1 -type f -printf '%f %s bytes\n' | sort
sha256sum -c /var/backups/ncp-pg/backup-YYYYMMDD-HHMM.sha256
```

The mirror is additive `rsync -a`, not `rsync --delete`: the MacBook's 30-day
window is intentionally longer than NCP's seven-day window. Remote retention
is independently enforced by an SSH `find -mtime` cleanup.
If the MacBook is offline (or either configured remote fails), NCP dumps and
checksums remain intact, the final report says `status=remote_failed`, and the
job exits `3`. Investigate and rerun manually; do not delete a local archive
to make a failed remote look healthy.

## Capacity and retention

Measure present logical database sizes before choosing storage:

```bash
psql -h 127.0.0.1 -p 25432 -U "$PGUSER" -d postgres \
  -c "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database ORDER BY pg_database_size(datname) DESC;"
du -sh /var/backups/ncp-pg
```

Observed planning sizes are `auto_trader` about 961 MB plus a small
`panewire`; `prefect` is about 4 GB and remains excluded by default. A rough
upper bound before custom-format compression is daily logical size × 7 on NCP
and × 30 on the MacBook. If `prefect` is explicitly added, budget its roughly
4 GB/day contribution before enabling it. Recheck actual dump sizes after the
first week rather than treating database size as a compression estimate.

## Restore rehearsal (OCI staging only)

Do not restore into NCP production. Copy a selected dump and its checksum to
an isolated OCI staging host, validate the checksum, restore into an empty
staging database, then compare row counts to a read-only source query.

```bash
# On the staging host, after copying the selected dump and checksum sidecar:
sha256sum -c backup-YYYYMMDD-HHMM.sha256
createdb auto_trader_restore_rehearsal
pg_restore --exit-on-error --clean --if-exists \
  -d auto_trader_restore_rehearsal auto_trader-YYYYMMDD-HHMM.dump

# On NCP source and OCI staging respectively; save both outputs with the drill.
psql -h 127.0.0.1 -p 25432 -U "$PGUSER" -d auto_trader \
  -Atc "SELECT n.nspname||'.'||c.relname||'='||count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_tables t ON t.schemaname=n.nspname AND t.tablename=c.relname WHERE n.nspname NOT IN ('pg_catalog','information_schema') GROUP BY 1 ORDER BY 1;"
psql -d auto_trader_restore_rehearsal \
  -Atc "SELECT n.nspname||'.'||c.relname||'='||count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_tables t ON t.schemaname=n.nspname AND t.tablename=c.relname WHERE n.nspname NOT IN ('pg_catalog','information_schema') GROUP BY 1 ORDER BY 1;"
```

Diff the two saved count files; any mismatch is a failed rehearsal. Record the
dump timestamp, checksum result, restore output, and row-count diff. Destroy
the staging database only after the drill evidence is retained.
