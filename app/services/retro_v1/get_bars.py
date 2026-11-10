"""``get_bars`` — stored bars with source, venue and adjusted flag per row."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.retro_v1 import daily_bars
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    MAX_SOURCE_ROWS,
    RetroReadError,
    Row,
    Unknowns,
    build_response,
    check_limit,
    check_span,
    check_symbol,
    dec,
    decode_cursor,
    guarded,
    iso,
    kst_window,
    params_digest,
    parse_date,
    read_snapshot,
    resolve_from,
)
from app.services.retro_v1.market import MarketInfo, infer_market, normalize_symbol

TIMEFRAMES = ("1m", "5m", "60m", "1d")


def _currency(market: MarketInfo, symbol: str) -> str:
    if market.key != "crypto":
        return market.currency
    quote = symbol.split("-", 1)[0] if "-" in symbol else ""
    return quote or "quote_unknown"


def bar_row(bar: daily_bars.Bar, market: MarketInfo) -> Row:
    """One bar. A source or adjusted flag the table cannot state is null + reason."""
    # Stored daily OHLC of US/crypto is raw (US keeps adj_close in its own
    # column and crypto has no corporate actions). The KR table does not record
    # how its provider adjusted the series, so the flag is unknown there.
    adjusted: bool | None = None if market.key == "kr" else False
    hints: dict[str, str] = {}
    missing: str | None = None
    if adjusted is None:
        hints["adjusted"] = "unavailable"
        missing = "unavailable"
    if bar.source is None:
        hints["source"] = "unavailable"
        missing = "unavailable"
    row = {
        "symbol": bar.symbol,
        "time": iso(bar.time),
        "open_price": dec(bar.open),
        "high_price": dec(bar.high),
        "low_price": dec(bar.low),
        "close_price": dec(bar.close),
        "quantity": dec(bar.volume),
        "currency": _currency(market, bar.symbol),
        "unit": market.quantity_unit,
        "source": bar.source,
        "venue": bar.venue,
        "adjusted": adjusted,
        "missing_reason": missing,
        # Bars are stored provider data, not a broker fill or order readback.
        "basis": "inference",
    }
    return row, hints


async def read_get_bars(
    session: AsyncSession,
    *,
    symbol: str,
    tf: str,
    to: Any,
    from_: Any = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Contract ``get_bars``; ``from`` is accepted as ``from_`` or ``**{"from": d}``."""
    start = resolve_from(from_, extra)
    end = parse_date(to)
    check_span(start, end)
    check_limit(limit)
    if tf not in TIMEFRAMES:
        raise RetroReadError("validation_error")
    clean = check_symbol(symbol)
    market = infer_market(clean)
    normalized = normalize_symbol(clean, market)
    digest = params_digest(symbol=normalized, tf=tf, start=start, end=end)
    offset = decode_cursor(cursor, digest)
    lo, hi = kst_window(start, end)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)
    bars: list[daily_bars.Bar] = []
    total: int | None = None

    async with read_snapshot(session):
        if not daily_bars.supports(market, tf):
            unknowns.note("data", "unsupported")
        else:

            async def read() -> list[daily_bars.Bar]:
                return await daily_bars.load_bars(
                    session,
                    market,
                    tf,
                    [normalized],
                    lo=lo,
                    hi=hi,
                    cap=MAX_SOURCE_ROWS,
                )

            bars, ok = await guarded(session, unknowns, "data", read, [])
            if ok and len(bars) >= MAX_SOURCE_ROWS:
                unknowns.note("data", "incomplete_evidence")
            elif ok:
                total = len(bars)

    rows = [bar_row(bar, market) for bar in bars]
    data_as_of = max((bar.time for bar in bars), default=None)
    return build_response(
        as_of=as_of,
        data_as_of=data_as_of,
        rows=rows,
        total=total,
        limit=limit,
        cursor=cursor,
        digest=digest,
        offset=offset,
        unknowns=unknowns,
    )
