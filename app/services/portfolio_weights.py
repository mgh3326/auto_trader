"""Shared weight-computation helpers for portfolio services."""

from __future__ import annotations

from typing import Any


def round_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 1)


def build_weights(
    positions: list[dict[str, Any]],
    base: dict[str, Any],
) -> dict[str, float | None]:
    def get_eval_krw(p: dict) -> float | None:
        market_type = str(p.get("market_type") or "").upper()
        val = p.get("evaluation_krw")
        if val is not None:
            return float(val)
        if market_type == "US":
            return None
        return float(p.get("evaluation", 0) or 0)

    base_evaluation_krw = get_eval_krw(base)
    if base_evaluation_krw in (None, 0):
        return {"portfolio_weight_pct": None, "market_weight_pct": None}

    portfolio_values = [get_eval_krw(p) for p in positions]
    total_portfolio_eval_krw = (
        sum(value for value in portfolio_values if value is not None)
        if all(value is not None for value in portfolio_values)
        else None
    )

    market_type = base.get("market_type")
    same_market_values = [
        get_eval_krw(p) for p in positions if p.get("market_type") == market_type
    ]
    total_same_market_eval_krw = (
        sum(value for value in same_market_values if value is not None)
        if all(value is not None for value in same_market_values)
        else None
    )

    portfolio_weight = (
        (base_evaluation_krw / total_portfolio_eval_krw) * 100
        if total_portfolio_eval_krw not in (None, 0)
        else None
    )
    market_weight = (
        (base_evaluation_krw / total_same_market_eval_krw) * 100
        if total_same_market_eval_krw not in (None, 0)
        else None
    )

    return {
        "portfolio_weight_pct": round_pct(portfolio_weight),
        "market_weight_pct": round_pct(market_weight),
    }
