from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.watch_alerts import WatchAlertService

if TYPE_CHECKING:
    from fastmcp import FastMCP

WATCH_ALERT_TOOL_NAMES: set[str] = {"manage_watch_alerts"}


async def manage_watch_alerts_impl(
    action: str,
    market: str | None = None,
    target_kind: str | None = None,
    symbol: str | None = None,
    metric: str | None = None,
    operator: str | None = None,
    threshold: float | None = None,
) -> dict:
    service = WatchAlertService()
    try:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"add", "remove", "list"}:
            return {
                "success": False,
                "error": f"Unknown action: {action}",
            }

        if normalized_action == "list":
            listed = await service.list_watches(market=market)
            return {
                "success": True,
                "action": "list",
                "watches": listed,
            }

        if not market or not symbol:
            return {
                "success": False,
                "error": "market and symbol are required for add/remove",
            }

        normalized_metric = str(metric or "").strip().lower()
        normalized_operator = str(operator or "").strip().lower()
        if normalized_metric not in {"price", "rsi", "trade_value"}:
            return {
                "success": False,
                "error": f"Invalid metric: {metric}",
            }
        if normalized_operator not in {"above", "below"}:
            return {
                "success": False,
                "error": f"Invalid operator: {operator}",
            }
        if threshold is None:
            return {
                "success": False,
                "error": "threshold is required for add/remove",
            }

        normalized_market = str(market).strip().lower()
        normalized_symbol = str(symbol).strip().upper()
        condition_type = f"{normalized_metric}_{normalized_operator}"
        normalized_threshold = float(threshold)

        if normalized_action == "add":
            result = await service.add_watch(
                market=market,
                symbol=symbol,
                condition_type=condition_type,
                threshold=normalized_threshold,
                target_kind=target_kind,
            )
            return {
                "success": True,
                "action": "add",
                **result,
                "target_kind": str(target_kind or "asset").strip().lower(),
                "market": normalized_market,
                "symbol": normalized_symbol,
                "condition_type": condition_type,
                "threshold": normalized_threshold,
            }

        result = await service.remove_watch(
            market=market,
            symbol=symbol,
            condition_type=condition_type,
            threshold=normalized_threshold,
            target_kind=target_kind,
        )
        return {
            "success": True,
            "action": "remove",
            **result,
            "target_kind": str(target_kind or "asset").strip().lower(),
            "market": normalized_market,
            "symbol": normalized_symbol,
            "condition_type": condition_type,
            "threshold": normalized_threshold,
        }
    except ValueError as exc:
        return {
            "success": False,
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"manage_watch_alerts failed: {exc}",
        }
    finally:
        await service.close()


def register_watch_alert_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="manage_watch_alerts",
        description=(
            "Manage watch alerts by action=add/remove/list. "
            "add/remove require market,symbol,metric,operator,threshold. "
            "list optionally accepts market."
        ),
    )(manage_watch_alerts_impl)


__all__ = [
    "WATCH_ALERT_TOOL_NAMES",
    "manage_watch_alerts_impl",
    "register_watch_alert_tools",
]
