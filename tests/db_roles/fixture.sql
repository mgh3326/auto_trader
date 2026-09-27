\set ON_ERROR_STOP on

-- Run only in the disposable auto_trader database after Alembic reaches head.
DO $$
BEGIN
  IF current_database() <> 'auto_trader' OR current_setting('server_version_num')::int < 170000 THEN
    RAISE EXCEPTION 'refusing non-fixture database';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 't789_ticks') THEN
    RAISE EXCEPTION 'fixture already exists';
  END IF;
END
$$;

-- Production ran the final protected migrations as the distinct postgres
-- superuser. Recreate that ownership mix after the fixture migration.
ALTER TABLE review.nhplug_mock_account_ref OWNER TO postgres;
ALTER TABLE review.nhplug_mock_account_binding OWNER TO postgres;
ALTER TABLE review.nhplug_mock_order_ledger OWNER TO postgres;
ALTER TABLE review.kiwoom_authority_attempts OWNER TO postgres;
ALTER TABLE review.kiwoom_authority_cessation_receipts OWNER TO postgres;
ALTER FUNCTION review.nhplug_body_field(text, text) OWNER TO postgres;
ALTER FUNCTION review.nhplug_body_digest_v1(text, text, text, bigint, bigint, text, text, text) OWNER TO postgres;
ALTER FUNCTION review.reject_kiwoom_authority_evidence_mutation() OWNER TO postgres;

SET ROLE mgh3326;
CREATE TABLE public.t789_ticks (
  ts timestamptz NOT NULL,
  price numeric NOT NULL
);
SELECT create_hypertable('public.t789_ticks', 'ts', chunk_time_interval => interval '1 day');
INSERT INTO public.t789_ticks VALUES
  (now() - interval '3 days', 10),
  (now() - interval '2 days', 20),
  (now() - interval '1 day', 30);

CREATE MATERIALIZED VIEW public.t789_ticks_hour
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', ts) AS bucket, avg(price) AS avg_price
FROM public.t789_ticks
GROUP BY bucket
WITH NO DATA;
SELECT add_continuous_aggregate_policy(
  'public.t789_ticks_hour',
  start_offset => interval '7 days',
  end_offset => interval '1 hour',
  schedule_interval => interval '1 day'
);
SELECT add_retention_policy('public.t789_ticks', interval '30 days');
RESET ROLE;

CREATE TABLE public.t789_postgres_owned (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, value text);
CREATE VIEW public.t789_postgres_view AS SELECT id, value FROM public.t789_postgres_owned;
CREATE FUNCTION public.t789_postgres_function() RETURNS int LANGUAGE sql AS $$SELECT 1$$;
CREATE TYPE public.t789_postgres_type AS ENUM ('fixture');

SET ROLE mgh3326;
CREATE TABLE public.t789_mgh_owned (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, value text);
RESET ROLE;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mgh3326' AND oid = 10)
     OR NOT EXISTS (
       SELECT 1 FROM pg_stat_activity
       WHERE usename = 'mgh3326'
         AND application_name = 'TimescaleDB Background Worker Scheduler'
     ) THEN
    RAISE EXCEPTION 'bootstrap and scheduler ownership fixture mismatch';
  END IF;
  IF (SELECT count(*) FROM timescaledb_information.chunks
      WHERE hypertable_schema = 'public' AND hypertable_name = 't789_ticks') < 2 THEN
    RAISE EXCEPTION 'fixture hypertable has too few chunks';
  END IF;
  IF (SELECT count(*) FROM timescaledb_information.jobs
      WHERE hypertable_name LIKE 't789_%' AND owner::text = 'mgh3326') <> 2 THEN
    RAISE EXCEPTION 'fixture policy job ownership mismatch';
  END IF;
END
$$;

SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner) AS owner
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname LIKE 't789_%'
ORDER BY n.nspname, c.relname;
SELECT job_id, proc_name, owner FROM timescaledb_information.jobs
WHERE hypertable_name LIKE 't789_%' ORDER BY job_id;
