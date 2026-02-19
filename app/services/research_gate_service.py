from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(slots=True)
class GateResult:
    status: str
    reason_code: str
    thresholds: dict[str, float | None]
    metrics: dict[str, float]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_candidate(
    *,
    total_trades: int,
    profit_factor: float | Decimal,
    max_drawdown: float | Decimal,
    config: dict[str, Any],
    expectancy: float | Decimal | None = None,
    total_return: float | Decimal | None = None,
) -> GateResult:
    min_trades = int(config.get("minimum_trade_count", 0))
    min_profit_factor = _to_float(config.get("minimum_profit_factor"), 0.0)
    max_drawdown_limit = _to_float(config.get("maximum_drawdown"), 1.0)
    raw_min_expectancy = config.get("minimum_expectancy")
    min_expectancy = (
        _to_float(raw_min_expectancy) if raw_min_expectancy is not None else None
    )
    raw_min_total_return = config.get("minimum_total_return")
    min_total_return = (
        _to_float(raw_min_total_return) if raw_min_total_return is not None else None
    )

    numeric_profit_factor = _to_float(profit_factor)
    numeric_max_drawdown = _to_float(max_drawdown)
    numeric_expectancy = _to_float(expectancy)
    numeric_total_return = _to_float(total_return)

    thresholds = {
        "minimum_trade_count": float(min_trades),
        "minimum_profit_factor": min_profit_factor,
        "maximum_drawdown": max_drawdown_limit,
        "minimum_expectancy": min_expectancy,
        "minimum_total_return": min_total_return,
    }
    metrics = {
        "total_trades": float(total_trades),
        "profit_factor": numeric_profit_factor,
        "max_drawdown": numeric_max_drawdown,
        "expectancy": numeric_expectancy,
        "total_return": numeric_total_return,
    }

    if total_trades < min_trades:
        return GateResult(
            status="FAIL",
            reason_code="MIN_TRADES",
            thresholds=thresholds,
            metrics=metrics,
        )

    if numeric_profit_factor < min_profit_factor:
        return GateResult(
            status="FAIL",
            reason_code="LOW_PROFIT_FACTOR",
            thresholds=thresholds,
            metrics=metrics,
        )

    if numeric_max_drawdown > max_drawdown_limit:
        return GateResult(
            status="FAIL",
            reason_code="HIGH_DRAWDOWN",
            thresholds=thresholds,
            metrics=metrics,
        )

    if min_expectancy is not None and numeric_expectancy < min_expectancy:
        return GateResult(
            status="FAIL",
            reason_code="LOW_EXPECTANCY",
            thresholds=thresholds,
            metrics=metrics,
        )

    if min_total_return is not None and numeric_total_return < min_total_return:
        return GateResult(
            status="FAIL",
            reason_code="LOW_RETURN",
            thresholds=thresholds,
            metrics=metrics,
        )

    return GateResult(
        status="PASS",
        reason_code="OK",
        thresholds=thresholds,
        metrics=metrics,
    )
