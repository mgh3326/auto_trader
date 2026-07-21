"""Trusted, normalized market-cap reads for KR quality filters.

The KR TradingView fundamentals snapshot stores market cap in a provider-specific
unit and is intentionally unsuitable for hard KRW thresholds.  Naver-backed
``market_valuation_snapshots`` rows are normalized to raw KRW by their builder,
so quality filters share this one source instead of inventing per-tool backfills.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.market_valuation_snapshot import MarketValuationSnapshot

logger = logging.getLogger(__name__)

KR_NORMALIZED_MARKET_CAP_SOURCE = "naver_finance"


@dataclass(frozen=True)
class NormalizedMarketCap:
    value: Decimal
    snapshot_date: dt.date
    source: str


async def load_normalized_kr_market_caps(
    session: AsyncSession,
    symbols: Iterable[str],
) -> dict[str, NormalizedMarketCap]:
    """Return each symbol's newest trusted raw-KRW market cap.

    Only ``naver_finance`` rows are eligible.  In particular, this excludes
    ``invest_kr_fundamentals_snapshots``/TradingView values whose KR unit is not
    normalized and caused blue-chip false negatives in ROB-976 R2.
    """

    normalized_symbols = {
        str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
    }
    if not normalized_symbols:
        return {}

    result = await session.execute(
        sa.select(
            MarketValuationSnapshot.symbol,
            MarketValuationSnapshot.market_cap,
            MarketValuationSnapshot.snapshot_date,
            MarketValuationSnapshot.source,
        )
        .where(
            MarketValuationSnapshot.market == "kr",
            MarketValuationSnapshot.symbol.in_(normalized_symbols),
            MarketValuationSnapshot.source == KR_NORMALIZED_MARKET_CAP_SOURCE,
            MarketValuationSnapshot.market_cap.is_not(None),
            MarketValuationSnapshot.market_cap > 0,
        )
        .order_by(
            MarketValuationSnapshot.symbol.asc(),
            MarketValuationSnapshot.snapshot_date.desc(),
            MarketValuationSnapshot.computed_at.desc(),
        )
    )

    caps: dict[str, NormalizedMarketCap] = {}
    for row in result.all():
        if row.symbol in caps:
            continue
        caps[row.symbol] = NormalizedMarketCap(
            value=Decimal(row.market_cap),
            snapshot_date=row.snapshot_date,
            source=row.source,
        )
    return caps


async def fetch_normalized_kr_market_caps(
    symbols: Iterable[str],
    *,
    session_factory: Any = AsyncSessionLocal,
) -> dict[str, NormalizedMarketCap]:
    """Session-owning, fail-soft wrapper for MCP ranking handlers."""

    try:
        async with session_factory() as session:
            return await load_normalized_kr_market_caps(session, symbols)
    except Exception:  # noqa: BLE001 - caller's fail-closed filter handles no coverage
        logger.warning("normalized KR market-cap snapshot read failed", exc_info=True)
        return {}
