"""Phase 2 — daily demo scalping review + buy&hold benchmark refresh (job logic).

Prefect-free so it is unit-testable (the @flow/@task wrapper lives in
``app/flows/binance_demo_scalping_review_flow.py``). Default-OFF behind
``settings.binance_demo_scalping_review_flow_enabled``. Read-only w.r.t.
brokers/orders: rolls that day's ``scalp_trade_analytics`` into the daily
review draft (``build_draft``) and computes the notional-weighted daily
buy&hold benchmark (``compute_and_store_daily_benchmark``). Per-product
failures are isolated via a SAVEPOINT so one product cannot poison another.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.demo_scalping.market_data import (
    DemoScalpingMarketData,
)
from app.services.brokers.binance.demo_scalping_exec.benchmark_runner import (
    compute_and_store_daily_benchmark,
)
from app.services.scalping_reviews.service import ScalpingReviewService

logger = logging.getLogger(__name__)

_DEFAULT_PRODUCTS: tuple[str, ...] = ("spot", "usdm_futures")


def _num(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


async def _refresh_with_session(
    session: AsyncSession,
    market_data: Any,
    review_date: dt.date,
    products: Sequence[str],
    now: dt.datetime,
) -> dict[str, Any]:
    service = ScalpingReviewService(session)
    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for product in products:
        try:
            tags = sorted(
                {""} | set(
                    await service.list_session_tags(
                        review_date=review_date, product=product
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 — isolate product on enumeration failure
            logger.exception(
                "demo scalping review tag enumeration failed for product=%s", product
            )
            errors.append({"product": product, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for session_tag in tags:
            try:
                async with session.begin_nested():  # SAVEPOINT: isolate per (product, tag)
                    review = await service.build_draft(
                        review_date=review_date,
                        product=product,
                        now=now,
                        session_tag=session_tag,
                    )
                    benchmark = await compute_and_store_daily_benchmark(
                        session=session,
                        market_data=market_data,
                        review_date=review_date,
                        product=product,
                        now=now,
                        session_tag=session_tag,
                    )
                    summaries.append(
                        {
                            "product": product,
                            "sessionTag": session_tag,
                            "tradeCount": review.trade_count,
                            "netReturnBps": _num(review.net_return_bps),
                            "benchmarkReturnBps": _num(benchmark),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 — isolate; savepoint rolled back
                logger.exception(
                    "demo scalping review refresh failed for product=%s tag=%s",
                    product,
                    session_tag,
                )
                errors.append(
                    {
                        "product": product,
                        "sessionTag": session_tag,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    return {
        "status": "ran",
        "reviewDate": review_date.isoformat(),
        "products": summaries,
        "errors": errors,
    }


async def run_demo_scalping_review_refresh(
    *,
    review_date: dt.date | None = None,
    products: Sequence[str] = _DEFAULT_PRODUCTS,
    now: dt.datetime | None = None,
    session: AsyncSession | None = None,
    market_data: Any | None = None,
) -> dict[str, Any]:
    """Build the daily review draft + buy&hold benchmark per demo product.

    No-op (``{"status": "disabled"}``) unless the env flag is set. When
    ``session``/``market_data`` are injected (tests) they are used as-is and
    NOT committed/closed by this function; otherwise they are created and
    committed/closed here."""
    if not settings.binance_demo_scalping_review_flow_enabled:
        return {"status": "disabled"}

    now = now or dt.datetime.now(dt.UTC)
    review_date = review_date or now.astimezone(dt.UTC).date()

    owns_md = market_data is None
    md = market_data or DemoScalpingMarketData()
    try:
        if session is not None:
            return await _refresh_with_session(session, md, review_date, products, now)
        async with AsyncSessionLocal() as own_session:
            result = await _refresh_with_session(
                own_session, md, review_date, products, now
            )
            await own_session.commit()
            return result
    finally:
        if owns_md:
            await md.aclose()
