from alembic import op

revision = "d2f4a8c1b9e3"
down_revision = "c9e4f5b8a2d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS market_candles_ingest_quarantine (
            id BIGSERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            route TEXT,
            exchange_raw TEXT NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL,
            reason TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_market_candles_ingest_quarantine_created_at
        ON market_candles_ingest_quarantine (created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS market_candles_1m_kr_v2 (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            open BIGINT NOT NULL,
            high BIGINT NOT NULL,
            low BIGINT NOT NULL,
            close BIGINT NOT NULL,
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
            'market_candles_1m_kr_v2',
            by_range('ts', INTERVAL '7 days'),
            if_not_exists => TRUE
        )
        """
    )

    op.execute(
        """
        INSERT INTO market_candles_1m_kr_v2 (
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
            exchange,
            symbol,
            ts,
            ROUND(open)::BIGINT,
            ROUND(high)::BIGINT,
            ROUND(low)::BIGINT,
            ROUND(close)::BIGINT,
            volume,
            value,
            source,
            fetched_at,
            updated_at
        FROM market_candles_1m_kr
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
        CREATE MATERIALIZED VIEW IF NOT EXISTS market_candles_1h_kr_v2
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
        FROM market_candles_1m_kr_v2
        WHERE exchange IN ('KRX', 'NXT')
        GROUP BY symbol, bucket_start
        WITH NO DATA
        """
    )

    op.execute(
        """
        ALTER MATERIALIZED VIEW market_candles_1h_kr_v2
        SET (timescaledb.materialized_only = false)
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_market_candles_1h_kr_v2_symbol_bucket
        ON market_candles_1h_kr_v2 (symbol, bucket_start)
        """
    )

    op.execute(
        """
        SELECT add_continuous_aggregate_policy(
            'market_candles_1h_kr_v2',
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
            'market_candles_1h_kr_v2',
            if_not_exists => TRUE
        )
        """
    )

    op.execute("DROP MATERIALIZED VIEW IF EXISTS market_candles_1h_kr_v2")
    op.execute("DROP TABLE IF EXISTS market_candles_1m_kr_v2")
    op.execute("DROP TABLE IF EXISTS market_candles_ingest_quarantine")
