"""add_us_candles_1d

Revision ID: 142b01f2eba0
Revises: 9f1a2b3c4d5e
Create Date: 2026-05-15 11:07:14.331675

"""

from collections.abc import Sequence

from alembic import op

revision: str = "142b01f2eba0"
down_revision: str | Sequence[str] | None = "9f1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
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
            v_parts := regexp_split_to_array(v_version_core, '\\.');

            v_major := COALESCE(v_parts[1], '0')::INTEGER;
            v_minor := COALESCE(v_parts[2], '0')::INTEGER;
            v_patch := COALESCE(v_parts[3], '0')::INTEGER;

            IF (v_major, v_minor, v_patch) < (2, 15, 0) THEN
                RAISE EXCEPTION
                    'timescaledb extension version % is below required minimum 2.15.0',
                    v_extversion;
            END IF;
        END
        $$
        """
    )

    op.execute(
        """
        CREATE TABLE public.us_candles_1d (
            time TIMESTAMPTZ NOT NULL,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            open NUMERIC NOT NULL,
            high NUMERIC NOT NULL,
            low NUMERIC NOT NULL,
            close NUMERIC NOT NULL,
            adj_close NUMERIC,
            volume NUMERIC NOT NULL,
            value NUMERIC NOT NULL,
            source TEXT NOT NULL,
            ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_us_candles_1d_exchange CHECK (exchange IN ('NASD', 'NYSE', 'AMEX')),
            CONSTRAINT uq_us_candles_1d_time_symbol_exchange UNIQUE (time, symbol, exchange)
        )
        """
    )

    op.execute(
        """
        SELECT create_hypertable(
            'public.us_candles_1d',
            'time',
            chunk_time_interval => INTERVAL '90 days',
            migrate_data => TRUE
        )
        """
    )

    op.execute(
        """
        CREATE INDEX ix_us_candles_1d_symbol_exchange_time_desc
            ON public.us_candles_1d (symbol, exchange, time DESC)
        """
    )

    op.execute(
        """
        CREATE INDEX ix_us_candles_1d_source_time
            ON public.us_candles_1d (source, time DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_us_candles_1d_source_time")
    op.execute("DROP INDEX IF EXISTS public.ix_us_candles_1d_symbol_exchange_time_desc")
    op.execute("DROP TABLE IF EXISTS public.us_candles_1d")
