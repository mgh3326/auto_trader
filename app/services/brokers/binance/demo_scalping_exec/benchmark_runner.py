"""Phase 1 — compute + store the daily buy&hold benchmark for a scalping review.

Bridges the Demo market-data client (day open/close klines) and the pure
benchmark math to the review service's storage. Lives under
``demo_scalping_exec`` (already market-data-aware) — NOT under the review
router/service, which must stay broker/market-data-free (ROB-315 boundary).
Best-effort: any market-data failure leaves the benchmark NULL (the review
still renders the strategy net PnL).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any

from app.services.scalping_reviews.benchmark import (
    daily_buy_and_hold_return_bps,
    notional_weighted_benchmark_bps,
)
from app.services.scalping_reviews.service import (
    SCALPING_REVIEW_ACCOUNT_SCOPE,
    ScalpingReviewService,
)

logger = logging.getLogger(__name__)


async def compute_and_store_daily_benchmark(
    *,
    session: Any,
    market_data: Any,
    review_date: dt.date,
    product: str,
    now: dt.datetime,
    session_tag: str = "",
    account_scope: str = SCALPING_REVIEW_ACCOUNT_SCOPE,
) -> Decimal | None:
    """Compute the notional-weighted daily buy&hold benchmark from that day's
    fill-proven analytics rows and store it on the review row. Returns the
    stored value (``None`` when it cannot be computed). Never raises on a
    market-data failure — logs and stores ``None``."""
    service = ScalpingReviewService(session)
    rows = await service.list_analytics(review_date=review_date, product=product)

    notional_by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        if row.entry_price is None or row.entry_notional_usdt is None:
            continue  # partial/anomaly row — no capital basis
        notional_by_symbol[row.symbol] += row.entry_notional_usdt

    value: Decimal | None = None
    detail: dict[str, Any] = {}
    try:
        weighted: list[tuple[Decimal, Decimal]] = []
        for symbol, notional in notional_by_symbol.items():
            candles = await market_data.fetch_klines(
                product, symbol, interval="1d", limit=1
            )
            if not candles:
                continue
            candle = candles[0]
            bps = daily_buy_and_hold_return_bps(
                open_price=candle.open, close_price=candle.close
            )
            weighted.append((notional, bps))
            detail[symbol] = {
                "open": str(candle.open),
                "close": str(candle.close),
                "bps": str(bps),
                "notional_usdt": str(notional),
            }
        value = notional_weighted_benchmark_bps(weighted)
    except Exception:  # noqa: BLE001 — benchmark is best-effort, never fatal
        logger.exception(
            "daily benchmark computation failed for %s %s", product, review_date
        )
        return None

    await service.set_benchmark(
        review_date=review_date,
        product=product,
        value=value,
        now=now,
        session_tag=session_tag,
        account_scope=account_scope,
        detail=detail or None,
    )
    return value
