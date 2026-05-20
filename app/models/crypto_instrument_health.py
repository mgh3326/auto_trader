"""ROB-285 — crypto_instrument_health ORM model.

Instrument-level health state for the Binance public adapter. All writes
go through ``app.services.instrument_health.service.CryptoInstrumentHealthService``
— the table is service-internal (no direct SQL writes outside the
``instrument_health`` package). See the runbook at
``docs/runbooks/binance-public-market-data.md`` for the state lifecycle.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CryptoInstrumentHealth(Base):
    __tablename__ = "crypto_instrument_health"
    __table_args__ = (
        CheckConstraint(
            "state IN ('healthy','degraded','rate_limited','manual_backfill_required')",
            # ``Base.metadata.naming_convention`` produces the final
            # ``ck_crypto_instrument_health_state`` name from this fragment.
            name="state",
        ),
    )

    instrument_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "crypto_instruments.id",
            name="fk_crypto_instrument_health_instrument_id_crypto_instruments",
        ),
        primary_key=True,
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="healthy"
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_state_change_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    last_closed_candle_time: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    retry_after_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # SQLAlchemy reserves the attribute name ``metadata`` on the declarative
    # base; map the Python attribute ``extra_metadata`` to the DB column
    # name ``metadata`` to keep parity with the migration.
    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
