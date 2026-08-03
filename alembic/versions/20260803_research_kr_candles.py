"""research.kr_candles_1m deep-history store + venue-preserving caggs + promotion watermark.

Additive only. Production ``public.kr_candles_1m``, its 90-day retention policy,
and the four existing public caggs are **not touched** by this migration.

Design source: herdr-inbox/answer-codexmock-research-db-1805.md (C1 hybrid).

Key points that differ from the production table, deliberately:

* ``source`` (KIWOOM|KIS|TOSS) is separate from ``venue`` (KRX|NTX). A provider
  is not a venue — Toss data is not automatically NTX (R-3).
* ``session_segment`` fails closed to ``UNKNOWN`` when the provider cannot prove
  which session a bar belongs to, rather than guessing KRX_REGULAR.
* research caggs keep ``venue`` and ``session_segment`` in the GROUP BY. The
  public caggs collapse KRX/NTX into one bucket and keep only a ``venues``
  array, so they cannot be promoted as-is for KRX-regular-only research (R-3).
* no retention policy on the research raw table or its caggs — deep history is
  the entire point.
* no continuous-aggregate refresh policy: historical backfill is not
  materialised by a 2-day refresh window anyway, so refresh stays an explicit,
  operator-invoked step instead of a new background job.

Identity is ``(time_utc, symbol, venue)`` — NOT including ``source``. Two
providers reporting the same bar must reconcile to one row; a disagreement is
isolated as a conflict artifact rather than silently overwriting (R-2).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260803_research_kr_candles"
down_revision: str | Sequence[str] | None = "20260803_kis_mock_signal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


VENUES = ("KRX", "NTX")
SESSION_SEGMENTS = (
    "KRX_REGULAR",
    "NXT_PRE",
    "NXT_OVERLAP",
    "NXT_POST",
    "UNKNOWN",
)
#: Provider that supplied the row. ``UNKNOWN`` is required, not optional:
#: production ``public.kr_candles_1m`` has no provider column, so rows promoted
#: out of it cannot be honestly attributed to KIWOOM/KIS/TOSS. Stamping a guess
#: would fabricate exactly the provenance this schema exists to preserve, so
#: promotion fails closed to UNKNOWN — the same rule R-3 applies to
#: ``session_segment`` when a provider cannot prove the venue.
SOURCES = ("KIWOOM", "KIS", "TOSS", "UNKNOWN")

CAGG_SPECS = (
    ("kr_candles_5m", "5 minutes"),
    ("kr_candles_15m", "15 minutes"),
    ("kr_candles_30m", "30 minutes"),
    ("kr_candles_1h", "1 hour"),
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS research")

    # ---- raw deep-history table -------------------------------------
    op.execute(
        f"""
        CREATE TABLE research.kr_candles_1m (
            time_utc TIMESTAMPTZ NOT NULL,
            session_date_kst DATE NOT NULL,
            symbol TEXT NOT NULL,
            venue TEXT NOT NULL,
            session_segment TEXT NOT NULL,
            source TEXT NOT NULL,
            open NUMERIC NOT NULL,
            high NUMERIC NOT NULL,
            low NUMERIC NOT NULL,
            close NUMERIC NOT NULL,
            volume NUMERIC NOT NULL,
            value NUMERIC NOT NULL,
            retrieved_at TIMESTAMPTZ NOT NULL,
            batch_id TEXT NOT NULL,
            CONSTRAINT ck_research_kr_candles_1m_venue
                CHECK (venue IN ({_quoted(VENUES)})),
            CONSTRAINT ck_research_kr_candles_1m_session_segment
                CHECK (session_segment IN ({_quoted(SESSION_SEGMENTS)})),
            CONSTRAINT ck_research_kr_candles_1m_source
                CHECK (source IN ({_quoted(SOURCES)})),
            CONSTRAINT uq_research_kr_candles_1m_time_symbol_venue
                UNIQUE (time_utc, symbol, venue)
        )
        """
    )

    # Hypertable conversion is conditional on the timescaledb extension.
    #
    # Production has it (2.26.3). CI does not: the test image is
    # postgres:15-alpine, and tests/services/paper_cohort/test_migration.py
    # stamps at 20260714_rob849_paper_cohort and runs `upgrade head`, so this is
    # the first timescale-dependent migration CI actually executes — the older
    # kr_candles timescale migration sits before that stamp and never runs.
    #
    # The plain table above is always created, so the schema is usable either
    # way. Where the extension is missing we skip the hypertable and the caggs
    # and raise a WARNING rather than failing the chain or pretending success.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable('research.kr_candles_1m', 'time_utc');
                PERFORM set_chunk_time_interval(
                    'research.kr_candles_1m', INTERVAL '7 days'
                );
            ELSE
                RAISE WARNING
                    'timescaledb absent: research.kr_candles_1m created as a plain '
                    'table; hypertable and continuous aggregates skipped';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "CREATE INDEX ix_research_kr_candles_1m_symbol_time_desc "
        "ON research.kr_candles_1m (symbol, time_utc DESC)"
    )
    op.execute(
        "CREATE INDEX ix_research_kr_candles_1m_session_date "
        "ON research.kr_candles_1m (session_date_kst, symbol)"
    )
    op.execute(
        "CREATE INDEX ix_research_kr_candles_1m_source_batch "
        "ON research.kr_candles_1m (source, batch_id)"
    )

    # ---- venue/session-preserving continuous aggregates --------------
    # Newly defined, NOT copied from public: the public caggs merge KRX and NTX
    # into a single bucket, which destroys the distinction research needs.
    for view_name, interval in CAGG_SPECS:
        # Continuous aggregates need timescaledb; same conditional as above.
        # $sql$ quoting keeps the inner statement readable inside the DO block.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                    EXECUTE $sql$
                        CREATE MATERIALIZED VIEW research.{view_name}
                        WITH (
                            timescaledb.continuous,
                            timescaledb.materialized_only = false
                        )
                        AS
                        SELECT
                            time_bucket(
                                INTERVAL '{interval}', time_utc, 'Asia/Seoul'
                            ) AS bucket,
                            symbol,
                            venue,
                            session_segment,
                            FIRST(open, time_utc) AS open,
                            MAX(high) AS high,
                            MIN(low) AS low,
                            LAST(close, time_utc) AS close,
                            SUM(volume) AS volume,
                            SUM(value) AS value,
                            COUNT(*) AS bar_count
                        FROM research.kr_candles_1m
                        GROUP BY bucket, symbol, venue, session_segment
                        WITH NO DATA
                    $sql$;
                END IF;
            END
            $$
            """
        )

    # ---- promotion watermark ----------------------------------------
    op.create_table(
        "kr_candle_promotion_watermark",
        sa.Column("source", sa.String(16), primary_key=True),
        sa.Column("venue", sa.String(8), primary_key=True),
        sa.Column("last_promoted_session_date_kst", sa.Date(), nullable=True),
        sa.Column("last_promoted_time_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rows_promoted_total", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"venue IN ({_quoted(VENUES)})", name="ck_promotion_watermark_venue"
        ),
        sa.CheckConstraint(
            f"source IN ({_quoted(SOURCES)})", name="ck_promotion_watermark_source"
        ),
        schema="research",
    )

    # ---- conflict quarantine ----------------------------------------
    # A disagreement between an already-stored bar and an incoming one is
    # isolated here. It is never resolved by overwriting: an unresolved row
    # blocks snapshot sealing (R-2).
    op.create_table(
        "kr_candle_promotion_conflicts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("time_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("venue", sa.String(8), nullable=False),
        sa.Column("existing_source", sa.String(16), nullable=False),
        sa.Column("incoming_source", sa.String(16), nullable=False),
        sa.Column("existing_values", sa.JSON(), nullable=False),
        sa.Column("incoming_values", sa.JSON(), nullable=False),
        sa.Column("batch_id", sa.String(128), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        schema="research",
    )
    op.create_index(
        "ix_research_kr_candle_conflicts_unresolved",
        "kr_candle_promotion_conflicts",
        ["resolved_at"],
        unique=False,
        schema="research",
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    # Drops only objects this migration created. Production public.kr_candles_1m,
    # its retention policy and the public caggs are never referenced here.
    op.drop_index(
        "ix_research_kr_candle_conflicts_unresolved",
        table_name="kr_candle_promotion_conflicts",
        schema="research",
    )
    op.drop_table("kr_candle_promotion_conflicts", schema="research")
    op.drop_table("kr_candle_promotion_watermark", schema="research")
    for view_name, _ in CAGG_SPECS:
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS research.{view_name}")
    op.execute("DROP TABLE IF EXISTS research.kr_candles_1m")
    # The research schema itself is left in place: it predates this migration
    # (ROB-848 paper validation) and dropping it would take unrelated tables.
