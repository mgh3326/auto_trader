-- NEEDS_DESK_SQL: run only by an approved operator against the intended DB.
-- Keep Timescale queries separate if the extension is absent.
BEGIN READ ONLY;
SELECT current_database() AS database_name, current_setting('server_version') AS pg_version,
       session_user, current_user;
SELECT extname, extversion, pg_get_userbyid(extowner) AS owner
FROM pg_extension ORDER BY extname;
SELECT d.datname,pg_get_userbyid(d.datdba) AS owner,d.datacl
FROM pg_database d WHERE d.datname=current_database();
SELECT d.datname,d.datallowconn,
       has_database_privilege('postgres',d.datname,'CONNECT') AS postgres_connect
FROM pg_database d
WHERE d.datname IN ('auto_trader','handoffkeep','prefect')
ORDER BY d.datname;
SELECT to_regclass('review.nhplug_mock_order_ledger') AS nhplug_711_present;
SELECT rolname,rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,
       rolreplication,rolbypassrls,rolinherit
FROM pg_roles WHERE rolname IN ('postgres','mgh3326','at_app',
       'at_migration_owner','at_migration_runner','nhplug_security_owner',
       'nhplug_operator') ORDER BY rolname;
SELECT rolname,rolpassword IS NULL AS password_absent
FROM pg_authid WHERE rolname='at_migration_owner';
SELECT rule_number,type,database,user_name,address,netmask,auth_method,error
FROM pg_hba_file_rules ORDER BY rule_number;
SELECT pg_conf_load_time() AS configuration_loaded_at,
       (pg_stat_file(current_setting('hba_file'))).modification AS hba_modified_at;
SELECT n.nspname, pg_get_userbyid(n.nspowner) AS owner, n.nspacl
FROM pg_namespace n WHERE n.nspname IN ('public','review','research') ORDER BY 1;
SELECT n.nspname,c.relname,c.relkind,pg_get_userbyid(c.relowner) AS owner,
       c.relacl,c.relrowsecurity,c.relforcerowsecurity,
       EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass
         AND d.objid=c.oid AND d.deptype='e') AS extension_member
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname IN ('public','review','research')
  AND c.relkind IN ('r','p','v','m','S','f') ORDER BY 1,2;
SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) AS identity_args,
       pg_get_userbyid(p.proowner) AS owner,p.prosecdef,p.proconfig,p.proacl,
       EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass
         AND d.objid=p.oid AND d.deptype='e') AS extension_member
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname IN ('public','review','research') ORDER BY 1,2,3;
SELECT n.nspname,t.typname,t.typtype,t.typrelid::regclass AS relation_type_of,
       pg_get_userbyid(t.typowner) AS owner,t.typacl
FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
WHERE n.nspname IN ('public','review','research','_timescaledb_internal')
ORDER BY 1,2;
SELECT pg_get_userbyid(d.defaclrole) AS creator,
       COALESCE(n.nspname,'<global>') AS schema_name,d.defaclobjtype,d.defaclacl
FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
ORDER BY 1,2,3;
SELECT p.rolname AS granted_role,m.rolname AS member_role,
       a.admin_option,a.inherit_option,a.set_option
FROM pg_auth_members a JOIN pg_roles p ON p.oid=a.roleid
JOIN pg_roles m ON m.oid=a.member ORDER BY 1,2;
SELECT usename,application_name,client_addr,state,count(*) AS sessions
FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
GROUP BY usename,application_name,client_addr,state ORDER BY 1,2,3,4;
SELECT pid,datname,usename,application_name,client_addr::text AS client_addr,
       backend_type,state
FROM pg_stat_activity
WHERE usename='postgres' AND datname IS NOT NULL AND pid<>pg_backend_pid()
ORDER BY datname,pid;
ROLLBACK;
