from __future__ import annotations

from typing import Any

from app.schemas.research_backtest import BacktestPairSummary, BacktestRunSummary


def _pick_strategy_name(payload: dict[str, Any], strategy_name: str | None) -> str:
    if strategy_name:
        return strategy_name
    strategies = payload.get("strategy")
    if isinstance(strategies, dict) and strategies:
        return next(iter(strategies))
    raw_name = payload.get("strategy_name")
    if isinstance(raw_name, str) and raw_name.strip():
        return raw_name.strip()
    raise ValueError("strategy_name is required")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_pairs(
    payload: dict[str, Any], strategy_metrics: dict[str, Any]
) -> list[dict[str, Any]]:
    rows_obj = strategy_metrics.get("results_per_pair")
    if isinstance(rows_obj, list):
        rows = rows_obj
    else:
        payload_pairs = payload.get("pairs")
        rows = payload_pairs if isinstance(payload_pairs, list) else []

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pair_name = row.get("pair") or row.get("key")
        if (
            not isinstance(pair_name, str)
            or not pair_name.strip()
            or pair_name == "TOTAL"
        ):
            continue
        normalized.append(
            BacktestPairSummary(
                pair=pair_name,
                total_trades=int(
                    _safe_float(row.get("trades") or row.get("total_trades"), 0)
                ),
                profit_factor=row.get("profit_factor"),
                max_drawdown=row.get("max_drawdown"),
                total_return=row.get("profit_total") or row.get("total_return"),
            ).model_dump()
        )
    return normalized


def parse_backtest_summary(
    payload: dict[str, Any],
    *,
    strategy_name: str | None = None,
    runner: str | None = None,
) -> BacktestRunSummary:
    selected_strategy = _pick_strategy_name(payload, strategy_name)

    strategy_metrics = payload
    metadata: dict[str, Any] = {}
    if isinstance(payload.get("strategy"), dict):
        strategy_payload = payload["strategy"].get(selected_strategy)
        if not isinstance(strategy_payload, dict):
            raise ValueError(f"strategy payload not found: {selected_strategy}")
        strategy_metrics = strategy_payload
        metadata_bucket = payload.get("metadata")
        if isinstance(metadata_bucket, dict):
            candidate = metadata_bucket.get(selected_strategy)
            if isinstance(candidate, dict):
                metadata = candidate

    total_trades = int(
        _safe_float(
            strategy_metrics.get("total_trades", payload.get("total_trades", 0)),
            0,
        )
    )
    wins = _safe_float(strategy_metrics.get("wins"), 0)
    win_rate = None
    if total_trades > 0 and wins >= 0:
        win_rate = wins / total_trades

    normalized_runner = (runner or payload.get("runner") or "mac").strip()
    if not normalized_runner:
        normalized_runner = "mac"

    summary_payload = {
        "run_id": payload.get("run_id")
        or metadata.get("run_id")
        or f"{selected_strategy}-{payload.get('backtest_start_ts', 'unknown')}",
        "strategy_name": selected_strategy,
        "strategy_version": payload.get("strategy_version"),
        "exchange": payload.get("exchange") or "binance",
        "market": payload.get("market")
        or strategy_metrics.get("trading_mode")
        or "spot",
        "timeframe": payload.get("timeframe")
        or metadata.get("timeframe")
        or strategy_metrics.get("timeframe")
        or "5m",
        "timerange": payload.get("timerange") or metadata.get("timerange"),
        "runner": normalized_runner,
        "started_at": payload.get("started_at"),
        "ended_at": payload.get("ended_at"),
        "total_trades": total_trades,
        "profit_factor": strategy_metrics.get(
            "profit_factor", payload.get("profit_factor", 0)
        ),
        "max_drawdown": strategy_metrics.get(
            "max_drawdown_account",
            strategy_metrics.get("max_drawdown", payload.get("max_drawdown", 0)),
        ),
        "win_rate": payload.get("win_rate", win_rate),
        "expectancy": strategy_metrics.get("expectancy", payload.get("expectancy")),
        "total_return": strategy_metrics.get(
            "profit_total", payload.get("total_return")
        ),
        "artifact_path": payload.get("artifact_path"),
        "artifact_hash": payload.get("artifact_hash"),
        "pairs": _normalize_pairs(payload, strategy_metrics),
        "raw_payload": payload,
    }
    return BacktestRunSummary.model_validate(summary_payload)
