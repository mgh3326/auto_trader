"""ROB-284 — crypto instruments ORM model.

Master/source-of-truth for crypto venue/product/symbol identity. Candles
(`crypto_candles_1d`, `crypto_candles_1m`) reference this table by
`instrument_id` FK so identity is captured at the database layer rather
than via free-form `(symbol, market)` strings.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CryptoInstrument(Base):
    __tablename__ = "crypto_instruments"
    __table_args__ = (
        UniqueConstraint(
            "venue",
            "product",
            "venue_symbol",
            name="uq_crypto_instruments_venue_product_symbol",
        ),
        CheckConstraint(
            "status IN ('active','delisted','halted')",
            name="ck_crypto_instruments_status",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    product: Mapped[str] = mapped_column(Text, nullable=False)
    venue_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    base_asset: Mapped[str] = mapped_column(Text, nullable=False)
    quote_asset: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active"
    )
    precision_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    precision_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tick_size: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    lot_size: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    min_notional: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    listed_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    delisted_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # SQLAlchemy reserves the attribute name `metadata` on the declarative base,
    # so the Python attribute is `extra_metadata` and is explicitly mapped to
    # the DB column name `metadata` for parity with the migration.
    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
