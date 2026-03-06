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

CREATE MATERIALIZED VIEW public.kr_candles_1h
WITH (
    timescaledb.continuous,
    timescaledb.materialized_only = false
)
AS
SELECT
    time_bucket(INTERVAL '1 hour', time, 'Asia/Seoul') AS bucket,
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
WITH NO DATA;

DO $$
BEGIN
    IF to_regclass('public.kr_candles_1h') IS NOT NULL THEN
        EXECUTE $sql$
            SELECT remove_continuous_aggregate_policy(
                'public.kr_candles_1h',
                if_exists => TRUE
            )
        $sql$;

        EXECUTE $sql$
            SELECT add_continuous_aggregate_policy(
                'public.kr_candles_1h',
                start_offset => INTERVAL '2 days',
                end_offset => INTERVAL '1 hour',
                schedule_interval => INTERVAL '5 minutes'
            )
        $sql$;
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('public.kr_candles_1m') IS NOT NULL THEN
        PERFORM remove_retention_policy(
            'public.kr_candles_1m',
            if_exists => TRUE
        );
        PERFORM add_retention_policy(
            'public.kr_candles_1m',
            INTERVAL '90 days'
        );
    END IF;

    IF to_regclass('public.kr_candles_1h') IS NOT NULL THEN
        PERFORM remove_retention_policy(
            'public.kr_candles_1h',
            if_exists => TRUE
        );
        PERFORM add_retention_policy(
            'public.kr_candles_1h',
            INTERVAL '90 days'
        );
    END IF;
END
$$;

DO $$
DECLARE
    v_start TIMESTAMPTZ;
    v_end TIMESTAMPTZ;
    v_refresh_end TIMESTAMPTZ;
BEGIN
    IF to_regclass('public.kr_candles_1h') IS NULL THEN
        RETURN;
    END IF;

    SELECT MIN(time), MAX(time)
    INTO v_start, v_end
    FROM public.kr_candles_1m;

    IF v_start IS NOT NULL AND v_end IS NOT NULL THEN
        v_refresh_end := LEAST(
            v_end + INTERVAL '1 hour',
            date_trunc('hour', now() - INTERVAL '1 hour')
        );

        IF v_refresh_end <= v_start THEN
            RETURN;
        END IF;

        CALL refresh_continuous_aggregate(
            'public.kr_candles_1h',
            v_start,
            v_refresh_end
        );
    END IF;
END
$$;
