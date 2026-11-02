"""Read lots, bars, open sells and protection from the database only (ROB-1460).

🔴 Nothing here may import a broker client: the shadow job runs on this module
alone. Positions therefore come from ``review.execution_ledger`` and not from a
broker balance. They can lag or miss what auto_trader cannot see, such as a sale
in a broker's own app. A shadow row records its ``position_source`` for that
reason, and the live path gets its quantity and cost checked by a fresh broker
preview before anything is sent.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.protected_positions import ProtectedPosition
from app.models.review import KISLiveOrderLedger, LiveOrderLedger, TossLiveOrderLedger
from app.models.trading import InstrumentType
from app.services.daily_candles.read_service import latest_exchange_session
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.execution_ledger.kis_lots import (
    AUTHORITATIVE_SOURCES,
    LedgerFill,
    fifo_position,
)
from app.services.mechanical_loss_cut.rules import Bar, HeldLot, LotKey

_OPEN_ORDER_STATUSES = ("accepted", "pending", "partial")
_ACCOUNT_BY_BROKER_MARKET: dict[tuple[str, str], str] = {
    ("kis", "equity_kr"): "kis_live",
    ("kis", "equity_us"): "kis_live",
    ("toss", "equity_kr"): "toss_live",
    ("toss", "equity_us"): "toss_live",
    ("upbit", "crypto"): "upbit",
}
_MARKET_BY_INSTRUMENT: dict[InstrumentType, str] = {
    InstrumentType.equity_kr: "equity_kr",
    InstrumentType.equity_us: "equity_us",
    InstrumentType.crypto: "crypto",
}
_CANDLE_READ: dict[str, tuple[MarketKey, str | None, str | None]] = {
    "equity_kr": (MarketKey.KR, "KRX", "XKRX"),
    "equity_us": (MarketKey.US, None, "XNYS"),
    "crypto": (MarketKey.CRYPTO, "upbit_krw", None),
}
_PROTECTED_MARKET = {"equity_kr": "kr", "equity_us": "us", "crypto": "crypto"}
_PROTECTED_SCOPE = {"upbit": "upbit_live"}
_CURRENCY = {"equity_kr": "KRW", "equity_us": "USD", "crypto": "KRW"}
_CRYPTO_PREFIX = "KRW-"


def lot_symbol(symbol: str, market: str) -> str:
    """Crypto lots use the proposal and candle form; the ledger strips the prefix."""
    upper = symbol.strip().upper()
    if market == "crypto" and "-" not in upper:
        return f"{_CRYPTO_PREFIX}{upper}"
    return upper


def _symbol_forms(keys: Sequence[LotKey]) -> set[str]:
    forms: set[str] = set()
    for key in keys:
        forms.add(key.symbol)
        if key.market == "crypto":
            forms.add(key.symbol.removeprefix(_CRYPTO_PREFIX))
    return forms


async def load_held_lots(
    session: AsyncSession,
    *,
    markets: Sequence[str],
    symbols: set[str] | None = None,
) -> list[HeldLot]:
    """FIFO position per account, market and symbol from authoritative fills.

    FIFO needs every fill, so this reads the whole history. ``symbols`` keeps the
    buy path from reading other symbols' rows.
    """
    grouped: dict[LotKey, list[LedgerFill]] = defaultdict(list)
    statement = select(ExecutionLedger).where(
        execution_ledger_in_effect(),
        ExecutionLedger.account_mode == "live",
        ExecutionLedger.source.in_(AUTHORITATIVE_SOURCES),
    )
    if symbols is not None:
        statement = statement.where(ExecutionLedger.symbol.in_(symbols))
    rows = await session.execute(statement)
    for row in rows.scalars().all():
        market = _MARKET_BY_INSTRUMENT.get(row.instrument_type)
        account = _ACCOUNT_BY_BROKER_MARKET.get((row.broker, market or ""))
        if market is None or account is None or market not in markets:
            continue
        symbol = row.raw_symbol if market == "crypto" else row.symbol
        grouped[LotKey(account, market, lot_symbol(symbol, market))].append(
            LedgerFill(
                id=row.id,
                source=row.source,
                side=row.side,
                quantity=row.filled_qty,
                price=row.filled_price,
                filled_at=row.filled_at,
                broker_order_id=row.broker_order_id,
                venue=row.venue,
            )
        )
    lots: list[HeldLot] = []
    for key, fills in grouped.items():
        held = held_lot_from_fills(key, fills)
        if held is not None:
            lots.append(held)
    return lots


def held_lot_from_fills(key: LotKey, fills: Sequence[LedgerFill]) -> HeldLot | None:
    """``None`` when flat. An oversold ledger is unreadable, not a position."""
    net, average_cost, oversold = fifo_position(fills)
    if net <= 0 or oversold > 0 or average_cost is None:
        return None
    return HeldLot(
        key=key,
        quantity=net,
        average_cost=average_cost,
        currency=_CURRENCY[key.market],
    )


async def load_position(session: AsyncSession, key: LotKey) -> HeldLot | None:
    lots = await load_held_lots(
        session, markets=(key.market,), symbols=_symbol_forms((key,))
    )
    return next((lot for lot in lots if lot.key == key), None)


def _to_bar(row: DailyCandleRow) -> Bar:
    return Bar(
        session=row.time_utc.date(),
        high=Decimal(str(row.high)),
        low=Decimal(str(row.low)),
        close=Decimal(str(row.close)),
        volume=Decimal(str(row.volume)),
        open=Decimal(str(row.open)),
    )


async def load_bars(
    session: AsyncSession,
    *,
    market: str,
    symbols: Sequence[str],
    count: int,
    now: datetime,
) -> dict[str, tuple[list[Bar], bool]]:
    """Completed daily bars and whether each symbol's newest bar is current.

    A session still forming is dropped so that a partial bar can never decide a
    stop. A bar counts as current only when it is the last completed session.
    """
    market_key, partition, exchange = _CANDLE_READ[market]
    rows_by_symbol = await DailyCandlesRepository(session=session).fetch_recent_batch(
        market=market_key, symbols=list(symbols), partition=partition, count=count
    )
    last_complete = (
        latest_exchange_session(exchange, now=now)
        if exchange is not None
        else now.astimezone(UTC).date() - timedelta(days=1)
    )
    out: dict[str, tuple[list[Bar], bool]] = {}
    for symbol, rows in rows_by_symbol.items():
        bars = [
            _to_bar(row)
            for row in rows
            if last_complete is not None and row.time_utc.date() <= last_complete
        ]
        fresh = bool(bars) and bars[-1].session == last_complete
        out[symbol] = (bars, fresh)
    return out


def _open_quantity(row: Any) -> Decimal:
    quantity = Decimal(str(row.quantity or 0))
    filled = Decimal(str(row.filled_qty or 0))
    return max(quantity - filled, Decimal("0"))


async def load_open_sell_quantities(
    session: AsyncSession, keys: Sequence[LotKey]
) -> dict[LotKey, Decimal]:
    """Quantity already committed to a sell, from ledgers and live proposals.

    A submitted rung also has a ledger row, so it counts twice. That can only
    shrink what is cut, which is the direction this reader is allowed to err in.
    Orders placed outside auto_trader are invisible here. The live path's
    fresh preview reads sellable quantity from the broker for that gap.
    """
    # Imported here: the order_proposals package reaches this module through
    # the buy path.
    from app.services.order_proposals.state_machine import PROPOSAL_TERMINAL_STATES

    wanted = set(keys)
    totals: dict[LotKey, Decimal] = dict.fromkeys(keys, Decimal("0"))
    symbols = _symbol_forms(keys)

    def add(key: LotKey, quantity: Decimal) -> None:
        if key in wanted:
            totals[key] += quantity

    kis_rows = await session.execute(
        select(KISLiveOrderLedger).where(
            KISLiveOrderLedger.side == "sell",
            KISLiveOrderLedger.status.in_(_OPEN_ORDER_STATUSES),
            KISLiveOrderLedger.symbol.in_(symbols),
        )
    )
    for row in kis_rows.scalars().all():
        market = _MARKET_BY_INSTRUMENT.get(row.instrument_type)
        if market is not None:
            add(LotKey("kis_live", market, row.symbol), _open_quantity(row))

    live_rows = await session.execute(
        select(LiveOrderLedger).where(
            LiveOrderLedger.side == "sell",
            LiveOrderLedger.status.in_(_OPEN_ORDER_STATUSES),
            LiveOrderLedger.symbol.in_(symbols),
        )
    )
    for row in live_rows.scalars().all():
        account = "upbit" if row.broker == "upbit" else "kis_live"
        market = "crypto" if account == "upbit" else "equity_us"
        add(
            LotKey(account, market, lot_symbol(row.symbol, market)), _open_quantity(row)
        )

    toss_rows = await session.execute(
        select(TossLiveOrderLedger).where(
            TossLiveOrderLedger.side == "sell",
            TossLiveOrderLedger.status.in_(_OPEN_ORDER_STATUSES),
            TossLiveOrderLedger.symbol.in_(symbols),
        )
    )
    for row in toss_rows.scalars().all():
        market = "equity_kr" if row.market == "kr" else "equity_us"
        add(LotKey("toss_live", market, row.symbol), _open_quantity(row))

    rung_rows = await session.execute(
        select(OrderProposal, OrderProposalRung)
        .join(OrderProposalRung, OrderProposalRung.proposal_pk == OrderProposal.id)
        .where(
            OrderProposalRung.side == "sell",
            OrderProposal.symbol.in_(symbols),
            OrderProposalRung.state.not_in(PROPOSAL_TERMINAL_STATES),
        )
    )
    for group, rung in rung_rows.all():
        add(
            LotKey(
                group.account_mode,
                group.market,
                lot_symbol(group.symbol, group.market),
            ),
            Decimal(str(rung.quantity or 0)),
        )
    return totals


async def load_protected_quantities(
    session: AsyncSession, keys: Sequence[LotKey]
) -> dict[LotKey, Decimal]:
    """The declared long-term quantity a sell may not touch (#943)."""
    totals: dict[LotKey, Decimal] = dict.fromkeys(keys, Decimal("0"))
    by_identity = {
        (
            _PROTECTED_SCOPE.get(key.account_mode, key.account_mode),
            _PROTECTED_MARKET[key.market],
            key.symbol,
        ): key
        for key in keys
    }
    rows = await session.execute(
        select(ProtectedPosition).where(
            ProtectedPosition.symbol.in_(_symbol_forms(keys))
        )
    )
    for row in rows.scalars().all():
        symbol = lot_symbol(row.symbol, row.market)
        key = by_identity.get((row.account_scope, row.market, symbol))
        if key is not None:
            totals[key] = Decimal(str(row.protected_quantity))
    return totals
