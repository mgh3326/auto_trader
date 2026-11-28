"""Append-only bounded backfill requests for official events (hk 6409).

One request is recorded once as a ``requested`` row; running it appends a
``started`` claim and then one ``done`` or ``failed`` row. No row is ever
updated. ``app/services/market_events/backfill/store.py`` is the only writer;
the database triggers are installed by
``alembic/versions/20261128_hk6409_backfill_requests.py``.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: NULL-safe on purpose: a CHECK that evaluates to NULL passes.
RESULT_CHECK = """
(state IN ('requested', 'started') AND exit_code IS NULL AND failure_code IS NULL)
OR (state = 'done' AND exit_code IS NOT NULL AND exit_code = 0 AND failure_code IS NULL)
OR (state = 'failed' AND failure_code IS NOT NULL AND (
    (failure_code = 'exit_nonzero' AND exit_code IS NOT NULL AND exit_code <> 0)
    OR (failure_code IN ('timeout', 'spawn_failed') AND exit_code IS NULL)
))
"""


class MarketEventBackfillRequest(Base):
    __tablename__ = "market_event_backfill_requests"
    __table_args__ = (
        UniqueConstraint(
            "request_hash", "state", name="uq_market_event_backfill_requests_state"
        ),
        CheckConstraint(
            "state IN ('requested', 'started', 'done', 'failed')", name="state"
        ),
        CheckConstraint(
            "from_date <= to_date AND to_date - from_date < 31", name="window"
        ),
        CheckConstraint("cardinality(symbols) <= 20", name="symbols_bound"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="request_hash"),
        CheckConstraint(
            "btrim(reason) <> '' AND btrim(requested_by) <> ''", name="audit_nonblank"
        ),
        CheckConstraint(RESULT_CHECK, name="result"),
        Index(
            "ix_market_event_backfill_requests_quota",
            "source",
            "state",
            "created_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_ref: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    symbols: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'::text[]")
    )
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    failure_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
