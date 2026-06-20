"""Phase 1 — operator CLI: build the daily scalping review draft + benchmark.

Builds (or refreshes) the ``scalping_daily_reviews`` draft for a day/product
from ``scalp_trade_analytics``, then computes + stores the notional-weighted
daily buy&hold benchmark (Demo public klines, read-only). No broker/order
mutation. Demo data hosts only.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys

logger = logging.getLogger("scalping_benchmark")

_VALID_PRODUCTS = ("spot", "usdm_futures")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build the daily demo scalping review draft + notional-weighted "
            "buy&hold benchmark (read-only; Demo data hosts only)."
        )
    )
    p.add_argument("--product", required=True, choices=_VALID_PRODUCTS)
    p.add_argument("--date", required=True, help="UTC review date YYYY-MM-DD")
    p.add_argument("--session-tag", default="")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    review_date = dt.date.fromisoformat(args.date)
    now = dt.datetime.now(dt.UTC)

    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo_scalping.market_data import (
        DemoScalpingMarketData,
    )
    from app.services.brokers.binance.demo_scalping_exec.benchmark_runner import (
        compute_and_store_daily_benchmark,
    )
    from app.services.scalping_reviews.service import ScalpingReviewService

    market_data = DemoScalpingMarketData()
    try:
        async with AsyncSessionLocal() as session:
            service = ScalpingReviewService(session)
            review = await service.build_draft(
                review_date=review_date,
                product=args.product,
                now=now,
                session_tag=args.session_tag,
            )
            value = await compute_and_store_daily_benchmark(
                session=session,
                market_data=market_data,
                review_date=review_date,
                product=args.product,
                now=now,
                session_tag=args.session_tag,
            )
            await session.commit()
    finally:
        await market_data.aclose()

    print(
        json.dumps(
            {
                "event": "scalping_benchmark",
                "review_id": review.id,
                "review_date": args.date,
                "product": args.product,
                "net_return_bps": (
                    None
                    if review.net_return_bps is None
                    else str(review.net_return_bps)
                ),
                "benchmark_return_bps": None if value is None else str(value),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level.upper())
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 — top-level CLI guard
        logger.error("scalping benchmark CLI failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
