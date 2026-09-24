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
* new entries only, proven by a CLOSED GRAMMAR rather than by guessing what a
  holding looks like (two tester rounds showed a denylist over free-form JSON
  always leaks):

  - row, action, rung and condition keys come from closed sets (anything else,
    including ``derivation``, ``matched_tier`` -- which can name a held-lot tier
    such as ``buy.underwater_support_net`` -- or a nested ``context``, denies);
  - exactly one condition uses ``metric: position_quantity`` and it is the
    flat proof: ``operator: eq``, numeric ``value: 0`` and a ``source`` that is
    EXACTLY ``get_holdings.accounts[<row account_mode>].positions[<symbol>]
    .quantity`` -- so it cannot point at another account or symbol;
  - every other condition uses a metric from a closed market-data vocabulary,
    a source that starts with a market-data tool name, and a scalar, scalar
    list or numeric range value;
  - and, as a second line, every other key and string in the row (invalidation
    prose included) is scanned for holding-shaped words in English and Korean.

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
DENIED_NOT_ONE_SYMBOL = "row_not_single_canonical_symbol"
DENIED_PARKING_SYMBOL = "cash_parking_symbol"
DENIED_ROW_GRAMMAR = "row_outside_new_entry_grammar"
DENIED_NO_FLAT_PROOF = "no_bound_flat_position_condition"
DENIED_HELD_POSITION = "held_position_evidence"

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
_RUNG_KEYS = frozenset({"rung", "price_min", "price_max", "qty", "tick", "formula"})
_CONDITION_KEYS = frozenset(
    {"metric", "source", "operator", "value", "max_age_seconds"}
)
_RANGE_VALUE_KEYS = frozenset(
    {"min_inclusive", "max_inclusive", "min_exclusive", "max_exclusive"}
)
# Market-data metrics seen in real KR prep tables that say nothing about a
# holding. A new metric must be added here deliberately; until then a row using
# it simply does not get the exception.
_MARKET_METRICS = frozenset(
    {
        "live_price_band",
        "krx_previous_close",
        "nxt_premarket_price",
        "nxt_tradable",
        "price_source_is_nxt",
        "abs_rel_diff_quote_vs_nxt_orderbook_mid",
        "premarket_session_change_pct",
        "nxt_premarket_change_pct_vs_prev_close",
        "rsi_14_last_completed_daily_bar",
        "fresh_support_s1_price",
        "fresh_support_s2_price",
        "fresh_resistance_r1_price",
        "fresh_resistance_r2_price",
        "crash_day_state_at_decision",
        "crash_day_trigger_069500_open_gap_pct",
        "catalyst_basis_recorded",
        "flow_basis_observed_at_decision",
        "required_thesis_evidence_present",
        "policy_frozen_keys",
        "toss_open_orders_count_same_symbol",
        "kis_open_orders_count_same_symbol",
    }
)
_MARKET_SOURCE_TOOLS = (
    "get_quote",
    "get_orderbook",
    "get_indicators",
    "get_support_resistance",
    "analyze_stock_batch",
    "get_trading_policy",
    "get_market_index",
    "get_krx_session_health",
    "get_news",
    "kis_live_get_order_history",
    "toss_get_order_history",
)
# Second line only. Substrings match anywhere in the NFKC/lower-cased text;
# segment words must equal a whole ``[a-z0-9]`` segment (so "threshold" and
# "positive" do not trip them).
_HELD_SUBSTRINGS = (
    "held",
    "holding",
    "position",
    "avg",
    "average",
    "cost",
    "qty",
    "quantity",
    "share",
    "balance",
    "owned",
    "inventory",
    "portfolio",
    "보유",
    "평단",
    "평균단가",
    "매입",
    "수량",
    "잔고",
)
_HELD_SEGMENTS = frozenset({"hold", "lot", "lots", "pos", "unit", "units"})
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


def looks_held(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    normalized = unicodedata.normalize("NFKC", text).lower()
    if any(marker in normalized for marker in _HELD_SUBSTRINGS):
        return True
    return any(
        segment in _HELD_SEGMENTS for segment in re.split(r"[^a-z0-9]+", normalized)
    )


def _is_scalar(value: Any) -> bool:
    return value is None or type(value) in (str, int, float, bool)


def _market_condition_ok(condition: Mapping[str, Any]) -> bool:
    if not set(condition) <= _CONDITION_KEYS:
        return False
    if condition.get("metric") not in _MARKET_METRICS:
        return False
    source = condition.get("source")
    if type(source) is not str or not any(
        source == tool or source.startswith((f"{tool}(", f"{tool}."))
        for tool in _MARKET_SOURCE_TOOLS
    ):
        return False
    value = condition.get("value")
    if _is_scalar(value):
        return True
    if type(value) is list:
        return all(_is_scalar(item) for item in value)
    if type(value) is dict:
        return set(value) <= _RANGE_VALUE_KEYS and all(
            _finite_decimal(item) is not None for item in value.values()
        )
    return False


def _is_bound_flat_proof(
    condition: Mapping[str, Any], account_mode: Any, symbol: str
) -> bool:
    value = condition.get("value")
    return (
        set(condition) <= _CONDITION_KEYS
        and condition.get("metric") == _FLAT_METRIC
        and condition.get("operator") == "eq"
        and type(value) in (int, float)
        and value == 0
        and type(account_mode) is str
        and condition.get("source") == flat_proof_source(account_mode, symbol)
    )


def _strings_and_keys(value: Any) -> list[str]:
    """Every dict key and string leaf below ``value``."""

    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            found.append(str(key))
            found.extend(_strings_and_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_strings_and_keys(child))
    elif isinstance(value, str):
        found.append(value)
    return found


def new_entry_grammar_denial(row: Mapping[str, Any], symbol: str) -> str | None:
    """``None`` when the row is provably a new entry under the closed grammar."""

    if not set(row) <= _ROW_KEYS:
        return DENIED_ROW_GRAMMAR
    action = row.get("action")
    conditions = row.get("conditions")
    invalidation = row.get("invalidation", [])
    if (
        type(action) is not dict
        or not set(action) <= _ACTION_KEYS
        or type(conditions) is not list
        or type(invalidation) is not list
        or not all(type(item) is str for item in invalidation)
    ):
        return DENIED_ROW_GRAMMAR
    rungs = action.get("rungs")
    if type(rungs) is not list or not all(
        type(rung) is dict and set(rung) <= _RUNG_KEYS for rung in rungs
    ):
        return DENIED_ROW_GRAMMAR

    flat_proofs = []
    for condition in conditions:
        if type(condition) is not dict:
            return DENIED_ROW_GRAMMAR
        if condition.get("metric") == _FLAT_METRIC:
            if not _is_bound_flat_proof(condition, action.get("account_mode"), symbol):
                return DENIED_NO_FLAT_PROOF
            flat_proofs.append(condition)
        elif not _market_condition_ok(condition):
            return DENIED_ROW_GRAMMAR
    if len(flat_proofs) != 1:
        return DENIED_NO_FLAT_PROOF

    # Second line: nothing else in the row may talk about a holding. The flat
    # proof is excluded (it is exactly-matched above); rung numbers are not
    # text, but a rung's free-text ``formula`` is scanned.
    scanned: list[str] = []
    for key, value in row.items():
        scanned.append(key)
        if key == "conditions":
            for condition in conditions:
                if condition is not flat_proofs[0]:
                    scanned.extend(_strings_and_keys(condition))
        elif key == "action":
            for action_key, action_value in action.items():
                scanned.append(action_key)
                if action_key == "rungs":
                    scanned.extend(
                        rung["formula"]
                        for rung in rungs
                        if isinstance(rung.get("formula"), str)
                    )
                else:
                    scanned.extend(_strings_and_keys(action_value))
        elif key != "symbols":
            scanned.extend(_strings_and_keys(value))
    if any(looks_held(text) for text in scanned):
        return DENIED_HELD_POSITION
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
    "DENIED_HELD_POSITION",
    "DENIED_NO_FLAT_PROOF",
    "DENIED_NOT_ONE_SYMBOL",
    "DENIED_NOT_A_SINGLE_SHARE",
    "DENIED_PARKING_SYMBOL",
    "DENIED_ROW_GRAMMAR",
    "DENIED_SHARE_WITHIN_BAND",
    "ONE_SHARE_EXCEPTION_MARKETS",
    "OneShareException",
    "canonical_row_symbol",
    "flat_proof_source",
    "looks_held",
    "new_entry_grammar_denial",
    "normalized_symbol_key",
    "one_share_exception_denial",
    "one_share_exception_for",
]
