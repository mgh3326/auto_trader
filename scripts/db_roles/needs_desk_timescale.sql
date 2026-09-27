-- NEEDS_DESK_SQL: run only after timescaledb presence is confirmed.
BEGIN READ ONLY;
SELECT extversion,pg_get_userbyid(extowner) AS extension_owner
FROM pg_extension WHERE extname='timescaledb';
SELECT p.oid::regprocedure AS policy_api,
       pg_get_function_arguments(p.oid) AS arguments,
       pg_get_function_result(p.oid) AS result
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='public' AND p.proname IN
 ('add_retention_policy','remove_retention_policy',
  'add_continuous_aggregate_policy','remove_continuous_aggregate_policy',
  'alter_job','run_job')
ORDER BY policy_api;
SELECT j.job_id,j.application_name,j.proc_schema,j.proc_name,j.owner,
       j.hypertable_schema,j.hypertable_name,j.config,j.schedule_interval,
       j.max_runtime,j.max_retries,j.retry_period,j.scheduled,j.fixed_schedule,
       j.initial_start,j.next_start,j.check_schema,j.check_name,b.timezone
FROM timescaledb_information.jobs j
JOIN _timescaledb_config.bgw_job b ON b.id=j.job_id ORDER BY j.job_id;
SELECT hypertable_schema,hypertable_name,owner
FROM timescaledb_information.hypertables ORDER BY 1,2;
SELECT view_schema,view_name,view_owner,
       materialization_hypertable_schema,materialization_hypertable_name
FROM timescaledb_information.continuous_aggregates ORDER BY 1,2;
SELECT hypertable_schema,hypertable_name,chunk_schema,chunk_name,is_compressed
FROM timescaledb_information.chunks ORDER BY 1,2,3,4;
SELECT n.nspname,c.relname,c.relkind,pg_get_userbyid(c.relowner) AS owner,
       EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass
           AND d.objid=c.oid AND d.deptype='e') AS extension_member
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname LIKE E'\\_timescaledb%' ESCAPE E'\\'
ORDER BY 1,2;
ROLLBACK;
