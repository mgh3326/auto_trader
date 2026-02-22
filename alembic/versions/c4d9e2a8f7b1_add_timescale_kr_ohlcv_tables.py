from alembic import op

revision = "c4d9e2a8f7b1"
down_revision = "b71d9dce2f34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS market_candles_1m (
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            open NUMERIC(20, 6) NOT NULL,
            high NUMERIC(20, 6) NOT NULL,
            low NUMERIC(20, 6) NOT NULL,
            close NUMERIC(20, 6) NOT NULL,
            volume BIGINT NOT NULL DEFAULT 0,
            value BIGINT NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'kis',
            route TEXT NOT NULL,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (market, symbol, ts)
        )
        """
    )

    op.execute(
        """
        SELECT create_hypertable(
            'market_candles_1m',
            by_range('ts', INTERVAL '7 days'),
            if_not_exists => TRUE
        )
        """
    )

    op.execute(
        """
        CREATE MATERIALIZED VIEW IF NOT EXISTS market_candles_1h_kr
        WITH (timescaledb.continuous) AS
        SELECT
            market,
            symbol,
            time_bucket('1 hour', ts, 'Asia/Seoul') AS bucket_start,
            first(open, ts) AS open,
            max(high) AS high,
            min(low) AS low,
            last(close, ts) AS close,
            sum(volume) AS volume,
            sum(value) AS value
        FROM market_candles_1m
        WHERE market = 'kr'
        GROUP BY market, symbol, bucket_start
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
        SELECT add_retention_policy(
            'market_candles_1m',
            INTERVAL '30 days',
            if_not_exists => TRUE
        )
        """
    )

    op.execute(
        """
        SELECT add_retention_policy(
            'market_candles_1h_kr',
            INTERVAL '400 days',
            if_not_exists => TRUE
        )
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
    op.execute(
        """
        SELECT remove_retention_policy(
            'market_candles_1h_kr',
            if_not_exists => TRUE
        )
        """
    )
    op.execute(
        """
        SELECT remove_retention_policy(
            'market_candles_1m',
            if_not_exists => TRUE
        )
        """
    )

    op.execute("DROP MATERIALIZED VIEW IF EXISTS market_candles_1h_kr")
    op.execute("DROP TABLE IF EXISTS market_candles_1m")
