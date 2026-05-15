"""add_kr_candles_1d

Revision ID: bad6e17e4115
Revises: 142b01f2eba0
Create Date: 2026-05-15 11:07:58.721821

"""

from collections.abc import Sequence

from alembic import op

revision: str = "bad6e17e4115"
down_revision: str | Sequence[str] | None = "142b01f2eba0"
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
        CREATE TABLE public.kr_candles_1d (
            time TIMESTAMPTZ NOT NULL,
            symbol TEXT NOT NULL,
            venue TEXT NOT NULL,
            open NUMERIC NOT NULL,
            high NUMERIC NOT NULL,
            low NUMERIC NOT NULL,
            close NUMERIC NOT NULL,
            volume NUMERIC NOT NULL,
            value NUMERIC NOT NULL,
            source TEXT NOT NULL,
            ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_kr_candles_1d_venue CHECK (venue IN ('KRX', 'NTX')),
            CONSTRAINT uq_kr_candles_1d_time_symbol_venue UNIQUE (time, symbol, venue)
        )
        """
    )

    op.execute(
        """
        SELECT create_hypertable(
            'public.kr_candles_1d',
            'time',
            chunk_time_interval => INTERVAL '90 days',
            migrate_data => TRUE
        )
        """
    )

    op.execute(
        """
        CREATE INDEX ix_kr_candles_1d_symbol_venue_time_desc
            ON public.kr_candles_1d (symbol, venue, time DESC)
        """
    )

    op.execute(
        """
        CREATE INDEX ix_kr_candles_1d_source_time
            ON public.kr_candles_1d (source, time DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_kr_candles_1d_source_time")
    op.execute("DROP INDEX IF EXISTS public.ix_kr_candles_1d_symbol_venue_time_desc")
    op.execute("DROP TABLE IF EXISTS public.kr_candles_1d")
