from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.mcp_server.tooling.market_data_indicators import _fetch_ohlcv_for_indicators
from app.services.invest_screener_snapshots.repository import SnapshotUpsert

logger = logging.getLogger(__name__)

_LOOKBACK = 10


@dataclass(frozen=True)
class DerivedMetrics:
    latest_close: Decimal
    prev_close: Decimal | None
    change_amount: Decimal | None
    change_rate: Decimal | None
    consecutive_up_days: int | None
    week_change_rate: Decimal | None


def derive_metrics(closes: Sequence[Decimal]) -> DerivedMetrics:
    if not closes:
        raise ValueError("closes must be non-empty")
    latest = Decimal(closes[-1])
    prev = Decimal(closes[-2]) if len(closes) >= 2 else None

    if prev is None:
        change_amount = None
        change_rate = None
    else:
        change_amount = latest - prev
        change_rate = (change_amount / prev * Decimal("100")) if prev != 0 else None

    streak: int | None
    if len(closes) < 2:
        streak = None
    else:
        streak = 0
        for current, previous in zip(
            reversed(list(closes[1:])), reversed(list(closes[:-1])), strict=False
        ):
            if Decimal(current) > Decimal(previous):
                streak += 1
                continue
            break

    if len(closes) >= 5:
        # closes[-5] is 5 sessions ago; week_change = (today - 5d_ago) / 5d_ago * 100
        base = Decimal(closes[-5])
        week_change_rate = (
            (latest - base) / base * Decimal("100") if base != 0 else None
        )
    else:
        week_change_rate = None

    return DerivedMetrics(
        latest_close=latest,
        prev_close=prev,
        change_amount=change_amount,
        change_rate=change_rate,
        consecutive_up_days=streak,
        week_change_rate=week_change_rate,
    )


def _market_type_and_source(market: str) -> tuple[str, str]:
    if market == "kr":
        return "equity_kr", "kis"
    if market == "us":
        return "equity_us", "yahoo"
    raise ValueError(f"unsupported market: {market}")


async def build_snapshot_for_symbol(
    *, market: str, symbol: str, today: dt.date
) -> SnapshotUpsert | None:
    market_type, source = _market_type_and_source(market)
    try:
        df = await _fetch_ohlcv_for_indicators(symbol, market_type, count=_LOOKBACK)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ohlcv fetch failed market=%s symbol=%s: %s", market, symbol, exc
        )
        return None
    if df is None or df.empty or "close" not in df.columns:
        return None
    df = df.sort_values("date").reset_index(drop=True) if "date" in df.columns else df
    closes_raw: list[Any] = list(df["close"].tolist())
    closes = [Decimal(str(c)) for c in closes_raw if c is not None]
    if not closes:
        return None

    metrics = derive_metrics(closes)
    snapshot_date = df["date"].iloc[-1].date() if "date" in df.columns else today
    daily_volume = (
        int(df["volume"].iloc[-1])
        if "volume" in df.columns and df["volume"].iloc[-1] is not None
        else None
    )

    return SnapshotUpsert(
        market=market,
        symbol=symbol,
        snapshot_date=snapshot_date,
        latest_close=metrics.latest_close,
        prev_close=metrics.prev_close,
        change_amount=metrics.change_amount,
        change_rate=metrics.change_rate,
        consecutive_up_days=metrics.consecutive_up_days,
        week_change_rate=metrics.week_change_rate,
        closes_window=[float(c) for c in closes[-_LOOKBACK:]],
        daily_volume=daily_volume,
        source=source,
    )


async def build_snapshots_for_market(
    *,
    market: str,
    symbols: Iterable[str],
    today: dt.date,
    concurrency: int = 4,
) -> list[SnapshotUpsert]:
    sem = asyncio.Semaphore(concurrency)
    symbols_list = list(symbols)
    results: list[SnapshotUpsert | None] = [None] * len(symbols_list)

    async def _one(idx: int, sym: str) -> None:
        async with sem:
            results[idx] = await build_snapshot_for_symbol(
                market=market, symbol=sym, today=today
            )

    await asyncio.gather(*(_one(i, s) for i, s in enumerate(symbols_list)))
    return [r for r in results if r is not None]
