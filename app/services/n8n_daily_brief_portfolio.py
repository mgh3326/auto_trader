"""Portfolio summary builders for n8n daily briefs."""

from __future__ import annotations

from typing import Any

from app.services.order_brief_formatting import fmt_pnl, fmt_value


def _build_portfolio_summary(
    overview: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build per-market portfolio summary from PortfolioOverviewService output."""
    positions = overview.get("positions", [])
    by_market: dict[str, list[dict[str, Any]]] = {}

    for pos in positions:
        market_type = str(pos.get("market_type", "")).upper()
        market_map = {"KR": "kr", "US": "us", "CRYPTO": "crypto"}
        market = market_map.get(market_type, "")
        if market:
            by_market.setdefault(market, []).append(pos)

    result: dict[str, dict[str, Any]] = {}
    for market, market_positions in by_market.items():
        total_eval = sum(float(p.get("evaluation") or 0) for p in market_positions)

        # Derive cost from profit_rate and evaluation to avoid currency mismatch.
        # For US stocks, avg_price may be in KRW (manual holdings) or USD (KIS),
        # but profit_rate and evaluation are always in the same currency context.
        total_cost = 0.0
        for p in market_positions:
            eval_amt = float(p.get("evaluation") or 0)
            rate = p.get("profit_rate")
            if eval_amt > 0 and rate is not None:
                denominator = 1.0 + float(rate)
                if denominator > 0:
                    total_cost += eval_amt / denominator
                else:
                    # profit_rate == -1.0 means total loss; cost = eval - profit_loss
                    profit_loss = float(p.get("profit_loss") or 0)
                    total_cost += eval_amt - profit_loss
            elif eval_amt <= 0 and rate is not None and rate <= -1.0:
                # Zero evaluation, total loss — derive cost from profit_loss
                profit_loss = float(p.get("profit_loss") or 0)
                total_cost += -profit_loss if profit_loss < 0 else 0
            else:
                # Fallback: use avg_price * quantity (safe for same-currency markets)
                avg = float(p.get("avg_price") or 0)
                qty = float(p.get("quantity") or 0)
                total_cost += avg * qty

        pnl_pct = (
            ((total_eval - total_cost) / total_cost * 100) if total_cost > 0 else None
        )

        # Top gainers/losers by profit_rate
        sorted_positions = sorted(
            [
                p
                for p in market_positions
                if p.get("profit_rate") is not None and not p.get("dust")
            ],
            key=lambda p: float(p.get("profit_rate") or 0),
            reverse=True,
        )
        top_gainers = [
            {
                "symbol": p["symbol"],
                "change_pct": fmt_pnl(float(p["profit_rate"]) * 100),
            }
            for p in sorted_positions[:3]
            if float(p.get("profit_rate") or 0) > 0
        ]
        top_losers = [
            {
                "symbol": p["symbol"],
                "change_pct": fmt_pnl(float(p["profit_rate"]) * 100),
            }
            for p in reversed(sorted_positions[-3:])
            if float(p.get("profit_rate") or 0) < 0
        ]

        currency = "USD" if market == "us" else "KRW"
        summary: dict[str, Any] = {
            "total_value_fmt": fmt_value(total_eval, currency),
            "pnl_pct": round(pnl_pct, 1) if pnl_pct is not None else None,
            "pnl_fmt": fmt_pnl(round(pnl_pct, 1) if pnl_pct is not None else None),
            "position_count": len(market_positions),
            "top_gainers": top_gainers,
            "top_losers": top_losers,
            "dust_positions": [
                {
                    "symbol": p.get("symbol"),
                    "quantity": p.get("quantity"),
                    "current_krw_value": float(p.get("evaluation") or 0),
                }
                for p in market_positions
                if p.get("dust")
            ],
        }

        if market == "us":
            summary["total_value_usd"] = total_eval
            summary["total_value_krw"] = None
        else:
            summary["total_value_krw"] = total_eval
            summary["total_value_usd"] = None

        result[market] = summary

    return result
