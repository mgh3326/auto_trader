"""Toss combined-KRX/NXT research corpus (additive, staging-load target only).

Toss may return a combined KRX+NXT minute product.  It is therefore stored in
its own physical table rather than being mixed with the KRX-only research
shard.  ``session_segment`` is a KST clock-time label, not a venue claim; this
migration intentionally defines no ``venue`` column.

The migration creates schema objects only.  It neither reads from nor writes
to the staging Parquet corpus, and it does not promote any pre-NXT row.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260804_toss_phase2"
down_revision: str | Sequence[str] | None = "20260803_research_kr_candles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "kr_candles_1m_toss"
SESSION_SEGMENTS = ("NXT_PRE", "KRX_REGULAR", "NXT_POST", "UNKNOWN")
VALUE_SEMANTICS = "CLOSE_X_VOLUME_SYNTHETIC"
CAGG_SPECS = (
    ("kr_candles_5m_toss", "5 minutes"),
    ("kr_candles_15m_toss", "15 minutes"),
    ("kr_candles_30m_toss", "30 minutes"),
    ("kr_candles_1h_toss", "1 hour"),
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # The research schema already exists.  Do not alter the earlier KRX-only
    # table or any public relation; this table is the physical separation.
    op.execute(
        f"""
        CREATE TABLE research.{TABLE_NAME} (
            time_utc TIMESTAMPTZ NOT NULL,
            session_date_kst DATE NOT NULL,
            symbol TEXT NOT NULL,
            session_segment TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'TOSS',
            open NUMERIC NOT NULL,
            high NUMERIC NOT NULL,
            low NUMERIC NOT NULL,
            close NUMERIC NOT NULL,
            volume NUMERIC NOT NULL,
            value NUMERIC NOT NULL,
            value_semantics TEXT NOT NULL DEFAULT '{VALUE_SEMANTICS}',
            is_padding BOOLEAN NOT NULL,
            pre_nxt BOOLEAN,
            retrieved_at TIMESTAMPTZ NOT NULL,
            batch_id TEXT NOT NULL,
            CONSTRAINT ck_research_{TABLE_NAME}_session_segment
                CHECK (session_segment IN ({_quoted(SESSION_SEGMENTS)})),
            CONSTRAINT ck_research_{TABLE_NAME}_source
                CHECK (source = 'TOSS'),
            CONSTRAINT ck_research_{TABLE_NAME}_value_semantics
                CHECK (value_semantics = '{VALUE_SEMANTICS}'),
            CONSTRAINT uq_research_{TABLE_NAME}_time_symbol
                UNIQUE (time_utc, symbol)
        )
        """
    )
    op.execute(
        f"""
        COMMENT ON TABLE research.{TABLE_NAME} IS
        'Toss combined KRX+NXT minute corpus. This table deliberately has no '
        'venue column: session_segment is a KST time-of-day label, not a trade-'
        'venue assertion. Toss 15:30 bars omit closing-auction volume, so daily '
        'volume/value aggregation undercounts. is_padding=true is a volume-zero '
        'provider placeholder and must not be counted as a coverage gap. value is '
        'synthetic close*volume ({VALUE_SEMANTICS}), not exchange-reported trade '
        'value. pre_nxt NULL means UNKNOWN until an exact sourced NXT launch date '
        'and boundary validation exist; this migration performs no promotion. '
        'Staging-to-DB loading requires a separate approved operation.'
        """
    )
    op.execute(
        f"CREATE INDEX ix_research_{TABLE_NAME}_symbol_time_desc "
        f"ON research.{TABLE_NAME} (symbol, time_utc DESC)"
    )
    op.execute(
        f"CREATE INDEX ix_research_{TABLE_NAME}_session_date "
        f"ON research.{TABLE_NAME} (session_date_kst, symbol)"
    )
    op.execute(
        f"CREATE INDEX ix_research_{TABLE_NAME}_batch "
        f"ON research.{TABLE_NAME} (batch_id)"
    )

    # Match the existing research-candle fallback: plain PostgreSQL remains
    # usable in CI, while production TimescaleDB gets the dedicated hypertable
    # and four cagg objects.  No refresh policy is registered here.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable('research.{TABLE_NAME}', 'time_utc');
                PERFORM set_chunk_time_interval(
                    'research.{TABLE_NAME}', INTERVAL '7 days'
                );
            ELSE
                RAISE WARNING
                    'timescaledb absent: research.{TABLE_NAME} created as a plain '
                    'table; hypertable and continuous aggregates skipped';
            END IF;
        END
        $$
        """
    )

    for view_name, interval in CAGG_SPECS:
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
                            session_date_kst,
                            symbol,
                            session_segment,
                            source,
                            value_semantics,
                            pre_nxt,
                            FIRST(open, time_utc) AS open,
                            MAX(high) AS high,
                            MIN(low) AS low,
                            LAST(close, time_utc) AS close,
                            SUM(volume) AS volume,
                            SUM(value) AS value,
                            COUNT(*) AS bar_count,
                            BOOL_AND(is_padding) AS is_padding,
                            SUM(CASE WHEN is_padding THEN 1 ELSE 0 END)
                                AS padding_bar_count
                        FROM research.{TABLE_NAME}
                        GROUP BY
                            bucket,
                            session_date_kst,
                            symbol,
                            session_segment,
                            source,
                            value_semantics,
                            pre_nxt
                        WITH NO DATA
                    $sql$;
                END IF;
            END
            $$
            """
        )
        op.execute(
            f"""
            DO $$
            BEGIN
                IF to_regclass('research.{view_name}') IS NOT NULL THEN
                    EXECUTE $sql$
                        COMMENT ON MATERIALIZED VIEW research.{view_name} IS
                        'Derived from research.{TABLE_NAME}; preserves time-based session '
                        'labels and padding visibility. value remains synthetic close*volume; '
                        'no cagg refresh policy is installed.'
                    $sql$;
                END IF;
            END
            $$
            """
        )

    # The approved backfill role is explicitly allowlisted for only the new raw
    # table.  This fresh table has no serial/identity column, hence no sequence
    # exists to grant; granting an unrelated sequence would violate least
    # privilege.  The conditional keeps the migration chain runnable in CI
    # databases where the operator-owned login role is intentionally absent.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'auto_trader_kr_backfill'
            ) THEN
                EXECUTE
                    'GRANT SELECT, INSERT ON TABLE research.{TABLE_NAME} '
                    'TO auto_trader_kr_backfill';
            ELSE
                RAISE NOTICE
                    'auto_trader_kr_backfill absent; no grant applied in this database';
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    # Only objects created above are removed.  No existing table, cagg, role,
    # or default privilege is altered by either direction of this migration.
    for view_name, _ in CAGG_SPECS:
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS research.{view_name}")
    op.execute(f"DROP TABLE IF EXISTS research.{TABLE_NAME}")
