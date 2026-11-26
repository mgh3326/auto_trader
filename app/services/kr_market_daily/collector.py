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
    breadth: list[dict[str, Any]] = []
    if target_date == today:
        from app.services.research_candles.session_segment import (
            KRX_AFTER,
            KRX_REGULAR,
            session_segment_of,
        )

        for market in ("KOSPI", "KOSDAQ"):
            started_at = now or datetime.now(UTC)
            segment = session_segment_of("kr", "KRX", started_at)
            label = {KRX_REGULAR: "intraday", KRX_AFTER: "after_close"}.get(segment)
            if label is not None:
                row = await client.fetch_breadth(market)
                observed_at = now or datetime.now(UTC)
                received_segment = session_segment_of("kr", "KRX", observed_at)
                received_label = {
                    KRX_REGULAR: "intraday",
                    KRX_AFTER: "after_close",
                }.get(received_segment)
                if received_label is None:
                    continue
                breadth.append(
                    {
                        **row,
                        "trade_date": target_date,
                        "session_label": received_label,
                        "observed_at": observed_at,
                        "source": SOURCE,
                        "first_received_at": observed_at,
                    }
                )

    investors: list[dict[str, Any]] = []
    for market in ("KOSPI", "KOSDAQ"):
        rows = await client.fetch_investors(market, target_date)
        received_at = now or datetime.now(UTC)
        investors.extend(
            {
                **row,
                "market": market,
                "trade_date": target_date,
                "source": SOURCE,
                "first_received_at": received_at,
            }
            for row in rows
        )

    rows = await client.fetch_deposits()
    received_at = now or datetime.now(UTC)
    deposits = [
        {**row, "source": SOURCE, "first_received_at": received_at} for row in rows
    ]
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
