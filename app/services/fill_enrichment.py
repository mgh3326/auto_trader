"""Best-effort, fail-open enrichment for fill notifications.

브로커 직접 조회(env 자격)로 체결 시점 평단/포지션/실현손익 근사치를 얻는다.
어떤 예외도 알림을 막지 않는다(항상 None 반환으로 graceful).
"""

from __future__ import annotations

import logging

from app.models.manual_holdings import MarketType
from app.services.brokers.kis.client import KISClient
from app.services.fill_notification import FillEnrichment, FillOrder
from app.services.kis_holdings_service import get_kis_holding_for_ticker

logger = logging.getLogger(__name__)


async def fetch_fill_enrichment(order: FillOrder) -> FillEnrichment | None:
    try:
        if order.market_type in ("kr", "us"):
            return await _fetch_kis(order)
        if order.market_type == "crypto":
            return await _fetch_upbit(order)
    except Exception:
        logger.warning(
            "fill enrichment failed (fail-open): symbol=%s market=%s",
            order.symbol,
            order.market_type,
            exc_info=True,
        )
    return None


def _build(order: FillOrder, *, qty: float, avg: float) -> FillEnrichment | None:
    if qty <= 0 or avg <= 0:
        return None
    enr = FillEnrichment(position_qty=qty, position_avg_price=avg, is_approximate=True)
    if order.side == "ask":  # 매도 → 실현손익 근사치
        enr.realized_pnl_amount = (order.filled_price - avg) * order.filled_qty
        enr.realized_pnl_rate = (order.filled_price / avg - 1) * 100
    return enr


async def _fetch_kis(order: FillOrder) -> FillEnrichment | None:
    market = MarketType.KR if order.market_type == "kr" else MarketType.US
    holding = await get_kis_holding_for_ticker(KISClient(), order.symbol, market)
    return _build(
        order,
        qty=float(holding.get("quantity") or 0),
        avg=float(holding.get("avg_price") or 0),
    )


async def _fetch_upbit(order: FillOrder) -> FillEnrichment | None:
    from app.services.brokers.upbit.client import (
        fetch_my_coins,
        parse_upbit_account_row,
    )

    currency = order.symbol.split("-")[-1] if "-" in order.symbol else order.symbol
    accounts = await fetch_my_coins()
    for row in accounts:
        if str(row.get("currency", "")).upper() == currency.upper():
            parsed = parse_upbit_account_row(row)
            return _build(
                order,
                qty=float(parsed["total_quantity"]),
                avg=float(parsed["avg_buy_price"]),
            )
    return None
