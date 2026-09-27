BEGIN READ ONLY;
SELECT n.nspname, pg_get_userbyid(n.nspowner) AS owner
FROM pg_namespace n WHERE n.nspname IN ('public','review','research','paper')
ORDER BY n.nspname;
SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner) AS owner,
       EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass
                 AND d.objid=c.oid AND d.deptype='e') AS extension_member
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname IN ('public','review','research','paper','_timescaledb_internal')
  AND c.relkind IN ('r','p','v','m','S','f')
ORDER BY n.nspname,c.relname;
SELECT n.nspname, p.oid::regprocedure AS function_name,
       pg_get_userbyid(p.proowner) AS owner, p.prosecdef, p.proconfig
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname IN ('public','review','research','paper')
ORDER BY n.nspname, function_name;
SELECT n.nspname, t.typname, t.typtype, t.typrelid::regclass AS relation_type_of,
       pg_get_userbyid(t.typowner) AS owner
FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
WHERE n.nspname IN ('public','review','research','paper','_timescaledb_internal')
ORDER BY n.nspname,t.typname;
SELECT extversion AS timescaledb_version FROM pg_extension WHERE extname='timescaledb';
SELECT j.job_id,j.application_name,j.proc_schema,j.proc_name,j.owner,
       j.hypertable_schema,j.hypertable_name,j.config,j.schedule_interval,
       j.max_runtime,j.max_retries,j.retry_period,j.scheduled,j.fixed_schedule,
       j.initial_start,j.next_start,j.check_schema,j.check_name,b.timezone
FROM timescaledb_information.jobs j
JOIN _timescaledb_config.bgw_job b ON b.id=j.job_id
ORDER BY j.job_id;
SELECT hypertable_schema,hypertable_name,owner
FROM timescaledb_information.hypertables ORDER BY 1,2;
SELECT view_schema,view_name,view_owner,
       materialization_hypertable_schema,materialization_hypertable_name
FROM timescaledb_information.continuous_aggregates ORDER BY 1,2;
SELECT hypertable_schema,hypertable_name,chunk_schema,chunk_name,is_compressed
FROM timescaledb_information.chunks ORDER BY 1,2,3,4;
ROLLBACK;
