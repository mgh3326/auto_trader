"""Explicit terminal handling for delisted (and similar) holdings.

Silent drop of a held name is forbidden. When a held symbol becomes delisted
as-of session ``t``, the harness emits a ``TerminalEvent`` and forces exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

__all__ = [
    "TerminalEvent",
    "TerminalReason",
    "force_exit_delisted_holdings",
]

TerminalReason = Literal["delisted", "suspended"]


@dataclass(frozen=True)
class TerminalEvent:
    session_date: date
    symbol: str
    reason: TerminalReason
    last_close: float | None


def force_exit_delisted_holdings(
    *,
    session_date: date,
    held_symbols: set[str],
    delisted_as_of: frozenset[str],
    last_close_by_symbol: dict[str, float],
) -> tuple[set[str], list[TerminalEvent]]:
    """Remove delisted holdings from ``held_symbols`` with explicit events.

    Returns the residual held set and the list of terminal events generated.
    """
    events: list[TerminalEvent] = []
    residual = set(held_symbols)
    for symbol in sorted(held_symbols & set(delisted_as_of)):
        events.append(
            TerminalEvent(
                session_date=session_date,
                symbol=symbol,
                reason="delisted",
                last_close=last_close_by_symbol.get(symbol),
            )
        )
        residual.discard(symbol)
    return residual, events
