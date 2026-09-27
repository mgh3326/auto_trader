BEGIN READ ONLY;
SELECT d.datname,d.datacl,
       has_database_privilege('at_app',d.datname,'CONNECT') AS app_connect,
       has_database_privilege('at_migration_owner',d.datname,'CONNECT') AS owner_connect,
       has_database_privilege('at_app',d.datname,'CREATE') AS app_create,
       has_database_privilege('at_app',d.datname,'TEMPORARY') AS app_temporary
FROM pg_database d WHERE d.datname=current_database();
SELECT a.privilege_type,a.is_grantable,pg_get_userbyid(a.grantor) AS grantor
FROM pg_database d, LATERAL aclexplode(d.datacl) a
JOIN pg_roles r ON r.oid=a.grantee
WHERE d.datname=current_database() AND r.rolname='at_app';
SELECT c.oid::regclass AS object_name, pg_get_userbyid(c.relowner) AS owner,
       has_table_privilege('at_app',c.oid,'SELECT') AS app_select,
       has_table_privilege('at_app',c.oid,'INSERT') AS app_insert,
       has_table_privilege('at_app',c.oid,'UPDATE') AS app_update,
       has_table_privilege('at_app',c.oid,'DELETE') AS app_delete,
       has_table_privilege('at_app',c.oid,'TRUNCATE') AS app_truncate,
       has_table_privilege('nhplug_operator',c.oid,'SELECT') AS operator_select,
       has_table_privilege('nhplug_operator',c.oid,'INSERT') AS operator_insert,
       has_table_privilege('nhplug_operator',c.oid,'UPDATE') AS operator_update
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='review' AND c.relname IN
 ('nhplug_mock_key_version','nhplug_success_proof_code',
  'nhplug_no_order_proof_code','nhplug_mock_operator_authorization',
  'nhplug_mock_account_ref','nhplug_mock_account_binding',
  'nhplug_mock_order_ledger','kiwoom_authority_attempts',
  'kiwoom_authority_cessation_receipts')
ORDER BY object_name;
SELECT c.oid::regclass AS sequence_name,
       has_sequence_privilege('at_app',c.oid,'USAGE') AS app_usage,
       has_sequence_privilege('at_app',c.oid,'SELECT') AS app_select,
       has_sequence_privilege('at_app',c.oid,'UPDATE') AS app_update
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname IN ('public','review','research','paper') AND c.relkind='S'
ORDER BY sequence_name;
SELECT p.oid::regprocedure AS function_name, p.prosecdef, p.proconfig,
       has_function_privilege('at_app',p.oid,'EXECUTE') AS app_execute,
       has_function_privilege('nhplug_security_owner',p.oid,'EXECUTE') AS security_owner_execute,
       has_function_privilege('at_app',p.oid,'EXECUTE') =
         (p.proname IN ('nhplug_body_field','nhplug_body_digest_v1'))
         AS app_execute_matrix_ok,
       has_function_privilege('nhplug_security_owner',p.oid,'EXECUTE') =
         (p.proname IN ('nhplug_body_field','nhplug_body_digest_v1')
          OR pg_get_userbyid(p.proowner)='nhplug_security_owner')
         AS security_owner_execute_matrix_ok,
       EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
               WHERE a.grantee=0 AND a.privilege_type='EXECUTE') AS public_execute
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='review' AND (p.proname LIKE 'nhplug_%'
  OR p.proname='reject_kiwoom_authority_evidence_mutation')
ORDER BY function_name;
SELECT pg_get_userbyid(d.defaclrole) AS creator,
       COALESCE(n.nspname,'<database>') AS schema_name,
       d.defaclobjtype,d.defaclacl
FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
ORDER BY creator,schema_name,d.defaclobjtype;
ROLLBACK;
