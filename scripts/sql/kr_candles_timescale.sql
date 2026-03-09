DO $$
DECLARE
    v_extversion TEXT;
    v_version_core TEXT;
    v_parts TEXT[];
    v_major INTEGER;
    v_minor INTEGER;
    v_patch INTEGER;
BEGIN
    SELECT extversion
    INTO v_extversion
    FROM pg_extension
    WHERE extname = 'timescaledb';

    IF v_extversion IS NULL THEN
        RAISE EXCEPTION 'timescaledb extension is not installed';
    END IF;

    v_version_core := split_part(v_extversion, '-', 1);
    v_parts := regexp_split_to_array(v_version_core, '\.');

    v_major := COALESCE(v_parts[1], '0')::INTEGER;
    v_minor := COALESCE(v_parts[2], '0')::INTEGER;
    v_patch := COALESCE(v_parts[3], '0')::INTEGER;

    IF (v_major, v_minor, v_patch) < (2, 8, 1) THEN
        RAISE EXCEPTION
            'timescaledb extension version % is below required minimum 2.8.1',
            v_extversion;
    END IF;
END
$$;

CREATE TABLE public.kr_candles_1m (
    time TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    venue TEXT NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    value NUMERIC NOT NULL,
    CONSTRAINT ck_kr_candles_1m_venue CHECK (venue IN ('KRX', 'NTX')),
    CONSTRAINT uq_kr_candles_1m_time_symbol_venue UNIQUE (time, symbol, venue)
);

SELECT create_hypertable(
    'public.kr_candles_1m',
    'time',
    migrate_data => TRUE
);

CREATE INDEX ix_kr_candles_1m_symbol_time_desc
    ON public.kr_candles_1m (symbol, time DESC);

DO $$
DECLARE
    v_relation_prefix CONSTANT TEXT := 'public.kr_candles_';
    v_cagg_suffixes CONSTANT TEXT[] := ARRAY['1h', '5m', '15m', '30m'];
    v_cagg_interval_sqls CONSTANT TEXT[] := ARRAY[
        'INTERVAL ''1 hour''',
        'INTERVAL ''5 minutes''',
        'INTERVAL ''15 minutes''',
        'INTERVAL ''30 minutes'''
    ];
    v_retention_suffixes CONSTANT TEXT[] := ARRAY['1m', '5m', '15m', '30m', '1h'];
    v_start_offset_sql CONSTANT TEXT := 'INTERVAL ''2 days''';
    v_schedule_interval_sql CONSTANT TEXT := 'INTERVAL ''5 minutes''';
    v_retention_interval_sql CONSTANT TEXT := 'INTERVAL ''90 days''';
    v_relation_name TEXT;
    v_bucket_interval_sql TEXT;
    v_suffix TEXT;
    v_view_name TEXT;
    v_refresh_end TIMESTAMPTZ;
    v_start TIMESTAMPTZ;
    v_end TIMESTAMPTZ;
    v_index INTEGER;
BEGIN
    FOR v_index IN 1..array_length(v_cagg_suffixes, 1) LOOP
        v_suffix := v_cagg_suffixes[v_index];
        v_relation_name := v_relation_prefix || v_suffix;
        v_bucket_interval_sql := v_cagg_interval_sqls[v_index];
        v_view_name := 'kr_candles_' || v_suffix;

        EXECUTE format(
            $sql$
            CREATE MATERIALIZED VIEW public.%I
            WITH (
                timescaledb.continuous,
                timescaledb.materialized_only = false
            )
            AS
            SELECT
                time_bucket(%s, time, 'Asia/Seoul') AS bucket,
                symbol,
                FIRST(
                    open,
                    ((extract(epoch from time) * 1000000)::bigint * 2
                    + CASE WHEN venue = 'KRX' THEN 0 ELSE 1 END)
                ) AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                LAST(
                    close,
                    ((extract(epoch from time) * 1000000)::bigint * 2
                    + CASE WHEN venue = 'KRX' THEN 1 ELSE 0 END)
                ) AS close,
                SUM(volume) AS volume,
                SUM(value) AS value,
                array_agg(DISTINCT venue ORDER BY venue) AS venues
            FROM public.kr_candles_1m
            GROUP BY bucket, symbol
            WITH NO DATA
            $sql$,
            v_view_name,
            v_bucket_interval_sql
        );

        IF to_regclass(v_relation_name) IS NOT NULL THEN
            EXECUTE format(
                $sql$
                SELECT remove_continuous_aggregate_policy(
                    %L,
                    if_exists => TRUE
                )
                $sql$,
                v_relation_name
            );

            EXECUTE format(
                $sql$
                SELECT add_continuous_aggregate_policy(
                    %L,
                    start_offset => %s,
                    end_offset => %s,
                    schedule_interval => %s
                )
                $sql$,
                v_relation_name,
                v_start_offset_sql,
                v_bucket_interval_sql,
                v_schedule_interval_sql
            );
        END IF;
    END LOOP;

    FOREACH v_suffix IN ARRAY v_retention_suffixes LOOP
        v_relation_name := v_relation_prefix || v_suffix;
        IF to_regclass(v_relation_name) IS NOT NULL THEN
            EXECUTE format(
                $sql$
                SELECT remove_retention_policy(
                    %L,
                    if_exists => TRUE
                )
                $sql$,
                v_relation_name
            );

            EXECUTE format(
                $sql$
                SELECT add_retention_policy(
                    %L,
                    %s
                )
                $sql$,
                v_relation_name,
                v_retention_interval_sql
            );
        END IF;
    END LOOP;

    IF to_regclass('public.kr_candles_1h') IS NULL THEN
        RETURN;
    END IF;

    SELECT MIN(time), MAX(time)
    INTO v_start, v_end
    FROM public.kr_candles_1m;

    IF v_start IS NULL OR v_end IS NULL THEN
        RETURN;
    END IF;

    FOR v_index IN 1..array_length(v_cagg_suffixes, 1) LOOP
        v_suffix := v_cagg_suffixes[v_index];
        v_relation_name := v_relation_prefix || v_suffix;
        v_bucket_interval_sql := v_cagg_interval_sqls[v_index];

        IF v_suffix = '1h' THEN
            v_refresh_end := LEAST(
                v_end + INTERVAL '1 hour',
                date_trunc('hour', now() - INTERVAL '1 hour')
            );
        ELSE
            EXECUTE format(
                'SELECT LEAST($1 + %s, now() - %s)',
                v_bucket_interval_sql,
                v_bucket_interval_sql
            )
            INTO v_refresh_end
            USING v_end;
        END IF;

        IF v_refresh_end > v_start THEN
            EXECUTE format(
                'CALL refresh_continuous_aggregate(%L, $1, $2)',
                v_relation_name
            )
            USING v_start, v_refresh_end;
        END IF;
    END LOOP;
END
$$;
