"""``retro_positions`` — ledger-derived holdings at a KST cutoff date.

Derived from ``review.execution_ledger`` (in-effect rows only, websocket
duplicates superseded) with the canonical FIFO of ``kis_lots.fifo_position``.
There is no broker call, so every row is ``basis=inference``: it states what the
ledger implies, not what the broker reports. A position the ledger cannot prove
(provisional rows no authoritative row covers, an oversold history, a cut-off
read) keeps ``lot_state=unknown`` and null quantity, average and unrealized
amount with a reason — never a number that looks measured.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.services.execution_ledger.kis_lots import LedgerFill, fifo_position
from app.services.execution_ledger.supersede import (
    AUTHORITATIVE_SOURCES,
    supersede_provisional,
)
from app.services.retro_v1 import daily_bars
from app.services.retro_v1.chain import MAX_LEDGER_ROWS
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    Row,
    Unknowns,
    build_response,
    check_limit,
    dec,
    decode_cursor,
    guarded,
    kst_window,
    latest,
    make_ref,
    params_digest,
    parse_date,
    read_snapshot,
)
from app.services.retro_v1.market import MARKETS, MarketInfo

_PRICE_LOOKBACK = timedelta(days=10)
_INFO_BY_INSTRUMENT = {m.instrument_type: m for m in MARKETS.values()}


def _fill(row: Any) -> LedgerFill:
    return LedgerFill(
        id=row.id,
        source=row.source,
        side=row.side,
        quantity=row.filled_qty,
        price=row.filled_price,
        filled_at=row.filled_at,
        broker_order_id=row.broker_order_id,
        venue=row.venue,
    )


def position_rows(
    ledger: list[Any],
    bars: list[daily_bars.Bar],
    *,
    cutoff_end: datetime,
    ledger_incomplete: bool,
    bars_incomplete: bool,
) -> list[Row]:
    """Pure projection: one row per (broker, instrument, symbol) held at cutoff."""
    kept, _ = supersede_provisional(ledger)
    groups: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for row in kept:
        if row.filled_at < cutoff_end:
            groups[
                (
                    row.broker,
                    str(getattr(row.instrument_type, "value", row.instrument_type)),
                    row.symbol.upper(),
                )
            ].append(row)
    closes: dict[str, daily_bars.Bar] = {}
    for bar in sorted(bars, key=lambda b: b.time):
        if bar.time < cutoff_end and bar.close is not None:
            closes[bar.symbol.upper()] = bar
    rows: list[Row] = []
    for (broker, instrument, symbol), fills in sorted(groups.items()):
        info: MarketInfo = _INFO_BY_INSTRUMENT[instrument]
        authoritative = [r for r in fills if r.source in AUTHORITATIVE_SOURCES]
        provisional = [r for r in fills if r.source not in AUTHORITATIVE_SOURCES]
        net, average, oversold = fifo_position([_fill(r) for r in authoritative])
        known = (
            bool(authoritative)
            and not provisional
            and oversold <= 0
            and not ledger_incomplete
        )
        if known and net <= 0:
            continue  # flat at the cutoff: not a position
        currency = fills[-1].currency
        base = {
            "symbol": fills[-1].symbol,
            "market": info.key,
            "broker": broker,
            "position_ref": make_ref("position", f"{broker}|{instrument}|{symbol}"),
            "currency": currency,
            "unit": info.quantity_unit,
            "basis": "inference",
        }
        if not known:
            rows.append(
                (
                    {
                        **base,
                        "quantity": None,
                        "average_price": None,
                        "unrealized_amount": None,
                        "lot_state": "unknown",
                        "missing_reason": "incomplete_evidence",
                    },
                    {},
                )
            )
            continue
        bar = closes.get(symbol)
        unrealized: Decimal | None = None
        missing: str | None = None
        if bar is None or bar.close is None:
            missing = "incomplete_evidence" if bars_incomplete else "missing_data"
        elif average is not None:
            unrealized = (bar.close - average) * net
        rows.append(
            (
                {
                    **base,
                    "quantity": dec(net),
                    "average_price": dec(average),
                    "unrealized_amount": dec(unrealized),
                    "lot_state": "known",
                    "missing_reason": missing,
                },
                {},
            )
        )
    return rows


async def read_ledger(session: AsyncSession, cutoff_end: datetime) -> list[Any]:
    stmt = (
        select(ExecutionLedger)
        .where(ExecutionLedger.account_mode == "live")
        .where(execution_ledger_in_effect())
        .where(ExecutionLedger.filled_at < cutoff_end)
        .order_by(ExecutionLedger.filled_at, ExecutionLedger.id)
        .limit(MAX_LEDGER_ROWS)
    )
    return list((await session.execute(stmt)).scalars().all())


async def read_retro_positions(
    session: AsyncSession,
    *,
    cutoff_date: Any,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    # Reference ledger and filter so read_retro_positions is counted as an in-effect reader on disk
    _ = (ExecutionLedger, execution_ledger_in_effect)
    check_limit(limit)
    day = parse_date(cutoff_date)
    digest = params_digest(op="retro_positions", day=day)
    offset = decode_cursor(cursor, digest)
    _, cutoff_end = kst_window(day, day)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)
    bars: list[daily_bars.Bar] = []
    bars_ok = True

    async with read_snapshot(session):
        ledger, ledger_ok = await guarded(
            session,
            unknowns,
            "source.execution_ledger",
            lambda: read_ledger(session, cutoff_end),
            [],
        )
        capped = ledger_ok and len(ledger) >= MAX_LEDGER_ROWS
        if capped:
            unknowns.note("source.execution_ledger", "incomplete_evidence")
        if ledger_ok and ledger:
            by_market: dict[str, set[str]] = defaultdict(set)
            for row in ledger:
                kind = str(getattr(row.instrument_type, "value", row.instrument_type))
                by_market[kind].add(row.symbol.upper())
            for kind, symbols in sorted(by_market.items()):
                info = _INFO_BY_INSTRUMENT[kind]

                async def read_bars(
                    info: MarketInfo = info, symbols: set[str] = symbols
                ) -> list[daily_bars.Bar]:
                    return await daily_bars.load_bars(
                        session,
                        info,
                        "1d",
                        sorted(symbols),
                        lo=cutoff_end - _PRICE_LOOKBACK,
                        hi=cutoff_end,
                        cap=MAX_LEDGER_ROWS,
                    )

                found, ok = await guarded(
                    session, unknowns, f"source.bars.{info.key}", read_bars, []
                )
                bars += found
                bars_ok = bars_ok and ok

    rows = position_rows(
        ledger,
        bars,
        cutoff_end=cutoff_end,
        ledger_incomplete=not ledger_ok or capped,
        bars_incomplete=not bars_ok,
    )
    return build_response(
        as_of=as_of,
        data_as_of=latest(r.filled_at for r in ledger),
        rows=rows,
        total=None if (not ledger_ok or capped) else len(rows),
        limit=limit,
        cursor=cursor,
        digest=digest,
        offset=offset,
        unknowns=unknowns,
    )
