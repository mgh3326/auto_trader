"""Upbit shadow-sim — the B0-X crypto 본선 lane. Zero real orders, anywhere.

Contract §3: *crypto 본선 = Upbit shadow-sim — 실계좌 아님 — 합성 체결
(record-only 인프라). 체결 증거 약함·신호/타이밍 증거 유효 라벨.*
Contract §4 footnote: *Upbit shadow 는 합성이므로 envelope 미적용(기록만).*

Read-only market data only: this module imports exactly one Upbit function,
``fetch_ohlcv`` (a public GET), and never touches accounts, balances, or
``app.services.brokers.upbit.orders``. The portfolio it trades is a JSON file.

=============================================================================
THE TOUCH RULE — what this lane counts as a fill
=============================================================================

Stated explicitly because everything downstream inherits its optimism.

A resting virtual order placed at level ``P`` at wall-clock ``t`` is filled
iff some **completed** 4h bar whose open time is at or after ``t`` satisfies:

    BUY   →  bar.low  <= P      filled at exactly P
    SELL  →  bar.high >= P      filled at exactly P

with these consequences made explicit rather than left implicit:

  1. **Completed bars only.** The Upbit minute endpoints return the forming
     bar; :func:`completed_bars` drops any bar whose window has not closed.
     A level touched by the in-progress bar is not a fill this cycle.
  2. **Fill price = limit price.** No slippage, no price improvement. For a
     buy this is pessimistic-neutral (a gap-down would really fill better);
     for a sell it is the same.
  3. **All-or-nothing.** No partial fills. Order book depth is not modelled.
  4. **No queue position.** This is the load-bearing optimism: a real resting
     limit at P only fills if the queue ahead of it clears. Touch-equals-fill
     systematically **over**-reports fills, most at levels the market barely
     grazes. This is exactly the bias the Binance Demo sidecar exists to
     measure, and why every artifact from this lane carries
     ``SHADOW_SYNTHETIC_FILL``.
  5. **Fees are charged** at :data:`UPBIT_KRW_FEE_RATE` on both sides, so P&L
     is not fee-free even though fills are free.
  6. **One bar, one order.** Each bar is evaluated against each resting order
     once; the earliest qualifying bar wins. A bar that touches both a buy and
     a sell level fills both — intrabar sequencing is unknowable from OHLC.

If a later calibration replaces this rule, the constant to change is here and
the label to update is ``SHADOW_SYNTHETIC_FILL`` — not the derivation core.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from app.services.brokers.upbit.client import fetch_ohlcv
from scripts.b0x.derivation import DerivedOrder
from scripts.b0x.scope import UPBIT_SHADOW_SCOPE_KEY
from scripts.b0x.state import B0XPosition, LaneAccountState

LANE: Final[str] = UPBIT_SHADOW_SCOPE_KEY
QUOTE_CURRENCY: Final[str] = "KRW"

#: Upbit KRW market fee, maker == taker == 0.05%.
UPBIT_KRW_FEE_RATE: Final[Decimal] = Decimal("0.0005")

#: 4h bar width — matches the policy table's ``config.candle_period``.
BAR_PERIOD: Final[str] = "4h"
BAR_WIDTH: Final[dt.timedelta] = dt.timedelta(hours=4)

#: Simulation bankroll. This is NOT a contract number — the §4 envelope is
#: explicitly not applied to this lane — it is a fixed, recorded starting
#: balance so the virtual book has a boundary at all. Stamped into every
#: artifact so a reader never has to guess what the P&L is a fraction of.
SEED_CASH_KRW: Final[Decimal] = Decimal("1000000")

#: How many 4h bars to pull when evaluating fills. 30 bars = 5 days, far more
#: than one cycle's gap, so a lane resumed after a pause still sees the bars
#: it slept through instead of silently skipping them.
FILL_LOOKBACK_BARS: Final[int] = 30

#: Upbit candle timestamps are KST-naive (``candle_date_time_kst``).
KST: Final[dt.timezone] = dt.timezone(dt.timedelta(hours=9))

TOUCH_RULE_ID: Final[str] = "b0x.shadow.touch_v1"
TOUCH_RULE_STATEMENT: Final[str] = (
    "완료된 4h 봉만 평가한다. 주문 접수 시각 이후 개장한 봉에 대해 "
    "BUY 는 bar.low <= 지정가, SELL 은 bar.high >= 지정가 이면 체결로 간주하고 "
    "체결가는 지정가 그대로다(슬리피지·부분체결·호가대기열 미모형, 수수료 0.05% 부과). "
    "터치≠체결이므로 이 규칙은 체결을 과대보고한다 — 그 편향의 크기를 재는 것이 "
    "Binance Demo 사이드카의 존재 이유다."
)


# ---------------------------------------------------------------------------
# Virtual book
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VirtualOrder:
    order_key: str
    symbol: str
    side: str
    leg: str
    price: Decimal
    quantity: Decimal
    notional: Decimal
    placed_at: dt.datetime
    cycle_id: str

    def to_json(self) -> dict[str, Any]:
        return {
            "order_key": self.order_key,
            "symbol": self.symbol,
            "side": self.side,
            "leg": self.leg,
            "price": format(self.price, "f"),
            "quantity": format(self.quantity, "f"),
            "notional": format(self.notional, "f"),
            "placed_at": self.placed_at.isoformat(),
            "cycle_id": self.cycle_id,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> VirtualOrder:
        return cls(
            order_key=payload["order_key"],
            symbol=payload["symbol"],
            side=payload["side"],
            leg=payload["leg"],
            price=Decimal(payload["price"]),
            quantity=Decimal(payload["quantity"]),
            notional=Decimal(payload["notional"]),
            placed_at=dt.datetime.fromisoformat(payload["placed_at"]),
            cycle_id=payload["cycle_id"],
        )


@dataclass(frozen=True, slots=True)
class SyntheticFill:
    order_key: str
    symbol: str
    side: str
    leg: str
    price: Decimal
    quantity: Decimal
    gross_notional: Decimal
    fee: Decimal
    bar_open_time: dt.datetime
    bar_low: Decimal
    bar_high: Decimal
    realized_pnl: Decimal | None

    def to_json(self) -> dict[str, Any]:
        return {
            "order_key": self.order_key,
            "symbol": self.symbol,
            "side": self.side,
            "leg": self.leg,
            "price": format(self.price, "f"),
            "quantity": format(self.quantity, "f"),
            "gross_notional": format(self.gross_notional, "f"),
            "fee": format(self.fee, "f"),
            "bar_open_time": self.bar_open_time.isoformat(),
            "bar_low": format(self.bar_low, "f"),
            "bar_high": format(self.bar_high, "f"),
            "realized_pnl": (
                None if self.realized_pnl is None else format(self.realized_pnl, "f")
            ),
            "touch_rule": TOUCH_RULE_ID,
        }


@dataclass
class VirtualPortfolio:
    """Mutable in-cycle book; persisted as JSON between cycles."""

    cash: Decimal
    positions: dict[str, B0XPosition]
    open_orders: list[VirtualOrder]
    utc_day: str
    new_entry_symbols_today: set[str]
    realized_pnl_today: Decimal
    realized_pnl_total: Decimal
    seed_cash: Decimal

    @classmethod
    def seed(cls, *, now: dt.datetime) -> VirtualPortfolio:
        return cls(
            cash=SEED_CASH_KRW,
            positions={},
            open_orders=[],
            utc_day=now.astimezone(dt.UTC).date().isoformat(),
            new_entry_symbols_today=set(),
            realized_pnl_today=Decimal("0"),
            realized_pnl_total=Decimal("0"),
            seed_cash=SEED_CASH_KRW,
        )

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> VirtualPortfolio:
        return cls(
            cash=Decimal(payload["cash"]),
            positions={
                symbol: B0XPosition(
                    symbol=symbol,
                    quantity=Decimal(row["quantity"]),
                    average_price=Decimal(row["average_price"]),
                    invested_notional=Decimal(row["invested_notional"]),
                    entry_count=int(row["entry_count"]),
                )
                for symbol, row in (payload.get("positions") or {}).items()
            },
            open_orders=[
                VirtualOrder.from_json(row) for row in payload.get("open_orders") or []
            ],
            utc_day=payload["utc_day"],
            new_entry_symbols_today=set(payload.get("new_entry_symbols_today") or []),
            realized_pnl_today=Decimal(payload.get("realized_pnl_today", "0")),
            realized_pnl_total=Decimal(payload.get("realized_pnl_total", "0")),
            seed_cash=Decimal(payload.get("seed_cash", str(SEED_CASH_KRW))),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "cash": format(self.cash, "f"),
            "positions": {
                symbol: pos.canonical()
                for symbol, pos in sorted(self.positions.items())
            },
            "open_orders": [order.to_json() for order in self.open_orders],
            "utc_day": self.utc_day,
            "new_entry_symbols_today": sorted(self.new_entry_symbols_today),
            "realized_pnl_today": format(self.realized_pnl_today, "f"),
            "realized_pnl_total": format(self.realized_pnl_total, "f"),
            "seed_cash": format(self.seed_cash, "f"),
        }

    def roll_utc_day(self, *, now: dt.datetime) -> bool:
        """Reset the daily counters when the UTC day changed. Returns True if so."""

        today = now.astimezone(dt.UTC).date().isoformat()
        if today == self.utc_day:
            return False
        self.utc_day = today
        self.new_entry_symbols_today = set()
        self.realized_pnl_today = Decimal("0")
        return True

    def account_state(self) -> LaneAccountState:
        return LaneAccountState(
            lane=LANE,
            quote_currency=QUOTE_CURRENCY,
            cash=self.cash,
            positions=tuple(
                self.positions[symbol] for symbol in sorted(self.positions)
            ),
            new_entry_symbols_today=tuple(sorted(self.new_entry_symbols_today)),
            realized_pnl_today=self.realized_pnl_today,
            open_order_keys=tuple(
                sorted(order.order_key for order in self.open_orders)
            ),
            # A virtual book has, by construction, no foreign writer: nothing
            # but this adapter can append to the JSON file it lives in. The
            # writer lock is what protects that claim.
            foreign_open_order_count=0,
            foreign_position_symbols=(),
        )


# ---------------------------------------------------------------------------
# Bars + the touch rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    open_time: dt.datetime  # tz-aware UTC
    high: Decimal
    low: Decimal
    close: Decimal


def completed_bars(frame: Any, *, now: dt.datetime) -> list[Bar]:
    """Convert an Upbit OHLCV frame to ascending, **completed** bars.

    Upbit's minute endpoints include the forming bar; a bar opening at ``T`` is
    complete only once ``now >= T + BAR_WIDTH``. Dropping it here is what makes
    rule (1) of the touch rule true.
    """

    if frame is None or getattr(frame, "empty", True):
        return []
    now_utc = now.astimezone(dt.UTC)
    bars: list[Bar] = []
    for _, raw in frame.iterrows():
        stamp = raw["datetime"]
        open_time = stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
        if open_time.tzinfo is None:
            open_time = open_time.replace(tzinfo=KST)
        open_time = open_time.astimezone(dt.UTC)
        if open_time + BAR_WIDTH > now_utc:
            continue
        bars.append(
            Bar(
                open_time=open_time,
                high=Decimal(str(raw["high"])),
                low=Decimal(str(raw["low"])),
                close=Decimal(str(raw["close"])),
            )
        )
    bars.sort(key=lambda bar: bar.open_time)
    return bars


def first_touch(order: VirtualOrder, bars: list[Bar]) -> Bar | None:
    """Earliest completed bar at/after placement that touches ``order.price``."""

    for bar in bars:
        if bar.open_time < order.placed_at:
            continue
        if order.side == "buy" and bar.low <= order.price:
            return bar
        if order.side == "sell" and bar.high >= order.price:
            return bar
    return None


async def fetch_bars(symbols: list[str], *, now: dt.datetime) -> dict[str, list[Bar]]:
    """Read-only public candle fetch for the symbols with resting orders."""

    out: dict[str, list[Bar]] = {}
    for symbol in sorted(set(symbols)):
        try:
            frame = await fetch_ohlcv(
                market=symbol, days=FILL_LOOKBACK_BARS, period=BAR_PERIOD
            )
        except Exception:  # noqa: BLE001 — per-symbol, recorded as "no bars"
            out[symbol] = []
            continue
        out[symbol] = completed_bars(frame, now=now)
    return out


def apply_fills(
    portfolio: VirtualPortfolio, bars_by_symbol: dict[str, list[Bar]]
) -> list[SyntheticFill]:
    """Settle every resting order the touch rule says filled. Mutates ``portfolio``."""

    fills: list[SyntheticFill] = []
    remaining: list[VirtualOrder] = []

    for order in sorted(portfolio.open_orders, key=lambda o: (o.symbol, o.order_key)):
        bar = first_touch(order, bars_by_symbol.get(order.symbol, []))
        if bar is None:
            remaining.append(order)
            continue

        gross = order.price * order.quantity
        fee = gross * UPBIT_KRW_FEE_RATE
        filled_qty = order.quantity
        realized: Decimal | None = None
        position = portfolio.positions.get(order.symbol)

        if order.side == "buy":
            portfolio.cash -= gross + fee
            if position is None:
                portfolio.positions[order.symbol] = B0XPosition(
                    symbol=order.symbol,
                    quantity=order.quantity,
                    average_price=order.price,
                    invested_notional=gross,
                    entry_count=1,
                )
                portfolio.new_entry_symbols_today.add(order.symbol)
            else:
                total_qty = position.quantity + order.quantity
                new_avg = (position.cost_basis + gross) / total_qty
                portfolio.positions[order.symbol] = B0XPosition(
                    symbol=order.symbol,
                    quantity=total_qty,
                    average_price=new_avg,
                    invested_notional=position.invested_notional + gross,
                    entry_count=position.entry_count + 1,
                )
        else:
            if position is None or position.quantity <= 0:
                # Cannot sell what the book does not hold. Drop the order
                # rather than inventing a short — B0-X is long-only by
                # construction, and a resting sell whose position was already
                # closed is stale intent, not a fill.
                continue
            filled_qty = min(order.quantity, position.quantity)
            gross = order.price * filled_qty
            fee = gross * UPBIT_KRW_FEE_RATE
            realized = (order.price - position.average_price) * filled_qty - fee
            portfolio.cash += gross - fee
            portfolio.realized_pnl_today += realized
            portfolio.realized_pnl_total += realized
            leftover = position.quantity - filled_qty
            if leftover > 0:
                portfolio.positions[order.symbol] = B0XPosition(
                    symbol=order.symbol,
                    quantity=leftover,
                    average_price=position.average_price,
                    invested_notional=position.invested_notional,
                    entry_count=position.entry_count,
                )
            else:
                del portfolio.positions[order.symbol]

        fills.append(
            SyntheticFill(
                order_key=order.order_key,
                symbol=order.symbol,
                side=order.side,
                leg=order.leg,
                price=order.price,
                quantity=filled_qty,
                gross_notional=gross,
                fee=fee,
                bar_open_time=bar.open_time,
                bar_low=bar.low,
                bar_high=bar.high,
                realized_pnl=realized,
            )
        )

    portfolio.open_orders = remaining
    return fills


def place_derived_orders(
    portfolio: VirtualPortfolio,
    orders: tuple[DerivedOrder, ...],
    *,
    now: dt.datetime,
    cycle_id: str,
) -> list[VirtualOrder]:
    """Cancel-replace the virtual book with this cycle's derived intent.

    Each cycle re-derives from a fresh table, so a stale resting order from an
    older table is not "still B0's intent" — it is a level the rule no longer
    holds. Replacing (rather than accumulating) keeps the book equal to the
    current table's intent, and an unchanged derivation reproduces an identical
    book because ``order_key`` is a pure function of the inputs.
    """

    portfolio.open_orders = []
    placed: list[VirtualOrder] = []
    for order in orders:
        if order.side == "buy":
            if order.notional is None or order.table_price <= 0:
                continue
            quantity = order.notional / order.table_price
            notional = order.notional
        else:
            position = portfolio.positions.get(order.symbol)
            if position is None or position.quantity <= 0:
                continue
            fraction = order.quantity_fraction or Decimal("0")
            quantity = position.quantity * fraction
            if quantity <= 0:
                continue
            notional = quantity * order.table_price
        placed.append(
            VirtualOrder(
                order_key=order.order_key,
                symbol=order.symbol,
                side=order.side,
                leg=order.leg,
                price=order.table_price,
                quantity=quantity,
                notional=notional,
                placed_at=now.astimezone(dt.UTC),
                cycle_id=cycle_id,
            )
        )
    portfolio.open_orders = placed
    return placed


__all__ = [
    "LANE",
    "QUOTE_CURRENCY",
    "UPBIT_KRW_FEE_RATE",
    "SEED_CASH_KRW",
    "TOUCH_RULE_ID",
    "TOUCH_RULE_STATEMENT",
    "Bar",
    "VirtualOrder",
    "VirtualPortfolio",
    "SyntheticFill",
    "completed_bars",
    "first_touch",
    "fetch_bars",
    "apply_fills",
    "place_derived_orders",
]
