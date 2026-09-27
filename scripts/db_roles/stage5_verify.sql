BEGIN READ ONLY;
SELECT oid,rolname,rolcanlogin,rolsuper,rolcreaterole,rolbypassrls,
       oid=10 AS bootstrap_role
FROM pg_roles WHERE rolname='postgres';
WITH expected(datname) AS
 (VALUES ('auto_trader'),('handoffkeep'),('prefect'))
SELECT expected.datname,d.oid IS NOT NULL AS database_exists,
       d.datallowconn,
       CASE WHEN d.oid IS NOT NULL
            THEN has_database_privilege('postgres',d.datname,'CONNECT')
       END AS postgres_connect
FROM expected LEFT JOIN pg_database d ON d.datname=expected.datname
ORDER BY expected.datname;
SELECT pid,datname,usename,application_name,client_addr::text AS client_addr,
       backend_type,state
FROM pg_stat_activity
WHERE usename='postgres' AND datname IS NOT NULL AND pid<>pg_backend_pid()
ORDER BY datname,pid;
SELECT member.rolname AS application_login,member.rolcanlogin,
       member.rolsuper,member.rolcreaterole,member.rolcreatedb,
       member.rolreplication,member.rolbypassrls,member.rolinherit,
       m.admin_option,m.inherit_option,m.set_option
FROM pg_auth_members m
JOIN pg_roles parent ON parent.oid=m.roleid
JOIN pg_roles member ON member.oid=m.member
WHERE parent.rolname='at_app'
ORDER BY application_login;
SELECT a.pid,a.datname,a.usename,a.application_name,
       a.client_addr::text AS client_addr,a.backend_type,a.state
FROM pg_stat_activity a
WHERE a.datname=current_database()
  AND a.usename IN (
    SELECT member.rolname FROM pg_auth_members m
    JOIN pg_roles parent ON parent.oid=m.roleid
    JOIN pg_roles member ON member.oid=m.member
    WHERE parent.rolname='at_app')
ORDER BY a.usename,a.pid;
SELECT extversion AS timescaledb_version FROM pg_extension WHERE extname='timescaledb';
SELECT j.job_id,j.application_name,j.proc_name,j.owner,j.hypertable_schema,
       j.hypertable_name,j.config,j.schedule_interval,j.scheduled,b.timezone
FROM timescaledb_information.jobs j
JOIN _timescaledb_config.bgw_job b ON b.id=j.job_id ORDER BY j.job_id;
SELECT n.nspname,c.relname,c.relkind,pg_get_userbyid(c.relowner) AS owner
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname IN ('public','review','research','paper','_timescaledb_internal')
ORDER BY n.nspname,c.relname;
ROLLBACK;
