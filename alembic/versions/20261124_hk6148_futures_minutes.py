"""hk 6148: research futures 1-minute bars (KOSPI200 / mini KOSPI200 futures).

Revision ID: 20261124_hk6148_futures_min
Revises: 20261124_hk6145_session_minutes
Create Date: 2026-11-24

Additive: one new table, nothing else is touched.

``research.kr_futures_candles_1m`` — insert-only closed 1-minute bars of one
contract (``symbol`` = ``<PRODUCT>.<YYMM>``, ``contract_month`` = ``YYMM``):

* ``time_utc`` is the bar START; ``retrieved_at`` is the first receipt (the
  collector writes ON CONFLICT DO NOTHING).
* ``roll_state`` front|next|expiring|expired, as of the bar's trading date.
* ``session_segment`` FUT_DAY|FUT_NIGHT|UNKNOWN; ``session_date`` is the trading
  date (night bars carry the NEXT day-session date) and NULL when the calendar
  cannot confirm it — never guessed.
* No ``value`` column: KIS ``acml_tr_pbmn`` is cumulative (see hk 6145) and no
  per-minute amount is proven. No ``open_interest`` column: the minute-chart TR
  carries open interest only in its header block, not per bar.
* Hypertable only when timescaledb exists (same guard as 20260803), no retention
  and no refresh policy.
* Conditional ``at_app`` grants (hard rule 18): SELECT, INSERT, USAGE on schema.

Downgrade refuses while the table has rows (first-receipt times would be lost).
"""

# expand-contract: contract-step 6148
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261124_hk6148_futures_min"
down_revision: str | Sequence[str] | None = "20261124_hk6145_session_minutes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEGMENTS = ("FUT_DAY", "FUT_NIGHT", "UNKNOWN")
SOURCES = ("KIS", "UNKNOWN")
ROLL_STATES = ("front", "next", "expiring", "expired")


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS research")
    op.execute(
        f"""
        CREATE TABLE research.kr_futures_candles_1m (
            time_utc TIMESTAMPTZ NOT NULL,
            symbol TEXT NOT NULL,
            product TEXT NOT NULL,
            contract_month TEXT NOT NULL,
            roll_state TEXT NOT NULL,
            session_segment TEXT NOT NULL,
            session_date DATE,
            source TEXT NOT NULL,
            open NUMERIC NOT NULL,
            high NUMERIC NOT NULL,
            low NUMERIC NOT NULL,
            close NUMERIC NOT NULL,
            volume NUMERIC NOT NULL,
            retrieved_at TIMESTAMPTZ NOT NULL,
            batch_id TEXT NOT NULL,
            CONSTRAINT ck_research_kr_futures_candles_1m_contract_month
                CHECK (contract_month ~ '^[0-9]{{4}}$'),
            CONSTRAINT ck_research_kr_futures_candles_1m_roll_state
                CHECK (roll_state IN ({_quoted(ROLL_STATES)})),
            CONSTRAINT ck_research_kr_futures_candles_1m_session_segment
                CHECK (session_segment IN ({_quoted(SEGMENTS)})),
            CONSTRAINT ck_research_kr_futures_candles_1m_source
                CHECK (source IN ({_quoted(SOURCES)})),
            CONSTRAINT pk_research_kr_futures_candles_1m
                PRIMARY KEY (time_utc, symbol)
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable('research.kr_futures_candles_1m', 'time_utc');
                PERFORM set_chunk_time_interval(
                    'research.kr_futures_candles_1m', INTERVAL '30 days'
                );
            ELSE
                RAISE WARNING
                    'timescaledb absent: research.kr_futures_candles_1m created as a '
                    'plain table';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "CREATE INDEX ix_research_kr_futures_candles_1m_symbol_time_desc "
        "ON research.kr_futures_candles_1m (symbol, time_utc DESC)"
    )
    op.execute(
        "CREATE INDEX ix_research_kr_futures_candles_1m_session_date "
        "ON research.kr_futures_candles_1m (session_date, symbol)"
    )
    op.execute(
        "CREATE INDEX ix_research_kr_futures_candles_1m_source_batch "
        "ON research.kr_futures_candles_1m (source, batch_id)"
    )
    # Hard rule 18: conditional grants, the role may be absent in dev/CI.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT USAGE ON SCHEMA research TO at_app;
                GRANT SELECT, INSERT ON research.kr_futures_candles_1m TO at_app;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    # Held through the emptiness check and the DROP (same transaction): a collector
    # INSERT cannot commit between them and be dropped with its first-receipt time.
    op.execute("LOCK TABLE research.kr_futures_candles_1m IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM research.kr_futures_candles_1m LIMIT 1) THEN
                RAISE EXCEPTION
                    'cannot drop research.kr_futures_candles_1m: rows exist (first-receipt times would be lost)'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END
        $$
        """
    )
    op.execute("DROP TABLE research.kr_futures_candles_1m")
