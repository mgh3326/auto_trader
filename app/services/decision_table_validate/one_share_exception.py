"""§664 KR new-entry one-share exception, as the decision-table validator reads it.

``buy.per_symbol_notional_krw_range`` is a [200,000, 400,000] KRW band for new
entries. A symbol whose single share already costs more than the band ceiling
can never produce an in-band order, so the policy's ``one_share_exception``
(the KR mirror of the US §139차 rule) lets such a symbol enter with exactly one
share. This module is the pure predicate; it reads JSON-shaped values only.

The exception is deliberately narrow and every doubt denies it (the rung then
falls back to the ordinary ``sizing_band_violation``):

* KR only. The US band also declares an exception, but the validator has never
  honoured it; changing that is a separate operator decision, not part of #664.
* buys only -- the caller never consults it for a sell.
* new entries only -- a row carrying held-position evidence is denied, because
  the band (and therefore its exception) is scoped to new entries.
* exactly one share, and the single share must itself exceed the band ceiling.
* the rung's worst-case notional (``price_max`` x 1) must not exceed
  ``absolute_ceiling_krw``.
* one symbol per row, and at most ``max_deep_rungs`` buy rungs for that symbol
  across the whole table (enforced by the validator's table-level pass).

The per-order auto-approve cap is not read here and is not relaxed: an
exception order above it is still demoted to a human card by
``order_proposals.auto_approve``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_CEILING_FIELD_BY_MARKET = {"kr": "absolute_ceiling_krw"}
ONE_SHARE_EXCEPTION_MARKETS = frozenset(_CEILING_FIELD_BY_MARKET)

DENIED_NOT_A_SINGLE_SHARE = "not_a_single_share"
DENIED_SHARE_WITHIN_BAND = "single_share_not_above_band_ceiling"
DENIED_ABOVE_CEILING = "above_absolute_ceiling"
DENIED_HELD_POSITION = "held_position_evidence"
DENIED_NOT_ONE_SYMBOL = "row_not_single_symbol"

_POSITION_METRIC_MARKERS = ("position", "avg_buy_price", "average_cost", "holding")
_POSITION_ACTION_KEYS = (
    "avg_price",
    "average_cost",
    "avg_buy_price",
    "position_quantity",
    "holding_quantity",
)


@dataclass(frozen=True)
class OneShareException:
    ceiling: Decimal
    max_deep_rungs: int


def _finite_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def one_share_exception_for(
    market: Any, threshold: Mapping[str, Any]
) -> OneShareException | None:
    """Return the enabled exception for ``market``, or ``None`` (fail closed)."""

    ceiling_field = _CEILING_FIELD_BY_MARKET.get(market)
    if ceiling_field is None:
        return None
    raw = threshold.get("one_share_exception")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    ceiling = _finite_decimal(raw.get(ceiling_field))
    max_deep_rungs = raw.get("max_deep_rungs")
    if (
        ceiling is None
        or ceiling <= 0
        or isinstance(max_deep_rungs, bool)
        or not isinstance(max_deep_rungs, int)
        or max_deep_rungs < 1
    ):
        return None
    return OneShareException(ceiling=ceiling, max_deep_rungs=max_deep_rungs)


def _is_affirmative_flat_condition(condition: Mapping[str, Any]) -> bool:
    """``position_quantity eq 0`` states a new entry; it is not held evidence."""

    metric = condition.get("metric")
    value = _finite_decimal(condition.get("value"))
    return (
        isinstance(metric, str)
        and "position_quantity" in metric
        and condition.get("operator") == "eq"
        and value is not None
        and value == 0
    )


def row_has_position_evidence(row: Mapping[str, Any]) -> bool:
    action = row.get("action")
    if isinstance(action, Mapping) and any(
        action.get(key) is not None for key in _POSITION_ACTION_KEYS
    ):
        return True
    conditions = row.get("conditions")
    if not isinstance(conditions, list):
        return False
    for condition in conditions:
        if not isinstance(condition, Mapping):
            continue
        metric = condition.get("metric")
        if not isinstance(metric, str):
            continue
        lowered = metric.lower()
        if any(marker in lowered for marker in _POSITION_METRIC_MARKERS):
            if not _is_affirmative_flat_condition(condition):
                return True
    return False


def _row_symbol_count(row: Mapping[str, Any]) -> int:
    symbols = row.get("symbols")
    if not isinstance(symbols, list):
        return 0
    return len({symbol for symbol in symbols if isinstance(symbol, str)})


def one_share_exception_denial(
    *,
    row: Mapping[str, Any],
    price_min: Decimal,
    price_max: Decimal,
    qty: Decimal,
    band_high: Decimal,
    exception: OneShareException,
) -> str | None:
    """``None`` when this over-band buy rung is admitted by the exception."""

    if qty != 1:
        return DENIED_NOT_A_SINGLE_SHARE
    if price_min <= band_high:
        return DENIED_SHARE_WITHIN_BAND
    if max(price_min, price_max) > exception.ceiling:
        return DENIED_ABOVE_CEILING
    if _row_symbol_count(row) != 1:
        return DENIED_NOT_ONE_SYMBOL
    if row_has_position_evidence(row):
        return DENIED_HELD_POSITION
    return None


__all__ = [
    "DENIED_ABOVE_CEILING",
    "DENIED_HELD_POSITION",
    "DENIED_NOT_ONE_SYMBOL",
    "DENIED_NOT_A_SINGLE_SHARE",
    "DENIED_SHARE_WITHIN_BAND",
    "ONE_SHARE_EXCEPTION_MARKETS",
    "OneShareException",
    "one_share_exception_denial",
    "one_share_exception_for",
    "row_has_position_evidence",
]
