"""ROB-313 — ORM model for ``scalp_trade_analytics``.

One row per **reconciled round-trip** of the Binance Demo scalping loop
(open leg + close leg), keyed by the open leg's ``client_order_id`` (which
is the ``parent_client_order_id`` of the close row in
``binance_demo_order_ledger``).

This table is **analytics only** — the order lifecycle stays in
``binance_demo_order_ledger`` (its contract is untouched). All writes go
through ``ScalpTradeAnalyticsService``.

Slippage is captured exactly (fill vs reference); fees are estimated from
``fee_rate_bps`` (stored per row for auditability) — see ROB-313 D3. Exit
fields are nullable: a close-leg anomaly records a row with no exit
price/PnL rather than a fake success.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ScalpTradeAnalytics(Base):
    """Per-round-trip cost/analytics record for Binance Demo scalping."""

    __tablename__ = "scalp_trade_analytics"
    __table_args__ = (
        UniqueConstraint(
            "open_client_order_id",
            name="uq_scalp_analytics_open_client_order_id",
        ),
        CheckConstraint(
            "product IN ('spot','usdm_futures')",
            name="scalp_analytics_product",
        ),
        CheckConstraint("side IN ('BUY','SELL')", name="scalp_analytics_side"),
        Index("ix_scalp_analytics_instrument_id", "instrument_id"),
        Index("ix_scalp_analytics_product", "product"),
        Index("ix_scalp_analytics_created_at", "created_at"),
        Index("ix_scalp_analytics_exit_reason", "exit_reason"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Identity: the open leg's client_order_id == close leg's parent_client_order_id.
    open_client_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    close_client_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    instrument_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "crypto_instruments.id",
            name="fk_scalp_analytics_instrument_id_crypto_instruments",
        ),
        nullable=False,
    )
    product: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    # Entry side (BUY = long entry, SELL = short entry).
    side: Mapped[str] = mapped_column(Text, nullable=False)

    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    entry_notional_usdt: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), nullable=True
    )

    # Fees (estimated from fee_rate_bps; the rate used is stored for audit).
    fee_rate_bps: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    entry_fee_usdt: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), nullable=True
    )
    exit_fee_usdt: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)

    # Execution quality (exact, fill vs reference). Adverse = positive.
    entry_slippage_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    exit_slippage_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    entry_spread_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    exit_spread_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )

    # Price-path diagnostics over the hold (from the bounded monitor poll).
    mae_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    mfe_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    # Performance.
    gross_pnl_usdt: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), nullable=True
    )
    net_pnl_usdt: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    net_return_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )

    holding_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # take_profit | stop_loss | timeout | monitor_error | immediate | anomaly
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_tag: Mapped[str | None] = mapped_column(Text, nullable=True)

    signal_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
