"""Append-only records for the #1120 quotes:toss shadow consumer.

Both tables are evidence: every row is written once through the
``app.services.quotes_consumer`` repository with an ``INSERT … ON CONFLICT
DO NOTHING`` dedupe key, and production/test DDL rejects UPDATE, DELETE
and TRUNCATE.  Nothing here orders, kicks a session, or changes policy.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

QUOTE_SESSIONS_SQL = (
    "'nxt_pre','krx_regular','nxt_after','us_pre','us_regular','us_after'"
)


class QuotesTriggerFiring(Base):
    """One spike-trigger firing (or a ``not_evaluable`` verdict) per edge."""

    __tablename__ = "quotes_trigger_firings"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_quotes_trigger_firings_dedupe"),
        # Check names are short suffixes: the ck_%(table_name)s_%(constraint_name)s
        # convention renders ck_quotes_trigger_firings_<suffix>, identical to
        # the verbatim names the migration emits.
        CheckConstraint(
            "trigger_type IN ('index_spike','holding_spike','vi_proxy','own_fill')",
            name="type",
        ),
        CheckConstraint(
            "outcome IN ('fired','not_evaluable')",
            name="outcome",
        ),
        CheckConstraint(
            "market IS NULL OR market IN ('kr','us','crypto','other')",
            name="market",
        ),
        CheckConstraint(
            f"session IS NULL OR session IN ({QUOTE_SESSIONS_SQL})",
            name="session",
        ),
        CheckConstraint(
            "suppress_reason IS NULL OR suppress_reason IN ("
            "'daily_cap','cooldown','not_evaluable')",
            name="suppress",
        ),
        CheckConstraint(
            "NOT (outcome = 'fired' AND not_evaluable_reason IS NOT NULL)",
            name="ne_consistency",
        ),
        Index("ix_quotes_trigger_firings_type_ts", "trigger_type", "event_ts"),
        Index("ix_quotes_trigger_firings_symbol_ts", "symbol", "event_ts"),
        Index("ix_quotes_trigger_firings_kst_date", "kst_date"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    # DB-normalized join symbol ('*' for market-wide triggers like the index).
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    # The stream symbol verbatim; NULL for non-stream sources (ledger fills).
    source_symbol: Mapped[str | None] = mapped_column(Text)
    market: Mapped[str | None] = mapped_column(Text)
    session: Mapped[str | None] = mapped_column(Text)
    reference_price: Mapped[object | None] = mapped_column(Numeric(20, 8))
    current_price: Mapped[object | None] = mapped_column(Numeric(20, 8))
    window: Mapped[str] = mapped_column(Text, nullable=False)
    event_ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    kst_date: Mapped[str] = mapped_column(Text, nullable=False)
    would_kick: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    suppress_reason: Mapped[str | None] = mapped_column(Text)
    daily_would_kick_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    last_would_kick_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    not_evaluable_reason: Mapped[str | None] = mapped_column(Text)
    # Stream entry id (``XADD`` id) or ``execution_ledger`` id — provenance.
    source_ref: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class LadderTouchEvent(Base):
    """Approach / touch / fill evidence for one resting rung anchor."""

    __tablename__ = "ladder_touch_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_ladder_touch_events_dedupe"),
        CheckConstraint(
            "event_type IN ('approach','touch','fill')",
            name="type",
        ),
        CheckConstraint(
            "order_ledger IN ("
            "'kis_live_order_ledger','toss_live_order_ledger',"
            "'live_order_ledger')",
            name="ledger",
        ),
        CheckConstraint("side IN ('buy','sell')", name="side"),
        CheckConstraint("market IN ('kr','us','crypto')", name="market"),
        CheckConstraint(
            f"session IS NULL OR session IN ({QUOTE_SESSIONS_SQL})",
            name="session",
        ),
        Index(
            "ix_ladder_touch_events_rung",
            "order_ledger",
            "order_ledger_id",
        ),
        Index("ix_ladder_touch_events_symbol_ts", "symbol", "event_ts"),
        Index("ix_ladder_touch_events_type_ts", "event_type", "event_ts"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    # The rung's resting order row (table name + row id).
    order_ledger: Mapped[str] = mapped_column(Text, nullable=False)
    order_ledger_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    client_order_id: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    session: Mapped[str | None] = mapped_column(Text)
    anchor_price: Mapped[object] = mapped_column(Numeric(20, 8), nullable=False)
    event_price: Mapped[object] = mapped_column(Numeric(20, 8), nullable=False)
    distance_pct: Mapped[object | None] = mapped_column(Numeric(10, 6))
    event_ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    # Order facts, copied from ledger evidence only (comment 956); NULL when
    # the ledger cannot prove them — never synthesized.
    received_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    nxt_tradable: Mapped[bool | None] = mapped_column(Boolean)
    died_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    stream_entry_id: Mapped[str | None] = mapped_column(Text)
    fill_ledger_id: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
