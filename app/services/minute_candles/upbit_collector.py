"""Collect closed Upbit KRW one-minute candles into the shared crypto table."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.core.symbol import to_upbit_symbol
from app.services.minute_candles.repository import (
    MinuteCandleRow,
    MinuteCandlesRepository,
)
from app.services.minute_candles.upbit_public import (
    PAGE_SIZE,
    Candle,
    UpbitMinutePublicData,
)

KST = timezone(timedelta(hours=9))
SOURCE = "upbit_rest_1m"
FIXED_MARKETS = frozenset({"KRW-BTC", "KRW-XRP", "KRW-ETH"})
MAX_TOP_N = 100
MAX_PAGES_PER_MARKET = 8
MINUTE_CLOSE_DELAY = timedelta(seconds=70)


@dataclass(frozen=True, slots=True)
class MarketResult:
    market: str
    status: str
    rows: int | None
    new: int | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionResult:
    targets: tuple[str, ...]
    held_markets: tuple[str, ...]
    fixed_markets: tuple[str, ...]
    top_markets: tuple[str, ...]
    top_n: int
    markets: tuple[MarketResult, ...]
    selection_error: str | None = None

    @property
    def errors(self) -> int:
        return sum(item.status == "error" for item in self.markets) + int(
            self.selection_error is not None
        )


@dataclass(frozen=True, slots=True)
class _PageWalk:
    rows: list[tuple[Candle, datetime]]
    incomplete: bool


def configured_top_n(value: int | None = None) -> int:
    raw = (
        value
        if value is not None
        else int(os.getenv("UPBIT_MINUTE_CANDLE_TOP_N", "20"))
    )
    if isinstance(raw, bool) or raw < 0:
        raise ValueError("top_n must be a non-negative integer")
    return min(raw, MAX_TOP_N)


def select_targets(
    held_markets: set[str], top_markets: list[str], top_n: int
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    effective = configured_top_n(top_n)
    held = {m for m in held_markets if m.startswith("KRW-")}
    selected_top = tuple(dict.fromkeys(m for m in top_markets if m.startswith("KRW-")))[
        :effective
    ]
    return (
        tuple(sorted(held | FIXED_MARKETS | set(selected_top))),
        tuple(sorted(held)),
        selected_top,
    )


async def _default_held_markets() -> set[str]:
    """Reuse the existing Upbit balance read path for held-market membership."""
    from app.services.brokers.upbit.client import fetch_all_market_codes, fetch_my_coins

    held: set[str] = set()
    for row in await fetch_my_coins():
        currency = row.get("currency")
        if not isinstance(currency, str) or not currency.strip() or currency == "KRW":
            continue
        try:
            balance = Decimal(str(row.get("balance")))
            locked = Decimal(str(row.get("locked")))
        except InvalidOperation:
            continue
        if all(v.is_finite() and v >= 0 for v in (balance, locked)) and (
            balance > 0 or locked > 0
        ):
            held.add(to_upbit_symbol(currency))
    if not held:
        return set()
    markets = await fetch_all_market_codes(fiat="KRW")
    return held.intersection(
        m for m in markets if isinstance(m, str) and m.startswith("KRW-")
    )


def _kst_day_start(now: datetime) -> datetime:
    local = now.astimezone(KST)
    return datetime.combine(local.date(), time.min, tzinfo=KST).astimezone(UTC)


def _walk_rows(
    data: UpbitMinutePublicData,
    market: str,
    now: datetime,
    *,
    after: datetime | None = None,
):
    start = _kst_day_start(now)
    cursor = now.astimezone(UTC)
    checkpoint = after.astimezone(UTC) if after is not None else None
    # A new collector starts with a short overlap so a run at midnight can
    # still capture the previous day's final closed minutes. Existing
    # checkpoints are retained across midnight and bound the walk directly.
    lower_bound = checkpoint or (start - timedelta(minutes=3))
    by_time: dict[datetime, tuple[Candle, datetime]] = {}
    reached_bound = False
    page = None
    for _ in range(MAX_PAGES_PER_MARKET):
        page = data.candles(market, cursor)
        if not page.candles:
            break
        stamps = [row.time_utc for row in page.candles]
        if max(stamps) >= cursor:
            raise ValueError("Upbit page is not strictly before its to cursor")
        for candle in page.candles:
            if (
                candle.time_utc >= lower_bound
                and (checkpoint is None or candle.time_utc > checkpoint)
                and candle.time_utc + MINUTE_CLOSE_DELAY <= now
            ):
                by_time[candle.time_utc] = (candle, page.received_at)
        oldest = min(stamps)
        if checkpoint is not None and oldest <= checkpoint:
            break
        if oldest <= lower_bound:
            reached_bound = True
            break
        if len(page.candles) < PAGE_SIZE:
            break
        cursor = oldest
    incomplete = bool(
        not reached_bound
        and page is not None
        and len(page.candles) >= PAGE_SIZE
        and len(by_time) > 0
    )
    return _PageWalk([by_time[key] for key in sorted(by_time)], incomplete)


def _rows_for_current_day(
    data: UpbitMinutePublicData,
    market: str,
    now: datetime,
    *,
    after: datetime | None = None,
):
    return _walk_rows(data, market, now, after=after).rows


async def collect_upbit_minute_candles(
    *,
    session=None,
    data: UpbitMinutePublicData | None = None,
    held_markets_loader: Callable[[], Awaitable[set[str]]] | None = None,
    now: datetime | None = None,
    top_n: int | None = None,
    dry_run: bool = True,
) -> CollectionResult:
    """Collect one KST calendar day; dry-run never creates a DB session."""
    data = data or UpbitMinutePublicData()
    current = (now or datetime.now(UTC)).astimezone(UTC)
    effective_top_n = configured_top_n(top_n)
    selection_error = None
    try:
        available = data.markets()
        top_markets = data.top_by_trade_value(available, effective_top_n)
    except Exception as exc:
        available, top_markets = [], []
        selection_error = type(exc).__name__

    loader = held_markets_loader or _default_held_markets
    try:
        held = await loader()
    except Exception as exc:
        held = set()
        selection_error = selection_error or f"held_markets:{type(exc).__name__}"

    targets, held_sorted, top_selected = select_targets(
        held, top_markets, effective_top_n
    )
    results: list[MarketResult] = []
    repo = None
    instrument_id: int | None = None
    if not dry_run:
        if session is None:
            raise ValueError("session is required when dry_run is false")
        repo = MinuteCandlesRepository(session=session)
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO public.crypto_instruments "
                "(venue, product, venue_symbol, base_asset, quote_asset, status) "
                "VALUES (:venue, :product, :venue_symbol, :base_asset, :quote_asset, 'active') "
                "ON CONFLICT (venue, product, venue_symbol) DO NOTHING"
            ),
            [
                {
                    "venue": "upbit",
                    "product": "spot",
                    "venue_symbol": market,
                    "base_asset": market[4:],
                    "quote_asset": "KRW",
                }
                for market in targets
            ],
        )
        await session.commit()

    for market in targets:
        try:
            latest_at = None
            if repo is not None:
                from sqlalchemy import func, select

                from app.models.crypto_candles import CryptoCandle1m
                from app.models.crypto_instruments import CryptoInstrument

                instrument_result = await session.execute(
                    select(CryptoInstrument.id).where(
                        CryptoInstrument.venue == "upbit",
                        CryptoInstrument.product == "spot",
                        CryptoInstrument.venue_symbol == market,
                    )
                )
                instrument_id = int(instrument_result.scalar_one())
                latest_result = await session.execute(
                    select(func.max(CryptoCandle1m.time)).where(
                        CryptoCandle1m.instrument_id == instrument_id,
                        CryptoCandle1m.source == SOURCE,
                    )
                )
                latest_at = latest_result.scalar_one_or_none()
            walk = _walk_rows(data, market, current, after=latest_at)
            rows = walk.rows
            new_rows = 0 if repo is not None else None
            if repo is not None and rows:
                if instrument_id is None:
                    raise RuntimeError(
                        "instrument id unavailable for Upbit candle write"
                    )
                new_rows = await repo.insert_rows(
                    rows=[
                        MinuteCandleRow(
                            instrument_id=int(instrument_id),
                            time_utc=candle.time_utc,
                            open=candle.open,
                            high=candle.high,
                            low=candle.low,
                            close=candle.close,
                            base_volume=candle.base_volume,
                            quote_volume=candle.quote_volume,
                            is_closed=True,
                            source=SOURCE,
                            source_event_at=received_at,
                        )
                        for candle, received_at in rows
                    ]
                )
                await session.commit()
            results.append(
                MarketResult(
                    market,
                    "error" if walk.incomplete else "ok",
                    len(rows),
                    new_rows,
                    f"page_limit_{MAX_PAGES_PER_MARKET}_gap"
                    if walk.incomplete
                    else None,
                )
            )
        except Exception as exc:
            if session is not None and not dry_run:
                await session.rollback()
            error = (
                f"HTTP_{exc.status_code}"
                if hasattr(exc, "status_code") and exc.status_code is not None
                else type(exc).__name__
            )
            results.append(MarketResult(market, "error", None, None, error))

    return CollectionResult(
        targets=targets,
        held_markets=held_sorted,
        fixed_markets=tuple(sorted(FIXED_MARKETS)),
        top_markets=top_selected,
        top_n=effective_top_n,
        markets=tuple(results),
        selection_error=selection_error,
    )
