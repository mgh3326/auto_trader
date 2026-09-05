"""Deterministic, side-effect-free decision-table validation.

This module accepts JSON-shaped values only. It deliberately imports no
database, network, ORM, or broker surface.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.mcp_server.tick_size import adjust_tick_size_kr, get_tick_size_kr
from app.services.trading_policy_service import get_policy_for, policy_version_stamp

CANONICAL_SHAPE_REF = (
    "docs/specs/mcp-session-tools-v1.md#canonical-decision-table-shape-v11"
)
CURRENT_SCHEMA_VERSION = "kr-nxt-decision-table/v1.1"
DEPRECATED_SCHEMA_VERSION = "kr-nxt-decision-table/v1"
DEPRECATED_SCHEMA_SUNSET = "2026-09-12"
_VALID_CONDITION_OPERATORS = frozenset(
    {"eq", "ne", "lt", "lte", "gt", "gte", "between", "in"}
)
_VALID_MARKETS = frozenset({"kr", "us", "crypto"})
_VALID_PROPOSAL_ACTIONS = frozenset({"place", "replace", "cancel"})
_VALID_SIDES = frozenset({"buy", "sell"})
_VALID_ORDER_TYPES = frozenset({"limit", "market"})
_VALID_ACCOUNT_MODES = frozenset(
    {
        "alpaca_paper",
        "alpaca_paper_lab",
        "binance_demo",
        "binance_futures_demo",
        "db_simulated",
        "kis_live",
        "kis_mock",
        "kiwoom_mock",
        "kiwoom_mock_us",
        "toss_live",
        "upbit_live",
    }
)
_CORE_DECISION_TABLE_KEYS = frozenset({"no_match_action", "rows", "extensions"})
_RUNG_REQUIRED_FIELDS = ("rung", "price_min", "price_max", "qty", "tick")
_RUNG_INTEGER_FIELDS = ("price_min", "price_max", "qty", "tick")
_PARENT_CORRELATION_ID = re.compile(r"^kr-nxt-prep-\d{4}-\d{2}-\d{2}$")


def decision_table_validate(table: Any, market: Any) -> dict[str, Any]:
    """Validate one decision-table envelope and never raise to an MCP caller."""

    policy = policy_version_stamp()
    if not isinstance(table, dict):
        return _result(
            policy,
            "unknown",
            [
                _violation(
                    None,
                    "table_not_object",
                    "object containing decision_table",
                    type(table).__name__,
                    "block",
                    "unknown",
                )
            ],
            None,
            [],
        )

    decision_table = table.get("decision_table")
    if not isinstance(decision_table, dict):
        return _result(
            policy,
            "unknown",
            [
                _violation(
                    None,
                    "missing_decision_table",
                    "decision_table object",
                    type(decision_table).__name__,
                    "block",
                    "unknown",
                )
            ],
            None,
            [],
        )

    detected_shape = _detect_shape(decision_table)
    violations: list[dict[str, Any]] = []
    _validate_schema(table, detected_shape, violations)
    _validate_envelope_market(table, market, detected_shape, violations)
    _validate_parent_correlation_id(table, detected_shape, violations)
    if _contains_non_finite(decision_table):
        violations.append(
            _violation(
                None,
                "non_finite_number",
                "finite JSON numbers only",
                "NaN or Infinity present",
                "block",
                detected_shape,
            )
        )

    digest = _canonical_digest(decision_table)
    if digest is None or table.get("decision_table_hash") != digest:
        violations.append(
            _violation(
                None,
                "decision_table_hash_mismatch",
                digest or "canonicalizable decision_table SHA-256",
                table.get("decision_table_hash"),
                "block",
                detected_shape,
            )
        )

    _validate_extensions(decision_table, detected_shape, violations)
    rows = decision_table.get("rows")
    if detected_shape == "columnar":
        violations.append(
            _violation(
                None,
                "unsupported_table_shape",
                "row-object decision table",
                "columns plus list-of-list rows",
                "block",
                detected_shape,
            )
        )
    if not isinstance(rows, list):
        rows = []

    scenario_rows: dict[str, list[int]] = {}
    recomputed_rows: list[dict[str, Any]] = []
    row_sides: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        scenario_id = row.get("scenario_id")
        if isinstance(scenario_id, str):
            scenario_rows.setdefault(scenario_id, []).append(index)
        _validate_conditions(row, index, detected_shape, violations)
        action = row.get("action")
        if not isinstance(action, dict):
            violations.append(
                _violation(
                    index,
                    "invalid_enum_value",
                    "action object with proposal_action, account_mode, side, order_type",
                    action,
                    "block",
                    detected_shape,
                )
            )
            recomputed_rows.append(_recomputed_row(scenario_id, None, None))
            continue

        _validate_action_enums(action, index, detected_shape, violations)
        _collect_row_side(row, action, index, row_sides)
        _validate_sector_concentration(
            row, action, market, index, detected_shape, violations
        )
        rungs = action.get("rungs")
        encoding = _rungs_encoding(rungs)
        if encoding == "prose":
            violations.append(
                _violation(
                    index,
                    "price_qty_not_machine_recomputable",
                    "v1.1 object-array rungs",
                    "prose string",
                    "block",
                    detected_shape,
                )
            )
            recomputed_rows.append(_recomputed_row(scenario_id, None, None))
            continue
        if encoding in {"scalar", "parallel"}:
            actual = "parallel list" if encoding == "parallel" else "scalar object"
            violations.append(
                _violation(
                    index,
                    "unsupported_rungs_encoding",
                    "v1.1 object-array rungs",
                    actual,
                    "block",
                    detected_shape,
                )
            )
            normalized = _normalize_parallel_rungs(rungs)
            if normalized is None:
                _validate_scalar_rung_guards(
                    rungs,
                    row,
                    action,
                    index,
                    detected_shape,
                    market,
                    violations,
                )
                recomputed_rows.append(_recomputed_row(scenario_id, None, None))
                continue
            prices, quantities = _validate_legacy_parallel_rungs(
                normalized, row, action, index, detected_shape, market, violations
            )
            recomputed_rows.append(
                _recomputed_row(
                    scenario_id, _collapse_scalar(prices), _collapse_scalar(quantities)
                )
            )
            continue
        if encoding != "v11":
            violations.append(
                _violation(
                    index,
                    "unsupported_rungs_encoding",
                    "v1.1 object-array rungs",
                    type(rungs).__name__,
                    "block",
                    detected_shape,
                )
            )
            recomputed_rows.append(_recomputed_row(scenario_id, None, None))
            continue

        prices, quantities = _validate_v11_rungs(
            rungs, row, action, index, detected_shape, market, violations
        )
        recomputed_rows.append(
            _recomputed_row(
                scenario_id, _collapse_scalar(prices), _collapse_scalar(quantities)
            )
        )

    _validate_duplicate_scenarios(scenario_rows, detected_shape, violations)
    _validate_opposite_orders(row_sides, detected_shape, violations)
    return _result(policy, detected_shape, violations, digest, recomputed_rows)


def _validate_schema(
    table: dict[str, Any], shape: str, violations: list[dict[str, Any]]
) -> None:
    version = table.get("schema_version")
    if version == CURRENT_SCHEMA_VERSION:
        return
    if version == DEPRECATED_SCHEMA_VERSION:
        violations.append(
            _violation(
                None,
                "schema_version_deprecated_v1",
                f"migrate to {CURRENT_SCHEMA_VERSION}; sunset {DEPRECATED_SCHEMA_SUNSET}",
                version,
                "advisory",
                shape,
            )
        )
        return
    violations.append(
        _violation(
            None,
            "schema_version_mismatch",
            CURRENT_SCHEMA_VERSION,
            version,
            "block",
            shape,
        )
    )


def _validate_envelope_market(
    table: dict[str, Any], market: Any, shape: str, violations: list[dict[str, Any]]
) -> None:
    if market not in _VALID_MARKETS or table.get("market") not in _VALID_MARKETS:
        violations.append(
            _violation(
                None,
                "invalid_enum_value",
                "market in {kr, us, crypto}",
                {"argument": market, "table": table.get("market")},
                "block",
                shape,
            )
        )


def _validate_parent_correlation_id(
    table: dict[str, Any], shape: str, violations: list[dict[str, Any]]
) -> None:
    parent = table.get("parent_correlation_id")
    if parent is not None and (
        not isinstance(parent, str) or _PARENT_CORRELATION_ID.fullmatch(parent) is None
    ):
        violations.append(
            _violation(
                None,
                "invalid_parent_correlation_id",
                "kr-nxt-prep-<YYYY-MM-DD>",
                parent,
                "block",
                shape,
            )
        )


def _validate_extensions(
    decision_table: dict[str, Any], shape: str, violations: list[dict[str, Any]]
) -> None:
    extensions = decision_table.get("extensions", [])
    declared = (
        {item for item in extensions if isinstance(item, str)}
        if isinstance(extensions, list)
        else set()
    )
    for key in decision_table:
        if key not in _CORE_DECISION_TABLE_KEYS and key not in declared:
            violations.append(
                _violation(
                    None,
                    "unknown_top_level_key",
                    "core key or key named in decision_table.extensions",
                    key,
                    "advisory",
                    shape,
                )
            )
    if not isinstance(extensions, list):
        return
    for entry in extensions:
        if not isinstance(entry, str) or entry not in decision_table:
            violations.append(
                _violation(
                    None,
                    "extensions_entry_absent",
                    "declared extension key present in decision_table",
                    entry,
                    "advisory",
                    shape,
                )
            )


def _validate_conditions(
    row: dict[str, Any], index: int, shape: str, violations: list[dict[str, Any]]
) -> None:
    conditions = row.get("conditions")
    if not isinstance(conditions, list):
        return
    for condition in conditions:
        if not isinstance(condition, dict):
            violations.append(
                _violation(
                    index,
                    "invalid_condition_operator",
                    "condition object with a valid operator",
                    condition,
                    "block",
                    shape,
                )
            )
            continue
        if condition.get("operator") not in _VALID_CONDITION_OPERATORS:
            violations.append(
                _violation(
                    index,
                    "invalid_condition_operator",
                    sorted(_VALID_CONDITION_OPERATORS),
                    condition.get("operator"),
                    "block",
                    shape,
                )
            )
        source = condition.get("source")
        if not isinstance(source, str) or not source.strip():
            violations.append(
                _violation(
                    index,
                    "condition_missing_source",
                    "non-empty source",
                    source,
                    "block",
                    shape,
                )
            )
        max_age = condition.get("max_age_seconds")
        if not _is_number(max_age) or _decimal(max_age) < 0:
            violations.append(
                _violation(
                    index,
                    "condition_missing_max_age_seconds",
                    "finite max_age_seconds >= 0 for live input",
                    max_age,
                    "block",
                    shape,
                )
            )


def _validate_action_enums(
    action: dict[str, Any], index: int, shape: str, violations: list[dict[str, Any]]
) -> None:
    values = {
        "proposal_action": (action.get("proposal_action"), _VALID_PROPOSAL_ACTIONS),
        "account_mode": (action.get("account_mode"), _VALID_ACCOUNT_MODES),
        "side": (action.get("side"), _VALID_SIDES),
        "order_type": (action.get("order_type"), _VALID_ORDER_TYPES),
    }
    for key, (value, allowed) in values.items():
        if value not in allowed:
            violations.append(
                _violation(
                    index,
                    "invalid_enum_value",
                    {key: sorted(allowed)},
                    {key: value},
                    "block",
                    shape,
                )
            )


def _validate_v11_rungs(
    rungs: list[dict[str, Any]],
    row: dict[str, Any],
    action: dict[str, Any],
    index: int,
    shape: str,
    market: Any,
    violations: list[dict[str, Any]],
) -> tuple[list[Any], list[Any]]:
    prices: list[Any] = []
    quantities: list[Any] = []
    for rung in rungs:
        missing = [field for field in _RUNG_REQUIRED_FIELDS if field not in rung]
        if missing:
            violations.append(
                _violation(
                    index,
                    "rungs_missing_field",
                    "required fields: rung, price_min, price_max, qty, tick",
                    missing,
                    "block",
                    shape,
                )
            )
            prices.append(None)
            quantities.append(None)
            continue
        invalid_fields = [
            field for field in _RUNG_INTEGER_FIELDS if not _is_int(rung[field])
        ]
        if invalid_fields:
            violations.append(
                _violation(
                    index,
                    "rungs_field_not_integer",
                    "price_min, price_max, qty, tick are JSON integers",
                    {field: rung[field] for field in invalid_fields},
                    "block",
                    shape,
                )
            )
            prices.append(None)
            quantities.append(None)
            continue
        price_min = rung["price_min"]
        price_max = rung["price_max"]
        qty = rung["qty"]
        tick = rung["tick"]
        prices.append(price_min)
        quantities.append(qty)
        if price_min > price_max:
            violations.append(
                _violation(
                    index,
                    "rungs_price_bounds_inverted",
                    "price_min <= price_max",
                    {"price_min": price_min, "price_max": price_max},
                    "block",
                    shape,
                )
            )
        if tick <= 0 or price_min % tick != 0 or price_max % tick != 0:
            violations.append(
                _violation(
                    index,
                    "rungs_price_not_tick_aligned",
                    "price_min and price_max are multiples of positive tick",
                    {"price_min": price_min, "price_max": price_max, "tick": tick},
                    "block",
                    shape,
                )
            )
        _validate_krx_grid(
            (price_min, price_max),
            tick,
            action.get("side"),
            market,
            index,
            shape,
            violations,
        )
        _validate_order_guards(
            row, action, price_min, qty, market, index, shape, violations
        )
    return prices, quantities


def _normalize_parallel_rungs(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, dict):
        return None
    count = value.get("count")
    fields = (
        "prices",
        "quantities",
        "price_min",
        "price_max",
        "quantity_min",
        "quantity_max",
    )
    if (
        not _is_int(count)
        or count <= 0
        or any(
            not isinstance(value.get(field), list) or len(value[field]) != count
            for field in fields
        )
    ):
        return None
    return [
        {
            "price": value["prices"][offset],
            "qty": value["quantities"][offset],
            "price_min": value["price_min"][offset],
            "price_max": value["price_max"][offset],
            "quantity_min": value["quantity_min"][offset],
            "quantity_max": value["quantity_max"][offset],
        }
        for offset in range(count)
    ]


def _validate_legacy_parallel_rungs(
    rungs: list[dict[str, Any]],
    row: dict[str, Any],
    action: dict[str, Any],
    index: int,
    shape: str,
    market: Any,
    violations: list[dict[str, Any]],
) -> tuple[list[Any], list[Any]]:
    prices: list[Any] = []
    quantities: list[Any] = []
    for rung in rungs:
        price = rung["price"]
        qty = rung["qty"]
        prices.append(price)
        quantities.append(qty)
        price_fields = (price, rung["price_min"], rung["price_max"])
        if not all(_is_number(value) for value in price_fields) or not (
            _decimal(rung["price_min"])
            <= _decimal(price)
            <= _decimal(rung["price_max"])
        ):
            violations.append(
                _violation(
                    index,
                    "price_recompute_mismatch",
                    "recorded price within exact rung bounds",
                    {
                        "price": price,
                        "min": rung["price_min"],
                        "max": rung["price_max"],
                    },
                    "block",
                    shape,
                )
            )
        qty_fields = (qty, rung["quantity_min"], rung["quantity_max"])
        if not all(_is_number(value) for value in qty_fields) or not (
            _decimal(rung["quantity_min"])
            <= _decimal(qty)
            <= _decimal(rung["quantity_max"])
        ):
            violations.append(
                _violation(
                    index,
                    "qty_recompute_mismatch",
                    "recorded quantity within exact rung bounds",
                    {
                        "qty": qty,
                        "min": rung["quantity_min"],
                        "max": rung["quantity_max"],
                    },
                    "block",
                    shape,
                )
            )
        _validate_krx_grid(
            (price,), None, action.get("side"), market, index, shape, violations
        )
        _validate_order_guards(
            row, action, price, qty, market, index, shape, violations
        )
    return prices, quantities


def _validate_scalar_rung_guards(
    rungs: Any,
    row: dict[str, Any],
    action: dict[str, Any],
    index: int,
    shape: str,
    market: Any,
    violations: list[dict[str, Any]],
) -> None:
    if not isinstance(rungs, dict):
        return
    price = rungs.get("price_min", rungs.get("price"))
    qty = rungs.get("qty", rungs.get("quantity", rungs.get("quantity_min")))
    _validate_order_guards(row, action, price, qty, market, index, shape, violations)


def _validate_krx_grid(
    prices: tuple[Any, ...],
    declared_tick: Any,
    side: Any,
    market: Any,
    index: int,
    shape: str,
    violations: list[dict[str, Any]],
) -> None:
    if market != "kr" or side not in _VALID_SIDES:
        return
    for price in prices:
        if not _is_number(price):
            continue
        try:
            expected_tick = get_tick_size_kr(float(price))
            adjusted = adjust_tick_size_kr(float(price), side=side)
        except (TypeError, ValueError, OverflowError):
            expected_tick = None
            adjusted = None
        if adjusted != price or (
            declared_tick is not None and declared_tick != expected_tick
        ):
            violations.append(
                _violation(
                    index,
                    "tick_grid_violation",
                    "KRX tick-aligned price and declared KRX tick",
                    {
                        "price": price,
                        "tick": declared_tick,
                        "expected_tick": expected_tick,
                    },
                    "block",
                    shape,
                )
            )


def _validate_order_guards(
    row: dict[str, Any],
    action: dict[str, Any],
    price: Any,
    qty: Any,
    market: Any,
    index: int,
    shape: str,
    violations: list[dict[str, Any]],
) -> None:
    _validate_loss_guard(row, price, action.get("side"), index, shape, violations)
    _validate_min_order_amount(action, row, price, qty, index, shape, violations)
    _validate_buy_policy(action, row, price, qty, market, index, shape, violations)


def _validate_loss_guard(
    row: dict[str, Any],
    price: Any,
    side: Any,
    index: int,
    shape: str,
    violations: list[dict[str, Any]],
) -> None:
    if side != "sell" or not _is_number(price):
        return
    average_cost = _average_cost(row)
    if average_cost is None:
        return
    minimum = average_cost * Decimal("1.01")
    if _decimal(price) < minimum:
        violations.append(
            _violation(
                index,
                "loss_guard_violation",
                f"price >= avg_price * 1.01 ({minimum})",
                price,
                "block",
                shape,
            )
        )


def _validate_min_order_amount(
    action: dict[str, Any],
    row: dict[str, Any],
    price: Any,
    qty: Any,
    index: int,
    shape: str,
    violations: list[dict[str, Any]],
) -> None:
    if not _is_number(price) or not _is_number(qty):
        return
    minimum = action.get("minimum_order_amount", row.get("minimum_order_amount", 1))
    if not _is_number(minimum) or _decimal(minimum) <= 0:
        return
    notional = _decimal(price) * _decimal(qty)
    if notional < _decimal(minimum):
        violations.append(
            _violation(
                index,
                "below_min_order_amount",
                f"price * qty >= {minimum}",
                str(notional),
                "block",
                shape,
            )
        )


def _validate_buy_policy(
    action: dict[str, Any],
    row: dict[str, Any],
    price: Any,
    qty: Any,
    market: Any,
    index: int,
    shape: str,
    violations: list[dict[str, Any]],
) -> None:
    if action.get("side") != "buy" or not _is_number(price) or not _is_number(qty):
        return
    threshold_key = {
        "kr": "buy.per_symbol_notional_krw_range",
        "us": "buy.per_symbol_notional_usd_range",
    }.get(market)
    if threshold_key is None:
        return
    try:
        policy = get_policy_for(market, "buy")
        low, high = policy["thresholds"][threshold_key]["value"]
    except (KeyError, TypeError, ValueError):
        return
    notional = _decimal(price) * _decimal(qty)
    if not (_decimal(low) <= notional <= _decimal(high)):
        violations.append(
            _violation(
                index,
                "sizing_band_violation",
                f"{threshold_key} in [{low}, {high}]",
                str(notional),
                "block",
                shape,
            )
        )
    reference_price = _first_number(
        action.get("reference_price"), row.get("reference_price")
    )
    if reference_price is None:
        return
    distance_pct = (_decimal(price) / reference_price - Decimal("1")) * Decimal("100")
    try:
        low, high = policy["thresholds"]["buy.deep_limit_pct_range"]["value"]
    except (KeyError, TypeError):
        return
    if not (_decimal(low) <= distance_pct <= _decimal(high)):
        violations.append(
            _violation(
                index,
                "deep_limit_violation",
                f"deep-limit distance in [{low}, {high}] percent",
                str(distance_pct),
                "block",
                shape,
            )
        )


def _validate_sector_concentration(
    row: dict[str, Any],
    action: dict[str, Any],
    market: Any,
    index: int,
    shape: str,
    violations: list[dict[str, Any]],
) -> None:
    concentration = action.get("sector_concentration", row.get("sector_concentration"))
    if not isinstance(concentration, dict) or market not in _VALID_MARKETS:
        return
    projected = _first_number(
        concentration.get("projected_pct"), concentration.get("projected_percent")
    )
    try:
        cap = get_policy_for(market, "buy")["thresholds"][
            "portfolio.sector_cluster_cap_pct"
        ]["value"]
    except (KeyError, TypeError, ValueError):
        return
    if concentration.get("verdict") == "over" or (
        projected is not None and projected > _decimal(cap)
    ):
        violations.append(
            _violation(
                index,
                "sector_concentration",
                f"projected sector concentration <= {cap}%",
                concentration,
                "advisory",
                shape,
            )
        )


def _collect_row_side(
    row: dict[str, Any],
    action: dict[str, Any],
    index: int,
    row_sides: dict[tuple[str, str], list[tuple[int, str]]],
) -> None:
    symbols = row.get("symbols")
    account_mode = action.get("account_mode")
    side = action.get("side")
    if (
        not isinstance(symbols, list)
        or not isinstance(account_mode, str)
        or not isinstance(side, str)
    ):
        return
    for symbol in symbols:
        if isinstance(symbol, str):
            row_sides.setdefault((symbol, account_mode), []).append((index, side))


def _validate_duplicate_scenarios(
    scenario_rows: dict[str, list[int]], shape: str, violations: list[dict[str, Any]]
) -> None:
    for scenario_id, indexes in scenario_rows.items():
        if len(indexes) > 1:
            for index in indexes:
                violations.append(
                    _violation(
                        index,
                        "duplicate_scenario_id",
                        "unique scenario_id",
                        scenario_id,
                        "block",
                        shape,
                    )
                )


def _validate_opposite_orders(
    row_sides: dict[tuple[str, str], list[tuple[int, str]]],
    shape: str,
    violations: list[dict[str, Any]],
) -> None:
    for (symbol, account_mode), indexed_sides in row_sides.items():
        sides = {side for _, side in indexed_sides}
        if {"buy", "sell"}.issubset(sides):
            for index, _ in indexed_sides:
                violations.append(
                    _violation(
                        index,
                        "same_day_chain_or_opposite_order",
                        "one side per (symbol, account_mode) per table",
                        {
                            "symbol": symbol,
                            "account_mode": account_mode,
                            "sides": sorted(sides),
                        },
                        "block",
                        shape,
                    )
                )


def _rungs_encoding(value: Any) -> str:
    if isinstance(value, str):
        return "prose"
    if isinstance(value, list):
        return "v11" if all(isinstance(item, dict) for item in value) else "other"
    if isinstance(value, dict):
        return (
            "parallel"
            if any(isinstance(item, list) for item in value.values())
            else "scalar"
        )
    return "other"


def _detect_shape(decision_table: dict[str, Any]) -> str:
    rows = decision_table.get("rows")
    if (
        "columns" in decision_table
        and isinstance(rows, list)
        and rows
        and all(isinstance(row, list) for row in rows)
    ):
        return "columnar"
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return "unknown"
    rung_values = [
        row.get("action", {}).get("rungs")
        for row in rows
        if isinstance(row.get("action"), dict)
    ]
    if not rung_values:
        return "unknown"
    encodings = {_rungs_encoding(value) for value in rung_values}
    if encodings == {"v11"}:
        return "row_object_rungs_v11"
    if encodings == {"prose"}:
        return "row_object_prose_rungs"
    if encodings == {"scalar"}:
        return "row_object_scalar_rungs"
    if encodings == {"parallel"}:
        return "row_object_parallel_list_rungs"
    return "unknown"


def _average_cost(row: dict[str, Any]) -> Decimal | None:
    action = row.get("action")
    if isinstance(action, dict):
        value = _first_number(action.get("avg_price"), action.get("average_cost"))
        if value is not None:
            return value
    conditions = row.get("conditions")
    if not isinstance(conditions, list):
        return None
    for condition in conditions:
        if (
            isinstance(condition, dict)
            and isinstance(condition.get("metric"), str)
            and (
                "avg_buy_price" in condition["metric"]
                or "average_cost" in condition["metric"]
            )
        ):
            return _first_number(condition.get("value"))
    return None


def _contains_non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_non_finite(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_non_finite(item) for item in value)
    return False


def _canonical_digest(decision_table: dict[str, Any]) -> str | None:
    if _contains_non_finite(decision_table):
        return None
    try:
        canon = json.dumps(
            decision_table,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canon).hexdigest()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float, Decimal))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("NaN")


def _first_number(*values: Any) -> Decimal | None:
    for value in values:
        if _is_number(value):
            return _decimal(value)
    return None


def _collapse_scalar(values: list[Any]) -> Any:
    return values[0] if len(values) == 1 else values


def _recomputed_row(scenario_id: Any, price: Any, qty: Any) -> dict[str, Any]:
    return {"scenario_id": scenario_id, "price": price, "qty": qty}


def _violation(
    row: int | None,
    rule: str,
    expected: Any,
    actual: Any,
    severity: str,
    detected_shape: str,
) -> dict[str, Any]:
    return {
        "row": row,
        "rule": rule,
        "expected": expected,
        "actual": actual,
        "severity": severity,
        "detected_shape": detected_shape,
        "canonical_shape_ref": CANONICAL_SHAPE_REF,
    }


def _result(
    policy: Any,
    detected_shape: str,
    violations: list[dict[str, Any]],
    digest: str | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "valid": not any(item["severity"] == "block" for item in violations),
        "detected_shape": detected_shape,
        "violations": violations,
        "recomputed": {"hash": digest, "rows": rows},
        "policy": policy,
    }
