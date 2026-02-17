from __future__ import annotations

from typing import Any


async def simulate_avg_cost_impl(
    holdings: dict[str, float],
    plans: list[dict[str, float]],
    current_market_price: float | None = None,
    target_price: float | None = None,
) -> dict[str, Any]:
    h_price = holdings.get("price")
    if h_price is None:
        h_price = holdings.get("avg_price")
    h_qty = holdings.get("quantity")
    if h_price is None or h_qty is None:
        raise ValueError(
            "holdings must contain 'price' (or 'avg_price') and 'quantity'"
        )
    h_price = float(h_price)
    h_qty = float(h_qty)
    if h_price < 0 or h_qty < 0:
        raise ValueError("holdings price and quantity must be >= 0")

    if not plans:
        raise ValueError("plans must contain at least one entry")

    validated_plans: list[tuple[float, float]] = []
    for i, p in enumerate(plans):
        pp = p.get("price")
        if pp is None:
            pp = p.get("avg_price")
        pq = p.get("quantity")
        if pp is None or pq is None:
            raise ValueError(
                f"plans[{i}] must contain 'price' (or 'avg_price') and 'quantity'"
            )
        pp, pq = float(pp), float(pq)
        if pp <= 0 or pq <= 0:
            raise ValueError(f"plans[{i}] price and quantity must be > 0")
        validated_plans.append((pp, pq))

    mkt = float(current_market_price) if current_market_price is not None else None
    tp = float(target_price) if target_price is not None else None
    if tp is not None and tp <= 0:
        raise ValueError("target_price must be > 0")

    total_qty = h_qty
    total_invested_raw = h_price * h_qty
    avg_price_raw = (total_invested_raw / total_qty) if total_qty > 0 else None
    avg_price = round(avg_price_raw, 2) if avg_price_raw is not None else None

    current_position: dict[str, Any] = {
        "avg_price": avg_price,
        "total_quantity": total_qty,
        "total_invested": round(total_invested_raw, 2),
    }

    if mkt is not None and avg_price is not None:
        pnl = round((mkt - avg_price) * total_qty, 2)
        pnl_pct = round((mkt / avg_price - 1) * 100, 2)
        current_position["unrealized_pnl"] = pnl
        current_position["unrealized_pnl_pct"] = pnl_pct
        current_position["pnl_vs_current"] = pnl
        current_position["pnl_vs_current_pct"] = pnl_pct

    if tp is not None and avg_price is not None:
        projected_profit = round((tp - avg_price) * total_qty, 2)
        target_return_pct = round((tp / avg_price - 1) * 100, 2)
        current_position["target_profit"] = projected_profit
        current_position["target_return_pct"] = target_return_pct

    steps: list[dict[str, Any]] = []
    for idx, (bp, bq) in enumerate(validated_plans, start=1):
        total_invested_raw += bp * bq
        total_qty = round(total_qty + bq, 10)
        avg_price = round(total_invested_raw / total_qty, 2)

        step: dict[str, Any] = {
            "step": idx,
            "buy_price": bp,
            "buy_quantity": bq,
            "new_avg_price": avg_price,
            "total_quantity": total_qty,
            "total_invested": round(total_invested_raw, 2),
        }
        if mkt is not None:
            breakeven_pct = round((avg_price / mkt - 1) * 100, 2)
            pnl = round((mkt - avg_price) * total_qty, 2)
            pnl_pct = round((mkt / avg_price - 1) * 100, 2)
            step["breakeven_change_pct"] = breakeven_pct
            step["unrealized_pnl"] = pnl
            step["unrealized_pnl_pct"] = pnl_pct
            step["pnl_vs_current"] = pnl
            step["pnl_vs_current_pct"] = pnl_pct

        if tp is not None:
            target_profit = round((tp - avg_price) * total_qty, 2)
            target_return_pct = round((tp / avg_price - 1) * 100, 2)
            step["target_profit"] = target_profit
            step["target_return_pct"] = target_return_pct

        steps.append(step)

    result: dict[str, Any] = {
        "current_position": current_position,
        "steps": steps,
    }
    if mkt is not None:
        result["current_market_price"] = mkt

    if tp is not None and steps:
        final_avg_price = float(steps[-1]["new_avg_price"])
        profit_per_unit = round(tp - final_avg_price, 2)
        total_profit = round(profit_per_unit * total_qty, 2)
        total_return_pct = round((tp / final_avg_price - 1) * 100, 2)
        result["target_analysis"] = {
            "target_price": tp,
            "final_avg_price": final_avg_price,
            "profit_per_unit": profit_per_unit,
            "total_profit": total_profit,
            "total_return_pct": total_return_pct,
        }

    return result


__all__ = ["simulate_avg_cost_impl"]
