from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.mcp_server.tooling.market_data_indicators import _fetch_ohlcv_for_indicators
from app.services.invest_screener_snapshots.freshness import expected_baseline_date
from app.services.invest_screener_snapshots.repository import SnapshotUpsert

logger = logging.getLogger(__name__)

_LOOKBACK = 10

#: ROB-430 PR-①: consecutive_gainers filters ``consecutive_up_days >= 5``, which
#: requires at least 6 daily closes to even be possible (6 closes → 5 up-moves).
#: When the OHLCV window is shorter than this, a trailing-streak count is a
#: truncated lower bound (the start of the run is off-screen), so we record
#: ``consecutive_up_days = None`` (insufficient data) rather than a misleadingly
#: small number that would silently fail the >= 5 filter. The fix that actually
#: surfaces streaks is an operator re-build over a full daily-candle history.
_MIN_SESSIONS_FOR_RELIABLE_STREAK = 6


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
    if len(closes) < _MIN_SESSIONS_FOR_RELIABLE_STREAK:
        # ROB-430 PR-①: too few sessions to establish a streak the >= 5 filter
        # needs; a trailing count here is a truncated lower bound, so report None
        # (insufficient) instead of a misleadingly small, silently-excluded value.
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


def _coerce_snapshot_date(value: Any, fallback: dt.date) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value

    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        converted = to_pydatetime()
        if isinstance(converted, dt.datetime):
            return converted.date()
        if isinstance(converted, dt.date):
            return converted

    if value is None:
        return fallback

    try:
        return dt.datetime.fromisoformat(str(value)).date()
    except ValueError:
        logger.warning(
            "unable to coerce snapshot_date value=%r; using fallback=%s",
            value,
            fallback,
        )
        return fallback


async def build_snapshot_for_symbol(
    *, market: str, symbol: str, today: dt.date, now: dt.datetime | None = None
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
    if "date" in df.columns:
        # ROB-430 트랙B follow-up: compute the streak on COMPLETED daily closes only
        # (Toss "종가 기준"). The KIS daily endpoint returns a forming bar for the
        # current session intraday; including it would let an intraday/down move
        # prematurely break a streak. expected_baseline_date returns the prior
        # session before KR close (or today once closed), so an incomplete today-bar
        # is dropped only when present — a no-op for an end-of-day build.
        completed_through = expected_baseline_date(market, now=now)
        df = df[
            df["date"].map(
                lambda d: _coerce_snapshot_date(d, today) <= completed_through
            )
        ].reset_index(drop=True)
        if df.empty:
            return None
    closes_raw: list[Any] = list(df["close"].tolist())
    closes = [Decimal(str(c)) for c in closes_raw if c is not None]
    if not closes:
        return None

    metrics = derive_metrics(closes)
    snapshot_date = (
        _coerce_snapshot_date(df["date"].iloc[-1], today)
        if "date" in df.columns
        else today
    )
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
    built = [r for r in results if r is not None]

    # ROB-430 PR-①: operator diagnostic. If a large share of rows have an OHLCV
    # window shorter than _MIN_SESSIONS_FOR_RELIABLE_STREAK, consecutive_up_days is
    # None for them and consecutive_gainers (>= 5) will be empty regardless of the
    # market — the partition needs a re-build over a fuller daily-candle history.
    thin = sum(
        1
        for r in built
        if len(r.closes_window or []) < _MIN_SESSIONS_FOR_RELIABLE_STREAK
    )
    if thin:
        logger.warning(
            "invest_screener_snapshots[%s]: %d/%d rows have < %d OHLCV sessions "
            "(consecutive_up_days unreliable → consecutive_gainers may be empty; "
            "re-build over a fuller daily-candle history)",
            market,
            thin,
            len(built),
            _MIN_SESSIONS_FOR_RELIABLE_STREAK,
        )
    return built
