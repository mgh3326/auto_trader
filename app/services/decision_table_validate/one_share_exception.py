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
* new entries only, proven affirmatively: the row must carry a
  ``position_quantity eq 0`` condition sourced from the row's own symbol, and
  ANY other position-/holding-/lot-/cost-/quantity-shaped condition metric,
  condition source, row key or action key denies it. A validator that cannot
  read holdings must not infer "new entry" from silence (tester round 1,
  BLOCKER 1: ``held_qty`` / ``action.position_qty`` slipped a closed list).
* not a cash-parking symbol: every symbol in ``PARKING_ALLOWLIST_SCOPES`` is
  denied, because its expanded-mode per-order cap is raised to 10,000,000 and
  the exception would otherwise let a one-share parking buy above 2,000,000
  auto-approve without a card (tester round 1, BLOCKER 2).
* exactly one share, and the single share must itself exceed the band ceiling.
* the rung's worst-case notional (``price_max`` x 1) must not exceed
  ``absolute_ceiling_krw``.
* the row's ``symbols`` list holds exactly one canonical KRX code (six ASCII
  ``[0-9A-Z]``, no padding), and at most ``max_deep_rungs`` buy rungs exist for
  that symbol across the whole table -- counted on a normalized key so padded or
  full-width spellings in other rows still count (tester round 1, BLOCKER 3).

The per-order auto-approve cap is not read here and is not relaxed: an
exception order above it is still demoted to a human card by
``order_proposals.auto_approve``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.order_proposals.parking_allowlist import PARKING_ALLOWLIST_SCOPES

_CEILING_FIELD_BY_MARKET = {"kr": "absolute_ceiling_krw"}
ONE_SHARE_EXCEPTION_MARKETS = frozenset(_CEILING_FIELD_BY_MARKET)

DENIED_NOT_A_SINGLE_SHARE = "not_a_single_share"
DENIED_SHARE_WITHIN_BAND = "single_share_not_above_band_ceiling"
DENIED_ABOVE_CEILING = "above_absolute_ceiling"
DENIED_HELD_POSITION = "held_position_evidence"
DENIED_NO_FLAT_PROOF = "no_affirmative_flat_position_condition"
DENIED_NOT_ONE_SYMBOL = "row_not_single_canonical_symbol"
DENIED_PARKING_SYMBOL = "cash_parking_symbol"

_FLAT_METRIC = "position_quantity"
# Substrings of a normalized key/metric/source that look like a holding. The
# list is broad on purpose: a false positive only costs the exception (the
# rung falls back to the band), a false negative admits a held-symbol add.
_HELD_MARKERS = (
    "held",
    "hold",
    "position",
    "pos_",
    "avg",
    "average",
    "cost",
    "lot",
    "qty",
    "quantity",
    "share",
    "balance",
    "owned",
    "inventory",
)
_KRX_CODE = re.compile(r"[0-9A-Z]{6}")
_PARKING_SYMBOLS = frozenset(scope.symbol for scope in PARKING_ALLOWLIST_SCOPES)


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


def normalized_symbol_key(value: Any) -> str | None:
    """Ledger key: one logical symbol however it is padded, cased or widened."""

    if not isinstance(value, str):
        return None
    key = unicodedata.normalize("NFKC", value).strip().upper()
    return key or None


def canonical_row_symbol(row: Mapping[str, Any]) -> str | None:
    """The row's only symbol, if it is already an exact canonical KRX code."""

    symbols = row.get("symbols")
    if type(symbols) is not list or len(symbols) != 1:
        return None
    symbol = symbols[0]
    if type(symbol) is not str or not symbol.isascii():
        return None
    return symbol if _KRX_CODE.fullmatch(symbol) else None


def _looks_held(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    normalized = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKC", text).lower())
    return any(marker in normalized for marker in _HELD_MARKERS)


def _is_affirmative_flat_condition(condition: Mapping[str, Any], symbol: str) -> bool:
    """Exactly ``position_quantity eq 0`` read from this symbol's holdings."""

    value = condition.get("value")
    source = condition.get("source")
    return (
        condition.get("metric") == _FLAT_METRIC
        and condition.get("operator") == "eq"
        and type(value) in (int, float)
        and value == 0
        and isinstance(source, str)
        and symbol in source
    )


def row_has_position_evidence(row: Mapping[str, Any]) -> bool:
    """Any holding-shaped key, metric or source outside the flat proof."""

    symbol = canonical_row_symbol(row) or ""
    if any(_looks_held(key) for key in row if key != "conditions"):
        return True
    action = row.get("action")
    if isinstance(action, Mapping) and any(_looks_held(key) for key in action):
        return True
    conditions = row.get("conditions")
    if not isinstance(conditions, list):
        return False
    for condition in conditions:
        if not isinstance(condition, Mapping):
            continue
        if symbol and _is_affirmative_flat_condition(condition, symbol):
            continue
        if any(_looks_held(key) for key in condition) or any(
            _looks_held(condition.get(field)) for field in ("metric", "source")
        ):
            return True
    return False


def row_has_flat_position_proof(row: Mapping[str, Any]) -> bool:
    symbol = canonical_row_symbol(row)
    conditions = row.get("conditions")
    if symbol is None or not isinstance(conditions, list):
        return False
    return any(
        isinstance(condition, Mapping)
        and _is_affirmative_flat_condition(condition, symbol)
        for condition in conditions
    )


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
    symbol = canonical_row_symbol(row)
    if symbol is None:
        return DENIED_NOT_ONE_SYMBOL
    if symbol in _PARKING_SYMBOLS:
        return DENIED_PARKING_SYMBOL
    if row_has_position_evidence(row):
        return DENIED_HELD_POSITION
    if not row_has_flat_position_proof(row):
        return DENIED_NO_FLAT_PROOF
    return None


__all__ = [
    "DENIED_ABOVE_CEILING",
    "DENIED_HELD_POSITION",
    "DENIED_NO_FLAT_PROOF",
    "DENIED_NOT_ONE_SYMBOL",
    "DENIED_NOT_A_SINGLE_SHARE",
    "DENIED_PARKING_SYMBOL",
    "DENIED_SHARE_WITHIN_BAND",
    "ONE_SHARE_EXCEPTION_MARKETS",
    "OneShareException",
    "canonical_row_symbol",
    "normalized_symbol_key",
    "one_share_exception_denial",
    "one_share_exception_for",
    "row_has_flat_position_proof",
    "row_has_position_evidence",
]
