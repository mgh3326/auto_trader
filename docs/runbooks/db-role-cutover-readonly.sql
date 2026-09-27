-- #789 additional operator-desk evidence. Run only against the intended
-- production auto_trader database with a read-only transaction and paste the
-- full output. This file performs no mutation.
BEGIN READ ONLY;

SELECT current_database() AS database_name,
       current_setting('server_version') AS postgres_version,
       current_user,
       session_user;

SHOW hba_file;
SELECT rule_number, type, database, user_name, address, auth_method, error
FROM pg_hba_file_rules
ORDER BY rule_number;

-- Returns only whether a password is absent; never return its value or hash.
SELECT rolname, rolcanlogin, rolpassword IS NULL AS password_null
FROM pg_authid WHERE rolname = 'at_migration_owner';

SELECT rolname, rolsuper, rolcanlogin, rolcreaterole, rolreplication
FROM pg_roles WHERE rolname = 'postgres';
SELECT datname,
       has_database_privilege('postgres', oid, 'CONNECT') AS postgres_connect
FROM pg_database
WHERE datname IN ('auto_trader', 'handoffkeep', 'prefect')
ORDER BY datname;
SELECT datname, usename, application_name, state, count(*) AS sessions
FROM pg_stat_activity
WHERE usename = 'postgres'
GROUP BY datname, usename, application_name, state
ORDER BY datname, application_name, state;

SELECT e.extname, e.extversion, pg_get_userbyid(e.extowner) AS extension_owner
FROM pg_extension e WHERE e.extname = 'timescaledb';

SELECT p.oid::regprocedure AS signature,
       pg_get_function_arguments(p.oid) AS arguments,
       pg_get_userbyid(p.proowner) AS owner
FROM pg_proc p
WHERE p.proname IN ('alter_job', 'run_job')
ORDER BY p.oid::regprocedure::text;

SELECT n.nspname, c.relname, c.relkind,
       pg_get_userbyid(c.relowner) AS owner
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE (n.nspname, c.relname) IN
      (('_timescaledb_config', 'bgw_job'),
       ('_timescaledb_catalog', 'hypertable'),
       ('_timescaledb_catalog', 'continuous_agg'))
ORDER BY n.nspname, c.relname;

SELECT n.nspname, pg_get_userbyid(n.nspowner) AS owner
FROM pg_namespace n
WHERE n.nspname IN ('public','review','research','paper')
ORDER BY n.nspname;

SELECT t.tgname, pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t
WHERE t.tgrelid = to_regclass('_timescaledb_config.bgw_job')
  AND NOT t.tgisinternal
ORDER BY t.tgname;

-- JSON preserves the version-specific job fields and every policy parameter.
-- Review the output for unexpected credential material before wider sharing.
SELECT job_id, to_jsonb(j) AS job_record
FROM timescaledb_information.jobs AS j ORDER BY job_id;

SELECT usename, application_name, state, count(*) AS sessions
FROM pg_stat_activity
WHERE datname = current_database()
  AND application_name LIKE 'TimescaleDB%'
GROUP BY usename, application_name, state
ORDER BY usename, application_name, state;

SELECT h.hypertable_schema, h.hypertable_name, h.owner AS hypertable_owner,
       count(ch.chunk_name) AS chunk_count,
       count(*) FILTER (WHERE ch.chunk_name IS NOT NULL
         AND pg_get_userbyid(c.relowner) IS DISTINCT FROM h.owner) AS differing_chunk_owners
FROM timescaledb_information.hypertables h
LEFT JOIN timescaledb_information.chunks ch
  ON ch.hypertable_schema = h.hypertable_schema
 AND ch.hypertable_name = h.hypertable_name
LEFT JOIN pg_namespace n ON n.nspname = ch.chunk_schema
LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = ch.chunk_name
GROUP BY h.hypertable_schema, h.hypertable_name, h.owner
ORDER BY h.hypertable_schema, h.hypertable_name;

SELECT p.rolname AS parent, m.rolname AS member,
       a.admin_option, a.inherit_option, a.set_option
FROM pg_auth_members a
JOIN pg_roles p ON p.oid = a.roleid
JOIN pg_roles m ON m.oid = a.member
WHERE p.rolname IN ('at_app', 'at_migration_owner',
                    'nhplug_security_owner', 'nhplug_operator')
   OR m.rolname IN ('at_app', 'at_migration_owner',
                    'nhplug_security_owner', 'nhplug_operator')
ORDER BY p.rolname, m.rolname;

ROLLBACK;
