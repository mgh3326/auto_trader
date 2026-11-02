"""Mark prices for sleeve unrealized P&L (ROB-1470).

The default mark is the newest *completed* daily close in ``daily_candles``, and
only when that close is the exchange's last completed session (for crypto, the
previous UTC day). A missing or stale close is ``None``; the book then reports
P&L as unknown and the gate sends the order to a person. No broker call.

pandas-backed calendar helpers are imported inside the function so importing
this module (and the dispatch path that uses it) stays cheap.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

MarkFn = Callable[
    [AsyncSession, str, Sequence[str], datetime],
    Awaitable[dict[str, Decimal | None]],
]

_EXCHANGE = {"equity_kr": "XKRX", "equity_us": "XNYS"}
_PARTITION = {"equity_kr": "KRX", "equity_us": None, "crypto": "upbit_krw"}
_LOCAL_TZ = {
    "equity_kr": ZoneInfo("Asia/Seoul"),
    "equity_us": ZoneInfo("America/New_York"),
}


async def latest_close_marks(
    session: AsyncSession, market: str, symbols: Sequence[str], now: datetime
) -> dict[str, Decimal | None]:
    if not symbols:
        return {}
    from app.services.daily_candles.read_service import latest_exchange_session
    from app.services.daily_candles.repository import (
        DailyCandlesRepository,
        MarketKey,
    )

    market_key = {
        "equity_kr": MarketKey.KR,
        "equity_us": MarketKey.US,
        "crypto": MarketKey.CRYPTO,
    }[market]
    exchange = _EXCHANGE.get(market)
    last_complete = (
        latest_exchange_session(exchange, now=now)
        if exchange is not None
        else now.astimezone(UTC).date() - timedelta(days=1)
    )
    rows_by_symbol = await DailyCandlesRepository(session=session).fetch_recent_batch(
        market=market_key,
        symbols=list(symbols),
        partition=_PARTITION[market],
        count=5,
    )
    marks: dict[str, Decimal | None] = dict.fromkeys(symbols)
    if last_complete is None:
        return marks
    for symbol, rows in rows_by_symbol.items():
        completed = [row for row in rows if row.time_utc.date() <= last_complete]
        if not completed:
            continue
        # Newest by date, not by list position.
        latest = max(completed, key=lambda row: row.time_utc)
        if latest.time_utc.date() != last_complete:
            continue
        try:
            close = Decimal(str(latest.close))
        except (InvalidOperation, ValueError):
            continue
        if close.is_finite() and close > 0:
            marks[symbol] = close
    return marks


HorizonFn = Callable[
    [AsyncSession, str, Sequence[tuple[str, str, datetime]], int, datetime],
    Awaitable[dict[str, tuple[bool, Decimal | None]]],
]


async def horizon_closes(
    session: AsyncSession,
    market: str,
    signals: Sequence[tuple[str, str, datetime]],
    horizon_sessions: int,
    now: datetime,
) -> dict[str, tuple[bool, Decimal | None]]:
    """Per signal key: (matured, close of the H-th completed bar after it).

    ``signals`` is ``(signal_key, symbol, signal_at)``. A bar is a session for
    KR and US and a UTC day for crypto. Matured means H completed sessions have
    passed on the exchange calendar (crypto: H days); the close is ``None`` when
    the candle table lacks that bar.
    """
    if not signals:
        return {}
    from app.services.daily_candles.read_service import latest_exchange_session
    from app.services.daily_candles.repository import (
        DailyCandlesRepository,
        MarketKey,
    )

    market_key = {
        "equity_kr": MarketKey.KR,
        "equity_us": MarketKey.US,
        "crypto": MarketKey.CRYPTO,
    }[market]
    exchange = _EXCHANGE.get(market)
    last_complete = (
        latest_exchange_session(exchange, now=now)
        if exchange is not None
        else now.astimezone(UTC).date() - timedelta(days=1)
    )
    oldest = min(signal_at for _key, _symbol, signal_at in signals)
    span_days = (now.astimezone(UTC).date() - oldest.astimezone(UTC).date()).days
    rows_by_symbol = await DailyCandlesRepository(session=session).fetch_recent_batch(
        market=market_key,
        symbols=sorted({symbol for _key, symbol, _at in signals}),
        partition=_PARTITION[market],
        count=min(max(span_days + 10, horizon_sessions + 10), 2000),
    )
    out: dict[str, tuple[bool, Decimal | None]] = {}
    for key, symbol, signal_at in signals:
        # The signal's own session date on the market's clock.
        signal_date = signal_at.astimezone(_LOCAL_TZ.get(market, UTC)).date()
        if last_complete is None:
            out[key] = (False, None)
            continue
        if exchange is None:
            target = signal_date + timedelta(days=horizon_sessions)
            matured = target <= last_complete
        else:
            from exchange_calendars import get_calendar

            sessions = get_calendar(exchange).sessions_in_range(
                signal_date + timedelta(days=1), last_complete
            )
            matured = len(sessions) >= horizon_sessions
            target = sessions[horizon_sessions - 1].date() if matured else None
        close: Decimal | None = None
        if matured:
            # The bar of exactly the H-th session; a gap in the candle table is
            # a missing close, never the next bar.
            bar = next(
                (
                    row
                    for row in rows_by_symbol.get(symbol.strip().upper(), [])
                    if row.time_utc.date() == target
                ),
                None,
            )
            try:
                candidate = None if bar is None else Decimal(str(bar.close))
            except (InvalidOperation, ValueError):
                candidate = None
            if candidate is not None and candidate.is_finite() and candidate > 0:
                close = candidate
        out[key] = (matured, close)
    return out
