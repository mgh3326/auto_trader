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
* exactly one share, the single share itself above the band ceiling, and the
  rung's worst-case notional (``price_max`` x 1) within ``absolute_ceiling_krw``.
* the row's ``symbols`` list holds exactly one canonical KRX code (six ASCII
  ``[0-9A-Z]``, no padding), and at most ``max_deep_rungs`` buy rungs exist for
  that symbol across the whole table -- counted on a normalized key so padded or
  full-width spellings in other rows still count.
* not a cash-parking symbol: every symbol in ``PARKING_ALLOWLIST_SCOPES`` is
  denied, because its expanded-mode per-order cap is raised to 10,000,000 and
  the exception would otherwise let a one-share parking buy above 2,000,000
  auto-approve without a card.
* new entries only, proven by a STRICT NO-FREE-TEXT GRAMMAR. A pure validator
  cannot read holdings, so it must not accept any field in which a table could
  also state one: three tester rounds showed that scanning free text for
  holding words always leaks (another key, another language, a zero-width
  character). Every string an exception row may carry is owned by this
  grammar -- a closed enum or an exact template -- and every other value is a
  typed number or bool:

  - no non-ASCII and no Unicode control/format (Cc/Cf) character anywhere in
    the row, keys included;
  - closed row / action / rung / condition keys; no ``formula``, no
    ``derivation``, no ``matched_tier``;
  - ``scenario_id`` is exactly ``one-share-entry-<symbol>`` or
    ``one-share-entry-<symbol>-<1..3 digits>``; ``priority`` is an int;
  - ``invalidation`` is absent or ``[]`` (the product cost: exception rows
    cannot carry prose invalidation);
  - ``required_thesis_fields`` is drawn from a closed enum;
  - ``sector_concentration`` has only numeric leaves under closed keys;
  - the action is exactly a limit ``place`` ``buy`` on a KR account mode, with
    optional ``time_in_force: DAY``, ``apply_kind: proposal`` and numeric
    ``reference_price`` / ``minimum_order_amount``; rungs carry integer
    ``rung/price_min/price_max/qty/tick`` only;
  - exactly one ``position_quantity eq 0`` condition whose ``source`` EQUALS
    ``get_holdings.accounts[<account_mode>].positions[<symbol>].quantity``;
  - every other condition is one of ``_MARKET_CONDITIONS``: its ``source``
    EQUALS that metric's template (not a prefix), its operator is from that
    metric's set, its value has that metric's type (numbers are numbers, never
    strings), and ``max_age_seconds`` is an int in [1, 86400].

The residual limit is a table that ONLY lies -- it declares a held symbol flat
and says nothing else. No pure validator can catch that; the live check is the
helmsman session's condition match before a real apply.

The per-order auto-approve cap is not read here and is not relaxed: an
exception order above it is still demoted to a human card by
``order_proposals.auto_approve``.
"""

from __future__ import annotations

import math
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
DENIED_NOT_ONE_SYMBOL = "row_not_single_canonical_symbol"
DENIED_PARKING_SYMBOL = "cash_parking_symbol"
DENIED_TEXT = "non_ascii_or_control_character"
DENIED_ROW_FIELD = "row_field_not_in_grammar"
DENIED_SCENARIO_ID = "scenario_id_not_template"
DENIED_INVALIDATION = "invalidation_not_empty"
DENIED_SECTOR = "sector_concentration_not_numeric_closed"
DENIED_ACTION = "action_not_in_grammar"
DENIED_RUNG = "rung_not_in_grammar"
DENIED_THESIS_FIELDS = "thesis_field_not_in_enum"
DENIED_CONDITION = "condition_not_a_template"
DENIED_NO_FLAT_PROOF = "no_bound_flat_position_condition"

_FLAT_METRIC = "position_quantity"
_ROW_KEYS = frozenset(
    {
        "scenario_id",
        "priority",
        "symbols",
        "conditions",
        "action",
        "invalidation",
        "sector_concentration",
    }
)
_ACTION_KEYS = frozenset(
    {
        "proposal_action",
        "account_mode",
        "side",
        "order_type",
        "rungs",
        "required_thesis_fields",
        "time_in_force",
        "reference_price",
        "minimum_order_amount",
        "apply_kind",
    }
)
_KR_ACCOUNT_MODES = frozenset({"kis_live", "toss_live", "kis_mock", "kiwoom_mock"})
_RUNG_KEYS = frozenset({"rung", "price_min", "price_max", "qty", "tick"})
_CONDITION_KEYS = frozenset(
    {"metric", "source", "operator", "value", "max_age_seconds"}
)
_RANGE_VALUE_KEYS = frozenset(
    {"min_inclusive", "max_inclusive", "min_exclusive", "max_exclusive"}
)
_THESIS_FIELDS = frozenset({"scenario_id", "decision_table_hash", "policy_version"})
_SECTOR_KEYS = frozenset(
    {"projected_pct", "projected_percent", "current_pct", "cap_pct"}
)
_MAX_AGE_SECONDS = 86400
_ORDER = frozenset({"lt", "lte", "gt", "gte"})


@dataclass(frozen=True)
class _MarketCondition:
    source: str
    operators: frozenset[str]
    value_kind: str  # "range" | "number" | "int" | "bool"


# The only non-flat conditions an exception row may carry: exact
# (metric, source) templates for market data that say nothing about a holding.
# A prep table must emit these strings verbatim; a new metric or a different
# spelling needs a code change here before it can appear on an exception row.
_MARKET_CONDITIONS: dict[str, _MarketCondition] = {
    "live_price_band": _MarketCondition(
        "get_quote(symbol,market='kr').price", frozenset({"between"}), "range"
    ),
    "krx_previous_close": _MarketCondition(
        "get_quote(symbol,market='kr').previous_close",
        _ORDER | {"eq"},
        "number",
    ),
    "nxt_tradable": _MarketCondition(
        "get_quote(symbol,market='kr').nxt_tradable", frozenset({"eq"}), "bool"
    ),
    "premarket_session_change_pct": _MarketCondition(
        "(get_quote(symbol,market='kr').price"
        " / get_quote(symbol,market='kr').previous_close - 1) * 100",
        _ORDER,
        "number",
    ),
    "rsi_14_last_completed_daily_bar": _MarketCondition(
        "analyze_stock_batch(symbol,quick=false).indicators.rsi.14",
        _ORDER,
        "number",
    ),
    "fresh_support_s1_price": _MarketCondition(
        "analyze_stock_batch(symbol,quick=false).support_resistance.supports[0].price",
        _ORDER | {"eq"},
        "number",
    ),
    "fresh_resistance_r1_price": _MarketCondition(
        "analyze_stock_batch(symbol,quick=false)"
        ".support_resistance.resistances[0].price",
        _ORDER | {"eq"},
        "number",
    ),
    "toss_open_orders_count_same_symbol": _MarketCondition(
        'toss_get_order_history(status="open").orders[symbol==sym].length',
        frozenset({"eq"}),
        "int",
    ),
    "kis_open_orders_count_same_symbol": _MarketCondition(
        'kis_live_get_order_history(status="pending",market="kr")'
        ".orders[symbol==sym].length",
        frozenset({"eq"}),
        "int",
    ),
}
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


def flat_proof_source(account_mode: str, symbol: str) -> str:
    return f"get_holdings.accounts[{account_mode}].positions[{symbol}].quantity"


def scenario_id_template(symbol: str) -> re.Pattern[str]:
    return re.compile(rf"one-share-entry-{symbol}(?:-[0-9]{{1,3}})?")


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return (type(value) is int) or (type(value) is float and math.isfinite(value))


def _clean_text(text: str) -> bool:
    return text.isascii() and not any(
        unicodedata.category(char) in ("Cc", "Cf") for char in text
    )


def row_text_is_clean(value: Any) -> bool:
    """Every key and string below ``value`` is ASCII with no Cc/Cf character."""

    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _clean_text(key) and row_text_is_clean(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return all(row_text_is_clean(child) for child in value)
    if isinstance(value, str):
        return _clean_text(value)
    return True


def _value_ok(value: Any, kind: str) -> bool:
    if kind == "bool":
        return type(value) is bool
    if kind == "int":
        return _is_int(value)
    if kind == "number":
        return _is_number(value)
    if kind == "range":
        return (
            type(value) is dict
            and bool(value)
            and set(value) <= _RANGE_VALUE_KEYS
            and all(_is_number(item) for item in value.values())
        )
    return False


def _max_age_ok(condition: Mapping[str, Any]) -> bool:
    max_age = condition.get("max_age_seconds")
    return _is_int(max_age) and 1 <= max_age <= _MAX_AGE_SECONDS


def _is_bound_flat_proof(
    condition: Mapping[str, Any], account_mode: str, symbol: str
) -> bool:
    value = condition.get("value")
    return (
        set(condition) == _CONDITION_KEYS
        and condition.get("metric") == _FLAT_METRIC
        and condition.get("operator") == "eq"
        and type(value) in (int, float)
        and value == 0
        and condition.get("source") == flat_proof_source(account_mode, symbol)
        and _max_age_ok(condition)
    )


def _market_condition_ok(condition: Mapping[str, Any]) -> bool:
    metric = condition.get("metric")
    spec = _MARKET_CONDITIONS.get(metric) if type(metric) is str else None
    return (
        spec is not None
        and set(condition) == _CONDITION_KEYS
        and condition.get("source") == spec.source
        and condition.get("operator") in spec.operators
        and _value_ok(condition.get("value"), spec.value_kind)
        and _max_age_ok(condition)
    )


def _action_denial(action: Any) -> str | None:
    if type(action) is not dict or not set(action) <= _ACTION_KEYS:
        return DENIED_ACTION
    if (
        action.get("proposal_action") != "place"
        or action.get("side") != "buy"
        or action.get("order_type") != "limit"
        or action.get("account_mode") not in _KR_ACCOUNT_MODES
        or action.get("time_in_force", "DAY") != "DAY"
        or action.get("apply_kind", "proposal") != "proposal"
        or any(
            key in action and not (_is_number(action[key]) and action[key] > 0)
            for key in ("reference_price", "minimum_order_amount")
        )
    ):
        return DENIED_ACTION
    rungs = action.get("rungs")
    if (
        type(rungs) is not list
        or not rungs
        or not all(
            type(rung) is dict
            and set(rung) == _RUNG_KEYS
            and all(_is_int(rung[key]) for key in _RUNG_KEYS)
            for rung in rungs
        )
    ):
        return DENIED_RUNG
    fields = action.get("required_thesis_fields", [])
    if (
        type(fields) is not list
        or len(set(map(str, fields))) != len(fields)
        or not all(type(field) is str and field in _THESIS_FIELDS for field in fields)
    ):
        return DENIED_THESIS_FIELDS
    return None


def new_entry_grammar_denial(row: Mapping[str, Any], symbol: str) -> str | None:
    """``None`` when the row is provably a new entry under the closed grammar."""

    if not row_text_is_clean(row):
        return DENIED_TEXT
    if not set(row) <= _ROW_KEYS:
        return DENIED_ROW_FIELD
    scenario_id = row.get("scenario_id")
    if type(scenario_id) is not str or not scenario_id_template(symbol).fullmatch(
        scenario_id
    ):
        return DENIED_SCENARIO_ID
    if "priority" in row and not _is_int(row["priority"]):
        return DENIED_ROW_FIELD
    if "invalidation" in row and row["invalidation"] != []:
        return DENIED_INVALIDATION
    if "sector_concentration" in row:
        sector = row["sector_concentration"]
        if (
            type(sector) is not dict
            or not set(sector) <= _SECTOR_KEYS
            or not all(_is_number(value) for value in sector.values())
        ):
            return DENIED_SECTOR
    action_denial = _action_denial(row.get("action"))
    if action_denial is not None:
        return action_denial
    account_mode = row["action"]["account_mode"]

    conditions = row.get("conditions")
    if type(conditions) is not list:
        return DENIED_CONDITION
    flat_proofs = 0
    for condition in conditions:
        if type(condition) is not dict:
            return DENIED_CONDITION
        if condition.get("metric") == _FLAT_METRIC:
            if not _is_bound_flat_proof(condition, account_mode, symbol):
                return DENIED_NO_FLAT_PROOF
            flat_proofs += 1
        elif not _market_condition_ok(condition):
            return DENIED_CONDITION
    if flat_proofs != 1:
        return DENIED_NO_FLAT_PROOF
    return None


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
    return new_entry_grammar_denial(row, symbol)


__all__ = [
    "DENIED_ABOVE_CEILING",
    "DENIED_ACTION",
    "DENIED_CONDITION",
    "DENIED_INVALIDATION",
    "DENIED_NO_FLAT_PROOF",
    "DENIED_NOT_ONE_SYMBOL",
    "DENIED_NOT_A_SINGLE_SHARE",
    "DENIED_PARKING_SYMBOL",
    "DENIED_ROW_FIELD",
    "DENIED_RUNG",
    "DENIED_SCENARIO_ID",
    "DENIED_SECTOR",
    "DENIED_SHARE_WITHIN_BAND",
    "DENIED_TEXT",
    "DENIED_THESIS_FIELDS",
    "ONE_SHARE_EXCEPTION_MARKETS",
    "OneShareException",
    "canonical_row_symbol",
    "flat_proof_source",
    "new_entry_grammar_denial",
    "normalized_symbol_key",
    "one_share_exception_denial",
    "one_share_exception_for",
    "row_text_is_clean",
    "scenario_id_template",
]
