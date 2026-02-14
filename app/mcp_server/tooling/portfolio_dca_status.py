"""DCA status helpers for portfolio MCP tools."""

from __future__ import annotations

from typing import Any

from app.models.dca_plan import DcaPlan, DcaStepStatus


def _resolve_step_status(step: Any) -> str:
    status_value = step.status
    if isinstance(status_value, str):
        return status_value
    return str(status_value.value)


def _format_dca_plan(plan: DcaPlan) -> dict[str, Any]:
    p: dict[str, Any] = {
        "plan_id": plan.id,
        "id": plan.id,
        "user_id": plan.user_id,
        "symbol": plan.symbol,
        "market": plan.market,
        "status": plan.status.value if hasattr(plan.status, "value") else str(plan.status),
        "total_amount": float(plan.total_amount)
        if getattr(plan, "total_amount", None) is not None
        else None,
        "splits": plan.splits,
        "strategy": plan.strategy,
        "rsi_14": float(plan.rsi_14) if plan.rsi_14 is not None else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        "completed_at": plan.completed_at.isoformat() if plan.completed_at else None,
    }

    steps_list: list[dict[str, Any]] = []
    total_steps = 0
    counts = {
        "filled": 0,
        "ordered": 0,
        "pending": 0,
        "cancelled": 0,
        "partial": 0,
        "skipped": 0,
    }
    invested = 0.0
    filled_qty_total = 0.0
    filled_price_weighted = 0.0

    if getattr(plan, "steps", None):
        for step in plan.steps:
            total_steps += 1
            status_name = _resolve_step_status(step)
            if status_name == DcaStepStatus.FILLED.value:
                counts["filled"] += 1
            elif status_name == DcaStepStatus.ORDERED.value:
                counts["ordered"] += 1
            elif status_name == DcaStepStatus.PENDING.value:
                counts["pending"] += 1
            elif status_name == DcaStepStatus.CANCELLED.value:
                counts["cancelled"] += 1
            elif status_name == DcaStepStatus.PARTIAL.value:
                counts["partial"] += 1
            elif status_name == DcaStepStatus.SKIPPED.value:
                counts["skipped"] += 1

            filled_amount = (
                float(step.filled_amount)
                if getattr(step, "filled_amount", None) is not None
                else 0.0
            )
            filled_qty = (
                float(step.filled_quantity)
                if getattr(step, "filled_quantity", None) is not None
                else 0.0
            )
            filled_price = (
                float(step.filled_price)
                if getattr(step, "filled_price", None) is not None
                else None
            )
            invested += filled_amount
            if filled_qty and filled_price is not None:
                filled_qty_total += filled_qty
                filled_price_weighted += filled_price * filled_qty

            ordered_at = getattr(step, "ordered_at", None)
            filled_at = getattr(step, "filled_at", None)
            steps_list.append(
                {
                    "id": step.id,
                    "plan_id": step.plan_id,
                    "step": step.step_number,
                    "step_number": step.step_number,
                    "target_price": float(step.target_price)
                    if getattr(step, "target_price", None) is not None
                    else None,
                    "target_amount": float(step.target_amount)
                    if getattr(step, "target_amount", None) is not None
                    else None,
                    "target_quantity": float(step.target_quantity)
                    if getattr(step, "target_quantity", None) is not None
                    else None,
                    "status": status_name,
                    "order_id": step.order_id,
                    "ordered_at": ordered_at.isoformat() if ordered_at is not None else None,
                    "filled_price": float(step.filled_price)
                    if getattr(step, "filled_price", None) is not None
                    else None,
                    "filled_quantity": float(step.filled_quantity)
                    if getattr(step, "filled_quantity", None) is not None
                    else None,
                    "filled_amount": float(step.filled_amount)
                    if getattr(step, "filled_amount", None) is not None
                    else None,
                    "filled_at": filled_at.isoformat() if filled_at is not None else None,
                    "level_source": getattr(step, "level_source", None),
                }
            )

    avg_filled_price = None
    if filled_qty_total > 0:
        avg_filled_price = filled_price_weighted / filled_qty_total

    remaining = None
    if p.get("total_amount") is not None:
        remaining = float(p["total_amount"]) - invested

    p["steps"] = steps_list
    p["progress"] = {
        "total_steps": total_steps,
        "filled": counts["filled"],
        "ordered": counts["ordered"],
        "pending": counts["pending"],
        "cancelled": counts["cancelled"],
        "partial": counts["partial"],
        "skipped": counts["skipped"],
        "invested": round(invested, 2),
        "remaining": round(remaining, 2) if remaining is not None else None,
        "avg_filled_price": round(avg_filled_price, 8) if avg_filled_price is not None else None,
    }

    return p


async def get_dca_status_impl(
    plan_id: int | None = None,
    symbol: str | None = None,
    status: str = "active",
    limit: int = 10,
    session_factory: Any = None,
    dca_service_factory: Any = None,
    logger_obj: Any = None,
    mcp_dca_user_id: int = 1,
) -> dict[str, Any]:
    valid_statuses = {"active", "completed", "cancelled", "expired", "all"}
    if status not in valid_statuses:
        return {
            "success": False,
            "error": (
                f"Invalid status '{status}'. Must be one of: {', '.join(sorted(valid_statuses))}"
            ),
            "plans": [],
            "total_plans": 0,
        }
    if limit < 1 or limit > 1000:
        return {
            "success": False,
            "error": f"limit must be between 1 and 1000, got: {limit}",
            "plans": [],
            "total_plans": 0,
        }

    if session_factory is None or dca_service_factory is None:
        return {
            "success": False,
            "error": "DCA service dependencies are not configured",
            "plans": [],
            "total_plans": 0,
        }

    try:
        async with session_factory() as db:
            dca_service = dca_service_factory(db)
            plans: list[DcaPlan] = []

            if plan_id is not None:
                plan = await dca_service.get_plan(plan_id, mcp_dca_user_id)
                if plan:
                    plans = [plan]
            elif symbol is not None:
                symbol = symbol.strip()
                if status == "all":
                    plans = await dca_service.get_plans_by_status(
                        user_id=mcp_dca_user_id,
                        symbol=symbol,
                        status=None,
                        limit=limit,
                    )
                else:
                    plans = await dca_service.get_plans_by_status(
                        user_id=mcp_dca_user_id,
                        symbol=symbol,
                        status=status,
                        limit=limit,
                    )
            else:
                if status == "all":
                    plans = await dca_service.get_plans_by_status(
                        user_id=mcp_dca_user_id,
                        status=None,
                        limit=limit,
                    )
                else:
                    plans = await dca_service.get_plans_by_status(
                        user_id=mcp_dca_user_id,
                        status=status,
                        limit=limit,
                    )

            formatted_plans = [_format_dca_plan(plan) for plan in plans]
            return {
                "success": True,
                "plans": formatted_plans,
                "total_plans": len(formatted_plans),
            }
    except ValueError as ve:
        return {
            "success": False,
            "error": str(ve),
            "plans": [],
            "total_plans": 0,
        }
    except Exception as exc:
        if logger_obj is not None:
            logger_obj.error("Error fetching DCA status: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "plans": [],
            "total_plans": 0,
        }
