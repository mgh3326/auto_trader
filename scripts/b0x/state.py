"""B0-X account state — the second half of "같은 표 + 같은 계좌상태 → 같은 주문".

Contract §2-1 makes the derived order a pure function of (policy table,
account state). That only holds if "account state" is itself a closed,
hashable value — hence this module: every input derivation is allowed to see
lives on :class:`LaneAccountState`, and :meth:`LaneAccountState.state_hash`
is what the determinism check compares.

**B0-X positions are B0-X's own, not the venue's.** The Upbit shadow lane
holds virtual positions in its own ledger, and the Binance sidecar attributes
only lifecycles carrying B0-X's correlation prefix. Positions the account
already held for other reasons are recorded as ``foreign_*`` and drive the
``CONTAMINATED`` marking (contract §2-3, writer = 1) — they are never treated
as B0-X inventory to average down or sell.

:attr:`LaneAccountState.broker_truth` is the *other* kind of position view and
the two must not be conflated: :attr:`~LaneAccountState.positions` is
attribution-scoped and drives sizing (averaging needs B0-X's own cost basis),
while :class:`~scripts.b0x.broker_truth.BrokerTruth` is account-wide and drives
the §4 caps (contract v1.5 ①). It is a required field with no default,
deliberately — the defect it replaces was a cap input that defaulted to empty
every cycle because its state file never existed, so "forgot to pass it" must
be a construction error rather than a silently-zero counter.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from scripts.b0x.broker_truth import BrokerTruth


def _dec(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True, slots=True)
class B0XPosition:
    """One B0-X-owned position, keyed by the *table's* symbol spelling."""

    symbol: str
    quantity: Decimal
    average_price: Decimal
    #: Cumulative quote-currency notional put into this symbol (new + adds).
    #: Drives the §4 per-symbol total cap; never decreases on a partial sell,
    #: because the cap is on *deployment*, not on current exposure.
    invested_notional: Decimal
    entry_count: int

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.average_price

    def canonical(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": _dec(self.quantity),
            "average_price": _dec(self.average_price),
            "invested_notional": _dec(self.invested_notional),
            "entry_count": self.entry_count,
        }


@dataclass(frozen=True, slots=True)
class LaneAccountState:
    """Everything derivation is allowed to know about the account."""

    lane: str
    quote_currency: str
    cash: Decimal
    #: Contract v1.5 ① cap inputs, read from the broker this cycle. Required:
    #: see the module docstring on why this has no default.
    broker_truth: BrokerTruth
    positions: tuple[B0XPosition, ...] = ()
    #: Realized P&L for the current UTC day; negative == loss (§4 kill switch).
    realized_pnl_today: Decimal = Decimal("0")
    #: Same-cycle net asset value (cash + mark-to-market positions), in
    #: ``quote_currency``. ``None`` for lanes whose envelope uses an absolute
    #: ``daily_loss_kill`` (it is not read there). A lane whose envelope uses
    #: ``daily_loss_kill_basis="pct_of_nav"`` (KR) must supply it —
    #: ``kill_switch.evaluate`` fails closed otherwise, because a NAV-relative
    #: threshold has no absolute value to compare ``realized_pnl_today``
    #: against without it. Deliberately part of the hashed derivation input
    #: (below): NAV depends on mark-to-market prices that are not otherwise
    #: captured by ``positions`` (which carries cost basis, not current
    #: price), so two cycles with identical cash/positions but different NAV
    #: are, correctly, a different account state for a pct_of_nav lane.
    nav: Decimal | None = None
    #: B0-X orders still working at the venue / in the virtual book.
    open_order_keys: tuple[str, ...] = ()
    #: Venue state NOT attributable to B0-X — the CONTAMINATED signal.
    foreign_open_order_count: int = 0
    foreign_position_symbols: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def position(self, symbol: str) -> B0XPosition | None:
        for pos in self.positions:
            if pos.symbol == symbol:
                return pos
        return None

    @property
    def contaminated(self) -> bool:
        return bool(self.foreign_open_order_count or self.foreign_position_symbols)

    def canonical(self) -> dict[str, Any]:
        """Full sorted view, for the observation record."""

        return {
            **self.canonical_derivation_input(),
            "open_order_keys": sorted(self.open_order_keys),
        }

    def canonical_derivation_input(self) -> dict[str, Any]:
        """Only the fields derivation actually reads — the hashing input.

        ``open_order_keys`` is deliberately excluded. B0-X's own resting orders
        are an *output* of the previous cycle's derivation, not an input to
        this one (nothing in :mod:`scripts.b0x.derivation` reads them). Hashing
        them would make the cycle identity self-referential: an unchanged table
        and an unchanged position would still produce a new ``cycle_id`` every
        cycle, and with it a new venue ``clientOrderId`` — destroying the
        idempotency that key exists to provide.

        ``foreign_*`` stays in: it is a real input, because it gates submission.
        """

        return {
            "lane": self.lane,
            "quote_currency": self.quote_currency,
            "cash": _dec(self.cash),
            "positions": [
                pos.canonical()
                for pos in sorted(self.positions, key=lambda p: p.symbol)
            ],
            "broker_truth": self.broker_truth.canonical(),
            "realized_pnl_today": _dec(self.realized_pnl_today),
            "nav": None if self.nav is None else _dec(self.nav),
            "foreign_open_order_count": self.foreign_open_order_count,
            "foreign_position_symbols": sorted(self.foreign_position_symbols),
        }

    def state_hash(self) -> str:
        blob = json.dumps(
            self.canonical_derivation_input(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        return f"sha256:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}"


__all__ = ["B0XPosition", "LaneAccountState"]
