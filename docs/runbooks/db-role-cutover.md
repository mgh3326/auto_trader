# #789 database role cutover: operator procedure

This procedure is a review artifact. This PR runs no command against production.
The operator-desk must obtain a separate approval for each numbered stage. An
approval covers one signed manifest SHA-256, one database, one maintenance window,
and one rollback journal path. A prior stage approval never authorizes a later
stage. Do not run a stage whose stop condition is unresolved.

The local reproduction command is:

    wrk heavy -- bash tests/db_roles/reproduce.sh

It starts a disposable timescale/timescaledb:2.22.1-pg17 container, applies the
repository migrations, builds the production-like ownership fixture, then runs
all five stages forward, rollback, and forward again. It checks owner socket
and TCP denial, policy job runs, migration identity, and app-role DB paths.
Its synthetic approval records and journals stay in a temporary
directory; the container and directory are removed on exit.

## Known production facts and open gate

The 2026-09-27 read-only preflight is handoffkeep document
report/2026-09-27/760-db-roles-preflight, id 5378. It recorded PostgreSQL
17.9, TimescaleDB 2.26.3, application sessions as postgres superuser, most
application objects and every listed TimescaleDB job owned by mgh3326, some
application objects owned by postgres or nhplug_operator, and a TimescaleDB
background scheduler session as mgh3326. The same catalog identified mgh3326
as the bootstrap superuser, OID 10, and postgres as a distinct superuser,
OID 285718. The only installed systemd timer in
that report is at-pg-backup.timer at 04:10 KST. The same report confirms #711
and the Kiwoom authority evidence objects are present. Refresh these facts at
the maintenance window; the preflight is evidence, not a live lock.

Operator-desk subsequently classified postgres as shared infrastructure.
The root-run backup uses PGUSER=postgres to dump auto_trader and handoffkeep
and run pg_dumpall --globals-only. Prefect uses postgres for its separate
prefect database. The application uses postgres through .env.api and
.env.scheduler; handoffkeep already has its own role. This cutover changes
only the app inputs .env.api and .env.scheduler. No stage alters, disables,
or rotates postgres. Prefect role separation is outside #789.

Stage 2 is stopped until a DBA demonstrates a supported TimescaleDB 2.26.3
policy-job owner transition and inverse. In the disposable 2.22.1 fixture,
changing a hypertable or continuous aggregate owner left its policy job owned
by mgh3326. The alter_job signature there has no owner argument. Operator-desk
should run db-role-cutover-readonly.sql and return the complete output, then
record the reviewed version-specific method and a reversible staging proof.
The same fixture rejected a transactional remove/add of a retention policy
under NOLOGIN at_migration_owner: TimescaleDB reported that a hypertable owner
must have LOGIN to run background tasks. Director-1 chose one common owner
for migrations, hypertables, aggregates, and jobs: at_migration_owner has
LOGIN but no password, with ordered pg_hba reject rules for local socket,
IPv4, and IPv6. It is not an interactive credential. A local unprivileged
LOGIN-role probe created a policy job successfully and rolled back. The
specific 2.26.3 job transition and inverse still need operator approval.
No direct write to the TimescaleDB internal job catalog is authorized by this
runbook. Retain mgh3326 while it owns an extension member, chunk,
materialization object, or job.

## Common preparation and command inputs

Before Stage 1, freeze unrelated DDL and manual database jobs. Record a
restorable backup and its restore-test result, plus the separate backup-client
identity. Record every live API, TaskIQ worker and scheduler, MCP profile,
monitor, host job, fill handoff, CLI, and database session. The tracked
deployment template is not proof that a unit is installed. Confirm all
installed timers again. Do not begin or continue a stage whose lock interval
could overlap the 04:10 KST backup. The backup can hold ACCESS SHARE locks
while owner changes need stronger relation locks.

The following commands assume the operator's approved secret mechanism has
provided DB_ROLES_DSN in the process environment for the DBA script connection,
and libpq PGHOST, PGPORT, PGUSER, and PGPASSFILE for psql. Both identities must
target the recorded auto_trader database. They also assume
CUTOVER_DIR names a mode-0700 directory outside this repository, DB_CONTAINER
is the reviewed production PostgreSQL container name, and that
STAGE2_SHA through STAGE5_SHA are the exact hashes in separate signed stage
approvals. No command here prints a DSN or credential. The operator must paste
the command, exit code, and full verify output back to director-1 after each
stage. Keep journals and signed manifests for rollback; never commit them.

Each apply is a separate change. The script's transaction uses a 3 second
lock timeout and a 30 second statement timeout. Ownership changes are catalog
DDL, with no intentional heap rewrite, but ALTER OWNER can wait for or block
application and TimescaleDB work. Hundreds of relations plus chunks may take
seconds to minutes in an idle staging copy; production duration is unproven.
Measure the exact manifest in staging and leave a window for rollback. A lock
timeout or catalog drift stops the stage; do not raise timeouts ad hoc.

## Stage 1: role creation and owner login rejection

Approval input: the fresh pg_roles and PG17 pg_auth_members output, including
inherit_option and set_option, and the preservation decision for the existing
nhplug_operator role. Stage 1 creates the app and NHPLUG groups as NOLOGIN.
at_migration_owner is LOGIN with a null password because TimescaleDB requires
LOGIN for the background job owner. Before creating it, the operator must
install ordered pg_hba reject rules for this exact role for local socket,
IPv4, and IPv6, reload PostgreSQL, and confirm a login attempt through both
socket and TCP is rejected. The rule file path and installation mechanism
must be part of this stage's separate approval and rollback record. Stage 1
never drops or repurposes nhplug_operator.

The HBA portion must be done first and is part of the Stage 1 approval. The
fragment is docs/runbooks/db-role-cutover-pg_hba.reject. The first matching
rules must be its six reject lines; appending them after a permissive rule
does not work. The exact operator commands, with DB_CONTAINER and CUTOVER_DIR
resolved from the signed approval, are:

    HBA_FILE="$(psql -X -At -d auto_trader -c 'SHOW hba_file')"
    docker exec "$DB_CONTAINER" cat "$HBA_FILE" > "$CUTOVER_DIR/pg_hba.before"
    cat docs/runbooks/db-role-cutover-pg_hba.reject "$CUTOVER_DIR/pg_hba.before" > "$CUTOVER_DIR/pg_hba.candidate"
    sha256sum "$CUTOVER_DIR/pg_hba.before" "$CUTOVER_DIR/pg_hba.candidate"
    docker exec -i "$DB_CONTAINER" sh -c 'cat > "$1"' sh "$HBA_FILE" < "$CUTOVER_DIR/pg_hba.candidate"
    psql -X -v ON_ERROR_STOP=1 -d auto_trader -c 'SELECT pg_reload_conf()'
    psql -X -v ON_ERROR_STOP=1 -d auto_trader -c "SELECT rule_number,type,database,user_name,address,auth_method,error FROM pg_hba_file_rules WHERE 'at_migration_owner'=ANY(user_name) ORDER BY rule_number"

Stop if pg_reload_conf is false, any rule has an error, or the six reject
rules are not first for at_migration_owner. The original file must remain in
CUTOVER_DIR until every later stage is finished or rolled back. The role
apply and verify commands follow:

    uv run python scripts/db_roles/stage1_apply.py --database auto_trader --journal "$CUTOVER_DIR/stage1.journal.json"
    psql -X -v ON_ERROR_STOP=1 -d auto_trader -f scripts/db_roles/stage1_verify.sql

After apply, the operator must run and paste both denied connection results:

    docker exec "$DB_CONTAINER" psql -X -w -U at_migration_owner -d auto_trader -c 'SELECT 1'
    docker exec "$DB_CONTAINER" psql -X -w -h 127.0.0.1 -U at_migration_owner -d auto_trader -c 'SELECT 1'

Both commands must fail specifically with pg_hba.conf rejects connection.
If Stage 1 needs rollback, run its inverse first. Only after it succeeds and
all three newly created roles were removed may the operator restore the
original HBA file and verify reload with:

    uv run python scripts/db_roles/stage1_rollback.py --database auto_trader --journal "$CUTOVER_DIR/stage1.journal.json"
    docker exec -i "$DB_CONTAINER" sh -c 'cat > "$1"' sh "$HBA_FILE" < "$CUTOVER_DIR/pg_hba.before"
    psql -X -v ON_ERROR_STOP=1 -d auto_trader -c 'SELECT pg_reload_conf()'

Paste four role rows. at_migration_owner must be LOGIN with a null password;
the other three must be NOLOGIN. All four must be NOSUPERUSER, NOCREATEDB,
NOCREATEROLE, NOREPLICATION, and NOBYPASSRLS. Paste the HBA rule rows and
rejected socket and TCP login results. Paste all membership rows; no
application login may inherit an owner role. Stop if an existing role has
different attributes or unexpected membership, the HBA rejection fails, or
the owner role has a password. Do not run the rollback after
later stages until their dependencies have been rolled back in reverse order.

## Stage 2: named ownership and TimescaleDB graph

Approval input: a complete signed object manifest covering schemas, relations,
sequences, views, functions, types, hypertables, continuous aggregates,
materialization objects, chunks, and jobs. It must name the current and target
owner for each object and exclude extension members. The record must include
the exact TimescaleDB version and the full policy-job schedule, configuration,
owner, and IDs. Compare the manifest with the fresh
catalog inventory. An absent #711 object is an explicit stop; do not silently
skip it or run its historical migration under the new owner. This stage needs
the supported 2.26.3 job-owner transition proof described above before approval.
The approved transaction for each user policy job removes the policy and
recreates it under at_migration_owner with its recorded configuration.
Job IDs change. The journal must map old and new IDs, and its inverse must
recreate the policy under mgh3326 from the recorded configuration. A custom
or unknown policy type stops the stage. No direct Timescale internal catalog
write or data rewrite is allowed.

    uv run python scripts/db_roles/stage2_apply.py --database auto_trader --manifest "$CUTOVER_DIR/stage2.approved.json" --sha256 "$STAGE2_SHA" --journal "$CUTOVER_DIR/stage2.journal.json"
    psql -X -v ON_ERROR_STOP=1 -d auto_trader -f scripts/db_roles/stage2_verify.sql
    uv run python scripts/db_roles/stage2_rollback.py --database auto_trader --journal "$CUTOVER_DIR/stage2.journal.json"

Paste the complete verify output and a zero-drift comparison against the signed
manifest. The next stage requires every listed application object at its
specified owner role, zero unexpected old-owned chunks and materialization
objects, and all user policy jobs at at_migration_owner. One refresh and one
retention job must run successfully in the disposable staging proof; production
verification observes their scheduled executions and job_stats without a
manual retention run that could drop data. The TimescaleDB background scheduler may still run
under the extension owner; record that session separately and never treat its
PID absence as owner-transition proof. Stop on any unresolved job, extension
member, or version difference.

## Stage 3: object grants and creator default privileges

Approval input: a signed object-level DML and sequence manifest, the exact
#711 and Kiwoom protected matrix, pre-change ACLs, and creator-role default
ACLs. The signed database CONNECT keep-role list must cover postgres for the
backup and Prefect, every currently active non-owner login, the new at_app
group, and the pre-provisioned at_migration_runner. Stage 3 revokes PUBLIC
CONNECT only after granting that reviewed set; rollback restores its exact
prior ACL. Provision the runner role and its membership before Stage 3, with
its credential activation reserved for Stage 4. The app gets no schema
CREATE, table TRUNCATE or blanket all-tables
grant. Its identity sequences get USAGE only. The #711 consume and guard
functions retain their fixed search_path and give no direct EXECUTE to app,
operator, or PUBLIC. Test any helper EXECUTE exception with a staging app
ledger insert before granting it. Future migration objects must be created
after SET ROLE at_migration_owner; a connection as the runner alone is not
enough to apply that creator's default ACL.
The defaults deny PUBLIC function execution and leave table and sequence DML
ungranted. Each later migration must include a reviewed object-level app grant
before its new code uses that object; the default ACL is not a blanket app
grant. The disposable fixture creates a table, identity sequence, and
function as the runner after SET ROLE and checks these denied defaults.

    uv run python scripts/db_roles/stage3_apply.py --database auto_trader --manifest "$CUTOVER_DIR/stage3.approved.json" --sha256 "$STAGE3_SHA" --journal "$CUTOVER_DIR/stage3.journal.json"
    psql -X -v ON_ERROR_STOP=1 -d auto_trader -f scripts/db_roles/stage3_verify.sql
    uv run python scripts/db_roles/stage3_rollback.py --database auto_trader --journal "$CUTOVER_DIR/stage3.journal.json"

Paste every protected-table privilege row, sequence row, function row, and
default-ACL row. Compare normal-domain privileges with the approved manifest;
the verify SQL is evidence, not an automatic approval. Stop if the app has
ownership, direct protected-function EXECUTE, sequence SELECT or UPDATE,
unexpected DELETE, or any missing normal application DML path.

## Stage 4: distinct credentials and process drain

Approval input: a separately provisioned application login per consumer class,
the NOINHERIT at_migration_runner pre-provisioned before Stage 3, a desk-only
NOINHERIT operator login, signed
secret mapping, and a rollback mapping. App logins must inherit only at_app,
with inherit_option true and set_option/admin_option false. The migration
runner must use the separate migration env input and establish current_user
at_migration_owner before Alembic DDL. The compose migration templates select
.env.migration; the host scripts/migrate.sh entrypoint selects the same input
and sets the same role gate. Its prior symbol-sync side effects require a
separate approved application-login invocation after migrations. Application
services continue to select their app input.
The host deploy script uses its app secret input and does not invoke Alembic.
Never copy a migration DSN into an app input or reuse an app DSN for backup.

    uv run python scripts/db_roles/stage4_apply.py --database auto_trader --manifest "$CUTOVER_DIR/stage4.approved.json" --sha256 "$STAGE4_SHA" --journal "$CUTOVER_DIR/stage4.journal.json"
    psql -X -v ON_ERROR_STOP=1 -d auto_trader -f scripts/db_roles/stage4_verify.sql
    PGUSER=at_migration_runner psql -X -qAt -v ON_ERROR_STOP=1 -d auto_trader -c 'SET ROLE at_migration_owner; SELECT session_user,current_user'
    PGUSER=at_api_login psql -X -qAt -v ON_ERROR_STOP=1 -d auto_trader -c 'SELECT session_user,current_user'
    PGUSER=at_scheduler_login psql -X -qAt -v ON_ERROR_STOP=1 -d auto_trader -c 'SELECT session_user,current_user'
    uv run python scripts/db_roles/stage4_rollback.py --database auto_trader --journal "$CUTOVER_DIR/stage4.journal.json"

The apply script is an evidence gate; the operator separately performs each
approved secret input and process rotation. Paste the role-membership rows,
session counts, the identity probe rows, and one recorded success under each
new login. The named probes assume the signed manifest uses at_api_login,
at_scheduler_login, and at_migration_runner; for different approved names, use those exact names in
the commands and paste the corresponding manifest rows. Rotate API,
worker, singleton scheduler, every MCP profile, monitors, host jobs and fill
handoff. Drain old pools explicitly. The script cannot roll back an external
secret mapping: if verification fails, restore the recorded prior mapping and
process image through the separate operator procedure, then re-run the SQL
verification. Do not revoke old access until new consumers pass.

## Stage 5: remove application use of the superuser

Approval input: the old credential absent from every application input,
zero old application sessions, successful new-login observations, preserved
postgres backup authority, TimescaleDB graph and job proof, and timer observations through
at least one firing of every installed host timer. If only the daily backup
timer remains, observe a successful 04:10 KST firing without overlapping a
DDL stage. The production mgh3326 bootstrap role and shared postgres role
must never be altered by this application cutover. Rotate only .env.api and
.env.scheduler to the new app identities, drain those application sessions,
and preserve .env.prefect and .env.pg-backup as infrastructure inputs. Do not
revoke or rotate the postgres credential here.

    uv run python scripts/db_roles/stage5_apply.py --database auto_trader --manifest "$CUTOVER_DIR/stage5.approved.json" --sha256 "$STAGE5_SHA" --journal "$CUTOVER_DIR/stage5.journal.json"
    psql -X -v ON_ERROR_STOP=1 -d auto_trader -f scripts/db_roles/stage5_verify.sql
    uv run python scripts/db_roles/stage5_rollback.py --database auto_trader --journal "$CUTOVER_DIR/stage5.journal.json"

Paste all old-role attributes, session counts by database and application,
timer firing evidence, and read-only backup identity evidence. postgres must
remain LOGIN SUPERUSER and retain CONNECT on auto_trader and handoffkeep for
the existing root-run backup. No dump is run for this check. The stage
completes only when no application process can reconnect as postgres and no
old privileged application session remains. Prefect, backup and DBA sessions
are classified separately; their continued use is expected.
If rollback is needed, restore old grants and owners from exact journals in
reverse stage order before returning a prior app secret mapping or process.
Rollback stops on catalog drift. A data backup is for data recovery and does
not substitute for ACL or ownership rollback.
