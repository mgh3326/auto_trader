"""Fixed-seed computed control ledger. This module has no broker or DB imports.

The predeclared grid is the H5 opportunity table: one complete 4h bar and
fresh bid/ask for each universe symbol, committed before signal evaluation.
Controls match every closed H5 trip by entry week, symbol and direction.
They are never submitted to the Demo account.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from .constants import TAKER_FEE_RATE

CONTROL_SEED = 84720260928
_KST = ZoneInfo("Asia/Seoul")
_FOUR_HOUR_MS = 4 * 60 * 60 * 1000


class ControlUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    decision_ts: int
    high: Decimal
    low: Decimal
    close: Decimal
    bid: Decimal
    ask: Decimal

    def quote(self, side: str) -> Decimal:
        return self.ask if side == "BUY" else self.bid


@dataclass(frozen=True)
class ActualTrip:
    signal_key: str
    symbol: str
    side: str
    decision_ts: int
    entry_notional_usdt: Decimal
    opened_at: dt.datetime
    closed_at: dt.datetime
    gross_pnl_usdt: Decimal
    fees_usdt: Decimal

    @property
    def net_pnl_usdt(self) -> Decimal:
        return self.gross_pnl_usdt - self.fees_usdt


@dataclass(frozen=True)
class ControlTrip:
    signal_key: str
    symbol: str
    side: str
    control_decision_ts: int
    opened_at: dt.datetime
    closed_at: dt.datetime
    entry_price: Decimal
    qty: Decimal
    gross_pnl_usdt: Decimal
    fees_usdt: Decimal
    exit_reason: str

    @property
    def net_pnl_usdt(self) -> Decimal:
        return self.gross_pnl_usdt - self.fees_usdt


def _week_key(ms: int) -> tuple[int, int]:
    date = dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).astimezone(_KST).date()
    iso = date.isocalendar()
    return iso.year, iso.week


def _at(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC)


def _simulate(
    actual: ActualTrip,
    entry: Opportunity,
    future: list[Opportunity],
) -> ControlTrip:
    if len(future) < 6 or any(
        future[i].decision_ts != entry.decision_ts + (i + 1) * _FOUR_HOUR_MS
        for i in range(6)
    ):
        raise ControlUnavailable("control opportunity lacks six complete future bars")
    long = actual.side == "BUY"
    entry_price = entry.quote(actual.side)
    if entry_price <= 0 or actual.entry_notional_usdt <= 0:
        raise ControlUnavailable("control entry quote or risk unavailable")
    qty = actual.entry_notional_usdt / entry_price
    remaining = qty
    gross = Decimal(0)
    fees = actual.entry_notional_usdt * TAKER_FEE_RATE
    half = qty * Decimal("0.5")
    tp1_done = False
    reason = "time_exit"
    closed_at = _at(future[5].decision_ts)

    def book_close(amount: Decimal, price: Decimal) -> None:
        nonlocal remaining, gross, fees
        sign = Decimal(1) if long else Decimal(-1)
        gross += sign * (price - entry_price) * amount
        fees += amount * price * TAKER_FEE_RATE
        remaining -= amount

    for index, bar in enumerate(future[:6], start=1):
        exit_quote = bar.bid if long else bar.ask
        hard = entry_price * (Decimal("0.95") if long else Decimal("1.05"))
        soft = entry_price * (Decimal("0.97") if long else Decimal("1.03"))
        tp1 = entry_price * (Decimal("1.03") if long else Decimal("0.97"))
        tp2 = entry_price * (Decimal("1.05") if long else Decimal("0.95"))
        # Ambiguous intrabar sequence is conservatively resolved in stop order.
        if bar.low <= hard if long else bar.high >= hard:
            book_close(
                remaining, min(hard, exit_quote) if long else max(hard, exit_quote)
            )
            reason = "hard_stop"
        elif bar.close <= soft if long else bar.close >= soft:
            book_close(remaining, exit_quote)
            reason = "bar_close_stop"
        elif index == 6:
            book_close(remaining, exit_quote)
            reason = "time_exit"
        else:
            if not tp1_done and (exit_quote >= tp1 if long else exit_quote <= tp1):
                book_close(half, exit_quote)
                tp1_done = True
            if bar.bid >= tp2 if long else bar.ask <= tp2:
                book_close(remaining, exit_quote)
                reason = "tp2"
        if remaining <= 0:
            closed_at = _at(bar.decision_ts)
            break
    if remaining != 0:
        raise ControlUnavailable("control position did not close by six bars")
    return ControlTrip(
        signal_key=actual.signal_key,
        symbol=actual.symbol,
        side=actual.side,
        control_decision_ts=entry.decision_ts,
        opened_at=_at(entry.decision_ts),
        closed_at=closed_at,
        entry_price=entry_price,
        qty=qty,
        gross_pnl_usdt=gross,
        fees_usdt=fees,
        exit_reason=reason,
    )


def build_control_ledger(
    actual_trips: list[ActualTrip],
    grid: list[Opportunity],
    *,
    seed: int = CONTROL_SEED,
) -> list[ControlTrip]:
    """One computed row per actual trip; matched week/symbol/side proportions."""
    rng = random.Random(seed)
    ordered_grid = sorted(grid, key=lambda row: (row.symbol, row.decision_ts))
    by_symbol = {
        symbol: [row for row in ordered_grid if row.symbol == symbol]
        for symbol in {row.symbol for row in ordered_grid}
    }
    used: set[tuple[str, int]] = set()
    control: list[ControlTrip] = []
    for actual in sorted(actual_trips, key=lambda row: row.signal_key):
        candidates = [
            row
            for row in by_symbol.get(actual.symbol, [])
            if _week_key(row.decision_ts) == _week_key(actual.decision_ts)
            and row.decision_ts != actual.decision_ts
            and (row.symbol, row.decision_ts) not in used
        ]
        rng.shuffle(candidates)
        chosen: ControlTrip | None = None
        for entry in candidates:
            future = [
                row
                for row in by_symbol[actual.symbol]
                if entry.decision_ts
                < row.decision_ts
                <= entry.decision_ts + 6 * _FOUR_HOUR_MS
            ]
            try:
                chosen = _simulate(actual, entry, future)
            except ControlUnavailable:
                continue
            used.add((entry.symbol, entry.decision_ts))
            break
        if chosen is None:
            raise ControlUnavailable(
                f"no matched control opportunity for {actual.signal_key}"
            )
        control.append(chosen)
    return control
