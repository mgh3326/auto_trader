"""hk 6145: session minute timeline storage (research schema).

Revision ID: 20261124_hk6145_session_minutes
Revises: 20261122_hk6026_market_event
Create Date: 2026-11-24

Additive. ``public.*_candles_1m`` are untouched.

1. ``research.kr_candles_1m.session_segment`` CHECK also admits ``KRX_AFTER``
   (KRX venue 16:00-20:00 KST).
2. ``research.us_candles_1m`` — same shape as the KR table, adapted to US:
   ``time_utc`` is the bar START, ``session_date`` is the ET trading day
   (20:00 ET rollover), venue NASD|NYSE|AMEX, session_segment US_PRE|US_REGULAR|
   US_POST|US_DAY|UNKNOWN, source KIS|TOSS|YAHOO|UNKNOWN, ``retrieved_at`` is
   the first receipt (the collector writes ON CONFLICT DO NOTHING).
   Hypertable and continuous aggregates only when timescaledb exists
   (same guard as 20260803). No retention policy, no refresh policy.
3. Continuous aggregates ``research.us_candles_{5m,15m,30m,1h}`` — bucket START,
   GROUP BY bucket, symbol, venue, session_segment, so a bucket never mixes
   sessions.
4. Conditional ``at_app`` grants (hard rule 18): SELECT, INSERT on both
   research minute tables, SELECT on the four continuous aggregates (only with
   timescaledb) and USAGE on schema research, only if the role exists.
5. ``research.kr_candles_1m.value`` drops NOT NULL: KIS reports the day-
   cumulative trade value, so a per-minute value is only stored when it can be
   proven; otherwise NULL (the bar is kept). Continuous-aggregate ``SUM(value)``
   ignores NULL, so a bucket with NULL minutes is a partial sum.
6. The widened/narrowed ``session_segment`` CHECK is added NOT VALID (no scan
   under ACCESS EXCLUSIVE on the deep-history hypertable).

Downgrade refuses while ``research.us_candles_1m`` has rows, while
``research.kr_candles_1m`` has a ``KRX_AFTER`` row (it would discard receipts)
or while it has a row with a NULL ``value`` (NOT NULL cannot be restored).
"""

# expand-contract: contract-step 6145
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261124_hk6145_session_minutes"
down_revision: str | Sequence[str] | None = "20261122_hk6026_market_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KR_CHECK = "ck_research_kr_candles_1m_session_segment"
KR_SEGMENTS_OLD = ("KRX_REGULAR", "NXT_PRE", "NXT_OVERLAP", "NXT_POST", "UNKNOWN")
KR_SEGMENTS_NEW = (*KR_SEGMENTS_OLD, "KRX_AFTER")

US_VENUES = ("NASD", "NYSE", "AMEX")
US_SEGMENTS = ("US_PRE", "US_REGULAR", "US_POST", "US_DAY", "UNKNOWN")
US_SOURCES = ("KIS", "TOSS", "YAHOO", "UNKNOWN")

US_CAGG_SPECS = (
    ("us_candles_5m", "5 minutes"),
    ("us_candles_15m", "15 minutes"),
    ("us_candles_30m", "30 minutes"),
    ("us_candles_1h", "1 hour"),
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _cagg_sql(view_name: str, interval: str) -> str:
    return f"""
        CREATE MATERIALIZED VIEW research.{view_name}
        WITH (
            timescaledb.continuous,
            timescaledb.materialized_only = false
        )
        AS
        SELECT
            time_bucket(
                INTERVAL '{interval}', time_utc, 'America/New_York'
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
        FROM research.us_candles_1m
        GROUP BY bucket, symbol, venue, session_segment
        WITH NO DATA
    """


def _if_kr_table(*statements: str) -> str:
    """DO block that runs ``statements`` only if research.kr_candles_1m exists.

    Production has the table (migration 20260803); a scratch database that was
    stamped past it (CI chain tests) does not, and must not fail here.
    """
    body = "\n".join(f"EXECUTE $kr${statement}$kr$;" for statement in statements)
    return f"""
        DO $$
        BEGIN
            IF to_regclass('research.kr_candles_1m') IS NOT NULL THEN
                {body}
            END IF;
        END
        $$
        """


def _set_kr_segment_check(values: Sequence[str]) -> None:
    # NOT VALID: no chunk scan while ACCESS EXCLUSIVE is held (the table is the
    # no-retention deep-history hypertable, a promotion target). New and updated
    # rows are checked immediately. Existing rows already satisfy the old CHECK,
    # which is a subset of the widened one, and the downgrade guard proves there
    # is no KRX_AFTER row before narrowing, so no VALIDATE revision is needed.
    op.execute(
        _if_kr_table(
            f"ALTER TABLE research.kr_candles_1m DROP CONSTRAINT IF EXISTS {KR_CHECK}",
            f"ALTER TABLE research.kr_candles_1m ADD CONSTRAINT {KR_CHECK} "
            f"CHECK (session_segment IN ({_quoted(values)})) NOT VALID",
        )
    )


def upgrade() -> None:
    _set_kr_segment_check(KR_SEGMENTS_NEW)
    op.execute(
        _if_kr_table(
            "ALTER TABLE research.kr_candles_1m ALTER COLUMN value DROP NOT NULL"
        )
    )

    op.execute("CREATE SCHEMA IF NOT EXISTS research")
    op.execute(
        f"""
        CREATE TABLE research.us_candles_1m (
            time_utc TIMESTAMPTZ NOT NULL,
            session_date DATE NOT NULL,
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
            CONSTRAINT ck_research_us_candles_1m_venue
                CHECK (venue IN ({_quoted(US_VENUES)})),
            CONSTRAINT ck_research_us_candles_1m_session_segment
                CHECK (session_segment IN ({_quoted(US_SEGMENTS)})),
            CONSTRAINT ck_research_us_candles_1m_source
                CHECK (source IN ({_quoted(US_SOURCES)})),
            CONSTRAINT pk_research_us_candles_1m
                PRIMARY KEY (time_utc, symbol, venue)
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable('research.us_candles_1m', 'time_utc');
                PERFORM set_chunk_time_interval(
                    'research.us_candles_1m', INTERVAL '7 days'
                );
            ELSE
                RAISE WARNING
                    'timescaledb absent: research.us_candles_1m created as a plain '
                    'table; hypertable and continuous aggregates skipped';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "CREATE INDEX ix_research_us_candles_1m_symbol_time_desc "
        "ON research.us_candles_1m (symbol, time_utc DESC)"
    )
    op.execute(
        "CREATE INDEX ix_research_us_candles_1m_session_date "
        "ON research.us_candles_1m (session_date, symbol)"
    )
    op.execute(
        "CREATE INDEX ix_research_us_candles_1m_source_batch "
        "ON research.us_candles_1m (source, batch_id)"
    )

    # No retention policy and no refresh policy: deep history, explicit refresh.
    for view_name, interval in US_CAGG_SPECS:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                    EXECUTE $sql${_cagg_sql(view_name, interval)}$sql$;
                END IF;
            END
            $$
            """
        )

    # Hard rule 18: conditional grants, the role may be absent in dev/CI.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT USAGE ON SCHEMA research TO at_app;
                GRANT SELECT, INSERT ON research.us_candles_1m TO at_app;
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('research.kr_candles_1m') IS NOT NULL
               AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT ON research.kr_candles_1m TO at_app;
            END IF;
        END
        $$
        """
    )
    # The continuous aggregates exist only with timescaledb: SELECT for at_app.
    for view_name, _ in US_CAGG_SPECS:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')
                   AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                    GRANT SELECT ON research.{view_name} TO at_app;
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
            IF EXISTS (SELECT 1 FROM research.us_candles_1m LIMIT 1) THEN
                RAISE EXCEPTION
                    'cannot drop research.us_candles_1m: rows exist (first-receipt times would be lost)'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF to_regclass('research.kr_candles_1m') IS NOT NULL AND EXISTS (
                SELECT 1 FROM research.kr_candles_1m
                WHERE session_segment = 'KRX_AFTER' LIMIT 1
            ) THEN
                RAISE EXCEPTION
                    'cannot narrow session_segment: KRX_AFTER rows exist'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF to_regclass('research.kr_candles_1m') IS NOT NULL AND EXISTS (
                SELECT 1 FROM research.kr_candles_1m WHERE value IS NULL LIMIT 1
            ) THEN
                RAISE EXCEPTION
                    'cannot restore NOT NULL: rows with NULL value exist'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END
        $$
        """
    )
    for view_name, _ in US_CAGG_SPECS:
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS research.{view_name}")
    op.execute("DROP TABLE research.us_candles_1m")
    op.execute(
        _if_kr_table(
            "ALTER TABLE research.kr_candles_1m ALTER COLUMN value SET NOT NULL"
        )
    )
    _set_kr_segment_check(KR_SEGMENTS_OLD)
