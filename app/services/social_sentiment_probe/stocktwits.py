from __future__ import annotations

import datetime as dt
from typing import Any

from app.services.social_sentiment_probe.models import source_result


def probe_stocktwits_firestream(
    symbol: str,
    market: str,
    username: str | None,
    password: str | None,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    observed_at = now or dt.datetime.now(dt.UTC)
    if not username or not password:
        return source_result(
            source="stocktwits_firestream",
            market=market,
            query=symbol,
            status="requires_credentials",
            items=[],
            observed_at=observed_at,
            error_reason=(
                "Official StockTwits Firestream docs require Basic Authentication; "
                "v0 does not scrape unauthenticated web endpoints"
            ),
        )
    return source_result(
        source="stocktwits_firestream",
        market=market,
        query=symbol,
        status="credentials_present",
        items=[],
        observed_at=observed_at,
        error_reason=(
            "Credentials are present; add a bounded live stream smoke only after "
            "operator confirms account terms"
        ),
    )
