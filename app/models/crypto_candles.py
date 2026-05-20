"""ROB-284 — crypto candle ORM models (1m + 1d).

Both tables reference `crypto_instruments(id)` as the source of truth for
venue/product/symbol identity. Identity for a row is `(instrument_id, time)`.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CryptoCandle1m(Base):
    __tablename__ = "crypto_candles_1m"
    # NOTE on naming: ``Base.metadata.naming_convention['ck']`` is
    # ``"ck_%(table_name)s_%(constraint_name)s"``, so the stem name passed
    # here is prefixed with ``ck_crypto_candles_1m_`` by SQLAlchemy at
    # ``create_all`` time. The alembic migration calls
    # ``create_check_constraint`` with the same final, fully-qualified
    # name, so both code paths produce identical constraint names on disk.
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "time", name="pk_crypto_candles_1m"),
        CheckConstraint("base_volume >= 0", name="base_volume_nn"),
        CheckConstraint(
            "quote_volume IS NULL OR quote_volume >= 0",
            name="quote_volume_nn",
        ),
        CheckConstraint(
            "trade_count IS NULL OR trade_count >= 0",
            name="trade_count_nn",
        ),
        CheckConstraint("vwap IS NULL OR vwap >= 0", name="vwap_nn"),
        CheckConstraint("high >= low", name="high_ge_low"),
        CheckConstraint(
            "high >= open AND high >= close", name="high_ge_oc"
        ),
        CheckConstraint("low <= open AND low <= close", name="low_le_oc"),
    )

    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("crypto_instruments.id"), nullable=False
    )
    time: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    open: Mapped[float] = mapped_column(Numeric, nullable=False)
    high: Mapped[float] = mapped_column(Numeric, nullable=False)
    low: Mapped[float] = mapped_column(Numeric, nullable=False)
    close: Mapped[float] = mapped_column(Numeric, nullable=False)
    base_volume: Mapped[float] = mapped_column(Numeric, nullable=False)
    quote_volume: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    trade_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vwap: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    taker_buy_base_volume: Mapped[float | None] = mapped_column(
        Numeric, nullable=True
    )
    taker_buy_quote_volume: Mapped[float | None] = mapped_column(
        Numeric, nullable=True
    )
    is_closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    ingested_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class CryptoCandle1d(Base):
    """ROB-284 — daily crypto candle, instrument-FK shape.

    The pre-ROB-284 schema (`time, symbol, market, open, high, low, close,
    volume, value, source`) is migrated in-place by the three-step alembic
    migrations (step 1 add nullable columns; step 2 seed instruments +
    backfill; step 3 finalize NOT NULL + drop legacy columns). This ORM
    model reflects the post-step-3 final shape and is what
    `Base.metadata.create_all` (used by the test fixture) produces.
    """

    __tablename__ = "crypto_candles_1d"
    # See CryptoCandle1m for naming-convention notes; final on-disk names
    # are produced by ``ck_%(table_name)s_%(constraint_name)s``.
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "time", name="pk_crypto_candles_1d"),
        CheckConstraint("base_volume >= 0", name="base_volume_nn"),
        CheckConstraint(
            "quote_volume IS NULL OR quote_volume >= 0",
            name="quote_volume_nn",
        ),
        CheckConstraint("high >= low", name="high_ge_low"),
        CheckConstraint(
            "high >= open AND high >= close", name="high_ge_oc"
        ),
        CheckConstraint("low <= open AND low <= close", name="low_le_oc"),
    )

    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("crypto_instruments.id"), nullable=False
    )
    time: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    open: Mapped[float] = mapped_column(Numeric, nullable=False)
    high: Mapped[float] = mapped_column(Numeric, nullable=False)
    low: Mapped[float] = mapped_column(Numeric, nullable=False)
    close: Mapped[float] = mapped_column(Numeric, nullable=False)
    base_volume: Mapped[float] = mapped_column(Numeric, nullable=False)
    quote_volume: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    is_closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    ingested_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
