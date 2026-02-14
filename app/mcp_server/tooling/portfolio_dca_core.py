"""DCA plan creation helpers for portfolio tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.mcp_server.tick_size import adjust_tick_size_kr
from app.mcp_server.tooling.market_data import (
    _compute_dca_price_levels,
    _compute_rsi_weights,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable


_DcaPlanStepRow = dict[str, Any]
_DcaPlanResponse = dict[str, Any]


async def simulate_avg_cost_impl(
    holdings: dict[str, float],
    plans: list[dict[str, float]],
    current_market_price: float | None = None,
    target_price: float | None = None,
) -> dict[str, Any]:
    """Simulate averaging-down / dollar-cost averaging."""
    h_price = holdings.get("price")
    if h_price is None:
        h_price = holdings.get("avg_price")
    h_qty = holdings.get("quantity")
    if h_price is None or h_qty is None:
        raise ValueError("holdings must contain 'price' (or 'avg_price') and 'quantity'")
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


def _validate_create_dca_input(
    symbol: str,
    total_amount: float,
    splits: int,
    strategy: str,
    execute_steps: list[int] | None,
    resolve_market_type: Callable[[str, str | None], tuple[str, str]],
) -> tuple[str, str, str]:
    """Validate shared create_dca_plan inputs.

    Returns `(market_type, normalized_symbol, source)`.
    """
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    total_amount = float(total_amount)
    if total_amount <= 0:
        raise ValueError("total_amount must be greater than 0")

    if not (2 <= splits <= 5):
        raise ValueError("splits must be between 2 and 5")

    valid_strategies = {"support", "equal", "aggressive"}
    if strategy not in valid_strategies:
        raise ValueError(
            f"Invalid strategy '{strategy}'. Must be one of: {', '.join(sorted(valid_strategies))}"
        )

    if execute_steps is not None:
        invalid_steps = [s for s in execute_steps if not (1 <= s <= splits)]
        if invalid_steps:
            raise ValueError(
                f"execute_steps must be between 1 and {splits}, got: {invalid_steps}"
            )

    market_type, normalized_symbol = resolve_market_type(symbol, None)
    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
    return market_type, normalized_symbol, source_map[market_type]


def _build_dca_plan_rows(
    *,
    market_type: str,
    normalized_symbol: str,
    current_price: float,
    supports: list[dict[str, Any]],
    total_amount: float,
    splits: int,
    strategy: str,
    rsi_value: float | None,
) -> tuple[list[_DcaPlanStepRow], dict[str, Any]]:
    if not current_price:
        raise ValueError("current_price is required")

    price_levels = _compute_dca_price_levels(strategy, splits, current_price, supports)
    weights = _compute_rsi_weights(rsi_value, splits)

    plans: list[_DcaPlanStepRow] = []
    total_quantity = 0.0
    for step, (level, weight) in enumerate(
        zip(price_levels, weights, strict=True),
        start=1,
    ):
        step_amount = total_amount * weight
        step_price = level["price"]
        level_source = level["source"]

        if market_type == "equity_kr":
            original_price = step_price
            step_price = adjust_tick_size_kr(step_price, "buy")
            tick_adjusted = step_price != original_price
        else:
            original_price = None
            tick_adjusted = False

        if market_type == "crypto":
            quantity = step_amount / step_price
        else:
            quantity = int(step_amount / step_price)
            if quantity == 0:
                raise ValueError(

                        f"Amount {step_amount:.0f} is insufficient for 1 unit at price {step_price}"

                )

        total_quantity += quantity
        distance_pct = round((step_price - current_price) / current_price * 100, 2)

        plans.append(
            {
                "step": step,
                "price": round(step_price, 2),
                "distance_pct": distance_pct,
                "amount": round(step_amount, 0),
                "quantity": round(quantity, 8)
                if market_type == "crypto"
                else quantity,
                "source": level_source,
            }
        )
        if tick_adjusted and original_price is not None:
            plans[-1]["original_price"] = round(original_price, 2)
            plans[-1]["tick_adjusted"] = True

    avg_target_price = sum(p["price"] for p in plans) / len(plans)
    min_dist = min(p["distance_pct"] for p in plans)
    max_dist = max(p["distance_pct"] for p in plans)

    summary = {
        "symbol": normalized_symbol,
        "current_price": current_price,
        "rsi_14": rsi_value,
        "strategy": strategy,
        "total_amount": total_amount,
        "avg_target_price": round(avg_target_price, 2),
        "total_quantity": round(total_quantity, 8)
        if market_type == "crypto"
        else int(total_quantity),
        "price_range_pct": f"{min_dist:.2f}% ~ {max_dist:.2f}%",
        "weight_mode": (
            "front_heavy"
            if rsi_value is not None and rsi_value < 30
            else "back_heavy"
            if rsi_value is not None and rsi_value > 50
            else "equal"
        ),
    }

    return plans, summary


async def _persist_dca_plan(
    *,
    normalized_symbol: str,
    market_type: str,
    total_amount: float,
    splits: int,
    strategy: str,
    plans: list[_DcaPlanStepRow],
    rsi_value: float | None,
    user_id: int,
    dca_service_factory: Callable[[Any], Any],
    session_factory: Callable[[], Any],
    logger_obj: Any,
) -> tuple[int | None, dict[int, Any]]:
    plans_for_db = [
        {
            "step": p["step"],
            "price": p["price"],
            "amount": p["amount"],
            "quantity": p["quantity"],
            "source": p.get("source"),
        }
        for p in plans
    ]

    try:
        async with session_factory() as db:
            dca_service = dca_service_factory(db)
            created_plan = await dca_service.create_plan(
                user_id=user_id,
                symbol=normalized_symbol,
                market=market_type,
                total_amount=total_amount,
                splits=splits,
                strategy=strategy,
                plans_data=plans_for_db,
                rsi_14=rsi_value,
            )

            plan_id = created_plan.id
            async with session_factory() as reload_db:
                reload_service = dca_service_factory(reload_db)
                reloaded_plan = await reload_service.get_plan(plan_id, user_id)
                if not reloaded_plan:
                    raise ValueError(f"Plan {plan_id} not found after creation")

                created_plan_steps: dict[int, Any] = {}
                for step in reloaded_plan.steps or []:
                    created_plan_steps[step.step_number] = step
                return plan_id, created_plan_steps
    except Exception as exc:
        logger_obj.error("Failed to persist DCA plan: %s", exc)
        raise


def _extract_order_id(order_result: dict[str, Any]) -> str | None:
    if "order_id" in order_result:
        order_id = order_result.get("order_id")
        if isinstance(order_id, str) and order_id:
            return order_id

    execution = order_result.get("execution")
    if isinstance(execution, dict):
        for key in ("uuid", "ord_no", "odno"):
            candidate = execution.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate

    return None


async def _execute_dca_plan_steps(
    *,
    plans: list[_DcaPlanStepRow],
    splits: int,
    execute_steps: list[int] | None,
    normalized_symbol: str,
    should_execute: bool,
    plan_id: int | None,
    created_plan_steps: dict[int, Any],
    place_order_fn: Callable[..., Awaitable[dict[str, Any]]],
    mark_step_ordered_fn: Callable[[Any, int], Awaitable[None]],
    session_factory: Callable[[], Any],
    logger_obj: Any,
) -> tuple[list[_DcaPlanStepRow], list[int], dict[str, Any] | None]:
    execution_results: list[_DcaPlanStepRow] = []
    executed_steps: list[int] = []

    for plan_step in plans:
        if execute_steps is not None and plan_step["step"] not in execute_steps:
            continue

        order_amount = plan_step["amount"]
        order_price = plan_step["price"]

        if order_amount > 1_000_000:
            return execution_results, executed_steps, {
                "success": False,
                "error": (
                    f"Step {plan_step['step']} amount {order_amount:.0f} KRW exceeds limit 1,000,000 KRW"
                ),
                "dry_run": not should_execute,
                "executed": bool(executed_steps),
                "plan_id": plan_id,
            }

        order_result = await place_order_fn(
            symbol=normalized_symbol,
            side="buy",
            order_type="limit",
            amount=order_amount,
            price=order_price,
            dry_run=False,
            reason=f"DCA plan step {plan_step['step']}/{splits}",
        )

        execution_results.append(
            {
                "step": plan_step["step"],
                "success": order_result.get("success", False),
                "result": order_result,
            }
        )
        executed_steps.append(plan_step["step"])

        if order_result.get("success") and plan_id is not None:
            order_id = _extract_order_id(order_result)
            if order_id and plan_step["step"] in created_plan_steps:
                step = created_plan_steps[plan_step["step"]]
                try:
                    async with session_factory() as db:
                        await mark_step_ordered_fn(db, step.id, str(order_id))
                except Exception as exc:
                    logger_obj.error("Failed to mark step ordered: %s", exc)
                    return execution_results, executed_steps, {
                        "success": False,
                        "error": f"Failed to mark step ordered: {exc}",
                        "dry_run": not should_execute,
                        "executed": bool(executed_steps),
                        "plan_id": plan_id,
                        "execution_results": execution_results,
                    }
            elif order_id:
                logger_obj.error(
                    "Step %s not found in plan %s - available steps: %s",
                    plan_step["step"],
                    plan_id,
                    list(created_plan_steps.keys()),
                )
                return execution_results, executed_steps, {
                    "success": False,
                    "error": (
                        f"Step {plan_step['step']} not found in plan {plan_id}"
                    ),
                    "dry_run": not should_execute,
                    "plan_id": plan_id,
                    "execution_results": execution_results,
                }

        if not order_result.get("success"):
            return execution_results, executed_steps, {
                "success": False,
                "error": f"Order failed at step {plan_step['step']}",
                "failed_step": plan_step["step"],
                "dry_run": not should_execute,
                "plan_id": plan_id,
                "execution_results": execution_results,
            }

    return execution_results, executed_steps, None


async def create_dca_plan_impl(
    *,
    symbol: str,
    total_amount: float,
    splits: int = 3,
    strategy: str = "support",
    dry_run: bool = True,
    market: str | None = None,
    execute_steps: list[int] | None = None,
    resolve_market_type: Callable[[str, str | None], tuple[str, str]],
    sr_impl: Callable[[str, str | None], Awaitable[dict[str, Any]]],
    indicators_impl: Callable[[str, list[str], str | None], Awaitable[dict[str, Any]]],
    place_order_impl: Callable[..., Awaitable[dict[str, Any]]],
    dca_service_factory: Callable[[Any], Any],
    session_factory: Callable[[], Any],
    logger_obj: Any,
    mcp_dca_user_id: int,
) -> dict[str, Any]:
    market_type, normalized_symbol, source = _validate_create_dca_input(
        symbol=symbol,
        total_amount=total_amount,
        splits=splits,
        strategy=strategy,
        execute_steps=execute_steps,
        resolve_market_type=resolve_market_type,
    )

    total_amount = float(total_amount)

    try:
        sr_result = await sr_impl(normalized_symbol, None)
        if "error" in sr_result:
            return {
                "success": False,
                "error": sr_result["error"],
                "source": sr_result.get("source", "get_support_resistance"),
                "dry_run": dry_run,
            }

        current_price = sr_result.get("current_price")
        if current_price is None:
            raise ValueError("current_price is not available")
        supports = sr_result.get("supports", [])

        indicator_result = await indicators_impl(normalized_symbol, ["rsi"], None)
        if "error" in indicator_result:
            return {
                "success": False,
                "error": indicator_result["error"],
                "source": indicator_result.get("source", "get_indicators"),
                "dry_run": dry_run,
            }

        rsi_data = indicator_result.get("indicators", {}).get("rsi", {})
        rsi_value = rsi_data.get("14") if rsi_data else None

        plans, summary = _build_dca_plan_rows(
            market_type=market_type,
            normalized_symbol=normalized_symbol,
            current_price=float(current_price),
            supports=supports,
            total_amount=total_amount,
            splits=splits,
            strategy=strategy,
            rsi_value=rsi_value,
        )

        should_execute = not dry_run or (execute_steps is not None)
        plan_id: int | None = None
        created_plan_steps: dict[int, Any] = {}

        plan_id, created_plan_steps = await _persist_dca_plan(
            normalized_symbol=normalized_symbol,
            market_type=market_type,
            total_amount=total_amount,
            splits=splits,
            strategy=strategy,
            plans=plans,
            rsi_value=rsi_value,
            user_id=mcp_dca_user_id,
            dca_service_factory=dca_service_factory,
            session_factory=session_factory,
            logger_obj=logger_obj,
        )

        execution_results: list[_DcaPlanStepRow] = []
        executed_steps: list[int] = []

        if should_execute:
            execution_results, executed_steps, error_payload = await _execute_dca_plan_steps(
                plans=plans,
                splits=splits,
                execute_steps=execute_steps,
                normalized_symbol=normalized_symbol,
                should_execute=should_execute,
                plan_id=plan_id,
                created_plan_steps=created_plan_steps,
                place_order_fn=place_order_impl,
                mark_step_ordered_fn=lambda db, step_id, order_id: dca_service_factory(db).mark_step_ordered(
                    step_id,
                    order_id,
                ),
                session_factory=session_factory,
                logger_obj=logger_obj,
            )

            if error_payload is not None:
                error_payload.setdefault("summary", summary)
                return error_payload

        response: dict[str, Any] = {
            "success": True,
            "dry_run": not should_execute,
            "executed": bool(executed_steps),
            "plan_id": plan_id,
            "plans": plans,
            "summary": summary,
        }
        if should_execute:
            response["execution_results"] = execution_results
            if executed_steps:
                response["executed_steps"] = executed_steps

        return response
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "source": source,
            "dry_run": dry_run,
        }
