BEGIN READ ONLY;
SELECT current_database() AS database_name, session_user, current_user;
SELECT rolname,rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,
       rolreplication,rolbypassrls,rolinherit
FROM pg_roles WHERE rolname IN ('at_app','at_migration_owner','at_migration_runner')
ORDER BY rolname;
SELECT rolname,rolpassword IS NULL AS password_absent
FROM pg_authid WHERE rolname='at_migration_owner';
SELECT parent.rolname AS granted_role, member.rolname AS login_name,
       member.rolcanlogin, member.rolinherit,
       m.admin_option,m.inherit_option,m.set_option
FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid
JOIN pg_roles member ON member.oid=m.member
WHERE parent.rolname IN ('at_app','at_migration_owner','nhplug_operator')
ORDER BY granted_role,login_name;
SELECT usename,application_name,client_addr,state,count(*) AS connections
FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
GROUP BY usename,application_name,client_addr,state
ORDER BY usename,application_name,client_addr,state;
ROLLBACK;
