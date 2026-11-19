"""Operator-declared US ticker changes for due paper exits (hk 5518).

An episode keeps the symbol it entered with (the journal column is immutable).
When the operator declares ``OLD=NEW`` and the broker evidence proves the swap,
only the exit leg is sent as NEW and its journal meta records the mapping.
Nothing here discovers corporate actions; it is pure parsing and bookkeeping.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation

from app.core.symbol import to_db_symbol
from app.models.mock_strategy import MockStrategyOrder

_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,31}")
_UNRESOLVED = {"sending", "uncertain", "accepted"}


def parse_symbol_changes(values: Iterable[str] | None) -> dict[str, str]:
    """``["OLD=NEW", ...]`` → ``{OLD: NEW}``; any malformed input is an error.

    Rejected before any network: bad shape, same symbol, repeated OLD or NEW,
    and chains (a NEW that is also an OLD).
    """
    result: dict[str, str] = {}
    for raw in values or ():
        parts = raw.split("=")
        if len(parts) != 2:
            raise ValueError("--symbol-change must be OLD=NEW")
        old, new = (to_db_symbol(p.strip()).upper() for p in parts)
        if _SYMBOL.fullmatch(old) is None or _SYMBOL.fullmatch(new) is None:
            raise ValueError("--symbol-change symbols must be US tickers")
        if old == new:
            raise ValueError("--symbol-change OLD and NEW must differ")
        if old in result:
            raise ValueError("--symbol-change OLD repeated")
        result[old] = new
    if len(set(result.values())) != len(result) or set(result) & set(result.values()):
        raise ValueError("--symbol-change mappings must be distinct and unchained")
    return result


def effective_symbol(row: MockStrategyOrder) -> str:
    """The symbol actually sent to the broker: the mapped one when journaled."""
    change = row.meta.get("symbol_change")
    if isinstance(change, dict) and isinstance(change.get("to"), str):
        return change["to"]
    return row.symbol


def open_quantity(
    rows: Sequence[MockStrategyOrder], symbol: str
) -> tuple[str, Decimal] | str:
    """Unclosed episode quantity journaled under ``symbol``.

    Returns ``(exit_side, quantity)``, or a refusal reason when the quantity is
    unknowable (a send whose outcome is unresolved) or the book mixes directions.
    """
    totals = {"buy": Decimal(0), "short": Decimal(0)}
    entries = [r for r in rows if r.symbol == symbol and r.side in totals]
    for entry in entries:
        if entry.status in {"planned", "closed", "canceled", "rejected"}:
            continue
        try:
            filled = Decimal(entry.meta.get("filled_qty", "0"))
        except (InvalidOperation, TypeError):
            return "entry_fill_unresolved"
        if (
            entry.status in _UNRESOLVED
            or not filled.is_finite()
            or (
                entry.status == "partially_filled"
                and not entry.meta.get("broker_terminal")
            )
        ):
            return "entry_fill_unresolved"
        consumed = Decimal(0)
        for leg in rows:
            if leg.episode_id != entry.episode_id or leg.side not in {"sell", "cover"}:
                continue
            if leg.status in _UNRESOLVED:
                return "exit_leg_unresolved"
            try:
                consumed += Decimal(leg.meta.get("filled_qty", "0"))
            except (InvalidOperation, TypeError):
                return "exit_leg_unresolved"
        totals[entry.side] += max(filled - consumed, Decimal(0))
    if totals["buy"] > 0 and totals["short"] > 0:
        return "mixed_direction_episodes"
    if totals["short"] > 0:
        return "cover", totals["short"]
    if totals["buy"] > 0:
        return "sell", totals["buy"]
    return "no_open_quantity"
