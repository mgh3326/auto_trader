from alembic import op

revision = "c9e4f5b8a2d1"
down_revision = "c4d9e2a8f7b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS market_candles_1m_kr (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            open NUMERIC(20, 6) NOT NULL,
            high NUMERIC(20, 6) NOT NULL,
            low NUMERIC(20, 6) NOT NULL,
            close NUMERIC(20, 6) NOT NULL,
            volume BIGINT NOT NULL DEFAULT 0,
            value BIGINT NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'kis',
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, symbol, ts)
        )
        """
    )

    op.execute(
        """
        SELECT create_hypertable(
            'market_candles_1m_kr',
            by_range('ts', INTERVAL '7 days'),
            if_not_exists => TRUE
        )
        """
    )

    op.execute(
        """
        DO $$
        DECLARE
            invalid_route_count BIGINT;
        BEGIN
            SELECT COUNT(*)
            INTO invalid_route_count
            FROM market_candles_1m
            WHERE lower(coalesce(market, '')) = 'kr'
              AND upper(coalesce(route, '')) NOT IN ('J', 'NX', 'NXT');

            IF invalid_route_count > 0 THEN
                RAISE EXCEPTION
                    'c9 KR backfill aborted: found % rows with unsupported route (allowed: J,NX,NXT)',
                    invalid_route_count;
            END IF;
        END
        $$;
        """
    )

    op.execute(
        """
        INSERT INTO market_candles_1m_kr (
            exchange,
            symbol,
            ts,
            open,
            high,
            low,
            close,
            volume,
            value,
            source,
            fetched_at,
            updated_at
        )
        SELECT
            CASE
                WHEN upper(coalesce(route, '')) = 'J' THEN 'KRX'
                WHEN upper(coalesce(route, '')) IN ('NX', 'NXT') THEN 'NXT'
            END AS exchange,
            symbol,
            ts,
            open,
            high,
            low,
            close,
            volume,
            value,
            source,
            fetched_at,
            updated_at
        FROM market_candles_1m
        WHERE lower(coalesce(market, '')) = 'kr'
          AND upper(coalesce(route, '')) IN ('J', 'NX', 'NXT')
        ON CONFLICT (exchange, symbol, ts) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            value = EXCLUDED.value,
            source = EXCLUDED.source,
            fetched_at = EXCLUDED.fetched_at,
            updated_at = EXCLUDED.updated_at
        """
    )

    op.execute(
        """
        SELECT remove_continuous_aggregate_policy(
            'market_candles_1h_kr',
            if_not_exists => TRUE
        )
        """
    )

    op.execute("DROP MATERIALIZED VIEW IF EXISTS market_candles_1h_kr")

    op.execute(
        """
        CREATE MATERIALIZED VIEW market_candles_1h_kr
        WITH (timescaledb.continuous) AS
        SELECT
            symbol,
            time_bucket(INTERVAL '1 hour', ts, 'Asia/Seoul') AS bucket_start,

            first(
                open,
                ts + CASE
                    WHEN exchange = 'NXT' THEN INTERVAL '1 millisecond'
                    ELSE INTERVAL '0'
                END
            ) AS open,

            MAX(high) AS high,
            MIN(low) AS low,

            last(
                close,
                ts + CASE
                    WHEN exchange = 'KRX' THEN INTERVAL '1 millisecond'
                    ELSE INTERVAL '0'
                END
            ) AS close,

            SUM(volume)::BIGINT AS volume,
            SUM(value)::BIGINT AS value
        FROM market_candles_1m_kr
        WHERE exchange IN ('KRX', 'NXT')
        GROUP BY symbol, bucket_start
        WITH NO DATA
        """
    )

    op.execute(
        """
        ALTER MATERIALIZED VIEW market_candles_1h_kr
        SET (timescaledb.materialized_only = false)
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_market_candles_1h_kr_symbol_bucket
        ON market_candles_1h_kr (symbol, bucket_start)
        """
    )

    op.execute(
        """
        SELECT add_continuous_aggregate_policy(
            'market_candles_1h_kr',
            start_offset => INTERVAL '8 days',
            end_offset => INTERVAL '1 minute',
            schedule_interval => INTERVAL '5 minutes',
            if_not_exists => TRUE
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SELECT remove_continuous_aggregate_policy(
            'market_candles_1h_kr',
            if_not_exists => TRUE
        )
        """
    )

    op.execute("DROP MATERIALIZED VIEW IF EXISTS market_candles_1h_kr")

    op.execute(
        """
        CREATE MATERIALIZED VIEW market_candles_1h_kr
        WITH (timescaledb.continuous) AS
        SELECT
            symbol,
            time_bucket(INTERVAL '1 hour', ts, 'Asia/Seoul') AS bucket_start,
            first(open, ts) AS open,
            MAX(high) AS high,
            MIN(low) AS low,
            last(close, ts) AS close,
            SUM(volume)::BIGINT AS volume,
            SUM(value)::BIGINT AS value
        FROM market_candles_1m_kr
        WHERE exchange IN ('KRX', 'NXT')
        GROUP BY symbol, bucket_start
        WITH NO DATA
        """
    )

    op.execute(
        """
        ALTER MATERIALIZED VIEW market_candles_1h_kr
        SET (timescaledb.materialized_only = false)
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_market_candles_1h_kr_symbol_bucket
        ON market_candles_1h_kr (symbol, bucket_start)
        """
    )

    op.execute(
        """
        SELECT add_continuous_aggregate_policy(
            'market_candles_1h_kr',
            start_offset => INTERVAL '8 days',
            end_offset => INTERVAL '1 minute',
            schedule_interval => INTERVAL '5 minutes',
            if_not_exists => TRUE
        )
        """
    )
