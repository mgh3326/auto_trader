BEGIN READ ONLY;
SELECT rolname, rolsuper, rolcanlogin, rolcreatedb, rolcreaterole,
       rolreplication, rolbypassrls, rolinherit
FROM pg_roles
WHERE rolname IN ('at_app', 'at_migration_owner',
                  'nhplug_security_owner', 'nhplug_operator')
ORDER BY rolname;
SELECT parent.rolname AS granted_role, member.rolname AS member_role,
       m.admin_option, m.inherit_option, m.set_option
FROM pg_auth_members m
JOIN pg_roles parent ON parent.oid = m.roleid
JOIN pg_roles member ON member.oid = m.member
WHERE parent.rolname IN ('at_app', 'at_migration_owner',
                         'nhplug_security_owner', 'nhplug_operator')
   OR member.rolname IN ('at_app', 'at_migration_owner',
                         'nhplug_security_owner', 'nhplug_operator')
ORDER BY granted_role, member_role;
SELECT rolname, rolpassword IS NULL AS password_absent
FROM pg_authid WHERE rolname='at_migration_owner';
SELECT rule_number,type,database,user_name,address,netmask,auth_method,error
FROM pg_hba_file_rules ORDER BY rule_number LIMIT 8;
SELECT pg_conf_load_time() AS configuration_loaded_at,
       (pg_stat_file(current_setting('hba_file'))).modification AS hba_modified_at;
ROLLBACK;
