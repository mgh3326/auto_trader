from collections.abc import Sequence

from alembic import op

revision: str = "5c6d7e8f9012"
down_revision: str | Sequence[str] | None = "d31f0a2b4c6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE MATERIALIZED VIEW public.kr_candles_5m
        WITH (
            timescaledb.continuous,
            timescaledb.materialized_only = false
        ) AS
        SELECT
            time_bucket(INTERVAL '5 minutes', time, 'Asia/Seoul') AS bucket,
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
        """
    )
    op.execute(
        """
        CREATE MATERIALIZED VIEW public.kr_candles_15m
        WITH (
            timescaledb.continuous,
            timescaledb.materialized_only = false
        ) AS
        SELECT
            time_bucket(INTERVAL '15 minutes', time, 'Asia/Seoul') AS bucket,
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
        """
    )
    op.execute(
        """
        CREATE MATERIALIZED VIEW public.kr_candles_30m
        WITH (
            timescaledb.continuous,
            timescaledb.materialized_only = false
        ) AS
        SELECT
            time_bucket(INTERVAL '30 minutes', time, 'Asia/Seoul') AS bucket,
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
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.kr_candles_5m') IS NOT NULL THEN
                EXECUTE $sql$
                    SELECT remove_continuous_aggregate_policy(
                        'public.kr_candles_5m',
                        if_exists => TRUE
                    )
                $sql$;
                EXECUTE $sql$
                    SELECT add_continuous_aggregate_policy(
                        'public.kr_candles_5m',
                        start_offset => INTERVAL '2 days',
                        end_offset => INTERVAL '5 minutes',
                        schedule_interval => INTERVAL '5 minutes'
                    )
                $sql$;
            END IF;

            IF to_regclass('public.kr_candles_15m') IS NOT NULL THEN
                EXECUTE $sql$
                    SELECT remove_continuous_aggregate_policy(
                        'public.kr_candles_15m',
                        if_exists => TRUE
                    )
                $sql$;
                EXECUTE $sql$
                    SELECT add_continuous_aggregate_policy(
                        'public.kr_candles_15m',
                        start_offset => INTERVAL '2 days',
                        end_offset => INTERVAL '15 minutes',
                        schedule_interval => INTERVAL '5 minutes'
                    )
                $sql$;
            END IF;

            IF to_regclass('public.kr_candles_30m') IS NOT NULL THEN
                EXECUTE $sql$
                    SELECT remove_continuous_aggregate_policy(
                        'public.kr_candles_30m',
                        if_exists => TRUE
                    )
                $sql$;
                EXECUTE $sql$
                    SELECT add_continuous_aggregate_policy(
                        'public.kr_candles_30m',
                        start_offset => INTERVAL '2 days',
                        end_offset => INTERVAL '30 minutes',
                        schedule_interval => INTERVAL '5 minutes'
                    )
                $sql$;
            END IF;

            IF to_regclass('public.kr_candles_5m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_5m',
                    if_exists => TRUE
                );
                PERFORM add_retention_policy(
                    'public.kr_candles_5m',
                    INTERVAL '90 days'
                );
            END IF;

            IF to_regclass('public.kr_candles_15m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_15m',
                    if_exists => TRUE
                );
                PERFORM add_retention_policy(
                    'public.kr_candles_15m',
                    INTERVAL '90 days'
                );
            END IF;

            IF to_regclass('public.kr_candles_30m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_30m',
                    if_exists => TRUE
                );
                PERFORM add_retention_policy(
                    'public.kr_candles_30m',
                    INTERVAL '90 days'
                );
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.kr_candles_5m') IS NOT NULL THEN
                EXECUTE $sql$
                    SELECT remove_continuous_aggregate_policy(
                        'public.kr_candles_5m',
                        if_exists => TRUE
                    )
                $sql$;
            END IF;

            IF to_regclass('public.kr_candles_15m') IS NOT NULL THEN
                EXECUTE $sql$
                    SELECT remove_continuous_aggregate_policy(
                        'public.kr_candles_15m',
                        if_exists => TRUE
                    )
                $sql$;
            END IF;

            IF to_regclass('public.kr_candles_30m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_30m',
                    if_exists => TRUE
                );
                EXECUTE $sql$
                    SELECT remove_continuous_aggregate_policy(
                        'public.kr_candles_30m',
                        if_exists => TRUE
                    )
                $sql$;
            END IF;

            IF to_regclass('public.kr_candles_15m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_15m',
                    if_exists => TRUE
                );
            END IF;

            IF to_regclass('public.kr_candles_5m') IS NOT NULL THEN
                PERFORM remove_retention_policy(
                    'public.kr_candles_5m',
                    if_exists => TRUE
                );
            END IF;
        END
        $$
        """
    )
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public.kr_candles_30m")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public.kr_candles_15m")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS public.kr_candles_5m")
