"""Detailed reporting helpers for the backtest CLI."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

import pandas as pd
import prepare


def _parse_date(value: str) -> datetime:
    """Parse YYYY-MM-DD or YYYY-MM-DD HH:MM:SS timestamps."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(value)


def build_round_trips(trade_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group raw trades into round trips per symbol until the position is flat."""
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for idx, trade in enumerate(trade_log):
        grouped[trade["symbol"]].append((idx, trade))

    round_trips: list[dict[str, Any]] = []
    for symbol, indexed_trades in grouped.items():
        indexed_trades.sort(key=lambda item: (item[1]["date"], item[0]))
        position_qty = 0.0
        buy_cost = 0.0
        total_pnl = 0.0
        entry_date: str | None = None
        entry_reason = ""
        last_sell_reason = ""
        last_exit_date: str | None = None

        for _idx, trade in indexed_trades:
            action = trade["action"]
            quantity = float(trade.get("quantity", 0.0))
            if action == "buy":
                if position_qty <= 0:
                    entry_date = trade["date"]
                    entry_reason = trade.get("reason", "")
                    buy_cost = 0.0
                    total_pnl = 0.0
                position_qty += quantity
                buy_cost += quantity * float(trade.get("price", 0.0)) + float(
                    trade.get("fee", 0.0)
                )
            elif action == "sell" and position_qty > 0:
                position_qty -= quantity
                total_pnl += float(trade.get("realized_pnl", 0.0))
                last_sell_reason = trade.get("reason", "")
                last_exit_date = trade["date"]

                if position_qty <= 1e-12 and entry_date and last_exit_date:
                    holding_days = (
                        _parse_date(last_exit_date) - _parse_date(entry_date)
                    ).days
                    return_pct = (total_pnl / buy_cost * 100.0) if buy_cost > 0 else 0.0
                    round_trips.append(
                        {
                            "symbol": symbol,
                            "entry_date": entry_date,
                            "exit_date": last_exit_date,
                            "holding_days": max(holding_days, 0),
                            "pnl": total_pnl,
                            "return_pct": return_pct,
                            "entry_reason": entry_reason,
                            "exit_reason": last_sell_reason,
                        }
                    )
                    position_qty = 0.0
                    buy_cost = 0.0
                    total_pnl = 0.0
                    entry_date = None
                    entry_reason = ""
                    last_sell_reason = ""
                    last_exit_date = None

    round_trips.sort(key=lambda trip: (trip["exit_date"], trip["symbol"]))
    return round_trips


def _calc_max_drawdown_pct(equity_values: list[float]) -> float:
    return prepare._calc_max_drawdown(equity_values)


def generate_monthly_table(
    equity_curve: list[float],
    equity_dates: list[str],
    trade_log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build monthly return rows from the equity curve."""
    if not equity_curve or not equity_dates or len(equity_curve) != len(equity_dates):
        return []

    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(equity_dates),
            "equity": equity_curve,
        }
    )
    frame["month"] = frame["date"].dt.to_period("M").astype(str)

    trades_by_month: dict[str, int] = defaultdict(int)
    for trade in trade_log:
        month = pd.to_datetime(trade["date"]).strftime("%Y-%m")
        trades_by_month[month] += 1

    rows: list[dict[str, Any]] = []
    for month, group in frame.groupby("month", sort=False):
        equities = group["equity"].astype(float).tolist()
        start_equity = equities[0]
        end_equity = equities[-1]
        return_pct = (
            (end_equity / start_equity - 1.0) * 100.0 if start_equity > 0 else 0.0
        )
        rows.append(
            {
                "month": month,
                "return_pct": return_pct,
                "equity": end_equity,
                "trades": trades_by_month.get(month, 0),
                "max_drawdown_pct": _calc_max_drawdown_pct(equities),
            }
        )
    return rows


def generate_symbol_table(trade_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate round-trip trades per symbol."""
    round_trips = build_round_trips(trade_log)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trip in round_trips:
        grouped[trip["symbol"]].append(trip)

    rows: list[dict[str, Any]] = []
    for symbol in sorted(grouped):
        trips = grouped[symbol]
        returns = [float(trip["return_pct"]) for trip in trips]
        pnls = [float(trip["pnl"]) for trip in trips]
        wins = sum(1 for pnl in pnls if pnl > 0)
        trades = len(trips)
        rows.append(
            {
                "symbol": symbol,
                "trades": trades,
                "win_rate_pct": (wins / trades * 100.0) if trades > 0 else 0.0,
                "avg_return_pct": sum(returns) / trades if trades > 0 else 0.0,
                "total_pnl": sum(pnls),
                "best_trade_pct": max(returns) if returns else 0.0,
                "worst_trade_pct": min(returns) if returns else 0.0,
            }
        )
    return rows


def _cagr(equity_curve: list[float], equity_dates: list[str]) -> float:
    if len(equity_curve) < 2 or len(equity_dates) < 2:
        return 0.0
    start = float(equity_curve[0])
    end = float(equity_curve[-1])
    if start <= 0 or end <= 0:
        return 0.0
    total_days = (_parse_date(equity_dates[-1]) - _parse_date(equity_dates[0])).days
    if total_days <= 0:
        return 0.0
    years = total_days / 365.25
    return (end / start) ** (1 / years) - 1.0


def _streak_lengths(round_trips: list[dict[str, Any]]) -> tuple[int, int]:
    max_wins = 0
    max_losses = 0
    wins = 0
    losses = 0
    for trip in round_trips:
        if float(trip["pnl"]) > 0:
            wins += 1
            losses = 0
        else:
            losses += 1
            wins = 0
        max_wins = max(max_wins, wins)
        max_losses = max(max_losses, losses)
    return max_losses, max_wins


def _drawdown_period_days(
    equity_curve: list[float], equity_dates: list[str]
) -> tuple[int, int | None]:
    if len(equity_curve) < 2 or len(equity_dates) < 2:
        return 0, None

    peak_value = float(equity_curve[0])
    peak_date = _parse_date(equity_dates[0])
    current_drawdown_start: datetime | None = None
    longest_days = 0

    max_drawdown_value = 0.0
    max_drawdown_peak = peak_value
    max_drawdown_trough_date: datetime | None = None
    max_drawdown_trough_index = 0

    for idx, (date_str, equity) in enumerate(
        zip(equity_dates, equity_curve, strict=True)
    ):
        date = _parse_date(date_str)
        equity_value = float(equity)
        if equity_value >= peak_value:
            if current_drawdown_start is not None:
                longest_days = max(longest_days, (date - current_drawdown_start).days)
                current_drawdown_start = None
            peak_value = equity_value
            peak_date = date
            continue

        if current_drawdown_start is None:
            current_drawdown_start = peak_date

        drawdown_value = (
            (peak_value - equity_value) / peak_value if peak_value > 0 else 0.0
        )
        if drawdown_value > max_drawdown_value:
            max_drawdown_value = drawdown_value
            max_drawdown_peak = peak_value
            max_drawdown_trough_date = date
            max_drawdown_trough_index = idx

    if current_drawdown_start is not None:
        longest_days = max(
            longest_days,
            (_parse_date(equity_dates[-1]) - current_drawdown_start).days,
        )

    recovery_days: int | None = None
    if max_drawdown_trough_date is not None:
        for idx in range(max_drawdown_trough_index + 1, len(equity_curve)):
            if float(equity_curve[idx]) >= max_drawdown_peak:
                recovery_days = (
                    _parse_date(equity_dates[idx]) - max_drawdown_trough_date
                ).days
                break

    return longest_days, recovery_days


def _time_in_market_pct(
    equity_dates: list[str], trade_log: list[dict[str, Any]]
) -> float:
    if len(equity_dates) <= 1:
        return 0.0

    bars = equity_dates[1:]
    trades_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trade_log:
        trades_by_date[trade["date"]].append(trade)

    positions: dict[str, float] = defaultdict(float)
    days_in_market = 0
    for date in bars:
        for trade in trades_by_date.get(date, []):
            symbol = trade["symbol"]
            quantity = float(trade.get("quantity", 0.0))
            if trade["action"] == "buy":
                positions[symbol] += quantity
            elif trade["action"] == "sell":
                positions[symbol] = max(0.0, positions[symbol] - quantity)
                if positions[symbol] <= 1e-12:
                    positions.pop(symbol, None)
        if positions:
            days_in_market += 1
    return days_in_market / len(bars) * 100.0


def generate_risk_metrics(
    equity_curve: list[float],
    equity_dates: list[str],
    trade_log: list[dict[str, Any]],
    time_in_market_pct: float | None = None,
) -> dict[str, Any]:
    """Build risk metrics based on the approved definitions.

    Args:
        equity_curve: List of equity values over time
        equity_dates: List of dates corresponding to equity values
        trade_log: List of executed trades
        time_in_market_pct: Optional override for time-in-market percentage.
            If not provided, will be calculated from trade_log.
    """
    round_trips = build_round_trips(trade_log)
    wins = [float(trip["pnl"]) for trip in round_trips if float(trip["pnl"]) > 0]
    losses = [abs(float(trip["pnl"])) for trip in round_trips if float(trip["pnl"]) < 0]
    max_losses, max_wins = _streak_lengths(round_trips)
    longest_dd_days, recovery_days = _drawdown_period_days(equity_curve, equity_dates)
    cagr = _cagr(equity_curve, equity_dates)
    max_dd_ratio = _calc_max_drawdown_pct(equity_curve) / 100.0

    # Use provided time_in_market_pct or calculate from trade log
    tim_pct = (
        time_in_market_pct
        if time_in_market_pct is not None
        else _time_in_market_pct(equity_dates, trade_log)
    )

    return {
        "calmar_ratio": (cagr / max_dd_ratio) if max_dd_ratio > 0 else 0.0,
        "avg_win_avg_loss": (
            (sum(wins) / len(wins)) / (sum(losses) / len(losses))
            if wins and losses
            else 0.0
        ),
        "max_consecutive_losses": max_losses,
        "max_consecutive_wins": max_wins,
        "longest_drawdown_period_days": longest_dd_days,
        "recovery_time_from_max_dd_days": recovery_days,
        "time_in_market_pct": tim_pct,
    }


def _build_cv_rows(cv_result: prepare.CVResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for position, (score, fold_result) in enumerate(
        zip(cv_result.fold_scores, cv_result.fold_results, strict=True)
    ):
        fold_index = cv_result.fold_indices[position]
        fold_meta = prepare.CV_FOLDS[fold_index]
        rows.append(
            {
                "fold": fold_index + 1,
                "start": fold_meta["val_start"],
                "end": fold_meta["val_end"],
                "score": score,
                "sharpe": fold_result.sharpe,
                "return_pct": fold_result.total_return_pct,
                "max_drawdown_pct": fold_result.max_drawdown_pct,
                "trades": fold_result.num_trades,
                "win_rate_pct": fold_result.win_rate_pct * 100.0,
            }
        )
    return rows


def build_report_payload(
    result: prepare.BacktestResult,
    data: dict[str, Any],
    split_info: dict[str, str],
    cv_result: prepare.CVResult,
    initial_capital: float = prepare.INITIAL_CAPITAL,
) -> dict[str, Any]:
    """Build the nested report payload used for both text and JSON output."""
    round_trips = build_round_trips(result.trade_log)
    buy_count = sum(1 for trade in result.trade_log if trade["action"] == "buy")
    sell_count = sum(1 for trade in result.trade_log if trade["action"] == "sell")
    summary = {
        "split": split_info["name"],
        "start": split_info["start"],
        "end": split_info["end"],
        "initial_capital": initial_capital,
        "final_equity": result.equity_curve[-1]
        if result.equity_curve
        else initial_capital,
        "total_return_pct": result.total_return_pct,
        "sharpe_ratio": result.sharpe,
        "max_drawdown_pct": result.max_drawdown_pct,
        "profit_factor": result.profit_factor,
        "total_trades": len(result.trade_log),
        "buy_trades": buy_count,
        "sell_trades": sell_count,
        "win_rate_pct": result.win_rate_pct * 100.0,
        "avg_holding_days": result.avg_holding_days,
        "time_in_market_pct": result.time_in_market_pct,
        "score": prepare.compute_score(result),
        "symbols": sorted(data.keys()),
    }

    monthly_rows = generate_monthly_table(
        result.equity_curve,
        result.equity_dates,
        result.trade_log,
    )
    symbol_rows = generate_symbol_table(result.trade_log)
    top_trades = sorted(
        round_trips,
        key=lambda trip: float(trip["return_pct"]),
        reverse=True,
    )[:5]
    bottom_trades = sorted(round_trips, key=lambda trip: float(trip["return_pct"]))[:5]
    cv_rows = _build_cv_rows(cv_result)
    risk_metrics = generate_risk_metrics(
        result.equity_curve,
        result.equity_dates,
        result.trade_log,
        time_in_market_pct=result.time_in_market_pct,
    )

    return {
        "summary": summary,
        "monthly_returns": monthly_rows,
        "per_symbol": symbol_rows,
        "top_trades": top_trades,
        "bottom_trades": bottom_trades,
        "cv": {
            "folds": cv_rows,
            "cv_score": cv_result.cv_score,
            "mean_score": cv_result.mean_score,
            "std_score": cv_result.std_score,
            "min_fold_score": cv_result.min_score,
        },
        "risk_metrics": risk_metrics,
    }


def _format_currency(value: float) -> str:
    return f"₩{value:,.0f}"


def _format_currency_compact(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 1_000_000:
        return f"₩{value / 1_000_000:.2f}M"
    if abs_value >= 1_000:
        return f"₩{value / 1_000:.1f}K"
    return _format_currency(value)


def _format_pct(value: float, signed: bool = True) -> str:
    if signed:
        return f"{value:+.2f}%"
    return f"{value:.2f}%"


def render_report_text(payload: dict[str, Any]) -> str:
    """Render the nested payload as a readable terminal report."""
    summary = payload["summary"]
    lines = [
        "========== STRATEGY REPORT ==========",
        f"Split: {summary['split']} ({summary['start']} ~ {summary['end']})",
        f"Initial Capital: {_format_currency(summary['initial_capital'])}",
        f"Final Equity: {_format_currency(summary['final_equity'])}",
        f"Total Return: {_format_pct(summary['total_return_pct'])}",
        f"Sharpe Ratio: {summary['sharpe_ratio']:.2f}",
        f"Max Drawdown: -{abs(summary['max_drawdown_pct']):.2f}%",
        f"Profit Factor: {summary['profit_factor']:.2f}",
        (
            f"Total Trades: {summary['total_trades']} "
            f"(Buy: {summary['buy_trades']}, Sell: {summary['sell_trades']})"
        ),
        f"Win Rate: {summary['win_rate_pct']:.1f}%",
        f"Avg Holding Days: {summary['avg_holding_days']:.1f}",
        f"Score: {summary['score']:.6f}",
        "",
        "Monthly Returns:",
        "Month       | Return   | Equity    | Trades | Max DD",
    ]

    for row in payload["monthly_returns"]:
        lines.append(
            f"{row['month']:<11} | "
            f"{_format_pct(row['return_pct']):>8} | "
            f"{_format_currency_compact(row['equity']):>9} | "
            f"{row['trades']:<6} | "
            f"-{abs(row['max_drawdown_pct']):.2f}%"
        )

    lines.extend(
        [
            "",
            "Per-Symbol Performance:",
            "Symbol | Trades | Win Rate | Avg Return | Total PnL   | Best Trade | Worst Trade",
        ]
    )
    for row in payload["per_symbol"]:
        lines.append(
            f"{row['symbol']:<6} | "
            f"{row['trades']:<6} | "
            f"{row['win_rate_pct']:>7.1f}% | "
            f"{_format_pct(row['avg_return_pct']):>10} | "
            f"{_format_currency_compact(row['total_pnl']):>11} | "
            f"{_format_pct(row['best_trade_pct']):>10} | "
            f"{_format_pct(row['worst_trade_pct']):>11}"
        )

    lines.extend(["", "Top 5 Winning Trades:"])
    for index, trip in enumerate(payload["top_trades"], start=1):
        lines.append(
            f"  {index}. {trip['symbol']} {trip['entry_date']} -> {trip['exit_date']} | "
            f"{_format_pct(trip['return_pct'])} | {_format_currency_compact(trip['pnl'])} | "
            f"reason: {trip['entry_reason'] or trip['exit_reason']}"
        )

    lines.extend(["", "Bottom 5 Losing Trades:"])
    for index, trip in enumerate(payload["bottom_trades"], start=1):
        lines.append(
            f"  {index}. {trip['symbol']} {trip['entry_date']} -> {trip['exit_date']} | "
            f"{_format_pct(trip['return_pct'])} | {_format_currency_compact(trip['pnl'])} | "
            f"reason: {trip['exit_reason'] or trip['entry_reason']}"
        )

    cv = payload["cv"]
    lines.extend(["", "Cross-Validation:"])
    for row in cv["folds"]:
        lines.append(
            f"  Fold {row['fold']} [{row['start']} ~ {row['end']}] "
            f"score={row['score']:.4f} sharpe={row['sharpe']:.2f} "
            f"return={row['return_pct']:.2f}% max_dd={row['max_drawdown_pct']:.2f}% "
            f"trades={row['trades']} win_rate={row['win_rate_pct']:.1f}%"
        )
    lines.extend(
        [
            f"  cv_score: {cv['cv_score']:.6f}",
            f"  mean_score: {cv['mean_score']:.6f}",
            f"  std_score: {cv['std_score']:.6f}",
            f"  min_fold_score: {cv['min_fold_score']:.6f}",
            "",
            "Risk Metrics:",
        ]
    )

    risk = payload["risk_metrics"]
    lines.extend(
        [
            f"  Calmar Ratio: {risk['calmar_ratio']:.2f}",
            f"  Avg Win / Avg Loss: {risk['avg_win_avg_loss']:.2f}",
            f"  Max Consecutive Losses: {risk['max_consecutive_losses']}",
            f"  Max Consecutive Wins: {risk['max_consecutive_wins']}",
            f"  Longest Drawdown Period: {risk['longest_drawdown_period_days']} days",
            (
                "  Recovery Time from Max DD: "
                f"{risk['recovery_time_from_max_dd_days']} days"
                if risk["recovery_time_from_max_dd_days"] is not None
                else "  Recovery Time from Max DD: N/A"
            ),
            f"  Time in Market: {risk['time_in_market_pct']:.1f}%",
        ]
    )

    return "\n".join(lines)


def generate_report(
    result: prepare.BacktestResult,
    data: dict[str, Any],
    split_info: dict[str, str],
    cv_result: prepare.CVResult,
    output: str = "text",
    initial_capital: float = prepare.INITIAL_CAPITAL,
) -> str | dict[str, Any]:
    """Build the report payload and render it in the requested format."""
    payload = build_report_payload(
        result,
        data=data,
        split_info=split_info,
        cv_result=cv_result,
        initial_capital=initial_capital,
    )
    if output == "json":
        return payload
    return render_report_text(payload)
