# =================================================================
# DCA (Dollar Cost Averaging) Helper Functions
# =================================================================


def _compute_rsi_weights(rsi_value: float | None, splits: int) -> list[float]:
    """Compute RSI-based weighting for DCA splits.

    Args:
        rsi_value: RSI value (0-100)
        splits: Number of DCA splits (e.g., 3 for 3-step buying)

    Returns:
        List of weights that sum to 1.0
    """
    return [1.0 / splits] * splits


def _compute_dca_price_levels(
    strategy: str,
    splits: int,
    current_price: float,
    supports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute DCA price levels based on strategy.

    Args:
        strategy: Strategy type ("support", "equal", or "aggressive")
        splits: Number of price levels to generate
        current_price: Current market price
        supports: List of support levels with prices

    Returns:
        List of price level dictionaries with "price" and "source" keys.
    """
    if strategy == "support":
        if len(supports) >= splits:
            return [
                {"price": s["price"], "source": "support"} for s in supports[:splits]
            ]
        else:
            return [{"price": s["price"], "source": "support"} for s in supports]
    elif strategy == "equal":
        step = (supports[0]["price"] - current_price) / (splits - 1)
        return [
            {"price": current_price - (step * i), "source": "equal_spaced"}
            for i in range(splits)
        ]
    elif strategy == "aggressive":
        first_price = current_price * 0.99
        return [{"price": first_price, "source": "aggressive_first"}]
    else:
        raise ValueError(
            f"Invalid strategy: {strategy}. Must be 'support', 'equal', or 'aggressive'"
        )


def _format_dca_plan(plan: Any) -> dict[str, Any]:
    """Format DCA plan for JSON serialization."""
    from decimal import Decimal

    result = {
        "id": plan.id,
        "user_id": plan.user_id,
        "symbol": plan.symbol,
        "market": plan.market,
        "status": plan.status.value
        if hasattr(plan.status, "value")
        else str(plan.status),
        "total_amount": float(plan.total_amount),
        "splits": plan.splits,
        "strategy": plan.strategy,
        "rsi_14": float(plan.rsi_14) if plan.rsi_14 is not None else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
    }

    if hasattr(plan, "steps") and plan.steps:
        result["steps"] = [
            {
                "id": step.id,
                "plan_id": step.plan_id,
                "step_number": step.step_number,
                "target_price": float(step.target_price),
                "target_amount": float(step.target_amount),
                "target_quantity": float(step.target_quantity)
                if step.target_quantity
                else None,
                "status": step.status.value
                if hasattr(step.status, "value")
                else str(step.status),
                "order_id": step.order_id,
                "filled_at": step.filled_at.isoformat() if step.filled_at else None,
                "cancelled_at": step.cancelled_at.isoformat()
                if step.cancelled_at
                else None,
            }
            for step in plan.steps
        ]

    return result


# User ID for DCA operations (TODO: make this configurable)
_MCP_USER_ID = 1


async def _get_dca_status_impl(
    plan_id: int | None = None,
    symbol: str | None = None,
    status: str = "active",
    limit: int = 10,
) -> dict[str, Any]:
    """Query DCA plans with progress aggregation."""
    # Validate status parameter
    valid_statuses = {"active", "completed", "cancelled", "expired", "all"}
    if status not in valid_statuses:
        return {
            "success": False,
            "error": f"Invalid status '{status}'. Must be one of: {', '.join(sorted(valid_statuses))}",
        }

    # Validate limit parameter
    if limit < 1:
        return {
            "success": False,
            "error": f"limit must be at least 1, got: {limit}",
        }

    async with AsyncSessionLocal() as db:
        dca_service = DcaService(db)

        # Priority 1: plan_id
        if plan_id is not None:
            plan = await dca_service.get_plan(plan_id, _MCP_USER_ID)
            if not plan:
                return {
                    "success": False,
                    "error": f"Plan with id {plan_id} not found",
                }
            return {
                "success": True,
                "plans": [_format_dca_plan(plan)],
                "total_plans": 1,
            }

        # Priority 2: symbol + status
        if symbol is not None:
            plans = await dca_service.get_plans_by_status(
                user_id=_MCP_USER_ID,
                symbol=symbol.upper(),
                status=status if status != "all" else None,
                limit=limit,
            )
            return {
                "success": True,
                "plans": [_format_dca_plan(p) for p in plans],
                "total_plans": len(plans),
            }

        # Priority 3: status only
        plans = await dca_service.get_plans_by_status(
            user_id=_MCP_USER_ID,
            status=status if status != "all" else None,
            limit=limit,
        )
        return {
            "success": True,
            "plans": [_format_dca_plan(p) for p in plans],
            "total_plans": len(plans),
        }
