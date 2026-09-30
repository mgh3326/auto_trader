"""Ladder rung approach / touch / fill tracker.

Anchors are open (non-terminal) order-ledger rows with a limit price —
see ``repository.py`` for the read.  Events are edge-driven:

- ``approach`` — a trade tick enters ±0.5% of the anchor while still on
  the resting order's untriggered side; re-arms when the price leaves the
  band without touching.
- ``touch`` — a trade tick crosses the anchor (buy: ``price <= anchor``,
  sell: ``price >= anchor``); once per rung lifetime.
- ``fill`` — first authoritative fill evidence for the rung's order,
  either an ``execution_ledger`` row or the order ledger reconciling to
  ``filled``; once per rung lifetime.

Order facts (``received_at``/``nxt_tradable``/``died_at``) are copied
from the ledger row only — NULL when unknown, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from .types import LadderRow, OwnFill, QuoteTick, RungAnchor

APPROACH_BAND = Decimal("0.005")  # ±0.5%

RungState = Literal["far", "near", "touched", "done"]


def _epoch_second(ts: datetime) -> int:
    return int(ts.timestamp())


@dataclass
class _RungRuntime:
    state: RungState = "far"
    filled: bool = False
    # Latest orderbook snapshot seen before the current trade tick.
    last_book: tuple[Decimal | None, Decimal | None] | None = None


class LadderTracker:
    """Stateful per-process tracker; rungs come from the repository read."""

    def __init__(self) -> None:
        self._rungs: dict[tuple[str, int], _RungRuntime] = {}
        self._last_books: dict[str, tuple[Decimal | None, Decimal | None]] = {}

    def prime(self, rungs: list[RungAnchor]) -> None:
        """(Re)seed the active rung universe, dropping resolved rungs."""
        active = {(r.ledger_name, r.ledger_id) for r in rungs}
        for key in list(self._rungs):
            if key not in active:
                # Terminal or gone — keep the fill marker so a late
                # execution-ledger row cannot double-record a fill.
                runtime = self._rungs[key]
                if not runtime.filled:
                    del self._rungs[key]
        for rung in rungs:
            self._rungs.setdefault((rung.ledger_name, rung.ledger_id), _RungRuntime())

    def mark_filled(self, rung_key: tuple[str, int]) -> None:
        runtime = self._rungs.setdefault(rung_key, _RungRuntime())
        runtime.filled = True
        runtime.state = "done"

    def _detail_book(self, key: tuple[str, int]) -> dict:
        book = self._rungs[key].last_book
        if book is None:
            return {}
        bid1, ask1 = book
        detail: dict[str, str] = {}
        if bid1 is not None:
            detail["bid1"] = str(bid1)
        if ask1 is not None:
            detail["ask1"] = str(ask1)
        return detail

    def observe_orderbook(self, tick: QuoteTick) -> None:
        """Remember the latest book snapshot for a symbol's rungs."""
        if tick.kind != "orderbook":
            return
        self._last_books[tick.symbol] = (tick.bid1, tick.ask1)

    def on_trade_tick(
        self, tick: QuoteTick, rungs: list[RungAnchor]
    ) -> list[LadderRow]:
        """Transition far→near→touched for every rung on this symbol."""
        if tick.kind != "trade" or tick.price is None:
            return []
        rows: list[LadderRow] = []
        book = self._last_books.get(tick.symbol)
        for rung in rungs:
            if rung.symbol != tick.symbol:
                continue
            key = (rung.ledger_name, rung.ledger_id)
            runtime = self._rungs.setdefault(key, _RungRuntime())
            if runtime.state in ("touched", "done"):
                continue
            if book is not None:
                runtime.last_book = book
            detail = self._detail_book(key)

            distance = (tick.price - rung.anchor_price) / rung.anchor_price
            crossed = (
                tick.price <= rung.anchor_price
                if rung.side == "buy"
                else tick.price >= rung.anchor_price
            )
            if crossed:
                runtime.state = "touched"
                rows.append(
                    LadderRow(
                        dedupe_key=(
                            f"ladder:{rung.ledger_name}:{rung.ledger_id}:touch:"
                            f"{_epoch_second(tick.ts)}"
                        ),
                        order_ledger=rung.ledger_name,
                        order_ledger_id=rung.ledger_id,
                        broker_order_id=rung.broker_order_id,
                        client_order_id=rung.client_order_id,
                        correlation_id=rung.correlation_id,
                        event_type="touch",
                        market=rung.market,
                        symbol=rung.symbol,
                        side=rung.side,
                        session=tick.session,
                        anchor_price=rung.anchor_price,
                        event_price=tick.price,
                        distance_pct=distance,
                        event_ts=tick.ts,
                        received_at=rung.received_at,
                        nxt_tradable=rung.nxt_tradable,
                        died_at=rung.died_at,
                        stream_entry_id=tick.entry_id,
                        fill_ledger_id=None,
                        detail=detail,
                    )
                )
                continue
            if abs(distance) <= APPROACH_BAND:
                if runtime.state == "far":
                    runtime.state = "near"
                    rows.append(
                        LadderRow(
                            dedupe_key=(
                                f"ladder:{rung.ledger_name}:{rung.ledger_id}:"
                                f"approach:{_epoch_second(tick.ts)}"
                            ),
                            order_ledger=rung.ledger_name,
                            order_ledger_id=rung.ledger_id,
                            broker_order_id=rung.broker_order_id,
                            client_order_id=rung.client_order_id,
                            correlation_id=rung.correlation_id,
                            event_type="approach",
                            market=rung.market,
                            symbol=rung.symbol,
                            side=rung.side,
                            session=tick.session,
                            anchor_price=rung.anchor_price,
                            event_price=tick.price,
                            distance_pct=distance,
                            event_ts=tick.ts,
                            received_at=rung.received_at,
                            nxt_tradable=rung.nxt_tradable,
                            died_at=rung.died_at,
                            stream_entry_id=tick.entry_id,
                            fill_ledger_id=None,
                            detail=detail,
                        )
                    )
            else:
                runtime.state = "far"
        return rows

    def on_fill(self, rung: RungAnchor, fill: OwnFill) -> LadderRow | None:
        """Record the rung's first fill evidence."""
        key = (rung.ledger_name, rung.ledger_id)
        runtime = self._rungs.setdefault(key, _RungRuntime())
        if runtime.filled:
            return None
        runtime.filled = True
        runtime.state = "done"
        return LadderRow(
            dedupe_key=(
                f"ladder:{rung.ledger_name}:{rung.ledger_id}:fill:{fill.ledger_id}"
            ),
            order_ledger=rung.ledger_name,
            order_ledger_id=rung.ledger_id,
            broker_order_id=rung.broker_order_id,
            client_order_id=rung.client_order_id,
            correlation_id=rung.correlation_id,
            event_type="fill",
            market=rung.market,
            symbol=rung.symbol,
            side=rung.side,
            session=None,  # ledger evidence carries no quote session
            anchor_price=rung.anchor_price,
            event_price=fill.price,
            distance_pct=(fill.price - rung.anchor_price) / rung.anchor_price,
            event_ts=fill.filled_at,
            received_at=rung.received_at,
            nxt_tradable=rung.nxt_tradable,
            died_at=rung.died_at,
            stream_entry_id=None,
            fill_ledger_id=fill.ledger_id,
            detail={"broker": fill.broker, "side_fill": fill.side},
        )

    def on_terminal(
        self,
        rung: RungAnchor,
        *,
        status: str,
        reconciled_at: datetime | None,
        avg_fill_price: Decimal | None,
    ) -> LadderRow | None:
        """Resolve a rung whose order row left the open set.

        ``filled`` produces a fill event whose ``died_at`` is the ledger's
        reconcile timestamp — the honest evidence for when the death was
        observed.  Any other terminal status just marks the rung done.
        A ``filled`` row without ``avg_fill_price`` records nothing:
        the fill price is never guessed.
        """
        key = (rung.ledger_name, rung.ledger_id)
        runtime = self._rungs.setdefault(key, _RungRuntime())
        if runtime.filled:
            return None
        runtime.state = "done"
        if status != "filled" or avg_fill_price is None or reconciled_at is None:
            # Without reconcile evidence there is no honest fill price or
            # fill timestamp — nothing is recorded instead of guessing.
            return None
        runtime.filled = True
        event_ts = reconciled_at
        return LadderRow(
            dedupe_key=(
                f"ladder:{rung.ledger_name}:{rung.ledger_id}:fill:"
                f"ledger:{_epoch_second(event_ts)}"
            ),
            order_ledger=rung.ledger_name,
            order_ledger_id=rung.ledger_id,
            broker_order_id=rung.broker_order_id,
            client_order_id=rung.client_order_id,
            correlation_id=rung.correlation_id,
            event_type="fill",
            market=rung.market,
            symbol=rung.symbol,
            side=rung.side,
            session=None,
            anchor_price=rung.anchor_price,
            event_price=avg_fill_price,
            distance_pct=(avg_fill_price - rung.anchor_price) / rung.anchor_price,
            event_ts=event_ts,
            received_at=rung.received_at,
            nxt_tradable=rung.nxt_tradable,
            died_at=reconciled_at,
            stream_entry_id=None,
            fill_ledger_id=None,
            detail={"terminal_status": status},
        )


__all__ = ["APPROACH_BAND", "LadderTracker"]
