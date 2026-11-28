"""hk 7119 (6939 I3) — the pure daily-loss calculator.

One KST day, one account, everything in KRW as ``Decimal``. The day's P&L is

* realized: today's sells matched FIFO against the lots open at the start of the
  day (priced at the previous session's close, the day's starting mark) and
  today's buys (priced at cost plus fees and tax), minus the sell's fees and tax;
* plus mark-to-market: every lot still open, at a fresh quote, against the
  same basis.

If any part cannot be measured the result carries closed reasons and no number
(operator answer Q2: unknown is not zero), and :func:`should_freeze` freezes.
No database, broker, network or clock here.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

#: Locked (design 6939 §3 I3). No env, CLI or argument can change it.
DAILY_LOSS_LIMIT_KRW: Final = Decimal("500000")
#: A quote older than this at the run is not fresh.
QUOTE_MAX_AGE: Final = timedelta(minutes=10)

#: Closed reasons for an unmeasurable day.
REASONS: Final = frozenset(
    {
        "ledger_unreadable",
        "position_oversold",
        "fee_not_provided",
        "fee_currency_mismatch",
        "fx_unavailable",
        "provisional_fill_unreconciled",
        "reference_price_unavailable",
        "quote_unavailable",
        "quote_stale",
        "currency_mismatch",
    }
)


@dataclass(frozen=True)
class Fill:
    """One in-effect authoritative ledger fill, in the fill currency."""

    symbol: str
    side: str  # "buy" | "sell"
    quantity: Decimal
    price: Decimal
    fee: Decimal | None  # None = not provided
    tax: Decimal | None
    fee_currency: str | None
    currency: str
    filled_at: datetime
    fx_krw: Decimal | None  # KRW per one unit of ``currency`` at the fill


@dataclass(frozen=True)
class Mark:
    price: Decimal
    observed_at: datetime
    fx_krw: Decimal | None


@dataclass(frozen=True)
class LossResult:
    pnl_krw: Decimal | None
    reasons: tuple[str, ...]

    @property
    def measured(self) -> bool:
        return self.pnl_krw is not None and not self.reasons


@dataclass
class _Lot:
    quantity: Decimal
    unit_cost_krw: Decimal


def _krw(amount: Decimal, currency: str, fx: Decimal | None) -> Decimal | None:
    if currency == "KRW":
        return amount
    if fx is None or fx <= 0:
        return None
    return amount * fx


def compute_daily_pnl(
    *,
    currency: str,
    history: list[Fill],
    today: list[Fill],
    reference: dict[str, Mark | None],
    quotes: dict[str, Mark | None],
    now: datetime,
    provisional_unreconciled: bool = False,
) -> LossResult:
    """``history``: fills before the KST day start; ``today``: fills in the day."""
    reasons: set[str] = set()
    if provisional_unreconciled:
        reasons.add("provisional_fill_unreconciled")

    start_qty: dict[str, Decimal] = {}
    for fill in history:
        sign = Decimal(1) if fill.side == "buy" else Decimal(-1)
        start_qty[fill.symbol] = start_qty.get(fill.symbol, Decimal(0)) + (
            sign * fill.quantity
        )

    lots: dict[str, deque[_Lot]] = {}
    for symbol, qty in sorted(start_qty.items()):
        if qty < 0:
            reasons.add("position_oversold")
            continue
        if qty == 0:
            continue
        mark = reference.get(symbol)
        if mark is None:
            reasons.add("reference_price_unavailable")
            continue
        unit = _krw(mark.price, currency, mark.fx_krw)
        if unit is None:
            reasons.add("fx_unavailable")
            continue
        lots[symbol] = deque([_Lot(qty, unit)])

    realized = Decimal(0)
    for fill in sorted(today, key=lambda f: f.filled_at):
        if fill.currency != currency:
            reasons.add("currency_mismatch")
            continue
        if fill.fee is None:
            reasons.add("fee_not_provided")
            continue
        if fill.fee_currency is not None and fill.fee_currency != fill.currency:
            reasons.add("fee_currency_mismatch")
            continue
        price = _krw(fill.price, currency, fill.fx_krw)
        costs = _krw(fill.fee + (fill.tax or Decimal(0)), currency, fill.fx_krw)
        if price is None or costs is None:
            reasons.add("fx_unavailable")
            continue
        book = lots.setdefault(fill.symbol, deque())
        if fill.side == "buy":
            book.append(_Lot(fill.quantity, price + costs / fill.quantity))
            continue
        remaining = fill.quantity
        realized -= costs
        while remaining > 0 and book:
            lot = book[0]
            take = min(lot.quantity, remaining)
            realized += (price - lot.unit_cost_krw) * take
            lot.quantity -= take
            remaining -= take
            if lot.quantity == 0:
                book.popleft()
        if remaining > 0:
            reasons.add("position_oversold")

    unrealized = Decimal(0)
    for symbol, book in sorted(lots.items()):
        open_qty = sum((lot.quantity for lot in book), Decimal(0))
        if open_qty == 0:
            continue
        quote = quotes.get(symbol)
        if quote is None:
            reasons.add("quote_unavailable")
            continue
        if now - quote.observed_at > QUOTE_MAX_AGE:
            reasons.add("quote_stale")
            continue
        mark = _krw(quote.price, currency, quote.fx_krw)
        if mark is None:
            reasons.add("fx_unavailable")
            continue
        unrealized += sum(
            ((mark - lot.unit_cost_krw) * lot.quantity for lot in book), Decimal(0)
        )

    if reasons:
        return LossResult(pnl_krw=None, reasons=tuple(sorted(reasons)))
    return LossResult(pnl_krw=realized + unrealized, reasons=())


def should_freeze(result: LossResult) -> bool:
    """Freeze at a loss of the limit or more, and whenever it is unmeasured."""
    if not result.measured:
        return True
    assert result.pnl_krw is not None
    return result.pnl_krw <= -DAILY_LOSS_LIMIT_KRW
