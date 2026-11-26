"""Orchestrate the three independent Naver market-wide daily sources."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.services.kr_market_daily.client import SOURCE, NaverMarketDailyClient
from app.services.kr_market_daily.store import insert_market_daily


async def collect_market_daily(
    *,
    target_date: date,
    today: date,
    client: NaverMarketDailyClient,
    session: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch facts and optionally insert them; no source failure is an empty success."""
    received_at = now or datetime.now(UTC)
    breadth: list[dict[str, Any]] = []
    if target_date == today:
        from app.services.research_candles.session_segment import (
            KRX_REGULAR,
            session_segment_of,
        )

        segment = session_segment_of("kr", "KRX", received_at)
        label = "intraday" if segment == KRX_REGULAR else "after_close"
        for market in ("KOSPI", "KOSDAQ"):
            row = await client.fetch_breadth(market)
            breadth.append(
                {
                    **row,
                    "trade_date": target_date,
                    "session_label": label,
                    "observed_at": received_at,
                    "source": SOURCE,
                }
            )

    investors: list[dict[str, Any]] = []
    for market in ("KOSPI", "KOSDAQ"):
        investors.extend(
            {**row, "market": market, "trade_date": target_date, "source": SOURCE}
            for row in await client.fetch_investors(market, target_date)
        )

    deposits = [{**row, "source": SOURCE} for row in await client.fetch_deposits()]
    inserted = None
    if session is not None:
        inserted = await insert_market_daily(
            session,
            breadth=breadth,
            investors=investors,
            deposits=deposits,
            first_received_at=received_at,
        )
        await session.commit()
    return {
        "trade_date": target_date.isoformat(),
        "breadth": breadth,
        "investor_groups": investors,
        "deposit_credit": deposits,
        "inserted": inserted,
    }
