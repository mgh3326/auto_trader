"""ROB-313 — write/read surface for ``scalp_trade_analytics``.

Thin service: persistence only. Cost figures are computed by the pure
``cost`` module and passed in; this service does not call the broker or
compute anything. One row per reconciled round-trip.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scalp_trade_analytics import ScalpTradeAnalytics


class ScalpTradeAnalyticsService:
    """Service-layer writes/reads for ``scalp_trade_analytics``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        open_client_order_id: str,
        instrument_id: int,
        product: str,
        symbol: str,
        side: str,
        qty: Decimal,
        entry_price: Decimal | None,
        entry_notional_usdt: Decimal | None,
        fee_rate_bps: Decimal | None,
        now: dt.datetime,
        close_client_order_id: str | None = None,
        exit_price: Decimal | None = None,
        entry_fee_usdt: Decimal | None = None,
        exit_fee_usdt: Decimal | None = None,
        entry_slippage_bps: Decimal | None = None,
        exit_slippage_bps: Decimal | None = None,
        entry_spread_bps: Decimal | None = None,
        exit_spread_bps: Decimal | None = None,
        mae_bps: Decimal | None = None,
        mfe_bps: Decimal | None = None,
        gross_pnl_usdt: Decimal | None = None,
        net_pnl_usdt: Decimal | None = None,
        net_return_bps: Decimal | None = None,
        holding_seconds: int | None = None,
        exit_reason: str | None = None,
        session_tag: str | None = None,
        signal_snapshot: dict[str, Any] | None = None,
    ) -> ScalpTradeAnalytics:
        """Insert one round-trip analytics row. Exit fields stay NULL on a
        close-leg anomaly — never a fabricated success."""
        row = ScalpTradeAnalytics(
            open_client_order_id=open_client_order_id,
            close_client_order_id=close_client_order_id,
            instrument_id=instrument_id,
            product=product,
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            entry_notional_usdt=entry_notional_usdt,
            fee_rate_bps=fee_rate_bps,
            entry_fee_usdt=entry_fee_usdt,
            exit_fee_usdt=exit_fee_usdt,
            entry_slippage_bps=entry_slippage_bps,
            exit_slippage_bps=exit_slippage_bps,
            entry_spread_bps=entry_spread_bps,
            exit_spread_bps=exit_spread_bps,
            mae_bps=mae_bps,
            mfe_bps=mfe_bps,
            gross_pnl_usdt=gross_pnl_usdt,
            net_pnl_usdt=net_pnl_usdt,
            net_return_bps=net_return_bps,
            holding_seconds=holding_seconds,
            exit_reason=exit_reason,
            session_tag=session_tag,
            signal_snapshot=signal_snapshot,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_by_open_client_order_id(
        self, open_client_order_id: str
    ) -> ScalpTradeAnalytics | None:
        return await self._session.scalar(
            select(ScalpTradeAnalytics).where(
                ScalpTradeAnalytics.open_client_order_id == open_client_order_id
            )
        )
