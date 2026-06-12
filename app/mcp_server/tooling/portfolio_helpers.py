"""Holdings-specific helpers for portfolio MCP tools.

These helpers were extracted from shared.py to keep shared.py limited to
cross-cutting utilities (market detection, type converters, error payloads).

The sole primary consumer is portfolio_holdings.py; min_order_krw is also
used by portfolio_overview_service.py.
"""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.shared import (
    DEFAULT_MINIMUM_VALUES,
    normalize_position_symbol,
    to_float,
)


def position_to_output(position: dict[str, Any]) -> dict[str, Any]:
    output = {
        "symbol": position["symbol"],
        "name": position["name"],
        "market": position["market"],
        "quantity": position["quantity"],
        "avg_buy_price": position["avg_buy_price"],
        "current_price": position["current_price"],
        "evaluation_amount": position["evaluation_amount"],
        "profit_loss": position["profit_loss"],
        "profit_rate": position["profit_rate"],
        "dust": bool(position.get("dust", False)),
    }
    if "price_error" in position:
        output["price_error"] = position["price_error"]
    if "strategy_signal" in position:
        output["strategy_signal"] = position["strategy_signal"]
    if "sellable_quantity" in position:
        output["sellable_quantity"] = position["sellable_quantity"]
    if "source" in position:
        output["source"] = position["source"]
    return output


def value_for_minimum_filter(position: dict[str, Any]) -> float:
    evaluation_amount = position.get("evaluation_amount")
    if evaluation_amount is not None:
        return to_float(evaluation_amount, default=0.0)

    quantity = to_float(position.get("quantity"))
    current_price = to_float(position.get("current_price"))
    if current_price <= 0:
        return 0.0
    return quantity * current_price


def min_order_krw(symbol: str) -> float:
    """Return KRW minimum order threshold for crypto positions.

    Upbit KRW markets share a fixed minimum order amount in current policy.
    The ``symbol`` parameter is intentionally retained for future per-market
    extensions without changing the public function signature.
    """
    _ = symbol
    return DEFAULT_MINIMUM_VALUES["crypto"]


def format_filter_threshold(value: float) -> str:
    return f"{value:g}"


def build_holdings_summary(
    positions: list[dict[str, Any]], include_current_price: bool
) -> dict[str, Any]:
    total_buy_amount = round(
        sum(
            to_float(position.get("avg_buy_price")) * to_float(position.get("quantity"))
            for position in positions
        ),
        2,
    )

    if not include_current_price:
        return {
            "total_buy_amount": total_buy_amount,
            "total_evaluation": None,
            "total_profit_loss": None,
            "total_profit_rate": None,
            "position_count": len(positions),
            "weights": None,
        }

    total_evaluation = round(
        sum(to_float(position.get("evaluation_amount")) for position in positions),
        2,
    )
    total_profit_loss = round(
        sum(to_float(position.get("profit_loss")) for position in positions),
        2,
    )
    total_profit_rate = (
        round((total_profit_loss / total_buy_amount) * 100, 2)
        if total_buy_amount > 0
        else None
    )

    weights: list[dict[str, Any]] = []
    if total_evaluation > 0:
        for position in positions:
            evaluation = to_float(position.get("evaluation_amount"))
            if evaluation <= 0:
                continue
            weights.append(
                {
                    "symbol": position.get("symbol"),
                    "name": position.get("name"),
                    "weight_pct": round((evaluation / total_evaluation) * 100, 2),
                }
            )
        weights.sort(key=lambda item: to_float(item.get("weight_pct")), reverse=True)

    return {
        "total_buy_amount": total_buy_amount,
        "total_evaluation": total_evaluation,
        "total_profit_loss": total_profit_loss,
        "total_profit_rate": total_profit_rate,
        "position_count": len(positions),
        "weights": weights,
    }


def is_position_symbol_match(
    *,
    position_symbol: str,
    query_symbol: str,
    instrument_type: str,
) -> bool:
    if instrument_type == "crypto":
        pos_norm = normalize_position_symbol(position_symbol, "crypto")
        query_norm = normalize_position_symbol(query_symbol, "crypto")
        if pos_norm == query_norm:
            return True
        pos_base = pos_norm.split("-", 1)[-1]
        query_base = query_norm.split("-", 1)[-1]
        return pos_base == query_base

    if instrument_type == "equity_us":
        from app.core.symbol import to_db_symbol

        return (
            to_db_symbol(position_symbol).upper() == to_db_symbol(query_symbol).upper()
        )

    return position_symbol.upper() == query_symbol.upper()


def recalculate_profit_fields(position: dict[str, Any]) -> None:
    current_price = position.get("current_price")
    quantity = to_float(position.get("quantity"))
    avg_buy_price = to_float(position.get("avg_buy_price"))

    if current_price is None or quantity <= 0:
        position["current_price"] = None
        position["evaluation_amount"] = None
        position["profit_loss"] = None
        position["profit_rate"] = None
        return

    current_price = to_float(current_price)
    position["current_price"] = current_price
    position["evaluation_amount"] = round(current_price * quantity, 2)

    if avg_buy_price > 0:
        profit_loss = (current_price - avg_buy_price) * quantity
        position["profit_loss"] = round(profit_loss, 2)
        position["profit_rate"] = round(
            ((current_price - avg_buy_price) / avg_buy_price) * 100, 2
        )
    else:
        position["profit_loss"] = None
        position["profit_rate"] = None


__all__ = [
    "position_to_output",
    "value_for_minimum_filter",
    "min_order_krw",
    "format_filter_threshold",
    "build_holdings_summary",
    "is_position_symbol_match",
    "recalculate_profit_fields",
]
