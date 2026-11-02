"""Pure per-sleeve book: lots, P&L and cash used (ROB-1470).

No database, broker or clock. ``store.load_sleeve_inputs`` supplies the sleeve's
own orders (rungs of proposals tagged with this sleeve) and the fills already
attributed to those orders; nothing here can see another sleeve's rows, which is
what keeps one sleeve's results apart from every other book.

* **Lots** are FIFO per symbol over the attributed fills, in fill order.
* **Realized P&L** is matched sells against those lots. A sell quantity the
  sleeve never bought (for example a cut of a lot bought outside the sleeve) has
  no sleeve cost basis: it is reported as ``unmatched_sell_quantity`` and adds
  nothing to realized P&L.
* **Unrealized P&L** needs a mark for every held symbol. One missing mark makes
  it ``None`` -- unknown, never zero. A rung with a ledger fill that could not
  be tied to it (``unattributed_rungs``) makes net P&L unknown too.
* **Cash used** = held cost basis + open buy orders + filled quantity the order
  state already reports but the ledger has not booked yet. The last term keeps
  a reconcile lag from understating what the sleeve has spent. A ``filled``
  buy rung that reports no quantity and has no booked fill is unmeasured: its
  spend is unknown, not zero.
* Every buy rung of every proposal counts, including the proposal being gated:
  its ``pending_approval`` rungs are not open-order states, so the gate adds
  them once as the order under evaluation, while its rungs already at the
  broker stay in ``used``.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

ZERO = Decimal("0")

#: Rung states whose order is, or may be, at the broker and not yet terminal.
#: ``revalidating`` is included: an approval in flight can reach the broker.
OPEN_ORDER_STATES = frozenset(
    {
        "revalidating",
        "approved",
        "submitting",
        "acked",
        "resting",
        "partially_filled",
        "unverified",
    }
)
#: Terminal rung states that may carry a fill the ledger has not booked yet.
FILLED_TERMINAL_STATES = frozenset({"filled", "cancelled", "expired"})


@dataclass(frozen=True)
class SleeveOrder:
    """One rung of a proposal tagged with the sleeve."""

    proposal_pk: int
    rung_id: int
    symbol: str
    side: str
    state: str
    quantity: Decimal
    limit_price: Decimal | None
    notional: Decimal | None
    rung_filled_qty: Decimal | None
    #: The signal this order belongs to: the proposal lineage root. Rungs,
    #: ladders, revisions and partial fills of one root are one signal.
    signal_key: str | None = None
    signal_at: datetime | None = None


@dataclass(frozen=True)
class SleeveFill:
    """One execution_ledger fill attributed to one sleeve order."""

    ledger_id: int
    rung_id: int
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class SymbolPosition:
    symbol: str
    quantity: Decimal
    cost_basis: Decimal
    mark: Decimal | None
    unrealized_pnl: Decimal | None


@dataclass(frozen=True)
class SleeveBook:
    positions: tuple[SymbolPosition, ...]
    held_cost_basis: Decimal
    realized_pnl: Decimal
    fees: Decimal
    unrealized_pnl: Decimal | None
    net_pnl: Decimal | None
    open_order_notional: Decimal
    unbooked_fill_notional: Decimal
    used: Decimal | None
    trades: int
    unmatched_sell_quantity: Decimal
    missing_marks: tuple[str, ...] = ()
    unmeasured_orders: tuple[int, ...] = field(default=())
    unattributed_rungs: tuple[int, ...] = field(default=())

    @property
    def loss(self) -> Decimal | None:
        """The loss as a non-negative amount; ``None`` when P&L is unknown."""
        if self.net_pnl is None:
            return None
        return max(-self.net_pnl, ZERO)


def _order_price(order: SleeveOrder) -> Decimal | None:
    price = order.limit_price
    return price if price is not None and price > 0 else None


def order_notional(order: SleeveOrder) -> Decimal | None:
    """Notional of a whole order: limit price x quantity, else its notional."""
    price = _order_price(order)
    if price is not None and order.quantity > 0:
        return price * order.quantity
    if order.notional is not None and order.notional > 0:
        return order.notional
    return None


def compute_sleeve_book(
    *,
    orders: Sequence[SleeveOrder],
    fills: Iterable[SleeveFill],
    marks: Mapping[str, Decimal | None],
    unattributed_rungs: Collection[int] = (),
) -> SleeveBook:
    """Fold one sleeve's orders and attributed fills into its book."""
    fill_list = sorted(fills, key=lambda f: (f.filled_at, f.ledger_id))
    attributed_qty: dict[int, Decimal] = defaultdict(lambda: ZERO)
    attributed_notional: dict[int, Decimal] = defaultdict(lambda: ZERO)
    queues: dict[str, deque[list[Decimal]]] = defaultdict(deque)
    realized = ZERO
    fees = ZERO
    unmatched = ZERO
    traded_rungs: set[int] = set()
    for fill in fill_list:
        if fill.quantity <= 0 or fill.price <= 0:
            continue
        attributed_qty[fill.rung_id] += fill.quantity
        attributed_notional[fill.rung_id] += fill.quantity * fill.price
        traded_rungs.add(fill.rung_id)
        fees += fill.fee
        queue = queues[fill.symbol]
        if fill.side == "buy":
            queue.append([fill.quantity, fill.price])
            continue
        remaining = fill.quantity
        while remaining > 0 and queue:
            head = queue[0]
            matched = min(remaining, head[0])
            realized += (fill.price - head[1]) * matched
            head[0] -= matched
            remaining -= matched
            if head[0] <= 0:
                queue.popleft()
        unmatched += remaining

    positions: list[SymbolPosition] = []
    held_cost = ZERO
    unrealized: Decimal | None = ZERO
    missing: list[str] = []
    for symbol in sorted(queues):
        qty = sum((lot[0] for lot in queues[symbol]), ZERO)
        if qty <= 0:
            continue
        cost = sum((lot[0] * lot[1] for lot in queues[symbol]), ZERO)
        held_cost += cost
        mark = marks.get(symbol)
        if mark is None or mark <= 0:
            missing.append(symbol)
            positions.append(SymbolPosition(symbol, qty, cost, None, None))
            unrealized = None
            continue
        pnl = mark * qty - cost
        positions.append(SymbolPosition(symbol, qty, cost, mark, pnl))
        if unrealized is not None:
            unrealized += pnl

    open_notional = ZERO
    unbooked = ZERO
    unmeasured: list[int] = []
    for order in orders:
        if order.side != "buy":
            continue
        booked_qty = attributed_qty[order.rung_id]
        if order.state in OPEN_ORDER_STATES:
            price = _order_price(order)
            if price is not None:
                open_notional += max(order.quantity - booked_qty, ZERO) * price
            elif order.notional is not None and order.notional > 0:
                open_notional += max(
                    order.notional - attributed_notional[order.rung_id], ZERO
                )
            else:
                unmeasured.append(order.rung_id)
            continue
        if order.state in FILLED_TERMINAL_STATES:
            if (
                order.state == "filled"
                and order.rung_filled_qty is None
                and booked_qty <= 0
            ):
                # Filled, but neither the order nor the ledger says how much.
                unmeasured.append(order.rung_id)
                continue
            reported = order.rung_filled_qty or ZERO
            gap = reported - booked_qty
            if gap <= 0:
                continue
            price = _order_price(order)
            if price is None:
                unmeasured.append(order.rung_id)
                continue
            unbooked += gap * price

    unattributed = tuple(sorted(set(unattributed_rungs)))
    net = None if unrealized is None or unattributed else realized + unrealized - fees
    used = None if unmeasured else held_cost + open_notional + unbooked
    return SleeveBook(
        positions=tuple(positions),
        held_cost_basis=held_cost,
        realized_pnl=realized,
        fees=fees,
        unrealized_pnl=unrealized,
        net_pnl=net,
        open_order_notional=open_notional,
        unbooked_fill_notional=unbooked,
        used=used,
        trades=len(traded_rungs),
        unmatched_sell_quantity=unmatched,
        missing_marks=tuple(missing),
        unmeasured_orders=tuple(sorted(unmeasured)),
        unattributed_rungs=unattributed,
    )
