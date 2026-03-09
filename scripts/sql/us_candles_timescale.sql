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

    IF (v_major, v_minor, v_patch) < (2, 15, 0) THEN
        RAISE EXCEPTION
            'timescaledb extension version % is below required minimum 2.15.0',
            v_extversion;
    END IF;
END
$$;

CREATE TABLE public.us_candles_1m (
    time TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    value NUMERIC NOT NULL,
    CONSTRAINT ck_us_candles_1m_exchange CHECK (exchange IN ('NASD', 'NYSE', 'AMEX')),
    CONSTRAINT uq_us_candles_1m_time_symbol_exchange UNIQUE (time, symbol, exchange)
);

SELECT create_hypertable(
    'public.us_candles_1m',
    'time',
    migrate_data => TRUE
);

CREATE INDEX ix_us_candles_1m_symbol_exchange_time_desc
    ON public.us_candles_1m (symbol, exchange, time DESC);

CREATE MATERIALIZED VIEW public.us_candles_5m
WITH (
    timescaledb.continuous,
    timescaledb.materialized_only = false
)
AS
SELECT
    time_bucket(INTERVAL '5 minutes', time) AS bucket,
    symbol,
    exchange,
    FIRST(open, (extract(epoch from time) * 1000000)::bigint) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, (extract(epoch from time) * 1000000)::bigint) AS close,
    SUM(volume) AS volume,
    SUM(value) AS value
FROM public.us_candles_1m
GROUP BY bucket, symbol, exchange
WITH NO DATA;

CREATE MATERIALIZED VIEW public.us_candles_15m
WITH (
    timescaledb.continuous,
    timescaledb.materialized_only = false
)
AS
SELECT
    time_bucket(INTERVAL '15 minutes', time) AS bucket,
    symbol,
    exchange,
    FIRST(open, (extract(epoch from time) * 1000000)::bigint) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, (extract(epoch from time) * 1000000)::bigint) AS close,
    SUM(volume) AS volume,
    SUM(value) AS value
FROM public.us_candles_1m
GROUP BY bucket, symbol, exchange
WITH NO DATA;

CREATE MATERIALIZED VIEW public.us_candles_30m
WITH (
    timescaledb.continuous,
    timescaledb.materialized_only = false
)
AS
SELECT
    time_bucket(INTERVAL '30 minutes', time) AS bucket,
    symbol,
    exchange,
    FIRST(open, (extract(epoch from time) * 1000000)::bigint) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, (extract(epoch from time) * 1000000)::bigint) AS close,
    SUM(volume) AS volume,
    SUM(value) AS value
FROM public.us_candles_1m
GROUP BY bucket, symbol, exchange
WITH NO DATA;

CREATE MATERIALIZED VIEW public.us_candles_1h
WITH (
    timescaledb.continuous,
    timescaledb.materialized_only = false
)
AS
SELECT
    time_bucket(
        INTERVAL '1 hour',
        time,
        timezone => 'America/New_York',
        "offset" => INTERVAL '30 minutes'
    ) AS bucket,
    symbol,
    exchange,
    FIRST(open, (extract(epoch from time) * 1000000)::bigint) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, (extract(epoch from time) * 1000000)::bigint) AS close,
    SUM(volume) AS volume,
    SUM(value) AS value
FROM public.us_candles_1m
WHERE (time AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
  AND (time AT TIME ZONE 'America/New_York')::time < TIME '16:00'
GROUP BY bucket, symbol, exchange
WITH NO DATA;

DO $$
BEGIN
    IF to_regclass('public.us_candles_5m') IS NOT NULL THEN
        EXECUTE $sql$
            SELECT remove_continuous_aggregate_policy(
                'public.us_candles_5m',
                if_exists => TRUE
            )
        $sql$;

        EXECUTE $sql$
            SELECT add_continuous_aggregate_policy(
                'public.us_candles_5m',
                start_offset => INTERVAL '2 days',
                end_offset => INTERVAL '5 minutes',
                schedule_interval => INTERVAL '5 minutes'
            )
        $sql$;
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('public.us_candles_15m') IS NOT NULL THEN
        EXECUTE $sql$
            SELECT remove_continuous_aggregate_policy(
                'public.us_candles_15m',
                if_exists => TRUE
            )
        $sql$;

        EXECUTE $sql$
            SELECT add_continuous_aggregate_policy(
                'public.us_candles_15m',
                start_offset => INTERVAL '2 days',
                end_offset => INTERVAL '15 minutes',
                schedule_interval => INTERVAL '15 minutes'
            )
        $sql$;
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('public.us_candles_30m') IS NOT NULL THEN
        EXECUTE $sql$
            SELECT remove_continuous_aggregate_policy(
                'public.us_candles_30m',
                if_exists => TRUE
            )
        $sql$;

        EXECUTE $sql$
            SELECT add_continuous_aggregate_policy(
                'public.us_candles_30m',
                start_offset => INTERVAL '2 days',
                end_offset => INTERVAL '30 minutes',
                schedule_interval => INTERVAL '30 minutes'
            )
        $sql$;
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('public.us_candles_1h') IS NOT NULL THEN
        EXECUTE $sql$
            SELECT remove_continuous_aggregate_policy(
                'public.us_candles_1h',
                if_exists => TRUE
            )
        $sql$;

        EXECUTE $sql$
            SELECT add_continuous_aggregate_policy(
                'public.us_candles_1h',
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
    IF to_regclass('public.us_candles_1m') IS NOT NULL THEN
        PERFORM remove_retention_policy(
            'public.us_candles_1m',
            if_exists => TRUE
        );
        PERFORM add_retention_policy(
            'public.us_candles_1m',
            INTERVAL '90 days'
        );
    END IF;

    IF to_regclass('public.us_candles_5m') IS NOT NULL THEN
        PERFORM remove_retention_policy(
            'public.us_candles_5m',
            if_exists => TRUE
        );
        PERFORM add_retention_policy(
            'public.us_candles_5m',
            INTERVAL '90 days'
        );
    END IF;

    IF to_regclass('public.us_candles_15m') IS NOT NULL THEN
        PERFORM remove_retention_policy(
            'public.us_candles_15m',
            if_exists => TRUE
        );
        PERFORM add_retention_policy(
            'public.us_candles_15m',
            INTERVAL '90 days'
        );
    END IF;

    IF to_regclass('public.us_candles_30m') IS NOT NULL THEN
        PERFORM remove_retention_policy(
            'public.us_candles_30m',
            if_exists => TRUE
        );
        PERFORM add_retention_policy(
            'public.us_candles_30m',
            INTERVAL '90 days'
        );
    END IF;

    IF to_regclass('public.us_candles_1h') IS NOT NULL THEN
        PERFORM remove_retention_policy(
            'public.us_candles_1h',
            if_exists => TRUE
        );
        PERFORM add_retention_policy(
            'public.us_candles_1h',
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
    IF to_regclass('public.us_candles_5m') IS NULL THEN
        RETURN;
    END IF;

    SELECT MIN(time), MAX(time)
    INTO v_start, v_end
    FROM public.us_candles_1m;

    IF v_start IS NOT NULL AND v_end IS NOT NULL THEN
        v_refresh_end := LEAST(
            v_end + INTERVAL '5 minutes',
            now() - INTERVAL '5 minutes'
        );

        IF v_refresh_end <= v_start THEN
            RETURN;
        END IF;

        CALL refresh_continuous_aggregate(
            'public.us_candles_5m',
            v_start,
            v_refresh_end
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
    IF to_regclass('public.us_candles_15m') IS NULL THEN
        RETURN;
    END IF;

    SELECT MIN(time), MAX(time)
    INTO v_start, v_end
    FROM public.us_candles_1m;

    IF v_start IS NOT NULL AND v_end IS NOT NULL THEN
        v_refresh_end := LEAST(
            v_end + INTERVAL '15 minutes',
            now() - INTERVAL '15 minutes'
        );

        IF v_refresh_end <= v_start THEN
            RETURN;
        END IF;

        CALL refresh_continuous_aggregate(
            'public.us_candles_15m',
            v_start,
            v_refresh_end
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
    IF to_regclass('public.us_candles_30m') IS NULL THEN
        RETURN;
    END IF;

    SELECT MIN(time), MAX(time)
    INTO v_start, v_end
    FROM public.us_candles_1m;

    IF v_start IS NOT NULL AND v_end IS NOT NULL THEN
        v_refresh_end := LEAST(
            v_end + INTERVAL '30 minutes',
            now() - INTERVAL '30 minutes'
        );

        IF v_refresh_end <= v_start THEN
            RETURN;
        END IF;

        CALL refresh_continuous_aggregate(
            'public.us_candles_30m',
            v_start,
            v_refresh_end
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
    IF to_regclass('public.us_candles_1h') IS NULL THEN
        RETURN;
    END IF;

    SELECT MIN(time), MAX(time)
    INTO v_start, v_end
    FROM public.us_candles_1m;

    IF v_start IS NOT NULL AND v_end IS NOT NULL THEN
        v_refresh_end := LEAST(
            v_end + INTERVAL '1 hour',
            now() - INTERVAL '1 hour'
        );

        IF v_refresh_end <= v_start THEN
            RETURN;
        END IF;

        CALL refresh_continuous_aggregate(
            'public.us_candles_1h',
            v_start,
            v_refresh_end
        );
    END IF;
END
$$;
